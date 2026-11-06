"""Shared connector primitives (Transformation Phase 13).

Small, dependency-free helpers that every connector uses so that limits,
redaction and timestamp formats are identical everywhere. Nothing here talks to
a network or a database.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "MAX_SUMMARY",
    "GateState",
    "body_sha256",
    "bounded_text",
    "canonical_json",
    "constant_time_equals",
    "redact_endpoint",
    "utc_now",
    "utc_stamp",
]

MAX_SUMMARY = 300
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_stamp(moment: datetime | None = None) -> str:
    """ISO-8601 UTC seconds precision, the storage format for every ledger."""

    return (moment or utc_now()).astimezone(UTC).replace(microsecond=0).isoformat()


def bounded_text(value: Any, limit: int = MAX_SUMMARY) -> str:
    """Collapse control characters and clamp length (log/message safety)."""

    text = _CONTROL_RE.sub(" ", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def redact_endpoint(url: str) -> str:
    """``https://host/path`` with query, fragment and userinfo removed.

    Chat and webhook URLs routinely contain tokens in the query string, so the
    only safe thing to print or store is scheme + host + path.
    """

    try:
        parsed = urllib.parse.urlsplit(str(url).strip())
    except ValueError:
        return "invalid-url"
    if not parsed.hostname:
        return "invalid-url"
    host = parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path if parsed.path and parsed.path != "/" else ""
    return f"{parsed.scheme or 'https'}://{host}{port}{path}"


def canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True, slots=True)
class GateState:
    """Why a connector is (not) usable, in words the user can act on."""

    name: str
    enabled: bool
    configured: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "connector": self.name,
            "enabled": self.enabled,
            "configured": self.configured,
            "reason": self.reason,
        }
