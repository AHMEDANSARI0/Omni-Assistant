"""Accessibility state, measured contrast and honest limitation reporting.

Phase 17 makes the interface usable without a mouse, readable at a larger text
size, and legible for low-vision users - and it refuses to pretend. Every claim
in :func:`a11y_status` is either a measured number (WCAG 2.1 contrast ratios
computed from the real palette) or a structural fact about this code base
(keyboard shortcuts that exist in ``gui/app.py``, the focus order the widgets are
built in, the fact that no panel updates on an unpausable timer).

Tk 8.6 does not implement Unicode bidi or complex-script shaping, so Arabic and
Urdu text in the *desktop* window may not join correctly depending on the
platform font. That limit is reported as a limit, not hidden: the layout is
mirrored and alignment is set, while the web dashboard and the CLI - which use
the platform's own text stack - render these scripts correctly.
"""

from __future__ import annotations

from typing import Any

from .errors import ConfigError
from .i18n import DIRECTIONS, LOCALES, Translator, locale_rows, normalize_locale

__all__ = [
    "CONTRAST_TARGETS",
    "HIGH_CONTRAST_PALETTE",
    "PALETTES",
    "THEME_PALETTE",
    "a11y_status",
    "apply_ui_settings",
    "contrast_ratio",
    "relative_luminance",
    "resolve_ui",
]

#: WCAG 2.1 minimums that this report is measured against.
CONTRAST_TARGETS: dict[str, float] = {
    "body_text": 4.5,
    "large_text": 3.0,
    "ui_component": 3.0,
}

#: The real palette used by ``web_ui`` / the desktop theme, as hex triples.
THEME_PALETTE: dict[str, str] = {
    "background": "#050b16",
    "panel": "#0a1526",
    "text": "#d9ecff",
    "muted": "#7fa3c8",
    "accent": "#38e1ff",
    "ok": "#2fe6a7",
    "warn": "#ffb84d",
    "bad": "#ff5d73",
}

#: The high-contrast palette this phase adds (opt-in, persisted setting).
HIGH_CONTRAST_PALETTE: dict[str, str] = {
    "background": "#000000",
    "panel": "#0b0b0b",
    "text": "#ffffff",
    "muted": "#f0f0f0",
    "accent": "#00e5ff",
    "ok": "#00ff9c",
    "warn": "#ffd400",
    "bad": "#ff5c5c",
}

PALETTES: dict[str, dict[str, str]] = {
    "jarvis": THEME_PALETTE,
    "high-contrast": HIGH_CONTRAST_PALETTE,
}

#: Minimum interactive target height (px) this build aims at for the web UI and
#: for tkinter rows. 24 px matches WCAG 2.1 AA (2.5.8 target size, minimum).
MIN_TARGET_PX = 24

#: Keyboard shortcuts that really exist in this build (kept in sync by tests that
#: read ``gui/app.py``): documented here for the report and the help tab.
KEYBOARD_SHORTCUTS: tuple[dict[str, str], ...] = (
    {"keys": "Tab / Shift+Tab", "action": "Move focus forward / backward"},
    {"keys": "Alt+1..9", "action": "Jump to a notebook tab"},
    {"keys": "Ctrl+R", "action": "Refresh the live data on the current tab"},
    {"keys": "Escape", "action": "Close the active dialog and cancel edits"},
    {"keys": "Space / Return", "action": "Activate the focused button"},
    {"keys": "Ctrl+Q", "action": "Quit the desktop application"},
)

#: Statements this build can actually make. Anything not listed here is not
#: claimed - the report is a whitelist, not a wish list.
_STRUCTURAL_CLAIMS: tuple[dict[str, Any], ...] = (
    {
        "id": "keyboard_focus",
        "statement": "a11y.focus_order",
        "verified_by": "every control is a native ttk widget, so Tab reaches it",
    },
    {
        "id": "no_keyboard_trap",
        "statement": "a11y.no_trap",
        "verified_by": "no modal grab is used anywhere in gui/app.py",
    },
    {
        "id": "escape_cancels",
        "statement": "a11y.escape",
        "verified_by": "dialogs bind <Escape> to their cancel handler",
    },
    {
        "id": "shortcuts",
        "statement": "a11y.shortcuts",
        "verified_by": "Alt+<digit> and Ctrl+R bindings in gui/app.py",
    },
    {
        "id": "no_forced_timing",
        "statement": "a11y.no_timing",
        "verified_by": (
            "live refreshes are user-initiated or coalesced background reads; "
            "nothing is dismissed on a timer"
        ),
    },
)

#: Things this build explicitly does *not* provide yet.
KNOWN_LIMITATIONS: tuple[str, ...] = (
    (
        "Tk 8.6 has no Unicode bidi/shaping: Arabic and Urdu may not join correctly "
        "in the desktop window; the layout is mirrored and alignment is set, while "
        "the web dashboard and CLI render these scripts correctly."
    ),
    (
        "The desktop window does not publish an accessibility tree, so screen "
        "readers see the OS-drawn widgets only; the web dashboard provides ARIA "
        "roles, live regions and a skip link instead."
    ),
    (
        "No high-contrast OS theme is detected automatically; the palette is an "
        "explicit, persisted opt-in."
    ),
    "No captioning or sign-language track exists for voice output.",
    (
        "Only four interface languages ship; every other locale must be added as a "
        "catalog with real translations."
    ),
)


# --------------------------------------------------------------------------- #
# colour maths (WCAG 2.1 relative luminance + contrast ratio)
# --------------------------------------------------------------------------- #
def _channel(value: int) -> float:
    srgb = value / 255.0
    return srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_colour: str) -> float:
    """WCAG 2.1 relative luminance of ``#rrggbb``."""

    text = str(hex_colour or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    if len(text) != 6:
        raise ConfigError(f"'{hex_colour}' is not a #rrggbb colour.")
    try:
        red, green, blue = (int(text[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError as exc:
        raise ConfigError(f"'{hex_colour}' is not a #rrggbb colour.") from exc
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 2.1 contrast ratio, always >= 1.0, rounded to 2 decimals."""

    first = relative_luminance(foreground)
    second = relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return round((lighter + 0.05) / (darker + 0.05), 2)


def _palette_checks(palette: dict[str, str]) -> list[dict[str, Any]]:
    background = palette["background"]
    pairs = (
        ("text_on_background", palette["text"], background, "body_text"),
        ("text_on_panel", palette["text"], palette["panel"], "body_text"),
        ("muted_on_panel", palette["muted"], palette["panel"], "body_text"),
        ("accent_on_background", palette["accent"], background, "large_text"),
        ("ok_on_panel", palette["ok"], palette["panel"], "ui_component"),
        ("warn_on_panel", palette["warn"], palette["panel"], "ui_component"),
        ("bad_on_panel", palette["bad"], palette["panel"], "ui_component"),
    )
    rows = []
    for name, foreground, back, target in pairs:
        ratio = contrast_ratio(foreground, back)
        minimum = CONTRAST_TARGETS[target]
        rows.append(
            {
                "pair": name,
                "foreground": foreground,
                "background": back,
                "ratio": ratio,
                "target": target,
                "minimum": minimum,
                "passes": ratio >= minimum,
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# resolve UI settings
# --------------------------------------------------------------------------- #
def resolve_ui(settings: Any) -> dict[str, Any]:
    """Read the Phase 17 settings into one plain, validated structure."""

    locale = normalize_locale(str(getattr(settings, "ui_locale", "en") or "en"))
    scale = float(getattr(settings, "ui_scale", 1.0) or 1.0)
    if not 0.8 <= scale <= 2.0:
        raise ConfigError("PC_AGENT_UI_SCALE must be between 0.8 and 2.0.")
    theme = "high-contrast" if bool(getattr(settings, "ui_high_contrast", False)) else "jarvis"
    return {
        "locale": locale,
        "direction": DIRECTIONS.get(locale, "ltr"),
        "rtl": DIRECTIONS.get(locale, "ltr") == "rtl",
        "scale": scale,
        "theme": theme,
        "palette": dict(PALETTES[theme]),
        "high_contrast": theme == "high-contrast",
        "reduce_motion": bool(getattr(settings, "ui_reduce_motion", False)),
        "keyboard_shortcuts": bool(getattr(settings, "ui_keyboard_shortcuts", True)),
        "cli_color": str(getattr(settings, "ui_cli_color", "auto") or "auto"),
    }


def apply_ui_settings(settings: Any, *, locale: str | None = None, scale: float | None = None,
                      high_contrast: bool | None = None, reduce_motion: bool | None = None) -> dict[str, Any]:
    """Validate a requested change and return the new values.

    The caller persists them (``pc-agent i18n set`` writes the settings file);
    this function never writes and never mutates ``settings`` in place, because
    ``Settings`` is a frozen/slots dataclass.
    """

    current = resolve_ui(settings)
    updated = {
        "ui_locale": normalize_locale(locale) if locale is not None else current["locale"],
        "ui_scale": float(scale) if scale is not None else current["scale"],
        "ui_high_contrast": (
            bool(high_contrast) if high_contrast is not None else current["high_contrast"]
        ),
        "ui_reduce_motion": (
            bool(reduce_motion)
            if reduce_motion is not None
            else current["reduce_motion"]
        ),
    }
    if not 0.8 <= updated["ui_scale"] <= 2.0:
        raise ConfigError("Text size must be between 0.8 and 2.0 (80%-200%).")
    return updated


def a11y_status(settings: Any) -> dict[str, Any]:
    """The honest accessibility report behind CLI, API, diagnostics and GUI."""

    ui = resolve_ui(settings)
    translator = Translator(ui["locale"])
    palette_rows = _palette_checks(ui["palette"])
    passing = sum(1 for row in palette_rows if row["passes"])
    return {
        "locale": ui["locale"],
        "language_name": translator.language_name,
        "direction": ui["direction"],
        "rtl": ui["rtl"],
        "scale": ui["scale"],
        "theme": ui["theme"],
        "high_contrast": ui["high_contrast"],
        "reduce_motion": ui["reduce_motion"],
        "keyboard_shortcuts": ui["keyboard_shortcuts"],
        "cli_color": ui["cli_color"],
        "locales": locale_rows(),
        "supported_locales": list(LOCALES),
        "coverage": translator.coverage(),
        "fallback_notice": translator.notice(),
        "contrast": {
            "palette": ui["theme"],
            "pairs": palette_rows,
            "passing": passing,
            "total": len(palette_rows),
            "wcag_level": "AA" if passing == len(palette_rows) else "below AA",
        },
        "targets": dict(CONTRAST_TARGETS),
        "min_target_px": MIN_TARGET_PX,
        "shortcuts": [dict(entry) for entry in KEYBOARD_SHORTCUTS],
        "claims": [
            {
                "id": claim["id"],
                "statement": translator.t(str(claim["statement"])),
                "verified_by": claim["verified_by"],
            }
            for claim in _STRUCTURAL_CLAIMS
        ],
        "limitations": list(KNOWN_LIMITATIONS),
    }


def a11y_summary_line(settings: Any) -> str:
    """One line for ``pc-agent doctor``."""

    report = a11y_status(settings)
    contrast = report["contrast"]
    return (
        f"locale={report['locale']} ({report['language_name']}, {report['direction']}) "
        f"coverage={report['coverage']['percent']}% "
        f"scale={int(report['scale'] * 100)}% theme={report['theme']} "
        f"contrast={contrast['passing']}/{contrast['total']} {contrast['wcag_level']} "
        f"motion={'reduced' if report['reduce_motion'] else 'full'}"
    )
