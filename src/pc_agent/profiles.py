"""File-based settings profiles (Transformation Phase 3).

A profile is a small JSON file under ``<data-dir>/profiles/<name>.json``
holding a deliberate *whitelist* of non-secret, non-gate settings:
logging, provider order, memory approval thresholds, task budgets,
safety bounds, and web limits. Secrets, capability gates
(``allow_subprocess`` and friends), and paths can never travel in a
profile, so applying one can never grant a new capability.

Precedence (highest first): explicit environment/``.env`` values, then
the active profile, then built-in defaults. The active profile is
chosen by ``PC_AGENT_PROFILE`` or the ``<data-dir>/active-profile``
marker file. A broken or invalid profile fails closed with
:class:`~pc_agent.errors.ConfigError` instead of silently falling back.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError

__all__ = [
    "PROFILE_FIELDS",
    "ProfileInfo",
    "active_profile_name",
    "delete_profile",
    "list_profiles",
    "load_profile",
    "profiles_dir",
    "save_profile",
    "set_active_profile",
    "snapshot_from_settings",
    "validate_profile_name",
]

# field name -> the environment variable that overrides it. This mapping is
# the whitelist: fields absent here can never be read from a profile file.
PROFILE_FIELDS: Mapping[str, str] = {
    "log_level": "PC_AGENT_LOG_LEVEL",
    "log_format": "PC_AGENT_LOG_FORMAT",
    "provider_order": "PC_AGENT_PROVIDER_ORDER",
    "max_recovery_attempts": "PC_AGENT_MAX_RECOVERY_ATTEMPTS",
    "task_token_budget": "PC_AGENT_TASK_TOKEN_BUDGET",
    "task_provider_call_budget": "PC_AGENT_TASK_PROVIDER_CALL_BUDGET",
    "task_tool_budget": "PC_AGENT_TASK_TOOL_BUDGET",
    "task_step_budget": "PC_AGENT_TASK_STEP_BUDGET",
    "task_time_budget_seconds": "PC_AGENT_TASK_TIME_BUDGET_SECONDS",
    "safety_max_tool_seconds": "PC_AGENT_SAFETY_MAX_TOOL_SECONDS",
    "safety_approval_expiry_minutes": "PC_AGENT_SAFETY_APPROVAL_EXPIRY_MINUTES",
    "command_allowlist": "PC_AGENT_COMMAND_ALLOWLIST",
    "web_timeout_seconds": "PC_AGENT_WEB_TIMEOUT_SECONDS",
    "web_max_bytes": "PC_AGENT_WEB_MAX_BYTES",
    "mission_max_parallel": "PC_AGENT_MISSION_MAX_PARALLEL",
    "scheduler_interval_seconds": "PC_AGENT_SCHEDULER_INTERVAL_SECONDS",
}

_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
_MAX_PROFILE_BYTES = 64 * 1024
_MARKER_FILENAME = "active-profile"


@dataclass(frozen=True, slots=True)
class ProfileInfo:
    name: str
    path: Path
    field_names: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "field_names": list(self.field_names),
        }


def validate_profile_name(name: str) -> str:
    clean = name.strip()
    if not _PROFILE_NAME_RE.match(clean) or clean in {".", ".."} or clean.endswith(".json"):
        raise ConfigError(
            "Profile names must be 1-40 characters of letters, digits, '.', '_', or '-'."
        )
    return clean


def profiles_dir(data_dir: Path) -> Path:
    return Path(data_dir).expanduser() / "profiles"


def profile_path(data_dir: Path, name: str) -> Path:
    return profiles_dir(data_dir) / f"{validate_profile_name(name)}.json"


def list_profiles(data_dir: Path) -> list[ProfileInfo]:
    directory = profiles_dir(data_dir)
    if not directory.is_dir():
        return []
    profiles: list[ProfileInfo] = []
    for path in sorted(directory.glob("*.json")):
        try:
            validate_profile_name(path.stem)
            data = _read_profile_file(path)
        except ConfigError:
            profiles.append(ProfileInfo(name=path.stem, path=path, field_names=()))
            continue
        profiles.append(
            ProfileInfo(name=path.stem, path=path, field_names=tuple(sorted(data)))
        )
    return profiles


def load_profile(data_dir: Path, name: str) -> dict[str, Any]:
    path = profile_path(data_dir, name)
    if not path.is_file():
        raise ConfigError(f"Settings profile '{name}' does not exist at {path}.")
    return _validated(_read_profile_file(path), name=name)


def save_profile(data_dir: Path, name: str, data: Mapping[str, Any]) -> ProfileInfo:
    clean = _validated(dict(data), name=name)
    directory = profiles_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = profile_path(data_dir, name)
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(clean, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(path)
    return ProfileInfo(name=name, path=path, field_names=tuple(sorted(clean)))


def delete_profile(data_dir: Path, name: str) -> bool:
    path = profile_path(data_dir, name)
    if not path.is_file():
        return False
    if active_profile_name(data_dir)[0] == name:
        set_active_profile(data_dir, None)
    path.unlink()
    return True


def marker_path(data_dir: Path) -> Path:
    return Path(data_dir).expanduser() / _MARKER_FILENAME


def active_profile_name(
    data_dir: Path, *, environment_value: str | None = None
) -> tuple[str | None, str]:
    """Return ``(name, source)`` where source is env/marker/none."""
    if environment_value and environment_value.strip():
        return validate_profile_name(environment_value), "environment"
    marker = marker_path(data_dir)
    if marker.is_file():
        name = marker.read_text(encoding="utf-8", errors="strict").strip()
        if name:
            return validate_profile_name(name), "marker"
    return None, "none"


def set_active_profile(data_dir: Path, name: str | None) -> None:
    target_dir = Path(data_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_path(data_dir)
    if name is None:
        marker.unlink(missing_ok=True)
        return
    clean = validate_profile_name(name)
    if not profile_path(data_dir, clean).is_file():
        raise ConfigError(f"Settings profile '{clean}' does not exist yet; save it first.")
    marker.write_text(clean + "\n", encoding="utf-8")


def snapshot_from_settings(settings: Any) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for field_name in PROFILE_FIELDS:
        value = getattr(settings, field_name)
        if isinstance(value, tuple):
            snapshot[field_name] = list(value)
        else:
            snapshot[field_name] = value
    return snapshot


def _read_profile_file(path: Path) -> dict[str, Any]:
    if path.stat().st_size > _MAX_PROFILE_BYTES:
        raise ConfigError(f"Settings profile {path.name} exceeds {_MAX_PROFILE_BYTES} bytes.")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8-sig", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Settings profile {path.name} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"Settings profile {path.name} must be a JSON object.")
    return parsed


def _validated(data: dict[str, Any], *, name: str) -> dict[str, Any]:
    unknown = sorted(key for key in data if key not in PROFILE_FIELDS)
    if unknown:
        raise ConfigError(
            f"Settings profile '{name}' contains unsupported field(s): "
            + ", ".join(unknown)
            + ". Secrets, capability gates, and paths cannot travel in a profile."
        )
    for key, value in data.items():
        if not isinstance(value, (str, int, float, list)) or isinstance(value, bool):
            raise ConfigError(f"Profile field '{key}' must be text, a number, or a list.")
        if isinstance(value, list) and not all(isinstance(item, str) for item in value):
            raise ConfigError(f"Profile field '{key}' must be a list of text values.")
    return dict(data)
