"""Connectors (Transformation Phase 13).

Five local-first connectors, each with its own explicit opt-in and its own
bounded contract:

* :mod:`pc_agent.connectors.calendar_ics` - read/write an iCalendar file on disk.
* :mod:`pc_agent.connectors.drive` - hash-verified sync with a local folder
  (no cloud account required).
* :mod:`pc_agent.connectors.email` - read headers over IMAP and send over SMTP,
  behind a separate gate and a per-send confirmation.
* :mod:`pc_agent.connectors.chat` - send a signed notification to one
  allow-listed HTTPS endpoint.
* :mod:`pc_agent.connectors.webhook` - verify HMAC-signed inbound events and
  turn them into local events (which flow triggers can react to).

:class:`~pc_agent.connectors.service.ConnectorService` is the only gate keeper;
:class:`~pc_agent.connectors.store.ConnectorStore` is the durable ledger.

Nothing here invents a connection: if a connector is not configured, the status
call says exactly which setting is missing.
"""

from __future__ import annotations

from .base import GateState, redact_endpoint, utc_stamp
from .calendar_ics import CalendarConnector, CalendarEvent
from .chat import ChatConnector, DeliveryResult
from .drive import DriveConnector, DriveEntry, SyncReport
from .email import EmailConnector, EmailMessage
from .service import CONNECTOR_NAMES, ConnectorService
from .store import ConnectorStore
from .webhook import WebhookConnector, WebhookReceipt, sign_payload

__all__ = [
    "CONNECTOR_NAMES",
    "CalendarConnector",
    "CalendarEvent",
    "ChatConnector",
    "ConnectorService",
    "ConnectorStore",
    "DeliveryResult",
    "DriveConnector",
    "DriveEntry",
    "EmailConnector",
    "EmailMessage",
    "GateState",
    "SyncReport",
    "WebhookConnector",
    "WebhookReceipt",
    "redact_endpoint",
    "sign_payload",
    "utc_stamp",
]
