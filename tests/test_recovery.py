from __future__ import annotations

import itertools
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import pc_agent.storage.db as db_module
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.documents.pdf import write_blank_pdf
from pc_agent.domain.task import Task, TaskStatus, utc_now
from pc_agent.errors import ConfigError, RecoveryError
from pc_agent.orchestrator import ApprovalRequest, TaskOrchestrator
from pc_agent.planning import DeterministicPlanner, PlannerOptions, TaskPlan
from pc_agent.recovery import (
    FailureCategory,
    RecoveryAction,
    RecoveryService,
    diagnose_tool_failure,
    diagnose_verification_failure,
)
from pc_agent.storage.db import Database
from pc_agent.storage.repositories import (
    ExecutionRepository,
    RecoveryRepository,
    TaskRepository,
)
from pc_agent.tools import build_default_registry
from pc_agent.tools.registry import ToolContext, ToolRegistry, ToolResult


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PC_AGENT_HOME",
        "PC_AGENT_ENVIRONMENT",
        "PC_AGENT_WORKSPACE_DIR",
        "PC_AGENT_DATA_DIR",
        "PC_AGENT_DATABASE_PATH",
        "PC_AGENT_PROVIDER_ORDER",
        "PC_AGENT_MAX_RECOVERY_ATTEMPTS",
        "PC_AGENT_RECOVERY_BACKOFF_SECONDS",
        "PC_AGENT_RECOVERY_STALE_AFTER_MINUTES",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "OLLAMA_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


class DelegatingRegistry(ToolRegistry):
    def __init__(self) -> None:
        super().__init__()
        for tool in build_default_registry().list_tools():
            registered = self.register(tool.spec, tool.handler)
            registered.enabled = tool.enabled


class FailFirstMergeRegistry(DelegatingRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def invoke(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        context: ToolContext,
        *,
        version: str | None = None,
    ) -> ToolResult:
        if tool_id == "pdf.merge_folders" and not self.failed:
            self.failed = True
            tool = self.get(tool_id)
            return ToolResult(
                tool_id=tool_id,
                version=tool.spec.version if tool else "unknown",
                success=False,
                error="Temporary sharing violation: an input is locked.",
            )
        return super().invoke(tool_id, arguments, context, version=version)


class FailFirstVerificationRegistry(DelegatingRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def invoke(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        context: ToolContext,
        *,
        version: str | None = None,
    ) -> ToolResult:
        result = super().invoke(tool_id, arguments, context, version=version)
        if tool_id == "pdf.verify" and not self.failed and result.output is not None:
            self.failed = True
            output = dict(result.output)
            output["sha256"] = "0" * 64
            return ToolResult(
                tool_id=result.tool_id,
                version=result.version,
                success=True,
                output=output,
                elapsed_ms=result.elapsed_ms,
            )
        return result


@dataclass
class Stack:
    settings: Settings
    database: Database
    tasks: TaskRepository
    execution: ExecutionRepository
    recovery: RecoveryRepository
    orchestrator: TaskOrchestrator
    service: RecoveryService
    delays: list[float]


def make_stack(
    tmp_path: Path,
    *,
    registry: ToolRegistry | None = None,
    max_recovery_attempts: int = 2,
) -> Stack:
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    tasks = TaskRepository(database)
    execution = ExecutionRepository(database)
    recovery = RecoveryRepository(database)
    delays: list[float] = []
    orchestrator = TaskOrchestrator(
        settings=settings,
        task_repository=tasks,
        execution_repository=execution,
        tool_registry=registry or build_default_registry(),
        deterministic_planner=DeterministicPlanner(settings.workspace_dir),
        recovery_repository=recovery,
        max_recovery_attempts=max_recovery_attempts,
        recovery_backoff_seconds=0.25,
        sleeper=delays.append,
    )
    service = RecoveryService(
        settings=settings,
        task_repository=tasks,
        execution_repository=execution,
        recovery_repository=recovery,
        orchestrator=orchestrator,
    )
    return Stack(
        settings=settings,
        database=database,
        tasks=tasks,
        execution=execution,
        recovery=recovery,
        orchestrator=orchestrator,
        service=service,
        delays=delays,
    )


def make_pdfs(folder: Path) -> None:
    write_blank_pdf(folder / "a.pdf", 1)
    write_blank_pdf(folder / "b.pdf", 2)


def merge_plan(
    *, inputs: list[str], output: str = "merged.pdf", tool_id: str = "pdf.merge"
) -> TaskPlan:
    key = "input_paths" if tool_id == "pdf.merge" else "input_folders"
    arguments: dict[str, Any] = {key: inputs, "output_path": output}
    if tool_id == "pdf.merge_folders":
        arguments["recursive"] = False
    return TaskPlan(
        intent=tool_id,
        tool_id=tool_id,
        arguments=arguments,
        summary=f"Merge test PDFs into {output}.",
        planner="test",
    )


def test_recovery_settings_are_bounded(tmp_path: Path) -> None:
    settings = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_MAX_RECOVERY_ATTEMPTS": "3",
            "PC_AGENT_RECOVERY_BACKOFF_SECONDS": "0",
            "PC_AGENT_RECOVERY_STALE_AFTER_MINUTES": "45",
        },
    )
    assert settings.max_recovery_attempts == 3
    assert settings.recovery_backoff_seconds == 0
    assert settings.recovery_stale_after_minutes == 45

    with pytest.raises(ConfigError, match="between 0 and 3"):
        Settings.from_env(
            tmp_path, environ={"PC_AGENT_MAX_RECOVERY_ATTEMPTS": "4"}
        )
    with pytest.raises(ConfigError, match="between 0 and 5"):
        Settings.from_env(
            tmp_path, environ={"PC_AGENT_RECOVERY_BACKOFF_SECONDS": "6"}
        )


def test_schema_v3_upgrades_through_v4_and_v5_to_v6_without_losing_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "data" / "test.db")
    original = db_module._MIGRATIONS
    monkeypatch.setattr(db_module, "_MIGRATIONS", original[:3])
    assert database.initialize() == 3
    created = TaskRepository(database).create(Task(instruction="Keep me"))

    monkeypatch.setattr(db_module, "_MIGRATIONS", original)
    assert database.initialize() == 6
    assert TaskRepository(database).get(created.id) is not None
    assert RecoveryRepository(database).count_attempts() == 0


def test_recovery_repository_round_trip_and_link(tmp_path: Path) -> None:
    stack = make_stack(tmp_path)
    source = stack.tasks.create(Task(instruction="Source"))
    clone = stack.tasks.create(Task(instruction="Recovery"))

    attempt = stack.recovery.start_attempt(
        clone.id,
        attempt_number=1,
        stage="execution",
        action="resume_stored_plan",
        source_task_id=source.id,
        plan_before={"tool_id": "pdf.merge"},
    )
    finished = stack.recovery.finish_attempt(
        attempt.id,
        status="retrying",
        stage="tool_execution",
        category="output_exists",
        action="retry_new_output",
        retryable=True,
        original_error="Output file already exists.",
        message="Retry with a fresh output.",
        plan_after={"tool_id": "pdf.merge", "arguments": {"output_path": "new.pdf"}},
    )
    link = stack.recovery.create_link(
        source_task_id=source.id, recovery_task_id=clone.id, reason="manual"
    )

    assert finished.stage == "tool_execution"
    assert finished.plan_after is not None
    assert stack.recovery.count_attempts() == 1
    assert [item.id for item in stack.recovery.list_attempts(task_id=clone.id)] == [
        attempt.id
    ]
    assert stack.recovery.source_for(clone.id) == source.id
    assert stack.recovery.list_links_for(source.id)[0].id == link.id


def test_diagnosis_rules_are_deterministic_and_conservative(tmp_path: Path) -> None:
    stack = make_stack(tmp_path)
    workspace = stack.settings.workspace_dir
    (workspace / "Archive").mkdir(parents=True)
    (workspace / "Inputs").mkdir(parents=True)
    write_blank_pdf(workspace / "Archive" / "report.pdf", 1)
    (workspace / "merged.pdf").write_bytes(b"sentinel")

    corrected = diagnose_tool_failure(
        TaskPlan(
            intent="pdf.verify",
            tool_id="pdf.verify",
            arguments={"path": "missing/report.pdf"},
            summary="Verify report.",
            planner="test",
        ),
        "Input file not found: missing/report.pdf",
        workspace_dir=workspace,
    )
    assert corrected.category is FailureCategory.MISSING_INPUT
    assert corrected.action is RecoveryAction.RETRY_CORRECTED_PATH
    assert corrected.revised_arguments == {"path": "Archive/report.pdf"}

    collision = diagnose_tool_failure(
        merge_plan(inputs=["Archive/report.pdf", "Archive/report.pdf"]),
        "Output file already exists. PDF tools never overwrite.",
        workspace_dir=workspace,
    )
    assert collision.category is FailureCategory.OUTPUT_EXISTS
    assert collision.action is RecoveryAction.RETRY_NEW_OUTPUT
    assert collision.revised_arguments is not None
    assert collision.revised_arguments["output_path"] == "merged-recovered.pdf"

    policy = diagnose_tool_failure(
        merge_plan(inputs=["Archive/report.pdf", "Archive/report.pdf"]),
        "Path is outside the workspace and not allowed.",
        workspace_dir=workspace,
    )
    assert policy.category is FailureCategory.POLICY
    assert not policy.retryable

    invalid = diagnose_tool_failure(
        merge_plan(inputs=["Inputs"], tool_id="pdf.merge_folders"),
        "At least two valid PDFs are required to merge.",
        workspace_dir=workspace,
    )
    assert invalid.category is FailureCategory.INVALID_INPUT
    assert not invalid.retryable

    transient = diagnose_tool_failure(
        merge_plan(inputs=["Inputs"], tool_id="pdf.merge_folders"),
        "Tool timed out while a file was locked.",
        workspace_dir=workspace,
    )
    assert transient.category is FailureCategory.TRANSIENT
    assert transient.action is RecoveryAction.RETRY_SAME_PLAN

    verification = diagnose_verification_failure(
        merge_plan(inputs=["Inputs"], tool_id="pdf.merge_folders"),
        "Merged PDF did not pass verification.",
        workspace_dir=workspace,
    )
    assert verification.category is FailureCategory.VERIFICATION_FAILED
    assert verification.action is RecoveryAction.RETRY_NEW_OUTPUT


def test_ambiguous_missing_path_is_never_guessed(tmp_path: Path) -> None:
    stack = make_stack(tmp_path)
    workspace = stack.settings.workspace_dir
    write_blank_pdf(workspace / "One" / "report.pdf", 1)
    write_blank_pdf(workspace / "Two" / "report.pdf", 1)

    diagnosis = diagnose_tool_failure(
        TaskPlan(
            intent="pdf.verify",
            tool_id="pdf.verify",
            arguments={"path": "Old/report.pdf"},
            summary="Verify report.",
            planner="test",
        ),
        "Input file not found: Old/report.pdf",
        workspace_dir=workspace,
    )

    assert diagnosis.category is FailureCategory.MISSING_INPUT
    assert diagnosis.needs_input
    assert not diagnosis.retryable


def test_output_collision_heals_to_fresh_file_and_preserves_existing(
    tmp_path: Path,
) -> None:
    stack = make_stack(tmp_path)
    make_pdfs(stack.settings.workspace_dir / "Inputs")
    existing = stack.settings.workspace_dir / "merged.pdf"
    existing.write_bytes(b"do-not-overwrite")
    task = stack.tasks.create(Task(instruction="Merge PDFs"))

    report = stack.orchestrator.execute(
        task.id,
        options=PlannerOptions(
            input_folders=("Inputs",), output_path="merged.pdf"
        ),
    )

    assert report.success, report.error
    assert report.recovered
    assert report.tool_result is not None and report.tool_result.output is not None
    assert report.tool_result.output["output_path"] == "merged-recovered.pdf"
    assert existing.read_bytes() == b"do-not-overwrite"
    assert [item.status for item in report.recovery_attempts] == [
        "retrying",
        "succeeded",
    ]
    attempts = stack.recovery.list_attempts(task_id=task.id)
    assert [item.category for item in attempts] == ["output_exists", None]
    statuses = [
        event.new_status
        for event in stack.tasks.list_events(task.id)
        if event.new_status is not None
    ]
    assert TaskStatus.RETRYING in statuses


def test_no_recovery_override_records_one_failed_attempt(tmp_path: Path) -> None:
    stack = make_stack(tmp_path)
    make_pdfs(stack.settings.workspace_dir / "Inputs")
    (stack.settings.workspace_dir / "merged.pdf").write_bytes(b"existing")
    task = stack.tasks.create(Task(instruction="Merge PDFs"))

    report = stack.orchestrator.execute(
        task.id,
        options=PlannerOptions(
            input_folders=("Inputs",), output_path="merged.pdf"
        ),
        max_recovery_attempts=0,
    )

    assert report.task.status is TaskStatus.FAILED
    assert not report.recovered
    assert len(report.recovery_attempts) == 1
    assert report.recovery_attempts[0].status == "failed"
    assert not (stack.settings.workspace_dir / "merged-recovered.pdf").exists()


def test_unique_missing_input_path_is_corrected_from_stored_plan(
    tmp_path: Path,
) -> None:
    stack = make_stack(tmp_path)
    make_pdfs(stack.settings.workspace_dir / "Archive")
    task = stack.tasks.create(Task(instruction="Merge PDFs"))
    plan = merge_plan(inputs=["a.pdf", "Archive/b.pdf"], output="corrected.pdf")
    stack.execution.save_plan(task.id, plan.to_dict(), plan.planner)

    report = stack.orchestrator.execute(task.id, resume_stored_plan=True)

    assert report.success, report.error
    assert report.recovered
    assert report.plan is not None
    assert report.plan.arguments["input_paths"] == [
        "Archive/a.pdf",
        "Archive/b.pdf",
    ]
    assert report.recovery_attempts[0].category == "missing_input"
    assert report.recovery_attempts[0].action == "retry_corrected_path"


def test_uncorrectable_missing_input_becomes_needs_input(tmp_path: Path) -> None:
    stack = make_stack(tmp_path)
    task = stack.tasks.create(Task(instruction="Verify a PDF"))
    plan = TaskPlan(
        intent="pdf.verify",
        tool_id="pdf.verify",
        arguments={"path": "gone.pdf"},
        summary="Verify gone.pdf.",
        planner="test",
    )
    stack.execution.save_plan(task.id, plan.to_dict(), plan.planner)

    report = stack.orchestrator.execute(task.id, resume_stored_plan=True)

    assert report.task.status is TaskStatus.NEEDS_INPUT
    assert report.missing_information
    assert report.recovery_attempts[0].category == "missing_input"
    assert stack.recovery.list_attempts(task_id=task.id)[0].category == "missing_input"


def test_transient_failure_retries_same_plan_with_bounded_backoff(
    tmp_path: Path,
) -> None:
    stack = make_stack(tmp_path, registry=FailFirstMergeRegistry())
    make_pdfs(stack.settings.workspace_dir / "Inputs")
    task = stack.tasks.create(Task(instruction="Merge PDFs"))

    report = stack.orchestrator.execute(
        task.id,
        options=PlannerOptions(
            input_folders=("Inputs",), output_path="merged.pdf"
        ),
    )

    assert report.success, report.error
    assert report.recovered
    assert report.tool_result is not None and report.tool_result.output is not None
    assert report.tool_result.output["output_path"] == "merged.pdf"
    assert report.recovery_attempts[0].action == "retry_same_plan"
    assert stack.delays == [0.25]


def test_verification_failure_retries_once_to_fresh_output(tmp_path: Path) -> None:
    stack = make_stack(tmp_path, registry=FailFirstVerificationRegistry())
    make_pdfs(stack.settings.workspace_dir / "Inputs")
    task = stack.tasks.create(Task(instruction="Merge PDFs"))

    report = stack.orchestrator.execute(
        task.id,
        options=PlannerOptions(
            input_folders=("Inputs",), output_path="merged.pdf"
        ),
    )

    assert report.success, report.error
    assert report.recovered
    assert report.recovery_attempts[0].stage == "verification"
    assert report.recovery_attempts[0].category == "verification_failed"
    assert (stack.settings.workspace_dir / "merged.pdf").is_file()
    assert (stack.settings.workspace_dir / "merged-recovered.pdf").is_file()


class DecisionSequence:
    def __init__(self, decisions: list[bool]) -> None:
        self.decisions = list(decisions)
        self.requests: list[ApprovalRequest] = []

    def __call__(self, request: ApprovalRequest) -> bool:
        self.requests.append(request)
        return self.decisions.pop(0)


def test_external_revised_plan_requires_fresh_approval(tmp_path: Path) -> None:
    stack = make_stack(tmp_path)
    outside = tmp_path / "outside" / "Inputs"
    make_pdfs(outside)
    (stack.settings.workspace_dir / "merged.pdf").write_bytes(b"existing")
    callback = DecisionSequence([True, False])
    task = stack.tasks.create(Task(instruction="Merge PDFs"))

    report = stack.orchestrator.execute(
        task.id,
        options=PlannerOptions(
            input_folders=(str(outside),), output_path="merged.pdf"
        ),
        approval_callback=callback,
    )

    assert report.task.status is TaskStatus.CANCELLED
    assert len(callback.requests) == 2
    assert callback.requests[0].plan.arguments["output_path"] == "merged.pdf"
    assert (
        callback.requests[1].plan.arguments["output_path"]
        == "merged-recovered.pdf"
    )
    assert [item.decision for item in stack.execution.list_approvals(task.id)] == [
        "approved",
        "denied",
    ]
    assert not (stack.settings.workspace_dir / "merged-recovered.pdf").exists()
    assert len(stack.execution.list_tool_invocations(task.id)) == 1


def test_failed_task_diagnosis_and_manual_recovery_clone(tmp_path: Path) -> None:
    stack = make_stack(tmp_path)
    make_pdfs(stack.settings.workspace_dir / "Inputs")
    (stack.settings.workspace_dir / "merged.pdf").write_bytes(b"existing")
    task = stack.tasks.create(Task(instruction="Merge PDFs"))
    failed = stack.orchestrator.execute(
        task.id,
        options=PlannerOptions(
            input_folders=("Inputs",), output_path="merged.pdf"
        ),
        max_recovery_attempts=0,
    )
    assert failed.task.status is TaskStatus.FAILED

    diagnosis = stack.service.diagnose(task.id)
    assert diagnosis.diagnosis is not None
    assert diagnosis.diagnosis.category is FailureCategory.OUTPUT_EXISTS

    outcome = stack.service.recover(task.id)

    assert outcome.cloned
    assert outcome.report is not None and outcome.report.success
    assert outcome.report.recovered
    assert outcome.source_task.status is TaskStatus.FAILED
    assert outcome.recovery_task.id != task.id
    assert stack.recovery.source_for(outcome.recovery_task.id) == task.id
    assert outcome.report.source_task_id == task.id

    second_diagnosis = stack.service.diagnose(task.id)
    assert second_diagnosis.diagnosis is not None
    assert second_diagnosis.diagnosis.action is RecoveryAction.NONE
    assert not second_diagnosis.diagnosis.retryable
    assert "Already recovered successfully" in second_diagnosis.diagnosis.message

    with pytest.raises(RecoveryError, match="already recovered successfully"):
        stack.service.recover(task.id)
    assert len(stack.tasks.list(limit=10)) == 2


def test_stale_running_task_is_scanned_and_recovered_as_clone(tmp_path: Path) -> None:
    stack = make_stack(tmp_path)
    make_pdfs(stack.settings.workspace_dir / "Inputs")
    old = utc_now() - timedelta(minutes=11)
    stale = stack.tasks.create(
        Task(
            instruction="Merge PDFs",
            status=TaskStatus.RUNNING,
            created_at=old,
            updated_at=old,
        )
    )
    plan = merge_plan(
        inputs=["Inputs"], output="stale-merged.pdf", tool_id="pdf.merge_folders"
    )
    stack.execution.save_plan(stale.id, plan.to_dict(), plan.planner)

    scan = stack.service.scan()
    assert [task.id for task in scan.stale_tasks] == [stale.id]
    assert stack.service.diagnose(stale.id).stale

    outcome = stack.service.recover(stale.id)

    assert outcome.cloned
    assert outcome.source_task.status is TaskStatus.FAILED
    assert outcome.report is not None and outcome.report.success
    assert (stack.settings.workspace_dir / "stale-merged.pdf").is_file()


def test_non_stale_active_task_is_not_stolen_without_force(tmp_path: Path) -> None:
    stack = make_stack(tmp_path)
    active = stack.tasks.create(
        Task(instruction="Merge PDFs", status=TaskStatus.RUNNING)
    )

    with pytest.raises(RecoveryError, match="may still be active"):
        stack.service.recover(active.id)

    assert len(stack.tasks.list()) == 1
    assert stack.tasks.get(active.id).status is TaskStatus.RUNNING  # type: ignore[union-attr]


def test_scan_reports_old_temp_pdf_without_deleting_it(tmp_path: Path) -> None:
    stack = make_stack(tmp_path)
    workspace = stack.settings.workspace_dir
    orphan = workspace / ".merged.tmp-0123456789abcdef0123456789abcdef.pdf"
    fresh = workspace / ".new.tmp-fedcba9876543210fedcba9876543210.pdf"
    orphan.write_bytes(b"partial")
    fresh.write_bytes(b"partial")
    old_epoch = (utc_now() - timedelta(minutes=11)).timestamp()
    os.utime(orphan, (old_epoch, old_epoch))

    scan = stack.service.scan()

    assert scan.orphan_temp_pdfs == (orphan.resolve(),)
    assert orphan.is_file()
    assert fresh.is_file()


def test_invalid_stored_plan_is_reported_and_replanned(tmp_path: Path) -> None:
    stack = make_stack(tmp_path)
    make_pdfs(stack.settings.workspace_dir / "Inputs")
    failed = stack.tasks.create(
        Task(
            instruction="Merge PDFs",
            status=TaskStatus.FAILED,
            error="Stored plan is invalid",
        )
    )
    stack.execution.save_plan(failed.id, {"arguments": "not-an-object"}, "broken")

    diagnosis = stack.service.diagnose(failed.id)
    assert diagnosis.plan is None
    assert diagnosis.plan_error is not None
    assert "invalid arguments" in diagnosis.plan_error

    outcome = stack.service.recover(
        failed.id,
        options=PlannerOptions(
            input_folders=("Inputs",), output_path="replanned.pdf"
        ),
    )
    assert outcome.cloned
    assert outcome.report is not None and outcome.report.success


def test_manual_recovery_chain_is_capped_at_three(tmp_path: Path) -> None:
    stack = make_stack(tmp_path)
    tasks = [
        stack.tasks.create(
            Task(instruction=f"Task {index}", status=TaskStatus.FAILED)
        )
        for index in range(4)
    ]
    for source, clone in itertools.pairwise(tasks):
        stack.recovery.create_link(
            source_task_id=source.id, recovery_task_id=clone.id, reason="test"
        )

    with pytest.raises(RecoveryError, match="chain limit"):
        stack.service.recover(tasks[-1].id)


def test_cli_run_reports_recovery_and_doctor_health(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = ["--root", str(tmp_path)]
    assert main([*root, "demo", "pdf"]) == 0
    capsys.readouterr()
    (tmp_path / "workspace" / "merged.pdf").write_bytes(b"existing")

    assert (
        main(
            [
                *root,
                "run",
                "Merge PDFs from Folder A and Folder B",
                "--output",
                "merged.pdf",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "succeeded"
    assert report["recovered"] is True
    assert len(report["recovery_attempts"]) == 2
    assert report["tool_result"]["output"]["output_path"] == "merged-recovered.pdf"

    assert main([*root, "recovery", "list"]) == 0
    assert "retry_new_output" in capsys.readouterr().out

    assert main([*root, "task", "show", report["task_id"]]) == 0
    assert "Recovery attempts recorded: 2" in capsys.readouterr().out

    assert main([*root, "recovery", "scan", "--json"]) == 0
    scan = json.loads(capsys.readouterr().out)
    assert scan["stale_tasks"] == []
    assert scan["orphan_temp_pdfs"] == []

    assert main([*root, "doctor"]) == 0
    doctor = capsys.readouterr().out
    assert "PHASE 8 File Operations" in doctor
    assert "recorded attempts=2" in doctor
    assert "Bundled dynamic-tool example arguments: missing" in doctor


def test_cli_diagnose_and_recover_failed_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = ["--root", str(tmp_path)]
    assert main([*root, "demo", "pdf"]) == 0
    capsys.readouterr()
    (tmp_path / "workspace" / "merged.pdf").write_bytes(b"existing")

    assert (
        main(
            [
                *root,
                "run",
                "Merge PDFs from Folder A and Folder B",
                "--output",
                "merged.pdf",
                "--no-recovery",
                "--json",
            ]
        )
        == 1
    )
    failed = json.loads(capsys.readouterr().out)

    assert main([*root, "task", "diagnose", failed["task_id"], "--json"]) == 0
    diagnosis = json.loads(capsys.readouterr().out)
    assert diagnosis["diagnosis"]["category"] == "output_exists"

    assert main([*root, "task", "recover", failed["task_id"]]) == 0
    recovered_out = capsys.readouterr().out
    assert f"Source task: {failed['task_id']}" in recovered_out
    assert "Recovery task:" in recovered_out
    assert "Recovered: True" in recovered_out

    assert main([*root, "recovery", "show", failed["task_id"], "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["recovery_links"][0]["source_task_id"] == failed["task_id"]


def approve_word_count(root: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    assert main([*root, "tool-builder", "template", "word_count", "--run-tests"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                *root,
                "tool-builder",
                "approve",
                "custom.text_word_count@1.0.0",
                "--yes",
            ]
        )
        == 0
    )
    approval_out = capsys.readouterr().out
    assert "Final candidate state (after approval):" in approval_out
    assert "Status: approved" in approval_out
    assert "Enabled: True" in approval_out
    assert "Status: tested" not in approval_out
    return approval_out


def test_cli_missing_bundled_arguments_file_uses_embedded_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = ["--root", str(tmp_path)]
    approve_word_count(root, capsys)
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                *root,
                "tools",
                "invoke",
                "custom.text_word_count",
                "--arguments-file",
                "examples\\dynamic-tools\\word_count_arguments.json",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out)["words"] == 3
    assert "embedded copy" in captured.err


def test_cli_arguments_file_search_order_and_missing_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = ["--root", str(tmp_path)]
    approve_word_count(root, capsys)

    cwd = tmp_path / "other-cwd"
    cwd_file = cwd / "args.json"
    cwd_file.parent.mkdir(parents=True)
    cwd_file.write_text(json.dumps({"text": "one two three four"}), encoding="utf-8")
    root_file = tmp_path / "args.json"
    root_file.write_text(json.dumps({"text": "one two"}), encoding="utf-8")
    monkeypatch.chdir(cwd)

    assert (
        main(
            [
                *root,
                "tools",
                "invoke",
                "custom.text_word_count",
                "--arguments-file",
                "args.json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["words"] == 4

    cwd_file.unlink()
    assert (
        main(
            [
                *root,
                "tools",
                "invoke",
                "custom.text_word_count",
                "--arguments-file",
                "args.json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["words"] == 2

    assert (
        main(
            [
                *root,
                "tools",
                "invoke",
                "custom.text_word_count",
                "--arguments-file",
                "does-not-exist.json",
            ]
        )
        == 1
    )
    error = capsys.readouterr().err
    assert "Checked, in order:" in error
    assert str(cwd / "does-not-exist.json") in error
    assert str(tmp_path / "does-not-exist.json") in error
