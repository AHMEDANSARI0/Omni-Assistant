"""OpenAI-compatible Chat Completions adapter.

One reviewed implementation powers OpenRouter, OpenAI and user-configured
OpenAI-compatible endpoints; provider identity and endpoint remain explicit.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from ..errors import ProviderError
from .base import (
    CompletionRequest,
    CompletionResponse,
    HttpJsonRequest,
    JsonTransport,
    StreamTransport,
    UrllibJsonTransport,
    UrllibStreamTransport,
    ensure_nonempty_content,
    extract_text_content,
    parse_openai_sse_lines,
)


class OpenAICompatibleProvider:
    implemented = True
    supports_vision = True
    supports_streaming = True

    def __init__(
        self,
        *,
        name: str,
        api_key: str | None,
        model: str,
        base_url: str,
        timeout_seconds: float,
        transport: JsonTransport | None = None,
        stream_transport: StreamTransport | None = None,
        api_key_required: bool = True,
    ) -> None:
        self.name = name
        self._api_key = api_key
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport or UrllibJsonTransport()
        self._stream_transport = stream_transport or UrllibStreamTransport()
        self._api_key_required = api_key_required

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if self.name == "openrouter":
            headers["X-OpenRouter-Title"] = "Self-Healing PC Agent"
        return headers

    def _payload(self, request: CompletionRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_payload(message) for message in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
        }
        if stream:
            payload["stream"] = True
        return payload

    def _require_configured(self) -> None:
        if not self.configured:
            raise ProviderError(
                f"Provider {self.name} is not configured. Check its API key/model in .env."
            )

    @property
    def configured(self) -> bool:
        has_key = bool(self._api_key) or not self._api_key_required
        return bool(has_key and self.model and self._base_url)

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self._require_configured()
        data = self._transport(
            HttpJsonRequest(
                url=f"{self._base_url}/chat/completions",
                payload=self._payload(request, stream=False),
                headers=self._headers(),
                timeout_seconds=self._timeout_seconds,
            )
        )
        content = ensure_nonempty_content(_extract_output(data, self.name), self.name)
        usage = data.get("usage") if isinstance(data.get("usage"), Mapping) else {}
        return CompletionResponse(
            content=content,
            provider=self.name,
            model=str(data.get("model") or self.model),
            usage=dict(usage),
        )

    def stream_complete(self, request: CompletionRequest) -> Iterator[str]:
        self._require_configured()
        lines = self._stream_transport(
            HttpJsonRequest(
                url=f"{self._base_url}/chat/completions",
                payload=self._payload(request, stream=True),
                headers=self._headers(),
                timeout_seconds=self._timeout_seconds,
            )
        )
        produced = False
        for delta in parse_openai_sse_lines(lines):
            produced = True
            yield delta
        if not produced:
            raise ProviderError(f"Provider {self.name} returned an empty stream.")


def _message_payload(message: Any) -> dict[str, Any]:
    if not message.images:
        return {"role": message.role.value, "content": message.content}
    content: list[dict[str, Any]] = [{"type": "text", "text": message.content}]
    for image in message.images:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": image.data_url, "detail": "auto"},
            }
        )
    return {"role": message.role.value, "content": content}


def _extract_output(data: Mapping[str, Any], provider: str) -> str:
    error = data.get("error")
    if error:
        detail = error.get("message") if isinstance(error, Mapping) else str(error)
        raise ProviderError(f"Provider {provider} returned an error: {detail}")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError(f"Provider {provider} returned no choices.")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise ProviderError(f"Provider {provider} returned an invalid choice.")
    message = first.get("message")
    if isinstance(message, Mapping):
        text = extract_text_content(message.get("content"))
        if text:
            return text
        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            raise ProviderError(f"Provider {provider} refused the request.")
    return extract_text_content(first.get("text"))
