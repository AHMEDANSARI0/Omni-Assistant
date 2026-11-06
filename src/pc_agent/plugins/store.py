"""Durable plugin registry and run ledger (schema v17).

Four tables, all bounded:

* ``plugins`` - one row per installed plugin: the validated manifest summary,
  signature provenance, the active version and its status;
* ``plugin_versions`` - installed versions kept for rollback (max 5 each);
* ``plugin_runs`` - one row per sandboxed tool call with duration, bytes and the
  sandbox verdict (max 500 rows);
* ``plugin_state`` - small key/value scratch space (install policy, counters).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from ..storage.db import Database

__all__ = ["MAX_RUNS", "MAX_VERSIONS", "PluginStore"]

MAX_RUNS = 500
MAX_VERSIONS = 5
MAX_STATE_VALUE = 100_000


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _parsed(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


class PluginStore:
    """CRUD over the four plugin tables. No policy lives here."""

    def __init__(self, database: Database) -> None:
        self._db = database
        self._db.initialize()

    # -- install / uninstall ------------------------------------------------
    def upsert_plugin(
        self,
        *,
        manifest: dict[str, Any],
        version: str,
        install_dir: str,
        checksum: str,
        signature_key: str,
        signature_status: str,
        file_count: int,
    ) -> dict[str, Any]:
        plugin_id = str(manifest["id"])
        stamp = _now()
        with self._db.connect() as connection:
            existing = connection.execute(
                "SELECT id, installed_at FROM plugins WHERE id = ?", (plugin_id,)
            ).fetchone()
            values = (
                plugin_id,
                str(manifest["name"]),
                str(manifest["publisher"]),
                version,
                str(manifest.get("description", "")),
                json.dumps(manifest.get("capabilities", []), sort_keys=True),
                str(install_dir),
                checksum,
                signature_key,
                signature_status,
                json.dumps(manifest.get("tools", []), sort_keys=True),
                json.dumps(manifest.get("skills", []), sort_keys=True),
                file_count,
            )
            if existing:
                connection.execute(
                    "UPDATE plugins SET name = ?, publisher = ?, version = ?,"
                    " description = ?, capabilities_json = ?, install_dir = ?,"
                    " checksum = ?, signature_key = ?, signature_status = ?,"
                    " tools_json = ?, skills_json = ?, file_count = ?, updated_at = ?"
                    " WHERE id = ?",
                    (*values[1:], stamp, plugin_id),
                )
            else:
                connection.execute(
                    "INSERT INTO plugins (id, name, publisher, version, description,"
                    " capabilities_json, status, install_dir, checksum, signature_key,"
                    " signature_status, tools_json, skills_json, file_count,"
                    " installed_at, updated_at, run_count)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'installed', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (*values, stamp, stamp),
                )
        return self.get_plugin(plugin_id) or {}

    def get_plugin(self, plugin_id: str) -> dict[str, Any] | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM plugins WHERE id = ?", (plugin_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_plugins(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM plugins ORDER BY name LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    def set_status(self, plugin_id: str, status: str) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "UPDATE plugins SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), plugin_id),
            )
        return cursor.rowcount > 0

    def set_active_version(
        self, plugin_id: str, version: str, install_dir: str, checksum: str
    ) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "UPDATE plugins SET version = ?, install_dir = ?, checksum = ?,"
                " updated_at = ? WHERE id = ?",
                (version, install_dir, checksum, _now(), plugin_id),
            )
        return cursor.rowcount > 0

    def delete_plugin(self, plugin_id: str) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM plugins WHERE id = ?", (plugin_id,)
            )
            connection.execute(
                "DELETE FROM plugin_versions WHERE plugin_id = ?", (plugin_id,)
            )
        return cursor.rowcount > 0

    def count(self) -> dict[str, int]:
        with self._db.connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM plugins").fetchone()[0]
            enabled = connection.execute(
                "SELECT COUNT(*) FROM plugins WHERE status = 'enabled'"
            ).fetchone()[0]
            runs = connection.execute("SELECT COUNT(*) FROM plugin_runs").fetchone()[0]
        return {"plugins": int(total), "enabled": int(enabled), "runs": int(runs)}

    # -- versions -----------------------------------------------------------
    def add_version(
        self,
        *,
        plugin_id: str,
        version: str,
        manifest: dict[str, Any],
        install_dir: str,
        checksum: str,
        signature_key: str,
    ) -> dict[str, Any]:
        version_id = uuid.uuid4().hex
        stamp = _now()
        with self._db.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO plugin_versions (id, plugin_id, version,"
                " manifest_json, install_dir, checksum, signature_key, installed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version_id,
                    plugin_id,
                    version,
                    json.dumps(manifest, sort_keys=True),
                    install_dir,
                    checksum,
                    signature_key,
                    stamp,
                ),
            )
            stale = connection.execute(
                "SELECT id FROM plugin_versions WHERE plugin_id = ?"
                " ORDER BY installed_at DESC, rowid DESC LIMIT -1 OFFSET ?",
                (plugin_id, MAX_VERSIONS),
            ).fetchall()
            for row in stale:
                connection.execute(
                    "DELETE FROM plugin_versions WHERE id = ?", (row["id"],)
                )
        return {
            "id": version_id,
            "plugin_id": plugin_id,
            "version": version,
            "install_dir": install_dir,
            "checksum": checksum,
            "signature_key": signature_key,
            "installed_at": stamp,
        }

    def versions(self, plugin_id: str, *, limit: int = MAX_VERSIONS) -> list[dict[str, Any]]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM plugin_versions WHERE plugin_id = ?"
                " ORDER BY installed_at DESC, rowid DESC LIMIT ?",
                (plugin_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- runs ---------------------------------------------------------------
    def record_run(
        self,
        *,
        plugin_id: str,
        tool: str,
        status: str,
        detail: str = "",
        duration_ms: int = 0,
        output_bytes: int = 0,
        sandbox: str = "",
    ) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        stamp = _now()
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO plugin_runs (id, plugin_id, tool, status, detail,"
                " duration_ms, output_bytes, sandbox, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    plugin_id,
                    tool,
                    status,
                    detail[:500],
                    int(duration_ms),
                    int(output_bytes),
                    sandbox[:120],
                    stamp,
                ),
            )
            connection.execute(
                "UPDATE plugins SET run_count = run_count + 1, last_run_at = ?"
                " WHERE id = ? AND ? = 'ok'",
                (stamp, plugin_id, status),
            )
            connection.execute(
                "DELETE FROM plugin_runs WHERE id IN ("
                " SELECT id FROM plugin_runs ORDER BY created_at DESC, rowid DESC"
                " LIMIT -1 OFFSET ?)",
                (MAX_RUNS,),
            )
        return {
            "id": run_id,
            "plugin_id": plugin_id,
            "tool": tool,
            "status": status,
            "detail": detail[:500],
            "duration_ms": int(duration_ms),
            "output_bytes": int(output_bytes),
            "sandbox": sandbox,
            "created_at": stamp,
        }

    def runs(
        self, *, plugin_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self._db.connect() as connection:
            if plugin_id:
                rows = connection.execute(
                    "SELECT * FROM plugin_runs WHERE plugin_id = ?"
                    " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (plugin_id, int(limit)),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM plugin_runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        return [dict(row) for row in rows]

    # -- state --------------------------------------------------------------
    def get_state(self, key: str, default: Any = None) -> Any:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT value FROM plugin_state WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        return _parsed(row["value"], default)

    def set_state(self, key: str, value: Any) -> None:
        payload = json.dumps(value, sort_keys=True)
        if len(payload) > MAX_STATE_VALUE:
            raise ValueError("plugin state value is too large")
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO plugin_state (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (key, payload, _now()),
            )
