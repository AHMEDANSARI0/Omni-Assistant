"""Persisted interface preferences (Transformation Phase 17).

Environment variables always win. When they are not set, the interface reads a
small ``ui.json`` file inside the data directory, which is what
``pc-agent i18n set`` writes - so a language, text size, high-contrast or
reduced-motion choice survives restarts without inventing a second settings
store or a database migration.

The file is deliberately tiny, validated on read, and never contains secrets: it
holds six interface values and nothing else. A corrupt or unknown file is
ignored (defaults apply) instead of breaking startup.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["PREF_FILE", "PREF_KEYS", "load_prefs", "prefs_path", "save_prefs"]

PREF_FILE = "ui.json"

PREF_KEYS: tuple[str, ...] = (
    "locale",
    "scale",
    "high_contrast",
    "reduce_motion",
    "keyboard_shortcuts",
    "cli_color",
)


def prefs_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / PREF_FILE


def load_prefs(data_dir: str | Path) -> dict[str, Any]:
    """Read the preference file; any problem means "use the defaults"."""

    path = prefs_path(data_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {key: payload[key] for key in PREF_KEYS if key in payload}


def save_prefs(data_dir: str | Path, values: dict[str, Any]) -> Path:
    """Merge ``values`` into the preference file and return its path."""

    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    merged = load_prefs(directory)
    for key, value in values.items():
        if key in PREF_KEYS:
            merged[key] = value
    path = prefs_path(directory)
    path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
