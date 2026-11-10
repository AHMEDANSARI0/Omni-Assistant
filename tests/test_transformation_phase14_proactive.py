"""Transformation Phase 14: proactive intelligence, notifications and briefings.

Every test drives the real engine against a real SQLite database with a pinned
clock, so quiet hours, cooldowns, hourly caps and repeat scheduling are asserted
exactly instead of being inferred. Where the policy must refuse or defer, the
test asserts the refusal *and* the recorded reason.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pc_agent import __version__
from pc_agent.api_server import LocalApiServer
from pc_agent.baseline import TRANSFORMATION_LABEL, TRANSFORMATION_PHASE
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.diagnostics import build_diagnostics, proactive_summary_line
from pc_agent.errors import ConfigError, ProactiveError
from pc_agent.gui.controller import GuiController
from pc_agent.proactive import (
    ProactiveEngine,
    ProactiveStore,
    in_quiet_window,
    next_quiet_end,
    parse_due,
    parse_quiet_window,
)
from pc_agent.release import audit_install
from pc_agent.service import LocalService
from pc_agent.storage.db import LATEST_SCHEMA_VERSION, Database
from pc_agent.web_ui import INDEX_HTML

# ---------------------------------------------------------------- fixtures ---


def _settings(tmp_path: Path, **overrides: str) -> Settings:
    settings = Settings.from_env(tmp_path, environ=dict(overrides), load_dotenv=False)
    settings.ensure_directories()
    return settings


def _engine(tmp_path: Path, *, now: datetime, **overrides: str) -> ProactiveEngine:
    settings = _settings(tmp_path, **overrides)
    database = Database(settings.database_path)
    database.initialize()
    return ProactiveEngine(settings, database, clock=lambda: now)


def _at(hour: int, minute: int = 0, day_offset: int = 0) -> datetime:
    base = datetime(2026, 11, 5, tzinfo=UTC).astimezone()
    return (base + timedelta(days=day_offset)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )


# ------------------------------------------------------------------ identity ---


def test_phase14_identity_is_wired_through_the_product() -> None:
    assert __version__ == "0.82.12"
    assert TRANSFORMATION_PHASE == 18
    assert "Production" in TRANSFORMATION_LABEL
    assert LATEST_SCHEMA_VERSION == 18


def test_schema_v16_creates_the_proactive_ledger(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    assert database.initialize() == 18
    with database.connect() as connection:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {
        "reminders",
        "notifications",
        "proactive_briefings",
        "proactive_state",
    } <= names
    # Phase 13 tables survive migration 16.
    assert {"connector_events", "connector_deliveries", "connector_state"} <= names


# ------------------------------------------------------------------ parsing ---


def test_due_parsing_accepts_iso_offsets_and_local_times() -> None:
    now = _at(9, 0)
    assert parse_due("+15m", now=now) == now.astimezone(UTC) + timedelta(minutes=15)
    assert parse_due("+2h", now=now) == now.astimezone(UTC) + timedelta(hours=2)
    assert parse_due("-1m", now=now) == now.astimezone(UTC) - timedelta(minutes=1)
    # A naive ISO stamp means *local* wall clock, so the assertion compares
    # instants: the stored value is always UTC, whatever the host offset is.
    iso = parse_due("2026-12-01T09:30:00", now=now)
    assert iso == datetime(2026, 12, 1, 9, 30).astimezone(UTC)
    assert iso.astimezone().strftime("%H:%M") == "09:30"
    offset_iso = parse_due("2026-12-01T09:30:00+00:00", now=now)
    assert offset_iso == datetime(2026, 12, 1, 9, 30, tzinfo=UTC)
    same_day = parse_due("23:45", now=now)
    assert same_day.astimezone().strftime("%H:%M") == "23:45"
    next_day = parse_due("08:00", now=now)
    assert next_day.astimezone().date() > now.date()
    for bad in ("", "tomorrow", "+5x", "-0m", "25:00", "9:5"):
        with pytest.raises(ProactiveError):
            parse_due(bad, now=now)


def test_quiet_window_math_is_wraparound_aware() -> None:
    night = parse_quiet_window("22:00-07:00")
    assert night == (1320, 420)
    assert in_quiet_window(_at(23, 30), night) is True
    assert in_quiet_window(_at(2, 0), night) is True
    assert in_quiet_window(_at(7, 0), night) is False
    assert in_quiet_window(_at(12, 0), night) is False
    assert next_quiet_end(_at(23, 30), night).strftime("%H:%M") == "07:00"
    day = parse_quiet_window("09:00-17:00")
    assert in_quiet_window(_at(12, 0), day) is True
    assert in_quiet_window(_at(18, 0), day) is False
    assert parse_quiet_window("") is None
    assert parse_quiet_window("22:00") is None


def test_bad_quiet_hours_and_briefing_times_are_refused_not_ignored(tmp_path: Path) -> None:
    for environ, expected in (
        ({"PC_AGENT_QUIET_HOURS": "22-07"}, "PC_AGENT_QUIET_HOURS"),
        ({"PC_AGENT_QUIET_HOURS": "22:00-22:00"}, "must differ"),
        ({"PC_AGENT_BRIEFING_TIME": "24:00"}, "PC_AGENT_BRIEFING_TIME"),
        ({"PC_AGENT_NOTIFICATION_MAX_PER_HOUR": "0"}, "between 1 and 500"),
    ):
        with pytest.raises(ConfigError) as excinfo:
            _settings(tmp_path, **environ)
        assert expected in str(excinfo.value)


# ----------------------------------------------------------------- reminders ---


def test_reminder_crud_round_trip(tmp_path: Path) -> None:
    engine = _engine(tmp_path, now=_at(9, 0))
    created = engine.add_reminder(title="Stretch", due="+20m", note="desk break")
    assert created["status"] == "open"
    assert created["severity"] == "normal"
    assert created["fired_count"] == 0
    assert engine.reminders()["total"] == 1

    snoozed = engine.snooze_reminder(created["id"][:8], for_spec="+1h")
    assert snoozed["due_at"] != created["due_at"]
    assert snoozed["status"] == "open"

    done = engine.resolve_reminder(created["id"][:8])
    assert done["status"] == "done"
    assert engine.reminders(status="open")["total"] == 0

    deleted = engine.delete_reminder(created["id"])
    assert deleted["deleted"] is True
    assert engine.reminders()["total"] == 0


def test_reminder_validation_and_limits(tmp_path: Path) -> None:
    engine = _engine(tmp_path, now=_at(9, 0))
    with pytest.raises(ProactiveError):
        engine.add_reminder(title="   ", due="+5m")
    with pytest.raises(ProactiveError):
        engine.add_reminder(title="Bad severity", due="+5m", severity="urgent")
    with pytest.raises(ProactiveError):
        engine.add_reminder(title="Too fast", due="+5m", repeat_seconds=5)
    with pytest.raises(ProactiveError):
        engine.resolve_reminder("does-not-exist")

    limited = _engine(tmp_path / "limited", now=_at(9, 0), PC_AGENT_REMINDER_MAX_OPEN="2")
    limited.add_reminder(title="one", due="+1h")
    limited.add_reminder(title="two", due="+2h")
    with pytest.raises(ProactiveError) as excinfo:
        limited.add_reminder(title="three", due="+3h")
    assert "open reminders" in str(excinfo.value)


def test_proactive_engine_can_be_switched_off(tmp_path: Path) -> None:
    engine = _engine(tmp_path, now=_at(9, 0), PC_AGENT_ALLOW_PROACTIVE="0")
    assert engine.available() is False
    with pytest.raises(ProactiveError) as excinfo:
        engine.add_reminder(title="nope", due="+5m")
    assert "PC_AGENT_ALLOW_PROACTIVE" in str(excinfo.value)
    with pytest.raises(ProactiveError):
        engine.tick()


# ---------------------------------------------------------------------- tick ---


def test_tick_fires_due_reminders_once_and_reschedules_repeats(tmp_path: Path) -> None:
    now = _at(9, 0)
    engine = _engine(tmp_path, now=now)
    one_shot = engine.add_reminder(title="One shot", due="-1m")
    engine.add_reminder(title="Hourly", due="-1m", repeat_seconds=3600)

    first = engine.tick()
    fired = {item["reminder"]["title"] for item in first["fired"]}
    assert fired == {"One shot", "Hourly"}
    assert first["total"] == 2

    second = engine.tick()
    assert second["total"] == 0  # one-shot is done, hourly is not due yet
    reminders = {item["title"]: item for item in engine.reminders()["reminders"]}
    assert reminders["One shot"]["status"] == "done"
    assert reminders["One shot"]["fired_count"] == 1
    assert reminders["Hourly"]["status"] == "open"
    assert reminders["Hourly"]["fired_count"] == 1
    # due_at is UTC and now may carry any local offset; compare instants.
    assert datetime.fromisoformat(reminders["Hourly"]["due_at"]) > now.astimezone(UTC)

    later = ProactiveEngine(
        engine._settings,
        Database(engine._settings.database_path),
        clock=lambda: now + timedelta(hours=1, minutes=1),
    )
    third = later.tick()
    assert [item["reminder"]["title"] for item in third["fired"]] == ["Hourly"]
    assert one_shot["status"] == "open"  # the returned dict is the pre-tick snapshot


def test_quiet_hours_defer_and_then_release(tmp_path: Path) -> None:
    night = _at(23, 30)
    engine = _engine(tmp_path, now=night, PC_AGENT_QUIET_HOURS="22:00-07:00")
    status = engine.status()
    assert status["quiet_now"] is True
    assert status["quiet_ends_at"].endswith("07:00+00:00") or "T07:00" in status["quiet_ends_at"]

    engine.add_reminder(title="Quiet reminder", due="-1m")
    tick = engine.tick()
    assert tick["total"] == 0
    assert tick["deferred"] and "quiet hours" in tick["deferred"][0]["reason"]
    deferred_rows = engine.notifications()["notifications"]
    assert deferred_rows[0]["status"] == "deferred"
    assert "quiet hours until" in deferred_rows[0]["detail"]

    morning = ProactiveEngine(
        engine._settings,
        Database(engine._settings.database_path),
        clock=lambda: _at(7, 5, day_offset=1),
    )
    released = morning.tick()
    assert [item["reminder"]["title"] for item in released["fired"]] == ["Quiet reminder"]


def test_critical_notifications_may_pass_quiet_hours_only_when_allowed(tmp_path: Path) -> None:
    now = _at(23, 30)
    blocked = _engine(tmp_path / "blocked", now=now, PC_AGENT_QUIET_HOURS="22:00-07:00")
    blocked.add_reminder(title="Critical", due="-1m", severity="critical")
    assert blocked.tick()["total"] == 0

    allowed = _engine(
        tmp_path / "allowed",
        now=now,
        PC_AGENT_QUIET_HOURS="22:00-07:00",
        PC_AGENT_QUIET_HOURS_ALLOW_CRITICAL="1",
    )
    allowed.add_reminder(title="Critical", due="-1m", severity="critical")
    assert allowed.tick()["total"] == 1


def test_hourly_cap_defers_instead_of_flooding(tmp_path: Path) -> None:
    now = _at(12, 0)
    engine = _engine(tmp_path, now=now, PC_AGENT_NOTIFICATION_MAX_PER_HOUR="2")
    for index in range(4):
        engine.add_reminder(title=f"Due {index}", due="-1m")
    tick = engine.tick()
    assert tick["total"] == 2
    assert len(tick["deferred"]) == 2
    assert all("hourly limit" in item["reason"] for item in tick["deferred"])


def test_cooldown_suppresses_duplicate_keys(tmp_path: Path) -> None:
    now = _at(12, 0)
    engine = _engine(tmp_path, now=now, PC_AGENT_NOTIFICATION_COOLDOWN_SECONDS="600")
    first = engine.notify(title="Same thing", dedupe_key="disk:full")
    assert first["status"] == "delivered"
    second = engine.notify(title="Same thing", dedupe_key="disk:full")
    assert second["status"] == "deferred"
    assert "cooldown" in second["detail"]
    forced = engine.notify(title="Same thing", dedupe_key="disk:full", force=True)
    assert forced["status"] == "delivered"


def test_outbound_delivery_is_gated_and_never_loses_the_in_app_copy(tmp_path: Path) -> None:
    now = _at(12, 0)
    calls: list[tuple[str, str, str]] = []

    def ok_outbound(title: str, body: str, severity: str) -> dict[str, Any]:
        calls.append((title, body, severity))
        return {"ok": True, "detail": "delivered"}

    settings = _settings(tmp_path, PC_AGENT_ALLOW_PROACTIVE_OUTBOUND="1")
    engine = ProactiveEngine(
        settings,
        Database(settings.database_path),
        outbound=ok_outbound,
        clock=lambda: now,
    )
    delivered = engine.notify(title="Backup done", body="nightly", severity="low")
    assert delivered["channels"] == "in_app,chat"
    assert calls == [("Backup done", "nightly", "low")]

    def refusing_outbound(title: str, body: str, severity: str) -> dict[str, Any]:
        return {"ok": False, "detail": "chat connector is disabled"}

    refused = ProactiveEngine(
        settings,
        Database(settings.database_path),
        outbound=refusing_outbound,
        clock=lambda: now + timedelta(minutes=30),
    ).notify(title="Second", body="still in-app", force=True)
    assert refused["status"] == "delivered"
    assert refused["channels"] == "in_app"
    assert "outbound refused" in refused["detail"]

    def broken_outbound(title: str, body: str, severity: str) -> dict[str, Any]:
        raise RuntimeError("network gone")

    survived = ProactiveEngine(
        settings,
        Database(settings.database_path),
        outbound=broken_outbound,
        clock=lambda: now + timedelta(hours=2),
    ).notify(title="Third", body="still delivered", force=True)
    assert survived["status"] == "delivered"
    assert "outbound failed" in survived["detail"]

    off_settings = _settings(tmp_path / "off", PC_AGENT_ALLOW_PROACTIVE_OUTBOUND="0")
    in_app_only = ProactiveEngine(
        off_settings,
        Database(off_settings.database_path),
        outbound=ok_outbound,
        clock=lambda: now,
    ).notify(title="Local only")
    assert in_app_only["channels"] == "in_app"


# ----------------------------------------------------------------- briefing ---


def test_briefing_composes_real_sections_and_is_idempotent_per_day(tmp_path: Path) -> None:
    now = _at(9, 0)
    engine = _engine(
        tmp_path, now=now, PC_AGENT_BRIEFING_ENABLED="1", PC_AGENT_BRIEFING_TIME="08:00"
    )
    engine.add_reminder(title="Send the report", due="+1h")
    result = engine.run_briefing()
    sections = result["sections"]
    assert set(sections) == {"reminders", "calendar", "drive", "flows", "tasks", "system"}
    assert sections["reminders"]["items"] == 1
    assert "connectors are off" in sections["calendar"]["summary"]
    assert "drive" in sections["drive"]["summary"].lower()
    assert result["briefing"]["item_count"] >= 1
    assert "Daily briefing:" in result["briefing"]["summary"]
    assert result["notification"]["kind"] == "briefing"

    with pytest.raises(ProactiveError) as excinfo:
        engine.run_briefing()
    assert "already delivered" in str(excinfo.value)
    # an explicit force still works and is recorded
    forced = engine.run_briefing(force=True)
    assert forced["briefing"]["id"] != result["briefing"]["id"]
    assert engine.briefings()["total"] == 2


def test_briefing_respects_its_schedule_and_switch(tmp_path: Path) -> None:
    early = _engine(
        tmp_path / "early",
        now=_at(6, 0),
        PC_AGENT_BRIEFING_ENABLED="1",
        PC_AGENT_BRIEFING_TIME="08:00",
    )
    with pytest.raises(ProactiveError) as excinfo:
        early.run_briefing()
    assert "08:00" in str(excinfo.value)
    assert early.tick()["briefing"] is None  # not due, nothing attempted

    disabled = _engine(tmp_path / "disabled", now=_at(9, 0))
    with pytest.raises(ProactiveError) as excinfo:
        disabled.run_briefing()
    assert "PC_AGENT_BRIEFING_ENABLED" in str(excinfo.value)


def test_tick_generates_the_briefing_once_per_day(tmp_path: Path) -> None:
    now = _at(9, 0)
    engine = _engine(
        tmp_path, now=now, PC_AGENT_BRIEFING_ENABLED="1", PC_AGENT_BRIEFING_TIME="08:00"
    )
    first = engine.tick()
    assert first["briefing"] and "briefing" in first["briefing"]
    second = engine.tick()
    assert second["briefing"] is None
    assert engine.briefings()["total"] == 1


# ------------------------------------------------------------------ surfaces ---


def test_local_service_exposes_the_proactive_surface(tmp_path: Path) -> None:
    settings = _settings(tmp_path, PC_AGENT_BRIEFING_ENABLED="1", PC_AGENT_BRIEFING_TIME="00:00")
    with LocalService(settings) as service:
        status = service.proactive_status()
        assert status["enabled"] is True
        assert status["outbound_enabled"] is False
        created = service.add_reminder(title="Call back", due="+30m", severity="high")
        assert created["severity"] == "high"
        assert service.reminders()["total"] == 1
        briefing = service.run_briefing(force=True)
        assert briefing["briefing"]["item_count"] >= 0
        assert service.briefings()["total"] == 1
        assert service.notify_user(title="Manual ping")["status"] == "delivered"
        assert service.notifications()["total"] >= 2
        assert service.proactive_summary()["reminders_open"] == 1
        assert service.clear_notifications()["removed"] >= 2


def test_cli_reminder_and_briefing_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import os

    environ = {
        # Pin every path so an ambient PC_AGENT_* variable left behind by another
        # test file can never point this CLI run at a shared database (which would
        # make the rolling-hour cap depend on test order).
        "PC_AGENT_ROOT": str(tmp_path),
        "PC_AGENT_DATA_DIR": str(tmp_path / "data"),
        "PC_AGENT_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "PC_AGENT_DATABASE_PATH": str(tmp_path / "data" / "pc_agent.db"),
        "PC_AGENT_BRIEFING_ENABLED": "1",
        "PC_AGENT_BRIEFING_TIME": "00:00",
    }
    saved = dict(os.environ)
    os.environ.update(environ)
    try:
        assert main(["proactive", "status"]) == 0
        assert "proactive=on" in capsys.readouterr().out

        assert main(["proactive", "reminders", "add", "--title", "CLI task", "--due", "+45m"]) == 0
        assert "CLI task" in capsys.readouterr().out

        assert main(["proactive", "reminders", "add", "--title", "Late", "--due", "-1m"]) == 0
        capsys.readouterr()
        assert main(["proactive", "tick"]) == 0
        tick_out = capsys.readouterr().out
        assert "fired: Late" in tick_out

        assert main(["proactive", "reminders", "list"]) == 0
        assert "CLI task" in capsys.readouterr().out

        assert main(["proactive", "notifications", "list"]) == 0
        assert "reminder" in capsys.readouterr().out

        assert main(["proactive", "briefing", "run", "--force"]) == 0
        assert "briefing" in capsys.readouterr().out

        assert main(["proactive", "reminders", "add", "--title", "Bad", "--due", "nope"]) == 1
        assert "not a due time" in capsys.readouterr().out
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_api_routes_for_proactive(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    server = LocalApiServer(replace(settings, api_port=0, scheduler_interval_seconds=0))
    server.start()
    try:
        _host, port = server.address
        base = f"http://127.0.0.1:{port}"

        def call(path: str, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
            data = json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(
                base + path,
                data=data,
                method=method,
                headers={
                    "Authorization": f"Bearer {server.token}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read())["data"]

        status = call("/v1/proactive/status")
        assert status["enabled"] is True
        created = call(
            "/v1/proactive/reminders",
            "POST",
            {"title": "API reminder", "due": "+2h", "severity": "high", "repeat_seconds": 86400},
        )
        assert created["repeat_seconds"] == 86400
        assert call("/v1/proactive/reminders")["total"] == 1
        snoozed = call(f"/v1/proactive/reminders/{created['id']}/snooze", "POST", {"for": "+5m"})
        assert snoozed["due_at"] != created["due_at"]
        assert call(f"/v1/proactive/reminders/{created['id']}/done", "POST", {})["status"] == "done"
        assert (
            call("/v1/proactive/notify", "POST", {"title": "Ping", "severity": "low"})["status"]
            == "delivered"
        )
        assert call("/v1/proactive/briefing", "POST", {"force": True})["briefing"]["id"]
        assert call("/v1/proactive/briefings")["total"] == 1
        assert call("/v1/proactive/notifications?limit=5")["total"] >= 2
        assert call("/v1/proactive/tick", "POST", {})["total"] == 0
        assert call("/v1/proactive/notifications/clear", "POST", {})["removed"] >= 2
        assert call(f"/v1/proactive/reminders/{created['id']}", "DELETE")["deleted"] is True
    finally:
        server.stop()


def test_gui_controller_and_web_panel_are_wired(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    controller = GuiController(settings)
    text = controller.proactive_status_text()
    assert "reminders=" in text and "briefing=" in text
    created = controller.add_reminder("GUI reminder", "+1h", "high")
    assert "GUI reminder" in created
    assert "GUI reminder" in controller.proactive_reminders_text()
    reminder_id = controller.proactive_status()["reminders_open"]
    assert reminder_id == 1
    assert "delivered" in controller.proactive_tick() or "deferred" in controller.proactive_tick()
    assert "briefing" in controller.run_briefing(True)
    assert "delivered" in controller.notify_user("GUI ping", "hello")
    for marker in ("Proactive", "reminder-add", "proactive-tick", "proactive-briefing"):
        assert marker in INDEX_HTML


def test_diagnostics_and_release_cover_proactive(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    section = build_diagnostics(settings, database)["proactive"]
    assert section["available"] is True
    assert section["notification_max_per_hour"] == 20
    line = proactive_summary_line(settings, database)
    assert line.startswith("reminders=")
    assert "briefing=" in line

    checks = {check.name: check for check in audit_install(settings).checks}
    assert "proactive_intelligence" in checks
    assert checks["proactive_intelligence"].status == "pass"
    assert "quiet hours" in checks["proactive_intelligence"].detail
    assert len(checks) == 34


def test_ledgers_stay_bounded(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = ProactiveStore(Database(settings.database_path))
    for index in range(20):
        store.record_notification(kind="general", title=f"n{index}", limit=5)
    assert len(store.notifications(limit=100)) == 5
    assert store.counts()["notifications"] == 5
    for index in range(3):
        store.record_briefing(
            sections={"reminders": {"items": index}},
            item_count=index,
            delivered="in_app",
            summary=f"briefing {index}",
        )
    assert len(store.briefings()) == 3


def test_a_full_day_of_policy_is_deterministic(tmp_path: Path) -> None:
    """One simulated day: quiet night, busy morning, cap, then a briefing."""

    settings = _settings(
        tmp_path,
        PC_AGENT_QUIET_HOURS="22:00-07:00",
        PC_AGENT_NOTIFICATION_MAX_PER_HOUR="3",
        PC_AGENT_BRIEFING_ENABLED="1",
        PC_AGENT_BRIEFING_TIME="08:00",
        PC_AGENT_NOTIFICATION_COOLDOWN_SECONDS="60",
    )
    database = Database(settings.database_path)
    database.initialize()
    clock = {"now": _at(23, 0)}

    def engine() -> ProactiveEngine:
        return ProactiveEngine(settings, database, clock=lambda: clock["now"])

    engine().add_reminder(title="Night check", due="-1m")
    engine().add_reminder(title="Morning standup", due="07:30")
    for index in range(5):
        engine().add_reminder(title=f"Burst {index}", due="09:00")

    night = engine().tick()
    assert night["total"] == 0 and night["deferred"]
    # The 08:00 briefing never ran that day, so it is delivered late rather than
    # silently skipped - and it is still exactly one briefing per local date.
    assert night["briefing"] and "briefing" in night["briefing"]

    clock["now"] = _at(7, 31, day_offset=1)
    morning = engine().tick()
    assert {item["reminder"]["title"] for item in morning["fired"]} == {
        "Night check",
        "Morning standup",
    }

    clock["now"] = _at(9, 0, day_offset=1)
    burst = engine().tick()
    # Five are due, the rolling hourly cap is 3, so two are deferred with a reason.
    assert burst["total"] == 3
    assert len(burst["deferred"]) == 2
    assert all("hourly limit" in item["reason"] for item in burst["deferred"])
    assert burst["briefing"] and "briefing" in burst["briefing"]

    clock["now"] = _at(10, 30, day_offset=1)
    later = engine().tick()
    assert later["total"] == 2  # the deferred ones fire once the window rolls over
    assert later["briefing"] is None  # today's briefing already went out
    assert engine().status()["briefings"] == 2  # one per local date, never two
    assert time.time() > 0  # keep the import honest for slower platforms
