"""Windows-first background auto-upgrade for signed online updates.

Phase 24 deliberately left online staging user-initiated and never applied a
downloaded bundle.  Phase 56 is the separately requested background layer.
It is still default-off and has a narrower trust policy than manual update
commands:

* both online updates and background auto-upgrade must be opted in;
* the feed must be HTTPS and signed updates must be required;
* a release public key and key ID must be configured;
* unsigned, downgrade and same-version bundles are never accepted;
* the default mode only checks, downloads, verifies and stages;
* ``apply-when-idle`` additionally requires the maintenance window, no
  actively executing task, the configured user-idle interval and AC power
  (when required) before it calls the existing backup/rollback apply path.

The worker is a one-shot command invoked by the operating-system
scheduler; it does not add a planner tool or a resident Python service.
Windows Scheduled Task and the Linux systemd user timer are implemented.
The macOS LaunchAgent profile remains reserved/planned.  Windows idle and
AC-power state come from Win32.  Linux idle state comes from the current
user's interactive systemd-logind sessions, while Linux AC state comes
from ``/sys/class/power_supply`` with logind's external-power value as a
fallback.  On every platform, an unknown required idle or power reading
defers application instead of being treated as permission to apply.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as datetime_time
from pathlib import Path
from typing import Any, Self

from . import __version__
from .config import Settings
from .errors import PcAgentError
from .logging_config import redact_text
from .online_updates import UpdateTransport, check_online_update, stage_online_update
from .updates import (
    apply_staged_update,
    b64url_decode,
    compare_versions,
    list_updates,
    parse_version,
    updates_root,
    validate_key_id,
)

AUTO_UPGRADE_FORMAT = 1
AUTO_UPGRADE_STATE_FILE = "auto-upgrade-state.json"
AUTO_UPGRADE_LOCK_FILE = "auto-upgrade.lock"
AUTO_UPGRADE_REPORT_DIR = "auto-upgrade-reports"
WINDOWS_SCHEDULED_TASK_PROFILE = "windows-scheduled-task"
LINUX_SYSTEMD_USER_PROFILE = "linux-systemd-user"
MACOS_LAUNCHD_PROFILE = "macos-launchd"
DEFAULT_AUTO_UPGRADE_TASK_NAME = "SelfHealingPCAgentAutoUpgrade"
DEFAULT_LINUX_AUTO_UPGRADE_UNIT_NAME = "self-healing-pc-agent-auto-upgrade"

_MAX_STATE_BYTES = 250_000
_MAX_REPORT_BYTES = 1_000_000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^auto-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
_TASK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$")
_SYSTEMD_UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_LINUX_SYSTEMD_MARKER = "X-SelfHealingPCAgent-Managed=auto-upgrade"
_MAX_SYSTEMD_UNIT_BYTES = 64_000
_LINUX_POWER_SUPPLY_ROOT = Path("/sys/class/power_supply")
_LINUX_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_LINUX_INTERACTIVE_SESSION_CLASSES = frozenset(
    {"lock-screen", "user", "user-early"}
)
_MAX_LINUX_SENSOR_OUTPUT_CHARS = 256_000
_MAX_LINUX_SENSOR_SESSIONS = 32
_MAX_LINUX_POWER_SUPPLIES = 128
_MODES = frozenset({"stage-only", "apply-when-idle"})
_ACTIVE_TASK_STATUSES = ("planning", "running", "verifying", "retrying")
_CHECK_NAMES = (
    "configuration",
    "single_instance_lock",
    "schedule_due",
    "maintenance_window",
    "feed_check",
    "download_and_stage",
    "idle_gate",
    "apply_update",
    "reinstall_metadata",
    "state_recorded",
)
_REPORT_STATUSES = frozenset(
    {
        "already_running",
        "applied",
        "applied_reinstall_failed",
        "deferred",
        "disabled",
        "failed",
        "misconfigured",
        "no_update",
        "not_due",
        "staged",
    }
)
_STATE_KEYS = {
    "app",
    "applied_update_id",
    "applied_version",
    "consecutive_failures",
    "format",
    "last_check_at",
    "last_reason",
    "last_run_at",
    "last_status",
    "latest_report",
    "next_check_at",
    "restart_required",
    "staged_update_id",
    "staged_version",
    "state_sha256",
    "updated_at",
}


@dataclass(frozen=True, slots=True)
class AutoUpgradeSchedulerProfile:
    profile_id: str
    platform: str
    display_name: str
    status: str
    description: str

    @property
    def implemented(self) -> bool:
        return self.status == "implemented"

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "display_name": self.display_name,
            "id": self.profile_id,
            "platform": self.platform,
            "status": self.status,
        }


AUTO_UPGRADE_SCHEDULER_PROFILES: tuple[AutoUpgradeSchedulerProfile, ...] = (
    AutoUpgradeSchedulerProfile(
        WINDOWS_SCHEDULED_TASK_PROFILE,
        "windows",
        "Windows Scheduled Task",
        "implemented",
        "Hourly/logon one-shot worker for the signed-in Windows user.",
    ),
    AutoUpgradeSchedulerProfile(
        LINUX_SYSTEMD_USER_PROFILE,
        "linux",
        "Linux systemd user timer",
        "implemented",
        "Per-user systemd service/timer that wakes the one-shot worker after boot/login and hourly.",
    ),
    AutoUpgradeSchedulerProfile(
        MACOS_LAUNCHD_PROFILE,
        "macos",
        "macOS LaunchAgent",
        "planned",
        "Reserved structure; macOS acceptance is deferred by request.",
    ),
)
_PROFILES_BY_ID = {
    profile.profile_id: profile for profile in AUTO_UPGRADE_SCHEDULER_PROFILES
}


def list_auto_upgrade_scheduler_profiles() -> tuple[AutoUpgradeSchedulerProfile, ...]:
    return AUTO_UPGRADE_SCHEDULER_PROFILES


def get_auto_upgrade_scheduler_profile(
    profile_id: str,
) -> AutoUpgradeSchedulerProfile:
    value = profile_id.strip().lower()
    if value not in _PROFILES_BY_ID:
        known = ", ".join(
            profile.profile_id for profile in AUTO_UPGRADE_SCHEDULER_PROFILES
        )
        raise PcAgentError(
            f"Unknown auto-upgrade scheduler profile. Known: {known}."
        )
    return _PROFILES_BY_ID[value]


def _require_windows_scheduler(profile: AutoUpgradeSchedulerProfile) -> None:
    if not profile.implemented:
        raise PcAgentError(
            f"Auto-upgrade scheduler profile {profile.profile_id} is reserved "
            f"for {profile.platform} but is not implemented in this build."
        )


def _raise_windows_scheduler_unavailable(platform_name: str | None) -> None:
    platform = _client_platform(platform_name)
    if platform == "linux":
        _require_windows_scheduler(
            get_auto_upgrade_scheduler_profile(LINUX_SYSTEMD_USER_PROFILE)
        )
    if platform == "macos":
        _require_windows_scheduler(
            get_auto_upgrade_scheduler_profile(MACOS_LAUNCHD_PROFILE)
        )
    raise PcAgentError(
        "The Windows auto-upgrade scheduler can run only on Windows."
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PcAgentError(f"Auto-upgrade {label} is invalid.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PcAgentError(f"Auto-upgrade {label} is invalid.") from exc
    if parsed.tzinfo is None:
        raise PcAgentError(f"Auto-upgrade {label} is invalid.")
    return parsed.astimezone(UTC)


def _safe_text(value: Any, *, limit: int = 300) -> str:
    text = "" if value is None else str(value)
    cleaned = "".join(
        char for char in text.strip() if ord(char) >= 32 and ord(char) != 127
    )
    return cleaned[:limit]


def _sanitize_error(error: BaseException | str) -> str:
    return _safe_text(redact_text(str(error)), limit=500) or (
        "Background auto-upgrade failed."
    )


def _client_platform(raw: str | None = None) -> str:
    value = (raw or sys.platform).lower()
    if value.startswith("win"):
        return "windows"
    if value.startswith("linux"):
        return "linux"
    if value == "darwin":
        return "macos"
    return "unknown"


def _is_windows(platform_name: str | None = None) -> bool:
    return _client_platform(platform_name) == "windows"


def _is_linux(platform_name: str | None = None) -> bool:
    return _client_platform(platform_name) == "linux"


def _ensure_auto_root(settings: Settings) -> Path:
    root = updates_root(settings)
    if root.is_symlink():
        raise PcAgentError("The updates folder must not be a symbolic link.")
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir() or root.is_symlink():
        raise PcAgentError("The updates folder is not a safe directory.")
    return root.resolve()


def auto_upgrade_state_path(settings: Settings) -> Path:
    return updates_root(settings) / AUTO_UPGRADE_STATE_FILE


def _default_state() -> dict[str, Any]:
    return {
        "app": "self-healing-pc-agent",
        "applied_update_id": None,
        "applied_version": None,
        "consecutive_failures": 0,
        "format": AUTO_UPGRADE_FORMAT,
        "last_check_at": None,
        "last_reason": None,
        "last_run_at": None,
        "last_status": None,
        "latest_report": None,
        "next_check_at": None,
        "restart_required": False,
        "staged_update_id": None,
        "staged_version": None,
        "updated_at": None,
    }


def _state_hash(state: dict[str, Any]) -> str:
    unsigned = dict(state)
    unsigned.pop("state_sha256", None)
    return _sha256(_canonical_bytes(unsigned))


def _validate_state(state: dict[str, Any]) -> dict[str, Any]:
    if set(state) != _STATE_KEYS:
        raise PcAgentError("Auto-upgrade state has an unexpected shape.")
    recorded = state["state_sha256"]
    if not isinstance(recorded, str) or _SHA256_RE.fullmatch(recorded) is None:
        raise PcAgentError("Auto-upgrade state hash is invalid.")
    if _state_hash(state) != recorded:
        raise PcAgentError("Auto-upgrade state hash does not match.")
    if (
        state["app"] != "self-healing-pc-agent"
        or state["format"] != AUTO_UPGRADE_FORMAT
        or isinstance(state["consecutive_failures"], bool)
        or not isinstance(state["consecutive_failures"], int)
        or not 0 <= state["consecutive_failures"] <= 10_000
        or not isinstance(state["restart_required"], bool)
    ):
        raise PcAgentError("Auto-upgrade state is invalid.")
    for key in (
        "last_check_at",
        "last_run_at",
        "next_check_at",
        "updated_at",
    ):
        if state[key] is not None:
            _parse_time(state[key], label=key)
    for key in ("last_reason", "last_status", "latest_report"):
        if state[key] is not None and (
            not isinstance(state[key], str) or len(state[key]) > 500
        ):
            raise PcAgentError("Auto-upgrade state text is invalid.")
    if state["last_status"] is not None and state["last_status"] not in _REPORT_STATUSES:
        raise PcAgentError("Auto-upgrade state status is invalid.")
    for id_key, version_key in (
        ("staged_update_id", "staged_version"),
        ("applied_update_id", "applied_version"),
    ):
        update_id = state[id_key]
        version = state[version_key]
        if (update_id is None) != (version is None):
            raise PcAgentError("Auto-upgrade state update record is incomplete.")
        if update_id is not None:
            if not isinstance(update_id, str) or len(update_id) > 160:
                raise PcAgentError("Auto-upgrade state update ID is invalid.")
            parse_version(str(version))
    return state


def _read_state(settings: Settings) -> dict[str, Any]:
    path = auto_upgrade_state_path(settings)
    if not path.exists():
        state = _default_state()
        state["state_sha256"] = _state_hash(state)
        return state
    if path.is_symlink() or not path.is_file():
        raise PcAgentError("Auto-upgrade state is not a safe file.")
    if path.stat().st_size > _MAX_STATE_BYTES:
        raise PcAgentError("Auto-upgrade state is too large.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PcAgentError("Auto-upgrade state is unreadable.") from exc
    if not isinstance(payload, dict):
        raise PcAgentError("Auto-upgrade state is invalid.")
    return _validate_state(payload)


def _write_state(settings: Settings, state: dict[str, Any], *, now: datetime) -> Path:
    root = _ensure_auto_root(settings)
    path = root / AUTO_UPGRADE_STATE_FILE
    updated = dict(state)
    updated.pop("state_sha256", None)
    updated["updated_at"] = _format_time(now)
    updated["state_sha256"] = _state_hash(updated)
    _validate_state(updated)
    data = _json_bytes(updated)
    temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(4)}")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    state.clear()
    state.update(updated)
    return path


class _AutoUpgradeLock:
    """Non-blocking cross-platform file lock held for one worker run."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any | None = None
        self._lock_offset: int | None = None
        self.acquired = False

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                # msvcrt.locking locks bytes starting at the *current*
                # file position.  An existing lock file opened with
                # ``a+b`` starts positioned at EOF, so locking before this
                # seek would lock a variable EOF byte and the byte-0 unlock
                # in __exit__ would correctly be rejected by Windows with
                # PermissionError on the second worker run.  Always lock
                # the same first byte.
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                self._lock_offset = 0
            else:
                import fcntl

                fcntl.flock(
                    self._handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            self.acquired = True
            self._handle.seek(0)
            self._handle.truncate()
            self._handle.write(
                f"pid={os.getpid()}; started={_format_time(_utc_now())}\n".encode()
            )
            self._handle.flush()
        except OSError:
            self.acquired = False
            self._lock_offset = None
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._handle is None:
            return
        try:
            if self.acquired:
                if sys.platform == "win32":
                    import msvcrt

                    self._handle.seek(self._lock_offset or 0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
            self._lock_offset = None
            self.acquired = False


def _config_blockers(settings: Settings) -> list[str]:
    blockers: list[str] = []
    if settings.auto_upgrade_mode not in _MODES:
        blockers.append("auto-upgrade mode is invalid")
    if not settings.online_updates_enabled:
        blockers.append("online updates are disabled")
    feed_url = (settings.update_feed_url or "").strip()
    if not feed_url:
        blockers.append("no update feed URL is configured")
    else:
        parsed = urllib.parse.urlsplit(feed_url)
        if parsed.scheme.lower() != "https":
            blockers.append("background auto-upgrade requires an HTTPS feed")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            blockers.append("update feed URL contains forbidden components")
    if not settings.release_public_key:
        blockers.append("no release public key is configured")
    else:
        try:
            b64url_decode(
                settings.release_public_key,
                expected_length=32,
                label="Release public key",
            )
        except PcAgentError:
            blockers.append("configured release public key is invalid")
    if not settings.release_key_id:
        blockers.append("no release key ID is configured")
    else:
        try:
            validate_key_id(settings.release_key_id)
        except PcAgentError:
            blockers.append("configured release key ID is invalid")
    if not settings.require_signed_updates:
        blockers.append("signed updates are not required")
    if not 1 <= int(settings.auto_upgrade_interval_hours) <= 168:
        blockers.append("auto-upgrade interval is invalid")
    if not 0 <= int(settings.auto_upgrade_idle_minutes) <= 240:
        blockers.append("auto-upgrade idle threshold is invalid")
    try:
        _parse_maintenance_window(settings.auto_upgrade_maintenance_window)
    except PcAgentError:
        blockers.append("auto-upgrade maintenance window is invalid")
    return blockers


def _parse_maintenance_window(
    value: str | None,
) -> tuple[datetime_time, datetime_time] | None:
    if value is None or not str(value).strip():
        return None
    match = re.fullmatch(
        r"([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)",
        str(value).strip(),
    )
    if match is None:
        raise PcAgentError("Auto-upgrade maintenance window is invalid.")
    start = datetime_time(int(match.group(1)), int(match.group(2)))
    end = datetime_time(int(match.group(3)), int(match.group(4)))
    if start == end:
        raise PcAgentError("Auto-upgrade maintenance window is invalid.")
    return start, end


def _in_maintenance_window(
    local_now: datetime,
    window: tuple[datetime_time, datetime_time] | None,
) -> bool:
    if window is None:
        return True
    start, end = window
    current = local_now.timetz().replace(tzinfo=None)
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _next_window_start_utc(
    local_now: datetime,
    window: tuple[datetime_time, datetime_time],
) -> datetime:
    start, _end = window
    candidate = local_now.replace(
        hour=start.hour, minute=start.minute, second=0, microsecond=0
    )
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def count_active_tasks(settings: Settings) -> int:
    """Count actively executing tasks without creating or migrating the DB."""

    path = settings.database_path.expanduser()
    if not path.exists():
        return 0
    if path.is_symlink() or not path.is_file():
        raise PcAgentError("The task database is not a safe file.")
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=2.0) as connection:
            placeholders = ",".join("?" for _ in _ACTIVE_TASK_STATUSES)
            row = connection.execute(
                f"SELECT COUNT(*) FROM tasks WHERE status IN ({placeholders})",
                _ACTIVE_TASK_STATUSES,
            ).fetchone()
    except sqlite3.Error as exc:
        raise PcAgentError("Active task state could not be read safely.") from exc
    return int(row[0]) if row is not None else 0


def _default_linux_sensor_runner(
    argv: Sequence[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    # loginctl must talk to the canonical system logind instance.  A stray
    # user-controlled system-bus address must not be able to fabricate an
    # idle or external-power reading for the background apply gate.
    env.pop("DBUS_SYSTEM_BUS_ADDRESS", None)
    return subprocess.run(
        [str(part) for part in argv],
        capture_output=True,
        env=env,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _run_linux_sensor_command(
    argv: Sequence[str],
    *,
    runner: Callable[
        [Sequence[str], float], subprocess.CompletedProcess[str]
    ]
    | None,
) -> str | None:
    try:
        completed = (runner or _default_linux_sensor_runner)(argv, 5.0)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if completed.returncode != 0 or not isinstance(completed.stdout, str):
        return None
    if len(completed.stdout) > _MAX_LINUX_SENSOR_OUTPUT_CHARS:
        return None
    return completed.stdout


def _parse_loginctl_properties(text: str) -> dict[str, str] | None:
    properties: dict[str, str] = {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or len(lines) > 128:
        return None
    for line in lines:
        key, separator, value = line.partition("=")
        key = key.strip()
        if (
            not separator
            or not key
            or len(key) > 64
            or not key.replace("_", "").isalnum()
            or key in properties
        ):
            return None
        properties[key] = value.strip()
    return properties


def _parse_nonnegative_int(value: str | None) -> int | None:
    if value is None or not re.fullmatch(r"[0-9]{1,20}", value.strip()):
        return None
    return int(value)


def _linux_session_ids_for_uid(
    text: str, *, uid: int
) -> list[str] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or len(payload) > 512:
        return None
    session_ids: list[str] = []
    seen: set[str] = set()
    for row in payload:
        if not isinstance(row, dict):
            return None
        row_uid = row.get("uid")
        row_session = row.get("session")
        if (
            isinstance(row_uid, bool)
            or not isinstance(row_uid, int)
            or row_uid < 0
            or not isinstance(row_session, str)
            or _LINUX_SESSION_ID_RE.fullmatch(row_session) is None
        ):
            return None
        if row_uid != uid:
            continue
        if row_session in seen:
            return None
        seen.add(row_session)
        session_ids.append(row_session)
        if len(session_ids) > _MAX_LINUX_SENSOR_SESSIONS:
            return None
    return session_ids


def _get_linux_idle_seconds(
    *,
    runner: Callable[
        [Sequence[str], float], subprocess.CompletedProcess[str]
    ]
    | None,
    loginctl: str,
    linux_uid: int | None,
    monotonic_seconds: float | None,
) -> float | None:
    if linux_uid is not None:
        uid = linux_uid
    elif hasattr(os, "getuid"):
        uid = int(os.getuid())  # type: ignore[attr-defined]
    else:
        return None
    if isinstance(uid, bool) or uid < 0:
        return None

    list_output = _run_linux_sensor_command(
        [
            loginctl,
            "--no-ask-password",
            "--no-pager",
            "list-sessions",
            "--json=short",
        ],
        runner=runner,
    )
    if list_output is None:
        return None
    session_ids = _linux_session_ids_for_uid(list_output, uid=uid)
    if not session_ids:
        return None

    now_us = round(
        (time.monotonic() if monotonic_seconds is None else monotonic_seconds)
        * 1_000_000
    )
    if now_us < 0:
        return None

    idle_values: list[float] = []
    for session_id in session_ids:
        show_output = _run_linux_sensor_command(
            [
                loginctl,
                "--no-ask-password",
                "--no-pager",
                "show-session",
                session_id,
                "--property=Id",
                "--property=Class",
                "--property=State",
                "--property=IdleHint",
                "--property=IdleSinceHintMonotonic",
            ],
            runner=runner,
        )
        if show_output is None:
            return None
        properties = _parse_loginctl_properties(show_output)
        if properties is None or properties.get("Id") != session_id:
            return None
        session_class = properties.get("Class", "").strip().lower()
        if session_class not in _LINUX_INTERACTIVE_SESSION_CLASSES:
            # User-manager/background sessions do not represent keyboard or
            # pointer activity.  Unknown classes are deliberately ignored
            # here, but the function still returns None below unless at
            # least one real interactive session supplied a valid reading.
            continue
        state = properties.get("State", "").strip().lower()
        if state == "closing":
            continue
        if state not in {"active", "online"}:
            return None
        idle_hint = properties.get("IdleHint", "").strip().lower()
        if idle_hint == "no":
            return 0.0
        if idle_hint != "yes":
            return None
        idle_since_us = _parse_nonnegative_int(
            properties.get("IdleSinceHintMonotonic")
        )
        if idle_since_us is None or idle_since_us <= 0:
            return None
        # Allow one second of clock/reporting skew, but never accept a
        # future or sentinel timestamp as a huge idle interval.
        if idle_since_us > now_us + 1_000_000:
            return None
        idle_values.append(max(0, now_us - idle_since_us) / 1_000_000.0)

    if not idle_values:
        return None
    return min(idle_values)


def get_idle_seconds(
    platform_name: str | None = None,
    *,
    runner: Callable[
        [Sequence[str], float], subprocess.CompletedProcess[str]
    ]
    | None = None,
    loginctl: str = "loginctl",
    linux_uid: int | None = None,
    monotonic_seconds: float | None = None,
) -> float | None:
    """Return user idle time in seconds; ``None`` means unavailable.

    Windows uses ``GetLastInputInfo``.  Linux uses systemd-logind's
    per-session idle hint and monotonic timestamp for interactive sessions
    belonging to the current UID.  Unsupported or ambiguous readings are
    always ``None`` so the apply gate can fail closed.
    """

    if _is_linux(platform_name):
        return _get_linux_idle_seconds(
            runner=runner,
            loginctl=loginctl,
            linux_uid=linux_uid,
            monotonic_seconds=monotonic_seconds,
        )
    if not _is_windows(platform_name):
        return None
    try:
        import ctypes

        class LastInputInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("dwTime", ctypes.c_uint32),
            ]

        info = LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        tick = int(ctypes.windll.kernel32.GetTickCount64())
        elapsed_ms = ((tick & 0xFFFFFFFF) - int(info.dwTime)) & 0xFFFFFFFF
        return elapsed_ms / 1000.0
    except (AttributeError, OSError, ValueError):
        return None


def _read_power_supply_value(path: Path) -> str | None:
    try:
        with path.open("r", encoding="ascii") as handle:
            value = handle.read(129)
    except (OSError, UnicodeDecodeError):
        return None
    if len(value) > 128:
        return None
    value = value.strip()
    return value if value else None


def _get_linux_ac_power_from_sysfs(root: Path) -> bool | None:
    try:
        entries = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return None
    if not entries or len(entries) > _MAX_LINUX_POWER_SUPPLIES:
        return None

    mains_seen = False
    mains_online = False
    mains_unknown = False
    battery_statuses: list[str] = []
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        supply_type = _read_power_supply_value(entry / "type")
        if supply_type is None:
            continue
        supply_type = supply_type.lower()
        if supply_type == "mains":
            online = _read_power_supply_value(entry / "online")
            if online == "1":
                mains_seen = True
                mains_online = True
            elif online == "0":
                mains_seen = True
            else:
                mains_unknown = True
        elif supply_type == "battery":
            present = _read_power_supply_value(entry / "present")
            if present == "0":
                continue
            status = _read_power_supply_value(entry / "status")
            if status is not None:
                battery_statuses.append(status.lower())

    if mains_online:
        return True
    if mains_unknown:
        return None
    if mains_seen:
        return False
    if not battery_statuses:
        return None
    if any(status == "discharging" for status in battery_statuses):
        return False
    if all(status in {"charging", "full"} for status in battery_statuses):
        return True
    return None


def _get_linux_ac_power_from_logind(
    *,
    runner: Callable[
        [Sequence[str], float], subprocess.CompletedProcess[str]
    ]
    | None,
    loginctl: str,
) -> bool | None:
    output = _run_linux_sensor_command(
        [
            loginctl,
            "--no-ask-password",
            "--no-pager",
            "show-session",
            "--property=OnExternalPower",
        ],
        runner=runner,
    )
    if output is None:
        return None
    properties = _parse_loginctl_properties(output)
    if properties is None or set(properties) != {"OnExternalPower"}:
        return None
    value = properties["OnExternalPower"].strip().lower()
    if value == "yes":
        return True
    if value == "no":
        return False
    return None


def get_ac_power(
    platform_name: str | None = None,
    *,
    runner: Callable[
        [Sequence[str], float], subprocess.CompletedProcess[str]
    ]
    | None = None,
    loginctl: str = "loginctl",
    power_supply_root: Path | None = None,
) -> bool | None:
    """Return AC/external-power state; ``None`` means unknown.

    Windows uses ``GetSystemPowerStatus``.  Linux first reads the kernel
    power-supply ABI and only then falls back to logind's
    ``OnExternalPower`` manager property, which also covers desktops and
    virtual machines that expose no battery or mains supply.
    """

    if _is_linux(platform_name):
        sysfs_result = _get_linux_ac_power_from_sysfs(
            _LINUX_POWER_SUPPLY_ROOT
            if power_supply_root is None
            else power_supply_root.expanduser()
        )
        if sysfs_result is not None:
            return sysfs_result
        return _get_linux_ac_power_from_logind(
            runner=runner, loginctl=loginctl
        )
    if not _is_windows(platform_name):
        return None
    try:
        import ctypes

        class SystemPowerStatus(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_ubyte),
                ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_uint32),
                ("BatteryFullLifeTime", ctypes.c_uint32),
            ]

        status = SystemPowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return None
        if status.ACLineStatus == 1:
            return True
        if status.ACLineStatus == 0:
            return False
        return None
    except (AttributeError, OSError, ValueError):
        return None


@dataclass(slots=True)
class AutoUpgradeCheck:
    name: str
    status: str = "pending"
    detail: str = ""

    def set(self, status: str, detail: str) -> None:
        self.status = status
        self.detail = _safe_text(detail)

    def to_dict(self) -> dict[str, str]:
        return {"detail": self.detail, "name": self.name, "status": self.status}


@dataclass(slots=True)
class AutoUpgradeRunReport:
    report_path: Path
    state_path: Path
    run_id: str
    started_at: str
    mode: str
    current_version: str
    client_platform: str
    status: str = "running"
    reason: str | None = None
    finished_at: str | None = None
    elapsed_ms: float = 0.0
    error: str | None = None
    feed_version: str | None = None
    staged_update_id: str | None = None
    applied_update_id: str | None = None
    next_check_at: str | None = None
    restart_required: bool = False
    reinstall_status: str = "not_reached"
    active_task_count: int | None = None
    idle_seconds: float | None = None
    on_ac_power: bool | None = None
    maintenance_window: str | None = None
    checks: list[AutoUpgradeCheck] = field(
        default_factory=lambda: [AutoUpgradeCheck(name) for name in _CHECK_NAMES]
    )

    @property
    def exit_code(self) -> int:
        return (
            1
            if self.status in {"failed", "misconfigured", "applied_reinstall_failed"}
            else 0
        )

    def check(self, name: str) -> AutoUpgradeCheck:
        for check in self.checks:
            if check.name == name:
                return check
        raise KeyError(name)

    def summary(self) -> str:
        return (
            f"status={self.status}; reason={self.reason}; "
            f"current={self.current_version}; feed={self.feed_version}; "
            f"report={self.report_path}"
        )

    def _base_dict(self) -> dict[str, Any]:
        return {
            "app": "Self-Healing PC Agent",
            "applied_update_id": self.applied_update_id,
            "checks": [check.to_dict() for check in self.checks],
            "client_platform": self.client_platform,
            "current_version": self.current_version,
            "elapsed_ms": round(float(self.elapsed_ms), 3),
            "error": self.error,
            "feed_version": self.feed_version,
            "finished_at": self.finished_at,
            "format": AUTO_UPGRADE_FORMAT,
            "gates": {
                "active_task_count": self.active_task_count,
                "idle_seconds": (
                    round(float(self.idle_seconds), 3)
                    if self.idle_seconds is not None
                    else None
                ),
                "maintenance_window": self.maintenance_window,
                "on_ac_power": self.on_ac_power,
            },
            "mode": self.mode,
            "next_check_at": self.next_check_at,
            "profile": WINDOWS_SCHEDULED_TASK_PROFILE,
            "reason": self.reason,
            "reinstall_status": self.reinstall_status,
            "restart_required": self.restart_required,
            "run_id": self.run_id,
            "staged_update_id": self.staged_update_id,
            "started_at": self.started_at,
            "status": self.status,
            "version": __version__,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._base_dict()
        payload["report_sha256"] = _sha256(_canonical_bytes(payload))
        return payload

    def write(self) -> None:
        data = _json_bytes(self.to_dict())
        if len(data) > _MAX_REPORT_BYTES:
            raise PcAgentError("Auto-upgrade report is too large.")
        if self.report_path.exists() or self.report_path.is_symlink():
            raise PcAgentError(
                f"Auto-upgrade report already exists: {self.report_path.name}."
            )
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.report_path.with_name(
            f".{self.report_path.name}.tmp-{secrets.token_hex(4)}"
        )
        try:
            temporary.write_bytes(data)
            if self.report_path.exists():
                raise PcAgentError(
                    f"Auto-upgrade report already exists: {self.report_path.name}."
                )
            temporary.replace(self.report_path)
        finally:
            if temporary.exists():
                temporary.unlink()


@dataclass(frozen=True, slots=True)
class AutoUpgradeReportVerification:
    report_path: Path
    version: str
    status: str
    reason: str | None
    run_id: str
    current_version: str
    feed_version: str | None
    report_sha256: str

    def summary(self) -> str:
        return (
            f"status={self.status}; reason={self.reason}; "
            f"version={self.version}; feed={self.feed_version}; "
            f"report_sha256={self.report_sha256[:16]}..."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_version": self.current_version,
            "feed_version": self.feed_version,
            "reason": self.reason,
            "report": str(self.report_path),
            "report_sha256": self.report_sha256,
            "run_id": self.run_id,
            "status": self.status,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class LinuxAutoUpgradeSystemdUnits:
    unit_name: str
    service_name: str
    timer_name: str
    service_text: str
    timer_text: str


@dataclass(frozen=True, slots=True)
class AutoUpgradeSchedulerResult:
    profile: str
    platform: str
    task_name: str
    installed: bool
    state: str | None
    executable: str | None
    detail: str

    def summary(self) -> str:
        return (
            f"profile={self.profile}; installed={self.installed}; "
            f"task={self.task_name}; state={self.state}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "detail": self.detail,
            "executable": self.executable,
            "installed": self.installed,
            "platform": self.platform,
            "profile": self.profile,
            "state": self.state,
            "task_name": self.task_name,
        }


@dataclass(frozen=True, slots=True)
class AutoUpgradeStatus:
    enabled: bool
    ready: bool
    blockers: tuple[str, ...]
    mode: str
    scheduler_profile: str
    scheduler_platform: str
    state_path: Path
    state: dict[str, Any]
    pending_update_id: str | None
    pending_version: str | None
    scheduler: AutoUpgradeSchedulerResult | None = None

    def summary(self) -> str:
        return (
            f"enabled={self.enabled}; ready={self.ready}; mode={self.mode}; "
            f"last={self.state.get('last_status')}; "
            f"next={self.state.get('next_check_at')}; "
            f"pending={self.pending_version}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "blockers": list(self.blockers),
            "enabled": self.enabled,
            "mode": self.mode,
            "pending_update_id": self.pending_update_id,
            "pending_version": self.pending_version,
            "ready": self.ready,
            "scheduler": self.scheduler.to_dict() if self.scheduler else None,
            "scheduler_platform": self.scheduler_platform,
            "scheduler_profile": self.scheduler_profile,
            "state": self.state,
            "state_path": str(self.state_path),
        }


def _resolve_report_path(
    settings: Settings,
    report_path: Path | None,
    *,
    run_id: str,
) -> Path:
    if report_path is None:
        candidate = (
            updates_root(settings)
            / AUTO_UPGRADE_REPORT_DIR
            / f"{run_id}.json"
        )
    else:
        candidate = report_path.expanduser()
    if candidate.suffix.lower() != ".json":
        raise PcAgentError("Auto-upgrade report output must end with .json.")
    if candidate.exists() or candidate.is_symlink():
        raise PcAgentError(f"Auto-upgrade report already exists: {candidate.name}.")
    return candidate.resolve()


def _pending_staged_update(settings: Settings) -> Any | None:
    updates_dir = updates_root(settings)
    if not updates_dir.exists():
        return None
    for update in list_updates(settings):
        if update.status != "staged":
            continue
        if compare_versions(update.bundle_version, __version__) > 0:
            return update
    return None


def _next_interval(now: datetime, settings: Settings) -> datetime:
    return now + timedelta(hours=int(settings.auto_upgrade_interval_hours))


def _next_backoff(
    now: datetime, settings: Settings, failures: int
) -> datetime:
    base = int(settings.auto_upgrade_interval_hours)
    multiplier = 2 ** min(max(failures - 1, 0), 5)
    return now + timedelta(hours=min(base * multiplier, 168))


def _apply_gates(
    settings: Settings,
    report: AutoUpgradeRunReport,
    *,
    local_now: datetime,
    idle_seconds: float | None,
    on_ac_power: bool | None,
    active_task_count: int,
    platform_name: str | None,
) -> list[str]:
    reasons: list[str] = []
    window = _parse_maintenance_window(settings.auto_upgrade_maintenance_window)
    if not _in_maintenance_window(local_now, window):
        reasons.append("outside_maintenance_window")
    if active_task_count > 0:
        reasons.append("active_tasks")
    # ``platform_name`` is retained in the report context, but permission
    # to apply never depends on a platform-specific exception: whenever a
    # sensor reading is required, unknown means defer on every platform.
    del platform_name
    required_idle = int(settings.auto_upgrade_idle_minutes) * 60
    if required_idle > 0:
        if idle_seconds is None:
            reasons.append("idle_unknown")
        elif idle_seconds < required_idle:
            reasons.append("user_not_idle")
    if settings.auto_upgrade_require_ac_power:
        if on_ac_power is False:
            reasons.append("on_battery")
        elif on_ac_power is None:
            reasons.append("ac_power_unknown")

    report.active_task_count = active_task_count
    report.idle_seconds = idle_seconds
    report.on_ac_power = on_ac_power
    maintenance_ok = "outside_maintenance_window" not in reasons
    report.check("maintenance_window").set(
        "passed" if maintenance_ok else "failed",
        "The local time is inside the configured maintenance window."
        if maintenance_ok
        else "The local time is outside the configured maintenance window.",
    )
    idle_ok = not any(
        reason in reasons
        for reason in (
            "active_tasks",
            "user_not_idle",
            "idle_unknown",
            "on_battery",
            "ac_power_unknown",
        )
    )
    report.check("idle_gate").set(
        "passed" if idle_ok else "failed",
        "No active task, user-idle and power gates permit application."
        if idle_ok
        else "An active-task, user-idle or power gate deferred application.",
    )
    return reasons


def _default_reinstall_runner(
    argv: Sequence[str], cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return subprocess.run(
        [str(part) for part in argv],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _run_reinstall(
    settings: Settings,
    *,
    runner: Callable[
        [Sequence[str], Path, float], subprocess.CompletedProcess[str]
    ]
    | None,
) -> tuple[bool, str]:
    root = settings.root_dir.expanduser().resolve()
    candidates = [
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
        Path(sys.executable),
    ]
    python = next((path for path in candidates if path.is_file()), candidates[-1])
    argv = [
        str(python),
        "-m",
        "pip",
        "install",
        "--no-deps",
        "--no-build-isolation",
        "-e",
        ".",
    ]
    try:
        completed = (runner or _default_reinstall_runner)(argv, root, 600.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, _sanitize_error(exc)
    if completed.returncode == 0:
        return True, "Editable package metadata refreshed without dependencies."
    output = _sanitize_error(completed.stderr or completed.stdout or "pip failed")
    return False, output


def _finish_run(
    settings: Settings,
    state: dict[str, Any],
    report: AutoUpgradeRunReport,
    *,
    started_monotonic: float,
    now: datetime,
    update_state: bool = True,
) -> AutoUpgradeRunReport:
    for check in report.checks:
        if check.status == "pending":
            check.set("skipped", "This check was not reached.")
    report.finished_at = _format_time(now)
    report.elapsed_ms = max(0.0, (time.monotonic() - started_monotonic) * 1_000)
    if update_state:
        state["last_run_at"] = _format_time(now)
        state["last_status"] = report.status
        state["last_reason"] = report.reason
        state["next_check_at"] = report.next_check_at
        state["latest_report"] = report.report_path.name
        state["restart_required"] = report.restart_required
        if report.status in {"failed", "applied_reinstall_failed"}:
            state["consecutive_failures"] = int(state["consecutive_failures"]) + 1
        elif report.status not in {"already_running", "disabled", "misconfigured"}:
            state["consecutive_failures"] = 0
        try:
            _write_state(settings, state, now=now)
            report.check("state_recorded").set(
                "passed", "The auto-upgrade state was recorded atomically."
            )
        except PcAgentError as exc:
            report.check("state_recorded").set(
                "failed", "The auto-upgrade state could not be recorded."
            )
            if report.status not in {"failed", "applied_reinstall_failed"}:
                report.status = "failed"
                report.reason = "state_write_failed"
            report.error = _sanitize_error(exc)
    else:
        report.check("state_recorded").set(
            "skipped", "Another worker owns the state; it was not changed."
        )
    report.write()
    return report


def run_auto_upgrade(
    settings: Settings,
    *,
    force: bool = False,
    report_path: Path | None = None,
    transport: UpdateTransport | None = None,
    now: datetime | None = None,
    local_now: datetime | None = None,
    idle_seconds: float | None = None,
    on_ac_power: bool | None = None,
    active_task_count: int | None = None,
    check_function: Callable[..., Any] | None = None,
    stage_function: Callable[..., Any] | None = None,
    apply_function: Callable[..., Any] | None = None,
    reinstall_runner: Callable[
        [Sequence[str], Path, float], subprocess.CompletedProcess[str]
    ]
    | None = None,
    platform_name: str | None = None,
) -> AutoUpgradeRunReport:
    """Run one bounded background auto-upgrade pass and retain its report."""

    settings.ensure_directories()
    _ensure_auto_root(settings)
    run_now = (now or _utc_now()).astimezone(UTC)
    run_local = local_now or datetime.now().astimezone()
    if run_local.tzinfo is None:
        run_local = run_local.astimezone()
    run_id = "auto-" + run_now.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
    destination = _resolve_report_path(settings, report_path, run_id=run_id)
    state = _read_state(settings)
    state_path = auto_upgrade_state_path(settings)
    started_monotonic = time.monotonic()
    mode = settings.auto_upgrade_mode
    report = AutoUpgradeRunReport(
        report_path=destination,
        state_path=state_path,
        run_id=run_id,
        started_at=_format_time(run_now),
        mode=mode,
        current_version=__version__,
        client_platform=_client_platform(platform_name),
        next_check_at=state.get("next_check_at"),
        restart_required=bool(state.get("restart_required")),
        staged_update_id=state.get("staged_update_id"),
        maintenance_window=settings.auto_upgrade_maintenance_window,
    )

    if not settings.auto_upgrade_enabled:
        report.status = "disabled"
        report.reason = "disabled"
        report.check("configuration").set(
            "failed", "Background auto-upgrade is disabled by configuration."
        )
        return _finish_run(
            settings,
            state,
            report,
            started_monotonic=started_monotonic,
            now=_utc_now(),
        )

    blockers = _config_blockers(settings)
    if blockers:
        report.status = "misconfigured"
        report.reason = "misconfigured"
        report.error = "; ".join(blockers)
        report.check("configuration").set(
            "failed", "Background auto-upgrade configuration is incomplete."
        )
        return _finish_run(
            settings,
            state,
            report,
            started_monotonic=started_monotonic,
            now=_utc_now(),
        )
    report.check("configuration").set(
        "passed",
        "Signed HTTPS online updates and background auto-upgrade are enabled.",
    )

    lock_path = _ensure_auto_root(settings) / AUTO_UPGRADE_LOCK_FILE
    with _AutoUpgradeLock(lock_path) as lock:
        if not lock.acquired:
            report.status = "already_running"
            report.reason = "already_running"
            report.check("single_instance_lock").set(
                "failed", "Another auto-upgrade worker is already running."
            )
            return _finish_run(
                settings,
                state,
                report,
                started_monotonic=started_monotonic,
                now=_utc_now(),
                update_state=False,
            )
        report.check("single_instance_lock").set(
            "passed", "This worker holds the single-instance lock."
        )

        # Another worker may have updated state before this lock was acquired.
        state = _read_state(settings)
        report.next_check_at = state.get("next_check_at")
        report.staged_update_id = state.get("staged_update_id")
        report.restart_required = bool(state.get("restart_required"))

        next_check: datetime | None = None
        if state.get("next_check_at"):
            next_check = _parse_time(state["next_check_at"], label="next check")
        due = force or next_check is None or run_now >= next_check
        report.check("schedule_due").set(
            "passed" if due else "failed",
            "This background check is due."
            if due
            else "The recorded next-check time has not arrived.",
        )

        pending = _pending_staged_update(settings)
        if pending is not None:
            report.staged_update_id = pending.update_id
            report.feed_version = pending.bundle_version
            state["staged_update_id"] = pending.update_id
            state["staged_version"] = pending.bundle_version
        else:
            state["staged_update_id"] = None
            state["staged_version"] = None
            report.staged_update_id = None

        window = _parse_maintenance_window(
            settings.auto_upgrade_maintenance_window
        )
        in_window = _in_maintenance_window(run_local, window)

        def evaluate_gates() -> list[str]:
            active = (
                count_active_tasks(settings)
                if active_task_count is None
                else int(active_task_count)
            )
            idle = get_idle_seconds(platform_name) if idle_seconds is None else idle_seconds
            power = get_ac_power(platform_name) if on_ac_power is None else on_ac_power
            return _apply_gates(
                settings,
                report,
                local_now=run_local,
                idle_seconds=idle,
                on_ac_power=power,
                active_task_count=active,
                platform_name=platform_name,
            )

        def apply_pending(update: Any) -> AutoUpgradeRunReport:
            if update.signature_status != "verified":
                report.status = "deferred"
                report.reason = "staged_signature_not_verified"
                report.error = (
                    "The pending staged update does not have a verified signature."
                )
                report.check("apply_update").set(
                    "failed", "Only a verified signed staged update may auto-apply."
                )
                report.next_check_at = _format_time(
                    _next_interval(run_now, settings)
                )
                return _finish_run(
                    settings,
                    state,
                    report,
                    started_monotonic=started_monotonic,
                    now=_utc_now(),
                )
            reasons = evaluate_gates()
            if reasons:
                report.status = "deferred"
                report.reason = reasons[0]
                report.check("apply_update").set(
                    "skipped", "Application was deferred by an idle/safety gate."
                )
                if "outside_maintenance_window" in reasons and window is not None:
                    report.next_check_at = _format_time(
                        _next_window_start_utc(run_local, window)
                    )
                else:
                    report.next_check_at = _format_time(
                        run_now + timedelta(hours=1)
                    )
                return _finish_run(
                    settings,
                    state,
                    report,
                    started_monotonic=started_monotonic,
                    now=_utc_now(),
                )
            try:
                applied = (apply_function or apply_staged_update)(
                    settings, update.update_id
                )
            except PcAgentError as exc:
                report.status = "failed"
                report.reason = "apply_failed"
                report.error = _sanitize_error(exc)
                report.check("apply_update").set(
                    "failed", "The staged update could not be applied."
                )
                failures = int(state["consecutive_failures"]) + 1
                report.next_check_at = _format_time(
                    _next_backoff(run_now, settings, failures)
                )
                return _finish_run(
                    settings,
                    state,
                    report,
                    started_monotonic=started_monotonic,
                    now=_utc_now(),
                )
            report.applied_update_id = applied.update_id
            report.feed_version = applied.bundle_version
            report.restart_required = True
            state["applied_update_id"] = applied.update_id
            state["applied_version"] = applied.bundle_version
            state["staged_update_id"] = None
            state["staged_version"] = None
            report.check("apply_update").set(
                "passed", "The verified staged update was applied with backups."
            )
            if settings.auto_upgrade_reinstall:
                reinstall_ok, detail = _run_reinstall(
                    settings, runner=reinstall_runner
                )
                report.reinstall_status = "succeeded" if reinstall_ok else "failed"
                report.check("reinstall_metadata").set(
                    "passed" if reinstall_ok else "failed", detail
                )
                if not reinstall_ok:
                    report.status = "applied_reinstall_failed"
                    report.reason = "reinstall_failed"
                    report.error = detail
                else:
                    report.status = "applied"
                    report.reason = "applied"
            else:
                report.reinstall_status = "not_requested"
                report.check("reinstall_metadata").set(
                    "skipped",
                    "Editable reinstall was not requested; restart remains required.",
                )
                report.status = "applied"
                report.reason = "applied"
            report.next_check_at = _format_time(_next_interval(run_now, settings))
            return _finish_run(
                settings,
                state,
                report,
                started_monotonic=started_monotonic,
                now=_utc_now(),
            )

        if pending is not None and mode == "apply-when-idle":
            return apply_pending(pending)

        if not due:
            if pending is not None:
                report.status = "staged"
                report.reason = "staged_waiting_manual_apply"
                report.check("download_and_stage").set(
                    "passed", "A verified update is already staged."
                )
                report.check("apply_update").set(
                    "skipped", "Stage-only mode never applies an update."
                )
            else:
                report.status = "not_due"
                report.reason = "not_due"
            return _finish_run(
                settings,
                state,
                report,
                started_monotonic=started_monotonic,
                now=_utc_now(),
            )

        if not in_window:
            report.status = "deferred"
            report.reason = "outside_maintenance_window"
            report.check("maintenance_window").set(
                "failed",
                "Background network work is outside the maintenance window.",
            )
            if window is not None:
                report.next_check_at = _format_time(
                    _next_window_start_utc(run_local, window)
                )
            return _finish_run(
                settings,
                state,
                report,
                started_monotonic=started_monotonic,
                now=_utc_now(),
            )
        report.check("maintenance_window").set(
            "passed", "Background work is inside the maintenance window."
        )

        state["last_check_at"] = _format_time(run_now)
        try:
            check = (check_function or check_online_update)(
                settings, transport=transport
            )
        except PcAgentError as exc:
            report.status = "failed"
            report.reason = "feed_check_failed"
            report.error = _sanitize_error(exc)
            report.check("feed_check").set(
                "failed", "The signed update feed could not be checked."
            )
            failures = int(state["consecutive_failures"]) + 1
            report.next_check_at = _format_time(
                _next_backoff(run_now, settings, failures)
            )
            return _finish_run(
                settings,
                state,
                report,
                started_monotonic=started_monotonic,
                now=_utc_now(),
            )
        report.feed_version = check.manifest.version
        report.check("feed_check").set(
            "passed", "The strict signed-update feed was fetched and parsed."
        )

        if pending is not None and compare_versions(
            check.manifest.version, pending.bundle_version
        ) <= 0:
            report.status = "staged"
            report.reason = "staged_waiting_manual_apply"
            report.check("download_and_stage").set(
                "passed", "The newest advertised update is already staged."
            )
            report.check("apply_update").set(
                "skipped", "Stage-only mode never applies an update."
            )
            report.next_check_at = _format_time(_next_interval(run_now, settings))
            return _finish_run(
                settings,
                state,
                report,
                started_monotonic=started_monotonic,
                now=_utc_now(),
            )

        if not check.update_available:
            report.status = "no_update"
            report.reason = "no_update"
            report.check("download_and_stage").set(
                "skipped", "The feed does not advertise a newer version."
            )
            report.next_check_at = _format_time(_next_interval(run_now, settings))
            return _finish_run(
                settings,
                state,
                report,
                started_monotonic=started_monotonic,
                now=_utc_now(),
            )

        try:
            result = (stage_function or stage_online_update)(
                settings,
                allow_unsigned=False,
                allow_downgrade=False,
                transport=transport,
            )
        except PcAgentError as exc:
            report.status = "failed"
            report.reason = "download_or_stage_failed"
            report.error = _sanitize_error(exc)
            report.check("download_and_stage").set(
                "failed", "The advertised update could not be verified/staged."
            )
            failures = int(state["consecutive_failures"]) + 1
            report.next_check_at = _format_time(
                _next_backoff(run_now, settings, failures)
            )
            return _finish_run(
                settings,
                state,
                report,
                started_monotonic=started_monotonic,
                now=_utc_now(),
            )

        staged = result.staged
        report.staged_update_id = staged.update_id
        report.feed_version = staged.bundle_version
        state["staged_update_id"] = staged.update_id
        state["staged_version"] = staged.bundle_version
        staged_ok = (
            staged.signature_status == "verified"
            and staged.bundle_version == check.manifest.version
            and result.downloaded.status == "staged"
        )
        report.check("download_and_stage").set(
            "passed" if staged_ok else "failed",
            "The bundle was downloaded, verified and staged."
            if staged_ok
            else "The staged result did not prove a verified signature.",
        )
        if not staged_ok:
            report.status = "failed"
            report.reason = "staged_signature_not_verified"
            report.error = "The staged update does not have a verified signature."
            failures = int(state["consecutive_failures"]) + 1
            report.next_check_at = _format_time(
                _next_backoff(run_now, settings, failures)
            )
            return _finish_run(
                settings,
                state,
                report,
                started_monotonic=started_monotonic,
                now=_utc_now(),
            )

        if mode == "stage-only":
            report.status = "staged"
            report.reason = "staged_waiting_manual_apply"
            report.check("apply_update").set(
                "skipped", "Stage-only mode never applies an update."
            )
            report.next_check_at = _format_time(_next_interval(run_now, settings))
            return _finish_run(
                settings,
                state,
                report,
                started_monotonic=started_monotonic,
                now=_utc_now(),
            )
        return apply_pending(staged)


def get_auto_upgrade_status(
    settings: Settings,
    *,
    include_scheduler: bool = False,
    task_name: str = DEFAULT_AUTO_UPGRADE_TASK_NAME,
    scheduler_runner: Callable[
        [Sequence[str], float], subprocess.CompletedProcess[str]
    ]
    | None = None,
    platform_name: str | None = None,
    scheduler_config_home: Path | None = None,
    scheduler_systemctl: str = "systemctl",
) -> AutoUpgradeStatus:
    state = _read_state(settings)
    blockers = tuple(_config_blockers(settings)) if settings.auto_upgrade_enabled else ()
    pending = _pending_staged_update(settings)
    client_platform = _client_platform(platform_name)
    scheduler_profile = {
        "windows": WINDOWS_SCHEDULED_TASK_PROFILE,
        "linux": LINUX_SYSTEMD_USER_PROFILE,
        "macos": MACOS_LAUNCHD_PROFILE,
    }.get(client_platform, "unsupported")
    scheduler = None
    if include_scheduler and client_platform in {"windows", "linux"}:
        scheduler = get_auto_upgrade_schedule(
            task_name=task_name,
            runner=scheduler_runner,
            platform_name=platform_name,
            config_home=scheduler_config_home,
            systemctl=scheduler_systemctl,
        )
    return AutoUpgradeStatus(
        enabled=settings.auto_upgrade_enabled,
        ready=settings.auto_upgrade_enabled and not blockers,
        blockers=blockers,
        mode=settings.auto_upgrade_mode,
        scheduler_profile=scheduler_profile,
        scheduler_platform=client_platform,
        state_path=auto_upgrade_state_path(settings),
        state=state,
        pending_update_id=pending.update_id if pending else None,
        pending_version=pending.bundle_version if pending else None,
        scheduler=scheduler,
    )


def verify_auto_upgrade_report(
    report_path: Path,
) -> AutoUpgradeReportVerification:
    """Strictly verify one retained auto-upgrade run report offline."""

    path = report_path.expanduser()
    if path.suffix.lower() != ".json":
        raise PcAgentError("Auto-upgrade report must end with .json.")
    if path.is_symlink() or not path.is_file():
        raise PcAgentError(f"Auto-upgrade report was not found: {path}.")
    if path.stat().st_size > _MAX_REPORT_BYTES:
        raise PcAgentError("Auto-upgrade report is too large.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PcAgentError("Auto-upgrade report is not valid JSON.") from exc
    expected_keys = {
        "app",
        "applied_update_id",
        "checks",
        "client_platform",
        "current_version",
        "elapsed_ms",
        "error",
        "feed_version",
        "finished_at",
        "format",
        "gates",
        "mode",
        "next_check_at",
        "profile",
        "reason",
        "reinstall_status",
        "report_sha256",
        "restart_required",
        "run_id",
        "staged_update_id",
        "started_at",
        "status",
        "version",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise PcAgentError("Auto-upgrade report has an unexpected shape.")
    recorded = payload["report_sha256"]
    if not isinstance(recorded, str) or _SHA256_RE.fullmatch(recorded) is None:
        raise PcAgentError("Auto-upgrade report hash is invalid.")
    unsigned = dict(payload)
    unsigned.pop("report_sha256")
    if _sha256(_canonical_bytes(unsigned)) != recorded:
        raise PcAgentError("Auto-upgrade report hash does not match.")
    if (
        payload["app"] != "Self-Healing PC Agent"
        or payload["format"] != AUTO_UPGRADE_FORMAT
        or payload["profile"] != WINDOWS_SCHEDULED_TASK_PROFILE
        or payload["status"] not in _REPORT_STATUSES
        or payload["mode"] not in _MODES
        or payload["client_platform"] not in {"windows", "linux", "macos", "unknown"}
    ):
        raise PcAgentError("Auto-upgrade report metadata is invalid.")
    if not isinstance(payload["run_id"], str) or _RUN_ID_RE.fullmatch(payload["run_id"]) is None:
        raise PcAgentError("Auto-upgrade report run ID is invalid.")
    parse_version(str(payload["version"]))
    parse_version(str(payload["current_version"]))
    if compare_versions(str(payload["version"]), __version__) > 0:
        raise PcAgentError("Auto-upgrade report is from a future application version.")
    if payload["feed_version"] is not None:
        parse_version(str(payload["feed_version"]))
    started = _parse_time(payload["started_at"], label="start time")
    finished = _parse_time(payload["finished_at"], label="finish time")
    if finished < started:
        raise PcAgentError("Auto-upgrade report times are inconsistent.")
    if payload["next_check_at"] is not None:
        _parse_time(payload["next_check_at"], label="next check time")
    elapsed = payload["elapsed_ms"]
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or elapsed < 0:
        raise PcAgentError("Auto-upgrade report elapsed time is invalid.")
    if not isinstance(payload["restart_required"], bool):
        raise PcAgentError("Auto-upgrade report restart flag is invalid.")
    if payload["reinstall_status"] not in {
        "failed",
        "not_reached",
        "not_requested",
        "succeeded",
    }:
        raise PcAgentError("Auto-upgrade report reinstall status is invalid.")
    for key in ("reason", "error", "staged_update_id", "applied_update_id"):
        if payload[key] is not None and (
            not isinstance(payload[key], str) or len(payload[key]) > 500
        ):
            raise PcAgentError("Auto-upgrade report text is invalid.")

    checks = payload["checks"]
    if not isinstance(checks, list) or len(checks) != len(_CHECK_NAMES):
        raise PcAgentError("Auto-upgrade report checks are invalid.")
    for expected, check in zip(_CHECK_NAMES, checks, strict=True):
        if (
            not isinstance(check, dict)
            or set(check) != {"detail", "name", "status"}
            or check["name"] != expected
            or check["status"] not in {"passed", "failed", "skipped"}
            or not isinstance(check["detail"], str)
            or len(check["detail"]) > 300
        ):
            raise PcAgentError("Auto-upgrade report checks are invalid.")

    gates = payload["gates"]
    if not isinstance(gates, dict) or set(gates) != {
        "active_task_count",
        "idle_seconds",
        "maintenance_window",
        "on_ac_power",
    }:
        raise PcAgentError("Auto-upgrade report gates are invalid.")
    if gates["active_task_count"] is not None and (
        isinstance(gates["active_task_count"], bool)
        or not isinstance(gates["active_task_count"], int)
        or gates["active_task_count"] < 0
    ):
        raise PcAgentError("Auto-upgrade report active-task gate is invalid.")
    if gates["idle_seconds"] is not None and (
        isinstance(gates["idle_seconds"], bool)
        or not isinstance(gates["idle_seconds"], (int, float))
        or gates["idle_seconds"] < 0
    ):
        raise PcAgentError("Auto-upgrade report idle gate is invalid.")
    if gates["on_ac_power"] not in {True, False, None}:
        raise PcAgentError("Auto-upgrade report power gate is invalid.")
    _parse_maintenance_window(gates["maintenance_window"])

    status = payload["status"]
    if status in {"applied", "applied_reinstall_failed"} and not payload["applied_update_id"]:
        raise PcAgentError("Auto-upgrade applied report has no update ID.")
    if status == "staged" and not payload["staged_update_id"]:
        raise PcAgentError("Auto-upgrade staged report has no update ID.")
    if status == "no_update" and not payload["feed_version"]:
        raise PcAgentError("Auto-upgrade no-update report has no feed version.")

    return AutoUpgradeReportVerification(
        report_path=path.resolve(),
        version=str(payload["version"]),
        status=str(status),
        reason=payload["reason"],
        run_id=str(payload["run_id"]),
        current_version=str(payload["current_version"]),
        feed_version=payload["feed_version"],
        report_sha256=recorded,
    )


def _validate_task_name(raw: str) -> str:
    value = raw.strip()
    if _TASK_NAME_RE.fullmatch(value) is None:
        raise PcAgentError(
            "Windows auto-upgrade task name must be 1-80 safe characters."
        )
    return value


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _resolve_pc_agent_executable(settings: Settings, raw: Path | None) -> Path:
    if raw is not None:
        candidate = raw.expanduser().resolve()
        if candidate.is_symlink() or not candidate.is_file():
            raise PcAgentError(f"pc-agent executable was not found: {candidate}.")
        return candidate
    root = settings.root_dir.expanduser().resolve()
    candidates = [
        root / ".venv" / "Scripts" / "pc-agent.exe",
        root / ".venv" / "bin" / "pc-agent",
    ]
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate.resolve()
    discovered = shutil.which("pc-agent")
    if discovered:
        return Path(discovered).resolve()
    raise PcAgentError(
        "Could not find pc-agent in the application .venv; pass --executable."
    )


def build_windows_auto_upgrade_task_script(
    settings: Settings,
    *,
    task_name: str = DEFAULT_AUTO_UPGRADE_TASK_NAME,
    executable: Path | None = None,
) -> str:
    clean_task = _validate_task_name(task_name)
    root = settings.root_dir.expanduser().resolve()
    exe = _resolve_pc_agent_executable(settings, executable)
    arguments = f'--root "{root}" update auto-run'
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$TaskName = {_ps_quote(clean_task)}",
            ("$UserId = if ($env:USERDOMAIN) { "
            "\"$env:USERDOMAIN\\$env:USERNAME\" } else { $env:USERNAME }"),
            ("$Action = New-ScheduledTaskAction "
            f"-Execute {_ps_quote(str(exe))} "
            f"-Argument {_ps_quote(arguments)} "
            f"-WorkingDirectory {_ps_quote(str(root))}"),
            "$TriggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $UserId",
            ("$TriggerRepeat = New-ScheduledTaskTrigger -Once "
            "-At (Get-Date).AddMinutes(5) "
            "-RepetitionInterval (New-TimeSpan -Hours 1)"),
            ("$Principal = New-ScheduledTaskPrincipal -UserId $UserId "
            "-LogonType Interactive -RunLevel Limited"),
            ("$TaskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable "
            "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
            "-ExecutionTimeLimit (New-TimeSpan -Hours 1) "
            "-MultipleInstances IgnoreNew"),
            ("Register-ScheduledTask -TaskName $TaskName -Action $Action "
            "-Trigger @($TriggerLogon, $TriggerRepeat) "
            "-Principal $Principal -Settings $TaskSettings -Force | Out-Null"),
            f"Write-Output {_ps_quote('installed:' + clean_task)}",
            "",
        ]
    )


def _powershell_argv(script: str) -> list[str]:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]


def _default_scheduler_runner(
    argv: Sequence[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(part) for part in argv],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _run_scheduler_script(
    script: str,
    *,
    runner: Callable[
        [Sequence[str], float], subprocess.CompletedProcess[str]
    ]
    | None,
    action: str,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = (runner or _default_scheduler_runner)(
            _powershell_argv(script), 120.0
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PcAgentError(f"Windows auto-upgrade scheduler {action} failed: {exc}") from exc
    if completed.returncode != 0:
        detail = _sanitize_error(completed.stderr or completed.stdout)
        raise PcAgentError(
            f"Windows auto-upgrade scheduler {action} failed: {detail}"
        )
    return completed


def install_windows_auto_upgrade_schedule(
    settings: Settings,
    *,
    task_name: str = DEFAULT_AUTO_UPGRADE_TASK_NAME,
    executable: Path | None = None,
    runner: Callable[
        [Sequence[str], float], subprocess.CompletedProcess[str]
    ]
    | None = None,
    platform_name: str | None = None,
) -> AutoUpgradeSchedulerResult:
    profile = get_auto_upgrade_scheduler_profile(WINDOWS_SCHEDULED_TASK_PROFILE)
    if not _is_windows(platform_name):
        _raise_windows_scheduler_unavailable(platform_name)
    clean_task = _validate_task_name(task_name)
    exe = _resolve_pc_agent_executable(settings, executable)
    script = build_windows_auto_upgrade_task_script(
        settings, task_name=clean_task, executable=exe
    )
    _run_scheduler_script(script, runner=runner, action="install")
    return AutoUpgradeSchedulerResult(
        profile=profile.profile_id,
        platform=profile.platform,
        task_name=clean_task,
        installed=True,
        state="registered",
        executable=str(exe),
        detail="Windows Scheduled Task registered for logon and hourly wakeups.",
    )


def uninstall_windows_auto_upgrade_schedule(
    *,
    task_name: str = DEFAULT_AUTO_UPGRADE_TASK_NAME,
    runner: Callable[
        [Sequence[str], float], subprocess.CompletedProcess[str]
    ]
    | None = None,
    platform_name: str | None = None,
) -> AutoUpgradeSchedulerResult:
    profile = get_auto_upgrade_scheduler_profile(WINDOWS_SCHEDULED_TASK_PROFILE)
    if not _is_windows(platform_name):
        _raise_windows_scheduler_unavailable(platform_name)
    clean_task = _validate_task_name(task_name)
    script = "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$TaskName = {_ps_quote(clean_task)}",
            ("$Task = Get-ScheduledTask -TaskName $TaskName "
            "-ErrorAction SilentlyContinue"),
            "if ($null -ne $Task) {",
            ("  Stop-ScheduledTask -TaskName $TaskName "
            "-ErrorAction SilentlyContinue"),
            "  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false",
            "}",
            "Write-Output 'removed'",
            "",
        ]
    )
    _run_scheduler_script(script, runner=runner, action="uninstall")
    return AutoUpgradeSchedulerResult(
        profile=profile.profile_id,
        platform=profile.platform,
        task_name=clean_task,
        installed=False,
        state=None,
        executable=None,
        detail="Windows Scheduled Task removed if it was registered.",
    )


def get_windows_auto_upgrade_schedule(
    *,
    task_name: str = DEFAULT_AUTO_UPGRADE_TASK_NAME,
    runner: Callable[
        [Sequence[str], float], subprocess.CompletedProcess[str]
    ]
    | None = None,
    platform_name: str | None = None,
) -> AutoUpgradeSchedulerResult:
    profile = get_auto_upgrade_scheduler_profile(WINDOWS_SCHEDULED_TASK_PROFILE)
    if not _is_windows(platform_name):
        _raise_windows_scheduler_unavailable(platform_name)
    clean_task = _validate_task_name(task_name)
    script = "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$TaskName = {_ps_quote(clean_task)}",
            ("$Task = Get-ScheduledTask -TaskName $TaskName "
            "-ErrorAction SilentlyContinue"),
            "if ($null -eq $Task) {",
            ("  @{ installed = $false; state = $null } | "
            "ConvertTo-Json -Compress"),
            "  exit 0",
            "}",
            ("@{ installed = $true; state = [string]$Task.State } | "
            "ConvertTo-Json -Compress"),
            "",
        ]
    )
    completed = _run_scheduler_script(script, runner=runner, action="status")
    payload: dict[str, Any] | None = None
    for line in reversed(completed.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    if payload is None or "installed" not in payload:
        raise PcAgentError("Windows auto-upgrade scheduler status was unreadable.")
    return AutoUpgradeSchedulerResult(
        profile=profile.profile_id,
        platform=profile.platform,
        task_name=clean_task,
        installed=bool(payload["installed"]),
        state=str(payload["state"]) if payload.get("state") is not None else None,
        executable=None,
        detail="Windows Scheduled Task status read successfully.",
    )


def _validate_systemd_unit_name(raw: str) -> str:
    value = raw.strip()
    if value == DEFAULT_AUTO_UPGRADE_TASK_NAME:
        return DEFAULT_LINUX_AUTO_UPGRADE_UNIT_NAME
    if value.endswith((".service", ".timer")):
        raise PcAgentError(
            "Linux auto-upgrade unit name must be the base name without "
            ".service or .timer."
        )
    if _SYSTEMD_UNIT_NAME_RE.fullmatch(value) is None:
        raise PcAgentError(
            "Linux auto-upgrade unit name must be 1-80 safe characters."
        )
    return value


def _require_linux_scheduler(platform_name: str | None) -> None:
    platform = _client_platform(platform_name)
    if platform == "linux":
        return
    if platform == "macos":
        _require_windows_scheduler(
            get_auto_upgrade_scheduler_profile(MACOS_LAUNCHD_PROFILE)
        )
    raise PcAgentError(
        "The Linux systemd auto-upgrade scheduler can run only on Linux."
    )


def _systemd_escape(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise PcAgentError(
            "Linux auto-upgrade scheduler paths must not contain control "
            "characters."
        )
    return value.replace("%", "%%")


def build_linux_auto_upgrade_systemd_units(
    settings: Settings,
    *,
    unit_name: str = DEFAULT_LINUX_AUTO_UPGRADE_UNIT_NAME,
    executable: Path | None = None,
) -> LinuxAutoUpgradeSystemdUnits:
    """Build the exact per-user systemd service and timer unit texts."""

    clean_unit = _validate_systemd_unit_name(unit_name)
    root = settings.root_dir.expanduser().resolve()
    exe = _resolve_pc_agent_executable(settings, executable)
    service_name = f"{clean_unit}.service"
    timer_name = f"{clean_unit}.timer"
    exec_start = _systemd_escape(
        shlex.join(
            [str(exe), "--root", str(root), "update", "auto-run"]
        )
    )
    service_text = "\n".join(
        [
            "# Managed by Self-Healing PC Agent. Do not add secrets here.",
            "[Unit]",
            "Description=Self-Healing PC Agent signed background auto-upgrade check",
            _LINUX_SYSTEMD_MARKER,
            "",
            "[Service]",
            "Type=oneshot",
            f"WorkingDirectory={_systemd_escape(str(root))}",
            f"ExecStart={exec_start}",
            "TimeoutStartSec=1h",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "UMask=0077",
            "",
        ]
    )
    timer_text = "\n".join(
        [
            "# Managed by Self-Healing PC Agent. Do not add secrets here.",
            "[Unit]",
            "Description=Hourly Self-Healing PC Agent signed auto-upgrade check",
            _LINUX_SYSTEMD_MARKER,
            "",
            "[Timer]",
            "OnBootSec=5min",
            "OnUnitInactiveSec=1h",
            "RandomizedDelaySec=5min",
            "AccuracySec=1min",
            "Persistent=true",
            f"Unit={service_name}",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )
    return LinuxAutoUpgradeSystemdUnits(
        unit_name=clean_unit,
        service_name=service_name,
        timer_name=timer_name,
        service_text=service_text,
        timer_text=timer_text,
    )


def _systemd_user_unit_dir(
    *,
    config_home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    if config_home is not None:
        base = config_home.expanduser()
    else:
        env = os.environ if environ is None else environ
        xdg_value = str(env.get("XDG_CONFIG_HOME", "")).strip()
        if xdg_value and Path(xdg_value).expanduser().is_absolute():
            base = Path(xdg_value).expanduser()
        else:
            home_value = str(env.get("HOME", "")).strip()
            home = (
                Path(home_value).expanduser()
                if home_value and Path(home_value).expanduser().is_absolute()
                else Path.home()
            )
            base = home / ".config"
    return base / "systemd" / "user"


def _linux_systemd_paths(
    unit_name: str,
    *,
    config_home: Path | None,
    environ: Mapping[str, str] | None,
) -> tuple[Path, Path, Path]:
    unit_dir = _systemd_user_unit_dir(
        config_home=config_home, environ=environ
    )
    return (
        unit_dir,
        unit_dir / f"{unit_name}.service",
        unit_dir / f"{unit_name}.timer",
    )


def _read_managed_systemd_unit(path: Path) -> str | None:
    if path.is_symlink():
        raise PcAgentError(
            f"Linux auto-upgrade unit must not be a symbolic link: {path}."
        )
    if not path.exists():
        return None
    if not path.is_file():
        raise PcAgentError(f"Linux auto-upgrade unit is not a file: {path}.")
    if path.stat().st_size > _MAX_SYSTEMD_UNIT_BYTES:
        raise PcAgentError(f"Linux auto-upgrade unit is too large: {path}.")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PcAgentError(
            f"Linux auto-upgrade unit is unreadable: {path}."
        ) from exc
    if _LINUX_SYSTEMD_MARKER not in text:
        raise PcAgentError(
            "Refusing to overwrite or remove an unmanaged systemd unit: "
            f"{path}."
        )
    return text


def _systemd_unit_kind(path: Path) -> str:
    if path.is_symlink():
        return "unmanaged"
    if not path.exists():
        return "missing"
    if not path.is_file() or path.stat().st_size > _MAX_SYSTEMD_UNIT_BYTES:
        return "unmanaged"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "unmanaged"
    return "managed" if _LINUX_SYSTEMD_MARKER in text else "unmanaged"


def _atomic_write_systemd_unit(path: Path, text: str) -> None:
    if path.is_symlink():
        raise PcAgentError(
            f"Linux auto-upgrade unit must not be a symbolic link: {path}."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(4)}")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _restore_systemd_unit(path: Path, prior_text: str | None) -> None:
    if prior_text is None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        return
    _atomic_write_systemd_unit(path, prior_text)


def _run_systemd_user_command(
    argv: Sequence[str],
    *,
    runner: Callable[
        [Sequence[str], float], subprocess.CompletedProcess[str]
    ]
    | None,
    action: str,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = (runner or _default_scheduler_runner)(argv, 120.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PcAgentError(
            f"Linux systemd auto-upgrade scheduler {action} failed: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = _sanitize_error(completed.stderr or completed.stdout)
        raise PcAgentError(
            f"Linux systemd auto-upgrade scheduler {action} failed: {detail}"
        )
    return completed


def _try_systemd_user_command(
    argv: Sequence[str],
    *,
    runner: Callable[
        [Sequence[str], float], subprocess.CompletedProcess[str]
    ]
    | None,
) -> bool:
    try:
        completed = (runner or _default_scheduler_runner)(argv, 120.0)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def install_linux_auto_upgrade_schedule(
    settings: Settings,
    *,
    unit_name: str = DEFAULT_LINUX_AUTO_UPGRADE_UNIT_NAME,
    executable: Path | None = None,
    runner: Callable[
        [Sequence[str], float], subprocess.CompletedProcess[str]
    ]
    | None = None,
    platform_name: str | None = None,
    config_home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    systemctl: str = "systemctl",
) -> AutoUpgradeSchedulerResult:
    """Install/enable the per-user systemd timer, rolling back on failure."""

    profile = get_auto_upgrade_scheduler_profile(LINUX_SYSTEMD_USER_PROFILE)
    _require_linux_scheduler(platform_name)
    clean_unit = _validate_systemd_unit_name(unit_name)
    exe = _resolve_pc_agent_executable(settings, executable)
    units = build_linux_auto_upgrade_systemd_units(
        settings, unit_name=clean_unit, executable=exe
    )
    _unit_dir, service_path, timer_path = _linux_systemd_paths(
        clean_unit, config_home=config_home, environ=environ
    )
    prior_service = _read_managed_systemd_unit(service_path)
    prior_timer = _read_managed_systemd_unit(timer_path)

    _run_systemd_user_command(
        [systemctl, "--user", "show-environment"],
        runner=runner,
        action="preflight",
    )

    written: list[tuple[Path, str | None]] = []
    try:
        _atomic_write_systemd_unit(service_path, units.service_text)
        written.append((service_path, prior_service))
        _atomic_write_systemd_unit(timer_path, units.timer_text)
        written.append((timer_path, prior_timer))
        _run_systemd_user_command(
            [systemctl, "--user", "daemon-reload"],
            runner=runner,
            action="daemon-reload",
        )
        _run_systemd_user_command(
            [systemctl, "--user", "enable", "--now", units.timer_name],
            runner=runner,
            action="enable",
        )
    except (OSError, PcAgentError) as exc:
        rollback_error: Exception | None = None
        for path, prior_text in reversed(written):
            try:
                _restore_systemd_unit(path, prior_text)
            except (OSError, PcAgentError) as rollback_exc:
                rollback_error = rollback_exc
        _try_systemd_user_command(
            [systemctl, "--user", "daemon-reload"], runner=runner
        )
        if rollback_error is not None:
            raise PcAgentError(
                "Linux systemd auto-upgrade scheduler install failed and "
                f"unit rollback also failed: {rollback_error}"
            ) from exc
        raise

    return AutoUpgradeSchedulerResult(
        profile=profile.profile_id,
        platform=profile.platform,
        task_name=clean_unit,
        installed=True,
        state="enabled",
        executable=str(exe),
        detail=(
            "Linux systemd user timer installed and enabled for boot/login "
            "plus hourly one-shot wakeups."
        ),
    )


def uninstall_linux_auto_upgrade_schedule(
    *,
    unit_name: str = DEFAULT_LINUX_AUTO_UPGRADE_UNIT_NAME,
    runner: Callable[
        [Sequence[str], float], subprocess.CompletedProcess[str]
    ]
    | None = None,
    platform_name: str | None = None,
    config_home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    systemctl: str = "systemctl",
) -> AutoUpgradeSchedulerResult:
    """Disable/remove only the managed systemd user service and timer."""

    profile = get_auto_upgrade_scheduler_profile(LINUX_SYSTEMD_USER_PROFILE)
    _require_linux_scheduler(platform_name)
    clean_unit = _validate_systemd_unit_name(unit_name)
    service_name = f"{clean_unit}.service"
    timer_name = f"{clean_unit}.timer"
    unit_dir, service_path, timer_path = _linux_systemd_paths(
        clean_unit, config_home=config_home, environ=environ
    )
    _read_managed_systemd_unit(service_path)
    _read_managed_systemd_unit(timer_path)

    disable_succeeded = False
    if _try_systemd_user_command(
        [systemctl, "--user", "daemon-reload"], runner=runner
    ):
        disable_succeeded = _try_systemd_user_command(
            [systemctl, "--user", "disable", "--now", timer_name],
            runner=runner,
        )

    for path in (timer_path, service_path):
        if path.is_file() and not path.is_symlink():
            path.unlink()
    wants_link = unit_dir / "timers.target.wants" / timer_name
    if wants_link.is_symlink():
        target_name = Path(os.readlink(wants_link)).name
        if target_name == timer_name:
            wants_link.unlink()
    _try_systemd_user_command(
        [systemctl, "--user", "daemon-reload"], runner=runner
    )
    _try_systemd_user_command(
        [systemctl, "--user", "reset-failed", service_name, timer_name],
        runner=runner,
    )
    detail = (
        "Linux systemd user timer disabled and managed unit files removed."
        if disable_succeeded
        else (
            "Managed Linux systemd unit files removed; the user manager "
            "could not confirm disable, so enablement state should be "
            "checked when it is available."
        )
    )
    return AutoUpgradeSchedulerResult(
        profile=profile.profile_id,
        platform=profile.platform,
        task_name=clean_unit,
        installed=False,
        state=None,
        executable=None,
        detail=detail,
    )


def _safe_systemd_value(value: str | None) -> str | None:
    if value is None:
        return None
    clean = value.strip()
    if not clean or len(clean) > 80:
        return None
    if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-/" for character in clean):
        return None
    return clean


def get_linux_auto_upgrade_schedule(
    *,
    unit_name: str = DEFAULT_LINUX_AUTO_UPGRADE_UNIT_NAME,
    runner: Callable[
        [Sequence[str], float], subprocess.CompletedProcess[str]
    ]
    | None = None,
    platform_name: str | None = None,
    config_home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    systemctl: str = "systemctl",
) -> AutoUpgradeSchedulerResult:
    """Read managed systemd timer state without making status fail hard."""

    profile = get_auto_upgrade_scheduler_profile(LINUX_SYSTEMD_USER_PROFILE)
    _require_linux_scheduler(platform_name)
    clean_unit = _validate_systemd_unit_name(unit_name)
    timer_name = f"{clean_unit}.timer"
    unit_dir, service_path, timer_path = _linux_systemd_paths(
        clean_unit, config_home=config_home, environ=environ
    )
    service_kind = _systemd_unit_kind(service_path)
    timer_kind = _systemd_unit_kind(timer_path)
    both_managed = service_kind == timer_kind == "managed"
    unmanaged = "unmanaged" in {service_kind, timer_kind}
    wants_link = unit_dir / "timers.target.wants" / timer_name
    wants_exists = wants_link.is_symlink()

    properties: dict[str, str] = {}
    manager_error = ""
    try:
        completed = _run_systemd_user_command(
            [
                systemctl,
                "--user",
                "show",
                timer_name,
                "--property=LoadState,ActiveState,SubState,UnitFileState",
            ],
            runner=runner,
            action="status",
        )
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                properties[key.strip()] = value.strip()
    except PcAgentError as exc:
        manager_error = _sanitize_error(exc)

    if "LoadState" in properties:
        load_state = _safe_systemd_value(properties.get("LoadState"))
        active_state = _safe_systemd_value(properties.get("ActiveState"))
        sub_state = _safe_systemd_value(properties.get("SubState"))
        unit_file_state = _safe_systemd_value(
            properties.get("UnitFileState")
        )
        installed = bool(
            both_managed
            and (
                unit_file_state in {"enabled", "enabled-runtime"}
                or active_state == "active"
            )
        )
        state = "/".join(
            part for part in (active_state, sub_state) if part is not None
        ) or unit_file_state or load_state
        detail = (
            "Linux systemd user timer status: "
            f"load={load_state}; active={active_state}; "
            f"sub={sub_state}; unit_file={unit_file_state}; "
            f"files={service_kind}/{timer_kind}."
        )
    else:
        installed = bool(both_managed and wants_exists)
        state = "unavailable"
        detail = (
            "Linux systemd user manager status is unavailable"
            + (f": {manager_error}" if manager_error else "")
            + f"; files={service_kind}/{timer_kind}; "
            f"enable_link={wants_exists}."
        )
    if unmanaged:
        installed = False
        detail += (
            " Existing unit files are not managed by this application and "
            "will not be overwritten or removed."
        )
    return AutoUpgradeSchedulerResult(
        profile=profile.profile_id,
        platform=profile.platform,
        task_name=clean_unit,
        installed=installed,
        state=state,
        executable=None,
        detail=detail,
    )


def install_auto_upgrade_schedule(
    settings: Settings,
    *,
    task_name: str = DEFAULT_AUTO_UPGRADE_TASK_NAME,
    executable: Path | None = None,
    runner: Callable[
        [Sequence[str], float], subprocess.CompletedProcess[str]
    ]
    | None = None,
    platform_name: str | None = None,
    config_home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    systemctl: str = "systemctl",
) -> AutoUpgradeSchedulerResult:
    """Install the implemented scheduler for the current platform."""

    platform = _client_platform(platform_name)
    if platform == "windows":
        return install_windows_auto_upgrade_schedule(
            settings,
            task_name=task_name,
            executable=executable,
            runner=runner,
            platform_name=platform_name,
        )
    if platform == "linux":
        return install_linux_auto_upgrade_schedule(
            settings,
            unit_name=task_name,
            executable=executable,
            runner=runner,
            platform_name=platform_name,
            config_home=config_home,
            environ=environ,
            systemctl=systemctl,
        )
    if platform == "macos":
        _require_windows_scheduler(
            get_auto_upgrade_scheduler_profile(MACOS_LAUNCHD_PROFILE)
        )
    raise PcAgentError(
        "No auto-upgrade scheduler is implemented for this platform."
    )


def uninstall_auto_upgrade_schedule(
    *,
    task_name: str = DEFAULT_AUTO_UPGRADE_TASK_NAME,
    runner: Callable[
        [Sequence[str], float], subprocess.CompletedProcess[str]
    ]
    | None = None,
    platform_name: str | None = None,
    config_home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    systemctl: str = "systemctl",
) -> AutoUpgradeSchedulerResult:
    """Remove the implemented scheduler for the current platform."""

    platform = _client_platform(platform_name)
    if platform == "windows":
        return uninstall_windows_auto_upgrade_schedule(
            task_name=task_name,
            runner=runner,
            platform_name=platform_name,
        )
    if platform == "linux":
        return uninstall_linux_auto_upgrade_schedule(
            unit_name=task_name,
            runner=runner,
            platform_name=platform_name,
            config_home=config_home,
            environ=environ,
            systemctl=systemctl,
        )
    if platform == "macos":
        _require_windows_scheduler(
            get_auto_upgrade_scheduler_profile(MACOS_LAUNCHD_PROFILE)
        )
    raise PcAgentError(
        "No auto-upgrade scheduler is implemented for this platform."
    )


def get_auto_upgrade_schedule(
    *,
    task_name: str = DEFAULT_AUTO_UPGRADE_TASK_NAME,
    runner: Callable[
        [Sequence[str], float], subprocess.CompletedProcess[str]
    ]
    | None = None,
    platform_name: str | None = None,
    config_home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    systemctl: str = "systemctl",
) -> AutoUpgradeSchedulerResult:
    """Read the implemented scheduler state for the current platform."""

    platform = _client_platform(platform_name)
    if platform == "windows":
        return get_windows_auto_upgrade_schedule(
            task_name=task_name,
            runner=runner,
            platform_name=platform_name,
        )
    if platform == "linux":
        return get_linux_auto_upgrade_schedule(
            unit_name=task_name,
            runner=runner,
            platform_name=platform_name,
            config_home=config_home,
            environ=environ,
            systemctl=systemctl,
        )
    if platform == "macos":
        _require_windows_scheduler(
            get_auto_upgrade_scheduler_profile(MACOS_LAUNCHD_PROFILE)
        )
    raise PcAgentError(
        "No auto-upgrade scheduler is implemented for this platform."
    )


__all__ = [
    "AUTO_UPGRADE_FORMAT",
    "AUTO_UPGRADE_LOCK_FILE",
    "AUTO_UPGRADE_REPORT_DIR",
    "AUTO_UPGRADE_SCHEDULER_PROFILES",
    "AUTO_UPGRADE_STATE_FILE",
    "DEFAULT_AUTO_UPGRADE_TASK_NAME",
    "DEFAULT_LINUX_AUTO_UPGRADE_UNIT_NAME",
    "LINUX_SYSTEMD_USER_PROFILE",
    "MACOS_LAUNCHD_PROFILE",
    "WINDOWS_SCHEDULED_TASK_PROFILE",
    "AutoUpgradeReportVerification",
    "AutoUpgradeRunReport",
    "AutoUpgradeSchedulerProfile",
    "AutoUpgradeSchedulerResult",
    "AutoUpgradeStatus",
    "LinuxAutoUpgradeSystemdUnits",
    "auto_upgrade_state_path",
    "build_linux_auto_upgrade_systemd_units",
    "build_windows_auto_upgrade_task_script",
    "count_active_tasks",
    "get_ac_power",
    "get_auto_upgrade_schedule",
    "get_auto_upgrade_scheduler_profile",
    "get_auto_upgrade_status",
    "get_idle_seconds",
    "get_linux_auto_upgrade_schedule",
    "get_windows_auto_upgrade_schedule",
    "install_auto_upgrade_schedule",
    "install_linux_auto_upgrade_schedule",
    "install_windows_auto_upgrade_schedule",
    "list_auto_upgrade_scheduler_profiles",
    "run_auto_upgrade",
    "uninstall_auto_upgrade_schedule",
    "uninstall_linux_auto_upgrade_schedule",
    "uninstall_windows_auto_upgrade_schedule",
    "verify_auto_upgrade_report",
]
