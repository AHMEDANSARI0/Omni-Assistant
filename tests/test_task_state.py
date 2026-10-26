from __future__ import annotations

import pytest

from pc_agent.domain.task import Task, TaskStatus, can_transition
from pc_agent.errors import InvalidTransitionError


def test_full_happy_path_must_pass_through_verifying() -> None:
    task = Task(instruction="Merge the PDFs")
    task.transition(TaskStatus.PLANNING)
    task.transition(TaskStatus.RUNNING)
    task.transition(TaskStatus.VERIFYING)
    task.transition(TaskStatus.SUCCEEDED)

    assert task.is_terminal
    assert task.completed_at is not None
    assert task.error is None


def test_running_cannot_skip_verification() -> None:
    task = Task(instruction="Merge the PDFs", status=TaskStatus.RUNNING)

    assert not can_transition(TaskStatus.RUNNING, TaskStatus.SUCCEEDED)
    with pytest.raises(InvalidTransitionError):
        task.transition(TaskStatus.SUCCEEDED)


def test_terminal_task_cannot_move_again() -> None:
    task = Task(instruction="Done task", status=TaskStatus.CANCELLED)

    with pytest.raises(InvalidTransitionError):
        task.transition(TaskStatus.PLANNING)


def test_same_status_transition_is_invalid() -> None:
    task = Task(instruction="Task")

    with pytest.raises(InvalidTransitionError):
        task.transition(TaskStatus.QUEUED)


def test_failure_records_error_and_completion_time() -> None:
    task = Task(instruction="Task", status=TaskStatus.RUNNING)
    task.transition(TaskStatus.FAILED, error="boom")

    assert task.error == "boom"
    assert task.completed_at is not None


def test_empty_instruction_is_rejected() -> None:
    with pytest.raises(ValueError):
        Task(instruction="   ")
