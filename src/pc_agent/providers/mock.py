"""Offline provider used when no external provider is configured."""

from __future__ import annotations

import json

from .base import CompletionRequest, CompletionResponse


class MockProvider:
    name = "mock"
    model = "offline-mock"
    implemented = True

    @property
    def configured(self) -> bool:
        return True

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        prompt = request.last_user_message.strip()
        if request.json_mode:
            content = json.dumps(
                {
                    "intent": "unsupported",
                    "tool_id": None,
                    "arguments": {},
                    "expected_pages": None,
                    "summary": (
                        "Offline mock provider cannot semantically plan this request. "
                        "Configure an external provider or use a supported deterministic task."
                    ),
                    "missing_information": ["A configured external AI provider is required."],
                }
            )
        else:
            preview = prompt[:240] + ("..." if len(prompt) > 240 else "")
            content = (
                "Offline mock response (no external AI provider was used). "
                f"I received: {preview}"
            )
        return CompletionResponse(
            content=content,
            provider=self.name,
            model=self.model,
            usage={"offline": True},
        )
