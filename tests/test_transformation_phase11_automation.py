"""Transformation Phase 11: advanced browser, desktop window control, vision.

Every test here runs against the real modules with generated images, injected
fake CDP / UIA backends, and a real SQLite database. Nothing is simulated to
make a green result: where a platform feature is absent (no CDP browser, no
Windows desktop composition) the test asserts the honest refusal path.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from pc_agent import __version__
from pc_agent.api_server import LocalApiServer
from pc_agent.baseline import TRANSFORMATION_LABEL, TRANSFORMATION_PHASE
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.desktop_automation import WindowInfo, WindowStateChange
from pc_agent.diagnostics import automation_summary_line
from pc_agent.errors import ToolError
from pc_agent.perception import (
    MAX_GRID,
    MAX_SOURCE_BYTES,
    analyse_image,
    locate_template,
    region_hint,
)
from pc_agent.release import audit_install
from pc_agent.service import LocalService
from pc_agent.storage import AutomationRepository, Database
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools.builtins import build_default_registry
from pc_agent.tools.registry import ToolContext

# ---------------------------------------------------------------- fixtures ---


def _settings(tmp_path: Path, **overrides: str) -> Settings:
    environ = {
        "PC_AGENT_ALLOW_BROWSER": "1",
        "PC_AGENT_BROWSER_PORT": "9222",
        **overrides,
    }
    settings = Settings.from_env(tmp_path, environ=environ, load_dotenv=False)
    settings.ensure_directories()
    return settings


def _database(settings: Settings) -> Database:
    database = Database(settings.database_path)
    database.initialize()
    return database


def _text_page(path: Path, size: tuple[int, int] = (900, 600)) -> Path:
    """A busy UI-like page: text rows plus a high-contrast striped button."""
    image = Image.new("RGB", size, (22, 28, 48))
    draw = ImageDraw.Draw(image)
    for row in range(8):
        draw.text((40, 40 + row * 18), f"File Edit View Help settings {row}", fill=(230, 240, 255))
    # A dense, high-contrast panel in the bottom-right grid cell (an icon grid
    # or table in a real screenshot looks like this to the edge-density probe).
    draw.rectangle([690, 455, 890, 590], fill=(40, 120, 200))
    for column in range(694, 890, 4):
        draw.rectangle([column, 459, column + 2, 586], fill=(255, 255, 255))
    image.save(path)
    return path


def _flat_page(path: Path, size: tuple[int, int] = (640, 480)) -> Path:
    Image.new("RGB", size, (120, 130, 140)).save(path)
    return path


def _png_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _recorder() -> tuple[list[dict], object]:
    recorded: list[dict] = []

    def record(payload: dict) -> None:
        recorded.append(dict(payload))

    return recorded, record


class _FakePage:
    """Minimal CDP page used to prove the wait loop and tab control."""

    def __init__(self, *, ready_after: int = 2) -> None:
        self.polls = 0
        self.ready_after = ready_after
        self.closed = False

    def wait_for(self, *, selector=None, text=None, url_contains=None, timeout_seconds=10.0):
        self.polls += 1
        matched = self.polls >= self.ready_after
        return {
            "matched": matched,
            "selector_seen": bool(matched and selector),
            "text_seen": bool(matched and text),
            "url_seen": bool(matched and url_contains),
            "url": "https://example.test/done" if matched else "https://example.test/",
            "title": "Example",
            "ready": "complete",
            "attempts": self.polls,
            "waited_ms": 40 * self.polls,
            "target_id": "page-1",
        }

    def close(self) -> None:
        self.closed = True


class _FakeBrowserClient:
    def __init__(self, *, ready_after: int = 2) -> None:
        self.page = _FakePage(ready_after=ready_after)
        self.activated: list[str] = []
        self.closed: list[str] = []

    def attach_page(self, target_id=None):
        del target_id
        return self.page

    def targets(self):
        return [
            {"id": "page-1", "type": "page", "title": "One", "url": "https://one.test"},
            {"id": "page-2", "type": "page", "title": "Two", "url": "https://two.test"},
        ]

    def activate_tab(self, target_id: str):
        self.activated.append(target_id)
        return {"activated": target_id}

    def close_tab(self, target_id: str):
        self.closed.append(target_id)
        pages = [item for item in self.targets() if item.get("type") == "page"]
        return {"closed": target_id, "pages_remaining": len(pages) - 1}


class _FakeDesktop:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[int, int, int, int] | None]] = []

    def set_window_state(self, selector, state, bounds=None):
        self.calls.append((selector.describe(), state, bounds))
        before = WindowInfo(
            handle=101,
            title="Untitled - Notepad",
            class_name="Notepad",
            process_id=4242,
            bounds=(10, 10, 400, 300),
        )
        after = WindowInfo(
            handle=101,
            title="Untitled - Notepad",
            class_name="Notepad",
            process_id=4242,
            is_minimized=state == "minimize",
            bounds=bounds or before.bounds,
        )
        return WindowStateChange(
            window_before=before,
            window_after=after,
            state=state,
            requested=state,
            changed=True,
            note="applied",
        )


# ------------------------------------------------------------------ identity ---


def test_phase11_identity_is_single_sourced() -> None:
    from pc_agent.baseline import baseline_identity

    identity = baseline_identity()
    assert __version__ == "0.81.2"
    assert LATEST_SCHEMA_VERSION == 18
    assert TRANSFORMATION_PHASE == 18
    assert identity["label"] == TRANSFORMATION_LABEL
    assert identity["previous_transformation_version"] == "0.81.1"
    assert len(build_default_registry().list_tools()) == 69


def test_phase11_new_tools_are_registered_with_contracts() -> None:
    registry = build_default_registry()
    added = {
        "browser.wait",
        "browser.tabs",
        "desktop.window_state",
        "vision.screen",
        "vision.locate",
    }
    specs = {tool.spec.id: tool.spec for tool in registry.list_tools()}
    assert added <= set(specs)
    assert specs["vision.locate"].input_schema["required"] == [
        "screen_path",
        "template_path",
    ]
    assert specs["browser.wait"].input_schema["properties"]["timeout_seconds"][
        "maximum"
    ] == 120
    assert specs["desktop.window_state"].input_schema["properties"]["state"]["enum"] == [
        "minimize",
        "maximize",
        "restore",
        "move",
        "resize",
    ]


def test_schema_v13_creates_automation_ledger(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    assert database.schema_version() == 18
    with database.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "automation_runs" in tables
    assert database.initialize() == 18


# ---------------------------------------------------------------- perception ---


def test_analyse_image_finds_dense_regions_without_guessing(tmp_path: Path) -> None:
    page = _text_page(tmp_path / "page.png")
    analysis = analyse_image(_png_bytes(page), grid=4)
    assert analysis.width == 900
    assert analysis.height == 600
    assert analysis.busy_cells >= 1
    assert analysis.text_likely is True
    hint = region_hint(analysis)
    assert "densest cell" in hint
    assert "bottom-right" in hint


def test_flat_image_is_reported_as_no_text(tmp_path: Path) -> None:
    flat = _flat_page(tmp_path / "flat.png")
    analysis = analyse_image(_png_bytes(flat), grid=4)
    assert analysis.busy_cells == 0
    assert analysis.text_likely is False
    assert "flat" in region_hint(analysis)


def test_analyse_image_rejects_oversized_and_non_image_payloads(tmp_path: Path) -> None:
    with pytest.raises(ToolError):
        analyse_image(b"not-an-image")
    with pytest.raises(ToolError):
        analyse_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_SOURCE_BYTES + 1))
    page = _text_page(tmp_path / "page.png")
    # A grid larger than the cap is clamped, never silently expanded.
    clamped = analyse_image(_png_bytes(page), grid=MAX_GRID + 5)
    assert clamped.grid_rows == MAX_GRID


def test_locate_template_maps_back_to_screen_coordinates(tmp_path: Path) -> None:
    screen = _text_page(tmp_path / "screen.png")
    template = tmp_path / "menu.png"
    box = (30, 30, 330, 110)
    Image.open(screen).crop(box).save(template)
    result = locate_template(_png_bytes(screen), _png_bytes(template))
    assert result["found"] is True
    assert result["score"] >= 0.9
    match = result["matches"][0]
    assert (match["x"], match["y"]) == (box[0], box[1])
    assert (match["width"], match["height"]) == (box[2] - box[0], box[3] - box[1])
    # search_steps now counts sampled scan offsets *and* every pixel of the
    # verification pass, so the bound is expressed against the documented cap.
    assert result["search_steps"] <= 2_000_000


def test_locate_template_refuses_flat_template_and_reports_absence(
    tmp_path: Path,
) -> None:
    screen = _text_page(tmp_path / "screen.png")
    flat = _flat_page(tmp_path / "flat.png", size=(40, 40))
    with pytest.raises(ToolError):
        locate_template(_png_bytes(screen), _png_bytes(flat))

    other = tmp_path / "other.png"
    image = Image.new("RGB", (120, 60), (200, 60, 60))
    draw = ImageDraw.Draw(image)
    for row in range(5):
        draw.text((5, 5 + row * 10), "ZZZ 555", fill=(0, 0, 0))
    image.save(other)
    result = locate_template(_png_bytes(screen), _png_bytes(other))
    assert result["found"] is False
    assert result["matches"] == []


def test_vision_tools_record_their_runs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _text_page(settings.workspace_dir / "page.png")
    recorded, record = _recorder()
    context = ToolContext.for_workspace(
        settings.workspace_dir, automation_recorder=record
    )
    registry = build_default_registry()

    screen = registry.invoke("vision.screen", {"path": "page.png", "grid": 4}, context)
    assert screen.success is True, screen.error
    assert screen.output["text_likely"] is True

    Image.open(settings.workspace_dir / "page.png").crop((30, 30, 330, 110)).save(
        settings.workspace_dir / "menu.png"
    )
    locate = registry.invoke(
        "vision.locate",
        {"screen_path": "page.png", "template_path": "menu.png"},
        context,
    )
    assert locate.success is True, locate.error
    assert locate.output["found"] is True
    assert [item["tool_id"] for item in recorded] == ["vision.screen", "vision.locate"]
    assert all(item["kind"] == "vision" for item in recorded)

    missing = registry.invoke("vision.screen", {"path": "absent.png"}, context)
    assert missing.success is False
    assert "not found" in (missing.error or "").lower()


# ------------------------------------------------------------------- browser ---


def test_browser_wait_polls_until_the_condition_holds() -> None:
    client = _FakeBrowserClient(ready_after=1)
    recorded, record = _recorder()
    context = ToolContext.for_workspace(
        Path("."),
        browser_allowed=True,
        browser_client_factory=lambda: client,
        automation_recorder=record,
    )
    result = build_default_registry().invoke(
        "browser.wait", {"selector": "#done", "timeout_seconds": 5}, context
    )
    assert result.success is True, result.error
    assert result.output["matched"] is True
    assert result.output["attempts"] == 1
    assert client.page.closed is True
    assert recorded and recorded[0]["kind"] == "browser"


def test_browser_wait_refuses_without_a_condition() -> None:
    client = _FakeBrowserClient()
    context = ToolContext.for_workspace(
        Path("."), browser_allowed=True, browser_client_factory=lambda: client
    )
    result = build_default_registry().invoke("browser.wait", {}, context)
    assert result.success is False
    assert "selector, text or url_contains" in (result.error or "")


def test_browser_tabs_lists_activates_and_refuses_last_tab() -> None:
    client = _FakeBrowserClient()
    context = ToolContext.for_workspace(
        Path("."), browser_allowed=True, browser_client_factory=lambda: client
    )
    registry = build_default_registry()

    listed = registry.invoke("browser.tabs", {"action": "list"}, context)
    assert listed.success is True, listed.error
    assert [tab["id"] for tab in listed.output["pages"]] == ["page-1", "page-2"]
    assert listed.output["page_count"] == 2

    activated = registry.invoke(
        "browser.tabs", {"action": "activate", "target_id": "page-2"}, context
    )
    assert activated.success is True, activated.error
    assert client.activated == ["page-2"]

    closed = registry.invoke(
        "browser.tabs", {"action": "close", "target_id": "page-2"}, context
    )
    assert closed.success is True, closed.error
    assert closed.output["pages_remaining"] == 1
    assert client.closed == ["page-2"]

    client.targets = lambda: [
        {"id": "page-1", "type": "page", "title": "One", "url": "https://one.test"}
    ]

    class _RefusingBrowser(_FakeBrowserClient):
        def close_tab(self, target_id: str):
            raise ToolError("Refusing to close the last page tab; close the browser window instead.")

    refusing = ToolContext.for_workspace(
        Path("."), browser_allowed=True, browser_client_factory=_RefusingBrowser
    )
    refused = registry.invoke(
        "browser.tabs", {"action": "close", "target_id": "page-1"}, refusing
    )
    assert refused.success is False
    assert "last" in (refused.error or "").lower()


def test_page_session_wait_probe_is_bounded_and_parses_state() -> None:
    from pc_agent.browser_cdp import _wait_probe_js

    script = _wait_probe_js(
        selector="#main", text="Ready", url_contains="/done"
    )
    assert "querySelector" in script
    assert "document.readyState" in script
    assert "location.href" in script
    # The probe must never interpolate raw user text without JSON quoting.
    assert json.dumps("Ready") in script or "'Ready'" in script

    empty = _wait_probe_js(selector="", text="", url_contains="")
    assert "readyState" in empty

    hostile = _wait_probe_js(selector="a'); alert(1); ('", text="", url_contains="")
    assert "alert(1)" not in hostile.replace(json.dumps("a'); alert(1); ('"), "")


# ------------------------------------------------------------ desktop window ---


def test_desktop_window_state_validates_bounds_before_touching_windows() -> None:
    backend = _FakeDesktop()
    context = ToolContext.for_workspace(
        Path("."),
        desktop_automation_allowed=True,
        desktop_automation_backend_factory=lambda: backend,
    )
    registry = build_default_registry()

    ok = registry.invoke(
        "desktop.window_state",
        {"state": "resize", "bounds": [10, 20, 640, 480], "window_title_contains": "Notepad"},
        context,
    )
    assert ok.success is True, ok.error
    assert ok.output["state"] == "resize"
    assert ok.output["bounds"] == [10, 20, 640, 480]
    assert backend.calls and backend.calls[0][1] == "resize"

    too_small = registry.invoke(
        "desktop.window_state",
        {"state": "resize", "bounds": [0, 0, 60, 60], "window_title_contains": "Notepad"},
        context,
    )
    assert too_small.success is False
    assert "120" in (too_small.error or "")

    bad_state = registry.invoke(
        "desktop.window_state",
        {"state": "teleport", "window_title_contains": "Notepad"},
        context,
    )
    assert bad_state.success is False
    assert "allowed value" in (bad_state.error or "")


def test_window_state_change_requires_real_difference() -> None:
    window = WindowInfo(
        handle=7,
        title="Notepad",
        class_name="Notepad",
        process_id=99,
        is_minimized=True,
    )
    assert isinstance(window.handle, int)
    change = WindowStateChange(
        window_before=window,
        window_after=window,
        state="minimize",
        requested="minimize",
        changed=False,
        note="already minimized",
    )
    assert change.changed is False
    assert change.state == "minimize"
    assert change.window_after is not None and change.window_after.is_minimized is True


# --------------------------------------------------------------- repository ---


def test_automation_repository_records_lists_and_summarises(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository = AutomationRepository(_database(settings))
    assert repository.summary() == {
        "total": 0,
        "by_kind": {},
        "refused": 0,
        "last_at": None,
    }

    ok = repository.record(
        kind="browser",
        tool_id="browser.wait",
        action="wait",
        target="#done",
        status="ok",
        detail="matched=True",
        duration_ms=123,
        allowed=True,
    )
    refused = repository.record(
        kind="desktop",
        tool_id="desktop.window_state",
        action="minimize",
        target="Notepad",
        status="refused",
        detail="policy",
        allowed=False,
    )
    summary = repository.summary()
    assert summary["total"] == 2
    assert summary["by_kind"] == {"browser": 1, "desktop": 1}
    assert summary["refused"] == 1
    assert repository.get(ok.id).tool_id == "browser.wait"
    assert [run.id for run in repository.list_runs(kind="desktop")] == [refused.id]
    assert repository.list_runs(status="ok")[0].id == ok.id
    assert repository.get(999_999) is None


def test_automation_repository_never_stores_file_contents(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    repository = AutomationRepository(database)
    repository.record(
        kind="vision",
        tool_id="vision.screen",
        action="screen",
        target="page.png",
        status="ok",
        detail="grid=4",
        allowed=True,
    )
    with database.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(automation_runs)")
        }
    assert "content" not in columns
    with database.connect() as connection:
        row = connection.execute(
            "SELECT target, detail FROM automation_runs"
        ).fetchone()
    assert row["target"] == "page.png"
    assert row["detail"] == "grid=4"


# ------------------------------------------------------------------ service ---


def test_service_exposes_automation_surface(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _text_page(settings.workspace_dir / "page.png")
    service = LocalService(settings)
    try:
        caps = service.automation_capabilities()
        assert caps["browser"]["port"] == 9222
        assert caps["vision"]["local_analysis"] is True
        assert "vision.screen" in caps["tools"]
        assert caps["ledger"]["total"] >= 0

        screen = service.vision_screen({"path": "page.png", "grid": 4})
        assert screen["text_likely"] is True

        Image.open(settings.workspace_dir / "page.png").crop((30, 30, 330, 110)).save(
            settings.workspace_dir / "menu.png"
        )
        located = service.vision_locate(
            {"screen_path": "page.png", "template_path": "menu.png"}
        )
        assert located["found"] is True

        runs = service.automation_runs(limit=10)
        assert runs["count"] == 2
        assert service.automation_stats()["automation"]["total"] == 2
    finally:
        service.close()


def test_service_reports_real_failure_for_missing_image(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    service = LocalService(settings)
    try:
        from pc_agent.errors import PlanningError

        with pytest.raises((PlanningError, ToolError)):
            service.vision_screen({"path": "ghost.png"})
        with pytest.raises(PlanningError):
            service.vision_locate({"screen_path": "a.png"})
    finally:
        service.close()


# ---------------------------------------------------------------------- API ---


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


def test_api_exposes_automation_routes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _text_page(settings.workspace_dir / "page.png")
    from dataclasses import replace

    service = LocalService(settings)
    server = LocalApiServer(replace(settings, api_port=0), service=service)
    server.start()
    try:
        host, port = server.address
        assert host == "127.0.0.1"
        token = server.token
        caps = _get(port, "/v1/automation/capabilities", token)
        assert caps["ok"] is True
        assert "vision.locate" in caps["data"]["tools"]

        screen = _post(port, "/v1/vision/screen", {"path": "page.png", "grid": 4}, token)
        assert screen["ok"] is True
        assert screen["data"]["busy_cells"] >= 1

        runs = _get(port, "/v1/automation/runs?limit=5", token)
        assert runs["ok"] is True
        assert runs["data"]["count"] == 1
        assert runs["data"]["runs"][0]["tool_id"] == "vision.screen"

        stats = _get(port, "/v1/automation/stats", token)
        assert stats["data"]["automation"]["total"] == 1
    finally:
        server.stop()
        service.close()


# ---------------------------------------------------------------------- CLI ---


def test_cli_automation_surface(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = _settings(tmp_path)
    _text_page(settings.workspace_dir / "page.png")
    root = ["--root", str(tmp_path)]

    assert main([*root, "automation", "capabilities", "--json"]) == 0
    caps = json.loads(capsys.readouterr().out)
    assert caps["vision"]["local_analysis"] is True

    assert main([*root, "automation", "screen", "--path", "page.png", "--grid", "4", "--json"]) == 0
    screen = json.loads(capsys.readouterr().out)
    assert screen["text_likely"] is True
    assert screen["busy_cells"] >= 1

    assert main([*root, "automation", "screen", "--path", "page.png"]) == 0
    text = capsys.readouterr().out
    assert "text_likely=True" in text
    assert "None" not in text

    assert main([*root, "automation", "stats", "--json"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["total"] >= 1

    assert main([*root, "doctor"]) == 0
    doctor = capsys.readouterr().out
    assert "Automation: browser=" in doctor
    assert "ledger=" in doctor


# ------------------------------------------------- diagnostics and hardening ---


def test_diagnostics_and_release_cover_automation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    line = automation_summary_line(settings, database)
    assert line.startswith("browser=")
    assert "vision=local" in line

    audit = audit_install(settings)
    assert len(audit.checks) == 33
    names = {check.name for check in audit.checks}
    assert "automation_audit" in names
    check = next(item for item in audit.checks if item.name == "automation_audit")
    assert check.status == "pass"
    assert "automation_runs" in check.detail


def test_gui_exposes_thirteen_tabs_and_automation_actions() -> None:
    from pc_agent.gui.app import PcAgentGuiApp
    from pc_agent.gui.controller import GuiController

    for name in (
        "_build_automation_tab",
        "_refresh_automation",
        "_automation_analyse",
        "_automation_locate",
        "_automation_browser_wait",
    ):
        assert hasattr(PcAgentGuiApp, name)
    for name in (
        "automation_capabilities_text",
        "automation_runs",
        "automation_screen_text",
        "automation_locate_text",
    ):
        assert hasattr(GuiController, name)
