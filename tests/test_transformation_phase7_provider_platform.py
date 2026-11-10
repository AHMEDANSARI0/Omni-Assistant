"""Transformation Phase 7: Provider Platform v2."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from pc_agent.api_server import LocalApiServer
from pc_agent.baseline import TRANSFORMATION_PHASE
from pc_agent.config import Settings
from pc_agent.errors import ConfigError, PlanningError, ProviderError
from pc_agent.gui.controller import GuiController
from pc_agent.provider_platform import (
    capability_catalog,
    load_latest_evaluation,
    load_usage_records,
    normalize_token_usage,
    ordered_provider_names,
    run_evaluations,
    usage_stats,
)
from pc_agent.providers.base import (
    CompletionRequest,
    parse_anthropic_sse_lines,
    parse_ollama_stream_lines,
    parse_openai_sse_lines,
)
from pc_agent.providers.mock import MockProvider
from pc_agent.providers.openai_compatible import OpenAICompatibleProvider
from pc_agent.providers.router import ModelRouter, build_model_router
from pc_agent.storage.db import Database


def test_phase7_identity():
    assert TRANSFORMATION_PHASE == 18


def test_stream_line_parsers():
    openai_lines = [
        ': keep-alive',
        'data: {"choices": [{"delta": {"content": "Hel"}}]}',
        'data: {"choices": [{"delta": {"content": "lo"}}]}',
        'data: [DONE]',
        'data: {"choices": [{"delta": {"content": "!"}}]}',
    ]
    assert list(parse_openai_sse_lines(openai_lines)) == ["Hel", "lo", "!"]
    with pytest.raises(ProviderError):
        list(parse_openai_sse_lines(['data: {"error": {"message": "boom"}}']))

    ollama_lines = [
        '{"message": {"content": "A"}, "done": false}',
        '{"message": {"content": "B"}, "done": true}',
        '{"message": {"content": "C"}, "done": true}',
    ]
    assert list(parse_ollama_stream_lines(ollama_lines)) == ["A", "B"]

    anthropic_lines = [
        'event: message_start',
        'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "X"}}',
        'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Y"}}',
    ]
    assert list(parse_anthropic_sse_lines(anthropic_lines)) == ["X", "Y"]


def test_token_usage_normalization():
    request = CompletionRequest.from_prompt("hello world")
    tokens_in, tokens_out, estimated = normalize_token_usage(
        {"prompt_tokens": 11, "completion_tokens": 7},
        request=request,
        content="answer",
    )
    assert (tokens_in, tokens_out, estimated) == (11, 7, False)

    tokens_in, tokens_out, estimated = normalize_token_usage(
        {"input_tokens": 5, "total_tokens": 9}, request=request, content="answer"
    )
    assert (tokens_in, tokens_out, estimated) == (5, 4, False)

    tokens_in, tokens_out, estimated = normalize_token_usage(
        {}, request=request, content="12345678"
    )
    assert tokens_in > 0 and tokens_out == 2 and estimated is True


def test_streaming_router_and_usage_recording(tmp_path):
    settings = Settings.from_env(tmp_path)
    settings.ensure_directories()
    router = build_model_router(settings)
    text = "".join(router.stream_complete(CompletionRequest.from_prompt("Say BANANA")))
    assert "BANANA" in text
    assert text == router.complete(CompletionRequest.from_prompt("Say BANANA")).response.content

    records = load_usage_records(settings)
    assert len(records) == 2
    assert records[0]["streamed"] is True
    assert records[0]["tokens_estimated"] is True
    assert "Say BANANA" not in json.dumps(records)  # prompts are never stored

    stats = usage_stats(records)
    assert stats["calls"] == 2 and stats["success_rate"] == 1.0
    assert stats["by_model"][0]["provider"] == "mock"

    disabled = replace(settings, provider_analytics_enabled=False)
    build_model_router(disabled).complete(CompletionRequest.from_prompt("Say BANANA"))
    assert len(load_usage_records(settings)) == 2  # nothing new was written


def test_streaming_adapter_with_injected_transport():
    lines = [
        'data: {"choices": [{"delta": {"content": "stream"}}]}',
        'data: {"choices": [{"delta": {"content": "ed"}}]}',
        "data: [DONE]",
    ]
    seen = {}

    def stream_transport(request):
        seen["stream"] = request.payload.get("stream")
        return iter(lines)

    provider = OpenAICompatibleProvider(
        name="openai",
        api_key="test-key",
        model="test-model",
        base_url="https://example.invalid/v1",
        timeout_seconds=5,
        stream_transport=stream_transport,
    )
    router = ModelRouter([provider], max_retries=0)
    assert "".join(router.stream_complete(CompletionRequest.from_prompt("hi"))) == "streamed"
    assert seen["stream"] is True


def test_streaming_fallback_rules():
    class EmptyStreamer:
        name = "empty"
        model = "empty-1"
        implemented = True
        supports_vision = False
        supports_streaming = True

        @property
        def configured(self):
            return True

        def complete(self, request):  # pragma: no cover - unused here
            raise ProviderError("nope")

        def stream_complete(self, request):
            return iter(())

    router = ModelRouter([EmptyStreamer(), MockProvider()], max_retries=0)
    text = "".join(router.stream_complete(CompletionRequest.from_prompt("Say BANANA")))
    assert "BANANA" in text  # fell back before any delta was emitted

    class PartialStreamer(EmptyStreamer):
        name = "partial"

        def stream_complete(self, request):
            yield "half"
            raise ProviderError("socket died")

    router = ModelRouter([PartialStreamer(), MockProvider()], max_retries=0)
    stream = router.stream_complete(CompletionRequest.from_prompt("Say BANANA"))
    with pytest.raises(ProviderError, match="partial output"):
        list(stream)


def test_strategy_and_pricing_settings(tmp_path):
    with pytest.raises(ConfigError):
        Settings.from_env(tmp_path, environ={"PC_AGENT_PROVIDER_STRATEGY": "magic"})
    with pytest.raises(ConfigError):
        Settings.from_env(tmp_path, environ={"PC_AGENT_PROVIDER_PRICES": "{not json"})
    with pytest.raises(ConfigError):
        Settings.from_env(
            tmp_path,
            environ={"PC_AGENT_PROVIDER_PRICES": '{"mock": {"input_per_1k": -1}}'},
        )

    settings = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_PROVIDER_ORDER": "openai,ollama,anthropic",
            "PC_AGENT_PROVIDER_STRATEGY": "local-first",
            "PC_AGENT_PROVIDER_PRICES": (
                '{"anthropic": {"input_per_1k": 0.003, "output_per_1k": 0.015},'
                ' "openai": {"input_per_1k": 0.001, "output_per_1k": 0.002}}'
            ),
            "PC_AGENT_MODEL_CONTEXT_TOKENS": '{"ollama": 8192}',
        },
    )
    assert ordered_provider_names(settings) == ["ollama", "openai", "anthropic"]
    cheapest = replace(settings, provider_strategy="cheapest")
    assert ordered_provider_names(cheapest) == ["openai", "anthropic", "ollama"]

    catalog = {
        row["provider"]: row
        for row in capability_catalog(settings, build_model_router(settings))
    }
    assert catalog["ollama"]["local"] is True
    assert catalog["ollama"]["context_tokens"] == 8192
    assert catalog["openai"]["priced"] is True  # priced by user config above
    assert catalog["ollama"]["priced"] is False  # no price configured for it


def test_evaluations_with_mock(tmp_path):
    settings = Settings.from_env(tmp_path)
    settings.ensure_directories()
    report = run_evaluations(settings, build_model_router(settings))
    assert report["passed"] == report["total"] == 4
    assert report["score"] == 1.0
    latest = load_latest_evaluation(settings)
    assert latest is not None and latest["passed"] == 4
    assert len(load_usage_records(settings)) == 4  # eval calls are metered too


def test_service_and_api_platform(tmp_path):
    settings = replace(
        Settings.from_env(tmp_path), api_port=0, scheduler_interval_seconds=0
    )
    server = LocalApiServer(settings)
    server.start()
    try:
        host, port = server.address
        token = server.token

        def call(method, path, body=None, auth=True):
            data = json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(
                f"http://{host}:{port}{path}", data=data, method=method
            )
            if auth:
                request.add_header("Authorization", "Bearer " + token)
            if data is not None:
                request.add_header("Content-Type", "application/json")
            return urllib.request.urlopen(request, timeout=30)

        with call("GET", "/v1/providers") as response:
            providers = json.loads(response.read())["data"]
        assert providers["strategy"] == "default"
        assert providers["route"] == ["mock"]

        with call("GET", "/v1/providers/capabilities") as response:
            caps = json.loads(response.read())["data"]["capabilities"]
        assert caps[0]["streaming"] is True and caps[0]["local"] is True

        with call("POST", "/v1/providers/evaluations/run", {}) as response:
            report = json.loads(response.read())["data"]
        assert report["passed"] == 4

        with call("POST", "/v1/providers/stream", {"prompt": "Say BANANA"}) as response:
            events = [json.loads(line) for line in response if line.strip()]
        assert events[0]["type"] == "start"
        assert events[-1]["type"] == "done"
        text = "".join(e.get("text", "") for e in events if e["type"] == "delta")
        assert "BANANA" in text

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            call("POST", "/v1/providers/stream", {"prompt": "x"}, auth=False)
        assert excinfo.value.code == 401

        with call("GET", "/v1/providers/usage") as response:
            usage = json.loads(response.read())["data"]["usage"]
        assert usage["calls"] >= 5

        with pytest.raises(PlanningError):
            server.service.stream_chat("  ")
    finally:
        server.stop()


def test_platform_cli_and_docs_surfaces(tmp_path, capsys):
    from pc_agent.cli import main

    assert main(["--root", str(tmp_path), "providers", "capabilities"]) == 0
    assert "mock model=offline-mock" in capsys.readouterr().out

    assert main(["--root", str(tmp_path), "providers", "route"]) == 0
    assert "Strategy: default" in capsys.readouterr().out

    assert main(["--root", str(tmp_path), "ask", "Say", "BANANA", "--stream"]) == 0
    assert "BANANA" in capsys.readouterr().out

    assert main(["--root", str(tmp_path), "providers", "evaluate"]) == 0
    assert "Evaluations: 4/4 passed" in capsys.readouterr().out

    assert main(["--root", str(tmp_path), "providers", "usage"]) == 0
    assert "calls=5" in capsys.readouterr().out

    assert main(["--root", str(tmp_path), "providers", "usage", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["usage"]["calls"] == 5

    assert main(["--root", str(tmp_path), "doctor"]) == 0
    assert "Provider platform: strategy=default" in capsys.readouterr().out


def test_diagnostics_release_gui_platform(tmp_path):
    settings = Settings.from_env(tmp_path)
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()

    from pc_agent.diagnostics import build_diagnostics
    from pc_agent.release import audit_install

    report = build_diagnostics(settings, database)
    platform = report["provider_platform"]
    assert platform["route"]["effective_route"] == ["mock"]
    assert platform["capabilities"][0]["streaming"] is True

    audit = audit_install(settings)
    checks = {check.name: check for check in audit.checks}
    assert len(audit.checks) == 34
    assert checks["provider_platform"].status == "pass"

    controller = GuiController(settings, database=database)
    try:
        summary = controller.provider_platform_summary()
        assert summary.startswith("Provider Platform v2: strategy=default")
        evaluation = controller.run_provider_evaluations()
        assert evaluation["passed"] == 4
    finally:
        orchestrator = getattr(controller, "_orchestrator", None)
        if orchestrator is not None:
            orchestrator._safety.close()


def test_web_ui_provider_platform():
    from pc_agent.web_ui import INDEX_HTML

    assert "/v1/providers/stream" in INDEX_HTML
    assert "/v1/providers/usage" in INDEX_HTML
    assert "/v1/providers/evaluations/run" in INDEX_HTML
    assert "apiStream" in INDEX_HTML
