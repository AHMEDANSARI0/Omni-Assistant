"""Deterministic, bounded self-healing for orchestrated local tasks.

Phase 3 does not let a model invent recovery actions. Local code classifies a
small set of known failures, may revise only safe plan parameters (a fresh
workspace output or a uniquely corrected workspace input path), and records
every attempt. Policy, approval and invalid-input failures are never bypassed.
"""

from __future__ import annotations

import copy
import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Settings
from .domain.task import Task, TaskStatus, utc_now
from .errors import PcAgentError, PlanningError, RecoveryError
from .planning import MultiStepPlan, PlannerOptions, TaskPlan, parse_stored_plan
from .storage.repositories import (
    ExecutionRepository,
    RecoveryAttemptRecord,
    RecoveryLinkRecord,
    RecoveryRepository,
    TaskRepository,
    ToolInvocationRecord,
)

if TYPE_CHECKING:
    from .orchestrator import ApprovalCallback, ExecutionReport, TaskOrchestrator

MAX_MANUAL_RECOVERY_CHAIN = 3
ACTIVE_STATUSES = frozenset(
    {
        TaskStatus.PLANNING,
        TaskStatus.RUNNING,
        TaskStatus.VERIFYING,
        TaskStatus.RETRYING,
    }
)
_RESUMABLE_STATUSES = frozenset(
    {TaskStatus.QUEUED, TaskStatus.NEEDS_INPUT, TaskStatus.WAITING_FOR_APPROVAL}
)
_MAX_WORKSPACE_SCAN = 20_000
_TEMP_PDF_RE = re.compile(r"^\..+\.tmp-[0-9a-f]{32}\.pdf$", re.IGNORECASE)

_POLICY_MARKERS = (
    "policy ",
    "outside the workspace",
    "outside the approved",
    "credential",
    "sensitive path",
    "not allowed",
    "permission policy",
)
_APPROVAL_MARKERS = (
    "approval was denied",
    "approval is required",
    "waiting_for_approval",
    "no interactive approval",
)
_INVALID_MARKERS = (
    "encrypted pdf",
    "could not read pdf",
    "invalid pdf",
    "at least two valid pdfs",
    "no pdf files were found",
    "only docx, xlsx, pptx and csv",
    "legacy or macro-enabled office",
    "not a valid office zip",
    "could not parse office xml",
    "office package is missing",
    "csv file is empty",
    "csv headers",
    "json file could not be parsed",
    "json-to-csv needs",
    "not valid utf-8",
    "could not decode image",
    "unsupported image format",
    "unknown field",
    "must be an object",
    "is missing required field",
    "tool not found",
    "no enabled, approved version",
)
_OUTPUT_TOOLS = frozenset(
    {
        "pdf.merge",
        "pdf.merge_folders",
        "office.extract_text",
        "office.csv_to_json",
        "office.json_to_csv",
        "office.csv_to_xlsx",
        "office.create_docx",
        # Only the copy: its source survives a failed attempt, so retrying to
        # a fresh output is safe. A move/rename source may already be gone.
        "files.copy",
        # Phase 9 editors also write a brand-new output from a surviving
        # source, so fresh-output retries are equally safe for them.
        "pdf.extract_pages",
        "office.docx_replace_text",
        "office.docx_append_paragraphs",
        "office.xlsx_set_cells",
        # Phase 15 image writers create a new file from a surviving source.
        "image.convert",
        "image.transform",
    }
)
# Phase 12/13/14/15: re-running a command, browser/desktop action or a
# provider vision send can duplicate side effects/cost, so these steps are
# never auto-retried - not even after a verification failure.
_SUBPROCESS_TOOLS = frozenset(
    {
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
        "image.analyze",
    }
)
_TRANSIENT_MARKERS = (
    "timed out",
    "timeout",
    "temporar",
    "locked",
    "being used by another process",
    "in use",
    "sharing violation",
    "winerror 32",
    "device or resource busy",
    "connection reset",
    "connection aborted",
    "try again",
)


class FailureCategory(str, enum.Enum):
    OUTPUT_EXISTS = "output_exists"
    MISSING_INPUT = "missing_input"
    VERIFICATION_FAILED = "verification_failed"
    TRANSIENT = "transient"
    INTERRUPTED = "interrupted"
    POLICY = "policy"
    APPROVAL = "approval"
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class RecoveryAction(str, enum.Enum):
    EXECUTE_PLAN = "execute_plan"
    RETRY_SAME_PLAN = "retry_same_plan"
    RETRY_NEW_OUTPUT = "retry_new_output"
    RETRY_CORRECTED_PATH = "retry_corrected_path"
    RESUME_STORED_PLAN = "resume_stored_plan"
    CLONE_TASK = "clone_task"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class FailureDiagnosis:
    category: FailureCategory
    action: RecoveryAction
    retryable: bool
    message: str
    original_error: str
    revised_arguments: Mapping[str, Any] | None = None
    needs_input: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "action": self.action.value,
            "retryable": self.retryable,
            "needs_input": self.needs_input,
            "message": self.message,
            "original_error": self.original_error,
            "revised_arguments": (
                copy.deepcopy(dict(self.revised_arguments))
                if self.revised_arguments is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class RecoveryAttemptSummary:
    attempt_number: int
    stage: str
    status: str
    action: str
    category: str | None = None
    retryable: bool = False
    message: str | None = None
    original_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_number": self.attempt_number,
            "stage": self.stage,
            "status": self.status,
            "action": self.action,
            "category": self.category,
            "retryable": self.retryable,
            "message": self.message,
            "original_error": self.original_error,
        }


@dataclass(frozen=True, slots=True)
class TaskDiagnosis:
    task: Task
    plan: TaskPlan | None
    diagnosis: FailureDiagnosis | None
    latest_invocation: ToolInvocationRecord | None
    attempts: tuple[RecoveryAttemptRecord, ...]
    links: tuple[RecoveryLinkRecord, ...]
    stale: bool
    age_seconds: float
    plan_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task.id,
            "status": self.task.status.value,
            "instruction": self.task.instruction,
            "error": self.task.error,
            "stale": self.stale,
            "age_seconds": round(self.age_seconds, 3),
            "plan": self.plan.to_dict() if self.plan else None,
            "plan_error": self.plan_error,
            "diagnosis": self.diagnosis.to_dict() if self.diagnosis else None,
            "latest_invocation": (
                {
                    "tool_id": self.latest_invocation.tool_id,
                    "tool_version": self.latest_invocation.tool_version,
                    "success": self.latest_invocation.success,
                    "error": self.latest_invocation.error,
                }
                if self.latest_invocation
                else None
            ),
            "recovery_attempts": [
                {
                    "attempt_number": attempt.attempt_number,
                    "stage": attempt.stage,
                    "status": attempt.status,
                    "category": attempt.category,
                    "action": attempt.action,
                    "retryable": attempt.retryable,
                    "message": attempt.message,
                    "original_error": attempt.original_error,
                }
                for attempt in self.attempts
            ],
            "recovery_links": [
                {
                    "source_task_id": link.source_task_id,
                    "recovery_task_id": link.recovery_task_id,
                    "reason": link.reason,
                }
                for link in self.links
            ],
        }


@dataclass(frozen=True, slots=True)
class RecoveryScan:
    stale_tasks: tuple[Task, ...]
    orphan_temp_pdfs: tuple[Path, ...]
    stale_after_minutes: int

    def to_dict(self, *, workspace_dir: Path) -> dict[str, Any]:
        return {
            "stale_after_minutes": self.stale_after_minutes,
            "stale_tasks": [
                {
                    "task_id": task.id,
                    "status": task.status.value,
                    "instruction": task.instruction,
                    "updated_at": task.updated_at.isoformat() if task.updated_at else None,
                }
                for task in self.stale_tasks
            ],
            "orphan_temp_pdfs": [
                _relative_posix(workspace_dir, path) for path in self.orphan_temp_pdfs
            ],
        }


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    source_task: Task
    recovery_task: Task
    cloned: bool
    report: ExecutionReport | None
    message: str


def diagnose_tool_failure(
    plan: TaskPlan, error: str, *, workspace_dir: Path
) -> FailureDiagnosis:
    workspace = workspace_dir.expanduser().resolve()
    if plan.tool_id in _SUBPROCESS_TOOLS:
        return FailureDiagnosis(
            FailureCategory.UNKNOWN,
            RecoveryAction.NONE,
            False,
            "Subprocess steps are never auto-retried: re-running a command "
            "can duplicate side effects. Fix the cause and run the task again.",
            error,
        )
    lowered = error.lower()

    if any(marker in lowered for marker in _POLICY_MARKERS):
        return FailureDiagnosis(
            FailureCategory.POLICY,
            RecoveryAction.NONE,
            False,
            "Policy failures are never retried or bypassed automatically.",
            error,
        )
    if any(marker in lowered for marker in _APPROVAL_MARKERS):
        return FailureDiagnosis(
            FailureCategory.APPROVAL,
            RecoveryAction.NONE,
            False,
            "Approval failures require an explicit user decision.",
            error,
        )

    if plan.tool_id in _OUTPUT_TOOLS:
        output_diagnosis = _output_collision_diagnosis(plan, error, workspace)
        if output_diagnosis is not None:
            return output_diagnosis

    corrected, has_missing = _correct_missing_arguments(plan, workspace)
    looks_missing = has_missing or "not found" in lowered or "no such file" in lowered
    if corrected is not None:
        return FailureDiagnosis(
            FailureCategory.MISSING_INPUT,
            RecoveryAction.RETRY_CORRECTED_PATH,
            True,
            "A unique matching workspace input was found; retrying with the corrected path.",
            error,
            revised_arguments=corrected,
        )
    if looks_missing:
        return FailureDiagnosis(
            FailureCategory.MISSING_INPUT,
            RecoveryAction.NONE,
            False,
            "An input is missing and no unique workspace correction was found.",
            error,
            needs_input=True,
        )

    if any(marker in lowered for marker in _INVALID_MARKERS):
        return FailureDiagnosis(
            FailureCategory.INVALID_INPUT,
            RecoveryAction.NONE,
            False,
            "The input or tool contract is invalid; an identical retry would not heal it.",
            error,
        )

    if plan.tool_id in _OUTPUT_TOOLS and (
        "winerror 5" in lowered
        or "access is denied" in lowered
        or "permissionerror" in lowered
    ):
        revised = _arguments_with_fresh_output(plan, workspace)
        if revised is not None:
            return FailureDiagnosis(
                FailureCategory.TRANSIENT,
                RecoveryAction.RETRY_NEW_OUTPUT,
                True,
                "The output appears locked or unavailable; retrying to a fresh workspace file.",
                error,
                revised_arguments=revised,
            )

    if any(marker in lowered for marker in _TRANSIENT_MARKERS):
        return FailureDiagnosis(
            FailureCategory.TRANSIENT,
            RecoveryAction.RETRY_SAME_PLAN,
            True,
            "The failure looks transient; retrying the exact same validated plan.",
            error,
        )

    return FailureDiagnosis(
        FailureCategory.UNKNOWN,
        RecoveryAction.NONE,
        False,
        "No safe deterministic recovery rule matches this failure.",
        error,
    )


def diagnose_verification_failure(
    plan: TaskPlan, error: str, *, workspace_dir: Path
) -> FailureDiagnosis:
    workspace = workspace_dir.expanduser().resolve()
    if plan.tool_id in _SUBPROCESS_TOOLS:
        return FailureDiagnosis(
            FailureCategory.VERIFICATION_FAILED,
            RecoveryAction.NONE,
            False,
            "Subprocess steps are never auto-retried after a verification "
            "failure: re-running a command can duplicate side effects.",
            error,
        )
    if plan.tool_id in _OUTPUT_TOOLS:
        revised = _arguments_with_fresh_output(plan, workspace)
        if revised is not None:
            return FailureDiagnosis(
                FailureCategory.VERIFICATION_FAILED,
                RecoveryAction.RETRY_NEW_OUTPUT,
                True,
                "The generated output did not verify; retrying once to a fresh output file.",
                error,
                revised_arguments=revised,
            )
    if plan.tool_id == "workspace.list_files":
        return FailureDiagnosis(
            FailureCategory.VERIFICATION_FAILED,
            RecoveryAction.RETRY_SAME_PLAN,
            True,
            "The workspace may have changed during listing; retrying the read-only check.",
            error,
        )
    corrected, has_missing = _correct_missing_arguments(plan, workspace)
    if corrected is not None:
        return FailureDiagnosis(
            FailureCategory.MISSING_INPUT,
            RecoveryAction.RETRY_CORRECTED_PATH,
            True,
            "The verified target moved, but a unique matching workspace file was found.",
            error,
            revised_arguments=corrected,
        )
    if has_missing:
        return FailureDiagnosis(
            FailureCategory.MISSING_INPUT,
            RecoveryAction.NONE,
            False,
            "The verified target is missing and no unique workspace correction was found.",
            error,
            needs_input=True,
        )
    return FailureDiagnosis(
        FailureCategory.INVALID_INPUT,
        RecoveryAction.NONE,
        False,
        "Deterministic verification reported the target itself is invalid.",
        error,
    )


def _output_collision_diagnosis(
    plan: TaskPlan, error: str, workspace: Path
) -> FailureDiagnosis | None:
    output_key = _output_argument_key(plan)
    if output_key is None:
        return None
    raw_output = plan.arguments.get(output_key)
    if not isinstance(raw_output, str) or not raw_output:
        return None
    target = _workspace_target(workspace, raw_output)
    if target is None:
        return None
    lowered = error.lower()
    if not any(
        marker in lowered
        for marker in ("already exists", "output file exists", "file exists", "eexist")
    ):
        return None
    revised = _arguments_with_fresh_output(plan, workspace)
    if revised is None:
        return None
    return FailureDiagnosis(
        FailureCategory.OUTPUT_EXISTS,
        RecoveryAction.RETRY_NEW_OUTPUT,
        True,
        "The planned output already exists and will be preserved; retrying to a fresh file.",
        error,
        revised_arguments=revised,
    )


def _arguments_with_fresh_output(
    plan: TaskPlan, workspace: Path
) -> dict[str, Any] | None:
    output_key = _output_argument_key(plan)
    if output_key is None:
        return None
    raw_output = plan.arguments.get(output_key)
    if not isinstance(raw_output, str) or not raw_output:
        return None
    target = _workspace_target(workspace, raw_output)
    if target is None:
        return None
    fresh = unique_workspace_output(workspace, raw_output)
    if fresh == raw_output:
        return None
    revised = copy.deepcopy(dict(plan.arguments))
    revised[output_key] = fresh
    return revised


def _output_argument_key(plan: TaskPlan) -> str | None:
    for key in ("output_path", "output"):
        value = plan.arguments.get(key)
        if isinstance(value, str) and value:
            return key
    return None


def unique_workspace_output(workspace_dir: Path, raw_output: str) -> str:
    workspace = workspace_dir.expanduser().resolve()
    target = _workspace_target(workspace, raw_output)
    if target is None:
        return raw_output
    if not target.exists():
        return target.relative_to(workspace).as_posix()
    for counter in range(1, 1000):
        suffix = "-recovered" if counter == 1 else f"-recovered-{counter}"
        candidate = target.with_name(f"{target.stem}{suffix}{target.suffix}")
        if not candidate.exists():
            return candidate.relative_to(workspace).as_posix()
    return raw_output


def _correct_missing_arguments(
    plan: TaskPlan, workspace: Path
) -> tuple[dict[str, Any] | None, bool]:
    if plan.external_read_roots:
        # A workspace substitution would silently change an approved external
        # scope. Recovery may still change the workspace output, with fresh
        # approval, but it may not reinterpret external inputs.
        return None, False

    corrected = copy.deepcopy(dict(plan.arguments))
    changed = False
    missing = False

    def correct_one(raw: Any, *, directory: bool) -> Any:
        nonlocal changed, missing
        if not isinstance(raw, str) or not raw:
            return raw
        target = _workspace_target(workspace, raw)
        if target is None:
            return raw
        exists = target.is_dir() if directory else target.is_file()
        if exists:
            return raw
        missing = True
        match = _unique_workspace_match(workspace, target, directory=directory)
        if match is None:
            return raw
        changed = True
        return match.relative_to(workspace).as_posix()

    if plan.tool_id == "pdf.merge":
        raw_items = corrected.get("input_paths")
        if isinstance(raw_items, list):
            corrected["input_paths"] = [
                correct_one(item, directory=False) for item in raw_items
            ]
    elif plan.tool_id == "pdf.merge_folders":
        raw_items = corrected.get("input_folders")
        if isinstance(raw_items, list):
            corrected["input_folders"] = [
                correct_one(item, directory=True) for item in raw_items
            ]
    elif plan.tool_id in {
        "pdf.verify",
        "pdf.inspect",
        "office.inspect",
        "office.extract_text",
        "office.csv_inspect",
    }:
        corrected["path"] = correct_one(corrected.get("path"), directory=False)
    elif plan.tool_id in {
        "office.csv_to_json",
        "office.json_to_csv",
        "office.csv_to_xlsx",
    }:
        corrected["input_path"] = correct_one(
            corrected.get("input_path"), directory=False
        )
    elif plan.tool_id == "workspace.list_files":
        corrected["directory"] = correct_one(
            corrected.get("directory") or ".", directory=True
        )

    if changed and corrected != dict(plan.arguments):
        # Only call it healed if every argument we know how to inspect now
        # exists. A second diagnosis after a failed retry can handle the rest,
        # but within the bounded attempt limit.
        return corrected, missing
    return None, missing


def _unique_workspace_match(
    workspace: Path, missing_target: Path, *, directory: bool
) -> Path | None:
    wanted_name = missing_target.name.casefold()
    if not wanted_name or wanted_name in {".", ".."}:
        return None
    matches: list[Path] = []
    scanned = 0
    for candidate in workspace.rglob("*"):
        scanned += 1
        if scanned > _MAX_WORKSPACE_SCAN:
            return None
        if candidate.name.casefold() != wanted_name:
            continue
        if directory and not candidate.is_dir():
            continue
        if not directory and not candidate.is_file():
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(workspace):
            continue
        matches.append(resolved)
        if len(matches) > 1:
            return None
    return matches[0] if matches else None


def _workspace_target(workspace: Path, raw_path: str) -> Path | None:
    candidate = Path(raw_path).expanduser()
    target = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    return target if target.is_relative_to(workspace) else None


def is_stale_task(task: Task, *, stale_after_minutes: int, now: datetime | None = None) -> bool:
    if task.status not in ACTIVE_STATUSES:
        return False
    reference = now or utc_now()
    updated = task.updated_at or task.created_at
    return updated <= reference - timedelta(minutes=stale_after_minutes)


def task_age_seconds(task: Task, *, now: datetime | None = None) -> float:
    reference = now or utc_now()
    updated = task.updated_at or task.created_at
    return max(0.0, (reference - updated).total_seconds())


def find_orphan_temp_pdfs(
    workspace_dir: Path, *, stale_after_minutes: int, now: datetime | None = None
) -> tuple[Path, ...]:
    workspace = workspace_dir.expanduser().resolve()
    if not workspace.is_dir():
        return ()
    reference = now or utc_now()
    cutoff = reference - timedelta(minutes=stale_after_minutes)
    results: list[Path] = []
    scanned = 0
    for candidate in workspace.rglob(".*.tmp-*.pdf"):
        scanned += 1
        if scanned > _MAX_WORKSPACE_SCAN:
            break
        if not candidate.is_file() or not _TEMP_PDF_RE.match(candidate.name):
            continue
        try:
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=UTC)
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_relative_to(workspace) and modified <= cutoff:
            results.append(resolved)
    return tuple(sorted(results, key=lambda item: item.as_posix().casefold()))


class RecoveryService:
    """Manual recovery for interrupted/failed tasks using linked clones."""

    def __init__(
        self,
        *,
        settings: Settings,
        task_repository: TaskRepository,
        execution_repository: ExecutionRepository,
        recovery_repository: RecoveryRepository,
        orchestrator: TaskOrchestrator,
    ) -> None:
        self._settings = settings
        self._tasks = task_repository
        self._execution = execution_repository
        self._recovery = recovery_repository
        self._orchestrator = orchestrator

    def _stored_plan(
        self, task_id: str
    ) -> tuple[dict[str, Any] | None, TaskPlan | MultiStepPlan | None, str | None]:
        try:
            stored = self._execution.get_plan(task_id)
            if stored is None:
                return None, None, None
            return stored, parse_stored_plan(stored), None
        except (PcAgentError, PlanningError, TypeError, ValueError) as exc:
            return None, None, str(exc)

    def diagnose(self, task_id: str) -> TaskDiagnosis:
        task = self._require_task(task_id)
        _stored, parsed, plan_error = self._stored_plan(task.id)
        plan = parsed if isinstance(parsed, TaskPlan) else None
        invocations = self._execution.list_tool_invocations(task.id)
        latest = invocations[-1] if invocations else None
        stale = is_stale_task(
            task, stale_after_minutes=self._settings.recovery_stale_after_minutes
        )

        diagnosis: FailureDiagnosis | None = None
        if stale:
            diagnosis = FailureDiagnosis(
                FailureCategory.INTERRUPTED,
                RecoveryAction.CLONE_TASK,
                True,
                "The task is in an active state but has not updated within the stale threshold.",
                task.error or "Task appears interrupted.",
            )
        elif task.status is TaskStatus.FAILED:
            error = (latest.error if latest and latest.error else task.error) or "Task failed."
            if isinstance(parsed, MultiStepPlan):
                diagnosis = FailureDiagnosis(
                    FailureCategory.UNKNOWN,
                    RecoveryAction.CLONE_TASK,
                    True,
                    (
                        "This multi-step task failed at one of its steps; a "
                        "recovery clone re-runs the stored multi-step plan "
                        "from the first step (writers choose fresh outputs "
                        "on collision)."
                    ),
                    error,
                )
            elif plan is None:
                diagnosis = FailureDiagnosis(
                    FailureCategory.UNSUPPORTED,
                    RecoveryAction.CLONE_TASK,
                    True,
                    (
                        "The stored plan is invalid; a manual recovery task can replan "
                        "the instruction from scratch."
                        if plan_error
                        else "No stored plan exists; a manual recovery task can replan the instruction."
                    ),
                    plan_error or error,
                )
            elif latest is not None and latest.success:
                diagnosis = diagnose_verification_failure(
                    plan, task.error or error, workspace_dir=self._settings.workspace_dir
                )
            else:
                diagnosis = diagnose_tool_failure(
                    plan, error, workspace_dir=self._settings.workspace_dir
                )
        elif task.status is TaskStatus.NEEDS_INPUT:
            diagnosis = FailureDiagnosis(
                FailureCategory.MISSING_INPUT,
                RecoveryAction.NONE,
                False,
                "Supply the missing input/options, then run or recover the task.",
                task.error or "More information is required.",
                needs_input=True,
            )

        successful_recovery = self._successful_recovery_for(task.id)
        if successful_recovery is not None:
            message = (
                f"Already recovered successfully by task {successful_recovery.id}; "
                "no duplicate recovery is needed. Run pc-agent task show "
                f"{successful_recovery.id}."
            )
            if diagnosis is None:
                diagnosis = FailureDiagnosis(
                    FailureCategory.UNSUPPORTED,
                    RecoveryAction.NONE,
                    False,
                    message,
                    task.error or "Task already has a successful recovery clone.",
                )
            else:
                diagnosis = FailureDiagnosis(
                    diagnosis.category,
                    RecoveryAction.NONE,
                    False,
                    message,
                    diagnosis.original_error,
                )

        return TaskDiagnosis(
            task=task,
            plan=plan,
            diagnosis=diagnosis,
            latest_invocation=latest,
            attempts=tuple(self._recovery.list_attempts(task_id=task.id, limit=100)),
            links=tuple(self._recovery.list_links_for(task.id)),
            stale=stale,
            age_seconds=task_age_seconds(task),
            plan_error=plan_error,
        )

    def list_stale(self, *, stale_after_minutes: int | None = None) -> tuple[Task, ...]:
        threshold = self._stale_threshold(stale_after_minutes)
        results: list[Task] = []
        for status in ACTIVE_STATUSES:
            results.extend(self._tasks.list(limit=500, status=status))
        stale = [
            task for task in results if is_stale_task(task, stale_after_minutes=threshold)
        ]
        return tuple(sorted(stale, key=lambda task: task.updated_at or task.created_at))

    def scan(self, *, stale_after_minutes: int | None = None) -> RecoveryScan:
        threshold = self._stale_threshold(stale_after_minutes)
        return RecoveryScan(
            stale_tasks=self.list_stale(stale_after_minutes=threshold),
            orphan_temp_pdfs=find_orphan_temp_pdfs(
                self._settings.workspace_dir, stale_after_minutes=threshold
            ),
            stale_after_minutes=threshold,
        )

    def recover(
        self,
        task_id: str,
        *,
        approval_callback: ApprovalCallback | None = None,
        options: PlannerOptions | None = None,
        use_ai: bool = False,
        force: bool = False,
        max_recovery_attempts: int | None = None,
    ) -> RecoveryOutcome:
        source = self._require_task(task_id)
        if source.status in {TaskStatus.SUCCEEDED, TaskStatus.CANCELLED}:
            raise RecoveryError(
                f"Task {source.id} is {source.status.value}; create a new task instead of recovering it."
            )
        if self._chain_depth(source.id) >= MAX_MANUAL_RECOVERY_CHAIN:
            raise RecoveryError(
                "Manual recovery chain limit reached (3). Create a fresh task after fixing the cause."
            )
        successful_recovery = self._successful_recovery_for(source.id)
        if successful_recovery is not None:
            raise RecoveryError(
                f"Task {source.id} was already recovered successfully by task "
                f"{successful_recovery.id}; no duplicate recovery clone was "
                "created. Run pc-agent task show "
                f"{successful_recovery.id}, or create a fresh task if you "
                "intentionally need to run the work again."
            )

        if source.status in _RESUMABLE_STATUSES:
            _stored, parsed_plan, _plan_error = self._stored_plan(source.id)
            resume = parsed_plan is not None and source.status is not TaskStatus.NEEDS_INPUT
            report = self._orchestrator.execute(
                source.id,
                options=options,
                approval_callback=approval_callback,
                use_ai=use_ai,
                max_recovery_attempts=max_recovery_attempts,
                resume_stored_plan=resume,
            )
            return RecoveryOutcome(
                source_task=source,
                recovery_task=report.task,
                cloned=False,
                report=report,
                message="Existing task was resumed; no clone was needed.",
            )

        if (
            source.status in ACTIVE_STATUSES
            and not force
            and not is_stale_task(
                source,
                stale_after_minutes=self._settings.recovery_stale_after_minutes,
            )
        ):
            age = task_age_seconds(source)
            raise RecoveryError(
                "Task may still be active: it updated "
                f"{age:.0f}s ago and the stale threshold is "
                f"{self._settings.recovery_stale_after_minutes} minute(s). "
                "Wait, or use --force only if you are sure no pc-agent process is running it."
            )

        if (
            source.status not in ACTIVE_STATUSES
            and source.status is not TaskStatus.FAILED
        ):
            raise RecoveryError(f"Task cannot be recovered from status {source.status.value}.")

        stored_plan, parsed_plan, _plan_error = self._stored_plan(source.id)
        if parsed_plan is None:
            stored_plan = None
        recovery_task = self._tasks.create(Task(instruction=source.instruction))
        reason = (
            "Interrupted task recovered into a linked task."
            if source.status in ACTIVE_STATUSES
            else "Failed task manually recovered into a linked task."
        )
        self._recovery.create_link(
            source_task_id=source.id,
            recovery_task_id=recovery_task.id,
            reason=reason,
        )
        if stored_plan is not None:
            self._execution.save_plan(
                recovery_task.id,
                stored_plan,
                str(stored_plan.get("planner") or "stored"),
            )

        if source.status in ACTIVE_STATUSES:
            source = self._tasks.update_status(
                source.id,
                TaskStatus.FAILED,
                message=f"Interrupted; recovery task created: {recovery_task.id}",
                error="Interrupted before completion; a linked recovery task was created.",
            )

        try:
            report = self._orchestrator.execute(
                recovery_task.id,
                options=options,
                approval_callback=approval_callback,
                use_ai=use_ai,
                max_recovery_attempts=max_recovery_attempts,
                resume_stored_plan=stored_plan is not None,
                source_task_id=source.id,
            )
        except PcAgentError as exc:
            raise RecoveryError(
                f"Recovery task {recovery_task.id} was created from {source.id}, but execution could not start: {exc}"
            ) from exc

        return RecoveryOutcome(
            source_task=source,
            recovery_task=report.task,
            cloned=True,
            report=report,
            message=reason,
        )

    def _stale_threshold(self, stale_after_minutes: int | None) -> int:
        threshold = (
            self._settings.recovery_stale_after_minutes
            if stale_after_minutes is None
            else stale_after_minutes
        )
        if threshold < 0 or threshold > 10_080:
            raise RecoveryError(
                "Stale threshold must be between 0 and 10080 minutes."
            )
        return threshold

    def _require_task(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise RecoveryError(f"Task not found: {task_id}")
        return task

    def _successful_recovery_for(self, source_task_id: str) -> Task | None:
        for link in self._recovery.list_links_for(source_task_id):
            if link.source_task_id != source_task_id:
                continue
            candidate = self._tasks.get(link.recovery_task_id)
            if candidate is not None and candidate.status is TaskStatus.SUCCEEDED:
                return candidate
        return None

    def _chain_depth(self, task_id: str) -> int:
        depth = 0
        current = task_id
        seen: set[str] = set()
        while current not in seen:
            seen.add(current)
            source = self._recovery.source_for(current)
            if source is None:
                break
            depth += 1
            current = source
            if depth > MAX_MANUAL_RECOVERY_CHAIN:
                break
        return depth


def _relative_posix(root: Path, target: Path) -> str:
    resolved_root = root.expanduser().resolve()
    resolved_target = target.expanduser().resolve()
    try:
        return resolved_target.relative_to(resolved_root).as_posix()
    except ValueError:
        return str(resolved_target)
