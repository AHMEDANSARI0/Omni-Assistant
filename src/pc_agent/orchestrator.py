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
from .desktop_automation import normalize_key_chord
from .documents.office import (
    evaluate_workbook_formulas,
    extract_office_text,
    inspect_csv,
    inspect_office,
    read_csv,
    read_xlsx_cells,
)
from .domain.task import Task, TaskStatus
from .errors import (
    ApprovalUnavailableError,
    PcAgentError,
    PlanningError,
    ProviderError,
)
from .planning import (
    DeterministicPlanner,
    MultiStepPlan,
    PlannerOptions,
    PlanningOutcome,
    StepBinding,
    TaskPlan,
    parse_stored_plan,
    plan_with_provider,
)
from .providers.router import ModelRouter, ProviderAttempt, build_model_router
from .recovery import (
    FailureDiagnosis,
    RecoveryAction,
    RecoveryAttemptSummary,
    diagnose_tool_failure,
    diagnose_verification_failure,
)
from .storage.repositories import (
    ExecutionRepository,
    MemoryItemRecord,
    MemoryRepository,
    RecoveryAttemptRecord,
    RecoveryRepository,
    TaskRepository,
)
from .tools.registry import ToolContext, ToolRegistry, ToolResult
from .verification import VerificationReport, build_criteria, evaluate_criteria

logger = logging.getLogger(__name__)

CancellationCallback = Callable[[], bool]

_CANCELLED_MESSAGE = "Cancelled by user; no further steps were executed."


def _normalize_instruction(instruction: str) -> str:
    return " ".join(instruction.lower().split()).strip(" .!")


def _workflow_key(plan: TaskPlan, normalized_instruction: str) -> str:
    # output_path is excluded: repeating the same verified work to a fresh
    # output is still the same workflow; the full last arguments are kept
    # inside the record for reuse.
    basis = {
        key: value
        for key, value in dict(plan.arguments).items()
        if key not in {"output_path", "output"}
    }
    canonical = json.dumps(basis, sort_keys=True, default=str)
    digest = hashlib.sha256(
        f"{plan.intent}|{plan.tool_id}|{normalized_instruction}|{canonical}".encode()
    ).hexdigest()
    return f"wf-{digest[:16]}"


def _multistep_workflow_key(
    multi: MultiStepPlan, normalized_instruction: str
) -> str:
    # Bound arguments and output paths are excluded from the identity:
    # repeating the same chain to fresh outputs is the same workflow. The
    # full arguments and bindings live inside the record for reuse.
    basis = []
    for step, bindings in zip(multi.steps, multi.bindings, strict=True):
        bound = {binding.argument for binding in bindings}
        arguments = {
            key: value
            for key, value in dict(step.arguments).items()
            if key not in {"output_path", "output"} and key not in bound
        }
        basis.append(
            {
                "intent": step.intent,
                "tool_id": step.tool_id,
                "arguments": arguments,
                "bindings": sorted(bound),
            }
        )
    canonical = json.dumps(basis, sort_keys=True, default=str)
    digest = hashlib.sha256(
        f"multistep|{normalized_instruction}|{canonical}".encode()
    ).hexdigest()
    return f"wf-{digest[:16]}"


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _numbers_close(left: str, right: str) -> bool:
    try:
        return abs(float(left) - float(right)) <= 1e-9
    except ValueError:
        return False

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
        "pdf.extract_pages",
        "pdf.split",
        "office.docx_replace_text",
        "office.docx_append_paragraphs",
        "office.xlsx_set_cells",
        "office.xlsx_verify_formulas",
        "git.status",
        "git.log",
        "git.diff_summary",
        "git.init",
        "git.add",
        "git.commit",
        "commands.run",
        "browser.status",
        "browser.open",
        "browser.snapshot",
        "browser.click",
        "browser.type",
        "browser.screenshot",
        "desktop.windows",
        "desktop.window_inspect",
        "desktop.window_focus",
        "desktop.window_click",
        "desktop.window_type",
        "desktop.send_keys",
        "desktop.screenshot",
        "image.inspect",
        "image.convert",
        "image.transform",
        "image.compare",
        "image.analyze",
    }
)

# Phase 12: these tools execute external programs or change repository
# state, so they always need a fresh approval of the exact arguments -
# even when a plan comes from storage or workflow memory.
_TOOLS_REQUIRING_APPROVAL = frozenset(
    {
        "commands.run",
        "git.init",
        "git.add",
        "git.commit",
        "browser.open",
        "browser.click",
        "browser.type",
        "desktop.window_focus",
        "desktop.window_click",
        "desktop.window_type",
        "desktop.send_keys",
        "image.analyze",
    }
)

# Phase 13: actions inside the user's debug browser session.
_BROWSER_ACTION_TOOLS = frozenset(
    {"browser.open", "browser.click", "browser.type"}
)

# Phase 15: sending one exact image to a configured provider.
_VISION_ACTION_TOOLS = frozenset({"image.analyze"})

# Phase 14: actions that focus or send input to the user's real desktop.
_DESKTOP_AUTOMATION_ACTION_TOOLS = frozenset(
    {
        "desktop.window_focus",
        "desktop.window_click",
        "desktop.window_type",
        "desktop.send_keys",
    }
)


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    task_id: str
    plan: TaskPlan
    read_roots: tuple[Path, ...]
    action: str = "read_external_inputs"


ApprovalCallback = Callable[[ApprovalRequest], bool]


@dataclass(frozen=True, slots=True)
class StepReport:
    """Per-step evidence for a Phase 11 multi-step run."""

    index: int  # 0-based
    tool_id: str
    summary: str
    status: str  # "succeeded" | "failed" | "needs_input" | "cancelled"
    tool_result: ToolResult | None = None
    verification_valid: bool | None = None
    verification_message: str = ""
    error: str | None = None
    resumed_from_checkpoint: bool = False


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
    workflow_key: str | None = None
    workflow_success_count: int = 0
    workflow_reused: bool = False
    # Phase 11 multi-step evidence (empty/zero for single-step runs, where
    # ``plan``/``tool_result`` above describe the only step).
    step_reports: tuple[StepReport, ...] = ()
    steps_total: int = 0
    plan_summary: str = ""

    @property
    def success(self) -> bool:
        return self.task.status is TaskStatus.SUCCEEDED

    @property
    def cancelled(self) -> bool:
        return self.task.status is TaskStatus.CANCELLED


@dataclass(frozen=True, slots=True)
class _StepOutcome:
    """Internal result of running one plan step (never marks SUCCEEDED).

    Terminal-ish failures (failed / needs_input / cancelled) have already
    been written to the task by the step executor; the caller decides what
    happens on success (finish a single-step task, or continue a pipeline).
    """

    kind: str  # "succeeded" | "failed" | "needs_input" | "cancelled"
    task: Task
    plan: TaskPlan
    tool_result: ToolResult | None = None
    verification_result: ToolResult | None = None
    verification_valid: bool | None = None
    verification_message: str = ""
    verification_report: VerificationReport | None = None
    recovery_attempts: tuple[RecoveryAttemptSummary, ...] = ()
    recovered: bool = False
    recovery_used: int = 0
    missing_information: tuple[str, ...] = ()
    error: str | None = None
    resumed_from_checkpoint: bool = False
    attempts_used: int = 0


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
            return self._run_plan(
                task,
                plan,
                approval_callback,
                (),
                max_recovery_attempts=recovery_limit,
                source_task_id=source_task_id,
                initial_action=RecoveryAction.RESUME_STORED_PLAN,
                cancellation_requested=cancellation_requested,
                resume_checkpoints=True,
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
            return self._run_plan(
                task,
                plan,
                approval_callback,
                (),
                max_recovery_attempts=recovery_limit,
                source_task_id=source_task_id,
                initial_action=RecoveryAction.RESUME_STORED_PLAN,
                cancellation_requested=cancellation_requested,
                resume_checkpoints=True,
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

        if outcome.plan is None and outcome.multi_plan is None:
            reused = self.find_workflow_plan(task.instruction)
            if reused is not None:
                workflow_key, plan, prior_successes = reused
                if self._is_cancellation_requested(task.id, cancellation_requested):
                    return self._cancelled_report(
                        task,
                        plan=plan if isinstance(plan, TaskPlan) else plan.steps[0],
                        provider_attempts=outcome.provider_attempts,
                        source_task_id=source_task_id,
                        steps_total=(
                            len(plan.steps)
                            if isinstance(plan, MultiStepPlan)
                            else 0
                        ),
                        plan_summary=(
                            plan.summary if isinstance(plan, MultiStepPlan) else ""
                        ),
                    )
                self._execution.save_plan(task.id, plan.to_dict(), plan.planner)
                self._tasks.record_event(
                    task.id,
                    "workflow_reuse",
                    f"Planner missed; reusing verified workflow {workflow_key} "
                    f"({prior_successes} prior verified success(es)) after "
                    "live revalidation.",
                )
                report = self._run_plan(
                    task,
                    plan,
                    approval_callback,
                    outcome.provider_attempts,
                    max_recovery_attempts=recovery_limit,
                    source_task_id=source_task_id,
                    initial_action=RecoveryAction.EXECUTE_PLAN,
                    cancellation_requested=cancellation_requested,
                )
                return replace(
                    report,
                    workflow_key=report.workflow_key or workflow_key,
                    workflow_success_count=(
                        report.workflow_success_count or prior_successes
                    ),
                    workflow_reused=True,
                )
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

        if outcome.multi_plan is not None:
            multi = outcome.multi_plan
            disallowed = [
                step
                for step in multi.steps
                if step.tool_id not in _ALLOWED_ORCHESTRATED_TOOLS
            ]
            if disallowed:
                message = (
                    "Plan requested a tool that orchestration does not allow: "
                    f"{disallowed[0].tool_id}"
                )
                failed = self._tasks.update_status(
                    task.id,
                    TaskStatus.FAILED,
                    message="Unsafe plan rejected",
                    error=message,
                )
                return ExecutionReport(
                    task=failed,
                    plan=disallowed[0],
                    provider_attempts=outcome.provider_attempts,
                    source_task_id=source_task_id,
                    error=message,
                    steps_total=len(multi.steps),
                    plan_summary=multi.summary,
                )
            if self._is_cancellation_requested(task.id, cancellation_requested):
                return self._cancelled_report(
                    task,
                    plan=multi.steps[0],
                    provider_attempts=outcome.provider_attempts,
                    source_task_id=source_task_id,
                    steps_total=len(multi.steps),
                    plan_summary=multi.summary,
                )
            self._execution.save_plan(task.id, multi.to_dict(), multi.planner)
            self._tasks.record_event(task.id, "plan_created", multi.summary)
            return self._run_multistep(
                task,
                multi,
                approval_callback,
                outcome.provider_attempts,
                max_recovery_attempts=recovery_limit,
                source_task_id=source_task_id,
                initial_action=RecoveryAction.EXECUTE_PLAN,
                cancellation_requested=cancellation_requested,
            )

        plan = outcome.plan
        if plan is None:  # Defensive: the branches above return in this case.
            raise PcAgentError("Planner returned neither a plan nor input needs.")
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
        if isinstance(plan, MultiStepPlan):
            return self._run_multistep(
                task,
                plan,
                approval_callback,
                (),
                max_recovery_attempts=recovery_limit,
                source_task_id=None,
                initial_action=RecoveryAction.RESUME_STORED_PLAN,
                cancellation_requested=cancellation_requested,
                resume_checkpoints=True,
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

    def _mark_cancelled(self, task: Task) -> Task:
        cancelled = self._tasks.update_status(
            task.id,
            TaskStatus.CANCELLED,
            message=_CANCELLED_MESSAGE,
            error=_CANCELLED_MESSAGE,
        )
        self._execution.clear_cancellation(task.id)
        self._tasks.record_event(task.id, "cancelled", _CANCELLED_MESSAGE)
        return cancelled

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
        step_reports: tuple[StepReport, ...] = (),
        steps_total: int = 0,
        plan_summary: str = "",
    ) -> ExecutionReport:
        cancelled = self._mark_cancelled(task)
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
            error=_CANCELLED_MESSAGE,
            verification_report=verification_report,
            resumed_from_checkpoint=resumed_from_checkpoint,
            step_reports=step_reports,
            steps_total=steps_total,
            plan_summary=plan_summary,
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

    def _checkpoint_result(
        self, task: Task, plan: TaskPlan, *, step_index: int = 0
    ) -> ToolResult | None:
        checkpoint = self._execution.get_checkpoint(task.id, step_index)
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
                self._execution.delete_checkpoint(task.id, step_index)
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
            if list(record.scope.get("read_roots") or []) != expected_roots:
                continue
            if plan.tool_id in _TOOLS_REQUIRING_APPROVAL and record.scope.get(
                "arguments"
            ) != dict(plan.arguments):
                continue
            return True
        return False

    def _record_workflow_memory(
        self, task: Task, plan: TaskPlan, result: ToolResult
    ) -> tuple[str, int] | None:
        if self._memory is None:
            return None
        try:
            key = _workflow_key(plan, _normalize_instruction(task.instruction))
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
                    "normalized_instruction": _normalize_instruction(task.instruction),
                    "intent": plan.intent,
                    "tool_id": plan.tool_id,
                    "planner": plan.planner,
                    "arguments": dict(plan.arguments),
                    "external_read_roots": [
                        str(root) for root in plan.external_read_roots
                    ],
                    "expected_pages": plan.expected_pages,
                    "output_path": output.get("output_path"),
                    "success_count": success_count,
                    "last_task_id": task.id,
                },
                source_task_id=task.id,
            )
            return key, success_count
        except PcAgentError as exc:
            # Memory is supporting evidence, never a reason to fail a task
            # that already executed and verified successfully.
            logger.warning("Workflow memory could not be recorded: %s", exc)
            return None

    def _record_multistep_workflow_memory(
        self, task: Task, multi: MultiStepPlan, final_result: ToolResult
    ) -> tuple[str, int] | None:
        if self._memory is None:
            return None
        try:
            key = _multistep_workflow_key(
                multi, _normalize_instruction(task.instruction)
            )
            existing = self._memory.get("workflow", key)
            success_count = 1
            if existing is not None:
                previous = existing.content.get("success_count")
                if isinstance(previous, int):
                    success_count = previous + 1
            output = final_result.output or {}
            roots = sorted(
                {
                    str(root)
                    for step in multi.steps
                    for root in step.external_read_roots
                }
            )
            self._memory.upsert(
                kind="workflow",
                key=key,
                title=multi.summary,
                content={
                    "instruction": task.instruction,
                    "normalized_instruction": _normalize_instruction(
                        task.instruction
                    ),
                    "intent": "multi_step",
                    "tool_id": multi.steps[0].tool_id,
                    "planner": multi.planner,
                    "steps": [step.to_dict() for step in multi.steps],
                    "bindings": [
                        [binding.to_dict() for binding in step_bindings]
                        for step_bindings in multi.bindings
                    ],
                    "external_read_roots": roots,
                    "expected_pages": None,
                    "output_path": output.get("output_path") or output.get("path"),
                    "success_count": success_count,
                    "last_task_id": task.id,
                },
                source_task_id=task.id,
            )
            return key, success_count
        except PcAgentError as exc:
            # Memory is supporting evidence, never a reason to fail a task
            # that already executed and verified successfully.
            logger.warning("Workflow memory could not be recorded: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Phase 10: verified workflow reuse. Memory supplies a candidate plan
    # only; the live registry, policy, approvals, verification, criteria
    # and bounded recovery decide everything else, exactly as for a fresh
    # plan. A workflow record can never grant authority by itself.
    # ------------------------------------------------------------------
    def resolve_workflow(self, key: str) -> MemoryItemRecord:
        if self._memory is None:
            raise PcAgentError("Workflow memory is not available.")
        records = self._memory.list(kind="workflow", limit=500)
        if not records:
            raise PcAgentError(
                "No verified workflows are recorded yet. Run a task to "
                "completion first; it will be recorded automatically."
            )
        cleaned = key.strip()
        if cleaned == "latest":
            return records[0]
        exact = [record for record in records if record.key == cleaned]
        if exact:
            return exact[0]
        prefix = [record for record in records if record.key.startswith(cleaned)]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise PcAgentError(
                f"Workflow key {cleaned!r} is ambiguous; use the full key."
            )
        raise PcAgentError(f"Workflow not found: {cleaned}")

    def workflow_records(self) -> tuple[MemoryItemRecord, ...]:
        if self._memory is None:
            return ()
        return tuple(self._memory.list(kind="workflow", limit=500))

    def delete_workflow(self, key: str) -> MemoryItemRecord:
        record = self.resolve_workflow(key)
        assert self._memory is not None  # resolve_workflow guarantees this
        self._memory.delete("workflow", record.key)
        return record

    def find_workflow_plan(
        self, instruction: str
    ) -> tuple[str, TaskPlan | MultiStepPlan, int] | None:
        """Best still-valid verified plan for this exact instruction, if any."""

        if self._memory is None:
            return None
        normalized = _normalize_instruction(instruction)
        if not normalized:
            return None
        candidates: list[MemoryItemRecord] = []
        for record in self._memory.list(kind="workflow", limit=500):
            recorded = record.content.get("normalized_instruction")
            if not isinstance(recorded, str):
                original = record.content.get("instruction")
                recorded = (
                    _normalize_instruction(original)
                    if isinstance(original, str)
                    else ""
                )
            if recorded == normalized:
                candidates.append(record)
        candidates.sort(
            key=lambda record: (
                -(record.content.get("success_count")
                  if isinstance(record.content.get("success_count"), int) else 0),
                -record.updated_at.timestamp(),
            )
        )
        for record in candidates:
            plan = self._plan_from_workflow(record)
            if plan is not None:
                count = record.content.get("success_count")
                return record.key, plan, count if isinstance(count, int) else 0
        return None

    def _plan_from_workflow(
        self, record: MemoryItemRecord
    ) -> TaskPlan | MultiStepPlan | None:
        content = record.content
        steps_raw = content.get("steps")
        if isinstance(steps_raw, list) and steps_raw:
            try:
                multi = MultiStepPlan.from_dict(
                    {
                        "steps": steps_raw,
                        "bindings": content.get("bindings"),
                        "summary": record.title
                        or f"Reuse verified workflow {record.key}.",
                        "planner": "workflow-reuse",
                    }
                )
            except (PcAgentError, PlanningError, TypeError, ValueError) as exc:
                logger.warning(
                    "Workflow %s steps are unusable: %s", record.key, exc
                )
                return None
            for step in multi.steps:
                if (
                    step.tool_id not in _ALLOWED_ORCHESTRATED_TOOLS
                    or self._tools.get(step.tool_id) is None
                ):
                    return None
            return replace(
                multi,
                steps=tuple(
                    replace(step, planner="workflow-reuse")
                    for step in multi.steps
                ),
            )
        arguments = content.get("arguments")
        plan: TaskPlan | MultiStepPlan | None = None
        if isinstance(arguments, Mapping):
            intent = content.get("intent")
            tool_id = content.get("tool_id")
            roots = content.get("external_read_roots")
            expected = content.get("expected_pages")
            if isinstance(intent, str) and isinstance(tool_id, str):
                plan = TaskPlan(
                    intent=intent,
                    tool_id=tool_id,
                    arguments=dict(arguments),
                    summary=record.title or f"Reuse verified workflow {record.key}.",
                    planner="workflow-reuse",
                    external_read_roots=tuple(
                        Path(str(root)) for root in roots
                    ) if isinstance(roots, list) else (),
                    expected_pages=expected if isinstance(expected, int) else None,
                )
        if plan is None:
            # Legacy records (Phase 7) carry no arguments; fall back to the
            # stored plan of the last verified task.
            last_task_id = content.get("last_task_id")
            if isinstance(last_task_id, str):
                try:
                    stored = self._execution.get_plan(last_task_id)
                    if stored is not None:
                        plan = parse_stored_plan(stored)
                except (PcAgentError, PlanningError, TypeError, ValueError) as exc:
                    logger.warning(
                        "Workflow %s stored plan is unusable: %s", record.key, exc
                    )
                    return None
        if plan is None:
            return None
        if isinstance(plan, MultiStepPlan):
            for step in plan.steps:
                if (
                    step.tool_id not in _ALLOWED_ORCHESTRATED_TOOLS
                    or self._tools.get(step.tool_id) is None
                ):
                    return None
            return replace(plan, planner="workflow-reuse")
        if plan.tool_id not in _ALLOWED_ORCHESTRATED_TOOLS:
            return None
        registered = self._tools.get(plan.tool_id)
        if registered is None:
            return None
        return replace(plan, planner="workflow-reuse")

    def run_workflow(
        self,
        task_id: str,
        workflow_key: str,
        *,
        approval_callback: ApprovalCallback | None = None,
        max_recovery_attempts: int | None = None,
        cancellation_requested: CancellationCallback | None = None,
    ) -> ExecutionReport:
        recovery_limit = self._validate_recovery_limit(
            self._max_recovery_attempts
            if max_recovery_attempts is None
            else max_recovery_attempts
        )
        task = self._require_task(task_id)
        if task.is_terminal:
            raise PcAgentError(
                f"Task {task.id} is already {task.status.value}; create a new task."
            )
        if task.status not in {TaskStatus.QUEUED, TaskStatus.NEEDS_INPUT}:
            raise PcAgentError(
                f"Task {task.id} cannot run a workflow from status {task.status.value}."
            )
        if self._is_cancellation_requested(task.id, cancellation_requested):
            return self._cancelled_report(task)
        record = self.resolve_workflow(workflow_key)
        task = self._tasks.update_status(
            task.id, TaskStatus.PLANNING, message="Reusing verified workflow"
        )
        plan = self._plan_from_workflow(record)
        if plan is None:
            message = (
                f"Workflow {record.key} no longer maps to a valid allowed plan; "
                "it was not executed. Re-run the task normally to record a "
                "fresh verified workflow."
            )
            failed = self._tasks.update_status(
                task.id, TaskStatus.FAILED, message="Workflow invalid", error=message
            )
            return ExecutionReport(task=failed, error=message)
        self._execution.save_plan(task.id, plan.to_dict(), plan.planner)
        self._tasks.record_event(
            task.id,
            "workflow_reuse",
            f"Reusing verified workflow {record.key} after live revalidation.",
        )
        report = self._run_plan(
            task,
            plan,
            approval_callback,
            (),
            max_recovery_attempts=recovery_limit,
            source_task_id=None,
            initial_action=RecoveryAction.EXECUTE_PLAN,
            cancellation_requested=cancellation_requested,
        )
        count = record.content.get("success_count")
        return replace(
            report,
            workflow_key=report.workflow_key or record.key,
            workflow_success_count=(
                report.workflow_success_count
                or (count if isinstance(count, int) else 0)
            ),
            workflow_reused=True,
        )

    def _stored_plan(
        self, task: Task, *, source_task_id: str | None = None
    ) -> tuple[TaskPlan | MultiStepPlan | None, ExecutionReport | None]:
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
            plan = parse_stored_plan(stored)
        except (PlanningError, TypeError, ValueError) as exc:
            message = f"Stored plan is invalid: {exc}"
            failed = self._tasks.update_status(
                task.id, TaskStatus.FAILED, message="Stored plan invalid", error=message
            )
            return None, ExecutionReport(
                task=failed, source_task_id=source_task_id, error=message
            )
        if isinstance(plan, MultiStepPlan):
            disallowed = [
                step.tool_id
                for step in plan.steps
                if step.tool_id not in _ALLOWED_ORCHESTRATED_TOOLS
            ]
            if disallowed:
                message = f"Stored plan requested a disallowed tool: {disallowed[0]}"
                failed = self._tasks.update_status(
                    task.id, TaskStatus.FAILED, message="Unsafe stored plan rejected", error=message
                )
                return None, ExecutionReport(
                    task=failed,
                    plan=plan.steps[0],
                    source_task_id=source_task_id,
                    error=message,
                    steps_total=len(plan.steps),
                    plan_summary=plan.summary,
                )
            return plan, None
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
        is_command = plan.tool_id in _TOOLS_REQUIRING_APPROVAL
        is_browser = plan.tool_id in _BROWSER_ACTION_TOOLS
        is_desktop_automation = plan.tool_id in _DESKTOP_AUTOMATION_ACTION_TOOLS
        is_vision = plan.tool_id in _VISION_ACTION_TOOLS
        if not plan.external_read_roots and not is_command:
            return task, True
        if is_vision:
            action = "vision_analysis"
            approval_label = "Approval required to send one exact image to a vision provider"
        elif is_desktop_automation:
            action = "desktop_automation_action"
            approval_label = "Approval required for a desktop UI Automation action"
        elif is_browser:
            action = "browser_action"
            approval_label = "Approval required for an action in the browser"
        elif is_command:
            action = "run_command"
            approval_label = "Approval required to run an external command"
        else:
            action = "read_external_inputs"
            approval_label = (
                "Approval required for read-only external input folder(s)"
            )
        if task.status is TaskStatus.PLANNING:
            task = self._tasks.update_status(
                task.id,
                TaskStatus.WAITING_FOR_APPROVAL,
                message=approval_label,
            )
        elif task.status is not TaskStatus.WAITING_FOR_APPROVAL:
            raise PcAgentError(
                f"Approval cannot be requested from task status {task.status.value}."
            )
        if approval_callback is None:
            raise ApprovalUnavailableError(
                f"{approval_label}, but no approval prompt is available. "
                "The task remains waiting_for_approval."
            )
        request = ApprovalRequest(
            task.id, plan, plan.external_read_roots, action=action
        )
        approved = bool(approval_callback(request))
        self._execution.record_approval(
            task.id,
            request.action,
            approved=approved,
            scope={
                "tool_id": plan.tool_id,
                "read_roots": [str(path) for path in plan.external_read_roots],
                "arguments": dict(plan.arguments),
                "permissions": (
                    "send exactly this one approved workspace image and question "
                    "to the configured vision-capable provider route"
                    if is_vision
                    else "perform exactly this approved action in one selected "
                    "Windows application window"
                    if is_desktop_automation
                    else "perform exactly this approved action in the user's "
                    "debug browser session"
                    if is_browser
                    else "execute exactly this approved command inside the "
                    "workspace; no shell; bounded time/output"
                    if is_command
                    else "read/list only; no write/delete outside workspace"
                ),
            },
        )
        self._tasks.record_event(
            task.id,
            "approval_granted" if approved else "approval_denied",
            (
                "Vision image/question scope was reviewed."
                if is_vision
                else "Desktop automation action scope was reviewed."
                if is_desktop_automation
                else "Browser action scope was reviewed."
                if is_browser
                else "External command scope was reviewed."
                if is_command
                else "Read-only external input scope was reviewed."
            ),
        )
        return task, approved

    def _execute_step(
        self,
        task: Task,
        plan: TaskPlan,
        approval_callback: ApprovalCallback | None,
        *,
        max_recovery_attempts: int,
        source_task_id: str | None,
        initial_action: RecoveryAction,
        cancellation_requested: CancellationCallback | None = None,
        resumed_result: ToolResult | None = None,
        step_index: int = 0,
        attempt_offset: int = 0,
        on_plan_revised: Callable[[TaskPlan], None] | None = None,
    ) -> _StepOutcome:
        """Run one plan step with verification and bounded recovery.

        Success is reported without completing the task, so a multi-step
        pipeline can continue; failed / needs_input / cancelled outcomes
        have already moved the task to the matching status.
        """
        summaries: list[RecoveryAttemptSummary] = []
        recovery_used = 0
        attempt_number = 1
        next_action = initial_action
        resumed_used = False

        while True:
            if self._is_cancellation_requested(task.id, cancellation_requested):
                return _StepOutcome(
                    kind="cancelled",
                    task=self._mark_cancelled(task),
                    plan=plan,
                    recovery_attempts=tuple(summaries),
                    error=_CANCELLED_MESSAGE,
                    resumed_from_checkpoint=resumed_used,
                    attempts_used=attempt_number - 1,
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
                subprocess_allowed=self._settings.allow_subprocess,
                command_allowlist=self._settings.command_allowlist,
                command_timeout_seconds=self._settings.command_timeout_seconds,
                browser_allowed=self._settings.allow_browser,
                browser_port=self._settings.browser_port,
                browser_timeout_seconds=self._settings.browser_timeout_seconds,
                desktop_automation_allowed=self._settings.allow_desktop_automation,
                desktop_automation_timeout_seconds=(
                    self._settings.desktop_automation_timeout_seconds
                ),
                vision_allowed=self._settings.allow_vision,
                vision_max_image_bytes=self._settings.vision_max_image_bytes,
                vision_router_factory=(
                    lambda: self._router or build_model_router(self._settings)
                ),
            )
            display_attempt = attempt_offset + attempt_number
            attempt_record = self._start_attempt(
                task_id=task.id,
                source_task_id=source_task_id,
                attempt_number=display_attempt,
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
                        step_index=step_index,
                        step_key=f"tool:{plan.tool_id}",
                        result=result,
                        plan_fingerprint=self._plan_fingerprint(plan),
                        artifacts=self._artifact_paths(result.output),
                    )
            if self._is_cancellation_requested(task.id, cancellation_requested):
                return _StepOutcome(
                    kind="cancelled",
                    task=self._mark_cancelled(task),
                    plan=plan,
                    tool_result=result,
                    recovery_attempts=tuple(summaries),
                    error=_CANCELLED_MESSAGE,
                    resumed_from_checkpoint=resumed_used,
                    attempts_used=attempt_number,
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
                            attempt_number=display_attempt,
                            stage="verification",
                            status="succeeded",
                            action=next_action,
                            message=verification_message,
                        )
                    )
                    return _StepOutcome(
                        kind="succeeded",
                        task=task,
                        plan=plan,
                        tool_result=result,
                        verification_result=verification_result,
                        verification_valid=True,
                        verification_message=verification_message,
                        verification_report=verification_report,
                        recovery_attempts=tuple(summaries),
                        recovered=recovery_used > 0,
                        recovery_used=recovery_used,
                        resumed_from_checkpoint=resumed_used,
                        attempts_used=attempt_number,
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
                    attempt_number=display_attempt,
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
                    if on_plan_revised is not None:
                        on_plan_revised(plan)
                    else:
                        self._execution.save_plan(
                            task.id, plan.to_dict(), plan.planner
                        )
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
                            return _StepOutcome(
                                kind="cancelled",
                                task=cancelled,
                                plan=plan,
                                tool_result=result,
                                verification_result=verification_result,
                                verification_valid=verification_valid,
                                verification_message=verification_message,
                                recovery_attempts=tuple(summaries),
                                error=message,
                                attempts_used=attempt_number,
                            )

                if self._is_cancellation_requested(task.id, cancellation_requested):
                    return _StepOutcome(
                        kind="cancelled",
                        task=self._mark_cancelled(task),
                        plan=plan,
                        tool_result=result,
                        verification_result=verification_result,
                        verification_valid=verification_valid,
                        verification_message=verification_message,
                        verification_report=verification_report,
                        recovery_attempts=tuple(summaries),
                        error=_CANCELLED_MESSAGE,
                        resumed_from_checkpoint=resumed_used,
                        attempts_used=attempt_number,
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
                return _StepOutcome(
                    kind="needs_input",
                    task=needs_input,
                    plan=plan,
                    tool_result=result,
                    verification_result=verification_result,
                    verification_valid=verification_valid,
                    verification_message=verification_message,
                    verification_report=verification_report,
                    recovery_attempts=tuple(summaries),
                    missing_information=missing,
                    error=failure_message,
                    resumed_from_checkpoint=resumed_used,
                    attempts_used=attempt_number,
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
            return _StepOutcome(
                kind="failed",
                task=failed,
                plan=plan,
                tool_result=result,
                verification_result=verification_result,
                verification_valid=verification_valid,
                verification_message=verification_message,
                verification_report=verification_report,
                recovery_attempts=tuple(summaries),
                error=failure_message,
                resumed_from_checkpoint=resumed_used,
                attempts_used=attempt_number,
            )

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
        """Single-step run: an ``_execute_step`` plus task completion."""

        outcome = self._execute_step(
            task,
            plan,
            approval_callback,
            max_recovery_attempts=max_recovery_attempts,
            source_task_id=source_task_id,
            initial_action=initial_action,
            cancellation_requested=cancellation_requested,
            resumed_result=resumed_result,
        )
        if outcome.kind == "succeeded":
            succeeded = self._tasks.update_status(
                outcome.task.id,
                TaskStatus.SUCCEEDED,
                message="Execution and verification completed",
            )
            self._tasks.record_event(
                outcome.task.id, "verified", outcome.verification_message
            )
            if outcome.recovered:
                self._tasks.record_event(
                    outcome.task.id,
                    "recovered",
                    f"Recovered after {outcome.recovery_used} bounded recovery attempt(s).",
                )
            assert outcome.tool_result is not None  # guaranteed on success
            recorded = self._record_workflow_memory(
                succeeded, outcome.plan, outcome.tool_result
            )
            return ExecutionReport(
                task=succeeded,
                plan=outcome.plan,
                tool_result=outcome.tool_result,
                verification_result=outcome.verification_result,
                verification_valid=True,
                verification_message=outcome.verification_message,
                provider_attempts=provider_attempts,
                recovery_attempts=outcome.recovery_attempts,
                recovered=outcome.recovered,
                source_task_id=source_task_id,
                verification_report=outcome.verification_report,
                resumed_from_checkpoint=outcome.resumed_from_checkpoint,
                workflow_key=recorded[0] if recorded else None,
                workflow_success_count=recorded[1] if recorded else 0,
            )
        return ExecutionReport(
            task=outcome.task,
            plan=outcome.plan,
            tool_result=outcome.tool_result,
            verification_result=outcome.verification_result,
            verification_valid=outcome.verification_valid,
            verification_message=outcome.verification_message,
            missing_information=outcome.missing_information,
            provider_attempts=provider_attempts,
            recovery_attempts=outcome.recovery_attempts,
            recovered=False,
            source_task_id=source_task_id,
            error=outcome.error,
            verification_report=outcome.verification_report,
            resumed_from_checkpoint=outcome.resumed_from_checkpoint,
        )

    def _run_plan(
        self,
        task: Task,
        plan: TaskPlan | MultiStepPlan,
        approval_callback: ApprovalCallback | None,
        provider_attempts: tuple[ProviderAttempt, ...],
        *,
        max_recovery_attempts: int,
        source_task_id: str | None,
        initial_action: RecoveryAction,
        cancellation_requested: CancellationCallback | None = None,
        resume_checkpoints: bool = False,
        resumed_result: ToolResult | None = None,
    ) -> ExecutionReport:
        if isinstance(plan, MultiStepPlan):
            return self._run_multistep(
                task,
                plan,
                approval_callback,
                provider_attempts,
                max_recovery_attempts=max_recovery_attempts,
                source_task_id=source_task_id,
                initial_action=initial_action,
                cancellation_requested=cancellation_requested,
                resume_checkpoints=resume_checkpoints,
            )
        return self._approve_and_run(
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

    @staticmethod
    def _resolve_bindings(
        plan: TaskPlan,
        bindings: tuple[StepBinding, ...],
        results: list[ToolResult],
        *,
        step_index: int,
    ) -> TaskPlan:
        if not bindings:
            return plan
        arguments = dict(plan.arguments)
        for binding in bindings:
            source = results[binding.from_step].output or {}
            value = source.get(binding.output_field)
            if not isinstance(value, str) or not value:
                raise PcAgentError(
                    f"Step {step_index + 1} needs '{binding.output_field}' from "
                    f"step {binding.from_step + 1}, but that step did not "
                    "report it."
                )
            arguments[binding.argument] = value
        return replace(plan, arguments=arguments)

    def _run_multistep(
        self,
        task: Task,
        multi: MultiStepPlan,
        approval_callback: ApprovalCallback | None,
        provider_attempts: tuple[ProviderAttempt, ...],
        *,
        max_recovery_attempts: int,
        source_task_id: str | None,
        initial_action: RecoveryAction,
        cancellation_requested: CancellationCallback | None = None,
        resume_checkpoints: bool = False,
    ) -> ExecutionReport:
        """Phase 11 pipeline: run each step through the full machinery.

        A step starts only after the previous one verified. Bindings are
        resolved from verified results just before the step runs, external
        read roots are approved per step (never inherited), and every step
        checkpoints under its own index so an interrupted run resumes at
        the first unfinished step. The task succeeds only when the final
        step verifies; the recovery budget applies per step.
        """

        steps: list[TaskPlan] = list(multi.steps)
        total = len(steps)
        summaries: list[RecoveryAttemptSummary] = []
        step_reports: list[StepReport] = []
        results: list[ToolResult] = []
        attempts_so_far = 0
        any_recovered = False
        any_resumed = False
        last_outcome: _StepOutcome | None = None

        for index in range(total):
            planned = steps[index]
            if self._is_cancellation_requested(task.id, cancellation_requested):
                return self._cancelled_report(
                    task,
                    plan=last_outcome.plan if last_outcome is not None else planned,
                    provider_attempts=provider_attempts,
                    source_task_id=source_task_id,
                    recovery_attempts=tuple(summaries),
                    resumed_from_checkpoint=any_resumed,
                    step_reports=tuple(step_reports),
                    steps_total=total,
                    plan_summary=multi.summary,
                )
            try:
                concrete = self._resolve_bindings(
                    planned, multi.bindings[index], results, step_index=index
                )
            except PcAgentError as exc:
                if task.status is not TaskStatus.PLANNING:
                    task = self._tasks.update_status(
                        task.id,
                        TaskStatus.PLANNING,
                        message=f"Step {index + 1}/{total}: {planned.summary}",
                    )
                failed = self._tasks.update_status(
                    task.id,
                    TaskStatus.FAILED,
                    message=f"Step {index + 1} could not start",
                    error=str(exc),
                )
                step_reports.append(
                    StepReport(
                        index=index,
                        tool_id=planned.tool_id,
                        summary=planned.summary,
                        status="failed",
                        error=str(exc),
                    )
                )
                return ExecutionReport(
                    task=failed,
                    plan=planned,
                    provider_attempts=provider_attempts,
                    recovery_attempts=tuple(summaries),
                    source_task_id=source_task_id,
                    error=(
                        f"Step {index + 1} of {total} ({planned.tool_id}) "
                        f"failed: {exc}"
                    ),
                    resumed_from_checkpoint=any_resumed,
                    step_reports=tuple(step_reports),
                    steps_total=total,
                    plan_summary=multi.summary,
                )
            if task.status is not TaskStatus.PLANNING:
                task = self._tasks.update_status(
                    task.id,
                    TaskStatus.PLANNING,
                    message=f"Step {index + 1}/{total}: {concrete.summary}",
                )
            task, approved = self._request_approval(
                task, concrete, approval_callback
            )
            if not approved:
                message = (
                    f"Approval was denied for step {index + 1} of {total}; "
                    "the tool was not executed."
                )
                cancelled = self._tasks.update_status(
                    task.id,
                    TaskStatus.CANCELLED,
                    message=message,
                    error=message,
                )
                step_reports.append(
                    StepReport(
                        index=index,
                        tool_id=concrete.tool_id,
                        summary=concrete.summary,
                        status="cancelled",
                        error=message,
                    )
                )
                return ExecutionReport(
                    task=cancelled,
                    plan=concrete,
                    provider_attempts=provider_attempts,
                    recovery_attempts=tuple(summaries),
                    source_task_id=source_task_id,
                    error=message,
                    resumed_from_checkpoint=any_resumed,
                    step_reports=tuple(step_reports),
                    steps_total=total,
                    plan_summary=multi.summary,
                )

            resumed_result = (
                self._checkpoint_result(task, concrete, step_index=index)
                if resume_checkpoints
                else None
            )

            def _on_revised(
                revised: TaskPlan, _index: int = index, _task_id: str = task.id
            ) -> None:
                steps[_index] = revised
                updated = MultiStepPlan(
                    steps=tuple(steps),
                    bindings=multi.bindings,
                    summary=multi.summary,
                    planner=multi.planner,
                )
                self._execution.save_plan(
                    _task_id, updated.to_dict(), updated.planner
                )

            outcome = self._execute_step(
                task,
                concrete,
                approval_callback,
                max_recovery_attempts=max_recovery_attempts,
                source_task_id=source_task_id,
                initial_action=(
                    initial_action
                    if index == 0
                    else RecoveryAction.EXECUTE_PLAN
                ),
                cancellation_requested=cancellation_requested,
                resumed_result=resumed_result,
                step_index=index,
                attempt_offset=attempts_so_far,
                on_plan_revised=_on_revised,
            )
            task = outcome.task
            steps[index] = outcome.plan
            attempts_so_far += outcome.attempts_used
            summaries.extend(outcome.recovery_attempts)
            any_recovered = any_recovered or outcome.recovered
            any_resumed = any_resumed or outcome.resumed_from_checkpoint
            last_outcome = outcome
            step_reports.append(
                StepReport(
                    index=index,
                    tool_id=outcome.plan.tool_id,
                    summary=outcome.plan.summary,
                    status=outcome.kind,
                    tool_result=outcome.tool_result,
                    verification_valid=outcome.verification_valid,
                    verification_message=outcome.verification_message,
                    error=outcome.error,
                    resumed_from_checkpoint=outcome.resumed_from_checkpoint,
                )
            )
            if outcome.kind == "succeeded":
                assert outcome.tool_result is not None  # guaranteed on success
                results.append(outcome.tool_result)
                self._tasks.record_event(
                    task.id,
                    "step_completed",
                    f"Step {index + 1}/{total} ({outcome.plan.tool_id}) "
                    f"verified: {outcome.verification_message}",
                )
                continue
            step_error = outcome.error or "Step failed."
            return ExecutionReport(
                task=task,
                plan=outcome.plan,
                tool_result=outcome.tool_result,
                verification_result=outcome.verification_result,
                verification_valid=outcome.verification_valid,
                verification_message=outcome.verification_message,
                missing_information=outcome.missing_information,
                provider_attempts=provider_attempts,
                recovery_attempts=tuple(summaries),
                recovered=False,
                source_task_id=source_task_id,
                error=(
                    f"Step {index + 1} of {total} ({outcome.plan.tool_id}) "
                    f"{outcome.kind}: {step_error}"
                ),
                verification_report=outcome.verification_report,
                resumed_from_checkpoint=any_resumed,
                step_reports=tuple(step_reports),
                steps_total=total,
                plan_summary=multi.summary,
            )

        assert last_outcome is not None and results  # all steps succeeded
        succeeded = self._tasks.update_status(
            task.id,
            TaskStatus.SUCCEEDED,
            message=f"All {total} steps executed and verified",
        )
        self._tasks.record_event(
            task.id, "verified", last_outcome.verification_message
        )
        if any_recovered:
            self._tasks.record_event(
                task.id,
                "recovered",
                "A bounded self-healing retry succeeded for at least one step.",
            )
        final_multi = MultiStepPlan(
            steps=tuple(steps),
            bindings=multi.bindings,
            summary=multi.summary,
            planner=multi.planner,
        )
        recorded = self._record_multistep_workflow_memory(
            succeeded, final_multi, results[-1]
        )
        return ExecutionReport(
            task=succeeded,
            plan=last_outcome.plan,
            tool_result=last_outcome.tool_result,
            verification_result=last_outcome.verification_result,
            verification_valid=True,
            verification_message=last_outcome.verification_message,
            provider_attempts=provider_attempts,
            recovery_attempts=tuple(summaries),
            recovered=any_recovered,
            source_task_id=source_task_id,
            verification_report=last_outcome.verification_report,
            resumed_from_checkpoint=any_resumed,
            workflow_key=recorded[0] if recorded else None,
            workflow_success_count=recorded[1] if recorded else 0,
            step_reports=tuple(step_reports),
            steps_total=total,
            plan_summary=multi.summary,
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

        if plan.tool_id == "desktop.windows":
            return self._verify_desktop_windows(task_id, plan, output, context)

        if plan.tool_id == "desktop.window_inspect":
            return self._verify_desktop_inspect(task_id, plan, output, context)

        if plan.tool_id == "desktop.window_focus":
            return self._verify_desktop_focus(task_id, output, context)

        if plan.tool_id == "desktop.window_click":
            return self._verify_desktop_click(task_id, output, context)

        if plan.tool_id == "desktop.window_type":
            return self._verify_desktop_type(task_id, plan, output, context)

        if plan.tool_id == "desktop.send_keys":
            return self._verify_desktop_send_keys(task_id, plan, output, context)

        if plan.tool_id == "desktop.screenshot":
            return self._verify_browser_screenshot(output)

        if plan.tool_id in {"files.copy", "files.move", "files.rename"}:
            return self._verify_file_transfer(
                output, source_must_exist=plan.tool_id == "files.copy"
            )

        if plan.tool_id == "files.organize":
            return self._verify_organize(output)

        if plan.tool_id == "files.search":
            return self._verify_search(output)

        if plan.tool_id == "pdf.extract_pages":
            arguments: dict[str, Any] = {
                "path": output.get("output_path"),
                "expected_pages": output.get("page_count"),
            }
            verification = self._tools.invoke("pdf.verify", arguments, context)
            self._execution.record_tool_invocation(task_id, arguments, verification)
            if not verification.success or verification.output is None:
                return verification, False, verification.error or "PDF verification tool failed."
            valid = bool(verification.output.get("valid")) and (
                verification.output.get("sha256") == output.get("sha256")
            )
            return (
                verification,
                valid,
                "Extracted PDF was re-opened; page count and SHA-256 matched."
                if valid
                else "Extracted PDF did not pass re-open verification.",
            )

        if plan.tool_id == "pdf.split":
            return self._verify_pdf_split(task_id, output, context)

        if plan.tool_id == "office.docx_replace_text":
            return self._verify_docx_replace(plan, output)

        if plan.tool_id == "office.docx_append_paragraphs":
            return self._verify_docx_append(plan, output)

        if plan.tool_id == "office.xlsx_set_cells":
            return self._verify_xlsx_set_cells(plan, output)

        if plan.tool_id == "office.xlsx_verify_formulas":
            return self._verify_xlsx_formulas(output)

        if plan.tool_id in {"git.status", "git.log", "git.diff_summary"}:
            return self._verify_git_read(task_id, plan, result, context)

        if plan.tool_id == "git.init":
            return self._verify_git_init(output)

        if plan.tool_id == "git.add":
            return self._verify_git_add(task_id, plan, context)

        if plan.tool_id == "git.commit":
            return self._verify_git_commit(task_id, plan, output, context)

        if plan.tool_id == "commands.run":
            return self._verify_command_run(plan, output)

        if plan.tool_id == "browser.status":
            return self._verify_browser_status(task_id, output, context)

        if plan.tool_id == "browser.snapshot":
            return self._verify_browser_snapshot(task_id, output, context)

        if plan.tool_id == "browser.open":
            return self._verify_browser_open(task_id, output, context)

        if plan.tool_id == "browser.click":
            return self._verify_browser_click(task_id, output, context)

        if plan.tool_id == "browser.type":
            return self._verify_browser_type(task_id, plan, output, context)

        if plan.tool_id == "browser.screenshot":
            return self._verify_browser_screenshot(output)

        if plan.tool_id == "image.inspect":
            return self._verify_image_inspect(task_id, plan, output, context)

        if plan.tool_id in {"image.convert", "image.transform"}:
            return self._verify_image_output(task_id, plan, output, context)

        if plan.tool_id == "image.compare":
            return self._verify_image_compare(task_id, plan, output, context)

        if plan.tool_id == "image.analyze":
            return self._verify_image_analyze(task_id, plan, output, context)

        return None, False, f"No deterministic verifier is registered for {plan.tool_id}."

    @staticmethod
    def _desktop_window_selector_arguments(plan: TaskPlan) -> dict[str, Any]:
        keys = (
            "window_handle",
            "window_title",
            "window_title_contains",
            "window_class_name",
            "window_process_id",
        )
        return {
            key: plan.arguments[key] for key in keys if plan.arguments.get(key) is not None
        }

    def _fresh_desktop_windows(
        self, task_id: str, context: ToolContext
    ) -> ToolResult:
        arguments: dict[str, Any] = {"max_windows": 200, "include_untitled": True}
        fresh = self._tools.invoke("desktop.windows", arguments, context)
        self._execution.record_tool_invocation(task_id, arguments, fresh)
        return fresh

    @staticmethod
    def _desktop_window_by_handle(
        output: Mapping[str, Any], handle: Any
    ) -> Mapping[str, Any] | None:
        windows = output.get("windows")
        if not isinstance(windows, list):
            return None
        for item in windows:
            if isinstance(item, Mapping) and item.get("handle") == handle:
                return item
        return None

    def _verify_desktop_windows(
        self,
        task_id: str,
        plan: TaskPlan,
        output: Mapping[str, Any],
        context: ToolContext,
    ) -> tuple[ToolResult | None, bool, str]:
        fresh = self._tools.invoke(
            "desktop.windows", dict(plan.arguments), context
        )
        self._execution.record_tool_invocation(
            task_id, dict(plan.arguments), fresh
        )
        if not fresh.success or fresh.output is None:
            return fresh, False, fresh.error or "Desktop window re-read failed."
        windows = output.get("windows")
        if not isinstance(windows, list) or output.get("count") != len(windows):
            return fresh, False, "Desktop window count did not match the returned windows."
        handles = [item.get("handle") for item in windows if isinstance(item, Mapping)]
        foreground = output.get("foreground_handle")
        marked = [
            item.get("handle")
            for item in windows
            if isinstance(item, Mapping) and item.get("is_foreground") is True
        ]
        valid = (
            len(handles) == len(windows)
            and len(set(handles)) == len(handles)
            and fresh.output.get("count") == len(fresh.output.get("windows") or [])
            and (marked == [foreground] if foreground else not marked)
        )
        return (
            fresh,
            bool(valid),
            "Desktop window list was independently re-read and its handles/foreground state are consistent."
            if valid
            else "Desktop window list was internally inconsistent on verification.",
        )

    def _verify_desktop_inspect(
        self,
        task_id: str,
        plan: TaskPlan,
        output: Mapping[str, Any],
        context: ToolContext,
    ) -> tuple[ToolResult | None, bool, str]:
        fresh = self._tools.invoke(
            "desktop.window_inspect", dict(plan.arguments), context
        )
        self._execution.record_tool_invocation(
            task_id, dict(plan.arguments), fresh
        )
        if not fresh.success or fresh.output is None:
            return fresh, False, fresh.error or "Desktop window re-inspection failed."
        output_window = output.get("window")
        fresh_window = fresh.output.get("window")
        elements = output.get("elements")
        valid = (
            isinstance(output_window, Mapping)
            and isinstance(fresh_window, Mapping)
            and output_window.get("handle") == fresh_window.get("handle")
            and isinstance(elements, list)
            and output.get("element_count") == len(elements)
            and all(
                isinstance(item, Mapping)
                and isinstance(item.get("control_type_id"), int)
                and int(item.get("control_type_id") or 0) > 0
                for item in elements
            )
        )
        return (
            fresh,
            bool(valid),
            "The same desktop window was re-inspected and its UIA element report is structurally consistent."
            if valid
            else "Desktop window re-inspection did not confirm the reported window/elements.",
        )

    def _verify_desktop_focus(
        self, task_id: str, output: Mapping[str, Any], context: ToolContext
    ) -> tuple[ToolResult | None, bool, str]:
        fresh = self._fresh_desktop_windows(task_id, context)
        if not fresh.success or fresh.output is None:
            return fresh, False, fresh.error or "Desktop foreground re-check failed."
        window = output.get("window")
        handle = window.get("handle") if isinstance(window, Mapping) else None
        checked = self._desktop_window_by_handle(fresh.output, handle)
        valid = bool(
            output.get("focused") is True
            and checked is not None
            and checked.get("is_foreground") is True
        )
        return (
            fresh,
            valid,
            "A fresh window list confirms the selected desktop window is foreground."
            if valid
            else "A fresh window list does not confirm the selected desktop window is foreground.",
        )

    def _verify_desktop_click(
        self, task_id: str, output: Mapping[str, Any], context: ToolContext
    ) -> tuple[ToolResult | None, bool, str]:
        fresh = self._fresh_desktop_windows(task_id, context)
        if not fresh.success or fresh.output is None:
            return fresh, False, fresh.error or "Desktop window re-check after click failed."
        before = output.get("window")
        after = output.get("window_after")
        before_handle = before.get("handle") if isinstance(before, Mapping) else None
        after_handle = after.get("handle") if isinstance(after, Mapping) else None
        if output.get("window_closed") is True:
            state_ok = after is None and (
                self._desktop_window_by_handle(fresh.output, before_handle) is None
            )
        else:
            state_ok = (
                isinstance(after, Mapping)
                and self._desktop_window_by_handle(fresh.output, after_handle)
                is not None
            )
        match_count = output.get("match_count")
        selected_index = output.get("selected_index")
        valid = bool(
            output.get("clicked") is True
            and bool(output.get("action_method"))
            and isinstance(match_count, int)
            and isinstance(selected_index, int)
            and 1 <= selected_index <= match_count
            and state_ok
        )
        return (
            fresh,
            valid,
            "The approved UIA click was dispatched and the target window state was re-read consistently."
            if valid
            else "The desktop click result could not be confirmed from a fresh window list.",
        )

    @staticmethod
    def _desktop_element_matches_output(
        candidate: Mapping[str, Any], expected: Mapping[str, Any]
    ) -> bool:
        runtime = expected.get("runtime_id")
        if isinstance(runtime, str) and runtime:
            return candidate.get("runtime_id") == runtime
        return (
            candidate.get("automation_id") == expected.get("automation_id")
            and candidate.get("name") == expected.get("name")
            and candidate.get("class_name") == expected.get("class_name")
            and candidate.get("control_type_id") == expected.get("control_type_id")
        )

    def _verify_desktop_type(
        self,
        task_id: str,
        plan: TaskPlan,
        output: Mapping[str, Any],
        context: ToolContext,
    ) -> tuple[ToolResult | None, bool, str]:
        expected_text = plan.arguments.get("text")
        if output.get("value") != expected_text or output.get("read_back_verified") is not True:
            return (
                None,
                False,
                "Desktop field read-back does not match the approved text.",
            )
        expected_element = output.get("element")
        if not isinstance(expected_element, Mapping):
            return None, False, "Desktop type result has no target element evidence."
        arguments = self._desktop_window_selector_arguments(plan)
        arguments.update({"max_elements": 1000, "include_offscreen": True})
        fresh = self._tools.invoke("desktop.window_inspect", arguments, context)
        self._execution.record_tool_invocation(task_id, arguments, fresh)
        if not fresh.success or fresh.output is None:
            return fresh, False, fresh.error or "Desktop text element re-inspection failed."
        elements = fresh.output.get("elements")
        valid = isinstance(elements, list) and any(
            isinstance(item, Mapping)
            and self._desktop_element_matches_output(item, expected_element)
            and item.get("value") == expected_text
            for item in elements
        )
        return (
            fresh,
            bool(valid),
            "A fresh UIA inspection found the same element with exactly the approved value."
            if valid
            else "A fresh UIA inspection did not find the approved value in the same element.",
        )

    def _verify_desktop_send_keys(
        self,
        task_id: str,
        plan: TaskPlan,
        output: Mapping[str, Any],
        context: ToolContext,
    ) -> tuple[ToolResult | None, bool, str]:
        try:
            canonical, _components = normalize_key_chord(str(plan.arguments.get("keys") or ""))
        except PcAgentError:
            return None, False, "The approved desktop key chord is invalid."
        fresh = self._fresh_desktop_windows(task_id, context)
        if not fresh.success or fresh.output is None:
            return fresh, False, fresh.error or "Desktop window re-check after keys failed."
        before = output.get("window")
        after = output.get("window_after")
        before_handle = before.get("handle") if isinstance(before, Mapping) else None
        after_handle = after.get("handle") if isinstance(after, Mapping) else None
        if output.get("window_closed") is True:
            state_ok = after is None and (
                self._desktop_window_by_handle(fresh.output, before_handle) is None
            )
        else:
            state_ok = (
                isinstance(after, Mapping)
                and self._desktop_window_by_handle(fresh.output, after_handle)
                is not None
            )
        expected_repeat = plan.arguments.get("repeat", 1)
        valid = bool(
            output.get("keys") == canonical
            and output.get("repeat") == expected_repeat
            and output.get("sent_count") == expected_repeat
            and state_ok
        )
        return (
            fresh,
            valid,
            "The exact approved key chord was sent the approved number of times and the target window state is consistent."
            if valid
            else "The desktop key result does not match the approved chord/repeat or target window state.",
        )

    def _fresh_image_inspect(
        self,
        task_id: str,
        path: Any,
        context: ToolContext,
        *,
        include_color_stats: bool,
    ) -> ToolResult:
        arguments: dict[str, Any] = {
            "path": path,
            "include_color_stats": include_color_stats,
        }
        fresh = self._tools.invoke("image.inspect", arguments, context)
        self._execution.record_tool_invocation(task_id, arguments, fresh)
        return fresh

    @staticmethod
    def _image_info_matches(output: Mapping[str, Any], fresh: Mapping[str, Any]) -> bool:
        keys = (
            "path",
            "format",
            "mime_type",
            "width",
            "height",
            "mode",
            "frame_count",
            "is_animated",
            "has_alpha",
            "size_bytes",
            "sha256",
            "average_color",
            "dominant_colors",
            "average_hash",
            "exif_orientation",
        )
        return all(output.get(key) == fresh.get(key) for key in keys)

    def _verify_image_inspect(
        self,
        task_id: str,
        plan: TaskPlan,
        output: Mapping[str, Any],
        context: ToolContext,
    ) -> tuple[ToolResult | None, bool, str]:
        fresh = self._tools.invoke("image.inspect", dict(plan.arguments), context)
        self._execution.record_tool_invocation(task_id, dict(plan.arguments), fresh)
        if not fresh.success or fresh.output is None:
            return fresh, False, fresh.error or "Image re-inspection failed."
        valid = dict(fresh.output) == dict(output)
        return (
            fresh,
            valid,
            "The image was independently re-opened and all reported metadata/hash values matched."
            if valid
            else "Image metadata changed between inspection and verification.",
        )

    def _verify_image_output(
        self,
        task_id: str,
        plan: TaskPlan,
        output: Mapping[str, Any],
        context: ToolContext,
    ) -> tuple[ToolResult | None, bool, str]:
        fresh = self._fresh_image_inspect(
            task_id,
            plan.arguments.get("output"),
            context,
            include_color_stats=True,
        )
        if not fresh.success or fresh.output is None:
            return fresh, False, fresh.error or "Image output re-inspection failed."
        valid = self._image_info_matches(output, fresh.output)
        if plan.tool_id == "image.transform":
            operations = output.get("operations")
            valid = valid and isinstance(operations, list) and bool(operations)
            width = fresh.output.get("width")
            height = fresh.output.get("height")
            rotate = plan.arguments.get("rotate_degrees")
            if (
                isinstance(plan.arguments.get("width"), int)
                and isinstance(plan.arguments.get("height"), int)
                and plan.arguments.get("max_dimension") is None
                and not rotate
            ):
                valid = valid and width == plan.arguments["width"] and height == plan.arguments["height"]
            if plan.arguments.get("grayscale") is True:
                valid = valid and fresh.output.get("mode") == "L"
            crop_keys = ("crop_left", "crop_top", "crop_right", "crop_bottom")
            if all(isinstance(plan.arguments.get(key), int) for key in crop_keys) and not any(
                key in plan.arguments
                for key in ("width", "height", "max_dimension", "rotate_degrees")
            ):
                valid = valid and width == (
                    plan.arguments["crop_right"] - plan.arguments["crop_left"]
                ) and height == (
                    plan.arguments["crop_bottom"] - plan.arguments["crop_top"]
                )
        return (
            fresh,
            bool(valid),
            "The written image was independently re-opened; format, dimensions and SHA-256 matched."
            if valid
            else "The written image did not match the reported format/dimensions/hash on re-inspection.",
        )

    def _verify_image_compare(
        self,
        task_id: str,
        plan: TaskPlan,
        output: Mapping[str, Any],
        context: ToolContext,
    ) -> tuple[ToolResult | None, bool, str]:
        fresh = self._tools.invoke("image.compare", dict(plan.arguments), context)
        self._execution.record_tool_invocation(task_id, dict(plan.arguments), fresh)
        if not fresh.success or fresh.output is None:
            return fresh, False, fresh.error or "Image comparison re-check failed."
        valid = dict(fresh.output) == dict(output)
        return (
            fresh,
            valid,
            "Both images were independently re-opened and the comparison metrics matched exactly."
            if valid
            else "Image comparison metrics changed during verification.",
        )

    def _verify_image_analyze(
        self,
        task_id: str,
        plan: TaskPlan,
        output: Mapping[str, Any],
        context: ToolContext,
    ) -> tuple[ToolResult | None, bool, str]:
        fresh = self._fresh_image_inspect(
            task_id,
            plan.arguments.get("path"),
            context,
            include_color_stats=False,
        )
        if not fresh.success or fresh.output is None:
            return fresh, False, fresh.error or "Vision source image re-inspection failed."
        answer = output.get("answer")
        provider = output.get("provider")
        model = output.get("model")
        sent_size = output.get("sent_size_bytes")
        valid = bool(
            self._image_info_matches(output, fresh.output)
            and output.get("question") == plan.arguments.get("question")
            and isinstance(answer, str)
            and bool(answer.strip())
            and isinstance(provider, str)
            and bool(provider.strip())
            and provider != "mock"
            and isinstance(model, str)
            and bool(model.strip())
            and isinstance(sent_size, int)
            and 0 < sent_size <= context.vision_max_image_bytes
        )
        return (
            fresh,
            valid,
            "The exact source image was re-inspected locally and the non-empty vision response is tied to its current hash/dimensions; model semantics are not deterministically re-graded or resent."
            if valid
            else "The vision response is not tied to the approved image/question or a real provider response.",
        )

    def _verify_git_read(
        self,
        task_id: str,
        plan: TaskPlan,
        result: ToolResult,
        context: ToolContext,
    ) -> tuple[ToolResult | None, bool, str]:
        again = self._tools.invoke(plan.tool_id, dict(plan.arguments), context)
        self._execution.record_tool_invocation(
            task_id, dict(plan.arguments), again
        )
        if not again.success or again.output is None:
            return again, False, again.error or "Git re-check failed."
        valid = dict(again.output) == dict(result.output or {})
        return (
            again,
            valid,
            "Git result was re-read and matched exactly."
            if valid
            else "Git result changed between the run and the re-check.",
        )

    def _verify_git_init(
        self, output: Mapping[str, Any]
    ) -> tuple[None, bool, str]:
        raw = output.get("repository")
        if not isinstance(raw, str) or not raw:
            return None, False, "Initialized repository path is missing."
        candidate = Path(raw).expanduser()
        target = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self._settings.workspace_dir / candidate).resolve()
        )
        workspace = self._settings.workspace_dir.expanduser().resolve()
        valid = target.is_relative_to(workspace) and (target / ".git").is_dir()
        return (
            None,
            valid,
            "The .git directory exists inside the workspace."
            if valid
            else "No .git directory found at the reported repository.",
        )

    def _verify_git_add(
        self, task_id: str, plan: TaskPlan, context: ToolContext
    ) -> tuple[ToolResult | None, bool, str]:
        status_args = {"repository": plan.arguments.get("repository", ".")}
        status = self._tools.invoke("git.status", status_args, context)
        self._execution.record_tool_invocation(task_id, status_args, status)
        if not status.success or status.output is None:
            return status, False, status.error or "Git status re-check failed."
        staged = [str(item) for item in (status.output.get("staged") or [])]

        def covered(path: str) -> bool:
            prefix = path.rstrip("/") + "/"
            return any(
                item == path
                or item.startswith(prefix)
                or item.endswith(" -> " + path)
                for item in staged
            )

        wanted = [str(item) for item in (plan.arguments.get("paths") or [])]
        valid = bool(wanted) and all(covered(path) for path in wanted)
        return (
            status,
            valid,
            "Every added path is staged in the re-checked status."
            if valid
            else "An added path is not staged in the re-checked status.",
        )

    def _verify_git_commit(
        self,
        task_id: str,
        plan: TaskPlan,
        output: Mapping[str, Any],
        context: ToolContext,
    ) -> tuple[ToolResult | None, bool, str]:
        log_args = {
            "repository": plan.arguments.get("repository", "."),
            "max_count": 1,
        }
        log = self._tools.invoke("git.log", log_args, context)
        self._execution.record_tool_invocation(task_id, log_args, log)
        if not log.success or log.output is None:
            return log, False, log.error or "Git log re-check failed."
        commits = log.output.get("commits") or []
        if not commits:
            return log, False, "No commit found after git.commit."
        first = commits[0]
        short = output.get("commit")
        valid = (
            isinstance(short, str)
            and bool(short)
            and str(first.get("hash", "")).startswith(short)
            and first.get("subject") == plan.arguments.get("message")
        )
        return (
            log,
            valid,
            "HEAD matches the reported commit and message."
            if valid
            else "HEAD does not match the reported commit.",
        )

    def _verify_command_run(
        self, plan: TaskPlan, output: Mapping[str, Any]
    ) -> tuple[None, bool, str]:
        expected_argv = [str(item) for item in (plan.arguments.get("argv") or [])]
        if (
            output.get("executable") != plan.arguments.get("executable")
            or [str(item) for item in (output.get("argv") or [])] != expected_argv
        ):
            return (
                None,
                False,
                "The executed command does not match the approved arguments.",
            )
        expected_exit = plan.arguments.get("expected_exit_code")
        expected_exit = expected_exit if isinstance(expected_exit, int) else 0
        if output.get("exit_code") != expected_exit:
            return (
                None,
                False,
                (
                    f"Command exited with {output.get('exit_code')}, "
                    f"expected {expected_exit}."
                ),
            )
        return (
            None,
            True,
            "The approved command ran exactly as planned and exited as expected.",
        )

    @staticmethod
    def _url_host(url: str) -> str:
        return url.split("://", 1)[-1].split("/", 1)[0].lower()

    def _fresh_browser_snapshot(
        self, task_id: str, output: Mapping[str, Any], context: ToolContext
    ) -> ToolResult:
        arguments: dict[str, Any] = {}
        target_id = output.get("target_id")
        if isinstance(target_id, str) and target_id:
            arguments["target_id"] = target_id
        fresh = self._tools.invoke("browser.snapshot", arguments, context)
        self._execution.record_tool_invocation(task_id, arguments, fresh)
        return fresh

    def _verify_browser_status(
        self, task_id: str, output: Mapping[str, Any], context: ToolContext
    ) -> tuple[ToolResult | None, bool, str]:
        fresh = self._tools.invoke("browser.status", {}, context)
        self._execution.record_tool_invocation(task_id, {}, fresh)
        if not fresh.success or fresh.output is None:
            return fresh, False, fresh.error or "Browser status re-check failed."
        valid = (
            fresh.output.get("browser") == output.get("browser")
            and fresh.output.get("endpoint") == output.get("endpoint")
        )
        return (
            fresh,
            valid,
            "Browser status was re-checked against the debug endpoint."
            if valid
            else "Browser status changed between execution and verification.",
        )

    def _verify_browser_snapshot(
        self, task_id: str, output: Mapping[str, Any], context: ToolContext
    ) -> tuple[ToolResult | None, bool, str]:
        fresh = self._fresh_browser_snapshot(task_id, output, context)
        if not fresh.success or fresh.output is None:
            return fresh, False, fresh.error or "Browser snapshot re-read failed."
        valid = (
            fresh.output.get("url") == output.get("url")
            and isinstance(output.get("text"), str)
            and isinstance(output.get("title"), str)
        )
        return (
            fresh,
            valid,
            "Page URL still matches a fresh snapshot of the same tab."
            if valid
            else "Page URL changed between snapshot and verification.",
        )

    def _verify_browser_open(
        self, task_id: str, output: Mapping[str, Any], context: ToolContext
    ) -> tuple[ToolResult | None, bool, str]:
        fresh = self._fresh_browser_snapshot(task_id, output, context)
        if not fresh.success or fresh.output is None:
            return fresh, False, fresh.error or "Opened page could not be re-read."
        final_url = str(output.get("url") or "")
        fresh_url = str(fresh.output.get("url") or "")
        valid = bool(
            final_url
            and fresh_url
            and final_url.split("://", 1)[0] in {"http", "https"}
            and self._url_host(fresh_url) == self._url_host(final_url)
        )
        return (
            fresh,
            valid,
            "Opened tab was re-read and is on the reported host."
            if valid
            else "Opened tab is not on the reported page.",
        )

    def _verify_browser_click(
        self, task_id: str, output: Mapping[str, Any], context: ToolContext
    ) -> tuple[ToolResult | None, bool, str]:
        fresh = self._fresh_browser_snapshot(task_id, output, context)
        if not fresh.success or fresh.output is None:
            return fresh, False, fresh.error or "Page could not be re-read after click."
        claimed = str(output.get("url") or "")
        fresh_url = str(fresh.output.get("url") or "")
        valid = bool(
            output.get("clicked") is True
            and claimed
            and fresh_url
            and self._url_host(fresh_url) == self._url_host(claimed)
        )
        return (
            fresh,
            valid,
            "Click was dispatched and the page was re-read on the reported host."
            if valid
            else "Click result could not be confirmed on the reported page.",
        )

    def _verify_browser_type(
        self,
        task_id: str,
        plan: TaskPlan,
        output: Mapping[str, Any],
        context: ToolContext,
    ) -> tuple[ToolResult | None, bool, str]:
        expected = plan.arguments.get("text")
        if output.get("value") != expected:
            return (
                None,
                False,
                "Field read-back does not match the approved text.",
            )
        fresh = self._fresh_browser_snapshot(task_id, output, context)
        if not fresh.success or fresh.output is None:
            return fresh, False, fresh.error or "Page could not be re-read after typing."
        valid = fresh.output.get("url") == output.get("url")
        return (
            fresh,
            valid,
            "Field value read-back matches and the page did not navigate."
            if valid
            else "Page navigated away after typing; result is unconfirmed.",
        )

    def _verify_browser_screenshot(
        self, output: Mapping[str, Any]
    ) -> tuple[None, bool, str]:
        raw = str(output.get("output_path") or "")
        target = self._settings.workspace_dir / raw if raw else None
        if target is None or not target.is_file():
            return None, False, "Screenshot file is missing from the workspace."
        data_ok = target.stat().st_size == output.get("size_bytes")
        magic_ok = target.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        hash_ok = _stream_sha256(target) == output.get("sha256")
        valid = bool(data_ok and magic_ok and hash_ok)
        return (
            None,
            valid,
            "Screenshot PNG exists with matching size and SHA-256."
            if valid
            else "Screenshot file failed PNG/size/hash verification.",
        )

    def _verify_pdf_split(
        self, task_id: str, output: Mapping[str, Any], context: ToolContext
    ) -> tuple[ToolResult | None, bool, str]:
        outputs = output.get("outputs")
        if not isinstance(outputs, list) or output.get("page_count") != len(outputs):
            return None, False, "Split page count did not match the output list."
        last: ToolResult | None = None
        for item in outputs:
            arguments: dict[str, Any] = {"path": item, "expected_pages": 1}
            verification = self._tools.invoke("pdf.verify", arguments, context)
            self._execution.record_tool_invocation(task_id, arguments, verification)
            last = verification
            if (
                not verification.success
                or verification.output is None
                or not verification.output.get("valid")
            ):
                return verification, False, f"Split page failed verification: {item}"
        source = self._tools.invoke(
            "pdf.inspect", {"path": output.get("path")}, context
        )
        self._execution.record_tool_invocation(
            task_id, {"path": output.get("path")}, source
        )
        if (
            not source.success
            or source.output is None
            or source.output.get("pages") != len(outputs)
        ):
            return last, False, "Split output count did not match the source pages."
        return last, True, "Every split page PDF was re-opened and verified."

    def _verify_docx_replace(
        self, plan: TaskPlan, output: Mapping[str, Any]
    ) -> tuple[None, bool, str]:
        source = self._workspace_file(output.get("input_path"))
        target = self._workspace_file(output.get("output_path"))
        if source is None or target is None:
            return None, False, "DOCX input/output is missing or outside the workspace."
        find = str(plan.arguments.get("find") or "")
        replace = str(plan.arguments.get("replace") or "")
        try:
            source_text = extract_office_text(source).text
            output_text = extract_office_text(target).text
        except PcAgentError as exc:
            return None, False, f"Could not re-read DOCX text: {exc}"
        reported = output.get("replacements")
        if source_text.count(find) != reported:
            return None, False, "Reported replacements do not match the source text."
        if replace and output_text.count(replace) < int(reported or 0):
            return None, False, "Replacement text is missing from the output document."
        if find and find not in replace and find in output_text:
            return None, False, "The old text still appears in the output document."
        return None, True, "DOCX text replacement was re-read and counted independently."

    def _verify_docx_append(
        self, plan: TaskPlan, output: Mapping[str, Any]
    ) -> tuple[None, bool, str]:
        target = self._workspace_file(output.get("output_path"))
        if target is None:
            return None, False, "Appended DOCX is missing or outside the workspace."
        paragraphs = plan.arguments.get("paragraphs")
        if not isinstance(paragraphs, list) or not paragraphs:
            return None, False, "The append plan contains no paragraphs to verify."
        try:
            output_text = extract_office_text(target).text
        except PcAgentError as exc:
            return None, False, f"Could not re-read DOCX text: {exc}"
        for paragraph in paragraphs:
            needle = str(paragraph).strip()
            if needle and needle not in output_text:
                return None, False, "An appended paragraph is missing from the output."
        return None, True, "Every appended paragraph was found in the re-read document."

    def _verify_xlsx_set_cells(
        self, plan: TaskPlan, output: Mapping[str, Any]
    ) -> tuple[None, bool, str]:
        target = self._workspace_file(output.get("output_path"))
        if target is None:
            return None, False, "Edited XLSX is missing or outside the workspace."
        try:
            workbook = read_xlsx_cells(target)
        except PcAgentError as exc:
            return None, False, f"Could not re-read XLSX cells: {exc}"
        sheet = next(
            (item for item in workbook.sheets if item.name == output.get("sheet")),
            workbook.sheets[0] if workbook.sheets else None,
        )
        if sheet is None:
            return None, False, "Edited XLSX contains no readable sheet."
        planned = plan.arguments.get("cells")
        if not isinstance(planned, list):
            return None, False, "The set-cells plan is malformed."
        for item in planned:
            if not isinstance(item, dict):
                return None, False, "The set-cells plan is malformed."
            reference = str(item.get("reference") or "").upper()
            expected = str(item.get("value") or "")
            cell = sheet.cells.get(reference)
            if cell is None:
                return None, False, f"Cell {reference} was not written."
            if expected.startswith("="):
                if (cell.formula or "").replace(" ", "") != expected[1:].replace(" ", ""):
                    return None, False, f"Cell {reference} formula does not match the plan."
            elif expected and cell.value != expected and not (
                cell.kind == "number"
                and _numbers_close(cell.value, expected)
            ):
                return None, False, f"Cell {reference} value does not match the plan."
        return self._compare_formula_output(workbook, output)

    def _verify_xlsx_formulas(
        self, output: Mapping[str, Any]
    ) -> tuple[None, bool, str]:
        target = self._workspace_file(output.get("path"))
        if target is None:
            return None, False, "XLSX file is missing or outside the workspace."
        try:
            workbook = read_xlsx_cells(target)
        except PcAgentError as exc:
            return None, False, f"Could not re-read XLSX cells: {exc}"
        return self._compare_formula_output(workbook, output)

    def _compare_formula_output(
        self, workbook: Any, output: Mapping[str, Any]
    ) -> tuple[None, bool, str]:
        reported = output.get("formulas")
        if not isinstance(reported, list):
            return None, False, "The tool did not report formula evaluations."
        evaluations = evaluate_workbook_formulas(workbook)
        expected = {
            (item.sheet, item.reference): item for item in evaluations
        }
        if len(reported) != len(expected):
            return None, False, "Formula count does not match an independent re-read."
        for item in reported:
            if not isinstance(item, dict):
                return None, False, "Malformed formula report entry."
            key = (str(item.get("sheet")), str(item.get("reference")))
            evaluation = expected.get(key)
            if evaluation is None:
                return None, False, f"Unknown formula reported at {key[1]}."
            computed = item.get("computed")
            if evaluation.value is None:
                if computed is not None:
                    return None, False, f"Formula {key[1]} should not have computed."
            elif not isinstance(computed, (int, float)) or abs(
                float(computed) - evaluation.value
            ) > 1e-9:
                return None, False, f"Formula {key[1]} computed value mismatch."
        return None, True, "Formulas were recomputed independently and all matched."

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
