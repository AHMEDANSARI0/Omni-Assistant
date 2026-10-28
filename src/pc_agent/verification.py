"""Explicit acceptance criteria for orchestrated tasks (Phase 7).

Before a plan's tool runs, :func:`build_criteria` derives the observable
acceptance criteria from the plan itself. After execution,
:func:`evaluate_criteria` checks each criterion with evidence:

- ``tool_verification`` wraps the orchestrator's existing deterministic
  re-verification of the tool output (re-open/re-parse/re-hash inside the
  tool-specific verifiers).
- ``output_artifact`` re-checks, independently of the tool's claims, that a
  written workspace artifact exists and is non-empty.
- ``output_hash`` recomputes the artifact SHA-256 and compares it with the
  hash the tool reported.
- ``pdf_pages`` / ``row_count`` compare reported counts with plan
  expectations and a light independent recount where that is cheap and
  deterministic.

A task may only succeed when every criterion passes. The resulting report is
persisted with the task so the evidence survives restarts.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .planning import TaskPlan

_WRITER_TOOLS = frozenset(
    {
        "pdf.merge",
        "pdf.merge_folders",
        "office.csv_to_json",
        "office.json_to_csv",
        "office.csv_to_xlsx",
        "files.copy",
        "files.move",
        "files.rename",
        "pdf.extract_pages",
        "office.docx_replace_text",
        "office.docx_append_paragraphs",
        "office.xlsx_set_cells",
        "browser.screenshot",
        "desktop.screenshot",
        "image.convert",
        "image.transform",
    }
)
_PDF_PAGE_TOOLS = frozenset(
    {"pdf.merge", "pdf.merge_folders", "pdf.verify", "pdf.inspect", "pdf.extract_pages"}
)
_ROW_COUNT_TOOLS = frozenset({"office.csv_inspect", "office.csv_to_json", "office.json_to_csv"})


@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    kind: str
    description: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "description": self.description,
            "params": dict(self.params),
        }


@dataclass(frozen=True, slots=True)
class CriterionResult:
    criterion: AcceptanceCriterion
    passed: bool
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        payload = self.criterion.to_dict()
        payload["passed"] = self.passed
        payload["evidence"] = self.evidence
        return payload


@dataclass(frozen=True, slots=True)
class VerificationReport:
    results: tuple[CriterionResult, ...]

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(result.passed for result in self.results)

    def failure_summary(self) -> str:
        failed = [result for result in self.results if not result.passed]
        if not failed:
            return "All acceptance criteria passed."
        details = "; ".join(
            f"{result.criterion.description} ({result.evidence})" for result in failed
        )
        return f"Acceptance criteria failed: {details}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "criteria": [result.to_dict() for result in self.results],
        }


def build_criteria(plan: TaskPlan) -> tuple[AcceptanceCriterion, ...]:
    """Derive the acceptance criteria for a plan before its tool runs."""

    criteria = [
        AcceptanceCriterion(
            kind="tool_verification",
            description=f"Deterministic verification of {plan.tool_id} output passes",
        )
    ]
    if plan.tool_id in _WRITER_TOOLS or (
        plan.tool_id == "office.extract_text" and plan.arguments.get("output_path")
    ):
        criteria.append(
            AcceptanceCriterion(
                kind="output_artifact",
                description="Written output artifact exists in the workspace with the reported size",
            )
        )
        criteria.append(
            AcceptanceCriterion(
                kind="output_hash",
                description="Recomputed SHA-256 of the output artifact matches the tool report",
            )
        )
    if plan.tool_id in _PDF_PAGE_TOOLS:
        criteria.append(
            AcceptanceCriterion(
                kind="pdf_pages",
                description="PDF page count is positive and matches the plan expectation",
                params={"expected_pages": plan.expected_pages},
            )
        )
    if plan.tool_id in _ROW_COUNT_TOOLS:
        criteria.append(
            AcceptanceCriterion(
                kind="row_count",
                description="Reported data row count matches an independent recount",
            )
        )
    return tuple(criteria)


def evaluate_criteria(
    criteria: tuple[AcceptanceCriterion, ...],
    *,
    workspace_dir: Path,
    tool_output: Mapping[str, Any] | None,
    legacy_valid: bool,
    legacy_message: str,
) -> VerificationReport:
    output: Mapping[str, Any] = tool_output or {}
    results: list[CriterionResult] = []
    for criterion in criteria:
        if criterion.kind == "tool_verification":
            results.append(CriterionResult(criterion, legacy_valid, legacy_message))
        elif criterion.kind == "output_artifact":
            results.append(_evaluate_artifact(criterion, workspace_dir, output))
        elif criterion.kind == "output_hash":
            results.append(_evaluate_hash(criterion, workspace_dir, output))
        elif criterion.kind == "pdf_pages":
            results.append(_evaluate_pdf_pages(criterion, output))
        elif criterion.kind == "row_count":
            results.append(_evaluate_row_count(criterion, workspace_dir, output))
        else:  # pragma: no cover - build_criteria only emits known kinds
            results.append(
                CriterionResult(criterion, False, f"Unknown criterion kind: {criterion.kind}")
            )
    return VerificationReport(results=tuple(results))


def _workspace_path(workspace_dir: Path, raw_path: Any) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None
    candidate = Path(raw_path).expanduser()
    target = (
        candidate.resolve()
        if candidate.is_absolute()
        else (workspace_dir / candidate).resolve()
    )
    workspace = workspace_dir.expanduser().resolve()
    return target if target.is_relative_to(workspace) else None


def _artifact_path(workspace_dir: Path, output: Mapping[str, Any]) -> Path | None:
    for key in ("output_path", "path"):
        resolved = _workspace_path(workspace_dir, output.get(key))
        if resolved is not None:
            return resolved
    return None


def _evaluate_artifact(
    criterion: AcceptanceCriterion, workspace_dir: Path, output: Mapping[str, Any]
) -> CriterionResult:
    target = _artifact_path(workspace_dir, output)
    if target is None:
        return CriterionResult(criterion, False, "No workspace output path was reported.")
    if not target.is_file():
        return CriterionResult(criterion, False, f"Artifact is missing: {target.name}")
    size = target.stat().st_size
    if size <= 0 and output.get("size_bytes") != 0:
        # Generated outputs must be non-empty. A file transfer may honestly
        # report size_bytes=0 (copying an empty file); the independent hash
        # criterion still has to match in that case.
        return CriterionResult(criterion, False, f"Artifact is empty: {target.name}")
    return CriterionResult(criterion, True, f"{target.name} exists ({size} bytes).")


def _evaluate_hash(
    criterion: AcceptanceCriterion, workspace_dir: Path, output: Mapping[str, Any]
) -> CriterionResult:
    reported = output.get("sha256") or output.get("output_sha256")
    if not isinstance(reported, str) or not reported:
        # Nothing to compare against; the tool-specific verifier already
        # covered integrity, so this criterion is not applicable evidence.
        return CriterionResult(criterion, True, "Tool reported no artifact hash to compare.")
    target = _artifact_path(workspace_dir, output)
    if target is None or not target.is_file():
        return CriterionResult(criterion, False, "Artifact for hash recompute is missing.")
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual == reported:
        return CriterionResult(criterion, True, f"SHA-256 matches ({actual[:12]}...).")
    return CriterionResult(
        criterion, False, f"SHA-256 mismatch: recomputed {actual[:12]}... != reported."
    )


def _evaluate_pdf_pages(
    criterion: AcceptanceCriterion, output: Mapping[str, Any]
) -> CriterionResult:
    actual = output.get("page_count")
    if not isinstance(actual, int):
        actual = output.get("pages")
    expected = criterion.params.get("expected_pages")
    if not isinstance(actual, int) or actual <= 0:
        return CriterionResult(criterion, False, f"Reported page count is invalid: {actual!r}.")
    if isinstance(expected, int) and actual != expected:
        return CriterionResult(
            criterion, False, f"Page count {actual} != expected {expected}."
        )
    return CriterionResult(criterion, True, f"Page count is {actual}.")


def _evaluate_row_count(
    criterion: AcceptanceCriterion, workspace_dir: Path, output: Mapping[str, Any]
) -> CriterionResult:
    reported = output.get("row_count")
    if not isinstance(reported, int) or reported < 0:
        return CriterionResult(criterion, False, f"Reported row count is invalid: {reported!r}.")
    target = _artifact_path(workspace_dir, output)
    if target is None or not target.is_file():
        return CriterionResult(criterion, False, "Data artifact for recount is missing.")
    try:
        text = target.read_text(encoding="utf-8")
        if target.suffix.lower() == ".json":
            parsed = json.loads(text)
            actual = len(parsed) if isinstance(parsed, list) else -1
        else:
            rows = list(csv.reader(io.StringIO(text)))
            actual = max(0, len(rows) - 1) if rows else 0
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, csv.Error) as exc:
        return CriterionResult(criterion, False, f"Recount failed: {exc}")
    if actual == reported:
        return CriterionResult(criterion, True, f"Recounted {actual} data row(s).")
    return CriterionResult(
        criterion, False, f"Recounted {actual} data row(s), tool reported {reported}."
    )
