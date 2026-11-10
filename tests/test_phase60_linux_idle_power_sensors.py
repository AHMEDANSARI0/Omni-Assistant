"""Phase 60: Linux idle and AC-power sensors for background auto-upgrade."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from pc_agent import __version__
from pc_agent import auto_upgrade as auto_module
from pc_agent.auto_upgrade import (
    AutoUpgradeRunReport,
    get_ac_power,
    get_idle_seconds,
    run_auto_upgrade,
    verify_auto_upgrade_report,
)
from pc_agent.config import Settings
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)
NOW_SECONDS = 10_000.0
NOW_US = int(NOW_SECONDS * 1_000_000)


def _future_version() -> str:
    major, minor, patch = (int(part) for part in __version__.split("."))
    return f"{major}.{minor + 1}.{patch}"


def _settings(root: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "root_dir": root,
        "workspace_dir": root / "workspace",
        "data_dir": root / "data",
        "database_path": root / "data" / "agent.db",
        "online_updates_enabled": True,
        "update_feed_url": "https://updates.example.com/stable.json",
        "release_public_key": base64.urlsafe_b64encode(b"x" * 32)
        .decode("ascii")
        .rstrip("="),
        "release_key_id": "release-test",
        "require_signed_updates": True,
        "auto_upgrade_enabled": True,
        "auto_upgrade_mode": "apply-when-idle",
        "auto_upgrade_idle_minutes": 15,
        "auto_upgrade_require_ac_power": True,
        "auto_upgrade_maintenance_window": "02:00-05:00",
    }
    values.update(overrides)
    settings = Settings(**values)  # type: ignore[arg-type]
    settings.ensure_directories()
    return settings


def _session_properties(
    session_id: str,
    *,
    session_class: str = "user",
    state: str = "active",
    idle_hint: str = "yes",
    idle_since_us: int = NOW_US - 3_600_000_000,
) -> str:
    return (
        f"Id={session_id}\n"
        f"Class={session_class}\n"
        f"State={state}\n"
        f"IdleHint={idle_hint}\n"
        f"IdleSinceHintMonotonic={idle_since_us}\n"
    )


class _FakeLoginctlRunner:
    def __init__(
        self,
        *,
        sessions: list[dict[str, object]] | None = None,
        properties: dict[str, str] | None = None,
        external_power: str = "yes",
        list_stdout: str | None = None,
        list_returncode: int = 0,
        show_returncode: int = 0,
    ) -> None:
        self.sessions = [] if sessions is None else sessions
        self.properties = {} if properties is None else properties
        self.external_power = external_power
        self.list_stdout = list_stdout
        self.list_returncode = list_returncode
        self.show_returncode = show_returncode
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(
        self, argv: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        args = [str(part) for part in argv]
        self.calls.append(args)
        self.timeouts.append(timeout)
        if "list-sessions" in args:
            stdout = (
                json.dumps(self.sessions)
                if self.list_stdout is None
                else self.list_stdout
            )
            return subprocess.CompletedProcess(
                args, self.list_returncode, stdout, ""
            )
        if "show-session" in args:
            if "--property=OnExternalPower" in args:
                return subprocess.CompletedProcess(
                    args,
                    self.show_returncode,
                    f"OnExternalPower={self.external_power}\n",
                    "",
                )
            session_id = args[args.index("show-session") + 1]
            return subprocess.CompletedProcess(
                args,
                self.show_returncode,
                self.properties.get(session_id, ""),
                "",
            )
        raise AssertionError(f"unexpected loginctl argv: {args}")


def _idle(runner: _FakeLoginctlRunner) -> float | None:
    return get_idle_seconds(
        "linux",
        runner=runner,
        linux_uid=1000,
        monotonic_seconds=NOW_SECONDS,
    )


def test_phase60_linux_idle_uses_minimum_interactive_logind_session() -> None:
    runner = _FakeLoginctlRunner(
        sessions=[
            {"session": "1", "uid": 999, "user": "other"},
            {"session": "2", "uid": 1000, "user": "alice"},
            {"session": "3", "uid": 1000, "user": "alice"},
            {"session": "4", "uid": 1000, "user": "alice"},
        ],
        properties={
            # The per-user manager session does not represent input and must
            # not make every desktop look permanently active.
            "2": _session_properties(
                "2", session_class="manager", idle_hint="no", idle_since_us=0
            ),
            "3": _session_properties(
                "3", idle_since_us=NOW_US - 3_600_000_000
            ),
            "4": _session_properties(
                "4",
                session_class="lock-screen",
                idle_since_us=NOW_US - 900_000_000,
            ),
        },
    )

    assert _idle(runner) == pytest.approx(900.0)
    assert runner.timeouts and set(runner.timeouts) == {5.0}
    assert runner.calls[0][:4] == [
        "loginctl",
        "--no-ask-password",
        "--no-pager",
        "list-sessions",
    ]
    assert "--json=short" in runner.calls[0]
    assert all(
        "--property=IdleSinceHintMonotonic" in call
        for call in runner.calls[1:]
    )


def test_phase60_linux_idle_active_and_ambiguous_readings_fail_closed() -> None:
    one_session = [{"session": "3", "uid": 1000, "user": "alice"}]
    active = _FakeLoginctlRunner(
        sessions=one_session,
        properties={"3": _session_properties("3", idle_hint="no")},
    )
    assert _idle(active) == 0.0

    ambiguous_properties = [
        _session_properties("3", idle_since_us=0),
        _session_properties("3", idle_since_us=NOW_US + 2_000_000),
        _session_properties("3", state="unknown"),
        _session_properties("wrong"),
        "Id=3\nClass=user\nState=active\nIdleHint=yes\n",
        (
            "Id=3\nClass=user\nState=active\nIdleHint=perhaps\n"
            "IdleSinceHintMonotonic=123\n"
        ),
    ]
    for properties in ambiguous_properties:
        runner = _FakeLoginctlRunner(
            sessions=one_session, properties={"3": properties}
        )
        assert _idle(runner) is None

    noninteractive = _FakeLoginctlRunner(
        sessions=one_session,
        properties={
            "3": _session_properties("3", session_class="background")
        },
    )
    assert _idle(noninteractive) is None

    closing = _FakeLoginctlRunner(
        sessions=one_session,
        properties={"3": _session_properties("3", state="closing")},
    )
    assert _idle(closing) is None

    malformed_lists = [
        "not json",
        "{}",
        json.dumps([{"session": "3", "uid": "1000"}]),
        json.dumps([{"session": "../3", "uid": 1000}]),
        json.dumps(
            [
                {"session": "3", "uid": 1000},
                {"session": "3", "uid": 1000},
            ]
        ),
    ]
    for list_stdout in malformed_lists:
        assert _idle(_FakeLoginctlRunner(list_stdout=list_stdout)) is None

    assert _idle(_FakeLoginctlRunner(sessions=[])) is None
    assert _idle(_FakeLoginctlRunner(list_returncode=1)) is None
    assert (
        _idle(
            _FakeLoginctlRunner(
                sessions=one_session,
                properties={"3": _session_properties("3")},
                show_returncode=1,
            )
        )
        is None
    )


def _write_supply(
    root: Path,
    name: str,
    supply_type: str,
    values: dict[str, str],
) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "type").write_text(f"{supply_type}\n", encoding="utf-8")
    for key, value in values.items():
        (directory / key).write_text(f"{value}\n", encoding="utf-8")


class _NoLoginctlRunner:
    def __call__(
        self, argv: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"logind fallback unexpectedly used: {argv} ({timeout})")


def test_phase60_linux_ac_power_sysfs_and_logind_fallback(
    tmp_path: Path,
) -> None:
    determinate_cases: list[tuple[str, dict[str, tuple[str, dict[str, str]]], bool]] = [
        (
            "mains-online",
            {
                "AC": ("Mains", {"online": "1"}),
                "BAT0": ("Battery", {"present": "1", "status": "Discharging"}),
            },
            True,
        ),
        (
            "mains-offline",
            {
                "AC": ("Mains", {"online": "0"}),
                "BAT0": ("Battery", {"present": "1", "status": "Full"}),
            },
            False,
        ),
        (
            "battery-charging",
            {"BAT0": ("Battery", {"present": "1", "status": "Charging"})},
            True,
        ),
        (
            "battery-discharging",
            {"BAT0": ("Battery", {"present": "1", "status": "Discharging"})},
            False,
        ),
    ]
    for label, supplies, expected in determinate_cases:
        root = tmp_path / label
        root.mkdir()
        for name, (supply_type, values) in supplies.items():
            _write_supply(root, name, supply_type, values)
        assert (
            get_ac_power(
                "linux",
                runner=_NoLoginctlRunner(),
                power_supply_root=root,
            )
            is expected
        )

    ambiguous = tmp_path / "not-charging"
    ambiguous.mkdir()
    _write_supply(
        ambiguous,
        "BAT0",
        "Battery",
        {"present": "1", "status": "Not charging"},
    )
    assert (
        get_ac_power(
            "linux",
            runner=_FakeLoginctlRunner(external_power="yes"),
            power_supply_root=ambiguous,
        )
        is True
    )
    assert (
        get_ac_power(
            "linux",
            runner=_FakeLoginctlRunner(external_power="no"),
            power_supply_root=ambiguous,
        )
        is False
    )
    assert (
        get_ac_power(
            "linux",
            runner=_FakeLoginctlRunner(external_power="perhaps"),
            power_supply_root=ambiguous,
        )
        is None
    )

    empty = tmp_path / "empty"
    empty.mkdir()
    assert (
        get_ac_power(
            "linux",
            runner=_FakeLoginctlRunner(external_power="yes"),
            power_supply_root=empty,
        )
        is True
    )


def _report(root: Path) -> AutoUpgradeRunReport:
    return AutoUpgradeRunReport(
        report_path=root / "report.json",
        state_path=root / "state.json",
        run_id="auto-20260101T030000Z-1234abcd",
        started_at="2026-01-01T03:00:00Z",
        mode="apply-when-idle",
        current_version=__version__,
        client_platform="linux",
        maintenance_window="02:00-05:00",
    )


def test_phase60_unknown_sensors_defer_on_every_platform(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "app")
    for platform in ("linux", "darwin", "win32", "unknown"):
        report = _report(tmp_path)
        reasons = auto_module._apply_gates(
            settings,
            report,
            local_now=NOW,
            idle_seconds=None,
            on_ac_power=None,
            active_task_count=0,
            platform_name=platform,
        )
        assert reasons == ["idle_unknown", "ac_power_unknown"]
        assert report.idle_seconds is None
        assert report.on_ac_power is None
        assert report.check("idle_gate").status == "failed"

    report = _report(tmp_path)
    assert (
        auto_module._apply_gates(
            settings,
            report,
            local_now=NOW,
            idle_seconds=3600,
            on_ac_power=True,
            active_task_count=0,
            platform_name="linux",
        )
        == []
    )
    assert report.check("idle_gate").status == "passed"

    relaxed = _settings(
        tmp_path / "relaxed",
        auto_upgrade_idle_minutes=0,
        auto_upgrade_require_ac_power=False,
    )
    assert (
        auto_module._apply_gates(
            relaxed,
            _report(tmp_path),
            local_now=NOW,
            idle_seconds=None,
            on_ac_power=None,
            active_task_count=0,
            platform_name="linux",
        )
        == []
    )


def test_phase60_linux_sensor_readings_drive_worker_apply_and_defer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path / "app")
    pending = SimpleNamespace(
        update_id="upd-phase60",
        bundle_version=_future_version(),
        signature_status="verified",
    )
    monkeypatch.setattr(
        auto_module, "_pending_staged_update", lambda _settings: pending
    )

    apply_calls: list[str] = []

    def fake_apply(_settings: Settings, update_id: str) -> object:
        apply_calls.append(update_id)
        return pending

    idle_runner = _FakeLoginctlRunner(
        sessions=[{"session": "3", "uid": 1000, "user": "alice"}],
        properties={"3": _session_properties("3")},
    )
    idle_value = _idle(idle_runner)
    power_root = tmp_path / "power"
    power_root.mkdir()
    _write_supply(power_root, "AC", "Mains", {"online": "1"})
    power_value = get_ac_power(
        "linux", runner=_NoLoginctlRunner(), power_supply_root=power_root
    )
    monkeypatch.setattr(
        auto_module, "get_idle_seconds", lambda _platform=None: idle_value
    )
    monkeypatch.setattr(
        auto_module, "get_ac_power", lambda _platform=None: power_value
    )

    applied = run_auto_upgrade(
        settings,
        force=True,
        now=NOW,
        local_now=NOW,
        active_task_count=0,
        apply_function=fake_apply,
        platform_name="linux",
    )
    assert applied.status == "applied"
    assert applied.idle_seconds == pytest.approx(3600.0)
    assert applied.on_ac_power is True
    assert apply_calls == ["upd-phase60"]
    assert verify_auto_upgrade_report(applied.report_path).status == "applied"

    monkeypatch.setattr(
        auto_module, "get_idle_seconds", lambda _platform=None: None
    )
    deferred = run_auto_upgrade(
        settings,
        force=True,
        now=NOW,
        local_now=NOW,
        active_task_count=0,
        apply_function=fake_apply,
        platform_name="linux",
    )
    assert deferred.status == "deferred"
    assert deferred.reason == "idle_unknown"
    assert apply_calls == ["upd-phase60"]
    assert verify_auto_upgrade_report(deferred.report_path).status == "deferred"


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("loginctl") is None,
    reason="live logind sensor oracle requires Linux loginctl",
)
def test_phase60_live_loginctl_sensor_oracle_never_crashes() -> None:
    idle = get_idle_seconds("linux")
    assert idle is None or idle >= 0
    power = get_ac_power("linux")
    assert power in {True, False, None}


def test_phase60_scope_inventory_and_documentation() -> None:
    assert __version__ == "0.82.12"
    assert LATEST_SCHEMA_VERSION == 18
    assert len(build_default_registry().list_tools()) == 69

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "docs/phase60.md",
        "tests/test_phase60_linux_idle_power_sensors.py",
    ):
        assert expected in release_names

    module = (ROOT / "src/pc_agent/auto_upgrade.py").read_text(encoding="utf-8")
    phase = (ROOT / "docs/phase60.md").read_text(encoding="utf-8")
    phase59 = (ROOT / "docs/phase59.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tools = (ROOT / "docs/tools.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "_get_linux_idle_seconds" in module
    assert "_get_linux_ac_power_from_sysfs" in module
    assert "OnExternalPower" in module
    for text in (phase, phase59, readme, tools):
        assert "Linux Idle and AC Power Sensors" in text
    assert "systemd-logind" in phase
    assert "/sys/class/power_supply" in phase
    assert "## 0.62.0 - PHASE 60 Linux Idle and AC Power Sensors" in (
        changelog.splitlines()
    )
