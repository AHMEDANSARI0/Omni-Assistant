"""Phase 7 foundation: acceptance criteria, checkpoints/resume, cancel, memory."""

from __future__ import annotations

import hashlib
import itertools
import sqlite3
from pathlib import Path

import pytest

from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.documents.pdf import create_demo_pdfs
from pc_agent.domain.task import Task, TaskStatus
from pc_agent.errors import PcAgentError, StorageError
from pc_agent.gui.controller import GuiController, format_task_details
from pc_agent.orchestrator import TaskOrchestrator
from pc_agent.planning import DeterministicPlanner, TaskPlan
from pc_agent.storage import db as db_module
from pc_agent.storage.db import Database
from pc_agent.storage.repositories import (
    ExecutionRepository,
    MemoryRepository,
    PreferenceRepository,
    RecoveryRepository,
    TaskRepository,
)
from pc_agent.tools import build_default_registry
from pc_agent.verification import build_criteria, evaluate_criteria


def _setup(tmp_path: Path, *, with_memory: bool = False):
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
        memory_repository=memory if with_memory else None,
        recovery_backoff_seconds=0,
    )
    return settings, tasks, execution, memory, orchestrator


def _age_task(settings: Settings, task_id: str) -> None:
    connection = sqlite3.connect(settings.database_path)
    try:
        connection.execute(
            "UPDATE tasks SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (task_id,),
        )
        connection.commit()
    finally:
        connection.close()


def _crash_after_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_memory: bool = False
):
    settings, tasks, execution, memory, orchestrator = _setup(
        tmp_path, with_memory=with_memory
    )
    create_demo_pdfs(settings.workspace_dir)
    task = tasks.create(Task(instruction="Merge PDFs from Folder A and Folder B"))

    def boom(*_args: object, **_kwargs: object):
        raise RuntimeError("simulated process crash during verification")

    monkeypatch.setattr(TaskOrchestrator, "_verify", boom)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        orchestrator.execute(task.id)
    monkeypatch.undo()
    crashed = tasks.get(task.id)
    assert crashed is not None and crashed.status is TaskStatus.VERIFYING
    return settings, tasks, execution, memory, orchestrator, task


# ----------------------------------------------------------------------
# Acceptance criteria / verification engine
# ----------------------------------------------------------------------
def test_build_criteria_reflects_plan_shape() -> None:
    merge_plan = TaskPlan(
        intent="pdf.merge_folders",
        tool_id="pdf.merge_folders",
        arguments={"output_path": "merged.pdf"},
        summary="merge",
        planner="test",
        expected_pages=6,
    )
    kinds = [criterion.kind for criterion in build_criteria(merge_plan)]
    assert kinds == ["tool_verification", "output_artifact", "output_hash", "pdf_pages"]

    info_plan = TaskPlan(
        intent="desktop.system_info",
        tool_id="desktop.system_info",
        arguments={},
        summary="info",
        planner="test",
    )
    assert [c.kind for c in build_criteria(info_plan)] == ["tool_verification"]


def test_evaluate_criteria_recomputes_hash_independently(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "merged.pdf"
    artifact.write_bytes(b"%PDF-fake")
    correct_hash = hashlib.sha256(b"%PDF-fake").hexdigest()
    plan = TaskPlan(
        intent="pdf.merge_folders",
        tool_id="pdf.merge_folders",
        arguments={},
        summary="merge",
        planner="test",
    )
    criteria = build_criteria(plan)

    good = evaluate_criteria(
        criteria,
        workspace_dir=workspace,
        tool_output={"output_path": "merged.pdf", "sha256": correct_hash, "page_count": 3},
        legacy_valid=True,
        legacy_message="legacy ok",
    )
    assert good.passed

    forged = evaluate_criteria(
        criteria,
        workspace_dir=workspace,
        tool_output={"output_path": "merged.pdf", "sha256": "0" * 64, "page_count": 3},
        legacy_valid=True,
        legacy_message="legacy ok",
    )
    assert not forged.passed
    assert "SHA-256" in forged.failure_summary()

    missing = evaluate_criteria(
        criteria,
        workspace_dir=workspace,
        tool_output={"output_path": "gone.pdf", "page_count": 3},
        legacy_valid=True,
        legacy_message="legacy ok",
    )
    assert not missing.passed


def test_evaluate_row_count_recounts_csv(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data.csv").write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    plan = TaskPlan(
        intent="office.csv_inspect",
        tool_id="office.csv_inspect",
        arguments={},
        summary="inspect",
        planner="test",
    )
    criteria = build_criteria(plan)
    report = evaluate_criteria(
        criteria,
        workspace_dir=workspace,
        tool_output={"path": "data.csv", "row_count": 5},
        legacy_valid=True,
        legacy_message="legacy ok",
    )
    assert not report.passed
    assert "Recounted 2" in report.failure_summary()


def test_successful_task_persists_checkpoint_verification_and_workflow(
    tmp_path: Path,
) -> None:
    settings, tasks, execution, memory, orchestrator = _setup(
        tmp_path, with_memory=True
    )
    create_demo_pdfs(settings.workspace_dir)
    task = tasks.create(Task(instruction="Merge PDFs from Folder A and Folder B"))

    report = orchestrator.execute(task.id)

    assert report.success, report.error
    assert report.verification_report is not None and report.verification_report.passed
    assert len(report.verification_report.results) == 4
    checkpoint = execution.get_checkpoint(task.id, 0)
    assert checkpoint is not None
    assert checkpoint.step_key == "tool:pdf.merge_folders"
    assert checkpoint.artifacts == ("merged.pdf",)
    stored = execution.get_latest_verification(task.id)
    assert stored is not None and stored.passed
    workflows = memory.list(kind="workflow")
    assert len(workflows) == 1
    assert workflows[0].content["success_count"] == 1
    assert workflows[0].content["tool_id"] == "pdf.merge_folders"


# ----------------------------------------------------------------------
# Cooperative cancellation
# ----------------------------------------------------------------------
def test_cancellation_flag_before_execution_runs_no_tool(tmp_path: Path) -> None:
    settings, tasks, execution, _memory, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)
    task = tasks.create(Task(instruction="Merge PDFs from Folder A and Folder B"))
    execution.request_cancellation(task.id)

    report = orchestrator.execute(task.id)

    assert report.task.status is TaskStatus.CANCELLED
    assert report.cancelled
    assert execution.list_tool_invocations(task.id) == []
    assert not (settings.workspace_dir / "merged.pdf").exists()


def test_cancellation_after_tool_stops_before_verification(tmp_path: Path) -> None:
    settings, tasks, execution, _memory, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)
    task = tasks.create(Task(instruction="Merge PDFs from Folder A and Folder B"))
    checks = itertools.count()

    report = orchestrator.execute(
        task.id, cancellation_requested=lambda: next(checks) >= 4
    )

    assert report.task.status is TaskStatus.CANCELLED
    assert [item.tool_id for item in execution.list_tool_invocations(task.id)] == [
        "pdf.merge_folders"
    ]
    assert execution.get_checkpoint(task.id, 0) is not None
    assert (settings.workspace_dir / "merged.pdf").is_file()


def test_request_cancellation_semantics(tmp_path: Path) -> None:
    _settings, tasks, execution, _memory, orchestrator = _setup(tmp_path)

    queued = tasks.create(Task(instruction="Merge PDFs"))
    cancelled, action = orchestrator.request_cancellation(queued.id)
    assert action == "cancelled"
    assert cancelled.status is TaskStatus.CANCELLED
    with pytest.raises(PcAgentError, match="nothing to cancel"):
        orchestrator.request_cancellation(queued.id)

    active = tasks.create(Task(instruction="Merge PDFs"))
    tasks.update_status(active.id, TaskStatus.PLANNING)
    tasks.update_status(active.id, TaskStatus.RUNNING)
    _task, action = orchestrator.request_cancellation(active.id)
    assert action == "requested"
    assert execution.cancellation_requested(active.id)


# ----------------------------------------------------------------------
# Checkpoints and resume
# ----------------------------------------------------------------------
def test_resume_reuses_checkpoint_without_rerunning_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, tasks, execution, _m, orchestrator, task = _crash_after_tool(
        tmp_path, monkeypatch
    )
    output_before = (settings.workspace_dir / "merged.pdf").read_bytes()
    _age_task(settings, task.id)

    report = orchestrator.resume(task.id)

    assert report.success, report.error
    assert report.resumed_from_checkpoint
    merge_invocations = [
        item
        for item in execution.list_tool_invocations(task.id)
        if item.tool_id == "pdf.merge_folders"
    ]
    assert len(merge_invocations) == 1
    assert (settings.workspace_dir / "merged.pdf").read_bytes() == output_before
    assert tasks.get(task.id).status is TaskStatus.SUCCEEDED  # type: ignore[union-attr]


def test_resume_refuses_recent_active_task_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _s, _t, _e, _m, orchestrator, task = _crash_after_tool(tmp_path, monkeypatch)
    with pytest.raises(PcAgentError, match="looks active"):
        orchestrator.resume(task.id)
    report = orchestrator.resume(task.id, force=True)
    assert report.success, report.error


def test_resume_terminal_task_is_rejected(tmp_path: Path) -> None:
    settings, tasks, _e, _m, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)
    task = tasks.create(Task(instruction="Merge PDFs from Folder A and Folder B"))
    assert orchestrator.execute(task.id).success
    with pytest.raises(PcAgentError, match="cannot be resumed"):
        orchestrator.resume(task.id)


def test_resume_with_missing_artifact_reruns_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, tasks, execution, _m, orchestrator, task = _crash_after_tool(
        tmp_path, monkeypatch
    )
    (settings.workspace_dir / "merged.pdf").unlink()
    _age_task(settings, task.id)

    report = orchestrator.resume(task.id)

    assert report.success, report.error
    assert not report.resumed_from_checkpoint
    merge_invocations = [
        item
        for item in execution.list_tool_invocations(task.id)
        if item.tool_id == "pdf.merge_folders"
    ]
    assert len(merge_invocations) == 2
    events = [event.event_type for event in tasks.list_events(task.id)]
    assert "checkpoint_invalidated" in events


# ----------------------------------------------------------------------
# Memory
# ----------------------------------------------------------------------
def test_memory_repository_crud_counts_and_clear(tmp_path: Path) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    memory = MemoryRepository(database)
    memory.upsert(kind="note", key="alpha", title="Alpha", content={"text": "one"})
    memory.upsert(kind="note", key="alpha", title="Alpha 2", content={"text": "two"})
    memory.upsert(kind="workflow", key="wf-1", title="Flow", content={"success_count": 3})
    assert memory.counts() == {"note": 1, "workflow": 1}
    record = memory.get("note", "alpha")
    assert record is not None and record.content["text"] == "two"
    assert memory.delete("note", "alpha")
    assert not memory.delete("note", "alpha")
    assert memory.clear() == 1
    with pytest.raises(StorageError):
        memory.upsert(kind="secret", key="x", title="x", content={})


def test_cli_memory_commands_and_secret_guard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = ["--root", str(tmp_path)]
    assert main([*root, "memory", "pref-set", "api_key", "nope"]) == 1
    assert "Refusing to store secret-like" in capsys.readouterr().err

    assert main([*root, "memory", "pref-set", "editor", "notepad", "plus"]) == 0
    capsys.readouterr()
    assert main([*root, "memory", "pref-get", "editor"]) == 0
    assert "notepad plus" in capsys.readouterr().out

    assert main([*root, "memory", "note-set", "home", "call", "office"]) == 0
    capsys.readouterr()
    assert main([*root, "memory", "note-show", "home"]) == 0
    assert "call office" in capsys.readouterr().out

    assert main([*root, "memory", "stats"]) == 0
    stats = capsys.readouterr().out
    assert "Preferences: 1" in stats and "Memory notes: 1" in stats

    assert main([*root, "memory", "clear"]) == 1
    capsys.readouterr()
    assert main([*root, "memory", "clear", "--yes"]) == 0
    assert "Task history was kept" in capsys.readouterr().out


def test_cli_task_cancel_delete_and_show_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings, tasks, _e, _m, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)
    done_task = tasks.create(Task(instruction="Merge PDFs from Folder A and Folder B"))
    assert orchestrator.execute(done_task.id).success

    assert main(["--root", str(tmp_path), "task", "show", done_task.id]) == 0
    shown = capsys.readouterr().out
    assert "Checkpoints: 1" in shown
    assert "Acceptance verification: passed (4 criteria)" in shown

    queued = tasks.create(Task(instruction="Merge PDFs later"))
    assert main(["--root", str(tmp_path), "task", "cancel", queued.id]) == 0
    capsys.readouterr()
    stored = tasks.get(queued.id)
    assert stored is not None and stored.status is TaskStatus.CANCELLED

    assert main(["--root", str(tmp_path), "task", "delete", queued.id]) == 1
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "task", "delete", queued.id, "--yes"]) == 0
    capsys.readouterr()
    assert tasks.get(queued.id) is None
    # Deleting the record never touches workspace files.
    assert (settings.workspace_dir / "merged.pdf").is_file()

    active = tasks.create(Task(instruction="Merge PDFs later"))
    assert main(["--root", str(tmp_path), "task", "delete", active.id, "--yes"]) == 1
    capsys.readouterr()


def test_cli_doctor_reports_phase7_memory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--root", str(tmp_path), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "PHASE 27 PDF Signature Inspection" in out
    assert "schema v6" in out
    assert "Memory: preferences=0" in out


def test_gui_controller_cancel_details_and_resume_queued(tmp_path: Path) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    controller = GuiController(settings, desktop_opener=lambda _target: None)
    controller.create_pdf_demo()

    report = controller.run_new_task("Merge PDFs from Folder A and Folder B")
    assert report.success, report.error
    details = controller.get_task_details(report.task.id)
    assert len(details.checkpoints) == 1
    assert details.latest_verification is not None
    assert details.latest_verification.passed
    formatted = format_task_details(details)
    assert "Step checkpoints:" in formatted
    assert "[PASS]" in formatted

    tasks = TaskRepository(controller.database)
    queued = tasks.create(Task(instruction="Merge PDFs later"))
    message = controller.request_task_cancellation(queued.id)
    assert "cancelled" in message
    stored = tasks.get(queued.id)
    assert stored is not None and stored.status is TaskStatus.CANCELLED

    resumable = tasks.create(Task(instruction="Merge PDFs from Folder A and Folder B"))
    resumed = controller.resume_task(resumable.id)
    assert resumed.task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}
    # The first output already exists, so bounded recovery must still succeed
    # by writing a fresh recovered output rather than overwriting.
    assert resumed.success, resumed.error


def test_schema_v5_upgrades_to_v6_without_losing_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "data" / "upgrade.db")
    original = db_module._MIGRATIONS
    monkeypatch.setattr(db_module, "_MIGRATIONS", original[:5])
    assert database.initialize() == 5
    created = TaskRepository(database).create(Task(instruction="Keep me"))

    monkeypatch.setattr(db_module, "_MIGRATIONS", original)
    assert database.initialize() == 6
    assert TaskRepository(database).get(created.id) is not None
    execution = ExecutionRepository(database)
    assert execution.count_checkpoints() == 0
    assert MemoryRepository(database).counts() == {"note": 0, "workflow": 0}
    assert PreferenceRepository(database).all() == {}
