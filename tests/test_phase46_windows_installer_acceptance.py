"""Phase 46: guarded live-Windows Inno installer acceptance harness."""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest

from pc_agent import __version__
from pc_agent import cli as cli_module
from pc_agent.cli import _run_release_command, build_parser
from pc_agent.config import Settings
from pc_agent.errors import PcAgentError
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry
from pc_agent.windows_installer import WINDOWS_INSTALLER_APP_ID
from pc_agent.windows_installer_acceptance import (
    WINDOWS_INSTALLER_ACCEPTANCE_REPORT,
    WindowsInstallerAcceptanceReport,
    WindowsInstallerAcceptanceVerification,
    run_windows_installer_acceptance,
    verify_windows_installer_acceptance_report,
    windows_uninstall_registry_key,
)

ROOT = Path(__file__).resolve().parents[1]
_PRESERVED = {
    ".env",
    "data/phase46-acceptance-sentinel.txt",
    "workspace/phase46-acceptance-sentinel.txt",
}


class FakeWindowsInstaller:
    def __init__(self, start_menu_group: Path) -> None:
        self.start_menu_group = start_menu_group
        self.registry: dict[str, str] | None = None
        self.calls: list[tuple[str, ...]] = []
        self.gui_returncode = 0

    def probe(self) -> dict[str, str] | None:
        return dict(self.registry) if self.registry is not None else None

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        args = tuple(str(part) for part in argv)
        self.calls.append(args)
        executable = Path(args[0])
        name = executable.name
        if name == "ISCC.exe":
            source = Path(args[1]).parent
            output = source / "output"
            output.mkdir()
            (output / f"SelfHealingPCAgent-Setup-{__version__}.exe").write_bytes(
                b"MZ phase46 fake setup"
            )
            return self._completed(args, 0, stdout="compiled")
        if name.startswith("SelfHealingPCAgent-Setup-"):
            install_dir = Path(self._option(args, "/DIR="))
            log_path = Path(self._option(args, "/LOG="))
            source = executable.parents[1]
            shutil.copytree(source / "payload", install_dir)
            scripts = install_dir / ".venv" / "Scripts"
            scripts.mkdir(parents=True)
            (scripts / "pc-agent.exe").write_bytes(b"fake cli")
            (scripts / "pc-agent-gui.exe").write_bytes(b"fake gui")
            (install_dir / "unins000.exe").write_bytes(b"fake uninstaller")
            self.start_menu_group.mkdir(parents=True)
            for shortcut in (
                "Self-Healing PC Agent.lnk",
                "Self-Healing PC Agent CLI.lnk",
                "Uninstall Self-Healing PC Agent.lnk",
            ):
                (self.start_menu_group / shortcut).write_bytes(b"fake shortcut")
            self.registry = {
                "DisplayName": "Self-Healing PC Agent",
                "DisplayVersion": __version__,
                "InstallLocation": str(install_dir) + "\\",
                "UninstallString": str(install_dir / "unins000.exe"),
            }
            log_path.write_text("fake install log\n", encoding="utf-8")
            return self._completed(args, 0, stdout="installed")
        if name == "pc-agent.exe":
            if "--version" in args:
                return self._completed(args, 0, stdout=f"pc-agent {__version__}\n")
            return self._completed(
                args,
                0,
                stdout=f"Self-Healing PC Agent {__version__}\nStatus: OK\n",
            )
        if name == "pc-agent-gui.exe":
            return self._completed(
                args,
                self.gui_returncode,
                stdout="Status: READY (no window opened)\n",
                stderr="gui failed" if self.gui_returncode else "",
            )
        if name == "unins000.exe":
            install_dir = executable.parent
            log_path = Path(self._option(args, "/LOG="))
            for path in sorted(
                (path for path in install_dir.rglob("*") if path.is_file()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                if path.relative_to(install_dir).as_posix() not in _PRESERVED:
                    path.unlink()
            for path in sorted(
                (path for path in install_dir.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                try:
                    path.rmdir()
                except OSError:
                    pass
            for shortcut in self.start_menu_group.glob("*.lnk"):
                shortcut.unlink()
            try:
                self.start_menu_group.rmdir()
            except OSError:
                pass
            self.registry = None
            log_path.write_text("fake uninstall log\n", encoding="utf-8")
            return self._completed(args, 0, stdout="uninstalled")
        raise AssertionError(f"Unexpected fake process: {args!r} cwd={cwd} timeout={timeout}")

    @staticmethod
    def _option(args: tuple[str, ...], prefix: str) -> str:
        for arg in args:
            if arg.startswith(prefix):
                return arg[len(prefix) :]
        raise AssertionError(f"Missing option {prefix} in {args!r}")

    @staticmethod
    def _completed(
        args: tuple[str, ...],
        returncode: int,
        *,
        stdout: str = "",
        stderr: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)


def _run_fake(
    tmp_path: Path,
    fake: FakeWindowsInstaller,
    *,
    compiler: Path | None = None,
    output_name: str = "acceptance",
) -> WindowsInstallerAcceptanceReport:
    compiler_path = compiler or (tmp_path / "ISCC.exe")
    if compiler is None:
        compiler_path.write_bytes(b"fake compiler")
    return run_windows_installer_acceptance(
        ROOT,
        tmp_path / output_name,
        compiler=compiler_path,
        is_windows=True,
        environ={},
        process_runner=fake,
        registry_probe=fake.probe,
        start_menu_group=fake.start_menu_group,
        poll_interval=0.01,
        sleeper=lambda _seconds: None,
    )


def test_phase46_acceptance_compile_install_uninstall_and_report(
    tmp_path: Path,
) -> None:
    start_group = tmp_path / "Start Menu" / "Self-Healing PC Agent"
    fake = FakeWindowsInstaller(start_group)
    report = _run_fake(tmp_path, fake)

    assert report.status == "passed", report.error
    assert report.error is None
    assert all(report.checks.values())
    assert [step.status for step in report.steps] == ["passed"] * 8
    assert report.report_path.name == WINDOWS_INSTALLER_ACCEPTANCE_REPORT
    recorded = json.loads(report.report_path.read_text(encoding="utf-8"))
    assert recorded["app_id"] == WINDOWS_INSTALLER_APP_ID
    assert recorded["version"] == __version__
    assert recorded["signed"] is False
    assert recorded["registry"]["after_uninstall"] is None
    assert WINDOWS_INSTALLER_APP_ID in windows_uninstall_registry_key()

    commands = [" ".join(call) for call in fake.calls]
    assert any("ISCC.exe" in command for command in commands)
    assert any(
        "/VERYSILENT" in command and "/DIR=" in command and "/LOG=" in command
        for command in commands
    )
    assert any("pc-agent.exe" in command and "doctor" in command for command in commands)
    assert any("pc-agent-gui.exe" in command and "--check" in command for command in commands)
    assert any("unins000.exe" in command and "/VERYSILENT" in command for command in commands)

    verification = verify_windows_installer_acceptance_report(report.report_path)
    assert verification.status == "passed"
    assert verification.setup_sha256 == recorded["setup"]["sha256"]
    assert Path(recorded["setup"]["path"]).is_file()
    for relative in _PRESERVED:
        assert (report.install_dir / relative).is_file()

    sentinel = report.install_dir / "data/phase46-acceptance-sentinel.txt"
    sentinel.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(PcAgentError, match="sentinel does not match"):
        verify_windows_installer_acceptance_report(report.report_path)


def test_phase46_acceptance_refuses_existing_appid_and_missing_compiler(
    tmp_path: Path,
) -> None:
    fake = FakeWindowsInstaller(tmp_path / "group")
    fake.registry = {"DisplayVersion": "0.0.1"}
    refused = _run_fake(tmp_path, fake, output_name="refused")
    assert refused.status == "failed"
    assert refused.steps[0].status == "failed"
    assert "already exists" in (refused.error or "")
    assert fake.calls == []

    missing_fake = FakeWindowsInstaller(tmp_path / "other-group")
    missing = _run_fake(
        tmp_path,
        missing_fake,
        compiler=tmp_path / "missing-ISCC.exe",
        output_name="missing",
    )
    assert missing.status == "failed"
    assert "will not download" in (missing.error or "")
    assert missing_fake.calls == []

    with pytest.raises(PcAgentError, match="only on Windows"):
        run_windows_installer_acceptance(
            ROOT,
            tmp_path / "linux",
            is_windows=False,
            process_runner=fake,
        )


def test_phase46_failed_installed_check_attempts_cleanup_uninstall(
    tmp_path: Path,
) -> None:
    fake = FakeWindowsInstaller(tmp_path / "cleanup-group")
    fake.gui_returncode = 1
    report = _run_fake(tmp_path, fake, output_name="cleanup")

    assert report.status == "failed"
    assert report.checks["installed_cli_verified"]
    assert not report.checks["installed_gui_verified"]
    cleanup = next(step for step in report.steps if step.name == "cleanup_uninstall")
    assert cleanup.status == "passed"
    assert fake.registry is None
    assert not (report.install_dir / ".venv").exists()


def test_phase46_direct_release_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "acceptance"
    report = WindowsInstallerAcceptanceReport(
        report_path=output / WINDOWS_INSTALLER_ACCEPTANCE_REPORT,
        acceptance_root=output,
        source_dir=output / "source",
        install_dir=output / "app",
        log_dir=output / "logs",
        status="passed",
    )
    monkeypatch.setattr(
        cli_module,
        "run_windows_installer_acceptance",
        lambda _root, _output, compiler=None: report,
    )
    monkeypatch.setattr(
        cli_module,
        "verify_windows_installer_acceptance_report",
        lambda _path: WindowsInstallerAcceptanceVerification(
            report_path=report.report_path,
            version=__version__,
            status="passed",
            setup_sha256="a" * 64,
            source_files=123,
        ),
    )
    settings = Settings(
        root_dir=ROOT,
        workspace_dir=tmp_path / "workspace",
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "agent.db",
    )
    parser = build_parser()

    acceptance_args = parser.parse_args(
        [
            "release",
            "installer-acceptance",
            "--output",
            str(output),
            "--json",
        ]
    )
    assert _run_release_command(settings, acceptance_args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "passed"
    assert payload["app_id"] == WINDOWS_INSTALLER_APP_ID

    verify_args = parser.parse_args(
        [
            "release",
            "installer-acceptance-verify",
            str(report.report_path),
            "--json",
        ]
    )
    assert _run_release_command(settings, verify_args) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["status"] == "passed"
    assert verified["source_files"] == 123


def test_phase46_scope_inventory_and_documentation() -> None:
    assert __version__ == "0.55.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(build_default_registry().list_tools()) == 62

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == __version__
    assert project["project"]["dependencies"] == [
        "certifi>=2024.7.4",
        "pypdf[crypto]>=5.0,<7",
        "pypdfium2>=5,<6",
        "Pillow>=10.4,<14",
        "comtypes>=1.4,<2; sys_platform == 'win32'",
    ]

    module = (ROOT / "src/pc_agent/windows_installer_acceptance.py").read_text(encoding="utf-8")
    cli = (ROOT / "src/pc_agent/cli.py").read_text(encoding="utf-8")
    phase = (ROOT / "docs/phase46.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tools_doc = (ROOT / "docs/tools.md").read_text(encoding="utf-8")
    phase38 = (ROOT / "docs/phase38.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "run_windows_installer_acceptance" in module
    assert "verify_windows_installer_acceptance_report" in module
    assert "ISCC.exe" in module
    assert "/VERYSILENT" in module
    assert "will not download" in module
    assert "installer-acceptance" in cli
    assert "installer-acceptance-verify" in cli

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "src/pc_agent/windows_installer_acceptance.py",
        "docs/phase46.md",
        "tests/test_phase46_windows_installer_acceptance.py",
    ):
        assert expected in release_names

    for text in (phase, readme, tools_doc, phase38):
        assert "Windows Inno Acceptance Harness" in text
    assert "does not provide a live Windows acceptance transcript" in phase
    assert "Authenticode" in phase
    assert "SmartScreen" in phase
    assert "## 0.48.0 - PHASE 46 Windows Inno Acceptance Harness" in (
        changelog.splitlines()
    )
