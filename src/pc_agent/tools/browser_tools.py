"""Browser automation tools over a user-started debug browser (Phase 13).

The user starts Edge/Chrome with ``--remote-debugging-port`` and a throwaway
profile; these tools drive that exact local session over CDP. Everything is
off until ``PC_AGENT_ALLOW_BROWSER=1``. ``browser.open`` creates a new tab
(or navigates a named one), reads act on the last listed page tab unless a
``target_id`` pins one, typing refuses password/non-text fields, and the
only file ever written is a new screenshot PNG inside the workspace.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..browser_cdp import BrowserClient, PageSession
from ..errors import ToolError
from ..policies.policy import Action
from ..web import validate_url
from .registry import ToolContext, ToolHandler, ToolRegistry, ToolSpec
from .workspace_tools import _resolve_allowed, relative_posix

_DISABLED_MESSAGE = (
    "Browser automation is disabled by policy "
    "(set PC_AGENT_ALLOW_BROWSER=1 in .env and start Edge/Chrome with "
    "--remote-debugging-port, see docs/phase13.md)."
)

_TARGET_ID_INPUT = {"type": "string", "maxLength": 200}


def _client(context: ToolContext) -> BrowserClient:
    if not context.browser_allowed:
        raise ToolError(_DISABLED_MESSAGE)
    if context.browser_client_factory is not None:
        return context.browser_client_factory()
    return BrowserClient(
        port=context.browser_port,
        timeout=context.browser_timeout_seconds,
    )


def _attach(context: ToolContext, arguments: dict[str, Any]) -> PageSession:
    client = _client(context)
    target_id = str(arguments.get("target_id") or "").strip() or None
    return client.attach_page(target_id)


def _status(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    del arguments
    client = _client(context)
    version = client.version()
    targets = []
    for item in client.targets()[:50]:
        targets.append(
            {
                "id": str(item.get("id") or ""),
                "type": str(item.get("type") or ""),
                "title": str(item.get("title") or "")[:200],
                "url": str(item.get("url") or "")[:1000],
            }
        )
    return {
        "browser": str(version.get("Browser") or "unknown"),
        "endpoint": client.endpoint,
        "targets": targets,
        "page_count": len([t for t in targets if t["type"] == "page"]),
    }


def _open(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    raw_url = str(arguments.get("url") or "")
    validated = validate_url(
        raw_url, allow_private_network=context.allow_private_network
    )
    wait = arguments.get("wait_seconds")
    wait_seconds = float(wait) if isinstance(wait, int | float) else None
    target_id = str(arguments.get("target_id") or "").strip() or None
    client = _client(context)
    session = (
        client.attach_page(target_id) if target_id else client.open_new_page()
    )
    try:
        identity = session.navigate(validated.url, wait_seconds=wait_seconds)
    finally:
        session.close()
    return {
        "requested_url": validated.url,
        "url": str(identity.get("url") or ""),
        "title": str(identity.get("title") or ""),
        "target_id": session.target_id,
    }


def _snapshot(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    max_chars = int(arguments.get("max_chars", 20000))
    max_links = int(arguments.get("max_links", 50))
    session = _attach(context, arguments)
    try:
        data = session.snapshot(max_chars=max_chars, max_links=max_links)
    finally:
        session.close()
    links = []
    for item in data.get("links") or []:
        if isinstance(item, dict):
            links.append(
                {
                    "text": str(item.get("text") or "")[:120],
                    "href": str(item.get("href") or "")[:1000],
                }
            )
    return {
        "url": str(data.get("url") or ""),
        "title": str(data.get("title") or ""),
        "text": str(data.get("text") or ""),
        "truncated": bool(data.get("truncated")),
        "links": links,
        "target_id": session.target_id,
    }


def _click(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    selector = str(arguments.get("selector") or "").strip()
    text = str(arguments.get("text") or "").strip()
    if bool(selector) == bool(text):
        raise ToolError("Give exactly one of 'selector' or 'text' to click.")
    if len(selector) > 500 or len(text) > 200:
        raise ToolError("Click selector/text is too long.")
    session = _attach(context, arguments)
    try:
        result = session.click(
            selector=selector or None, text=text or None
        )
    finally:
        session.close()
    return {
        "clicked": True,
        "tag": str(result.get("tag") or ""),
        "text": str(result.get("text") or ""),
        "url_before": str(result.get("url_before") or ""),
        "url": str(result.get("url") or ""),
        "title": str(result.get("title") or ""),
        "target_id": session.target_id,
    }


def _type(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    selector = str(arguments.get("selector") or "").strip()
    text = str(arguments.get("text") or "")
    if not selector or len(selector) > 500:
        raise ToolError("A field selector (max 500 chars) is required.")
    if len(text) > 4000:
        raise ToolError("Typed text is limited to 4000 characters.")
    session = _attach(context, arguments)
    try:
        result = session.type_text(selector=selector, text=text)
    finally:
        session.close()
    return {
        "selector": selector,
        "value": str(result.get("value") or ""),
        "typed_length": len(text),
        "url": str(result.get("url") or ""),
        "target_id": session.target_id,
    }


def _screenshot(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    raw_output = str(arguments.get("output_path") or "").strip()
    if not raw_output:
        raise ToolError("An output_path for the screenshot is required.")
    target = _resolve_allowed(context, raw_output, Action.WRITE_FILE)
    if target.suffix.lower() != ".png":
        raise ToolError("Screenshot output_path must end with .png.")
    if target.exists():
        raise ToolError(f"Screenshot already exists: {raw_output}")
    session = _attach(context, arguments)
    try:
        png = session.screenshot_png()
        identity = session.page_identity()
    finally:
        session.close()
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ToolError("Browser screenshot was not a PNG image.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:  # exclusive create: never overwrite
        handle.write(png)
    return {
        "output_path": relative_posix(context, target),
        "size_bytes": len(png),
        "sha256": hashlib.sha256(png).hexdigest(),
        "url": str(identity.get("url") or ""),
        "title": str(identity.get("title") or ""),
    }


def _specs() -> list[tuple[ToolSpec, ToolHandler]]:
    target_prop = {"target_id": _TARGET_ID_INPUT}
    return [
        (
            ToolSpec(
                id="browser.status",
                name="Browser status",
                version="1.0.0",
                description="Report the connected debug browser and its open tabs (read-only).",
                input_schema={"type": "object", "properties": {}},
                output_schema={
                    "type": "object",
                    "properties": {
                        "browser": {"type": "string"},
                        "endpoint": {"type": "string"},
                        "targets": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "type": {"type": "string"},
                                    "title": {"type": "string"},
                                    "url": {"type": "string"},
                                },
                                "required": ["id", "type", "title", "url"],
                            },
                        },
                        "page_count": {"type": "integer"},
                    },
                    "required": ["browser", "endpoint", "targets", "page_count"],
                },
                permissions=(),
                timeout_seconds=30.0,
            ),
            _status,
        ),
        (
            ToolSpec(
                id="browser.open",
                name="Browser open URL",
                version="1.0.0",
                description="Open one exact http(s) URL in the debug browser (new tab unless target_id navigates an existing one) and wait for load.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "maxLength": 4096},
                        "wait_seconds": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 60,
                        },
                        **target_prop,
                    },
                    "required": ["url"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "requested_url": {"type": "string"},
                        "url": {"type": "string"},
                        "title": {"type": "string"},
                        "target_id": {"type": "string"},
                    },
                    "required": ["requested_url", "url", "title", "target_id"],
                },
                permissions=(Action.NETWORK_ACCESS,),
                timeout_seconds=120.0,
            ),
            _open,
        ),
        (
            ToolSpec(
                id="browser.snapshot",
                name="Browser page snapshot",
                version="1.0.0",
                description="Read the current tab's URL, title, visible text and links (read-only, bounded).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "max_chars": {
                            "type": "integer",
                            "minimum": 500,
                            "maximum": 100000,
                        },
                        "max_links": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 200,
                        },
                        **target_prop,
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "title": {"type": "string"},
                        "text": {"type": "string"},
                        "truncated": {"type": "boolean"},
                        "links": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string"},
                                    "href": {"type": "string"},
                                },
                                "required": ["text", "href"],
                            },
                        },
                        "target_id": {"type": "string"},
                    },
                    "required": [
                        "url",
                        "title",
                        "text",
                        "truncated",
                        "links",
                        "target_id",
                    ],
                },
                permissions=(Action.NETWORK_ACCESS,),
                timeout_seconds=60.0,
            ),
            _snapshot,
        ),
        (
            ToolSpec(
                id="browser.click",
                name="Browser click",
                version="1.0.0",
                description="Click the unique element matching a CSS selector or exact visible text on the current tab.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "selector": {"type": "string", "maxLength": 500},
                        "text": {"type": "string", "maxLength": 200},
                        **target_prop,
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "clicked": {"type": "boolean"},
                        "tag": {"type": "string"},
                        "text": {"type": "string"},
                        "url_before": {"type": "string"},
                        "url": {"type": "string"},
                        "title": {"type": "string"},
                        "target_id": {"type": "string"},
                    },
                    "required": [
                        "clicked",
                        "tag",
                        "text",
                        "url_before",
                        "url",
                        "title",
                        "target_id",
                    ],
                },
                permissions=(Action.NETWORK_ACCESS,),
                timeout_seconds=120.0,
            ),
            _click,
        ),
        (
            ToolSpec(
                id="browser.type",
                name="Browser type text",
                version="1.0.0",
                description="Replace the value of one text input/textarea (never password or file fields) on the current tab.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "selector": {"type": "string", "maxLength": 500},
                        "text": {"type": "string", "maxLength": 4000},
                        **target_prop,
                    },
                    "required": ["selector", "text"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "selector": {"type": "string"},
                        "value": {"type": "string"},
                        "typed_length": {"type": "integer"},
                        "url": {"type": "string"},
                        "target_id": {"type": "string"},
                    },
                    "required": [
                        "selector",
                        "value",
                        "typed_length",
                        "url",
                        "target_id",
                    ],
                },
                permissions=(Action.NETWORK_ACCESS,),
                timeout_seconds=60.0,
            ),
            _type,
        ),
        (
            ToolSpec(
                id="browser.screenshot",
                name="Browser screenshot",
                version="1.0.0",
                description="Save a PNG screenshot of the current tab as a new workspace file (never overwrites).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "output_path": {"type": "string", "maxLength": 500},
                        **target_prop,
                    },
                    "required": ["output_path"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "output_path": {"type": "string"},
                        "size_bytes": {"type": "integer"},
                        "sha256": {"type": "string"},
                        "url": {"type": "string"},
                        "title": {"type": "string"},
                    },
                    "required": [
                        "output_path",
                        "size_bytes",
                        "sha256",
                        "url",
                        "title",
                    ],
                },
                permissions=(Action.WRITE_FILE, Action.NETWORK_ACCESS),
                timeout_seconds=120.0,
            ),
            _screenshot,
        ),
    ]


def register_browser_tools(registry: ToolRegistry) -> None:
    for spec, handler in _specs():
        registry.register(spec, handler)


__all__ = ["register_browser_tools"]


# Keep Path import for type checkers that follow workspace helpers.
_ = Path
