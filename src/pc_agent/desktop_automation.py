"""Windows desktop automation primitives for Phase 14.

The real backend combines two native Windows surfaces:

* Win32 top-level window enumeration/focus/screenshot APIs through
  ``ctypes``; and
* Microsoft UI Automation (UIA) through the pure-Python ``comtypes``
  package and the system ``UIAutomationCore.dll`` type library.

No Windows API is touched at import time.  Tool handlers construct the
backend only after their explicit policy gate has passed.  On non-Windows
systems every live operation fails closed with a clear message; tests inject
an in-memory backend with the same method surface.
"""

from __future__ import annotations

import ctypes
import importlib.util
import logging
import os
import struct
import sys
import threading
import time
import zlib
from ctypes import wintypes
from dataclasses import dataclass, replace
from pathlib import PureWindowsPath
from typing import Any, Protocol

from .errors import ToolError

logger = logging.getLogger(__name__)

MAX_WINDOW_SCAN = 1_000
MAX_ELEMENT_SCAN = 5_000
MAX_SCREENSHOT_PIXELS = 40_000_000

CONTROL_TYPE_NAMES = {
    50000: "Button",
    50001: "Calendar",
    50002: "CheckBox",
    50003: "ComboBox",
    50004: "Edit",
    50005: "Hyperlink",
    50006: "Image",
    50007: "ListItem",
    50008: "List",
    50009: "Menu",
    50010: "MenuBar",
    50011: "MenuItem",
    50012: "ProgressBar",
    50013: "RadioButton",
    50014: "ScrollBar",
    50015: "Slider",
    50016: "Spinner",
    50017: "StatusBar",
    50018: "Tab",
    50019: "TabItem",
    50020: "Text",
    50021: "ToolBar",
    50022: "ToolTip",
    50023: "Tree",
    50024: "TreeItem",
    50025: "Custom",
    50026: "Group",
    50027: "Thumb",
    50028: "DataGrid",
    50029: "DataItem",
    50030: "Document",
    50031: "SplitButton",
    50032: "Window",
    50033: "Pane",
    50034: "Header",
    50035: "HeaderItem",
    50036: "Table",
    50037: "TitleBar",
    50038: "Separator",
    50039: "SemanticZoom",
    50040: "AppBar",
}

_PATTERN_SPECS = {
    "Invoke": (10000, "IUIAutomationInvokePattern"),
    "Value": (10002, "IUIAutomationValuePattern"),
    "ExpandCollapse": (10005, "IUIAutomationExpandCollapsePattern"),
    "SelectionItem": (10010, "IUIAutomationSelectionItemPattern"),
    "Text": (10014, "IUIAutomationTextPattern"),
    "Toggle": (10015, "IUIAutomationTogglePattern"),
    "LegacyIAccessible": (10018, "IUIAutomationLegacyIAccessiblePattern"),
}


@dataclass(frozen=True, slots=True)
class WindowInfo:
    handle: int
    title: str
    class_name: str
    process_id: int
    process_name: str = ""
    is_foreground: bool = False
    is_minimized: bool = False
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class ElementInfo:
    index: int
    name: str
    automation_id: str
    class_name: str
    control_type_id: int
    control_type: str
    localized_control_type: str = ""
    runtime_id: str = ""
    native_window_handle: int = 0
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    is_enabled: bool = True
    is_offscreen: bool = False
    has_keyboard_focus: bool = False
    is_keyboard_focusable: bool = False
    is_password: bool = False
    is_content_element: bool = False
    is_control_element: bool = False
    patterns: tuple[str, ...] = ()
    value: str | None = None


@dataclass(frozen=True, slots=True)
class WindowSelector:
    handle: int | None = None
    title: str | None = None
    title_contains: str | None = None
    class_name: str | None = None
    process_id: int | None = None

    def __post_init__(self) -> None:
        for field in ("title", "title_contains", "class_name"):
            value = getattr(self, field)
            if value is not None:
                cleaned = str(value).strip()
                object.__setattr__(self, field, cleaned or None)
        if self.handle is not None and (
            not isinstance(self.handle, int) or isinstance(self.handle, bool) or self.handle <= 0
        ):
            raise ToolError("Desktop window handle must be a positive integer.")
        if self.process_id is not None and (
            not isinstance(self.process_id, int)
            or isinstance(self.process_id, bool)
            or self.process_id <= 0
        ):
            raise ToolError("Desktop window process_id must be a positive integer.")
        if not any(
            value not in (None, "")
            for value in (
                self.handle,
                self.title,
                self.title_contains,
                self.class_name,
                self.process_id,
            )
        ):
            raise ToolError(
                "Choose exactly one desktop window with window_handle, window_title, "
                "window_title_contains, window_class_name or window_process_id."
            )

    def matches(self, window: WindowInfo) -> bool:
        if self.handle is not None and window.handle != self.handle:
            return False
        if self.title is not None and window.title.casefold() != self.title.casefold():
            return False
        if (
            self.title_contains is not None
            and self.title_contains.casefold() not in window.title.casefold()
        ):
            return False
        if (
            self.class_name is not None
            and window.class_name.casefold() != self.class_name.casefold()
        ):
            return False
        return not (
            self.process_id is not None and window.process_id != self.process_id
        )

    def describe(self) -> str:
        parts: list[str] = []
        if self.handle is not None:
            parts.append(f"handle={self.handle}")
        if self.title is not None:
            parts.append(f"title={self.title!r}")
        if self.title_contains is not None:
            parts.append(f"title_contains={self.title_contains!r}")
        if self.class_name is not None:
            parts.append(f"class_name={self.class_name!r}")
        if self.process_id is not None:
            parts.append(f"process_id={self.process_id}")
        return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class ElementSelector:
    runtime_id: str | None = None
    name: str | None = None
    name_contains: str | None = None
    automation_id: str | None = None
    class_name: str | None = None
    control_type: str | None = None
    index: int | None = None

    def __post_init__(self) -> None:
        for field in (
            "runtime_id",
            "name",
            "name_contains",
            "automation_id",
            "class_name",
            "control_type",
        ):
            value = getattr(self, field)
            if value is not None:
                cleaned = str(value).strip()
                object.__setattr__(self, field, cleaned or None)
        if self.index is not None and (
            not isinstance(self.index, int)
            or isinstance(self.index, bool)
            or not 1 <= self.index <= 100
        ):
            raise ToolError("Desktop element_index must be between 1 and 100.")
        if not any(
            value not in (None, "")
            for value in (
                self.runtime_id,
                self.name,
                self.name_contains,
                self.automation_id,
                self.class_name,
                self.control_type,
            )
        ):
            raise ToolError(
                "Choose a desktop element with element_runtime_id, element_name, "
                "element_name_contains, element_automation_id, element_class_name "
                "or element_control_type."
            )

    def matches(self, element: ElementInfo) -> bool:
        if self.runtime_id is not None and element.runtime_id != self.runtime_id:
            return False
        if self.name is not None and element.name.casefold() != self.name.casefold():
            return False
        if (
            self.name_contains is not None
            and self.name_contains.casefold() not in element.name.casefold()
        ):
            return False
        if (
            self.automation_id is not None
            and element.automation_id.casefold() != self.automation_id.casefold()
        ):
            return False
        if (
            self.class_name is not None
            and element.class_name.casefold() != self.class_name.casefold()
        ):
            return False
        return not (
            self.control_type is not None
            and not _control_type_matches(self.control_type, element)
        )

    def describe(self) -> str:
        parts: list[str] = []
        for field in (
            "runtime_id",
            "name",
            "name_contains",
            "automation_id",
            "class_name",
            "control_type",
        ):
            value = getattr(self, field)
            if value is not None:
                parts.append(f"{field}={value!r}")
        if self.index is not None:
            parts.append(f"index={self.index}")
        return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class InspectResult:
    window: WindowInfo
    elements: tuple[ElementInfo, ...]
    visited_count: int
    reported_count: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ClickResult:
    window_before: WindowInfo
    window_after: WindowInfo | None
    element: ElementInfo
    action_method: str
    match_count: int
    selected_index: int


@dataclass(frozen=True, slots=True)
class TypeResult:
    window: WindowInfo
    element: ElementInfo
    value: str
    method: str
    read_back_verified: bool = True


@dataclass(frozen=True, slots=True)
class KeysResult:
    window_before: WindowInfo
    window_after: WindowInfo | None
    keys: str
    repeat: int
    sent_count: int


@dataclass(frozen=True, slots=True)
class ScreenshotCapture:
    png: bytes
    target: str
    window: WindowInfo | None
    bounds: tuple[int, int, int, int]


class DesktopAutomationBackend(Protocol):
    def list_windows(
        self, *, max_windows: int = 100, include_untitled: bool = False
    ) -> list[WindowInfo]: ...

    def inspect_window(
        self,
        selector: WindowSelector,
        *,
        max_elements: int = 200,
        include_offscreen: bool = False,
    ) -> InspectResult: ...

    def focus_window(self, selector: WindowSelector) -> WindowInfo: ...

    def click_element(
        self, window_selector: WindowSelector, element_selector: ElementSelector
    ) -> ClickResult: ...

    def set_element_text(
        self,
        window_selector: WindowSelector,
        element_selector: ElementSelector,
        text: str,
    ) -> TypeResult: ...

    def send_keys(
        self, selector: WindowSelector, keys: str, *, repeat: int = 1
    ) -> KeysResult: ...

    def screenshot_png(
        self, selector: WindowSelector | None = None
    ) -> ScreenshotCapture: ...


def parse_window_handle(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    cleaned = str(value).strip()
    if not cleaned:
        return None
    try:
        return int(cleaned, 0)
    except ValueError as exc:
        raise ToolError("Desktop window_handle must be a decimal or 0x-prefixed integer.") from exc


def resolve_window_from_list(
    windows: list[WindowInfo] | tuple[WindowInfo, ...], selector: WindowSelector
) -> WindowInfo:
    matches = [window for window in windows if selector.matches(window)]
    if not matches:
        raise ToolError(f"No visible desktop window matched {selector.describe()}.")
    if len(matches) > 1:
        titles = ", ".join(repr(window.title or window.class_name) for window in matches[:5])
        raise ToolError(
            f"{len(matches)} desktop windows matched {selector.describe()} ({titles}); "
            "refusing to guess."
        )
    return matches[0]


def select_element_from_list(
    elements: list[ElementInfo] | tuple[ElementInfo, ...], selector: ElementSelector
) -> tuple[ElementInfo, int, int]:
    matches = [element for element in elements if selector.matches(element)]
    if not matches:
        raise ToolError(f"No desktop UI element matched {selector.describe()}.")
    selected_index = selector.index or 1
    if len(matches) > 1 and selector.index is None:
        shown = ", ".join(
            repr(element.name or element.automation_id or element.class_name)
            for element in matches[:5]
        )
        raise ToolError(
            f"{len(matches)} desktop UI elements matched {selector.describe()} ({shown}); "
            "refusing to guess. Add a more exact selector or element_index."
        )
    if selected_index > len(matches):
        raise ToolError(
            f"Desktop element_index {selected_index} exceeds the {len(matches)} matching element(s)."
        )
    return matches[selected_index - 1], len(matches), selected_index


def _normalize_control_type(value: str) -> str:
    return "".join(ch for ch in value.casefold() if ch.isalnum())


def _control_type_matches(wanted: str, element: ElementInfo) -> bool:
    cleaned = wanted.strip()
    if cleaned.isdigit():
        return element.control_type_id == int(cleaned)
    normalized = _normalize_control_type(cleaned)
    return normalized in {
        _normalize_control_type(element.control_type),
        _normalize_control_type(element.localized_control_type),
    }


_MODIFIER_ALIASES = {
    "ctrl": "Ctrl",
    "control": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
}
_KEY_ALIASES = {
    "esc": "Escape",
    "escape": "Escape",
    "return": "Enter",
    "enter": "Enter",
    "tab": "Tab",
    "space": "Space",
    "backspace": "Backspace",
    "bksp": "Backspace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "ins": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp",
    "pgup": "PageUp",
    "pagedown": "PageDown",
    "pgdn": "PageDown",
    "up": "Up",
    "arrowup": "Up",
    "down": "Down",
    "arrowdown": "Down",
    "left": "Left",
    "arrowleft": "Left",
    "right": "Right",
    "arrowright": "Right",
}
_FORBIDDEN_CHORDS = {
    "Alt+F4",
    "Alt+Escape",
    "Alt+Tab",
    "Ctrl+Alt+Delete",
    "Ctrl+Shift+Escape",
}


def normalize_key_chord(raw: str) -> tuple[str, tuple[str, ...]]:
    """Return ``(canonical, components)`` for one bounded approved key chord."""

    cleaned = str(raw).strip()
    if not cleaned or len(cleaned) > 80:
        raise ToolError("Desktop keys must name one key chord, for example Ctrl+S or Enter.")
    if any(ch in cleaned for ch in ",;\n\r\t"):
        raise ToolError("Desktop keys accepts one chord at a time; use repeat or separate approved steps.")
    tokens = [token.strip() for token in cleaned.split("+")]
    if not tokens or any(not token for token in tokens) or len(tokens) > 4:
        raise ToolError("Desktop keys must name one key chord, for example Ctrl+S or Enter.")
    modifiers: list[str] = []
    for token in tokens[:-1]:
        canonical = _MODIFIER_ALIASES.get(token.casefold().replace(" ", ""))
        if canonical is None:
            raise ToolError(
                f"Unsupported desktop modifier {token!r}; allowed modifiers are Ctrl, Alt and Shift."
            )
        if canonical not in modifiers:
            modifiers.append(canonical)
    primary_raw = tokens[-1].strip()
    primary_key = primary_raw.casefold().replace(" ", "")
    if primary_key in _MODIFIER_ALIASES:
        raise ToolError("Desktop keys chord must end with a non-modifier key.")
    if len(primary_raw) == 1 and primary_raw.isalnum() and primary_raw.isascii():
        primary = primary_raw.upper()
    elif primary_key in _KEY_ALIASES:
        primary = _KEY_ALIASES[primary_key]
    elif primary_key.startswith("f") and primary_key[1:].isdigit() and 1 <= int(primary_key[1:]) <= 12:
        primary = f"F{int(primary_key[1:])}"
    else:
        raise ToolError(
            f"Unsupported desktop key {primary_raw!r}. Use Enter, Escape, Tab, Space, "
            "arrows, Home/End, PageUp/PageDown, F1-F12, or one letter/digit."
        )
    components = (*modifiers, primary)
    canonical = "+".join(components)
    if canonical in _FORBIDDEN_CHORDS:
        raise ToolError(
            f"{canonical} is refused because it switches/closes windows or invokes a system surface."
        )
    return canonical, components


def encode_png_bgra(width: int, height: int, bgra: bytes) -> bytes:
    """Encode raw top-down 32-bit BGRA pixels as a PNG using only ``zlib``."""

    if width <= 0 or height <= 0 or width > 10_000 or height > 10_000:
        raise ToolError("Desktop screenshot dimensions are invalid.")
    if width * height > MAX_SCREENSHOT_PIXELS:
        raise ToolError("Desktop screenshot is too large to capture safely.")
    expected = width * height * 4
    if len(bgra) != expected:
        raise ToolError("Desktop screenshot pixel buffer has an unexpected size.")
    rgba = bytearray(expected)
    rgba[0::4] = bgra[2::4]
    rgba[1::4] = bgra[1::4]
    rgba[2::4] = bgra[0::4]
    rgba[3::4] = b"\xff" * (width * height)
    stride = width * 4
    raw = b"".join(
        b"\x00" + bytes(rgba[row * stride : (row + 1) * stride]) for row in range(height)
    )

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, level=6))
        + chunk(b"IEND", b"")
    )


def probe_desktop_automation() -> dict[str, Any]:
    """Report whether the Windows UIA runtime can even be imported."""

    if sys.platform != "win32":
        return {"available": False, "detail": "Windows only"}
    if importlib.util.find_spec("comtypes") is None:
        return {
            "available": False,
            "detail": "comtypes missing (rerun pip install -e \".[dev]\")",
        }
    return {"available": True, "detail": "Microsoft UI Automation via comtypes"}


_UIA_STATE = threading.local()


class WindowsDesktopAutomationBackend:
    """Live Windows backend.  Methods are stateless apart from COM setup."""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise ToolError("Desktop automation timeout must be between 0 and 300 seconds.")
        self.timeout_seconds = float(timeout_seconds)

    # ------------------------------------------------------------------ API

    def list_windows(
        self, *, max_windows: int = 100, include_untitled: bool = False
    ) -> list[WindowInfo]:
        windows = self._enum_windows()
        if not include_untitled:
            windows = [window for window in windows if window.title.strip()]
        return windows[:max_windows]

    def inspect_window(
        self,
        selector: WindowSelector,
        *,
        max_elements: int = 200,
        include_offscreen: bool = False,
    ) -> InspectResult:
        window = self._resolve_window(selector)
        _gen, pairs, reported = self._descendants(window)
        visible = [
            (info, element)
            for info, element in pairs
            if include_offscreen or not info.is_offscreen
        ]
        chosen = visible[:max_elements]
        enriched = tuple(
            self._enrich_element(info, element) for info, element in chosen
        )
        return InspectResult(
            window=window,
            elements=enriched,
            visited_count=len(pairs),
            reported_count=reported,
            truncated=(reported > len(pairs) or len(visible) > len(chosen)),
        )

    def focus_window(self, selector: WindowSelector) -> WindowInfo:
        window = self._resolve_window(selector)
        return self._focus_window_info(window)

    def click_element(
        self, window_selector: WindowSelector, element_selector: ElementSelector
    ) -> ClickResult:
        window = self._resolve_window(window_selector)
        self._refuse_own_window(window, "click in")
        gen, element, info, match_count, selected_index = self._find_element(
            window, element_selector
        )
        if not info.is_enabled:
            raise ToolError("The selected desktop UI element is disabled.")
        if info.is_offscreen:
            raise ToolError("The selected desktop UI element is off-screen; inspect/focus the window first.")
        self._focus_window_info(window)
        method = self._dispatch_click(gen, element, info)
        time.sleep(0.25)
        after = self._window_by_handle(window.handle)
        return ClickResult(
            window_before=window,
            window_after=after,
            element=info,
            action_method=method,
            match_count=match_count,
            selected_index=selected_index,
        )

    def set_element_text(
        self,
        window_selector: WindowSelector,
        element_selector: ElementSelector,
        text: str,
    ) -> TypeResult:
        if len(text) > 4000:
            raise ToolError("Desktop text is limited to 4000 characters.")
        window = self._resolve_window(window_selector)
        self._refuse_own_window(window, "type into")
        gen, element, info, _matches, _index = self._find_element(
            window, element_selector
        )
        info = self._enrich_element(info, element, gen=gen)
        if info.is_password:
            raise ToolError("Typing into a password field is refused by design.")
        if not info.is_enabled:
            raise ToolError("The selected desktop text element is disabled.")
        if info.is_offscreen:
            raise ToolError("The selected desktop text element is off-screen; inspect/focus the window first.")

        readable_before, read_method = self._read_element_value(gen, element)
        if readable_before is None and read_method is None:
            raise ToolError(
                "The selected element exposes no readable Value, LegacyIAccessible or Text "
                "pattern, so the agent refuses to type without read-back verification."
            )

        self._focus_window_info(window)
        self._try_call(element, "SetFocus")
        method: str | None = None
        errors: list[str] = []

        value_pattern = self._pattern(gen, element, "Value")
        if value_pattern is not None:
            try:
                if bool(self._prop(value_pattern, "CurrentIsReadOnly", default=False)):
                    raise ToolError("The selected desktop text element is read-only.")
                self._pattern_call(value_pattern, "SetValue", text)
                method = "UIA ValuePattern.SetValue"
            except Exception as exc:  # noqa: BLE001 - try another supported UIA contract.
                errors.append(f"ValuePattern: {exc}")

        if method is None:
            legacy = self._pattern(gen, element, "LegacyIAccessible")
            if legacy is not None:
                try:
                    self._pattern_call(legacy, "SetValue", text)
                    method = "UIA LegacyIAccessible.SetValue"
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"LegacyIAccessible: {exc}")

        if method is None and read_method == "UIA TextPattern.GetText":
            self._send_key_components(("Ctrl", "A"))
            if text:
                self._send_unicode_text(text)
            else:
                self._send_key_components(("Backspace",))
            method = "Focused keyboard input with UIA TextPattern read-back"

        if method is None:
            detail = "; ".join(errors) or "no writable pattern found"
            raise ToolError(f"Could not set the selected desktop text element ({detail}).")

        time.sleep(0.15)
        actual, _method = self._read_element_value(gen, element)
        if actual != text:
            raise ToolError(
                "Desktop text read-back did not match the approved text; the result is unconfirmed."
            )
        refreshed = replace(self._element_info(gen, element, info.index), value=actual)
        return TypeResult(window=window, element=refreshed, value=actual, method=method)

    def send_keys(
        self, selector: WindowSelector, keys: str, *, repeat: int = 1
    ) -> KeysResult:
        if not 1 <= repeat <= 10:
            raise ToolError("Desktop key repeat must be between 1 and 10.")
        canonical, components = normalize_key_chord(keys)
        window = self._resolve_window(selector)
        self._refuse_own_window(window, "send keys to")
        self._focus_window_info(window)
        user32 = self._user32()
        sent = 0
        for _ in range(repeat):
            foreground = int(user32.GetForegroundWindow() or 0)
            if foreground != window.handle:
                raise ToolError(
                    "The selected desktop window lost foreground focus before all approved keys were sent."
                )
            self._send_key_components(components)
            sent += 1
            time.sleep(0.05)
        return KeysResult(
            window_before=window,
            window_after=self._window_by_handle(window.handle),
            keys=canonical,
            repeat=repeat,
            sent_count=sent,
        )

    def screenshot_png(
        self, selector: WindowSelector | None = None
    ) -> ScreenshotCapture:
        self._require_windows()
        if selector is None:
            bounds = self._virtual_screen_bounds()
            png = self._capture_png(bounds=bounds, hwnd=None)
            return ScreenshotCapture(
                png=png, target="Full desktop", window=None, bounds=bounds
            )
        window = self._resolve_window(selector)
        if window.is_minimized:
            raise ToolError("The selected desktop window is minimized; focus/restore it before a screenshot.")
        png = self._capture_png(bounds=window.bounds, hwnd=window.handle)
        return ScreenshotCapture(
            png=png,
            target=window.title or window.class_name or f"Window {window.handle}",
            window=window,
            bounds=window.bounds,
        )

    # -------------------------------------------------------------- Win32

    def _require_windows(self) -> None:
        if sys.platform != "win32":
            raise ToolError("Windows desktop automation requires Windows.")

    def _user32(self) -> Any:
        self._require_windows()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.EnumWindows.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        return user32

    def _kernel32(self) -> Any:
        self._require_windows()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        return kernel32

    def _process_name(self, process_id: int) -> str:
        if process_id <= 0:
            return ""
        kernel32 = self._kernel32()
        handle = kernel32.OpenProcess(0x1000, False, process_id)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return ""
            return PureWindowsPath(buffer.value).name
        finally:
            kernel32.CloseHandle(handle)

    def _enum_windows(self) -> list[WindowInfo]:
        user32 = self._user32()
        foreground = int(user32.GetForegroundWindow() or 0)
        results: list[WindowInfo] = []
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd: int, _lparam: int) -> bool:
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = int(user32.GetWindowTextLengthW(hwnd) or 0)
                title = ""
                if length:
                    buffer = ctypes.create_unicode_buffer(min(length, 4096) + 1)
                    user32.GetWindowTextW(hwnd, buffer, len(buffer))
                    title = buffer.value
                class_buffer = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                rect = wintypes.RECT()
                bounds = (0, 0, 0, 0)
                if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    bounds = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
                results.append(
                    WindowInfo(
                        handle=int(hwnd),
                        title=title,
                        class_name=class_buffer.value,
                        process_id=int(pid.value),
                        process_name=self._process_name(int(pid.value)),
                        is_foreground=int(hwnd) == foreground,
                        is_minimized=bool(user32.IsIconic(hwnd)),
                        bounds=bounds,
                    )
                )
                if len(results) >= MAX_WINDOW_SCAN:
                    return False
            except Exception:  # noqa: BLE001 - windows can disappear during enumeration.
                return True
            return True

        proc = enum_proc(callback)
        user32.EnumWindows(proc, 0)
        return results

    def _resolve_window(self, selector: WindowSelector) -> WindowInfo:
        return resolve_window_from_list(self._enum_windows(), selector)

    def _window_by_handle(self, handle: int) -> WindowInfo | None:
        for window in self._enum_windows():
            if window.handle == handle:
                return window
        return None

    @staticmethod
    def _refuse_own_window(window: WindowInfo, verb: str) -> None:
        if window.process_id == os.getpid():
            raise ToolError(
                f"Refusing to {verb} the agent's own process window; choose another application window."
            )

    def _focus_window_info(self, window: WindowInfo) -> WindowInfo:
        self._refuse_own_window(window, "focus")
        user32 = self._user32()
        handle = window.handle
        if not user32.IsWindow(handle):
            raise ToolError("The selected desktop window no longer exists.")
        user32.ShowWindow(handle, 9 if window.is_minimized else 5)  # RESTORE / SHOW
        user32.BringWindowToTop(handle)
        user32.SetForegroundWindow(handle)
        deadline = time.monotonic() + min(3.0, self.timeout_seconds)
        while time.monotonic() < deadline:
            if int(user32.GetForegroundWindow() or 0) == handle:
                fresh = self._window_by_handle(handle)
                if fresh is not None:
                    return fresh
            time.sleep(0.05)
        # A still-supported activation helper for cases where Windows'
        # foreground lock rejects the first request from a background tool.
        switch = getattr(user32, "SwitchToThisWindow", None)
        if switch is not None:
            try:
                switch(handle, True)
                time.sleep(0.1)
            except Exception:
                logger.debug("SwitchToThisWindow fallback failed", exc_info=True)
            if int(user32.GetForegroundWindow() or 0) == handle:
                fresh = self._window_by_handle(handle)
                if fresh is not None:
                    return fresh
        raise ToolError(
            "Windows did not confirm that the selected window became the foreground window."
        )

    def _virtual_screen_bounds(self) -> tuple[int, int, int, int]:
        user32 = self._user32()
        left = int(user32.GetSystemMetrics(76))
        top = int(user32.GetSystemMetrics(77))
        width = int(user32.GetSystemMetrics(78))
        height = int(user32.GetSystemMetrics(79))
        if width <= 0 or height <= 0:
            width = int(user32.GetSystemMetrics(0))
            height = int(user32.GetSystemMetrics(1))
            left = top = 0
        return left, top, left + width, top + height

    # ---------------------------------------------------------------- UIA

    def _uia(self) -> tuple[Any, Any]:
        self._require_windows()
        gen = getattr(_UIA_STATE, "gen", None)
        automation = getattr(_UIA_STATE, "automation", None)
        if gen is not None and automation is not None:
            return gen, automation
        try:
            import comtypes  # type: ignore[import-not-found]
            import comtypes.client  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ToolError(
                "Windows UI Automation support is unavailable because comtypes is not "
                "installed. Re-run: python -m pip install -e \".[dev]\""
            ) from exc
        try:
            comtypes.CoInitialize()
            gen = comtypes.client.GetModule("UIAutomationCore.dll")
            automation = comtypes.CoCreateInstance(
                gen.CUIAutomation._reg_clsid_,
                interface=gen.IUIAutomation,
                clsctx=comtypes.CLSCTX_INPROC_SERVER,
            )
        except Exception as exc:
            raise ToolError(
                f"Could not initialize Microsoft UI Automation: {type(exc).__name__}: {exc}"
            ) from exc
        _UIA_STATE.gen = gen
        _UIA_STATE.automation = automation
        return gen, automation

    @staticmethod
    def _prop(obj: Any, *names: str, default: Any = None) -> Any:
        for name in names:
            if not hasattr(obj, name):
                continue
            try:
                value = getattr(obj, name)
                return value() if callable(value) and name.startswith("Get") else value
            except Exception:
                logger.debug("Ignoring unavailable UIA property %s", name, exc_info=True)
                continue
        return default

    @staticmethod
    def _try_call(obj: Any, method_name: str, *args: Any) -> Any:
        method = getattr(obj, method_name, None)
        if not callable(method):
            return None
        try:
            return method(*args)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _pattern_call(pattern: Any, method_name: str, *args: Any) -> None:
        method = getattr(pattern, method_name)
        result = method(*args)
        if isinstance(result, int) and result != 0:
            raise ToolError(f"UI Automation {method_name} returned HRESULT 0x{result & 0xFFFFFFFF:08X}.")

    def _pattern(self, gen: Any, element: Any, name: str) -> Any | None:
        pattern_id, interface_name = _PATTERN_SPECS[name]
        try:
            raw = element.GetCurrentPattern(pattern_id)
            if raw is None:
                return None
            interface = getattr(gen, interface_name)
            return raw.QueryInterface(interface)
        except Exception:  # noqa: BLE001 - pattern absence is represented in several ways.
            return None

    def _pattern_names(self, gen: Any, element: Any) -> tuple[str, ...]:
        return tuple(
            name for name in _PATTERN_SPECS if self._pattern(gen, element, name) is not None
        )

    @staticmethod
    def _bounds(value: Any) -> tuple[int, int, int, int]:
        if value is None:
            return (0, 0, 0, 0)
        try:
            return (
                int(value.left),
                int(value.top),
                int(value.right),
                int(value.bottom),
            )
        except Exception:
            logger.debug("UIA bounds object has no rectangular fields", exc_info=True)
        try:
            parts = tuple(int(item) for item in value)
            return parts if len(parts) == 4 else (0, 0, 0, 0)
        except Exception:  # noqa: BLE001
            return (0, 0, 0, 0)

    @staticmethod
    def _runtime_id(element: Any) -> str:
        try:
            raw = element.GetRuntimeId()
        except Exception:  # noqa: BLE001
            return ""
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        try:
            return ",".join(str(int(item)) for item in raw)
        except Exception:
            logger.debug("UIA runtime id was not directly iterable", exc_info=True)
        try:
            return ",".join(str(int(raw[index])) for index in range(len(raw)))
        except Exception:  # noqa: BLE001
            return ""

    def _read_element_value(self, gen: Any, element: Any) -> tuple[str | None, str | None]:
        value_pattern = self._pattern(gen, element, "Value")
        if value_pattern is not None:
            value = self._prop(value_pattern, "CurrentValue", default=None)
            if isinstance(value, str):
                return value, "UIA ValuePattern.CurrentValue"
        legacy = self._pattern(gen, element, "LegacyIAccessible")
        if legacy is not None:
            value = self._prop(legacy, "CurrentValue", default=None)
            if isinstance(value, str):
                return value, "UIA LegacyIAccessible.CurrentValue"
        text_pattern = self._pattern(gen, element, "Text")
        if text_pattern is not None:
            document_range = self._prop(text_pattern, "DocumentRange", default=None)
            if document_range is not None:
                value = self._try_call(document_range, "GetText", -1)
                if isinstance(value, str):
                    return value, "UIA TextPattern.GetText"
        return None, None

    def _element_info(
        self,
        gen: Any,
        element: Any,
        index: int,
        *,
        include_patterns: bool = False,
        include_value: bool = False,
    ) -> ElementInfo:
        control_type_id = int(self._prop(element, "CurrentControlType", default=0) or 0)
        value: str | None = None
        if include_value and control_type_id in {50003, 50004, 50030}:
            value, _method = self._read_element_value(gen, element)
            if value is not None and len(value) > 10_000:
                value = value[:10_000]
        return ElementInfo(
            index=index,
            name=str(self._prop(element, "CurrentName", default="") or ""),
            automation_id=str(self._prop(element, "CurrentAutomationId", default="") or ""),
            class_name=str(self._prop(element, "CurrentClassName", default="") or ""),
            control_type_id=control_type_id,
            control_type=CONTROL_TYPE_NAMES.get(control_type_id, f"ControlType{control_type_id}"),
            localized_control_type=str(
                self._prop(element, "CurrentLocalizedControlType", default="") or ""
            ),
            runtime_id=self._runtime_id(element),
            native_window_handle=int(
                self._prop(element, "CurrentNativeWindowHandle", default=0) or 0
            ),
            bounds=self._bounds(self._prop(element, "CurrentBoundingRectangle", default=None)),
            is_enabled=bool(self._prop(element, "CurrentIsEnabled", default=True)),
            is_offscreen=bool(self._prop(element, "CurrentIsOffscreen", default=False)),
            has_keyboard_focus=bool(
                self._prop(element, "CurrentHasKeyboardFocus", default=False)
            ),
            is_keyboard_focusable=bool(
                self._prop(element, "CurrentIsKeyboardFocusable", default=False)
            ),
            is_password=bool(self._prop(element, "CurrentIsPassword", default=False)),
            is_content_element=bool(
                self._prop(element, "CurrentIsContentElement", default=False)
            ),
            is_control_element=bool(
                self._prop(element, "CurrentIsControlElement", default=False)
            ),
            patterns=self._pattern_names(gen, element) if include_patterns else (),
            value=value,
        )

    def _enrich_element(
        self, info: ElementInfo, element: Any, *, gen: Any | None = None
    ) -> ElementInfo:
        actual_gen = gen or self._uia()[0]
        return self._element_info(
            actual_gen, element, info.index, include_patterns=True, include_value=True
        )

    def _descendants(
        self, window: WindowInfo
    ) -> tuple[Any, list[tuple[ElementInfo, Any]], int]:
        gen, automation = self._uia()
        try:
            root = automation.ElementFromHandle(window.handle)
        except Exception as exc:
            raise ToolError(f"UI Automation could not attach to the selected window: {exc}") from exc
        if root is None:
            raise ToolError("UI Automation could not attach to the selected window.")
        condition = self._prop(automation, "TrueCondition", default=None)
        if condition is None:
            condition = self._try_call(automation, "CreateTrueCondition")
        if condition is None:
            raise ToolError("UI Automation TrueCondition is unavailable.")
        scope = int(getattr(gen, "TreeScope_Descendants", 4))
        try:
            array = root.FindAll(scope, condition)
        except Exception as exc:
            raise ToolError(f"UI Automation element scan failed: {exc}") from exc
        if array is None:
            return gen, [], 0
        reported = int(self._prop(array, "Length", default=0) or 0)
        pairs: list[tuple[ElementInfo, Any]] = []
        for index in range(min(reported, MAX_ELEMENT_SCAN)):
            try:
                element = array.GetElement(index)
            except Exception:
                logger.debug("UIA element vanished during scan", exc_info=True)
                continue
            if element is None:
                continue
            pairs.append((self._element_info(gen, element, index), element))
        return gen, pairs, reported

    def _find_element(
        self, window: WindowInfo, selector: ElementSelector
    ) -> tuple[Any, Any, ElementInfo, int, int]:
        gen, pairs, reported = self._descendants(window)
        infos = [info for info, _element in pairs]
        info, match_count, selected_index = select_element_from_list(infos, selector)
        # select_element_from_list returns one of the exact info objects above.
        position = infos.index(info)
        if reported > len(pairs) and match_count == 0:  # defensive; select raises first.
            raise ToolError("The desktop UI element scan reached its safety limit.")
        return gen, pairs[position][1], info, match_count, selected_index

    def _dispatch_click(self, gen: Any, element: Any, info: ElementInfo) -> str:
        invoke = self._pattern(gen, element, "Invoke")
        if invoke is not None:
            self._pattern_call(invoke, "Invoke")
            return "UIA InvokePattern.Invoke"
        toggle = self._pattern(gen, element, "Toggle")
        if toggle is not None:
            self._pattern_call(toggle, "Toggle")
            return "UIA TogglePattern.Toggle"
        selection = self._pattern(gen, element, "SelectionItem")
        if selection is not None:
            self._pattern_call(selection, "Select")
            return "UIA SelectionItemPattern.Select"
        expand = self._pattern(gen, element, "ExpandCollapse")
        if expand is not None:
            state = int(self._prop(expand, "CurrentExpandCollapseState", default=0) or 0)
            if state == 1:
                self._pattern_call(expand, "Collapse")
                return "UIA ExpandCollapsePattern.Collapse"
            self._pattern_call(expand, "Expand")
            return "UIA ExpandCollapsePattern.Expand"
        legacy = self._pattern(gen, element, "LegacyIAccessible")
        if legacy is not None:
            self._pattern_call(legacy, "DoDefaultAction")
            return "UIA LegacyIAccessible.DoDefaultAction"
        self._physical_click(element, info)
        return "Physical left click at the UIA clickable point"

    def _physical_click(self, element: Any, info: ElementInfo) -> None:
        point: tuple[int, int] | None = None
        raw = self._try_call(element, "GetClickablePoint")
        if isinstance(raw, tuple) and raw:
            candidate = raw[0]
            try:
                point = (int(candidate.x), int(candidate.y))  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                point = None
        if point is None:
            left, top, right, bottom = info.bounds
            if right <= left or bottom <= top:
                raise ToolError("The selected element has no clickable point or usable bounds.")
            point = ((left + right) // 2, (top + bottom) // 2)
        user32 = self._user32()
        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.SetCursorPos.restype = wintypes.BOOL
        user32.mouse_event.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_size_t,
        ]
        user32.mouse_event.restype = None
        if not user32.SetCursorPos(point[0], point[1]):
            raise ToolError("Windows rejected moving the cursor to the selected element.")
        time.sleep(0.05)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
        time.sleep(0.03)
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP

    # -------------------------------------------------------------- Input

    @staticmethod
    def _input_structures() -> tuple[Any, Any, Any]:
        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [
                ("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD),
            ]

        class INPUT_UNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]

        return INPUT, KEYBDINPUT, INPUT_UNION

    def _send_inputs(self, inputs: list[Any]) -> None:
        if not inputs:
            return
        user32 = self._user32()
        input_type = type(inputs[0])
        array_type = input_type * len(inputs)
        array = array_type(*inputs)
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(input_type), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
        sent = int(user32.SendInput(len(inputs), array, ctypes.sizeof(input_type)) or 0)
        if sent != len(inputs):
            error = ctypes.get_last_error()
            raise ToolError(
                f"Windows accepted {sent} of {len(inputs)} keyboard input event(s)"
                + (f" (Win32 error {error})." if error else ".")
            )

    @staticmethod
    def _vk_code(key: str) -> int:
        named = {
            "Ctrl": 0x11,
            "Alt": 0x12,
            "Shift": 0x10,
            "Backspace": 0x08,
            "Tab": 0x09,
            "Enter": 0x0D,
            "Escape": 0x1B,
            "Space": 0x20,
            "PageUp": 0x21,
            "PageDown": 0x22,
            "End": 0x23,
            "Home": 0x24,
            "Left": 0x25,
            "Up": 0x26,
            "Right": 0x27,
            "Down": 0x28,
            "Insert": 0x2D,
            "Delete": 0x2E,
        }
        if key in named:
            return named[key]
        if len(key) == 1:
            return ord(key.upper())
        if key.startswith("F") and key[1:].isdigit():
            return 0x70 + int(key[1:]) - 1
        raise ToolError(f"Unsupported desktop key {key!r}.")

    def _keyboard_event(self, *, vk: int = 0, scan: int = 0, flags: int = 0) -> Any:
        input_cls, keybd_cls, union_cls = self._input_structures()
        event = input_cls()
        event.type = 1  # INPUT_KEYBOARD
        event.union = union_cls(
            ki=keybd_cls(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
        )
        return event

    def _send_key_components(self, components: tuple[str, ...]) -> None:
        events = [
            self._keyboard_event(vk=self._vk_code(key)) for key in components
        ] + [
            self._keyboard_event(vk=self._vk_code(key), flags=0x0002)
            for key in reversed(components)
        ]
        self._send_inputs(events)

    def _send_unicode_text(self, text: str) -> None:
        events: list[Any] = []
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        for char in normalized:
            if char == "\n":
                events.append(self._keyboard_event(vk=0x0D))
                events.append(self._keyboard_event(vk=0x0D, flags=0x0002))
                continue
            if char == "\t":
                events.append(self._keyboard_event(vk=0x09))
                events.append(self._keyboard_event(vk=0x09, flags=0x0002))
                continue
            encoded = char.encode("utf-16-le", errors="surrogatepass")
            units = struct.unpack(f"<{len(encoded) // 2}H", encoded)
            for unit in units:
                events.append(self._keyboard_event(scan=int(unit), flags=0x0004))
                events.append(self._keyboard_event(scan=int(unit), flags=0x0004 | 0x0002))
        for start in range(0, len(events), 100):
            self._send_inputs(events[start : start + 100])

    # ---------------------------------------------------------- Screenshot

    def _capture_png(
        self, *, bounds: tuple[int, int, int, int], hwnd: int | None
    ) -> bytes:
        self._require_windows()
        left, top, right, bottom = bounds
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            raise ToolError("The selected desktop capture area is empty.")
        if width * height > MAX_SCREENSHOT_PIXELS:
            raise ToolError("The selected desktop capture area is too large.")
        user32 = self._user32()
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        screen_dc = user32.GetDC(None)
        if not screen_dc:
            raise ToolError("Windows did not provide a screen device context.")
        memory_dc = None
        bitmap = None
        old_bitmap = None
        try:
            memory_dc = gdi32.CreateCompatibleDC(screen_dc)
            bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
            if not memory_dc or not bitmap:
                raise ToolError("Windows could not allocate a screenshot bitmap.")
            old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
            captured = False
            if hwnd is not None and hasattr(user32, "PrintWindow"):
                try:
                    captured = bool(user32.PrintWindow(hwnd, memory_dc, 2))
                except Exception:  # noqa: BLE001
                    captured = False
            if not captured:
                captured = bool(
                    gdi32.BitBlt(
                        memory_dc,
                        0,
                        0,
                        width,
                        height,
                        screen_dc,
                        left,
                        top,
                        0x00CC0020 | 0x40000000,  # SRCCOPY | CAPTUREBLT
                    )
                )
            if not captured:
                raise ToolError("Windows could not capture the selected desktop area.")

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", wintypes.DWORD),
                    ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD),
                ]

            class RGBQUAD(ctypes.Structure):
                _fields_ = [
                    ("rgbBlue", ctypes.c_ubyte),
                    ("rgbGreen", ctypes.c_ubyte),
                    ("rgbRed", ctypes.c_ubyte),
                    ("rgbReserved", ctypes.c_ubyte),
                ]

            class BITMAPINFO(ctypes.Structure):
                _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]

            info = BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height  # top-down
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = 0  # BI_RGB
            buffer = (ctypes.c_ubyte * (width * height * 4))()
            lines = int(
                gdi32.GetDIBits(
                    memory_dc,
                    bitmap,
                    0,
                    height,
                    buffer,
                    ctypes.byref(info),
                    0,
                )
                or 0
            )
            if lines != height:
                raise ToolError("Windows returned an incomplete desktop screenshot.")
            return encode_png_bgra(width, height, bytes(buffer))
        finally:
            if memory_dc and old_bitmap:
                gdi32.SelectObject(memory_dc, old_bitmap)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if memory_dc:
                gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(None, screen_dc)


__all__ = [
    "ClickResult",
    "DesktopAutomationBackend",
    "ElementInfo",
    "ElementSelector",
    "InspectResult",
    "KeysResult",
    "ScreenshotCapture",
    "TypeResult",
    "WindowInfo",
    "WindowSelector",
    "WindowsDesktopAutomationBackend",
    "encode_png_bgra",
    "normalize_key_chord",
    "parse_window_handle",
    "probe_desktop_automation",
    "resolve_window_from_list",
    "select_element_from_list",
]
