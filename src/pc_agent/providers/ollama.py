"""Ollama local/cloud chat adapter."""

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
    parse_ollama_stream_lines,
)


class OllamaProvider:
    implemented = True
    supports_vision = True
    supports_streaming = True

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        base_url: str,
        timeout_seconds: float,
        transport: JsonTransport | None = None,
        stream_transport: StreamTransport | None = None,
    ) -> None:
        self.name = "ollama"
        self._api_key = api_key
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport or UrllibJsonTransport()
        self._stream_transport = stream_transport or UrllibStreamTransport()

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _payload(self, request: CompletionRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_payload(message) for message in request.messages],
            "stream": stream,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_output_tokens,
            },
        }
        if request.json_mode:
            payload["format"] = "json"
        return payload

    @property
    def configured(self) -> bool:
        return bool(self.model and self._base_url)

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        if not self.configured:
            raise ProviderError("Ollama is not configured. Set its model/base URL in .env.")

        data = self._transport(
            HttpJsonRequest(
                url=f"{self._base_url}/api/chat",
                payload=self._payload(request, stream=False),
                headers=self._headers(),
                timeout_seconds=self._timeout_seconds,
            )
        )
        if data.get("error"):
            raise ProviderError(f"Ollama returned an error: {data['error']}")
        message = data.get("message")
        content = ""
        if isinstance(message, Mapping):
            content = extract_text_content(message.get("content"))
        content = ensure_nonempty_content(content, self.name)
        usage = {
            key: data[key]
            for key in ("prompt_eval_count", "eval_count", "total_duration")
            if key in data
        }
        return CompletionResponse(
            content=content,
            provider=self.name,
            model=str(data.get("model") or self.model),
            usage=usage,
        )

    def stream_complete(self, request: CompletionRequest) -> Iterator[str]:
        if not self.configured:
            raise ProviderError("Ollama is not configured. Set its model/base URL in .env.")
        lines = self._stream_transport(
            HttpJsonRequest(
                url=f"{self._base_url}/api/chat",
                payload=self._payload(request, stream=True),
                headers=self._headers(),
                timeout_seconds=self._timeout_seconds,
            )
        )
        produced = False
        for delta in parse_ollama_stream_lines(lines):
            produced = True
            yield delta
        if not produced:
            raise ProviderError("Provider ollama returned an empty stream.")


def _message_payload(message: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": message.role.value,
        "content": message.content,
    }
    if message.images:
        payload["images"] = [image.base64_data for image in message.images]
    return payload
