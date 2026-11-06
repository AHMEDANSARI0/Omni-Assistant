"""Isolated worker process for Safety Kernel tool execution.

Protocol: newline-delimited JSON on stdin/stdout. The worker prints one
``{"ready": true}`` line, then answers each request with either
``{"id": ..., "result": {...}}`` or ``{"id": ..., "fatal": "..."}``.
Nothing else may be written to stdout; diagnostics go to stderr.

The worker rebuilds the runtime registry (built-ins plus approved
dynamic tools) from the same database the parent uses and reconstructs
each :class:`ToolContext` from the parent's primitive snapshot, so tool
policy (workspace, approved read roots, gates and limits) is identical
to in-process execution while a hung or crashing tool stays contained
in this process. Run with ``python -m pc_agent.safety_worker``; it is
started and stopped by :class:`pc_agent.safety.SafetyKernel` only.
"""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .logging_config import redact_text
from .storage.db import Database
from .tooling.service import build_runtime_registry
from .tools.registry import ToolContext, ToolRegistry

_REGISTRIES: dict[tuple[str, str], ToolRegistry] = {}


def _registry_for(database_path: str, data_dir: str) -> ToolRegistry:
    key = (database_path, data_dir)
    registry = _REGISTRIES.get(key)
    if registry is None:
        registry = build_runtime_registry(
            Database(Path(database_path)), data_dir=Path(data_dir)
        )
        _REGISTRIES[key] = registry
    return registry


def _context_from(snapshot: Mapping[str, Any]) -> ToolContext:
    roots = tuple(Path(str(item)) for item in snapshot.get("approved_read_roots") or ())
    return ToolContext.for_workspace(
        Path(str(snapshot["workspace_dir"])),
        approved_read_roots=roots,
        network_access_allowed=bool(snapshot.get("network_access_allowed")),
        allow_private_network=bool(snapshot.get("allow_private_network")),
        desktop_open_allowed=bool(snapshot.get("desktop_open_allowed")),
        web_timeout_seconds=float(snapshot.get("web_timeout_seconds") or 15.0),
        web_max_bytes=int(snapshot.get("web_max_bytes") or 2_000_000),
        desktop_automation_allowed=bool(snapshot.get("desktop_automation_allowed")),
        desktop_automation_timeout_seconds=float(
            snapshot.get("desktop_automation_timeout_seconds") or 30.0
        ),
        vision_allowed=bool(snapshot.get("vision_allowed")),
        vision_max_image_bytes=int(snapshot.get("vision_max_image_bytes") or 10_000_000),
        subprocess_allowed=bool(snapshot.get("subprocess_allowed")),
        command_allowlist=tuple(
            str(item) for item in snapshot.get("command_allowlist") or ()
        ),
        command_timeout_seconds=float(snapshot.get("command_timeout_seconds") or 60.0),
        browser_allowed=bool(snapshot.get("browser_allowed")),
        browser_port=int(snapshot.get("browser_port") or 9222),
        browser_timeout_seconds=float(snapshot.get("browser_timeout_seconds") or 20.0),
    )


def _sanitize(value: Any) -> Any:
    """Convert a validated tool output into strict-JSON-safe data."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _handle(request: Mapping[str, Any]) -> dict[str, Any]:
    Database_info = request.get("Database")
    if not isinstance(Database_info, Mapping):
        raise TypeError("request is missing Database routing")
    registry = _registry_for(
        str(Database_info["path"]), str(Database_info["data_dir"])
    )
    snapshot = request.get("context")
    if not isinstance(snapshot, Mapping):
        raise TypeError("request is missing a context snapshot")
    context = _context_from(snapshot)
    arguments = request.get("arguments")
    result = registry.invoke(
        str(request["tool_id"]),
        arguments if isinstance(arguments, Mapping) else {},
        context,
        version=request.get("version"),
    )
    return {
        "tool_id": result.tool_id,
        "version": result.version,
        "success": result.success,
        "output": _sanitize(result.output) if result.output is not None else None,
        "error": result.error,
        "elapsed_ms": result.elapsed_ms,
    }


def main() -> int:
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        if not line.strip():
            continue
        request_id: Any = None
        try:
            request = json.loads(line)
            if not isinstance(request, Mapping):
                raise TypeError("request must be a JSON object")
            request_id = request.get("id")
            response: dict[str, Any] = {"id": request_id, "result": _handle(request)}
        except Exception as exc:  # noqa: BLE001 - worker boundary: report, stay alive.
            response = {
                "id": request_id,
                "fatal": redact_text(f"{type(exc).__name__}: {exc}")[:500],
            }
        print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
