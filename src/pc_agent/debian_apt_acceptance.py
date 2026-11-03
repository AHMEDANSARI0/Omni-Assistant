"""Live Debian/Ubuntu APT install acceptance evidence.

Phase 50 could build and verify a static APT repository and its automated
oracle could make APT download the package, but it deliberately did not
install anything.  Phase 57 is the direct-only live evidence operation:

* verify the Phase 50 repository and Phase 47 ``.deb`` again;
* point one bounded APT process at a temporary source list and isolated
  APT state/cache without editing the host's APT source files;
* require the host package to be absent before the test;
* run ``apt-get update`` and inspect the exact candidate stanza;
* install the current package with administrator privileges;
* verify dpkg state and every installed file against the verified package
  manifest; and
* purge only this package and prove it is absent again.

Dependencies installed by APT are not auto-removed.  A signed repository is
referenced with its exported ``signed-by`` keyring.  An unsigned local file
repository is referenced with ``trusted=yes`` only inside the temporary
acceptance source; the report records that weaker local trust mode rather
than presenting it as production repository trust.  Command output is not
retained, only byte counts and SHA-256 digests.

No planner, task or registered tool can invoke this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings
from .debian_package import (
    DEBIAN_PACKAGE_ARCHITECTURE,
    DEBIAN_PACKAGE_MANIFEST,
    DEBIAN_PACKAGE_NAME,
    verify_debian_package,
)
from .debian_repository import (
    DEBIAN_REPOSITORY_KEYRING,
    verify_debian_repository,
)
from .errors import PcAgentError
from .logging_config import redact_text

DEBIAN_APT_ACCEPTANCE_REPORT = "debian-apt-acceptance-report.json"

_REPORT_FORMAT = 1
_APP_NAME = "Self-Healing PC Agent"
_MAX_REPORT_BYTES = 1_000_000
_MAX_OS_RELEASE_BYTES = 65_536
_MAX_COMMAND_OUTPUT_BYTES = 10_000_000
_MAX_INSTALLED_MANIFEST_BYTES = 1_000_000
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_FINGERPRINT_RE = re.compile(r"[0-9A-F]{40}")
_OS_ID_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_ARCHITECTURE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_SUITE_RE = re.compile(r"[a-z0-9][a-z0-9+.-]{0,63}")
_PACKAGE_VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.+~-]*")
_CHECK_NAMES = (
    "debian_or_ubuntu_host",
    "apt_tools_available",
    "administrator_access",
    "repository_verified",
    "package_absent_before",
    "apt_update",
    "apt_candidate",
    "apt_install",
    "dpkg_installed_state",
    "installed_files_verified",
    "apt_purge_cleanup",
    "package_absent_after",
)
_COMMAND_NAMES = frozenset(
    {
        "apt_architecture",
        "apt_install",
        "apt_policy",
        "apt_purge",
        "apt_show",
        "apt_update",
        "apt_version",
        "dpkg_state_after",
        "dpkg_state_before",
        "dpkg_state_installed",
        "dpkg_version",
        "sudo_check",
    }
)

AptRunner = Callable[
    [Sequence[str], float, Path | None, Mapping[str, str] | None],
    subprocess.CompletedProcess[str],
]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _report_hash(payload: dict[str, Any]) -> str:
    return _sha256_bytes(_canonical_json_bytes(payload))


def _safe_text(value: Any, *, limit: int = 300) -> str:
    text = "" if value is None else str(value)
    cleaned = "".join(
        character
        for character in text.strip()
        if ord(character) >= 32 and ord(character) != 127
    )
    return cleaned[:limit]


def _sanitize_error(
    error: BaseException | str,
    *,
    private_paths: Sequence[Path] = (),
) -> str:
    text = redact_text(str(error))
    for path in private_paths:
        raw = str(path)
        if raw:
            text = text.replace(raw, "[local-path]")
    return _safe_text(text, limit=500) or "Debian APT acceptance failed."


@dataclass(slots=True)
class DebianAptAcceptanceCheck:
    name: str
    status: str = "pending"
    detail: str = ""

    def set(self, status: str, detail: str) -> None:
        self.status = status
        self.detail = _safe_text(detail)

    def to_dict(self) -> dict[str, str]:
        return {"detail": self.detail, "name": self.name, "status": self.status}


@dataclass(frozen=True, slots=True)
class DebianAptCommandRecord:
    name: str
    returncode: int
    stdout_sha256: str
    stderr_sha256: str
    stdout_bytes: int
    stderr_bytes: int
    elapsed_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "elapsed_ms": round(float(self.elapsed_ms), 3),
            "name": self.name,
            "returncode": self.returncode,
            "stderr_bytes": self.stderr_bytes,
            "stderr_sha256": self.stderr_sha256,
            "stdout_bytes": self.stdout_bytes,
            "stdout_sha256": self.stdout_sha256,
        }


@dataclass(frozen=True, slots=True)
class _CommandOutput:
    name: str
    returncode: int
    stdout: str
    stderr: str


def _empty_host() -> dict[str, Any]:
    return {
        "architecture": None,
        "apt_version": None,
        "dpkg_version": None,
        "os_codename": None,
        "os_id": None,
        "os_name": None,
        "os_version_id": None,
    }


def _empty_repository() -> dict[str, Any]:
    return {
        "architectures": [],
        "component": None,
        "file_count": None,
        "manifest_sha256": None,
        "package_architecture": None,
        "package_sha256": None,
        "package_version": None,
        "release_sha256": None,
        "signed": None,
        "signing_key_fingerprint": None,
        "suite": None,
        "total_bytes": None,
        "trust_mode": None,
        "version": None,
    }


def _empty_candidate() -> dict[str, Any]:
    return {
        "architecture": None,
        "filename": None,
        "sha256": None,
        "size_bytes": None,
        "version": None,
    }


def _empty_installed() -> dict[str, Any]:
    return {
        "architecture": None,
        "file_count": None,
        "manifest_sha256": None,
        "package_version": None,
        "payload_version": None,
        "status": None,
        "verified_file_count": None,
    }


@dataclass(slots=True)
class DebianAptAcceptanceReport:
    report_path: Path
    repository_path: Path
    started_at: str
    status: str = "running"
    finished_at: str | None = None
    elapsed_ms: float = 0.0
    error: str | None = None
    host: dict[str, Any] = field(default_factory=_empty_host)
    repository: dict[str, Any] = field(default_factory=_empty_repository)
    apt_candidate: dict[str, Any] = field(default_factory=_empty_candidate)
    installed: dict[str, Any] = field(default_factory=_empty_installed)
    cleanup: dict[str, Any] = field(
        default_factory=lambda: {
            "package_absent_after": False,
            "purge_attempted": False,
        }
    )
    commands: list[DebianAptCommandRecord] = field(default_factory=list)
    checks: list[DebianAptAcceptanceCheck] = field(
        default_factory=lambda: [
            DebianAptAcceptanceCheck(name) for name in _CHECK_NAMES
        ]
    )

    def check(self, name: str) -> DebianAptAcceptanceCheck:
        for check in self.checks:
            if check.name == name:
                return check
        raise KeyError(name)

    def summary(self) -> str:
        return (
            f"status={self.status}; host={self.host.get('os_id')}; "
            f"package={self.repository.get('package_version')}; "
            f"report={self.report_path}"
        )

    def _base_dict(self) -> dict[str, Any]:
        return {
            "app": _APP_NAME,
            "apt_candidate": dict(self.apt_candidate),
            "checks": [check.to_dict() for check in self.checks],
            "cleanup": dict(self.cleanup),
            "commands": [command.to_dict() for command in self.commands],
            "elapsed_ms": round(float(self.elapsed_ms), 3),
            "error": self.error,
            "finished_at": self.finished_at,
            "format": _REPORT_FORMAT,
            "host": dict(self.host),
            "installed": dict(self.installed),
            "operation": "apt-install-verify-purge",
            "platform": "linux",
            "repository": dict(self.repository),
            "started_at": self.started_at,
            "status": self.status,
            "system_package": DEBIAN_PACKAGE_NAME,
            "version": __version__,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._base_dict()
        payload["report_sha256"] = _report_hash(payload)
        return payload

    def write(self) -> None:
        data = (json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        if len(data) > _MAX_REPORT_BYTES:
            raise PcAgentError("Debian APT acceptance report is too large.")
        if self.report_path.exists() or self.report_path.is_symlink():
            raise PcAgentError(
                "Debian APT acceptance report already exists: "
                f"{self.report_path.name}."
            )
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.report_path.with_name(
            f".{self.report_path.name}.tmp-{secrets.token_hex(4)}"
        )
        try:
            temporary.write_bytes(data)
            if self.report_path.exists():
                raise PcAgentError(
                    "Debian APT acceptance report already exists: "
                    f"{self.report_path.name}."
                )
            temporary.replace(self.report_path)
        finally:
            if temporary.exists():
                temporary.unlink()


@dataclass(frozen=True, slots=True)
class DebianAptAcceptanceVerification:
    report_path: Path
    version: str
    status: str
    os_id: str
    architecture: str
    package_version: str
    package_sha256: str
    report_sha256: str

    def summary(self) -> str:
        return (
            f"status={self.status}; version={self.version}; "
            f"host={self.os_id}/{self.architecture}; "
            f"package={self.package_version}; "
            f"report_sha256={self.report_sha256[:16]}..."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "architecture": self.architecture,
            "os_id": self.os_id,
            "package_sha256": self.package_sha256,
            "package_version": self.package_version,
            "platform": "linux",
            "report": str(self.report_path),
            "report_sha256": self.report_sha256,
            "status": self.status,
            "system_package": DEBIAN_PACKAGE_NAME,
            "version": self.version,
        }


def _default_runner(
    argv: Sequence[str],
    timeout_seconds: float,
    cwd: Path | None,
    env: Mapping[str, str] | None,
) -> subprocess.CompletedProcess[str]:
    merged_env = dict(os.environ)
    if env is not None:
        merged_env.update({str(key): str(value) for key, value in env.items()})
    return subprocess.run(
        [str(part) for part in argv],
        cwd=cwd,
        env=merged_env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )


def _run_command(
    report: DebianAptAcceptanceReport,
    *,
    name: str,
    argv: Sequence[str],
    timeout_seconds: float,
    runner: AptRunner,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> _CommandOutput:
    started = time.monotonic()
    try:
        completed = runner(argv, timeout_seconds, cwd, env)
        returncode = int(completed.returncode)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(
            exc.stdout, bytes
        ) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(
            exc.stderr, bytes
        ) else (exc.stderr or f"Command timed out after {timeout_seconds:g} seconds.")
    except OSError as exc:
        returncode = 127
        stdout = ""
        stderr = str(exc)
    stdout_bytes = stdout.encode("utf-8", errors="replace")
    stderr_bytes = stderr.encode("utf-8", errors="replace")
    if len(stdout_bytes) > _MAX_COMMAND_OUTPUT_BYTES:
        stdout = stdout_bytes[:_MAX_COMMAND_OUTPUT_BYTES].decode(
            "utf-8", errors="replace"
        )
    if len(stderr_bytes) > _MAX_COMMAND_OUTPUT_BYTES:
        stderr = stderr_bytes[:_MAX_COMMAND_OUTPUT_BYTES].decode(
            "utf-8", errors="replace"
        )
    elapsed_ms = max(0.0, (time.monotonic() - started) * 1_000)
    report.commands.append(
        DebianAptCommandRecord(
            name=name,
            returncode=returncode,
            stdout_sha256=_sha256_bytes(stdout_bytes),
            stderr_sha256=_sha256_bytes(stderr_bytes),
            stdout_bytes=len(stdout_bytes),
            stderr_bytes=len(stderr_bytes),
            elapsed_ms=elapsed_ms,
        )
    )
    return _CommandOutput(name, returncode, stdout, stderr)


def _command_failure_detail(
    output: _CommandOutput,
    *,
    private_paths: Sequence[Path] = (),
) -> str:
    for text in (output.stderr, output.stdout):
        for line in text.splitlines():
            cleaned = _sanitize_error(line, private_paths=private_paths)
            if cleaned:
                return cleaned
    return f"Command {output.name} exited with status {output.returncode}."


def parse_os_release(text: str) -> dict[str, str]:
    """Parse the small KEY=VALUE subset needed from os-release."""

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            continue
        raw_value = raw_value.strip()
        value = raw_value
        if raw_value.startswith(('"', "'")):
            try:
                parts = shlex.split(raw_value, posix=True)
                value = parts[0] if len(parts) == 1 else ""
            except ValueError:
                value = ""
        values[key] = _safe_text(value, limit=160)
    return values


def _read_os_release(path: Path) -> dict[str, str]:
    candidate = path.expanduser()
    try:
        resolved = candidate.resolve()
        if not resolved.is_file() or resolved.stat().st_size > _MAX_OS_RELEASE_BYTES:
            return {}
        return parse_os_release(
            resolved.read_bytes().decode("utf-8", errors="replace")
        )
    except OSError:
        return {}


def _find_tools() -> dict[str, str | None]:
    return {
        "apt_cache": shutil.which("apt-cache"),
        "apt_get": shutil.which("apt-get"),
        "dpkg": shutil.which("dpkg"),
        "dpkg_query": shutil.which("dpkg-query"),
        "sudo": shutil.which("sudo"),
    }


def _administrator_prefix(
    report: DebianAptAcceptanceReport,
    tools: Mapping[str, str | None],
    *,
    runner: AptRunner,
) -> tuple[str, ...] | None:
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid) and geteuid() == 0:
        return ()
    sudo = tools.get("sudo")
    if not sudo:
        return None
    output = _run_command(
        report,
        name="sudo_check",
        argv=[sudo, "-n", "true"],
        timeout_seconds=15.0,
        runner=runner,
    )
    return (sudo, "-n") if output.returncode == 0 else None


def _first_match(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, flags=re.MULTILINE)
    if match is None:
        return None
    return _safe_text(match.group(1), limit=80) or None


def _detect_host_versions(
    report: DebianAptAcceptanceReport,
    tools: Mapping[str, str | None],
    *,
    runner: AptRunner,
) -> None:
    if tools.get("apt_get"):
        output = _run_command(
            report,
            name="apt_version",
            argv=[str(tools["apt_get"]), "--version"],
            timeout_seconds=15.0,
            runner=runner,
        )
        if output.returncode == 0:
            report.host["apt_version"] = _first_match(
                r"^apt\s+([0-9][^\s(]*)", output.stdout
            )
    if tools.get("dpkg"):
        output = _run_command(
            report,
            name="dpkg_version",
            argv=[str(tools["dpkg"]), "--version"],
            timeout_seconds=15.0,
            runner=runner,
        )
        if output.returncode == 0:
            report.host["dpkg_version"] = _first_match(
                r"\bversion\s+([0-9][^\s(]*)", output.stdout
            )
        output = _run_command(
            report,
            name="apt_architecture",
            argv=[str(tools["dpkg"]), "--print-architecture"],
            timeout_seconds=15.0,
            runner=runner,
        )
        if output.returncode == 0:
            architecture = _safe_text(output.stdout, limit=80)
            if _ARCHITECTURE_RE.fullmatch(architecture):
                report.host["architecture"] = architecture


def _parse_dpkg_state(output: _CommandOutput) -> dict[str, str | None] | None:
    if output.returncode != 0:
        combined = f"{output.stdout}\n{output.stderr}".lower()
        if "no packages found" in combined or "no package found" in combined:
            return None
        return {"architecture": None, "status": "unavailable", "version": None}
    lines = [_safe_text(line, limit=160) for line in output.stdout.splitlines()]
    lines = [line for line in lines if line]
    if len(lines) != 3:
        return {"architecture": None, "status": "unavailable", "version": None}
    return {
        "status": lines[0],
        "version": lines[1] or None,
        "architecture": lines[2] or None,
    }


def _dpkg_state(
    report: DebianAptAcceptanceReport,
    *,
    command_name: str,
    dpkg_query: str,
    runner: AptRunner,
) -> dict[str, str | None] | None:
    output = _run_command(
        report,
        name=command_name,
        argv=[
            dpkg_query,
            "-W",
            "-f",
            "${Status}\n${Version}\n${Architecture}\n",
            DEBIAN_PACKAGE_NAME,
        ],
        timeout_seconds=30.0,
        runner=runner,
    )
    return _parse_dpkg_state(output)


def _is_absent_state(state: Mapping[str, str | None] | None) -> bool:
    return state is None or state.get("status") == "unknown ok not-installed"


def _remove_temporary_state(
    work: Path,
    *,
    prefix: Sequence[str],
    runner: AptRunner,
    env: Mapping[str, str],
) -> None:
    """Remove generated APT state, including files created under sudo.

    Privileged APT can leave root-owned cache directories inside the
    unprivileged caller's ``TemporaryDirectory``.  Deleting this exact
    generated directory must not be allowed to destroy the acceptance
    report, so cleanup falls back to the same already-approved sudo
    prefix and ultimately ignores only this temporary-state removal.
    """

    try:
        shutil.rmtree(work)
        return
    except FileNotFoundError:
        return
    except OSError:
        pass
    rm_executable = shutil.which("rm")
    if rm_executable:
        argv = [*prefix, rm_executable, "-rf", "--", str(work)]
        try:
            runner(argv, 60.0, None, env)
        except Exception:  # noqa: BLE001, S110 - report writing takes precedence.
            pass
    shutil.rmtree(work, ignore_errors=True)


def _apt_options(work: Path, sources: Path) -> list[str]:
    state = work / "state"
    cache = work / "cache"
    return [
        "-o",
        f"Dir::State={state}",
        "-o",
        f"Dir::State::lists={state / 'lists'}",
        "-o",
        f"Dir::Cache={cache}",
        "-o",
        f"Dir::Cache::archives={cache / 'archives'}",
        "-o",
        f"Dir::Etc::sourcelist={sources}",
        "-o",
        "APT::Get::List-Cleanup=0",
        "-o",
        "Acquire::Languages=none",
        "-o",
        "DPkg::Lock::Timeout=120",
    ]


def _apt_source_line(repository: Path, info: Any, *, keyring: Path | None) -> str:
    uri = repository.resolve().as_uri()
    if info.signed:
        if keyring is None:
            raise PcAgentError("Signed APT acceptance requires the exported keyring.")
        keyring_text = str(keyring.resolve())
        if any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in keyring_text
        ):
            raise PcAgentError(
                "Signed APT acceptance keyring path contains whitespace."
            )
        options = f"signed-by={keyring_text}"
    else:
        options = "trusted=yes"
    return f"deb [{options}] {uri} {info.suite} {info.component}\n"


def _parse_apt_policy(output: str) -> dict[str, str | None]:
    candidate = _first_match(r"^\s*Candidate:\s*([^\s]+)", output)
    installed = _first_match(r"^\s*Installed:\s*([^\s]+)", output)
    return {"candidate": candidate, "installed": installed}


def _parse_apt_show(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current: str | None = None
    for raw_line in output.splitlines():
        if not raw_line.strip():
            break
        if raw_line.startswith((" ", "\t")) and current is not None:
            fields[current] += "\n" + raw_line.strip()
            continue
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        current = key.strip()
        fields[current] = value.strip()
    return fields


def _verify_installed_files(
    *,
    system_root: Path,
    package_info: Any,
) -> dict[str, Any]:
    manifest_path = system_root / DEBIAN_PACKAGE_MANIFEST
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or manifest_path.stat().st_size > _MAX_INSTALLED_MANIFEST_BYTES
    ):
        raise PcAgentError("Installed Debian package manifest is unavailable.")
    manifest_data = manifest_path.read_bytes()
    manifest_sha256 = _sha256_bytes(manifest_data)
    if manifest_sha256 != package_info.manifest_sha256:
        raise PcAgentError("Installed Debian package manifest hash does not match.")
    try:
        manifest = json.loads(manifest_data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PcAgentError("Installed Debian package manifest is invalid.") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != __version__
        or manifest.get("package_version") != package_info.package_version
        or manifest.get("package") != DEBIAN_PACKAGE_NAME
    ):
        raise PcAgentError("Installed Debian package manifest identity is invalid.")

    verified = 1  # The manifest itself, checked against the package verifier.
    for record in package_info.files:
        relative = str(record["path"])
        if not relative.startswith("usr/") or ".." in Path(relative).parts:
            raise PcAgentError("Installed Debian package record path is unsafe.")
        installed_path = system_root / relative
        if installed_path.is_symlink() or not installed_path.is_file():
            raise PcAgentError(
                f"Installed Debian package file is missing: {relative}."
            )
        data = installed_path.read_bytes()
        if len(data) != record["size"] or _sha256_bytes(data) != record["sha256"]:
            raise PcAgentError(
                f"Installed Debian package file hash does not match: {relative}."
            )
        if os.name == "posix":
            actual_mode = installed_path.stat().st_mode & 0o777
            if actual_mode != int(record["mode"]) & 0o777:
                raise PcAgentError(
                    f"Installed Debian package file mode does not match: {relative}."
                )
        verified += 1
    return {
        "file_count": int(package_info.file_count),
        "manifest_sha256": manifest_sha256,
        "package_version": str(manifest["package_version"]),
        "payload_version": str(manifest["version"]),
        "verified_file_count": verified,
    }


def _resolve_report_path(
    settings: Settings, report_path: Path | None
) -> Path:
    if report_path is None:
        stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
        candidate = (
            settings.data_dir
            / "debian-apt-acceptance"
            / f"debian-apt-acceptance-{stamp}.json"
        )
    else:
        candidate = report_path.expanduser()
    if candidate.suffix.lower() != ".json":
        raise PcAgentError("Debian APT acceptance report output must end with .json.")
    if candidate.exists() or candidate.is_symlink():
        raise PcAgentError(
            f"Debian APT acceptance report already exists: {candidate.name}."
        )
    return candidate.resolve()


def _finish_report(
    report: DebianAptAcceptanceReport,
    *,
    started_monotonic: float,
) -> DebianAptAcceptanceReport:
    for check in report.checks:
        if check.status == "pending":
            check.set("skipped", "This check was not reached.")
    report.finished_at = _format_time(_utc_now())
    report.elapsed_ms = max(0.0, (time.monotonic() - started_monotonic) * 1_000)
    all_passed = all(check.status == "passed" for check in report.checks)
    report.status = "passed" if all_passed and report.error is None else "failed"
    if report.status != "passed" and report.error is None:
        report.error = "One or more Debian APT acceptance checks failed."
    report.write()
    return report


def run_debian_apt_acceptance(
    settings: Settings,
    repository: Path,
    report_path: Path | None = None,
    *,
    gpg_executable: str = "gpg",
    runner: AptRunner | None = None,
    platform_name: str | None = None,
    os_release_path: Path = Path("/etc/os-release"),
    system_root: Path = Path("/"),
) -> DebianAptAcceptanceReport:
    """Install, verify and purge the current package on Debian/Ubuntu.

    The package must be absent before this direct operator action.  Failed
    gates still retain a report.  If installation is attempted, purge is
    attempted in a ``finally`` block.  Runner/platform/path injection is for
    deterministic tests; production uses the live host.
    """

    settings.ensure_directories()
    destination = _resolve_report_path(settings, report_path)
    repository_root = repository.expanduser().resolve()
    started = _utc_now()
    started_monotonic = time.monotonic()
    report = DebianAptAcceptanceReport(
        report_path=destination,
        repository_path=repository_root,
        started_at=_format_time(started),
    )
    private_paths = [repository_root]
    active_runner = runner or _default_runner
    command_env = {
        "APT_LISTCHANGES_FRONTEND": "none",
        "DEBIAN_FRONTEND": "noninteractive",
        "LC_ALL": "C",
    }

    is_linux = (platform_name or sys.platform).startswith("linux")
    os_release = _read_os_release(os_release_path) if is_linux else {}
    os_id = os_release.get("ID", "").strip().lower() or None
    report.host.update(
        {
            "os_codename": os_release.get("VERSION_CODENAME") or None,
            "os_id": os_id,
            "os_name": os_release.get("NAME") or None,
            "os_version_id": os_release.get("VERSION_ID") or None,
        }
    )
    host_ok = is_linux and os_id in {"debian", "ubuntu"}
    report.check("debian_or_ubuntu_host").set(
        "passed" if host_ok else "failed",
        f"The live host is {os_release.get('PRETTY_NAME') or os_id}."
        if host_ok
        else "Live APT acceptance requires a Debian or Ubuntu Linux host.",
    )
    if not host_ok:
        report.error = "Debian APT acceptance was not attempted on this host."
        return _finish_report(report, started_monotonic=started_monotonic)

    tools = _find_tools()
    tools_ok = all(
        tools.get(name) for name in ("apt_get", "apt_cache", "dpkg", "dpkg_query")
    )
    if tools_ok:
        _detect_host_versions(report, tools, runner=active_runner)
        tools_ok = bool(
            report.host.get("architecture")
            and report.host.get("apt_version")
            and report.host.get("dpkg_version")
        )
    report.check("apt_tools_available").set(
        "passed" if tools_ok else "failed",
        "apt-get, apt-cache, dpkg and dpkg-query are available."
        if tools_ok
        else "apt-get, apt-cache, dpkg and dpkg-query are not all available.",
    )
    if not tools_ok:
        report.error = "Debian APT acceptance tools are unavailable."
        return _finish_report(report, started_monotonic=started_monotonic)

    prefix = _administrator_prefix(report, tools, runner=active_runner)
    admin_ok = prefix is not None
    report.check("administrator_access").set(
        "passed" if admin_ok else "failed",
        "The direct operator process has non-interactive administrator access."
        if admin_ok
        else "Root or passwordless sudo access is required for APT install/purge.",
    )
    if not admin_ok:
        report.error = "Debian APT acceptance lacks administrator access."
        return _finish_report(report, started_monotonic=started_monotonic)
    assert prefix is not None

    try:
        repository_info = verify_debian_repository(
            repository_root, gpg_executable=gpg_executable
        )
        package_info = verify_debian_package(repository_info.package_path)
        if (
            repository_info.version != __version__
            or package_info.version != __version__
            or repository_info.package_version != package_info.package_version
            or repository_info.package_sha256 != package_info.package_sha256
            or package_info.architecture != DEBIAN_PACKAGE_ARCHITECTURE
        ):
            raise PcAgentError(
                "The APT repository does not contain the current verified package."
            )
        if report.host["architecture"] not in repository_info.architectures and (
            "all" not in repository_info.architectures
        ):
            raise PcAgentError(
                "The APT repository has no index for this host architecture."
            )
    except PcAgentError as exc:
        report.check("repository_verified").set(
            "failed", "The APT repository or embedded package failed verification."
        )
        report.error = _sanitize_error(exc, private_paths=private_paths)
        return _finish_report(report, started_monotonic=started_monotonic)

    report.repository.update(
        {
            "architectures": list(repository_info.architectures),
            "component": repository_info.component,
            "file_count": repository_info.file_count,
            "manifest_sha256": repository_info.manifest_sha256,
            "package_architecture": package_info.architecture,
            "package_sha256": repository_info.package_sha256,
            "package_version": repository_info.package_version,
            "release_sha256": repository_info.release_sha256,
            "signed": repository_info.signed,
            "signing_key_fingerprint": repository_info.signing_key_fingerprint,
            "suite": repository_info.suite,
            "total_bytes": repository_info.total_bytes,
            "trust_mode": (
                "signed-by-exported-keyring"
                if repository_info.signed
                else "trusted-yes-local-acceptance-only"
            ),
            "version": repository_info.version,
        }
    )
    report.check("repository_verified").set(
        "passed",
        "The complete APT repository and embedded current .deb verified offline.",
    )

    assert tools["dpkg_query"] is not None
    before_state = _dpkg_state(
        report,
        command_name="dpkg_state_before",
        dpkg_query=str(tools["dpkg_query"]),
        runner=active_runner,
    )
    absent_before = _is_absent_state(before_state)
    report.check("package_absent_before").set(
        "passed" if absent_before else "failed",
        "The acceptance package was absent before installation."
        if absent_before
        else "The package already exists; acceptance will not replace or purge it.",
    )
    if not absent_before:
        report.error = (
            "Debian APT acceptance refuses to modify a pre-existing package."
        )
        return _finish_report(report, started_monotonic=started_monotonic)

    install_attempted = False
    with tempfile.TemporaryDirectory(
        prefix="pc-agent-apt-acceptance-"
    ) as temporary_name:
        work = Path(temporary_name)
        private_paths.append(work)
        try:
            (work / "state" / "lists" / "partial").mkdir(parents=True)
            (work / "cache" / "archives" / "partial").mkdir(parents=True)
            keyring_copy: Path | None = None
            if repository_info.signed:
                source_keyring = repository_root / DEBIAN_REPOSITORY_KEYRING
                if source_keyring.is_symlink() or not source_keyring.is_file():
                    raise PcAgentError(
                        "Signed APT repository keyring is unavailable."
                    )
                keyring_copy = work / DEBIAN_REPOSITORY_KEYRING
                keyring_copy.write_bytes(source_keyring.read_bytes())
            sources = work / "sources.list"
            sources.write_text(
                _apt_source_line(
                    repository_root, repository_info, keyring=keyring_copy
                ),
                encoding="utf-8",
            )
            options = _apt_options(work, sources)
            assert tools["apt_get"] is not None
            assert tools["apt_cache"] is not None
            apt_get = str(tools["apt_get"])
            apt_cache = str(tools["apt_cache"])

            update = _run_command(
                report,
                name="apt_update",
                argv=[apt_get, *options, "update"],
                timeout_seconds=600.0,
                runner=active_runner,
                env=command_env,
            )
            update_ok = update.returncode == 0
            report.check("apt_update").set(
                "passed" if update_ok else "failed",
                "APT updated from the temporary acceptance source list."
                if update_ok
                else _command_failure_detail(update, private_paths=private_paths),
            )
            if not update_ok:
                report.error = "Live APT update failed."
                return _finish_report(report, started_monotonic=started_monotonic)

            policy_output = _run_command(
                report,
                name="apt_policy",
                argv=[apt_cache, *options, "policy", DEBIAN_PACKAGE_NAME],
                timeout_seconds=60.0,
                runner=active_runner,
                env=command_env,
            )
            show_output = _run_command(
                report,
                name="apt_show",
                argv=[apt_cache, *options, "show", DEBIAN_PACKAGE_NAME],
                timeout_seconds=60.0,
                runner=active_runner,
                env=command_env,
            )
            policy = (
                _parse_apt_policy(policy_output.stdout)
                if policy_output.returncode == 0
                else {}
            )
            fields = (
                _parse_apt_show(show_output.stdout)
                if show_output.returncode == 0
                else {}
            )
            expected_filename = repository_info.package_path.relative_to(
                repository_root
            ).as_posix()
            try:
                candidate_size = int(fields.get("Size", ""))
            except ValueError:
                candidate_size = None
            report.apt_candidate.update(
                {
                    "architecture": fields.get("Architecture"),
                    "filename": fields.get("Filename"),
                    "sha256": fields.get("SHA256"),
                    "size_bytes": candidate_size,
                    "version": policy.get("candidate") or fields.get("Version"),
                }
            )
            candidate_ok = (
                policy_output.returncode == 0
                and show_output.returncode == 0
                and policy.get("candidate") == repository_info.package_version
                and policy.get("installed") in {"(none)", None}
                and fields.get("Package") == DEBIAN_PACKAGE_NAME
                and fields.get("Version") == repository_info.package_version
                and fields.get("Architecture") == DEBIAN_PACKAGE_ARCHITECTURE
                and fields.get("Filename") == expected_filename
                and fields.get("SHA256") == repository_info.package_sha256
                and candidate_size == repository_info.package_path.stat().st_size
            )
            report.check("apt_candidate").set(
                "passed" if candidate_ok else "failed",
                "APT selected the exact verified current package stanza."
                if candidate_ok
                else "APT did not select the exact verified current package stanza.",
            )
            if not candidate_ok:
                report.error = "Live APT candidate verification failed."
                return _finish_report(report, started_monotonic=started_monotonic)

            def privileged_apt(arguments: Sequence[str]) -> list[str]:
                apt_argv = [apt_get, *options, *arguments]
                if not prefix:
                    return apt_argv
                return [
                    *prefix,
                    "env",
                    "DEBIAN_FRONTEND=noninteractive",
                    "APT_LISTCHANGES_FRONTEND=none",
                    "LC_ALL=C",
                    *apt_argv,
                ]

            install_attempted = True
            install = _run_command(
                report,
                name="apt_install",
                argv=privileged_apt(
                    [
                        "install",
                        "-y",
                        "--no-install-recommends",
                        "--",
                        DEBIAN_PACKAGE_NAME,
                    ]
                ),
                timeout_seconds=900.0,
                runner=active_runner,
                env=command_env,
            )
            install_ok = install.returncode == 0
            report.check("apt_install").set(
                "passed" if install_ok else "failed",
                "APT installed the exact current package and its dependencies."
                if install_ok
                else _command_failure_detail(install, private_paths=private_paths),
            )
            if not install_ok and report.error is None:
                report.error = "Live APT installation failed."

            if install_ok:
                installed_state = _dpkg_state(
                    report,
                    command_name="dpkg_state_installed",
                    dpkg_query=str(tools["dpkg_query"]),
                    runner=active_runner,
                )
                state_ok = bool(
                    installed_state
                    and installed_state.get("status") == "install ok installed"
                    and installed_state.get("version")
                    == repository_info.package_version
                    and installed_state.get("architecture")
                    == DEBIAN_PACKAGE_ARCHITECTURE
                )
                if installed_state:
                    report.installed.update(
                        {
                            "architecture": installed_state.get("architecture"),
                            "package_version": installed_state.get("version"),
                            "status": installed_state.get("status"),
                        }
                    )
                report.check("dpkg_installed_state").set(
                    "passed" if state_ok else "failed",
                    "dpkg reports the exact current package as installed."
                    if state_ok
                    else "dpkg does not report the exact current package as installed.",
                )
                if not state_ok and report.error is None:
                    report.error = "Installed dpkg state verification failed."

                if state_ok:
                    try:
                        installed_files = _verify_installed_files(
                            system_root=system_root,
                            package_info=package_info,
                        )
                        report.installed.update(installed_files)
                        files_ok = (
                            installed_files["verified_file_count"]
                            == package_info.file_count
                        )
                        detail = (
                            "Every installed file matched the verified package "
                            "manifest hashes and modes."
                        )
                    except (OSError, PcAgentError) as exc:
                        files_ok = False
                        detail = _sanitize_error(
                            exc, private_paths=[system_root, *private_paths]
                        )
                        if report.error is None:
                            report.error = detail
                    report.check("installed_files_verified").set(
                        "passed" if files_ok else "failed", detail
                    )
        except (OSError, PcAgentError) as exc:
            if report.error is None:
                report.error = _sanitize_error(exc, private_paths=private_paths)
        finally:
            if install_attempted:
                report.cleanup["purge_attempted"] = True
                # The nested finally runs before TemporaryDirectory cleanup;
                # rebuild the exact privileged argv from values still in scope
                # when they exist.  If setup failed before APT options were
                # created, dpkg state below still records the safe outcome.
                purge_ok = False
                try:
                    apt_get_value = str(tools["apt_get"])
                    options_value = _apt_options(work, work / "sources.list")
                    purge_argv = [
                        apt_get_value,
                        *options_value,
                        "purge",
                        "-y",
                        "--",
                        DEBIAN_PACKAGE_NAME,
                    ]
                    if prefix:
                        purge_argv = [
                            *prefix,
                            "env",
                            "DEBIAN_FRONTEND=noninteractive",
                            "APT_LISTCHANGES_FRONTEND=none",
                            "LC_ALL=C",
                            *purge_argv,
                        ]
                    purge = _run_command(
                        report,
                        name="apt_purge",
                        argv=purge_argv,
                        timeout_seconds=600.0,
                        runner=active_runner,
                        env=command_env,
                    )
                    purge_ok = purge.returncode == 0
                    detail = (
                        "APT purged only the acceptance package."
                        if purge_ok
                        else _command_failure_detail(
                            purge, private_paths=private_paths
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - cleanup evidence.
                    detail = _sanitize_error(exc, private_paths=private_paths)
                report.check("apt_purge_cleanup").set(
                    "passed" if purge_ok else "failed", detail
                )
                if not purge_ok and report.error is None:
                    report.error = "APT purge cleanup failed."

                after_state = _dpkg_state(
                    report,
                    command_name="dpkg_state_after",
                    dpkg_query=str(tools["dpkg_query"]),
                    runner=active_runner,
                )
                absent_after = _is_absent_state(after_state)
                report.cleanup["package_absent_after"] = absent_after
                report.check("package_absent_after").set(
                    "passed" if absent_after else "failed",
                    "dpkg reports the acceptance package absent after purge."
                    if absent_after
                    else "The acceptance package remains present after purge.",
                )
                if not absent_after and report.error is None:
                    report.error = "The acceptance package remains installed."
            else:
                after_state = _dpkg_state(
                    report,
                    command_name="dpkg_state_after",
                    dpkg_query=str(tools["dpkg_query"]),
                    runner=active_runner,
                )
                report.cleanup["package_absent_after"] = _is_absent_state(
                    after_state
                )
            _remove_temporary_state(
                work,
                prefix=prefix,
                runner=active_runner,
                env=command_env,
            )

    return _finish_report(report, started_monotonic=started_monotonic)


def _parse_report_time(report: Mapping[str, Any], name: str) -> datetime:
    value = report.get(name)
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PcAgentError(f"Debian APT acceptance report {name} is invalid.")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise PcAgentError(
            f"Debian APT acceptance report {name} is invalid."
        ) from exc


def _valid_optional_text(value: Any, *, limit: int = 300) -> bool:
    return value is None or (
        isinstance(value, str)
        and bool(value)
        and len(value) <= limit
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def verify_debian_apt_acceptance_report(
    report_path: Path,
) -> DebianAptAcceptanceVerification:
    """Strictly validate a passed Debian/Ubuntu APT acceptance report.

    This verifies the retained report's exact current-version shape and
    self-hash.  It does not rerun APT and cannot prove that a wholly
    fabricated report was produced by a real host.
    """

    path = report_path.expanduser().resolve()
    if path.suffix.lower() != ".json" or not path.is_file() or path.is_symlink():
        raise PcAgentError(f"Debian APT acceptance report was not found: {path}.")
    if path.stat().st_size > _MAX_REPORT_BYTES:
        raise PcAgentError("Debian APT acceptance report exceeds the size limit.")
    try:
        report = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PcAgentError("Debian APT acceptance report is not valid JSON.") from exc

    expected_keys = {
        "app",
        "apt_candidate",
        "checks",
        "cleanup",
        "commands",
        "elapsed_ms",
        "error",
        "finished_at",
        "format",
        "host",
        "installed",
        "operation",
        "platform",
        "report_sha256",
        "repository",
        "started_at",
        "status",
        "system_package",
        "version",
    }
    if not isinstance(report, dict) or set(report) != expected_keys:
        raise PcAgentError("Debian APT acceptance report has an unexpected shape.")
    recorded_hash = report.get("report_sha256")
    if not isinstance(recorded_hash, str) or _SHA256_RE.fullmatch(recorded_hash) is None:
        raise PcAgentError("Debian APT acceptance report hash is invalid.")
    unsigned = dict(report)
    unsigned.pop("report_sha256", None)
    if _report_hash(unsigned) != recorded_hash:
        raise PcAgentError("Debian APT acceptance report hash does not match.")
    if (
        report["app"] != _APP_NAME
        or report["format"] != _REPORT_FORMAT
        or report["operation"] != "apt-install-verify-purge"
        or report["platform"] != "linux"
        or report["status"] != "passed"
        or report["system_package"] != DEBIAN_PACKAGE_NAME
        or report["version"] != __version__
        or report["error"] is not None
    ):
        raise PcAgentError("Debian APT acceptance report is not a passed current run.")
    started = _parse_report_time(report, "started_at")
    finished = _parse_report_time(report, "finished_at")
    if finished < started:
        raise PcAgentError("Debian APT acceptance report times are inconsistent.")
    elapsed = report["elapsed_ms"]
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or elapsed < 0:
        raise PcAgentError("Debian APT acceptance report elapsed time is invalid.")

    checks = report["checks"]
    if not isinstance(checks, list) or len(checks) != len(_CHECK_NAMES):
        raise PcAgentError("Debian APT acceptance report checks are invalid.")
    for expected_name, check in zip(_CHECK_NAMES, checks, strict=True):
        if (
            not isinstance(check, dict)
            or set(check) != {"detail", "name", "status"}
            or check["name"] != expected_name
            or check["status"] != "passed"
            or not _valid_optional_text(check["detail"])
            or check["detail"] is None
        ):
            raise PcAgentError("Debian APT acceptance report checks are invalid.")

    host = report["host"]
    if not isinstance(host, dict) or set(host) != set(_empty_host()):
        raise PcAgentError("Debian APT acceptance report host is invalid.")
    if (
        host["os_id"] not in {"debian", "ubuntu"}
        or not isinstance(host["architecture"], str)
        or _ARCHITECTURE_RE.fullmatch(host["architecture"]) is None
        or not _valid_optional_text(host["apt_version"], limit=80)
        or host["apt_version"] is None
        or not _valid_optional_text(host["dpkg_version"], limit=80)
        or host["dpkg_version"] is None
        or not _valid_optional_text(host["os_codename"], limit=80)
        or not _valid_optional_text(host["os_name"], limit=160)
        or not _valid_optional_text(host["os_version_id"], limit=80)
    ):
        raise PcAgentError("Debian APT acceptance report host is invalid.")

    repository = report["repository"]
    if not isinstance(repository, dict) or set(repository) != set(_empty_repository()):
        raise PcAgentError("Debian APT acceptance report repository is invalid.")
    expected_package_version = f"{__version__}-1"
    if (
        repository["version"] != __version__
        or repository["package_version"] != expected_package_version
        or repository["package_architecture"] != DEBIAN_PACKAGE_ARCHITECTURE
        or repository["component"] != "main"
        or not isinstance(repository["suite"], str)
        or _SUITE_RE.fullmatch(repository["suite"]) is None
        or repository["architectures"] != ["all", "amd64"]
        or isinstance(repository["file_count"], bool)
        or not isinstance(repository["file_count"], int)
        or repository["file_count"] < 1
        or isinstance(repository["total_bytes"], bool)
        or not isinstance(repository["total_bytes"], int)
        or repository["total_bytes"] < 1
        or not isinstance(repository["signed"], bool)
    ):
        raise PcAgentError("Debian APT acceptance report repository is invalid.")
    for key in ("manifest_sha256", "package_sha256", "release_sha256"):
        if not isinstance(repository[key], str) or _SHA256_RE.fullmatch(repository[key]) is None:
            raise PcAgentError("Debian APT acceptance report hash is invalid.")
    if repository["signed"]:
        if (
            repository["trust_mode"] != "signed-by-exported-keyring"
            or not isinstance(repository["signing_key_fingerprint"], str)
            or _FINGERPRINT_RE.fullmatch(repository["signing_key_fingerprint"]) is None
        ):
            raise PcAgentError("Signed Debian APT acceptance report trust is invalid.")
    elif (
        repository["trust_mode"] != "trusted-yes-local-acceptance-only"
        or repository["signing_key_fingerprint"] is not None
    ):
        raise PcAgentError("Unsigned Debian APT acceptance report trust is invalid.")

    candidate = report["apt_candidate"]
    if not isinstance(candidate, dict) or set(candidate) != set(_empty_candidate()):
        raise PcAgentError("Debian APT acceptance report candidate is invalid.")
    if (
        candidate["architecture"] != DEBIAN_PACKAGE_ARCHITECTURE
        or candidate["version"] != expected_package_version
        or candidate["sha256"] != repository["package_sha256"]
        or not isinstance(candidate["filename"], str)
        or not candidate["filename"].startswith("pool/main/s/self-healing-pc-agent/")
        or not candidate["filename"].endswith("_all.deb")
        or isinstance(candidate["size_bytes"], bool)
        or not isinstance(candidate["size_bytes"], int)
        or candidate["size_bytes"] < 1
    ):
        raise PcAgentError("Debian APT acceptance report candidate is invalid.")

    installed = report["installed"]
    if not isinstance(installed, dict) or set(installed) != set(_empty_installed()):
        raise PcAgentError("Debian APT acceptance report installed state is invalid.")
    if (
        installed["architecture"] != DEBIAN_PACKAGE_ARCHITECTURE
        or installed["package_version"] != expected_package_version
        or installed["payload_version"] != __version__
        or installed["status"] != "install ok installed"
        or not isinstance(installed["manifest_sha256"], str)
        or _SHA256_RE.fullmatch(installed["manifest_sha256"]) is None
        or isinstance(installed["file_count"], bool)
        or not isinstance(installed["file_count"], int)
        or installed["file_count"] < 1
        or installed["verified_file_count"] != installed["file_count"]
    ):
        raise PcAgentError("Debian APT acceptance report installed state is invalid.")

    cleanup = report["cleanup"]
    if cleanup != {"package_absent_after": True, "purge_attempted": True}:
        raise PcAgentError("Debian APT acceptance report cleanup is invalid.")

    commands = report["commands"]
    if not isinstance(commands, list) or not commands:
        raise PcAgentError("Debian APT acceptance report commands are invalid.")
    seen: set[str] = set()
    for command in commands:
        if (
            not isinstance(command, dict)
            or set(command)
            != {
                "elapsed_ms",
                "name",
                "returncode",
                "stderr_bytes",
                "stderr_sha256",
                "stdout_bytes",
                "stdout_sha256",
            }
            or command["name"] not in _COMMAND_NAMES
            or command["name"] in seen
            or isinstance(command["returncode"], bool)
            or not isinstance(command["returncode"], int)
            or not isinstance(command["stdout_sha256"], str)
            or _SHA256_RE.fullmatch(command["stdout_sha256"]) is None
            or not isinstance(command["stderr_sha256"], str)
            or _SHA256_RE.fullmatch(command["stderr_sha256"]) is None
            or isinstance(command["stdout_bytes"], bool)
            or not isinstance(command["stdout_bytes"], int)
            or command["stdout_bytes"] < 0
            or isinstance(command["stderr_bytes"], bool)
            or not isinstance(command["stderr_bytes"], int)
            or command["stderr_bytes"] < 0
            or isinstance(command["elapsed_ms"], bool)
            or not isinstance(command["elapsed_ms"], (int, float))
            or command["elapsed_ms"] < 0
        ):
            raise PcAgentError("Debian APT acceptance report commands are invalid.")
        seen.add(str(command["name"]))
    required_commands = {
        "apt_architecture",
        "apt_install",
        "apt_policy",
        "apt_purge",
        "apt_show",
        "apt_update",
        "apt_version",
        "dpkg_state_after",
        "dpkg_state_before",
        "dpkg_state_installed",
        "dpkg_version",
    }
    if not required_commands.issubset(seen):
        raise PcAgentError("Debian APT acceptance report commands are incomplete.")
    by_name = {str(command["name"]): command for command in commands}
    for name in required_commands - {"dpkg_state_after", "dpkg_state_before"}:
        if by_name[name]["returncode"] != 0:
            raise PcAgentError("Debian APT acceptance report command status is invalid.")
    for name in ("dpkg_state_after", "dpkg_state_before"):
        if by_name[name]["returncode"] not in {0, 1}:
            raise PcAgentError("Debian APT acceptance report command status is invalid.")

    return DebianAptAcceptanceVerification(
        report_path=path,
        version=str(report["version"]),
        status=str(report["status"]),
        os_id=str(host["os_id"]),
        architecture=str(host["architecture"]),
        package_version=str(repository["package_version"]),
        package_sha256=str(repository["package_sha256"]),
        report_sha256=recorded_hash,
    )


__all__ = [
    "DEBIAN_APT_ACCEPTANCE_REPORT",
    "DebianAptAcceptanceReport",
    "DebianAptAcceptanceVerification",
    "parse_os_release",
    "run_debian_apt_acceptance",
    "verify_debian_apt_acceptance_report",
]
