"""Task state machine.

Transitions are intentionally explicit. In particular, a RUNNING task cannot
jump straight to SUCCEEDED: it must pass through VERIFYING so "success" always
means the output was checked, not merely produced.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..errors import InvalidTransitionError


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class TaskStatus(str, enum.Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    RUNNING = "running"
    VERIFYING = "verifying"
    RETRYING = "retrying"
    NEEDS_INPUT = "needs_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

_VALID_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.PLANNING, TaskStatus.CANCELLED}),
    TaskStatus.PLANNING: frozenset(
        {
            TaskStatus.WAITING_FOR_APPROVAL,
            TaskStatus.RUNNING,
            TaskStatus.NEEDS_INPUT,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_FOR_APPROVAL: frozenset(
        {
            TaskStatus.PLANNING,
            TaskStatus.RUNNING,
            TaskStatus.NEEDS_INPUT,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.PLANNING,  # resume of an interrupted task re-enters planning
            TaskStatus.WAITING_FOR_APPROVAL,
            TaskStatus.VERIFYING,
            TaskStatus.RETRYING,
            TaskStatus.NEEDS_INPUT,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.VERIFYING: frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.PLANNING,  # resume of an interrupted task re-enters planning
            TaskStatus.RETRYING,
            TaskStatus.NEEDS_INPUT,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RETRYING: frozenset(
        {
            TaskStatus.PLANNING,
            TaskStatus.RUNNING,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.NEEDS_INPUT: frozenset(
        {TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.CANCELLED}
    ),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


def can_transition(current: TaskStatus, new: TaskStatus) -> bool:
    return new in _VALID_TRANSITIONS[current]


@dataclass(slots=True)
class Task:
    instruction: str
    status: TaskStatus = TaskStatus.QUEUED
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        self.instruction = self.instruction.strip()
        if not self.instruction:
            raise ValueError("Task instruction must not be empty.")
        if not self.id.strip():
            raise ValueError("Task id must not be empty.")
        if self.updated_at is None:
            self.updated_at = self.created_at

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def transition(
        self,
        new_status: TaskStatus,
        *,
        error: str | None = None,
        at: datetime | None = None,
    ) -> None:
        if not can_transition(self.status, new_status):
            raise InvalidTransitionError(
                f"Task cannot move from {self.status.value} to {new_status.value}."
            )
        now = at if at is not None else utc_now()
        self.status = new_status
        self.updated_at = now
        if self.is_terminal:
            self.completed_at = now
        else:
            self.completed_at = None
        if error is not None:
            self.error = error
        elif new_status is not TaskStatus.FAILED:
            self.error = None
