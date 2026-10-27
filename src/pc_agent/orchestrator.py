"""Task orchestration with bounded deterministic self-healing.

Flow: plan -> approve -> run one allow-listed tool -> verify. Known safe
failures may pass through RETRYING a bounded number of times; every attempt is
recorded and the final result is verified again before success.

Phase 7 foundation: acceptance criteria are built from the plan before the
tool runs and evaluated with evidence afterwards; a checkpoint is saved the
moment the tool step completes so an interrupted task can resume into
verification without re-running the tool; cancellation is cooperative (a
database request and/or injected callback checked between stages); and a
verified success can be recorded as reusable workflow memory (evidence, not
proof - it is never trusted without re-verification).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .documents.office import inspect_csv, inspect_office, read_csv
from .domain.task import Task, TaskStatus
from .errors import (
    ApprovalUnavailableError,
    PcAgentError,
    PlanningError,
    ProviderError,
)
from .planning import (
    DeterministicPlanner,
    PlannerOptions,
    PlanningOutcome,
    TaskPlan,
    plan_with_provider,
)
from .providers.router import ModelRouter, ProviderAttempt
from .recovery import (
    FailureDiagnosis,
    RecoveryAction,
    RecoveryAttemptSummary,
    diagnose_tool_failure,
    diagnose_verification_failure,
)
from .storage.repositories import (
    ExecutionRepository,
    MemoryRepository,
    RecoveryAttemptRecord,
    RecoveryRepository,
    TaskRepository,
)
from .tools.registry import ToolContext, ToolRegistry, ToolResult
from .verification import VerificationReport, build_criteria, evaluate_criteria

logger = logging.getLogger(__name__)

CancellationCallback = Callable[[], bool]


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

_ALLOWED_ORCHESTRATED_TOOLS = frozenset(
    {
        "pdf.merge_folders",
        "pdf.merge",
        "pdf.verify",
        "pdf.inspect",
        "workspace.list_files",
        "office.inspect",
        "office.extract_text",
        "office.csv_inspect",
        "office.csv_to_json",
        "office.json_to_csv",
        "office.csv_to_xlsx",
        "desktop.system_info",
        "files.copy",
        "files.move",
        "files.rename",
        "files.organize",
        "files.search",
    }
)


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    task_id: str
    plan: TaskPlan
    read_roots: tuple[Path, ...]

    @property
    def action(self) -> str:
        return "read_external_inputs"


ApprovalCallback = Callable[[ApprovalRequest], bool]


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    task: Task
    plan: TaskPlan | None = None
    tool_result: ToolResult | None = None
    verification_result: ToolResult | None = None
    verification_valid: bool | None = None
    verification_message: str = ""
    missing_information: tuple[str, ...] = ()
    provider_attempts: tuple[ProviderAttempt, ...] = ()
    recovery_attempts: tuple[RecoveryAttemptSummary, ...] = ()
    recovered: bool = False
    source_task_id: str | None = None
    error: str | None = None
    verification_report: VerificationReport | None = None
    resumed_from_checkpoint: bool = False

    @property
    def success(self) -> bool:
        return self.task.status is TaskStatus.SUCCEEDED

    @property
    def cancelled(self) -> bool:
        return self.task.status is TaskStatus.CANCELLED


class TaskOrchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        task_repository: TaskRepository,
        execution_repository: ExecutionRepository,
        tool_registry: ToolRegistry,
        deterministic_planner: DeterministicPlanner,
        model_router: ModelRouter | None = None,
        recovery_repository: RecoveryRepository | None = None,
        memory_repository: MemoryRepository | None = None,
        max_recovery_attempts: int | None = None,
        recovery_backoff_seconds: float | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._tasks = task_repository
        self._execution = execution_repository
        self._tools = tool_registry
        self._planner = deterministic_planner
        self._router = model_router
        self._recovery = recovery_repository
        self._memory = memory_repository
        self._max_recovery_attempts = self._validate_recovery_limit(
            settings.max_recovery_attempts
            if max_recovery_attempts is None
            else max_recovery_attempts
        )
        backoff = (
            settings.recovery_backoff_seconds
            if recovery_backoff_seconds is None
            else recovery_backoff_seconds
        )
        if backoff < 0 or backoff > 5:
            raise ValueError("recovery_backoff_seconds must be between 0 and 5.")
        self._recovery_backoff_seconds = backoff
        self._sleeper = sleeper

    @staticmethod
    def _validate_recovery_limit(value: int) -> int:
        if value < 0 or value > 3:
            raise ValueError("max_recovery_attempts must be between 0 and 3.")
        return value

    def execute(
        self,
        task_id: str,
        *,
        options: PlannerOptions | None = None,
        approval_callback: ApprovalCallback | None = None,
        use_ai: bool = False,
        max_recovery_attempts: int | None = None,
        resume_stored_plan: bool = False,
        source_task_id: str | None = None,
        cancellation_requested: CancellationCallback | None = None,
    ) -> ExecutionReport:
        options = options or PlannerOptions()
        recovery_limit = self._validate_recovery_limit(
            self._max_recovery_attempts
            if max_recovery_attempts is None
            else max_recovery_attempts
        )
        task = self._require_task(task_id)
        if task.is_terminal:
            raise PcAgentError(
                f"Task {task.id} is already {task.status.value}; create a new task to run it again."
            )
        if self._is_cancellation_requested(task.id, cancellation_requested):
            return self._cancelled_report(
                task, source_task_id=source_task_id
            )

        if task.status is TaskStatus.WAITING_FOR_APPROVAL:
            plan, failure = self._stored_plan(task, source_task_id=source_task_id)
            if failure is not None:
                return failure
            if plan is None:  # Defensive; _stored_plan returns a failure in this case.
                raise PcAgentError("Stored plan could not be loaded.")
            return self._approve_and_run(
                task,
                plan,
                approval_callback,
                (),
                max_recovery_attempts=recovery_limit,
                source_task_id=source_task_id,
                initial_action=RecoveryAction.RESUME_STORED_PLAN,
                cancellation_requested=cancellation_requested,
            )

        if task.status not in {TaskStatus.QUEUED, TaskStatus.NEEDS_INPUT}:
            raise PcAgentError(
                f"Task {task.id} cannot be executed from status {task.status.value}."
            )

        if resume_stored_plan:
            task = self._tasks.update_status(
                task.id, TaskStatus.PLANNING, message="Resuming stored plan"
            )
            plan, failure = self._stored_plan(task, source_task_id=source_task_id)
            if failure is not None:
                return failure
            if plan is None:  # Defensive; _stored_plan returns a failure in this case.
                raise PcAgentError("Stored plan could not be loaded.")
            return self._approve_and_run(
                task,
                plan,
                approval_callback,
                (),
                max_recovery_attempts=recovery_limit,
                source_task_id=source_task_id,
                initial_action=RecoveryAction.RESUME_STORED_PLAN,
                cancellation_requested=cancellation_requested,
            )

        task = self._tasks.update_status(
            task.id, TaskStatus.PLANNING, message="Planning started"
        )

        outcome = self._planner.plan(task.instruction, options)
        if outcome is None:
            if not use_ai:
                outcome = PlanningOutcome(
                    missing_information=(
                        (
                            "This instruction is not a supported deterministic capability yet. "
                            "Re-run with --use-ai to request a constrained provider plan, or use "
                            "a direct supported pdf/tools command."
                        ),
                    )
                )
            elif self._router is None:
                outcome = PlanningOutcome(
                    missing_information=("AI planning was requested but no model router is configured.",),
                    planner="ai-unavailable",
                )
            else:
                try:
                    outcome = plan_with_provider(self._router, task.instruction, options)
                except (ProviderError, PcAgentError) as exc:
                    failed = self._tasks.update_status(
                        task.id,
                        TaskStatus.FAILED,
                        message="AI planning failed",
                        error=str(exc),
                    )
                    return ExecutionReport(
                        task=failed, source_task_id=source_task_id, error=str(exc)
                    )

        if outcome.plan is None:
            message = " ".join(outcome.missing_information) or "More information is required."
            needs_input = self._tasks.update_status(
                task.id,
                TaskStatus.NEEDS_INPUT,
                message=message,
                error=message,
            )
            return ExecutionReport(
                task=needs_input,
                missing_information=outcome.missing_information,
                provider_attempts=outcome.provider_attempts,
                source_task_id=source_task_id,
                error=message,
            )

        plan = outcome.plan
        if plan.tool_id not in _ALLOWED_ORCHESTRATED_TOOLS:
            message = f"Plan requested a tool that orchestration does not allow: {plan.tool_id}"
            failed = self._tasks.update_status(
                task.id, TaskStatus.FAILED, message="Unsafe plan rejected", error=message
            )
            return ExecutionReport(
                task=failed,
                plan=plan,
                provider_attempts=outcome.provider_attempts,
                source_task_id=source_task_id,
                error=message,
            )

        if self._is_cancellation_requested(task.id, cancellation_requested):
            return self._cancelled_report(
                task,
                plan=plan,
                provider_attempts=outcome.provider_attempts,
                source_task_id=source_task_id,
            )
        self._execution.save_plan(task.id, plan.to_dict(), plan.planner)
        self._tasks.record_event(task.id, "plan_created", plan.summary)
        return self._approve_and_run(
            task,
            plan,
            approval_callback,
            outcome.provider_attempts,
            max_recovery_attempts=recovery_limit,
            source_task_id=source_task_id,
            initial_action=RecoveryAction.EXECUTE_PLAN,
            cancellation_requested=cancellation_requested,
        )

    def resume(
        self,
        task_id: str,
        *,
        options: PlannerOptions | None = None,
        approval_callback: ApprovalCallback | None = None,
        use_ai: bool = False,
        max_recovery_attempts: int | None = None,
        force: bool = False,
        cancellation_requested: CancellationCallback | None = None,
    ) -> ExecutionReport:
        """Resume an interrupted (non-terminal) task in place.

        Queued/needs-input tasks simply execute. Active-looking tasks must be
        stale (or ``force`` given) so a second process never runs the same
        task twice. A checkpointed tool result is reused when its plan
        fingerprint still matches and its artifacts still exist; otherwise
        the stored plan is executed again. Failed terminal tasks keep using
        the audited recovery-clone flow instead.
        """

        recovery_limit = self._validate_recovery_limit(
            self._max_recovery_attempts
            if max_recovery_attempts is None
            else max_recovery_attempts
        )
        task = self._require_task(task_id)
        if task.is_terminal:
            raise PcAgentError(
                f"Task {task.id} is already {task.status.value} and cannot be resumed. "
                "Use 'task recover' for a failed task or create a new task."
            )
        if task.status in {TaskStatus.QUEUED, TaskStatus.NEEDS_INPUT}:
            return self.execute(
                task.id,
                options=options,
                approval_callback=approval_callback,
                use_ai=use_ai,
                max_recovery_attempts=recovery_limit,
                resume_stored_plan=self._execution.get_plan(task.id) is not None,
                cancellation_requested=cancellation_requested,
            )

        stale_after = timedelta(minutes=self._settings.recovery_stale_after_minutes)
        updated_at = task.updated_at
        if not force and updated_at is not None:
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            if datetime.now(UTC) - updated_at < stale_after:
                raise PcAgentError(
                    f"Task {task.id} still looks active (status {task.status.value}, "
                    f"last updated {updated_at.isoformat()}). Wait for it to become "
                    "stale, or resume with force only if no other pc-agent process "
                    "is running it."
                )

        # An explicit resume is newer user intent than an old cancel request
        # left behind by the interrupted process.
        self._execution.clear_cancellation(task.id)
        plan, failure = self._stored_plan(task)
        if failure is not None:
            return failure
        if plan is None:  # Defensive; _stored_plan returns a failure in this case.
            raise PcAgentError("Stored plan could not be loaded.")

        if task.status is not TaskStatus.PLANNING:
            task = self._tasks.update_status(
                task.id,
                TaskStatus.PLANNING,
                message="Resuming interrupted task with stored plan/checkpoints",
            )
        resumed_result = self._checkpoint_result(task, plan)
        if resumed_result is not None and (
            not plan.external_read_roots
            or self._approval_previously_granted(task.id, plan)
        ):
            return self._run_with_recovery(
                task,
                plan,
                approval_callback,
                (),
                max_recovery_attempts=recovery_limit,
                source_task_id=None,
                initial_action=RecoveryAction.RESUME_STORED_PLAN,
                cancellation_requested=cancellation_requested,
                resumed_result=resumed_result,
            )
        return self._approve_and_run(
            task,
            plan,
            approval_callback,
            (),
            max_recovery_attempts=recovery_limit,
            source_task_id=None,
            initial_action=RecoveryAction.RESUME_STORED_PLAN,
            cancellation_requested=cancellation_requested,
            resumed_result=resumed_result,
        )

    def request_cancellation(
        self, task_id: str, *, reason: str | None = None
    ) -> tuple[Task, str]:
        """Cancel now (queued tasks) or request cooperative cancellation."""

        task = self._require_task(task_id)
        if task.is_terminal:
            raise PcAgentError(
                f"Task {task.id} is already {task.status.value}; nothing to cancel."
            )
        if task.status in {TaskStatus.QUEUED, TaskStatus.NEEDS_INPUT}:
            cancelled = self._tasks.update_status(
                task.id,
                TaskStatus.CANCELLED,
                message="Cancelled by user before execution",
                error="Cancelled by user before execution.",
            )
            self._execution.clear_cancellation(task.id)
            self._tasks.record_event(task.id, "cancelled", reason or "Cancelled by user.")
            return cancelled, "cancelled"
        self._execution.request_cancellation(task.id, reason=reason)
        self._tasks.record_event(
            task.id, "cancellation_requested", reason or "Cancellation requested by user."
        )
        return task, "requested"

    def _is_cancellation_requested(
        self, task_id: str, callback: CancellationCallback | None
    ) -> bool:
        if callback is not None and callback():
            return True
        return self._execution.cancellation_requested(task_id)

    def _cancelled_report(
        self,
        task: Task,
        *,
        plan: TaskPlan | None = None,
        provider_attempts: tuple[ProviderAttempt, ...] = (),
        source_task_id: str | None = None,
        tool_result: ToolResult | None = None,
        verification_result: ToolResult | None = None,
        verification_valid: bool | None = None,
        verification_message: str = "",
        verification_report: VerificationReport | None = None,
        recovery_attempts: tuple[RecoveryAttemptSummary, ...] = (),
        resumed_from_checkpoint: bool = False,
    ) -> ExecutionReport:
        message = "Cancelled by user; no further steps were executed."
        cancelled = self._tasks.update_status(
            task.id, TaskStatus.CANCELLED, message=message, error=message
        )
        self._execution.clear_cancellation(task.id)
        self._tasks.record_event(task.id, "cancelled", message)
        return ExecutionReport(
            task=cancelled,
            plan=plan,
            tool_result=tool_result,
            verification_result=verification_result,
            verification_valid=verification_valid,
            verification_message=verification_message,
            provider_attempts=provider_attempts,
            recovery_attempts=recovery_attempts,
            recovered=False,
            source_task_id=source_task_id,
            error=message,
            verification_report=verification_report,
            resumed_from_checkpoint=resumed_from_checkpoint,
        )

    @staticmethod
    def _plan_fingerprint(plan: TaskPlan) -> str:
        canonical = json.dumps(plan.to_dict(), sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _artifact_paths(output: Mapping[str, Any] | None) -> tuple[str, ...]:
        if not output:
            return ()
        artifacts: list[str] = []
        for key in ("output_path", "path"):
            value = output.get(key)
            if isinstance(value, str) and value and value not in artifacts:
                artifacts.append(value)
        return tuple(artifacts)

    def _checkpoint_result(self, task: Task, plan: TaskPlan) -> ToolResult | None:
        checkpoint = self._execution.get_checkpoint(task.id, 0)
        if checkpoint is None or checkpoint.output is None:
            return None
        if checkpoint.plan_fingerprint != self._plan_fingerprint(plan):
            return None
        workspace = self._settings.workspace_dir.expanduser().resolve()
        for artifact in checkpoint.artifacts:
            candidate = Path(artifact).expanduser()
            target = (
                candidate.resolve()
                if candidate.is_absolute()
                else (workspace / candidate).resolve()
            )
            if target.is_relative_to(workspace) and not target.is_file():
                self._execution.delete_checkpoint(task.id, 0)
                self._tasks.record_event(
                    task.id,
                    "checkpoint_invalidated",
                    f"Checkpoint artifact is missing ({artifact}); the tool step will run again.",
                )
                return None
        return ToolResult(
            tool_id=checkpoint.tool_id,
            version=checkpoint.tool_version,
            success=True,
            output=dict(checkpoint.output),
            elapsed_ms=0.0,
        )

    def _approval_previously_granted(self, task_id: str, plan: TaskPlan) -> bool:
        expected_roots = [str(path) for path in plan.external_read_roots]
        for record in self._execution.list_approvals(task_id):
            if record.decision != "approved":
                continue
            if record.scope.get("tool_id") not in (None, plan.tool_id):
                continue
            if list(record.scope.get("read_roots") or []) == expected_roots:
                return True
        return False

    def _record_workflow_memory(
        self, task: Task, plan: TaskPlan, result: ToolResult
    ) -> None:
        if self._memory is None:
            return
        try:
            key = "wf-" + hashlib.sha256(
                f"{plan.intent}|{plan.tool_id}".encode()
            ).hexdigest()[:16]
            existing = self._memory.get("workflow", key)
            success_count = 1
            if existing is not None:
                previous = existing.content.get("success_count")
                if isinstance(previous, int):
                    success_count = previous + 1
            output = result.output or {}
            self._memory.upsert(
                kind="workflow",
                key=key,
                title=plan.summary,
                content={
                    "instruction": task.instruction,
                    "intent": plan.intent,
                    "tool_id": plan.tool_id,
                    "planner": plan.planner,
                    "output_path": output.get("output_path"),
                    "success_count": success_count,
                    "last_task_id": task.id,
                },
                source_task_id=task.id,
            )
        except PcAgentError as exc:
            # Memory is supporting evidence, never a reason to fail a task
            # that already executed and verified successfully.
            logger.warning("Workflow memory could not be recorded: %s", exc)

    def _stored_plan(
        self, task: Task, *, source_task_id: str | None = None
    ) -> tuple[TaskPlan | None, ExecutionReport | None]:
        try:
            stored = self._execution.get_plan(task.id)
        except (PcAgentError, TypeError, ValueError) as exc:
            message = f"Stored plan could not be read: {exc}"
            failed = self._tasks.update_status(
                task.id, TaskStatus.FAILED, message="Stored plan invalid", error=message
            )
            return None, ExecutionReport(
                task=failed, source_task_id=source_task_id, error=message
            )
        if stored is None:
            message = "Task cannot resume because no stored plan was found."
            failed = self._tasks.update_status(
                task.id, TaskStatus.FAILED, message="Stored plan missing", error=message
            )
            return None, ExecutionReport(
                task=failed, source_task_id=source_task_id, error=message
            )
        try:
            plan = TaskPlan.from_dict(stored)
        except (PlanningError, TypeError, ValueError) as exc:
            message = f"Stored plan is invalid: {exc}"
            failed = self._tasks.update_status(
                task.id, TaskStatus.FAILED, message="Stored plan invalid", error=message
            )
            return None, ExecutionReport(
                task=failed, source_task_id=source_task_id, error=message
            )
        if plan.tool_id not in _ALLOWED_ORCHESTRATED_TOOLS:
            message = f"Stored plan requested a disallowed tool: {plan.tool_id}"
            failed = self._tasks.update_status(
                task.id, TaskStatus.FAILED, message="Unsafe stored plan rejected", error=message
            )
            return None, ExecutionReport(
                task=failed, plan=plan, source_task_id=source_task_id, error=message
            )
        return plan, None

    def _approve_and_run(
        self,
        task: Task,
        plan: TaskPlan,
        approval_callback: ApprovalCallback | None,
        provider_attempts: tuple[ProviderAttempt, ...],
        *,
        max_recovery_attempts: int,
        source_task_id: str | None,
        initial_action: RecoveryAction,
        cancellation_requested: CancellationCallback | None = None,
        resumed_result: ToolResult | None = None,
    ) -> ExecutionReport:
        task, approved = self._request_approval(task, plan, approval_callback)
        if not approved:
            message = "Approval was denied; the tool was not executed."
            cancelled = self._tasks.update_status(
                task.id,
                TaskStatus.CANCELLED,
                message=message,
                error=message,
            )
            return ExecutionReport(
                task=cancelled,
                plan=plan,
                provider_attempts=provider_attempts,
                source_task_id=source_task_id,
                error=message,
            )
        if self._is_cancellation_requested(task.id, cancellation_requested):
            return self._cancelled_report(
                task,
                plan=plan,
                provider_attempts=provider_attempts,
                source_task_id=source_task_id,
            )
        return self._run_with_recovery(
            task,
            plan,
            approval_callback,
            provider_attempts,
            max_recovery_attempts=max_recovery_attempts,
            source_task_id=source_task_id,
            initial_action=initial_action,
            cancellation_requested=cancellation_requested,
            resumed_result=resumed_result,
        )

    def _request_approval(
        self,
        task: Task,
        plan: TaskPlan,
        approval_callback: ApprovalCallback | None,
    ) -> tuple[Task, bool]:
        if not plan.external_read_roots:
            return task, True
        if task.status is TaskStatus.PLANNING:
            task = self._tasks.update_status(
                task.id,
                TaskStatus.WAITING_FOR_APPROVAL,
                message="Approval required for read-only external input folder(s)",
            )
        elif task.status is not TaskStatus.WAITING_FOR_APPROVAL:
            raise PcAgentError(
                f"Approval cannot be requested from task status {task.status.value}."
            )
        if approval_callback is None:
            raise ApprovalUnavailableError(
                "Approval is required for external input paths, but no approval "
                "prompt is available. The task remains waiting_for_approval."
            )
        request = ApprovalRequest(task.id, plan, plan.external_read_roots)
        approved = bool(approval_callback(request))
        self._execution.record_approval(
            task.id,
            request.action,
            approved=approved,
            scope={
                "tool_id": plan.tool_id,
                "read_roots": [str(path) for path in plan.external_read_roots],
                "arguments": dict(plan.arguments),
                "permissions": "read/list only; no write/delete outside workspace",
            },
        )
        self._tasks.record_event(
            task.id,
            "approval_granted" if approved else "approval_denied",
            "Read-only external input scope was reviewed.",
        )
        return task, approved

    def _run_with_recovery(
        self,
        task: Task,
        plan: TaskPlan,
        approval_callback: ApprovalCallback | None,
        provider_attempts: tuple[ProviderAttempt, ...],
        *,
        max_recovery_attempts: int,
        source_task_id: str | None,
        initial_action: RecoveryAction,
        cancellation_requested: CancellationCallback | None = None,
        resumed_result: ToolResult | None = None,
    ) -> ExecutionReport:
        summaries: list[RecoveryAttemptSummary] = []
        recovery_used = 0
        attempt_number = 1
        next_action = initial_action
        resumed_used = False

        while True:
            if self._is_cancellation_requested(task.id, cancellation_requested):
                return self._cancelled_report(
                    task,
                    plan=plan,
                    provider_attempts=provider_attempts,
                    source_task_id=source_task_id,
                    recovery_attempts=tuple(summaries),
                    resumed_from_checkpoint=resumed_used,
                )
            criteria = build_criteria(plan)
            run_message = (
                plan.summary
                if attempt_number == 1
                else f"Recovery attempt {recovery_used}: {plan.summary}"
            )
            task = self._tasks.update_status(
                task.id, TaskStatus.RUNNING, message=run_message
            )
            context = ToolContext.for_workspace(
                self._settings.workspace_dir,
                approved_read_roots=plan.external_read_roots,
            )
            attempt_record = self._start_attempt(
                task_id=task.id,
                source_task_id=source_task_id,
                attempt_number=attempt_number,
                action=next_action,
                plan=plan,
            )
            if resumed_result is not None and attempt_number == 1:
                # Interrupted-task resume: the tool step already completed and
                # was checkpointed, so reuse its recorded result and go
                # straight to (re-)verification instead of re-running it.
                result = resumed_result
                resumed_used = True
                resumed_result = None
                self._tasks.record_event(
                    task.id,
                    "checkpoint_resumed",
                    f"Reused checkpointed {plan.tool_id} result; tool was not re-run.",
                )
            else:
                result = self._tools.invoke(plan.tool_id, plan.arguments, context)
                self._execution.record_tool_invocation(task.id, plan.arguments, result)
                if result.success and result.output is not None:
                    self._execution.save_checkpoint(
                        task.id,
                        step_index=0,
                        step_key=f"tool:{plan.tool_id}",
                        result=result,
                        plan_fingerprint=self._plan_fingerprint(plan),
                        artifacts=self._artifact_paths(result.output),
                    )
            if self._is_cancellation_requested(task.id, cancellation_requested):
                return self._cancelled_report(
                    task,
                    plan=plan,
                    provider_attempts=provider_attempts,
                    source_task_id=source_task_id,
                    tool_result=result,
                    recovery_attempts=tuple(summaries),
                    resumed_from_checkpoint=resumed_used,
                )

            verification_result: ToolResult | None = None
            verification_valid: bool | None = None
            verification_message = ""
            verification_report: VerificationReport | None = None
            diagnosis: FailureDiagnosis
            failure_stage: str
            failure_message: str

            if result.success and result.output is not None:
                task = self._tasks.update_status(
                    task.id,
                    TaskStatus.VERIFYING,
                    message="Verifying tool output deterministically",
                )
                verification_result, legacy_valid, verification_message = self._verify(
                    task.id, plan, result, context
                )
                verification_report = evaluate_criteria(
                    criteria,
                    workspace_dir=self._settings.workspace_dir,
                    tool_output=result.output,
                    legacy_valid=legacy_valid,
                    legacy_message=verification_message,
                )
                valid = verification_report.passed
                if legacy_valid and not valid:
                    verification_message = verification_report.failure_summary()
                verification_valid = valid
                self._execution.save_verification(
                    task.id, verification_report.to_dict()
                )
                if valid:
                    summaries.append(
                        self._finish_attempt(
                            attempt_record,
                            attempt_number=attempt_number,
                            stage="verification",
                            status="succeeded",
                            action=next_action,
                            message=verification_message,
                        )
                    )
                    succeeded = self._tasks.update_status(
                        task.id,
                        TaskStatus.SUCCEEDED,
                        message="Execution and verification completed",
                    )
                    self._tasks.record_event(task.id, "verified", verification_message)
                    if recovery_used:
                        self._tasks.record_event(
                            task.id,
                            "recovered",
                            f"Recovered after {recovery_used} bounded recovery attempt(s).",
                        )
                    self._record_workflow_memory(succeeded, plan, result)
                    return ExecutionReport(
                        task=succeeded,
                        plan=plan,
                        tool_result=result,
                        verification_result=verification_result,
                        verification_valid=True,
                        verification_message=verification_message,
                        provider_attempts=provider_attempts,
                        recovery_attempts=tuple(summaries),
                        recovered=recovery_used > 0,
                        source_task_id=source_task_id,
                        verification_report=verification_report,
                        resumed_from_checkpoint=resumed_used,
                    )
                failure_stage = "verification"
                failure_message = verification_message
                diagnosis = diagnose_verification_failure(
                    plan,
                    verification_message,
                    workspace_dir=self._settings.workspace_dir,
                )
            else:
                failure_stage = "tool_execution"
                failure_message = result.error or "Tool failed without an error message."
                diagnosis = diagnose_tool_failure(
                    plan,
                    failure_message,
                    workspace_dir=self._settings.workspace_dir,
                )

            can_retry = diagnosis.retryable and recovery_used < max_recovery_attempts
            revised_plan = (
                self._revise_plan(plan, diagnosis) if diagnosis.revised_arguments else plan
            )
            summaries.append(
                self._finish_attempt(
                    attempt_record,
                    attempt_number=attempt_number,
                    stage=failure_stage,
                    status="retrying" if can_retry else "failed",
                    action=diagnosis.action,
                    diagnosis=diagnosis,
                    plan_after=revised_plan if can_retry else None,
                )
            )

            if can_retry:
                recovery_used += 1
                task = self._tasks.update_status(
                    task.id,
                    TaskStatus.RETRYING,
                    message=diagnosis.message,
                    error=failure_message,
                )
                self._tasks.record_event(
                    task.id,
                    "recovery_scheduled",
                    f"{diagnosis.category.value}: {diagnosis.message}",
                )

                if revised_plan != plan:
                    plan = revised_plan
                    self._execution.save_plan(task.id, plan.to_dict(), plan.planner)
                    self._tasks.record_event(
                        task.id, "recovery_plan_revised", diagnosis.message
                    )
                    if plan.external_read_roots:
                        task = self._tasks.update_status(
                            task.id,
                            TaskStatus.PLANNING,
                            message="Revised plan requires approval again",
                        )
                        task, approved = self._request_approval(
                            task, plan, approval_callback
                        )
                        if not approved:
                            message = (
                                "Approval was denied for the revised recovery plan; "
                                "no further tool was executed."
                            )
                            cancelled = self._tasks.update_status(
                                task.id,
                                TaskStatus.CANCELLED,
                                message=message,
                                error=message,
                            )
                            return ExecutionReport(
                                task=cancelled,
                                plan=plan,
                                tool_result=result,
                                verification_result=verification_result,
                                verification_valid=verification_valid,
                                verification_message=verification_message,
                                provider_attempts=provider_attempts,
                                recovery_attempts=tuple(summaries),
                                recovered=False,
                                source_task_id=source_task_id,
                                error=message,
                            )

                if self._is_cancellation_requested(task.id, cancellation_requested):
                    return self._cancelled_report(
                        task,
                        plan=plan,
                        provider_attempts=provider_attempts,
                        source_task_id=source_task_id,
                        tool_result=result,
                        verification_result=verification_result,
                        verification_valid=verification_valid,
                        verification_message=verification_message,
                        verification_report=verification_report,
                        recovery_attempts=tuple(summaries),
                        resumed_from_checkpoint=resumed_used,
                    )
                next_action = diagnosis.action
                attempt_number += 1
                delay = min(
                    5.0,
                    self._recovery_backoff_seconds * (2 ** (recovery_used - 1)),
                )
                if delay:
                    self._sleeper(delay)
                continue

            if diagnosis.needs_input and task.status in {
                TaskStatus.RUNNING,
                TaskStatus.VERIFYING,
            }:
                missing = (failure_message, diagnosis.message)
                needs_input = self._tasks.update_status(
                    task.id,
                    TaskStatus.NEEDS_INPUT,
                    message=diagnosis.message,
                    error=failure_message,
                )
                return ExecutionReport(
                    task=needs_input,
                    plan=plan,
                    tool_result=result,
                    verification_result=verification_result,
                    verification_valid=verification_valid,
                    verification_message=verification_message,
                    missing_information=missing,
                    provider_attempts=provider_attempts,
                    recovery_attempts=tuple(summaries),
                    recovered=False,
                    source_task_id=source_task_id,
                    error=failure_message,
                    verification_report=verification_report,
                    resumed_from_checkpoint=resumed_used,
                )

            final_message = (
                "Tool execution failed"
                if failure_stage == "tool_execution"
                else "Verification failed"
            )
            failed = self._tasks.update_status(
                task.id,
                TaskStatus.FAILED,
                message=final_message,
                error=failure_message,
            )
            return ExecutionReport(
                task=failed,
                plan=plan,
                tool_result=result,
                verification_result=verification_result,
                verification_valid=verification_valid,
                verification_message=verification_message,
                provider_attempts=provider_attempts,
                recovery_attempts=tuple(summaries),
                recovered=False,
                source_task_id=source_task_id,
                error=failure_message,
                verification_report=verification_report,
                resumed_from_checkpoint=resumed_used,
            )

    def _start_attempt(
        self,
        *,
        task_id: str,
        source_task_id: str | None,
        attempt_number: int,
        action: RecoveryAction,
        plan: TaskPlan,
    ) -> RecoveryAttemptRecord | None:
        if self._recovery is None:
            return None
        return self._recovery.start_attempt(
            task_id,
            attempt_number=attempt_number,
            stage="execution",
            action=action.value,
            source_task_id=source_task_id,
            plan_before=plan.to_dict(),
            message=f"Attempt {attempt_number} started.",
        )

    def _finish_attempt(
        self,
        record: RecoveryAttemptRecord | None,
        *,
        attempt_number: int,
        stage: str,
        status: str,
        action: RecoveryAction,
        diagnosis: FailureDiagnosis | None = None,
        message: str | None = None,
        plan_after: TaskPlan | None = None,
    ) -> RecoveryAttemptSummary:
        summary = RecoveryAttemptSummary(
            attempt_number=attempt_number,
            stage=stage,
            status=status,
            action=action.value,
            category=diagnosis.category.value if diagnosis else None,
            retryable=diagnosis.retryable if diagnosis else False,
            message=diagnosis.message if diagnosis else message,
            original_error=diagnosis.original_error if diagnosis else None,
        )
        if record is not None and self._recovery is not None:
            self._recovery.finish_attempt(
                record.id,
                status=status,
                stage=stage,
                category=summary.category,
                action=action.value,
                retryable=summary.retryable,
                original_error=summary.original_error,
                message=summary.message,
                plan_after=plan_after.to_dict() if plan_after is not None else None,
            )
        return summary

    @staticmethod
    def _revise_plan(plan: TaskPlan, diagnosis: FailureDiagnosis) -> TaskPlan:
        if diagnosis.revised_arguments is None:
            return plan
        planner = (
            plan.planner if plan.planner.endswith("+recovery") else f"{plan.planner}+recovery"
        )
        return replace(
            plan,
            arguments=copy.deepcopy(dict(diagnosis.revised_arguments)),
            summary=f"{plan.summary} Recovery: {diagnosis.message}",
            planner=planner,
        )

    def _verify(
        self,
        task_id: str,
        plan: TaskPlan,
        result: ToolResult,
        context: ToolContext,
    ) -> tuple[ToolResult | None, bool, str]:
        output: Mapping[str, Any] = result.output or {}

        if plan.tool_id in {"pdf.merge", "pdf.merge_folders"}:
            expected = plan.expected_pages or output.get("page_count")
            arguments: dict[str, Any] = {"path": output.get("output_path")}
            if isinstance(expected, int):
                arguments["expected_pages"] = expected
            verification = self._tools.invoke("pdf.verify", arguments, context)
            self._execution.record_tool_invocation(task_id, arguments, verification)
            if not verification.success or verification.output is None:
                return verification, False, verification.error or "PDF verification tool failed."
            valid = bool(verification.output.get("valid"))
            same_hash = verification.output.get("sha256") == output.get("sha256")
            if valid and same_hash:
                return verification, True, "Merged PDF was re-opened and page count/SHA-256 matched."
            return verification, False, "Merged PDF did not pass re-open/page-count verification."

        if plan.tool_id == "pdf.verify":
            valid = bool(output.get("valid"))
            message = (
                "PDF verification reported valid."
                if valid
                else f"PDF verification reported invalid: {output.get('error') or 'unknown reason'}"
            )
            return None, valid, message

        if plan.tool_id == "pdf.inspect":
            pages = output.get("pages")
            valid = isinstance(pages, int) and pages > 0
            return None, valid, "PDF page count was positive." if valid else "Invalid page count."

        if plan.tool_id == "workspace.list_files":
            files = output.get("files")
            count = output.get("count")
            if not isinstance(files, list) or count != len(files):
                return None, False, "File-list count did not match the returned files."
            for item in files:
                candidate = Path(str(item))
                target = candidate if candidate.is_absolute() else self._settings.workspace_dir / candidate
                if not target.is_file():
                    return None, False, f"Listed file no longer exists: {item}"
            return None, True, "File-list count and listed paths were re-checked."

        if plan.tool_id == "office.inspect":
            return self._verify_office_inspect(output)

        if plan.tool_id == "office.csv_inspect":
            return self._verify_csv_inspect(output)

        if plan.tool_id == "office.extract_text":
            return self._verify_office_extract(plan, output)

        if plan.tool_id in {
            "office.csv_to_json",
            "office.json_to_csv",
            "office.csv_to_xlsx",
        }:
            return self._verify_office_conversion(plan.tool_id, output)

        if plan.tool_id == "desktop.system_info":
            workspace_matches = output.get("workspace") == str(
                self._settings.workspace_dir.expanduser().resolve()
            )
            cpu_count = output.get("cpu_count")
            valid = (
                workspace_matches
                and isinstance(cpu_count, int)
                and cpu_count > 0
                and bool(output.get("operating_system"))
                and bool(output.get("python_version"))
            )
            return (
                None,
                valid,
                "System information workspace and core fields were re-checked."
                if valid
                else "System information did not match the configured workspace/core fields.",
            )

        if plan.tool_id in {"files.copy", "files.move", "files.rename"}:
            return self._verify_file_transfer(
                output, source_must_exist=plan.tool_id == "files.copy"
            )

        if plan.tool_id == "files.organize":
            return self._verify_organize(output)

        if plan.tool_id == "files.search":
            return self._verify_search(output)

        return None, False, f"No deterministic verifier is registered for {plan.tool_id}."

    def _verify_file_transfer(
        self, output: Mapping[str, Any], *, source_must_exist: bool
    ) -> tuple[None, bool, str]:
        destination = self._workspace_file(output.get("output_path"))
        if destination is None:
            return None, False, "Transferred file is missing or outside the workspace."
        reported_hash = output.get("sha256")
        if not isinstance(reported_hash, str) or not reported_hash:
            return None, False, "The transfer did not report a SHA-256."
        if _stream_sha256(destination) != reported_hash:
            return None, False, "Destination SHA-256 does not match the transfer report."
        source = self._workspace_file(output.get("path"))
        if source_must_exist:
            if source is None:
                return None, False, "Copy source no longer exists."
            if _stream_sha256(source) != reported_hash:
                return None, False, "Source bytes changed or differ from the copy."
            return None, True, "Copy destination and source SHA-256 both match."
        if source is not None:
            return None, False, "Move/rename source path still exists."
        return None, True, "Destination SHA-256 matches and the source path is gone."

    def _verify_organize(
        self, output: Mapping[str, Any]
    ) -> tuple[None, bool, str]:
        moved = output.get("moved")
        if not isinstance(moved, list) or output.get("moved_count") != len(moved):
            return None, False, "Organize moved count did not match the moved list."
        for item in moved:
            if not isinstance(item, dict):
                return None, False, "Organize reported a malformed moved entry."
            if self._workspace_file(item.get("to")) is None:
                return None, False, f"Organized file is missing: {item.get('to')}"
            if self._workspace_file(item.get("from")) is not None:
                return None, False, f"Organize source still exists: {item.get('from')}"
        return None, True, "Every organized file was re-checked at its new path."

    def _verify_search(self, output: Mapping[str, Any]) -> tuple[None, bool, str]:
        matches = output.get("matches")
        if not isinstance(matches, list) or output.get("count") != len(matches):
            return None, False, "Search count did not match the returned matches."
        for item in matches:
            if not isinstance(item, dict) or self._workspace_file(item.get("path")) is None:
                return None, False, "Search returned a path that no longer exists."
        return None, True, "Search matches were re-checked and all paths exist."

    def _verify_office_inspect(
        self, output: Mapping[str, Any]
    ) -> tuple[None, bool, str]:
        target = self._workspace_file(output.get("path"))
        if target is None:
            return None, False, "Inspected Office file is missing or outside the workspace."
        try:
            current = inspect_office(target)
        except PcAgentError as exc:
            return None, False, str(exc)
        valid = (
            current.format == output.get("format")
            and current.sha256 == output.get("sha256")
            and current.size_bytes == output.get("size_bytes")
            and current.unit_count == output.get("unit_count")
        )
        return (
            None,
            valid,
            "Office file was re-inspected and format/count/hash matched."
            if valid
            else "Office file changed or inspection output did not match on recheck.",
        )

    def _verify_csv_inspect(
        self, output: Mapping[str, Any]
    ) -> tuple[None, bool, str]:
        target = self._workspace_file(output.get("path"))
        if target is None:
            return None, False, "Inspected CSV file is missing or outside the workspace."
        try:
            current = inspect_csv(target, sample_rows=0)
        except PcAgentError as exc:
            return None, False, str(exc)
        valid = (
            current.data.sha256 == output.get("sha256")
            and current.data.size_bytes == output.get("size_bytes")
            and len(current.data.rows) == output.get("row_count")
            and list(current.data.columns) == list(output.get("columns") or [])
        )
        return (
            None,
            valid,
            "CSV was re-parsed and row/column/hash data matched."
            if valid
            else "CSV changed or inspection output did not match on recheck.",
        )

    def _verify_office_extract(
        self, plan: TaskPlan, output: Mapping[str, Any]
    ) -> tuple[None, bool, str]:
        source = self._workspace_file(output.get("path"))
        if source is None:
            return None, False, "Text-extraction source is missing or outside the workspace."
        try:
            current = inspect_office(source)
        except PcAgentError as exc:
            return None, False, str(exc)
        text = output.get("text")
        characters = output.get("characters")
        max_chars = plan.arguments.get("max_chars", 100_000)
        basic_valid = (
            current.format == output.get("format")
            and isinstance(text, str)
            and isinstance(characters, int)
            and characters >= len(text)
            and isinstance(max_chars, int)
            and len(text) <= max_chars
        )
        if not basic_valid:
            return None, False, "Extracted-text bounds or source format did not verify."
        if output.get("output_path"):
            target = self._workspace_file(output.get("output_path"))
            if target is None:
                return None, False, "Extracted-text output is missing or outside the workspace."
            digest, size = self._file_digest(target)
            if digest != output.get("output_sha256") or size != output.get(
                "output_size_bytes"
            ):
                return None, False, "Extracted-text output hash/size did not match."
        return None, True, "Extracted text bounds and any saved output hash were verified."

    def _verify_office_conversion(
        self, tool_id: str, output: Mapping[str, Any]
    ) -> tuple[None, bool, str]:
        target = self._workspace_file(output.get("output_path"))
        if target is None:
            return None, False, "Converted Office output is missing or outside the workspace."
        digest, size = self._file_digest(target)
        if digest != output.get("sha256") or size != output.get("size_bytes"):
            return None, False, "Converted output hash/size did not match."
        try:
            if tool_id == "office.csv_to_json":
                parsed = json.loads(target.read_text(encoding="utf-8"))
                valid = (
                    isinstance(parsed, list)
                    and len(parsed) == output.get("row_count")
                    and all(isinstance(item, dict) for item in parsed)
                )
                message = "Generated JSON was re-parsed and its row count/hash matched."
            elif tool_id == "office.json_to_csv":
                current = read_csv(target)
                valid = (
                    len(current.rows) == output.get("row_count")
                    and list(current.columns) == list(output.get("columns") or [])
                )
                message = "Generated CSV was re-parsed and its row/column/hash data matched."
            else:
                current = inspect_office(target)
                sheets = current.details.get("sheets")
                sheet_rows = (
                    sheets[0].get("rows")
                    if isinstance(sheets, list) and sheets and isinstance(sheets[0], dict)
                    else None
                )
                expected_rows = output.get("row_count")
                valid = (
                    current.format == "xlsx"
                    and current.sha256 == digest
                    and isinstance(expected_rows, int)
                    and sheet_rows == expected_rows + 1
                )
                message = "Generated XLSX was re-opened and sheet row count/hash matched."
        except (PcAgentError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, False, f"Converted output could not be re-verified: {exc}"
        return None, valid, message if valid else "Converted output did not pass re-verification."

    def _workspace_file(self, raw_path: Any) -> Path | None:
        if not isinstance(raw_path, str) or not raw_path:
            return None
        candidate = Path(raw_path).expanduser()
        target = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self._settings.workspace_dir / candidate).resolve()
        )
        workspace = self._settings.workspace_dir.expanduser().resolve()
        if not target.is_relative_to(workspace) or not target.is_file():
            return None
        return target

    @staticmethod
    def _file_digest(target: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        return digest.hexdigest(), size

    def _require_task(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise PcAgentError(f"Task not found: {task_id}")
        return task
