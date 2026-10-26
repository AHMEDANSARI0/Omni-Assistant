"""Ollama local/cloud chat adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import ProviderError
from .base import (
    CompletionRequest,
    CompletionResponse,
    HttpJsonRequest,
    JsonTransport,
    UrllibJsonTransport,
    ensure_nonempty_content,
    extract_text_content,
)


class OllamaProvider:
    implemented = True

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        base_url: str,
        timeout_seconds: float,
        transport: JsonTransport | None = None,
    ) -> None:
        self.name = "ollama"
        self._api_key = api_key
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport or UrllibJsonTransport()

    @property
    def configured(self) -> bool:
        return bool(self.model and self._base_url)

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        if not self.configured:
            raise ProviderError("Ollama is not configured. Set its model/base URL in .env.")

        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in request.messages
            ],
            "stream": False,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_output_tokens,
            },
        }
        if request.json_mode:
            payload["format"] = "json"

        data = self._transport(
            HttpJsonRequest(
                url=f"{self._base_url}/api/chat",
                payload=payload,
                headers=headers,
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
