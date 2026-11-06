"""Transformation Phase 12: visual workflow builder, triggers and import/export.

Everything here runs against the real modules and a real SQLite database.
Mission start is injected with a recording stub so the compilation contract can
be asserted exactly (task specs, dependencies, level order) without launching a
model. Where a feature must refuse — cycles, tampering, disabled flows, unknown
document keys, secret-bearing error text — the test asserts the refusal.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from pc_agent import __version__
from pc_agent.api_server import LocalApiServer
from pc_agent.baseline import TRANSFORMATION_LABEL, TRANSFORMATION_PHASE
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.diagnostics import flows_summary_line
from pc_agent.domain.task import utc_now
from pc_agent.errors import FlowError
from pc_agent.flows import (
    FLOW_SCHEMA,
    LIMITS,
    FlowRunner,
    FlowStore,
    definition_checksum,
    export_payload,
    load_export_payload,
    validate_definition,
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


def _database(settings: Settings) -> Database:
    database = Database(settings.database_path)
    database.initialize()
    return database


def _document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "flow_schema": FLOW_SCHEMA,
        "name": "Morning report",
        "description": "Two-step briefing",
        "variables": {"window": "today"},
        "nodes": [
            {
                "id": "gather",
                "title": "Gather",
                "instruction": "Summarise the {{window}} logs",
            },
            {"id": "deliver", "title": "Deliver", "instruction": "Write the summary"},
        ],
        "edges": [{"from": "gather", "to": "deliver"}],
    }
    document.update(overrides)
    return document


class _MissionRecorder:
    """Stands in for ``service.start_mission`` and records every call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, task: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"task": task, **kwargs})
        return {"id": f"mission-{len(self.calls)}", "accepted": True, "mission": {}}


def _store_with_flow(tmp_path: Path) -> tuple[Database, FlowStore, Any]:
    database = _database(_settings(tmp_path))
    store = FlowStore(database)
    record = store.create(validate_definition(_document()))
    return database, store, record


# --------------------------------------------------------------- validation ---


def test_valid_document_compiles_to_levels_and_substituted_instructions() -> None:
    definition = validate_definition(_document())
    assert definition.name == "Morning report"
    assert definition.node_ids == ("gather", "deliver")
    assert definition.topological_levels() == [["gather"], ["deliver"]]
    specs = definition.mission_specs()
    assert [spec["key"] for spec in specs] == ["gather", "deliver"]
    assert specs[0]["instruction"] == "Summarise the today logs"
    assert specs[0]["depends_on"] == []
    assert specs[1]["depends_on"] == ["gather"]


def test_parallel_branches_form_one_level() -> None:
    document = _document(
        nodes=[
            {"id": "root", "instruction": "Prepare"},
            {"id": "left", "instruction": "Left branch"},
            {"id": "right", "instruction": "Right branch"},
            {"id": "join", "instruction": "Join results"},
        ],
        edges=[
            {"from": "root", "to": "left"},
            {"from": "root", "to": "right"},
            {"from": "left", "to": "join"},
            {"from": "right", "to": "join"},
        ],
    )
    assert validate_definition(document).topological_levels() == [
        ["root"],
        ["left", "right"],
        ["join"],
    ]


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda doc: doc.update({"unexpected": 1}), "unexpected"),
        (lambda doc: doc.update({"flow_schema": 99}), "flow_schema must be 1"),
        (lambda doc: doc.update({"name": ""}), "name"),
        (lambda doc: doc.update({"nodes": []}), "non-empty"),
        (
            lambda doc: doc["nodes"].append({"id": "Bad Id", "instruction": "x"}),
            "lowercase",
        ),
        (
            lambda doc: doc["nodes"].append({"id": "gather", "instruction": "dup"}),
            "duplicate",
        ),
        (lambda doc: doc["nodes"].append({"id": "extra"}), "instruction"),
        (
            lambda doc: doc["edges"].append({"from": "gather", "to": "ghost"}),
            "unknown node",
        ),
        (lambda doc: doc.update({"variables": {"Bad": "x"}}), "invalid name"),
        (
            lambda doc: doc["nodes"][0].update({"instruction": "Use {{missing}}"}),
            "missing",
        ),
    ],
)
def test_validation_negatives(mutate: Any, fragment: str) -> None:
    document = _document()
    mutate(document)
    with pytest.raises(FlowError) as excinfo:
        validate_definition(document)
    assert fragment.lower() in str(excinfo.value).lower()


def test_cycle_is_refused_with_a_clear_message() -> None:
    document = _document(
        edges=[
            {"from": "gather", "to": "deliver"},
            {"from": "deliver", "to": "gather"},
        ]
    )
    with pytest.raises(FlowError, match="cycle"):
        validate_definition(document)


def test_limits_are_real_and_enforced() -> None:
    assert LIMITS["max_nodes"] == 20
    assert LIMITS["max_variables"] == 8
    nodes = [
        {"id": f"n{index}", "instruction": f"step {index}"}
        for index in range(LIMITS["max_nodes"] + 1)
    ]
    with pytest.raises(FlowError, match="more than 20"):
        validate_definition(_document(nodes=nodes, edges=[]))

    variables = {f"v{index}": "x" for index in range(LIMITS["max_variables"] + 1)}
    with pytest.raises(FlowError, match="variables"):
        validate_definition(_document(variables=variables))


def test_node_ids_must_be_stable_slugs() -> None:
    for bad in ("-lead", "with space", "UPPER"):
        with pytest.raises(FlowError, match="lowercase"):
            validate_definition(
                _document(nodes=[{"id": bad, "instruction": "x"}], edges=[])
            )
    with pytest.raises(FlowError, match="32"):
        validate_definition(
            _document(nodes=[{"id": "x" * 33, "instruction": "x"}], edges=[])
        )


# -------------------------------------------------------- export / import ----


def test_export_checksum_detects_editing() -> None:
    definition = validate_definition(_document())
    payload = export_payload(
        definition,
        triggers=[{"kind": "schedule", "interval_seconds": 600}],
        version=__version__,
    )
    assert payload["flow_schema"] == FLOW_SCHEMA
    assert payload["exported_by"] == f"pc-agent {__version__}"
    assert payload["checksum"] == definition_checksum(
        payload["flow"], payload["triggers"]
    )

    loaded, triggers = load_export_payload(payload)
    assert loaded.name == "Morning report"
    assert triggers == [{"kind": "schedule", "interval_seconds": 600}]

    payload["flow"]["nodes"][0]["instruction"] = "Something else entirely"
    with pytest.raises(FlowError, match="checksum"):
        load_export_payload(payload)


def test_import_refuses_unknown_fields_and_oversized_documents() -> None:
    definition = validate_definition(_document())
    payload = export_payload(definition)
    payload["extra"] = True
    with pytest.raises(FlowError, match="extra"):
        load_export_payload(payload)

    big = export_payload(definition)
    big["flow"]["description"] = "x" * (LIMITS["max_import_bytes"] + 1)
    with pytest.raises(FlowError):
        load_export_payload(big)


# ------------------------------------------------------------------- store ---


def test_store_versions_are_immutable_and_listed(tmp_path: Path) -> None:
    _database_obj, store, record = _store_with_flow(tmp_path)
    assert record.version == 1
    updated = store.update(
        record.id, validate_definition(_document(name="Morning report v2"))
    )
    assert updated.version == 2
    assert updated.name == "Morning report v2"
    versions = store.versions(record.id)
    assert [item["version"] for item in versions] == [2, 1]
    assert versions[0]["sha256"] == updated.sha256


def test_store_delete_removes_child_rows(tmp_path: Path) -> None:
    _database_obj, store, record = _store_with_flow(tmp_path)
    store.add_trigger(record.id, kind="manual")
    assert store.delete(record.id) is True
    assert store.get(record.id) is None
    assert store.triggers(record.id) == []


def test_disabled_flow_is_not_run(tmp_path: Path) -> None:
    _database_obj, store, record = _store_with_flow(tmp_path)
    store.set_enabled(record.id, False)
    runner = FlowRunner(store=store, start_mission=_MissionRecorder())
    with pytest.raises(FlowError, match="disabled"):
        runner.run_flow(record.id)


def test_one_active_run_per_flow(tmp_path: Path) -> None:
    _database_obj, store, record = _store_with_flow(tmp_path)
    runner = FlowRunner(store=store, start_mission=_MissionRecorder())
    first = runner.run_flow(record.id)
    assert first["mission_id"] == "mission-1"
    with pytest.raises(FlowError, match="already has a run"):
        runner.run_flow(record.id)


def test_reconcile_closes_finished_missions(tmp_path: Path) -> None:
    _database_obj, store, record = _store_with_flow(tmp_path)
    states = {"mission-1": "running"}
    runner = FlowRunner(
        store=store,
        start_mission=_MissionRecorder(),
        mission_state=lambda mission_id: states.get(mission_id),
    )
    runner.run_flow(record.id)
    assert runner.reconcile() == 0
    states["mission-1"] = "completed"
    assert runner.reconcile() == 1
    assert store.list_runs(flow_id=record.id)[0].status == "completed"
    # The finished run no longer blocks the flow.
    assert runner.run_flow(record.id)["mission_id"] == "mission-2"


# ------------------------------------------------------------------ runner ---


def test_runner_compiles_mission_with_dependencies(tmp_path: Path) -> None:
    _database_obj, store, record = _store_with_flow(tmp_path)
    recorder = _MissionRecorder()
    runner = FlowRunner(store=store, start_mission=recorder, emit=lambda *a, **k: None)
    result = runner.run_flow(record.id)

    assert result["levels"] == [["gather"], ["deliver"]]
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["task"] == "Morning report"
    assert [spec["key"] for spec in call["task_specs"]] == ["gather", "deliver"]
    assert call["task_specs"][1]["depends_on"] == ["gather"]

    runs = store.list_runs(flow_id=record.id)
    assert runs[0].status == "started"
    assert runs[0].mission_id == "mission-1"
    assert runs[0].node_count == 2


def test_runner_records_failure_and_redacts_secrets(tmp_path: Path) -> None:
    _database_obj, store, record = _store_with_flow(tmp_path)

    def explode(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("provider key sk-live-abcdef1234567890 rejected")

    runner = FlowRunner(store=store, start_mission=explode)
    with pytest.raises(RuntimeError):
        runner.run_flow(record.id)
    stored = store.list_runs(flow_id=record.id)[0]
    assert stored.status == "failed"
    assert "sk-live-abcdef1234567890" not in (stored.error or "")
    assert store.has_active_run(record.id) is False


# ---------------------------------------------------------------- triggers ---


def test_schedule_trigger_fires_once_per_interval(tmp_path: Path) -> None:
    database, store, record = _store_with_flow(tmp_path)
    recorder = _MissionRecorder()
    runner = FlowRunner(
        store=store, start_mission=recorder, mission_state=lambda _mission: "completed"
    )
    start = utc_now()
    trigger = store.add_trigger(record.id, kind="schedule", interval_seconds=600)
    with database.connect() as connection:
        connection.execute(
            "UPDATE flow_triggers SET next_run_at = ? WHERE id = ?",
            (start.isoformat(), trigger.id),
        )

    assert runner.tick(now=start + timedelta(seconds=1))["total"] == 1
    assert len(recorder.calls) == 1
    assert runner.tick(now=start + timedelta(seconds=5))["total"] == 0
    assert runner.tick(now=start + timedelta(seconds=602))["total"] == 1
    assert len(recorder.calls) == 2


def test_event_trigger_uses_watermark_without_replay(tmp_path: Path) -> None:
    database, store, record = _store_with_flow(tmp_path)
    runner = FlowRunner(
        store=store,
        start_mission=_MissionRecorder(),
        mission_state=lambda _mission: "completed",
    )
    start = utc_now()
    trigger = store.add_trigger(record.id, kind="event", event_type="task.completed")
    assert trigger.event_type == "task.completed"

    with database.connect() as connection:
        connection.execute(
            "INSERT INTO app_events (event_type, source, summary,"
            " payload_json, created_at)"
            " VALUES ('task.completed', 'test', 'done', '{}', ?)",
            (start.isoformat(),),
        )
    assert runner.tick(now=start + timedelta(seconds=1))["total"] == 1
    assert runner.tick(now=start + timedelta(seconds=2))["total"] == 0

    with database.connect() as connection:
        connection.execute(
            "INSERT INTO app_events (event_type, source, summary,"
            " payload_json, created_at)"
            " VALUES ('task.completed', 'test', 'again', '{}', ?)",
            ((start + timedelta(seconds=3)).isoformat(),),
        )
    assert runner.tick(now=start + timedelta(seconds=4))["total"] == 1


def test_trigger_bounds_are_enforced(tmp_path: Path) -> None:
    _database_obj, store, record = _store_with_flow(tmp_path)
    with pytest.raises(FlowError, match="60"):
        store.add_trigger(record.id, kind="schedule", interval_seconds=5)
    with pytest.raises(FlowError, match="kind"):
        store.add_trigger(record.id, kind="webhook")
    with pytest.raises(FlowError, match="event"):
        store.add_trigger(record.id, kind="event", event_type="")
    with pytest.raises(FlowError, match="future"):
        store.add_trigger(record.id, kind="schedule", run_at=utc_now() - timedelta(days=1))


def test_trigger_cap_per_flow(tmp_path: Path) -> None:
    _database_obj, store, record = _store_with_flow(tmp_path)
    for _ in range(LIMITS["max_triggers_per_flow"]):
        store.add_trigger(record.id, kind="manual")
    with pytest.raises(FlowError, match="at most"):
        store.add_trigger(record.id, kind="manual")


# ----------------------------------------------------------------- service ---


def test_service_flow_surface_end_to_end(tmp_path: Path) -> None:
    service = LocalService(_settings(tmp_path))
    try:
        validated = service.validate_flow(_document())
        assert validated["valid"] is True
        assert validated["levels"] == [["gather"], ["deliver"]]

        created = service.create_flow(_document())
        flow_id = created["id"]
        assert created["version"] == 1

        listed = service.list_flows(limit=5)
        assert listed["total"] == 1
        assert listed["flows"][0]["node_count"] == 2

        shown = service.get_flow(flow_id)
        assert shown["name"] == "Morning report"
        assert shown["levels"] == [["gather"], ["deliver"]]

        run = service.run_flow(flow_id)
        assert run["mission_id"]
        assert service.flow_runs(flow_id)["total"] == 1

        trigger = service.flow_add_trigger(
            flow_id, {"kind": "schedule", "interval_seconds": 900}
        )
        assert trigger["next_run_at"]
        assert service.flow_triggers(flow_id)["total"] == 1
        assert (
            service.flow_set_trigger_enabled(trigger["id"], enabled=False)["enabled"]
            is False
        )

        exported = service.export_flow(flow_id)
        assert exported["checksum"]
        imported = service.import_flow(exported, overwrite=True)
        assert imported["version"] >= 2

        updated = service.update_flow(flow_id, _document(name="Renamed flow"))
        assert updated["version"] == 3
        assert updated["name"] == "Renamed flow"

        assert service.set_flow_enabled(flow_id, enabled=False)["enabled"] is False
        assert service.flow_tick(limit=5)["total"] == 0

        stats = service.flow_stats()["flows"]
        assert stats["total"] == 1
        assert stats["runs"] == 1
        assert service.flow_delete_trigger(trigger["id"])["deleted"] is True
        assert service.delete_flow(flow_id)["deleted"] is True
    finally:
        service.close()


def test_service_reports_a_real_error_for_unknown_flow(tmp_path: Path) -> None:
    service = LocalService(_settings(tmp_path))
    try:
        with pytest.raises(Exception) as excinfo:
            service.get_flow("does-not-exist")
        assert "not found" in str(excinfo.value).lower()
    finally:
        service.close()


# --------------------------------------------------------------------- API ---


def _get(port: int, path: str, token: str) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers={"Authorization": "Bearer " + token}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(port: int, path: str, payload: dict, token: str) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _delete(port: int, path: str, token: str) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"Authorization": "Bearer " + token},
        method="DELETE",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_api_exposes_flow_routes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    service = LocalService(settings)
    server = LocalApiServer(replace(settings, api_port=0), service=service)
    server.start()
    try:
        port, token = server.address[1], server.token

        created = _post(port, "/v1/flows", _document(), token)
        assert created["ok"] is True
        flow_id = created["data"]["id"]

        listed = _get(port, "/v1/flows?limit=5", token)
        assert listed["data"]["total"] == 1

        validated = _post(port, "/v1/flows/validate", _document(), token)
        assert validated["data"]["valid"] is True

        shown = _get(port, f"/v1/flows/{flow_id}", token)
        assert shown["data"]["name"] == "Morning report"

        ran = _post(port, f"/v1/flows/{flow_id}/run", {}, token)
        assert ran["data"]["mission_id"]

        exported = _get(port, f"/v1/flows/{flow_id}/export", token)
        assert exported["data"]["checksum"]

        imported = _post(
            port,
            "/v1/flows/import",
            {"document": exported["data"], "overwrite": True},
            token,
        )
        assert imported["data"]["version"] >= 2

        trigger = _post(
            port,
            f"/v1/flows/{flow_id}/triggers",
            {"kind": "schedule", "interval_seconds": 300},
            token,
        )
        assert trigger["data"]["next_run_at"]

        toggled = _post(
            port,
            f"/v1/flow-triggers/{trigger['data']['id']}/enabled",
            {"enabled": False},
            token,
        )
        assert toggled["data"]["enabled"] is False

        assert _post(port, "/v1/flows/tick", {"limit": 5}, token)["data"]["total"] == 0
        assert _get(port, "/v1/flows/stats", token)["data"]["flows"]["total"] == 1
        assert _get(port, f"/v1/flows/{flow_id}/runs", token)["data"]["total"] == 1
        assert _get(port, "/v1/flow-runs?limit=5", token)["data"]["total"] >= 1

        updated = _post(
            port,
            f"/v1/flows/{flow_id}/definition",
            _document(name="API renamed"),
            token,
        )
        assert updated["data"]["name"] == "API renamed"

        disabled = _post(
            port, f"/v1/flows/{flow_id}/enabled", {"enabled": False}, token
        )
        assert disabled["data"]["enabled"] is False

        assert _delete(port, f"/v1/flow-triggers/{trigger['data']['id']}", token)["ok"]
        assert _delete(port, f"/v1/flows/{flow_id}", token)["data"]["deleted"] is True

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(port, "/v1/flows/missing-flow", token)
        assert excinfo.value.code == 404
    finally:
        server.stop()
        service.close()


# --------------------------------------------------------------------- CLI ---


def test_cli_flow_surface(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    document_path = tmp_path / "flow.json"
    document_path.write_text(json.dumps(_document()), encoding="utf-8")
    export_path = tmp_path / "export.json"
    root = ["--root", str(tmp_path)]

    assert (
        main([*root, "flows", "validate", "--file", str(document_path), "--json"]) == 0
    )
    assert json.loads(capsys.readouterr().out)["valid"] is True

    assert main([*root, "flows", "create", "--file", str(document_path), "--json"]) == 0
    flow_id = json.loads(capsys.readouterr().out)["id"]

    assert main([*root, "flows", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 1

    assert main([*root, "flows", "show", flow_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "Morning report"

    assert main([*root, "flows", "show", flow_id]) == 0
    text = capsys.readouterr().out
    assert "gather" in text and "deliver" in text

    assert main([*root, "flows", "run", flow_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mission_id"]

    assert main([*root, "flows", "runs", flow_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 1

    assert (
        main(
            [
                *root,
                "flows",
                "trigger",
                "add",
                flow_id,
                "--kind",
                "schedule",
                "--interval-seconds",
                "600",
                "--json",
            ]
        )
        == 0
    )
    trigger = json.loads(capsys.readouterr().out)
    assert trigger["next_run_at"]

    assert main([*root, "flows", "trigger", "list", flow_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 1

    assert (
        main([*root, "flows", "export", flow_id, "--out", str(export_path), "--json"]) == 0
    )
    exported = json.loads(capsys.readouterr().out)
    assert exported["checksum"]
    assert (
        json.loads(export_path.read_text(encoding="utf-8"))["checksum"]
        == exported["checksum"]
    )

    assert (
        main(
            [
                *root,
                "flows",
                "import",
                "--file",
                str(export_path),
                "--overwrite",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["version"] >= 2

    assert main([*root, "flows", "tick", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 0

    assert main([*root, "flows", "stats", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["flows"]["total"] == 1

    assert main([*root, "flows", "disable", flow_id]) == 0
    capsys.readouterr()
    assert main([*root, "flows", "enable", flow_id]) == 0
    capsys.readouterr()
    assert main([*root, "flows", "delete", flow_id]) == 0
    capsys.readouterr()

    assert main([*root, "doctor"]) == 0
    assert "Flows: flows=" in capsys.readouterr().out


# ------------------------------------------------- diagnostics and releases ---


def test_diagnostics_and_release_cover_flows(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    assert flows_summary_line(settings, database).startswith("flows=")

    audit = audit_install(settings)
    assert len(audit.checks) == 33
    check = next(item for item in audit.checks if item.name == "flow_builder")
    assert check.status == "pass"
    assert "flow_definitions" in check.detail


def test_schema_v14_creates_flow_tables(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    with database.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    for table in ("flow_definitions", "flow_versions", "flow_triggers", "flow_runs"):
        assert table in tables
    assert "automation_runs" in tables  # the v13 ledger survives the v14 migration
    assert database.schema_version() == LATEST_SCHEMA_VERSION


def test_identity_and_web_panel_are_current() -> None:
    assert __version__ == "0.81.2"
    assert TRANSFORMATION_PHASE == 18
    assert TRANSFORMATION_LABEL == "TRANSFORMATION PHASE 18 Production Hardening"
    assert LATEST_SCHEMA_VERSION == 18
    assert "Workflow Builder" in INDEX_HTML
    for marker in ("/v1/flows", "/v1/flow-runs", "flow-create", "flow-tick"):
        assert marker in INDEX_HTML


def test_gui_controller_exposes_flow_actions(tmp_path: Path) -> None:
    from pc_agent.gui.controller import GuiController

    controller = GuiController(_settings(tmp_path))
    document = json.dumps(_document())
    assert controller.flow_validate(document).startswith("valid:")
    assert controller.flow_save(document).startswith("created ")
    assert "Morning report" in controller.flow_show_text("Morning report")
    assert controller.flow_run("Morning report").startswith("started mission ")
    assert controller.flow_tick().startswith("fired ")
    assert "checksum" in controller.flow_export_text("Morning report")
    assert controller.flow_runs(limit=5)
    assert controller.flows_list(limit=5)
    assert controller.flows_summary_text().startswith("flows=")
