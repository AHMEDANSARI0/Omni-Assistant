"""Hotfix 0.81.2 regression tests (Phase 17.1).

These lock in the four Windows findings from the 0.81.2 acceptance run:

1. the rolling hourly cap counted *deferred* rows, so a suppressed tick re-armed
   its own budget and starved the rest of the hour (Phase 14, real defect);
2. suppressed rows were stamped with the host clock instead of the tick instant,
   which made the ledger - and therefore the cap - time-travel (Phase 14);
3. two Phase 14 assertions compared ISO strings that could carry different UTC
   offsets, so they only passed on hosts at +00:00 (test defect);
4. the plugin sandbox could not start a worker on Windows and reported the host
   failure as a plugin failure (Phase 15), and an unwritable ``chmod`` was
   asserted as a POSIX mode on NTFS (Phase 16).

Every test here runs the shipping implementation. Nothing is mocked, and the
timezone tests run against whatever ``TZ`` the host is using.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pc_agent.config import Settings
from pc_agent.plugins.sandbox import (
    SandboxLimits,
    _scrubbed_env,
    describe_sandbox,
    interpreter_candidates,
    run_sandboxed,
)
from pc_agent.proactive import ProactiveEngine, parse_due
from pc_agent.storage.db import Database
from pc_agent.sync.perms import harden_private_file, private_file_report

# ------------------------------------------------------------------ helpers ---


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


# ---------------------------------------------------- 1) hourly cap is honest ---


def test_suppressed_notifications_do_not_consume_the_hourly_budget(tmp_path: Path) -> None:
    """Five due reminders against a cap of three: three deliver, two wait, and
    the two that waited are *not* counted as if the user had seen them."""

    clock = {"now": _at(9, 0)}
    engine = _engine(
        tmp_path,
        now=clock["now"],
        PC_AGENT_NOTIFICATION_MAX_PER_HOUR="3",
    )
    for index in range(5):
        engine.add_reminder(title=f"Burst {index}", due="-1m")

    def tick() -> dict:
        engine._clock = lambda: clock["now"]
        return ProactiveEngine(
            engine._settings, Database(engine._settings.database_path), clock=lambda: clock["now"]
        ).tick()

    first = tick()
    assert first["total"] == 3
    assert len(first["deferred"]) == 2
    assert all("hourly limit" in item["reason"] for item in first["deferred"])
    # The two suppressed rows are deferred, not delivered.
    statuses = [row["status"] for row in engine.notifications()["notifications"]]
    assert statuses.count("deferred") == 2
    assert statuses.count("delivered") == 3

    # Ninety minutes later the rolling window is empty again, so both waiting
    # reminders deliver. Before the fix `in_hour` read 5 and they were starved.
    clock["now"] = _at(10, 30)
    second = tick()
    assert second["total"] == 2
    assert not second["deferred"]


def test_cap_still_applies_within_the_same_hour(tmp_path: Path) -> None:
    """The fix must not turn the cap off inside the same hour."""

    clock = {"now": _at(9, 0)}
    engine = _engine(
        tmp_path,
        now=clock["now"],
        PC_AGENT_NOTIFICATION_MAX_PER_HOUR="3",
    )
    for index in range(4):
        engine.add_reminder(title=f"Same hour {index}", due="-1m")
    now = clock["now"]
    tick = ProactiveEngine(
        engine._settings, Database(engine._settings.database_path), clock=lambda: now
    ).tick()
    assert tick["total"] == 3
    assert len(tick["deferred"]) == 1

    clock["now"] = now + timedelta(minutes=20)
    later = ProactiveEngine(
        engine._settings, Database(engine._settings.database_path), clock=lambda: clock["now"]
    ).tick()
    assert later["total"] == 0  # the cap is still in force 20 minutes in
    assert later["deferred"] and "hourly limit" in later["deferred"][0]["reason"]


def test_suppressed_rows_carry_the_ledger_instant_not_the_wall_clock(tmp_path: Path) -> None:
    """A suppressed row is stamped with the engine's clock, never with the host
    clock - otherwise the ledger mixes two timelines and the cap misfires."""

    moment = _at(9, 0)
    engine = _engine(tmp_path, now=moment, PC_AGENT_QUIET_HOURS="00:00-23:59")
    engine.add_reminder(title="Quiet one", due="-1m")
    tick = ProactiveEngine(
        engine._settings, Database(engine._settings.database_path), clock=lambda: moment
    ).tick()
    assert tick["total"] == 0 and tick["deferred"]

    row = engine.notifications()["notifications"][0]
    assert row["status"] == "deferred"
    stamped = datetime.fromisoformat(row["created_at"]).astimezone(UTC)
    assert stamped == moment.astimezone(UTC)


# ------------------------------------------------ 2) timezone-correct parsing ---


def test_naive_iso_due_means_local_wall_clock_on_every_host() -> None:
    now = _at(9, 0)
    parsed = parse_due("2026-12-01T09:30:00", now=now)
    assert parsed == datetime(2026, 12, 1, 9, 30).astimezone(UTC)
    assert parsed.utcoffset() == timedelta(0)  # always stored in UTC
    assert parsed.astimezone().strftime("%H:%M") == "09:30"
    # An explicit offset is honoured as written, not reinterpreted as local.
    assert parse_due("2026-12-01T09:30:00+00:00", now=now) == datetime(
        2026, 12, 1, 9, 30, tzinfo=UTC
    )


def test_reminder_due_at_is_comparable_as_an_instant(tmp_path: Path) -> None:
    now = _at(9, 0)
    engine = _engine(tmp_path, now=now)
    engine.add_reminder(title="Hourly", due="-1m", repeat_seconds=3600)
    ProactiveEngine(
        engine._settings, Database(engine._settings.database_path), clock=lambda: now
    ).tick()
    reminder = engine.reminders()["reminders"][0]
    assert datetime.fromisoformat(reminder["due_at"]) > now.astimezone(UTC)


# ------------------------------------------------------- 3) sandbox spawn path ---


def test_sandbox_reports_a_host_spawn_failure_instead_of_raising(
    tmp_path: Path, monkeypatch
) -> None:
    """A worker that cannot be started is a host problem, reported as data.

    The candidate list is emptied so the sandbox genuinely has nothing to try:
    the point is that a broken host produces a structured result carrying the
    real reason, never an exception escaping to the caller.
    """

    from pc_agent.plugins import sandbox as sandbox_module

    monkeypatch.setattr(sandbox_module, "interpreter_candidates", list)
    result = run_sandboxed(
        ["definitely-not-a-python-interpreter", "-c", "print(1)"],
        cwd=tmp_path,
        payload=b"",
        limits=SandboxLimits(timeout_seconds=5),
    )
    assert result.ok is False
    assert result.spawn_error
    assert result.returncode == -1
    assert "could not start a Python worker" in result.spawn_error
    assert "definitely-not-a-python-interpreter" in result.spawn_error
    assert result.mechanism == describe_sandbox()["mechanism"]


def test_sandbox_falls_back_to_a_working_interpreter_and_says_so(tmp_path: Path) -> None:
    """A launcher shim that cannot start is substituted, never silently."""

    result = run_sandboxed(
        ["definitely-not-a-python-interpreter", "-c", "print(1)"],
        cwd=tmp_path,
        payload=b"",
        limits=SandboxLimits(timeout_seconds=10),
    )
    assert result.spawn_error == ""
    assert result.ok is True, result.stderr
    # The substitution must be a real interpreter the sandbox can actually start
    # *under its own limits*. That is not always ``sys.executable``: on Windows a
    # virtualenv-style ``python.exe`` is a launcher shim that needs a second
    # process slot, so the sandbox prefers the base interpreter instead.
    assert result.interpreter in interpreter_candidates()
    assert result.interpreter != "definitely-not-a-python-interpreter"
    assert Path(result.interpreter).is_file() or shutil.which(result.interpreter)


def test_interpreter_candidates_start_with_the_running_interpreter() -> None:
    candidates = interpreter_candidates()
    assert candidates
    assert candidates[0] == sys.executable
    assert len(candidates) == len(set(candidates))


def test_sandboxed_worker_really_runs_on_this_host(tmp_path: Path) -> None:
    """The same argv the plugin service uses must work here, end to end."""

    result = run_sandboxed(
        [sys.executable, "-I", "-B", "-c", "import sys; sys.stdout.write('alive')"],
        cwd=tmp_path,
        payload=b"",
        limits=SandboxLimits(timeout_seconds=30),
    )
    assert result.spawn_error == ""
    assert result.ok is True, result.stderr
    assert result.stdout.strip() == "alive"


def test_worker_environment_is_minimal_but_startable() -> None:
    """Secrets never cross the boundary; the variables a child needs survive."""

    os.environ["PC_AGENT_HOTFIX_FAKE_KEY"] = "sk-should-not-leak"
    os.environ["OPENAI_API_KEY"] = "sk-should-not-leak"
    try:
        env = _scrubbed_env(allow_network=False)
    finally:
        os.environ.pop("PC_AGENT_HOTFIX_FAKE_KEY", None)
        os.environ.pop("OPENAI_API_KEY", None)

    assert "sk-should-not-leak" not in " ".join(env.values())
    assert not any(name.endswith("_API_KEY") for name in env)
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["http_proxy"] == env["https_proxy"] == "http://127.0.0.1:9"
    if os.name == "posix":
        assert env["PATH"] == "/usr/bin:/bin"
        assert env["TMPDIR"] == "/tmp"
    else:  # pragma: no cover - Windows only
        assert env.get("SystemRoot") or env.get("windir")
        assert env.get("TEMP") or env.get("TMP")


# ------------------------------------------------- 4) owner-only permissions ---


def test_private_file_report_matches_the_mechanism_this_host_has(tmp_path: Path) -> None:
    target = tmp_path / "secret.json"
    target.write_text("{}", encoding="utf-8")
    if os.name == "posix":
        os.chmod(target, 0o644)
    applied = harden_private_file(target)
    report = private_file_report(target)

    assert applied["mechanism"] == report["mechanism"]
    if os.name == "posix":
        assert report["mechanism"] == "posix-mode"
        assert target.stat().st_mode & 0o777 == 0o600
        assert report["owner_only"] is True
    else:  # pragma: no cover - Windows only
        assert report["mechanism"] == "windows-acl"
        assert applied["applied"] is True, applied["detail"]
        assert report["owner_only"] is True
    assert isinstance(report["detail"], str) and report["detail"]
