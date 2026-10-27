"""Registered user-requested web fetch tool."""

from __future__ import annotations

from typing import Any

from ..errors import ToolError
from ..policies.policy import Action, ActionRequest
from ..web import fetch_url
from .registry import ToolContext, ToolHandler, ToolRegistry, ToolSpec


def _fetch(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    raw_url = str(arguments["url"])
    decision = context.policy_engine.evaluate(
        ActionRequest(
            Action.NETWORK_ACCESS,
            target_url=raw_url,
            description="Fetch one user-requested public web page as text.",
        )
    )
    if not decision.allowed:
        raise ToolError(
            f"Policy {decision.outcome.value}: {decision.reason} "
            "Run the dedicated command with your real URL, for example: "
            "pc-agent web fetch https://example.com/"
        )

    result = fetch_url(
        raw_url,
        timeout_seconds=context.web_timeout_seconds,
        max_bytes=context.web_max_bytes,
        allow_private_network=context.allow_private_network,
    )
    max_chars = int(arguments.get("max_chars", 50_000))
    if max_chars < 1 or max_chars > 200_000:
        raise ToolError("max_chars must be between 1 and 200000.")
    text = result.text[:max_chars]
    return {
        "requested_url": result.requested_url,
        "final_url": result.final_url,
        "status": result.status,
        "content_type": result.content_type,
        "title": result.title,
        "text": text,
        "characters": len(result.text),
        "links": [link.to_dict() for link in result.links],
        "size_bytes": result.size_bytes,
        "body_sha256": result.body_sha256,
        "truncated": result.truncated or len(result.text) > max_chars,
    }


def _specs() -> list[tuple[ToolSpec, ToolHandler]]:
    return [
        (
            ToolSpec(
                id="web.fetch",
                name="Fetch web page text",
                version="1.0.0",
                description="Fetch one explicitly approved HTTP(S) page as bounded text; scripts are never executed and private/local addresses are blocked unless the direct web command explicitly allows them.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "minLength": 1, "maxLength": 4096},
                        "max_chars": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 200000,
                        },
                    },
                    "required": ["url"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "requested_url": {"type": "string"},
                        "final_url": {"type": "string"},
                        "status": {"type": "integer"},
                        "content_type": {"type": "string"},
                        "title": {"type": ["string", "null"]},
                        "text": {"type": "string"},
                        "characters": {"type": "integer"},
                        "links": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "url": {"type": "string"},
                                    "text": {"type": "string"},
                                },
                                "required": ["url", "text"],
                            },
                        },
                        "size_bytes": {"type": "integer"},
                        "body_sha256": {"type": "string"},
                        "truncated": {"type": "boolean"},
                    },
                    "required": [
                        "requested_url",
                        "final_url",
                        "status",
                        "content_type",
                        "title",
                        "text",
                        "characters",
                        "links",
                        "size_bytes",
                        "body_sha256",
                        "truncated",
                    ],
                },
                permissions=(Action.NETWORK_ACCESS,),
                timeout_seconds=75.0,
                network_required=True,
            ),
            _fetch,
        )
    ]


def register_web_tools(registry: ToolRegistry) -> None:
    for spec, handler in _specs():
        registry.register(spec, handler)
