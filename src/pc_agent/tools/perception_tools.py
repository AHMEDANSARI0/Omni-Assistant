"""Computer-vision tools that run entirely on this machine (Phase 11).

Two tools, both deterministic and offline:

* ``vision.screen`` - measure an image (dimensions, brightness, contrast,
  dominant colours, per-cell ink density) and say where the dense regions are.
  Optionally it captures the live Windows screen first, which needs the
  desktop-automation gate because it reads what is on the display.
* ``vision.locate`` - find a small template image inside a screenshot with a
  bounded coarse correlation search.

Neither tool can describe a picture in words. That stays on the separate,
approved provider-vision route (``PC_AGENT_ALLOW_VISION``), so nothing here
silently uploads a screenshot.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from .. import perception
from ..errors import ToolError
from ..policies.policy import Action
from .registry import ToolContext, ToolHandler, ToolRegistry, ToolSpec
from .workspace_tools import _resolve_allowed, relative_posix

_MAX_SCREENSHOT_BYTES = 20_000_000

_DISABLED_MESSAGE = (
    "Capturing the live screen needs desktop automation "
    "(set PC_AGENT_ALLOW_DESKTOP_AUTOMATION=1). Analysing an image file you "
    "already have needs no gate."
)


def _read(context: ToolContext, raw: str) -> tuple[Path, bytes]:
    path = _resolve_allowed(context, raw, Action.READ_FILE)
    data = perception.read_image_file(path, max_bytes=min(_MAX_SCREENSHOT_BYTES, context.vision_max_image_bytes))
    return path, data


def _record(
    context: ToolContext,
    *,
    tool_id: str,
    action: str,
    target: str,
    status: str,
    detail: str,
    duration_ms: int,
    artifact: Path | None = None,
) -> None:
    recorder = context.automation_recorder
    if recorder is None:
        return
    recorder(
        {
            "kind": "vision",
            "tool_id": tool_id,
            "action": action,
            "target": target,
            "status": status,
            "detail": detail,
            "duration_ms": duration_ms,
            "artifact_path": str(artifact) if artifact is not None else None,
            "allowed": status != "refused",
        }
    )


def _grab_screen(context: ToolContext) -> tuple[bytes, str]:
    """Capture the primary screen through the desktop backend (gated)."""
    if not context.desktop_automation_allowed:
        raise ToolError(_DISABLED_MESSAGE)
    if context.desktop_automation_backend_factory is None:
        raise ToolError(
            "No desktop backend is available in this context; run the tool "
            "through pc-agent automation, the API or the GUI."
        )
    backend = context.desktop_automation_backend_factory()
    try:
        png = backend.screenshot_png()
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ToolError("The desktop backend returned a non-PNG screenshot.")
    return png, "live-screen"


def _screen(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    started = time.perf_counter()
    grid = arguments.get("grid", 4)
    if isinstance(grid, bool) or not isinstance(grid, int) or not 1 <= grid <= perception.MAX_GRID:
        raise ToolError(f"grid must be between 1 and {perception.MAX_GRID}.")
    source = str(arguments.get("path") or "").strip()
    if source:
        path, data = _read(context, source)
        origin = "file"
    else:
        data, origin = _grab_screen(context)
        path = None
    try:
        facts = perception.analyse_image(data, grid=grid)
    except ToolError:
        _record(
            context,
            tool_id="vision.screen",
            action="analyse",
            target=source or origin,
            status="failed",
            detail="image analysis refused",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        raise
    payload = facts.to_dict()
    digest = hashlib.sha256(data).hexdigest()
    payload.update(
        {
            "source": origin,
            "path": relative_posix(context, path) if path is not None else None,
            "sha256": digest,
            "region_hint": perception.region_hint(facts),
            "method": "local-analysis-v1",
        }
    )
    _record(
        context,
        tool_id="vision.screen",
        action="analyse",
        target=source or origin,
        status="ok",
        detail=f"{facts.width}x{facts.height}; busy_cells={facts.busy_cells}; "
        f"text_likely={facts.text_likely}",
        duration_ms=int((time.perf_counter() - started) * 1000),
        artifact=path,
    )
    return payload


def _locate(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    started = time.perf_counter()
    screen_raw = str(arguments.get("screen_path") or "").strip()
    template_raw = str(arguments.get("template_path") or "").strip()
    if not screen_raw or not template_raw:
        raise ToolError("screen_path and template_path are both required.")
    threshold = arguments.get("threshold", perception.MIN_TEMPLATE_SCORE)
    if isinstance(threshold, bool) or not isinstance(threshold, int | float):
        raise ToolError("threshold must be a number between 0.5 and 1.0.")
    threshold_value = float(threshold)
    if not 0.5 <= threshold_value <= 1.0:
        raise ToolError("threshold must be between 0.5 and 1.0.")
    _screen_path, screen_bytes = _read(context, screen_raw)
    _template_path, template_bytes = _read(context, template_raw)
    result = perception.locate_template(
        screen_bytes, template_bytes, threshold=threshold_value
    )
    _record(
        context,
        tool_id="vision.locate",
        action="locate",
        target=template_raw,
        status="ok" if result["found"] else "partial",
        detail=f"score={result['score']}; steps={result['search_steps']}",
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    return result


def _specs() -> list[tuple[ToolSpec, ToolHandler]]:
    return [
        (
            ToolSpec(
                id="vision.screen",
                name="Screen analysis (local)",
                version="1.0.0",
                description=(
                    "Measure an image offline: size, brightness, contrast, dominant "
                    "colours and per-cell ink density. With desktop automation enabled "
                    "and no path, it captures the live screen first. It never describes "
                    "content in words and never uploads pixels."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "maxLength": 400},
                        "grid": {"type": "integer", "minimum": 1, "maximum": 12},
                    },
                    "required": [],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "path": {"type": ["string", "null"]},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                        "mode": {"type": "string"},
                        "format": {"type": "string"},
                        "size_bytes": {"type": "integer"},
                        "brightness": {"type": "number"},
                        "contrast": {"type": "number"},
                        "dominant_colors": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "r": {"type": "integer"},
                                    "g": {"type": "integer"},
                                    "b": {"type": "integer"},
                                },
                                "required": ["r", "g", "b"],
                            },
                        },
                        "grid": {
                            "type": "object",
                            "properties": {
                                "rows": {"type": "integer"},
                                "columns": {"type": "integer"},
                                "brightness": {"type": "array"},
                                "ink": {"type": "array"},
                            },
                            "required": ["rows", "columns"],
                        },
                        "busy_cells": {"type": "integer"},
                        "text_likely": {"type": "boolean"},
                        "region_hint": {"type": "string"},
                        "sha256": {"type": "string"},
                        "method": {"type": "string"},
                    },
                    "required": [
                        "source",
                        "width",
                        "height",
                        "busy_cells",
                        "text_likely",
                        "method",
                    ],
                },
                permissions=(),
                timeout_seconds=60.0,
            ),
            _screen,
        ),
        (
            ToolSpec(
                id="vision.locate",
                name="Locate template in screenshot (local)",
                version="1.0.0",
                description=(
                    "Find a small template image inside a screenshot using a bounded, "
                    "scale-aware correlation search. Returns the observed score and up "
                    "to five boxes; it is not a neural detector."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "screen_path": {"type": "string", "maxLength": 400},
                        "template_path": {"type": "string", "maxLength": 400},
                        "threshold": {"type": "number", "minimum": 0.5, "maximum": 1.0},
                        "max_matches": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["screen_path", "template_path"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "found": {"type": "boolean"},
                        "score": {"type": "number"},
                        "threshold": {"type": "number"},
                        "scale": {"type": "number"},
                        "matches": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "width": {"type": "integer"},
                                    "height": {"type": "integer"},
                                    "score": {"type": "number"},
                                },
                                "required": ["x", "y", "width", "height", "score"],
                            },
                        },
                        "search_steps": {"type": "integer"},
                        "screen": {
                            "type": "object",
                            "properties": {
                                "width": {"type": "integer"},
                                "height": {"type": "integer"},
                            },
                            "required": ["width", "height"],
                        },
                        "template": {
                            "type": "object",
                            "properties": {
                                "width": {"type": "integer"},
                                "height": {"type": "integer"},
                            },
                            "required": ["width", "height"],
                        },
                        "reduction_factor": {"type": "integer"},
                        "method": {"type": "string"},
                    },
                    "required": ["found", "score", "matches", "method"],
                },
                permissions=(),
                timeout_seconds=120.0,
            ),
            _locate,
        ),
    ]


def register_perception_tools(registry: ToolRegistry) -> None:
    for spec, handler in _specs():
        registry.register(spec, handler)


__all__ = ["register_perception_tools"]
