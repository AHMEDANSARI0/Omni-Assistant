"""Gemini Interactions API adapter (REST, standard library transport)."""

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
    extract_text_content,
)


class GeminiProvider:
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
        self.name = "Gemini"
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
            raise ProviderError("Gemini is not configured. Set GEMINI_API_KEY in .env.")

        input_text = _transcript(request)
        payload: dict[str, Any] = {
            "model": self.model,
            "input": input_text,
            # Do not retain local task prompts server-side for later state use.
            "store": False,
            "generation_config": {
                "temperature": request.temperature,
                "max_output_tokens": request.max_output_tokens,
            },
        }
        if request.system_instruction:
            payload["system_instruction"] = request.system_instruction

        data = self._transport(
            HttpJsonRequest(
                url=f"{self._base_url}/interactions",
                payload=payload,
                headers={"x-goog-api-key": str(self._api_key)},
                timeout_seconds=self._timeout_seconds,
            )
        )
        content = ensure_nonempty_content(_extract_output(data), self.name)
        usage = data.get("usage") if isinstance(data.get("usage"), Mapping) else {}
        returned_model = str(data.get("model") or self.model)
        return CompletionResponse(content, self.name, returned_model, dict(usage))


def _transcript(request: CompletionRequest) -> str:
    non_system = [
        message for message in request.messages if message.role is not MessageRole.SYSTEM
    ]
    if len(non_system) == 1:
        return non_system[0].content
    labels = {MessageRole.USER: "User", MessageRole.ASSISTANT: "Assistant"}
    return "\n\n".join(f"{labels[message.role]}: {message.content}" for message in non_system)


def _extract_output(data: Mapping[str, Any]) -> str:
    status = str(data.get("status") or "")
    if status and status.lower() not in {"completed", "complete", "succeeded"}:
        raise ProviderError(f"Gemini interaction did not complete (status: {status}).")
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct

    parts: list[str] = []
    steps = data.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, Mapping) or step.get("type") != "model_output":
                continue
            text = extract_text_content(step.get("content"))
            if text:
                parts.append(text)
    return "".join(parts)
