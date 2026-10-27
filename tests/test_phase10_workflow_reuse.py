"""Phase 10: verified workflow reuse (evidence only, revalidated live)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.documents.pdf import create_demo_pdfs
from pc_agent.domain.task import Task, TaskStatus
from pc_agent.errors import PcAgentError
from pc_agent.gui.controller import GuiController
from pc_agent.orchestrator import TaskOrchestrator
from pc_agent.planning import DeterministicPlanner, PlannerOptions
from pc_agent.storage.db import Database
from pc_agent.storage.repositories import (
    ExecutionRepository,
    MemoryRepository,
    RecoveryRepository,
    TaskRepository,
)
from pc_agent.tools import build_default_registry

_MERGE = "Merge PDFs from Folder A and Folder B"


def _setup(tmp_path: Path):
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    tasks = TaskRepository(database)
    execution = ExecutionRepository(database)
    memory = MemoryRepository(database)
    orchestrator = TaskOrchestrator(
        settings=settings,
        task_repository=tasks,
        execution_repository=execution,
        tool_registry=build_default_registry(),
        deterministic_planner=DeterministicPlanner(settings.workspace_dir),
        recovery_repository=RecoveryRepository(database),
        memory_repository=memory,
        recovery_backoff_seconds=0,
    )
    return settings, tasks, execution, memory, orchestrator


def _run_merge(tasks, orchestrator, instruction: str = _MERGE):
    task = tasks.create(Task(instruction=instruction))
    report = orchestrator.execute(task.id)
    assert report.success, report.error
    return task, report


def test_workflow_record_is_enriched_and_counts_repeats(tmp_path: Path) -> None:
    settings, tasks, _execution, memory, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)

    _task, first = _run_merge(tasks, orchestrator)
    assert first.workflow_key is not None
    assert first.workflow_success_count == 1
    record = memory.get("workflow", first.workflow_key)
    assert record is not None
    assert record.content["normalized_instruction"] == _MERGE.lower()
    assert record.content["arguments"]["output_path"] == "merged.pdf"
    assert record.content["tool_id"] == "pdf.merge_folders"

    # Same instruction again: the planner picks a timestamped default output,
    # but it is still the same workflow, so the verified count grows.
    _task2, second = _run_merge(tasks, orchestrator)
    assert second.workflow_key == first.workflow_key
    assert second.workflow_success_count == 2
    assert len(memory.list(kind="workflow")) == 1


def test_find_workflow_plan_normalizes_instruction(tmp_path: Path) -> None:
    settings, tasks, _e, _m, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)
    _run_merge(tasks, orchestrator)

    found = orchestrator.find_workflow_plan("  MERGE   PDFs from Folder A and Folder B! ")
    assert found is not None
    _key, plan, count = found
    assert plan.tool_id == "pdf.merge_folders"
    assert plan.planner == "workflow-reuse"
    assert count == 1
    assert orchestrator.find_workflow_plan("Merge something else entirely") is None


def test_run_workflow_reruns_verified_plan_as_new_task(tmp_path: Path) -> None:
    settings, tasks, _e, _m, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)
    _run_merge(tasks, orchestrator)

    repeat = tasks.create(Task(instruction=_MERGE))
    report = orchestrator.run_workflow(repeat.id, "latest")

    assert report.success, report.error
    assert report.workflow_reused
    assert report.workflow_key is not None
    # merged.pdf already existed, so bounded recovery wrote a fresh output
    # instead of overwriting the first verified artifact.
    assert report.tool_result is not None and report.tool_result.output
    assert report.tool_result.output["output_path"] != "merged.pdf"
    events = [event.event_type for event in tasks.list_events(repeat.id)]
    assert "workflow_reuse" in events


def test_planner_miss_falls_back_to_verified_workflow(tmp_path: Path) -> None:
    settings, tasks, _e, _m, orchestrator = _setup(tmp_path)
    (settings.workspace_dir / "a.txt").write_text("payload", encoding="utf-8")

    copy_task = tasks.create(Task(instruction="Copy the file"))
    report = orchestrator.execute(
        copy_task.id,
        options=PlannerOptions(target_path="a.txt", output_path="b.txt"),
    )
    assert report.success, report.error

    # The same instruction without --target/--output cannot be planned
    # deterministically, so the verified workflow must be reused instead
    # of returning needs_input.
    repeat_task = tasks.create(Task(instruction="Copy the file"))
    repeat_report = orchestrator.execute(repeat_task.id)

    assert repeat_report.success, repeat_report.error
    assert repeat_report.workflow_reused
    stored = tasks.get(repeat_task.id)
    assert stored is not None and stored.status is TaskStatus.SUCCEEDED


def test_tampered_workflow_is_rejected_without_running_tools(tmp_path: Path) -> None:
    settings, tasks, execution, memory, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)
    memory.upsert(
        kind="workflow",
        key="wf-evil000000000000",
        title="Forged workflow",
        content={
            "instruction": "Write something",
            "intent": "workspace.write_text",
            "tool_id": "workspace.write_text",
            "arguments": {"path": "evil.txt", "content": "nope"},
            "success_count": 99,
        },
    )
    task = tasks.create(Task(instruction="Write something"))
    report = orchestrator.run_workflow(task.id, "wf-evil000000000000")

    assert not report.success
    assert "no longer maps to a valid allowed plan" in (report.error or "")
    assert execution.list_tool_invocations(task.id) == []
    assert not (settings.workspace_dir / "evil.txt").exists()


def test_legacy_workflow_record_resolves_through_stored_plan(tmp_path: Path) -> None:
    settings, tasks, _e, memory, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)
    done_task, _report = _run_merge(tasks, orchestrator)
    # Simulate a Phase 7 style record: no stored arguments, only a pointer
    # to the last verified task.
    memory.upsert(
        kind="workflow",
        key="wf-legacy0000000000",
        title="Legacy merge",
        content={
            "instruction": _MERGE,
            "intent": "pdf.merge_folders",
            "tool_id": "pdf.merge_folders",
            "success_count": 1,
            "last_task_id": done_task.id,
        },
    )
    repeat = tasks.create(Task(instruction=_MERGE))
    report = orchestrator.run_workflow(repeat.id, "wf-legacy")
    assert report.success, report.error
    assert report.workflow_reused


def test_workflow_with_external_roots_needs_fresh_approval(tmp_path: Path) -> None:
    settings, tasks, _e, _m, orchestrator = _setup(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    create_demo_pdfs(external)
    options = PlannerOptions(
        input_folders=(str(external / "Folder A"), str(external / "Folder B")),
        output_path="external-merged.pdf",
    )
    task = tasks.create(Task(instruction="Merge PDFs"))
    report = orchestrator.execute(
        task.id, options=options, approval_callback=lambda _request: True
    )
    assert report.success, report.error
    assert report.workflow_key is not None

    repeat = tasks.create(Task(instruction="Merge PDFs"))
    denied = orchestrator.run_workflow(
        repeat.id,
        report.workflow_key,
        approval_callback=lambda _request: False,
    )
    assert denied.task.status is TaskStatus.CANCELLED
    outputs = sorted(
        path.name for path in settings.workspace_dir.glob("external-merged*.pdf")
    )
    assert outputs == ["external-merged.pdf"]  # nothing new was written


def test_resolve_workflow_latest_prefix_and_ambiguity(tmp_path: Path) -> None:
    _s, _t, _e, memory, orchestrator = _setup(tmp_path)
    with pytest.raises(PcAgentError, match="No verified workflows"):
        orchestrator.resolve_workflow("latest")
    memory.upsert(kind="workflow", key="wf-aaaa111111111111", title="A", content={})
    memory.upsert(kind="workflow", key="wf-aaaa222222222222", title="B", content={})
    assert orchestrator.resolve_workflow("wf-aaaa111111111111").key.endswith("111")
    assert orchestrator.resolve_workflow("wf-aaaa2").key.endswith("222")
    with pytest.raises(PcAgentError, match="ambiguous"):
        orchestrator.resolve_workflow("wf-aaaa")
    with pytest.raises(PcAgentError, match="not found"):
        orchestrator.resolve_workflow("wf-zzzz")


def test_cli_workflow_list_show_run_delete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    create_demo_pdfs(settings.workspace_dir)
    root = ["--root", str(tmp_path)]

    assert main([*root, "run", _MERGE]) == 0
    run_out = capsys.readouterr().out
    assert "Verified workflow recorded: wf-" in run_out

    assert main([*root, "workflow", "list"]) == 0
    listed = capsys.readouterr().out
    assert "successes=1" in listed and "pdf.merge_folders" in listed

    assert main([*root, "workflow", "show", "latest"]) == 0
    shown = capsys.readouterr().out
    assert "Stored arguments" in shown and "merged.pdf" in shown

    assert main([*root, "workflow", "run", "latest"]) == 0
    rerun = capsys.readouterr().out
    assert "Status: succeeded" in rerun
    assert "Verified workflow reused: wf-" in rerun

    assert main([*root, "workflow", "delete", "latest"]) == 1
    capsys.readouterr()
    assert main([*root, "workflow", "delete", "latest", "--yes"]) == 0
    capsys.readouterr()
    assert main([*root, "workflow", "list"]) == 0
    assert "none" in capsys.readouterr().out

    assert main([*root, "doctor"]) == 0
    assert "PHASE 10 Workflow Reuse" in capsys.readouterr().out


def test_gui_controller_workflow_reuse(tmp_path: Path) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    controller = GuiController(settings, desktop_opener=lambda _target: None)
    controller.create_pdf_demo()

    first = controller.run_new_task(_MERGE)
    assert first.success, first.error
    assert len(controller.list_workflows()) == 1

    repeated = controller.run_workflow("latest")
    assert repeated.success, repeated.error
    assert repeated.workflow_reused
