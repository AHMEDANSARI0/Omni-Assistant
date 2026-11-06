"""Durable connector ledger: events, deliveries and small state (Phase 13).

Three tables, all schema v15, and no schema changes after this phase:

* ``connector_events`` - one row per inbound item (calendar read, mail header,
  webhook, drive change) so the dashboard and flows can react to real data;
* ``connector_deliveries`` - one row per outbound attempt, success or failure,
  with byte count and duration so "did it work?" is answerable after the fact;
* ``connector_state`` - key/value scratch space (drive manifest, webhook replay
  window, last-run timestamps). Values are JSON and always bounded.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from ..storage.db import Database
from .base import bounded_text, utc_stamp

__all__ = ["MAX_STATE_VALUE", "ConnectorStore"]

MAX_STATE_VALUE = 1_000_000
MAX_REPLAY_KEYS = 400


def _parsed(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class ConnectorStore:
    """CRUD over the three connector tables."""

    def __init__(self, database: Database) -> None:
        self._db = database
        self._db.initialize()

    # -- events -------------------------------------------------------------
    def record_event(
        self,
        connector: str,
        kind: str,
        *,
        external_id: str = "",
        summary: str = "",
        payload: dict[str, Any] | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        event_id = uuid.uuid4().hex
        stamp = utc_stamp()
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO connector_events (id, connector, kind, external_id,"
                " summary, payload_json, received_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    bounded_text(connector, 40),
                    bounded_text(kind, 40) or "event",
                    bounded_text(external_id, 120),
                    bounded_text(summary, 300),
                    json.dumps(payload or {}, sort_keys=True)[:MAX_STATE_VALUE],
                    stamp,
                ),
            )
            total = connection.execute("SELECT COUNT(*) FROM connector_events").fetchone()[0]
            if total > limit:
                connection.execute(
                    "DELETE FROM connector_events WHERE id IN ("
                    " SELECT id FROM connector_events ORDER BY received_at LIMIT ?)",
                    (int(total) - int(limit),),
                )
        return {
            "id": event_id,
            "connector": connector,
            "kind": kind,
            "external_id": external_id,
            "summary": summary,
            "received_at": stamp,
        }

    def events(self, *, connector: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        cap = max(1, min(int(limit), 500))
        with self._db.connect() as connection:
            if connector:
                rows = connection.execute(
                    "SELECT * FROM connector_events WHERE connector = ?"
                    " ORDER BY received_at DESC, id LIMIT ?",
                    (bounded_text(connector, 40), cap),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM connector_events ORDER BY received_at DESC, id LIMIT ?",
                    (cap,),
                ).fetchall()
        return [
            {
                "id": row["id"],
                "connector": row["connector"],
                "kind": row["kind"],
                "external_id": row["external_id"],
                "summary": row["summary"],
                "payload": _parsed(row["payload_json"]),
                "received_at": row["received_at"],
            }
            for row in rows
        ]

    def seen_external_ids(self, connector: str, *, limit: int = MAX_REPLAY_KEYS) -> set[str]:
        cap = max(1, min(int(limit), MAX_REPLAY_KEYS))
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT kind, external_id FROM connector_events"
                " WHERE connector = ? AND external_id != ''"
                " ORDER BY received_at DESC LIMIT ?",
                (bounded_text(connector, 40), cap),
            ).fetchall()
        return {f"{row['kind']}:{row['external_id']}" for row in rows}

    # -- deliveries ---------------------------------------------------------
    def record_delivery(
        self,
        connector: str,
        target: str,
        status: str,
        *,
        detail: str = "",
        payload_bytes: int = 0,
        duration_ms: int = 0,
        limit: int = 500,
    ) -> dict[str, Any]:
        delivery_id = uuid.uuid4().hex
        stamp = utc_stamp()
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO connector_deliveries (id, connector, target, status,"
                " detail, payload_bytes, duration_ms, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    delivery_id,
                    bounded_text(connector, 40),
                    bounded_text(target, 200),
                    bounded_text(status, 20) or "unknown",
                    bounded_text(detail, 300),
                    max(0, int(payload_bytes)),
                    max(0, int(duration_ms)),
                    stamp,
                ),
            )
            total = connection.execute("SELECT COUNT(*) FROM connector_deliveries").fetchone()[0]
            if total > limit:
                connection.execute(
                    "DELETE FROM connector_deliveries WHERE id IN ("
                    " SELECT id FROM connector_deliveries ORDER BY created_at LIMIT ?)",
                    (int(total) - int(limit),),
                )
        return {
            "id": delivery_id,
            "connector": connector,
            "target": target,
            "status": status,
            "detail": detail,
            "payload_bytes": payload_bytes,
            "duration_ms": duration_ms,
            "created_at": stamp,
        }

    def deliveries(self, *, connector: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        cap = max(1, min(int(limit), 500))
        with self._db.connect() as connection:
            if connector:
                rows = connection.execute(
                    "SELECT * FROM connector_deliveries WHERE connector = ?"
                    " ORDER BY created_at DESC, id LIMIT ?",
                    (bounded_text(connector, 40), cap),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM connector_deliveries ORDER BY created_at DESC, id LIMIT ?",
                    (cap,),
                ).fetchall()
        return [dict(row) for row in rows]

    # -- state --------------------------------------------------------------
    def get_state(self, key: str, default: Any = None) -> Any:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT value FROM connector_state WHERE key = ?", (bounded_text(key, 120),)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def set_state(self, key: str, value: Any) -> None:
        payload = json.dumps(value, sort_keys=True)
        if len(payload) > MAX_STATE_VALUE:
            raise ValueError("connector state value is too large to store")
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO connector_state (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (bounded_text(key, 120), payload, utc_stamp()),
            )

    def state_keys(self) -> list[dict[str, Any]]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT key, value, updated_at FROM connector_state ORDER BY key"
            ).fetchall()
        return [
            {"key": row["key"], "bytes": len(row["value"]), "updated_at": row["updated_at"]}
            for row in rows
        ]

    # -- summary ------------------------------------------------------------
    def counts(self) -> dict[str, Any]:
        with self._db.connect() as connection:
            events = connection.execute("SELECT COUNT(*) FROM connector_events").fetchone()[0]
            deliveries = connection.execute("SELECT COUNT(*) FROM connector_deliveries").fetchone()[
                0
            ]
            failed = connection.execute(
                "SELECT COUNT(*) FROM connector_deliveries WHERE status != 'sent'"
            ).fetchone()[0]
            states = connection.execute("SELECT COUNT(*) FROM connector_state").fetchone()[0]
            last_event = connection.execute(
                "SELECT MAX(received_at) FROM connector_events"
            ).fetchone()[0]
            last_delivery = connection.execute(
                "SELECT MAX(created_at) FROM connector_deliveries"
            ).fetchone()[0]
        return {
            "events": int(events),
            "deliveries": int(deliveries),
            "deliveries_failed": int(failed),
            "state_keys": int(states),
            "last_event_at": last_event,
            "last_delivery_at": last_delivery,
        }
