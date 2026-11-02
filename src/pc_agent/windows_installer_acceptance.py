"""Guarded live-Windows acceptance for the Phase 38 Inno installer.

The acceptance run builds a fresh hash-verified installer source package,
compiles it with a locally installed Inno Setup compiler, silently installs
it into an isolated acceptance directory, checks the installed CLI/GUI and
Start Menu shortcuts, writes non-secret sentinel user files, silently
uninstalls it, and verifies that those sentinels survive.  A JSON report is
retained as evidence.

This module never downloads Inno Setup, signs an executable, installs into
the product's default application directory, or proceeds when the stable
Inno AppId or Start Menu group already exists.  It is a direct release
operation only; no planner or registered tool can invoke it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .errors import PcAgentError
from .release import _sha256_file
from .windows_installer import (
    WINDOWS_INSTALLER_APP_ID,
    WINDOWS_INSTALLER_APP_NAME,
    WINDOWS_INSTALLER_SCRIPT,
    WindowsInstallerSourceInfo,
    build_windows_installer_source,
    verify_windows_installer_source,
)

WINDOWS_INSTALLER_ACCEPTANCE_REPORT = "windows-installer-acceptance-report.json"

_REPORT_FORMAT = 1
_MAX_REPORT_BYTES = 5_000_000
_MAX_SETUP_BYTES = 250_000_000
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_REQUIRED_STEPS = (
    "preflight",
    "build_source",
    "compile",
    "install",
    "verify_install",
    "create_sentinels",
    "uninstall",
    "verify_uninstall",
)
_REQUIRED_CHECKS = (
    "source_verified",
    "setup_compiled",
    "installed_files_verified",
    "installed_cli_verified",
    "installed_gui_verified",
    "shortcuts_created",
    "registry_created",
    "sentinels_preserved",
    "shortcuts_removed",
    "registry_removed",
    "installed_files_removed",
)
_SHORTCUT_NAMES = (
    "Self-Healing PC Agent.lnk",
    "Self-Healing PC Agent CLI.lnk",
    "Uninstall Self-Healing PC Agent.lnk",
)
_SENTINELS = {
    ".env": (
        "# Phase 46 Windows installer acceptance sentinel\n"
        "PC_AGENT_ACCEPTANCE_SENTINEL=preserved\n"
    ),
    "data/phase46-acceptance-sentinel.txt": (
        "Phase 46 sentinel data must survive Inno uninstall.\n"
    ),
    "workspace/phase46-acceptance-sentinel.txt": (
        "Phase 46 sentinel workspace data must survive Inno uninstall.\n"
    ),
}

ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]
RegistryProbe = Callable[[], Mapping[str, str] | None]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(part) for part in argv],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )


@dataclass(slots=True)
class WindowsInstallerAcceptanceStep:
    name: str
    status: str = "pending"
    detail: str = ""
    exit_code: int | None = None
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 3),
        }


@dataclass(slots=True)
class WindowsInstallerAcceptanceReport:
    report_path: Path
    acceptance_root: Path
    source_dir: Path
    install_dir: Path
    log_dir: Path
    status: str = "running"
    started_at: str = field(default_factory=_utc_now)
    finished_at: str | None = None
    compiler: str | None = None
    source: dict[str, Any] = field(default_factory=dict)
    setup: dict[str, Any] = field(default_factory=dict)
    registry: dict[str, Any] = field(default_factory=dict)
    checks: dict[str, bool] = field(
        default_factory=lambda: {name: False for name in _REQUIRED_CHECKS}
    )
    sentinels: dict[str, dict[str, Any]] = field(default_factory=dict)
    steps: list[WindowsInstallerAcceptanceStep] = field(
        default_factory=lambda: [
            WindowsInstallerAcceptanceStep(name) for name in _REQUIRED_STEPS
        ]
    )
    error: str | None = None

    def summary(self) -> str:
        return (
            f"status={self.status}; version={__version__}; "
            f"report={self.report_path}; install={self.install_dir}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": _REPORT_FORMAT,
            "app": WINDOWS_INSTALLER_APP_NAME,
            "app_id": WINDOWS_INSTALLER_APP_ID,
            "version": __version__,
            "status": self.status,
            "platform": "windows",
            "signed": False,
            "authenticode_status": "not_performed",
            "smart_screen_status": "not_evaluated",
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "paths": {
                "acceptance_root": str(self.acceptance_root),
                "source_dir": str(self.source_dir),
                "install_dir": str(self.install_dir),
                "log_dir": str(self.log_dir),
                "report": str(self.report_path),
            },
            "compiler": self.compiler,
            "source": self.source,
            "setup": self.setup,
            "registry": self.registry,
            "checks": self.checks,
            "sentinels": self.sentinels,
            "steps": [step.to_dict() for step in self.steps],
            "error": self.error,
        }

    def write(self) -> None:
        data = (json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n").encode()
        if len(data) > _MAX_REPORT_BYTES:
            raise PcAgentError("Windows installer acceptance report is too large.")
        temporary = self.report_path.with_suffix(".json.tmp")
        temporary.write_bytes(data)
        temporary.replace(self.report_path)


@dataclass(frozen=True, slots=True)
class WindowsInstallerAcceptanceVerification:
    report_path: Path
    version: str
    status: str
    setup_sha256: str
    source_files: int

    def summary(self) -> str:
        return (
            f"status={self.status}; version={self.version}; "
            f"source_files={self.source_files}; setup_sha256={self.setup_sha256[:16]}..."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "report": str(self.report_path),
            "version": self.version,
            "status": self.status,
            "setup_sha256": self.setup_sha256,
            "source_files": self.source_files,
            "signed": False,
            "authenticode_status": "not_performed",
        }


def windows_uninstall_registry_key() -> str:
    """Return Inno's per-user uninstall subkey for the stable AppId."""

    return (
        r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
        rf"\{{{WINDOWS_INSTALLER_APP_ID}}}_is1"
    )


def read_windows_uninstall_entry() -> dict[str, str] | None:
    """Read the current user's Inno uninstall entry on Windows, if present."""

    if sys.platform != "win32":
        return None
    import winreg

    subkey = windows_uninstall_registry_key()
    access_modes = (
        winreg.KEY_READ,
        winreg.KEY_READ | winreg.KEY_WOW64_32KEY,
        winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
    )
    for access in dict.fromkeys(access_modes):
        try:
            handle = winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, access)
        except FileNotFoundError:
            continue
        with handle:
            entry = {"key": rf"HKEY_CURRENT_USER\{subkey}"}
            for name in (
                "DisplayName",
                "DisplayVersion",
                "InstallLocation",
                "UninstallString",
            ):
                try:
                    value, _kind = winreg.QueryValueEx(handle, name)
                except FileNotFoundError:
                    continue
                entry[name] = str(value)
            return entry
    return None


def default_start_menu_group(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    appdata = env.get("APPDATA")
    base = (
        Path(appdata)
        if appdata
        else Path.home() / "AppData" / "Roaming"
    )
    return (
        base
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / WINDOWS_INSTALLER_APP_NAME
    )


def find_inno_compiler(
    explicit: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Find ISCC.exe locally; never download or install it."""

    env = os.environ if environ is None else environ
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit.expanduser())
    configured = env.get("INNO_SETUP_COMPILER")
    if configured:
        candidates.append(Path(configured).expanduser())
    discovered = shutil.which("ISCC.exe") or shutil.which("iscc")
    if discovered:
        candidates.append(Path(discovered))
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        root = env.get(variable)
        if root:
            candidates.append(Path(root) / "Inno Setup 6" / "ISCC.exe")
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate.resolve()
    return None


def _validate_timeout(name: str, value: float, *, maximum: float = 3600) -> float:
    timeout = float(value)
    if not 0 < timeout <= maximum:
        raise PcAgentError(f"The {name} timeout must be between 0 and {maximum:g} seconds.")
    return timeout


def _validate_acceptance_output(root: Path, output: Path) -> Path:
    destination = output.expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise PcAgentError(
            f"Windows installer acceptance output already exists: {destination}."
        )
    if destination == root or destination in root.parents:
        raise PcAgentError(
            "Windows installer acceptance output must not contain the project root."
        )
    try:
        relative = destination.relative_to(root)
    except ValueError:
        return destination
    if not relative.parts or relative.parts[0] != "workspace":
        raise PcAgentError(
            "Windows installer acceptance output inside the project must be under workspace/."
        )
    return destination


def _same_windows_path(left: str | Path, right: str | Path) -> bool:
    def normalize(value: str | Path) -> str:
        return str(value).replace("/", "\\").rstrip("\\").casefold()

    return normalize(left) == normalize(right)


def _hash_record(path: Path) -> dict[str, Any]:
    return {"path": str(path), "size": path.stat().st_size, "sha256": _sha256_file(path)}


class _AcceptanceRunner:
    def __init__(
        self,
        *,
        root: Path,
        output: Path,
        compiler_request: Path | None,
        environ: Mapping[str, str],
        process_runner: ProcessRunner,
        registry_probe: RegistryProbe,
        start_menu_group: Path,
        compile_timeout: float,
        install_timeout: float,
        uninstall_timeout: float,
        check_timeout: float,
        completion_timeout: float,
        poll_interval: float,
        sleeper: Callable[[float], None],
        clock: Callable[[], float],
    ) -> None:
        self.root = root
        self.output = output
        self.compiler_request = compiler_request
        self.environ = environ
        self.process_runner = process_runner
        self.registry_probe = registry_probe
        self.start_menu_group = start_menu_group
        self.compile_timeout = compile_timeout
        self.install_timeout = install_timeout
        self.uninstall_timeout = uninstall_timeout
        self.check_timeout = check_timeout
        self.completion_timeout = completion_timeout
        self.poll_interval = poll_interval
        self.sleeper = sleeper
        self.clock = clock
        self.source_dir = output / "source"
        self.install_dir = output / "app"
        self.log_dir = output / "logs"
        self.report = WindowsInstallerAcceptanceReport(
            report_path=output / WINDOWS_INSTALLER_ACCEPTANCE_REPORT,
            acceptance_root=output,
            source_dir=self.source_dir,
            install_dir=self.install_dir,
            log_dir=self.log_dir,
        )
        self.compiler: Path | None = None
        self.source_info: WindowsInstallerSourceInfo | None = None
        self.setup_exe: Path | None = None
        self.install_attempted = False
        self.uninstall_completed = False

    def step(self, name: str) -> WindowsInstallerAcceptanceStep:
        for step in self.report.steps:
            if step.name == name:
                return step
        step = WindowsInstallerAcceptanceStep(name)
        self.report.steps.append(step)
        return step

    @contextmanager
    def run_step(self, name: str) -> Iterator[WindowsInstallerAcceptanceStep]:
        step = self.step(name)
        step.status = "running"
        started = self.clock()
        self.report.write()
        try:
            yield step
        except Exception as exc:
            step.status = "failed"
            step.detail = str(exc)
            raise
        else:
            step.status = "passed"
            if not step.detail:
                step.detail = "completed"
        finally:
            step.duration_seconds = max(0.0, self.clock() - started)
            self.report.write()

    def execute(
        self,
        step: WindowsInstallerAcceptanceStep,
        argv: Sequence[str | Path],
        *,
        cwd: Path,
        timeout: float,
        log_name: str,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(part) for part in argv]
        log_path = self.log_dir / log_name
        try:
            completed = self.process_runner(command, cwd=cwd, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            log_path.write_text(
                f"COMMAND: {command!r}\nTIMEOUT: {timeout}\n\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}\n",
                encoding="utf-8",
            )
            raise PcAgentError(
                f"Windows installer acceptance process timed out after {timeout:g} seconds."
            ) from exc
        except OSError as exc:
            raise PcAgentError(
                "Windows installer acceptance process could not be started."
            ) from exc
        step.exit_code = int(completed.returncode)
        log_path.write_text(
            f"COMMAND: {command!r}\nEXIT CODE: {completed.returncode}\n\n"
            f"STDOUT:\n{completed.stdout or ''}\n\nSTDERR:\n{completed.stderr or ''}\n",
            encoding="utf-8",
        )
        if completed.returncode != 0:
            raise PcAgentError(
                f"Windows installer acceptance process failed with exit code {completed.returncode}; see {log_path}."
            )
        return completed

    def wait_until(
        self,
        predicate: Callable[[], bool],
        *,
        timeout: float,
        failure: str,
    ) -> None:
        deadline = self.clock() + timeout
        while True:
            if predicate():
                return
            if self.clock() >= deadline:
                raise PcAgentError(failure)
            self.sleeper(self.poll_interval)

    def shortcut_paths(self) -> tuple[Path, ...]:
        return tuple(self.start_menu_group / name for name in _SHORTCUT_NAMES)

    def uninstaller(self) -> Path | None:
        candidates = sorted(self.install_dir.glob("unins*.exe"))
        return candidates[0] if len(candidates) == 1 else None

    def preflight(self) -> None:
        with self.run_step("preflight") as step:
            self.compiler = find_inno_compiler(self.compiler_request, self.environ)
            if self.compiler is None:
                raise PcAgentError(
                    "Inno Setup 6 compiler (ISCC.exe) was not found. Install it locally or set "
                    "INNO_SETUP_COMPILER; the acceptance harness will not download it."
                )
            self.report.compiler = str(self.compiler)
            existing = self.registry_probe()
            if existing is not None:
                raise PcAgentError(
                    "An Inno installation with the stable Self-Healing PC Agent AppId already "
                    "exists for this Windows user. Refusing acceptance to avoid modifying it."
                )
            if self.start_menu_group.exists():
                raise PcAgentError(
                    "The Self-Healing PC Agent Start Menu group already exists. Refusing "
                    "acceptance to avoid replacing or removing existing shortcuts."
                )
            if self.install_dir.exists():
                raise PcAgentError("The isolated acceptance install directory already exists.")
            step.detail = f"compiler={self.compiler}; AppId and shortcut group are unused"

    def build_source(self) -> None:
        with self.run_step("build_source") as step:
            self.source_info = build_windows_installer_source(self.root, self.source_dir)
            verified = verify_windows_installer_source(self.source_dir)
            self.report.source = {
                "path": str(self.source_dir),
                "file_count": verified.file_count,
                "payload_file_count": verified.payload_file_count,
                "total_bytes": verified.total_bytes,
                "script_sha256": verified.script_sha256,
                "manifest_sha256": verified.manifest_sha256,
            }
            self.report.checks["source_verified"] = True
            step.detail = verified.summary()

    def compile(self) -> None:
        assert self.compiler is not None
        assert self.source_info is not None
        with self.run_step("compile") as step:
            script = self.source_dir / WINDOWS_INSTALLER_SCRIPT
            self.execute(
                step,
                (self.compiler, script),
                cwd=self.source_dir,
                timeout=self.compile_timeout,
                log_name="compile-process.log",
            )
            self.setup_exe = (
                self.source_dir
                / "output"
                / f"SelfHealingPCAgent-Setup-{__version__}.exe"
            )
            if (
                not self.setup_exe.is_file()
                or self.setup_exe.is_symlink()
                or self.setup_exe.stat().st_size <= 0
                or self.setup_exe.stat().st_size > _MAX_SETUP_BYTES
            ):
                raise PcAgentError("Inno Setup did not produce the expected bounded setup EXE.")
            verified = verify_windows_installer_source(self.source_dir)
            if verified.compiled_output_count < 1:
                raise PcAgentError("Compiled setup EXE was not reported by source verification.")
            self.report.setup = _hash_record(self.setup_exe)
            self.report.checks["setup_compiled"] = True
            step.detail = f"setup={self.setup_exe}; sha256={self.report.setup['sha256']}"

    def install(self) -> None:
        assert self.setup_exe is not None
        with self.run_step("install") as step:
            self.install_attempted = True
            inno_log = self.log_dir / "install-inno.log"
            self.execute(
                step,
                (
                    self.setup_exe,
                    "/VERYSILENT",
                    "/SUPPRESSMSGBOXES",
                    "/NORESTART",
                    "/SP-",
                    f"/DIR={self.install_dir}",
                    f"/LOG={inno_log}",
                ),
                cwd=self.output,
                timeout=self.install_timeout,
                log_name="install-process.log",
            )
            self.wait_until(
                lambda: self.uninstaller() is not None
                and (self.install_dir / ".venv" / "Scripts" / "pc-agent.exe").is_file()
                and self.registry_probe() is not None,
                timeout=self.completion_timeout,
                failure="Inno installation did not finish publishing its files and registry entry.",
            )
            step.detail = f"installed into isolated directory {self.install_dir}"

    def verify_install(self) -> None:
        assert self.source_info is not None
        with self.run_step("verify_install") as step:
            payload_records = [
                record
                for record in self.source_info.files
                if record.path.startswith("payload/")
            ]
            for record in payload_records:
                relative = record.path.removeprefix("payload/")
                installed = self.install_dir / relative
                if (
                    not installed.is_file()
                    or installed.is_symlink()
                    or installed.stat().st_size != record.size
                    or _sha256_file(installed) != record.sha256
                ):
                    raise PcAgentError(
                        f"Installed payload file failed hash verification: {relative}."
                    )
            self.report.checks["installed_files_verified"] = True

            if self.uninstaller() is None:
                raise PcAgentError("Exactly one Inno uninstaller was not found after install.")
            missing_shortcuts = [
                str(path) for path in self.shortcut_paths() if not path.is_file()
            ]
            if missing_shortcuts:
                raise PcAgentError(
                    "Inno install did not create every expected Start Menu shortcut: "
                    + ", ".join(missing_shortcuts)
                    + "."
                )
            self.report.checks["shortcuts_created"] = True

            entry = self.registry_probe()
            if entry is None:
                raise PcAgentError("Inno uninstall registry entry was not found after install.")
            self.report.registry = {
                "key": rf"HKEY_CURRENT_USER\{windows_uninstall_registry_key()}",
                "after_install": dict(entry),
            }
            self.report.checks["registry_created"] = True
            display_version = entry.get("DisplayVersion")
            if display_version and display_version != __version__:
                raise PcAgentError("Inno uninstall registry version does not match this build.")
            install_location = entry.get("InstallLocation")
            if install_location and not _same_windows_path(
                install_location, self.install_dir
            ):
                raise PcAgentError(
                    "Inno uninstall registry InstallLocation does not match the isolated directory."
                )

            cli = self.install_dir / ".venv" / "Scripts" / "pc-agent.exe"
            gui = self.install_dir / ".venv" / "Scripts" / "pc-agent-gui.exe"
            version = self.execute(
                step,
                (cli, "--version"),
                cwd=self.install_dir,
                timeout=self.check_timeout,
                log_name="installed-version.log",
            )
            if version.stdout.strip() != f"pc-agent {__version__}":
                raise PcAgentError("Installed CLI reported an unexpected version.")
            doctor = self.execute(
                step,
                (cli, "--root", self.install_dir, "doctor"),
                cwd=self.install_dir,
                timeout=self.check_timeout,
                log_name="installed-doctor.log",
            )
            if f"Self-Healing PC Agent {__version__}" not in doctor.stdout:
                raise PcAgentError("Installed CLI doctor did not report the expected build.")
            self.report.checks["installed_cli_verified"] = True
            gui_check = self.execute(
                step,
                (gui, "--root", self.install_dir, "--check"),
                cwd=self.install_dir,
                timeout=self.check_timeout,
                log_name="installed-gui-check.log",
            )
            if "Status: READY" not in gui_check.stdout:
                raise PcAgentError("Installed GUI check did not report READY status.")
            self.report.checks["installed_gui_verified"] = True
            step.detail = (
                f"payload_files={len(payload_records)}; CLI, GUI, shortcuts and registry verified"
            )

    def create_sentinels(self) -> None:
        with self.run_step("create_sentinels") as step:
            for relative, content in _SENTINELS.items():
                path = self.install_dir / relative
                if path.exists() or path.is_symlink():
                    raise PcAgentError(
                        f"Acceptance sentinel path already exists and will not be overwritten: {relative}."
                    )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="\n")
                self.report.sentinels[relative] = {
                    "size": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
            step.detail = "created .env, data and workspace sentinels with recorded hashes"

    def uninstall(self, *, cleanup: bool = False) -> None:
        name = "cleanup_uninstall" if cleanup else "uninstall"
        with self.run_step(name) as step:
            uninstaller = self.uninstaller()
            if uninstaller is None:
                raise PcAgentError("Exactly one Inno uninstaller was not found.")
            inno_log = self.log_dir / ("cleanup-inno.log" if cleanup else "uninstall-inno.log")
            self.execute(
                step,
                (
                    uninstaller,
                    "/VERYSILENT",
                    "/SUPPRESSMSGBOXES",
                    "/NORESTART",
                    f"/LOG={inno_log}",
                ),
                cwd=self.install_dir,
                timeout=self.uninstall_timeout,
                log_name="cleanup-process.log" if cleanup else "uninstall-process.log",
            )
            self.wait_until(
                lambda: self.registry_probe() is None
                and not any(path.exists() for path in self.shortcut_paths())
                and not (self.install_dir / ".venv").exists()
                and not any(self.install_dir.glob("unins*.exe")),
                timeout=self.completion_timeout,
                failure="Inno uninstall did not finish removing its registry, shortcuts and venv.",
            )
            self.uninstall_completed = True
            step.detail = "silent Inno uninstall completed"

    def verify_uninstall(self) -> None:
        assert self.source_info is not None
        with self.run_step("verify_uninstall") as step:
            remaining_payload = []
            for record in self.source_info.files:
                if not record.path.startswith("payload/"):
                    continue
                relative = record.path.removeprefix("payload/")
                if (self.install_dir / relative).exists():
                    remaining_payload.append(relative)
            if remaining_payload:
                raise PcAgentError(
                    "Inno uninstall left installed payload files behind: "
                    + ", ".join(remaining_payload[:10])
                    + "."
                )
            if (self.install_dir / ".venv").exists() or any(
                self.install_dir.glob("unins*.exe")
            ):
                raise PcAgentError("Inno uninstall left the venv or uninstaller behind.")
            self.report.checks["installed_files_removed"] = True
            if any(path.exists() for path in self.shortcut_paths()):
                raise PcAgentError("Inno uninstall left a Start Menu shortcut behind.")
            self.report.checks["shortcuts_removed"] = True
            if self.registry_probe() is not None:
                raise PcAgentError("Inno uninstall left its registry entry behind.")
            self.report.registry["after_uninstall"] = None
            self.report.checks["registry_removed"] = True
            for relative, expected in self.report.sentinels.items():
                path = self.install_dir / relative
                if (
                    not path.is_file()
                    or path.stat().st_size != expected["size"]
                    or _sha256_file(path) != expected["sha256"]
                ):
                    raise PcAgentError(
                        f"Inno uninstall did not preserve sentinel user file: {relative}."
                    )
            self.report.checks["sentinels_preserved"] = True
            step.detail = "payload removed and every sentinel hash preserved"

    def run(self) -> WindowsInstallerAcceptanceReport:
        self.output.mkdir(parents=True)
        self.log_dir.mkdir()
        self.report.write()
        try:
            self.preflight()
            self.build_source()
            self.compile()
            self.install()
            self.verify_install()
            self.create_sentinels()
            self.uninstall()
            self.verify_uninstall()
        except (PcAgentError, OSError, RuntimeError) as exc:
            self.report.status = "failed"
            self.report.error = str(exc)
            if self.install_attempted and not self.uninstall_completed:
                try:
                    if self.uninstaller() is not None:
                        self.uninstall(cleanup=True)
                except (PcAgentError, OSError, RuntimeError) as cleanup_exc:
                    cleanup = self.step("cleanup_uninstall")
                    cleanup.status = "failed"
                    cleanup.detail = str(cleanup_exc)
            self.report.finished_at = _utc_now()
            self.report.write()
            return self.report
        self.report.status = "passed"
        self.report.finished_at = _utc_now()
        self.report.write()
        return self.report


def run_windows_installer_acceptance(
    root_dir: Path,
    output_dir: Path,
    *,
    compiler: Path | None = None,
    is_windows: bool | None = None,
    environ: Mapping[str, str] | None = None,
    process_runner: ProcessRunner | None = None,
    registry_probe: RegistryProbe | None = None,
    start_menu_group: Path | None = None,
    compile_timeout: float = 600,
    install_timeout: float = 1800,
    uninstall_timeout: float = 900,
    check_timeout: float = 300,
    completion_timeout: float = 300,
    poll_interval: float = 1.0,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> WindowsInstallerAcceptanceReport:
    """Run the guarded live-Windows Inno acceptance sequence."""

    windows = sys.platform == "win32" if is_windows is None else is_windows
    if not windows:
        raise PcAgentError("Windows installer acceptance can run only on Windows.")
    root = root_dir.expanduser().resolve()
    if not (root / "pyproject.toml").is_file() or not (
        root / "scripts" / "windows_installer.iss"
    ).is_file():
        raise PcAgentError("Windows installer acceptance root is not a project checkout.")
    output = _validate_acceptance_output(root, output_dir)
    env = dict(os.environ if environ is None else environ)
    runner = _AcceptanceRunner(
        root=root,
        output=output,
        compiler_request=compiler,
        environ=env,
        process_runner=process_runner or _run_process,
        registry_probe=registry_probe or read_windows_uninstall_entry,
        start_menu_group=(start_menu_group or default_start_menu_group(env)).expanduser(),
        compile_timeout=_validate_timeout("compile", compile_timeout),
        install_timeout=_validate_timeout("install", install_timeout),
        uninstall_timeout=_validate_timeout("uninstall", uninstall_timeout),
        check_timeout=_validate_timeout("installed check", check_timeout),
        completion_timeout=_validate_timeout("completion", completion_timeout),
        poll_interval=_validate_timeout("poll interval", poll_interval, maximum=60),
        sleeper=sleeper,
        clock=clock,
    )
    return runner.run()


def verify_windows_installer_acceptance_report(
    report_path: Path,
    *,
    verify_files: bool = True,
) -> WindowsInstallerAcceptanceVerification:
    """Validate a passed Phase 46 acceptance report and retained artifacts."""

    path = report_path.expanduser().resolve()
    if not path.is_file() or path.is_symlink():
        raise PcAgentError(f"Windows installer acceptance report was not found: {path}.")
    if path.stat().st_size > _MAX_REPORT_BYTES:
        raise PcAgentError("Windows installer acceptance report exceeds the size limit.")
    try:
        report = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PcAgentError("Windows installer acceptance report is not valid JSON.") from exc
    expected_keys = {
        "format",
        "app",
        "app_id",
        "version",
        "status",
        "platform",
        "signed",
        "authenticode_status",
        "smart_screen_status",
        "started_at",
        "finished_at",
        "paths",
        "compiler",
        "source",
        "setup",
        "registry",
        "checks",
        "sentinels",
        "steps",
        "error",
    }
    if not isinstance(report, dict) or set(report) != expected_keys:
        raise PcAgentError("Windows installer acceptance report has an unexpected shape.")
    if report["format"] != _REPORT_FORMAT:
        raise PcAgentError("Windows installer acceptance report format is unsupported.")
    if report["app"] != WINDOWS_INSTALLER_APP_NAME:
        raise PcAgentError("Windows installer acceptance report app is unexpected.")
    if report["app_id"] != WINDOWS_INSTALLER_APP_ID:
        raise PcAgentError("Windows installer acceptance report AppId is unexpected.")
    if report["version"] != __version__:
        raise PcAgentError("Windows installer acceptance report version does not match this build.")
    if report["status"] != "passed" or report["error"] is not None:
        raise PcAgentError("Windows installer acceptance report does not record a passed run.")
    if report["platform"] != "windows":
        raise PcAgentError("Windows installer acceptance report platform is unexpected.")
    if report["signed"] is not False or report["authenticode_status"] != "not_performed":
        raise PcAgentError("Windows installer acceptance report signing status is unexpected.")
    checks = report["checks"]
    if not isinstance(checks, dict) or set(checks) != set(_REQUIRED_CHECKS):
        raise PcAgentError("Windows installer acceptance checks are incomplete.")
    if not all(checks[name] is True for name in _REQUIRED_CHECKS):
        raise PcAgentError("Windows installer acceptance report contains a failed check.")
    steps = report["steps"]
    if not isinstance(steps, list) or [step.get("name") for step in steps[:8]] != list(
        _REQUIRED_STEPS
    ):
        raise PcAgentError("Windows installer acceptance steps are incomplete.")
    if any(
        not isinstance(step, dict) or step.get("status") != "passed" for step in steps[:8]
    ):
        raise PcAgentError("Windows installer acceptance report contains a failed step.")
    setup = report["setup"]
    source = report["source"]
    paths = report["paths"]
    if not isinstance(setup, dict) or not isinstance(source, dict) or not isinstance(paths, dict):
        raise PcAgentError("Windows installer acceptance artifact records are malformed.")
    setup_sha = setup.get("sha256")
    if not isinstance(setup_sha, str) or _SHA256_RE.fullmatch(setup_sha) is None:
        raise PcAgentError("Windows installer acceptance setup hash is invalid.")
    if verify_files:
        source_dir = Path(str(paths.get("source_dir", "")))
        setup_path = Path(str(setup.get("path", "")))
        install_dir = Path(str(paths.get("install_dir", "")))
        verified_source = verify_windows_installer_source(source_dir)
        if verified_source.file_count != source.get("file_count"):
            raise PcAgentError("Windows installer acceptance source count does not match.")
        if verified_source.script_sha256 != source.get("script_sha256"):
            raise PcAgentError("Windows installer acceptance source script hash does not match.")
        if verified_source.manifest_sha256 != source.get("manifest_sha256"):
            raise PcAgentError("Windows installer acceptance source manifest hash does not match.")
        if (
            not setup_path.is_file()
            or setup_path.stat().st_size != setup.get("size")
            or _sha256_file(setup_path) != setup_sha
        ):
            raise PcAgentError("Retained Windows setup EXE does not match its acceptance report.")
        sentinels = report["sentinels"]
        if not isinstance(sentinels, dict) or set(sentinels) != set(_SENTINELS):
            raise PcAgentError("Windows installer acceptance sentinel records are incomplete.")
        for relative, expected in sentinels.items():
            sentinel = install_dir / relative
            if (
                not isinstance(expected, dict)
                or not sentinel.is_file()
                or sentinel.stat().st_size != expected.get("size")
                or _sha256_file(sentinel) != expected.get("sha256")
            ):
                raise PcAgentError(
                    f"Retained installer acceptance sentinel does not match: {relative}."
                )
    return WindowsInstallerAcceptanceVerification(
        report_path=path,
        version=str(report["version"]),
        status=str(report["status"]),
        setup_sha256=setup_sha,
        source_files=int(source.get("file_count", 0)),
    )
