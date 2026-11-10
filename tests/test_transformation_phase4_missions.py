"""Transformation Phase 4: Mission Center, dependencies, scheduler."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import timedelta

import pytest

from pc_agent.api_server import LocalApiServer
from pc_agent.baseline import TRANSFORMATION_PHASE
from pc_agent.config import Settings
from pc_agent.documents.pdf import create_demo_pdfs
from pc_agent.domain.task import utc_now
from pc_agent.errors import ConfigError, StorageError
from pc_agent.missions import execution_levels
from pc_agent.service import LocalService
from pc_agent.storage.db import LATEST_SCHEMA_VERSION, Database
from pc_agent.storage.repositories import MissionRepository

MERGE = "Merge PDFs from Folder A and Folder B"
UNSUPPORTED = "fly to the moon please"


@pytest.fixture
def settings(tmp_path):
    value = Settings.from_env(tmp_path)
    value.ensure_directories()
    create_demo_pdfs(value.workspace_dir)
    return value


def test_schema_v9_and_identity(settings):
    assert LATEST_SCHEMA_VERSION == 18
    assert TRANSFORMATION_PHASE == 18
    database = Database(settings.database_path)
    database.initialize()
    assert database.schema_version() == 18


def test_execution_levels_and_cycle_detection():
    levels = execution_levels(["a", "b", "c", "d"], {"c": ["a"], "d": ["b", "c"]})
    assert levels[0] == ["a", "b"]
    assert levels[1] == ["c"]
    assert levels[2] == ["d"]
    with pytest.raises(Exception, match="cycle"):
        execution_levels(["a", "b"], {"a": ["b"], "b": ["a"]})


def test_mission_repository_rejects_cycles(settings):
    database = Database(settings.database_path)
    database.initialize()
    missions = MissionRepository(database)
    mission = missions.create_mission("cycle guard")
    from pc_agent.domain.task import Task as TaskDomain
    from pc_agent.storage.repositories import TaskRepository

    tasks = TaskRepository(database)
    first = tasks.create(TaskDomain(instruction="one"))
    second = tasks.create(TaskDomain(instruction="two"))
    missions.add_task(mission.id, first.id)
    missions.add_task(mission.id, second.id)
    missions.add_dependency(mission.id, second.id, first.id)
    with pytest.raises(StorageError, match="cycle"):
        missions.add_dependency(mission.id, first.id, second.id)
    with pytest.raises(StorageError):
        missions.add_dependency(mission.id, first.id, first.id)


def test_mission_run_parallel_and_blocked_dependent(settings):
    with LocalService(settings) as service:
        mission = service.create_mission(
            "pdf pipeline",
            task_specs=[
                {"key": "m1", "instruction": MERGE},
                {"key": "m2", "instruction": MERGE},
                {"key": "bad", "instruction": UNSUPPORTED},
                {"key": "after", "instruction": MERGE, "depends_on": ["bad"]},
                {"key": "joiner", "instruction": MERGE, "depends_on": ["m1", "m2"]},
            ],
        )
        assert [len(level) for level in mission["graph"]["levels"]] == [3, 2]
        started = time.monotonic()
        service.start_mission(mission["id"])
        final = service.wait_for_mission(mission["id"], timeout_seconds=600)
        _elapsed = time.monotonic() - started
        assert final["status"] == "failed"  # bad branch blocks `after`
        by_instruction = {}
        for task in final["tasks"]:
            by_instruction.setdefault(task["instruction"], []).append(task["status"])
        assert by_instruction[UNSUPPORTED] == ["needs_input"]
        merge_statuses = sorted(by_instruction[MERGE])
        assert merge_statuses == ["queued", "succeeded", "succeeded", "succeeded"]
        assert len(final["last_run"]["blocked_task_ids"]) == 1
        event_types = [
            event["event_type"]
            for event in service.list_events(limit=200)["events"]
            if event["event_type"].startswith("mission.")
        ]
        assert event_types[:2] == ["mission.created", "mission.run_started"]
        assert "mission.run_finished" in event_types


def test_mission_all_success_and_delete_keeps_tasks(settings):
    with LocalService(settings) as service:
        mission = service.create_mission(
            "green",
            task_specs=[
                {"key": "a", "instruction": MERGE},
                {"key": "b", "instruction": MERGE, "depends_on": ["a"]},
            ],
        )
        service.start_mission(mission["id"])
        final = service.wait_for_mission(mission["id"], timeout_seconds=600)
        assert final["status"] == "succeeded"
        assert final["progress"].get("succeeded") == 2
        deleted = service.delete_mission(mission["id"])
        assert deleted["deleted"] is True
        assert service.overview()["tasks"]["total"] == 2  # tasks kept
        assert service.overview()["missions"] == 0


def test_schedules_once_interval_and_due_run(settings):
    with LocalService(settings) as service:
        once = service.create_schedule(
            "past once",
            instruction=UNSUPPORTED,
            kind="once",
            run_at=utc_now() - timedelta(minutes=1),
        )
        interval = service.create_schedule(
            "hourly",
            instruction=UNSUPPORTED,
            kind="interval",
            interval_seconds=3600,
        )
        assert once["kind"] == "once"
        result = service.run_due_schedules()
        assert result["total"] == 1
        task_id = result["triggered"][0]["task_id"]
        service.wait_for_task(task_id, timeout_seconds=300)
        schedules = {row["id"]: row for row in service.list_schedules()["schedules"]}
        assert schedules[once["id"]]["enabled"] is False
        assert schedules[once["id"]]["run_count"] == 1
        assert schedules[once["id"]]["last_task_id"] == task_id
        assert schedules[interval["id"]]["enabled"] is True
        assert schedules[interval["id"]]["run_count"] == 0
        assert service.run_due_schedules()["total"] == 0
        toggled = service.set_schedule_enabled(interval["id"], enabled=False)
        assert toggled["enabled"] is False
        assert service.delete_schedule(interval["id"])["deleted"] is True
        event_types = [
            event["event_type"]
            for event in service.list_events(limit=200)["events"]
            if event["event_type"].startswith("schedule.")
        ]
        assert "schedule.created" in event_types
        assert "schedule.triggered" in event_types


def test_mission_and_schedule_api(settings):
    server = LocalApiServer(replace(settings, api_port=0, scheduler_interval_seconds=0))
    server.start()
    try:
        host, port = server.address
        base = f"http://{host}:{port}"
        token = server.token

        def call(method, path, body=None):
            data = json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(base + path, data=data, method=method)
            request.add_header("Authorization", "Bearer " + token)
            if data:
                request.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())

        status, created = call(
            "POST",
            "/v1/missions",
            {
                "name": "api mission",
                "tasks": [
                    {"key": "a", "instruction": MERGE},
                    {"key": "b", "instruction": MERGE, "depends_on": ["a"]},
                ],
            },
        )
        assert status == 201
        mission_id = created["data"]["id"]
        status, graph = call("GET", f"/v1/missions/{mission_id}/graph")
        assert status == 200
        assert graph["data"]["graph"]["levels"][0]
        assert graph["data"]["graph"]["max_parallel"] == 3
        status, accepted = call("POST", f"/v1/missions/{mission_id}/run", {})
        assert status == 202 and accepted["data"]["accepted"] is True
        final = server.service.wait_for_mission(mission_id, timeout_seconds=600)
        assert final["status"] == "succeeded"
        status, schedules = call("GET", "/v1/schedules")
        assert status == 200 and schedules["data"]["total"] == 0
        status, _made = call(
            "POST",
            "/v1/schedules",
            {
                "name": "api once",
                "instruction": UNSUPPORTED,
                "kind": "once",
                "run_at": (utc_now() - timedelta(seconds=5)).isoformat(),
            },
        )
        assert status == 201
        status, due = call("POST", "/v1/schedules/run-due", {})
        assert status == 200 and due["data"]["total"] == 1
        status, overview = call("GET", "/v1/overview")
        assert overview["data"]["missions"] == 1
        assert overview["data"]["schedules"] == 1
    finally:
        server.stop()


def test_phase4_config_bounds(tmp_path):
    with pytest.raises(ConfigError, match="MISSION_MAX_PARALLEL"):
        Settings.from_env(tmp_path, environ={"PC_AGENT_MISSION_MAX_PARALLEL": "0"})
    with pytest.raises(ConfigError, match="SCHEDULER"):
        Settings.from_env(tmp_path, environ={"PC_AGENT_SCHEDULER_INTERVAL_SECONDS": "9999"})
    value = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_MISSION_MAX_PARALLEL": "5",
            "PC_AGENT_SCHEDULER_INTERVAL_SECONDS": "0",
        },
    )
    assert value.mission_max_parallel == 5
    assert value.scheduler_interval_seconds == 0


def test_phase4_cli_surfaces(tmp_path, capsys):
    from pc_agent.cli import main
    from pc_agent.documents.pdf import create_demo_pdfs as seed_pdfs

    root_settings = Settings.from_env(tmp_path)
    root_settings.ensure_directories()
    seed_pdfs(root_settings.workspace_dir)

    assert main(["--root", str(tmp_path), "missions", "create", "cli mission", "--json"]) == 0
    mission = json.loads(capsys.readouterr().out)
    mission_id = mission["id"]
    assert main(["--root", str(tmp_path), "missions", "add-task", mission_id, MERGE]) == 0
    out = capsys.readouterr().out
    assert "Execution graph levels:" in out
    assert main(["--root", str(tmp_path), "missions", "run", mission_id]) == 0
    assert "[succeeded]" in capsys.readouterr().out
    assert main(["--root", str(tmp_path), "missions", "show", "latest"]) == 0
    assert "succeeded" in capsys.readouterr().out

    assert main(["--root", str(tmp_path), "schedules", "add", "later", MERGE, "--in-minutes", "90"]) == 0
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "schedules", "list", "--json"]) == 0
    schedules = json.loads(capsys.readouterr().out)
    assert schedules["total"] == 1
    assert main(["--root", str(tmp_path), "schedules", "tick"]) == 0
    assert "Triggered 0" in capsys.readouterr().out

    assert main(["--root", str(tmp_path), "doctor"]) == 0
    doctor = capsys.readouterr().out
    assert "Mission center:" in doctor
    assert "missions=1" in doctor


def test_diagnostics_release_gui_mission_surfaces(settings):
    from pc_agent.diagnostics import build_diagnostics
    from pc_agent.gui.controller import GuiController
    from pc_agent.release import audit_install

    initialized = Database(settings.database_path)
    initialized.initialize()
    report = build_diagnostics(settings, initialized)
    assert report["missions"]["max_parallel"] == 3
    assert report["missions"]["missions"] == 0

    audit = audit_install(settings)
    checks = {check.name: check for check in audit.checks}
    assert len(audit.checks) == 34
    assert checks["mission_center"].status == "pass"

    controller = GuiController(settings)
    summary = controller.doctor_summary()
    assert "max_parallel=3" in summary.mission_summary
    assert "Mission center:" in summary.to_text()
    orchestrator = getattr(controller, "_orchestrator", None)
    if orchestrator is not None:
        orchestrator._safety.close()
