"""Transformation Phase 17 - accessibility, multilingual UI, RTL and polish.

These tests are deliberately adversarial about honesty:

* every shipped locale must carry a *real* translation, so a value that is simply
  the English string (for a key that is not a product name) fails;
* placeholders must match the English source exactly, so a translation cannot
  silently drop ``{count}``;
* every contrast number in the report is recomputed here from the WCAG 2.1
  formula against known reference values (white/black = 21.0);
* the accessibility report must disclose its limits instead of claiming
  screen-reader support this build does not have.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

from pc_agent import __version__
from pc_agent.a11y import (
    CONTRAST_TARGETS,
    HIGH_CONTRAST_PALETTE,
    KEYBOARD_SHORTCUTS,
    KNOWN_LIMITATIONS,
    THEME_PALETTE,
    a11y_status,
    a11y_summary_line,
    apply_ui_settings,
    contrast_ratio,
    relative_luminance,
    resolve_ui,
)
from pc_agent.api_server import LocalApiServer
from pc_agent.baseline import TRANSFORMATION_LABEL, TRANSFORMATION_PHASE
from pc_agent.config import Settings
from pc_agent.diagnostics import build_diagnostics
from pc_agent.errors import ConfigError
from pc_agent.i18n import (
    DEFAULT_LOCALE,
    DIRECTIONS,
    LOCALE_NAMES,
    LOCALES,
    MESSAGES,
    Translator,
    catalog_for,
    coverage,
    locale_rows,
    normalize_locale,
    placeholder_names,
)
from pc_agent.release import audit_install
from pc_agent.service import LocalService
from pc_agent.storage.db import LATEST_SCHEMA_VERSION, Database
from pc_agent.ui_prefs import load_prefs, prefs_path, save_prefs


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def settings_for(root: Path, **env: str) -> Settings:
    settings = Settings.from_env(root, environ=dict(env), load_dotenv=False)
    settings.ensure_directories()
    Database(settings.database_path).initialize()
    return settings


@pytest.fixture()
def base(tmp_path: Path) -> Settings:
    return settings_for(tmp_path)


# --------------------------------------------------------------------------- #
# catalogs
# --------------------------------------------------------------------------- #
def test_every_locale_covers_every_english_key() -> None:
    source = set(MESSAGES[DEFAULT_LOCALE])
    assert source
    for locale in LOCALES:
        assert set(MESSAGES[locale]) == source, f"{locale} key set differs"


def test_no_locale_ships_a_copy_of_the_english_text() -> None:
    """A fake translation must fail this test."""

    from pc_agent.i18n.catalog import IDENTICAL_OK

    source = MESSAGES[DEFAULT_LOCALE]
    for locale in LOCALES:
        if locale == DEFAULT_LOCALE:
            continue
        identical = [
            key
            for key, value in MESSAGES[locale].items()
            if key not in IDENTICAL_OK and value.strip() == source[key].strip()
        ]
        assert identical == [], f"{locale} has untranslated copies: {identical}"


def test_placeholders_survive_translation() -> None:
    source = MESSAGES[DEFAULT_LOCALE]
    for locale in LOCALES:
        for key, value in MESSAGES[locale].items():
            assert placeholder_names(value) == placeholder_names(source[key]), (
                f"{locale}:{key} changed its placeholders"
            )


def test_locale_names_are_written_in_their_own_language() -> None:
    assert LOCALE_NAMES["ur"] == "اردو"
    assert LOCALE_NAMES["ar"] == "العربية"
    assert LOCALE_NAMES["es"] == "Español"
    assert LOCALE_NAMES["en"] == "English"


def test_direction_is_declared_for_every_locale() -> None:
    assert set(DIRECTIONS) == set(LOCALES)
    assert DIRECTIONS["ur"] == "rtl"
    assert DIRECTIONS["ar"] == "rtl"
    assert DIRECTIONS["en"] == "ltr"
    assert DIRECTIONS["es"] == "ltr"


def test_coverage_is_computed_not_asserted() -> None:
    for locale in LOCALES:
        report = coverage(locale)
        assert report["total_keys"] == len(MESSAGES[DEFAULT_LOCALE])
        assert report["translated_keys"] == len(MESSAGES[locale])
        assert report["percent"] == pytest.approx(
            len(MESSAGES[locale]) * 100.0 / len(MESSAGES[DEFAULT_LOCALE]), abs=0.05
        )
        assert report["missing_keys"] == []


def test_locale_rows_expose_names_directions_and_percentages() -> None:
    rows = locale_rows()
    assert [row["locale"] for row in rows] == list(LOCALES)
    assert sum(1 for row in rows if row["direction"] == "rtl") == 2
    assert all(row["percent"] == 100.0 for row in rows)


# --------------------------------------------------------------------------- #
# translator
# --------------------------------------------------------------------------- #
def test_translator_returns_real_urdu_and_arabic() -> None:
    urdu = Translator("ur")
    arabic = Translator("ar")
    assert urdu.t("nav.dashboard") == "ڈیش بورڈ"
    assert arabic.t("nav.dashboard") == "لوحة التحكم"
    assert urdu.direction == "rtl" and urdu.is_rtl is True
    assert arabic.is_rtl is True
    assert Translator("es").is_rtl is False


def test_translator_substitutes_placeholders() -> None:
    english = Translator("en")
    assert english.t("label.tab_index", index=3) == "Tab 3"
    assert "اردو" in Translator("ur").t("msg.language_changed", name="اردو")
    assert "150" in Translator("es").t("msg.scale_applied", percent=150)


def test_missing_placeholder_value_is_an_error() -> None:
    with pytest.raises(ConfigError):
        Translator("en").t("label.tab_index")


def test_unknown_key_returns_the_key_and_is_visible() -> None:
    assert Translator("en").t("does.not.exist") == "does.not.exist"


def test_unknown_locale_falls_back_to_english_keys_but_refuses_to_invent() -> None:
    with pytest.raises(ConfigError):
        Translator("fr")
    assert normalize_locale("") == DEFAULT_LOCALE
    assert normalize_locale("en-US") == "en"
    assert normalize_locale("UR_pk") == "ur"
    assert catalog_for("fr")["nav.dashboard"] == "Dashboard"


def test_translator_reports_what_it_cannot_show() -> None:
    translator = Translator("ur")
    assert translator.coverage()["percent"] == 100.0
    assert translator.notice() == ""
    assert translator.fallback_count == 0
    payload = Translator("ar").as_dict()
    assert payload["rtl"] is True
    assert payload["name"] == "العربية"
    assert payload["missing_keys"] == []


def test_partial_locale_is_reported_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    from pc_agent.i18n import catalog as catalog_module

    trimmed = dict(catalog_module.MESSAGES["es"])
    trimmed.pop("nav.sync")
    monkeypatch.setitem(catalog_module.MESSAGES, "es", trimmed)
    translator = Translator("es")
    assert translator.t("nav.sync") == "Sync"  # English fallback, deliberately
    assert translator.fallback_count == 1
    assert translator.coverage()["missing_keys"] == ["nav.sync"]
    assert "{count}" not in translator.notice()


# --------------------------------------------------------------------------- #
# WCAG contrast maths
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("foreground", "background", "expected"),
    [
        ("#ffffff", "#000000", 21.0),
        ("#000000", "#ffffff", 21.0),
        ("#777777", "#777777", 1.0),
        ("#000000", "#000000", 1.0),
    ],
)
def test_contrast_known_reference_values(foreground, background, expected) -> None:
    assert contrast_ratio(foreground, background) == pytest.approx(expected, abs=0.01)


def test_relative_luminance_matches_wcag_reference() -> None:
    assert relative_luminance("#ffffff") == pytest.approx(1.0, abs=0.001)
    assert relative_luminance("#000000") == pytest.approx(0.0, abs=0.001)
    assert relative_luminance("fff") == relative_luminance("#ffffff")


def test_bad_colour_is_refused() -> None:
    for value in ("", "blue", "#12", "#zzzzzz"):
        with pytest.raises(ConfigError):
            relative_luminance(value)


def test_every_shipped_palette_meets_wcag_aa(base: Settings) -> None:
    for palette_name in ("jarvis", "high-contrast"):
        report = a11y_status(replace(base, ui_high_contrast=palette_name == "high-contrast"))
        contrast = report["contrast"]
        assert contrast["palette"] == palette_name
        assert contrast["passing"] == contrast["total"], (
            f"{palette_name} has a failing pair: "
            + ", ".join(
                "{}={}".format(row["pair"], row["ratio"])
                for row in contrast["pairs"]
                if not row["passes"]
            )
        )
        assert contrast["wcag_level"] == "AA"
        for row in contrast["pairs"]:
            assert row["ratio"] == contrast_ratio(row["foreground"], row["background"])
            assert row["minimum"] == CONTRAST_TARGETS[row["target"]]


def test_high_contrast_palette_really_differs() -> None:
    assert HIGH_CONTRAST_PALETTE["background"] != THEME_PALETTE["background"]
    assert relative_luminance(HIGH_CONTRAST_PALETTE["text"]) > relative_luminance(
        THEME_PALETTE["text"]
    )


# --------------------------------------------------------------------------- #
# resolution + preferences persistence
# --------------------------------------------------------------------------- #
def test_defaults_are_english_ltr_full_motion(base: Settings) -> None:
    ui = resolve_ui(base)
    assert ui["locale"] == "en"
    assert ui["direction"] == "ltr"
    assert ui["rtl"] is False
    assert ui["scale"] == 1.0
    assert ui["theme"] == "jarvis"
    assert ui["reduce_motion"] is False


def test_environment_controls_the_interface(tmp_path: Path) -> None:
    settings = settings_for(
        tmp_path,
        PC_AGENT_UI_LOCALE="ar",
        PC_AGENT_UI_SCALE="1.3",
        PC_AGENT_UI_HIGH_CONTRAST="1",
        PC_AGENT_UI_REDUCE_MOTION="true",
        PC_AGENT_UI_CLI_COLOR="never",
    )
    ui = resolve_ui(settings)
    assert (ui["locale"], ui["direction"], ui["rtl"]) == ("ar", "rtl", True)
    assert ui["scale"] == 1.3
    assert ui["theme"] == "high-contrast"
    assert ui["reduce_motion"] is True
    assert ui["cli_color"] == "never"
    summary = settings.public_summary()
    assert summary["ui_locale"] == "ar"
    assert summary["ui_theme"] == "high-contrast"


def test_bad_values_are_refused_at_load(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(tmp_path, environ={"PC_AGENT_UI_LOCALE": "fr"}, load_dotenv=False)
    with pytest.raises(ConfigError):
        Settings.from_env(tmp_path, environ={"PC_AGENT_UI_SCALE": "3.5"}, load_dotenv=False)
    with pytest.raises(ConfigError):
        Settings.from_env(tmp_path, environ={"PC_AGENT_UI_SCALE": "wide"}, load_dotenv=False)
    with pytest.raises(ConfigError):
        Settings.from_env(
            tmp_path, environ={"PC_AGENT_UI_HIGH_CONTRAST": "maybe"}, load_dotenv=False
        )
    with pytest.raises(ConfigError):
        Settings.from_env(
            tmp_path, environ={"PC_AGENT_UI_CLI_COLOR": "rainbow"}, load_dotenv=False
        )


def test_preference_file_persists_and_environment_still_wins(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    path = save_prefs(
        settings.data_dir,
        {"locale": "ur", "scale": 1.25, "high_contrast": True, "reduce_motion": True},
    )
    assert path == prefs_path(settings.data_dir)
    assert json.loads(path.read_text(encoding="utf-8"))["locale"] == "ur"
    reloaded = Settings.from_env(tmp_path, environ={}, load_dotenv=False)
    assert reloaded.ui_locale == "ur"
    assert reloaded.ui_scale == 1.25
    assert reloaded.ui_high_contrast is True
    assert reloaded.ui_reduce_motion is True
    overridden = Settings.from_env(
        tmp_path, environ={"PC_AGENT_UI_LOCALE": "es"}, load_dotenv=False
    )
    assert overridden.ui_locale == "es"


def test_corrupt_preference_file_does_not_break_startup(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    prefs_path(settings.data_dir).write_text("{not json", encoding="utf-8")
    assert load_prefs(settings.data_dir) == {}
    reloaded = Settings.from_env(tmp_path, environ={}, load_dotenv=False)
    assert reloaded.ui_locale == "en"


def test_apply_ui_settings_validates_before_writing(base: Settings) -> None:
    updated = apply_ui_settings(base, locale="ur-PK", scale=1.4, reduce_motion=True)
    assert updated == {
        "ui_locale": "ur",
        "ui_scale": 1.4,
        "ui_high_contrast": False,
        "ui_reduce_motion": True,
    }
    with pytest.raises(ConfigError):
        apply_ui_settings(base, scale=5.0)
    with pytest.raises(ConfigError):
        apply_ui_settings(base, locale="de")


# --------------------------------------------------------------------------- #
# the honest report
# --------------------------------------------------------------------------- #
def test_report_discloses_limits_and_shortcuts(base: Settings) -> None:
    report = a11y_status(base)
    assert len(report["limitations"]) == len(KNOWN_LIMITATIONS) >= 3
    assert any("bidi" in item for item in report["limitations"])
    assert any("accessibility tree" in item for item in report["limitations"])
    assert len(report["shortcuts"]) == len(KEYBOARD_SHORTCUTS)
    assert report["claims"]
    assert all(claim["verified_by"] for claim in report["claims"])


def test_report_never_claims_screen_reader_support(base: Settings) -> None:
    report = a11y_status(base)
    blob = json.dumps(report).lower()
    for overclaim in ("fully accessible", "screen reader support", "wcag aaa"):
        assert overclaim not in blob


def test_report_follows_the_locale(base: Settings) -> None:
    report = a11y_status(replace(base, ui_locale="ur"))
    assert report["locale"] == "ur"
    assert report["direction"] == "rtl"
    assert report["rtl"] is True
    assert report["coverage"]["percent"] == 100.0
    assert report["shortcuts"]
    assert report["claims"][0]["statement"] == "ٹیب دبانے سے فوکس ہر کنٹرول پر جاتا ہے۔"


def test_summary_line_is_a_single_line(base: Settings) -> None:
    line = a11y_summary_line(base)
    assert "\n" not in line
    assert line.startswith("locale=en")
    assert "AA" in line
    assert "scale=100%" in line


def test_diagnostics_and_release_cover_accessibility(base: Settings) -> None:
    section = build_diagnostics(base, Database(base.database_path))["a11y"]
    assert section["available"] is True
    assert section["coverage_percent"] == 100.0
    assert section["contrast_passing"] == section["contrast_total"]
    assert section["known_limitations"] >= 3

    audit = audit_install(base)
    assert len(audit.checks) == 33
    check = next(item for item in audit.checks if item.name == "accessibility")
    assert check.status == "pass"
    assert "WCAG 2.1 AA" in check.detail


def test_release_check_warns_when_contrast_cannot_be_guaranteed(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    from pc_agent import a11y as a11y_module

    broken = dict(a11y_module.THEME_PALETTE)
    broken["muted"] = "#16233a"  # deliberately unreadable on the dark panel
    a11y_module.PALETTES["jarvis"] = broken
    try:
        check = next(
            item for item in audit_install(settings).checks if item.name == "accessibility"
        )
        assert check.status == "warn"
    finally:
        a11y_module.PALETTES["jarvis"] = a11y_module.THEME_PALETTE


# --------------------------------------------------------------------------- #
# service, CLI, API, GUI and web surfaces
# --------------------------------------------------------------------------- #
def test_service_sets_and_reports_the_language(base: Settings) -> None:
    service = LocalService(base)
    before = service.i18n_status()
    assert before["locale"] == "en" and before["direction"] == "ltr"
    result = service.i18n_set(locale="ar", scale=1.2, high_contrast=True)
    assert result["saved"] == {"locale": "ar", "scale": 1.2, "high_contrast": True}
    assert Path(result["path"]).is_file()
    assert load_prefs(base.data_dir)["locale"] == "ar"
    events = [event["event_type"] for event in service.list_events(limit=20)["events"]]
    assert "ui.preferences_saved" in events
    after = LocalService(Settings.from_env(base.root_dir, environ={}, load_dotenv=False))
    assert after.i18n_status()["locale"] == "ar"
    assert after.i18n_status()["direction"] == "rtl"


def test_service_refuses_a_bad_change(base: Settings) -> None:
    service = LocalService(base)
    with pytest.raises(ConfigError):
        service.i18n_set(locale="fr")
    with pytest.raises(ConfigError):
        service.i18n_set(scale=9.0)
    with pytest.raises(ConfigError):
        service.i18n_set()


def test_service_messages_and_perf_are_real(base: Settings) -> None:
    service = LocalService(base)
    payload = service.i18n_messages("ur")
    assert payload["direction"] == "rtl"
    assert payload["messages"]["nav.sync"] == "ہم آہنگی"
    bench = service.perf_bench(iterations=2)
    assert bench["schema"] == LATEST_SCHEMA_VERSION
    assert set(bench["measurements_ms"]) >= {
        "health_first_call",
        "health_warm",
        "events_list",
        "a11y_status",
        "system_status",
    }
    assert all(value >= 0 for value in bench["measurements_ms"].values())


def test_cli_surfaces(base: Settings, capsys) -> None:
    from pc_agent.cli import build_parser, main

    parser = build_parser()
    assert parser.parse_args(["i18n", "status"]).command == "i18n"
    assert parser.parse_args(["a11y", "check"]).a11y_command == "check"
    assert parser.parse_args(["perf", "bench"]).perf_command == "bench"

    assert main(["--root", str(base.root_dir), "i18n", "list"]) == 0
    listed = capsys.readouterr().out
    assert "en" in listed and "اردو" in listed and "rtl" in listed

    assert main(["--root", str(base.root_dir), "i18n", "set", "--locale", "ur"]) == 0
    assert "saved locale=ur" in capsys.readouterr().out
    assert load_prefs(base.data_dir)["locale"] == "ur"

    assert main(["--root", str(base.root_dir), "i18n", "set", "--locale", "fr"]) == 1
    assert "Unsupported interface language" in capsys.readouterr().err

    assert main(["--root", str(base.root_dir), "a11y", "contrast"]) == 0
    printed = capsys.readouterr().out
    assert "AA" in printed and ": 1" in printed

    assert main(["--root", str(base.root_dir), "perf", "bench", "--iterations", "1"]) == 0
    bench_out = capsys.readouterr().out
    assert "a11y_status" in bench_out and "ms" in bench_out


def test_a11y_check_command_exit_codes(base: Settings, capsys) -> None:
    from pc_agent.cli import main

    root = str(base.root_dir)
    assert main(["--root", root, "a11y", "check"]) == 0
    out = capsys.readouterr().out
    assert "[PASS] contrast_aa" in out
    assert "limits:" in out


def test_doctor_line_reports_accessibility(base: Settings, capsys) -> None:
    from pc_agent.cli import main

    assert main(["--root", str(base.root_dir), "doctor"]) in {0, 1}
    out = capsys.readouterr().out
    assert "Accessibility: locale=en" in out
    assert "AA" in out


def test_http_routes_are_wired(base: Settings) -> None:
    server = LocalApiServer(
        replace(base, api_port=0, scheduler_interval_seconds=0, api_enabled=True)
    )
    server.start()
    try:
        _host, port = server.address
        headers = {"Authorization": f"Bearer {server.token}"}
        base_url = f"http://127.0.0.1:{port}"

        def call(path: str, method: str = "GET", body: dict | None = None) -> dict:
            data = json.dumps(body).encode("utf-8") if body is not None else None
            request = urllib.request.Request(
                base_url + path,
                data=data,
                method=method,
                headers={**headers, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))["data"]

        status = call("/v1/i18n/status")
        assert status["locale"] == "en"
        assert len(status["locales"]) == len(LOCALES)
        messages = call("/v1/i18n/messages?locale=ur")
        assert messages["direction"] == "rtl"
        changed = call(
            "/v1/i18n/locale",
            "POST",
            {"locale": "ar", "scale": 1.1, "reduce_motion": True},
        )
        assert changed["saved"]["locale"] == "ar"
        a11y = call("/v1/a11y/status")
        assert a11y["contrast"]["wcag_level"] == "AA"
        bench = call("/v1/perf/bench?iterations=1")
        assert bench["measurements_ms"]
    finally:
        server.stop()


def test_gui_and_web_are_exposed(base: Settings) -> None:
    from pc_agent.gui.app import NOTEBOOK_TAB_COUNT
    from pc_agent.gui.controller import GuiController
    from pc_agent.web_ui import INDEX_HTML

    assert NOTEBOOK_TAB_COUNT == 20
    controller = GuiController(base)
    assert controller.i18n_status_text().startswith("language en")
    report = controller.a11y_report()
    assert report["locale"] == "en"
    text = controller.a11y_check_text()
    assert "[PASS] contrast_aa" in text
    assert "Known limits:" in text
    saved = controller.a11y_apply(
        locale="es", scale=1.13, high_contrast=False, reduce_motion=True
    )
    assert "locale=es" in saved

    for marker in (
        'Skip to main content',
        'aria-live="polite"',
        'id="main"',
        'prefers-reduced-motion',
        ":focus-visible",
        "high-contrast",
        'dir="rtl"',
        "a11y-save",
        "Apply here now",
    ):
        assert marker in INDEX_HTML, marker


def test_gui_source_really_contains_the_accessibility_controls(base: Settings) -> None:
    """A window needs a display; the wiring is still verifiable from source."""

    source = Path("src/pc_agent/gui/app.py").read_text(encoding="utf-8")
    for marker in (
        "self.accessibility_tab",
        "_build_accessibility_tab",
        "a11y_locale_var",
        "a11y_scale_var",
        "a11y_contrast_var",
        "a11y_motion_var",
        "_apply_direction",
        "NOTEBOOK_TAB_COUNT = 20",
    ):
        assert marker in source, marker


def test_identity_is_current() -> None:
    assert __version__ == "0.81.2"
    assert TRANSFORMATION_PHASE == 18
    assert TRANSFORMATION_LABEL == "TRANSFORMATION PHASE 18 Production Hardening"
    assert LATEST_SCHEMA_VERSION == 18  # interface settings live in data/ui.json
    assert len(LOCALES) == 4
