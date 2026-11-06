"""Phase 56: Windows-first background auto-upgrade for signed updates."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pc_agent import __version__, release_admin
from pc_agent import release as release_module
from pc_agent.auto_upgrade import (
    auto_upgrade_state_path,
    build_windows_auto_upgrade_task_script,
    count_active_tasks,
    get_auto_upgrade_status,
    get_windows_auto_upgrade_schedule,
    install_windows_auto_upgrade_schedule,
    list_auto_upgrade_scheduler_profiles,
    run_auto_upgrade,
    uninstall_windows_auto_upgrade_schedule,
    verify_auto_upgrade_report,
)
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.domain.task import Task, TaskStatus
from pc_agent.errors import ConfigError, PcAgentError
from pc_agent.gui.controller import (
    GuiController,
    format_auto_upgrade_run,
    format_auto_upgrade_status,
)
from pc_agent.release import audit_install, build_release_bundle
from pc_agent.storage.db import LATEST_SCHEMA_VERSION, Database
from pc_agent.storage.repositories import TaskRepository
from pc_agent.tools import build_default_registry
from pc_agent.updates import default_signature_path, list_updates


def _future_version() -> str:
    major, minor, patch = (int(part) for part in __version__.split("."))
    return f"{major}.{minor + 1}.{patch}"


FUTURE = _future_version()
FEED_URL = "https://updates.example.com/self-healing-pc-agent/stable.json"
BUNDLE_NAME = f"release-{FUTURE}.zip"
SIGNATURE_NAME = f"{BUNDLE_NAME}.sig.json"
BUNDLE_URL = f"https://updates.example.com/self-healing-pc-agent/{BUNDLE_NAME}"
SIGNATURE_URL = (
    f"https://updates.example.com/self-healing-pc-agent/{SIGNATURE_NAME}"
)
NOW = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)


def _write_project(root: Path, version: str, *, value: int) -> None:
    (root / "src" / "pc_agent").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(exist_ok=True)
    (root / "scripts").mkdir(exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "workspace").mkdir(exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "self-healing-pc-agent"\n'
        f'version = "{version}"\n',
        encoding="utf-8",
    )
    (root / "src" / "pc_agent" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    (root / "src" / "pc_agent" / "module.py").write_text(
        f"VALUE = {value}\n", encoding="utf-8"
    )
    (root / "README.md").write_text("Background update test.\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("Changes.\n", encoding="utf-8")
    (root / ".env.example").write_text("EXAMPLE=1\n", encoding="utf-8")
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")
    (root / "docs" / "phase56.md").write_text("Phase 56.\n", encoding="utf-8")
    (root / "scripts" / "install_windows.ps1").write_text(
        "Write-Host ok\n", encoding="utf-8"
    )
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (root / "workspace" / ".gitkeep").write_text("", encoding="utf-8")


def _settings(root: Path, **overrides: str) -> Settings:
    values = {
        "PC_AGENT_PROVIDER_ORDER": "mock",
        "PC_AGENT_CREDENTIAL_STORE": "env",
        "PC_AGENT_ONLINE_UPDATES": "1",
        "PC_AGENT_UPDATE_FEED_URL": FEED_URL,
        "PC_AGENT_UPDATE_CHANNEL": "stable",
        "PC_AGENT_AUTO_UPGRADE": "1",
        "PC_AGENT_AUTO_UPGRADE_MODE": "stage-only",
        "PC_AGENT_AUTO_UPGRADE_INTERVAL_HOURS": "24",
        "PC_AGENT_AUTO_UPGRADE_IDLE_MINUTES": "15",
        "PC_AGENT_AUTO_UPGRADE_MAINTENANCE_WINDOW": "02:00-05:00",
        "PC_AGENT_AUTO_UPGRADE_REQUIRE_AC_POWER": "1",
        **overrides,
    }
    settings = Settings.from_env(root, environ=values, load_dotenv=False)
    settings.ensure_directories()
    return settings


def _signed_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[bytes, bytes, str]:
    source = tmp_path / "future-source"
    _write_project(source, FUTURE, value=57)
    bundle = tmp_path / BUNDLE_NAME
    monkeypatch.setattr(release_module, "__version__", FUTURE)
    build_release_bundle(source, bundle)

    keys = tmp_path / "vendor-keys"
    assert (
        release_admin.main(
            ["keygen", "--out-dir", str(keys), "--key-id", "release-test"]
        )
        == 0
    )
    capsys.readouterr()
    public_key = next(
        line.split("=", 1)[1]
        for line in (keys / "release-public-key.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.startswith("PC_AGENT_RELEASE_PUBLIC_KEY=")
    )
    assert (
        release_admin.main(
            [
                "sign",
                str(bundle),
                "--private-key",
                str(keys / "release-private-key.txt"),
                "--key-id",
                "release-test",
            ]
        )
        == 0
    )
    capsys.readouterr()
    return (
        bundle.read_bytes(),
        default_signature_path(bundle).read_bytes(),
        public_key,
    )


def _feed_bytes(
    bundle: bytes,
    *,
    signature_url: str | None = SIGNATURE_NAME,
) -> bytes:
    payload: dict[str, Any] = {
        "format": 1,
        "app": "self-healing-pc-agent",
        "channel": "stable",
        "version": FUTURE,
        "bundle_url": BUNDLE_NAME,
        "bundle_sha256": hashlib.sha256(bundle).hexdigest(),
        "size_bytes": len(bundle),
        "published_at": "2026-01-01T00:00:00Z",
    }
    if signature_url is not None:
        payload["signature_url"] = signature_url
    return json.dumps(payload, indent=2).encode("utf-8")


class _FakeTransport:
    def __init__(self, mapping: dict[str, bytes]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, url: str, max_bytes: int, timeout: float) -> bytes:
        self.calls.append(url)
        if url not in self.mapping:
            raise RuntimeError(f"unexpected URL {url}")
        return self.mapping[url]


def _transport(bundle: bytes, signature: bytes, feed: bytes) -> _FakeTransport:
    return _FakeTransport(
        {FEED_URL: feed, BUNDLE_URL: bundle, SIGNATURE_URL: signature}
    )


def _signed_settings(root: Path, public_key: str, **overrides: str) -> Settings:
    return _settings(
        root,
        PC_AGENT_RELEASE_PUBLIC_KEY=public_key,
        PC_AGENT_RELEASE_KEY_ID="release-test",
        PC_AGENT_REQUIRE_SIGNED_UPDATES="1",
        **overrides,
    )


def _state(settings: Settings) -> dict[str, Any]:
    return json.loads(auto_upgrade_state_path(settings).read_text(encoding="utf-8"))


def test_phase56_configuration_defaults_validation_and_disabled_run(
    tmp_path: Path,
) -> None:
    root = tmp_path / "target"
    _write_project(root, __version__, value=56)

    default = Settings.from_env(
        root,
        environ={"PC_AGENT_CREDENTIAL_STORE": "env"},
        load_dotenv=False,
    )
    assert default.auto_upgrade_enabled is False
    assert default.auto_upgrade_mode == "stage-only"
    assert default.auto_upgrade_interval_hours == 24
    assert default.auto_upgrade_idle_minutes == 15
    assert default.auto_upgrade_require_ac_power is True
    summary = default.public_summary()
    assert summary["auto_upgrade"] == "no"
    assert summary["auto_upgrade_mode"] == "stage-only"
    assert summary["auto_upgrade_maintenance_window"] == "any"

    disabled_report = run_auto_upgrade(
        default,
        force=True,
        now=NOW,
        local_now=NOW,
        platform_name="win32",
    )
    assert disabled_report.status == "disabled"
    assert disabled_report.exit_code == 0
    assert verify_auto_upgrade_report(disabled_report.report_path).status == (
        "disabled"
    )

    misconfigured = _settings(root)
    blocked = run_auto_upgrade(
        misconfigured,
        force=True,
        now=NOW,
        local_now=NOW,
        platform_name="win32",
    )
    assert blocked.status == "misconfigured"
    assert blocked.exit_code == 1
    assert "no release public key" in (blocked.error or "")

    invalid_cases = [
        ({"PC_AGENT_AUTO_UPGRADE": "sometimes"}, "PC_AGENT_AUTO_UPGRADE"),
        ({"PC_AGENT_AUTO_UPGRADE_MODE": "always"}, "AUTO_UPGRADE_MODE"),
        ({"PC_AGENT_AUTO_UPGRADE_INTERVAL_HOURS": "0"}, "INTERVAL_HOURS"),
        ({"PC_AGENT_AUTO_UPGRADE_IDLE_MINUTES": "999"}, "IDLE_MINUTES"),
        (
            {"PC_AGENT_AUTO_UPGRADE_MAINTENANCE_WINDOW": "02:00-02:00"},
            "MAINTENANCE_WINDOW",
        ),
        (
            {"PC_AGENT_AUTO_UPGRADE_REQUIRE_AC_POWER": "maybe"},
            "REQUIRE_AC_POWER",
        ),
    ]
    for override, needle in invalid_cases:
        with pytest.raises(ConfigError, match=needle):
            _settings(root, **override)

    status = get_auto_upgrade_status(default, platform_name="win32")
    assert status.enabled is False
    assert status.ready is False
    assert status.scheduler_profile == "windows-scheduled-task"
    audit = {check.name: check for check in audit_install(default).checks}
    assert audit["auto_upgrade"].status == "info"
    audit = {check.name: check for check in audit_install(misconfigured).checks}
    assert audit["auto_upgrade"].status == "warn"


def test_phase56_stage_only_downloads_verifies_and_never_applies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle, signature, public_key = _signed_bundle(tmp_path, monkeypatch, capsys)
    target = tmp_path / "target"
    _write_project(target, __version__, value=56)
    settings = _signed_settings(target, public_key)
    transport = _transport(bundle, signature, _feed_bytes(bundle))

    report = run_auto_upgrade(
        settings,
        transport=transport,
        now=NOW,
        local_now=NOW,
        platform_name="win32",
    )

    assert report.status == "staged"
    assert report.reason == "staged_waiting_manual_apply"
    assert report.feed_version == FUTURE
    assert report.applied_update_id is None
    assert report.restart_required is False
    assert verify_auto_upgrade_report(report.report_path).status == "staged"
    assert (target / "src" / "pc_agent" / "module.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 56\n"
    updates = list_updates(settings)
    assert len(updates) == 1
    assert updates[0].status == "staged"
    assert updates[0].signature_status == "verified"
    assert _state(settings)["staged_version"] == FUTURE
    calls_after_first = list(transport.calls)
    assert calls_after_first.count(FEED_URL) == 2
    assert BUNDLE_URL in calls_after_first
    assert SIGNATURE_URL in calls_after_first

    second = run_auto_upgrade(
        settings,
        transport=transport,
        now=NOW,
        local_now=NOW,
        platform_name="win32",
    )
    assert second.status == "staged"
    assert second.reason == "staged_waiting_manual_apply"
    assert transport.calls == calls_after_first


def test_phase56_apply_when_idle_gates_then_applies_and_reinstalls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle, signature, public_key = _signed_bundle(tmp_path, monkeypatch, capsys)
    target = tmp_path / "target"
    _write_project(target, __version__, value=56)
    settings = _signed_settings(
        target,
        public_key,
        PC_AGENT_AUTO_UPGRADE_MODE="apply-when-idle",
        PC_AGENT_AUTO_UPGRADE_REINSTALL="1",
    )
    transport = _transport(bundle, signature, _feed_bytes(bundle))

    deferred = run_auto_upgrade(
        settings,
        transport=transport,
        now=NOW,
        local_now=NOW,
        idle_seconds=0,
        on_ac_power=True,
        active_task_count=0,
        platform_name="win32",
    )
    assert deferred.status == "deferred"
    assert deferred.reason == "user_not_idle"
    assert (target / "src" / "pc_agent" / "module.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 56\n"
    calls_after_stage = list(transport.calls)

    active = run_auto_upgrade(
        settings,
        transport=transport,
        force=True,
        now=NOW,
        local_now=NOW,
        idle_seconds=3600,
        on_ac_power=True,
        active_task_count=1,
        platform_name="win32",
    )
    assert active.status == "deferred"
    assert active.reason == "active_tasks"
    assert transport.calls == calls_after_stage

    reinstall_calls: list[tuple[list[str], Path, float]] = []

    def fake_reinstall(
        argv: Sequence[str], cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        reinstall_calls.append(([str(part) for part in argv], cwd, timeout))
        return subprocess.CompletedProcess(list(argv), 0, "ok", "")

    applied = run_auto_upgrade(
        settings,
        transport=transport,
        force=True,
        now=NOW,
        local_now=NOW,
        idle_seconds=3600,
        on_ac_power=True,
        active_task_count=0,
        reinstall_runner=fake_reinstall,
        platform_name="win32",
    )
    assert applied.status == "applied"
    assert applied.reason == "applied"
    assert applied.reinstall_status == "succeeded"
    assert applied.restart_required is True
    assert verify_auto_upgrade_report(applied.report_path).status == "applied"
    assert (target / "src" / "pc_agent" / "module.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 57\n"
    assert reinstall_calls
    assert reinstall_calls[0][0][1:] == [
        "-m",
        "pip",
        "install",
        "--no-deps",
        "--no-build-isolation",
        "-e",
        ".",
    ]
    state = _state(settings)
    assert state["applied_version"] == FUTURE
    assert state["staged_update_id"] is None
    assert state["restart_required"] is True


def test_phase56_maintenance_window_and_unsigned_feed_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle, signature, public_key = _signed_bundle(tmp_path, monkeypatch, capsys)
    outside_target = tmp_path / "outside"
    _write_project(outside_target, __version__, value=56)
    outside_settings = _signed_settings(outside_target, public_key)
    transport = _transport(bundle, signature, _feed_bytes(bundle))
    midday = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    deferred = run_auto_upgrade(
        outside_settings,
        transport=transport,
        force=True,
        now=midday,
        local_now=midday,
        platform_name="win32",
    )
    assert deferred.status == "deferred"
    assert deferred.reason == "outside_maintenance_window"
    assert transport.calls == []

    unsigned_target = tmp_path / "unsigned"
    _write_project(unsigned_target, __version__, value=56)
    unsigned_settings = _signed_settings(unsigned_target, public_key)
    unsigned_transport = _FakeTransport(
        {
            FEED_URL: _feed_bytes(bundle, signature_url=None),
            BUNDLE_URL: bundle,
        }
    )
    failed = run_auto_upgrade(
        unsigned_settings,
        transport=unsigned_transport,
        force=True,
        now=NOW,
        local_now=NOW,
        platform_name="win32",
    )
    assert failed.status == "failed"
    assert failed.reason == "download_or_stage_failed"
    assert failed.exit_code == 1
    assert list_updates(unsigned_settings) == ()
    assert _state(unsigned_settings)["consecutive_failures"] == 1


def test_phase56_active_task_count_ignores_queued_and_terminal_tasks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "target"
    _write_project(root, __version__, value=56)
    settings = _settings(root, PC_AGENT_AUTO_UPGRADE="0")
    database = Database(settings.database_path)
    database.initialize()
    tasks = TaskRepository(database)
    tasks.create(Task(instruction="Queued", status=TaskStatus.QUEUED))
    tasks.create(Task(instruction="Running", status=TaskStatus.RUNNING))
    tasks.create(Task(instruction="Done", status=TaskStatus.SUCCEEDED))
    assert database.schema_version() == LATEST_SCHEMA_VERSION
    assert count_active_tasks(settings) == 1


def test_phase56_windows_scheduler_and_reserved_platform_profiles(
    tmp_path: Path,
) -> None:
    profiles = {
        profile.profile_id: profile.status
        for profile in list_auto_upgrade_scheduler_profiles()
    }
    assert profiles == {
        "windows-scheduled-task": "implemented",
        "linux-systemd-user": "implemented",
        "macos-launchd": "planned",
    }

    root = tmp_path / "target"
    _write_project(root, __version__, value=56)
    executable = root / ".venv" / "Scripts" / "pc-agent.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fake")
    settings = _settings(root)

    with pytest.raises(PcAgentError, match="only on Windows"):
        install_windows_auto_upgrade_schedule(
            settings,
            executable=executable,
            platform_name="linux",
        )
    with pytest.raises(PcAgentError, match="task name"):
        build_windows_auto_upgrade_task_script(
            settings,
            task_name="bad/name",
            executable=executable,
        )

    script = build_windows_auto_upgrade_task_script(
        settings, executable=executable
    )
    assert "SelfHealingPCAgentAutoUpgrade" in script
    assert "update auto-run" in script
    assert "-AtLogOn" in script
    assert "-RepetitionInterval (New-TimeSpan -Hours 1)" in script
    assert "-RunLevel Limited" in script
    assert "-StartWhenAvailable" in script

    calls: list[list[str]] = []

    def installed_runner(
        argv: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        calls.append([str(part) for part in argv])
        return subprocess.CompletedProcess(list(argv), 0, "installed", "")

    installed = install_windows_auto_upgrade_schedule(
        settings,
        executable=executable,
        runner=installed_runner,
        platform_name="win32",
    )
    assert installed.installed is True
    encoded = calls[0][-1]
    decoded = base64.b64decode(encoded).decode("utf-16le")
    assert "Register-ScheduledTask" in decoded

    def status_runner(
        argv: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            list(argv),
            0,
            '{"installed":true,"state":"Ready"}\n',
            "",
        )

    schedule_status = get_windows_auto_upgrade_schedule(
        runner=status_runner, platform_name="win32"
    )
    assert schedule_status.installed is True
    assert schedule_status.state == "Ready"

    def removed_runner(
        argv: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 0, "removed", "")

    removed = uninstall_windows_auto_upgrade_schedule(
        runner=removed_runner, platform_name="win32"
    )
    assert removed.installed is False


def test_phase56_cli_gui_report_verification_and_scope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "target"
    _write_project(root, __version__, value=56)
    report_path = tmp_path / "auto-run.json"

    assert (
        main(
            [
                "--root",
                str(root),
                "update",
                "auto-run",
                "--report",
                str(report_path),
                "--json",
            ]
        )
        == 0
    )
    run_output = json.loads(capsys.readouterr().out)
    assert run_output["status"] == "disabled"
    assert (
        main(
            [
                "--root",
                str(root),
                "update",
                "auto-verify-report",
                str(report_path),
                "--json",
            ]
        )
        == 0
    )
    verification_output = json.loads(capsys.readouterr().out)
    assert verification_output["status"] == "disabled"

    tampered = tmp_path / "tampered.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["status"] = "applied"
    tampered.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PcAgentError, match="hash"):
        verify_auto_upgrade_report(tampered)

    settings = Settings.from_env(
        root,
        environ={"PC_AGENT_CREDENTIAL_STORE": "env"},
        load_dotenv=False,
    )
    database = Database(settings.database_path)
    database.initialize()
    controller = GuiController(settings, database=database)
    status = controller.auto_upgrade_status()
    assert status.enabled is False
    assert "auto_upgrade=no" in controller.signed_updates_summary()
    assert "Background auto-upgrade status" in format_auto_upgrade_status(status)
    run = controller.auto_upgrade_run_now()
    assert run.status == "disabled"
    assert "Background auto-upgrade:" in format_auto_upgrade_run(run)

    assert __version__ == "0.81.2"
    assert len(build_default_registry().list_tools()) == 69
    assert database.schema_version() == 18

    project_root = Path(__file__).resolve().parents[1]
    phase_doc = (project_root / "docs" / "phase56.md").read_text(
        encoding="utf-8"
    )
    readme = (project_root / "README.md").read_text(encoding="utf-8")
    phase24 = (project_root / "docs" / "phase24.md").read_text(
        encoding="utf-8"
    )
    phase55 = (project_root / "docs" / "phase55.md").read_text(
        encoding="utf-8"
    )
    for phrase in (
        "PC_AGENT_AUTO_UPGRADE=1",
        "stage-only",
        "apply-when-idle",
        "Windows Scheduled Task",
        "signed HTTPS",
        "Linux systemd user timer",
        "macOS LaunchAgent",
        "planned",
    ):
        assert phrase in phase_doc
    assert "Background auto-upgrade" in readme
    assert "Phase 56" in phase24
    assert "Phase 56" in phase55
