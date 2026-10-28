"""Phase 13: browser automation over a (faked) DevTools connection."""

from __future__ import annotations

import base64
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pc_agent import cli
from pc_agent.browser_cdp import (
    BrowserClient,
    WebSocketConnection,
    _SocketReader,
    ws_connect,
)
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.domain.task import Task, TaskStatus
from pc_agent.errors import ConfigError, PlanningError, ToolError
from pc_agent.gui.controller import GuiController
from pc_agent.orchestrator import TaskOrchestrator
from pc_agent.planning import (
    DeterministicPlanner,
    PlannerOptions,
    plan_with_provider,
)
from pc_agent.storage.db import Database
from pc_agent.storage.repositories import (
    ExecutionRepository,
    MemoryRepository,
    RecoveryRepository,
    TaskRepository,
)
from pc_agent.tools import build_default_registry
from pc_agent.tools.registry import ToolContext

_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8Xw8AAwMCAO+ip1sAAAAASUVORK5CYII="
)


class FakeCdpConnection:
    """Scripted CDP connection: same call/wait_event/close surface."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.calls: list[tuple[str, dict[str, Any]]] = state.setdefault("calls", [])

    def call(self, method, params=None, *, session_id=None):
        params = dict(params or {})
        self.calls.append((method, params))
        state = self.state
        if method == "Target.createTarget":
            return {"targetId": "target-1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "session-1"}
        if method in {"Page.enable", "Runtime.enable"}:
            return {}
        if method == "Page.navigate":
            state["url"] = params["url"]
            state["title"] = state["pages"].get(
                params["url"].rstrip("/"), "Fake Page"
            )
            return {}
        if method == "Input.insertText":
            state["values"][state.get("last_selector", "")] = params["text"]
            return {}
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(_PNG_1PX).decode("ascii")}
        if method == "Runtime.evaluate":
            return {"result": {"value": self._evaluate(str(params["expression"]))}}
        raise AssertionError(f"Unexpected CDP call: {method}")

    def wait_event(self, method, *, session_id=None, timeout):
        return {}

    def close(self) -> None:
        pass

    # ---------------------------------------------------------- fake page JS

    def _evaluate(self, expression: str) -> Any:
        state = self.state
        selector = _selector_of(expression)
        if "document.readyState" in expression:
            return json.dumps(
                {"url": state["url"], "title": state["title"], "ready": "complete"}
            )
        if "truncated" in expression and "a[href]" in expression:
            return json.dumps(
                {
                    "url": state["url"],
                    "title": state["title"],
                    "text": state["text"],
                    "truncated": False,
                    "links": state["links"],
                }
            )
        if "getAttribute('type')" in expression:
            state["last_selector"] = selector
            field = state["fields"].get(selector or "")
            if field is None:
                return json.dumps({"found": False, "tag": None, "type": None})
            tag, field_type = field
            return json.dumps({"found": True, "tag": tag, "type": field_type})
        if "HTMLTextAreaElement" in expression:
            state["last_selector"] = selector
            typed = _json_string_after(expression, "setter.set.call(el,")
            state["values"][selector or ""] = typed or ""
            return _maybe_corrupt(state, state["values"][selector or ""])
        if "el.select()" in expression:
            state["last_selector"] = selector
            return True
        if "String(el.value)" in expression:
            return _maybe_corrupt(state, state["values"].get(selector or "", ""))
        if "scrollIntoView" in expression:
            state["url"] = state["click_href"]
            state["title"] = state["pages"].get(state["url"], state["title"])
            return json.dumps(
                {"clicked": True, "tag": "A", "text": state["click_text"]}
            )
        if "matches:els.length" in expression:
            matches = state["click_matches"]
            return json.dumps(
                {
                    "found": matches > 0,
                    "matches": matches,
                    "tag": "A" if matches else None,
                    "text": state["click_text"] if matches else None,
                }
            )
        raise AssertionError(f"Unscripted evaluate: {expression[:120]}")


def _maybe_corrupt(state: dict[str, Any], value: str) -> str:
    return "tampered" if state.get("corrupt_value") else value


def _selector_of(expression: str) -> str | None:
    marker = "querySelector("
    index = expression.find(marker)
    if index < 0:
        return None
    return _json_string_at(expression, index + len(marker))


def _json_string_after(expression: str, marker: str) -> str | None:
    index = expression.find(marker)
    if index < 0:
        return None
    return _json_string_at(expression, index + len(marker))


def _json_string_at(expression: str, start: int) -> str | None:
    if start >= len(expression) or expression[start] != '"':
        return None
    try:
        value, _end = json.JSONDecoder().raw_decode(expression[start:])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def _fake_state() -> dict[str, Any]:
    return {
        "url": "about:blank",
        "title": "",
        "text": "Welcome to the fake example page.",
        "links": [{"text": "More info", "href": "https://example.com/info"}],
        "pages": {
            "https://example.com": "Example Domain",
            "https://example.com/info": "Info Page",
        },
        "fields": {"#name": ("input", "text"), "#pw": ("input", "password")},
        "values": {},
        "click_matches": 1,
        "click_text": "More info",
        "click_href": "https://example.com/info",
        "calls": [],
    }


def _fake_client(state: dict[str, Any]) -> BrowserClient:
    def http_get_json(url: str, timeout: float) -> Any:
        if url.endswith("/json/version"):
            return {
                "Browser": "FakeChrome/1.0",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/fake",
            }
        return [
            {
                "id": "target-1",
                "type": "page",
                "title": state["title"],
                "url": state["url"],
            }
        ]

    return BrowserClient(
        port=9222,
        timeout=5.0,
        connection_factory=lambda _url, _timeout: FakeCdpConnection(state),
        http_get_json=http_get_json,
    )


def _install_fake(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]
) -> BrowserClient:
    client = _fake_client(state)
    real = ToolContext.for_workspace.__func__

    def wrapper(cls, workspace_dir, approved_read_roots=(), **kwargs):
        kwargs["browser_client_factory"] = lambda: client
        return real(cls, workspace_dir, approved_read_roots, **kwargs)

    monkeypatch.setattr(ToolContext, "for_workspace", classmethod(wrapper))
    return client


def _setup(tmp_path: Path, environ: dict[str, str] | None = None):
    settings = Settings.from_env(
        tmp_path,
        environ=({"PC_AGENT_ALLOW_BROWSER": "1"} if environ is None else environ),
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    tasks = TaskRepository(database)
    execution = ExecutionRepository(database)
    orchestrator = TaskOrchestrator(
        settings=settings,
        task_repository=tasks,
        execution_repository=execution,
        tool_registry=build_default_registry(),
        deterministic_planner=DeterministicPlanner(settings.workspace_dir),
        recovery_repository=RecoveryRepository(database),
        memory_repository=MemoryRepository(database),
        recovery_backoff_seconds=0,
    )
    return settings, tasks, execution, orchestrator


def _run(tasks, orchestrator, instruction: str, **kwargs):
    task = tasks.create(Task(instruction=instruction))
    return task, orchestrator.execute(task.id, **kwargs)


def _approve(_request) -> bool:
    return True


def _deny(_request) -> bool:
    return False


# ---------------------------------------------------------------------- tests


def test_browser_disabled_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(monkeypatch, _fake_state())
    _s, tasks, _e, orchestrator = _setup(tmp_path, environ={})

    _t, report = _run(tasks, orchestrator, "Show browser status")

    assert report.task.status is TaskStatus.FAILED
    assert "disabled by policy" in (report.error or "")
    assert len(report.recovery_attempts) == 1  # never auto-retried


def test_browser_open_snapshot_click_type_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _fake_state()
    _install_fake(monkeypatch, state)
    _s, tasks, _e, orchestrator = _setup(tmp_path)

    _t, opened = _run(
        tasks,
        orchestrator,
        "Open the page",
        options=PlannerOptions(browser_url="https://example.com"),
        approval_callback=_approve,
    )
    assert opened.success, opened.error
    assert opened.verification_valid is True

    _t, snap = _run(tasks, orchestrator, "Read the browser page")
    assert snap.success, snap.error
    assert snap.tool_result is not None and snap.tool_result.output
    assert "fake example page" in snap.tool_result.output["text"]

    _t, clicked = _run(
        tasks,
        orchestrator,
        "Click it",
        options=PlannerOptions(browser_click_text="More info"),
        approval_callback=_approve,
    )
    assert clicked.success, clicked.error
    assert state["url"] == "https://example.com/info"

    _t, typed = _run(
        tasks,
        orchestrator,
        "Type it",
        options=PlannerOptions(
            browser_type_selector="#name", browser_type_text="Ahmed Ansari"
        ),
        approval_callback=_approve,
    )
    assert typed.success, typed.error
    assert typed.verification_valid is True
    assert state["values"]["#name"] == "Ahmed Ansari"


def test_browser_click_approval_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _fake_state()
    _install_fake(monkeypatch, state)
    _s, tasks, execution, orchestrator = _setup(tmp_path)
    _run(
        tasks,
        orchestrator,
        "Open the page",
        options=PlannerOptions(browser_url="https://example.com"),
        approval_callback=_approve,
    )
    calls_before = len(state["calls"])

    task, denied = _run(
        tasks,
        orchestrator,
        "Click it",
        options=PlannerOptions(browser_click_text="More info"),
        approval_callback=_deny,
    )
    assert denied.task.status is TaskStatus.CANCELLED
    approvals = execution.list_approvals(task.id)
    assert approvals and approvals[0].scope["tool_id"] == "browser.click"
    assert approvals[0].decision == "denied"
    assert "scrollIntoView" not in str(state["calls"][calls_before:])


def test_browser_type_tampered_readback_fails_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _fake_state()
    state["corrupt_value"] = True
    _install_fake(monkeypatch, state)
    _s, tasks, _e, orchestrator = _setup(tmp_path)
    _run(
        tasks,
        orchestrator,
        "Open the page",
        options=PlannerOptions(browser_url="https://example.com"),
        approval_callback=_approve,
    )

    _t, report = _run(
        tasks,
        orchestrator,
        "Type it",
        options=PlannerOptions(
            browser_type_selector="#name", browser_type_text="Ahmed Ansari"
        ),
        approval_callback=_approve,
    )
    assert report.task.status is TaskStatus.FAILED
    assert report.verification_valid is False


def test_browser_type_refuses_password_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _fake_state()
    client = _install_fake(monkeypatch, state)
    settings, _t, _e, _o = _setup(tmp_path)
    context = ToolContext.for_workspace(
        settings.workspace_dir,
        browser_allowed=True,
        browser_client_factory=lambda: client,
    )
    result = build_default_registry().invoke(
        "browser.type", {"selector": "#pw", "text": "secret"}, context
    )
    assert not result.success
    assert "password" in (result.error or "")


def test_browser_screenshot_task_writes_png(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(monkeypatch, _fake_state())
    settings, tasks, _e, orchestrator = _setup(tmp_path)

    _t, report = _run(
        tasks,
        orchestrator,
        "Screenshot the browser page",
        options=PlannerOptions(output_path="shot.png"),
    )
    assert report.success, report.error
    target = settings.workspace_dir / "shot.png"
    assert target.is_file()
    assert target.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_browser_open_blocks_private_url_in_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(monkeypatch, _fake_state())
    _s, tasks, _e, orchestrator = _setup(tmp_path)

    _t, report = _run(
        tasks,
        orchestrator,
        "Open the page",
        options=PlannerOptions(browser_url="http://127.0.0.1:8765/index.html"),
        approval_callback=_approve,
    )
    assert report.task.status is TaskStatus.FAILED
    assert "rivate" in (report.error or "")  # private-network block


def test_browser_click_missing_and_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _fake_state()
    client = _install_fake(monkeypatch, state)
    settings, _t, _e, _o = _setup(tmp_path)
    context = ToolContext.for_workspace(
        settings.workspace_dir,
        browser_allowed=True,
        browser_client_factory=lambda: client,
    )
    registry = build_default_registry()
    state["click_matches"] = 0
    missing = registry.invoke(
        "browser.click", {"text": "Nope"}, context
    )
    assert not missing.success and "Nothing clickable" in (missing.error or "")
    state["click_matches"] = 2
    ambiguous = registry.invoke(
        "browser.click", {"text": "More info"}, context
    )
    assert not ambiguous.success and "refusing to guess" in (ambiguous.error or "")


# ---------------------------------------------------------------------- wire


def test_websocket_framing_roundtrip() -> None:
    left, right = socket.socketpair()
    left.settimeout(5)
    right.settimeout(5)
    client = WebSocketConnection(left, _SocketReader(left))
    try:
        client.send_text("hello cdp")
        frame = right.recv(1024)
        assert frame[0] == 0x81 and frame[1] & 0x80  # masked text frame
        mask = frame[2:6]
        payload = bytes(
            byte ^ mask[index % 4] for index, byte in enumerate(frame[6:])
        )
        assert payload == b"hello cdp"

        right.sendall(bytes([0x81, 5]) + b"world")
        assert client.recv_text() == "world"

        right.sendall(bytes([0x01, 3]) + b"fra")
        right.sendall(bytes([0x80, 5]) + b"gment")
        assert client.recv_text() == "fragment"

        right.sendall(bytes([0x89, 2]) + b"hi")  # ping
        right.sendall(bytes([0x81, 2]) + b"ok")
        assert client.recv_text() == "ok"
        pong = right.recv(1024)
        assert pong[0] == 0x8A  # client answered with pong
    finally:
        client.close()
        right.close()


def test_ws_connect_rejects_remote_hosts() -> None:
    with pytest.raises(ToolError, match="localhost"):
        ws_connect("ws://example.com:9222/devtools/browser/x", timeout=1)


def test_config_browser_parsing(tmp_path: Path) -> None:
    default = Settings.from_env(tmp_path, environ={})
    assert default.allow_browser is False
    assert default.browser_port == 9222
    enabled = Settings.from_env(
        tmp_path,
        environ={"PC_AGENT_ALLOW_BROWSER": "1", "PC_AGENT_BROWSER_PORT": "9333"},
    )
    assert enabled.allow_browser is True
    assert enabled.browser_port == 9333
    with pytest.raises(ConfigError, match="PC_AGENT_ALLOW_BROWSER"):
        Settings.from_env(tmp_path, environ={"PC_AGENT_ALLOW_BROWSER": "sure"})
    with pytest.raises(ConfigError, match="PC_AGENT_BROWSER_PORT"):
        Settings.from_env(tmp_path, environ={"PC_AGENT_BROWSER_PORT": "99999"})


class _StubRouter:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def complete(self, _request):
        return SimpleNamespace(
            response=SimpleNamespace(
                content=json.dumps(self._payload), provider="stub", model="test"
            ),
            attempts=(),
        )


def test_ai_may_snapshot_but_never_click_or_type() -> None:
    outcome = plan_with_provider(
        _StubRouter(
            {
                "intent": "browser.snapshot",
                "tool_id": "browser.snapshot",
                "arguments": {"max_chars": 5000},
                "summary": "read page",
            }
        ),
        "What is on the page?",
        PlannerOptions(),
    )
    assert outcome.plan is not None
    assert outcome.plan.tool_id == "browser.snapshot"

    for tool_id, arguments in (
        ("browser.click", {"text": "Buy"}),
        ("browser.type", {"selector": "#q", "text": "x"}),
        ("browser.open", {"url": "https://example.com"}),
    ):
        with pytest.raises(PlanningError, match="not allow-listed"):
            plan_with_provider(
                _StubRouter(
                    {
                        "intent": tool_id,
                        "tool_id": tool_id,
                        "arguments": arguments,
                        "summary": "do it",
                    }
                ),
                "Do it in the browser",
                PlannerOptions(),
            )


# ------------------------------------------------------------------ CLI + GUI


def test_cli_browser_commands(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _fake_state()
    client = _install_fake(monkeypatch, state)
    monkeypatch.setenv("PC_AGENT_ALLOW_BROWSER", "1")
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    context = ToolContext.for_workspace(
        settings.workspace_dir,
        browser_allowed=True,
        browser_client_factory=lambda: client,
    )
    monkeypatch.setattr(
        cli, "_tool_context", lambda _settings, **_kwargs: context
    )
    root = ["--root", str(tmp_path)]

    assert main([*root, "doctor"]) == 0
    assert "Browser: enabled" in capsys.readouterr().out

    assert main([*root, "browser", "status"]) == 0
    assert "FakeChrome/1.0" in capsys.readouterr().out

    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    assert main([*root, "browser", "open", "https://example.com"]) == 4
    denied_out = capsys.readouterr().out
    assert "browser open https://example.com --yes" in denied_out

    assert main([*root, "browser", "open", "https://example.com", "--yes"]) == 0
    assert "Example Domain" in capsys.readouterr().out

    assert main([*root, "browser", "snapshot"]) == 0
    assert "fake example page" in capsys.readouterr().out

    assert main([*root, "browser", "screenshot", "cli-shot.png"]) == 0
    capsys.readouterr()
    assert (settings.workspace_dir / "cli-shot.png").is_file()


def test_gui_controller_browser_and_doctor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(monkeypatch, _fake_state())
    settings = Settings.from_env(
        tmp_path, environ={"PC_AGENT_ALLOW_BROWSER": "1"}
    )
    controller = GuiController(settings, desktop_opener=lambda _target: None)
    monkeypatch.setattr(
        "pc_agent.gui.controller.probe_browser",
        lambda *, port: {"Browser": "FakeChrome/1.0"},
    )

    summary = controller.doctor_summary()
    assert "Browser: enabled" in summary.to_text()
    assert "FakeChrome/1.0" in summary.to_text()

    status = controller.browser_status()
    assert status.success, status.error
    snapshot = controller.browser_snapshot()
    assert snapshot.success, snapshot.error
