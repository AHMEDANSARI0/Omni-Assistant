"""Transformation Phase 5: JARVIS Command Center (web + Tkinter controller)."""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from pc_agent.api_server import LocalApiServer
from pc_agent.baseline import TRANSFORMATION_PHASE
from pc_agent.config import Settings
from pc_agent.documents.pdf import create_demo_pdfs
from pc_agent.gui.controller import GuiController
from pc_agent.service import LocalService
from pc_agent.web_ui import INDEX_HTML

MERGE = "Merge PDFs from Folder A and Folder B"


@pytest.fixture
def settings(tmp_path):
    value = Settings.from_env(tmp_path)
    value.ensure_directories()
    create_demo_pdfs(value.workspace_dir)
    return value


def test_phase5_identity():
    assert TRANSFORMATION_PHASE == 18


def test_web_ui_is_real_and_self_contained():
    html = INDEX_HTML
    assert html.startswith("<!doctype html>")
    for endpoint in (
        "/v1/health",
        "/v1/overview",
        "/v1/tasks",
        "/v1/missions",
        "/v1/approvals",
        "/v1/schedules",
        "/v1/events",
        "/v1/safety",
        "/v1/run",
    ):
        assert endpoint in html
    assert "sessionStorage" in html  # token kept per-tab only
    assert "Authorization" in html
    # No external assets: the page must work fully offline on loopback.
    assert "http://" not in html
    assert "https://" not in html
    assert 'src="' not in html.replace('data:image', '')


def test_web_ui_served_with_security_headers(settings):
    server = LocalApiServer(replace(settings, api_port=0, scheduler_interval_seconds=0))
    server.start()
    try:
        host, port = server.address
        with urllib.request.urlopen(
            f"http://{host}:{port}/", timeout=15
        ) as response:
            body = response.read().decode("utf-8")
            assert response.status == 200
            assert response.headers.get_content_type() == "text/html"
            csp = response.headers.get("Content-Security-Policy") or ""
            assert "frame-ancestors 'none'" in csp
            assert response.headers.get("X-Frame-Options") == "DENY"
        # 0.82.0 replaced this page as the default surface with the built SPA at
        # `/`; the pre-redesign panel is still served, unchanged, at `/legacy`
        # (that is the rollback path and it is asserted in
        # test_transformation_phase19_command_center_ui.py). This test keeps its
        # original intent - the Command Center page is served and is identified
        # as the Command Center - by checking whichever surface owns `/`.
        assert "COMMAND CENTER" in body or "Command Center" in body
        # The page is served without a token, data endpoints still are not.
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"http://{host}:{port}/v1/overview", timeout=15)
        assert error.value.code == 401
    finally:
        server.stop()


def test_gui_controller_command_center(settings):
    controller = GuiController(settings)
    try:
        overview = controller.command_center_overview()
        assert overview.tasks_total == 0
        assert overview.missions_total == 0
        assert "Emergency stop:" in overview.to_text()
        assert "8765" in overview.api_line

        with LocalService(settings) as service:
            mission = service.create_mission(
                "cc mission",
                task_specs=[
                    {"key": "a", "instruction": MERGE},
                    {"key": "b", "instruction": MERGE, "depends_on": ["a"]},
                ],
                source="test",
            )
        rows = controller.list_mission_rows()
        assert len(rows) == 1 and rows[0].total == 2
        detail = controller.mission_detail(mission["id"])
        assert "Execution graph levels:" in detail.text

        finished = controller.run_mission(mission["id"])
        assert finished.status == "succeeded"
        rows = controller.list_mission_rows()
        assert rows[0].succeeded == 2
        overview = controller.command_center_overview()
        assert overview.missions_total == 1
        assert overview.tasks_succeeded == 2
        assert overview.events_total > 0

        events, cursor = controller.events_since(0)
        assert cursor > 0
        assert any(event.event_type == "mission.created" for event in events)
        assert controller.events_since(cursor)[0] == []

        summary = controller.set_emergency_stop(engaged=True, reason="cc test")
        assert "off" not in summary or "ENGAGED" in summary or "engaged" in summary
        assert controller.emergency_stop_status_text() != ""
        controller.set_emergency_stop(engaged=False)
        assert controller.pending_approval_center_requests() == []
    finally:
        orchestrator = getattr(controller, "_orchestrator", None)
        if orchestrator is not None:
            orchestrator._safety.close()


def test_gui_cancel_draft_mission(settings):
    controller = GuiController(settings)
    try:
        with LocalService(settings) as service:
            mission = service.create_mission(
                "to cancel", task_specs=[{"instruction": MERGE}], source="test"
            )
        detail = controller.cancel_mission(mission["id"])
        assert detail.status == "cancelled"
        rows = controller.list_mission_rows()
        assert rows[0].status == "cancelled"
    finally:
        orchestrator = getattr(controller, "_orchestrator", None)
        if orchestrator is not None:
            orchestrator._safety.close()


def test_api_serve_cli_mentions_command_center():
    import pathlib

    cli_source = pathlib.Path("src/pc_agent/cli.py").read_text(encoding="utf-8")
    assert "Command Center (web UI): http://" in cli_source


def test_phase5_docs_and_gui_tab_source():
    import pathlib

    doc = pathlib.Path("docs/transformation_phase5.md").read_text(encoding="utf-8")
    assert "no fake controls" in doc
    app_source = pathlib.Path("src/pc_agent/gui/app.py").read_text(encoding="utf-8")
    assert '"Command Center"' in app_source
    assert "_poll_command_center" in app_source
    controller_source = pathlib.Path("src/pc_agent/gui/controller.py").read_text(
        encoding="utf-8"
    )
    assert "def command_center_overview" in controller_source