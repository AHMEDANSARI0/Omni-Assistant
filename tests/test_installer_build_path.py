"""0.82.11: the installer build path, and what it protects.

Two things this file exists for:

1. `npm run build` deletes `src/pc_agent/webui/assets` and `index.html` *before*
   it compiles. A failed installer build used to leave the Python package with no
   Command Center at all - a 404 where the app should be. `--build` now snapshots
   the bundle and puts it back when the build fails.
2. The Tauri window is only a window: it starts `python -m pc_agent api serve`
   and honours `OMNI_PYTHON`. On a PC with several interpreters "python" is a
   coin flip, so `--launcher` writes a `.cmd` that pins the interpreter which
   actually imports this package.

Nothing here claims a Rust build happened: the toolchain checks are monkeypatched
and the runner is a fake, exactly like the module's own tests.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pc_agent import installer_build
from pc_agent.cli import build_parser
from pc_agent.installer_build import (
    LAUNCHER_NAME,
    launcher_path,
    run_installer_build,
    write_launcher,
)

SHELL = Path(__file__).resolve().parents[1] / "frontend" / "src-tauri"


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _tree(tmp_path: Path, *, with_bundle: bool = True, with_lock: bool = True) -> Path:
    """A project tree with a shell definition and (optionally) a built UI."""

    root = tmp_path / "project"
    (root / "frontend" / "src-tauri").mkdir(parents=True)
    (root / "frontend" / "src-tauri" / "tauri.conf.json").write_text(
        json.dumps(
            {"build": {"frontendDist": "../../src/pc_agent/webui"}, "version": "0.82.12"}
        ),
        encoding="utf-8",
    )
    if with_lock:
        (root / "frontend" / "package-lock.json").write_text("{}", encoding="utf-8")
    webui = root / "src" / "pc_agent" / "webui"
    webui.mkdir(parents=True)
    (webui / "__init__.py").write_text("# hand-written package code\n", encoding="utf-8")
    if with_bundle:
        (webui / "assets").mkdir()
        (webui / "assets" / "index-abc123.js").write_text("console.log(1)\n", encoding="utf-8")
        (webui / "index.html").write_text("<html>Command Center</html>\n", encoding="utf-8")
    return root


def _fake_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Tool:
        def __init__(self, name: str) -> None:
            self.name = name
            self.found = True
            self.version = "fake 1.0"
            self.hint = ""
            self.required = True

    monkeypatch.setattr(
        installer_build, "toolchain_status", lambda: [_Tool(n) for n in ("node", "npm", "cargo", "rustc")]
    )


def _no_webview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer_build, "_webview2_present", lambda: (True, "fake"))


def _fake_runner(statuses: dict[str, int], seen: list[list[str]] | None = None):
    def run(argv: list[str], timeout: int):
        if seen is not None:
            seen.append(list(argv))
        # match on the sub-command, not the executable path
        key = "ci" if "ci" in argv else "install" if "install" in argv else "tauri"
        return _Completed(returncode=statuses.get(key, 0), stdout=f"{key} ran")

    return run


# ---------------------------------------------------------------- the build
def test_a_lockfile_means_npm_ci_and_no_lockfile_means_npm_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_toolchain(monkeypatch)
    _no_webview(monkeypatch)

    with_lock = _tree(tmp_path / "one", with_lock=True)
    seen: list[list[str]] = []
    report = run_installer_build(
        root_dir=with_lock, build=True, runner=_fake_runner({}, seen), write_report=False
    )
    assert report.step("npm_ci") is not None
    assert report.step("npm_install") is None
    assert seen[0][1] == "ci"

    without_lock = _tree(tmp_path / "two", with_lock=False)
    seen.clear()
    report = run_installer_build(
        root_dir=without_lock, build=True, runner=_fake_runner({}, seen), write_report=False
    )
    assert report.step("npm_install") is not None
    assert report.step("npm_ci") is None
    assert seen[0][1] == "install"


def test_a_failed_build_gives_the_command_center_bundle_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact damage: npm run build empties the bundle, then the build dies."""

    _fake_toolchain(monkeypatch)
    _no_webview(monkeypatch)
    root = _tree(tmp_path)
    webui = root / "src" / "pc_agent" / "webui"
    before = (webui / "index.html").read_text(encoding="utf-8")

    def runner(argv: list[str], timeout: int):
        key = "ci" if "ci" in argv else "install" if "install" in argv else "tauri"
        if key == "tauri":
            # what vite/tauri does before it compiles
            (webui / "index.html").unlink()
            import shutil

            shutil.rmtree(webui / "assets")
            return _Completed(returncode=1, stderr="error: could not compile `omni-command-center`")
        return _Completed()

    report = run_installer_build(root_dir=root, build=True, runner=runner, write_report=False)

    assert report.step("tauri_build").status == "fail"
    restored = report.step("ui_bundle_restored")
    assert restored is not None and restored.status == "pass"
    assert (webui / "index.html").read_text(encoding="utf-8") == before
    assert (webui / "assets" / "index-abc123.js").is_file()
    # hand-written package code is never part of the snapshot dance
    assert (webui / "__init__.py").is_file()
    assert report.ok is False


def test_a_successful_build_keeps_the_old_bundle_out_of_the_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_toolchain(monkeypatch)
    _no_webview(monkeypatch)
    root = _tree(tmp_path)

    report = run_installer_build(
        root_dir=root, build=True, runner=_fake_runner({}), write_report=False
    )

    assert report.step("npm_ci").status == "pass"
    assert report.step("tauri_build").status == "pass"
    assert report.step("ui_bundle_restored") is None
    # the snapshot directory does not linger after a good build
    assert not (root / "data" / "ui-bundle-backup").exists()


def test_a_failed_build_with_no_previous_bundle_does_not_pretend_to_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_toolchain(monkeypatch)
    _no_webview(monkeypatch)
    root = _tree(tmp_path, with_bundle=False)

    report = run_installer_build(
        root_dir=root, build=True, runner=_fake_runner({"tauri": 1}), write_report=False
    )

    assert report.step("ui_bundle").status == "not-run"
    assert report.step("ui_bundle_restored") is None


def test_npm_failing_skips_the_tauri_build_instead_of_running_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_toolchain(monkeypatch)
    _no_webview(monkeypatch)
    root = _tree(tmp_path)
    seen: list[list[str]] = []

    report = run_installer_build(
        root_dir=root, build=True, runner=_fake_runner({"ci": 1}, seen), write_report=False
    )

    assert report.step("npm_ci").status == "fail"
    assert report.step("tauri_build").status == "skip"
    assert len(seen) == 1  # tauri was never invoked


# ------------------------------------------------------------- the launcher
def test_the_launcher_pins_the_interpreter_and_needs_no_exe(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    path = write_launcher(root, python=r"C:\venv\Scripts\python.exe")

    assert path == launcher_path(root) == root / LAUNCHER_NAME
    text = path.read_text(encoding="utf-8")
    assert 'set "OMNI_PYTHON=C:\\venv\\Scripts\\python.exe"' in text
    assert '"C:\\venv\\Scripts\\python.exe" -m pc_agent gui %*' in text
    assert "pc-agent release installer --build" in text  # tells the user how to get the exe
    # cmd.exe wants CRLF; assert on the bytes because read_text normalises them
    raw = path.read_bytes()
    assert b"\r\n" in raw
    assert b"\n\n" not in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_the_launcher_prefers_a_built_exe_and_falls_back_when_it_is_gone(
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path)
    exe = r"C:\Program Files\Omni Assistant\omni-command-center.exe"
    path = write_launcher(root, python="python", executable=exe)
    text = path.read_text(encoding="utf-8")

    assert f'if exist "{exe}"' in text
    assert f'start "" "{exe}" %*' in text
    # and the built-in window is still there for a moved/uninstalled shell
    assert '-m pc_agent gui' in text


def test_the_launcher_survives_a_path_with_spaces_and_has_no_secrets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "my projects" / "omni assistant"
    root.mkdir(parents=True)
    path = write_launcher(root, python=r"C:\Program Files\Python311\python.exe")
    text = path.read_text(encoding="utf-8")

    assert str(root) in text
    lowered = text.lower()
    for forbidden in ("api_key", "token", "secret", "password", "--password"):
        assert forbidden not in lowered


def test_the_launcher_step_reports_where_it_wrote_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_toolchain(monkeypatch)
    _no_webview(monkeypatch)
    root = _tree(tmp_path)

    report = run_installer_build(
        root_dir=root, build=True, launcher=True, runner=_fake_runner({}), write_report=False
    )

    step = report.step("launcher")
    assert step.status == "pass"
    assert str(root / LAUNCHER_NAME) in step.detail
    assert (root / LAUNCHER_NAME).is_file()


def test_the_launcher_is_not_written_unless_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_toolchain(monkeypatch)
    _no_webview(monkeypatch)
    root = _tree(tmp_path)

    report = run_installer_build(root_dir=root, write_report=False)

    assert report.step("launcher").status == "not-run"
    assert not (root / LAUNCHER_NAME).exists()


# -------------------------------------------------------------- the runtime
def test_the_report_states_that_the_shell_starts_this_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installer ships a window; the core is the installed package."""

    _fake_toolchain(monkeypatch)
    _no_webview(monkeypatch)
    root = _tree(tmp_path)

    report = run_installer_build(root_dir=root, write_report=False)

    step = report.step("runtime")
    assert step.status == "pass"
    assert "python -m pc_agent" in step.detail
    assert "OMNI_PYTHON=" in step.detail


def test_an_interpreter_that_cannot_import_the_package_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "ModuleNotFoundError")

    monkeypatch.setattr(installer_build.subprocess, "run", fake_run)
    assert installer_build._importable(interpreter="python") is False


# --------------------------------------------------------------------- CLI
def test_the_cli_exposes_the_launcher_flag_and_passes_it_through() -> None:
    args = build_parser().parse_args(["release", "installer", "--launcher"])
    assert args.launcher is True
    assert build_parser().parse_args(["release", "installer", "--check"]).launcher is False


def test_check_mode_never_writes_the_launcher(tmp_path: Path, monkeypatch) -> None:
    """`--check` runs nothing, so it must not create files either."""

    _fake_toolchain(monkeypatch)
    _no_webview(monkeypatch)
    root = _tree(tmp_path)
    # the CLI forces launcher=False when --check is present; mirror that here
    report = run_installer_build(
        root_dir=root, launcher=False, runner=_fake_runner({}), write_report=False
    )
    assert report.step("launcher").status == "not-run"
    assert not (root / LAUNCHER_NAME).exists()


def test_the_report_file_carries_the_new_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_toolchain(monkeypatch)
    _no_webview(monkeypatch)
    root = _tree(tmp_path)

    run_installer_build(root_dir=root, build=True, launcher=True, runner=_fake_runner({}))
    payload = json.loads((root / "data" / "installer-build.json").read_text(encoding="utf-8"))

    names = {step["name"] for step in payload["steps"]}
    assert {"ui_bundle", "runtime", "launcher"} <= names
    assert payload["version"] == installer_build._installed_version()


# ------------------------------------------------------------- next steps
def test_the_next_steps_are_deduplicated_commands_without_placeholders() -> None:
    from pc_agent.installer_build import InstallerBuildReport, next_steps

    report = InstallerBuildReport(
        ok=False,
        version="0.82.11",
        generated_at="now",
        platform="Windows",
        shell_dir="C:/x",
        blockers=[
            "cargo is not installed",
            "rustc is not installed",
            "node is not installed",
            "WebView2 runtime is not installed",
        ],
    )
    steps = next_steps(report)

    # rustup installs both cargo and rustc: one line, not two
    assert sum("Rustlang.Rustup" in step for step in steps) == 1
    assert any("OpenJS.NodeJS.LTS" in step for step in steps)
    assert any("Microsoft.EdgeWebView2Runtime" in step for step in steps)
    assert steps[-2:] == [
        "pc-agent release installer --check",
        "pc-agent release installer --build --launcher",
    ]
    for step in steps:
        assert "<" not in step and ">" not in step  # no angle-bracket placeholders


def test_a_ready_report_has_no_next_steps() -> None:
    from pc_agent.installer_build import InstallerBuildReport, next_steps

    report = InstallerBuildReport(
        ok=True, version="0.82.11", generated_at="now", platform="Windows", shell_dir="C:/x"
    )
    assert next_steps(report) == []


def test_check_mode_prints_the_ordered_fix_commands(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`release installer --check` must end with commands, not advice."""

    from pc_agent.cli import _run_release_installer
    from pc_agent.config import Settings

    class _Tool:
        def __init__(self, name: str, found: bool) -> None:
            self.name = name
            self.found = found
            self.version = "1.0" if found else ""
            self.hint = "install it"
            self.required = True

    monkeypatch.setattr(
        installer_build,
        "toolchain_status",
        lambda: [_Tool("node", True), _Tool("npm", True), _Tool("cargo", False), _Tool("rustc", False)],
    )
    monkeypatch.setattr(installer_build, "_webview2_present", lambda: (True, "fake"))
    root = _tree(tmp_path)
    args = build_parser().parse_args(["release", "installer", "--check"])
    code = _run_release_installer(Settings.from_env(root), args)
    out = capsys.readouterr().out

    assert code == 1  # not ready, and it says so
    assert "Next, one command at a time" in out
    assert "winget install --id Rustlang.Rustup" in out
    assert out.index("Rustlang.Rustup") < out.index("--build --launcher")
