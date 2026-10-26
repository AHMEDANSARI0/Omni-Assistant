from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pc_agent.errors import PlanningError
from pc_agent.planning import DeterministicPlanner, PlannerOptions, plan_with_provider
from pc_agent.providers.base import CompletionResponse
from pc_agent.providers.router import ModelRouter


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "Folder A").mkdir(parents=True)
    (root / "Folder B").mkdir(parents=True)
    return root


def test_deterministic_workspace_folder_merge(workspace: Path) -> None:
    planner = DeterministicPlanner(workspace)

    outcome = planner.plan("Merge PDFs from Folder A and Folder B")

    assert outcome is not None and outcome.plan is not None
    assert outcome.plan.tool_id == "pdf.merge_folders"
    assert outcome.plan.arguments["input_folders"] == ["Folder A", "Folder B"]
    assert outcome.plan.arguments["output_path"] == "merged.pdf"
    assert outcome.plan.external_read_roots == ()


def test_deterministic_desktop_merge_requires_external_scope(tmp_path: Path) -> None:
    home = tmp_path / "home"
    folder_a = home / "Desktop" / "Folder A"
    folder_b = home / "Desktop" / "Folder B"
    folder_a.mkdir(parents=True)
    folder_b.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    outcome = DeterministicPlanner(workspace, home_dir=home).plan(
        "Desktop ke Folder A aur Folder B mein jitni PDFs hain, unko merge kar do"
    )

    assert outcome is not None and outcome.plan is not None
    assert outcome.plan.external_read_roots == (folder_a.resolve(), folder_b.resolve())
    assert outcome.plan.arguments["output_path"] == "merged.pdf"


def test_default_output_is_made_unique_without_overwrite(workspace: Path) -> None:
    (workspace / "merged.pdf").write_bytes(b"existing")
    fixed_now = datetime(2026, 10, 26, 9, 30, tzinfo=UTC)
    planner = DeterministicPlanner(workspace, now=lambda: fixed_now)

    outcome = planner.plan("Merge PDFs from Folder A and Folder B")

    assert outcome is not None and outcome.plan is not None
    assert outcome.plan.arguments["output_path"] == "merged-20261026-093000.pdf"


def test_missing_named_folder_is_needs_input_not_guessed(workspace: Path) -> None:
    outcome = DeterministicPlanner(workspace).plan(
        "Merge PDFs from Folder A and Missing Folder",
        PlannerOptions(input_folders=("Folder A", "Missing Folder")),
    )

    assert outcome is not None
    assert outcome.plan is None
    assert "Missing Folder" in " ".join(outcome.missing_information)


def test_deterministic_verify_and_list_plans(workspace: Path) -> None:
    (workspace / "merged.pdf").write_bytes(b"fake for planner existence only")
    planner = DeterministicPlanner(workspace)

    verify = planner.plan("Verify merged.pdf has 6 pages")
    listing = planner.plan("List PDF files in Folder A")

    assert verify is not None and verify.plan is not None
    assert verify.plan.tool_id == "pdf.verify"
    assert verify.plan.expected_pages == 6
    assert listing is not None and listing.plan is not None
    assert listing.plan.arguments["pattern"] == "*.pdf"


class StaticProvider:
    implemented = True

    def __init__(self, content: str) -> None:
        self.name = "fake-ai"
        self.model = "fake-model"
        self.content = content

    @property
    def configured(self) -> bool:
        return True

    def complete(self, request: object) -> CompletionResponse:
        return CompletionResponse(self.content, self.name, self.model)


def test_ai_plan_is_validated_before_it_can_execute() -> None:
    content = json.dumps(
        {
            "intent": "pdf.merge_folders",
            "tool_id": "pdf.merge_folders",
            "arguments": {
                "input_folders": ["Folder A", "Folder B"],
                "output_path": "ai-merged.pdf",
                "recursive": False,
            },
            "expected_pages": None,
            "summary": "Merge two folders",
            "missing_information": [],
        }
    )
    outcome = plan_with_provider(
        ModelRouter([StaticProvider(content)], backoff_seconds=0),
        "Please combine the documents as discussed",
        PlannerOptions(),
    )

    assert outcome.plan is not None
    assert outcome.plan.planner == "ai:fake-ai:fake-model"
    assert outcome.provider_attempts[0].success is True


def test_ai_plan_rejects_absolute_paths_and_disallowed_tools() -> None:
    absolute = json.dumps(
        {
            "intent": "pdf.merge_folders",
            "tool_id": "pdf.merge_folders",
            "arguments": {
                "input_folders": ["C:/Users/test/Desktop"],
                "output_path": "merged.pdf",
            },
            "expected_pages": None,
            "summary": "Unsafe",
            "missing_information": [],
        }
    )
    with pytest.raises(PlanningError, match="workspace-relative"):
        plan_with_provider(
            ModelRouter([StaticProvider(absolute)], backoff_seconds=0),
            "Do it",
            PlannerOptions(),
        )

    disallowed = json.dumps(
        {
            "intent": "pdf.merge",
            "tool_id": "workspace.delete",
            "arguments": {},
            "expected_pages": None,
            "summary": "Unsafe",
            "missing_information": [],
        }
    )
    with pytest.raises(PlanningError, match="allow-listed"):
        plan_with_provider(
            ModelRouter([StaticProvider(disallowed)], backoff_seconds=0),
            "Do it",
            PlannerOptions(),
        )
