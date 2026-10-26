from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pc_agent.config import Settings
from pc_agent.errors import ConfigError, ProviderError
from pc_agent.providers.base import CompletionRequest, CompletionResponse, HttpJsonRequest
from pc_agent.providers.gemini_adapter import GeminiProvider
from pc_agent.providers.ollama import OllamaProvider
from pc_agent.providers.openai_compatible import OpenAICompatibleProvider
from pc_agent.providers.router import ModelRouter, build_model_router, provider_statuses


class FakeTransport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.requests: list[HttpJsonRequest] = []

    def __call__(self, request: HttpJsonRequest) -> dict[str, Any]:
        self.requests.append(request)
        return self.response


class FakeProvider:
    implemented = True

    def __init__(
        self,
        name: str,
        outcomes: list[CompletionResponse | ProviderError],
    ) -> None:
        self.name = name
        self.model = f"{name}-model"
        self.outcomes = list(outcomes)
        self.calls = 0

    @property
    def configured(self) -> bool:
        return True

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, ProviderError):
            raise outcome
        return outcome


def response(provider: str, content: str = "ok") -> CompletionResponse:
    return CompletionResponse(content=content, provider=provider, model=f"{provider}-model")


def test_gemini_adapter_uses_interactions_and_never_needs_key_in_url() -> None:
    transport = FakeTransport(
        {
            "status": "completed",
            "model": "gemini-test",
            "steps": [
                {"type": "thought", "signature": "hidden"},
                {
                    "type": "model_output",
                    "content": [{"type": "text", "text": "Hello from Gemini"}],
                },
            ],
            "usage": {"total_tokens": 9},
        }
    )
    provider = GeminiProvider(
        api_key="secret-key",
        model="gemini-test",
        base_url="https://example.test/v1beta",
        timeout_seconds=12,
        transport=transport,
    )

    result = provider.complete(
        CompletionRequest.from_prompt("Say hello", system_instruction="Be brief")
    )

    request = transport.requests[0]
    assert result.content == "Hello from Gemini"
    assert request.url == "https://example.test/v1beta/interactions"
    assert request.headers["x-goog-api-key"] == "secret-key"
    assert "secret-key" not in request.url
    assert request.payload["store"] is False
    assert request.payload["system_instruction"] == "Be brief"


def test_openrouter_adapter_uses_chat_completions() -> None:
    transport = FakeTransport(
        {
            "model": "route/model",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Routed answer"}],
                    }
                }
            ],
        }
    )
    provider = OpenAICompatibleProvider(
        name="openrouter",
        api_key="router-key",
        model="openrouter/auto",
        base_url="https://openrouter.test/api/v1",
        timeout_seconds=10,
        transport=transport,
    )

    result = provider.complete(CompletionRequest.from_prompt("Answer"))

    request = transport.requests[0]
    assert result.content == "Routed answer"
    assert request.url.endswith("/chat/completions")
    assert request.headers["Authorization"] == "Bearer router-key"
    assert request.headers["X-OpenRouter-Title"] == "Self-Healing PC Agent"


def test_ollama_adapter_uses_local_chat_endpoint() -> None:
    transport = FakeTransport(
        {"model": "local", "message": {"role": "assistant", "content": "Local answer"}}
    )
    provider = OllamaProvider(
        api_key=None,
        model="llama3.1",
        base_url="http://localhost:11434",
        timeout_seconds=10,
        transport=transport,
    )

    result = provider.complete(CompletionRequest.from_prompt("Answer", json_mode=True))

    assert result.content == "Local answer"
    assert transport.requests[0].url == "http://localhost:11434/api/chat"
    assert transport.requests[0].payload["format"] == "json"


def test_router_retries_transient_error_then_succeeds() -> None:
    provider = FakeProvider(
        "first",
        [ProviderError("temporary", retryable=True), response("first", "recovered")],
    )
    router = ModelRouter([provider], max_retries=1, backoff_seconds=0, sleeper=lambda _: None)

    routed = router.complete(CompletionRequest.from_prompt("hi"))

    assert routed.response.content == "recovered"
    assert [attempt.success for attempt in routed.attempts] == [False, True]
    assert provider.calls == 2


def test_router_falls_back_only_to_next_configured_provider() -> None:
    first = FakeProvider("first", [ProviderError("bad key")])
    second = FakeProvider("second", [response("second", "fallback worked")])
    router = ModelRouter([first, second], backoff_seconds=0)

    routed = router.complete(CompletionRequest.from_prompt("hi"))

    assert routed.response.provider == "second"
    assert [attempt.provider for attempt in routed.attempts] == ["first", "second"]


def test_router_can_disable_fallback() -> None:
    first = FakeProvider("first", [ProviderError("down", retryable=True)])
    second = FakeProvider("second", [response("second")])
    router = ModelRouter([first, second], max_retries=0, backoff_seconds=0)

    with pytest.raises(ProviderError, match="All configured AI providers failed"):
        router.complete(CompletionRequest.from_prompt("hi"), allow_fallback=False)
    assert second.calls == 0


def test_factory_skips_unkeyed_providers_and_defaults_to_mock(tmp_path: Path) -> None:
    settings = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_PROVIDER_ORDER": "gemini,openrouter,anthropic",
            "OPENROUTER_API_KEY": "router-secret",
        },
    )
    router = build_model_router(settings)
    statuses = provider_statuses(settings)

    assert router.provider_names == ("openrouter", "anthropic")
    assert all("router-secret" not in repr(status) for status in statuses)
    assert next(status for status in statuses if status.name == "anthropic").implemented is False

    empty = Settings.from_env(tmp_path, environ={})
    assert build_model_router(empty).provider_names == ("mock",)


def test_anthropic_reserved_slot_does_not_block_mock_fallback(tmp_path: Path) -> None:
    settings = Settings.from_env(
        tmp_path,
        environ={"PC_AGENT_PROVIDER_ORDER": "anthropic,mock"},
    )
    routed = build_model_router(settings).complete(CompletionRequest.from_prompt("hi"))

    assert routed.response.provider == "mock"
    assert routed.attempts[0].success is False


def test_provider_config_rejects_plaintext_remote_endpoint_and_bad_model(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="HTTPS"):
        Settings.from_env(
            tmp_path,
            environ={"PC_AGENT_OPENAI_BASE_URL": "http://example.invalid/v1"},
        )
    with pytest.raises(ConfigError, match="without whitespace"):
        Settings.from_env(
            tmp_path,
            environ={"PC_AGENT_GEMINI_MODEL": "bad model"},
        )
