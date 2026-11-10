"""Transformation Phase 3: Local Service, events, logging, profiles."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from pc_agent.api_server import LocalApiServer, load_or_create_api_token
from pc_agent.baseline import TRANSFORMATION_PHASE
from pc_agent.config import Settings
from pc_agent.errors import ConfigError
from pc_agent.profiles import load_profile, save_profile, set_active_profile
from pc_agent.service import LocalService
from pc_agent.storage.db import LATEST_SCHEMA_VERSION, Database
from pc_agent.storage.repositories import EventRepository

SECRET_TEXT = "api" + "_key=" + "super-secret-value"
LOG_SECRET_TEXT = "api" + "_key=" + "do-not-print"

@pytest.fixture
def settings(tmp_path):
    return Settings.from_env(tmp_path)


def test_schema_v8_and_baseline_identity(settings, tmp_path):
    assert LATEST_SCHEMA_VERSION == 18
    assert TRANSFORMATION_PHASE == 18
    database = Database(settings.database_path)
    database.initialize()
    assert database.schema_version() == 18


def test_event_repository_records_lists_and_redacts(settings, tmp_path):
    database = Database(settings.database_path)
    database.initialize()
    events = EventRepository(database)
    first = events.record(
        "task.created", source="test", summary="Task created: demo", task_id=None
    )
    second = events.record(
        "safety.emergency_stop_engaged",
        source="test",
        summary="Emergency stop engaged " + SECRET_TEXT,
        payload={"api_key": "never-stored"},
    )
    assert second.id == first.id + 1
    listed = events.list(since_id=first.id)
    assert [event.event_type for event in listed] == ["safety.emergency_stop_engaged"]
    assert "super-secret-value" not in listed[0].summary
    assert listed[0].payload == {"api_key": "<redacted>"}
    assert events.count() == 2
    recent = events.list(descending=True, limit=1)
    assert recent[0].id == second.id


def test_local_service_task_lifecycle_and_events(settings):
    with LocalService(settings) as service:
        health = service.health()
        assert health["schema_version"] == 18
        assert health["version"] == "0.82.12"
        created = service.create_task("an instruction no planner supports xyz")
        assert created["status"] == "queued"
        accepted = service.start_task(created["id"])
        assert accepted["accepted"] is True
        final = service.wait_for_task(created["id"], timeout_seconds=180)
        assert final["status"] == "needs_input"
        assert final["report"]["status"] == "needs_input"
        event_types = [
            event["event_type"] for event in service.list_events(limit=50)["events"]
        ]
        assert event_types[:3] == [
            "task.created",
            "task.run_started",
            "task.run_finished",
        ]
        assert service.overview()["tasks"]["total"] == 1
        assert service.list_tools()["total"] == 69
        with pytest.raises(Exception, match="not terminal|already"):
            service.start_task(created["id"])
        cancelled = service.cancel_task(created["id"], reason="test cleanup")
        assert cancelled["task"]["status"] == "cancelled"
        deleted = service.delete_task(created["id"])
        assert deleted["deleted"] is True
        assert service.overview()["tasks"]["total"] == 0


def test_api_requires_token_and_runs_tasks(settings):
    server = LocalApiServer(replace(settings, api_port=0))
    server.start()
    try:
        host, port = server.address
        base = f"http://{host}:{port}"
        token = server.token

        def call(method, path, tok=None, body=None):
            data = json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(base + path, data=data, method=method)
            if tok:
                request.add_header("Authorization", "Bearer " + tok)
            if data:
                request.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())

        status, health = call("GET", "/v1/health")
        assert status == 200 and health["data"]["status"] == "ok"
        assert call("GET", "/v1/overview")[0] == 401
        assert call("GET", "/v1/overview", "wrong-token")[0] == 401
        status, overview = call("GET", "/v1/overview", token)
        assert status == 200 and overview["data"]["tools"] == 69
        status, run = call("POST", "/v1/run", token, {"instruction": "unsupported xyz"})
        assert status == 202
        task_id = run["data"]["task_id"]
        final = server.service.wait_for_task(task_id, timeout_seconds=180)
        assert final["status"] == "needs_input"
        status, fetched = call("GET", f"/v1/tasks/{task_id}", token)
        assert status == 200 and fetched["data"]["status"] == "needs_input"
        status, settings_payload = call("GET", "/v1/settings", token)
        serialized = json.dumps(settings_payload)
        assert "api_token" in serialized  # the label only
        assert token not in serialized
        status, events = call("GET", "/v1/events?limit=20", token)
        types = [event["event_type"] for event in events["data"]["events"]]
        assert "service.api_started" in types
        assert "task.run_finished" in types
        status, stopped = call("POST", "/v1/safety/stop", token, {"reason": "test"})
        assert stopped["data"]["engaged"] is True
        status, resumed = call("POST", "/v1/safety/resume", token, {})
        assert resumed["data"]["engaged"] is False
        status, _saved = call("POST", "/v1/profiles/save-current", token, {"name": "apitest"})
        assert status == 201
        status, activated = call("POST", "/v1/profiles/activate", token, {"name": "apitest"})
        assert activated["data"]["active"] == "apitest"
        assert activated["data"]["restart_required_to_apply"] is True
    finally:
        server.stop()


def test_api_token_file_is_created_once(settings):
    first = load_or_create_api_token(settings)
    second = load_or_create_api_token(settings)
    assert first == second and len(first) >= 16
    assert (settings.data_dir / "api-token").is_file()


def test_profile_precedence_and_gate_rejection(tmp_path):
    save_profile(
        tmp_path / "data",
        "quiet",
        {"log_level": "WARNING", "log_format": "json", "task_tool_budget": 7},
    )
    set_active_profile(tmp_path / "data", "quiet")
    active = Settings.from_env(tmp_path)
    assert active.active_profile == "quiet"
    assert active.log_level == "WARNING"
    assert active.log_format == "json"
    assert active.task_tool_budget == 7
    overridden = Settings.from_env(
        tmp_path,
        environ={"PC_AGENT_TASK_TOOL_BUDGET": "9", "PC_AGENT_LOG_LEVEL": "ERROR"},
    )
    assert overridden.log_level == "ERROR"  # environment wins
    assert overridden.task_tool_budget == 9
    assert overridden.log_format == "json"  # profile fills the rest
    assert load_profile(tmp_path / "data", "quiet")["task_tool_budget"] == 7
    with pytest.raises(ConfigError):
        save_profile(tmp_path / "data", "bad", {"allow_subprocess": True})
    (tmp_path / "data" / "profiles" / "bad.json").write_text(
        '{"gemini_api_key": "leaked"}', encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="unsupported field"):
        Settings.from_env(tmp_path, environ={"PC_AGENT_PROFILE": "bad"})
    (tmp_path / "data" / "profiles" / "bad.json").write_text(
        '{"task_tool_budget": -5}', encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        Settings.from_env(tmp_path, environ={"PC_AGENT_PROFILE": "bad"})


def test_api_host_must_be_loopback(tmp_path):
    with pytest.raises(ConfigError, match="loopback"):
        Settings.from_env(tmp_path, environ={"PC_AGENT_API_HOST": "0.0.0.0"})
    with pytest.raises(ConfigError):
        Settings.from_env(tmp_path, environ={"PC_AGENT_API_HOST": "192.0.2.10"})
    assert Settings.from_env(tmp_path, environ={"PC_AGENT_API_HOST": "::1"}).api_host == "::1"
    with pytest.raises(ConfigError):
        Settings.from_env(tmp_path, environ={"PC_AGENT_API_TOKEN": "too-short"})
    with pytest.raises(ConfigError):
        Settings.from_env(tmp_path, environ={"PC_AGENT_LOG_FORMAT": "yaml"})


def test_structured_json_logging(tmp_path, capsys):
    import logging
    from io import StringIO

    from pc_agent.logging_config import configure_logging

    log_file = tmp_path / "logs" / "pc-agent.jsonl"
    stream = StringIO()
    # Earlier tests may already have installed a stream handler; drop the
    # marked ones so this test observes its own stream deterministically.
    base_logger = logging.getLogger("pc_agent")
    for handler in list(base_logger.handlers):
        if getattr(handler, "_pc_agent_handler", False) and not getattr(
            handler, "_pc_agent_file_handler", False
        ):
            base_logger.removeHandler(handler)
    logger = configure_logging("INFO", stream=stream, log_format="json", log_file=log_file)
    logger.warning("structured event %s", LOG_SECRET_TEXT, extra={"ctx_task_id": "abc"})
    for handler in logger.handlers:
        handler.flush()
    line = [row for row in stream.getvalue().splitlines() if row.strip()][-1]
    parsed = json.loads(line)
    assert parsed["level"] == "WARNING"
    assert "do-not-print" not in parsed["message"]
    assert parsed["task_id"] == "abc"
    file_line = json.loads(log_file.read_text(encoding='utf-8').strip().splitlines()[-1])
    assert file_line["message"].startswith("structured event")
    # restore text formatting for other tests sharing the logger
    configure_logging("INFO", log_format="text")
    assert logging.getLogger("pc_agent").level == logging.INFO


def test_phase3_cli_surfaces(tmp_path, capsys):
    from pc_agent.cli import main

    assert main(["--root", str(tmp_path), "api", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["api_enabled"] is True
    assert payload["health"]["schema_version"] == 18

    assert main(["--root", str(tmp_path), "profiles", "save-current", "work", "--json"]) == 0
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "profiles", "activate", "work"]) == 0
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "profiles", "list", "--json"]) == 0
    profiles = json.loads(capsys.readouterr().out)
    assert profiles["active"] == "work"
    names = [info["name"] for info in profiles["profiles"]]
    assert "work" in names
    work = next(info for info in profiles["profiles"] if info["name"] == "work")
    assert "allow_subprocess" not in work["field_names"]
    assert main(["--root", str(tmp_path), "events", "list", "--json"]) == 0
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "profiles", "delete", "work", "--yes"]) == 0
    capsys.readouterr()

    assert main(["--root", str(tmp_path), "doctor"]) == 0
    doctor_output = capsys.readouterr().out
    assert "Local service:" in doctor_output
    assert "log_format=text" in doctor_output


def test_diagnostics_and_release_include_service(settings):
    from pc_agent.diagnostics import build_diagnostics
    from pc_agent.release import audit_install

    initialized = Database(settings.database_path)
    initialized.initialize()
    report = build_diagnostics(settings, initialized)
    service_section = report["service"]
    assert service_section["api_host"] == "127.0.0.1"
    assert service_section["log_format"] == "text"
    assert service_section["events_total"] == 0

    audit = audit_install(settings)
    checks = {check.name: check for check in audit.checks}
    assert len(audit.checks) == 34
    assert checks["local_service"].status == "pass"
    assert "api=enabled" in checks["local_service"].detail


def test_gui_doctor_service_summary(settings):
    from pc_agent.gui.controller import GuiController

    controller = GuiController(settings)
    summary = controller.doctor_summary()
    assert "api=enabled" in summary.service_summary
    assert "Local service:" in summary.to_text()
    controller_orchestrator = getattr(controller, "_orchestrator", None)
    if controller_orchestrator is not None:
        controller_orchestrator._safety.close()
