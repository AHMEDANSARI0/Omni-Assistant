# Transformation Phase 5 — JARVIS Command Center UI with real live state

## 1. Existing findings

- The Tkinter GUI was a capable operations client (Dashboard/Tasks/
  Documents/Images/Web/Desktop/Tools/Providers) but it predated the
  Safety Kernel surfaces (Phase 2), the Local Service/API (Phase 3),
  and the Mission Center (Phase 4): there was no place to *watch*
  missions/events/approvals/safety together or to act on them, and no
  modern command-center surface at all. The audit rule for this phase:
  no fake controls and no simulated metrics — every visible number and
  button must map to real application state.

## 2. What changed

### Web Command Center (primary new surface)
- `src/pc_agent/web_ui.py` holds one self-contained page (~20 KB, no
  external assets, no frameworks) served by the local API at `GET /`
  and `/index.html`. The page is unauthenticated HTML with security
  headers (CSP `default-src 'self'`, inline script/style only,
  `frame-ancestors 'none'`, nosniff); all *data* comes from the
  token-authenticated `/v1` API. The pasted token lives only in
  `sessionStorage` and travels in the `Authorization` header.
- Panels refresh every 2.5 s from real endpoints: health pills
  (version/schema/emergency stop/running counts), overview cards,
  quick-run (`POST /v1/run`), task table with Run/Cancel/Delete,
  Mission Center (create with optional chaining, Run/Cancel, and a
  level-by-level execution-graph rendering colored by live task
  status), Safety (status + engage/clear emergency stop with reason/
  confirmation), Approval Center pending list with Approve/Deny
  (same exact-scope consume-once semantics), Schedules (+ run-due),
  a cursor-based live event tail (`/v1/events?since_id=…`), providers,
  tools, and the public settings view. All dynamic text is HTML-escaped.
- `pc-agent api serve` prints the Command Center URL next to the API
  line. The API itself is unchanged behaviorally besides serving the
  page; loopback-only binding and token rules from Phase 3 stand.

### Tkinter Command Center tab (desktop parity)
- New second tab "Command Center": live overview (auto-refresh 4 s via
  `GuiController.command_center_overview()`), Mission Center tree with
  per-mission progress and a detail/execution-graph text pane,
  Run/Cancel mission actions through the existing `_run_async` worker
  pattern, Safety panel (emergency stop engage/clear with
  confirmations, events recorded), Approval Center pending list with
  Approve/Deny, and a live events tail fed by an `app_events` cursor.
- Controller additions (`command_center_overview`, `list_mission_rows`,
  `mission_detail`, `run_mission`, `cancel_mission`,
  `pending_approval_center_requests`, `decide_approval_center`,
  `set_emergency_stop`, `events_since`) are transport-independent and
  unit-tested without a window. Mission runs from the GUI use their
  own orchestrators via `MissionRunner`; inline approvals deny (the
  request lands in the Approval Center list on the same tab) — the
  pre-existing in-band broker behavior for single Tasks-tab runs is
  unchanged.

### Verification notes
- The sandbox previously skipped the real Tk-window test (headless).
  Phase 5 installed Xvfb in the sandbox and instantiated the full
  `PcAgentGuiApp`: all nine tabs build, `_cc_refresh()` populates real
  (empty-project) state, and the window destroys cleanly. The shipped
  test-suite keeps its normal skip when no display exists; Windows
  rendering remains part of Windows acceptance.

## 3. What was intentionally reused

- Every API endpoint from Phases 3–4 (the web UI adds no backend
  behavior), the GUI controller/async worker/approval-broker
  architecture, and the MissionRunner/Safety Kernel execution path.

## 4. Deferred

- Live CPU/GPU/RAM/process/network widgets (Phase 6 will feed real
  sensors into both UIs — deliberately absent rather than faked).
- Voice controls (Phase 10), workflow visual builder (Phase 12),
  theming/localization polish (Phase 17).
- The web UI is a **local** control surface: no TLS, no remote access,
  no multi-user model (Phase 16 owns remote coordination design).
