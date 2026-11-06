"""Chat connector: one signed outbound notification endpoint (Phase 13).

The connector speaks a tiny, documented contract that Slack/Discord/Teams
incoming webhooks all accept in spirit: a JSON POST with a ``text`` field. Two
differences keep it safe:

* the endpoint is validated with the same SSRF guard the browser tool uses
  (no private/loopback addresses unless the user opted in, no odd ports, no
  credentials in the URL), and redirects are refused;
* every request carries an HMAC-SHA256 signature over ``timestamp.body`` so the
  receiving side can prove the message came from this machine.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..errors import ConnectorError
from .base import bounded_text, canonical_json, redact_endpoint

__all__ = ["MAX_TEXT", "ChatConnector", "DeliveryResult"]

MAX_TEXT = 2_000
MAX_RESPONSE = 4_096

Transport = Callable[[urllib.request.Request, float], tuple[int, bytes]]


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    ok: bool
    status_code: int
    target: str
    detail: str
    payload_bytes: int
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status_code": self.status_code,
            "target": self.target,
            "detail": self.detail,
            "payload_bytes": self.payload_bytes,
            "duration_ms": self.duration_ms,
        }


def _default_transport(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        return int(response.status), response.read(MAX_RESPONSE)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(
            req.full_url, code, "Redirects are not followed by the chat connector", headers, fp
        )


class ChatConnector:
    """Send one signed message to a configured chat webhook."""

    def __init__(
        self,
        url: str,
        *,
        secret: str,
        timeout: float = 15.0,
        allow_private_network: bool = False,
        transport: Transport | None = None,
        resolver: Any = None,
    ) -> None:
        self._url = url.strip()
        self._secret = secret
        self._timeout = max(1.0, min(float(timeout), 120.0))
        self._allow_private_network = bool(allow_private_network)
        self._transport = transport or _default_transport
        self._resolver = resolver

    def status(self) -> dict[str, Any]:
        issues: list[str] = []
        if not self._url:
            issues.append("PC_AGENT_CONNECTOR_CHAT_URL is not set")
        if not self._secret:
            issues.append("PC_AGENT_CONNECTOR_CHAT_SECRET is not set")
        usable = not issues
        if usable:
            self._validated_url()  # raises a ConnectorError with a precise reason
        return {
            "connector": "chat",
            "kind": "signed_webhook",
            "configured": usable,
            "target": redact_endpoint(self._url) if self._url else "none",
            "issues": issues,
        }

    def _validated_url(self) -> str:
        from ..errors import ToolError
        from ..web import validate_url

        try:
            kwargs: dict[str, Any] = {
                "allow_private_network": self._allow_private_network
            }
            if self._resolver is not None:
                kwargs["resolver"] = self._resolver
            return validate_url(self._url, **kwargs).url
        except ToolError as exc:
            raise ConnectorError(f"The chat endpoint was refused: {exc}") from exc

    def send(self, text: str, *, target: str = "") -> DeliveryResult:
        if not self._url:
            raise ConnectorError("The chat connector has no endpoint configured.")
        if not self._secret:
            raise ConnectorError(
                "The chat connector needs PC_AGENT_CONNECTOR_CHAT_SECRET to sign messages."
            )
        message = bounded_text(text, MAX_TEXT)
        if not message:
            raise ConnectorError("A chat message cannot be empty.")
        url = self._validated_url()
        payload = canonical_json(
            {
                "text": message,
                "source": "omni-assistant",
                "channel": bounded_text(target, 80),
                "ts": int(time.time()),
            }
        )
        stamp = str(int(time.time()))
        signature = _sign(self._secret, stamp, payload)
        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Omni-Assistant/connector",
                "X-PcAgent-Timestamp": stamp,
                "X-PcAgent-Signature": f"sha256={signature}",
            },
        )
        started = time.perf_counter()
        try:
            code, body = self._transport(request, self._timeout)
        except urllib.error.HTTPError as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            return DeliveryResult(
                ok=False,
                status_code=int(exc.code),
                target=redact_endpoint(url),
                detail=bounded_text(f"HTTP {exc.code} {exc.reason}", 200),
                payload_bytes=len(payload),
                duration_ms=elapsed,
            )
        except (urllib.error.URLError, OSError) as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            return DeliveryResult(
                ok=False,
                status_code=0,
                target=redact_endpoint(url),
                detail=bounded_text(f"{exc.__class__.__name__}: {exc}", 200),
                payload_bytes=len(payload),
                duration_ms=elapsed,
            )
        elapsed = int((time.perf_counter() - started) * 1000)
        ok = 200 <= code < 300
        detail = (
            "delivered"
            if ok
            else bounded_text(
                f"HTTP {code}: {body[:180].decode('utf-8', 'replace')}", 200
            )
        )
        return DeliveryResult(
            ok=ok,
            status_code=code,
            target=redact_endpoint(url),
            detail=detail,
            payload_bytes=len(payload),
            duration_ms=elapsed,
        )

    @staticmethod
    def describe_payload(text: str) -> str:
        return json.dumps({"text": bounded_text(text, MAX_TEXT)}, indent=2)


def _sign(secret: str, stamp: str, payload: bytes) -> str:
    import hashlib
    import hmac

    return hmac.new(
        secret.encode("utf-8"), stamp.encode("utf-8") + b"." + payload, hashlib.sha256
    ).hexdigest()
