from __future__ import annotations

import json
from pathlib import Path

from pc_agent.config import Settings
from pc_agent.documents.pdf import create_demo_pdfs, write_blank_pdf
from pc_agent.domain.task import Task, TaskStatus
from pc_agent.orchestrator import TaskOrchestrator
from pc_agent.planning import DeterministicPlanner, PlannerOptions
from pc_agent.providers.base import CompletionResponse
from pc_agent.providers.router import ModelRouter
from pc_agent.storage.db import Database
from pc_agent.storage.repositories import ExecutionRepository, TaskRepository
from pc_agent.tools import build_default_registry


def _setup(tmp_path: Path, *, router: ModelRouter | None = None):
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    tasks = TaskRepository(database)
    execution = ExecutionRepository(database)
    orchestrator = TaskOrchestrator(
        settings=settings,
        task_repository=tasks,
        execution_repository=execution,
        tool_registry=build_default_registry(),
        deterministic_planner=DeterministicPlanner(settings.workspace_dir),
        model_router=router,
    )
    return settings, tasks, execution, orchestrator


def test_orchestrated_pdf_merge_moves_through_verifying_to_succeeded(tmp_path: Path) -> None:
    settings, tasks, execution, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)
    task = tasks.create(Task(instruction="Merge PDFs from Folder A and Folder B"))

    report = orchestrator.execute(task.id)

    assert report.success, report.error
    assert report.task.status is TaskStatus.SUCCEEDED
    assert (settings.workspace_dir / "merged.pdf").is_file()
    assert [item.tool_id for item in execution.list_tool_invocations(task.id)] == [
        "pdf.merge_folders",
        "pdf.verify",
    ]
    event_statuses = [
        event.new_status for event in tasks.list_events(task.id) if event.new_status is not None
    ]
    assert TaskStatus.VERIFYING in event_statuses
    assert event_statuses[-1] is TaskStatus.SUCCEEDED
    assert execution.get_plan(task.id) is not None


def test_unsupported_instruction_becomes_needs_input(tmp_path: Path) -> None:
    _settings, tasks, _execution, orchestrator = _setup(tmp_path)
    task = tasks.create(Task(instruction="Build me a mobile game"))

    report = orchestrator.execute(task.id)

    assert report.task.status is TaskStatus.NEEDS_INPUT
    assert report.missing_information


def test_external_folder_requires_approval_and_denial_executes_nothing(tmp_path: Path) -> None:
    settings, tasks, execution, orchestrator = _setup(tmp_path)
    outside = tmp_path / "outside" / "Folder A"
    write_blank_pdf(outside / "a.pdf", 1)
    write_blank_pdf(outside / "b.pdf", 1)
    options = PlannerOptions(
        input_folders=(str(outside),), output_path="external-merged.pdf"
    )
    task = tasks.create(Task(instruction="Merge PDFs"))

    report = orchestrator.execute(
        task.id,
        options=options,
        approval_callback=lambda _request: False,
    )

    assert report.task.status is TaskStatus.CANCELLED
    assert not (settings.workspace_dir / "external-merged.pdf").exists()
    approvals = execution.list_approvals(task.id)
    assert len(approvals) == 1 and approvals[0].decision == "denied"


def test_approved_external_folder_is_read_into_workspace_output(tmp_path: Path) -> None:
    settings, tasks, execution, orchestrator = _setup(tmp_path)
    outside = tmp_path / "outside" / "Folder A"
    write_blank_pdf(outside / "a.pdf", 2)
    write_blank_pdf(outside / "b.pdf", 1)
    task = tasks.create(Task(instruction="Merge PDFs"))

    report = orchestrator.execute(
        task.id,
        options=PlannerOptions(
            input_folders=(str(outside),), output_path="external-merged.pdf"
        ),
        approval_callback=lambda _request: True,
    )

    assert report.success, report.error
    assert (settings.workspace_dir / "external-merged.pdf").is_file()
    assert execution.list_approvals(task.id)[0].decision == "approved"


def test_tool_failure_marks_task_failed(tmp_path: Path) -> None:
    settings, tasks, _execution, orchestrator = _setup(tmp_path)
    (settings.workspace_dir / "Empty").mkdir()
    task = tasks.create(Task(instruction="Merge PDFs"))

    report = orchestrator.execute(
        task.id,
        options=PlannerOptions(input_folders=("Empty",), output_path="none.pdf"),
    )

    assert report.task.status is TaskStatus.FAILED
    assert report.error is not None and "No PDF" in report.error


class StaticProvider:
    implemented = True
    name = "fake-ai"
    model = "fake-model"

    @property
    def configured(self) -> bool:
        return True

    def complete(self, request: object) -> CompletionResponse:
        return CompletionResponse(
            json.dumps(
                {
                    "intent": "pdf.merge_folders",
                    "tool_id": "pdf.merge_folders",
                    "arguments": {
                        "input_folders": ["Folder A", "Folder B"],
                        "output_path": "ai-merged.pdf",
                        "recursive": False,
                    },
                    "expected_pages": None,
                    "summary": "Merge demo folders",
                    "missing_information": [],
                }
            ),
            self.name,
            self.model,
        )


def test_ai_planned_task_uses_same_registry_and_verification(tmp_path: Path) -> None:
    router = ModelRouter([StaticProvider()], backoff_seconds=0)
    settings, tasks, _execution, orchestrator = _setup(tmp_path, router=router)
    create_demo_pdfs(settings.workspace_dir)
    task = tasks.create(Task(instruction="Please prepare the folder PDF output"))

    report = orchestrator.execute(task.id, use_ai=True)

    assert report.success, report.error
    assert report.plan is not None and report.plan.planner.startswith("ai:")
    assert report.provider_attempts[0].success is True
    assert (settings.workspace_dir / "ai-merged.pdf").is_file()
