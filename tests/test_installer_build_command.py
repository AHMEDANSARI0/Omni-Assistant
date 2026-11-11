"""0.82.7: building the desktop installer is a command, not a paragraph of advice.

`pc-agent release installer` has to be usable in three moods:

* ``--check`` - "can this machine build it?" It must run **nothing** that
  installs or compiles, and it must name the missing tool rather than fail with a
  stack trace from npm.
* ``--build`` - run ``npm ci`` then ``npm run tauri:build`` as argv lists (never a
  shell), keep the tail of the output, and discover what came out.
* ``--sign`` - reuse the Authenticode runbook, so "signed" means Windows would
  trust it.

The tests use a fake runner, so they assert the exact commands and the exact
report a real run would produce without needing Node, Rust or a certificate.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pc_agent import cli
from pc_agent.installer_build import (
    INSTALLER_REPORT_NAME,
    ToolInfo,
    installer_report_path,
    run_installer_build,
    toolchain_status,
)

ROOT = Path(__file__).resolve().parent.parent


class Completed:
    def __init__(self, code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


def _shell(tmp_path: Path, *, ui_built: bool = True) -> Path:
    """A minimal Tauri shell with a real config and (optionally) a built UI."""

    root = tmp_path
    (root / "frontend").mkdir(parents=True, exist_ok=True)
    (root / "frontend" / "package-lock.json").write_text("{}", encoding="utf-8")
    shell = root / "frontend" / "src-tauri"
    shell.mkdir(parents=True)
    ui = root / "src" / "pc_agent" / "webui"
    ui.mkdir(parents=True)
    if ui_built:
        (ui / "index.html").write_bytes(b"<!doctype html><title>ui</title>")
    (shell / "tauri.conf.json").write_text(
        json.dumps({"build": {"frontendDist": "../../src/pc_agent/webui"}, "version": "0.82.17"}),
        encoding="utf-8",
    )
    return shell


def _bundle(shell: Path) -> None:
    (shell / "target" / "release" / "bundle" / "nsis").mkdir(parents=True, exist_ok=True)
    (shell / "target" / "release" / "bundle" / "msi").mkdir(parents=True, exist_ok=True)
    (shell / "target" / "release" / "bundle" / "nsis" / "Omni_0.82.9_x64-setup.exe").write_bytes(b"MZ")
    (shell / "target" / "release" / "bundle" / "msi" / "Omni_0.82.9_x64_en-US.msi").write_bytes(b"MSI")


def _tools(*, missing: tuple[str, ...] = ()) -> list[ToolInfo]:
    names = ("node", "npm", "cargo", "rustc")
    return [
        ToolInfo(name=name, found=name not in missing, path=f"/usr/bin/{name}", version="ok")
        for name in names
    ]


# ------------------------------------------------------------------ --check

def test_check_mode_runs_no_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = _shell(tmp_path)
    monkeypatch.setattr("pc_agent.installer_build.toolchain_status", lambda: _tools())
    monkeypatch.setattr("pc_agent.installer_build._webview2_present", lambda: (False, "n/a"))

    def _no_run(argv, timeout):  # pragma: no cover - must never be called
        pytest.fail(f"--check must not run {argv}")

    report = run_installer_build(root_dir=tmp_path, shell_dir=shell, runner=_no_run)
    assert report.step("build").status == "not-run"
    assert report.step("signing").status == "not-run"
    assert report.artifacts == []


def test_missing_toolchain_is_named_not_crashed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = _shell(tmp_path)
    monkeypatch.setattr(
        "pc_agent.installer_build.toolchain_status", lambda: _tools(missing=("cargo", "rustc"))
    )
    monkeypatch.setattr("pc_agent.installer_build._webview2_present", lambda: (True, "1.0"))
    report = run_installer_build(root_dir=tmp_path, shell_dir=shell, build=True, runner=lambda a, t: None)
    assert report.step("toolchain").status == "fail"
    assert "missing: cargo, rustc" in report.step("toolchain").detail
    assert "cargo is not installed" in report.blockers
    assert report.step("build").status == "skip", "a build must not be attempted without its tools"
    assert report.ok is False


def test_a_missing_ui_bundle_is_a_blocker_with_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = _shell(tmp_path, ui_built=False)
    monkeypatch.setattr("pc_agent.installer_build.toolchain_status", lambda: _tools())
    monkeypatch.setattr("pc_agent.installer_build._webview2_present", lambda: (True, "1.0"))
    report = run_installer_build(root_dir=tmp_path, shell_dir=shell)
    step = report.step("shell_definition")
    assert step.status == "fail"
    assert "frontendDist" in step.detail
    assert "npm run build" in step.detail
    assert any("UI bundle is not built" in blocker for blocker in report.blockers)


# ------------------------------------------------------------------ --build

def test_build_runs_npm_ci_then_tauri_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = _shell(tmp_path)
    _bundle(shell)
    monkeypatch.setattr("pc_agent.installer_build.toolchain_status", lambda: _tools())
    monkeypatch.setattr("pc_agent.installer_build._webview2_present", lambda: (True, "1.0"))
    monkeypatch.setattr("pc_agent.installer_build._npm_command", lambda: "npm")

    seen: list[list[str]] = []

    def _run(argv, timeout):
        seen.append(list(argv))
        return Completed(0, "done")

    report = run_installer_build(root_dir=tmp_path, shell_dir=shell, build=True, runner=_run)
    assert [argv[1:3] for argv in seen] == [["ci", "--no-audit"], ["run", "tauri:build"]]
    assert all(argv[0] == "npm" for argv in seen)
    assert report.step("npm_ci").status == "pass"
    assert report.step("tauri_build").status == "pass"
    assert report.step("artifacts").status == "pass"
    assert len(report.artifacts) == 2
    assert report.ok is True, report.blockers


def test_a_failed_install_stops_before_the_expensive_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = _shell(tmp_path)
    monkeypatch.setattr("pc_agent.installer_build.toolchain_status", lambda: _tools())
    monkeypatch.setattr("pc_agent.installer_build._webview2_present", lambda: (True, "1.0"))
    monkeypatch.setattr("pc_agent.installer_build._npm_command", lambda: "npm")

    seen: list[str] = []

    def _run(argv, timeout):
        seen.append(argv[1])
        return Completed(1, "", "npm ERR! No matching version found")

    report = run_installer_build(root_dir=tmp_path, shell_dir=shell, build=True, runner=_run)
    # 0.82.14: a broken `npm ci` retries once with `npm install` (it reconciles an
    # out-of-sync lockfile); when that fails too, the expensive build never runs.
    assert seen == ["ci", "install"], "tauri build must not run after a broken install"
    assert report.step("tauri_build").status == "skip"
    assert any("No matching version" in line for line in report.step("npm_ci").tail)
    assert report.ok is False


def test_a_missing_npm_binary_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = _shell(tmp_path)
    monkeypatch.setattr("pc_agent.installer_build.toolchain_status", lambda: _tools())
    monkeypatch.setattr("pc_agent.installer_build._webview2_present", lambda: (True, "1.0"))
    monkeypatch.setattr("pc_agent.installer_build._npm_command", lambda: "npm")

    def _run(argv, timeout):
        raise FileNotFoundError("npm not found")

    report = run_installer_build(root_dir=tmp_path, shell_dir=shell, build=True, runner=_run)
    assert report.step("npm_ci").status == "fail"
    assert "could not start" in report.step("npm_ci").detail


def test_a_build_timeout_is_a_failure_with_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = _shell(tmp_path)
    monkeypatch.setattr("pc_agent.installer_build.toolchain_status", lambda: _tools())
    monkeypatch.setattr("pc_agent.installer_build._webview2_present", lambda: (True, "1.0"))
    monkeypatch.setattr("pc_agent.installer_build._npm_command", lambda: "npm")

    def _run(argv, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout, output="still compiling")

    report = run_installer_build(root_dir=tmp_path, shell_dir=shell, build=True, runner=_run)
    step = report.step("npm_ci")
    assert step.status == "fail"
    assert "timed out" in step.detail
    assert any("still compiling" in line for line in step.tail)


# ------------------------------------------------------------------- --sign

def test_sign_requested_but_blocked_records_the_real_blockers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = _shell(tmp_path)  # no bundle on purpose
    monkeypatch.setattr("pc_agent.installer_build.toolchain_status", lambda: _tools())
    monkeypatch.setattr("pc_agent.installer_build._webview2_present", lambda: (True, "1.0"))
    report = run_installer_build(
        root_dir=tmp_path, shell_dir=shell, sign=True, thumbprint="A" * 40, runner=lambda a, t: None
    )
    step = report.step("signing")
    assert step.status == "fail"
    assert any("no installer found" in line for line in step.tail)
    assert report.signing == {}


def test_sign_after_build_uses_the_signing_runbook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = _shell(tmp_path)
    _bundle(shell)
    monkeypatch.setattr("pc_agent.installer_build.toolchain_status", lambda: _tools())
    monkeypatch.setattr("pc_agent.installer_build._webview2_present", lambda: (True, "1.0"))
    monkeypatch.setattr("pc_agent.installer_build._npm_command", lambda: "npm")
    monkeypatch.setattr(
        "pc_agent.windows_signing.find_signtool", lambda explicit=None: Path("/opt/signtool.exe")
    )
    monkeypatch.setattr("pc_agent.windows_signing._IS_WINDOWS", True)
    class _Signed:
        def __init__(self, artifact: Path) -> None:
            self.artifact = artifact

        def to_dict(self) -> dict[str, object]:
            return {
                "path": str(self.artifact),
                "ok": True,
                "status": "signed",
                "detail": "signed with the certificate store",
            }

    monkeypatch.setattr(
        "pc_agent.windows_signing.sign_installer",
        lambda artifact, **kwargs: _Signed(Path(artifact)),
    )
    monkeypatch.setattr(
        "pc_agent.windows_signing.verify_authenticode",
        lambda path, **kwargs: type("R", (), {"ok": True, "status": "trusted", "detail": "ok"})(),
    )
    report = run_installer_build(
        root_dir=tmp_path,
        shell_dir=shell,
        build=True,
        sign=True,
        thumbprint="A" * 40,
        runner=lambda argv, timeout: Completed(0, "built"),
    )
    step = report.step("signing")
    assert step.status == "pass", (step.detail, step.tail)
    assert report.signing.get("signed") == 2
    assert report.ok is True, report.blockers


# --------------------------------------------------------------- report file

def test_the_report_is_written_where_the_docs_say(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = _shell(tmp_path)
    monkeypatch.setattr("pc_agent.installer_build.toolchain_status", lambda: _tools())
    monkeypatch.setattr("pc_agent.installer_build._webview2_present", lambda: (True, "1.0"))
    report = run_installer_build(root_dir=tmp_path, shell_dir=shell)
    target = installer_report_path(tmp_path)
    assert target.name == INSTALLER_REPORT_NAME
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["version"] == report.version
    assert payload["shell_dir"] == str(shell)
    assert [step["name"] for step in payload["steps"]][:3] == [
        "toolchain",
        "webview2",
        "shell_definition",
    ]


def test_writing_the_report_can_be_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = _shell(tmp_path)
    monkeypatch.setattr("pc_agent.installer_build.toolchain_status", lambda: _tools())
    monkeypatch.setattr("pc_agent.installer_build._webview2_present", lambda: (True, "1.0"))
    run_installer_build(root_dir=tmp_path, shell_dir=shell, write_report=False)
    assert not installer_report_path(tmp_path).exists()


# ---------------------------------------------------------------------- CLI

def test_cli_flags_exist() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["release", "installer", "--check", "--json"])
    assert args.check is True and args.json is True and args.build is False
    args = parser.parse_args(
        ["release", "installer", "--build", "--sign", "--thumbprint", "A" * 40, "--timeout", "60"]
    )
    assert args.build is True and args.sign is True and args.timeout == 60


def test_cli_refuses_sign_without_a_certificate(capsys: pytest.CaptureFixture[str]) -> None:
    from pc_agent.config import Settings

    args = cli.build_parser().parse_args(["release", "installer", "--sign"])
    code = cli._run_release_installer(Settings.from_env(ROOT), args)
    captured = capsys.readouterr()
    assert code == 2
    assert "--thumbprint" in captured.err
    assert "release signing --certificates" in captured.err


def test_cli_check_mode_is_honest_about_this_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pc_agent.config import Settings

    shell = _shell(tmp_path)
    monkeypatch.setattr("pc_agent.installer_build.toolchain_status", lambda: _tools(missing=("cargo",)))
    monkeypatch.setattr("pc_agent.installer_build._webview2_present", lambda: (False, "n/a"))
    settings = Settings.from_env(tmp_path / "install")
    settings.ensure_directories()
    args = cli.build_parser().parse_args(
        ["release", "installer", "--check", "--shell", str(shell)]
    )
    code = cli._run_release_installer(settings, args)
    captured = capsys.readouterr()
    assert code == 1
    assert "cargo is not installed" in captured.out
    assert "--build" in captured.out, "the output must say what to do next"


def test_the_real_toolchain_probe_returns_records() -> None:
    tools = toolchain_status()
    names = [tool.name for tool in tools]
    assert names == ["node", "npm", "cargo", "rustc"]
    assert all(isinstance(tool.found, bool) for tool in tools)
    for tool in tools:
        if tool.found:
            assert tool.path
        else:
            assert tool.hint, f"{tool.name} must say how to install it"
