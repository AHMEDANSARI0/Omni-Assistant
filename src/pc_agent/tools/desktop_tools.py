"""Small, explicit desktop utilities.

Phase 4 intentionally covers read-only system information and opening a
workspace document/folder or an HTTP(S) URL in the user's default app. It does
not provide shell execution, clicking/typing, screenshots or general GUI
automation.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import __version__
from ..errors import ToolError
from ..policies.policy import Action, ActionRequest
from ..web import validate_url
from .registry import ToolContext, ToolHandler, ToolRegistry, ToolSpec
from .workspace_tools import _resolve_allowed, relative_posix

_BLOCKED_OPEN_EXTENSIONS = {
    ".bat",
    ".cmd",
    ".com",
    ".cpl",
    ".dll",
    ".exe",
    ".jar",
    ".js",
    ".jse",
    ".lnk",
    ".msi",
    ".msp",
    ".ps1",
    ".scr",
    ".sh",
    ".sys",
    ".vb",
    ".vbs",
    ".wsf",
    ".wsh",
}


def desktop_system_info(workspace_dir: Path) -> dict[str, Any]:
    workspace = workspace_dir.expanduser().resolve()
    try:
        usage = shutil.disk_usage(workspace)
        disk = {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free}
    except OSError:
        disk = {"total_bytes": None, "used_bytes": None, "free_bytes": None}
    memory_total, memory_available = _memory_info()
    return {
        "agent_version": __version__,
        "operating_system": platform.system() or sys.platform,
        "os_release": platform.release(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor() or platform.machine(),
        "python_version": platform.python_version(),
        "cpu_count": os.cpu_count() or 1,
        "memory_total_bytes": memory_total,
        "memory_available_bytes": memory_available,
        "workspace": str(workspace),
        "disk_total_bytes": disk["total_bytes"],
        "disk_used_bytes": disk["used_bytes"],
        "disk_free_bytes": disk["free_bytes"],
    }


def _memory_info() -> tuple[int | None, int | None]:
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return int(status.ullTotalPhys), int(status.ullAvailPhys)
        except (AttributeError, OSError):
            return None, None
        return None, None

    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        values: dict[str, int] = {}
        try:
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                if ":" not in line:
                    continue
                key, raw = line.split(":", 1)
                parts = raw.strip().split()
                if parts and parts[0].isdigit():
                    values[key] = int(parts[0]) * 1024
        except OSError:
            return None, None
        return values.get("MemTotal"), values.get("MemAvailable")
    return None, None


def _default_open(target: str) -> None:
    if target.lower().startswith(("http://", "https://")):
        if not webbrowser.open(target, new=2):
            raise ToolError("The default browser could not be opened.")
        return
    if sys.platform.startswith("win"):
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            raise ToolError("Windows desktop open is unavailable in this Python build.")
        startfile(target)  # Opening with the registered app is the requested action.
        return
    command = ["/usr/bin/open", target] if sys.platform == "darwin" else ["xdg-open", target]
    try:
        subprocess.run(command, check=True, timeout=15)  # Fixed argv, no shell.
    except FileNotFoundError as exc:
        raise ToolError(f"Desktop opener was not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise ToolError("The desktop opener reported a failure.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError("The desktop opener timed out.") from exc


def _opener(context: ToolContext) -> Callable[[str], None]:
    return context.desktop_opener or _default_open


def _require_desktop_allowed(
    context: ToolContext,
    *,
    target_path: Path | None = None,
    target_url: str | None = None,
) -> None:
    decision = context.policy_engine.evaluate(
        ActionRequest(
            Action.DESKTOP_OPEN,
            target_path=target_path,
            target_url=target_url,
            description="Open one exact target in the user's default desktop app.",
        )
    )
    if not decision.allowed:
        raise ToolError(
            f"Policy {decision.outcome.value}: {decision.reason} "
            "Use pc-agent desktop open/open-url with the exact target."
        )


def _system_info(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    _ = arguments
    return desktop_system_info(context.workspace_dir)


def _open_path(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    raw_path = str(arguments["path"])
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = context.workspace_dir / candidate
    preliminary = candidate.resolve()
    action = Action.LIST_FILES if preliminary.is_dir() else Action.READ_FILE
    target = _resolve_allowed(context, raw_path, action)
    _require_desktop_allowed(context, target_path=target)
    if not target.exists():
        raise ToolError(f"Desktop target not found: {relative_posix(context, target)}")
    if target.is_file() and target.suffix.lower() in _BLOCKED_OPEN_EXTENSIONS:
        raise ToolError(
            "Executable, installer, script and shortcut files cannot be opened by this tool."
        )
    if not target.is_file() and not target.is_dir():
        raise ToolError("Desktop target must be a regular file or folder.")
    _opener(context)(str(target))
    return {
        "target": relative_posix(context, target),
        "kind": "folder" if target.is_dir() else "file",
        "opened": True,
    }


def _open_url(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    validated = validate_url(str(arguments["url"]), resolve=False)
    _require_desktop_allowed(context, target_url=validated.url)
    _opener(context)(validated.url)
    return {"target": validated.url, "kind": "url", "opened": True}


def _specs() -> list[tuple[ToolSpec, ToolHandler]]:
    string_target = {"type": "string", "minLength": 1}
    open_output = {
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "kind": {"type": "string"},
            "opened": {"type": "boolean"},
        },
        "required": ["target", "kind", "opened"],
    }
    return [
        (
            ToolSpec(
                id="desktop.system_info",
                name="Desktop system information",
                version="1.0.0",
                description="Report local OS, CPU, memory and workspace-disk information without running a shell.",
                input_schema={"type": "object", "properties": {}},
                output_schema={
                    "type": "object",
                    "properties": {
                        "agent_version": {"type": "string"},
                        "operating_system": {"type": "string"},
                        "os_release": {"type": "string"},
                        "os_version": {"type": "string"},
                        "machine": {"type": "string"},
                        "processor": {"type": "string"},
                        "python_version": {"type": "string"},
                        "cpu_count": {"type": "integer"},
                        "memory_total_bytes": {"type": ["integer", "null"]},
                        "memory_available_bytes": {"type": ["integer", "null"]},
                        "workspace": {"type": "string"},
                        "disk_total_bytes": {"type": ["integer", "null"]},
                        "disk_used_bytes": {"type": ["integer", "null"]},
                        "disk_free_bytes": {"type": ["integer", "null"]},
                    },
                    "required": [
                        "agent_version",
                        "operating_system",
                        "os_release",
                        "os_version",
                        "machine",
                        "processor",
                        "python_version",
                        "cpu_count",
                        "memory_total_bytes",
                        "memory_available_bytes",
                        "workspace",
                        "disk_total_bytes",
                        "disk_used_bytes",
                        "disk_free_bytes",
                    ],
                },
                permissions=(),
                timeout_seconds=15.0,
            ),
            _system_info,
        ),
        (
            ToolSpec(
                id="desktop.open_path",
                name="Open workspace item",
                version="1.0.0",
                description="Open one explicitly approved workspace document or folder in the default desktop app; executable/script/installer files are blocked.",
                input_schema={
                    "type": "object",
                    "properties": {"path": string_target},
                    "required": ["path"],
                },
                output_schema=open_output,
                permissions=(Action.READ_FILE, Action.LIST_FILES, Action.DESKTOP_OPEN),
                timeout_seconds=20.0,
            ),
            _open_path,
        ),
        (
            ToolSpec(
                id="desktop.open_url",
                name="Open URL in browser",
                version="1.0.0",
                description="Open one explicitly approved HTTP(S) URL in the user's default browser without shell execution.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "minLength": 1, "maxLength": 4096}
                    },
                    "required": ["url"],
                },
                output_schema=open_output,
                permissions=(Action.DESKTOP_OPEN,),
                timeout_seconds=20.0,
                network_required=True,
            ),
            _open_url,
        ),
    ]


def register_desktop_tools(registry: ToolRegistry) -> None:
    for spec, handler in _specs():
        registry.register(spec, handler)
