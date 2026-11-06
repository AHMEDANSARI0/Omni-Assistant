"""Connector service: gates, registry and the audit trail (Phase 13).

``ConnectorService`` is the only place that decides whether a connector may do
anything. Every public method:

1. checks the gates (master switch + per-connector configuration) and raises a
   :class:`~pc_agent.errors.ConnectorError` with an actionable message when a
   gate is closed - it never silently no-ops;
2. performs the work through a narrow connector class;
3. writes a durable record (``connector_events`` or ``connector_deliveries``)
   and an application event, so the dashboard can show what actually happened.

Nothing is cached: ``status()`` reports the live configuration, and a connector
that is not configured says exactly which setting is missing.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..errors import ConnectorError, PcAgentError
from ..storage.db import Database
from ..storage.repositories import EventRepository
from .base import GateState, bounded_text, redact_endpoint
from .calendar_ics import CalendarConnector, CalendarEvent
from .chat import ChatConnector, DeliveryResult
from .drive import DriveConnector, SyncReport
from .email import EmailConnector, EmailMessage
from .store import ConnectorStore
from .webhook import WebhookConnector, WebhookReceipt

__all__ = ["CONNECTOR_NAMES", "ConnectorService"]

_LOG = logging.getLogger("pc_agent.connectors")
CONNECTOR_NAMES = ("calendar", "drive", "email", "chat", "webhook")
_MASTER_HINT = "Set PC_AGENT_ALLOW_CONNECTORS=1 to enable connectors."


class ConnectorService:
    """Gate-enforcing facade over the five Phase 13 connectors."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        events: EventRepository | None = None,
        store: ConnectorStore | None = None,
    ) -> None:
        self._settings = settings
        self._store = store or ConnectorStore(database)
        self._events = events or EventRepository(database)

    # -- construction helpers ----------------------------------------------
    @property
    def store(self) -> ConnectorStore:
        return self._store

    def calendar_connector(self) -> CalendarConnector:
        path = self._settings.connector_calendar_path
        if not path:
            raise ConnectorError("Set PC_AGENT_CONNECTOR_CALENDAR to the path of an .ics file.")
        return CalendarConnector(path, max_events=self._settings.connector_max_items * 8)

    def drive_connector(self) -> DriveConnector:
        root = self._settings.connector_drive_root
        if not root:
            raise ConnectorError(
                "Set PC_AGENT_CONNECTOR_DRIVE_ROOT to the folder you want to index."
            )
        return DriveConnector(root, max_files=self._settings.connector_max_items * 40)

    def email_connector(self) -> EmailConnector:
        settings = self._settings
        return EmailConnector(
            imap_host=settings.connector_email_host,
            username=settings.connector_email_user,
            password=settings.connector_email_password,
            folder=settings.connector_email_folder,
            smtp_host=settings.connector_smtp_host,
            smtp_port=settings.connector_smtp_port,
            timeout=settings.connector_timeout_seconds,
        )

    def chat_connector(self) -> ChatConnector:
        settings = self._settings
        if not settings.connector_chat_url:
            raise ConnectorError(
                "Set PC_AGENT_CONNECTOR_CHAT_URL to the chat webhook you want to notify."
            )
        return ChatConnector(
            settings.connector_chat_url,
            secret=settings.connector_chat_secret,
            timeout=settings.connector_timeout_seconds,
            allow_private_network=settings.connector_allow_private_endpoints,
        )

    def webhook_connector(self) -> WebhookConnector:
        return WebhookConnector(
            secret=self._settings.connector_webhook_secret,
            skew_seconds=self._settings.connector_webhook_skew_seconds,
        )

    # -- gates --------------------------------------------------------------
    def gates(self) -> dict[str, GateState]:
        settings = self._settings
        master = bool(settings.allow_connectors)
        calendar_path = settings.connector_calendar_path
        drive_root = settings.connector_drive_root
        chat_ready = bool(settings.connector_chat_url and settings.connector_chat_secret)
        email_ready = bool(
            settings.connector_email_host
            and settings.connector_email_user
            and settings.connector_email_password
        )
        email_gate = bool(settings.allow_email_connector)
        webhook_ready = bool(settings.connector_webhook_secret)

        def _state(name: str, configured: bool, detail: str) -> GateState:
            enabled = master and configured
            if not master:
                return GateState(name, False, configured, _MASTER_HINT)
            return GateState(name, enabled, configured, "" if enabled else detail)

        states = {
            "calendar": _state("calendar", bool(calendar_path), "Set PC_AGENT_CONNECTOR_CALENDAR."),
            "drive": _state("drive", bool(drive_root), "Set PC_AGENT_CONNECTOR_DRIVE_ROOT."),
            "email": _state(
                "email",
                email_ready and email_gate,
                "Email needs PC_AGENT_ALLOW_EMAIL_CONNECTOR=1 plus host, user and"
                " password settings.",
            ),
            "chat": _state(
                "chat",
                chat_ready,
                "Chat needs both PC_AGENT_CONNECTOR_CHAT_URL and PC_AGENT_CONNECTOR_CHAT_SECRET.",
            ),
            "webhook": _state(
                "webhook",
                webhook_ready,
                "Set PC_AGENT_CONNECTOR_WEBHOOK_SECRET to accept inbound events.",
            ),
        }
        return states

    def _require(self, name: str) -> GateState:
        state = self.gates()[name]
        if not state.enabled:
            raise ConnectorError(f"The {name} connector is disabled. {state.reason}")
        return state

    # -- status -------------------------------------------------------------
    def status(self, *, probe: bool = False) -> dict[str, Any]:
        gates = self.gates()
        settings = self._settings
        connectors: list[dict[str, Any]] = []
        for name in CONNECTOR_NAMES:
            state = gates[name]
            entry = state.to_dict()
            if name == "calendar":
                entry["target"] = _name_only(settings.connector_calendar_path)
                if probe and state.enabled:
                    entry["probe"] = self.calendar_connector().status()
            elif name == "drive":
                entry["target"] = _name_only(settings.connector_drive_root)
                if probe and state.enabled:
                    entry["probe"] = self.drive_connector().status()
            elif name == "email":
                entry["target"] = settings.connector_email_host or "none"
                entry["folder"] = settings.connector_email_folder
                entry["can_send"] = bool(settings.connector_smtp_host)
                entry["email_gate"] = bool(settings.allow_email_connector)
            elif name == "chat":
                entry["target"] = (
                    redact_endpoint(settings.connector_chat_url)
                    if settings.connector_chat_url
                    else "none"
                )
            elif name == "webhook":
                entry["target"] = "inbound"
                entry["skew_seconds"] = settings.connector_webhook_skew_seconds
            connectors.append(entry)
        return {
            "allow_connectors": bool(settings.allow_connectors),
            "allow_email_connector": bool(settings.allow_email_connector),
            "max_items": settings.connector_max_items,
            "timeout_seconds": settings.connector_timeout_seconds,
            "connectors": connectors,
            "enabled": [item["connector"] for item in connectors if item["enabled"]],
            "ledger": self._store.counts(),
        }

    def summary(self) -> dict[str, Any]:
        gates = self.gates()
        counts = self._store.counts()
        return {
            "allow_connectors": bool(self._settings.allow_connectors),
            "enabled": [name for name, state in gates.items() if state.enabled],
            "configured": [name for name, state in gates.items() if state.configured],
            **counts,
        }

    # -- calendar -----------------------------------------------------------
    def list_calendar_events(
        self, *, days_ahead: int = 7, limit: int | None = None, now: datetime | None = None
    ) -> dict[str, Any]:
        self._require("calendar")
        connector = self.calendar_connector()
        cap = min(limit or self._settings.connector_max_items, self._settings.connector_max_items)
        events = connector.list_events(days_ahead=days_ahead, limit=cap, now=now)
        return {
            "connector": "calendar",
            "days_ahead": days_ahead,
            "events": [event.to_dict() for event in events],
            "total": len(events),
            "path_name": connector.path.name,
            "digest": connector.digest(),
        }

    def add_calendar_event(
        self,
        *,
        summary: str,
        start: str,
        duration_minutes: int = 30,
        location: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        self._require("calendar")
        event: CalendarEvent = self.calendar_connector().add_event(
            summary=summary,
            start=start,
            duration_minutes=duration_minutes,
            location=location,
            description=description,
        )
        record = self._store.record_event(
            "calendar",
            "event_added",
            external_id=event.uid,
            summary=bounded_text(event.summary, 200),
            payload=event.to_dict(),
        )
        self._emit(
            "connector.calendar.event_added",
            summary=f"Calendar event '{event.summary}' added.",
            payload={"uid": event.uid, "start": event.start},
        )
        return {"connector": "calendar", "event": event.to_dict(), "ledger_event": record}

    # -- drive --------------------------------------------------------------
    def drive_sync(self) -> dict[str, Any]:
        self._require("drive")
        connector = self.drive_connector()
        previous = self._store.get_state("drive.manifest", {}) or {}
        report: SyncReport = connector.scan(previous=previous if isinstance(previous, dict) else {})
        self._store.set_state("drive.manifest", report.manifest())
        self._store.set_state(
            "drive.last_sync",
            {
                "at": report.scanned_at,
                "files": len(report.indexed),
                "added": len(report.added),
                "modified": len(report.modified),
                "removed": len(report.removed),
            },
        )
        changes = len(report.added) + len(report.modified) + len(report.removed)
        record = self._store.record_event(
            "drive",
            "sync",
            summary=(
                f"{len(report.indexed)} file(s) indexed, {changes} change(s)"
                + (" (truncated)" if report.truncated else "")
            ),
            payload={
                "added": report.added[:20],
                "modified": report.modified[:20],
                "removed": report.removed[:20],
                "truncated": report.truncated,
            },
        )
        if changes:
            self._emit(
                "connector.drive.changed",
                summary=f"Drive folder changed: {changes} file(s).",
                payload={"added": len(report.added), "modified": len(report.modified)},
            )
        payload = report.to_dict()
        payload["ledger_event"] = record
        return payload

    # -- email --------------------------------------------------------------
    def mail_inbox(self, *, limit: int = 10) -> dict[str, Any]:
        self._require("email")
        cap = min(int(limit), self._settings.connector_max_items)
        messages: list[EmailMessage] = self.email_connector().inbox(limit=cap)
        self._store.record_event(
            "email",
            "inbox_read",
            summary=f"{len(messages)} message header(s) read",
            payload={"count": len(messages)},
        )
        return {
            "connector": "email",
            "messages": [message.to_dict() for message in messages],
            "total": len(messages),
        }

    def mail_send(
        self,
        *,
        to: str | list[str],
        subject: str,
        body: str,
        confirm: str = "",
    ) -> dict[str, Any]:
        self._require("email")
        if not self._settings.connector_smtp_host:
            raise ConnectorError("Sending needs PC_AGENT_CONNECTOR_SMTP_HOST.")
        result = self.email_connector().send(to=to, subject=subject, body=body, confirm=confirm)
        delivery = self._store.record_delivery(
            "email",
            ", ".join(result["recipients"])[:200],
            "sent",
            detail=bounded_text(result["subject"], 200),
            payload_bytes=int(result["bytes"]),
        )
        delivery["recipient_count"] = len(result["recipients"])
        self._emit(
            "connector.email.sent",
            summary=f"Mail sent to {len(result['recipients'])} recipient(s).",
            payload={"delivery_id": delivery["id"]},
        )
        return {"connector": "email", "delivery": delivery}

    # -- chat ---------------------------------------------------------------
    def chat_send(self, text: str, *, target: str = "") -> dict[str, Any]:
        self._require("chat")
        result: DeliveryResult = self.chat_connector().send(text, target=target)
        delivery = self._store.record_delivery(
            "chat",
            result.target,
            "sent" if result.ok else "failed",
            detail=result.detail,
            payload_bytes=result.payload_bytes,
            duration_ms=result.duration_ms,
        )
        self._emit(
            "connector.chat.sent" if result.ok else "connector.chat.failed",
            summary=(
                f"Chat message delivered to {result.target}."
                if result.ok
                else f"Chat delivery failed: {result.detail}"
            ),
            payload={"status_code": result.status_code, "delivery_id": delivery["id"]},
        )
        return {"connector": "chat", "result": result.to_dict(), "delivery": delivery}

    # -- webhook ------------------------------------------------------------
    def receive_webhook(self, body: bytes, *, timestamp: str, signature: str) -> dict[str, Any]:
        self._require("webhook")
        connector = self.webhook_connector()
        seen = self._store.seen_external_ids("webhook")
        receipt: WebhookReceipt = connector.verify(
            body, timestamp=timestamp, signature=signature, seen=seen
        )
        if not receipt.accepted:
            self._store.record_event(
                "webhook",
                "rejected",
                summary=f"Webhook refused: {receipt.reason}",
                payload={"reason": receipt.reason},
            )
            return {"connector": "webhook", "receipt": receipt.to_dict()}
        record = self._store.record_event(
            "webhook",
            receipt.kind,
            external_id=receipt.external_id,
            summary=receipt.summary or f"Webhook event '{receipt.kind}'",
            payload=receipt.payload,
        )
        self._emit(
            "connector.webhook.received",
            summary=receipt.summary or f"Webhook event '{receipt.kind}' received.",
            payload={"connector_event": record["id"], "kind": receipt.kind},
        )
        return {
            "connector": "webhook",
            "receipt": receipt.to_dict(),
            "ledger_event": record,
        }

    # -- ledger views -------------------------------------------------------
    def events(self, *, connector: str | None = None, limit: int = 50) -> dict[str, Any]:
        rows = self._store.events(connector=connector, limit=limit)
        return {"events": rows, "total": len(rows)}

    def deliveries(self, *, connector: str | None = None, limit: int = 20) -> dict[str, Any]:
        rows = self._store.deliveries(connector=connector, limit=limit)
        return {"deliveries": rows, "total": len(rows)}

    def last_event_at(self) -> str | None:
        return self._store.counts()["last_event_at"]

    # -- internals ----------------------------------------------------------
    def _emit(self, event_type: str, *, summary: str, payload: dict[str, Any]) -> None:
        try:
            self._events.record(event_type, source="connectors", summary=summary, payload=payload)
        except PcAgentError:
            _LOG.exception("Failed to record connector event %s", event_type)


def _name_only(path: str) -> str:
    if not path:
        return "none"
    try:
        return Path(path).name or "root"
    except (OSError, ValueError):
        return "invalid"
