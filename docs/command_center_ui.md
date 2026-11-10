# Command Center UI (0.82.x)

The 0.82.0 build replaces the Tkinter-first presentation with a React/TypeScript
Command Center served by the local Python API. **No engine code was rewritten for
it**: the UI is a client of the same ~80 `/v1` routes the CLI and the GUI use.

```
browser (or Tauri window)
        │  same origin: http://127.0.0.1:8765
        ├─ GET /                 → built SPA shell (+ token meta, strict CSP)
        ├─ GET /assets/*         → hashed JS/CSS, immutable cache
        ├─ GET /v1/...           → JSON envelope  {ok, data}
        └─ GET /v1/events/stream → NDJSON: stream.ready, events, heartbeats
```

## Why the UI is served by the API (and not by a second server)

* **Same origin** means no CORS, no preflight, no proxy and no token in a URL.
  The token arrives in a `<meta name="pc-agent-token">` tag that the Python
  process injects into the shell per request; the browser sends it as a bearer
  header. A token in a query string would end up in history and logs.
* **One process** to start, stop, harden and firewall. The portable release and
  the update bundles already cover `src/**`, so the built assets ship inside them.
* **Offline-first**: no font, icon, chart or analytics host is contacted. The
  shell contains zero absolute URLs except the SVG namespace inside an inlined
  `data:` favicon.

## Module boundaries (frontend/src)

| Path | Responsibility |
|---|---|
| `lib/api.ts` | The only place `fetch` is called for JSON: envelope unwrapping, `ApiError`, meta-token, abort support |
| `lib/types.ts` | Response types transcribed from real captures of every endpoint |
| `lib/actions.ts` | One typed function per write route (run, approve, stop, ingest, speak, …) |
| `lib/format.ts` | Pure formatters (pct, MB, B/s, relative time, event severity) |
| `store/store.ts` | Resource store with **per-key subscriptions**, explicit poll intervals, one in-flight request per resource, paused polling when hidden, NDJSON stream with reconnect |
| `store/ui.ts` | Persisted preferences: theme, motion, sidebar, widget order (`omni.ui.v1`) |
| `store/activity.ts` | Real in-flight request counters that drive the "thinking"/"speaking" core states |
| `store/router.ts` | Hash router: `/`, `/core`, `/agents`, … (no server rewrite needed) |
| `store/toast.ts` | Result feedback for writes, including the API's own error text |
| `components/` | Shell (sidebar, top bar, palette, command bar, toasts, error boundary), `AICore` canvas, `IntelligenceFeed`, `WidgetGrid`, widgets, `Panel` primitives |
| `views/` | One screen per destination, each code-split |

## Layout map (matches the reference image)

```
┌───────────┬──────────────────────────────────────────────┬──────────────────┐
│ identity  │ top bar: health · clock · search · alerts    │                  │
│ runtime   │──────────────────────────────────────────────│  intelligence    │
│ nav (17)  │            holographic AI CORE               │  activity        │
│           │            + state legend                    │  approvals       │
│           │                                              │  alerts          │
│           │──────────────────────────────────────────────│  insights        │
│           │ widgets: agents · timeline · quick commands  │                  │
│           │          monitor · memory · models · voice   │                  │
├───────────┴──────────────────────────────────────────────┴──────────────────┤
│ command bar: instruction input · mic · stop · voice state · task progress    │
└─────────────────────────────────────────────────────────────────────────────┘
```

## The AI core is not decoration

The canvas draws a Fibonacci-sphere particle shell rotated with a real rotation
matrix, depth-sorted for size and alpha, plus three tilted rings. Its **state is
derived only from signals the engine exposes**, and the legend documents each:

| State | Signal |
|---|---|
| `idle` | `/v1/health` reports no running task or mission |
| `thinking` | a request from this UI is in flight (real counter in `store/activity.ts`) |
| `executing` | `/v1/health` reports a running task or mission |
| `listening` | `/v1/voice/status` reports an open capture session |
| `speaking` | a `POST /v1/voice/speak` call is in flight |
| `error` | the emergency stop is engaged |
| `offline` | the last `/v1/health` call failed |

States the backend cannot report (a model's internal "planning" step, for
example) are **not simulated**. `docs/command_center_ui.md` is the reason the
vocabulary stops where the data stops.

## Design system

* Tokens only — components never hard-code a colour. `src/theme.css` defines
  dark and light as channel triplets so Tailwind can apply alpha.
* Dark: near-black navy `#050b16`, electric cyan `#22d3ee`, restrained violet
  `#a78bfa`. Light: `#e9f0f8` surface, white glass panels, cyan darkened to
  `#056b80` so accent text clears AA on every light surface.
* Glass surfaces, 1 px technical borders, corner marks, mono micro-labels with
  `0.18em` tracking, tabular numerals.
* Both themes are contrast-checked by `frontend/scripts/check_contrast.py`
  (dark: accent 9.47:1, faint 5.76:1; light: accent 5.31:1, faint 4.96:1 on the
  inset well). The script is executed by the Python test suite as well, so a
  palette edit that breaks AA fails CI.

## Interaction contract

| Control | What it actually does |
|---|---|
| Instruction input / Send | `POST /v1/run` — creates a real task; failure shows the API message |
| Command palette (Ctrl/Cmd+K) | Navigate to any screen, or run: emergency stop, release stop, scheduler sweep, perf bench. Unknown text offers "queue as instruction" |
| Mic | `POST /v1/voice/enable` → `capture/start` → `capture/stop`; disabled with the host's own reason when no backend exists |
| Stop | `POST /v1/safety/stop` (the real emergency stop) |
| Approval approve/deny | `POST /v1/approvals/{key}/decide` |
| Mission create | `POST /v1/missions` with one task per line |
| Knowledge ingest / ask / delete | `POST /v1/knowledge/ingest`, `POST /v1/knowledge/ask`, `DELETE /v1/knowledge/documents/{id}` |
| Flow import / tick | `POST /v1/flows/import`, `POST /v1/flows/tick` |
| Quick commands | `schedules/run-due`, `flows/tick`, `proactive/tick` — each reports the engine's answer |
| Widget reorder | Native drag-and-drop **and** the ‹ › buttons; order persisted |
| Widget width | ⤢ toggles half/full span (measured 325px → 660px); persisted |
| Widget visibility | ✕ hides, the **Widgets (n/9)** menu restores; a hidden widget keeps its slot |
| Reset layout | Restores default order, spans and visibility; also offered when every widget is hidden |
| Arrange mode | **Arrange** pins every widget control open at a **32 px** touch target (`aria-pressed`), **Done** leaves it; remembered across reloads |
| Theme / motion / sidebar | Persisted in `localStorage`; motion `system` follows the OS |
| Ctrl+B, Esc, Enter | Sidebar toggle, close overlays, send instruction |

Unavailable capabilities are rendered as unavailable, with the reason the
backend gave: calendar connector off, plugin sandbox closed, no speech engine,
no GPU, no provider key. There is no placeholder button anywhere in the build.

## Performance

Measured with Chromium against the real API on the Linux sandbox (see
`PERFORMANCE_REPORT.md` for the table):

* 323 ms first contentful paint, 838 DOM nodes, 78 KB transferred for the
  dashboard (this harness run; the numbers move by ±20% between runs).
* The initial chunks are React 134 kB, motion 122 kB and the view chunk, all
  gzipped to ~112 kB; the other 16 views are separate chunks of 3–7 kB.
* Polling is per-resource and stops when the tab is hidden; the event stream is
  server-bounded (5–900 s) and reconnects with backoff.
* The core canvas pauses on `visibilitychange` and when scrolled out of view,
  caps device pixel ratio at 2, and draws a reduced particle set under reduced
  motion.

## Desktop shell (Tauri 2)

`frontend/src-tauri/` contains a thin shell: the window points at
`http://127.0.0.1:8765`, so the desktop app is byte-for-byte the UI the API
serves, with the same CSP and token. It adds a native window, tray-less
single-instance focus handling and, if nothing is listening, starts
`python -m pc_agent api serve` before showing the window.

**Status: scaffolded, not built here.** This sandbox has no Rust toolchain and no
WebView2, so the shell is not claimed as verified. Building it needs
`npm i -D @tauri-apps/cli@^2`, icons from `npx tauri icon`, and a Windows machine
with the Rust toolchain; see `INSTALLATION_GUIDE.md` §12. Until then the shipped,
tested path is `pc-agent api serve` + a browser at `http://127.0.0.1:8765`.

## Known limitations

* Light mode is a first-class theme but the neon bloom is intentionally weaker
  there; a light "high-glow" variant is not offered.
* The UI is English. The engine's four locales (`en/es/ur/ar`) are switched from
  Settings for engine output, but the React shell does not machine-translate
  itself — RTL mirroring of the shell is not implemented in 0.82.0.
* Widget controls live in a hover/focus toolbar. **Arrange mode** (one tap) pins
  them open at a 32 px target for touch and keyboard use; there is no drag
  reordering *inside* arrange mode that differs from the normal drag behaviour.
* Widgets are chosen from a fixed set of nine; the grid is not user-extensible
  (adding a tenth panel needs a code change in `frontend/src/widgets`).
* The workflow canvas is not embedded; flows are inspected and imported as JSON.
* Mobile/tablet layouts work down to 1024 px; below that the dashboard stacks and
  is not an optimised target.

## Entry points and rollback (0.82.2)

| Command | What happens |
|---|---|
| `pc-agent ui [--browser] [--no-window] [--explain] [--duration N]` | starts `LocalApiServer` on loopback and opens the UI as a desktop window |
| `pc-agent gui` | the same Command Center window (default since 0.82.2) |
| `pc-agent gui --tk` | the Tkinter panel from Phase 5, unchanged |
| `pc-agent gui --check` | readiness check, no window |
| `pc-agent api serve` | API only |
| `GET /legacy` | pre-0.82 web panel |

The launcher (`cli._open_command_center`) never puts the token in a URL: the
server injects it into the page's `meta` tag, exactly as it does for a browser
tab. `--duration` exists so a bounded run can be scripted/tested; the default is
"until Ctrl+C".

## Right-to-left support

`omni.ui.v1.direction` is `auto` (default), `ltr` or `rtl`.

* `auto` resolves from the engine's locale report (`/v1/a11y/status` →
  `direction`), fetched once on mount so it works on every screen; the browser
  locale is never used as a guess.
* An explicit value is applied by the pre-paint boot script in `index.html`, so
  the mirrored layout is the *first* frame.
* The layout uses logical utilities only (`ms/me`, `ps/pe`, `border-s/e`,
  `text-start/end`, `start/end`). `tests/.../test_ui_sources_use_logical_direction_utilities`
  fails the build if a physical one comes back.
* Technical values (`.font-mono`, `code`, `kbd`, inputs) are `unicode-bidi:
  isolate`, so ids, ports and byte counts keep their own direction.

Known limitation, unchanged in shape: the React shell's copy is English. RTL is a
layout direction, not a translation - the engine's locale switch governs engine
output (CLI/reports), not this UI's strings. Wall-clock 12-hour formatting and
Arabic-Indic digits are also not localised.

## Desktop shell (Tauri 2) - status in 0.82.2

Scaffolded in the tree and now complete on the asset side:

* `scripts/make_app_icons.py` generates `icons/32x32.png`, `128x128.png`,
  `128x128@2x.png`, `icon.png`, multi-size `icon.ico` and `icon.icns`
  (reproducible from source; Pillow is a build-machine tool only);
* `tauri.conf.json` version tracks `pc_agent.__version__`, loads
  `http://127.0.0.1:8765/` (same origin as the API - no CORS, no token plumbing),
  minimum window 1024x700;
* capabilities stay `core:default`; no shell/fs/http plugin is granted;
* single-instance plugin focuses the existing window.

**Still not built or verified here**: this machine has no Rust toolchain and no
WebView2, so `npm run tauri:build` has never run. The browser path (`pc-agent ui`)
is the shipped, tested entry point.

## Desktop window vs browser tab (0.82.3)

`desktop_window.py` decides how the UI is presented, in this order:

1. **pywebview** (optional `desktop` extra) - OS webview: WebView2 on Windows,
   WebKitGTK on Linux, WKWebView on macOS. `webview.start()` owns the main thread,
   the API runs on a worker thread.
2. **Chromium-family app window** - `--app=<loopback url>
   --window-size=1560,980 --user-data-dir=<data>/ui-window --no-first-run`. The
   dedicated profile makes it a separate instance, so it neither inherits the
   user's extensions nor closes their tabs when it exits.
3. **Default browser** - the fallback that cannot fail.

Guards: the URL must be loopback (`ConfigError` otherwise), the launch is an argv
list with no shell, and the process is owned by the launcher so stopping the
server closes the window. `--explain` prints the plan without binding a port.

This is deliberately *not* a second copy of the UI: the window loads the same
loopback origin as a tab, so the token, CSP, approvals and API remain one system.
The Tauri shell (`frontend/src-tauri/`) is the installer-grade fourth option and
is still unbuilt here (no Rust toolchain/WebView2 on this machine).
