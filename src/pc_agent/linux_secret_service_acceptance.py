"""Live GNOME Keyring/KWallet Secret Service acceptance evidence.

Phase 44 implemented Linux provider-key storage through the freedesktop
Secret Service, but its automated verification deliberately used a fake
``secret-tool`` process.  This direct-only acceptance operation closes the
evidence gap in a running Linux desktop session:

* it identifies the process that owns ``org.freedesktop.secrets`` on the
  session bus and accepts only GNOME Keyring or KWallet/KSecretService;
* it stores one freshly generated, purpose-isolated probe secret through
  the real ``secret-tool`` backend, reads it back, deletes it and proves it
  is gone;
* it snapshots the real provider-key *presence/values in memory only*
  before and after, proving the probe attributes did not disturb them;
* it retains a secret-safe JSON report that can be revalidated offline.

No real provider key is written, replaced, printed or recorded.  The random
probe value also is never recorded; only its length and SHA-256 are in the
report.  No planner, task or registered tool can invoke this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings
from .credentials import (
    _SECRET_SERVICE_ACCEPTANCE_PROVIDER,
    _SECRET_SERVICE_ACCEPTANCE_PURPOSE,
    SUPPORTED_CREDENTIAL_PROVIDERS,
    LinuxSecretServiceBackend,
    build_credential_store,
)
from .errors import CredentialError, PcAgentError
from .logging_config import redact_text

LINUX_SECRET_SERVICE_ACCEPTANCE_REPORT = "linux-secret-service-acceptance-report.json"

_REPORT_FORMAT = 1
_APP_NAME = "Self-Healing PC Agent"
_MAX_REPORT_BYTES = 1_000_000
_DBUS_TIMEOUT_SECONDS = 5.0
_DBUS_MAX_OUTPUT_CHARS = 8_192
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PROBE_ID_RE = re.compile(r"[0-9a-f]{32}")
_PROCESS_NAME_RE = re.compile(r"[A-Za-z0-9._+-]{1,80}")
_BUS_NAME_RE = re.compile(r":[0-9]+\.[0-9]+")
_CHECK_NAMES = (
    "linux_platform",
    "linux_backend_selected",
    "session_bus_present",
    "secret_service_available",
    "provider_is_gnome_or_kwallet",
    "probe_absent_before",
    "real_provider_keys_snapshot",
    "probe_store_and_readback",
    "probe_delete_and_absent_after",
    "real_provider_keys_untouched",
    "secret_not_recorded",
)
_ALLOWED_STORE_MODES = frozenset({"auto", "windows", "linux", "macos", "env"})
_ALLOWED_DETECTIONS = frozenset({"gdbus-owner", "busctl-owner"})


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _report_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _safe_text(value: Any, *, limit: int = 300) -> str:
    text = "" if value is None else str(value)
    cleaned = "".join(
        character
        for character in text.strip()
        if ord(character) >= 32 and ord(character) != 127
    )
    return cleaned[:limit]


def _sanitize_error(error: BaseException | str, secret: str | None) -> str:
    text = redact_text(str(error))
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return _safe_text(text, limit=500) or "Linux Secret Service acceptance failed."


def _desktop_values(environ: Mapping[str, str]) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    for field_name, env_name in (
        ("current_desktop", "XDG_CURRENT_DESKTOP"),
        ("session_desktop", "XDG_SESSION_DESKTOP"),
        ("desktop_session", "DESKTOP_SESSION"),
    ):
        value = _safe_text(environ.get(env_name, ""), limit=80)
        values[field_name] = value or None
    return values


@dataclass(frozen=True, slots=True)
class SecretServiceProviderIdentity:
    kind: str = "unknown"
    process_name: str | None = None
    detection: str = "unavailable"

    def to_dict(self) -> dict[str, Any]:
        return {
            "detection": self.detection,
            "kind": self.kind,
            "process_name": self.process_name,
        }


DbusRunner = Callable[
    [Sequence[str], float], subprocess.CompletedProcess[str]
]
ProviderDetector = Callable[[], SecretServiceProviderIdentity]


def _run_dbus_process(
    argv: Sequence[str], timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(part) for part in argv],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )


def _dbus_output(
    argv: Sequence[str], *, runner: DbusRunner
) -> str | None:
    try:
        completed = runner(argv, _DBUS_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or len(completed.stdout) > _DBUS_MAX_OUTPUT_CHARS:
        return None
    return completed.stdout


def _parse_pid(output: str) -> int | None:
    match = re.search(r"uint32\s+([0-9]+)|\b([0-9]+)\b", output)
    if match is None:
        return None
    return int(match.group(1) or match.group(2))


def _owner_pid_gdbus(executable: str, *, runner: DbusRunner) -> int | None:
    base = [
        executable,
        "call",
        "--session",
        "--dest",
        "org.freedesktop.DBus",
        "--object-path",
        "/org/freedesktop/DBus",
    ]
    owner_output = _dbus_output(
        [
            *base,
            "--method",
            "org.freedesktop.DBus.GetNameOwner",
            "org.freedesktop.secrets",
        ],
        runner=runner,
    )
    if owner_output is None:
        return None
    owner_match = _BUS_NAME_RE.search(owner_output)
    if owner_match is None:
        return None
    pid_output = _dbus_output(
        [
            *base,
            "--method",
            "org.freedesktop.DBus.GetConnectionUnixProcessID",
            owner_match.group(0),
        ],
        runner=runner,
    )
    if pid_output is None:
        return None
    return _parse_pid(pid_output)


def _owner_pid_busctl(executable: str, *, runner: DbusRunner) -> int | None:
    base = [
        executable,
        "--user",
        "call",
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
    ]
    owner_output = _dbus_output(
        [*base, "GetNameOwner", "s", "org.freedesktop.secrets"],
        runner=runner,
    )
    if owner_output is None:
        return None
    owner_match = _BUS_NAME_RE.search(owner_output)
    if owner_match is None:
        return None
    pid_output = _dbus_output(
        [*base, "GetConnectionUnixProcessID", "s", owner_match.group(0)],
        runner=runner,
    )
    if pid_output is None:
        return None
    return _parse_pid(pid_output)


def _process_name(pid: int, proc_root: Path) -> str | None:
    if not 0 < pid <= 10_000_000:
        return None
    process_dir = proc_root / str(pid)
    name = ""
    try:
        raw_cmdline = (process_dir / "cmdline").read_bytes()[:4096]
        first = raw_cmdline.split(b"\0", 1)[0].decode("utf-8", errors="replace")
        name = Path(first).name
    except OSError:
        name = ""
    if not name:
        try:
            name = (process_dir / "comm").read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except OSError:
            return None
    return name if _PROCESS_NAME_RE.fullmatch(name) else None


def _provider_kind(process_name: str | None) -> str:
    if process_name is None:
        return "unknown"
    lower = process_name.lower()
    if lower.startswith("gnome-keyring"):
        return "gnome-keyring"
    if "kwalletd" in lower or lower.startswith("ksecretd"):
        return "kwallet"
    return "unknown"


def detect_secret_service_provider(
    *,
    runner: DbusRunner | None = None,
    which: Callable[[str], str | None] = shutil.which,
    proc_root: Path = Path("/proc"),
) -> SecretServiceProviderIdentity:
    """Identify the session-bus owner of ``org.freedesktop.secrets``.

    Only the owner process basename is considered.  Command stdout is
    bounded, stderr is discarded, no shell is used, and no process argument
    beyond argv[0]'s basename is retained.
    """

    actual_runner = runner or _run_dbus_process
    attempts = (
        ("gdbus", _owner_pid_gdbus, "gdbus-owner"),
        ("busctl", _owner_pid_busctl, "busctl-owner"),
    )
    for command, resolver, detection in attempts:
        executable = which(command)
        if not executable:
            continue
        pid = resolver(executable, runner=actual_runner)
        if pid is None:
            continue
        process_name = _process_name(pid, proc_root)
        if process_name is None:
            continue
        return SecretServiceProviderIdentity(
            kind=_provider_kind(process_name),
            process_name=process_name,
            detection=detection,
        )
    return SecretServiceProviderIdentity()


@dataclass(slots=True)
class LinuxSecretServiceAcceptanceCheck:
    name: str
    status: str = "pending"
    detail: str = ""

    def set(self, status: str, detail: str) -> None:
        self.status = status
        self.detail = _safe_text(detail)

    def to_dict(self) -> dict[str, str]:
        return {"detail": self.detail, "name": self.name, "status": self.status}


@dataclass(slots=True)
class LinuxSecretServiceAcceptanceReport:
    report_path: Path
    started_at: str
    credential_store_mode: str
    desktop: dict[str, str | None]
    session_bus_present: bool
    probe_id: str
    probe_secret_sha256: str
    probe_secret_chars: int
    status: str = "running"
    finished_at: str | None = None
    provider: SecretServiceProviderIdentity = field(
        default_factory=SecretServiceProviderIdentity
    )
    backend_detail: str = ""
    probe_cleaned_up: bool = False
    stored_provider_names_before: tuple[str, ...] | None = None
    stored_provider_names_after: tuple[str, ...] | None = None
    elapsed_ms: float = 0.0
    error: str | None = None
    checks: list[LinuxSecretServiceAcceptanceCheck] = field(
        default_factory=lambda: [
            LinuxSecretServiceAcceptanceCheck(name) for name in _CHECK_NAMES
        ]
    )

    def check(self, name: str) -> LinuxSecretServiceAcceptanceCheck:
        for check in self.checks:
            if check.name == name:
                return check
        raise KeyError(name)

    def summary(self) -> str:
        return (
            f"status={self.status}; provider={self.provider.kind}; "
            f"backend=linux-secret-service; report={self.report_path}"
        )

    def _base_dict(self) -> dict[str, Any]:
        return {
            "app": _APP_NAME,
            "backend": "linux-secret-service",
            "backend_detail": self.backend_detail,
            "checks": [check.to_dict() for check in self.checks],
            "credential_store_mode": self.credential_store_mode,
            "desktop": self.desktop,
            "elapsed_ms": round(float(self.elapsed_ms), 3),
            "error": self.error,
            "finished_at": self.finished_at,
            "format": _REPORT_FORMAT,
            "platform": "linux",
            "probe": {
                "cleaned_up": self.probe_cleaned_up,
                "id": self.probe_id,
                "provider_attribute": _SECRET_SERVICE_ACCEPTANCE_PROVIDER,
                "purpose": _SECRET_SERVICE_ACCEPTANCE_PURPOSE,
                "secret_chars": self.probe_secret_chars,
                "secret_sha256": self.probe_secret_sha256,
            },
            "provider": self.provider.to_dict(),
            "real_provider_keys": {
                "after": (
                    list(self.stored_provider_names_after)
                    if self.stored_provider_names_after is not None
                    else None
                ),
                "before": (
                    list(self.stored_provider_names_before)
                    if self.stored_provider_names_before is not None
                    else None
                ),
                "values_recorded": False,
            },
            "session_bus_present": self.session_bus_present,
            "started_at": self.started_at,
            "status": self.status,
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
            raise PcAgentError("Linux Secret Service acceptance report is too large.")
        if self.report_path.exists():
            raise PcAgentError(
                f"Linux Secret Service acceptance report already exists: {self.report_path.name}."
            )
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.report_path.with_name(
            f".{self.report_path.name}.tmp-{secrets.token_hex(4)}"
        )
        try:
            temporary.write_bytes(data)
            if self.report_path.exists():
                raise PcAgentError(
                    f"Linux Secret Service acceptance report already exists: {self.report_path.name}."
                )
            temporary.replace(self.report_path)
        finally:
            if temporary.exists():
                temporary.unlink()


@dataclass(frozen=True, slots=True)
class LinuxSecretServiceAcceptanceVerification:
    report_path: Path
    version: str
    status: str
    provider_kind: str
    process_name: str
    probe_id: str
    report_sha256: str

    def summary(self) -> str:
        return (
            f"status={self.status}; version={self.version}; "
            f"provider={self.provider_kind}; process={self.process_name}; "
            f"report_sha256={self.report_sha256[:16]}..."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": "linux-secret-service",
            "platform": "linux",
            "probe_id": self.probe_id,
            "process_name": self.process_name,
            "provider_kind": self.provider_kind,
            "report": str(self.report_path),
            "report_sha256": self.report_sha256,
            "status": self.status,
            "version": self.version,
        }


def _resolve_report_path(settings: Settings, report_path: Path | None) -> Path:
    if report_path is None:
        stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
        candidate = (
            settings.data_dir
            / "credential-acceptance"
            / f"linux-secret-service-acceptance-{stamp}.json"
        )
    else:
        candidate = report_path.expanduser()
    if candidate.suffix.lower() != ".json":
        raise PcAgentError(
            "Linux Secret Service acceptance report output must end with .json."
        )
    if candidate.exists():
        raise PcAgentError(
            f"Linux Secret Service acceptance report already exists: {candidate.name}."
        )
    return candidate.resolve()


def _provider_snapshot(
    backend: LinuxSecretServiceBackend,
) -> dict[str, str | None]:
    return {provider: backend.get(provider) for provider in SUPPORTED_CREDENTIAL_PROVIDERS}


def _snapshot_names(snapshot: Mapping[str, str | None]) -> tuple[str, ...]:
    return tuple(
        provider
        for provider in SUPPORTED_CREDENTIAL_PROVIDERS
        if snapshot.get(provider) is not None
    )


def _finish_report(
    report: LinuxSecretServiceAcceptanceReport,
    *,
    started_monotonic: float,
    probe_secret: str,
) -> LinuxSecretServiceAcceptanceReport:
    for check in report.checks:
        if check.status == "pending":
            check.set("skipped", "This check was not reached.")
    report.finished_at = _format_time(_utc_now())
    report.elapsed_ms = max(0.0, (time.monotonic() - started_monotonic) * 1_000)
    preliminary = json.dumps(report._base_dict(), ensure_ascii=False, sort_keys=True)
    if probe_secret and probe_secret in preliminary:
        report.check("secret_not_recorded").set(
            "failed", "Probe secret material would have been present in the report."
        )
        report.error = "Linux Secret Service acceptance failed its secret-safety check."
    else:
        report.check("secret_not_recorded").set(
            "passed", "Neither the probe secret nor any real provider key was recorded."
        )
    all_passed = all(check.status == "passed" for check in report.checks)
    report.status = "passed" if all_passed and report.error is None else "failed"
    if report.status != "passed" and report.error is None:
        report.error = "One or more Linux Secret Service acceptance checks failed."
    report.write()
    return report


def run_linux_secret_service_acceptance(
    settings: Settings,
    report_path: Path | None = None,
    *,
    backend: Any | None = None,
    environ: Mapping[str, str] | None = None,
    provider_detector: ProviderDetector | None = None,
    is_linux: bool | None = None,
) -> LinuxSecretServiceAcceptanceReport:
    """Run the isolated live Secret Service round-trip and retain its report.

    Failed platform/provider/round-trip states still produce a failed
    report.  Only an unsafe or pre-existing report destination raises
    before a report can be written.  ``backend``/``provider_detector``
    injection exists for deterministic tests; production uses the selected
    credential store and D-Bus owner detection.
    """

    destination = _resolve_report_path(settings, report_path)
    started = _utc_now()
    started_monotonic = time.monotonic()
    env = os.environ if environ is None else environ
    mode = _safe_text(settings.credential_store, limit=32).lower() or "auto"
    if mode not in _ALLOWED_STORE_MODES:
        mode = "auto"
    probe_id = secrets.token_hex(16)
    probe_secret = secrets.token_hex(16)
    report = LinuxSecretServiceAcceptanceReport(
        report_path=destination,
        started_at=_format_time(started),
        credential_store_mode=mode,
        desktop=_desktop_values(env),
        session_bus_present=bool(env.get("DBUS_SESSION_BUS_ADDRESS", "").strip()),
        probe_id=probe_id,
        probe_secret_sha256=hashlib.sha256(probe_secret.encode("utf-8")).hexdigest(),
        probe_secret_chars=len(probe_secret),
    )

    platform_is_linux = (
        sys.platform.startswith("linux") if is_linux is None else is_linux
    )
    report.check("linux_platform").set(
        "passed" if platform_is_linux else "failed",
        "The current platform is Linux."
        if platform_is_linux
        else "Linux Secret Service acceptance is available only on Linux.",
    )

    backend_obj = (
        backend
        if backend is not None
        else build_credential_store(settings.credential_store).backend
    )
    backend_is_linux = isinstance(backend_obj, LinuxSecretServiceBackend)
    report.check("linux_backend_selected").set(
        "passed" if backend_is_linux else "failed",
        "The selected credential backend is linux-secret-service."
        if backend_is_linux
        else "The selected credential backend is not linux-secret-service.",
    )
    report.check("session_bus_present").set(
        "passed" if report.session_bus_present else "failed",
        "A desktop session bus address is present; its value was not recorded."
        if report.session_bus_present
        else "No desktop session bus address is present.",
    )

    if not platform_is_linux or not backend_is_linux or not report.session_bus_present:
        report.error = (
            "Linux Secret Service acceptance was not attempted because the "
            "platform, selected backend or desktop session is unavailable."
        )
        return _finish_report(
            report, started_monotonic=started_monotonic, probe_secret=probe_secret
        )

    assert isinstance(backend_obj, LinuxSecretServiceBackend)
    available, detail = backend_obj.probe()
    report.backend_detail = _safe_text(detail)
    report.check("secret_service_available").set(
        "passed" if available else "failed",
        "secret-tool reached the freedesktop Secret Service."
        if available
        else "secret-tool could not reach an unlocked Secret Service.",
    )
    if not available:
        report.error = "Linux Secret Service is unavailable or locked."
        return _finish_report(
            report, started_monotonic=started_monotonic, probe_secret=probe_secret
        )

    detector = provider_detector or detect_secret_service_provider
    try:
        report.provider = detector()
    except Exception as exc:  # noqa: BLE001 - detector failures become report evidence.
        report.provider = SecretServiceProviderIdentity()
        report.error = _sanitize_error(exc, probe_secret)
    provider_ok = report.provider.kind in {"gnome-keyring", "kwallet"}
    report.check("provider_is_gnome_or_kwallet").set(
        "passed" if provider_ok else "failed",
        (
            f"org.freedesktop.secrets is owned by {report.provider.process_name} "
            f"({report.provider.kind}) via {report.provider.detection}."
            if provider_ok
            else "The org.freedesktop.secrets owner could not be proven to be GNOME Keyring or KWallet."
        ),
    )
    if not provider_ok:
        if report.error is None:
            report.error = (
                "Linux Secret Service acceptance did not write a probe because "
                "the Secret Service provider identity was not proven."
            )
        return _finish_report(
            report, started_monotonic=started_monotonic, probe_secret=probe_secret
        )

    snapshot_before: dict[str, str | None] | None = None
    store_attempted = False
    probe_deleted = False
    try:
        try:
            existing_probe = backend_obj.get_acceptance_secret(probe_id)
        except CredentialError as exc:
            report.check("probe_absent_before").set(
                "failed", "The isolated acceptance probe could not be checked."
            )
            report.error = _sanitize_error(exc, probe_secret)
        else:
            absent = existing_probe is None
            report.check("probe_absent_before").set(
                "passed" if absent else "failed",
                "The unique isolated acceptance probe did not already exist."
                if absent
                else "The unique isolated acceptance probe unexpectedly already existed.",
            )
            if not absent:
                report.error = "The acceptance probe identifier collided with an existing item."

        if report.check("probe_absent_before").status == "passed":
            try:
                snapshot_before = _provider_snapshot(backend_obj)
                report.stored_provider_names_before = _snapshot_names(snapshot_before)
                report.check("real_provider_keys_snapshot").set(
                    "passed",
                    "Real provider-key presence was captured in memory only; values were not recorded.",
                )
            except CredentialError as exc:
                report.check("real_provider_keys_snapshot").set(
                    "failed", "Real provider-key presence could not be captured safely."
                )
                report.error = _sanitize_error(exc, probe_secret)

        if snapshot_before is not None:
            store_attempted = True
            try:
                backend_obj.set_acceptance_secret(probe_id, probe_secret)
                returned = backend_obj.get_acceptance_secret(probe_id)
                matched = returned is not None and secrets.compare_digest(
                    returned, probe_secret
                )
                report.check("probe_store_and_readback").set(
                    "passed" if matched else "failed",
                    "The isolated random probe was stored and read back exactly; only its hash was retained."
                    if matched
                    else "The isolated random probe did not read back exactly.",
                )
                if not matched and report.error is None:
                    report.error = "The Linux Secret Service probe read-back did not match."
            except CredentialError as exc:
                report.check("probe_store_and_readback").set(
                    "failed", "The isolated random probe could not be stored/read back."
                )
                report.error = _sanitize_error(exc, probe_secret)
    finally:
        if store_attempted:
            try:
                probe_deleted = backend_obj.delete_acceptance_secret(probe_id)
                absent_after = backend_obj.get_acceptance_secret(probe_id) is None
                report.probe_cleaned_up = bool(probe_deleted and absent_after)
                report.check("probe_delete_and_absent_after").set(
                    "passed" if report.probe_cleaned_up else "failed",
                    "The isolated acceptance probe was deleted and a follow-up lookup found it absent."
                    if report.probe_cleaned_up
                    else "The isolated acceptance probe could not be proven deleted/absent.",
                )
            except CredentialError as exc:
                report.check("probe_delete_and_absent_after").set(
                    "failed", "The isolated acceptance probe cleanup failed."
                )
                if report.error is None:
                    report.error = _sanitize_error(exc, probe_secret)

        if snapshot_before is not None:
            try:
                snapshot_after = _provider_snapshot(backend_obj)
                report.stored_provider_names_after = _snapshot_names(snapshot_after)
                untouched = snapshot_after == snapshot_before
                report.check("real_provider_keys_untouched").set(
                    "passed" if untouched else "failed",
                    "Every real provider-key value compared unchanged in memory; no value or hash was recorded."
                    if untouched
                    else "A real provider-key value changed during acceptance.",
                )
                if not untouched and report.error is None:
                    report.error = "A real provider key changed during Linux Secret Service acceptance."
            except CredentialError as exc:
                report.check("real_provider_keys_untouched").set(
                    "failed", "Real provider-key presence could not be rechecked safely."
                )
                if report.error is None:
                    report.error = _sanitize_error(exc, probe_secret)

    return _finish_report(
        report, started_monotonic=started_monotonic, probe_secret=probe_secret
    )


def _parse_report_time(report: Mapping[str, Any], name: str) -> datetime:
    value = report.get(name)
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PcAgentError(f"Linux Secret Service acceptance report {name} is invalid.")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise PcAgentError(
            f"Linux Secret Service acceptance report {name} is invalid."
        ) from exc


def _valid_desktop_value(value: Any) -> bool:
    return value is None or (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 80
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _valid_provider_names(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    order = {name: index for index, name in enumerate(SUPPORTED_CREDENTIAL_PROVIDERS)}
    return (
        all(isinstance(name, str) and name in order for name in value)
        and len(set(value)) == len(value)
        and [order[name] for name in value] == sorted(order[name] for name in value)
    )


def verify_linux_secret_service_acceptance_report(
    report_path: Path,
) -> LinuxSecretServiceAcceptanceVerification:
    """Strictly validate a passed Linux Secret Service acceptance report.

    This is an integrity/shape check over local operator evidence, not a
    cryptographic attestation by GNOME or KDE.  The self-hash detects
    alteration after writing; it does not make a wholly fabricated file
    impossible.
    """

    path = report_path.expanduser().resolve()
    if path.suffix.lower() != ".json" or not path.is_file() or path.is_symlink():
        raise PcAgentError(f"Linux Secret Service acceptance report was not found: {path}.")
    if path.stat().st_size > _MAX_REPORT_BYTES:
        raise PcAgentError("Linux Secret Service acceptance report exceeds the size limit.")
    try:
        report = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PcAgentError("Linux Secret Service acceptance report is not valid JSON.") from exc

    expected_keys = {
        "app",
        "backend",
        "backend_detail",
        "checks",
        "credential_store_mode",
        "desktop",
        "elapsed_ms",
        "error",
        "finished_at",
        "format",
        "platform",
        "probe",
        "provider",
        "real_provider_keys",
        "report_sha256",
        "session_bus_present",
        "started_at",
        "status",
        "version",
    }
    if not isinstance(report, dict) or set(report) != expected_keys:
        raise PcAgentError("Linux Secret Service acceptance report has an unexpected shape.")

    actual_hash = report.get("report_sha256")
    unsigned = {key: value for key, value in report.items() if key != "report_sha256"}
    if (
        not isinstance(actual_hash, str)
        or _SHA256_RE.fullmatch(actual_hash) is None
        or not secrets.compare_digest(actual_hash, _report_hash(unsigned))
    ):
        raise PcAgentError("Linux Secret Service acceptance report hash does not match.")

    if report["format"] != _REPORT_FORMAT or report["app"] != _APP_NAME:
        raise PcAgentError("Linux Secret Service acceptance report format is unsupported.")
    if report["version"] != __version__:
        raise PcAgentError(
            "Linux Secret Service acceptance report version does not match this build."
        )
    if (
        report["backend"] != "linux-secret-service"
        or report["platform"] != "linux"
        or report["status"] != "passed"
        or report["error"] is not None
        or report["session_bus_present"] is not True
        or report["credential_store_mode"] not in _ALLOWED_STORE_MODES
    ):
        raise PcAgentError(
            "Linux Secret Service acceptance report does not record a passed Linux run."
        )
    backend_detail = report["backend_detail"]
    if (
        not isinstance(backend_detail, str)
        or not backend_detail
        or len(backend_detail) > 300
        or any(ord(character) < 32 or ord(character) == 127 for character in backend_detail)
    ):
        raise PcAgentError("Linux Secret Service acceptance backend detail is invalid.")

    desktop = report["desktop"]
    if not isinstance(desktop, dict) or set(desktop) != {
        "current_desktop",
        "desktop_session",
        "session_desktop",
    } or not all(_valid_desktop_value(value) for value in desktop.values()):
        raise PcAgentError("Linux Secret Service acceptance desktop record is invalid.")

    started = _parse_report_time(report, "started_at")
    finished = _parse_report_time(report, "finished_at")
    if finished < started:
        raise PcAgentError("Linux Secret Service acceptance timestamps are inconsistent.")
    elapsed = report["elapsed_ms"]
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not (0 <= float(elapsed) <= 3_600_000)
    ):
        raise PcAgentError("Linux Secret Service acceptance elapsed time is invalid.")

    checks = report["checks"]
    if not isinstance(checks, list) or len(checks) != len(_CHECK_NAMES):
        raise PcAgentError("Linux Secret Service acceptance checks are incomplete.")
    for expected_name, check in zip(_CHECK_NAMES, checks, strict=True):
        if not isinstance(check, dict) or set(check) != {"detail", "name", "status"}:
            raise PcAgentError("Linux Secret Service acceptance checks are malformed.")
        detail = check["detail"]
        if (
            check["name"] != expected_name
            or check["status"] != "passed"
            or not isinstance(detail, str)
            or not detail
            or len(detail) > 300
            or any(ord(character) < 32 or ord(character) == 127 for character in detail)
        ):
            raise PcAgentError(
                "Linux Secret Service acceptance report contains a failed check."
            )

    provider = report["provider"]
    if not isinstance(provider, dict) or set(provider) != {
        "detection",
        "kind",
        "process_name",
    }:
        raise PcAgentError("Linux Secret Service acceptance provider record is malformed.")
    provider_kind = provider["kind"]
    process_name = provider["process_name"]
    if (
        provider_kind not in {"gnome-keyring", "kwallet"}
        or provider["detection"] not in _ALLOWED_DETECTIONS
        or not isinstance(process_name, str)
        or _PROCESS_NAME_RE.fullmatch(process_name) is None
        or _provider_kind(process_name) != provider_kind
    ):
        raise PcAgentError("Linux Secret Service acceptance provider identity is invalid.")

    probe = report["probe"]
    if not isinstance(probe, dict) or set(probe) != {
        "cleaned_up",
        "id",
        "provider_attribute",
        "purpose",
        "secret_chars",
        "secret_sha256",
    }:
        raise PcAgentError("Linux Secret Service acceptance probe record is malformed.")
    probe_id = probe["id"]
    probe_hash = probe["secret_sha256"]
    if (
        probe["cleaned_up"] is not True
        or not isinstance(probe_id, str)
        or _PROBE_ID_RE.fullmatch(probe_id) is None
        or probe["provider_attribute"] != _SECRET_SERVICE_ACCEPTANCE_PROVIDER
        or probe["purpose"] != _SECRET_SERVICE_ACCEPTANCE_PURPOSE
        or probe["secret_chars"] != 32
        or not isinstance(probe_hash, str)
        or _SHA256_RE.fullmatch(probe_hash) is None
    ):
        raise PcAgentError("Linux Secret Service acceptance probe record is invalid.")

    real_keys = report["real_provider_keys"]
    if not isinstance(real_keys, dict) or set(real_keys) != {
        "after",
        "before",
        "values_recorded",
    }:
        raise PcAgentError("Linux Secret Service acceptance real-key record is malformed.")
    if (
        real_keys["values_recorded"] is not False
        or not _valid_provider_names(real_keys["before"])
        or not _valid_provider_names(real_keys["after"])
        or real_keys["before"] != real_keys["after"]
    ):
        raise PcAgentError("Linux Secret Service acceptance real-key record is invalid.")

    return LinuxSecretServiceAcceptanceVerification(
        report_path=path,
        version=str(report["version"]),
        status=str(report["status"]),
        provider_kind=provider_kind,
        process_name=process_name,
        probe_id=probe_id,
        report_sha256=actual_hash,
    )
