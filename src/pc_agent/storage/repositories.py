"""Repositories - the only code allowed to read/write task tables directly."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..domain.task import Task, TaskStatus, parse_iso_datetime, utc_now
from ..errors import StorageError
from ..logging_config import redact_text
from ..tools.registry import ToolResult
from .db import Database


def _format(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _approval_center_from_row(row: Any) -> ApprovalCenterRequest:
    roots = _parsed_json(row["read_roots_json"], [])
    return ApprovalCenterRequest(
        id=int(row["id"]),
        request_key=str(row["request_key"]),
        task_id=str(row["task_id"]),
        action=str(row["action"]),
        tool_id=str(row["tool_id"]),
        arguments_sha256=str(row["arguments_sha256"]),
        read_roots=tuple(str(item) for item in roots) if isinstance(roots, list) else (),
        summary=str(row["summary"]),
        status=str(row["status"]),
        decided_by=str(row["decided_by"]) if row["decided_by"] else None,
        created_at=parse_iso_datetime(str(row["created_at"])),
        decided_at=(
            parse_iso_datetime(str(row["decided_at"])) if row["decided_at"] else None
        ),
        expires_at=parse_iso_datetime(str(row["expires_at"])),
        consumed_at=(
            parse_iso_datetime(str(row["consumed_at"])) if row["consumed_at"] else None
        ),
    )


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
class ApprovalCenterRequest:
    """One out-of-band Approval Center request (Transformation Phase 2).

    Raw tool arguments are never stored here; ``arguments_sha256`` binds
    the decision to the exact approved scope.
    """

    id: int
    request_key: str
    task_id: str
    action: str
    tool_id: str
    arguments_sha256: str
    read_roots: tuple[str, ...]
    summary: str
    status: str  # pending | approved | denied | expired | consumed
    decided_by: str | None
    created_at: datetime
    decided_at: datetime | None
    expires_at: datetime
    consumed_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "request_key": self.request_key,
            "task_id": self.task_id,
            "action": self.action,
            "tool_id": self.tool_id,
            "arguments_sha256": self.arguments_sha256,
            "read_roots": list(self.read_roots),
            "summary": self.summary,
            "status": self.status,
            "decided_by": self.decided_by,
            "created_at": self.created_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "expires_at": self.expires_at.isoformat(),
            "consumed_at": self.consumed_at.isoformat() if self.consumed_at else None,
        }


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
class AppEventRecord:
    """One global application event (Transformation Phase 3 event system)."""

    id: int
    event_type: str
    source: str
    task_id: str | None
    summary: str
    payload: Mapping[str, Any]
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "source": self.source,
            "task_id": self.task_id,
            "summary": self.summary,
            "payload": dict(self.payload),
            "created_at": self.created_at.isoformat(),
        }


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
    # Transformation Phase 8 (Memory v2); vector bytes stay in the repo.
    embedding_model: str | None = None
    tags: str = ""


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
        tags: str = "",
    ) -> MemoryItemRecord:
        if kind not in self._KINDS:
            raise StorageError(f"Unknown memory kind: {kind}")
        clean_key = key.strip()
        if not clean_key or len(clean_key) > 120:
            raise StorageError("Memory key must be 1-120 characters.")
        clean_title = title.strip() or clean_key
        if len(clean_title) > 200:
            raise StorageError("Memory title must be at most 200 characters.")
        clean_tags = " ".join(tags.replace(",", " ").split())[:200]
        now = _format(utc_now())
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO memory_items (kind, key, title, content_json,"
                " source_task_id, created_at, updated_at, tags)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(kind, key) DO UPDATE SET title = excluded.title,"
                # Memory v2: a content change invalidates the stored vector.
                " embedding = CASE WHEN excluded.content_json != memory_items.content_json"
                " THEN NULL ELSE memory_items.embedding END,"
                " embedding_model = CASE WHEN excluded.content_json != memory_items.content_json"
                " THEN NULL ELSE memory_items.embedding_model END,"
                " tags = CASE WHEN excluded.tags != '' THEN excluded.tags"
                " ELSE memory_items.tags END,"
                " content_json = excluded.content_json,"
                " source_task_id = excluded.source_task_id,"
                " updated_at = excluded.updated_at",
                (
                    kind,
                    clean_key,
                    clean_title,
                    _json(dict(content)),
                    source_task_id,
                    now,
                    now,
                    clean_tags,
                ),
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

    # -- Memory v2 semantic embeddings (Transformation Phase 8) ----------------
    def set_embedding(self, kind: str, key: str, vector: bytes, model: str) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "UPDATE memory_items SET embedding = ?, embedding_model = ?"
                " WHERE kind = ? AND key = ?",
                (vector, model, kind, key),
            )
            return cursor.rowcount > 0

    def notes_needing_embedding(
        self, model: str, *, limit: int = 200
    ) -> list[MemoryItemRecord]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_items WHERE kind = 'note'"
                " AND (embedding IS NULL OR embedding_model IS NULL"
                " OR embedding_model != ?)"
                " ORDER BY updated_at DESC, id DESC LIMIT ?",
                (model, max(1, min(limit, 1000))),
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def embedded_notes(
        self, model: str, *, limit: int = 5000
    ) -> list[tuple[MemoryItemRecord, bytes]]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_items WHERE kind = 'note'"
                " AND embedding IS NOT NULL AND embedding_model = ?"
                " ORDER BY updated_at DESC, id DESC LIMIT ?",
                (model, max(1, min(limit, 20000))),
            ).fetchall()
        return [
            (self._record_from_row(row), bytes(row["embedding"])) for row in rows
        ]

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> MemoryItemRecord:
        keys = row.keys()
        return MemoryItemRecord(
            id=int(row["id"]),
            kind=str(row["kind"]),
            key=str(row["key"]),
            title=str(row["title"]),
            content=_json_object(str(row["content_json"])),
            source_task_id=str(row["source_task_id"]) if row["source_task_id"] else None,
            created_at=parse_iso_datetime(str(row["created_at"])),
            updated_at=parse_iso_datetime(str(row["updated_at"])),
            embedding_model=(
                str(row["embedding_model"])
                if "embedding_model" in keys and row["embedding_model"]
                else None
            ),
            tags=str(row["tags"]) if "tags" in keys and row["tags"] else "",
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

    def create_approval_center_request(
        self,
        task_id: str,
        *,
        action: str,
        tool_id: str,
        arguments_sha256: str,
        read_roots: tuple[str, ...],
        summary: str,
        expires_at: datetime,
    ) -> ApprovalCenterRequest:
        """Create (or reuse) a pending Approval Center request.

        An identical pending request for the same task/tool/exact-arguments
        is returned unchanged, so repeated non-interactive runs do not pile
        up duplicate rows. Expired pendings are closed first.
        """

        self._expire_approval_center_requests()
        for existing in self.list_approval_center_requests(task_id=task_id, limit=200):
            if (
                existing.status == "pending"
                and existing.tool_id == tool_id
                and existing.action == action
                and existing.arguments_sha256 == arguments_sha256
                and existing.read_roots == tuple(read_roots)
            ):
                return existing
        request_key = f"apr-{secrets.token_hex(6)}"
        with self._db.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO approval_center_requests (request_key, task_id, action,"
                " tool_id, arguments_sha256, read_roots_json, summary, status,"
                " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    request_key,
                    task_id,
                    action,
                    tool_id,
                    arguments_sha256,
                    _json(list(read_roots)),
                    summary[:300],
                    _format(utc_now()),
                    _format(expires_at),
                ),
            )
            row_id = int(cursor.lastrowid)
        created = self.get_approval_center_request(request_key)
        assert created is not None and created.id == row_id
        EventRepository(self._db).record(
            "approval.requested",
            source="approval-center",
            task_id=task_id,
            summary=f"Approval Center request {request_key} pending for {tool_id}.",
            payload={"request_key": request_key, "tool_id": tool_id, "action": action},
        )
        return created

    def get_approval_center_request(
        self, request_key: str
    ) -> ApprovalCenterRequest | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM approval_center_requests WHERE request_key = ?",
                (request_key,),
            ).fetchone()
        return _approval_center_from_row(row) if row is not None else None

    def list_approval_center_requests(
        self,
        *,
        status: str | None = None,
        task_id: str | None = None,
        limit: int = 50,
    ) -> list[ApprovalCenterRequest]:
        self._expire_approval_center_requests()
        query = "SELECT * FROM approval_center_requests"
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._db.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_approval_center_from_row(row) for row in rows]

    def pending_approval_center_count(self) -> int:
        self._expire_approval_center_requests()
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM approval_center_requests"
                " WHERE status = 'pending'"
            ).fetchone()
        return int(row["total"])

    def decide_approval_center_request(
        self, request_key: str, *, approved: bool, decided_by: str
    ) -> ApprovalCenterRequest:
        self._expire_approval_center_requests()
        current = self.get_approval_center_request(request_key)
        if current is None:
            raise StorageError(f"Approval Center request was not found: {request_key}.")
        if current.status != "pending":
            raise StorageError(
                f"Approval Center request {request_key} is {current.status} "
                "and can no longer be decided."
            )
        with self._db.connect() as connection:
            connection.execute(
                "UPDATE approval_center_requests SET status = ?, decided_by = ?,"
                " decided_at = ? WHERE request_key = ? AND status = 'pending'",
                (
                    "approved" if approved else "denied",
                    decided_by[:40],
                    _format(utc_now()),
                    request_key,
                ),
            )
        decided = self.get_approval_center_request(request_key)
        assert decided is not None
        EventRepository(self._db).record(
            "approval.decided",
            source="approval-center",
            task_id=decided.task_id,
            summary=(
                f"Approval Center request {request_key} "
                f"{decided.status} by {decided_by}."
            ),
            payload={
                "request_key": request_key,
                "tool_id": decided.tool_id,
                "decision": decided.status,
                "decided_by": decided_by,
            },
        )
        return decided

    def find_decided_approval_center_request(
        self,
        task_id: str,
        *,
        action: str,
        tool_id: str,
        arguments_sha256: str,
        read_roots: tuple[str, ...],
    ) -> ApprovalCenterRequest | None:
        """Latest unconsumed approved/denied decision for an exact scope."""

        for request in self.list_approval_center_requests(task_id=task_id, limit=200):
            if (
                request.status in {"approved", "denied"}
                and request.action == action
                and request.tool_id == tool_id
                and request.arguments_sha256 == arguments_sha256
                and request.read_roots == tuple(read_roots)
            ):
                return request
        return None

    def consume_approval_center_request(
        self, request_key: str
    ) -> ApprovalCenterRequest:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "UPDATE approval_center_requests SET status = 'consumed',"
                " consumed_at = ? WHERE request_key = ?"
                " AND status IN ('approved', 'denied')",
                (_format(utc_now()), request_key),
            )
            if cursor.rowcount != 1:
                raise StorageError(
                    f"Approval Center request {request_key} could not be consumed."
                )
        consumed = self.get_approval_center_request(request_key)
        assert consumed is not None
        return consumed

    def _expire_approval_center_requests(self) -> None:
        with self._db.connect() as connection:
            connection.execute(
                "UPDATE approval_center_requests SET status = 'expired'"
                " WHERE status IN ('pending', 'approved', 'denied')"
                " AND expires_at <= ?",
                (_format(utc_now()),),
            )

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


class EventRepository:
    """Global application events: the Phase 3 event system's durable log.

    Events are small, secret-redacted facts (task lifecycle, approvals,
    safety, backups, API). Consumers poll with a ``since_id`` cursor;
    the table is trimmed to the most recent ``MAX_EVENTS`` rows so it
    cannot grow without bound.
    """

    MAX_EVENTS = 10_000

    def __init__(self, Database: Database) -> None:
        self._db = Database

    def record(
        self,
        event_type: str,
        *,
        source: str,
        summary: str,
        task_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> AppEventRecord:
        clean_type = event_type.strip()
        if not clean_type or len(clean_type) > 80:
            raise StorageError("Event type must be 1-80 characters.")
        clean_source = source.strip()[:40] or "app"
        clean_summary = redact_text(summary.strip())[:300]
        with self._db.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO app_events (event_type, source, task_id, summary,"
                " payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    clean_type,
                    clean_source,
                    task_id,
                    clean_summary,
                    _json(dict(payload or {})),
                    _format(utc_now()),
                ),
            )
            row_id = int(cursor.lastrowid)
            connection.execute(
                "DELETE FROM app_events WHERE id <= "
                "(SELECT COALESCE(MAX(id), 0) - ? FROM app_events)",
                (self.MAX_EVENTS,),
            )
        created = self.get(row_id)
        if created is None:  # Trimmed instantly only if MAX_EVENTS were 0.
            raise StorageError("The recorded event could not be read back.")
        return created

    def get(self, event_id: int) -> AppEventRecord | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM app_events WHERE id = ?", (event_id,)
            ).fetchone()
        return _app_event_from_row(row) if row is not None else None

    def list(
        self,
        *,
        since_id: int = 0,
        event_type: str | None = None,
        task_id: str | None = None,
        limit: int = 100,
        descending: bool = False,
    ) -> list[AppEventRecord]:
        query = "SELECT * FROM app_events WHERE id > ?"
        params: list[Any] = [max(0, int(since_id))]
        if event_type is not None:
            query += " AND event_type = ?"
            params.append(event_type)
        if task_id is not None:
            query += " AND task_id = ?"
            params.append(task_id)
        query += " ORDER BY id " + ("DESC" if descending else "ASC") + " LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._db.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_app_event_from_row(row) for row in rows]

    def count(self) -> int:
        with self._db.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM app_events").fetchone()
        return int(row["total"])


@dataclass(frozen=True, slots=True)
class MissionRecord:
    """One mission: a named group of tasks with a dependency graph."""

    id: str
    name: str
    description: str
    status: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


@dataclass(frozen=True, slots=True)
class ScheduleRecord:
    """One recurring or one-off scheduled instruction (Phase 4)."""

    id: str
    name: str
    instruction: str
    kind: str
    run_at: datetime | None
    interval_seconds: int | None
    next_run_at: datetime
    enabled: bool
    use_ai: bool
    created_at: datetime
    updated_at: datetime
    last_run_at: datetime | None
    last_task_id: str | None
    run_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "instruction": self.instruction,
            "kind": self.kind,
            "run_at": self.run_at.isoformat() if self.run_at else None,
            "interval_seconds": self.interval_seconds,
            "next_run_at": self.next_run_at.isoformat(),
            "enabled": self.enabled,
            "use_ai": self.use_ai,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_task_id": self.last_task_id,
            "run_count": self.run_count,
        }


MISSION_STATUSES = ("draft", "running", "succeeded", "failed", "cancelled")


class MissionRepository:
    """Missions, their task membership/dependencies, and task schedules."""

    def __init__(self, Database: Database) -> None:
        self._db = Database

    # -- missions -------------------------------------------------------------
    def create_mission(self, name: str, *, description: str = "") -> MissionRecord:
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 120:
            raise StorageError("Mission name must be 1-120 characters.")
        mission_id = uuid.uuid4().hex
        now = _format(utc_now())
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO missions (id, name, description, status, created_at,"
                " updated_at) VALUES (?, ?, ?, 'draft', ?, ?)",
                (mission_id, clean_name, description.strip()[:2000], now, now),
            )
        mission = self.get_mission(mission_id)
        assert mission is not None
        return mission

    def get_mission(self, mission_id: str) -> MissionRecord | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM missions WHERE id = ?", (mission_id,)
            ).fetchone()
        return _mission_from_row(row) if row is not None else None

    def list_missions(self, *, limit: int = 100) -> list[MissionRecord]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM missions ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [_mission_from_row(row) for row in rows]

    def count_missions(self) -> int:
        with self._db.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM missions").fetchone()
        return int(row["total"])

    def update_mission_status(self, mission_id: str, status: str) -> MissionRecord:
        if status not in MISSION_STATUSES:
            raise StorageError(f"Unknown mission status: {status}")
        now = utc_now()
        completed = _format(now) if status in {"succeeded", "failed", "cancelled"} else None
        with self._db.connect() as connection:
            cursor = connection.execute(
                "UPDATE missions SET status = ?, updated_at = ?, completed_at ="
                " COALESCE(?, completed_at) WHERE id = ?",
                (status, _format(now), completed, mission_id),
            )
            if cursor.rowcount != 1:
                raise StorageError(f"Mission not found: {mission_id}")
        mission = self.get_mission(mission_id)
        assert mission is not None
        return mission

    def delete_mission(self, mission_id: str) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM missions WHERE id = ?", (mission_id,)
            )
            return cursor.rowcount == 1

    # -- membership and dependencies --------------------------------------------
    def add_task(self, mission_id: str, task_id: str) -> None:
        with self._db.connect() as connection:
            position = connection.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM mission_tasks"
                " WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()["next"]
            connection.execute(
                "INSERT OR IGNORE INTO mission_tasks (mission_id, task_id, position,"
                " added_at) VALUES (?, ?, ?, ?)",
                (mission_id, task_id, int(position), _format(utc_now())),
            )

    def remove_task(self, mission_id: str, task_id: str) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM mission_tasks WHERE mission_id = ? AND task_id = ?",
                (mission_id, task_id),
            )
            return cursor.rowcount == 1

    def list_task_ids(self, mission_id: str) -> list[str]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT task_id FROM mission_tasks WHERE mission_id = ?"
                " ORDER BY position, task_id",
                (mission_id,),
            ).fetchall()
        return [str(row["task_id"]) for row in rows]

    def add_dependency(self, mission_id: str, task_id: str, depends_on: str) -> None:
        if task_id == depends_on:
            raise StorageError("A task cannot depend on itself.")
        members = set(self.list_task_ids(mission_id))
        if task_id not in members or depends_on not in members:
            raise StorageError("Both tasks must belong to the mission before linking.")
        graph = self.list_dependencies(mission_id)
        graph.setdefault(task_id, []).append(depends_on)
        _assert_acyclic(graph)
        with self._db.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO mission_dependencies (mission_id, task_id,"
                " depends_on_task_id, created_at) VALUES (?, ?, ?, ?)",
                (mission_id, task_id, depends_on, _format(utc_now())),
            )

    def list_dependencies(self, mission_id: str) -> dict[str, list[str]]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT task_id, depends_on_task_id FROM mission_dependencies"
                " WHERE mission_id = ? ORDER BY task_id, depends_on_task_id",
                (mission_id,),
            ).fetchall()
        graph: dict[str, list[str]] = {}
        for row in rows:
            graph.setdefault(str(row["task_id"]), []).append(str(row["depends_on_task_id"]))
        return graph

    # -- schedules ---------------------------------------------------------------
    def create_schedule(
        self,
        name: str,
        *,
        instruction: str,
        kind: str,
        run_at: datetime | None = None,
        interval_seconds: int | None = None,
        use_ai: bool = False,
        now: datetime | None = None,
    ) -> ScheduleRecord:
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 120:
            raise StorageError("Schedule name must be 1-120 characters.")
        if not instruction.strip():
            raise StorageError("Schedule instruction must not be empty.")
        moment = now or utc_now()
        if kind == "once":
            if run_at is None:
                raise StorageError("A one-off schedule needs a run_at time.")
            next_run = run_at
        elif kind == "interval":
            if interval_seconds is None or interval_seconds < 60 or interval_seconds > 31_536_000:
                raise StorageError("Interval schedules need 60..31536000 seconds.")
            next_run = moment + timedelta(seconds=interval_seconds)
        else:
            raise StorageError(f"Unknown schedule kind: {kind}")
        schedule_id = "sch-" + uuid.uuid4().hex[:12]
        stamp = _format(moment)
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO task_schedules (id, name, instruction, kind, run_at,"
                " interval_seconds, next_run_at, enabled, use_ai, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    schedule_id,
                    clean_name,
                    instruction.strip(),
                    kind,
                    _format(run_at) if run_at else None,
                    interval_seconds,
                    _format(next_run),
                    1 if use_ai else 0,
                    stamp,
                    stamp,
                ),
            )
        schedule = self.get_schedule(schedule_id)
        assert schedule is not None
        return schedule

    def get_schedule(self, schedule_id: str) -> ScheduleRecord | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        return _schedule_from_row(row) if row is not None else None

    def list_schedules(self, *, limit: int = 200) -> list[ScheduleRecord]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_schedules ORDER BY next_run_at, id LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [_schedule_from_row(row) for row in rows]

    def count_schedules(self, *, enabled_only: bool = False) -> int:
        query = "SELECT COUNT(*) AS total FROM task_schedules"
        if enabled_only:
            query += " WHERE enabled = 1"
        with self._db.connect() as connection:
            row = connection.execute(query).fetchone()
        return int(row["total"])

    def due_schedules(self, *, moment: datetime | None = None) -> list[ScheduleRecord]:
        stamp = _format(moment or utc_now())
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_schedules WHERE enabled = 1 AND next_run_at <= ?"
                " ORDER BY next_run_at, id",
                (stamp,),
            ).fetchall()
        return [_schedule_from_row(row) for row in rows]

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> ScheduleRecord:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "UPDATE task_schedules SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, _format(utc_now()), schedule_id),
            )
            if cursor.rowcount != 1:
                raise StorageError(f"Schedule not found: {schedule_id}")
        schedule = self.get_schedule(schedule_id)
        assert schedule is not None
        return schedule

    def delete_schedule(self, schedule_id: str) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM task_schedules WHERE id = ?", (schedule_id,)
            )
            return cursor.rowcount == 1

    def mark_schedule_triggered(
        self, schedule_id: str, *, task_id: str, moment: datetime | None = None
    ) -> ScheduleRecord:
        schedule = self.get_schedule(schedule_id)
        if schedule is None:
            raise StorageError(f"Schedule not found: {schedule_id}")
        fired_at = moment or utc_now()
        if schedule.kind == "once":
            enabled = 0
            next_run = schedule.next_run_at
        else:
            enabled = 1
            interval = schedule.interval_seconds or 3600
            next_run = fired_at + timedelta(seconds=interval)
        with self._db.connect() as connection:
            connection.execute(
                "UPDATE task_schedules SET enabled = ?, next_run_at = ?, last_run_at = ?,"
                " last_task_id = ?, run_count = run_count + 1, updated_at = ? WHERE id = ?",
                (
                    enabled,
                    _format(next_run),
                    _format(fired_at),
                    task_id,
                    _format(fired_at),
                    schedule_id,
                ),
            )
        updated = self.get_schedule(schedule_id)
        assert updated is not None
        return updated


def _assert_acyclic(graph: dict[str, list[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise StorageError("That dependency would create a cycle in the mission graph.")
        visiting.add(node)
        for dependency in graph.get(node, []):
            visit(dependency)
        visiting.discard(node)
        visited.add(node)

    for node in graph:
        visit(node)


def _mission_from_row(row: Any) -> MissionRecord:
    return MissionRecord(
        id=str(row["id"]),
        name=str(row["name"]),
        description=str(row["description"]),
        status=str(row["status"]),
        created_at=parse_iso_datetime(str(row["created_at"])),
        updated_at=parse_iso_datetime(str(row["updated_at"])),
        completed_at=parse_iso_datetime(str(row["completed_at"])) if row["completed_at"] else None,
    )


def _schedule_from_row(row: Any) -> ScheduleRecord:
    return ScheduleRecord(
        id=str(row["id"]),
        name=str(row["name"]),
        instruction=str(row["instruction"]),
        kind=str(row["kind"]),
        run_at=parse_iso_datetime(str(row["run_at"])) if row["run_at"] else None,
        interval_seconds=int(row["interval_seconds"]) if row["interval_seconds"] else None,
        next_run_at=parse_iso_datetime(str(row["next_run_at"])),
        enabled=bool(row["enabled"]),
        use_ai=bool(row["use_ai"]),
        created_at=parse_iso_datetime(str(row["created_at"])),
        updated_at=parse_iso_datetime(str(row["updated_at"])),
        last_run_at=parse_iso_datetime(str(row["last_run_at"])) if row["last_run_at"] else None,
        last_task_id=str(row["last_task_id"]) if row["last_task_id"] else None,
        run_count=int(row["run_count"]),
    )


def _app_event_from_row(row: Any) -> AppEventRecord:
    return AppEventRecord(
        id=int(row["id"]),
        event_type=str(row["event_type"]),
        source=str(row["source"]),
        task_id=str(row["task_id"]) if row["task_id"] else None,
        summary=str(row["summary"]),
        payload=_json_object(str(row["payload_json"])),
        created_at=parse_iso_datetime(str(row["created_at"])),
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


@dataclass(frozen=True, slots=True)
class KnowledgeDocumentRecord:
    """One ingested document in the local knowledge base (Phase 8)."""

    id: int
    source_name: str
    source_path: str | None
    kind: str
    sha256: str
    title: str
    characters: int
    chunk_count: int
    embedding_model: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class KnowledgeChunkRecord:
    """One embedded text chunk; ``vector`` is packed float32 bytes."""

    id: int
    document_id: int
    ordinal: int
    text: str
    vector: bytes
    embedding_model: str
    document_title: str = ""
    source_name: str = ""


class KnowledgeRepository:
    """Document + chunk storage for the local knowledge base (Phase 8).

    Vectors are stored as packed float32 BLOBs tagged with the exact
    embedding model that produced them; search only ever compares
    vectors from one model, so switching embedding models can never
    silently mix incompatible spaces.
    """

    def __init__(self, Database: Database) -> None:
        self._db = Database

    def get_document_by_sha(self, sha256: str) -> KnowledgeDocumentRecord | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_documents WHERE sha256 = ?", (sha256,)
            ).fetchone()
        return self._document_from_row(row) if row is not None else None

    def get_document(self, document_id: int) -> KnowledgeDocumentRecord | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_documents WHERE id = ?", (document_id,)
            ).fetchone()
        return self._document_from_row(row) if row is not None else None

    def add_document(
        self,
        *,
        source_name: str,
        source_path: str | None,
        kind: str,
        sha256: str,
        title: str,
        characters: int,
        chunk_count: int,
        embedding_model: str,
    ) -> KnowledgeDocumentRecord:
        now = _format(utc_now())
        with self._db.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO knowledge_documents (source_name, source_path, kind,"
                " sha256, title, characters, chunk_count, embedding_model, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_name,
                    source_path,
                    kind,
                    sha256,
                    title,
                    characters,
                    chunk_count,
                    embedding_model,
                    now,
                ),
            )
            document_id = int(cursor.lastrowid)
        record = self.get_document(document_id)
        if record is None:  # pragma: no cover - row was just written
            raise StorageError("Knowledge document could not be reloaded after saving.")
        return record

    def add_chunks(
        self,
        document_id: int,
        chunks: list[tuple[int, str, bytes]],
        *,
        embedding_model: str,
    ) -> None:
        now = _format(utc_now())
        with self._db.connect() as connection:
            connection.executemany(
                "INSERT INTO knowledge_chunks (document_id, ordinal, text, vector,"
                " embedding_model, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (document_id, ordinal, text, vector, embedding_model, now)
                    for ordinal, text, vector in chunks
                ],
            )

    def list_documents(self, *, limit: int = 200) -> list[KnowledgeDocumentRecord]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_documents ORDER BY created_at DESC, id DESC"
                " LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [self._document_from_row(row) for row in rows]

    def delete_document(self, document_id: int) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM knowledge_documents WHERE id = ?", (document_id,)
            )
            return cursor.rowcount > 0

    def chunks_for_model(
        self, embedding_model: str, *, limit: int = 20000
    ) -> list[KnowledgeChunkRecord]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT c.*, d.title AS doc_title, d.source_name AS doc_source"
                " FROM knowledge_chunks c"
                " JOIN knowledge_documents d ON d.id = c.document_id"
                " WHERE c.embedding_model = ? ORDER BY c.id LIMIT ?",
                (embedding_model, max(1, min(limit, 100000))),
            ).fetchall()
        return [self._chunk_from_row(row) for row in rows]

    def counts(self) -> dict[str, Any]:
        with self._db.connect() as connection:
            documents = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM knowledge_documents"
                ).fetchone()["n"]
            )
            chunks = int(
                connection.execute("SELECT COUNT(*) AS n FROM knowledge_chunks").fetchone()[
                    "n"
                ]
            )
            models = [
                str(row["embedding_model"])
                for row in connection.execute(
                    "SELECT DISTINCT embedding_model FROM knowledge_chunks"
                    " ORDER BY embedding_model"
                ).fetchall()
            ]
        return {"documents": documents, "chunks": chunks, "embedding_models": models}

    @staticmethod
    def _document_from_row(row: sqlite3.Row) -> KnowledgeDocumentRecord:
        return KnowledgeDocumentRecord(
            id=int(row["id"]),
            source_name=str(row["source_name"]),
            source_path=str(row["source_path"]) if row["source_path"] else None,
            kind=str(row["kind"]),
            sha256=str(row["sha256"]),
            title=str(row["title"]),
            characters=int(row["characters"]),
            chunk_count=int(row["chunk_count"]),
            embedding_model=str(row["embedding_model"]),
            created_at=parse_iso_datetime(str(row["created_at"])),
        )

    @staticmethod
    def _chunk_from_row(row: sqlite3.Row) -> KnowledgeChunkRecord:
        keys = row.keys()
        return KnowledgeChunkRecord(
            id=int(row["id"]),
            document_id=int(row["document_id"]),
            ordinal=int(row["ordinal"]),
            text=str(row["text"]),
            vector=bytes(row["vector"]),
            embedding_model=str(row["embedding_model"]),
            document_title=str(row["doc_title"]) if "doc_title" in keys else "",
            source_name=str(row["doc_source"]) if "doc_source" in keys else "",
        )


@dataclass(frozen=True, slots=True)
class AgentDefinitionRecord:
    id: int
    name: str
    role: str
    description: str
    system_prompt: str
    allowed_tools: tuple[str, ...]
    provider: str
    max_steps: int
    token_budget: int | None
    builtin: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AgentRunRecord:
    id: int
    team_run_id: str
    agent_name: str
    task_id: str | None
    instruction: str
    status: str
    summary: str
    created_at: datetime
    completed_at: datetime | None


class AgentRepository:
    """Agent Studio definitions + run ledger (Transformation Phase 9)."""

    _RUN_STATUSES = frozenset(
        {"running", "succeeded", "failed", "blocked", "cancelled", "needs_input"}
    )

    def __init__(self, Database: Database) -> None:
        self._db = Database

    # -- definitions ---------------------------------------------------------
    def upsert_definition(
        self,
        *,
        name: str,
        role: str,
        description: str,
        system_prompt: str,
        allowed_tools: tuple[str, ...],
        provider: str = "",
        max_steps: int = 5,
        token_budget: int | None = None,
        builtin: bool = False,
        enabled: bool = True,
    ) -> AgentDefinitionRecord:
        now = _format(utc_now())
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO agent_definitions (name, role, description,"
                " system_prompt, allowed_tools, provider, max_steps,"
                " token_budget, builtin, enabled, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET role = excluded.role,"
                " description = excluded.description,"
                " system_prompt = excluded.system_prompt,"
                " allowed_tools = excluded.allowed_tools,"
                " provider = excluded.provider,"
                " max_steps = excluded.max_steps,"
                " token_budget = excluded.token_budget,"
                " enabled = excluded.enabled,"
                " updated_at = excluded.updated_at",
                (
                    name,
                    role,
                    description,
                    system_prompt,
                    _json(list(allowed_tools)),
                    provider,
                    max_steps,
                    token_budget,
                    1 if builtin else 0,
                    1 if enabled else 0,
                    now,
                    now,
                ),
            )
        record = self.get_definition(name)
        if record is None:  # pragma: no cover - row was just written
            raise StorageError("Agent definition could not be reloaded after saving.")
        return record

    def get_definition(self, name: str) -> AgentDefinitionRecord | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_definitions WHERE name = ?", (name,)
            ).fetchone()
        return self._definition_from_row(row) if row is not None else None

    def list_definitions(
        self, *, enabled_only: bool = False, limit: int = 200
    ) -> list[AgentDefinitionRecord]:
        query = "SELECT * FROM agent_definitions"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY builtin DESC, name ASC LIMIT ?"
        with self._db.connect() as connection:
            rows = connection.execute(query, (max(1, min(limit, 1000)),)).fetchall()
        return [self._definition_from_row(row) for row in rows]

    def delete_definition(self, name: str) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM agent_definitions WHERE name = ?", (name,)
            )
            return cursor.rowcount > 0

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "UPDATE agent_definitions SET enabled = ?, updated_at = ?"
                " WHERE name = ?",
                (1 if enabled else 0, _format(utc_now()), name),
            )
            return cursor.rowcount > 0

    def counts(self) -> dict[str, int]:
        with self._db.connect() as connection:
            definitions = connection.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(enabled), 0) AS enabled"
                " FROM agent_definitions"
            ).fetchone()
            runs = connection.execute(
                "SELECT COUNT(*) AS n FROM agent_runs"
            ).fetchone()
        return {
            "agents": int(definitions["n"]),
            "agents_enabled": int(definitions["enabled"]),
            "agent_runs": int(runs["n"]),
        }

    # -- run ledger ----------------------------------------------------------
    def start_run(
        self, *, agent_name: str, instruction: str, team_run_id: str = ""
    ) -> AgentRunRecord:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO agent_runs (team_run_id, agent_name, task_id,"
                " instruction, status, summary, created_at, completed_at)"
                " VALUES (?, ?, NULL, ?, 'running', '', ?, NULL)",
                (team_run_id, agent_name, instruction, _format(utc_now())),
            )
            run_id = int(cursor.lastrowid)
        record = self._get_run(run_id)
        if record is None:  # pragma: no cover - row was just written
            raise StorageError("Agent run could not be reloaded after saving.")
        return record

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        task_id: str | None = None,
        summary: str = "",
    ) -> AgentRunRecord:
        if status not in self._RUN_STATUSES or status == "running":
            raise StorageError(f"Invalid agent run status: {status}")
        with self._db.connect() as connection:
            connection.execute(
                "UPDATE agent_runs SET status = ?,"
                " task_id = COALESCE(?, task_id), summary = ?,"
                " completed_at = ? WHERE id = ?",
                (status, task_id, summary[:500], _format(utc_now()), run_id),
            )
        record = self._get_run(run_id)
        if record is None:
            raise StorageError(f"Agent run not found: {run_id}")
        return record

    def list_runs(
        self,
        *,
        agent_name: str | None = None,
        team_run_id: str | None = None,
        limit: int = 50,
    ) -> list[AgentRunRecord]:
        query = "SELECT * FROM agent_runs"
        conditions: list[str] = []
        params: list[object] = []
        if agent_name is not None:
            conditions.append("agent_name = ?")
            params.append(agent_name)
        if team_run_id is not None:
            conditions.append("team_run_id = ?")
            params.append(team_run_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self._db.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._run_from_row(row) for row in rows]

    def _get_run(self, run_id: int) -> AgentRunRecord | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._run_from_row(row) if row is not None else None

    @staticmethod
    def _definition_from_row(row: sqlite3.Row) -> AgentDefinitionRecord:
        tools = _parsed_json(row["allowed_tools"], [])
        return AgentDefinitionRecord(
            id=int(row["id"]),
            name=str(row["name"]),
            role=str(row["role"]),
            description=str(row["description"]),
            system_prompt=str(row["system_prompt"]),
            allowed_tools=tuple(str(item) for item in tools if isinstance(item, str)),
            provider=str(row["provider"]),
            max_steps=int(row["max_steps"]),
            token_budget=(int(row["token_budget"]) if row["token_budget"] is not None else None),
            builtin=bool(row["builtin"]),
            enabled=bool(row["enabled"]),
            created_at=parse_iso_datetime(str(row["created_at"])),
            updated_at=parse_iso_datetime(str(row["updated_at"])),
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> AgentRunRecord:
        return AgentRunRecord(
            id=int(row["id"]),
            team_run_id=str(row["team_run_id"]),
            agent_name=str(row["agent_name"]),
            task_id=(str(row["task_id"]) if row["task_id"] is not None else None),
            instruction=str(row["instruction"]),
            status=str(row["status"]),
            summary=str(row["summary"]),
            created_at=parse_iso_datetime(str(row["created_at"])),
            completed_at=(
                parse_iso_datetime(str(row["completed_at"]))
                if row["completed_at"] is not None
                else None
            ),
        )

@dataclass(frozen=True, slots=True)
class VoiceSessionRecord:
    id: int
    mode: str
    state: str
    engine_stt: str
    engine_tts: str
    started_at: datetime
    ended_at: datetime | None
    duration_ms: int
    audio_seconds: float
    audio_retained: bool
    audio_path: str | None
    transcript: str | None
    transcript_chars: int
    task_id: str | None
    status: str
    error: str
    created_at: datetime


class VoiceRepository:
    """Voice session ledger (Transformation Phase 10).

    Rows record *that* a capture happened and how long it lasted. Audio
    bytes are only referenced when the user explicitly opted into
    retention, and transcripts are only stored when transcript storage
    is enabled - the default row proves a session happened without
    keeping either.
    """

    MAX_SESSIONS = 2_000

    def __init__(self, Database: Database) -> None:
        self._db = Database

    def add_session(
        self,
        *,
        mode: str,
        engine_stt: str = "",
        engine_tts: str = "",
        duration_ms: int = 0,
        audio_seconds: float = 0.0,
        audio_retained: bool = False,
        audio_path: str | None = None,
        transcript: str | None = None,
        transcript_chars: int = 0,
        task_id: str | None = None,
        state: str = "completed",
        status: str = "ok",
        error: str = "",
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> VoiceSessionRecord:
        start = started_at or utc_now()
        end = ended_at or utc_now()
        with self._db.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO voice_sessions (mode, state, engine_stt, engine_tts,"
                " started_at, ended_at, duration_ms, audio_seconds, audio_retained,"
                " audio_path, transcript, transcript_chars, task_id, status, error,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mode,
                    state,
                    engine_stt,
                    engine_tts,
                    _format(start),
                    _format(end),
                    int(duration_ms),
                    float(audio_seconds),
                    1 if audio_retained else 0,
                    audio_path,
                    transcript,
                    int(transcript_chars),
                    task_id,
                    status,
                    error[:500],
                    _format(utc_now()),
                ),
            )
            session_id = int(cursor.lastrowid)
            connection.execute(
                "DELETE FROM voice_sessions WHERE id NOT IN"
                " (SELECT id FROM voice_sessions ORDER BY id DESC LIMIT ?)",
                (self.MAX_SESSIONS,),
            )
        record = self.get_session(session_id)
        if record is None:  # pragma: no cover - row was just written
            raise StorageError("Voice session could not be reloaded after saving.")
        return record

    def get_session(self, session_id: int) -> VoiceSessionRecord | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM voice_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return self._session_from_row(row) if row is not None else None

    def list_sessions(self, limit: int = 20) -> list[VoiceSessionRecord]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM voice_sessions ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def sessions_with_retained_audio(self) -> list[VoiceSessionRecord]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM voice_sessions WHERE audio_path IS NOT NULL"
                " ORDER BY id DESC"
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def clear_audio_reference(self, session_id: int) -> None:
        with self._db.connect() as connection:
            connection.execute(
                "UPDATE voice_sessions SET audio_retained = 0, audio_path = NULL"
                " WHERE id = ?",
                (session_id,),
            )

    def delete_session(self, session_id: int) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM voice_sessions WHERE id = ?", (session_id,)
            )
            return cursor.rowcount > 0

    def count_sessions(self) -> int:
        with self._db.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM voice_sessions").fetchone()
        return int(row["total"]) if row is not None else 0

    def summary(self) -> dict[str, Any]:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total,"
                " COALESCE(SUM(audio_seconds), 0) AS audio,"
                " COALESCE(SUM(transcript_chars), 0) AS chars,"
                " COALESCE(SUM(audio_retained), 0) AS retained,"
                " MAX(created_at) AS last_at"
                " FROM voice_sessions"
            ).fetchone()
        if row is None:  # pragma: no cover - aggregate always returns a row
            return {"total": 0, "audio_seconds": 0.0, "transcript_chars": 0, "retained": 0, "last_at": None}
        return {
            "total": int(row["total"]),
            "audio_seconds": round(float(row["audio"]), 3),
            "transcript_chars": int(row["chars"]),
            "retained": int(row["retained"]),
            "last_at": str(row["last_at"]) if row["last_at"] is not None else None,
        }

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> VoiceSessionRecord:
        return VoiceSessionRecord(
            id=int(row["id"]),
            mode=str(row["mode"]),
            state=str(row["state"]),
            engine_stt=str(row["engine_stt"]),
            engine_tts=str(row["engine_tts"]),
            started_at=parse_iso_datetime(str(row["started_at"])),
            ended_at=(
                parse_iso_datetime(str(row["ended_at"]))
                if row["ended_at"] is not None
                else None
            ),
            duration_ms=int(row["duration_ms"]),
            audio_seconds=float(row["audio_seconds"]),
            audio_retained=bool(row["audio_retained"]),
            audio_path=(str(row["audio_path"]) if row["audio_path"] is not None else None),
            transcript=(str(row["transcript"]) if row["transcript"] is not None else None),
            transcript_chars=int(row["transcript_chars"]),
            task_id=(str(row["task_id"]) if row["task_id"] is not None else None),
            status=str(row["status"]),
            error=str(row["error"]),
            created_at=parse_iso_datetime(str(row["created_at"])),
        )

@dataclass(frozen=True, slots=True)
class AutomationRunRecord:
    id: int
    kind: str
    tool_id: str
    action: str
    target: str
    status: str
    detail: str
    artifact_path: str | None
    duration_ms: int
    allowed: bool
    created_at: datetime


class AutomationRepository:
    """Audit ledger for gated browser/desktop/vision actions (Phase 11).

    One row per attempted automation action - including refusals, which are
    the interesting ones for a security review. Rows hold identifiers and
    counts, never content: no page text, no keystrokes, no pixels.
    """

    MAX_RUNS = 5_000
    _KINDS = frozenset({"browser", "desktop", "vision"})
    _STATUSES = frozenset({"ok", "refused", "failed", "partial"})

    def __init__(self, Database: Database) -> None:
        self._db = Database

    def record(
        self,
        *,
        kind: str,
        tool_id: str,
        action: str = "",
        target: str = "",
        status: str = "ok",
        detail: str = "",
        artifact_path: str | None = None,
        duration_ms: int = 0,
        allowed: bool = True,
    ) -> AutomationRunRecord:
        clean_kind = kind.strip().lower()
        if clean_kind not in self._KINDS:
            raise StorageError(f"Automation kind must be one of: {', '.join(sorted(self._KINDS))}.")
        clean_status = status.strip().lower() or "ok"
        if clean_status not in self._STATUSES:
            raise StorageError(
                f"Automation status must be one of: {', '.join(sorted(self._STATUSES))}."
            )
        with self._db.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO automation_runs (kind, tool_id, action, target, status,"
                " detail, artifact_path, duration_ms, allowed, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    clean_kind,
                    tool_id.strip()[:120],
                    action.strip()[:120],
                    target.strip()[:400],
                    clean_status,
                    detail.strip()[:500],
                    artifact_path,
                    max(0, int(duration_ms)),
                    1 if allowed else 0,
                    _format(utc_now()),
                ),
            )
            run_id = int(cursor.lastrowid)
            connection.execute(
                "DELETE FROM automation_runs WHERE id NOT IN"
                " (SELECT id FROM automation_runs ORDER BY id DESC LIMIT ?)",
                (self.MAX_RUNS,),
            )
        record = self.get(run_id)
        if record is None:  # pragma: no cover - row was just written
            raise StorageError("Automation run could not be reloaded after saving.")
        return record

    def get(self, run_id: int) -> AutomationRunRecord | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM automation_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_runs(
        self, *, kind: str = "", limit: int = 50, status: str = ""
    ) -> list[AutomationRunRecord]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if kind.strip():
            clauses.append("kind = ?")
            parameters.append(kind.strip().lower())
        if status.strip():
            clauses.append("status = ?")
            parameters.append(status.strip().lower())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        parameters.append(max(1, min(int(limit), 500)))
        with self._db.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM automation_runs{where} ORDER BY id DESC LIMIT ?",
                tuple(parameters),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        with self._db.connect() as connection:
            totals = connection.execute(
                "SELECT kind, COUNT(*) AS total FROM automation_runs GROUP BY kind"
            ).fetchall()
            refused = connection.execute(
                "SELECT COUNT(*) AS total FROM automation_runs WHERE allowed = 0"
            ).fetchone()
            last = connection.execute(
                "SELECT created_at FROM automation_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        by_kind = {str(row["kind"]): int(row["total"]) for row in totals}
        return {
            "total": sum(by_kind.values()),
            "by_kind": by_kind,
            "refused": int(refused["total"]) if refused is not None else 0,
            "last_at": str(last["created_at"]) if last is not None else None,
        }

    @staticmethod
    def _from_row(row: sqlite3.Row) -> AutomationRunRecord:
        return AutomationRunRecord(
            id=int(row["id"]),
            kind=str(row["kind"]),
            tool_id=str(row["tool_id"]),
            action=str(row["action"]),
            target=str(row["target"]),
            status=str(row["status"]),
            detail=str(row["detail"]),
            artifact_path=(str(row["artifact_path"]) if row["artifact_path"] is not None else None),
            duration_ms=int(row["duration_ms"]),
            allowed=bool(row["allowed"]),
            created_at=parse_iso_datetime(str(row["created_at"])),
        )
