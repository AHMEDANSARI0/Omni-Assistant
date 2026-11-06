"""Visual Workflow Builder (Transformation Phase 12).

A *flow* is a small, versioned, user-authored graph of instruction nodes. It is
never a second executor: a flow is compiled into ordinary Mission Center tasks
with dependencies, so every node runs through the same planner, Safety Kernel,
approval model, budgets, verification and recovery as a hand-written mission.

Three pieces live here:

* :func:`validate_definition` — strict, bounded validation of the JSON that the
  GUI/CLI/API/import path all share. Unknown keys are refused, limits are
  explicit, and ``{{variable}}`` placeholders must resolve.
* :class:`FlowStore` — persistence (definitions, immutable versions, triggers,
  runs) on top of the SQLite schema v14 tables.
* :class:`FlowRunner` — compiles a definition into a mission, starts it through
  an injected ``start_mission`` callable, and records the run. Triggers
  (manual / schedule / event) are evaluated by :meth:`FlowRunner.tick`.

Nothing here opens a network connection, spawns a shell, or invents a step: an
invalid flow raises :class:`FlowError` with the exact problems listed.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .domain.task import utc_now
from .errors import FlowError
from .logging_config import redact_text
from .storage.db import Database

__all__ = [
    "FLOW_SCHEMA",
    "LIMITS",
    "FlowDefinition",
    "FlowRecord",
    "FlowRunRecord",
    "FlowRunner",
    "FlowStore",
    "FlowTriggerRecord",
    "definition_checksum",
    "export_payload",
    "validate_definition",
]

FLOW_SCHEMA = 1
"""Version of the portable flow document format (import/export)."""

LIMITS = {
    "max_nodes": 20,
    "max_variables": 8,
    "max_name": 120,
    "max_description": 2000,
    "max_title": 80,
    "max_instruction": 1000,
    "max_note": 300,
    "max_variable_length": 300,
    "max_import_bytes": 64_000,
    "min_schedule_seconds": 60,
    "max_schedule_seconds": 30 * 24 * 3600,
    "max_triggers_per_flow": 8,
    "max_runs_listed": 200,
}

_ALLOWED_FLOW_KEYS = {"flow_schema", "name", "description", "variables", "nodes", "edges"}
_ALLOWED_NODE_KEYS = {"id", "title", "instruction", "note"}
_ALLOWED_EDGE_KEYS = {"from", "to"}
_ALLOWED_TRIGGER_KEYS = {
    "kind",
    "interval_seconds",
    "run_at",
    "event_type",
    "enabled",
}
_ALLOWED_EXPORT_KEYS = {"flow_schema", "exported_by", "flow", "triggers", "checksum"}
_NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_VARIABLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]{1,32})\s*\}\}")
EVENT_TYPES_HINT = "task.completed, task.failed, mission.completed, schedule.triggered"


# --------------------------------------------------------------------------- #
# Definition model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class FlowNode:
    id: str
    instruction: str
    title: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "instruction": self.instruction,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class FlowDefinition:
    name: str
    nodes: tuple[FlowNode, ...]
    edges: tuple[tuple[str, str], ...] = ()
    variables: tuple[tuple[str, str], ...] = ()
    description: str = ""
    schema: int = FLOW_SCHEMA

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(node.id for node in self.nodes)

    def dependencies(self) -> dict[str, list[str]]:
        graph: dict[str, list[str]] = {node.id: [] for node in self.nodes}
        for source, target in self.edges:
            graph[target].append(source)
        return graph

    def topological_levels(self) -> list[list[str]]:
        """Kahn's algorithm; raises :class:`FlowError` on a cycle."""
        graph = self.dependencies()
        placed: set[str] = set()
        levels: list[list[str]] = []
        remaining = {key: set(value) for key, value in graph.items()}
        while remaining:
            ready = sorted(key for key, deps in remaining.items() if deps <= placed)
            if not ready:
                raise FlowError("The flow graph contains a cycle.")
            levels.append(ready)
            placed.update(ready)
            for key in ready:
                remaining.pop(key)
        return levels

    def to_dict(self) -> dict[str, Any]:
        return {
            "flow_schema": self.schema,
            "name": self.name,
            "description": self.description,
            "variables": dict(self.variables),
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [{"from": a, "to": b} for a, b in self.edges],
        }

    def mission_specs(self) -> list[dict[str, Any]]:
        """Compile to Mission Center task specs (key + instruction + depends_on)."""
        return [
            {
                "key": node.id,
                "instruction": node.instruction,
                "depends_on": list(self.dependencies().get(node.id, [])),
            }
            for node in self.nodes
        ]


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def definition_checksum(flow: Mapping[str, Any], triggers: list[Any] | None = None) -> str:
    body = {"flow": dict(flow), "triggers": list(triggers or [])}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def _require_text(value: Any, *, field: str, limit: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise FlowError(f"{field} must be text.")
    text = value.strip()
    if not text and not allow_empty:
        raise FlowError(f"{field} must not be empty.")
    if len(text) > limit:
        raise FlowError(f"{field} is longer than {limit} characters.")
    return text


def _check_unknown_keys(payload: Mapping[str, Any], allowed: set[str], *, field: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise FlowError(f"{field} contains unknown field(s): {', '.join(unknown)}.")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_definition(payload: Mapping[str, Any], *, path: str = "flow") -> FlowDefinition:
    """Validate a flow document and return the typed definition.

    Every problem is reported with the field that caused it. The function is
    intentionally strict: an unversioned or misspelled document is refused
    rather than silently interpreted.
    """
    if not isinstance(payload, Mapping):
        raise FlowError(f"{path} must be a JSON object.")
    _check_unknown_keys(payload, _ALLOWED_FLOW_KEYS, field=path)
    raw_schema = payload.get("flow_schema", FLOW_SCHEMA)
    if raw_schema != FLOW_SCHEMA:
        raise FlowError(
            f"{path}.flow_schema must be {FLOW_SCHEMA} (received {raw_schema!r})."
        )
    name = _require_text(payload.get("name"), field=f"{path}.name", limit=LIMITS["max_name"])
    description = _require_text(
        payload.get("description", ""),
        field=f"{path}.description",
        limit=LIMITS["max_description"],
        allow_empty=True,
    )

    raw_variables = payload.get("variables", {})
    if not isinstance(raw_variables, Mapping):
        raise FlowError(f"{path}.variables must be an object.")
    if len(raw_variables) > LIMITS["max_variables"]:
        raise FlowError(
            f"{path}.variables has more than {LIMITS['max_variables']} entries."
        )
    variables: list[tuple[str, str]] = []
    for key, value in raw_variables.items():
        if not isinstance(key, str) or not _VARIABLE_RE.match(key):
            raise FlowError(
                f"{path}.variables has an invalid name {key!r} "
                "(lowercase letters, digits and _ only, starting with a letter)."
            )
        if not isinstance(value, str):
            raise FlowError(f"{path}.variables.{key} must be text.")
        if len(value) > LIMITS["max_variable_length"]:
            raise FlowError(
                f"{path}.variables.{key} is longer than "
                f"{LIMITS['max_variable_length']} characters."
            )
        variables.append((key, value))

    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise FlowError(f"{path}.nodes must be a non-empty list.")
    if len(raw_nodes) > LIMITS["max_nodes"]:
        raise FlowError(f"{path}.nodes has more than {LIMITS['max_nodes']} nodes.")

    variables_map = dict(variables)
    nodes: list[FlowNode] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_nodes):
        where = f"{path}.nodes[{index}]"
        if not isinstance(raw, Mapping):
            raise FlowError(f"{where} must be an object.")
        _check_unknown_keys(raw, _ALLOWED_NODE_KEYS, field=where)
        node_id = _require_text(raw.get("id"), field=f"{where}.id", limit=32)
        if not _NODE_ID_RE.match(node_id):
            raise FlowError(
                f"{where}.id must be lowercase letters, digits, - or _ (max 32)."
            )
        if node_id in seen:
            raise FlowError(f"{where}.id duplicates node id {node_id!r}.")
        seen.add(node_id)
        title = _require_text(
            raw.get("title", ""),
            field=f"{where}.title",
            limit=LIMITS["max_title"],
            allow_empty=True,
        )
        note = _require_text(
            raw.get("note", ""),
            field=f"{where}.note",
            limit=LIMITS["max_note"],
            allow_empty=True,
        )
        instruction = _require_text(
            raw.get("instruction"),
            field=f"{where}.instruction",
            limit=LIMITS["max_instruction"],
        )
        for match in _PLACEHOLDER_RE.findall(instruction):
            if match not in variables_map:
                raise FlowError(
                    f"{where}.instruction uses unknown variable {{{{{match}}}}}; "
                    f"declare it in variables."
                )
        instruction = _PLACEHOLDER_RE.sub(
            lambda item: variables_map[item.group(1)], instruction
        )
        nodes.append(FlowNode(id=node_id, instruction=instruction, title=title, note=note))

    raw_edges = payload.get("edges", [])
    if not isinstance(raw_edges, list):
        raise FlowError(f"{path}.edges must be a list.")
    edges: list[tuple[str, str]] = []
    edge_seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_edges):
        where = f"{path}.edges[{index}]"
        if not isinstance(raw, Mapping):
            raise FlowError(f"{where} must be an object.")
        _check_unknown_keys(raw, _ALLOWED_EDGE_KEYS, field=where)
        source = _require_text(raw.get("from"), field=f"{where}.from", limit=32)
        target = _require_text(raw.get("to"), field=f"{where}.to", limit=32)
        if source == target:
            raise FlowError(f"{where} links {source!r} to itself.")
        for item in (source, target):
            if item not in seen:
                raise FlowError(f"{where} references unknown node {item!r}.")
        if (source, target) in edge_seen:
            raise FlowError(f"{where} duplicates edge {source}->{target}.")
        edge_seen.add((source, target))
        edges.append((source, target))

    definition = FlowDefinition(
        name=name,
        description=description,
        nodes=tuple(nodes),
        edges=tuple(edges),
        variables=tuple(variables),
    )
    definition.topological_levels()  # raises FlowError on a cycle
    return definition


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class FlowRecord:
    id: str
    name: str
    description: str
    version: int
    definition: FlowDefinition
    sha256: str
    enabled: bool
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "enabled": self.enabled,
            "sha256": self.sha256,
            "node_count": len(self.definition.nodes),
            "levels": self.definition.topological_levels(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "flow": self.definition.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class FlowTriggerRecord:
    id: str
    flow_id: str
    kind: str
    interval_seconds: int | None
    run_at: datetime | None
    event_type: str
    enabled: bool
    next_run_at: datetime | None
    last_fired_at: datetime | None
    last_event_id: int
    run_count: int
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "flow_id": self.flow_id,
            "kind": self.kind,
            "interval_seconds": self.interval_seconds,
            "run_at": self.run_at.isoformat() if self.run_at else None,
            "event_type": self.event_type,
            "enabled": self.enabled,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "last_fired_at": self.last_fired_at.isoformat() if self.last_fired_at else None,
            "run_count": self.run_count,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class FlowRunRecord:
    id: str
    flow_id: str
    flow_name: str
    flow_version: int
    trigger_kind: str
    trigger_id: str | None
    mission_id: str | None
    status: str
    node_count: int
    error: str
    created_at: datetime
    finished_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "flow_id": self.flow_id,
            "flow_name": self.flow_name,
            "flow_version": self.flow_version,
            "trigger_kind": self.trigger_kind,
            "trigger_id": self.trigger_id,
            "mission_id": self.mission_id,
            "status": self.status,
            "node_count": self.node_count,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    from .domain.task import parse_iso_datetime

    try:
        return parse_iso_datetime(str(value))
    except Exception as exc:
        raise FlowError(f"Stored timestamp {value!r} is not valid.") from exc


def _flow_from_row(row: Any) -> FlowRecord:
    payload = json.loads(row["definition"])
    return FlowRecord(
        id=str(row["id"]),
        name=str(row["name"]),
        description=str(row["description"]),
        version=int(row["version"]),
        definition=validate_definition(payload),
        sha256=str(row["sha256"]),
        enabled=bool(row["enabled"]),
        created_at=_parse_dt(row["created_at"]) or utc_now(),
        updated_at=_parse_dt(row["updated_at"]) or utc_now(),
    )


def _trigger_from_row(row: Any) -> FlowTriggerRecord:
    return FlowTriggerRecord(
        id=str(row["id"]),
        flow_id=str(row["flow_id"]),
        kind=str(row["kind"]),
        interval_seconds=(
            int(row["interval_seconds"]) if row["interval_seconds"] is not None else None
        ),
        run_at=_parse_dt(row["run_at"]),
        event_type=str(row["event_type"] or ""),
        enabled=bool(row["enabled"]),
        next_run_at=_parse_dt(row["next_run_at"]),
        last_fired_at=_parse_dt(row["last_fired_at"]),
        last_event_id=int(row["last_event_id"] or 0),
        run_count=int(row["run_count"] or 0),
        created_at=_parse_dt(row["created_at"]) or utc_now(),
    )


def _run_from_row(row: Any) -> FlowRunRecord:
    return FlowRunRecord(
        id=str(row["id"]),
        flow_id=str(row["flow_id"]),
        flow_name=str(row["flow_name"]),
        flow_version=int(row["flow_version"]),
        trigger_kind=str(row["trigger_kind"]),
        trigger_id=str(row["trigger_id"]) if row["trigger_id"] else None,
        mission_id=str(row["mission_id"]) if row["mission_id"] else None,
        status=str(row["status"]),
        node_count=int(row["node_count"]),
        error=str(row["error"] or ""),
        created_at=_parse_dt(row["created_at"]) or utc_now(),
        finished_at=_parse_dt(row["finished_at"]),
    )


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
class FlowStore:
    """Definitions, immutable versions, triggers and runs (schema v14)."""

    def __init__(self, database: Database) -> None:
        self._db = database

    # -- definitions ---------------------------------------------------------
    def create(self, definition: FlowDefinition) -> FlowRecord:
        payload = definition.to_dict()
        data = json.dumps(payload, sort_keys=True)
        digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
        flow_id = uuid.uuid4().hex
        now = utc_now().isoformat()
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO flow_definitions (id, name, description, version,"
                " definition, sha256, enabled, created_at, updated_at)"
                " VALUES (?, ?, ?, 1, ?, ?, 1, ?, ?)",
                (flow_id, definition.name, definition.description, data, digest, now, now),
            )
            connection.execute(
                "INSERT INTO flow_versions (flow_id, version, definition, sha256,"
                " created_at) VALUES (?, 1, ?, ?, ?)",
                (flow_id, data, digest, now),
            )
        record = self.get(flow_id)
        assert record is not None
        return record

    def get(self, flow_id: str) -> FlowRecord | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM flow_definitions WHERE id = ?", (flow_id,)
            ).fetchone()
        return _flow_from_row(row) if row is not None else None

    def find_by_name(self, name: str) -> FlowRecord | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM flow_definitions WHERE name = ?", (name.strip(),)
            ).fetchone()
        return _flow_from_row(row) if row is not None else None

    def list_flows(self, *, limit: int = 100) -> list[FlowRecord]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM flow_definitions ORDER BY updated_at DESC, name"
                " LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [_flow_from_row(row) for row in rows]

    def update(self, flow_id: str, definition: FlowDefinition) -> FlowRecord:
        current = self.get(flow_id)
        if current is None:
            raise FlowError(f"Flow not found: {flow_id}")
        payload = definition.to_dict()
        data = json.dumps(payload, sort_keys=True)
        digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
        version = current.version + 1
        now = utc_now().isoformat()
        with self._db.connect() as connection:
            connection.execute(
                "UPDATE flow_definitions SET name = ?, description = ?, version = ?,"
                " definition = ?, sha256 = ?, updated_at = ? WHERE id = ?",
                (definition.name, definition.description, version, data, digest, now, flow_id),
            )
            connection.execute(
                "INSERT INTO flow_versions (flow_id, version, definition, sha256,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (flow_id, version, data, digest, now),
            )
        record = self.get(flow_id)
        assert record is not None
        return record

    def set_enabled(self, flow_id: str, enabled: bool) -> FlowRecord:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "UPDATE flow_definitions SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, utc_now().isoformat(), flow_id),
            )
            if cursor.rowcount != 1:
                raise FlowError(f"Flow not found: {flow_id}")
        record = self.get(flow_id)
        assert record is not None
        return record

    def delete(self, flow_id: str) -> bool:
        with self._db.connect() as connection:
            connection.execute("DELETE FROM flow_triggers WHERE flow_id = ?", (flow_id,))
            cursor = connection.execute(
                "DELETE FROM flow_definitions WHERE id = ?", (flow_id,)
            )
            return cursor.rowcount == 1

    def versions(self, flow_id: str) -> list[dict[str, Any]]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT version, sha256, created_at FROM flow_versions WHERE flow_id = ?"
                " ORDER BY version DESC",
                (flow_id,),
            ).fetchall()
        return [
            {
                "version": int(row["version"]),
                "sha256": str(row["sha256"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def count(self) -> int:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM flow_definitions"
            ).fetchone()
        return int(row["total"])

    # -- triggers ------------------------------------------------------------
    def add_trigger(
        self,
        flow_id: str,
        *,
        kind: str,
        interval_seconds: int | None = None,
        run_at: datetime | None = None,
        event_type: str = "",
        enabled: bool = True,
    ) -> FlowTriggerRecord:
        if self.get(flow_id) is None:
            raise FlowError(f"Flow not found: {flow_id}")
        if kind not in {"manual", "schedule", "event"}:
            raise FlowError("trigger kind must be manual, schedule or event.")
        if len(self.triggers(flow_id)) >= LIMITS["max_triggers_per_flow"]:
            raise FlowError(
                f"A flow can have at most {LIMITS['max_triggers_per_flow']} triggers."
            )
        next_run: datetime | None = None
        if kind == "schedule":
            if interval_seconds is None and run_at is None:
                raise FlowError("A schedule trigger needs interval_seconds or run_at.")
            if interval_seconds is not None:
                if not LIMITS["min_schedule_seconds"] <= int(interval_seconds) <= LIMITS["max_schedule_seconds"]:
                    raise FlowError(
                        "interval_seconds must be between "
                        f"{LIMITS['min_schedule_seconds']} and "
                        f"{LIMITS['max_schedule_seconds']}."
                    )
                next_run = utc_now() + timedelta(seconds=int(interval_seconds))
            else:
                if run_at is None or run_at <= utc_now():
                    raise FlowError("run_at must be a future timestamp.")
                next_run = run_at
        clean_event = event_type.strip()
        if kind == "event":
            if not clean_event:
                raise FlowError("An event trigger needs event_type.")
            if len(clean_event) > 80 or not re.match(r"^[a-z][a-z0-9._-]{1,79}$", clean_event):
                raise FlowError(f"event_type looks invalid: {clean_event!r}.")
        trigger_id = uuid.uuid4().hex
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO flow_triggers (id, flow_id, kind, interval_seconds,"
                " run_at, event_type, enabled, next_run_at, last_fired_at,"
                " last_event_id, run_count, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 0, ?)",
                (
                    trigger_id,
                    flow_id,
                    kind,
                    int(interval_seconds) if interval_seconds is not None else None,
                    run_at.isoformat() if run_at else None,
                    clean_event,
                    1 if enabled else 0,
                    next_run.isoformat() if next_run else None,
                    utc_now().isoformat(),
                ),
            )
        record = self.get_trigger(trigger_id)
        assert record is not None
        return record

    def get_trigger(self, trigger_id: str) -> FlowTriggerRecord | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM flow_triggers WHERE id = ?", (trigger_id,)
            ).fetchone()
        return _trigger_from_row(row) if row is not None else None

    def triggers(self, flow_id: str | None = None) -> list[FlowTriggerRecord]:
        query = "SELECT * FROM flow_triggers"
        params: tuple[Any, ...] = ()
        if flow_id is not None:
            query += " WHERE flow_id = ?"
            params = (flow_id,)
        query += " ORDER BY created_at, id"
        with self._db.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_trigger_from_row(row) for row in rows]

    def set_trigger_enabled(self, trigger_id: str, enabled: bool) -> FlowTriggerRecord:
        trigger = self.get_trigger(trigger_id)
        if trigger is None:
            raise FlowError(f"Trigger not found: {trigger_id}")
        next_run = trigger.next_run_at
        if enabled and trigger.kind == "schedule" and next_run is None:
            next_run = utc_now() + timedelta(seconds=int(trigger.interval_seconds or 60))
        with self._db.connect() as connection:
            connection.execute(
                "UPDATE flow_triggers SET enabled = ?, next_run_at = ? WHERE id = ?",
                (
                    1 if enabled else 0,
                    next_run.isoformat() if next_run else None,
                    trigger_id,
                ),
            )
        record = self.get_trigger(trigger_id)
        assert record is not None
        return record

    def delete_trigger(self, trigger_id: str) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM flow_triggers WHERE id = ?", (trigger_id,)
            )
            return cursor.rowcount == 1

    def due_triggers(self, *, now: datetime | None = None) -> list[FlowTriggerRecord]:
        moment = now or utc_now()
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM flow_triggers WHERE enabled = 1 AND kind = 'schedule'"
                " AND next_run_at IS NOT NULL AND next_run_at <= ?"
                " ORDER BY next_run_at, id LIMIT 50",
                (moment.isoformat(),),
            ).fetchall()
        return [_trigger_from_row(row) for row in rows]

    def pending_events(
        self, trigger: FlowTriggerRecord, *, limit: int = 5
    ) -> list[dict[str, Any]]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT id, event_type, summary, created_at FROM app_events"
                " WHERE id > ? AND event_type = ? ORDER BY id LIMIT ?",
                (trigger.last_event_id, trigger.event_type, max(1, min(limit, 50))),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_trigger_fired(
        self,
        trigger_id: str,
        *,
        next_run_at: datetime | None,
        last_event_id: int | None = None,
        event_consumed: bool = False,
        disable: bool = False,
    ) -> FlowTriggerRecord:
        trigger = self.get_trigger(trigger_id)
        if trigger is None:
            raise FlowError(f"Trigger not found: {trigger_id}")
        with self._db.connect() as connection:
            connection.execute(
                "UPDATE flow_triggers SET last_fired_at = ?, run_count = run_count + 1,"
                " next_run_at = ?, last_event_id = ?, enabled = ? WHERE id = ?",
                (
                    utc_now().isoformat(),
                    next_run_at.isoformat() if next_run_at else None,
                    (
                        int(last_event_id)
                        if last_event_id is not None
                        else trigger.last_event_id
                    ),
                    0 if disable else (1 if trigger.enabled else 0),
                    trigger_id,
                ),
            )
            del event_consumed
        record = self.get_trigger(trigger_id)
        assert record is not None
        return record

    # -- runs ----------------------------------------------------------------
    def create_run(
        self,
        *,
        flow_id: str,
        flow_name: str,
        flow_version: int,
        trigger_kind: str,
        trigger_id: str | None,
        node_count: int,
    ) -> FlowRunRecord:
        run_id = uuid.uuid4().hex
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO flow_runs (id, flow_id, flow_name, flow_version,"
                " trigger_kind, trigger_id, mission_id, status, node_count, error,"
                " created_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, NULL, 'running',"
                " ?, '', ?, NULL)",
                (
                    run_id,
                    flow_id,
                    flow_name,
                    flow_version,
                    trigger_kind,
                    trigger_id,
                    node_count,
                    utc_now().isoformat(),
                ),
            )
        record = self.get_run(run_id)
        assert record is not None
        return record

    def finish_run(
        self, run_id: str, *, status: str, mission_id: str | None, error: str = ""
    ) -> FlowRunRecord:
        with self._db.connect() as connection:
            connection.execute(
                "UPDATE flow_runs SET status = ?, mission_id = COALESCE(?, mission_id),"
                " error = ?, finished_at = ? WHERE id = ?",
                (
                    status,
                    mission_id,
                    redact_text(error)[:500],
                    utc_now().isoformat(),
                    run_id,
                ),
            )
        record = self.get_run(run_id)
        assert record is not None
        return record

    def get_run(self, run_id: str) -> FlowRunRecord | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM flow_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return _run_from_row(row) if row is not None else None

    def list_runs(
        self, *, flow_id: str | None = None, limit: int = 50
    ) -> list[FlowRunRecord]:
        query = "SELECT * FROM flow_runs"
        params: list[Any] = []
        if flow_id is not None:
            query += " WHERE flow_id = ?"
            params.append(flow_id)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), LIMITS["max_runs_listed"])))
        with self._db.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [_run_from_row(row) for row in rows]

    def has_active_run(self, flow_id: str) -> bool:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM flow_runs WHERE flow_id = ?"
                " AND status IN ('running', 'started')",
                (flow_id,),
            ).fetchone()
        return int(row["total"]) > 0

    def summary(self) -> dict[str, Any]:
        with self._db.connect() as connection:
            totals = connection.execute(
                "SELECT COUNT(*) AS flows FROM flow_definitions"
            ).fetchone()
            triggers = connection.execute(
                "SELECT COUNT(*) AS total FROM flow_triggers WHERE enabled = 1"
            ).fetchone()
            runs = connection.execute(
                "SELECT status, COUNT(*) AS total FROM flow_runs GROUP BY status"
            ).fetchall()
            last = connection.execute(
                "SELECT created_at FROM flow_runs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        by_status = {str(row["status"]): int(row["total"]) for row in runs}
        return {
            "flows": int(totals["flows"]),
            "total": int(totals["flows"]),
            "triggers_enabled": int(triggers["total"]),
            "runs": sum(by_status.values()),
            "by_status": by_status,
            "last_run_at": str(last["created_at"]) if last is not None else None,
        }


# --------------------------------------------------------------------------- #
# Import / export
# --------------------------------------------------------------------------- #
def export_payload(
    definition: FlowDefinition,
    *,
    triggers: list[dict[str, Any]] | None = None,
    version: str = "",
) -> dict[str, Any]:
    """Portable document: flow + triggers + a checksum over both."""
    flow = definition.to_dict()
    clean_triggers = [dict(item) for item in (triggers or [])]
    return {
        "flow_schema": FLOW_SCHEMA,
        "exported_by": f"pc-agent {version}".strip(),
        "flow": flow,
        "triggers": clean_triggers,
        "checksum": definition_checksum(flow, clean_triggers),
    }


def load_export_payload(payload: Any) -> tuple[FlowDefinition, list[dict[str, Any]]]:
    """Validate an exported document; refuse tampering and unknown fields."""
    if not isinstance(payload, Mapping):
        raise FlowError("The imported document must be a JSON object.")
    _check_unknown_keys(payload, _ALLOWED_EXPORT_KEYS, field="import")
    raw = json.dumps(payload)
    if len(raw.encode("utf-8")) > LIMITS["max_import_bytes"]:
        raise FlowError(
            f"The imported document is larger than {LIMITS['max_import_bytes']} bytes."
        )
    flow_payload = payload.get("flow")
    if not isinstance(flow_payload, Mapping):
        raise FlowError("import.flow must be an object.")
    triggers = payload.get("triggers", [])
    if not isinstance(triggers, list):
        raise FlowError("import.triggers must be a list.")
    clean_triggers: list[dict[str, Any]] = []
    for index, item in enumerate(triggers):
        where = f"import.triggers[{index}]"
        if not isinstance(item, Mapping):
            raise FlowError(f"{where} must be an object.")
        _check_unknown_keys(item, _ALLOWED_TRIGGER_KEYS, field=where)
        clean_triggers.append(dict(item))
    checksum = payload.get("checksum")
    if checksum is not None:
        expected = definition_checksum(flow_payload, clean_triggers)
        if str(checksum) != expected:
            raise FlowError(
                "The imported document failed its checksum; it was edited after export."
            )
    definition = validate_definition(flow_payload, path="import.flow")
    return definition, clean_triggers


# --------------------------------------------------------------------------- #
# Runner + triggers
# --------------------------------------------------------------------------- #
class FlowRunner:
    """Compiles a flow into a mission and records the run."""

    def __init__(
        self,
        *,
        store: FlowStore,
        start_mission: Callable[..., Any],
        emit: Callable[..., None] | None = None,
        mission_state: Callable[[str], str | None] | None = None,
    ) -> None:
        self._store = store
        self._start_mission = start_mission
        self._emit = emit or (lambda *args, **kwargs: None)
        self._mission_state = mission_state

    def reconcile(self, *, limit: int = 50) -> int:
        """Close out runs whose mission has already finished.

        A run stays ``started`` while its mission is in flight. Without this
        sweep the one-active-run guard would block a flow forever after its
        first run, so the state is re-read from the Mission Center instead of
        being guessed from a timer.
        """
        if self._mission_state is None:
            return 0
        closed = 0
        for run in self._store.list_runs(limit=limit):
            if run.status not in {"started", "running"} or not run.mission_id:
                continue
            state = ""
            try:
                state = str(self._mission_state(run.mission_id) or "")
            except Exception:  # noqa: BLE001 - a missing mission is not a crash
                state = ""
            if state in {"completed", "failed", "cancelled", "blocked"}:
                self._store.finish_run(
                    run.id,
                    status="completed" if state == "completed" else state,
                    mission_id=run.mission_id,
                    error="" if state == "completed" else f"mission {state}",
                )
                closed += 1
        return closed

    def run_flow(
        self,
        flow_id: str,
        *,
        trigger_kind: str = "manual",
        trigger_id: str | None = None,
        use_ai: bool = False,
        source: str = "flow",
    ) -> dict[str, Any]:
        flow = self._store.get(flow_id)
        if flow is None:
            raise FlowError(f"Flow not found: {flow_id}")
        if not flow.enabled:
            raise FlowError(f"Flow '{flow.name}' is disabled.")
        self.reconcile()
        if self._store.has_active_run(flow_id):
            raise FlowError(f"Flow '{flow.name}' already has a run in progress.")
        levels = flow.definition.topological_levels()
        run = self._store.create_run(
            flow_id=flow.id,
            flow_name=flow.name,
            flow_version=flow.version,
            trigger_kind=trigger_kind,
            trigger_id=trigger_id,
            node_count=len(flow.definition.nodes),
        )
        try:
            mission = self._start_mission(
                flow.name,
                description=(
                    f"Flow {flow.name} v{flow.version} ({trigger_kind}); "
                    f"{len(flow.definition.nodes)} node(s), {len(levels)} level(s)."
                ),
                task_specs=flow.definition.mission_specs(),
                source=source,
            )
            mission_id = str(mission["id"])
        except Exception as exc:
            self._store.finish_run(
                run.id, status="failed", mission_id=None, error=str(exc)
            )
            raise
        self._store.finish_run(run.id, status="started", mission_id=mission_id)
        self._emit(
            "flow.run_started",
            source=source,
            summary=(
                f"Flow '{flow.name}' v{flow.version} started "
                f"({len(flow.definition.nodes)} node(s)) via {trigger_kind}."
            ),
            payload={"flow_id": flow.id, "run_id": run.id, "mission_id": mission_id},
        )
        del use_ai
        return {
            "run": self._store.get_run(run.id).to_dict(),
            "mission_id": mission_id,
            "levels": levels,
            "flow": {"id": flow.id, "name": flow.name, "version": flow.version},
        }

    def tick(self, *, limit: int = 10, now: datetime | None = None) -> dict[str, Any]:
        """Fire due schedule/event triggers once each (bounded, no replay storms)."""
        moment = now or utc_now()
        fired: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        budget = max(1, min(int(limit), 50))

        for trigger in self._store.due_triggers(now=moment):
            if len(fired) >= budget:
                break
            outcome = self._fire(trigger, trigger_kind="schedule", now=moment)
            (fired if outcome["started"] else skipped).append(outcome)

        for trigger in self._store.triggers():
            if trigger.kind != "event" or not trigger.enabled:
                continue
            if len(fired) >= budget:
                break
            events = self._store.pending_events(trigger)
            if not events:
                continue
            outcome = self._fire(
                trigger,
                trigger_kind="event",
                now=moment,
                last_event_id=int(events[-1]["id"]),
            )
            (fired if outcome["started"] else skipped).append(outcome)

        return {"fired": fired, "skipped": skipped, "total": len(fired)}

    def _fire(
        self,
        trigger: FlowTriggerRecord,
        *,
        trigger_kind: str,
        now: datetime,
        last_event_id: int | None = None,
    ) -> dict[str, Any]:
        flow = self._store.get(trigger.flow_id)
        next_run: datetime | None = None
        disable = False
        if trigger.kind == "schedule":
            if trigger.interval_seconds:
                next_run = now + timedelta(seconds=int(trigger.interval_seconds))
            else:
                disable = True  # one-shot run_at trigger
        if flow is None or not flow.enabled:
            self._store.mark_trigger_fired(
                trigger.id,
                next_run_at=next_run,
                last_event_id=last_event_id,
                disable=disable,
            )
            return {
                "started": False,
                "reason": "flow missing or disabled",
                "trigger_id": trigger.id,
                "flow_id": trigger.flow_id,
            }
        try:
            result = self.run_flow(
                flow.id,
                trigger_kind=trigger_kind,
                trigger_id=trigger.id,
                source=f"flow-{trigger_kind}",
            )
        except Exception as exc:  # noqa: BLE001 - report, do not kill the tick
            self._store.mark_trigger_fired(
                trigger.id,
                next_run_at=next_run,
                last_event_id=last_event_id,
                disable=disable,
            )
            return {
                "started": False,
                "reason": redact_text(str(exc))[:200],
                "trigger_id": trigger.id,
                "flow_id": trigger.flow_id,
            }
        self._store.mark_trigger_fired(
            trigger.id,
            next_run_at=next_run,
            last_event_id=last_event_id,
            disable=disable,
        )
        return {
            "started": True,
            "trigger_id": trigger.id,
            "flow_id": flow.id,
            "run_id": result["run"]["id"],
            "mission_id": result["mission_id"],
        }
