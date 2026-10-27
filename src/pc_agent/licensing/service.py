"""Local license state: verification, trial clock, activation and gating.

Enforcement modes (``PC_AGENT_LICENSE_ENFORCEMENT``):

- ``off`` (default): licensing is reported but never blocks anything, so
  development builds and existing installs behave exactly as before.
- ``warn``: unlicensed features still run, but a warning is emitted.
- ``enforce``: unlicensed features fail with an actionable ``LicenseError``.

The trial clock only starts when enforcement is ``warn``/``enforce`` (or a
license command asks for status), so merely installing a development build
never burns trial days.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import platform
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..config import Settings
from ..errors import LicenseError
from ..storage.db import Database
from ..storage.repositories import LicenseRepository
from .models import (
    ALL_FEATURES,
    MAX_TOKEN_BYTES,
    PLAN_FEATURES,
    LicensePayload,
    b64u_decode,
    verify_token,
)

logger = logging.getLogger(__name__)

# Production builds embed the vendor public key(s) here at packaging time
# (Phase 8). Development/source builds configure PC_AGENT_LICENSE_PUBLIC_KEY.
VENDOR_PUBLIC_KEYS: dict[str, str] = {}

_TRIAL_STARTED_KEY = "trial_started_at"
_LAST_SEEN_KEY = "last_seen_at"
_CLOCK_ROLLBACK_TOLERANCE = timedelta(hours=24)


def _whole_days_remaining(expires_at: datetime, now: datetime) -> int:
    """Calendar-style remaining days: a fresh 365-day term reads as 365."""

    return max(0, math.ceil((expires_at - now).total_seconds() / 86_400))


def machine_fingerprint() -> str:
    """Privacy-preserving stable machine id (SHA-256 hex, never the raw id)."""

    raw_value: str | None = None
    source = "fallback"
    if os.name == "nt":  # pragma: no cover - exercised on Windows installs
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
            ) as key:
                raw_value, _kind = winreg.QueryValueEx(key, "MachineGuid")
            source = "windows-machine-guid"
        except OSError:
            raw_value = None
    if raw_value is None:
        for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                text = Path(candidate).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if text:
                raw_value = text
                source = "machine-id-file"
                break
    if raw_value is None:
        raw_value = f"{platform.node()}|{uuid.getnode()}"
        source = "node-fallback"
    digest = hashlib.sha256(
        f"pc-agent-machine-v1|{source}|{raw_value}".encode()
    ).hexdigest()
    return digest


@dataclass(frozen=True, slots=True)
class LicenseStatus:
    state: str
    enforcement: str
    configured: bool
    detail: str
    license_file: Path
    features: tuple[str, ...] = ()
    plan: str | None = None
    licensee: str | None = None
    license_id: str | None = None
    key_id: str | None = None
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    days_remaining: int | None = None
    machine_bound: bool = False
    clock_rollback: bool = False

    def allows(self, feature: str) -> bool:
        return self.state in {"licensed", "trial"} and feature in self.features

    @property
    def summary(self) -> str:
        """One-line, secret-free summary for doctor/GUI headers."""

        parts = [f"state={self.state}", f"enforcement={self.enforcement}"]
        if self.plan:
            parts.append(f"plan={self.plan}")
        if self.days_remaining is not None and self.state in {"licensed", "trial"}:
            parts.append(f"days_remaining={self.days_remaining}")
        if self.clock_rollback:
            parts.append("clock_rollback=yes")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "enforcement": self.enforcement,
            "configured": self.configured,
            "detail": self.detail,
            "license_file": str(self.license_file),
            "features": list(self.features),
            "plan": self.plan,
            "licensee": self.licensee,
            "license_id": self.license_id,
            "key_id": self.key_id,
            "issued_at": self.issued_at.isoformat() if self.issued_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "days_remaining": self.days_remaining,
            "machine_bound": self.machine_bound,
            "clock_rollback": self.clock_rollback,
        }


class LicenseService:
    """Deterministic local licensing over signed offline tokens."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        now_fn: Callable[[], datetime] | None = None,
        machine_id_fn: Callable[[], str] | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.database.initialize()
        self._repo = LicenseRepository(database)
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._machine_id_fn = machine_id_fn or machine_fingerprint

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------
    def public_keys(self) -> dict[str, bytes]:
        keys: dict[str, bytes] = {}
        for key_id, encoded in VENDOR_PUBLIC_KEYS.items():
            try:
                keys[key_id] = b64u_decode(encoded)
            except LicenseError:  # pragma: no cover - packaging-time constant
                continue
        if self.settings.license_public_key:
            keys[self.settings.license_key_id] = b64u_decode(
                self.settings.license_public_key
            )
        return keys

    def machine_id(self) -> str:
        return self._machine_id_fn()

    # ------------------------------------------------------------------
    # Status evaluation
    # ------------------------------------------------------------------
    def status(self) -> LicenseStatus:
        now = self._now_fn().astimezone(UTC)
        enforcement = self.settings.license_enforcement
        keys = self.public_keys()
        configured = bool(keys)
        rollback = self._update_clock(now) if enforcement != "off" else False

        token = self._read_token()
        if token is not None:
            if not configured:
                return LicenseStatus(
                    state="invalid",
                    enforcement=enforcement,
                    configured=False,
                    detail=(
                        "A license file is present but no vendor public key is "
                        "configured, so it cannot be verified. Set "
                        "PC_AGENT_LICENSE_PUBLIC_KEY in .env."
                    ),
                    license_file=self.settings.license_file,
                )
            verification = verify_token(
                token, keys, now=now, machine_id=self.machine_id()
            )
            payload = verification.payload
            if rollback:
                return self._status_from_payload(
                    "invalid",
                    enforcement,
                    configured,
                    "The system clock appears to have moved backwards by more "
                    "than 24 hours; license time checks fail closed.",
                    payload,
                    now,
                    clock_rollback=True,
                )
            if verification.valid and payload is not None:
                return self._status_from_payload(
                    "licensed", enforcement, configured, verification.message, payload, now
                )
            state = "expired" if verification.code == "expired" else "invalid"
            return self._status_from_payload(
                state, enforcement, configured, verification.message, payload, now
            )

        # No license file: fall back to the built-in trial (if enabled).
        trial_days = self.settings.trial_days
        if trial_days > 0:
            started = self._trial_started_at(now, enforcement)
            if started is None:
                state = "unconfigured" if not configured else "trial_available"
                return LicenseStatus(
                    state=state,
                    enforcement=enforcement,
                    configured=configured,
                    detail=(
                        "Licensing is disabled (development default). The "
                        f"{trial_days}-day trial has not started; it starts "
                        "automatically when enforcement is warn or enforce."
                    ),
                    license_file=self.settings.license_file,
                )
            expires = started + timedelta(days=trial_days)
            if rollback:
                return LicenseStatus(
                    state="invalid",
                    enforcement=enforcement,
                    configured=configured,
                    detail=(
                        "The system clock appears to have moved backwards by "
                        "more than 24 hours; the trial fails closed."
                    ),
                    license_file=self.settings.license_file,
                    plan="trial",
                    issued_at=started,
                    expires_at=expires,
                    clock_rollback=True,
                )
            if now <= expires:
                return LicenseStatus(
                    state="trial",
                    enforcement=enforcement,
                    configured=configured,
                    detail=f"Built-in {trial_days}-day trial is active.",
                    license_file=self.settings.license_file,
                    features=tuple(sorted(PLAN_FEATURES["trial"])),
                    plan="trial",
                    issued_at=started,
                    expires_at=expires,
                    days_remaining=_whole_days_remaining(expires, now),
                )
            return LicenseStatus(
                state="trial_expired",
                enforcement=enforcement,
                configured=configured,
                detail="The built-in trial has ended. Activate a license to continue.",
                license_file=self.settings.license_file,
                plan="trial",
                issued_at=started,
                expires_at=expires,
                days_remaining=0,
            )

        state = "unconfigured" if not configured and enforcement == "off" else "unlicensed"
        return LicenseStatus(
            state=state,
            enforcement=enforcement,
            configured=configured,
            detail="No license is activated and the built-in trial is disabled.",
            license_file=self.settings.license_file,
        )

    def _status_from_payload(
        self,
        state: str,
        enforcement: str,
        configured: bool,
        detail: str,
        payload: LicensePayload | None,
        now: datetime,
        *,
        clock_rollback: bool = False,
    ) -> LicenseStatus:
        if payload is None:
            return LicenseStatus(
                state=state,
                enforcement=enforcement,
                configured=configured,
                detail=detail,
                license_file=self.settings.license_file,
                clock_rollback=clock_rollback,
            )
        days_remaining = (
            _whole_days_remaining(payload.expires_at, now) if payload.expires_at else None
        )
        return LicenseStatus(
            state=state,
            enforcement=enforcement,
            configured=configured,
            detail=detail,
            license_file=self.settings.license_file,
            features=tuple(sorted(payload.features)) if state == "licensed" else (),
            plan=payload.plan,
            licensee=payload.licensee,
            license_id=payload.license_id,
            key_id=payload.key_id,
            issued_at=payload.issued_at,
            expires_at=payload.expires_at,
            days_remaining=days_remaining,
            machine_bound=payload.machine_id is not None,
            clock_rollback=clock_rollback,
        )

    def _trial_started_at(self, now: datetime, enforcement: str) -> datetime | None:
        raw = self._repo.get_state(_TRIAL_STARTED_KEY)
        if raw:
            try:
                return datetime.fromisoformat(raw).astimezone(UTC)
            except ValueError:
                logger.warning("Stored trial start value is unreadable; restarting trial clock.")
        if enforcement == "off":
            return None
        self._repo.set_state(_TRIAL_STARTED_KEY, now.isoformat())
        self._repo.record_event(
            "trial_started", detail={"trial_days": self.settings.trial_days}
        )
        return now

    def _update_clock(self, now: datetime) -> bool:
        raw = self._repo.get_state(_LAST_SEEN_KEY)
        if raw:
            try:
                last_seen = datetime.fromisoformat(raw).astimezone(UTC)
            except ValueError:
                last_seen = None
            if last_seen is not None:
                if now < last_seen - _CLOCK_ROLLBACK_TOLERANCE:
                    return True
                if now <= last_seen:
                    return False
        self._repo.set_state(_LAST_SEEN_KEY, now.isoformat())
        return False

    def _read_token(self) -> str | None:
        path = self.settings.license_file
        try:
            if not path.is_file():
                return None
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LicenseError(f"License file could not be read at {path}.") from exc
        except UnicodeDecodeError as exc:
            raise LicenseError("License file is not valid UTF-8 text.") from exc
        cleaned = text.strip()
        return cleaned or None

    # ------------------------------------------------------------------
    # Activation lifecycle
    # ------------------------------------------------------------------
    def activate(self, token: str) -> LicenseStatus:
        cleaned = token.strip()
        if not cleaned or len(cleaned.encode("utf-8", errors="ignore")) > MAX_TOKEN_BYTES:
            raise LicenseError("License token is empty or too large.")
        keys = self.public_keys()
        if not keys:
            raise LicenseError(
                "No vendor public key is configured, so this license cannot be "
                "verified. Set PC_AGENT_LICENSE_PUBLIC_KEY (and "
                "PC_AGENT_LICENSE_KEY_ID if needed) in .env first."
            )
        now = self._now_fn().astimezone(UTC)
        verification = verify_token(cleaned, keys, now=now, machine_id=self.machine_id())
        if not verification.valid or verification.payload is None:
            raise LicenseError(
                f"License was not activated: {verification.message} "
                "Any previously activated license is unchanged."
            )
        path = self.settings.license_file
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(cleaned + "\n", encoding="utf-8")
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:  # Windows ACLs differ; best effort only.
            pass
        self._repo.record_event(
            "license_activated",
            license_id=verification.payload.license_id,
            detail={
                "plan": verification.payload.plan,
                "token_sha256": hashlib.sha256(cleaned.encode("utf-8")).hexdigest(),
            },
        )
        return self.status()

    def activate_file(self, source: Path) -> LicenseStatus:
        try:
            if not source.is_file():
                raise LicenseError(f"License file was not found: {source}")
            if source.stat().st_size > MAX_TOKEN_BYTES:
                raise LicenseError("License file is too large to be a license token.")
            text = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise LicenseError(f"License file could not be read at {source}.") from exc
        except UnicodeDecodeError as exc:
            raise LicenseError("License file is not valid UTF-8 text.") from exc
        return self.activate(text)

    def deactivate(self) -> LicenseStatus:
        path = self.settings.license_file
        removed = False
        if path.is_file():
            path.unlink()
            removed = True
        self._repo.record_event("license_deactivated", detail={"removed": removed})
        return self.status()

    # ------------------------------------------------------------------
    # Feature gating
    # ------------------------------------------------------------------
    def require(
        self, feature: str, *, warn: Callable[[str], None] | None = None
    ) -> LicenseStatus:
        if feature not in ALL_FEATURES:
            raise LicenseError(f"Unknown licensed feature: {feature}")
        status = self.status()
        if status.enforcement == "off" or status.allows(feature):
            return status
        message = (
            f"Feature '{feature}' is not available right now "
            f"(license state: {status.state}; {status.detail}) "
            "Run 'pc-agent license status' for details."
        )
        if status.enforcement == "warn":
            logger.warning("License warning: %s", message)
            if warn is not None:
                warn(message + " Continuing because enforcement=warn.")
            return status
        raise LicenseError(message)
