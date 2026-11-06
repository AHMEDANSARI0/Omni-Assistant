# Transformation Phase 11 — Advanced Browser, Windows Desktop Automation, Computer Vision

Version **0.73.0** · label **TRANSFORMATION PHASE 11 Advanced Automation** ·
schema **v13** · release audit **25 checks** · registered tools **69**
(+`browser.wait`, `browser.tabs`, `desktop.window_state`, `vision.screen`,
`vision.locate`)

Phase 11 makes the assistant *act* on a real desktop instead of only answering:
it can wait for a page condition instead of guessing a sleep, minimise / resize a
real window, and — offline, with no cloud call — read a screenshot well enough to
say where the text is and whether a known button is on screen. Every action is
bounded, gated by the existing opt-in settings, and written to a local audit
ledger.

---

## 1. What existed before this phase

| Area | State at 0.72.0 |
| --- | --- |
| Browser automation | `browser.status/open/snapshot/click/type/screenshot` over CDP, opt-in (`PC_AGENT_ALLOW_BROWSER`) — no waiting, no tab management |
| Desktop automation | `desktop.list_windows/inspect_window/click_element/set_element_text/…` over Windows UIA, opt-in — read/interact only, **no** window placement |
| Vision | `image.analyze` (provider vision, needs a key) and local Pillow metadata (`image.info`) — no offline understanding of *where* things are |
| Audit | Events/approvals for tasks — no per-action automation ledger |
| CLI | No automation group (browser/desktop only via tools/tasks) |

## 2. What was missing / broken

1. **No wait primitive.** Agents slept and hoped. A real automation stack must
   poll a documented condition with a hard timeout and report the attempt count.
2. **No tab control.** Multi-tab browsing could open tabs but never activate or
   close one, and nothing stopped a run from closing the last tab.
3. **No window placement.** "Snap the editor to the left half" was impossible:
   the UIA layer could read `bounds` but not set them.
4. **No offline screen understanding.** Provider vision costs money, needs a key
   and ships pixels off the machine — unacceptable as the only path.
5. **No automation ledger.** Nothing recorded which browser/desktop action ran,
   whether it was allowed, or which target it touched.
6. **Schema/registry drift risk.** New tools must keep the single-sourced
   identity (version, schema, checks, tool count) consistent — the pre-existing
   tests assert exact counts.

## 3. What was built

### 3.1 New modules and shape

```
src/pc_agent/perception.py            # offline CV: analyse_image, locate_template,
                                      #   image_crop, read_image_file, region_hint
src/pc_agent/tools/perception_tools.py# vision.screen, vision.locate
src/pc_agent/browser_cdp.py           # + PageSession.wait_for, +activate_tab/close_tab
src/pc_agent/tools/browser_tools.py   # + browser.wait, + browser.tabs
src/pc_agent/desktop_automation.py    # + WindowStateChange, + set_window_state
src/pc_agent/tools/desktop_automation_tools.py  # + desktop.window_state
src/pc_agent/storage/db.py            # schema v13: automation_runs + 2 indexes
src/pc_agent/storage/repositories.py  # + AutomationRunRecord, AutomationRepository
src/pc_agent/cli.py                   # + automation capabilities|runs|stats|screen|locate
```

### 3.2 Offline perception (`perception.py`, Pillow only)

* `analyse_image(bytes, grid=1..12)` → *measured* facts: size, mode, format,
  byte size, mean brightness, contrast, five dominant colours, and a
  `rows × columns` grid of mean brightness and **ink density** (share of edge
  pixels per cell), plus `busy_cells`, `text_likely` and a plain-language
  `region_hint` ("densest cell is bottom-right…").
  *Ink density is resolution-normalised*: each cell is reduced to a fixed
  128 px working width and filtered with a gradient kernel in Pillow's C code,
  so the same page reports a comparable density at 1366×768 and 2560×1440
  (measured 0.14–0.21 for text pages at all four tested resolutions).
* `locate_template(screen, template)` is a **two-stage, verified** search:
  1. a coarse scan samples ≤96 template pixels per offset on a reduced copy of
     the screenshot and nominates the strongest non-neighbouring peaks;
  2. every nomination is re-scored with **all** template pixels at reduced size
     and then refined to the pixel and fully re-scored on the **full-size**
     screenshot.
  Only a position that survives full-pixel verification at or above the
  threshold is reported, so the returned `score` is a real measurement, not a
  sampled guess.
* Hard caps: source ≤ 20 MB, ≤ 40 MP; grid ≤ 12; template ≤ 250 000 px; search
  offsets ≤ 40 000 sampled steps; scales 1.0 / 0.75 / 0.5; threshold 0.90;
  identical-pixel verification tolerance 24/255.
* A **flat template is refused** (`MIN_TEMPLATE_CONTRAST`), because a solid
  colour patch "matches" everywhere and any score would be meaningless.
* `image_crop` / `read_image_file` stay inside the workspace.

### 3.3 Browser: real waiting and tab control

* `PageSession.wait_for(selector | text | url_contains, timeout_seconds ≤ 120,
  poll)` runs one bounded JS probe per poll (`_wait_probe_js`, values JSON-quoted
  so a selector string cannot break out of the script) and returns
  `{matched, selector_seen, text_seen, url_seen, url, title, ready, attempts,
  waited_ms, target_id}`.
* `browser.wait` exposes it as a tool; a timeout is a tool **error** with the
  observed URL, so the planner can recover instead of silently continuing.
* `browser.tabs` lists page targets, activates one by exact id, or closes one —
  and `BrowserClient.close_tab` **refuses to close the last page tab**.
* The handler normalises backend replies (`_tab_result`), so a stubbed or older
  backend can never crash the tool with `{**bool}`.

### 3.4 Desktop: window state control

* `set_window_state(selector, state, bounds=None)` supports
  `minimize | maximize | restore | move | resize`.
* `move`/`resize` require `bounds=[left, top, right, bottom]`; each value within
  ±32 000, minimum window size **120×120**, and the rectangle is clamped to the
  virtual screen. The assistant's **own window is refused**, as are protected
  system classes already blocked by the UIA layer.
* `SetWindowPos` is declared with explicit argtypes (`HWND`, `UINT`, `int`, …)
  so 64-bit handles are never truncated.
* Returns `WindowStateChange` (`window_before`, `window_after`, `requested`,
  `state`, `changed`, `note`) — `changed` is computed from the observed
  before/after bounds, not assumed.

### 3.5 Automation ledger (schema v13)

One table, `automation_runs` (`kind`, `tool_id`, `action`, `target`, `status`,
`detail`, `artifact_path`, `duration_ms`, `allowed`, `created_at`) with indexes
on `(kind, created_at)` and `(status, created_at)`.

* `AutomationRepository.record(...)`, `.get(id)`, `.list_runs(kind=, status=,
  limit ≤ 500)`, `.summary()` → `{total, by_kind, refused, last_at}`.
* **No page text, no screenshots and no file contents are stored** — only the
  action, the target label and a short detail string.
* Retention is capped at 5 000 rows; the ledger is written **best-effort**, so a
  failed audit insert can never block a user action.

### 3.6 Wiring and surfaces

| Surface | What it exposes |
| --- | --- |
| CLI | `pc-agent automation capabilities \| runs [--kind --status --limit] \| stats \| screen [--path FILE] [--grid N] \| locate SCREEN TEMPLATE [--threshold]`, all with `--json` |
| HTTP API | `GET /v1/automation/capabilities`, `/runs`, `/stats`; `POST /v1/vision/screen`, `/vision/locate`, `/browser/wait`, `/browser/tabs`, `/desktop/window-state` |
| GUI (Tk) | 13th tab **Automation**: capability line, ledger line, screen path + grid + *Analyse screen*, template + threshold + *Locate template*, selector + timeout + *Wait*, and the recent-run list |
| Web Command Center | same routes, no duplicated logic |
| Diagnostics | `automation` section: browser/desktop/vision capability, ledger summary and the last 10 runs |
| `pc-agent doctor` | new `Automation:` line: `browser=off (no debug browser); desktop=off (unavailable); vision=local; ledger=6 (refused=0)` |
| Release audit | **25th check** `automation_audit`: gates reported, refusals recorded, bounds clamped, flat template refused, no pixel upload |

Every control performs a real action through the same service/tool path and
displays the returned measurement. Nothing is a placeholder.

## 4. Security implications

* **Gates unchanged:** browser and desktop automation still require
  `PC_AGENT_ALLOW_BROWSER` / `PC_AGENT_ALLOW_DESKTOP_AUTOMATION`; provider vision
  still requires `PC_AGENT_ALLOW_VISION` plus a key. Vision tools added here are
  **local-only** and never leave the machine.
* **Refusals are recorded too** (`allowed=false` rows), so a refused action is as
  auditable as a permitted one.
* **Bounded blast radius:** wait ≤ 120 s, window ≥ 120×120 and clamped to the
  virtual screen, own window refused, previous "protected class" checks kept,
  5 000-row ledger, 20 MB / 40 MP image caps.
* **No injection surface added:** the JS probe JSON-quotes every interpolated
  value; window titles are truncated to 200 chars in the ledger.
* **No secrets or content in the ledger** — target labels only.
* **Verified matches only:** a match is reported only after full-pixel
  verification, so a "found" answer is not a sampled guess that could send a
  click to the wrong place.

## 5. Performance implications

Measured in the Linux sandbox (deterministic images, no GPU):

| Operation | Size | Result |
| --- | --- | --- |
| `analyse_image` (grid 4) | 1366×768 | ~70 ms |
| `analyse_image` (grid 4) | 1920×1080 | ~130 ms |
| `analyse_image` (grid 4) | 2560×1440 | ~180 ms |
| `vision.screen` tool end-to-end (file read + analysis) | 1920×1080 | ~136 ms |
| `locate_template` (400×320 template) | 1920×1080 | 0.5–0.9 s |
| `locate_template` (400×320 template) | 2560×1440 | 0.9–1.4 s |
| `locate_template` (260×120 button) | 2560×1440 | ~0.55 s |
| Ledger insert | 1 row | ~1.4 ms |
| Ledger `summary()` | 200 rows | ~0.6 ms |
| `automation capabilities` (probes + counts) | — | ~40 ms |

Costs are bounded by constants (`MAX_SEARCH_STEPS`, `MAX_OFFSETS_PER_AXIS`,
`TEMPLATE_SAMPLES`, `CANDIDATE_LIMIT`), so a 4K screen cannot turn a search into
a minutes-long loop. `browser.wait` is I/O-bound on the page, not on us.

## 6. Tests

`tests/test_transformation_phase11_automation.py` — 23 tests: identity/schema/
registry contracts, dense-vs-flat analysis, oversized and non-image rejection,
exact template location with coordinate mapping, flat-template refusal and
absence reporting, ledger recording from tools, `browser.wait` polling + missing
condition, `browser.tabs` list/activate/close + last-tab refusal, JS probe
quoting, window-state validation (bounds, minimum size, state enum, real
`changed` flag), repository record/list/summary/`get`, no content columns,
service surface, service failure paths, HTTP routes with bearer auth, CLI JSON
and text output, diagnostics + release check + doctor line, and the GUI tab /
controller surface.

Full suite: **754 passed, 1 skipped** (the skip is the real-Tk window, which
needs a display).

## 7. Known limitations (honest)

* **Template matching on repeating content is ambiguous.** A template cropped
  from a large block of body text can match several offsets at ~0.99; the
  reported match is then a genuinely-matching position, but not necessarily the
  one a human would pick. Crop distinctive UI elements (buttons, icons, headers),
  not paragraphs.
* **No OCR.** `text_likely` is an ink-density estimate; the module never claims
  to read words. Provider vision remains the option for reading text.
* **No element-level vision grounding.** `vision.locate` returns coordinates; the
  caller still decides what to click, and clicking stays behind UIA/CDP tools
  with their own gates.
* **`desktop.window_state` is Windows-only.** On Linux/macOS it reports
  `unavailable` with a reason instead of pretending.
* **Browser features need a CDP-enabled browser** started with
  `--remote-debugging-port`; without it the tools fail with the documented hint.
* **Scales are discrete** (1.0 / 0.75 / 0.5), so a template that is genuinely
  0.87 scale of the on-screen element may not be found.
* **macOS/Linux desktop automation remains out of scope** (Windows-first).

## 8. Next tasks (Phase 12 preview)

Visual Workflow Builder: turn these bounded primitives (browser wait/tabs,
window state, vision locate) into user-authored, versioned workflows with
triggers, schedules and import/export — reusing the Mission Center graph rather
than inventing a second executor.
