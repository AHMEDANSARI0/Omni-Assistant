"""Build the desktop installer from the command line, then (optionally) sign it.

The Tauri build needs Node, Rust and WebView2, which a Python-only machine does
not have. That is exactly why this module exists: instead of a paragraph of
advice, `pc-agent release installer` answers three questions with facts.

1. **Can this machine build it?** `--check` reports every tool it found (with its
   version) and every tool it did not, plus whether the shell definition itself
   passes preflight. It runs nothing that installs or compiles.
2. **Build it.** `--build` runs `npm ci` then `npm run tauri:build` with an argv
   list, a timeout, and the tail of each output kept - the real build log, not a
   summary of it.
3. **Sign what came out.** `--sign` reuses the Authenticode runbook
   (`sign_bundle`): both the NSIS `.exe` and the MSI are signed and verified, and
   the command fails if any of them would still show "unknown publisher".

Every step appends to `data/installer-build.json`, so the answer to "what did
that command actually do" is a file, not a memory. Nothing here is claimed on a
machine that cannot run it: `--check` on Linux prints the missing tools and exits
non-zero, and no code path pretends a build or a signature happened.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import PcAgentError
from .windows_signing import (
    discover_installer_artifacts,
    plan_signing,
    sign_bundle,
)

__all__ = [
    "INSTALLER_REPORT_NAME",
    "LAUNCHER_NAME",
    "BuildStep",
    "InstallerBuildReport",
    "installer_report_path",
    "launcher_path",
    "next_steps",
    "run_installer_build",
    "toolchain_status",
    "write_launcher",
]

INSTALLER_REPORT_NAME = "installer-build.json"
#: Written next to the project root by ``pc-agent release installer --launcher``.
LAUNCHER_NAME = "Omni-Assistant.cmd"

Runner = Callable[[list[str], int], "subprocess.CompletedProcess[str] | Any"]


@dataclass(frozen=True, slots=True)
class ToolInfo:
    name: str
    found: bool
    path: str = ""
    version: str = ""
    required: bool = True
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "found": self.found,
            "path": self.path,
            "version": self.version,
            "required": self.required,
            "hint": self.hint,
        }


@dataclass
class BuildStep:
    name: str
    status: str  # pass | fail | skip | not-run
    detail: str = ""
    seconds: float = 0.0
    tail: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "seconds": round(self.seconds, 2),
            "tail": self.tail,
        }


@dataclass
class InstallerBuildReport:
    ok: bool
    version: str
    generated_at: str
    platform: str
    shell_dir: str
    steps: list[BuildStep] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    signing: dict[str, Any] = field(default_factory=dict)

    def step(self, name: str) -> BuildStep | None:
        for step in self.steps:
            if step.name == name:
                return step
        return None

    def failures(self) -> list[BuildStep]:
        return [step for step in self.steps if step.status == "fail"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "version": self.version,
            "generated_at": self.generated_at,
            "platform": self.platform,
            "shell_dir": self.shell_dir,
            "steps": [step.to_dict() for step in self.steps],
            "artifacts": self.artifacts,
            "blockers": self.blockers,
            "signing": self.signing,
        }

    def summary(self) -> str:
        passed = sum(1 for step in self.steps if step.status == "pass")
        return (
            f"{'READY' if self.ok else 'ATTENTION'}; steps={len(self.steps)}; "
            f"passed={passed}; failures={len(self.failures())}"
        )


def installer_report_path(root_dir: Path | str) -> Path:
    return Path(root_dir).expanduser() / "data" / INSTALLER_REPORT_NAME


# --------------------------------------------------------------- toolchain

def _tool(name: str, args: tuple[str, ...] = ("--version",), *, hint: str = "",
          required: bool = True) -> ToolInfo:
    executable = shutil.which(name)
    if not executable:
        return ToolInfo(name=name, found=False, required=required, hint=hint)
    version = ""
    try:
        completed = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )
        first = (completed.stdout or completed.stderr or "").strip().splitlines()
        version = first[0].strip() if first else ""
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover - env
        version = f"could not run: {exc}"
    return ToolInfo(name=name, found=True, path=executable, version=version,
                    required=required, hint=hint)


def toolchain_status() -> list[ToolInfo]:
    """What this machine has, with the version string each tool printed."""

    node = _tool("node", hint="install Node.js 20+ (needed only to build the UI)")
    npm = _tool("npm", hint="ships with Node.js")
    cargo = _tool("cargo", hint="install Rust (stable, MSVC toolchain) via rustup")
    rustc = _tool("rustc", hint="install Rust via rustup")
    return [node, npm, cargo, rustc]


def _webview2_present() -> tuple[bool, str]:
    """WebView2 is what the window renders in; Tauri refuses to build without it."""

    if sys.platform != "win32":
        return False, "not applicable off Windows"
    try:  # pragma: no cover - Windows registry
        import winreg
    except ImportError:  # pragma: no cover - defensive
        return False, "winreg unavailable"
    keys = (
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
    )
    for hive, path in keys:  # pragma: no cover - Windows registry
        try:
            with winreg.OpenKey(hive, path) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                return True, str(version)
        except OSError:
            continue
    return False, "install the WebView2 Evergreen runtime"


# --------------------------------------------------- the UI bundle, protected
#
# `npm run build` deletes `src/pc_agent/webui/assets` and `index.html` *before*
# it compiles, and the compile can fail (a type error, a missing node_modules,
# Ctrl+C). When that happened during an installer build the Python package was
# left without its UI - the Command Center then served a 404 instead of the app.
# So `--build` snapshots the bundle first and puts it back when the build fails.
#
# The snapshot lives under `data/`, which is already the install's own scratch
# area and is never packaged.

_SNAPSHOT_PARTS = ("assets", "index.html")


def _webui_dir(root: Path) -> Path:
    return root / "src" / "pc_agent" / "webui"


def _snapshot_ui_bundle(root: Path) -> Path | None:
    """Copy the current command-center bundle aside. Returns the backup dir."""

    source = _webui_dir(root)
    present = [part for part in _SNAPSHOT_PARTS if (source / part).exists()]
    if not present:
        return None
    backup = root / "data" / "ui-bundle-backup"
    shutil.rmtree(backup, ignore_errors=True)
    backup.mkdir(parents=True, exist_ok=True)
    for part in present:
        src = source / part
        dst = backup / part
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    return backup


def _restore_ui_bundle(root: Path, backup: Path | None) -> bool:
    """Put the snapshot back. Returns True when something was restored."""

    if backup is None or not backup.is_dir():
        return False
    target = _webui_dir(root)
    target.mkdir(parents=True, exist_ok=True)
    for part in _SNAPSHOT_PARTS:
        src = backup / part
        if not src.exists():
            continue
        dst = target / part
        if dst.is_dir():
            shutil.rmtree(dst, ignore_errors=True)
        elif dst.exists():
            dst.unlink()
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    shutil.rmtree(backup, ignore_errors=True)
    return True


def _ui_bundle_is_complete(root: Path) -> bool:
    webui = _webui_dir(root)
    return (webui / "index.html").is_file() and (webui / "assets").is_dir()


# --------------------------------------------------------------- launcher
#
# The desktop shell starts the core with `python -m pc_agent api serve` and
# honours OMNI_PYTHON. On a machine with several interpreters (or none on PATH)
# "python" is a coin flip, so the launcher pins the interpreter that actually
# imports this package - the venv the user installed into - and only then starts
# the window. No secrets, no absolute paths baked into the repo: the .cmd is
# generated, and re-generating it is the supported way to move the venv.

_LAUNCHER_HEADER = """@echo off
rem Omni-Assistant Command Center launcher, generated by:
rem   pc-agent release installer --launcher
rem It pins OMNI_PYTHON to the interpreter that imports pc_agent (the venv you
rem installed into) and then starts the desktop shell. Regenerate it after
rem moving or rebuilding that virtual environment. It writes no credentials.
setlocal
set "OMNI_PYTHON={python}"
set "OMNI_ASSISTANT_ROOT={root}"
"""

_LAUNCHER_WITH_EXE = """rem Desktop shell built by pc-agent release installer --build:
if exist "{exe}" (
  start "" "{exe}" %*
  exit /b 0
)
rem The installer was moved or rebuilt - falling back to the built-in window.
"""

_LAUNCHER_NO_EXE = """rem No Tauri executable yet - using the built-in desktop window.
rem Build one with: pc-agent release installer --build
"""

_LAUNCHER_TAIL = '"{python}" -m pc_agent gui %*\nendlocal\n'


def _launcher_text(*, python: str, root: Path, exe: str | None) -> str:
    body = _LAUNCHER_HEADER.format(python=python, root=root)
    body += _LAUNCHER_WITH_EXE.format(exe=exe) if exe else _LAUNCHER_NO_EXE
    body += _LAUNCHER_TAIL.format(python=python)
    # cmd.exe files are CRLF; writing them as LF makes `rem` swallowing visible.
    return body.replace("\n", "\r\n")


def launcher_path(root_dir: Path | str) -> Path:
    return Path(root_dir).expanduser().resolve() / LAUNCHER_NAME


def write_launcher(
    root_dir: Path | str,
    *,
    python: str | None = None,
    executable: str | None = None,
) -> Path:
    """Write (or rewrite) the Windows launcher and return its path.

    ``python`` defaults to the interpreter running this command - the one that
    can import ``pc_agent``. ``executable`` is the built Tauri exe; when it is
    not given (or not built yet) the launcher falls back to the project's own
    ``pc-agent gui`` window, which needs no Rust at all.
    """

    root = Path(root_dir).expanduser().resolve()
    interpreter = python or sys.executable
    text = _launcher_text(python=interpreter, root=root, exe=executable or None)
    target = root / LAUNCHER_NAME
    target.write_text(text, encoding="utf-8", newline="")
    return target


# ------------------------------------------------------------------- build

def _tail(text: str, lines: int = 20) -> list[str]:
    kept = [line for line in (text or "").strip().splitlines() if line.strip()]
    return kept[-lines:]


def _default_runner(argv: list[str], timeout: int) -> Any:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        cwd=None,
        env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"),
    )


def _npm_command() -> str:
    """``npm`` is ``npm.cmd`` on Windows; ``shutil.which`` resolves that for us."""

    for candidate in ("npm.cmd", "npm"):
        found = shutil.which(candidate)
        if found:
            return found
    return "npm"


def _run_step(
    name: str,
    argv: list[str],
    *,
    cwd: Path,
    timeout: int,
    runner: Runner,
) -> BuildStep:
    since = time.perf_counter()
    try:
        completed = runner(argv, timeout)
    except FileNotFoundError as exc:
        return BuildStep(
            name=name,
            status="fail",
            detail=f"could not start {argv[0]}: {exc}",
            seconds=time.perf_counter() - since,
        )
    except subprocess.TimeoutExpired as exc:
        output = f"{exc.stdout or ''}{exc.stderr or ''}"
        return BuildStep(
            name=name,
            status="fail",
            detail=f"timed out after {timeout}s",
            seconds=time.perf_counter() - since,
            tail=_tail(output),
        )
    code = getattr(completed, "returncode", 1)
    output = f"{getattr(completed, 'stdout', '') or ''}{getattr(completed, 'stderr', '') or ''}"
    return BuildStep(
        name=name,
        status="pass" if code == 0 else "fail",
        detail=f"exit {code}",
        seconds=time.perf_counter() - since,
        tail=_tail(output),
    )


def run_installer_build(
    *,
    root_dir: Path | str,
    shell_dir: Path | str | None = None,
    build: bool = False,
    sign: bool = False,
    launcher: bool = False,
    thumbprint: str | None = None,
    pfx: Path | str | None = None,
    timestamp_url: str | None = None,
    timeout: int = 3600,
    runner: Runner | None = None,
    write_report: bool = True,
) -> InstallerBuildReport:
    """Check, build, and optionally sign - reporting every step it took."""

    root = Path(root_dir).expanduser().resolve()
    shell = (
        Path(shell_dir).expanduser().resolve()
        if shell_dir is not None
        else root / "frontend" / "src-tauri"
    )
    report = InstallerBuildReport(
        ok=False,
        version=_installed_version(),
        generated_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        platform=platform.platform(),
        shell_dir=str(shell),
    )
    run = runner or _default_runner

    # 1) toolchain ---------------------------------------------------------
    since = time.perf_counter()
    tools = toolchain_status()
    missing = [tool.name for tool in tools if not tool.found and tool.required]
    webview_ok, webview_detail = _webview2_present()
    report.steps.append(
        BuildStep(
            name="toolchain",
            status="pass" if not missing else "fail",
            detail=(
                ", ".join(f"{tool.name}={tool.version or 'found'}" for tool in tools)
                if not missing
                else f"missing: {', '.join(missing)}"
            ),
            seconds=time.perf_counter() - since,
            tail=[f"{tool.name}: {tool.hint}" for tool in tools if not tool.found],
        )
    )
    report.steps.append(
        BuildStep(
            name="webview2",
            status="pass" if webview_ok else ("fail" if sys.platform == "win32" else "not-run"),
            detail=webview_detail,
        )
    )
    for name in missing:
        report.blockers.append(f"{name} is not installed")
    if sys.platform == "win32" and not webview_ok:
        report.blockers.append("WebView2 runtime is not installed")

    # 2) the shell definition ---------------------------------------------
    since = time.perf_counter()
    config = shell / "tauri.conf.json"
    if not config.is_file():
        report.steps.append(
            BuildStep(
                name="shell_definition",
                status="fail",
                detail=f"missing {config}",
                seconds=time.perf_counter() - since,
            )
        )
        report.blockers.append(f"no Tauri shell definition at {config}")
    else:
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
            dist = str(data.get("build", {}).get("frontendDist", ""))
        except (OSError, ValueError) as exc:  # pragma: no cover - corrupt file
            report.steps.append(
                BuildStep(
                    name="shell_definition",
                    status="fail",
                    detail=f"could not read tauri.conf.json: {exc}",
                    seconds=time.perf_counter() - since,
                )
            )
        else:
            resolved = (shell / dist).resolve() if dist else None
            has_index = bool(resolved and (resolved / "index.html").is_file())
            report.steps.append(
                BuildStep(
                    name="shell_definition",
                    status="pass" if has_index else "fail",
                    detail=(
                        f"frontendDist={dist} -> {resolved} (index.html present)"
                        if has_index
                        else f"frontendDist={dist} does not point at a built UI; run: cd frontend; npm run build"
                    ),
                    seconds=time.perf_counter() - since,
                )
            )
            if not has_index:
                report.blockers.append("the UI bundle is not built (frontendDist has no index.html)")

    # 3) the build itself --------------------------------------------------
    frontend = shell.parent
    if not build:
        report.steps.append(
            BuildStep(
                name="build",
                status="not-run",
                detail="pass --build to run npm ci and npm run tauri:build",
            )
        )
    elif missing:
        report.steps.append(
            BuildStep(
                name="build",
                status="skip",
                detail=f"not attempted: {', '.join(missing)} missing",
            )
        )
    else:
        npm = _npm_command()
        # `npm ci` needs a lockfile and is the reproducible choice; `npm install`
        # is the honest fallback when a tree has none (a fresh checkout of an old
        # version), instead of failing with npm's own cryptic message.
        has_lock = (frontend / "package-lock.json").is_file()
        install_argv = (
            [npm, "ci", "--no-audit", "--no-fund"]
            if has_lock
            else [npm, "install", "--no-audit", "--no-fund"]
        )
        install_name = "npm_ci" if has_lock else "npm_install"
        snapshot = _snapshot_ui_bundle(root)
        report.steps.append(
            BuildStep(
                name="ui_bundle",
                status="pass" if snapshot is not None else "not-run",
                detail=(
                    f"snapshot of {_webui_dir(root)} kept; it is restored if the build fails"
                    if snapshot is not None
                    else "no built UI bundle to protect (it will be created by the build)"
                ),
            )
        )
        step = _run_step(install_name, install_argv, cwd=frontend, timeout=timeout, runner=run)
        report.steps.append(step)
        if step.status == "pass":
            report.steps.append(
                _run_step(
                    "tauri_build",
                    [npm, "run", "tauri:build"],
                    cwd=frontend,
                    timeout=timeout,
                    runner=run,
                )
            )
        else:
            report.steps.append(
                BuildStep(name="tauri_build", status="skip", detail=f"{install_name} failed")
            )
        # A failed build must not cost the user their running UI.
        built_ok = all(
            item.status == "pass"
            for item in report.steps
            if item.name in {install_name, "tauri_build"}
        ) and _ui_bundle_is_complete(root)
        if built_ok:
            shutil.rmtree(root / "data" / "ui-bundle-backup", ignore_errors=True)
        if not built_ok and _restore_ui_bundle(root, snapshot):
            report.steps.append(
                BuildStep(
                    name="ui_bundle_restored",
                    status="pass",
                    detail="the previous Command Center bundle was put back after the failed build",
                )
            )

    # 4) what came out -----------------------------------------------------
    since = time.perf_counter()
    artifacts = discover_installer_artifacts(shell)
    report.artifacts = [
        {"path": str(path), "name": path.name, "size": path.stat().st_size}
        for path in artifacts
    ]
    report.steps.append(
        BuildStep(
            name="artifacts",
            status="pass" if artifacts else "not-run",
            detail=(
                f"{len(artifacts)} installer(s): " + ", ".join(path.name for path in artifacts)
                if artifacts
                else "no installer found in bundle/nsis or bundle/msi"
            ),
            seconds=time.perf_counter() - since,
        )
    )
    if not artifacts and build:
        report.blockers.append("the build produced no installer artifact")

    # 5) runtime the installed shell will start ---------------------------
    #
    # The Tauri window is only a window: it starts `python -m pc_agent api serve`
    # and honours OMNI_PYTHON. So the honest statement is that the installer
    # ships the window and the *same machine* must have this package importable -
    # which `pip install -e .` already provides. Report that instead of implying
    # the exe is self-contained.
    report.steps.append(
        BuildStep(
            name="runtime",
            status="pass" if _importable(interpreter=sys.executable) else "fail",
            detail=(
                f"the shell starts the core with python -m pc_agent; "
                f"OMNI_PYTHON={sys.executable}"
            ),
        )
    )
    if launcher:
        path = write_launcher(
            root,
            python=sys.executable,
            executable=_launcher_executable(artifacts),
        )
        report.steps.append(
            BuildStep(
                name="launcher",
                status="pass",
                detail=f"{path} pins OMNI_PYTHON to this interpreter",
            )
        )
    else:
        report.steps.append(
            BuildStep(
                name="launcher",
                status="not-run",
                detail="pass --launcher to write Omni-Assistant.cmd next to the project",
            )
        )

    # 6) signing -----------------------------------------------------------
    if not sign:
        report.steps.append(
            BuildStep(
                name="signing",
                status="not-run",
                detail="pass --sign (with a certificate) to sign and verify the installers",
            )
        )
    else:
        plan = plan_signing(shell, thumbprint=thumbprint, pfx=pfx)
        if not plan.ready:
            report.steps.append(
                BuildStep(
                    name="signing",
                    status="fail",
                    detail="signing cannot run yet",
                    tail=list(plan.blockers),
                )
            )
            report.blockers.extend(plan.blockers)
        else:
            since = time.perf_counter()
            result = sign_bundle(
                shell, thumbprint=thumbprint, pfx=pfx, timestamp_url=timestamp_url
            )
            report.signing = result
            report.steps.append(
                BuildStep(
                    name="signing",
                    status="pass" if result.get("ok") else "fail",
                    detail=(
                        f"signed and verified {result.get('signed', 0)}/"
                        f"{result.get('artifacts', 0)} installer(s)"
                    ),
                    seconds=time.perf_counter() - since,
                    tail=[
                        f"{item.get('path')}: {item.get('status')} / {item.get('verify')}"
                        for item in result.get("results", [])
                    ],
                )
            )

    report.ok = not report.failures() and bool(
        [step for step in report.steps if step.status == "pass"]
    ) and not report.blockers
    if write_report:
        target = installer_report_path(root)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(report.to_dict(), indent=1, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:  # pragma: no cover - read-only install
            raise PcAgentError(f"Could not write {target}: {exc}") from exc
    return report


#: What to install, in the order that unblocks the build. Written as commands the
#: user can paste one at a time; no placeholders, no "see the docs".
_FIXES: tuple[tuple[str, str], ...] = (
    ("cargo is not installed", "winget install --id Rustlang.Rustup --accept-source-agreements"),
    ("rustc is not installed", "winget install --id Rustlang.Rustup --accept-source-agreements"),
    ("node is not installed", "winget install --id OpenJS.NodeJS.LTS --accept-source-agreements"),
    ("npm is not installed", "winget install --id OpenJS.NodeJS.LTS --accept-source-agreements"),
    (
        "WebView2 runtime is not installed",
        "winget install --id Microsoft.EdgeWebView2Runtime --accept-source-agreements",
    ),
)


def next_steps(report: InstallerBuildReport) -> list[str]:
    """The ordered commands that remove this report's blockers.

    Deduplicated on purpose: `cargo` and `rustc` come from the same installer, so
    the user gets one line for Rust, not two copies of it.
    """

    steps: list[str] = []
    for blocker in report.blockers:
        for needle, command in _FIXES:
            if blocker == needle and command not in steps:
                steps.append(command)
    if report.blockers:
        steps.append("pc-agent release installer --check")
        steps.append("pc-agent release installer --build --launcher")
    return steps


def _importable(*, interpreter: str) -> bool:
    """Can this interpreter import the package the shell will ask it to run?"""

    try:
        completed = subprocess.run(
            [interpreter, "-c", "import pc_agent, sys; sys.exit(0)"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - env
        return False
    return completed.returncode == 0


def _launcher_executable(artifacts: list[Path]) -> str | None:
    """The NSIS ``.exe`` when it exists, else the first artifact."""

    for path in artifacts:
        if path.suffix.lower() == ".exe":
            return str(path)
    return str(artifacts[0]) if artifacts else None


def _installed_version() -> str:
    from . import __version__

    return __version__
