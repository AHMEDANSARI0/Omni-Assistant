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
LATEST_SCHEMA_VERSION = 6

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
    (
        3,
        """
        CREATE TABLE dynamic_tool_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_key TEXT NOT NULL UNIQUE,
            tool_id TEXT NOT NULL,
            version TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            input_schema_json TEXT NOT NULL,
            output_schema_json TEXT NOT NULL,
            source_code TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            tests_json TEXT NOT NULL,
            dependencies_json TEXT NOT NULL,
            timeout_seconds REAL NOT NULL DEFAULT 10,
            status TEXT NOT NULL CHECK (
                status IN (
                    'proposed', 'tested', 'test_failed',
                    'approved', 'rejected', 'superseded'
                )
            ),
            enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
            provenance TEXT NOT NULL,
            generator_model TEXT,
            parent_candidate_id INTEGER REFERENCES dynamic_tool_candidates (id),
            repair_attempt INTEGER NOT NULL DEFAULT 0,
            static_report_json TEXT NOT NULL,
            test_report_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            approved_at TEXT,
            UNIQUE (tool_id, version)
        );
        CREATE INDEX idx_dynamic_tools_tool_status
            ON dynamic_tool_candidates (tool_id, status, version);

        CREATE TABLE dynamic_tool_test_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL
                REFERENCES dynamic_tool_candidates (id) ON DELETE CASCADE,
            success INTEGER NOT NULL CHECK (success IN (0, 1)),
            report_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_dynamic_tool_test_runs_candidate
            ON dynamic_tool_test_runs (candidate_id, id);

        CREATE TABLE dynamic_tool_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL
                REFERENCES dynamic_tool_candidates (id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            message TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_dynamic_tool_events_candidate
            ON dynamic_tool_events (candidate_id, id);
        """,
    ),
    (
        4,
        """
        CREATE TABLE recovery_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
            source_task_id TEXT REFERENCES tasks (id),
            attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
            stage TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('started', 'succeeded', 'failed', 'retrying', 'skipped')
            ),
            category TEXT,
            action TEXT NOT NULL,
            retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1)),
            original_error TEXT,
            message TEXT,
            plan_before_json TEXT,
            plan_after_json TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE INDEX idx_recovery_attempts_task
            ON recovery_attempts (task_id, attempt_number, id);
        CREATE INDEX idx_recovery_attempts_created
            ON recovery_attempts (created_at, id);

        CREATE TABLE task_recovery_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_task_id TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
            recovery_task_id TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
            reason TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (source_task_id, recovery_task_id)
        );
        CREATE INDEX idx_task_recovery_links_source
            ON task_recovery_links (source_task_id, id);
        CREATE INDEX idx_task_recovery_links_recovery
            ON task_recovery_links (recovery_task_id, id);
        """,
    ),
    (
        5,
        """
        CREATE TABLE license_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE license_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            license_id TEXT,
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_license_events_created
            ON license_events (created_at, id);
        """,
    ),
    (
        6,
        """
        CREATE TABLE task_checkpoints (
            task_id TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
            step_index INTEGER NOT NULL CHECK (step_index >= 0),
            step_key TEXT NOT NULL,
            tool_id TEXT NOT NULL,
            tool_version TEXT NOT NULL,
            plan_fingerprint TEXT NOT NULL,
            output_json TEXT,
            artifacts_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (task_id, step_index)
        );

        CREATE TABLE task_cancellations (
            task_id TEXT PRIMARY KEY REFERENCES tasks (id) ON DELETE CASCADE,
            reason TEXT,
            requested_at TEXT NOT NULL
        );

        CREATE TABLE task_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
            passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
            report_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_task_verifications_task
            ON task_verifications (task_id, id);

        CREATE TABLE memory_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK (kind IN ('note', 'workflow')),
            key TEXT NOT NULL,
            title TEXT NOT NULL,
            content_json TEXT NOT NULL DEFAULT '{}',
            source_task_id TEXT REFERENCES tasks (id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (kind, key)
        );
        CREATE INDEX idx_memory_items_kind ON memory_items (kind, key);
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
