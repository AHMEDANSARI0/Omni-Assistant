"""Inbound webhook connector: verify, then turn into a local event (Phase 13).

Inbound traffic is where prompt-injection and replay attacks arrive, so this
connector is a *verifier*, not a listener: it is handed a body, a timestamp and
a signature by whatever transport the user chose (CLI, API route, future tunnel)
and it answers a single question - is this really from the holder of the shared
secret, is it fresh, and have we already seen it?

Accepted events are recorded in ``connector_events`` and emitted as app events,
which flow triggers can react to. Payload text is truncated and control
characters are stripped before it is stored, so a hostile payload cannot become
a formatting or log-injection vector.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..errors import ConnectorError
from .base import bounded_text

__all__ = ["MAX_BODY_BYTES", "WebhookConnector", "WebhookReceipt", "sign_payload"]

MAX_BODY_BYTES = 64 * 1024
MIN_SKEW = 30
MAX_SKEW = 86_400


@dataclass(frozen=True, slots=True)
class WebhookReceipt:
    accepted: bool
    reason: str
    event_id: str = ""
    kind: str = ""
    summary: str = ""
    external_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "event_id": self.event_id,
            "kind": self.kind,
            "summary": self.summary,
            "external_id": self.external_id,
            "payload": dict(self.payload),
        }


def sign_payload(secret: str, timestamp: str, body: bytes) -> str:
    """The exact signature the receiver expects: ``sha256=HMAC(secret, ts.body)``."""

    digest = hmac.new(
        secret.encode("utf-8"),
        str(timestamp).encode("utf-8") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


class WebhookConnector:
    """Constant-time HMAC verification with a bounded replay window."""

    def __init__(
        self,
        *,
        secret: str,
        skew_seconds: int = 300,
        clock: Any = time.time,
    ) -> None:
        self._secret = secret or ""
        self._skew = max(MIN_SKEW, min(int(skew_seconds), MAX_SKEW))
        self._clock = clock

    @property
    def skew_seconds(self) -> int:
        return self._skew

    def status(self) -> dict[str, Any]:
        return {
            "connector": "webhook",
            "kind": "inbound_hmac",
            "configured": bool(self._secret),
            "skew_seconds": self._skew,
            "issues": [] if self._secret else ["PC_AGENT_CONNECTOR_WEBHOOK_SECRET is not set"],
        }

    def verify(
        self,
        body: bytes,
        *,
        timestamp: str,
        signature: str,
        seen: set[str] | None = None,
    ) -> WebhookReceipt:
        if not self._secret:
            raise ConnectorError("Inbound webhooks need PC_AGENT_CONNECTOR_WEBHOOK_SECRET.")
        if not isinstance(body, (bytes, bytearray)):
            raise ConnectorError("The webhook body must be raw bytes.")
        raw = bytes(body)
        if len(raw) > MAX_BODY_BYTES:
            raise ConnectorError(
                f"The webhook body exceeds {MAX_BODY_BYTES} bytes and was refused."
            )
        if len(raw) == 0:
            return WebhookReceipt(accepted=False, reason="empty body")
        try:
            sent_at = int(str(timestamp).strip())
        except (TypeError, ValueError):
            return WebhookReceipt(accepted=False, reason="missing or non-numeric timestamp")
        drift = abs(int(self._clock()) - sent_at)
        if drift > self._skew:
            return WebhookReceipt(
                accepted=False,
                reason=f"timestamp is {drift}s outside the {self._skew}s window",
            )
        provided = str(signature or "").strip()
        if provided.lower().startswith("sha256="):
            provided = provided[7:]
        expected = sign_payload(self._secret, str(timestamp).strip(), raw)[7:]
        if not provided or not hmac.compare_digest(provided.encode(), expected.encode()):
            return WebhookReceipt(accepted=False, reason="signature mismatch")

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return WebhookReceipt(accepted=False, reason="body is not JSON")
        if not isinstance(payload, dict):
            return WebhookReceipt(accepted=False, reason="body must be a JSON object")

        kind = bounded_text(payload.get("kind") or payload.get("type") or "webhook", 40)
        external_id = bounded_text(payload.get("id") or payload.get("event_id") or "", 120)
        summary = bounded_text(
            payload.get("summary") or payload.get("text") or payload.get("message") or "",
            200,
        )
        if external_id:
            replay_key = f"{kind}:{external_id}"
            if seen is not None and replay_key in seen:
                return WebhookReceipt(
                    accepted=False, reason="replay: this event id was already accepted"
                )
        return WebhookReceipt(
            accepted=True,
            reason="verified",
            event_id=str(uuid.uuid4()),
            kind=kind or "webhook",
            summary=summary,
            external_id=external_id,
            payload=_sanitise(payload),
        )


def _sanitise(value: Any, *, depth: int = 0) -> dict[str, Any]:
    """Strip control characters and clamp sizes so stored payloads stay inert."""

    if depth > 3 or not isinstance(value, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for index, (key, item) in enumerate(value.items()):
        if index >= 40:
            break
        name = bounded_text(key, 60)
        if not name:
            continue
        if isinstance(item, str):
            cleaned[name] = bounded_text(item, 400)
        elif isinstance(item, (int, float, bool)) or item is None:
            cleaned[name] = item
        elif isinstance(item, dict):
            cleaned[name] = _sanitise(item, depth=depth + 1)
        elif isinstance(item, list):
            cleaned[name] = [
                bounded_text(entry, 200)
                for entry in item[:20]
                if isinstance(entry, (str, int, float, bool))
            ]
        else:
            cleaned[name] = bounded_text(item, 100)
    return cleaned
