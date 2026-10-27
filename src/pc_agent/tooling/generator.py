"""Provider-backed candidate generation and bounded repair.

The provider returns untrusted text. It is parsed as strict JSON and then sent
through the same proposal/schema/AST/subprocess pipeline as every other source.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..errors import ProviderError, ToolBuilderError
from ..providers.base import CompletionRequest
from ..providers.router import ModelRouter
from .models import ToolProposal
from .validation import ALLOWED_IMPORTS

_ALLOWED_IMPORT_TEXT = ", ".join(sorted(ALLOWED_IMPORTS))


def generate_with_provider(
    router: ModelRouter,
    requirement: str,
    *,
    tool_id: str,
) -> tuple[ToolProposal, str]:
    response = _request(router, _generation_prompt(requirement, tool_id))
    return _parse_response(response.content, expected_tool_id=tool_id), (
        f"{response.provider}:{response.model}"
    )


def repair_with_provider(
    router: ModelRouter,
    *,
    requirement: str,
    proposal: ToolProposal,
    report: Mapping[str, Any],
) -> tuple[ToolProposal, str]:
    response = _request(
        router,
        _repair_prompt(
            requirement=requirement,
            proposal=proposal,
            report=report,
        ),
    )
    return _parse_response(response.content, expected_tool_id=proposal.tool_id), (
        f"{response.provider}:{response.model}"
    )


def _request(router: ModelRouter, prompt: str) -> Any:
    has_real_provider = any(
        provider.name != "mock" and provider.configured and provider.implemented
        for provider in router.providers
    )
    if not has_real_provider:
        raise ToolBuilderError(
            "AI tool generation needs a configured real provider. Configure Gemini, "
            "OpenRouter, OpenAI or local Ollama in .env, or use a template/import first."
        )
    try:
        routed = router.complete(
            CompletionRequest.from_prompt(
                prompt,
                system_instruction=(
                    "You propose untrusted code for later deterministic validation. "
                    "Return JSON only and never claim it has been approved or executed."
                ),
                temperature=0,
                max_output_tokens=6000,
                json_mode=True,
            )
        )
    except ProviderError as exc:
        raise ToolBuilderError(f"AI tool generation failed: {exc}") from exc
    if routed.response.provider == "mock":
        raise ToolBuilderError(
            "The configured real provider did not produce a usable tool proposal; "
            "the offline mock cannot generate tools."
        )
    return routed.response


def _generation_prompt(requirement: str, tool_id: str) -> str:
    return f"""Create one JSON tool proposal for this missing pure-compute capability.

Requirement:
{requirement}

Return ONLY one JSON object with exactly these fields:
tool_id, name, version, description, input_schema, output_schema,
source_code, test_cases, dependencies, timeout_seconds, permissions,
network_required.

Mandatory constraints:
- tool_id must be exactly {tool_id!r}; version must be "1.0.0".
- permissions must be [] and network_required must be false.
- Python source must define exactly run(arguments) and return a JSON object.
- Only these imports are allowed: {_ALLOWED_IMPORT_TEXT}.
- No filesystem, network, shell, classes, decorators, dynamic imports,
  eval/exec/open/print/getattr, dunder/private access, or top-level side effects.
- Only constant defaults. Use f-strings instead of .format(...).
- Schemas use only type/properties/required/items and simple min/max fields.
- dependencies must exactly equal the imported top-level modules.
- Include at least two exact test_cases with name, arguments and expected_output.
- Keep source under 120 lines."""


def _repair_prompt(
    *,
    requirement: str,
    proposal: ToolProposal,
    report: Mapping[str, Any],
) -> str:
    previous = json.dumps(proposal.to_dict(), indent=2)[:16_000]
    failure = json.dumps(report, indent=2)[:8_000]
    return f"""Repair this untrusted dynamic-tool proposal and return the complete JSON object again.

Original requirement:
{requirement}

Keep tool_id exactly {proposal.tool_id!r}. Keep the same public purpose and
schema unless the failure report proves a schema/test correction is required.
Do not add permissions, filesystem/network/shell access, dependencies, or new
capabilities. Allowed imports only: {_ALLOWED_IMPORT_TEXT}.

Previous proposal:
{previous}

Validation/test failure report:
{failure}

Return JSON only, with the same field set and at least the same useful tests.
The deterministic validator will reject anything unsafe or unproven."""


def _parse_response(text: str, *, expected_tool_id: str) -> ToolProposal:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        data = json.loads(candidate)
    except (TypeError, ValueError) as exc:
        raise ToolBuilderError("AI provider did not return a strict JSON tool proposal.") from exc
    if not isinstance(data, dict):
        raise ToolBuilderError("AI provider tool proposal must be a JSON object.")
    proposal = ToolProposal.from_dict(data)
    if proposal.tool_id != expected_tool_id:
        raise ToolBuilderError(f"AI provider changed the tool id; expected '{expected_tool_id}'.")
    return proposal
