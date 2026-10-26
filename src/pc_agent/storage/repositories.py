"""Repositories - the only code allowed to read/write task tables directly."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..domain.task import Task, TaskStatus, parse_iso_datetime, utc_now
from ..errors import StorageError
from ..tools.registry import ToolResult
from .db import Database


def _format(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(frozen=True, slots=True)
class TaskEvent:
    id: int
    task_id: str
    event_type: str
    old_status: TaskStatus | None
    new_status: TaskStatus | None
    message: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    id: int
    task_id: str
    action: str
    decision: str
    scope: Mapping[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ToolInvocationRecord:
    id: int
    task_id: str
    tool_id: str
    tool_version: str
    success: bool
    arguments: Mapping[str, Any]
    output: Mapping[str, Any] | None
    error: str | None
    elapsed_ms: float
    created_at: datetime


class TaskRepository:
    def __init__(self, Database: Database) -> None:
        self._db = Database

    def create(self, task: Task) -> Task:
        try:
            with self._db.connect() as connection:
                connection.execute(
                    "INSERT INTO tasks (id, instruction, status, created_at, updated_at,"
                    " completed_at, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        task.id,
                        task.instruction,
                        task.status.value,
                        _format(task.created_at),
                        _format(task.updated_at),
                        _format(task.completed_at),
                        task.error,
                    ),
                )
                connection.execute(
                    "INSERT INTO task_events (task_id, event_type, old_status, new_status,"
                    " message, created_at) VALUES (?, 'created', NULL, ?, NULL, ?)",
                    (task.id, task.status.value, _format(task.created_at)),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"A task with id {task.id} already exists.") from exc
        return task

    def get(self, task_id: str) -> Task | None:
        with self._db.connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_task(row) if row is not None else None

    def list(
        self, *, limit: int = 50, offset: int = 0, status: TaskStatus | None = None
    ) -> list[Task]:
        if limit < 1 or limit > 500:
            raise StorageError("Task list limit must be between 1 and 500.")
        if offset < 0:
            raise StorageError("Task list offset must not be negative.")
        query = "SELECT * FROM tasks"
        params: list[object] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status.value)
        query += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._db.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_task(row) for row in rows]

    def update_status(
        self,
        task_id: str,
        new_status: TaskStatus,
        *,
        message: str | None = None,
        error: str | None = None,
    ) -> Task:
        """Move a task to a new status and record the event in one transaction."""
        with self._db.connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise StorageError(f"Task {task_id} was not found.")
            task = self._row_to_task(row)
            old_status = task.status
            # Raises InvalidTransitionError for illegal moves; the context manager
            # rolls the transaction back, so nothing is written in that case.
            task.transition(new_status, error=error)
            connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ?, completed_at = ?, error = ?"
                " WHERE id = ?",
                (
                    task.status.value,
                    _format(task.updated_at),
                    _format(task.completed_at),
                    task.error,
                    task.id,
                ),
            )
            connection.execute(
                "INSERT INTO task_events (task_id, event_type, old_status, new_status,"
                " message, created_at) VALUES (?, 'status_changed', ?, ?, ?, ?)",
                (task.id, old_status.value, task.status.value, message, _format(utc_now())),
            )
            return task

    def record_event(
        self, task_id: str, event_type: str, message: str | None = None
    ) -> None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise StorageError(f"Task {task_id} was not found.")
            status = str(row["status"])
            connection.execute(
                "INSERT INTO task_events (task_id, event_type, old_status, new_status,"
                " message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, event_type, status, status, message, _format(utc_now())),
            )

    def list_events(self, task_id: str) -> list[TaskEvent]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        return [
            TaskEvent(
                id=int(row["id"]),
                task_id=str(row["task_id"]),
                event_type=str(row["event_type"]),
                old_status=TaskStatus(row["old_status"]) if row["old_status"] else None,
                new_status=TaskStatus(row["new_status"]) if row["new_status"] else None,
                message=row["message"],
                created_at=parse_iso_datetime(str(row["created_at"])),
            )
            for row in rows
        ]

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(
            instruction=str(row["instruction"]),
            status=TaskStatus(str(row["status"])),
            id=str(row["id"]),
            created_at=parse_iso_datetime(str(row["created_at"])),
            updated_at=parse_iso_datetime(str(row["updated_at"])),
            completed_at=(
                parse_iso_datetime(str(row["completed_at"])) if row["completed_at"] else None
            ),
            error=row["error"],
        )


class PreferenceRepository:
    """Approved user preferences only - never secrets or credentials."""

    def __init__(self, Database: Database) -> None:
        self._db = Database

    def get(self, key: str, default: str | None = None) -> str | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT value FROM user_preferences WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row is not None else default

    def set(self, key: str, value: str) -> None:
        if not key.strip():
            raise StorageError("Preference key must not be empty.")
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO user_preferences (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (key, value, _format(utc_now())),
            )

    def all(self) -> dict[str, str]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT key, value FROM user_preferences ORDER BY key"
            ).fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}


class ExecutionRepository:
    """Auditable plans, approval decisions and orchestrated tool invocations."""

    def __init__(self, Database: Database) -> None:
        self._db = Database

    def save_plan(self, task_id: str, plan: Mapping[str, Any], planner: str) -> None:
        now = _format(utc_now())
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO task_plans (task_id, plan_json, planner, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(task_id) DO UPDATE SET plan_json = excluded.plan_json,"
                " planner = excluded.planner, updated_at = excluded.updated_at",
                (task_id, _json(plan), planner, now, now),
            )

    def get_plan(self, task_id: str) -> dict[str, Any] | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT plan_json FROM task_plans WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        parsed = json.loads(str(row["plan_json"]))
        return parsed if isinstance(parsed, dict) else None

    def record_approval(
        self,
        task_id: str,
        action: str,
        *,
        approved: bool,
        scope: Mapping[str, Any],
    ) -> None:
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO approval_records (task_id, action, decision, scope_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    task_id,
                    action,
                    "approved" if approved else "denied",
                    _json(scope),
                    _format(utc_now()),
                ),
            )

    def list_approvals(self, task_id: str) -> list[ApprovalRecord]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM approval_records WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        return [
            ApprovalRecord(
                id=int(row["id"]),
                task_id=str(row["task_id"]),
                action=str(row["action"]),
                decision=str(row["decision"]),
                scope=_json_object(str(row["scope_json"])),
                created_at=parse_iso_datetime(str(row["created_at"])),
            )
            for row in rows
        ]

    def record_tool_invocation(
        self, task_id: str, arguments: Mapping[str, Any], result: ToolResult
    ) -> None:
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO tool_invocations (task_id, tool_id, tool_version, success,"
                " arguments_json, output_json, error, elapsed_ms, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    result.tool_id,
                    result.version,
                    1 if result.success else 0,
                    _json(arguments),
                    _json(result.output) if result.output is not None else None,
                    result.error,
                    result.elapsed_ms,
                    _format(utc_now()),
                ),
            )

    def list_tool_invocations(self, task_id: str) -> list[ToolInvocationRecord]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_invocations WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        return [
            ToolInvocationRecord(
                id=int(row["id"]),
                task_id=str(row["task_id"]),
                tool_id=str(row["tool_id"]),
                tool_version=str(row["tool_version"]),
                success=bool(row["success"]),
                arguments=_json_object(str(row["arguments_json"])),
                output=(
                    _json_object(str(row["output_json"])) if row["output_json"] else None
                ),
                error=row["error"],
                elapsed_ms=float(row["elapsed_ms"]),
                created_at=parse_iso_datetime(str(row["created_at"])),
            )
            for row in rows
        ]


def _json(value: Any) -> str:
    return json.dumps(_redact_json(value), sort_keys=True, default=str)


def _json_object(raw: str) -> dict[str, Any]:
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def _redact_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if any(word in lowered for word in ("api_key", "apikey", "token", "password", "secret")):
                result[key_text] = "<redacted>"
            elif lowered == "content" and isinstance(item, str):
                result[key_text] = f"<redacted: {len(item)} chars>"
            else:
                result[key_text] = _redact_json(item)
        return result
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_json(item) for item in value]
    return value
