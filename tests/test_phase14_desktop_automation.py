"""Phase 14: Windows desktop UI Automation over a simulated backend."""

from __future__ import annotations

import json
import struct
import sys
import zlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from pc_agent import cli
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.desktop_automation import (
    ClickResult,
    ElementInfo,
    ElementSelector,
    KeysResult,
    ScreenshotCapture,
    TypeResult,
    WindowInfo,
    WindowsDesktopAutomationBackend,
    WindowSelector,
    encode_png_bgra,
    normalize_key_chord,
    probe_desktop_automation,
    resolve_window_from_list,
    select_element_from_list,
)
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


class FakeDesktopBackend:
    """In-memory Windows desktop with the production backend surface."""

    def __init__(self) -> None:
        self.windows: list[WindowInfo] = [
            WindowInfo(
                handle=100,
                title="Untitled - Notepad",
                class_name="Notepad",
                process_id=4242,
                process_name="Notepad.exe",
                bounds=(10, 20, 810, 620),
            ),
            WindowInfo(
                handle=200,
                title="Calculator",
                class_name="CalcFrame",
                process_id=4343,
                process_name="Calculator.exe",
                is_foreground=True,
                bounds=(100, 100, 500, 700),
            ),
        ]
        self.elements: dict[int, list[ElementInfo]] = {
            100: [
                ElementInfo(
                    index=0,
                    name="Text editor",
                    automation_id="TextEditor",
                    class_name="RichEditD2DPT",
                    control_type_id=50030,
                    control_type="Document",
                    runtime_id="42,1,1",
                    bounds=(20, 80, 780, 540),
                    is_keyboard_focusable=True,
                    is_content_element=True,
                    is_control_element=True,
                    patterns=("Value", "Text"),
                    value="Old text",
                ),
                ElementInfo(
                    index=1,
                    name="Save",
                    automation_id="SaveButton",
                    class_name="Button",
                    control_type_id=50000,
                    control_type="Button",
                    runtime_id="42,1,2",
                    bounds=(700, 550, 780, 600),
                    is_control_element=True,
                    patterns=("Invoke",),
                ),
                ElementInfo(
                    index=2,
                    name="Password",
                    automation_id="PasswordBox",
                    class_name="Edit",
                    control_type_id=50004,
                    control_type="Edit",
                    runtime_id="42,1,3",
                    bounds=(20, 550, 300, 590),
                    is_keyboard_focusable=True,
                    is_password=True,
                    is_control_element=True,
                    patterns=("Value",),
                    value="",
                ),
                ElementInfo(
                    index=3,
                    name="OK",
                    automation_id="OkOne",
                    class_name="Button",
                    control_type_id=50000,
                    control_type="Button",
                    runtime_id="42,1,4",
                    patterns=("Invoke",),
                ),
                ElementInfo(
                    index=4,
                    name="OK",
                    automation_id="OkTwo",
                    class_name="Button",
                    control_type_id=50000,
                    control_type="Button",
                    runtime_id="42,1,5",
                    patterns=("Invoke",),
                ),
            ],
            200: [],
        }
        self.focus_count = 0
        self.click_count = 0
        self.type_count = 0
        self.keys: list[str] = []
        self.corrupt_type_readback = False

    def _resolve(self, selector: WindowSelector) -> WindowInfo:
        return resolve_window_from_list(self.windows, selector)

    def list_windows(
        self, *, max_windows: int = 100, include_untitled: bool = False
    ) -> list[WindowInfo]:
        result = self.windows
        if not include_untitled:
            result = [window for window in result if window.title]
        return list(result[:max_windows])

    def inspect_window(
        self,
        selector: WindowSelector,
        *,
        max_elements: int = 200,
        include_offscreen: bool = False,
    ):
        from pc_agent.desktop_automation import InspectResult

        window = self._resolve(selector)
        all_elements = self.elements.get(window.handle, [])
        visible = [
            element
            for element in all_elements
            if include_offscreen or not element.is_offscreen
        ]
        chosen = visible[:max_elements]
        return InspectResult(
            window=window,
            elements=tuple(chosen),
            visited_count=len(all_elements),
            reported_count=len(all_elements),
            truncated=len(chosen) < len(visible),
        )

    def focus_window(self, selector: WindowSelector) -> WindowInfo:
        target = self._resolve(selector)
        self.focus_count += 1
        self.windows = [
            replace(window, is_foreground=window.handle == target.handle)
            for window in self.windows
        ]
        return self._resolve(WindowSelector(handle=target.handle))

    def click_element(
        self, window_selector: WindowSelector, element_selector: ElementSelector
    ) -> ClickResult:
        window = self._resolve(window_selector)
        element, match_count, selected_index = select_element_from_list(
            self.elements.get(window.handle, []), element_selector
        )
        self.click_count += 1
        return ClickResult(
            window_before=window,
            window_after=self._resolve(WindowSelector(handle=window.handle)),
            element=element,
            action_method="Fake UIA InvokePattern.Invoke",
            match_count=match_count,
            selected_index=selected_index,
        )

    def set_element_text(
        self,
        window_selector: WindowSelector,
        element_selector: ElementSelector,
        text: str,
    ) -> TypeResult:
        window = self._resolve(window_selector)
        elements = self.elements.get(window.handle, [])
        element, _matches, _index = select_element_from_list(
            elements, element_selector
        )
        if element.is_password:
            raise ToolError("Typing into a password field is refused by design.")
        self.type_count += 1
        actual = "tampered" if self.corrupt_type_readback else text
        updated = replace(element, value=actual)
        self.elements[window.handle] = [
            updated if item.runtime_id == element.runtime_id else item
            for item in elements
        ]
        # In the corruption case, the tool claims success but a fresh
        # orchestrator inspection should catch the mismatch.
        claimed = replace(element, value=text)
        return TypeResult(
            window=window,
            element=claimed,
            value=text,
            method="Fake UIA ValuePattern.SetValue",
        )

    def send_keys(
        self, selector: WindowSelector, keys: str, *, repeat: int = 1
    ) -> KeysResult:
        canonical, _components = normalize_key_chord(keys)
        window = self.focus_window(selector)
        self.keys.extend([canonical] * repeat)
        return KeysResult(
            window_before=window,
            window_after=self._resolve(WindowSelector(handle=window.handle)),
            keys=canonical,
            repeat=repeat,
            sent_count=repeat,
        )

    def screenshot_png(
        self, selector: WindowSelector | None = None
    ) -> ScreenshotCapture:
        png = encode_png_bgra(2, 2, bytes([0, 0, 255, 0] * 4))
        if selector is None:
            return ScreenshotCapture(
                png=png, target="Full desktop", window=None, bounds=(0, 0, 2, 2)
            )
        window = self._resolve(selector)
        return ScreenshotCapture(
            png=png,
            target=window.title,
            window=window,
            bounds=(10, 20, 12, 22),
        )


def _install_fake(
    monkeypatch: pytest.MonkeyPatch, backend: FakeDesktopBackend | None = None
) -> FakeDesktopBackend:
    selected = backend or FakeDesktopBackend()
    real = ToolContext.for_workspace.__func__

    def wrapper(cls, workspace_dir, approved_read_roots=(), **kwargs):
        kwargs["desktop_automation_backend_factory"] = lambda: selected
        return real(cls, workspace_dir, approved_read_roots, **kwargs)

    monkeypatch.setattr(ToolContext, "for_workspace", classmethod(wrapper))
    return selected


def _setup(tmp_path: Path, environ: dict[str, str] | None = None):
    settings = Settings.from_env(
        tmp_path,
        environ=(
            {"PC_AGENT_ALLOW_DESKTOP_AUTOMATION": "1"}
            if environ is None
            else environ
        ),
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


def test_selector_and_key_chord_validation() -> None:
    backend = FakeDesktopBackend()
    window = resolve_window_from_list(
        backend.windows, WindowSelector(title_contains="notepad")
    )
    assert window.handle == 100
    with pytest.raises(ToolError, match="refusing to guess"):
        resolve_window_from_list(
            [*backend.windows, replace(backend.windows[0], handle=300)],
            WindowSelector(title_contains="Notepad"),
        )
    selected, matches, index = select_element_from_list(
        backend.elements[100], ElementSelector(name="OK", index=2)
    )
    assert (selected.automation_id, matches, index) == ("OkTwo", 2, 2)
    assert normalize_key_chord("control + s") == ("Ctrl+S", ("Ctrl", "S"))
    assert normalize_key_chord("Page Down") == ("PageDown", ("PageDown",))
    assert normalize_key_chord("Shift+F12") == ("Shift+F12", ("Shift", "F12"))
    for forbidden in ("Alt+F4", "Ctrl+Alt+Delete", "Win+D", "Ctrl+Alt+Tab+X", "F13"):
        with pytest.raises(ToolError):
            normalize_key_chord(forbidden)


def test_png_encoder_roundtrip_structure() -> None:
    # Two BGRA pixels: blue then red. Alpha is forced opaque in PNG RGBA.
    png = encode_png_bgra(2, 1, bytes([255, 0, 0, 0, 0, 0, 255, 0]))
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    offset = 8
    chunks: dict[bytes, bytes] = {}
    while offset < len(png):
        size = struct.unpack(">I", png[offset : offset + 4])[0]
        kind = png[offset + 4 : offset + 8]
        payload = png[offset + 8 : offset + 8 + size]
        chunks[kind] = payload
        offset += 12 + size
    assert struct.unpack(">II", chunks[b"IHDR"][:8]) == (2, 1)
    assert zlib.decompress(chunks[b"IDAT"]) == bytes(
        [0, 0, 0, 255, 255, 255, 0, 0, 255]
    )


def test_config_and_probe(tmp_path: Path) -> None:
    default = Settings.from_env(tmp_path, environ={})
    assert default.allow_desktop_automation is False
    assert default.desktop_automation_timeout_seconds == 30.0
    enabled = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_ALLOW_DESKTOP_AUTOMATION": "yes",
            "PC_AGENT_DESKTOP_AUTOMATION_TIMEOUT_SECONDS": "12.5",
        },
    )
    assert enabled.allow_desktop_automation is True
    assert enabled.desktop_automation_timeout_seconds == 12.5
    with pytest.raises(ConfigError, match="ALLOW_DESKTOP_AUTOMATION"):
        Settings.from_env(
            tmp_path, environ={"PC_AGENT_ALLOW_DESKTOP_AUTOMATION": "sure"}
        )
    with pytest.raises(ConfigError, match="DESKTOP_AUTOMATION_TIMEOUT"):
        Settings.from_env(
            tmp_path,
            environ={"PC_AGENT_DESKTOP_AUTOMATION_TIMEOUT_SECONDS": "999"},
        )
    probe = probe_desktop_automation()
    if sys.platform != "win32":
        assert probe == {"available": False, "detail": "Windows only"}
        with pytest.raises(ToolError, match="requires Windows"):
            WindowsDesktopAutomationBackend().list_windows()


def test_desktop_disabled_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(monkeypatch)
    _s, tasks, _e, orchestrator = _setup(tmp_path, environ={})

    _t, report = _run(tasks, orchestrator, "List desktop windows")

    assert report.task.status is TaskStatus.FAILED
    assert "disabled by policy" in (report.error or "")
    assert len(report.recovery_attempts) == 1  # never auto-retried


def test_desktop_orchestrated_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _install_fake(monkeypatch)
    settings, tasks, _e, orchestrator = _setup(tmp_path)

    _t, windows = _run(tasks, orchestrator, "List desktop windows")
    assert windows.success, windows.error
    assert windows.tool_result is not None
    assert windows.tool_result.output is not None
    assert windows.tool_result.output["count"] == 2

    _t, inspected = _run(tasks, orchestrator, 'Inspect "Notepad" window')
    assert inspected.success, inspected.error
    assert inspected.verification_valid is True

    _t, focused = _run(
        tasks,
        orchestrator,
        'Focus "Notepad" window',
        approval_callback=_approve,
    )
    assert focused.success, focused.error
    assert focused.verification_valid is True
    assert backend.focus_count == 1

    _t, clicked = _run(
        tasks,
        orchestrator,
        "Click Save",
        options=PlannerOptions(
            desktop_window_title_contains="Notepad",
            desktop_click_name="Save",
        ),
        approval_callback=_approve,
    )
    assert clicked.success, clicked.error
    assert backend.click_count == 1

    _t, typed = _run(
        tasks,
        orchestrator,
        "Type the note",
        options=PlannerOptions(
            desktop_window_title_contains="Notepad",
            desktop_type_name="Text editor",
            desktop_type_text="Hello Phase 14",
        ),
        approval_callback=_approve,
    )
    assert typed.success, typed.error
    assert typed.verification_valid is True
    assert backend.elements[100][0].value == "Hello Phase 14"

    _t, keys = _run(
        tasks,
        orchestrator,
        "Save the note",
        options=PlannerOptions(
            desktop_window_title_contains="Notepad",
            desktop_keys="control+s",
        ),
        approval_callback=_approve,
    )
    assert keys.success, keys.error
    assert backend.keys == ["Ctrl+S"]

    _t, shot = _run(
        tasks,
        orchestrator,
        "Screenshot the desktop",
        options=PlannerOptions(
            output_path="desktop.png",
            desktop_window_title_contains="Notepad",
        ),
    )
    assert shot.success, shot.error
    target = settings.workspace_dir / "desktop.png"
    assert target.is_file()
    assert target.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_desktop_click_approval_denied_and_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _install_fake(monkeypatch)
    _s, tasks, execution, orchestrator = _setup(tmp_path)

    task, denied = _run(
        tasks,
        orchestrator,
        "Click Save",
        options=PlannerOptions(
            desktop_window_title_contains="Notepad",
            desktop_click_name="Save",
        ),
        approval_callback=_deny,
    )
    assert denied.task.status is TaskStatus.CANCELLED
    assert backend.click_count == 0
    approvals = execution.list_approvals(task.id)
    assert approvals and approvals[0].action == "desktop_automation_action"
    assert approvals[0].scope["tool_id"] == "desktop.window_click"
    assert approvals[0].decision == "denied"


def test_desktop_type_tampered_readback_fails_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _install_fake(monkeypatch)
    backend.corrupt_type_readback = True
    _s, tasks, _e, orchestrator = _setup(tmp_path)

    _t, report = _run(
        tasks,
        orchestrator,
        "Type the note",
        options=PlannerOptions(
            desktop_window_title_contains="Notepad",
            desktop_type_name="Text editor",
            desktop_type_text="Expected",
        ),
        approval_callback=_approve,
    )
    assert report.task.status is TaskStatus.FAILED
    assert report.verification_valid is False
    assert backend.elements[100][0].value == "tampered"


def test_direct_password_missing_and_ambiguous_elements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _install_fake(monkeypatch)
    settings, _t, _e, _o = _setup(tmp_path)
    context = ToolContext.for_workspace(
        settings.workspace_dir,
        desktop_automation_allowed=True,
        desktop_automation_backend_factory=lambda: backend,
    )
    registry = build_default_registry()
    window = {"window_title_contains": "Notepad"}
    password = registry.invoke(
        "desktop.window_type",
        {**window, "element_name": "Password", "text": "secret"},
        context,
    )
    assert not password.success and "password" in (password.error or "")

    missing = registry.invoke(
        "desktop.window_click",
        {**window, "element_name": "Missing"},
        context,
    )
    assert not missing.success and "No desktop UI element" in (missing.error or "")

    ambiguous = registry.invoke(
        "desktop.window_click", {**window, "element_name": "OK"}, context
    )
    assert not ambiguous.success and "refusing to guess" in (ambiguous.error or "")


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


def test_ai_may_read_windows_but_never_drive_desktop() -> None:
    inspected = plan_with_provider(
        _StubRouter(
            {
                "intent": "desktop.window_inspect",
                "tool_id": "desktop.window_inspect",
                "arguments": {
                    "window_title_contains": "Notepad",
                    "max_elements": 100,
                },
                "summary": "inspect window",
            }
        ),
        "Inspect Notepad",
        PlannerOptions(),
    )
    assert inspected.plan is not None
    assert inspected.plan.tool_id == "desktop.window_inspect"

    for tool_id, arguments in (
        ("desktop.window_focus", {"window_title_contains": "Notepad"}),
        (
            "desktop.window_click",
            {"window_title_contains": "Notepad", "element_name": "Save"},
        ),
        (
            "desktop.window_type",
            {
                "window_title_contains": "Notepad",
                "element_name": "Text editor",
                "text": "x",
            },
        ),
        (
            "desktop.send_keys",
            {"window_title_contains": "Notepad", "keys": "Ctrl+S"},
        ),
    ):
        with pytest.raises(PlanningError, match="not allow-listed"):
            plan_with_provider(
                _StubRouter(
                    {
                        "intent": tool_id,
                        "tool_id": tool_id,
                        "arguments": arguments,
                        "summary": "drive desktop",
                    }
                ),
                "Drive the desktop",
                PlannerOptions(),
            )


def test_cli_desktop_commands(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _install_fake(monkeypatch)
    monkeypatch.setenv("PC_AGENT_ALLOW_DESKTOP_AUTOMATION", "1")
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    root = ["--root", str(tmp_path)]

    assert main([*root, "doctor"]) == 0
    assert "Desktop automation: enabled" in capsys.readouterr().out

    assert main([*root, "desktop", "windows"]) == 0
    assert "Untitled - Notepad" in capsys.readouterr().out

    assert (
        main(
            [
                *root,
                "desktop",
                "inspect",
                "--window-title-contains",
                "Notepad",
            ]
        )
        == 0
    )
    assert "Text editor" in capsys.readouterr().out

    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    assert (
        main(
            [
                *root,
                "desktop",
                "focus",
                "--window-title-contains",
                "Notepad",
            ]
        )
        == 4
    )
    denied = capsys.readouterr().out
    assert "desktop focus" in denied and "--yes" in denied
    assert backend.focus_count == 0

    assert (
        main(
            [
                *root,
                "desktop",
                "focus",
                "--window-title-contains",
                "Notepad",
                "--yes",
            ]
        )
        == 0
    )
    assert "Focused:" in capsys.readouterr().out

    assert (
        main(
            [
                *root,
                "desktop",
                "type",
                "--window-title-contains",
                "Notepad",
                "--element-name",
                "Text editor",
                "--text",
                "CLI text",
                "--yes",
            ]
        )
        == 0
    )
    assert "CLI text" in capsys.readouterr().out
    assert backend.elements[100][0].value == "CLI text"

    assert (
        main(
            [
                *root,
                "desktop",
                "keys",
                "Ctrl+S",
                "--window-title-contains",
                "Notepad",
                "--yes",
            ]
        )
        == 0
    )
    assert "Sent Ctrl+S" in capsys.readouterr().out

    assert main([*root, "desktop", "screenshot", "cli-desktop.png"]) == 0
    capsys.readouterr()
    assert (settings.workspace_dir / "cli-desktop.png").is_file()
    assert cli is not None  # keep cli import visible for monkeypatching patterns


def test_gui_controller_desktop_and_doctor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(monkeypatch)
    settings = Settings.from_env(
        tmp_path, environ={"PC_AGENT_ALLOW_DESKTOP_AUTOMATION": "1"}
    )
    controller = GuiController(settings, desktop_opener=lambda _target: None)
    monkeypatch.setattr(
        "pc_agent.gui.controller.probe_desktop_automation",
        lambda: {"available": True, "detail": "Fake UIA"},
    )

    summary = controller.doctor_summary()
    assert "Desktop automation: enabled" in summary.to_text()
    assert "Fake UIA" in summary.to_text()

    windows = controller.desktop_windows()
    assert windows.success, windows.error
    inspected = controller.desktop_window_inspect("Notepad")
    assert inspected.success, inspected.error
    shot = controller.desktop_screenshot(
        "gui-desktop.png", title_contains="Notepad"
    )
    assert shot.success, shot.error
    assert (settings.workspace_dir / "gui-desktop.png").is_file()
