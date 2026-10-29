"""Phase 19: direct Anthropic Messages API provider."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pc_agent import __version__
from pc_agent.budgets import (
    BudgetedModelRouter,
    TaskBudgetLimits,
    TaskBudgetTracker,
)
from pc_agent.cli import main
from pc_agent.config import DEFAULT_ANTHROPIC_MODEL, Settings
from pc_agent.credentials import CredentialStore, InMemoryCredentialBackend
from pc_agent.errors import ProviderError
from pc_agent.gui.controller import GuiController
from pc_agent.providers.anthropic import AnthropicProvider
from pc_agent.providers.base import (
    ChatMessage,
    CompletionRequest,
    HttpJsonRequest,
    ImageInput,
)
from pc_agent.providers.router import ModelRouter, build_model_router, provider_statuses
from pc_agent.release import release_candidate_files
from pc_agent.tools import build_default_registry

_SECRET = "phase19-anthropic-secret"
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
)


class FakeTransport:
    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)
        self.requests: list[HttpJsonRequest] = []

    def __call__(self, request: HttpJsonRequest) -> dict[str, Any]:
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        return {
            "model": "claude-test",
            "content": [{"type": "text", "text": "Anthropic answer"}],
            "usage": {"input_tokens": 12, "output_tokens": 6},
        }


def _provider(transport: FakeTransport, *, base_url: str = "https://api.example.test"):
    return AnthropicProvider(
        api_key=_SECRET,
        model="claude-test",
        base_url=base_url,
        timeout_seconds=17,
        transport=transport,
    )


def test_anthropic_text_system_multi_turn_and_usage_contract() -> None:
    transport = FakeTransport(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-returned",
            "content": [
                {"type": "thinking", "thinking": "not shown"},
                {"type": "text", "text": "Hello "},
                {"type": "tool_use", "name": "ignored", "input": {}},
                {"type": "text", "text": "from Claude"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 6},
        }
    )
    provider = _provider(transport)
    request = CompletionRequest(
        messages=(
            ChatMessage.system("Be exact"),
            ChatMessage.user("Hello"),
            ChatMessage.assistant("Hi"),
            ChatMessage.user("Say hello again"),
        ),
        temperature=0.7,
        max_output_tokens=321,
        json_mode=True,
    )

    result = provider.complete(request)

    sent = transport.requests[0]
    assert result.content == "Hello from Claude"
    assert result.provider == "anthropic"
    assert result.model == "claude-returned"
    assert result.usage == {"input_tokens": 12, "output_tokens": 6}
    assert sent.url == "https://api.example.test/v1/messages"
    assert sent.timeout_seconds == 17
    assert sent.headers == {
        "Authorization": f"Bearer {_SECRET}",
        "anthropic-version": "2026-10-21",
    }
    assert _SECRET not in sent.url
    assert sent.payload["model"] == "claude-test"
    assert sent.payload["max_tokens"] == 321
    assert sent.payload["stream"] is False
    assert sent.payload["system"] == [{"type": "text", "text": "Be exact"}]
    assert sent.payload["messages"] == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
        {"role": "user", "content": "Say hello again"},
    ]
    # Anthropic API version 2026-10-21 removed sampling parameters.
    assert "temperature" not in sent.payload
    assert "top_p" not in sent.payload
    assert "top_k" not in sent.payload


def test_anthropic_base_url_v1_and_image_payload_are_normalized() -> None:
    transport = FakeTransport()
    provider = _provider(transport, base_url="https://api.example.test/v1")
    images = (
        ImageInput(_PNG, "image/png"),
        ImageInput(b"jpeg-bytes", "image/jpeg"),
    )

    provider.complete(
        CompletionRequest.from_prompt("Compare them", images=images)
    )

    sent = transport.requests[0]
    assert sent.url == "https://api.example.test/v1/messages"
    content = sent.payload["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Image 1:"}
    assert content[1]["type"] == "image"
    assert content[1]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": images[0].base64_data,
    }
    assert content[2] == {"type": "text", "text": "Image 2:"}
    assert content[3]["source"]["media_type"] == "image/jpeg"
    assert content[4] == {"type": "text", "text": "Compare them"}


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "bad model"},
            },
            "bad model",
        ),
        (
            {
                "content": [],
                "stop_reason": "refusal",
                "stop_details": {"type": "refusal", "category": "cyber"},
            },
            "refused the request (cyber)",
        ),
        ({"content": [{"type": "thinking", "thinking": "hidden"}]}, "empty response"),
    ],
)
def test_anthropic_error_refusal_and_empty_responses_fail_closed(
    response: dict[str, Any], message: str
) -> None:
    provider = _provider(FakeTransport(response))

    with pytest.raises(ProviderError, match=message.replace("(", "\\(").replace(")", "\\)")):
        provider.complete(CompletionRequest.from_prompt("hello"))


def test_anthropic_requires_first_non_system_user_message() -> None:
    provider = _provider(FakeTransport())
    request = CompletionRequest(
        messages=(
            ChatMessage.system("Be exact"),
            ChatMessage.assistant("Synthetic preface"),
            ChatMessage.user("Question"),
        )
    )

    with pytest.raises(ProviderError, match="first non-system message"):
        provider.complete(request)


def test_anthropic_settings_defaults_status_and_secret_safety(tmp_path: Path) -> None:
    defaults = Settings.from_env(
        tmp_path,
        environ={"PC_AGENT_ANTHROPIC_MODEL": ""},
    )
    assert defaults.anthropic_model == DEFAULT_ANTHROPIC_MODEL == "claude-sonnet-5-5"
    assert defaults.anthropic_base_url == "https://api.anthropic.com"

    settings = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_PROVIDER_ORDER": "anthropic",
            "ANTHROPIC_API_KEY": _SECRET,
            "PC_AGENT_ANTHROPIC_MODEL": "claude-test",
            "PC_AGENT_ANTHROPIC_BASE_URL": "https://api.example.test",
        },
    )
    status = next(
        item for item in provider_statuses(settings) if item.name == "anthropic"
    )

    assert status.implemented is True
    assert status.configured is True
    assert status.in_route is True
    assert status.model == "claude-test"
    assert _SECRET not in repr(status)
    assert _SECRET not in json.dumps(settings.public_summary())
    assert settings.providers_with_credentials == ("anthropic",)


def test_anthropic_routes_from_settings_and_windows_store_abstraction(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    settings = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_PROVIDER_ORDER": "anthropic",
            "ANTHROPIC_API_KEY": _SECRET,
            "PC_AGENT_ANTHROPIC_MODEL": "claude-test",
            "PC_AGENT_ANTHROPIC_BASE_URL": "https://api.example.test",
        },
    )
    router = build_model_router(settings, transport=transport)
    routed = router.complete(CompletionRequest.from_prompt("hello"))

    assert router.provider_names == ("anthropic",)
    assert routed.response.provider == "anthropic"
    assert routed.response.content == "Anthropic answer"

    backend = InMemoryCredentialBackend()
    store = CredentialStore(backend, mode="auto")
    store.set_api_key("anthropic", _SECRET)
    stored_settings = Settings.from_env(
        tmp_path,
        environ={"PC_AGENT_PROVIDER_ORDER": "anthropic"},
        credential_store=store,
    )
    assert stored_settings.anthropic_api_key == _SECRET
    assert stored_settings.credential_source("anthropic") == "windows-credential-manager"


def test_anthropic_vision_route_and_budget_usage() -> None:
    transport = FakeTransport()
    provider = _provider(transport)
    router = ModelRouter([provider], max_retries=0, backoff_seconds=0)
    tracker = TaskBudgetTracker(TaskBudgetLimits(token_budget=10_000))
    budgeted = BudgetedModelRouter(router, tracker)
    request = CompletionRequest.from_prompt(
        "What color is it?",
        images=(ImageInput(_PNG, "image/png"),),
        max_output_tokens=64,
    )

    routed = budgeted.complete(request)

    assert routed.response.provider == "anthropic"
    assert tracker.snapshot().tokens_reported == 18
    content = transport.requests[0].payload["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[1] == {"type": "text", "text": "What color is it?"}


def test_anthropic_cli_and_gui_visibility(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".env").write_text(
        "PC_AGENT_PROVIDER_ORDER=anthropic\n"
        f"ANTHROPIC_API_KEY={_SECRET}\n"
        "PC_AGENT_ANTHROPIC_MODEL=claude-test\n"
        "PC_AGENT_ALLOW_VISION=1\n",
        encoding="utf-8",
    )

    assert main(["--root", str(tmp_path), "providers", "list"]) == 0
    providers_output = capsys.readouterr().out
    assert "anthropic" in providers_output
    assert "adapter" in providers_output
    assert _SECRET not in providers_output

    settings = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_PROVIDER_ORDER": "anthropic",
            "ANTHROPIC_API_KEY": _SECRET,
            "PC_AGENT_ANTHROPIC_MODEL": "claude-test",
            "PC_AGENT_ALLOW_VISION": "1",
        },
    )
    summary = GuiController(settings).doctor_summary()
    assert summary.version == __version__ == "0.29.0"
    assert summary.vision_enabled is True
    assert summary.vision_providers == "anthropic"
    assert "PHASE 27 PDF Signature Inspection" in summary.to_text()
    assert _SECRET not in summary.to_text()


def test_phase19_scope_and_release_candidates() -> None:
    project_root = Path(__file__).resolve().parents[1]
    candidates = {
        path.as_posix() for path in release_candidate_files(project_root)
    }

    assert __version__ == "0.29.0"
    assert len(build_default_registry().list_tools()) == 62
    assert "src/pc_agent/providers/anthropic.py" in candidates
    assert "docs/phase19.md" in candidates
    assert "tests/test_phase19_anthropic.py" in candidates
