# Transformation Phase 17 - Accessibility, Multilingual UI, RTL and Polish

Application version **0.79.1**, database schema **v18** (unchanged - interface
preferences are a small `data/ui.json`, not a table), release audit **31 checks**,
**19** GUI tabs, tool registry unchanged at **69** built-ins.

The goal of this phase is not "more features". It is that the same assistant can be
read and driven by someone who uses a different language, needs larger text, needs
high contrast, cannot use a mouse, or is sensitive to motion - and that every claim
made about that is either measured or disclosed as a limit.

---

## 1. What already existed before this phase

* 18 GUI tabs, a web Command Center, an HTTP API and a large CLI - all English only,
  fixed 10 pt text, one dark palette, mouse-first in practice.
* No text direction concept, no translation layer, no contrast data, no keyboard
  shortcut documentation, no accessibility statement beyond what the OS draws.
* No local performance measurement command (benchmarks existed only in reports).

## 2. What was missing

1. A translation layer with real translations and an honest coverage number.
2. Right-to-left support that actually changes the layout, not just a label.
3. User controls for text size, contrast and motion that persist across restarts.
4. Accessibility information the user can inspect: what shortcuts exist, what
   contrast is actually achieved, what this build does *not* do.
5. Web-dashboard semantics (skip link, landmarks, live regions, focus visibility).
6. A repeatable, local performance measurement instead of quoted numbers.

## 3. What was built

### 3.1 `src/pc_agent/i18n/` - translation layer

* `catalog.py`: **61 interface keys** in **four locales** - English (source of
  truth), Spanish, **Urdu** and **Arabic** - written for this product. `DIRECTIONS`
  marks Urdu/Arabic as `rtl`. `IDENTICAL_OK` is a four-entry allow-list of product
  names/symbols, so a value that is merely a copy of the English string fails the
  test suite.
* `translator.py`: `Translator` with `t(key, **values)`, explicit `{placeholder}`
  substitution (never `str.format` on caller text), English fallback for a missing
  key, key-echo for an unknown key, and a counted fallback so the UI can say
  "*N texts are shown in English because the translation is not written yet*".
  `normalize_locale()` accepts `en-US`, `EN`, `ur_PK`; anything unsupported is
  refused with the list of real choices.
* Only *interface* strings are translated. Task instructions, reminders, file
  paths, provider errors and user content are never machine-translated - that
  would be inventing information.

### 3.2 `src/pc_agent/a11y.py` - measured accessibility

* Real WCAG 2.1 maths: `relative_luminance()` and `contrast_ratio()` implemented
  from the specification (verified in tests against white/black = 21.0 and
  identical colours = 1.0).
* Two palettes: the `jarvis` theme and a new **high-contrast** palette. Every
  text/UI pair is checked against 4.5 (body), 3.0 (large text, UI parts).
* `a11y_status()` returns: locale, language name, direction, RTL flag, text scale,
  theme, motion state, CLI colour mode, all locales with coverage, the contrast
  table with `passing/total` and the resulting WCAG level, the keyboard shortcut
  list, the claims this build can actually make (each with *how it is verified*),
  and **five disclosed limitations**.
* The limitations are not marketing: Tk 8.6 has no bidi/shaping, the desktop window
  publishes no accessibility tree, no OS high-contrast theme detection, no
  captioning, only four locales ship.

### 3.3 `src/pc_agent/ui_prefs.py` - persistence without a migration

`data/ui.json` holds six values (`locale`, `scale`, `high_contrast`,
`reduce_motion`, `keyboard_shortcuts`, `cli_color`). Precedence is
**environment variable → `ui.json` → default**; a corrupt file silently falls back
to defaults instead of breaking startup. `pc-agent i18n set` writes it and says
plainly *"restart the desktop app to apply"* and warns when an environment variable
overrides the stored value.

### 3.4 Six new settings

| Variable | Default | Values |
| --- | --- | --- |
| `PC_AGENT_UI_LOCALE` | `en` | `en`, `es`, `ur`, `ar` (case/region normalised) |
| `PC_AGENT_UI_SCALE` | `1.0` | 0.8 - 2.0 |
| `PC_AGENT_UI_HIGH_CONTRAST` | off | truthy/falsy |
| `PC_AGENT_UI_REDUCE_MOTION` | off | truthy/falsy |
| `PC_AGENT_UI_KEYBOARD_SHORTCUTS` | on | truthy/falsy |
| `PC_AGENT_UI_CLI_COLOR` | `auto` | `auto`, `always`, `never` |

Bad values are refused at load with the exact accepted list - never coerced.

### 3.5 Desktop GUI (19th tab **Accessibility**)

* Language picker (all four locales), text size 80-200 %, high-contrast and
  reduce-motion checkboxes, **Apply and save**, **Refresh report**, **Run a11y check**.
* The report pane renders the real contrast table, the shortcut list, the claims and
  the disclosed limits.
* The theme is applied from the resolved settings: font sizes scale from the base
  size, the palette colours the window and styles, and RTL **mirrors the header**
  (`grid_configure`) so the change is a layout change, not a label.

### 3.6 Web Command Center

* `lang`/`dir` set from the resolved locale (real RTL mirroring via `[dir="rtl"]`).
* A **skip link**, `role="banner"`, `role="main"` + `id="main"` + `tabindex="-1"`.
* `aria-live="polite"` status regions for the toast and the accessibility notice.
* `:focus-visible` outlines, `prefers-reduced-motion` media query, a
  `body.reduce-motion` class and a `body.high-contrast` palette.
* A real **Accessibility** panel: pick language, type a size, tick contrast/motion,
  **Apply here now** (instant, tab-local) and **Save** (`POST /v1/i18n/locale`).

### 3.7 CLI and API

* `pc-agent i18n status|list|strings|set`
* `pc-agent a11y status|check|contrast`
* `pc-agent perf bench`
* `doctor` prints an `Accessibility:` line; `diagnostics` gains an `a11y` section;
  release check **31** (`accessibility`) measures and reports.
* HTTP: `GET /v1/i18n/status`, `GET /v1/i18n/messages`, `POST /v1/i18n/locale`,
  `GET /v1/a11y/status`, `GET /v1/perf/bench`.

## 4. Files added / changed

* Added: `src/pc_agent/i18n/{__init__,catalog,translator}.py`,
  `src/pc_agent/a11y.py`, `src/pc_agent/ui_prefs.py`,
  `tests/test_transformation_phase17_accessibility.py`,
  `docs/transformation_phase17.md`.
* Changed: `config.py` (6 settings + validators reading `ui.json`), `service.py`
  (`i18n_status`, `i18n_messages`, `i18n_set`, `a11y_status`, `perf_bench`),
  `api_server.py` (5 routes), `cli.py` (`i18n`, `a11y`, `perf` + doctor line),
  `diagnostics.py` (`a11y` section), `release.py` (check 31), `gui/app.py`
  (tab 19, scaled styles, RTL mirroring), `gui/controller.py` (4 methods),
  `web_ui.py` (ARIA/RTL/contrast/motion + panel), `baseline.py`, `__init__.py`,
  `pyproject.toml`, plus the delivery documents.

## 5. Deliberately not included

* No machine translation of user content, and no provider call for translation.
* No new locale without real translations (the test refuses copies).
* No automatic OS high-contrast/HC-theme detection.
* No desktop accessibility tree (a Tk limitation, disclosed).
* No voice-output captioning, no sign-language track.
* No reflow-to-one-column layout for narrow screens beyond the existing responsive
  grid behaviour.
* Schema stays **v18** - preferences are a file, so no migration was needed.

## 6. Tests and results

`tests/test_transformation_phase17_accessibility.py` - **44 tests**:

* catalogues: full key coverage in all four locales, **no English copies**, identical
  placeholders, locale names in their own script, declared directions, computed
  coverage, locale rows;
* translator: real Urdu/Arabic strings, placeholder substitution, missing-value
  error, unknown key echo, unsupported locale refusal, `en-US`/`UR_pk` normalisation,
  coverage notice, and a monkeypatched partial locale proving the honest fallback;
* WCAG: white/black = 21.0, identical colours = 1.0, black = 0/white = 1 luminance,
  bad colour refusal, **both shipped palettes pass AA**, high-contrast really differs;
* settings: English/LTR defaults, environment overrides, five bad-value refusals,
  `ui.json` persistence, environment-still-wins, corrupt file tolerance,
  `apply_ui_settings` validation;
* report honesty: five disclosed limits, no overclaim strings, locale-following
  report (Urdu claim text asserted verbatim), single-line doctor summary,
  diagnostics section, release check 31 pass **and** a deliberately broken palette
  turning it into a warn;
* surfaces: service set/report/persistence/event, refusals, messages, `perf_bench`
  shape; CLI `i18n list|set`, refusal exit 1, `a11y contrast`, `perf bench`,
  `a11y check` exit 0 with `[PASS] contrast_aa`, doctor line; 5 HTTP routes;
  GUI constant 19 + controller text + source markers; web markers (skip link,
  `aria-live`, `id="main"`, `prefers-reduced-motion`, `:focus-visible`,
  `high-contrast`, `dir="rtl"`, save/apply buttons).

Full suite: **982 passed, 1 skipped** (the 1 skip is the real-Tk-window test, which
needs a display; `ruff check` clean).

## 7. Security implications

* No new network surface, no new secret, no provider call. `i18n` and `a11y` read
  local settings only.
* `POST /v1/i18n/locale` stays behind the existing bearer token and validates every
  field; it can change presentation, never capabilities, paths or gates.
* The preference file contains six presentation values and nothing else; a corrupt
  or hostile file cannot enable a feature (unknown values are refused, unknown keys
  ignored).
* Translations cannot alter behaviour: placeholders are substituted from an explicit
  mapping, and user content is never translated.
* No user text, path or secret is written to logs by this layer.

## 8. Performance implications

Measured on Linux (this workspace) with `pc-agent perf bench`:

| Operation | Measured |
| --- | --- |
| `a11y_status()` (contrast maths for 7 pairs + 4 locale coverages) | 0.06 ms |
| `health()` warm | 0.64 - 0.80 ms |
| `list_events(limit=20)` | 1.1 - 1.7 ms |
| `system_status()` | 2.6 - 3.4 ms |
| `health()` first call (cold service) | 0.75 - 0.97 ms |

The accessibility layer adds no background work: it is called only when a surface
asks for it, the contrast table is computed from 9 constants, and style application
happens once at window build. Nothing in this phase adds a timer, a thread or a
network call.

## 9. Limitations

* Tk 8.6 does not implement bidi/shaping: Arabic and Urdu in the desktop window may
  not join correctly depending on the platform font. The layout is mirrored, the web
  dashboard and CLI render the scripts correctly.
* The desktop window publishes no accessibility tree; screen-reader support is
  whatever the OS draws for standard widgets.
* No automatic OS theme/contrast detection.
* Coverage is 100 % for the 61 interface keys that exist; the wider product text
  (panels built before this phase, provider messages) is still English.
* This phase does not audit third-party output (documents, provider responses) for
  accessibility - it presents them as text.
* Native Windows verification for this phase has not yet been run.

## 10. Next tasks

> **Update (0.79.1):** the Windows acceptance run for 0.79.0 returned 12 failed /
> 919 passed / 8 skipped. All twelve are fixed in the 0.79.1 hotfix - see
> [`transformation_phase17_1.md`](transformation_phase17_1.md).

* **Phase 18** - final production hardening: CI, security/performance gates, signed
  packaging, update/rollback rehearsal, and the four still-missing delivery
  documents (`AUDIT_REPORT.md`, `DEVELOPER_GUIDE.md`, `INSTALLATION_GUIDE.md`,
  `PLUGIN_DEVELOPMENT_GUIDE.md`).
* Not scheduled: extending translations to every panel string, an OS-theme detector,
  a desktop accessibility-tree bridge, captioning for voice output.
