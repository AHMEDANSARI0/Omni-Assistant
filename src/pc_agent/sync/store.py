"""Durable sync state: peers, bundle ledger, coordination requests (schema v18).

Four tables, all bounded:

* ``sync_devices`` - paired peers (public keys only, never a private key);
* ``sync_bundles`` - one row per exported or imported bundle with the verdict;
* ``sync_requests`` - coordination requests a peer sent, awaiting a local decision;
* ``sync_state`` - small key/value scratch space (per-peer sequence numbers,
  last export time, applied record counters).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from ..storage.db import Database

__all__ = ["MAX_BUNDLE_ROWS", "MAX_REQUEST_ROWS", "SyncStore"]

MAX_BUNDLE_ROWS = 400
MAX_REQUEST_ROWS = 200
MAX_STATE_VALUE = 100_000
MAX_JSON = 200_000


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _parsed(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


class SyncStore:
    """CRUD over the four sync tables. Policy lives in the service."""

    def __init__(self, database: Database) -> None:
        self._db = database
        self._db.initialize()

    # -- devices ------------------------------------------------------------
    def upsert_device(
        self,
        *,
        device_id: str,
        name: str,
        sign_public: str,
        encrypt_public: str,
        fingerprint: str,
        status: str = "paired",
    ) -> dict[str, Any]:
        stamp = _now()
        with self._db.connect() as connection:
            existing = connection.execute(
                "SELECT device_id FROM sync_devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE sync_devices SET name = ?, sign_public = ?,"
                    " encrypt_public = ?, fingerprint = ?, status = ?, paired_at = ?"
                    " WHERE device_id = ?",
                    (name, sign_public, encrypt_public, fingerprint, status, stamp, device_id),
                )
            else:
                connection.execute(
                    "INSERT INTO sync_devices (device_id, name, sign_public,"
                    " encrypt_public, fingerprint, status, paired_at, bundle_count)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                    (device_id, name, sign_public, encrypt_public, fingerprint, status, stamp),
                )
        return self.get_device(device_id) or {}

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sync_devices WHERE device_id = ?", (device_id,)
            ).fetchone()
        return dict(row) if row else None

    def devices(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sync_devices ORDER BY name LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    def set_device_status(self, device_id: str, status: str) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "UPDATE sync_devices SET status = ? WHERE device_id = ?", (status, device_id)
            )
        return cursor.rowcount > 0

    def delete_device(self, device_id: str) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM sync_devices WHERE device_id = ?", (device_id,)
            )
        return cursor.rowcount > 0

    def touch_device(self, device_id: str) -> None:
        with self._db.connect() as connection:
            connection.execute(
                "UPDATE sync_devices SET last_seen_at = ?, bundle_count = bundle_count + 1"
                " WHERE device_id = ?",
                (_now(), device_id),
            )

    # -- bundles ------------------------------------------------------------
    def record_bundle(
        self,
        *,
        bundle_id: str,
        direction: str,
        peer: str,
        status: str,
        detail: str = "",
        record_count: int = 0,
        size_bytes: int = 0,
        seq: int = 0,
        path: str = "",
    ) -> dict[str, Any]:
        stamp = _now()
        with self._db.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO sync_bundles (id, direction, peer, status,"
                " detail, record_count, size_bytes, seq, path, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bundle_id,
                    direction,
                    peer,
                    status,
                    detail[:500],
                    int(record_count),
                    int(size_bytes),
                    int(seq),
                    path[:400],
                    stamp,
                ),
            )
            connection.execute(
                "DELETE FROM sync_bundles WHERE id IN ("
                " SELECT id FROM sync_bundles ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
                (MAX_BUNDLE_ROWS,),
            )
        return {
            "bundle_id": bundle_id,
            "direction": direction,
            "peer": peer,
            "status": status,
            "detail": detail[:500],
            "record_count": int(record_count),
            "size_bytes": int(size_bytes),
            "seq": int(seq),
            "path": path,
            "created_at": stamp,
        }

    def bundle_seen(self, bundle_id: str) -> bool:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM sync_bundles WHERE id = ?", (bundle_id,)
            ).fetchone()
        return row is not None

    def bundles(self, *, direction: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        with self._db.connect() as connection:
            if direction:
                rows = connection.execute(
                    "SELECT * FROM sync_bundles WHERE direction = ?"
                    " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (direction, int(limit)),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM sync_bundles ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        return [dict(row) for row in rows]

    # -- coordination requests ---------------------------------------------
    def add_request(
        self,
        *,
        request_id: str,
        device: str,
        kind: str,
        payload: dict[str, Any],
        bundle_id: str,
    ) -> dict[str, Any]:
        stamp = _now()
        payload_json = json.dumps(payload, sort_keys=True)
        if len(payload_json) > MAX_JSON:
            raise ValueError("coordination request payload is too large")
        with self._db.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO sync_requests (id, device, kind, payload_json,"
                " status, bundle_id, created_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (request_id, device, kind, payload_json, bundle_id, stamp),
            )
            connection.execute(
                "DELETE FROM sync_requests WHERE id IN ("
                " SELECT id FROM sync_requests ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
                (MAX_REQUEST_ROWS,),
            )
        return self.get_request(request_id) or {}

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sync_requests WHERE id = ?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["payload"] = _parsed(record.get("payload_json"), {})
        return record

    def requests(
        self, *, status: str | None = None, limit: int = 25
    ) -> list[dict[str, Any]]:
        with self._db.connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM sync_requests WHERE status = ?"
                    " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (status, int(limit)),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM sync_requests ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            record["payload"] = _parsed(record.get("payload_json"), {})
            records.append(record)
        return records

    def decide_request(self, request_id: str, *, status: str, detail: str = "") -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "UPDATE sync_requests SET status = ?, detail = ?, decided_at = ?"
                " WHERE id = ? AND status = 'pending'",
                (status, detail[:500], _now(), request_id),
            )
        return cursor.rowcount > 0

    # -- state --------------------------------------------------------------
    def get_state(self, key: str, default: Any = None) -> Any:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT value FROM sync_state WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        return _parsed(row["value"], default)

    def set_state(self, key: str, value: Any) -> None:
        payload = json.dumps(value, sort_keys=True)
        if len(payload) > MAX_STATE_VALUE:
            raise ValueError("sync state value is too large")
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (key, payload, _now()),
            )

    def counts(self) -> dict[str, int]:
        with self._db.connect() as connection:
            devices = connection.execute(
                "SELECT COUNT(*) FROM sync_devices WHERE status = 'paired'"
            ).fetchone()[0]
            bundles = connection.execute("SELECT COUNT(*) FROM sync_bundles").fetchone()[0]
            pending = connection.execute(
                "SELECT COUNT(*) FROM sync_requests WHERE status = 'pending'"
            ).fetchone()[0]
        return {"peers": int(devices), "bundles": int(bundles), "pending_requests": int(pending)}

    def new_id(self) -> str:
        return uuid.uuid4().hex
