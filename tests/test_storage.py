from __future__ import annotations

from pathlib import Path

import pytest

from pc_agent.domain.task import Task, TaskStatus
from pc_agent.errors import InvalidTransitionError, StorageError
from pc_agent.storage.db import Database
from pc_agent.storage.repositories import (
    ExecutionRepository,
    PreferenceRepository,
    TaskRepository,
)
from pc_agent.tools.registry import ToolResult


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.initialize()
    return database


def test_migrations_are_idempotent(database: Database) -> None:
    assert database.schema_version() == 2
    assert database.initialize() == 2
    assert database.schema_version() == 2


def test_task_round_trip(database: Database) -> None:
    repository = TaskRepository(database)
    created = repository.create(Task(instruction="Merge PDFs"))

    loaded = repository.get(created.id)

    assert loaded is not None
    assert loaded.instruction == "Merge PDFs"
    assert loaded.status is TaskStatus.QUEUED
    assert [task.id for task in repository.list()] == [created.id]


def test_status_update_records_event(database: Database) -> None:
    repository = TaskRepository(database)
    task = repository.create(Task(instruction="Merge PDFs"))

    updated = repository.update_status(task.id, TaskStatus.PLANNING, message="planning started")
    events = repository.list_events(task.id)

    assert updated.status is TaskStatus.PLANNING
    assert [event.event_type for event in events] == ["created", "status_changed"]
    assert events[1].old_status is TaskStatus.QUEUED
    assert events[1].new_status is TaskStatus.PLANNING


def test_invalid_status_update_writes_nothing(database: Database) -> None:
    repository = TaskRepository(database)
    task = repository.create(Task(instruction="Merge PDFs"))

    with pytest.raises(InvalidTransitionError):
        repository.update_status(task.id, TaskStatus.SUCCEEDED)

    loaded = repository.get(task.id)
    assert loaded is not None
    assert loaded.status is TaskStatus.QUEUED
    assert len(repository.list_events(task.id)) == 1


def test_updating_missing_task_raises_storage_error(database: Database) -> None:
    repository = TaskRepository(database)

    with pytest.raises(StorageError):
        repository.update_status("missing", TaskStatus.PLANNING)


def test_preferences_round_trip(database: Database) -> None:
    repository = PreferenceRepository(database)

    assert repository.get("language") is None
    repository.set("language", "Roman Urdu")
    repository.set("language", "Urdu")

    assert repository.all() == {"language": "Urdu"}


def test_execution_records_round_trip_and_redact_secrets(database: Database) -> None:
    tasks = TaskRepository(database)
    execution = ExecutionRepository(database)
    task = tasks.create(Task(instruction="Merge PDFs"))

    execution.save_plan(
        task.id,
        {"tool_id": "pdf.merge", "api_key": "must-not-be-stored"},
        "test",
    )
    execution.record_approval(
        task.id,
        "read_external_inputs",
        approved=True,
        scope={"read_roots": ["C:/demo"]},
    )
    execution.record_tool_invocation(
        task.id,
        {"path": "merged.pdf", "content": "private"},
        ToolResult("pdf.verify", "1.0.0", True, {"valid": True}, None, 4.5),
    )

    plan = execution.get_plan(task.id)
    assert plan is not None and plan["api_key"] == "<redacted>"
    assert execution.list_approvals(task.id)[0].decision == "approved"
    invocation = execution.list_tool_invocations(task.id)[0]
    assert invocation.success is True
    assert invocation.arguments["content"] == "<redacted: 7 chars>"
