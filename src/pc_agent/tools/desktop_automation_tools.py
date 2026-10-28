"""Windows UI Automation built-in tools (Phase 14).

The handlers are intentionally thin: policy gating, strict selector parsing
and workspace-safe screenshot writes live here, while native Win32/UIA calls
live in :mod:`pc_agent.desktop_automation`.  Actions are exact-selector based;
there is no free-form coordinate or arbitrary-key tool.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any

from ..desktop_automation import (
    DesktopAutomationBackend,
    ElementInfo,
    ElementSelector,
    WindowInfo,
    WindowsDesktopAutomationBackend,
    WindowSelector,
    parse_window_handle,
)
from ..errors import ToolError
from ..policies.policy import Action, ActionRequest
from .registry import ToolContext, ToolHandler, ToolRegistry, ToolSpec
from .workspace_tools import _resolve_allowed, relative_posix

_DISABLED_MESSAGE = (
    "Windows desktop automation is disabled by policy "
    "(set PC_AGENT_ALLOW_DESKTOP_AUTOMATION=1 in .env; see docs/phase14.md)."
)


def _backend(context: ToolContext) -> DesktopAutomationBackend:
    if not context.desktop_automation_allowed:
        raise ToolError(_DISABLED_MESSAGE)
    decision = context.policy_engine.evaluate(
        ActionRequest(
            Action.DESKTOP_AUTOMATION,
            description="Use one reviewed Windows UI Automation built-in tool.",
        )
    )
    if not decision.allowed:
        raise ToolError(f"Policy {decision.outcome.value}: {decision.reason}")
    if context.desktop_automation_backend_factory is not None:
        return context.desktop_automation_backend_factory()
    return WindowsDesktopAutomationBackend(
        timeout_seconds=context.desktop_automation_timeout_seconds
    )


def _has_window_criteria(arguments: dict[str, Any]) -> bool:
    return any(
        arguments.get(name) not in (None, "")
        for name in (
            "window_handle",
            "window_title",
            "window_title_contains",
            "window_class_name",
            "window_process_id",
        )
    )


def _window_selector(
    arguments: dict[str, Any], *, required: bool = True
) -> WindowSelector | None:
    if not _has_window_criteria(arguments):
        if required:
            raise ToolError(
                "Choose a desktop window with window_handle, window_title, "
                "window_title_contains, window_class_name or window_process_id."
            )
        return None
    return WindowSelector(
        handle=parse_window_handle(arguments.get("window_handle")),
        title=arguments.get("window_title"),
        title_contains=arguments.get("window_title_contains"),
        class_name=arguments.get("window_class_name"),
        process_id=arguments.get("window_process_id"),
    )


def _element_selector(arguments: dict[str, Any]) -> ElementSelector:
    return ElementSelector(
        runtime_id=arguments.get("element_runtime_id"),
        name=arguments.get("element_name"),
        name_contains=arguments.get("element_name_contains"),
        automation_id=arguments.get("element_automation_id"),
        class_name=arguments.get("element_class_name"),
        control_type=arguments.get("element_control_type"),
        index=arguments.get("element_index"),
    )


def _bounds(bounds: tuple[int, int, int, int]) -> dict[str, int]:
    left, top, right, bottom = bounds
    return {
        "left": int(left),
        "top": int(top),
        "right": int(right),
        "bottom": int(bottom),
        "width": max(0, int(right) - int(left)),
        "height": max(0, int(bottom) - int(top)),
    }


def _window_dict(window: WindowInfo) -> dict[str, Any]:
    return {
        "handle": str(window.handle),
        "title": str(window.title)[:1000],
        "class_name": str(window.class_name)[:256],
        "process_id": int(window.process_id),
        "process_name": str(window.process_name)[:256],
        "is_foreground": bool(window.is_foreground),
        "is_minimized": bool(window.is_minimized),
        "bounds": _bounds(window.bounds),
    }


def _element_dict(element: ElementInfo) -> dict[str, Any]:
    return {
        "index": int(element.index),
        "name": str(element.name)[:1000],
        "automation_id": str(element.automation_id)[:500],
        "class_name": str(element.class_name)[:256],
        "control_type": str(element.control_type)[:100],
        "control_type_id": int(element.control_type_id),
        "localized_control_type": str(element.localized_control_type)[:100],
        "runtime_id": str(element.runtime_id)[:500],
        "native_window_handle": str(int(element.native_window_handle)),
        "bounds": _bounds(element.bounds),
        "is_enabled": bool(element.is_enabled),
        "is_offscreen": bool(element.is_offscreen),
        "has_keyboard_focus": bool(element.has_keyboard_focus),
        "is_keyboard_focusable": bool(element.is_keyboard_focusable),
        "is_password": bool(element.is_password),
        "is_content_element": bool(element.is_content_element),
        "is_control_element": bool(element.is_control_element),
        "patterns": [str(pattern)[:100] for pattern in element.patterns],
        "value": (str(element.value)[:10_000] if element.value is not None else None),
    }


def _windows(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    backend = _backend(context)
    windows = backend.list_windows(
        max_windows=int(arguments.get("max_windows", 100)),
        include_untitled=bool(arguments.get("include_untitled", False)),
    )
    foreground = next((window for window in windows if window.is_foreground), None)
    return {
        "windows": [_window_dict(window) for window in windows],
        "count": len(windows),
        "foreground_handle": str(foreground.handle) if foreground else "",
        "foreground_title": str(foreground.title)[:1000] if foreground else "",
    }


def _inspect(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    selector = _window_selector(arguments)
    assert selector is not None
    result = _backend(context).inspect_window(
        selector,
        max_elements=int(arguments.get("max_elements", 200)),
        include_offscreen=bool(arguments.get("include_offscreen", False)),
    )
    return {
        "window": _window_dict(result.window),
        "elements": [_element_dict(element) for element in result.elements],
        "element_count": len(result.elements),
        "visited_count": int(result.visited_count),
        "reported_count": int(result.reported_count),
        "truncated": bool(result.truncated),
    }


def _focus(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    selector = _window_selector(arguments)
    assert selector is not None
    window = _backend(context).focus_window(selector)
    return {"window": _window_dict(window), "focused": bool(window.is_foreground)}


def _click(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    window_selector = _window_selector(arguments)
    assert window_selector is not None
    result = _backend(context).click_element(
        window_selector, _element_selector(arguments)
    )
    return {
        "clicked": True,
        "action_method": str(result.action_method)[:200],
        "match_count": int(result.match_count),
        "selected_index": int(result.selected_index),
        "window": _window_dict(result.window_before),
        "window_after": (
            _window_dict(result.window_after) if result.window_after is not None else None
        ),
        "window_closed": result.window_after is None,
        "element": _element_dict(result.element),
    }


def _type(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    window_selector = _window_selector(arguments)
    assert window_selector is not None
    text = str(arguments.get("text") or "")
    if len(text) > 4000:
        raise ToolError("Desktop type text is limited to 4000 characters.")
    result = _backend(context).set_element_text(
        window_selector, _element_selector(arguments), text
    )
    return {
        "window": _window_dict(result.window),
        "element": _element_dict(result.element),
        "value": str(result.value),
        "typed_length": len(text),
        "value_method": str(result.method)[:200],
        "read_back_verified": bool(result.read_back_verified),
    }


def _send_keys(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    window_selector = _window_selector(arguments)
    assert window_selector is not None
    keys = str(arguments.get("keys") or "").strip()
    if not keys:
        raise ToolError("A desktop key chord is required.")
    repeat = int(arguments.get("repeat", 1))
    result = _backend(context).send_keys(window_selector, keys, repeat=repeat)
    return {
        "window": _window_dict(result.window_before),
        "window_after": (
            _window_dict(result.window_after) if result.window_after is not None else None
        ),
        "window_closed": result.window_after is None,
        "keys": str(result.keys),
        "repeat": int(result.repeat),
        "sent_count": int(result.sent_count),
    }


def _screenshot(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    raw_output = str(arguments.get("output_path") or "").strip()
    if not raw_output:
        raise ToolError("An output_path for the desktop screenshot is required.")
    target = _resolve_allowed(context, raw_output, Action.WRITE_FILE)
    if target.suffix.lower() != ".png":
        raise ToolError("Desktop screenshot output_path must end with .png.")
    if target.exists():
        raise ToolError(f"Desktop screenshot already exists: {raw_output}")
    selector = _window_selector(arguments, required=False)
    capture = _backend(context).screenshot_png(selector)
    png = bytes(capture.png)
    if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) < 24:
        raise ToolError("Desktop screenshot was not a PNG image.")
    width, height = struct.unpack(">II", png[16:24])
    left, top, right, bottom = capture.bounds
    if width != max(0, right - left) or height != max(0, bottom - top):
        # DPI virtualization may legitimately give a scaled image; the PNG's
        # own IHDR remains the truth for the saved artifact.
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:
        handle.write(png)
    return {
        "output_path": relative_posix(context, target),
        "size_bytes": len(png),
        "sha256": hashlib.sha256(png).hexdigest(),
        "target": str(capture.target)[:1000],
        "window_handle": str(capture.window.handle) if capture.window else "",
        "width": int(width),
        "height": int(height),
        "bounds": _bounds(capture.bounds),
    }


def _window_properties() -> dict[str, Any]:
    return {
        "window_handle": {"type": "string", "maxLength": 32},
        "window_title": {"type": "string", "maxLength": 1000},
        "window_title_contains": {"type": "string", "maxLength": 1000},
        "window_class_name": {"type": "string", "maxLength": 256},
        "window_process_id": {"type": "integer", "minimum": 1},
    }


def _element_properties() -> dict[str, Any]:
    return {
        "element_runtime_id": {"type": "string", "maxLength": 500},
        "element_name": {"type": "string", "maxLength": 1000},
        "element_name_contains": {"type": "string", "maxLength": 1000},
        "element_automation_id": {"type": "string", "maxLength": 500},
        "element_class_name": {"type": "string", "maxLength": 256},
        "element_control_type": {"type": "string", "maxLength": 100},
        "element_index": {"type": "integer", "minimum": 1, "maximum": 100},
    }


def _bounds_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "left": {"type": "integer"},
            "top": {"type": "integer"},
            "right": {"type": "integer"},
            "bottom": {"type": "integer"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
        },
        "required": ["left", "top", "right", "bottom", "width", "height"],
    }


def _window_schema(*, nullable: bool = False) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": ["object", "null"] if nullable else "object",
        "properties": {
            "handle": {"type": "string"},
            "title": {"type": "string"},
            "class_name": {"type": "string"},
            "process_id": {"type": "integer"},
            "process_name": {"type": "string"},
            "is_foreground": {"type": "boolean"},
            "is_minimized": {"type": "boolean"},
            "bounds": _bounds_schema(),
        },
        "required": [
            "handle",
            "title",
            "class_name",
            "process_id",
            "process_name",
            "is_foreground",
            "is_minimized",
            "bounds",
        ],
    }
    return schema


def _element_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "name": {"type": "string"},
            "automation_id": {"type": "string"},
            "class_name": {"type": "string"},
            "control_type": {"type": "string"},
            "control_type_id": {"type": "integer"},
            "localized_control_type": {"type": "string"},
            "runtime_id": {"type": "string"},
            "native_window_handle": {"type": "string"},
            "bounds": _bounds_schema(),
            "is_enabled": {"type": "boolean"},
            "is_offscreen": {"type": "boolean"},
            "has_keyboard_focus": {"type": "boolean"},
            "is_keyboard_focusable": {"type": "boolean"},
            "is_password": {"type": "boolean"},
            "is_content_element": {"type": "boolean"},
            "is_control_element": {"type": "boolean"},
            "patterns": {"type": "array", "items": {"type": "string"}},
            "value": {"type": ["string", "null"]},
        },
        "required": [
            "index",
            "name",
            "automation_id",
            "class_name",
            "control_type",
            "control_type_id",
            "localized_control_type",
            "runtime_id",
            "native_window_handle",
            "bounds",
            "is_enabled",
            "is_offscreen",
            "has_keyboard_focus",
            "is_keyboard_focusable",
            "is_password",
            "is_content_element",
            "is_control_element",
            "patterns",
            "value",
        ],
    }


def _specs() -> list[tuple[ToolSpec, ToolHandler]]:
    desktop_permission = (Action.DESKTOP_AUTOMATION,)
    return [
        (
            ToolSpec(
                id="desktop.windows",
                name="List desktop windows",
                version="1.0.0",
                description="List visible Windows top-level windows with title, process, bounds and foreground state (read-only).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "max_windows": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 200,
                        },
                        "include_untitled": {"type": "boolean"},
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "windows": {"type": "array", "items": _window_schema()},
                        "count": {"type": "integer"},
                        "foreground_handle": {"type": "string"},
                        "foreground_title": {"type": "string"},
                    },
                    "required": [
                        "windows",
                        "count",
                        "foreground_handle",
                        "foreground_title",
                    ],
                },
                permissions=desktop_permission,
                timeout_seconds=60.0,
            ),
            _windows,
        ),
        (
            ToolSpec(
                id="desktop.window_inspect",
                name="Inspect desktop window UI",
                version="1.0.0",
                description="Inspect one selected window's Microsoft UI Automation elements, patterns and readable editor values (read-only, bounded).",
                input_schema={
                    "type": "object",
                    "properties": {
                        **_window_properties(),
                        "max_elements": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 1000,
                        },
                        "include_offscreen": {"type": "boolean"},
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "window": _window_schema(),
                        "elements": {"type": "array", "items": _element_schema()},
                        "element_count": {"type": "integer"},
                        "visited_count": {"type": "integer"},
                        "reported_count": {"type": "integer"},
                        "truncated": {"type": "boolean"},
                    },
                    "required": [
                        "window",
                        "elements",
                        "element_count",
                        "visited_count",
                        "reported_count",
                        "truncated",
                    ],
                },
                permissions=desktop_permission,
                timeout_seconds=120.0,
            ),
            _inspect,
        ),
        (
            ToolSpec(
                id="desktop.window_focus",
                name="Focus desktop window",
                version="1.0.0",
                description="Restore and foreground exactly one selected application window; requires fresh approval in orchestrated runs.",
                input_schema={"type": "object", "properties": _window_properties()},
                output_schema={
                    "type": "object",
                    "properties": {
                        "window": _window_schema(),
                        "focused": {"type": "boolean"},
                    },
                    "required": ["window", "focused"],
                },
                permissions=desktop_permission,
                timeout_seconds=60.0,
            ),
            _focus,
        ),
        (
            ToolSpec(
                id="desktop.window_click",
                name="Click desktop UI element",
                version="1.0.0",
                description="Click exactly one selected UI Automation element in one selected window, preferring Invoke/Toggle/Selection patterns; requires fresh approval in orchestrated runs.",
                input_schema={
                    "type": "object",
                    "properties": {
                        **_window_properties(),
                        **_element_properties(),
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "clicked": {"type": "boolean"},
                        "action_method": {"type": "string"},
                        "match_count": {"type": "integer"},
                        "selected_index": {"type": "integer"},
                        "window": _window_schema(),
                        "window_after": _window_schema(nullable=True),
                        "window_closed": {"type": "boolean"},
                        "element": _element_schema(),
                    },
                    "required": [
                        "clicked",
                        "action_method",
                        "match_count",
                        "selected_index",
                        "window",
                        "window_after",
                        "window_closed",
                        "element",
                    ],
                },
                permissions=desktop_permission,
                timeout_seconds=120.0,
            ),
            _click,
        ),
        (
            ToolSpec(
                id="desktop.window_type",
                name="Set desktop text element",
                version="1.0.0",
                description="Replace exactly one selected readable text element's value through UIA patterns (password elements refused) and verify by read-back; requires fresh approval in orchestrated runs.",
                input_schema={
                    "type": "object",
                    "properties": {
                        **_window_properties(),
                        **_element_properties(),
                        "text": {"type": "string", "maxLength": 4000},
                    },
                    "required": ["text"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "window": _window_schema(),
                        "element": _element_schema(),
                        "value": {"type": "string"},
                        "typed_length": {"type": "integer"},
                        "value_method": {"type": "string"},
                        "read_back_verified": {"type": "boolean"},
                    },
                    "required": [
                        "window",
                        "element",
                        "value",
                        "typed_length",
                        "value_method",
                        "read_back_verified",
                    ],
                },
                permissions=desktop_permission,
                timeout_seconds=120.0,
            ),
            _type,
        ),
        (
            ToolSpec(
                id="desktop.send_keys",
                name="Send desktop key chord",
                version="1.0.0",
                description="Send one allow-listed key chord (for example Ctrl+S or Enter) to exactly one foreground-verified window; system/window-switching chords are refused and orchestrated runs require fresh approval.",
                input_schema={
                    "type": "object",
                    "properties": {
                        **_window_properties(),
                        "keys": {"type": "string", "minLength": 1, "maxLength": 80},
                        "repeat": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["keys"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "window": _window_schema(),
                        "window_after": _window_schema(nullable=True),
                        "window_closed": {"type": "boolean"},
                        "keys": {"type": "string"},
                        "repeat": {"type": "integer"},
                        "sent_count": {"type": "integer"},
                    },
                    "required": [
                        "window",
                        "window_after",
                        "window_closed",
                        "keys",
                        "repeat",
                        "sent_count",
                    ],
                },
                permissions=desktop_permission,
                timeout_seconds=60.0,
            ),
            _send_keys,
        ),
        (
            ToolSpec(
                id="desktop.screenshot",
                name="Desktop screenshot",
                version="1.0.0",
                description="Save a PNG screenshot of the full desktop or exactly one selected window as a new workspace file (never overwrites).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "output_path": {"type": "string", "maxLength": 500},
                        **_window_properties(),
                    },
                    "required": ["output_path"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "output_path": {"type": "string"},
                        "size_bytes": {"type": "integer"},
                        "sha256": {"type": "string"},
                        "target": {"type": "string"},
                        "window_handle": {"type": "string"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                        "bounds": _bounds_schema(),
                    },
                    "required": [
                        "output_path",
                        "size_bytes",
                        "sha256",
                        "target",
                        "window_handle",
                        "width",
                        "height",
                        "bounds",
                    ],
                },
                permissions=(Action.WRITE_FILE, Action.DESKTOP_AUTOMATION),
                timeout_seconds=120.0,
            ),
            _screenshot,
        ),
    ]


def register_desktop_automation_tools(registry: ToolRegistry) -> None:
    for spec, handler in _specs():
        registry.register(spec, handler)


__all__ = ["register_desktop_automation_tools"]
