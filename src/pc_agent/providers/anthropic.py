"""Direct Anthropic Messages API adapter.

The adapter uses Anthropic's stateless ``POST /v1/messages`` contract with
the current ``2026-10-21`` API version. Sampling fields such as
``temperature`` were removed from that API version, so they are deliberately
not forwarded; prompts and the caller's output-token ceiling remain explicit.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import ProviderError
from .base import (
    CompletionRequest,
    CompletionResponse,
    HttpJsonRequest,
    JsonTransport,
    MessageRole,
    UrllibJsonTransport,
    ensure_nonempty_content,
)

_ANTHROPIC_VERSION = "2026-10-21"


class AnthropicProvider:
    implemented = True
    supports_vision = True

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        base_url: str,
        timeout_seconds: float,
        transport: JsonTransport | None = None,
    ) -> None:
        self.name = "anthropic"
        self._api_key = api_key
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport or UrllibJsonTransport()

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self.model and self._base_url)

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        if not self.configured:
            raise ProviderError(
                "Anthropic is not configured. Set ANTHROPIC_API_KEY in .env "
                "or Windows Credential Manager."
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_output_tokens,
            "messages": _message_payloads(request),
            "stream": False,
        }
        system_blocks = [
            {"type": "text", "text": message.content}
            for message in request.messages
            if message.role is MessageRole.SYSTEM
        ]
        if system_blocks:
            payload["system"] = system_blocks

        data = self._transport(
            HttpJsonRequest(
                url=_messages_url(self._base_url),
                payload=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "anthropic-version": _ANTHROPIC_VERSION,
                },
                timeout_seconds=self._timeout_seconds,
            )
        )
        content = ensure_nonempty_content(_extract_output(data), self.name)
        usage = data.get("usage") if isinstance(data.get("usage"), Mapping) else {}
        return CompletionResponse(
            content=content,
            provider=self.name,
            model=str(data.get("model") or self.model),
            usage=dict(usage),
        )


def _messages_url(base_url: str) -> str:
    if base_url.endswith("/v1/messages"):
        return base_url
    if base_url.endswith("/v1"):
        return f"{base_url}/messages"
    return f"{base_url}/v1/messages"


def _message_payloads(request: CompletionRequest) -> list[dict[str, Any]]:
    messages = [
        message
        for message in request.messages
        if message.role is not MessageRole.SYSTEM
    ]
    if not messages or messages[0].role is not MessageRole.USER:
        raise ProviderError(
            "Anthropic requires the first non-system message to use the user role."
        )

    payloads: list[dict[str, Any]] = []
    for message in messages:
        if not message.images:
            payloads.append(
                {"role": message.role.value, "content": message.content}
            )
            continue
        content: list[dict[str, Any]] = []
        for index, image in enumerate(message.images, start=1):
            if len(message.images) > 1:
                content.append({"type": "text", "text": f"Image {index}:"})
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.mime_type,
                        "data": image.base64_data,
                    },
                }
            )
        content.append({"type": "text", "text": message.content})
        payloads.append({"role": message.role.value, "content": content})
    return payloads


def _extract_output(data: Mapping[str, Any]) -> str:
    error = data.get("error")
    if data.get("type") == "error" or error:
        detail = error.get("message") if isinstance(error, Mapping) else str(error)
        raise ProviderError(f"Provider anthropic returned an error: {detail}")

    if data.get("stop_reason") == "refusal":
        details = data.get("stop_details")
        category = (
            str(details.get("category"))
            if isinstance(details, Mapping) and details.get("category")
            else "unspecified"
        )
        raise ProviderError(f"Provider anthropic refused the request ({category}).")

    content = data.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)
