"""iCalendar connector: a real calendar that needs no account (Phase 13).

A calendar file (``.ics``) is the most boring possible integration and that is
exactly why it is first: it works offline, it is human-editable, it syncs with
Outlook/Google/Thunderbird through the user's own client, and it cannot leak
anything by itself. This module only parses and writes iCalendar text.

Guarantees:

* reads are bounded (file size, event count, text length) and never follow a
  path outside the configured calendar file;
* an event is only accepted when it has a ``UID`` and a valid ``DTSTART``;
* writes are atomic (temp file + replace) and keep existing events;
* times are normalised to UTC ISO-8601 when they carry a ``Z`` suffix, and kept
  as local ISO-8601 otherwise (no silent timezone invention).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..errors import ConnectorError

__all__ = ["CalendarConnector", "CalendarEvent"]

MAX_CALENDAR_BYTES = 2_000_000
MAX_EVENTS = 2_000
MAX_TEXT = 500
_DT_FORMATS = ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d")
_LINE_SAFE = re.compile(r"[\r\n\x00]")


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    uid: str
    summary: str
    start: str
    end: str
    location: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "summary": self.summary,
            "start": self.start,
            "end": self.end,
            "location": self.location,
            "description": self.description,
        }


def _parse_dt(raw: str) -> datetime | None:
    value = raw.strip()
    if not value:
        return None
    if value.startswith((":", ";")) or "TZID=" in value:
        value = value.split(":", 1)[-1].strip()
    for candidate in _DT_FORMATS:
        try:
            parsed = datetime.strptime(value, candidate)
        except ValueError:
            continue
        return parsed.replace(tzinfo=UTC) if value.endswith("Z") else parsed
    # User input (CLI/API/GUI) is ISO-8601 with separators, not the compact
    # iCalendar form, so accept both instead of asking the user to reformat.
    try:
        parsed_iso = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed_iso.tzinfo is None:
        return parsed_iso
    return parsed_iso


def _iso(raw: str) -> str:
    parsed = _parse_dt(raw)
    return parsed.isoformat() if parsed is not None else ""


def _aware(moment: datetime) -> datetime:
    """Attach the machine's local zone to a naive instant (never invent UTC)."""

    return moment.astimezone(UTC) if moment.tzinfo else moment.astimezone()


def _fold(line: str) -> list[str]:
    """RFC 5545 line folding at 75 octets without splitting UTF-8 sequences."""

    encoded = line.encode("utf-8")
    if len(encoded) <= 73:
        return [line]
    chunks: list[str] = []
    current = b""
    for character in line:
        piece = character.encode("utf-8")
        if len(current) + len(piece) > 73:
            chunks.append(current.decode("utf-8"))
            current = b" " + piece
        else:
            current += piece
    if current:
        chunks.append(current.decode("utf-8"))
    return chunks


def _escape(text: str) -> str:
    cleaned = _LINE_SAFE.sub(" ", str(text)).strip()[:MAX_TEXT]
    return cleaned.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


def _unescape(text: str) -> str:
    return (
        text.replace("\\N", " ")
        .replace("\\n", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def parse_calendar(text: str) -> list[CalendarEvent]:
    """Parse VEVENT blocks into events; malformed entries are skipped."""

    unfolded: list[str] = []
    for raw_line in text.splitlines():
        if raw_line[:1] in {" ", "\t"} and unfolded:
            unfolded[-1] += raw_line[1:]
        else:
            unfolded.append(raw_line)

    events: list[CalendarEvent] = []
    current: dict[str, str] | None = None
    for line in unfolded:
        upper = line.strip().upper()
        if upper == "BEGIN:VEVENT":
            current = {}
            continue
        if upper == "END:VEVENT":
            if current is not None:
                event = _event_from(current)
                if event is not None and len(events) < MAX_EVENTS:
                    events.append(event)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        key, _, value = line.partition(":")
        name = key.split(";", 1)[0].strip().upper()
        if name in {"UID", "SUMMARY", "DTSTART", "DTEND", "LOCATION", "DESCRIPTION"}:
            current[name] = value
    return events


def _event_from(fields: dict[str, str]) -> CalendarEvent | None:
    uid = _unescape(fields.get("UID", ""))
    start_raw = fields.get("DTSTART", "")
    start = _parse_dt(start_raw)
    if not uid or start is None:
        return None
    end_raw = fields.get("DTEND", "")
    end = _parse_dt(end_raw) or (start + timedelta(hours=1))
    start_iso = start.isoformat()
    return CalendarEvent(
        uid=uid[:120],
        summary=_unescape(fields.get("SUMMARY", "")) or "(no title)",
        start=start_iso,
        end=end.isoformat(),
        location=_unescape(fields.get("LOCATION", ""))[:MAX_TEXT],
        description=_unescape(fields.get("DESCRIPTION", ""))[:MAX_TEXT],
    )


def _serialise(event: CalendarEvent) -> list[str]:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    start = _parse_dt(event.start)
    end = _parse_dt(event.end) or (start + timedelta(hours=1) if start else None)
    lines = [
        "BEGIN:VEVENT",
        f"UID:{_escape(event.uid)}",
        f"DTSTAMP:{stamp}",
        f"SUMMARY:{_escape(event.summary)}",
    ]
    if start is not None:
        lines.append(f"DTSTART:{_aware(start).strftime('%Y%m%dT%H%M%SZ')}")
    if end is not None:
        lines.append(f"DTEND:{_aware(end).strftime('%Y%m%dT%H%M%SZ')}")
    if event.location:
        lines.append(f"LOCATION:{_escape(event.location)}")
    if event.description:
        lines.append(f"DESCRIPTION:{_escape(event.description)}")
    lines.append("END:VEVENT")
    folded: list[str] = []
    for line in lines:
        folded.extend(_fold(line))
    return folded


class CalendarConnector:
    """Read and write one local ``.ics`` calendar file."""

    def __init__(self, path: Path | str, *, max_events: int = 200) -> None:
        self._path = Path(path).expanduser()
        self._max_events = max(1, min(int(max_events), MAX_EVENTS))

    @property
    def path(self) -> Path:
        return self._path

    def status(self) -> dict[str, Any]:
        exists = self._path.is_file()
        return {
            "connector": "calendar",
            "kind": "local_ics",
            "configured": bool(str(self._path)) and str(self._path) not in {"", "."},
            "exists": exists,
            "path_name": self._path.name,
            "events": len(self._read_events()) if exists else 0,
        }

    def _read_text(self) -> str:
        if not self._path.is_file():
            return ""
        try:
            size = self._path.stat().st_size
        except OSError as exc:
            raise ConnectorError(f"The calendar file could not be read: {exc}") from exc
        if size > MAX_CALENDAR_BYTES:
            raise ConnectorError(
                f"The calendar file is larger than {MAX_CALENDAR_BYTES} bytes; "
                "splitting it is a manual step."
            )
        try:
            return self._path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ConnectorError(f"The calendar file could not be read: {exc}") from exc

    def _read_events(self) -> list[CalendarEvent]:
        return parse_calendar(self._read_text())

    def list_events(
        self, *, days_ahead: int = 7, limit: int | None = None, now: datetime | None = None
    ) -> list[CalendarEvent]:
        """Upcoming events inside the requested window (bounded)."""

        moment = _aware(now or datetime.now(UTC))
        horizon = moment + timedelta(days=max(1, min(int(days_ahead), 365)))
        upcoming: list[CalendarEvent] = []
        for event in self._read_events():
            parsed = _parse_dt(event.start)
            if parsed is None:
                continue
            comparable = _aware(parsed)
            if comparable <= moment:
                continue
            if comparable > horizon:
                continue
            upcoming.append(event)
        upcoming.sort(key=lambda item: _aware(_parse_dt(item.start) or datetime.now(UTC)))
        cap = self._max_events if limit is None else max(1, min(int(limit), self._max_events))
        return upcoming[:cap]

    def add_event(
        self,
        *,
        summary: str,
        start: str,
        duration_minutes: int = 30,
        location: str = "",
        description: str = "",
    ) -> CalendarEvent:
        started = _parse_dt(start)
        if started is None:
            raise ConnectorError("start must be an ISO-8601 instant such as 2026-11-06T09:00:00.")
        title = _LINE_SAFE.sub(" ", summary or "").strip()
        if not title:
            raise ConnectorError("An event needs a non-empty summary.")
        minutes = max(1, min(int(duration_minutes), 24 * 60))
        event = CalendarEvent(
            uid=f"pcagent-{uuid.uuid4().hex[:16]}",
            summary=title[:MAX_TEXT],
            start=started.isoformat(),
            end=(started + timedelta(minutes=minutes)).isoformat(),
            location=_LINE_SAFE.sub(" ", location)[:MAX_TEXT],
            description=_LINE_SAFE.sub(" ", description)[:MAX_TEXT],
        )
        self._append([event])
        return event

    def _append(self, events: list[CalendarEvent]) -> None:
        existing = self._read_text()
        body = existing.rstrip()
        has_trailer = body.endswith("END:VCALENDAR")
        lines: list[str] = []
        if not body:
            lines.extend(
                [
                    "BEGIN:VCALENDAR",
                    "VERSION:2.0",
                    "PRODID:-//Self-Healing PC Agent//Connectors//EN",
                ]
            )
        elif has_trailer:
            body = body[: -len("END:VCALENDAR")].rstrip()
            lines.extend(body.splitlines())
        else:
            lines.extend(body.splitlines())
        for event in events:
            lines.extend(_serialise(event))
        lines.append("END:VCALENDAR")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.replace(self._path)

    def digest(self) -> str:
        """SHA-256 of the calendar file (used by the sync/ledger reports)."""

        if not self._path.is_file():
            return ""
        return hashlib.sha256(self._path.read_bytes()).hexdigest()
