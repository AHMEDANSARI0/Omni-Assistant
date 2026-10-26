"""SQLite connection handling and versioned migrations.

Schema changes must always ship as a new numbered migration below - never as a
silent edit to an already released migration.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..domain.task import TaskStatus
from ..errors import StorageError

_STATUS_CHECK = ", ".join(f"'{status.value}'" for status in TaskStatus)

_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        f"""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            instruction TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ({_STATUS_CHECK})),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            error TEXT
        );
        CREATE INDEX idx_tasks_status_created ON tasks (status, created_at);

        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            old_status TEXT,
            new_status TEXT,
            message TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_task_events_task ON task_events (task_id, id);

        CREATE TABLE user_preferences (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        """
        CREATE TABLE task_plans (
            task_id TEXT PRIMARY KEY REFERENCES tasks (id) ON DELETE CASCADE,
            plan_json TEXT NOT NULL,
            planner TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE approval_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN ('approved', 'denied')),
            scope_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_approval_records_task ON approval_records (task_id, id);

        CREATE TABLE tool_invocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
            tool_id TEXT NOT NULL,
            tool_version TEXT NOT NULL,
            success INTEGER NOT NULL CHECK (success IN (0, 1)),
            arguments_json TEXT NOT NULL,
            output_json TEXT,
            error TEXT,
            elapsed_ms REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_tool_invocations_task ON tool_invocations (task_id, id);
        """,
    ),
)


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path.expanduser()

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(self._path)
        except sqlite3.Error as exc:
            raise StorageError(f"Could not open the local database at {self._path}.") from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> int:
        """Apply pending migrations. Safe to call on every startup."""
        try:
            with self.connect() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
                ).fetchone()
                current = int(row["version"])
                for version, script in _MIGRATIONS:
                    if version <= current:
                        continue
                    connection.executescript(script)
                    connection.execute(
                        "INSERT INTO schema_migrations (version, applied_at) "
                        "VALUES (?, datetime('now'))",
                        (version,),
                    )
                    current = version
                return current
        except sqlite3.Error as exc:
            raise StorageError("Local database migration failed.") from exc

    def schema_version(self) -> int:
        if not self._path.is_file():
            return 0
        with self.connect() as connection:
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            if table is None:
                return 0
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            return int(row["version"])
