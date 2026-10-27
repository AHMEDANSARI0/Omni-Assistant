"""Application service for the controlled dynamic Tool Builder."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..errors import ToolBuilderError
from ..providers.router import ModelRouter
from ..storage.db import Database
from ..storage.repositories import DynamicToolRecord, DynamicToolRepository, version_key
from ..tools.builtins import build_default_registry
from ..tools.registry import ToolContext, ToolRegistry
from .generator import generate_with_provider, repair_with_provider
from .models import ToolProposal
from .sandbox import invoke_candidate, run_candidate_tests
from .templates import get_template, suggest_template
from .validation import next_patch_version, prepare_proposal, validate_source

MAX_REPAIR_ATTEMPTS = 3
_MAX_IMPORT_BYTES = 250_000
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "build",
    "can",
    "create",
    "for",
    "i",
    "in",
    "is",
    "it",
    "make",
    "me",
    "my",
    "need",
    "of",
    "on",
    "please",
    "that",
    "the",
    "this",
    "to",
    "tool",
    "want",
    "with",
}


class ToolBuilderService:
    def __init__(
        self,
        Database: Database,
        *,
        data_dir: Path,
        model_router: ModelRouter | None = None,
    ) -> None:
        self._db = Database
        self._data_dir = data_dir.expanduser().resolve()
        self._repository = DynamicToolRepository(Database)
        self._model_router = model_router
        self._builtin_ids = {
            tool.spec.id for tool in build_default_registry().list_tools()
        }

    @property
    def repository(self) -> DynamicToolRepository:
        return self._repository

    def suggest(self, requirement: str) -> dict[str, Any]:
        requirement = _clean_requirement(requirement)
        query_words = _search_terms(requirement)
        builtin_matches: list[dict[str, str]] = []
        for tool in build_default_registry().list_tools():
            if _matches_search(
                query_words,
                strong_text=f"{tool.spec.id} {tool.spec.name}",
                weak_text=tool.spec.description,
            ):
                builtin_matches.append(
                    {"tool_id": tool.spec.id, "version": tool.spec.version}
                )

        dynamic_matches = [
            {
                "candidate_key": record.candidate_key,
                "tool_id": record.tool_id,
                "version": record.version,
                "status": record.status,
            }
            for record in self.search_candidates(requirement)
        ]
        return {
            "requirement": requirement,
            "suggested_tool_id": derive_tool_id(requirement),
            "suggested_template": suggest_template(requirement),
            "builtin_matches": builtin_matches,
            "dynamic_matches": dynamic_matches,
        }

    def create_from_template(self, template_id: str) -> DynamicToolRecord:
        try:
            proposal_data = get_template(template_id)
        except ValueError as exc:
            raise ToolBuilderError(str(exc)) from exc
        proposal = ToolProposal.from_dict(proposal_data)
        return self.create_candidate(proposal, provenance=f"template:{template_id}")

    def import_proposal_file(self, path: Path) -> DynamicToolRecord:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise ToolBuilderError(f"Proposal file does not exist: {resolved}")
        if resolved.stat().st_size > _MAX_IMPORT_BYTES:
            raise ToolBuilderError("Proposal JSON file exceeds the 250 KB limit.")
        try:
            data = json.loads(resolved.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ToolBuilderError(f"Could not read proposal file: {exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ToolBuilderError("Proposal file must contain one UTF-8 JSON object.") from exc
        if not isinstance(data, dict):
            raise ToolBuilderError("Proposal file must contain one JSON object.")
        return self.create_candidate(
            ToolProposal.from_dict(data), provenance=f"import:{resolved.name}"
        )

    def generate_candidate(
        self,
        requirement: str,
        *,
        tool_id: str | None = None,
    ) -> DynamicToolRecord:
        requirement = _clean_requirement(requirement)
        if self._model_router is None:
            raise ToolBuilderError("No model router is configured for AI tool generation.")
        selected_tool_id = tool_id or derive_tool_id(requirement)
        proposal, generator_model = generate_with_provider(
            self._model_router, requirement, tool_id=selected_tool_id
        )
        proposal = replace(
            proposal,
            version=self._next_unused_version(proposal.tool_id, proposal.version),
        )
        return self.create_candidate(
            proposal,
            provenance="provider-generated",
            generator_model=generator_model,
        )

    def create_candidate(
        self,
        proposal: ToolProposal,
        *,
        provenance: str,
        generator_model: str | None = None,
        parent_candidate_id: int | None = None,
        repair_attempt: int = 0,
    ) -> DynamicToolRecord:
        normalized, report = prepare_proposal(
            proposal, builtin_tool_ids=self._builtin_ids
        )
        return self._repository.create_candidate(
            normalized,
            static_report=report.to_dict(),
            provenance=provenance,
            generator_model=generator_model,
            parent_candidate_id=parent_candidate_id,
            repair_attempt=repair_attempt,
        )

    def get_candidate(self, reference: str) -> DynamicToolRecord:
        record = self._repository.get_candidate(reference)
        if record is None:
            raise ToolBuilderError(f"Dynamic tool candidate was not found: {reference}")
        return record

    def list_candidates(
        self,
        *,
        tool_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[DynamicToolRecord]:
        return self._repository.list_candidates(
            tool_id=tool_id, status=status, limit=limit
        )

    def search_candidates(self, query: str) -> list[DynamicToolRecord]:
        words = _search_terms(query)
        if not words:
            return []
        results: list[DynamicToolRecord] = []
        for record in self._repository.list_candidates(limit=1000):
            if _matches_search(
                words,
                strong_text=f"{record.tool_id} {record.proposal.name}",
                weak_text=record.proposal.description,
            ):
                results.append(record)
        return results

    def test_candidate(self, reference: str) -> DynamicToolRecord:
        record = self.get_candidate(reference)
        if record.status not in {"proposed", "tested", "test_failed"}:
            raise ToolBuilderError(
                f"Candidate in status '{record.status}' cannot be sandbox-tested."
            )
        if record.static_report.get("passed") is not True:
            report = {
                "passed": False,
                "cases": [
                    {
                        "name": test.name,
                        "passed": False,
                        "expected": dict(test.expected_output),
                        "actual": None,
                        "error": "Static validation failed; sandbox tests were not run",
                        "duration_ms": 0,
                    }
                    for test in record.proposal.test_cases
                ],
            }
            return self._repository.record_test_result(record, passed=False, report=report)

        report = run_candidate_tests(record.proposal, data_dir=self._data_dir)
        return self._repository.record_test_result(
            record, passed=report.passed, report=report.to_dict()
        )

    def approve_candidate(self, reference: str) -> DynamicToolRecord:
        return self._repository.approve_candidate(self.get_candidate(reference))

    def reject_candidate(
        self, reference: str, *, reason: str | None = None
    ) -> DynamicToolRecord:
        return self._repository.reject_candidate(
            self.get_candidate(reference), reason=reason
        )

    def repair_candidate(
        self,
        reference: str,
        *,
        requirement: str | None = None,
    ) -> DynamicToolRecord:
        if self._model_router is None:
            raise ToolBuilderError("No model router is configured for AI tool repair.")
        parent = self.get_candidate(reference)
        if parent.repair_attempt >= MAX_REPAIR_ATTEMPTS:
            raise ToolBuilderError(
                f"Repair limit reached ({MAX_REPAIR_ATTEMPTS}) for this candidate chain. "
                "Create a fresh proposal instead."
            )
        static_failed = parent.static_report.get("passed") is not True
        tests_failed = parent.status == "test_failed"
        if parent.status not in {"proposed", "test_failed"} or not (static_failed or tests_failed):
            raise ToolBuilderError(
                "Only a candidate with failed static validation or failed sandbox tests can be repaired. "
                "Run its tests first when static validation passed."
            )

        repaired, generator_model = repair_with_provider(
            self._model_router,
            requirement=_clean_requirement(
                requirement or f"Repair dynamic tool {parent.tool_id}"
            ),
            proposal=parent.proposal,
            report={
                "static": dict(parent.static_report),
                "tests": dict(parent.test_report or {}),
            },
        )
        # A repair may change implementation details, but it may not weaken the
        # previously declared contract or rewrite expected test outputs.
        repaired = replace(
            repaired,
            version=self._next_unused_version(parent.tool_id, parent.version),
            input_schema=parent.proposal.input_schema,
            output_schema=parent.proposal.output_schema,
            test_cases=parent.proposal.test_cases,
            timeout_seconds=min(repaired.timeout_seconds, parent.proposal.timeout_seconds),
        )
        return self.create_candidate(
            repaired,
            provenance=f"provider-repair:{parent.candidate_key}",
            generator_model=generator_model,
            parent_candidate_id=parent.database_id,
            repair_attempt=parent.repair_attempt + 1,
        )

    def set_enabled(self, tool_id: str, *, enabled: bool) -> DynamicToolRecord:
        return self._repository.set_enabled(tool_id, enabled=enabled)

    def rollback(self, tool_id: str) -> DynamicToolRecord:
        return self._repository.rollback(tool_id)

    def events(self, reference: str) -> list[dict[str, Any]]:
        return self._repository.events(self.get_candidate(reference))

    def runtime_registry(self) -> ToolRegistry:
        return build_runtime_registry(self._db, data_dir=self._data_dir)

    def _next_unused_version(self, tool_id: str, preferred_version: str) -> str:
        existing = {
            record.version
            for record in self._repository.list_candidates(tool_id=tool_id, limit=1000)
        }
        candidate = preferred_version
        while candidate in existing:
            candidate = next_patch_version(candidate)
        if existing and version_key(candidate) <= max(version_key(item) for item in existing):
            candidate = next_patch_version(max(existing, key=version_key))
            while candidate in existing:
                candidate = next_patch_version(candidate)
        return candidate


def build_runtime_registry(Database: Database, *, data_dir: Path) -> ToolRegistry:
    """Built-ins plus persisted approved dynamic tools.

    Disabled dynamic tools are registered as visible-but-disabled; unapproved
    candidates never reach the registry.
    """
    registry = build_default_registry()
    repository = DynamicToolRepository(Database)
    resolved_data_dir = data_dir.expanduser().resolve()

    for record in repository.approved_candidates(include_disabled=True):
        static_report = validate_source(record.proposal.normalized_source)
        if not static_report.passed:
            raise ToolBuilderError(
                f"Approved dynamic tool {record.tool_id}@{record.version} no longer "
                "passes static validation and was not loaded."
            )
        spec = record.proposal.to_tool_spec(
            provenance=f"dynamic:{record.candidate_key}:{record.provenance}"
        )
        registry.register(spec, _handler_for(record, resolved_data_dir))
        if not record.enabled:
            registry.set_enabled(record.tool_id, False, version=record.version)
    return registry


def _handler_for(record: DynamicToolRecord, data_dir: Path):
    def handler(
        arguments: dict[str, Any],
        _context: ToolContext,
        _record: DynamicToolRecord = record,
        _data_dir: Path = data_dir,
    ) -> dict[str, Any]:
        return invoke_candidate(
            tool_id=_record.tool_id,
            source=_record.proposal.normalized_source,
            arguments=arguments,
            timeout_seconds=_record.proposal.timeout_seconds,
            data_dir=_data_dir,
        )

    return handler


def _search_terms(text: str) -> set[str]:
    terms: set[str] = set()
    for raw in re.findall(r"[a-z0-9]+", text.lower()):
        term = raw[:-1] if len(raw) > 3 and raw.endswith("s") else raw
        if term not in _STOP_WORDS and raw not in _STOP_WORDS:
            terms.add(term)
    return terms


def _matches_search(query: set[str], *, strong_text: str, weak_text: str) -> bool:
    """Avoid calling a tool a match merely because one generic word overlaps."""
    strong_terms = _search_terms(strong_text)
    all_terms = strong_terms.union(_search_terms(weak_text))
    return bool(query.intersection(strong_terms)) or len(query.intersection(all_terms)) >= 2


def derive_tool_id(requirement: str) -> str:
    words = [
        word
        for word in re.findall(r"[a-z][a-z0-9]*", requirement.lower())
        if word not in _STOP_WORDS
    ]
    if not words:
        words = ["new_tool"]
    return "custom." + "_".join(words[:4])


def _clean_requirement(requirement: str) -> str:
    cleaned = " ".join(requirement.strip().split())
    if not cleaned:
        raise ToolBuilderError("A tool requirement must not be empty.")
    if len(cleaned) > 2000:
        raise ToolBuilderError("A tool requirement must be at most 2000 characters.")
    return cleaned
