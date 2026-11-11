"""Phase 53: explicit direct live Anthropic verification evidence."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from pc_agent import __version__
from pc_agent import cli as cli_module
from pc_agent.anthropic_verification import (
    ANTHROPIC_VERIFICATION_EXPECTED_RESPONSE,
    run_anthropic_live_verification,
    verify_anthropic_live_verification_report,
)
from pc_agent.cli import build_parser, main
from pc_agent.config import Settings
from pc_agent.errors import PcAgentError, ProviderError
from pc_agent.providers.base import HttpJsonRequest
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[1]
_SECRET = "phase53-anthropic-secret"


class FakeTransport:
    def __init__(
        self,
        *,
        content: str = ANTHROPIC_VERIFICATION_EXPECTED_RESPONSE,
        model: str = "claude-test",
        usage: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.content = content
        self.model = model
        self.usage = (
            {"input_tokens": 21, "output_tokens": 7} if usage is None else usage
        )
        self.error = error
        self.requests: list[HttpJsonRequest] = []

    def __call__(self, request: HttpJsonRequest) -> dict[str, Any]:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return {
            "model": self.model,
            "content": [{"type": "text", "text": self.content}],
            "usage": self.usage,
        }


def _settings(
    tmp_path: Path,
    *,
    secret: str | None = _SECRET,
    base_url: str = "https://api.example.test",
) -> Settings:
    environ = {
        "PC_AGENT_PROVIDER_ORDER": "anthropic",
        "PC_AGENT_ANTHROPIC_MODEL": "claude-test",
        "PC_AGENT_ANTHROPIC_BASE_URL": base_url,
    }
    if secret is not None:
        environ["ANTHROPIC_API_KEY"] = secret
    return Settings.from_env(tmp_path, environ=environ)


def test_phase53_direct_verification_and_report_round_trip(tmp_path: Path) -> None:
    transport = FakeTransport()
    report_path = tmp_path / "evidence" / "anthropic.json"

    report = run_anthropic_live_verification(
        _settings(tmp_path), report_path, transport=transport
    )

    assert report.status == "passed"
    assert report.network_call_made is True
    assert report.requested_model == "claude-test"
    assert report.response_model == "claude-test"
    assert report.endpoint_origin == "https://api.example.test"
    assert report.credential_source == "environment"
    assert report.input_tokens == 21
    assert report.output_tokens == 7
    assert all(check.status == "passed" for check in report.checks)

    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.url == "https://api.example.test/v1/messages"
    assert request.headers["Authorization"] == f"Bearer {_SECRET}"
    assert request.payload["model"] == "claude-test"
    assert request.payload["max_tokens"] == 32
    assert request.payload["stream"] is False
    assert ANTHROPIC_VERIFICATION_EXPECTED_RESPONSE in str(request.payload)
    assert "image" not in str(request.payload).lower()

    raw = report_path.read_text(encoding="utf-8")
    assert _SECRET not in raw
    assert '"content"' not in raw
    payload = json.loads(raw)
    assert payload["fallback_used"] is False
    assert payload["retries_used"] == 0
    assert payload["vision_status"] == "not_tested"
    assert payload["response"]["sha256"] == report.response_sha256

    verification = verify_anthropic_live_verification_report(report_path)
    assert verification.status == "passed"
    assert verification.response_model == "claude-test"
    assert verification.report_sha256 == payload["report_sha256"]

    payload["response"]["model"] = "tampered-model"
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PcAgentError, match="hash does not match"):
        verify_anthropic_live_verification_report(report_path)


def test_phase53_missing_key_and_unsafe_endpoint_make_no_network_call(
    tmp_path: Path,
) -> None:
    missing_transport = FakeTransport()
    report = run_anthropic_live_verification(
        _settings(tmp_path, secret=None),
        tmp_path / "missing.json",
        transport=missing_transport,
    )
    assert report.status == "failed"
    assert report.network_call_made is False
    assert missing_transport.requests == []
    assert report.check("credential_present").status == "failed"
    with pytest.raises(PcAgentError, match="does not record a passed run"):
        verify_anthropic_live_verification_report(tmp_path / "missing.json")

    unsafe_settings = _settings(tmp_path)
    unsafe_settings.anthropic_base_url = "http://api.example.test"
    unsafe_transport = FakeTransport()
    unsafe_report = run_anthropic_live_verification(
        unsafe_settings,
        tmp_path / "unsafe.json",
        transport=unsafe_transport,
    )
    assert unsafe_report.status == "failed"
    assert unsafe_report.network_call_made is False
    assert unsafe_report.check("endpoint_allowed").status == "failed"
    assert unsafe_transport.requests == []


def test_phase53_failures_are_failed_reports_and_never_leak_secret(
    tmp_path: Path,
) -> None:
    error_transport = FakeTransport(
        error=ProviderError(f"provider rejected {_SECRET} during the call")
    )
    error_path = tmp_path / "provider-error.json"
    error_report = run_anthropic_live_verification(
        _settings(tmp_path), error_path, transport=error_transport
    )
    assert error_report.status == "failed"
    assert error_report.network_call_made is True
    assert error_report.check("live_response_received").status == "failed"
    assert _SECRET not in error_path.read_text(encoding="utf-8")
    assert _SECRET not in str(error_report.error)

    wrong = run_anthropic_live_verification(
        _settings(tmp_path),
        tmp_path / "wrong-token.json",
        transport=FakeTransport(content="PC_AGENT_OTHER_OK"),
    )
    assert wrong.status == "failed"
    assert wrong.check("expected_response").status == "failed"

    missing_usage = run_anthropic_live_verification(
        _settings(tmp_path),
        tmp_path / "missing-usage.json",
        transport=FakeTransport(usage={}),
    )
    assert missing_usage.status == "failed"
    assert missing_usage.check("usage_reported").status == "failed"


def test_phase53_existing_report_is_refused_before_network(tmp_path: Path) -> None:
    report_path = tmp_path / "already-there.json"
    report_path.write_text("{}\n", encoding="utf-8")
    transport = FakeTransport()

    with pytest.raises(PcAgentError, match="already exists"):
        run_anthropic_live_verification(
            _settings(tmp_path), report_path, transport=transport
        )
    assert transport.requests == []


def test_phase53_cli_direct_run_and_offline_report_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    help_text = build_parser().format_help()
    assert "providers" in help_text

    transport = FakeTransport()

    def fake_run(settings: Settings, report_path: Path | None):
        return run_anthropic_live_verification(
            settings, report_path, transport=transport
        )

    monkeypatch.setattr(
        cli_module, "run_anthropic_live_verification", fake_run
    )
    (tmp_path / ".env").write_text(
        "PC_AGENT_PROVIDER_ORDER=anthropic\n"
        f"ANTHROPIC_API_KEY={_SECRET}\n"
        "PC_AGENT_ANTHROPIC_MODEL=claude-test\n"
        "PC_AGENT_ANTHROPIC_BASE_URL=https://api.example.test\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "cli-report.json"

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "providers",
                "verify-anthropic",
                "--report",
                str(report_path),
                "--json",
            ]
        )
        == 0
    )
    run_output = capsys.readouterr().out
    assert json.loads(run_output)["status"] == "passed"
    assert _SECRET not in run_output
    assert len(transport.requests) == 1

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "providers",
                "verify-anthropic-report",
                str(report_path),
                "--json",
            ]
        )
        == 0
    )
    verify_output = capsys.readouterr().out
    assert json.loads(verify_output)["status"] == "passed"
    assert _SECRET not in verify_output


def test_phase53_scope_inventory_and_documentation() -> None:
    assert __version__ == "0.82.17"
    assert LATEST_SCHEMA_VERSION == 18
    assert len(build_default_registry().list_tools()) == 69

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == __version__
    assert project["project"]["dependencies"] == [
        "certifi>=2024.7.4",
        "cryptography>=42,<51",
        "pypdf[crypto]>=5.0,<7",
        "pypdfium2>=5,<6",
        "Pillow>=10.4,<14",
        "comtypes>=1.4,<2; sys_platform == 'win32'",
    ]

    module = (ROOT / "src/pc_agent/anthropic_verification.py").read_text(
        encoding="utf-8"
    )
    cli = (ROOT / "src/pc_agent/cli.py").read_text(encoding="utf-8")
    phase = (ROOT / "docs/phase53.md").read_text(encoding="utf-8")
    phase19 = (ROOT / "docs/phase19.md").read_text(encoding="utf-8")
    providers_doc = (ROOT / "docs/providers.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tools_doc = (ROOT / "docs/tools.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert module.count("AnthropicProvider(") == 1
    assert "build_model_router" not in module
    assert "allow_fallback" not in module
    assert "ImageInput" not in module
    assert '"fallback_used": False' in module
    assert '"retries_used": 0' in module
    assert "verify-anthropic" in cli
    assert "verify-anthropic-report" in cli

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "src/pc_agent/anthropic_verification.py",
        "docs/phase53.md",
        "tests/test_phase53_live_anthropic_verification.py",
    ):
        assert expected in release_names

    for text in (phase, phase19, providers_doc, readme, tools_doc):
        assert "Live Anthropic Verification" in text
    assert ANTHROPIC_VERIFICATION_EXPECTED_RESPONSE in phase
    assert "vision_status: not_tested" in phase
    assert "no fallback" in phase.lower()
    assert "## 0.55.0 - PHASE 53 Live Anthropic Verification" in changelog
