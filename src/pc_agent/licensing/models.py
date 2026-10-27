"""License plans, signed-token format and deterministic verification.

A license token is ``PCA1.<base64url canonical-json>.<base64url signature>``.
The signed bytes are a fixed domain prefix plus the canonical JSON payload,
so a token minted for another product or purpose can never verify here.
Only the vendor's public key is needed on a customer machine; the private
signing key never ships with the product.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..errors import LicenseError
from . import ed25519

PRODUCT_ID = "self-healing-pc-agent"
TOKEN_FORMAT_VERSION = 1
_SIGNED_PREFIX = b"PCA-LICENSE-V1\n"
_CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)

FEATURE_CORE_TASKS = "core.tasks"
FEATURE_CORE_RECOVERY = "core.recovery"
FEATURE_DOCUMENTS = "documents"
FEATURE_WEB = "web"
FEATURE_DESKTOP = "desktop"
FEATURE_GUI = "gui"
FEATURE_PROVIDERS = "providers"
FEATURE_TOOL_BUILDER = "tool_builder"

ALL_FEATURES = frozenset(
    {
        FEATURE_CORE_TASKS,
        FEATURE_CORE_RECOVERY,
        FEATURE_DOCUMENTS,
        FEATURE_WEB,
        FEATURE_DESKTOP,
        FEATURE_GUI,
        FEATURE_PROVIDERS,
        FEATURE_TOOL_BUILDER,
    }
)

# Plan -> features included before any per-license extra features are added.
PLAN_FEATURES: dict[str, frozenset[str]] = {
    "trial": ALL_FEATURES,
    "standard": ALL_FEATURES - {FEATURE_TOOL_BUILDER},
    "pro": ALL_FEATURES,
    "enterprise": ALL_FEATURES,
}

MAX_TOKEN_BYTES = 65_536


def b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64u_decode(text: str) -> bytes:
    cleaned = text.strip()
    if not cleaned:
        raise LicenseError("Expected a non-empty base64url value.")
    padded = cleaned + "=" * (-len(cleaned) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise LicenseError("Value is not valid base64url data.") from exc


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(raw: object, field_name: str) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise LicenseError(f"License field '{field_name}' must be an ISO-8601 timestamp.")
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LicenseError(f"License field '{field_name}' is not a valid timestamp.") from exc
    if parsed.tzinfo is None:
        raise LicenseError(f"License field '{field_name}' must include a timezone.")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class LicensePayload:
    license_id: str
    licensee: str
    plan: str
    issued_at: datetime
    expires_at: datetime | None
    extra_features: tuple[str, ...]
    seats: int
    machine_id: str | None
    key_id: str

    @property
    def features(self) -> frozenset[str]:
        return PLAN_FEATURES[self.plan] | frozenset(self.extra_features)

    def to_dict(self) -> dict[str, object]:
        return {
            "v": TOKEN_FORMAT_VERSION,
            "product": PRODUCT_ID,
            "license_id": self.license_id,
            "licensee": self.licensee,
            "plan": self.plan,
            "issued_at": _format_time(self.issued_at),
            "expires_at": _format_time(self.expires_at) if self.expires_at else None,
            "features": list(self.extra_features),
            "seats": self.seats,
            "machine_id": self.machine_id,
            "key_id": self.key_id,
        }


@dataclass(frozen=True, slots=True)
class LicenseVerification:
    valid: bool
    code: str
    message: str
    payload: LicensePayload | None = None


def _payload_from_dict(data: Mapping[str, object]) -> LicensePayload:
    if data.get("v") != TOKEN_FORMAT_VERSION:
        raise LicenseError("unsupported license format version")
    if data.get("product") != PRODUCT_ID:
        raise LicenseError("license was issued for a different product")
    plan = data.get("plan")
    if plan not in PLAN_FEATURES:
        raise LicenseError("license contains an unknown plan")
    license_id = data.get("license_id")
    licensee = data.get("licensee")
    key_id = data.get("key_id")
    if not isinstance(license_id, str) or not (8 <= len(license_id) <= 64):
        raise LicenseError("license_id is missing or invalid")
    if not isinstance(licensee, str) or not licensee.strip() or len(licensee) > 200:
        raise LicenseError("licensee is missing or invalid")
    if not isinstance(key_id, str) or not key_id.strip() or len(key_id) > 64:
        raise LicenseError("key_id is missing or invalid")
    raw_features = data.get("features", [])
    if not isinstance(raw_features, list) or len(raw_features) > len(ALL_FEATURES):
        raise LicenseError("features must be a short list")
    extras: list[str] = []
    for feature in raw_features:
        if feature not in ALL_FEATURES:
            raise LicenseError("license contains an unknown feature")
        if feature not in extras:
            extras.append(str(feature))
    seats = data.get("seats", 1)
    if not isinstance(seats, int) or isinstance(seats, bool) or not (1 <= seats <= 100_000):
        raise LicenseError("seats must be a positive integer")
    machine_id = data.get("machine_id")
    if machine_id is not None and (
        not isinstance(machine_id, str) or not (8 <= len(machine_id) <= 128)
    ):
        raise LicenseError("machine_id is invalid")
    expires_raw = data.get("expires_at")
    return LicensePayload(
        license_id=license_id,
        licensee=licensee,
        plan=str(plan),
        issued_at=_parse_time(data.get("issued_at"), "issued_at"),
        expires_at=_parse_time(expires_raw, "expires_at") if expires_raw is not None else None,
        extra_features=tuple(extras),
        seats=seats,
        machine_id=machine_id,
        key_id=key_id,
    )


def canonical_payload_bytes(payload: LicensePayload) -> bytes:
    return json.dumps(
        payload.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def issue_token(
    private_seed: bytes,
    *,
    key_id: str,
    licensee: str,
    plan: str,
    expires_at: datetime | None,
    extra_features: Sequence[str] = (),
    seats: int = 1,
    machine_id: str | None = None,
    license_id: str | None = None,
    issued_at: datetime | None = None,
) -> str:
    """Mint a signed license token. Vendor-side only; keep the seed secret."""

    payload = LicensePayload(
        license_id=license_id or uuid.uuid4().hex,
        licensee=licensee.strip(),
        plan=plan,
        issued_at=(issued_at or datetime.now(UTC)).astimezone(UTC),
        expires_at=expires_at.astimezone(UTC) if expires_at else None,
        extra_features=tuple(dict.fromkeys(extra_features)),
        seats=seats,
        machine_id=machine_id,
        key_id=key_id,
    )
    # Re-run the same structural validation that verification applies, so the
    # vendor can never mint a token this build would reject as malformed.
    _payload_from_dict(payload.to_dict())
    if payload.expires_at is not None and payload.expires_at <= payload.issued_at:
        raise LicenseError("expires_at must be after issued_at.")
    canonical = canonical_payload_bytes(payload)
    signature = ed25519.sign(private_seed, _SIGNED_PREFIX + canonical)
    return f"PCA1.{b64u_encode(canonical)}.{b64u_encode(signature)}"


def verify_token(
    token: str,
    public_keys: Mapping[str, bytes],
    *,
    now: datetime,
    machine_id: str | None = None,
) -> LicenseVerification:
    """Verify structure, signature, product, term and optional machine binding."""

    cleaned = token.strip()
    if len(cleaned.encode("utf-8", errors="ignore")) > MAX_TOKEN_BYTES:
        return LicenseVerification(False, "malformed", "License token is too large.")
    parts = cleaned.split(".")
    if len(parts) != 3 or parts[0] != "PCA1":
        return LicenseVerification(
            False, "malformed", "License token format is not recognized."
        )
    try:
        canonical = b64u_decode(parts[1])
        signature = b64u_decode(parts[2])
        data = json.loads(canonical.decode("utf-8"))
    except (LicenseError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return LicenseVerification(False, "malformed", f"License token cannot be decoded ({exc}).")
    if not isinstance(data, dict):
        return LicenseVerification(False, "malformed", "License payload must be a JSON object.")
    try:
        payload = _payload_from_dict(data)
    except LicenseError as exc:
        code = "malformed"
        if "different product" in str(exc):
            code = "wrong_product"
        return LicenseVerification(False, code, f"License is invalid: {exc}")
    if canonical_payload_bytes(payload) != canonical:
        return LicenseVerification(
            False, "malformed", "License payload is not in canonical signed form.", payload
        )
    public_key = public_keys.get(payload.key_id)
    if public_key is None:
        return LicenseVerification(
            False,
            "unknown_key",
            f"No configured public key matches license key id '{payload.key_id}'.",
            payload,
        )
    if not ed25519.verify(public_key, _SIGNED_PREFIX + canonical, signature):
        return LicenseVerification(False, "bad_signature", "License signature does not verify.", payload)
    current = now.astimezone(UTC)
    if payload.issued_at > current + _CLOCK_SKEW_TOLERANCE:
        return LicenseVerification(
            False, "not_yet_valid", "License was issued in the future; check the system clock.", payload
        )
    if payload.expires_at is not None and current > payload.expires_at:
        return LicenseVerification(False, "expired", "License term has ended.", payload)
    if payload.machine_id is not None and machine_id != payload.machine_id:
        return LicenseVerification(
            False, "machine_mismatch", "License is bound to a different machine.", payload
        )
    return LicenseVerification(True, "ok", "License verified.", payload)
