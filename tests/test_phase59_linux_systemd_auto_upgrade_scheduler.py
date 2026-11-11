"""Phase 59: Linux systemd-user background auto-upgrade scheduler."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from pc_agent import __version__
from pc_agent import cli as cli_module
from pc_agent.auto_upgrade import (
    DEFAULT_AUTO_UPGRADE_TASK_NAME,
    DEFAULT_LINUX_AUTO_UPGRADE_UNIT_NAME,
    AutoUpgradeSchedulerResult,
    build_linux_auto_upgrade_systemd_units,
    get_auto_upgrade_status,
    get_linux_auto_upgrade_schedule,
    install_auto_upgrade_schedule,
    install_linux_auto_upgrade_schedule,
    list_auto_upgrade_scheduler_profiles,
    uninstall_linux_auto_upgrade_schedule,
)
from pc_agent.cli import _run_update_command, build_parser
from pc_agent.config import Settings
from pc_agent.errors import PcAgentError
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[1]


def _settings(root: Path) -> Settings:
    settings = Settings(
        root_dir=root,
        workspace_dir=root / "workspace",
        data_dir=root / "data",
        database_path=root / "data" / "agent.db",
    )
    settings.ensure_directories()
    executable = root / ".venv" / "bin" / "pc-agent"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    if os.name == "posix":
        executable.chmod(0o755)
    return settings


class _FakeSystemdRunner:
    def __init__(
        self,
        *,
        fail_enable: bool = False,
        manager_available: bool = True,
    ) -> None:
        self.fail_enable = fail_enable
        self.manager_available = manager_available
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        args = [str(part) for part in argv]
        self.calls.append(args)
        if not self.manager_available:
            return subprocess.CompletedProcess(
                args, 1, "", "Failed to connect to user scope bus"
            )
        if "show-environment" in args:
            return subprocess.CompletedProcess(args, 0, "LANG=C.UTF-8\n", "")
        if "enable" in args and self.fail_enable:
            return subprocess.CompletedProcess(args, 1, "", "enable failed")
        if "show" in args:
            return subprocess.CompletedProcess(
                args,
                0,
                "LoadState=loaded\nActiveState=active\nSubState=waiting\n"
                "UnitFileState=enabled\n",
                "",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    @property
    def actions(self) -> list[str]:
        result: list[str] = []
        for call in self.calls:
            result.extend(
                part
                for part in call
                if part
                in {
                    "daemon-reload",
                    "disable",
                    "enable",
                    "reset-failed",
                    "show",
                    "show-environment",
                }
            )
        return result


def _unit_paths(config_home: Path, unit_name: str) -> tuple[Path, Path]:
    unit_dir = config_home / "systemd" / "user"
    return unit_dir / f"{unit_name}.service", unit_dir / f"{unit_name}.timer"


def test_phase59_systemd_units_and_name_validation(tmp_path: Path) -> None:
    root = tmp_path / "app root %"
    settings = _settings(root)
    units = build_linux_auto_upgrade_systemd_units(
        settings, unit_name=DEFAULT_AUTO_UPGRADE_TASK_NAME
    )
    assert units.unit_name == DEFAULT_LINUX_AUTO_UPGRADE_UNIT_NAME
    assert units.service_name.endswith(".service")
    assert units.timer_name.endswith(".timer")
    assert "X-SelfHealingPCAgent-Managed=auto-upgrade" in units.service_text
    assert "Type=oneshot" in units.service_text
    assert "update auto-run" in units.service_text
    assert "app root %%" in units.service_text
    assert "NoNewPrivileges=true" in units.service_text
    assert "PrivateTmp=true" in units.service_text
    assert "OnBootSec=5min" in units.timer_text
    assert "OnUnitInactiveSec=1h" in units.timer_text
    assert "Persistent=true" in units.timer_text
    assert "WantedBy=timers.target" in units.timer_text

    for invalid in ("bad/name", "bad name", "unit.service", "unit.timer"):
        with pytest.raises(PcAgentError, match="unit name"):
            build_linux_auto_upgrade_systemd_units(
                settings, unit_name=invalid
            )


def test_phase59_install_status_uninstall_and_generic_dispatch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "app"
    config_home = tmp_path / "config"
    settings = _settings(root)
    runner = _FakeSystemdRunner()

    installed = install_auto_upgrade_schedule(
        settings,
        runner=runner,
        platform_name="linux",
        config_home=config_home,
        systemctl="systemctl",
    )
    service_path, timer_path = _unit_paths(
        config_home, DEFAULT_LINUX_AUTO_UPGRADE_UNIT_NAME
    )
    assert installed.profile == "linux-systemd-user"
    assert installed.platform == "linux"
    assert installed.task_name == DEFAULT_LINUX_AUTO_UPGRADE_UNIT_NAME
    assert installed.installed is True
    assert installed.state == "enabled"
    assert service_path.is_file()
    assert timer_path.is_file()
    assert runner.actions == ["show-environment", "daemon-reload", "enable"]

    status = get_linux_auto_upgrade_schedule(
        runner=runner,
        platform_name="linux",
        config_home=config_home,
    )
    assert status.installed is True
    assert status.state == "active/waiting"
    overall = get_auto_upgrade_status(
        settings,
        include_scheduler=True,
        platform_name="linux",
        scheduler_runner=runner,
        scheduler_config_home=config_home,
    )
    assert overall.scheduler_profile == "linux-systemd-user"
    assert overall.scheduler_platform == "linux"
    assert overall.scheduler is not None
    assert overall.scheduler.installed is True

    wants_link = (
        config_home
        / "systemd"
        / "user"
        / "timers.target.wants"
        / timer_path.name
    )
    wants_link.parent.mkdir(parents=True, exist_ok=True)
    link_created = False
    try:
        wants_link.symlink_to(timer_path)
        link_created = True
    except OSError:
        pass

    removed = uninstall_linux_auto_upgrade_schedule(
        runner=runner,
        platform_name="linux",
        config_home=config_home,
    )
    assert removed.installed is False
    assert not service_path.exists()
    assert not timer_path.exists()
    if link_created:
        assert not wants_link.exists() and not wants_link.is_symlink()
    assert runner.actions[-4:] == [
        "daemon-reload",
        "disable",
        "daemon-reload",
        "reset-failed",
    ]

    unavailable = get_linux_auto_upgrade_schedule(
        runner=_FakeSystemdRunner(manager_available=False),
        platform_name="linux",
        config_home=config_home,
    )
    assert unavailable.installed is False
    assert unavailable.state == "unavailable"


def test_phase59_install_rolls_back_and_refuses_unmanaged_units(
    tmp_path: Path,
) -> None:
    root = tmp_path / "app"
    config_home = tmp_path / "config"
    settings = _settings(root)
    service_path, timer_path = _unit_paths(
        config_home, DEFAULT_LINUX_AUTO_UPGRADE_UNIT_NAME
    )

    failing = _FakeSystemdRunner(fail_enable=True)
    with pytest.raises(PcAgentError, match="enable failed"):
        install_linux_auto_upgrade_schedule(
            settings,
            runner=failing,
            platform_name="linux",
            config_home=config_home,
        )
    assert not service_path.exists()
    assert not timer_path.exists()

    good = _FakeSystemdRunner()
    install_linux_auto_upgrade_schedule(
        settings,
        runner=good,
        platform_name="linux",
        config_home=config_home,
    )
    prior_timer = timer_path.read_text(encoding="utf-8") + "# prior revision\n"
    timer_path.write_text(prior_timer, encoding="utf-8")
    with pytest.raises(PcAgentError, match="enable failed"):
        install_linux_auto_upgrade_schedule(
            settings,
            runner=_FakeSystemdRunner(fail_enable=True),
            platform_name="linux",
            config_home=config_home,
        )
    assert timer_path.read_text(encoding="utf-8") == prior_timer

    timer_path.write_text("[Unit]\nDescription=someone else\n", encoding="utf-8")
    unmanaged_runner = _FakeSystemdRunner()
    with pytest.raises(PcAgentError, match="unmanaged systemd unit"):
        install_linux_auto_upgrade_schedule(
            settings,
            runner=unmanaged_runner,
            platform_name="linux",
            config_home=config_home,
        )
    assert unmanaged_runner.calls == []
    with pytest.raises(PcAgentError, match="unmanaged systemd unit"):
        uninstall_linux_auto_upgrade_schedule(
            runner=unmanaged_runner,
            platform_name="linux",
            config_home=config_home,
        )
    assert "someone else" in timer_path.read_text(encoding="utf-8")


def test_phase59_cli_confirmation_and_generic_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(tmp_path / "app")
    parser = build_parser()
    install_args = parser.parse_args(
        ["update", "auto-install-schedule", "--json"]
    )
    assert _run_update_command(settings, install_args) == 3
    assert "cancelled" in capsys.readouterr().out

    fake_result = AutoUpgradeSchedulerResult(
        profile="linux-systemd-user",
        platform="linux",
        task_name=DEFAULT_LINUX_AUTO_UPGRADE_UNIT_NAME,
        installed=True,
        state="active/waiting",
        executable=None,
        detail="fake status",
    )
    monkeypatch.setattr(
        cli_module,
        "get_auto_upgrade_schedule",
        lambda task_name: fake_result,
    )
    status_args = parser.parse_args(
        ["update", "auto-schedule-status", "--json"]
    )
    assert _run_update_command(settings, status_args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"] == "linux-systemd-user"
    assert payload["installed"] is True


@pytest.mark.skipif(
    not sys.platform.startswith("linux")
    or shutil.which("systemd-analyze") is None,
    reason="systemd-analyze oracle requires Linux systemd tools",
)
def test_phase59_systemd_analyze_verifies_generated_units(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "app")
    units = build_linux_auto_upgrade_systemd_units(settings)
    service_path = tmp_path / units.service_name
    timer_path = tmp_path / units.timer_name
    service_path.write_text(units.service_text, encoding="utf-8")
    timer_path.write_text(units.timer_text, encoding="utf-8")
    completed = subprocess.run(
        [
            str(shutil.which("systemd-analyze")),
            "verify",
            str(service_path),
            str(timer_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_phase59_scope_inventory_and_documentation() -> None:
    assert __version__ == "0.82.17"
    assert LATEST_SCHEMA_VERSION == 18
    assert len(build_default_registry().list_tools()) == 69
    profiles = {
        profile.profile_id: profile.status
        for profile in list_auto_upgrade_scheduler_profiles()
    }
    assert profiles == {
        "windows-scheduled-task": "implemented",
        "linux-systemd-user": "implemented",
        "macos-launchd": "planned",
    }

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "docs/phase59.md",
        "tests/test_phase59_linux_systemd_auto_upgrade_scheduler.py",
    ):
        assert expected in release_names

    module = (ROOT / "src/pc_agent/auto_upgrade.py").read_text(encoding="utf-8")
    cli = (ROOT / "src/pc_agent/cli.py").read_text(encoding="utf-8")
    phase = (ROOT / "docs/phase59.md").read_text(encoding="utf-8")
    phase56 = (ROOT / "docs/phase56.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tools = (ROOT / "docs/tools.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "build_linux_auto_upgrade_systemd_units" in module
    assert "install_auto_upgrade_schedule" in module
    assert "get_auto_upgrade_schedule" in cli
    for text in (phase, phase56, readme, tools):
        assert "Linux systemd-user Auto-Upgrade Scheduler" in text
    assert "stage-only" in phase
    assert "apply-when-idle" in phase
    assert "macOS LaunchAgent" in phase
    assert "## 0.61.0 - PHASE 59 Linux systemd-user Auto-Upgrade Scheduler" in (
        changelog.splitlines()
    )
