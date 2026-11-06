"""Email connector: IMAP headers in, SMTP out, both explicitly gated.

Reading mail is privacy-sensitive and sending mail is externally visible, so
this connector is deliberately the most restricted one:

* it is off unless ``allow_connectors`` **and** ``allow_email_connector`` are on;
* reading fetches headers only (From / Subject / Date / Message-Id) and never
  downloads bodies or attachments;
* sending needs a third, per-message confirmation token (``confirm="SEND"``),
  refuses header injection, and accepts at most five recipients;
* credentials come from the environment/config and are never printed, logged or
  written into the delivery ledger.

The IMAP/SMTP factories are injectable so tests can drive the exact protocol
conversation without a network or a mail server.
"""

from __future__ import annotations

import imaplib
import re
import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage as _StdEmailMessage
from email.utils import formataddr, parseaddr
from typing import Any

from ..errors import ConnectorError

__all__ = ["MAX_RECIPIENTS", "EmailConnector", "EmailMessage"]

MAX_MESSAGES = 50
MAX_RECIPIENTS = 5
MAX_SUBJECT = 200
MAX_BODY = 10_000
_HEADER_INJECTION = re.compile(r"[\r\n\x00]")
_ADDRESS_RE = re.compile(r"^[^@\s,<>]{1,64}@[^@\s,<>]{1,190}\.[A-Za-z]{2,}$")

ImapFactory = Callable[..., Any]
SmtpFactory = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class EmailMessage:
    message_id: str
    sender: str
    subject: str
    date: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "from": self.sender,
            "subject": self.subject,
            "date": self.date,
        }


def _decode(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeError, LookupError):
        return value


def _clean_address(value: str) -> str:
    _, address = parseaddr(value)
    return address.strip()


class EmailConnector:
    """Header-only IMAP reads and approved SMTP sends."""

    def __init__(
        self,
        *,
        imap_host: str,
        username: str,
        password: str,
        folder: str = "INBOX",
        imap_port: int = 993,
        smtp_host: str = "",
        smtp_port: int = 587,
        timeout: float = 15.0,
        imap_factory: ImapFactory | None = None,
        smtp_factory: SmtpFactory | None = None,
    ) -> None:
        self._imap_host = imap_host.strip()
        self._username = username.strip()
        self._password = password
        self._folder = (folder or "INBOX").strip() or "INBOX"
        self._imap_port = int(imap_port)
        self._smtp_host = smtp_host.strip()
        self._smtp_port = int(smtp_port)
        self._timeout = max(1.0, min(float(timeout), 120.0))
        self._imap_factory = imap_factory or imaplib.IMAP4_SSL
        self._smtp_factory = smtp_factory or smtplib.SMTP

    # -- status -------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        issues: list[str] = []
        if not self._imap_host:
            issues.append("PC_AGENT_CONNECTOR_EMAIL_HOST is not set")
        if not self._username:
            issues.append("PC_AGENT_CONNECTOR_EMAIL_USER is not set")
        if not self._password:
            issues.append("PC_AGENT_CONNECTOR_EMAIL_PASSWORD is not set")
        can_send = bool(self._smtp_host and self._username and self._password)
        if not self._smtp_host:
            issues.append("PC_AGENT_CONNECTOR_SMTP_HOST is not set (send disabled)")
        return {
            "connector": "email",
            "kind": "imap_smtp",
            "configured": not issues[:3],
            "can_read": not issues[:3],
            "can_send": can_send,
            "host": self._imap_host,
            "folder": self._folder,
            "smtp_host": self._smtp_host,
            "smtp_port": self._smtp_port,
            "issues": issues,
        }

    # -- reading ------------------------------------------------------------
    def _connect_imap(self) -> Any:
        try:
            client = self._imap_factory(
                self._imap_host, self._imap_port, ssl_context=ssl.create_default_context()
            )
        except TypeError:
            client = self._imap_factory(self._imap_host, self._imap_port)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise ConnectorError(f"IMAP connection failed ({exc.__class__.__name__}).") from exc
        try:
            client.login(self._username, self._password)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise ConnectorError("IMAP login was rejected; check the mailbox credentials.") from exc
        return client

    def inbox(self, *, limit: int = 10) -> list[EmailMessage]:
        """Newest ``limit`` messages, headers only, newest first."""

        count = max(1, min(int(limit), MAX_MESSAGES))
        client = self._connect_imap()
        try:
            try:
                typ, _ = client.select(self._folder, readonly=True)
            except (OSError, imaplib.IMAP4.error) as exc:
                raise ConnectorError(f"The folder '{self._folder}' could not be opened.") from exc
            if typ != "OK":
                raise ConnectorError(f"The folder '{self._folder}' could not be opened.")
            typ, data = client.search(None, "ALL")
            if typ != "OK" or not data:
                return []
            identifiers = (data[0] or b"").split()
            selected = identifiers[-count:][::-1]
            messages: list[EmailMessage] = []
            for identifier in selected:
                typ, payload = client.fetch(
                    identifier, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
                )
                if typ != "OK" or not payload:
                    continue
                raw = _first_bytes(payload)
                message = _headers_from_bytes(raw)
                if message is not None:
                    messages.append(message)
            return messages
        finally:
            try:
                client.logout()
            except (OSError, imaplib.IMAP4.error):
                pass

    # -- sending ------------------------------------------------------------
    def send(
        self,
        *,
        to: str | list[str],
        subject: str,
        body: str,
        confirm: str,
    ) -> dict[str, Any]:
        if confirm != "SEND":
            raise ConnectorError(
                "Sending mail needs an explicit confirmation; pass confirm='SEND'."
            )
        if not self._smtp_host:
            raise ConnectorError("Sending is not configured: set PC_AGENT_CONNECTOR_SMTP_HOST.")
        recipients = [to] if isinstance(to, str) else list(to)
        if not recipients:
            raise ConnectorError("At least one recipient is required.")
        if len(recipients) > MAX_RECIPIENTS:
            raise ConnectorError(f"At most {MAX_RECIPIENTS} recipients are allowed.")
        cleaned: list[str] = []
        for item in recipients:
            address = _clean_address(str(item))
            if not _ADDRESS_RE.match(address):
                raise ConnectorError(f"Not a usable e-mail address: {str(item)[:80]!r}")
            cleaned.append(address)
        if _HEADER_INJECTION.search(subject) or _HEADER_INJECTION.search(body):
            raise ConnectorError("Subject and body must not contain line breaks or NULs.")
        title = subject.strip()
        if not title:
            raise ConnectorError("A subject is required.")
        if len(title) > MAX_SUBJECT:
            raise ConnectorError(f"Subject is longer than {MAX_SUBJECT} characters.")
        if len(body) > MAX_BODY:
            raise ConnectorError(f"Body is longer than {MAX_BODY} characters.")

        message = _StdEmailMessage()
        message["From"] = formataddr(("Omni-Assistant", self._username))
        message["To"] = ", ".join(cleaned)
        message["Subject"] = title
        message.set_content(body.strip())

        try:
            client = self._smtp_factory(self._smtp_host, self._smtp_port, timeout=self._timeout)
            try:
                client.ehlo()
                if hasattr(client, "starttls"):
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                client.login(self._username, self._password)
                client.send_message(message)
            finally:
                try:
                    client.quit()
                except (OSError, smtplib.SMTPException):
                    pass
        except (TimeoutError, OSError, smtplib.SMTPException) as exc:
            raise ConnectorError(f"SMTP delivery failed ({exc.__class__.__name__}).") from exc
        return {
            "connector": "email",
            "status": "sent",
            "recipients": cleaned,
            "subject": title,
            "bytes": len(message.as_bytes()),
        }


def _first_bytes(payload: Any) -> bytes:
    for item in payload:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return b""


def _headers_from_bytes(raw: bytes) -> EmailMessage | None:
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[:1] in {" ", "\t"}:
            continue
        key, _, value = line.partition(":")
        name = key.strip().lower()
        if name in {"from", "subject", "date", "message-id"} and name not in fields:
            fields[name] = value.strip()
    if not fields:
        return None
    return EmailMessage(
        message_id=_decode(fields.get("message-id", ""))[:200],
        sender=_decode(fields.get("from", ""))[:200],
        subject=_decode(fields.get("subject", ""))[:300],
        date=_decode(fields.get("date", ""))[:80],
    )
