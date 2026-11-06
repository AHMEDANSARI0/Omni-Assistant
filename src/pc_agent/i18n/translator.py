"""Translator: bounded, fallback-honest lookup over the Phase 17 catalogs.

Rules enforced here:

* an unknown locale falls back to English instead of guessing;
* an unknown key returns the key itself (so a missing string is visible, never
  silently blank);
* a missing translation in a known locale falls back to English *and* is counted,
  so the UI can say "N texts are shown in English because the translation is not
  written yet";
* placeholders are substituted from an explicit mapping - no ``str.format`` on
  caller-supplied text, so a translated string can never mutate user content.
"""

from __future__ import annotations

from typing import Any

from ..errors import ConfigError
from .catalog import (
    DEFAULT_LOCALE,
    DIRECTIONS,
    LOCALE_NAMES,
    LOCALES,
    MESSAGES,
    catalog_for,
    coverage,
    placeholder_names,
)

__all__ = ["Translator", "locale_rows", "normalize_locale"]


def normalize_locale(locale: str) -> str:
    """Map ``en-US``/``EN``/``ur_PK`` onto a supported locale, or refuse."""

    raw = str(locale or "").strip().lower().replace("_", "-")
    if not raw:
        return DEFAULT_LOCALE
    base = raw.split("-", 1)[0]
    if base not in LOCALES:
        raise ConfigError(
            f"Unsupported interface language '{locale}'. Choose one of: "
            + ", ".join(sorted(LOCALES))
        )
    return base


def locale_rows() -> list[dict[str, Any]]:
    """Every supported locale with its real coverage and direction."""

    rows = []
    for locale in LOCALES:
        report = coverage(locale)
        rows.append(
            {
                "locale": locale,
                "name": LOCALE_NAMES.get(locale, locale),
                "direction": DIRECTIONS.get(locale, "ltr"),
                "is_default": locale == DEFAULT_LOCALE,
                "total_keys": report["total_keys"],
                "translated_keys": report["translated_keys"],
                "percent": report["percent"],
            }
        )
    return rows


class Translator:
    """Look up interface text for one locale, with visible fallbacks."""

    def __init__(self, locale: str = DEFAULT_LOCALE) -> None:
        self._locale = normalize_locale(locale)
        self._fallbacks: dict[str, int] = {}

    @property
    def locale(self) -> str:
        return self._locale

    @property
    def direction(self) -> str:
        return DIRECTIONS.get(self._locale, "ltr")

    @property
    def is_rtl(self) -> bool:
        return self.direction == "rtl"

    @property
    def language_name(self) -> str:
        return LOCALE_NAMES.get(self._locale, self._locale)

    @property
    def fallback_count(self) -> int:
        return sum(self._fallbacks.values())

    def _substitute(self, template: str, values: dict[str, Any]) -> str:
        rendered = template
        for name in placeholder_names(template):
            if name not in values:
                raise ConfigError(
                    f"Translated text needs '{name}' but no value was supplied."
                )
            rendered = rendered.replace("{" + name + "}", str(values[name]))
        return rendered

    def t(self, key: str, /, **values: Any) -> str:
        """Translate ``key``; fall back to English, then to the key itself."""

        target = catalog_for(self._locale)
        if key in target:
            return self._substitute(target[key], values)
        source = MESSAGES[DEFAULT_LOCALE]
        if key in source:
            self._fallbacks[key] = self._fallbacks.get(key, 0) + 1
            return self._substitute(source[key], values)
        return key

    def coverage(self) -> dict[str, Any]:
        return coverage(self._locale)

    def notice(self) -> str:
        """Human-readable note about what this locale cannot show yet."""

        count = len(self.coverage()["missing_keys"])
        if count == 0:
            return ""
        return self.t("msg.fallback_notice", count=count)

    def as_dict(self) -> dict[str, Any]:
        report = self.coverage()
        return {
            "locale": self._locale,
            "name": self.language_name,
            "direction": self.direction,
            "rtl": self.is_rtl,
            "total_keys": report["total_keys"],
            "translated_keys": report["translated_keys"],
            "percent": report["percent"],
            "missing_keys": report["missing_keys"],
            "fallbacks_used": self.fallback_count,
        }
