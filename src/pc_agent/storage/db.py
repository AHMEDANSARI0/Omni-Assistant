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
LATEST_SCHEMA_VERSION = 18

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
    (
        7,
        """
        CREATE TABLE approval_center_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_key TEXT NOT NULL UNIQUE,
            task_id TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            tool_id TEXT NOT NULL,
            arguments_sha256 TEXT NOT NULL,
            read_roots_json TEXT NOT NULL DEFAULT '[]',
            summary TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'denied', 'expired', 'consumed')),
            decided_by TEXT,
            created_at TEXT NOT NULL,
            decided_at TEXT,
            expires_at TEXT NOT NULL,
            consumed_at TEXT
        );
        CREATE INDEX idx_approval_center_task
            ON approval_center_requests (task_id, id);
        CREATE INDEX idx_approval_center_status
            ON approval_center_requests (status, id);
        """,
    ),
    (
        8,
        """
        CREATE TABLE app_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            source TEXT NOT NULL,
            task_id TEXT REFERENCES tasks (id) ON DELETE SET NULL,
            summary TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_app_events_type ON app_events (event_type, id);
        CREATE INDEX idx_app_events_task ON app_events (task_id, id);
        """,
    ),
    (
        9,
        """
        CREATE TABLE missions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE TABLE mission_tasks (
            mission_id TEXT NOT NULL REFERENCES missions (id) ON DELETE CASCADE,
            task_id TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
            position INTEGER NOT NULL DEFAULT 0,
            added_at TEXT NOT NULL,
            PRIMARY KEY (mission_id, task_id)
        );
        CREATE TABLE mission_dependencies (
            mission_id TEXT NOT NULL REFERENCES missions (id) ON DELETE CASCADE,
            task_id TEXT NOT NULL,
            depends_on_task_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (mission_id, task_id, depends_on_task_id),
            FOREIGN KEY (mission_id, task_id)
                REFERENCES mission_tasks (mission_id, task_id) ON DELETE CASCADE,
            FOREIGN KEY (mission_id, depends_on_task_id)
                REFERENCES mission_tasks (mission_id, task_id) ON DELETE CASCADE
        );
        CREATE TABLE task_schedules (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            instruction TEXT NOT NULL,
            kind TEXT NOT NULL,
            run_at TEXT,
            interval_seconds INTEGER,
            next_run_at TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            use_ai INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_run_at TEXT,
            last_task_id TEXT,
            run_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_task_schedules_due ON task_schedules (enabled, next_run_at);
        """,
    ),
    (
        10,
        """
        ALTER TABLE memory_items ADD COLUMN embedding BLOB;
        ALTER TABLE memory_items ADD COLUMN embedding_model TEXT;
        ALTER TABLE memory_items ADD COLUMN tags TEXT NOT NULL DEFAULT '';

        CREATE TABLE knowledge_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL,
            source_path TEXT,
            kind TEXT NOT NULL,
            sha256 TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            characters INTEGER NOT NULL DEFAULT 0,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            embedding_model TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE knowledge_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL
                REFERENCES knowledge_documents (id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            text TEXT NOT NULL,
            vector BLOB NOT NULL,
            embedding_model TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (document_id, ordinal)
        );
        CREATE INDEX idx_knowledge_chunks_document
            ON knowledge_chunks (document_id, ordinal);
        CREATE INDEX idx_knowledge_chunks_model
            ON knowledge_chunks (embedding_model);
        """,
    ),
    (
        11,
        """
        -- Transformation Phase 9: specialized agents + Agent Studio ledger.
        CREATE TABLE agent_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            system_prompt TEXT NOT NULL DEFAULT '',
            allowed_tools TEXT NOT NULL DEFAULT '[]',
            provider TEXT NOT NULL DEFAULT '',
            max_steps INTEGER NOT NULL DEFAULT 5,
            token_budget INTEGER,
            builtin INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE agent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_run_id TEXT NOT NULL DEFAULT '',
            agent_name TEXT NOT NULL,
            task_id TEXT,
            instruction TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE INDEX idx_agent_runs_team ON agent_runs (team_run_id);
        CREATE INDEX idx_agent_runs_agent ON agent_runs (agent_name, id DESC);
        """,
    ),
    (
        12,
        """
        CREATE TABLE voice_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL DEFAULT 'push_to_talk',
            state TEXT NOT NULL DEFAULT 'completed',
            engine_stt TEXT NOT NULL DEFAULT '',
            engine_tts TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            ended_at TEXT,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            audio_seconds REAL NOT NULL DEFAULT 0,
            audio_retained INTEGER NOT NULL DEFAULT 0,
            audio_path TEXT,
            transcript TEXT,
            transcript_chars INTEGER NOT NULL DEFAULT 0,
            task_id TEXT,
            status TEXT NOT NULL DEFAULT 'ok',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_voice_sessions_created ON voice_sessions (id DESC);
        """,
    ),
    (
        13,
        """
        CREATE TABLE automation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            tool_id TEXT NOT NULL,
            action TEXT NOT NULL DEFAULT '',
            target TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'ok',
            detail TEXT NOT NULL DEFAULT '',
            artifact_path TEXT,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            allowed INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_automation_runs_created ON automation_runs (id DESC);
        CREATE INDEX idx_automation_runs_kind ON automation_runs (kind, id DESC);
        """,
    ),
    (
        14,
        """
        CREATE TABLE flow_definitions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 1,
            definition TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_flow_definitions_name ON flow_definitions (name);

        CREATE TABLE flow_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flow_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            definition TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (flow_id, version)
        );
        CREATE INDEX idx_flow_versions_flow ON flow_versions (flow_id, version);

        CREATE TABLE flow_triggers (
            id TEXT PRIMARY KEY,
            flow_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            interval_seconds INTEGER,
            run_at TEXT,
            event_type TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            next_run_at TEXT,
            last_fired_at TEXT,
            last_event_id INTEGER NOT NULL DEFAULT 0,
            run_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_flow_triggers_due ON flow_triggers (enabled, next_run_at);
        CREATE INDEX idx_flow_triggers_flow ON flow_triggers (flow_id);

        CREATE TABLE flow_runs (
            id TEXT PRIMARY KEY,
            flow_id TEXT NOT NULL,
            flow_name TEXT NOT NULL DEFAULT '',
            flow_version INTEGER NOT NULL,
            trigger_kind TEXT NOT NULL DEFAULT 'manual',
            trigger_id TEXT,
            mission_id TEXT,
            status TEXT NOT NULL,
            node_count INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE INDEX idx_flow_runs_flow ON flow_runs (flow_id, created_at);
        CREATE INDEX idx_flow_runs_status ON flow_runs (status, created_at);
        """,
    ),
    (
        15,
        """
        CREATE TABLE connector_events (
            id TEXT PRIMARY KEY,
            connector TEXT NOT NULL,
            kind TEXT NOT NULL,
            external_id TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            received_at TEXT NOT NULL
        );
        CREATE INDEX idx_connector_events_connector ON connector_events (connector, received_at);
        CREATE INDEX idx_connector_events_external ON connector_events (connector, external_id);

        CREATE TABLE connector_deliveries (
            id TEXT PRIMARY KEY,
            connector TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            payload_bytes INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_connector_deliveries_created ON connector_deliveries (created_at);
        CREATE INDEX idx_connector_deliveries_connector ON connector_deliveries (connector, created_at);

        CREATE TABLE connector_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        16,
        """
        CREATE TABLE reminders (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            due_at TEXT NOT NULL,
            repeat_seconds INTEGER,
            severity TEXT NOT NULL DEFAULT 'normal',
            enabled INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'open',
            source TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_fired_at TEXT,
            fired_count INTEGER NOT NULL DEFAULT 0,
            snoozed_until TEXT
        );
        CREATE INDEX idx_reminders_due ON reminders (status, enabled, due_at);

        CREATE TABLE notifications (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'normal',
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            channels TEXT NOT NULL DEFAULT 'in_app',
            status TEXT NOT NULL DEFAULT 'delivered',
            detail TEXT NOT NULL DEFAULT '',
            reminder_id TEXT,
            dedupe_key TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_notifications_created ON notifications (created_at);
        CREATE INDEX idx_notifications_dedupe ON notifications (dedupe_key, created_at);

        CREATE TABLE proactive_briefings (
            id TEXT PRIMARY KEY,
            generated_at TEXT NOT NULL,
            local_date TEXT NOT NULL,
            item_count INTEGER NOT NULL DEFAULT 0,
            sections_json TEXT NOT NULL DEFAULT '{}',
            delivered TEXT NOT NULL DEFAULT 'in_app',
            summary TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_briefings_date ON proactive_briefings (local_date);

        CREATE TABLE proactive_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        17,
        """
        CREATE TABLE plugins (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            publisher TEXT NOT NULL DEFAULT '',
            version TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            capabilities_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'installed',
            install_dir TEXT NOT NULL,
            checksum TEXT NOT NULL DEFAULT '',
            signature_key TEXT NOT NULL DEFAULT '',
            signature_status TEXT NOT NULL DEFAULT 'verified',
            tools_json TEXT NOT NULL DEFAULT '[]',
            skills_json TEXT NOT NULL DEFAULT '[]',
            file_count INTEGER NOT NULL DEFAULT 0,
            installed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            run_count INTEGER NOT NULL DEFAULT 0,
            last_run_at TEXT
        );
        CREATE INDEX idx_plugins_status ON plugins (status);

        CREATE TABLE plugin_versions (
            id TEXT PRIMARY KEY,
            plugin_id TEXT NOT NULL,
            version TEXT NOT NULL,
            manifest_json TEXT NOT NULL DEFAULT '{}',
            install_dir TEXT NOT NULL,
            checksum TEXT NOT NULL DEFAULT '',
            signature_key TEXT NOT NULL DEFAULT '',
            installed_at TEXT NOT NULL
        );
        CREATE INDEX idx_plugin_versions_plugin ON plugin_versions (plugin_id, installed_at);

        CREATE TABLE plugin_runs (
            id TEXT PRIMARY KEY,
            plugin_id TEXT NOT NULL,
            tool TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER NOT NULL DEFAULT 0,
            output_bytes INTEGER NOT NULL DEFAULT 0,
            sandbox TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_plugin_runs_created ON plugin_runs (created_at);
        CREATE INDEX idx_plugin_runs_plugin ON plugin_runs (plugin_id, created_at);

        CREATE TABLE plugin_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        18,
        """
        CREATE TABLE sync_devices (
            device_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            sign_public TEXT NOT NULL,
            encrypt_public TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'paired',
            paired_at TEXT NOT NULL,
            last_seen_at TEXT,
            bundle_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_sync_devices_status ON sync_devices (status);

        CREATE TABLE sync_bundles (
            id TEXT PRIMARY KEY,
            direction TEXT NOT NULL,
            peer TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            record_count INTEGER NOT NULL DEFAULT 0,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            seq INTEGER NOT NULL DEFAULT 0,
            path TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_sync_bundles_created ON sync_bundles (created_at);
        CREATE INDEX idx_sync_bundles_direction ON sync_bundles (direction, created_at);

        CREATE TABLE sync_requests (
            id TEXT PRIMARY KEY,
            device TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            detail TEXT NOT NULL DEFAULT '',
            bundle_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            decided_at TEXT
        );
        CREATE INDEX idx_sync_requests_status ON sync_requests (status, created_at);

        CREATE TABLE sync_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
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
