"""Transformation Phase 9: specialized multi-agent runtime + Agent Studio."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from pc_agent import __version__
from pc_agent.agents import (
    BUILTIN_AGENTS,
    AgentStudio,
    build_agent_registry,
    builtin_tool_ids,
)
from pc_agent.api_server import LocalApiServer
from pc_agent.baseline import TRANSFORMATION_LABEL, TRANSFORMATION_PHASE
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.documents.pdf import create_demo_pdfs
from pc_agent.errors import AgentError
from pc_agent.gui.controller import GuiController
from pc_agent.planning import PlannerOptions, plan_with_provider
from pc_agent.storage.db import LATEST_SCHEMA_VERSION, Database


def _studio(tmp_path: Path, **kwargs) -> tuple[Settings, Database, AgentStudio]:
    settings = Settings.from_env(tmp_path)
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    return settings, database, AgentStudio(settings, database, **kwargs)


class _CapturingRouter:
    """Returns a fixed payload; records the request for prompt assertions."""

    def __init__(self, payload) -> None:
        self._payload = payload
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        content = (
            self._payload
            if isinstance(self._payload, str)
            else json.dumps(self._payload)
        )
        return SimpleNamespace(
            response=SimpleNamespace(content=content, provider="stub", model="test"),
            attempts=(),
        )


def test_phase9_identity():
    assert TRANSFORMATION_PHASE == 18
    assert TRANSFORMATION_LABEL == "TRANSFORMATION PHASE 18 Production Hardening"
    assert __version__ == "0.81.2"
    assert LATEST_SCHEMA_VERSION == 18


def test_builtins_seeded_idempotently_and_registry_filtering(tmp_path):
    _, _, studio = _studio(tmp_path)
    assert studio.ensure_builtins() == len(BUILTIN_AGENTS) == 6
    assert studio.ensure_builtins() == 0
    names = [agent["name"] for agent in studio.list_agents()["agents"]]
    assert names == sorted(spec["name"] for spec in BUILTIN_AGENTS)

    # User edits survive re-seeding.
    studio.save_agent(
        {"name": "reviewer", "role": "Chief Reviewer", "allowed_tools": "pdf.inspect"}
    )
    studio.ensure_builtins()
    assert studio.get_agent("reviewer")["role"] == "Chief Reviewer"

    subset = build_agent_registry(["pdf.inspect", "pdf.verify"])
    assert [tool.spec.id for tool in subset.list_tools()] == [
        "pdf.inspect",
        "pdf.verify",
    ]
    full = build_agent_registry(["*"])
    assert len(full.list_tools()) == len(builtin_tool_ids()) == 69
    empty = build_agent_registry([])
    assert empty.list_tools() == []


def test_custom_agent_crud_validation_and_builtin_protection(tmp_path):
    _, _, studio = _studio(tmp_path)
    saved = studio.save_agent(
        {
            "name": "pdf-clerk",
            "role": "PDF Clerk",
            "system_prompt": "Only inspect PDFs.",
            "allowed_tools": "pdf.inspect, pdf.verify",
            "max_steps": 3,
        }
    )
    assert saved["builtin"] is False
    assert saved["allowed_tools"] == ["pdf.inspect", "pdf.verify"]
    assert studio.delete_agent("pdf-clerk") == {"deleted": "pdf-clerk"}

    with pytest.raises(AgentError, match="cannot be deleted"):
        studio.delete_agent("reviewer")
    disabled = studio.set_agent_enabled("reviewer", False)
    assert disabled["enabled"] is False
    with pytest.raises(AgentError, match="disabled"):
        studio.run_agent("reviewer", "List files in workspace")

    with pytest.raises(AgentError, match="Agent name"):
        studio.save_agent({"name": "Bad Name!", "allowed_tools": []})
    with pytest.raises(AgentError, match="Unknown builtin tool"):
        studio.save_agent({"name": "ok-name", "allowed_tools": "nope.tool"})
    with pytest.raises(AgentError, match="max_steps"):
        studio.save_agent({"name": "ok-name", "allowed_tools": [], "max_steps": 9})
    with pytest.raises(AgentError, match="Unknown agent"):
        studio.get_agent("ghost")


def test_solo_run_succeeds_with_filtered_registry(tmp_path):
    settings, _, studio = _studio(tmp_path)
    create_demo_pdfs(settings.workspace_dir)
    result = studio.run_agent(
        "document-worker", "Merge PDFs from Folder A and Folder B"
    )
    assert result["status"] == "succeeded"
    assert result["task_id"]
    assert (settings.workspace_dir / "merged.pdf").is_file()
    runs = studio.list_runs(agent_name="document-worker")["runs"]
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["task_id"] == result["task_id"]


def test_least_privilege_blocks_disallowed_tool_without_side_effects(tmp_path):
    settings, _, studio = _studio(tmp_path)
    create_demo_pdfs(settings.workspace_dir)
    result = studio.run_agent("reviewer", "Merge PDFs from Folder A and Folder B")
    assert result["status"] == "needs_input"
    assert "pdf.merge_folders" in result["summary"]
    assert not list(settings.workspace_dir.glob("merged*.pdf"))


def test_ai_planner_persona_catalog_and_restriction():
    router = _CapturingRouter(
        {
            "intent": "workspace.list_files",
            "tool_id": "workspace.list_files",
            "arguments": {},
            "summary": "list workspace",
        }
    )
    options = PlannerOptions(
        agent_system_prompt="Agent: researcher. Stay read-only.",
        allowed_tool_ids=("workspace.list_files", "web.fetch"),
    )
    outcome = plan_with_provider(router, "List the workspace files", options)
    assert outcome.plan is not None
    request = router.requests[0]
    system_text = request.messages[0].content
    assert "Specialist agent role" in system_text
    assert "Stay read-only" in system_text
    payload = json.loads(request.messages[-1].content)
    catalog_tools = {item["tool_id"] for item in payload["available_tools"]}
    assert catalog_tools <= {"workspace.list_files", "web.fetch"}
    assert "workspace.list_files" in catalog_tools

    # A plan outside the restriction is rejected before execution.
    rogue = _CapturingRouter(
        {
            "intent": "pdf.merge",
            "tool_id": "pdf.merge",
            "arguments": {"input_paths": ["a.pdf", "b.pdf"], "output_path": "out.pdf"},
            "summary": "merge",
        }
    )
    outcome = plan_with_provider(rogue, "Merge a.pdf", options)
    assert outcome.plan is None
    assert "tool restriction excludes" in outcome.missing_information[0]


def test_team_explicit_dag_and_blocked_dependency(tmp_path):
    settings, _, studio = _studio(tmp_path)
    create_demo_pdfs(settings.workspace_dir)
    team = studio.run_team(
        "merge then list",
        assignments=[
            {
                "id": "a1",
                "agent": "document-worker",
                "instruction": "Merge PDFs from Folder A and Folder B",
                "depends_on": [],
            },
            {
                "id": "a2",
                "agent": "reviewer",
                "instruction": "List files in workspace",
                "depends_on": ["a1"],
            },
        ],
    )
    assert team["succeeded"] == 2
    assert [item["status"] for item in team["results"]] == ["succeeded", "succeeded"]
    ledger = studio.list_runs(team_run_id=team["team_run_id"])["runs"]
    assert len(ledger) == 2

    blocked = studio.run_team(
        "blocked demo",
        assignments=[
            {
                "id": "b1",
                "agent": "reviewer",
                "instruction": "Merge PDFs from Folder A and Folder B",
                "depends_on": [],
            },
            {
                "id": "b2",
                "agent": "researcher",
                "instruction": "List files in workspace",
                "depends_on": ["b1"],
            },
        ],
    )
    assert [item["status"] for item in blocked["results"]] == [
        "needs_input",
        "blocked",
    ]
    assert blocked["results"][1]["task_id"] is None


def test_team_validation_errors(tmp_path):
    _, _, studio = _studio(tmp_path)
    with pytest.raises(AgentError, match="unavailable agent"):
        studio.run_team(
            "g",
            assignments=[{"id": "a1", "agent": "ghost", "instruction": "x"}],
        )
    with pytest.raises(AgentError, match="coordinator plans teams"):
        studio.run_team(
            "g",
            assignments=[{"id": "a1", "agent": "coordinator", "instruction": "x"}],
        )
    with pytest.raises(AgentError, match="unknown/later"):
        studio.run_team(
            "g",
            assignments=[
                {
                    "id": "a1",
                    "agent": "researcher",
                    "instruction": "x",
                    "depends_on": ["a2"],
                }
            ],
        )
    with pytest.raises(AgentError, match="at most"):
        studio.run_team(
            "g",
            assignments=[
                {"id": f"a{i}", "agent": "researcher", "instruction": "x"}
                for i in range(7)
            ],
        )


def test_coordinator_proposes_assignments_via_validated_json(tmp_path):
    router = _CapturingRouter(
        {
            "assignments": [
                {
                    "id": "a1",
                    "agent": "document-worker",
                    "instruction": "Merge PDFs from Folder A and Folder B",
                    "depends_on": [],
                },
                {
                    "id": "a2",
                    "agent": "reviewer",
                    "instruction": "List files in workspace",
                    "depends_on": ["a1"],
                },
            ]
        }
    )
    settings, _, studio = _studio(tmp_path, router=router)
    create_demo_pdfs(settings.workspace_dir)
    team = studio.run_team("merge the demo folders and check the result")
    assert team["succeeded"] == 2
    prompt = router.requests[0].messages[-1].content
    assert "document-worker" in prompt and "untrusted data" in prompt

    bad = _CapturingRouter("not json at all")
    _, _, broken = _studio(tmp_path / "bad", router=bad)
    with pytest.raises(AgentError, match="valid JSON"):
        broken.run_team("anything")

    rogue = _CapturingRouter(
        {"assignments": [{"id": "a1", "agent": "ghost", "instruction": "x"}]}
    )
    _, _, rogue_studio = _studio(tmp_path / "rogue", router=rogue)
    with pytest.raises(AgentError, match="unavailable agent"):
        rogue_studio.run_team("anything")


def test_cli_agents_flow(tmp_path, capsys):
    assert main(["--root", str(tmp_path), "demo", "pdf"]) == 0
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "agents", "list"]) == 0
    out = capsys.readouterr().out
    assert "document-worker" in out and "reviewer" in out

    assert (
        main(
            [
                "--root", str(tmp_path), "agents", "run", "document-worker",
                "Merge PDFs from Folder A and Folder B", "--yes",
            ]
        )
        == 0
    )
    assert "succeeded" in capsys.readouterr().out

    assert (
        main(
            [
                "--root", str(tmp_path), "agents", "run", "reviewer",
                "Merge PDFs from Folder A and Folder B", "--yes",
            ]
        )
        == 1
    )
    capsys.readouterr()

    assert main(["--root", str(tmp_path), "agents", "stats"]) == 0
    assert "runs=2" in capsys.readouterr().out

    assert main(["--root", str(tmp_path), "doctor"]) == 0
    assert "Agents: agents=6" in capsys.readouterr().out


def test_service_and_api_agents_flow(tmp_path):
    settings = replace(
        Settings.from_env(tmp_path), api_port=0, scheduler_interval_seconds=0
    )
    settings.ensure_directories()
    create_demo_pdfs(settings.workspace_dir)
    server = LocalApiServer(settings)
    server.start()
    try:
        host, port = server.address
        token = server.token

        def call(method, path, body=None, auth=True):
            data = json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(
                f"http://{host}:{port}{path}", data=data, method=method
            )
            if auth:
                request.add_header("Authorization", "Bearer " + token)
            if data is not None:
                request.add_header("Content-Type", "application/json")
            return urllib.request.urlopen(request, timeout=30)

        with call("GET", "/v1/agents") as response:
            agents = json.loads(response.read())["data"]["agents"]
        assert len(agents) == 6

        with call(
            "POST",
            "/v1/agents",
            {
                "name": "pdf-clerk",
                "role": "PDF Clerk",
                "allowed_tools": ["pdf.inspect", "pdf.verify"],
            },
        ) as response:
            assert response.status == 201

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            call("DELETE", "/v1/agents/reviewer")
        assert excinfo.value.code in (400, 422)

        with call(
            "POST",
            "/v1/agents/document-worker/run",
            {"instruction": "Merge PDFs from Folder A and Folder B"},
        ) as response:
            assert response.status == 202

        deadline = time.monotonic() + 60
        status = "running"
        while time.monotonic() < deadline:
            with call("GET", "/v1/agents/runs?agent=document-worker") as response:
                runs = json.loads(response.read())["data"]["runs"]
            if runs and runs[0]["status"] != "running":
                status = runs[0]["status"]
                break
            time.sleep(0.2)
        assert status == "succeeded"

        with call(
            "POST",
            "/v1/agents/team",
            {
                "goal": "list as review",
                "assignments": [
                    {
                        "id": "a1",
                        "agent": "reviewer",
                        "instruction": "List files in workspace",
                        "depends_on": [],
                    }
                ],
            },
        ) as response:
            team = json.loads(response.read())["data"]
        assert team["accepted"] is True

        deadline = time.monotonic() + 60
        result = None
        while time.monotonic() < deadline:
            with call("GET", f"/v1/agents/team/{team['team_run_id']}") as response:
                snapshot = json.loads(response.read())["data"]
            if not snapshot["running"] and snapshot["result"] is not None:
                result = snapshot["result"]
                break
            time.sleep(0.2)
        assert result is not None and result["succeeded"] == 1

        with call("GET", "/v1/agents/stats") as response:
            stats = json.loads(response.read())["data"]["agents"]
        assert stats["agents"] == 7  # 6 builtins + pdf-clerk

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            call("GET", "/v1/agents", auth=False)
        assert excinfo.value.code == 401
    finally:
        server.stop()


def test_diagnostics_release_and_gui_agents(tmp_path):
    settings, database, _unused = _studio(tmp_path)
    create_demo_pdfs(settings.workspace_dir)

    from pc_agent.diagnostics import build_diagnostics
    from pc_agent.release import audit_install

    report = build_diagnostics(settings, database)
    agents_section = report["agents"]
    assert agents_section["stats"]["agents"] == 6
    assert {item["name"] for item in agents_section["agents"]} >= {"coordinator"}

    audit = audit_install(settings)
    checks = {check.name: check for check in audit.checks}
    assert len(audit.checks) == 33
    assert checks["agent_studio"].status == "pass"

    controller = GuiController(settings, database=database)
    try:
        agents = controller.agents_list()
        assert len(agents) == 6
        text = controller.agent_run_text(
            "document-worker",
            "Merge PDFs from Folder A and Folder B",
            use_ai=False,
        )
        assert "succeeded" in text
        assert "document-worker" in controller.agent_runs_text()
        assert controller.agents_stats_text().startswith("agents=6")
    finally:
        orchestrator = getattr(controller, "_orchestrator", None)
        if orchestrator is not None:
            orchestrator._safety.close()


def test_web_ui_agents_panel():
    from pc_agent.web_ui import INDEX_HTML

    assert "/v1/agents" in INDEX_HTML
    assert "/v1/agents/team" in INDEX_HTML
    assert "/v1/agents/runs" in INDEX_HTML
    assert "agent-run" in INDEX_HTML
