"""Repositories - the only code allowed to read/write task tables directly."""

from __future__ import annotations

import hashlib
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


@dataclass(frozen=True, slots=True)
class TaskCheckpointRecord:
    task_id: str
    step_index: int
    step_key: str
    tool_id: str
    tool_version: str
    plan_fingerprint: str
    output: Mapping[str, Any] | None
    artifacts: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskVerificationRecord:
    id: int
    task_id: str
    passed: bool
    report: Mapping[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryItemRecord:
    id: int
    kind: str
    key: str
    title: str
    content: Mapping[str, Any]
    source_task_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RecoveryAttemptRecord:
    id: int
    task_id: str
    source_task_id: str | None
    attempt_number: int
    stage: str
    status: str
    category: str | None
    action: str
    retryable: bool
    original_error: str | None
    message: str | None
    plan_before: Mapping[str, Any] | None
    plan_after: Mapping[str, Any] | None
    created_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class RecoveryLinkRecord:
    id: int
    source_task_id: str
    recovery_task_id: str
    reason: str | None
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

    def count(self, *, status: TaskStatus | None = None) -> int:
        query = "SELECT COUNT(*) AS n FROM tasks"
        params: list[object] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status.value)
        with self._db.connect() as connection:
            row = connection.execute(query, params).fetchone()
        return int(row["n"])

    def delete(self, task_id: str) -> bool:
        """Delete one task and its cascaded records (caller checks status)."""

        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return False
            references = connection.execute(
                "SELECT COUNT(*) AS n FROM recovery_attempts WHERE source_task_id = ?",
                (task_id,),
            ).fetchone()
            if int(references["n"]) > 0:
                raise StorageError(
                    "Task is referenced as the source of "
                    f"{int(references['n'])} recovery attempt(s); keep it for audit "
                    "or delete the linked recovery tasks first."
                )
            cursor = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            return cursor.rowcount > 0

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

    def delete(self, key: str) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM user_preferences WHERE key = ?", (key,)
            )
            return cursor.rowcount > 0

    def clear(self) -> int:
        with self._db.connect() as connection:
            cursor = connection.execute("DELETE FROM user_preferences")
            return int(cursor.rowcount)


class MemoryRepository:
    """Phase 7 local memory: user notes and recorded workflow evidence.

    Workflow items are evidence that a verified task shape succeeded before;
    they are never treated as proof that it will succeed again without
    re-verification. Secrets must never be stored here.
    """

    _KINDS = frozenset({"note", "workflow"})

    def __init__(self, Database: Database) -> None:
        self._db = Database

    def upsert(
        self,
        *,
        kind: str,
        key: str,
        title: str,
        content: Mapping[str, Any],
        source_task_id: str | None = None,
    ) -> MemoryItemRecord:
        if kind not in self._KINDS:
            raise StorageError(f"Unknown memory kind: {kind}")
        clean_key = key.strip()
        if not clean_key or len(clean_key) > 120:
            raise StorageError("Memory key must be 1-120 characters.")
        clean_title = title.strip() or clean_key
        if len(clean_title) > 200:
            raise StorageError("Memory title must be at most 200 characters.")
        now = _format(utc_now())
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO memory_items (kind, key, title, content_json,"
                " source_task_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(kind, key) DO UPDATE SET title = excluded.title,"
                " content_json = excluded.content_json,"
                " source_task_id = excluded.source_task_id,"
                " updated_at = excluded.updated_at",
                (kind, clean_key, clean_title, _json(dict(content)), source_task_id, now, now),
            )
        record = self.get(kind, clean_key)
        if record is None:  # pragma: no cover - row was just written
            raise StorageError("Memory item could not be reloaded after saving.")
        return record

    def get(self, kind: str, key: str) -> MemoryItemRecord | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM memory_items WHERE kind = ? AND key = ?", (kind, key)
            ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def list(self, *, kind: str | None = None, limit: int = 100) -> list[MemoryItemRecord]:
        query = "SELECT * FROM memory_items"
        params: list[object] = []
        if kind is not None:
            query += " WHERE kind = ?"
            params.append(kind)
        query += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self._db.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._record_from_row(row) for row in rows]

    def delete(self, kind: str, key: str) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM memory_items WHERE kind = ? AND key = ?", (kind, key)
            )
            return cursor.rowcount > 0

    def clear(self) -> int:
        with self._db.connect() as connection:
            cursor = connection.execute("DELETE FROM memory_items")
            return int(cursor.rowcount)

    def counts(self) -> dict[str, int]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT kind, COUNT(*) AS n FROM memory_items GROUP BY kind"
            ).fetchall()
        result = {"note": 0, "workflow": 0}
        for row in rows:
            result[str(row["kind"])] = int(row["n"])
        return result

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> MemoryItemRecord:
        return MemoryItemRecord(
            id=int(row["id"]),
            kind=str(row["kind"]),
            key=str(row["key"]),
            title=str(row["title"]),
            content=_json_object(str(row["content_json"])),
            source_task_id=str(row["source_task_id"]) if row["source_task_id"] else None,
            created_at=parse_iso_datetime(str(row["created_at"])),
            updated_at=parse_iso_datetime(str(row["updated_at"])),
        )


@dataclass(frozen=True, slots=True)
class LicenseEventRecord:
    id: int
    event_type: str
    license_id: str | None
    detail: Mapping[str, Any]
    created_at: datetime


class LicenseRepository:
    """Phase 6 license trial/clock state and activation audit events.

    Raw license tokens are never stored here; events keep only the license
    id and a SHA-256 fingerprint of the token for traceability.
    """

    def __init__(self, Database: Database) -> None:
        self._db = Database

    def get_state(self, key: str) -> str | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT value FROM license_state WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def set_state(self, key: str, value: str) -> None:
        if not key.strip():
            raise StorageError("License state key must not be empty.")
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO license_state (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (key, value, _format(utc_now())),
            )

    def record_event(
        self,
        event_type: str,
        *,
        license_id: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> int:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO license_events (event_type, license_id, detail_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    event_type,
                    license_id,
                    _plain_json(dict(detail or {})),
                    _format(utc_now()),
                ),
            )
            return int(cursor.lastrowid)

    def list_events(self, *, limit: int = 50) -> list[LicenseEventRecord]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT id, event_type, license_id, detail_json, created_at"
                " FROM license_events ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [
            LicenseEventRecord(
                id=int(row["id"]),
                event_type=str(row["event_type"]),
                license_id=str(row["license_id"]) if row["license_id"] else None,
                detail=_parsed_json(row["detail_json"], {}),
                created_at=parse_iso_datetime(str(row["created_at"])),
            )
            for row in rows
        ]


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


    # ------------------------------------------------------------------
    # Phase 7: step checkpoints, cooperative cancellation, verifications
    # ------------------------------------------------------------------
    def save_checkpoint(
        self,
        task_id: str,
        *,
        step_index: int,
        step_key: str,
        result: ToolResult,
        plan_fingerprint: str,
        artifacts: tuple[str, ...] = (),
    ) -> None:
        now = _format(utc_now())
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO task_checkpoints (task_id, step_index, step_key, tool_id,"
                " tool_version, plan_fingerprint, output_json, artifacts_json,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(task_id, step_index) DO UPDATE SET"
                " step_key = excluded.step_key, tool_id = excluded.tool_id,"
                " tool_version = excluded.tool_version,"
                " plan_fingerprint = excluded.plan_fingerprint,"
                " output_json = excluded.output_json,"
                " artifacts_json = excluded.artifacts_json,"
                " updated_at = excluded.updated_at",
                (
                    task_id,
                    step_index,
                    step_key,
                    result.tool_id,
                    result.version,
                    plan_fingerprint,
                    _json(result.output) if result.output is not None else None,
                    _plain_json(list(artifacts)),
                    now,
                    now,
                ),
            )

    def get_checkpoint(
        self, task_id: str, step_index: int
    ) -> TaskCheckpointRecord | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_checkpoints WHERE task_id = ? AND step_index = ?",
                (task_id, step_index),
            ).fetchone()
        return self._checkpoint_from_row(row) if row is not None else None

    def list_checkpoints(self, task_id: str) -> list[TaskCheckpointRecord]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_checkpoints WHERE task_id = ? ORDER BY step_index",
                (task_id,),
            ).fetchall()
        return [self._checkpoint_from_row(row) for row in rows]

    def delete_checkpoint(self, task_id: str, step_index: int) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM task_checkpoints WHERE task_id = ? AND step_index = ?",
                (task_id, step_index),
            )
            return cursor.rowcount > 0

    def count_checkpoints(self) -> int:
        with self._db.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM task_checkpoints").fetchone()
        return int(row["n"])

    def request_cancellation(self, task_id: str, *, reason: str | None = None) -> None:
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO task_cancellations (task_id, reason, requested_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(task_id) DO UPDATE SET reason = excluded.reason,"
                " requested_at = excluded.requested_at",
                (task_id, reason, _format(utc_now())),
            )

    def cancellation_requested(self, task_id: str) -> bool:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT task_id FROM task_cancellations WHERE task_id = ?", (task_id,)
            ).fetchone()
        return row is not None

    def clear_cancellation(self, task_id: str) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM task_cancellations WHERE task_id = ?", (task_id,)
            )
            return cursor.rowcount > 0

    def save_verification(self, task_id: str, report: Mapping[str, Any]) -> int:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO task_verifications (task_id, passed, report_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    1 if report.get("passed") else 0,
                    _plain_json(dict(report)),
                    _format(utc_now()),
                ),
            )
            return int(cursor.lastrowid)

    def get_latest_verification(self, task_id: str) -> TaskVerificationRecord | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_verifications WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return TaskVerificationRecord(
            id=int(row["id"]),
            task_id=str(row["task_id"]),
            passed=bool(row["passed"]),
            report=_json_object(str(row["report_json"])),
            created_at=parse_iso_datetime(str(row["created_at"])),
        )

    def count_verifications(self) -> int:
        with self._db.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM task_verifications").fetchone()
        return int(row["n"])

    @staticmethod
    def _checkpoint_from_row(row: sqlite3.Row) -> TaskCheckpointRecord:
        artifacts = _parsed_json(row["artifacts_json"], [])
        return TaskCheckpointRecord(
            task_id=str(row["task_id"]),
            step_index=int(row["step_index"]),
            step_key=str(row["step_key"]),
            tool_id=str(row["tool_id"]),
            tool_version=str(row["tool_version"]),
            plan_fingerprint=str(row["plan_fingerprint"]),
            output=(
                _json_object(str(row["output_json"])) if row["output_json"] else None
            ),
            artifacts=tuple(str(item) for item in artifacts)
            if isinstance(artifacts, list)
            else (),
            created_at=parse_iso_datetime(str(row["created_at"])),
            updated_at=parse_iso_datetime(str(row["updated_at"])),
        )


class RecoveryRepository:
    """Auditable checkpoints for bounded self-healing and task recovery."""

    _STATUSES = frozenset({"started", "succeeded", "failed", "retrying", "skipped"})

    def __init__(self, Database: Database) -> None:
        self._db = Database

    def start_attempt(
        self,
        task_id: str,
        *,
        attempt_number: int,
        stage: str,
        action: str,
        source_task_id: str | None = None,
        plan_before: Mapping[str, Any] | None = None,
        message: str | None = None,
    ) -> RecoveryAttemptRecord:
        if attempt_number < 1:
            raise StorageError("Recovery attempt_number must be at least 1.")
        self._db.initialize()
        now = _format(utc_now())
        with self._db.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO recovery_attempts (
                    task_id, source_task_id, attempt_number, stage, status,
                    category, action, retryable, original_error, message,
                    plan_before_json, plan_after_json, created_at, completed_at
                ) VALUES (?, ?, ?, ?, 'started', NULL, ?, 0, NULL, ?, ?, NULL, ?, NULL)
                """,
                (
                    task_id,
                    source_task_id,
                    attempt_number,
                    stage,
                    action,
                    message,
                    _json(plan_before) if plan_before is not None else None,
                    now,
                ),
            )
            attempt_id = int(cursor.lastrowid)
        record = self.get_attempt(attempt_id)
        if record is None:  # pragma: no cover - insert/read guard
            raise StorageError("Recovery attempt disappeared after creation.")
        return record

    def finish_attempt(
        self,
        attempt_id: int,
        *,
        status: str,
        stage: str | None = None,
        category: str | None = None,
        action: str | None = None,
        retryable: bool = False,
        original_error: str | None = None,
        message: str | None = None,
        plan_after: Mapping[str, Any] | None = None,
    ) -> RecoveryAttemptRecord:
        if status not in self._STATUSES or status == "started":
            raise StorageError(f"Invalid final recovery attempt status: {status}")
        now = _format(utc_now())
        with self._db.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE recovery_attempts
                SET status = ?, stage = COALESCE(?, stage), category = ?,
                    action = COALESCE(?, action), retryable = ?,
                    original_error = ?, message = ?, plan_after_json = ?,
                    completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    stage,
                    category,
                    action,
                    int(retryable),
                    original_error,
                    message,
                    _json(plan_after) if plan_after is not None else None,
                    now,
                    attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StorageError(f"Recovery attempt {attempt_id} was not found.")
        record = self.get_attempt(attempt_id)
        if record is None:  # pragma: no cover
            raise StorageError("Recovery attempt disappeared after update.")
        return record

    def get_attempt(self, attempt_id: int) -> RecoveryAttemptRecord | None:
        self._db.initialize()
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recovery_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
        return self._attempt_from_row(row) if row is not None else None

    def list_attempts(
        self, *, task_id: str | None = None, limit: int = 100
    ) -> list[RecoveryAttemptRecord]:
        if limit < 1 or limit > 1000:
            raise StorageError("Recovery attempt list limit must be between 1 and 1000.")
        self._db.initialize()
        with self._db.connect() as connection:
            if task_id is None:
                rows = connection.execute(
                    """
                    SELECT * FROM recovery_attempts
                    ORDER BY id DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                rows = list(reversed(rows))
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM recovery_attempts WHERE task_id = ?
                    ORDER BY attempt_number, id LIMIT ?
                    """,
                    (task_id, limit),
                ).fetchall()
        return [self._attempt_from_row(row) for row in rows]

    def count_attempts(self) -> int:
        self._db.initialize()
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM recovery_attempts"
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def create_link(
        self, *, source_task_id: str, recovery_task_id: str, reason: str | None = None
    ) -> RecoveryLinkRecord:
        if source_task_id == recovery_task_id:
            raise StorageError("A task cannot be linked as its own recovery task.")
        self._db.initialize()
        now = _format(utc_now())
        try:
            with self._db.connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO task_recovery_links (
                        source_task_id, recovery_task_id, reason, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (source_task_id, recovery_task_id, reason, now),
                )
                link_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise StorageError("That recovery task link already exists.") from exc
        link = self.get_link(link_id)
        if link is None:  # pragma: no cover
            raise StorageError("Recovery link disappeared after creation.")
        return link

    def get_link(self, link_id: int) -> RecoveryLinkRecord | None:
        self._db.initialize()
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_recovery_links WHERE id = ?", (link_id,)
            ).fetchone()
        return self._link_from_row(row) if row is not None else None

    def list_links_for(self, task_id: str) -> list[RecoveryLinkRecord]:
        self._db.initialize()
        with self._db.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_recovery_links
                WHERE source_task_id = ? OR recovery_task_id = ?
                ORDER BY id
                """,
                (task_id, task_id),
            ).fetchall()
        return [self._link_from_row(row) for row in rows]

    def count_links_from(self, source_task_id: str) -> int:
        self._db.initialize()
        with self._db.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM task_recovery_links
                WHERE source_task_id = ?
                """,
                (source_task_id,),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def source_for(self, recovery_task_id: str) -> str | None:
        self._db.initialize()
        with self._db.connect() as connection:
            row = connection.execute(
                """
                SELECT source_task_id FROM task_recovery_links
                WHERE recovery_task_id = ? ORDER BY id DESC LIMIT 1
                """,
                (recovery_task_id,),
            ).fetchone()
        return str(row["source_task_id"]) if row is not None else None

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> RecoveryAttemptRecord:
        return RecoveryAttemptRecord(
            id=int(row["id"]),
            task_id=str(row["task_id"]),
            source_task_id=row["source_task_id"],
            attempt_number=int(row["attempt_number"]),
            stage=str(row["stage"]),
            status=str(row["status"]),
            category=row["category"],
            action=str(row["action"]),
            retryable=bool(row["retryable"]),
            original_error=row["original_error"],
            message=row["message"],
            plan_before=(
                _json_object(str(row["plan_before_json"]))
                if row["plan_before_json"]
                else None
            ),
            plan_after=(
                _json_object(str(row["plan_after_json"])) if row["plan_after_json"] else None
            ),
            created_at=parse_iso_datetime(str(row["created_at"])),
            completed_at=(
                parse_iso_datetime(str(row["completed_at"])) if row["completed_at"] else None
            ),
        )

    @staticmethod
    def _link_from_row(row: sqlite3.Row) -> RecoveryLinkRecord:
        return RecoveryLinkRecord(
            id=int(row["id"]),
            source_task_id=str(row["source_task_id"]),
            recovery_task_id=str(row["recovery_task_id"]),
            reason=row["reason"],
            created_at=parse_iso_datetime(str(row["created_at"])),
        )


def version_key(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0,)


@dataclass(frozen=True, slots=True)
class DynamicToolRecord:
    database_id: int
    candidate_key: str
    proposal: Any
    status: str
    enabled: bool
    provenance: str
    generator_model: str | None
    parent_candidate_id: int | None
    repair_attempt: int
    static_report: Mapping[str, Any]
    test_report: Mapping[str, Any] | None
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None

    @property
    def tool_id(self) -> str:
        return str(self.proposal.tool_id)

    @property
    def version(self) -> str:
        return str(self.proposal.version)


class DynamicToolRepository:
    """Versioned dynamic-tool candidates and immutable test evidence."""

    def __init__(self, Database: Database) -> None:
        self._db = Database

    @staticmethod
    def candidate_key_for(tool_id: str, version: str) -> str:
        digest = hashlib.sha256(f"{tool_id}@{version}".encode()).hexdigest()[:12]
        slug = "".join(char if char.isalnum() else "-" for char in tool_id).strip("-")
        slug = "-".join(part for part in slug.split("-") if part)
        return f"{slug}-{digest}"

    def create_candidate(
        self,
        proposal: Any,
        *,
        static_report: Mapping[str, Any],
        provenance: str,
        generator_model: str | None = None,
        parent_candidate_id: int | None = None,
        repair_attempt: int = 0,
    ) -> DynamicToolRecord:
        from ..errors import ToolBuilderError

        self._db.initialize()
        candidate_key = self.candidate_key_for(proposal.tool_id, proposal.version)
        now = _format(utc_now())
        try:
            with self._db.connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO dynamic_tool_candidates (
                        candidate_key, tool_id, version, name, description,
                        input_schema_json, output_schema_json, source_code,
                        source_sha256, tests_json, dependencies_json,
                        timeout_seconds, status, enabled, provenance,
                        generator_model, parent_candidate_id, repair_attempt,
                        static_report_json, test_report_json,
                        created_at, updated_at, approved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', 0,
                              ?, ?, ?, ?, ?, NULL, ?, ?, NULL)
                    """,
                    (
                        candidate_key,
                        proposal.tool_id,
                        proposal.version,
                        proposal.name,
                        proposal.description,
                        _plain_json(dict(proposal.input_schema)),
                        _plain_json(dict(proposal.output_schema)),
                        proposal.normalized_source,
                        proposal.source_sha256,
                        _plain_json([test.to_dict() for test in proposal.test_cases]),
                        _plain_json(list(proposal.dependencies)),
                        proposal.timeout_seconds,
                        provenance,
                        generator_model,
                        parent_candidate_id,
                        repair_attempt,
                        _plain_json(dict(static_report)),
                        now,
                        now,
                    ),
                )
                self._insert_event(
                    connection,
                    int(cursor.lastrowid),
                    "created",
                    f"Provenance: {provenance}",
                )
        except sqlite3.IntegrityError as exc:
            raise ToolBuilderError(
                f"Dynamic tool version already exists: {proposal.tool_id}@{proposal.version}"
            ) from exc
        record = self.get_candidate(candidate_key)
        if record is None:  # pragma: no cover - insert/read guard
            raise StorageError("Dynamic tool candidate disappeared after creation.")
        return record

    def get_candidate(self, reference: str) -> DynamicToolRecord | None:
        self._db.initialize()
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM dynamic_tool_candidates WHERE candidate_key = ?",
                (reference,),
            ).fetchone()
            if row is None and "@" in reference:
                tool_id, version = reference.rsplit("@", 1)
                row = connection.execute(
                    """
                    SELECT * FROM dynamic_tool_candidates
                    WHERE tool_id = ? AND version = ?
                    """,
                    (tool_id, version),
                ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def find_candidate_id(self, tool_id: str, version: str) -> int | None:
        self._db.initialize()
        with self._db.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM dynamic_tool_candidates
                WHERE tool_id = ? AND version = ?
                """,
                (tool_id, version),
            ).fetchone()
        return int(row["id"]) if row is not None else None

    def list_candidates(
        self,
        *,
        tool_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[DynamicToolRecord]:
        self._db.initialize()
        if limit < 1 or limit > 1000:
            raise StorageError("Dynamic tool list limit must be between 1 and 1000.")
        clauses: list[str] = []
        values: list[Any] = []
        if tool_id:
            clauses.append("tool_id = ?")
            values.append(tool_id)
        if status:
            clauses.append("status = ?")
            values.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._db.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM dynamic_tool_candidates{where}
                ORDER BY updated_at DESC, id DESC LIMIT ?
                """,
                (*values, limit),
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def approved_candidate(self, tool_id: str) -> DynamicToolRecord | None:
        records = self.list_candidates(tool_id=tool_id, status="approved", limit=100)
        return records[0] if records else None

    def approved_candidates(self, *, include_disabled: bool = True) -> list[DynamicToolRecord]:
        records = self.list_candidates(status="approved", limit=1000)
        return records if include_disabled else [record for record in records if record.enabled]

    def rollback_target(self, tool_id: str) -> DynamicToolRecord | None:
        records = [
            record
            for record in self.list_candidates(tool_id=tool_id, limit=1000)
            if record.status == "superseded"
            and record.test_report is not None
            and record.test_report.get("passed") is True
        ]
        return max(records, key=lambda record: version_key(record.version), default=None)

    def record_test_result(
        self,
        record: DynamicToolRecord,
        *,
        passed: bool,
        report: Mapping[str, Any],
    ) -> DynamicToolRecord:
        from ..errors import ToolBuilderError

        if record.status not in {"proposed", "tested", "test_failed"}:
            raise ToolBuilderError(f"Candidate in status '{record.status}' cannot be retested.")
        new_status = "tested" if passed else "test_failed"
        now = _format(utc_now())
        with self._db.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE dynamic_tool_candidates
                SET status = ?, test_report_json = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    new_status,
                    _plain_json(dict(report)),
                    now,
                    record.database_id,
                    record.status,
                ),
            )
            if cursor.rowcount != 1:
                raise ToolBuilderError("Candidate status changed before its test result was stored.")
            connection.execute(
                """
                INSERT INTO dynamic_tool_test_runs (
                    candidate_id, success, report_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (record.database_id, int(passed), _plain_json(dict(report)), now),
            )
            self._insert_event(
                connection,
                record.database_id,
                "tests_passed" if passed else "tests_failed",
                None,
            )
        updated = self.get_candidate(record.candidate_key)
        if updated is None:  # pragma: no cover
            raise StorageError("Dynamic tool candidate disappeared after testing.")
        return updated

    def approve_candidate(self, record: DynamicToolRecord) -> DynamicToolRecord:
        from ..errors import ToolBuilderError

        if record.status != "tested":
            raise ToolBuilderError("Only a candidate with current passing tests can be approved.")
        if record.static_report.get("passed") is not True:
            raise ToolBuilderError("Static validation must pass before approval.")
        if not record.test_report or record.test_report.get("passed") is not True:
            raise ToolBuilderError("Candidate tests must pass before approval.")

        now = _format(utc_now())
        with self._db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                """
                SELECT id FROM dynamic_tool_candidates
                WHERE tool_id = ? AND status = 'approved' AND id <> ?
                """,
                (record.tool_id, record.database_id),
            ).fetchall()
            for row in previous:
                connection.execute(
                    """
                    UPDATE dynamic_tool_candidates
                    SET status = 'superseded', enabled = 0, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                self._insert_event(
                    connection,
                    int(row["id"]),
                    "superseded",
                    f"Superseded by {record.candidate_key}",
                )
            cursor = connection.execute(
                """
                UPDATE dynamic_tool_candidates
                SET status = 'approved', enabled = 1, approved_at = ?, updated_at = ?
                WHERE id = ? AND status = 'tested'
                """,
                (now, now, record.database_id),
            )
            if cursor.rowcount != 1:
                raise ToolBuilderError("Candidate status changed before approval.")
            self._insert_event(connection, record.database_id, "approved", None)
        updated = self.get_candidate(record.candidate_key)
        if updated is None:  # pragma: no cover
            raise StorageError("Dynamic tool candidate disappeared after approval.")
        return updated

    def reject_candidate(
        self, record: DynamicToolRecord, *, reason: str | None = None
    ) -> DynamicToolRecord:
        from ..errors import ToolBuilderError

        if record.status == "approved":
            raise ToolBuilderError("Disable an approved tool instead of rejecting it.")
        if record.status == "superseded":
            raise ToolBuilderError(
                "A superseded version is retained as rollback/history and cannot be rejected."
            )
        if record.status == "rejected":
            return record
        now = _format(utc_now())
        with self._db.connect() as connection:
            connection.execute(
                """
                UPDATE dynamic_tool_candidates
                SET status = 'rejected', enabled = 0, updated_at = ?
                WHERE id = ?
                """,
                (now, record.database_id),
            )
            self._insert_event(connection, record.database_id, "rejected", reason)
        updated = self.get_candidate(record.candidate_key)
        if updated is None:  # pragma: no cover
            raise StorageError("Dynamic tool candidate disappeared after rejection.")
        return updated

    def set_enabled(self, tool_id: str, *, enabled: bool) -> DynamicToolRecord:
        from ..errors import ToolBuilderError

        record = self.approved_candidate(tool_id)
        if record is None:
            raise ToolBuilderError(f"No approved dynamic tool exists for '{tool_id}'.")
        now = _format(utc_now())
        with self._db.connect() as connection:
            connection.execute(
                """
                UPDATE dynamic_tool_candidates
                SET enabled = ?, updated_at = ?
                WHERE id = ? AND status = 'approved'
                """,
                (int(enabled), now, record.database_id),
            )
            self._insert_event(
                connection,
                record.database_id,
                "enabled" if enabled else "disabled",
                None,
            )
        updated = self.get_candidate(record.candidate_key)
        if updated is None:  # pragma: no cover
            raise StorageError("Dynamic tool candidate disappeared after enablement change.")
        return updated

    def rollback(self, tool_id: str) -> DynamicToolRecord:
        from ..errors import ToolBuilderError

        current = self.approved_candidate(tool_id)
        target = self.rollback_target(tool_id)
        if current is None:
            raise ToolBuilderError(f"No approved dynamic tool exists for '{tool_id}'.")
        if target is None:
            raise ToolBuilderError(f"No tested previous version is available for '{tool_id}'.")
        now = _format(utc_now())
        with self._db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE dynamic_tool_candidates
                SET status = 'superseded', enabled = 0, updated_at = ?
                WHERE id = ? AND status = 'approved'
                """,
                (now, current.database_id),
            )
            self._insert_event(
                connection,
                current.database_id,
                "rolled_back_from",
                f"Rollback to {target.candidate_key}",
            )
            cursor = connection.execute(
                """
                UPDATE dynamic_tool_candidates
                SET status = 'approved', enabled = 1, approved_at = ?, updated_at = ?
                WHERE id = ? AND status = 'superseded'
                """,
                (now, now, target.database_id),
            )
            if cursor.rowcount != 1:
                raise ToolBuilderError("Rollback target status changed before rollback.")
            self._insert_event(connection, target.database_id, "rollback_approved", None)
        updated = self.get_candidate(target.candidate_key)
        if updated is None:  # pragma: no cover
            raise StorageError("Dynamic tool candidate disappeared after rollback.")
        return updated

    def events(self, record: DynamicToolRecord) -> list[dict[str, Any]]:
        with self._db.connect() as connection:
            rows = connection.execute(
                """
                SELECT event_type, message, created_at FROM dynamic_tool_events
                WHERE candidate_id = ? ORDER BY id
                """,
                (record.database_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        candidate_id: int,
        event_type: str,
        message: str | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO dynamic_tool_events (candidate_id, event_type, message, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (candidate_id, event_type, message, _format(utc_now())),
        )

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> DynamicToolRecord:
        from ..tooling.models import ToolProposal

        proposal = ToolProposal.from_dict(
            {
                "tool_id": str(row["tool_id"]),
                "name": str(row["name"]),
                "version": str(row["version"]),
                "description": str(row["description"]),
                "input_schema": _parsed_json(row["input_schema_json"], {}),
                "output_schema": _parsed_json(row["output_schema_json"], {}),
                "source_code": str(row["source_code"]),
                "test_cases": _parsed_json(row["tests_json"], []),
                "dependencies": _parsed_json(row["dependencies_json"], []),
                "timeout_seconds": float(row["timeout_seconds"]),
                "permissions": [],
                "network_required": False,
            }
        )
        if proposal.source_sha256 != str(row["source_sha256"]):
            raise StorageError(f"Integrity check failed for dynamic tool {row['candidate_key']}.")
        test_report = _parsed_json(row["test_report_json"], None)
        return DynamicToolRecord(
            database_id=int(row["id"]),
            candidate_key=str(row["candidate_key"]),
            proposal=proposal,
            status=str(row["status"]),
            enabled=bool(row["enabled"]),
            provenance=str(row["provenance"]),
            generator_model=row["generator_model"],
            parent_candidate_id=(
                int(row["parent_candidate_id"]) if row["parent_candidate_id"] is not None else None
            ),
            repair_attempt=int(row["repair_attempt"]),
            static_report=_parsed_json(row["static_report_json"], {}),
            test_report=test_report if isinstance(test_report, Mapping) else None,
            created_at=parse_iso_datetime(str(row["created_at"])),
            updated_at=parse_iso_datetime(str(row["updated_at"])),
            approved_at=(
                parse_iso_datetime(str(row["approved_at"])) if row["approved_at"] else None
            ),
        )


def _plain_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False)


def _parsed_json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return default


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
