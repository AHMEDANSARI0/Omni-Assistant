"""Multilingual interface layer (Transformation Phase 17).

Interface text only: the assistant never translates your data, your task
instructions, file paths or provider messages. Four locales ship with real
translations written for this product - English (source of truth), Spanish,
Urdu and Arabic - with Urdu and Arabic marked right-to-left, plus an honest
coverage number for every locale and a visible fallback to English for any text
that is not written yet.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_LOCALE",
    "DIRECTIONS",
    "LOCALES",
    "LOCALE_NAMES",
    "MESSAGES",
    "Translator",
    "catalog_for",
    "coverage",
    "locale_rows",
    "normalize_locale",
    "placeholder_names",
]

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
from .translator import Translator, locale_rows, normalize_locale
