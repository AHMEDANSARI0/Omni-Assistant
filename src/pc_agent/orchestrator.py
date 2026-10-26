"""Task orchestration: plan -> approve -> run one tool -> verify -> report."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .domain.task import Task, TaskStatus
from .errors import PcAgentError, ProviderError
from .planning import (
    DeterministicPlanner,
    PlannerOptions,
    PlanningOutcome,
    TaskPlan,
    plan_with_provider,
)
from .providers.router import ModelRouter, ProviderAttempt
from .storage.repositories import ExecutionRepository, TaskRepository
from .tools.registry import ToolContext, ToolRegistry, ToolResult

_ALLOWED_ORCHESTRATED_TOOLS = frozenset(
    {
        "pdf.merge_folders",
        "pdf.merge",
        "pdf.verify",
        "pdf.inspect",
        "workspace.list_files",
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
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.task.status is TaskStatus.SUCCEEDED


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
    ) -> None:
        self._settings = settings
        self._tasks = task_repository
        self._execution = execution_repository
        self._tools = tool_registry
        self._planner = deterministic_planner
        self._router = model_router

    def execute(
        self,
        task_id: str,
        *,
        options: PlannerOptions | None = None,
        approval_callback: ApprovalCallback | None = None,
        use_ai: bool = False,
    ) -> ExecutionReport:
        options = options or PlannerOptions()
        task = self._require_task(task_id)
        if task.is_terminal:
            raise PcAgentError(
                f"Task {task.id} is already {task.status.value}; create a new task to run it again."
            )

        if task.status is TaskStatus.WAITING_FOR_APPROVAL:
            plan, failure = self._stored_plan(task)
            if failure is not None:
                return failure
            if plan is None:  # Defensive; _stored_plan returns a failure in this case.
                raise PcAgentError("Stored plan could not be loaded.")
            return self._approve_and_run(task, plan, approval_callback, ())

        if task.status not in {TaskStatus.QUEUED, TaskStatus.NEEDS_INPUT}:
            raise PcAgentError(
                f"Task {task.id} cannot be executed from status {task.status.value}."
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
                    return ExecutionReport(task=failed, error=str(exc))

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
                error=message,
            )

        self._execution.save_plan(task.id, plan.to_dict(), plan.planner)
        self._tasks.record_event(task.id, "plan_created", plan.summary)
        return self._approve_and_run(
            task, plan, approval_callback, outcome.provider_attempts
        )

    def _stored_plan(self, task: Task) -> tuple[TaskPlan | None, ExecutionReport | None]:
        stored = self._execution.get_plan(task.id)
        if stored is None:
            message = "Task is waiting for approval but no stored plan was found."
            failed = self._tasks.update_status(
                task.id, TaskStatus.FAILED, message="Stored plan missing", error=message
            )
            return None, ExecutionReport(task=failed, error=message)
        plan = TaskPlan.from_dict(stored)
        if plan.tool_id not in _ALLOWED_ORCHESTRATED_TOOLS:
            message = f"Stored plan requested a disallowed tool: {plan.tool_id}"
            failed = self._tasks.update_status(
                task.id, TaskStatus.FAILED, message="Unsafe stored plan rejected", error=message
            )
            return None, ExecutionReport(task=failed, plan=plan, error=message)
        return plan, None

    def _approve_and_run(
        self,
        task: Task,
        plan: TaskPlan,
        approval_callback: ApprovalCallback | None,
        provider_attempts: tuple[ProviderAttempt, ...],
    ) -> ExecutionReport:
        if plan.external_read_roots:
            if task.status is TaskStatus.PLANNING:
                task = self._tasks.update_status(
                    task.id,
                    TaskStatus.WAITING_FOR_APPROVAL,
                    message="Approval required for read-only external input folder(s)",
                )
            if approval_callback is None:
                from .errors import ApprovalUnavailableError

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
                    error=message,
                )

        task = self._tasks.update_status(task.id, TaskStatus.RUNNING, message=plan.summary)
        context = ToolContext.for_workspace(
            self._settings.workspace_dir,
            approved_read_roots=plan.external_read_roots,
        )
        result = self._tools.invoke(plan.tool_id, plan.arguments, context)
        self._execution.record_tool_invocation(task.id, plan.arguments, result)

        if not result.success or result.output is None:
            message = result.error or "Tool failed without an error message."
            failed = self._tasks.update_status(
                task.id, TaskStatus.FAILED, message="Tool execution failed", error=message
            )
            return ExecutionReport(
                task=failed,
                plan=plan,
                tool_result=result,
                provider_attempts=provider_attempts,
                error=message,
            )

        task = self._tasks.update_status(
            task.id, TaskStatus.VERIFYING, message="Verifying tool output deterministically"
        )
        verification_result, valid, verification_message = self._verify(
            task.id, plan, result, context
        )
        if not valid:
            failed = self._tasks.update_status(
                task.id,
                TaskStatus.FAILED,
                message="Verification failed",
                error=verification_message,
            )
            return ExecutionReport(
                task=failed,
                plan=plan,
                tool_result=result,
                verification_result=verification_result,
                verification_valid=False,
                verification_message=verification_message,
                provider_attempts=provider_attempts,
                error=verification_message,
            )

        succeeded = self._tasks.update_status(
            task.id, TaskStatus.SUCCEEDED, message="Execution and verification completed"
        )
        self._tasks.record_event(task.id, "verified", verification_message)
        return ExecutionReport(
            task=succeeded,
            plan=plan,
            tool_result=result,
            verification_result=verification_result,
            verification_valid=True,
            verification_message=verification_message,
            provider_attempts=provider_attempts,
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

        return None, False, f"No deterministic verifier is registered for {plan.tool_id}."

    def _require_task(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise PcAgentError(f"Task not found: {task_id}")
        return task
