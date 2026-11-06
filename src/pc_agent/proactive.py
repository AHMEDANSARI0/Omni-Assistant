"""Proactive intelligence: reminders, notifications and daily briefings.

Phase 14 turns the local service from "answers when asked" into "tells you when
it matters", without ever becoming a notification firehose and without sending
anything off the machine unless the user opened that gate.

Three rules shape this module:

* **Nothing is invented.** A notification exists only because a real reminder is
  due or a real briefing was composed from live state (calendar, drive ledger,
  flows, missions, system health). Every count comes from the database.
* **Quiet means quiet.** A configured quiet window defers normal notifications
  until it ends, unless the user explicitly allows critical ones through.
* **Bounded by construction.** A rolling hourly cap, a per-key cooldown, an open
  reminder limit and a per-tick limit are enforced in code, and every deferral is
  reported with the reason instead of being swallowed.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .errors import PcAgentError, ProactiveError
from .storage.db import Database
from .storage.repositories import EventRepository

__all__ = [
    "Briefing",
    "Notification",
    "ProactiveEngine",
    "ProactiveStore",
    "Reminder",
    "in_quiet_window",
    "next_quiet_end",
    "parse_quiet_window",
]

_LOG = logging.getLogger("pc_agent.proactive")

SEVERITIES = ("low", "normal", "high", "critical")
MAX_TITLE = 120
MAX_BODY = 1_000
MAX_NOTE = 500
MAX_REPEAT = 366 * 24 * 3_600
MIN_NOTIFICATIONS_PER_HOUR = 1
MAX_NOTIFICATIONS_PER_HOUR = 500


# ------------------------------------------------------------------ helpers ---
def _utc(moment: datetime | None = None) -> datetime:
    return (moment or datetime.now(UTC)).astimezone(UTC)


def _stamp(moment: datetime | None = None) -> str:
    return _utc(moment).replace(microsecond=0).isoformat()


def _parse_instant(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def parse_due(value: Any, *, now: datetime | None = None) -> datetime:
    """Parse a due instant; a bare ``HH:MM`` means the next occurrence today.

    Accepts ISO-8601 (``2026-11-06T09:00:00``), ``+15m``/``+2h``/``+1d`` relative
    offsets and ``HH:MM`` local times, and refuses everything else with a message
    that says what was wrong.
    """

    moment = _utc(now or datetime.now(UTC)).astimezone()
    text = str(value or "").strip()
    if not text:
        raise ProactiveError("A due time is required (ISO-8601, HH:MM or +15m).")
    if text[0] in "+-" and len(text) > 1:
        sign = -1 if text[0] == "-" else 1
        body = text[1:].strip().lower()
        unit = body[-1]
        amount_text = body[:-1]
        try:
            amount = float(amount_text)
        except ValueError as exc:
            raise ProactiveError(f"'{text}' is not a valid relative time such as +15m.") from exc
        multiplier = {"s": 1, "m": 60, "h": 3_600, "d": 86_400}.get(unit)
        if multiplier is None or amount <= 0:
            raise ProactiveError(f"'{text}' is not a valid relative time such as +15m.")
        return _utc(moment + timedelta(seconds=sign * amount * multiplier))
    if len(text) == 5 and text[2] == ":" and text[:2].isdigit() and text[3:].isdigit():
        hour, minute = int(text[:2]), int(text[3:])
        if hour > 23 or minute > 59:
            raise ProactiveError(f"'{text}' is not a valid HH:MM time.")
        candidate = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= moment:
            candidate += timedelta(days=1)
        return _utc(candidate)
    parsed = _parse_instant(text)
    if parsed is None:
        raise ProactiveError(f"'{text}' is not a due time; use ISO-8601, HH:MM or a +15m offset.")
    return _utc(parsed)


def parse_quiet_window(text: str) -> tuple[int, int] | None:
    """``"22:00-07:00"`` -> ``(1320, 420)`` in minutes since local midnight."""

    value = str(text or "").strip()
    if not value or "-" not in value:
        return None
    start_text, end_text = (part.strip() for part in value.split("-", 1))
    try:
        start_hour, start_minute = (int(part) for part in start_text.split(":"))
        end_hour, end_minute = (int(part) for part in end_text.split(":"))
    except ValueError:
        return None
    if not all(0 <= item <= 59 for item in (start_minute, end_minute)):
        return None
    if not all(0 <= item <= 23 for item in (start_hour, end_hour)):
        return None
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    return (start, end) if start != end else None


def in_quiet_window(moment: datetime, window: tuple[int, int] | None) -> bool:
    """True when ``moment`` (interpreted in its own zone) sits in the window."""

    if window is None:
        return False
    minute = moment.hour * 60 + moment.minute
    start, end = window
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # wraps midnight


def next_quiet_end(moment: datetime, window: tuple[int, int] | None) -> datetime:
    """The next instant at which the quiet window is over (same zone)."""

    if window is None:
        return moment
    start, end = window
    candidate = moment.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    if end <= start and (moment.hour * 60 + moment.minute) >= start:
        candidate += timedelta(days=1)
    if candidate <= moment:
        candidate += timedelta(days=1)
    return candidate


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


# ---------------------------------------------------------------- data model ---
@dataclass(frozen=True, slots=True)
class Reminder:
    id: str
    title: str
    note: str
    due_at: str
    repeat_seconds: int | None
    severity: str
    enabled: bool
    status: str
    source: str
    created_at: str
    updated_at: str
    last_fired_at: str | None
    fired_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "note": self.note,
            "due_at": self.due_at,
            "repeat_seconds": self.repeat_seconds,
            "severity": self.severity,
            "enabled": self.enabled,
            "status": self.status,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_fired_at": self.last_fired_at,
            "fired_count": self.fired_count,
        }


@dataclass(frozen=True, slots=True)
class Notification:
    id: str
    kind: str
    severity: str
    title: str
    body: str
    channels: str
    status: str
    detail: str
    reminder_id: str | None
    dedupe_key: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "body": self.body,
            "channels": self.channels,
            "status": self.status,
            "detail": self.detail,
            "reminder_id": self.reminder_id,
            "dedupe_key": self.dedupe_key,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class Briefing:
    id: str
    generated_at: str
    local_date: str
    item_count: int
    sections: dict[str, Any]
    delivered: str
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "generated_at": self.generated_at,
            "local_date": self.local_date,
            "item_count": self.item_count,
            "sections": self.sections,
            "delivered": self.delivered,
            "summary": self.summary,
        }

    def text(self) -> str:
        lines = [self.summary]
        for name, section in self.sections.items():
            lines.append(f"- {name}: {section.get('summary', 'n/a')}")
        return "\n".join(lines)


# --------------------------------------------------------------------- store ---
def _reminder_from_row(row: Any) -> Reminder:
    return Reminder(
        id=row["id"],
        title=row["title"],
        note=row["note"],
        due_at=row["due_at"],
        repeat_seconds=row["repeat_seconds"],
        severity=row["severity"],
        enabled=bool(row["enabled"]),
        status=row["status"],
        source=row["source"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_fired_at=row["last_fired_at"],
        fired_count=int(row["fired_count"]),
    )


def _notification_from_row(row: Any) -> Notification:
    return Notification(
        id=row["id"],
        kind=row["kind"],
        severity=row["severity"],
        title=row["title"],
        body=row["body"],
        channels=row["channels"],
        status=row["status"],
        detail=row["detail"],
        reminder_id=row["reminder_id"],
        dedupe_key=row["dedupe_key"],
        created_at=row["created_at"],
    )


class ProactiveStore:
    """Reminders, notifications, briefings and small state (schema v16)."""

    def __init__(self, database: Database) -> None:
        self._db = database
        self._db.initialize()

    # -- reminders ----------------------------------------------------------
    def add_reminder(
        self,
        *,
        title: str,
        due_at: datetime,
        note: str = "",
        repeat_seconds: int | None = None,
        severity: str = "normal",
        source: str = "user",
        max_open: int = 200,
    ) -> Reminder:
        cleaned = _clean(title, MAX_TITLE)
        if not cleaned:
            raise ProactiveError("A reminder needs a title.")
        if severity not in SEVERITIES:
            raise ProactiveError(f"Severity must be one of {', '.join(SEVERITIES)}.")
        if repeat_seconds is not None:
            repeat_seconds = int(repeat_seconds)
            if repeat_seconds < 30 or repeat_seconds > MAX_REPEAT:
                raise ProactiveError(f"A repeat interval must be between 30 s and {MAX_REPEAT} s.")
        with self._db.connect() as connection:
            open_count = connection.execute(
                "SELECT COUNT(*) FROM reminders WHERE status = 'open'"
            ).fetchone()[0]
            if int(open_count) >= max(1, int(max_open)):
                raise ProactiveError(
                    f"There are already {open_count} open reminders (limit {max_open});"
                    " resolve some first."
                )
            reminder_id = uuid.uuid4().hex
            now = _stamp()
            connection.execute(
                "INSERT INTO reminders (id, title, note, due_at, repeat_seconds,"
                " severity, enabled, status, source, created_at, updated_at,"
                " fired_count) VALUES (?, ?, ?, ?, ?, ?, 1, 'open', ?, ?, ?, 0)",
                (
                    reminder_id,
                    cleaned,
                    _clean(note, MAX_NOTE),
                    _stamp(due_at),
                    repeat_seconds,
                    severity,
                    _clean(source, 40) or "user",
                    now,
                    now,
                ),
            )
        reminder = self.get_reminder(reminder_id)
        assert reminder is not None
        return reminder

    def get_reminder(self, reminder_id: str) -> Reminder | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
        return _reminder_from_row(row) if row is not None else None

    def resolve_reference(self, reference: str) -> Reminder:
        text = str(reference or "").strip()
        if not text:
            raise ProactiveError("A reminder id (or id prefix) is required.")
        reminder = self.get_reminder(text)
        if reminder is not None:
            return reminder
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reminders WHERE id LIKE ? ORDER BY created_at DESC LIMIT 5",
                (f"{text}%",),
            ).fetchall()
        if not rows:
            raise ProactiveError(f"Reminder not found: {text}")
        if len(rows) > 1:
            raise ProactiveError(f"Reminder reference '{text}' is ambiguous.")
        return _reminder_from_row(rows[0])

    def list_reminders(self, *, status: str | None = None, limit: int = 50) -> list[Reminder]:
        cap = max(1, min(int(limit), 500))
        with self._db.connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM reminders WHERE status = ? ORDER BY due_at LIMIT ?",
                    (_clean(status, 20), cap),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM reminders ORDER BY"
                    " CASE status WHEN 'open' THEN 0 ELSE 1 END, due_at LIMIT ?",
                    (cap,),
                ).fetchall()
        return [_reminder_from_row(row) for row in rows]

    def due_reminders(self, *, now: datetime, limit: int = 25) -> list[Reminder]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reminders WHERE status = 'open' AND enabled = 1"
                " AND due_at <= ? ORDER BY due_at LIMIT ?",
                (_stamp(now), max(1, min(int(limit), 200))),
            ).fetchall()
        return [_reminder_from_row(row) for row in rows]

    def mark_fired(
        self, reminder_id: str, *, fired_at: datetime, next_due: datetime | None
    ) -> Reminder:
        with self._db.connect() as connection:
            if next_due is None:
                connection.execute(
                    "UPDATE reminders SET status = 'done', last_fired_at = ?,"
                    " fired_count = fired_count + 1, updated_at = ? WHERE id = ?",
                    (_stamp(fired_at), _stamp(), reminder_id),
                )
            else:
                connection.execute(
                    "UPDATE reminders SET due_at = ?, last_fired_at = ?,"
                    " fired_count = fired_count + 1, updated_at = ? WHERE id = ?",
                    (_stamp(next_due), _stamp(fired_at), _stamp(), reminder_id),
                )
        reminder = self.get_reminder(reminder_id)
        assert reminder is not None
        return reminder

    def set_status(self, reminder_id: str, status: str) -> Reminder:
        if status not in {"open", "done", "dismissed"}:
            raise ProactiveError("Reminder status must be open, done or dismissed.")
        with self._db.connect() as connection:
            cursor = connection.execute(
                "UPDATE reminders SET status = ?, updated_at = ? WHERE id = ?",
                (status, _stamp(), reminder_id),
            )
            if cursor.rowcount == 0:
                raise ProactiveError(f"Reminder not found: {reminder_id}")
        reminder = self.get_reminder(reminder_id)
        assert reminder is not None
        return reminder

    def snooze(self, reminder_id: str, *, until: datetime) -> Reminder:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "UPDATE reminders SET due_at = ?, status = 'open', updated_at = ?,"
                " snoozed_until = ? WHERE id = ?",
                (_stamp(until), _stamp(), _stamp(until), reminder_id),
            )
            if cursor.rowcount == 0:
                raise ProactiveError(f"Reminder not found: {reminder_id}")
        reminder = self.get_reminder(reminder_id)
        assert reminder is not None
        return reminder

    def set_enabled(self, reminder_id: str, enabled: bool) -> Reminder:
        with self._db.connect() as connection:
            cursor = connection.execute(
                "UPDATE reminders SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, _stamp(), reminder_id),
            )
            if cursor.rowcount == 0:
                raise ProactiveError(f"Reminder not found: {reminder_id}")
        reminder = self.get_reminder(reminder_id)
        assert reminder is not None
        return reminder

    def delete_reminder(self, reminder_id: str) -> bool:
        with self._db.connect() as connection:
            cursor = connection.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        return cursor.rowcount > 0

    # -- notifications ------------------------------------------------------
    def record_notification(
        self,
        *,
        kind: str,
        title: str,
        body: str = "",
        severity: str = "normal",
        channels: str = "in_app",
        status: str = "delivered",
        detail: str = "",
        reminder_id: str | None = None,
        dedupe_key: str = "",
        limit: int = 500,
        created_at: datetime | None = None,
    ) -> Notification:
        notification_id = uuid.uuid4().hex
        created_at = _stamp(created_at)
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO notifications (id, kind, severity, title, body, channels,"
                " status, detail, reminder_id, dedupe_key, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    notification_id,
                    _clean(kind, 40) or "general",
                    severity if severity in SEVERITIES else "normal",
                    _clean(title, MAX_TITLE) or "(untitled)",
                    _clean(body, MAX_BODY),
                    _clean(channels, 60) or "in_app",
                    _clean(status, 20) or "delivered",
                    _clean(detail, 300),
                    reminder_id,
                    _clean(dedupe_key, 120),
                    created_at,
                ),
            )
            total = connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
            if int(total) > max(1, int(limit)):
                connection.execute(
                    "DELETE FROM notifications WHERE id IN ("
                    " SELECT id FROM notifications ORDER BY created_at LIMIT ?)",
                    (int(total) - max(1, int(limit)),),
                )
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM notifications WHERE id = ?", (notification_id,)
            ).fetchone()
        assert row is not None
        return _notification_from_row(row)

    def notifications(self, *, limit: int = 25, kind: str | None = None) -> list[Notification]:
        cap = max(1, min(int(limit), 500))
        with self._db.connect() as connection:
            if kind:
                rows = connection.execute(
                    "SELECT * FROM notifications WHERE kind = ?"
                    " ORDER BY created_at DESC, id DESC LIMIT ?",
                    (_clean(kind, 40), cap),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM notifications ORDER BY created_at DESC, id DESC LIMIT ?",
                    (cap,),
                ).fetchall()
        return [_notification_from_row(row) for row in rows]

    def count_notifications_since(self, since: datetime) -> int:
        """How many notifications were *delivered* since ``since``.

        Only ``status = 'delivered'`` rows count towards the rolling hourly cap.
        Deferred rows (quiet hours, cap, cooldown) are ledger records of a
        suppression, not notifications the user saw - counting them would let
        every suppressed tick re-arm the cap and starve the rest of the hour.
        """

        with self._db.connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM notifications"
                    " WHERE created_at >= ? AND status = 'delivered'",
                    (_stamp(since),),
                ).fetchone()[0]
            )

    def last_notification_for_key(self, dedupe_key: str) -> Notification | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM notifications WHERE dedupe_key = ? ORDER BY created_at DESC LIMIT 1",
                (_clean(dedupe_key, 120),),
            ).fetchone()
        return _notification_from_row(row) if row is not None else None

    def clear_notifications(self) -> int:
        with self._db.connect() as connection:
            cursor = connection.execute("DELETE FROM notifications")
        return int(cursor.rowcount)

    # -- briefings ----------------------------------------------------------
    def record_briefing(
        self,
        *,
        sections: dict[str, Any],
        item_count: int,
        delivered: str,
        summary: str,
        moment: datetime | None = None,
    ) -> Briefing:
        at = _utc(moment)
        briefing_id = uuid.uuid4().hex
        local_date = at.astimezone().date().isoformat()
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO proactive_briefings (id, generated_at, local_date,"
                " item_count, sections_json, delivered, summary)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    briefing_id,
                    _stamp(at),
                    local_date,
                    max(0, int(item_count)),
                    json.dumps(sections, sort_keys=True)[:40_000],
                    _clean(delivered, 60) or "in_app",
                    _clean(summary, MAX_BODY),
                ),
            )
            total = connection.execute("SELECT COUNT(*) FROM proactive_briefings").fetchone()[0]
            if int(total) > 200:
                connection.execute(
                    "DELETE FROM proactive_briefings WHERE id IN ("
                    " SELECT id FROM proactive_briefings ORDER BY generated_at LIMIT ?)",
                    (int(total) - 200,),
                )
        briefing = self.get_briefing_by_id(briefing_id)
        assert briefing is not None
        return briefing

    def get_briefing_by_id(self, briefing_id: str) -> Briefing | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM proactive_briefings WHERE id = ?", (briefing_id,)
            ).fetchone()
        return _briefing_from_row(row) if row is not None else None

    def briefings(self, *, limit: int = 10) -> list[Briefing]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM proactive_briefings ORDER BY generated_at DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [_briefing_from_row(row) for row in rows]

    def briefing_for_date(self, local_date: str) -> Briefing | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM proactive_briefings WHERE local_date = ?"
                " ORDER BY generated_at DESC LIMIT 1",
                (local_date,),
            ).fetchone()
        return _briefing_from_row(row) if row is not None else None

    # -- state --------------------------------------------------------------
    def get_state(self, key: str, default: Any = None) -> Any:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT value FROM proactive_state WHERE key = ?", (_clean(key, 120),)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def set_state(self, key: str, value: Any) -> None:
        payload = json.dumps(value, sort_keys=True)
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO proactive_state (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (_clean(key, 120), payload[:40_000], _stamp()),
            )

    # -- counts -------------------------------------------------------------
    def counts(self) -> dict[str, Any]:
        with self._db.connect() as connection:
            open_reminders = connection.execute(
                "SELECT COUNT(*) FROM reminders WHERE status = 'open'"
            ).fetchone()[0]
            total_reminders = connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
            notifications = connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
            briefings = connection.execute("SELECT COUNT(*) FROM proactive_briefings").fetchone()[0]
            last_notification = connection.execute(
                "SELECT MAX(created_at) FROM notifications"
            ).fetchone()[0]
            last_briefing = connection.execute(
                "SELECT MAX(generated_at) FROM proactive_briefings"
            ).fetchone()[0]
            next_due = connection.execute(
                "SELECT MIN(due_at) FROM reminders WHERE status = 'open' AND enabled = 1"
            ).fetchone()[0]
        return {
            "reminders_open": int(open_reminders),
            "reminders_total": int(total_reminders),
            "notifications": int(notifications),
            "briefings": int(briefings),
            "last_notification_at": last_notification,
            "last_briefing_at": last_briefing,
            "next_due_at": next_due,
        }


def _briefing_from_row(row: Any) -> Briefing:
    try:
        sections = json.loads(row["sections_json"] or "{}")
    except (TypeError, ValueError):
        sections = {}
    return Briefing(
        id=row["id"],
        generated_at=row["generated_at"],
        local_date=row["local_date"],
        item_count=int(row["item_count"]),
        sections=sections if isinstance(sections, dict) else {},
        delivered=row["delivered"],
        summary=row["summary"],
    )


# ------------------------------------------------------------------- engine ---
class ProactiveEngine:
    """Reminder firing, notification policy and briefing composition."""

    def __init__(
        self,
        settings: Any,
        database: Database,
        *,
        store: ProactiveStore | None = None,
        events: EventRepository | None = None,
        outbound: Any = None,
        clock: Any = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._store = store or ProactiveStore(database)
        self._events = events or EventRepository(database)
        self._outbound = outbound
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- accessors ----------------------------------------------------------
    @property
    def store(self) -> ProactiveStore:
        return self._store

    def quiet_window(self) -> tuple[int, int] | None:
        return parse_quiet_window(getattr(self._settings, "quiet_hours", "") or "")

    def now(self) -> datetime:
        return self._clock().astimezone(UTC)

    def _local(self, moment: datetime) -> datetime:
        return moment.astimezone()

    def available(self) -> bool:
        return bool(getattr(self._settings, "allow_proactive", True))

    def _require(self) -> None:
        if not self.available():
            raise ProactiveError(
                "Proactive intelligence is disabled; set PC_AGENT_ALLOW_PROACTIVE=1."
            )

    # -- status -------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        moment = self.now()
        window = self.quiet_window()
        local = self._local(moment)
        quiet_now = in_quiet_window(local, window)
        hour_ago = moment - timedelta(hours=1)
        in_last_hour = self._store.count_notifications_since(hour_ago)
        max_per_hour = int(getattr(self._settings, "notification_max_per_hour", 20))
        today = local.date().isoformat()
        briefing_today = self._store.briefing_for_date(today)
        return {
            "enabled": self.available(),
            "outbound_enabled": bool(getattr(self._settings, "allow_proactive_outbound", False)),
            "outbound_wired": self._outbound is not None,
            "quiet_hours": getattr(self._settings, "quiet_hours", "") or "none",
            "quiet_now": quiet_now,
            "quiet_ends_at": (
                next_quiet_end(local, window).isoformat() if quiet_now and window else None
            ),
            "quiet_allows_critical": bool(
                getattr(self._settings, "quiet_hours_allow_critical", False)
            ),
            "briefing_enabled": bool(getattr(self._settings, "briefing_enabled", False)),
            "briefing_time": getattr(self._settings, "briefing_time", "08:00"),
            "briefing_today": briefing_today.to_dict() if briefing_today else None,
            "notifications_last_hour": in_last_hour,
            "notification_max_per_hour": max_per_hour,
            "cooldown_seconds": int(getattr(self._settings, "notification_cooldown_seconds", 300)),
            "now": moment.isoformat(),
            **self._store.counts(),
        }

    def summary(self) -> dict[str, Any]:
        status = self.status()
        return {
            "enabled": status["enabled"],
            "reminders_open": status["reminders_open"],
            "next_due_at": status["next_due_at"],
            "notifications": status["notifications"],
            "quiet_now": status["quiet_now"],
            "briefing_today": bool(status["briefing_today"]),
        }

    # -- reminders ----------------------------------------------------------
    def add_reminder(
        self,
        *,
        title: str,
        due: str,
        note: str = "",
        repeat_seconds: int | None = None,
        severity: str = "normal",
        source: str = "user",
    ) -> dict[str, Any]:
        self._require()
        due_at = parse_due(due, now=self.now())
        reminder = self._store.add_reminder(
            title=title,
            due_at=due_at,
            note=note,
            repeat_seconds=repeat_seconds,
            severity=severity,
            source=source,
            max_open=int(getattr(self._settings, "reminder_max_open", 200)),
        )
        self._emit(
            "proactive.reminder_created",
            summary=f"Reminder '{reminder.title}' scheduled for {reminder.due_at}.",
            payload={"reminder_id": reminder.id, "due_at": reminder.due_at},
        )
        return reminder.to_dict()

    def reminders(self, *, status: str | None = None, limit: int = 50) -> dict[str, Any]:
        rows = self._store.list_reminders(status=status, limit=limit)
        return {"reminders": [row.to_dict() for row in rows], "total": len(rows)}

    def resolve_reminder(self, reference: str) -> dict[str, Any]:
        self._require()
        reminder = self._store.resolve_reference(reference)
        updated = self._store.set_status(reminder.id, "done")
        self._emit(
            "proactive.reminder_done",
            summary=f"Reminder '{updated.title}' marked done.",
            payload={"reminder_id": updated.id},
        )
        return updated.to_dict()

    def dismiss_reminder(self, reference: str) -> dict[str, Any]:
        self._require()
        reminder = self._store.resolve_reference(reference)
        updated = self._store.set_status(reminder.id, "dismissed")
        self._emit(
            "proactive.reminder_dismissed",
            summary=f"Reminder '{updated.title}' dismissed.",
            payload={"reminder_id": updated.id},
        )
        return updated.to_dict()

    def snooze_reminder(self, reference: str, *, for_spec: str = "+15m") -> dict[str, Any]:
        self._require()
        reminder = self._store.resolve_reference(reference)
        until = parse_due(for_spec, now=self.now())
        updated = self._store.snooze(reminder.id, until=until)
        self._emit(
            "proactive.reminder_snoozed",
            summary=f"Reminder '{updated.title}' snoozed until {updated.due_at}.",
            payload={"reminder_id": updated.id, "due_at": updated.due_at},
        )
        return updated.to_dict()

    def enable_reminder(self, reference: str, *, enabled: bool) -> dict[str, Any]:
        self._require()
        reminder = self._store.resolve_reference(reference)
        return self._store.set_enabled(reminder.id, enabled).to_dict()

    def delete_reminder(self, reference: str) -> dict[str, Any]:
        self._require()
        reminder = self._store.resolve_reference(reference)
        deleted = self._store.delete_reminder(reminder.id)
        if not deleted:
            raise ProactiveError(f"Reminder not found: {reference}")
        self._emit(
            "proactive.reminder_deleted",
            summary=f"Reminder '{reminder.title}' deleted.",
            payload={"reminder_id": reminder.id},
        )
        return {"reminder_id": reminder.id, "deleted": True}

    # -- notifications ------------------------------------------------------
    def notifications(self, *, limit: int = 25, kind: str | None = None) -> dict[str, Any]:
        rows = self._store.notifications(limit=limit, kind=kind)
        return {"notifications": [row.to_dict() for row in rows], "total": len(rows)}

    def clear_notifications(self) -> dict[str, Any]:
        self._require()
        removed = self._store.clear_notifications()
        self._emit(
            "proactive.notifications_cleared",
            summary=f"Cleared {removed} notification(s).",
            payload={"removed": removed},
        )
        return {"removed": removed}

    def notify(
        self,
        *,
        title: str,
        body: str = "",
        kind: str = "general",
        severity: str = "normal",
        dedupe_key: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        """Deliver one notification through the full policy (used by tests/CLI)."""

        self._require()
        return self._deliver(
            title=title,
            body=body,
            kind=kind,
            severity=severity,
            dedupe_key=dedupe_key,
            force=force,
        ).to_dict()

    # -- briefing -----------------------------------------------------------
    def build_briefing(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Compose the briefing from live state (no invented content)."""

        moment = now or self.now()
        sections: dict[str, Any] = {}

        sections["reminders"] = self._reminder_section()
        sections["calendar"] = self._calendar_section(moment)
        sections["drive"] = self._drive_section()
        sections["flows"] = self._flow_section()
        sections["tasks"] = self._task_section()
        sections["system"] = self._system_section()
        item_count = sum(
            int(section.get("items", 0))
            for section in sections.values()
            if isinstance(section, dict)
        )
        headline_bits = [
            f"{sections['reminders']['summary']}",
            f"{sections['calendar']['summary']}",
            f"{sections['tasks']['summary']}",
        ]
        summary = "Daily briefing: " + "; ".join(headline_bits)
        return {"summary": summary, "sections": sections, "item_count": item_count}

    def run_briefing(self, *, force: bool = False) -> dict[str, Any]:
        self._require()
        moment = self.now()
        local = self._local(moment)
        today = local.date().isoformat()
        if not force:
            if not bool(getattr(self._settings, "briefing_enabled", False)):
                raise ProactiveError("The daily briefing is off; set PC_AGENT_BRIEFING_ENABLED=1.")
            if local.strftime("%H:%M") < str(getattr(self._settings, "briefing_time", "08:00")):
                raise ProactiveError(
                    f"The briefing is scheduled for {self._settings.briefing_time}; it is not due yet."
                )
            if self._store.briefing_for_date(today) is not None:
                raise ProactiveError("Today's briefing was already delivered.")
        composed = self.build_briefing(now=moment)
        delivered = self._deliver(
            title="Daily briefing",
            body=composed["summary"],
            kind="briefing",
            severity="low",
            dedupe_key=f"briefing:{today}",
            force=force,
            quiet_exempt=True,
        )
        briefing = self._store.record_briefing(
            sections=composed["sections"],
            item_count=composed["item_count"],
            delivered=delivered.channels if delivered.status == "delivered" else "deferred",
            summary=composed["summary"],
            moment=moment,
        )
        self._emit(
            "proactive.briefing",
            summary=f"Briefing generated ({composed['item_count']} item(s)).",
            payload={"briefing_id": briefing.id, "items": composed["item_count"]},
        )
        return {
            "briefing": briefing.to_dict(),
            "notification": delivered.to_dict(),
            "sections": composed["sections"],
        }

    def briefings(self, *, limit: int = 10) -> dict[str, Any]:
        rows = self._store.briefings(limit=limit)
        return {"briefings": [row.to_dict() for row in rows], "total": len(rows)}

    # -- the tick -----------------------------------------------------------
    def tick(self, *, now: datetime | None = None, limit: int | None = None) -> dict[str, Any]:
        """Fire due reminders and the daily briefing, honouring every policy."""

        self._require()
        moment = now or self.now()
        cap = int(limit or getattr(self._settings, "proactive_tick_limit", 25))
        cap = max(1, min(cap, 200))
        fired: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        for reminder in self._store.due_reminders(now=moment, limit=cap):
            decision = self._deliver(
                title=reminder.title,
                body=reminder.note,
                kind="reminder",
                severity=reminder.severity,
                dedupe_key=f"reminder:{reminder.id}",
                reminder_id=reminder.id,
                cooldown=0,
            )
            if decision.status == "delivered":
                next_due = self._next_occurrence(reminder, moment)
                updated = self._store.mark_fired(reminder.id, fired_at=moment, next_due=next_due)
                fired.append({"reminder": updated.to_dict(), "notification": decision.to_dict()})
            else:
                deferred.append(
                    {
                        "reminder_id": reminder.id,
                        "title": reminder.title,
                        "reason": decision.detail or decision.status,
                    }
                )
        briefing_result: dict[str, Any] | None = None
        if self._briefing_due(moment):
            try:
                briefing_result = self.run_briefing()
            except PcAgentError as exc:  # policy said no: record it, never crash a tick
                briefing_result = {"skipped": str(exc)}
        result = {
            "at": moment.isoformat(),
            "limit": cap,
            "fired": fired,
            "deferred": deferred,
            "briefing": briefing_result,
            "total": len(fired),
        }
        if fired or deferred or briefing_result:
            self._emit(
                "proactive.tick",
                summary=(
                    f"{len(fired)} notification(s) delivered, {len(deferred)} deferred"
                    + (
                        ", briefing generated"
                        if briefing_result and "briefing" in (briefing_result or {})
                        else ""
                    )
                ),
                payload={
                    "fired": len(fired),
                    "deferred": len(deferred),
                    "briefing": bool(briefing_result),
                },
            )
            self._store.set_state("last_tick", {"at": result["at"], "fired": len(fired)})
        return result

    # -- policy internals ---------------------------------------------------
    def _briefing_due(self, moment: datetime) -> bool:
        if not bool(getattr(self._settings, "briefing_enabled", False)):
            return False
        local = self._local(moment)
        if local.strftime("%H:%M") < str(getattr(self._settings, "briefing_time", "08:00")):
            return False
        return self._store.briefing_for_date(local.date().isoformat()) is None

    def _next_occurrence(self, reminder: Reminder, moment: datetime) -> datetime | None:
        if not reminder.repeat_seconds:
            return None
        due = _parse_instant(reminder.due_at) or moment
        step = timedelta(seconds=int(reminder.repeat_seconds))
        candidate = due + step
        guard = 0
        while candidate <= moment and guard < 5_000:
            candidate += step
            guard += 1
        return candidate

    def _deliver(
        self,
        *,
        title: str,
        body: str,
        kind: str,
        severity: str,
        dedupe_key: str = "",
        reminder_id: str | None = None,
        cooldown: int | None = None,
        force: bool = False,
        quiet_exempt: bool = False,
    ) -> Notification:
        moment = self.now()
        window = self.quiet_window()
        local = self._local(moment)
        cooldown_seconds = (
            int(getattr(self._settings, "notification_cooldown_seconds", 300))
            if cooldown is None
            else int(cooldown)
        )
        if severity not in SEVERITIES:
            severity = "normal"

        # 1) quiet hours ----------------------------------------------------
        if not quiet_exempt and in_quiet_window(local, window):
            allowed = severity == "critical" and bool(
                getattr(self._settings, "quiet_hours_allow_critical", False)
            )
            if not allowed:
                ends = next_quiet_end(local, window).isoformat()
                return self._store.record_notification(
                    kind=kind,
                    title=title,
                    body=body,
                    severity=severity,
                    channels="deferred",
                    status="deferred",
                    detail=f"quiet hours until {ends}",
                    reminder_id=reminder_id,
                    dedupe_key=dedupe_key,
                    created_at=moment,
                )

        # 2) rolling hourly cap --------------------------------------------
        max_per_hour = int(getattr(self._settings, "notification_max_per_hour", 20))
        in_hour = self._store.count_notifications_since(moment - timedelta(hours=1))
        if in_hour >= max_per_hour and not force:
            return self._store.record_notification(
                kind=kind,
                title=title,
                body=body,
                severity=severity,
                channels="deferred",
                status="deferred",
                detail=f"hourly limit reached ({in_hour}/{max_per_hour})",
                reminder_id=reminder_id,
                dedupe_key=dedupe_key,
                created_at=moment,
            )

        # 3) dedupe / cooldown ---------------------------------------------
        if dedupe_key and cooldown_seconds > 0 and not force:
            previous = self._store.last_notification_for_key(dedupe_key)
            if previous is not None and previous.status == "delivered":
                last_at = _parse_instant(previous.created_at)
                if last_at is not None and (moment - last_at) < timedelta(seconds=cooldown_seconds):
                    return self._store.record_notification(
                        kind=kind,
                        title=title,
                        body=body,
                        severity=severity,
                        channels="deferred",
                        status="deferred",
                        detail=f"cooldown active for {dedupe_key}",
                        reminder_id=reminder_id,
                        dedupe_key=dedupe_key,
                        created_at=moment,
                    )

        # 4) in-app delivery is always durable ------------------------------
        channels = ["in_app"]
        detail = "in-app"
        app_event_type = f"proactive.{kind}"
        try:
            self._events.record(
                app_event_type,
                source="proactive",
                summary=f"{title}"[:280],
                payload={
                    "notification_kind": kind,
                    "severity": severity,
                    "reminder_id": reminder_id,
                },
            )
        except PcAgentError:
            _LOG.exception("Failed to record proactive app event for %s", kind)

        # 5) outbound only when the user opened that gate -------------------
        if self._outbound is not None and bool(
            getattr(self._settings, "allow_proactive_outbound", False)
        ):
            try:
                result = self._outbound(title, body, severity)
            except Exception as exc:  # noqa: BLE001 - never lose the in-app copy
                detail = f"in-app; outbound failed: {type(exc).__name__}"
                _LOG.warning("Proactive outbound delivery failed: %s", exc)
            else:
                if bool(getattr(result, "get", lambda *_: None)("ok", False)):
                    channels.append("chat")
                    detail = "in-app + chat"
                else:
                    detail = "in-app; outbound refused"

        return self._store.record_notification(
            kind=kind,
            title=title,
            body=body,
            severity=severity,
            channels=",".join(channels),
            status="delivered",
            detail=detail,
            reminder_id=reminder_id,
            dedupe_key=dedupe_key,
            created_at=moment,
        )

    # -- briefing sections --------------------------------------------------
    def _reminder_section(self) -> dict[str, Any]:
        counts = self._store.counts()
        open_list = self._store.list_reminders(status="open", limit=5)
        titles = [f"{row.title} @ {row.due_at[11:16]}" for row in open_list]
        return {
            "items": counts["reminders_open"],
            "summary": (
                f"{counts['reminders_open']} open reminder(s)"
                + (f", next {counts['next_due_at'][:16]}" if counts["next_due_at"] else "")
            ),
            "next_due_at": counts["next_due_at"],
            "preview": titles,
        }

    def _calendar_section(self, moment: datetime) -> dict[str, Any]:
        from .errors import ConnectorError

        try:
            from .connectors import ConnectorService

            service = ConnectorService(self._settings, self._database)
            listing = service.list_calendar_events(days_ahead=1, limit=5)
        except ConnectorError as exc:
            # Say why in the user's terms: off, unconfigured, or a real failure.
            if not getattr(self._settings, "allow_connectors", False):
                reason = "connectors are off (PC_AGENT_ALLOW_CONNECTORS)"
            elif not getattr(self._settings, "connector_calendar_path", ""):
                reason = "no calendar file configured (PC_AGENT_CONNECTOR_CALENDAR)"
            else:
                reason = str(exc)[:120]
            return {"items": 0, "summary": f"calendar: {reason}", "events": []}
        except Exception as exc:  # noqa: BLE001 - a briefing must compose regardless
            return {
                "items": 0,
                "summary": f"calendar unavailable ({type(exc).__name__})",
                "events": [],
            }
        events = listing.get("events", [])
        return {
            "items": len(events),
            "summary": f"{len(events)} event(s) in the next 24 h",
            "events": [
                {"start": item.get("start"), "summary": item.get("summary")} for item in events[:5]
            ],
        }

    def _drive_section(self) -> dict[str, Any]:
        state = self._store.get_state("drive.last_sync")
        if not isinstance(state, dict):
            return {"items": 0, "summary": "no drive sync recorded", "last_sync": None}
        return {
            "items": int(state.get("added", 0)) + int(state.get("modified", 0)),
            "summary": (
                f"drive: {state.get('files', 0)} file(s) indexed, "
                f"added {state.get('added', 0)}, modified {state.get('modified', 0)}"
            ),
            "last_sync": state.get("at"),
        }

    def _flow_section(self) -> dict[str, Any]:
        try:
            from .flows import FlowStore

            summary = FlowStore(self._database).summary()
        except Exception as exc:  # noqa: BLE001
            return {"items": 0, "summary": f"flows unavailable ({type(exc).__name__})"}
        return {
            "items": int(summary.get("runs", 0)),
            "summary": (
                f"{summary.get('flows', 0)} flow(s), {summary.get('triggers_enabled', 0)}"
                f" trigger(s), {summary.get('runs', 0)} run(s)"
            ),
            "last_run_at": summary.get("last_run_at"),
        }

    def _task_section(self) -> dict[str, Any]:
        try:
            with self._database.connect() as connection:
                failed = connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status = 'failed'"
                ).fetchone()[0]
                pending = connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status IN ('pending', 'running')"
                ).fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            return {"items": 0, "summary": f"tasks unavailable ({type(exc).__name__})"}
        return {
            "items": int(failed) + int(pending),
            "summary": f"{pending} task(s) in flight, {failed} failed",
            "failed": int(failed),
            "in_flight": int(pending),
        }

    def _system_section(self) -> dict[str, Any]:
        try:
            from .system_monitor import SystemMonitor

            monitor = SystemMonitor(
                root_dir=self._settings.root_dir,
                workspace_dir=self._settings.workspace_dir,
            )
            snapshot = monitor.snapshot(include_top_processes=False)
        except Exception as exc:  # noqa: BLE001
            return {"items": 0, "summary": f"system unavailable ({type(exc).__name__})"}
        def _pct(value: Any) -> str:
            return f"{value:.0f}%" if isinstance(value, (int, float)) else "n/a"

        disk = snapshot.disks.get("install", {}).get("percent")
        return {
            "items": 1,
            "summary": (
                f"cpu {_pct(snapshot.cpu_percent)}, memory"
                f" {_pct(snapshot.memory.get('percent'))}, disk {_pct(disk)}"
            ),
            "cpu_percent": snapshot.cpu_percent,
            "memory_percent": snapshot.memory.get("percent"),
            "disk_percent": disk,
        }

    # -- internals ----------------------------------------------------------
    def _emit(self, event_type: str, *, summary: str, payload: dict[str, Any]) -> None:
        try:
            self._events.record(event_type, source="proactive", summary=summary, payload=payload)
        except PcAgentError:
            _LOG.exception("Failed to record proactive event %s", event_type)
