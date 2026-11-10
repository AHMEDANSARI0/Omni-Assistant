# Test Report

## Phase 0 audit evidence (GitHub Phase 58)

Audited commit: `89610f3d18f9348b6fe8ead679bab81ee34155df`.

- Ruff: clean.
- Full pytest: **603 passed, 1 skipped** in 85.83 s.
- Coverage rerun: **603 passed, 1 skipped** in 118.75 s; line coverage
  76.8% (27,497 of 35,797 statements).
- `doctor`: pass.
- `gui --check`: ready without opening a real window.
- `release audit`: READY, 0 failures, 1 mock-provider warning, 15 checks.
- Deterministic Folder A/B PDF merge-then-extract acceptance: pass.
- The one skip was the real Tk window test in a headless Linux sandbox.

## Transformation Phase 1 additional evidence

- Non-shallow GitHub history secret-pattern scan: 9 commits and 533
  reachable blobs. No high-confidence AWS, GitHub, OpenAI-style, Google or
  Slack token patterns were found. The only private-key-header match is a
  source-code rejection check in `update_feed_hosting.py`.
- Native Windows acceptance: **pending**. Linux results must not be used as
  Windows GUI, UI Automation, Credential Manager, installer, Task Scheduler
  or live-feed proof.

## Transformation Phase 1 Linux verification

Staging tree (version 0.63.0, Python 3.13, Linux sandbox):

- Full pytest: **621 passed, 1 skipped** in 84.59 s. The one skip is the
  real Tk window test in the headless sandbox.
- Ruff: clean.
- `pc-agent --version`: `pc-agent 0.63.0`.
- `pc-agent doctor`: pass; reports the Transformation Phase 1 baseline
  line with `documents_missing=none`, `env_example_ignored=no`,
  `direct_cryptography=yes` and
  `windows_acceptance=pending_user_windows_evidence`.
- `pc-agent gui --check`: READY without opening a window; 62 tools.
- `pc-agent release audit`: READY, 0 failures, 1 expected mock-only
  warning, 16 checks including `transformation_baseline` PASS;
  270 allow-listed bundle files.

Clean-room patcher verification for
`TRANSFORMATION_PHASE_01_CANONICAL_BASELINE.mjs` (216 embedded files,
cumulative Phase 9 -> 0.63.0 delta; two consecutive builder runs produced
a byte-identical patcher):

- Phase 9 (0.11.0) base tree: patcher applied; all 269 allow-listed
  files byte-identical to staging; full pytest **621 passed, 1 skipped**;
  Ruff clean; `gui --check` READY; release audit READY (16 checks).
- Delivered Phase 60 (0.62.0) base tree: patcher reported
  created=10, updated=62, skipped=144; all 269 allow-listed files
  byte-identical to staging; full pytest **621 passed, 1 skipped**;
  Ruff clean; doctor baseline line present; `gui --check` READY;
  release audit READY (16 checks).
- Audited GitHub Phase 58 (0.60.0, commit
  `89610f3d18f9348b6fe8ead679bab81ee34155df`) base tree: patcher reported
  created=14, updated=64, skipped=138; all 269 allow-listed files
  byte-identical to staging; full pytest **621 passed, 1 skipped**;
  Ruff clean; doctor baseline line present; `gui --check` READY;
  release audit READY (16 checks).

No Windows count is claimed here; native Windows acceptance remains
pending real evidence (see `docs/windows_acceptance.md`).

## Transformation Phase 2 Linux verification

Staging tree (version 0.64.0, Python 3.13, Linux sandbox):

- Full pytest: **636 passed, 1 skipped** in 125.79 s (Phase 1's 621 plus
  15 new Safety Kernel tests). The one skip is the real Tk window test
  in the headless sandbox.
- Ruff: clean.
- `pc-agent --version`: `pc-agent 0.64.0`; database schema **v7**.
- New Phase 2 evidence inside the suite: hard thread timeout abandons a
  slow in-process tool; the isolated worker executes `pdf.verify` and is
  **killed** while running a deliberately hung `commands.run`, then
  respawns healthy; the emergency stop cancels orchestrated execution
  and clearing it restores it; a non-interactive run records a pending
  Approval Center request, an out-of-band approve + resume completes
  the task with a digest-only approval record, and deny cancels it; an
  encrypted backup round-trips (wrong/missing passphrase refused) and
  restore rolls the live task count back.
- `pc-agent doctor`: pass; prints the Safety Kernel line
  (`hard_timeouts=on; worker_isolation=on; max_tool_seconds=600;
  emergency_stop=off; pending_approvals=0`).
- `pc-agent gui --check`: READY without opening a window; 62 tools.
- `pc-agent release audit`: READY, 0 failures, 1 expected mock-only
  warning, **17 checks** including `safety_kernel` PASS;
  274 allow-listed bundle files.
- Deterministic Folder A/B merge-then-extract through the isolated
  worker: succeeded (2 steps, verified).

Clean-room patcher verification for
`TRANSFORMATION_PHASE_02_SAFETY_KERNEL.mjs` (223 embedded files,
cumulative Phase 9 -> 0.64.0 delta; two consecutive builder runs
produced a byte-identical patcher):

- Phase 9 (0.11.0) base tree: patcher reported created=176, updated=47,
  skipped=0; all 273 allow-listed files byte-identical to staging;
  full pytest **636 passed, 1 skipped**; Ruff clean; doctor Safety
  Kernel line present; `safety status` on; `gui --check` READY;
  release audit READY (17 checks).
- Transformation Phase 1 (0.63.0) base tree: patcher reported
  created=4, updated=79, skipped=140; all 273 allow-listed files
  byte-identical to staging; full pytest **636 passed, 1 skipped**;
  Ruff clean; doctor Safety Kernel line present; `safety status` on;
  `gui --check` READY; release audit READY (17 checks).
- Audited GitHub Phase 58 (0.60.0, commit
  `89610f3d18f9348b6fe8ead679bab81ee34155df`) base tree: patcher reported
  created=18, updated=74, skipped=131; all 273 allow-listed files
  byte-identical to staging; full pytest **636 passed, 1 skipped**;
  Ruff clean; doctor Safety Kernel line present; `safety status` on;
  `gui --check` READY; release audit READY (17 checks).

No Windows count is claimed here; native Windows acceptance (including
the isolated worker under the real Windows venv and the UIA thread
path) remains pending real evidence (see `docs/windows_acceptance.md`).

## Transformation Phase 3 Linux verification

Staging tree (version 0.65.0, Python 3.13, Linux sandbox):

- Full pytest: **647 passed, 1 skipped** in 104.53 s (Phase 2's 636 plus
  11 new Local Service tests). The one skip remains the real Tk window
  test in the headless sandbox.
- Ruff: clean.
- `pc-agent --version`: `pc-agent 0.65.0`; database schema **v8**.
- New Phase 3 evidence inside the suite: event repository records,
  redacts secrets from summaries/payloads, lists by cursor and newest
  first; the Local Service runs a task end-to-end on its background
  thread and records `task.created`/`run_started`/`run_finished`; the
  HTTP API returns 200 for `/v1/health`, 401 without/with a wrong token,
  201/202 for task create/run, and drives safety stop/resume and profile
  save/activate; profile precedence (environment wins, invalid or
  gate-carrying profiles rejected) and loopback-only host validation;
  structured JSON logging writes redacted JSONL to stream and file;
  diagnostics/release/GUI surfaces expose the service section.
- `pc-agent doctor`: pass; prints the Local service line
  (`api=on http://127.0.0.1:8765 (loopback); log_format=text;
  profile=none; events=0`).
- `pc-agent api status`: health + overview in-process (Tasks 0, Tools
  62, schema v8).
- `pc-agent gui --check`: READY without opening a window; 62 tools.
- `pc-agent release audit`: READY, 0 failures, 1 expected mock-only
  warning, **18 checks** including `local_service` PASS;
  280 allow-listed bundle files.

Clean-room patcher verification for
`TRANSFORMATION_PHASE_03_LOCAL_SERVICE.mjs` (230 embedded files,
cumulative Phase 9 -> 0.65.0 delta; two consecutive builder runs
produced a byte-identical patcher):

- Phase 9 (0.11.0) base tree: patcher reported created=182, updated=48,
  skipped=0; all 280 allow-listed files byte-identical to staging;
  full pytest **647 passed, 1 skipped**; Ruff clean; doctor Local
  service line present; `api status` OK; `gui --check` READY;
  release audit READY (18 checks).
- Transformation Phase 2 (0.64.0) base tree: patcher reported
  created=6, updated=78, skipped=146; all 280 allow-listed files
  byte-identical to staging; full pytest **647 passed, 1 skipped**;
  Ruff clean; doctor Local service line present; `api status` OK;
  `gui --check` READY; release audit READY (18 checks).
- Audited GitHub Phase 58 (0.60.0, commit
  `89610f3d18f9348b6fe8ead679bab81ee34155df`, re-cloned for this phase)
  base tree: patcher reported created=24, updated=75, skipped=131;
  all 280 allow-listed files byte-identical to staging; full pytest
  **647 passed, 1 skipped**; Ruff clean; doctor Local service line
  present; `api status` OK; `gui --check` READY; release audit READY
  (18 checks).

No Windows count is claimed here; native Windows acceptance (including
the API server and service worker under the real Windows venv) remains
pending real evidence (see `docs/windows_acceptance.md`).

## Transformation Phase 4 Linux verification

Staging tree (version 0.66.0, Python 3.13, Linux sandbox):

- Full pytest: **657 passed, 1 skipped** in 106.21 s (Phase 3's 647 plus
  10 new Mission Center tests).
- Ruff: clean.
- `pc-agent --version`: `pc-agent 0.66.0`; database schema **v9**.
- New Phase 4 evidence inside the suite: topological levels + cycle
  rejection (on link and before a run); a 5-task mission runs two PDF
  merges in parallel, a joiner after both, an unsupported branch ends
  needs_input and its dependent stays queued/blocked (mission failed);
  an all-green mission ends succeeded and deleting it keeps its tasks;
  once/interval schedules fire through `run_due_schedules` with correct
  advance/disable semantics and `schedule.triggered` events; the HTTP
  API creates/runs a mission, serves the graph, and triggers due
  schedules; CLI missions/schedules surfaces + doctor line.
- `pc-agent doctor`: pass; prints `Mission center: missions=0;
  max_parallel=3; schedules=0 (enabled=0); scheduler_interval=30s`.
- `pc-agent gui --check`: READY without opening a window; 62 tools.
- `pc-agent release audit`: READY, 0 failures, 1 expected mock-only
  warning, **19 checks** including `mission_center` PASS;
  283 allow-listed bundle files.

Clean-room patcher verification for
`TRANSFORMATION_PHASE_04_MISSION_CENTER.mjs` (233 embedded files,
cumulative Phase 9 -> 0.66.0 delta; two consecutive builder runs
produced a byte-identical patcher):

- Phase 9 (0.11.0) base tree: patcher reported created=185, updated=48,
  skipped=0; all 283 allow-listed files byte-identical to staging;
  full pytest **657 passed, 1 skipped**; Ruff clean; doctor Mission
  center line present; `gui --check` READY; release audit READY
  (19 checks).
- Transformation Phase 3 (0.65.0) base tree: patcher reported
  created=3, updated=82, skipped=148; all 283 allow-listed files
  byte-identical to staging; full pytest **657 passed, 1 skipped**;
  Ruff clean; doctor Mission center line present; `gui --check` READY;
  release audit READY (19 checks).
- Audited GitHub Phase 58 (0.60.0, commit
  `89610f3d18f9348b6fe8ead679bab81ee34155df`) base tree: patcher
  reported created=27, updated=75, skipped=131; all 283 allow-listed
  files byte-identical to staging; full pytest **657 passed,
  1 skipped**; Ruff clean; doctor Mission center line present;
  `gui --check` READY; release audit READY (19 checks).
  (A first attempt in this room reported spurious failures because
  the sandbox `/tmp` filled to 100%; after cleaning test residue the
  identical room passed 657/1 — recorded here for honesty.)

No Windows count is claimed here; native Windows acceptance (including
parallel mission workers and the scheduler thread under the real
Windows venv) remains pending real evidence (see
`docs/windows_acceptance.md`).

## Transformation Phase 5 Linux verification

Staging tree (version 0.67.0, Python 3.13, Linux sandbox):

- Full pytest: **664 passed, 1 skipped** in 107.26 s (Phase 4's 657 plus
  7 new Command Center tests). The skip is the real Tk window test in
  the headless default shell; with Xvfb installed for this phase the
  same test **passes** (9 tabs incl. Command Center) — see below.
- Ruff: clean.
- `pc-agent --version`: `pc-agent 0.67.0`; database schema **v9**
  (unchanged; Phase 5 is a UI phase).
- New Phase 5 evidence: the web UI module is fully self-contained
  (no external URLs), references only real `/v1` endpoints, and is
  served with CSP/anti-framing headers while `/v1/overview` still
  returns 401 without a token; the GUI controller's Command Center
  methods run an end-to-end mission (succeeded), cancel a draft
  mission, stream events by cursor, and engage/clear the emergency
  stop; under `xvfb-run`, `tests/test_phase5_gui.py` +
  `tests/test_transformation_phase5_command_center.py` = **19 passed**
  including the real-window construction test.
- `pc-agent gui --check`: READY without opening a window; 62 tools.
- `pc-agent release audit`: READY, 0 failures, 1 expected mock-only
  warning, **19 checks**; 286 allow-listed bundle files.

Clean-room patcher verification for
`TRANSFORMATION_PHASE_05_COMMAND_CENTER.mjs` (236 embedded files,
cumulative Phase 9 -> 0.67.0 delta; two consecutive builder runs
produced a byte-identical patcher):

- Phase 9 (0.11.0) base tree: patcher reported created=188, updated=48,
  skipped=0; all 286 allow-listed files byte-identical to staging;
  full pytest **664 passed, 1 skipped**; Ruff clean; `gui --check`
  READY; release audit READY (19 checks).
- Transformation Phase 4 (0.66.0) base tree: patcher reported
  created=3, updated=72, skipped=161; all 286 allow-listed files
  byte-identical to staging; full pytest **664 passed, 1 skipped**;
  Ruff clean; `gui --check` READY; release audit READY (19 checks).
- Audited GitHub Phase 58 (0.60.0, commit
  `89610f3d18f9348b6fe8ead679bab81ee34155df`) base tree: patcher
  reported created=30, updated=75, skipped=131; all 286 allow-listed
  files byte-identical to staging; full pytest **664 passed,
  1 skipped**; Ruff clean; `gui --check` READY; release audit READY
  (19 checks).

No Windows count is claimed here; native Windows acceptance (including
the Command Center rendering in a real Windows browser and the Tkinter
window on Windows) remains pending real evidence (see
`docs/windows_acceptance.md`).

## Transformation Phase 6 Linux verification

Staging tree (version 0.68.0, Python 3.13, Linux sandbox):

- Full pytest: **671 passed, 1 skipped** in 115.55 s (Phase 5's 664 plus
  7 new live-monitoring tests; the skip is the real Tk window test in
  the headless default shell).
- Ruff: clean.
- `pc-agent --version`: `pc-agent 0.68.0`; database schema **v9**
  (unchanged; telemetry is live state, never persisted).
- New Phase 6 evidence: pure parsers for `/proc/stat`, `/proc/meminfo`,
  `/proc/net/dev`, and `nvidia-smi` output are unit-tested; a live
  two-sample snapshot on the sandbox proves CPU % is `null` on the
  first (baseline) sample and bounded 0–100 afterwards, memory/disk/
  RSS/network values are real, and the `/proc` process scan works.
  `GET /v1/system` returns `{snapshot, history}` behind the bearer
  token; `pc-agent system status` measured on the sandbox: CPU 2.0%,
  memory 745.4 MB of 1.9 GB (37.6%), agent RSS 59.3 MB, GPU honestly
  `unavailable` (no `nvidia-smi` in the sandbox). Snapshot cost:
  median 2.6 ms with the process scan, 0.2 ms without; JSON payload
  1,678 bytes. Doctor prints a `System:` line; diagnostics gained a
  `system` section; release audit now runs **20 checks**.
- `pc-agent gui --check`: READY without opening a window; 62 tools.
- Windows sensor codepaths (`GetSystemTimes`, `GlobalMemoryStatusEx`,
  `EnumProcesses`, `GetIfTable`, `GetSystemPowerStatus`) are
  implemented but **UNVERIFIED** until the Windows acceptance run.

Clean-room patcher verification for
`TRANSFORMATION_PHASE_06_LIVE_MONITORING.mjs` (239 embedded files,
cumulative Phase 9 -> 0.68.0 delta; two consecutive builder runs
produced a byte-identical patcher):

- Phase 9 (0.11.0) base tree: patcher reported created=191, updated=48, skipped=0;
  all 289 allow-listed files byte-identical to staging; full pytest
  **671 passed, 1 skipped**; Ruff clean; `gui --check` READY; release
  audit READY (20 checks).
- Transformation Phase 5 (0.67.0) base tree: patcher reported
  created=3, updated=78, skipped=158; all 289 allow-listed files
  byte-identical to staging; full pytest **671 passed, 1 skipped**;
  Ruff clean; `gui --check` READY; release audit READY (20 checks).
- Audited GitHub Phase 58 (0.60.0, commit
  `89610f3d18f9348b6fe8ead679bab81ee34155df`) base tree: patcher
  reported created=33, updated=75, skipped=131; all 289 allow-listed
  files byte-identical to staging; full pytest **671 passed,
  1 skipped**; Ruff clean; `gui --check` READY; release audit READY
  (20 checks).

No Windows count is claimed here; native Windows acceptance (including
the Windows sensor codepaths, Command Center rendering, and the Tkinter
window) remains pending real evidence (see `docs/windows_acceptance.md`).

## Transformation Phase 7 Linux verification

Staging tree (version 0.69.0, Python 3.13, Linux sandbox):

- Full pytest: **683 passed, 1 skipped** in 149.50 s (Phase 6's 671 plus
  12 new Provider Platform tests; the skip is the real Tk window test
  in the headless default shell).
- Ruff: clean.
- `pc-agent --version`: `pc-agent 0.69.0`; database schema **v9**
  (unchanged; analytics live in a local JSONL file, not the database).
- New Phase 7 evidence: SSE/NDJSON line parsers (Chat Completions,
  Ollama, Anthropic) are unit-tested with injected lines; the mock
  streams through the router and streaming falls back only before the
  first delta (a mid-stream failure raises after partial output);
  token usage normalises provider-reported keys and labels estimates;
  strategy ordering proves local-first and cheapest-by-user-prices
  with fail-closed settings parsing; the 4-case evaluation suite
  passes 4/4 on the mock route and its calls are metered; the usage
  file contains no prompt text and stops growing when analytics are
  disabled. Live sandbox smokes: `providers capabilities|route|usage`,
  `ask --stream`, `providers evaluate` (4/4), API stream NDJSON with
  401 for missing token. Mock overhead measured: first-delta median
  0.01 ms, full eval suite 0.5 ms (local wiring only — live model
  latency/cost remain **UNVERIFIED** until a real key exercises them;
  Gemini streaming is not implemented and reports `streaming: false`).
- `pc-agent gui --check`: READY without opening a window; 62 tools.
- `pc-agent release audit`: READY, 0 failures, 1 expected mock-only
  warning, **21 checks**; 292 allow-listed bundle files.

Clean-room patcher verification for
`TRANSFORMATION_PHASE_07_PROVIDER_PLATFORM.mjs` (242 embedded files,
cumulative Phase 9 -> 0.69.0 delta; two consecutive builder runs
produced a byte-identical patcher):

- Phase 9 (0.11.0) base tree: patcher reported created=194, updated=48, skipped=0;
  all 292 allow-listed files byte-identical to staging; full pytest
  **683 passed, 1 skipped**; Ruff clean; `gui --check` READY; release
  audit READY (21 checks).
- Transformation Phase 6 (0.68.0) base tree: patcher reported
  created=3, updated=87, skipped=152; all 292 allow-listed files
  byte-identical to staging; full pytest **683 passed, 1 skipped**;
  Ruff clean; `gui --check` READY; release audit READY (21 checks).
- Audited GitHub Phase 58 (0.60.0, commit
  `89610f3d18f9348b6fe8ead679bab81ee34155df`) base tree: patcher
  reported created=36, updated=81, skipped=125; all 292 allow-listed
  files byte-identical to staging; full pytest **683 passed,
  1 skipped**; Ruff clean; `gui --check` READY; release audit READY
  (21 checks).

No Windows count is claimed here; native Windows acceptance and
live-provider streaming/latency/cost evidence remain pending real
evidence (see `docs/windows_acceptance.md`).

## Transformation Phase 8 (0.70.0) - Memory v2, knowledge base, embeddings, RAG

Sandbox verification (Linux, Python 3.13, mock provider; native Windows
acceptance is not implied by anything below):

- Full pytest suite: **698 passed, 1 skipped** (683 at 0.69.0 + 15 new
  Phase 8 tests; the skip is the headless real-Tk-window test). Ruff
  clean across `src/` and tests.
- Phase 8 tests (`tests/test_transformation_phase8_memory_knowledge.py`)
  cover: identity/schema v10; hashing-embedder determinism, packing,
  stemming and paraphrase overlap; config validation; chunk bounds;
  markdown ingest with SHA-256 dedupe, hybrid ranking, list/remove;
  text-bearing PDF ingest via a pypdf fixture and the explicit
  no-silent-OCR error for a text-less PDF; Memory v2 backfill,
  paraphrase search, edit-invalidation and re-embed; grounded `ask`
  via the mock route with sources, and the no-source path proving by
  a counting router that **no provider call happens**; labelled
  keyword fallback when the embedder raises `ProviderError`; tool
  registration (64) and worker exclusion; CLI knowledge/memory flows
  incl. the doctor `Knowledge:` line; the authenticated API flow
  (ingest text + workspace-containment rejection, search, ask, stats,
  memory search, delete, 401 without token); diagnostics `knowledge`
  section, release audit check 22 (`knowledge_base`), GUI controller
  knowledge methods, and web-panel markers.
- `pc-agent gui --check`: READY without opening a window; 64 tools.
- `pc-agent release audit`: READY, 0 failures, 1 expected mock-only
  warning, **22 checks**; 297 allow-listed bundle files.
- Loopback embedding cost: hashing embed 0.107 ms per ~900-char
  chunk; hybrid search over 144 chunks 22.5 ms median (linear in
  chunk count; details in PERFORMANCE_REPORT).

Clean-room patcher verification for
`TRANSFORMATION_PHASE_08_MEMORY_KNOWLEDGE.mjs` (248
embedded files, cumulative Phase 9 -> 0.70.0 delta; two consecutive
builder runs produced a byte-identical patcher):

- Phase 9 (0.11.0) base tree: patcher reported created=199, updated=49, skipped=0;
  all 297 allow-listed files byte-identical to staging;
  full pytest **698 passed, 1 skipped**; Ruff clean; `gui --check`
  READY; release audit READY (22 checks).
- Transformation Phase 7 (0.69.0) base tree: patcher reported
  created=5, updated=95, skipped=148; all 297 allow-listed files
  byte-identical to staging; full pytest **698 passed, 1 skipped**;
  Ruff clean; `gui --check` READY; release audit READY (22 checks).
- Audited GitHub Phase 58 (0.60.0, commit
  `89610f3d18f9348b6fe8ead679bab81ee34155df`) base tree: patcher
  reported created=41, updated=85, skipped=122; all 297 allow-listed files
  byte-identical to staging; full pytest **698 passed, 1 skipped**;
  Ruff clean; `gui --check` READY; release audit READY (22 checks).

No Windows count is claimed here; native Windows acceptance and
live-provider embedding evidence remain pending real evidence (see
`docs/windows_acceptance.md`).

## Transformation Phase 9 (0.71.0) - Specialized Multi-Agent Runtime & Agent Studio

Sandbox verification (Linux, Python 3.13, mock provider; native Windows
acceptance is not implied by anything below):

- Full pytest suite: **711 passed, 1 skipped** (698 at 0.70.0 + 13 new
  Phase 9 tests; the skip is the headless real-Tk-window test). Ruff
  clean across `src/` and tests.
- Phase 9 tests (`tests/test_transformation_phase9_agents.py`) cover:
  identity/schema v11; builtin seeding idempotence (user edits never
  overwritten); filtered-registry exactness (`*` = 64, subsets, empty);
  custom-agent CRUD with name/tool/step validation and builtin
  delete-protection; a solo document-worker merge succeeding through
  its filtered registry; least privilege proven by a read-only
  reviewer's merge attempt ending `needs_input` with **no merged file
  created**; AI-planner persona injection, catalog narrowing, and
  pre-execution rejection of an out-of-restriction plan (capturing
  stub router); explicit team DAG success plus blocked-dependent
  semantics (no task created); assignment validation errors (unknown
  agent, coordinator-as-worker, forward dependency, >6);
  coordinator-proposed teams via validated JSON plus invalid-JSON and
  rogue-agent rejections; CLI agents flow incl. the doctor `Agents:`
  line; the authenticated API flow (create, builtin-delete rejection,
  202 solo run polled to success, explicit team polled to result,
  401); diagnostics `agents` section, release audit check 23
  (`agent_studio`), GUI controller methods, and web-panel markers.
- `pc-agent gui --check`: READY without opening a window; 64 tools.
- `pc-agent release audit`: READY, 0 failures, 1 expected mock-only
  warning, **24 checks**; 300 allow-listed bundle files.
- Agent overhead measured: filtered registry build 0.63 ms; solo
  trivial run ~253 ms end-to-end; 3-assignment team ~576 ms
  (details in PERFORMANCE_REPORT).

Clean-room patcher verification for
`TRANSFORMATION_PHASE_09_MULTI_AGENT_STUDIO.mjs` (251
embedded files, cumulative Phase 9 -> 0.71.0 delta; two consecutive
builder runs produced a byte-identical patcher):

- Phase 9 (0.11.0) base tree: patcher reported created=202, updated=49, skipped=0;
  all 300 allow-listed files byte-identical to staging;
  full pytest **711 passed, 1 skipped**; Ruff clean; `gui --check`
  READY; release audit READY (24 checks).
- Transformation Phase 8 (0.70.0) base tree: patcher reported
  created=3, updated=90, skipped=158; all 300 allow-listed files
  byte-identical to staging; full pytest **711 passed, 1 skipped**;
  Ruff clean; `gui --check` READY; release audit READY (24 checks).
- Audited GitHub Phase 58 (0.60.0, commit
  `89610f3d18f9348b6fe8ead679bab81ee34155df`) base tree: patcher
  reported created=44, updated=86, skipped=121; all 300 allow-listed files
  byte-identical to staging; full pytest **711 passed, 1 skipped**;
  Ruff clean; `gui --check` READY; release audit READY (24 checks).

No Windows count is claimed here; native Windows acceptance remains
pending real evidence (see `docs/windows_acceptance.md`).

## Transformation Phase 10 (0.72.0) - Voice Pipeline + Windows acceptance fixes

Scope: `src/pc_agent/voice/*` (devices, engines, capture, STT, TTS, privacy,
service), schema migration 12 (`voice_sessions`), CLI `voice` group, 13 HTTP
routes under `/v1/voice`, GUI Voice tab (12th), web Voice panel, diagnostics
`voice` section, release audit check 24 (`voice_privacy`), and two Windows
failures reported by the user on 0.71.0.

### New tests

`tests/test_transformation_phase10_voice.py` - 20 tests:

1. identity (`0.72.0`, schema 12)
2. microphone off by default refuses capture
3. enable persists across service instances
4. wake word needs the microphone plus a second opt-in
5. push-to-talk transcribes and discards audio by default
6. retention keeps audio; purge removes it
7. silent capture reports "no speech" and still writes a session
8. transcripts stored only when enabled
9. cloud STT requires per-call consent
10. TTS never places text in the command line (temp file deleted after use)
11. unavailable TTS engine reports its reason
12. emergency stop blocks capture and forces the mic off
13. only one capture at a time
14. transcript normalisation does not invent words
15. engine selection prefers local and explains unavailable preferences
16. STT discovery is honest about this host
17. `pc-agent voice status --json` and `voice privacy`
18. `/v1/voice/*` round trip over the loopback API (enable, wake-word, sessions, stop-without-capture)
19. diagnostics `voice` section + release `voice_privacy` check

### Windows acceptance fixes covered by existing tests

- `tests/test_transformation_phase2_safety.py::test_worker_hard_timeout_kills_hung_tool_and_recovers`
  now resolves a bare, PATH-resolvable interpreter instead of hard-coding
  `python3` (absent on Windows), so the hard-timeout path is actually exercised.
- `tests/test_transformation_phase6_monitoring.py::test_live_snapshot_on_this_machine`
  now passes because `WindowsProcessApi` resolves
  `K32GetProcessMemoryInfo` -> `psapi!GetProcessMemoryInfo` -> kernel32
  forwarder with explicit ctypes signatures (no truncated 64-bit handles), and
  the top-process scan uses the same resolved API plus `K32EnumProcesses`.

### Results

- Staging full suite (Linux sandbox): **731 passed, 1 skipped** (skip = real Tk
  window needs a display).
- Phase 10 file alone: 20 passed. Ruff: clean across `src/` and `tests/`.
- Release audit: **READY**, 24 checks, 0 failures, 1 warning (unsigned-release
  trust anchor is not configured in a sandbox build).
- Patcher clean rooms (cumulative Phase 10 -> 0.72.0): Phase 10 baseline
  `created=created=212, updated=49, skipped=0`, Transformation Phase 8 baseline
  `created=created=10, updated=92, skipped=159`, GitHub Phase 58 baseline `created=created=54, updated=86, skipped=121`;
  every room ran `pytest` to `731 passed, 1 skipped`, `ruff` clean, release audit
  READY 24 checks, `pc-agent voice status` reporting the microphone as off,
  rerun idempotent (`skipped=261`), sentinel `.env` untouched.

### Not claimed

Native Windows execution of this build remains unverified in-repo: the fixes
above are reasoned from the Windows failure output and are covered by the tests,
but only a run on the user's Windows machine makes them facts.

## Transformation Phase 13 (0.75.1) - Connectors

- New file `tests/test_transformation_phase13_connectors.py`: **26 passed**.
  Real `.ics` files, a real folder with real SHA-256 hashes, real HMAC
  signatures, real SQLite rows, and injected IMAP/SMTP protocol objects - no
  network and no fake assertions.
- Refusals asserted, not assumed: closed master gate, closed e-mail gate, chat
  endpoint with credentials/loopback, chat redirect, mail send without
  `confirm="SEND"`, header injection (CRLF), >5 recipients, forged signature,
  stale timestamp, replayed `kind:external_id`, oversized webhook body,
  non-JSON body, oversized calendar file, invalid `start`, empty summary,
  symlink escaping the drive root, secret-named files, and out-of-range
  configuration values (`ConfigError`, never a silent clamp).
- Secrets asserted absent from every public surface: `public_summary()`,
  `connector status` (with `--probe`) and the diagnostics export; tokenised chat
  URLs are echoed only as `scheme://host/path`.
- Surfaces covered: `LocalService` connector methods, the full `pc-agent
  connectors …` CLI group, the `connectors` diagnostics section,
  `connectors_summary_line()`, and release-audit check 27 (pass when off, warn
  when on).
- Ledger bounding: 30 recorded events keep 10 rows at `limit=10`, 25 deliveries
  keep 5 at `limit=5`.
- Full suite: **827 passed, 1 skipped** (skip = no desktop display for Tk).
  Ruff: clean across `src/` and `tests/`.
- Release audit: **27 checks, 0 fail, 1 warning** on a default install.
- Clean rooms (cumulative Phase 13 -> 0.75.1) are recorded at the end of this
  report.

### What Phase 13 does not claim

- No CalDAV/Google/Microsoft calendar account support, no mail bodies or
  attachments, no provider-specific chat payloads (Slack blocks/Teams cards).
- No OAuth anywhere, so nothing acts on behalf of a third-party account.
- Connectors are not agent tools yet: an autonomous mission cannot read the
  calendar or send a chat message by itself until Phase 14 defines the
  notification policy.
- Webhooks are verified through the CLI/API; no public listener is started.
- Native Windows behaviour of the connectors is not verified in-repo - the
  file-based connectors are platform-neutral, but IMAP/SMTP/HTTPS behaviour on
  the user's machine still needs the acceptance run.

## Transformation Phase 12 (0.74.0) - Visual Workflow Builder

Scope: `src/pc_agent/flows.py` (new engine: validation, store, runner,
triggers, export/import), schema migration 14 (`flow_definitions`,
`flow_versions`, `flow_triggers`, `flow_runs` + 6 indexes), 22 service methods,
the `flows` CLI group, 18 HTTP routes, the `flows` diagnostics section,
`Flows:` doctor line, release check 26 `flow_builder`, the GUI **Workflows** tab
and the web **Workflow Builder** panel, plus the provider-key redaction
hardening in `logging_config.py`.

### Automated tests

`tests/test_transformation_phase12_flows.py` - 36 tests:

1. valid document -> dependency levels, `{{variable}}` substitution, task specs
2. parallel branches collapse into one execution level
3. 10 parametrised validation refusals (unknown key, wrong `flow_schema`, empty
   name, empty node list, bad id slug, duplicate id, missing instruction,
   edge to unknown node, bad variable name, unresolved placeholder)
4. cycle refused by Kahn
5. node/variable limits and the 32-character id cap
6. export checksum + "edited after export" refusal; import unknown-field and
   size-cap refusal
7. immutable version history (`v2, v1` with hashes) and delete cascades
8. disabled flow not run; one active run per flow; `reconcile()` closes runs
   whose mission finished and unblocks the next run
9. mission compilation asserted exactly (task name, `task_specs`, `depends_on`)
10. failed mission start recorded as `failed` **and** the provider key in the
    error is redacted before storage
11. schedule trigger fires once per interval with an explicit `now=`
12. event trigger watermark: one fire per new `app_events` row, no replay
13. trigger bounds (60 s minimum, one-shot `run_at` must be in the future,
    unknown kind, empty `event_type`) and the 8-trigger cap
14. service end-to-end (validate/create/list/show/run/trigger/toggle/export/
    import/update/enable/tick/stats/delete)
15. unknown flow raises a real "not found" error
16. HTTP sweep: 201/202/404 semantics, `POST .../definition` update, trigger
    toggle + delete, `DELETE` flow
17. CLI sweep: every `flows` sub-command incl. `--json`, export file checksum
    matches the printed checksum, `doctor` prints the `Flows:` line
18. diagnostics + release audit (26 checks, `flow_builder` PASS)
19. schema v14 creates the four flow tables and keeps `automation_runs`
20. identity + web panel markers (`/v1/flows`, `/v1/flow-runs`, Workflow Builder)
21. GUI controller actions (validate/save/show/run/tick/export/runs/summary)

### Results

- Staging full suite (Linux sandbox): **793 passed, 1 skipped** (skip = real Tk
  window needs a display).
- Phase 12 file alone: 36 passed. Ruff: clean across `src/`.
- Manual smoke (service, CLI, API, doctor): create -> run (mission started) ->
  schedule trigger -> tick -> export -> import (v2) -> doctor
  `Flows: flows=1; triggers_enabled=…` and `release audit` `checks=26`.
- Performance (measured, Linux sandbox): validate 0.043 ms median, `get`
  0.711 ms, `list_flows(20)` 0.727 ms, `run_flow` 7.6 ms, `tick` (8 due
  triggers) 1.42 ms, `reconcile` (50 runs) 0.63 ms.

### Native Windows evidence from the 0.73.0 build (user machine)

`python -m pytest -q` on Windows 10, Python 3.11.0, `D:\omni-assistant`:
**746 passed, 8 skipped, 1 failed** in 11m55s; `ruff check` clean; `pc-agent
--version` = 0.73.0; `doctor` Status OK; `release audit` READY, 25 checks, 0
failures, 2 warnings; `gui --check` READY; 70 tools registered; the Windows-only
paths that previously returned `None` were live and working
(`cpu … via GetSystemTimes`, `memory=83%`, `network … via GetIfTable`,
`process_rss … via K32GetProcessMemoryInfo`, `voice devices` via
`winmm.waveOutGetDevCapsW`).

The single failure was real and is fixed in 0.74.0 (folded into this patcher):

| Failure | Cause | Fix |
| --- | --- | --- |
| `test_live_snapshot_on_this_machine` — `assert second.top_processes` -> `[]` | `WindowsProcessApi._resolve()` returns `(function, name)`; `EnumProcesses` stored the tuple while the memory calls unwrapped `entry[0]`. Calling it raised `TypeError`, which the scan's `except` swallowed | unwrap the enumeration entry; retry `OpenProcess` with `PROCESS_QUERY_LIMITED_INFORMATION`; injectable DLL loader + 3 new tests (`callable` contract, sort/limit mapping, denied-handle skipping) that run on Linux and Windows |

The 8 skips are environment-gated, not defects: 4 symlink-privilege skips
(`WinError 1314` — enable Developer Mode or run elevated to execute them) and 4
POSIX/systemd/logind oracles that only run on Linux.

### Clean-room patcher verification (Phase 12 -> 0.74.0)

Each room is a fresh copy of a different starting point, patched with
`TRANSFORMATION_PHASE_12_WORKFLOWS.mjs`, installed into its own virtualenv and
fully tested:

| Room | Base | Patcher result | Byte identity | Suite | Ruff | Release audit | Rerun |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A | `/home/user/verify_9_final` (delivered Phase 60 tree, 0.62.0) | created=219, updated=49, skipped=0 | allow-listed=317, mismatched=0 | 793 passed, 1 skipped | clean | READY, 26 checks, 0 fail, 1 warn | created=0, updated=0, skipped=268 |
| C | GitHub Phase 58 `89610f3d` (0.60.0) | created=61, updated=90, skipped=117 | allow-listed=317, mismatched=0 | 793 passed, 1 skipped | clean | READY, 26 checks, 0 fail, 1 warn | created=0, updated=0, skipped=268 |
| D | current 0.74.0 tree (idempotence) | created=0, updated=0, skipped=268 | allow-listed=317, mismatched=0 | 793 passed, 1 skipped | clean | READY, 26 checks, 0 fail, 1 warn | created=0, updated=0, skipped=268 |

A Transformation Phase 11 (0.73.0) tree was also upgraded during development of
this patcher (created=3, updated=90, skipped=175, suite green, sentinel `.env`
untouched); that throwaway tree is deleted, so the reproducible bases above are
the two historic trees plus the current one.

Every room also confirmed: `pc-agent --version` = 0.74.0, `pc-agent doctor`
printing both the `Automation:` and `Flows:` lines, `pc-agent gui --check` READY,
`pc-agent flows stats` and `flows list` exiting 0, the patcher being idempotent
on a second run, and the sentinel `.env` byte-identical afterwards.

### Not claimed

- No drag-and-drop canvas: the GUI/web editor is a JSON document plus real
  buttons.
- No conditions, loops or per-node model/tool selection; flows are linear DAGs of
  instructions.
- Event triggers poll `app_events`; there is no webhook/push until Phase 13.
- Two app instances sharing one database could both tick; the one-active-run
  guard limits the damage, a database lock is future work.
- Native Windows re-run of this build is still pending (the same statement as
  0.73.0: only a run on the user's machine makes platform behaviour a fact).


## Transformation Phase 11 (0.73.0) - Advanced Automation + local vision

Scope: `src/pc_agent/perception.py`, `tools/perception_tools.py`,
`browser_cdp.py` (`wait_for`, `_wait_probe_js`, `activate_tab`, `close_tab`),
`tools/browser_tools.py` (`browser.wait`, `browser.tabs`),
`desktop_automation.py` (`WindowStateChange`, `set_window_state`),
`tools/desktop_automation_tools.py` (`desktop.window_state`), schema migration
13 (`automation_runs` + 2 indexes) with `AutomationRepository`, CLI `automation`
group, 8 HTTP routes, GUI Automation tab (13th), diagnostics `automation`
section, release audit check 25 (`automation_audit`).

### New tests

`tests/test_transformation_phase11_automation.py` - 23 tests:

1. identity is single-sourced (`0.73.0`, schema 13, phase 11, 69 tools)
2. the five new tools are registered with their contracts
3. schema v13 creates `automation_runs` and re-initialising is idempotent
4. dense page analysis reports busy cells + a region hint
5. a flat image reports `text_likely=False` and says so
6. oversized payloads are refused; an over-large grid is clamped, not expanded
7. template location maps back to exact screen coordinates
8. a flat template is refused; an absent template reports not-found
9. `vision.screen` / `vision.locate` record their runs in the ledger
10. `browser.wait` polls to a match and closes its session
11. `browser.wait` refuses with no condition
12. `browser.tabs` lists, activates, closes, and refuses the last tab
13. the wait probe JSON-quotes values (no script breakout)
14. `desktop.window_state` validates bounds/state before touching Windows
15. `WindowStateChange` reports a real `changed` flag
16. repository record/list/summary/get round trip
17. the ledger has no content columns and stores only target/detail labels
18. the service exposes capabilities, vision and ledger routes
19. the service reports real failures for missing images
20. `/v1/automation/*` + `/v1/vision/screen` round trip with bearer auth
21. `pc-agent automation capabilities|screen|stats` JSON and text output
22. diagnostics section + release `automation_audit` (25 checks) + doctor line
23. GUI 13th tab and controller automation actions exist

### Results

- Staging full suite (Linux sandbox): **754 passed, 1 skipped** (skip = real Tk
  window needs a display).
- Phase 11 file alone: 23 passed. Ruff: clean across `src/`.
- Release audit: **READY**, 25 checks, 0 failures, 1 warning (unsigned-release
  trust anchor is not configured in a sandbox build).
- Patcher clean rooms (cumulative Phase 11 -> 0.73.0): see the final matrix
  below.

### Not claimed

- Native Windows execution of this build remains unverified in-repo: window
  placement, UIA and CDP behaviour are reasoned from the API contracts and
  covered by injected-backend tests, but only a run on the user's Windows machine
  makes them facts.
- No OCR and no element-level vision grounding: `vision.locate` returns
  coordinates only, and matching a large block of repeating body text can return
  an equally-matching offset.
- Patcher clean rooms (cumulative Phase 11 -> 0.73.0, 314 allow-listed files,
  `mismatched=0` in every room):

| Room | Base | Patcher result | Suite | Ruff | Release audit | Rerun |
| --- | --- | --- | --- | --- | --- | --- |
| A | `/home/user/verify_9_final` (delivered Phase 60 tree) | created=216, updated=49, skipped=0 | 754 passed, 1 skipped | clean | READY, 25 checks, 0 fail, 1 warn | skipped=265 |
| B | Transformation Phase 10 tree (0.72.0) | created=4, updated=95, skipped=166 | 754 passed, 1 skipped | clean | READY, 25 checks, 0 fail, 1 warn | skipped=265 |
| C | GitHub Phase 58 `89610f3d` (0.60.0) | created=58, updated=90, skipped=117 | 754 passed, 1 skipped | clean | READY, 25 checks, 0 fail, 1 warn | skipped=265 |

Every room also confirmed: `pc-agent --version` = 0.73.0, `pc-agent doctor`
printing the `Automation:` line, `pc-agent gui --check` READY,
`pc-agent automation capabilities` and `automation runs` exiting 0, and the
sentinel `.env` byte-identical after the patcher ran twice.

### Process-scan hotfix (0.75.1) - from real Windows evidence

The user's Windows run of 0.75.0 failed exactly one test
(`test_live_snapshot_on_this_machine`: `top_processes == []`) while every other
metric was correct, and the same symptom had already been reported on 0.74.0.
The previous fix (unwrapping the `EnumProcesses` tuple) was necessary but not
sufficient: the scan itself was still wrapped in one broad `except`, so a
refused `OpenProcess`, a missing PSAPI export or an empty enumeration all
collapsed into an empty list that was indistinguishable from success.

What changed:

- `_top_processes()` now returns `ProcessScan(rows, source, detail)` and the
  Windows path is a four-layer chain - `win32-enum` -> `win32-toolhelp` ->
  `tasklist` -> `powershell` - tried in that order, with every earlier failure
  kept in `detail`.
- The snapshot exposes `top_processes_source` and `top_processes_detail`, and
  the `System:` doctor line ends with `processes=<n> (<source>)`, so an empty
  scan can never again look healthy.
- The OS-level fallbacks use a fixed, argument-free argv with
  `CREATE_NO_WINDOW`, an 8 s timeout and capped output; they run only after the
  in-process API produced nothing.
- Parsing is locale-tolerant: `1,234 K`, `1.234 K`, `12 345 K` and `640,000 MB`
  all parse, and malformed rows are skipped instead of shifting columns.

New tests (8, in the existing Phase 6 monitoring file, so it now holds 18):

| Test | What it nails down |
| --- | --- |
| `test_fallback_chain_uses_the_first_layer_that_reports_rows` | layer order + reason preservation |
| `test_fallback_chain_reports_a_reason_instead_of_an_empty_list` | honest `unavailable` + detail |
| `test_tasklist_csv_parsing_is_locale_tolerant` | separators, MB, malformed rows, empty input |
| `test_powershell_fallback_parsing` | `Id\|WorkingSet64\|Name` parsing and sorting |
| `test_enum_layer_records_opened_and_unreadable_counts` | `opened=2/3 unreadable=1` evidence |
| `test_enum_layer_explains_a_missing_export` | "GetProcessMemoryInfo export missing" |
| `test_toolhelp_layer_uses_snapshot_names` | snapshot names override `pid-<n>` |
| `test_snapshot_never_hides_an_empty_process_scan` | empty scan still explains itself |

Result: **827 passed, 1 skipped**, ruff clean, release audit unchanged at 27
checks. Native Windows confirmation of this specific fix still needs the next
run on the user's machine - the run itself is the evidence, not this report.

### Clean-room patcher verification (Phase 13 -> 0.75.1)

`TRANSFORMATION_PHASE_13_CONNECTORS.mjs` embeds 279 files and was verified in
three clean rooms; the allow-list byte-identity check compares **328**
allow-listed files against the staging tree (`mismatched=0` in every room):

| Room | Base | Patcher result | Suite | Ruff | Release audit | Rerun |
| --- | --- | --- | --- | --- | --- | --- |
| A | `/home/user/verify_9_final` (delivered Phase 60 tree, 0.62.0) | created=230, updated=49, skipped=0 | 827 passed, 1 skipped | clean | READY, 27 checks, 0 fail, 1 warn | created=0, updated=0, skipped=279 |
| C | GitHub Phase 58 `89610f3d` (0.60.0) | created=72, updated=90, skipped=117 | 827 passed, 1 skipped | clean | READY, 27 checks, 0 fail, 1 warn | created=0, updated=0, skipped=279 |
| D | current 0.75.1 tree (idempotence) | created=0, updated=0, skipped=279 | 827 passed, 1 skipped | clean | READY, 27 checks, 0 fail, 1 warn | created=0, updated=0, skipped=279 |

Every room also confirmed: `pc-agent --version` = 0.75.1, `pc-agent doctor`
printing the `Connectors:` line (plus the existing `System:`, `Provider
platform:`, `Knowledge:`, `Agents:`, `Voice:`, `Automation:` and `Flows:` lines),
`pc-agent gui --check` READY, `connectors status` and `connectors events`
exiting 0, and the sentinel `.env` byte-identical after the patcher ran twice.

## Transformation Phase 14 (0.76.0) - Proactive Intelligence

- New file `tests/test_transformation_phase14_proactive.py`: **24 passed**.
- Full suite: **851 passed, 1 skipped** (skip = no desktop display for a real Tk
  window). `ruff check` clean. Release audit `READY`, `failures=0`,
  `warnings=1`, `checks=28`.

### What the 24 tests actually prove

| Group | Tests | Claim |
| --- | --- | --- |
| Identity | 1 | `0.76.0`, phase 14, schema v16, 16 GUI tabs and 28 release checks are single-sourced |
| Schema | 1 | v16 creates `reminders`, `notifications`, `proactive_briefings`, `proactive_state` with their indexes on a fresh database |
| Parsing | 3 | offsets/`HH:MM`/ISO resolve against the injected clock; quiet windows wrap midnight; malformed quiet hours and briefing times are **refused**, not ignored |
| CRUD and limits | 3 | create/read/snooze/done/dismiss/enable/delete round-trip; title/body/severity/repeat bounds; the open-reminder cap errors with a reason; the whole engine can be switched off |
| Firing | 1 | a due reminder fires exactly once and a repeating one reschedules from the same row |
| Policy | 5 | quiet hours defer then release; `critical` crosses the window only with the explicit switch; the rolling hourly cap defers with "hourly limit"; the per-key cooldown suppresses duplicates; outbound is gated and a refusal/exception never removes the in-app notification |
| Briefings | 3 | six real sections, idempotent per local date, schedule switch respected, and the scheduler tick produces at most one briefing per day |
| Surfaces | 4 | `LocalService` methods, `pc-agent proactive …` (including `--due -1m` after the argparse fold), all `/v1/proactive/*` routes over a real `LocalApiServer`, and the GUI controller + web panel |
| Audit | 2 | the `proactive` diagnostics section and release check 28 exist; the notification and briefing ledgers stay bounded (500 / 200) |
| Determinism | 1 | a simulated 24-hour day: night deferral -> morning release -> 09:00 burst capped at 3 with 2 deferred "hourly limit" -> both fire when the hour rolls over -> exactly one briefing per local date |

### Verification outside pytest

Run by hand on the sandbox against real files and a real HTTP listener (no mocks):

- CLI: `proactive reminders add` with `+2h` and `-1m`, `list`, `tick` (fired a
  reminder and the briefing), `notifications list`, `briefing show`,
  `snooze`, `done`, `delete`; `--due nonsense` exits 1 with
  `proactive error: 'nonsense' is not a due time …`.
- API: `status`, reminder create/snooze/done, `notify`, forced `briefing`,
  `briefings`, `tick`, `notifications/clear` - all returned real data.
- `pc-agent doctor` printed `Proactive: reminders=0 next=none; notifications=2
  (last hour 2/20); quiet=none(open); briefing=off; outbound=off` and
  `Release audit: READY; failures=0; warnings=1; checks=28`.

### What Phase 14 does not claim

- No OS-level toast notification is delivered; in-app notifications and the
  event log are real, Windows toasts are not implemented (and therefore not
  claimed).
- Reminders do not fire while the service is stopped; they fire late on the next
  start.
- Windows acceptance of 0.76.0 needs a run on the user's machine; this report
  records Linux-sandbox evidence only.

### Clean-room patcher verification (Phase 14 -> 0.76.0)

`TRANSFORMATION_PHASE_14_PROACTIVE.mjs` embeds 282 files and was verified in
three clean rooms (the digest of the delivered file is published next to it as
`TRANSFORMATION_PHASE_14_PROACTIVE.mjs.sha256`, so this report cannot go stale
every time the patcher is rebuilt). The allow-list byte-identity check compares
**331** allow-listed files against the staging tree (`mismatched=0` everywhere):

| Room | Base | Patcher result | Suite | Ruff | Release audit | Rerun |
| --- | --- | --- | --- | --- | --- | --- |
| A | `/home/user/verify_9_final` (delivered Phase 60 tree, 0.62.0) | created=233, updated=49, skipped=0 | 851 passed, 1 skipped | clean | READY, 28 checks, 0 fail, 1 warn | created=0, updated=0, skipped=282 |
| C | GitHub Phase 58 `89610f3d` (0.60.0) | created=75, updated=90, skipped=117 | 851 passed, 1 skipped | clean | READY, 28 checks, 0 fail, 1 warn | created=0, updated=0, skipped=282 |
| D | current 0.76.0 tree (idempotence) | created=0, updated=0, skipped=282 | 851 passed, 1 skipped | clean | READY, 28 checks, 0 fail, 1 warn | created=0, updated=0, skipped=282 |

Every room also confirmed: `pc-agent --version` = 0.76.0, `pc-agent doctor`
printing the new `Proactive:` line (plus the existing `System:`, `Provider
platform:`, `Knowledge:`, `Agents:`, `Voice:`, `Automation:`, `Flows:` and
`Connectors:` lines), `pc-agent gui --check` READY, and `proactive status`,
`proactive reminders list`, `proactive notifications list` and
`proactive briefing show` all exiting 0. The sentinel `.env` stayed
byte-identical after running the patcher twice in each room.

## Transformation Phase 15 (0.77.0) - Plugin Ecosystem

- New file `tests/test_transformation_phase15_plugins.py`: **36 passed**.
- Full suite: **887 passed, 1 skipped**. `ruff check` clean. Release audit
  `READY`, `failures=0`, `warnings=1`, `checks=29`.

### What the 36 tests actually prove

Real Ed25519 keys, real `.zip` bundles and real child processes are used
throughout: the verifier, the sandbox and the worker are never mocked.

| Group | Tests | Claim |
| --- | --- | --- |
| Identity/schema | 2 | 0.77.0, phase 15, schema v17, 17 GUI tabs, 29 release checks; v17 creates `plugins`, `plugin_versions`, `plugin_runs`, `plugin_state` |
| Manifest | 2 | 8 parametrized bad documents refused (bad id, `1.0` version, unknown schema, unknown field, unknown capability, traversal entry, non-`.md` skill, bad tool name); the capability list is closed and `network`/`subprocess` are marked non-grantable |
| Gating | 1 | service off by default and install refuses before touching disk |
| Signatures | 4 | signed install verifies; unsigned refused unless opted in; tampered content refused; signature from an untrusted key refused; missing manifest refused |
| Archive safety | 2 | `../evil.py` traversal and symlink entries refused; file-count limit enforced |
| Capabilities | 1 | `network` refused at install time |
| Lifecycle | 2 | install → enable → disable → rollback → uninstall with version history and directory cleanup; rollback without history refused |
| Sandbox | 3 | a real tool call returns a real result plus a `plugin_runs` row with the sandbox mechanism; undeclared tool and missing `confirm` refused; wrong argument type refused inside the worker |
| Containment | 3 | 30 s sleep killed by a 1 s timeout; `socket.create_connection` refused; write outside granted roots refused **and no file created** |
| Skills | 1 | Markdown skills returned as bounded text only |
| Surfaces | 5 | CLI (status/verify/install/enable/run/list/runs/remove), bad-bundle exit codes, 12 API routes over a real `LocalApiServer`, GUI controller methods + web panel markers, diagnostics section + doctor line + release check |
| Ledgers | 1 | 510 runs collapse to 500 rows; version history capped at 5 |

### Verification outside pytest

- `pc-agent plugins status` printed the real host mechanism
  (`setrlimit + isolated-interpreter + audit-hook`), the limits in force and
  `not grantable: network, subprocess`.
- A signed bundle was verified, installed, enabled, run (`{"words": 3}`),
  inspected for skills, listed in the run ledger and removed - through the CLI
  and again over a real HTTP listener.
- `pc-agent doctor` printed `Plugins: off (PC_AGENT_ALLOW_PLUGINS is not set)`
  on a default install, and `pc-agent release audit` reported `READY`,
  29 checks, 0 failures, 1 warning.

### What Phase 15 does not claim

- The audit hook is a CPython mechanism: native code inside a plugin could
  bypass it. OS limits remain in force, and native plugins are not accepted.
- Reads are not path-confined in this phase (writes are).
- Windows acceptance of 0.77.0 needs a run on the user's machine; the Job Object
  path is exercised by unit-level code only, not by real Windows evidence here.

### Clean-room patcher verification (Phase 15 -> 0.77.0)

`TRANSFORMATION_PHASE_15_PLUGINS.mjs` embeds 290 files and was verified in three
clean rooms; the allow-list byte-identity check compares **339** allow-listed
files against the staging tree (`mismatched=0` everywhere). The delivered file's
digest is published next to it as `TRANSFORMATION_PHASE_15_PLUGINS.mjs.sha256`.

| Room | Base | Patcher result | Suite | Ruff | Release audit | Rerun |
| --- | --- | --- | --- | --- | --- | --- |
| A | `/home/user/verify_9_final` (delivered Phase 60 tree, 0.62.0) | created=241, updated=49, skipped=0 | 887 passed, 1 skipped | clean | READY, 29 checks, 0 fail, 1 warn | created=0, updated=0, skipped=290 |
| C | GitHub Phase 58 `89610f3d` (0.60.0) | created=83, updated=90, skipped=117 | 887 passed, 1 skipped | clean | READY, 29 checks, 0 fail, 1 warn | created=0, updated=0, skipped=290 |
| D | current 0.77.0 tree (idempotence) | created=0, updated=0, skipped=290 | 887 passed, 1 skipped | clean | READY, 29 checks, 0 fail, 1 warn | created=0, updated=0, skipped=290 |

Every room also confirmed: `pc-agent --version` = 0.77.0, `pc-agent doctor`
printing the new `Plugins:` line (plus `System:`, `Provider platform:`,
`Knowledge:`, `Agents:`, `Voice:`, `Automation:`, `Flows:`, `Connectors:` and
`Proactive:`), `pc-agent gui --check` READY, `plugins status`, `plugins list` and
`plugins runs` all exiting 0, and a sentinel `.env` that stayed byte-identical
after running the patcher twice.

## Transformation Phase 16 (0.78.0) - Encrypted Device Sync

**51 new tests** in `tests/test_transformation_phase16_sync.py`, plus the full suite
re-run: **938 passed, 1 skipped** (Windows skips the 3 symlink + 5 POSIX tests; the
single skip on Linux is pre-existing). `ruff check` clean. Release audit:
**30 checks, 0 failures, 1 warning** (`READY`).

The Phase 16 tests use **two real device identities in two independent data
directories** and **real bundles on disk** - no crypto is mocked.

| Area | Tests | What is proven |
| --- | --- | --- |
| Identity | 6 | created once and reused, `0600` file, public document carries no seed, device id derived from both public keys, Ed25519+X25519 key shapes, deterministic fingerprint |
| Crypto | 4 | seal/open round-trip, a third device cannot read a bundle ("not addressed"), header tamper fails the signature, ciphertext tamper fails AES-GCM |
| Pairing | 6 | `confirm="PAIR"` required, malformed keys refused, self-pairing refused, fingerprint persisted, unpair removes trust, pause stops new bundles |
| Export/import | 9 | `0600` bundle in the sync folder, correct suffix, one recipient, no plaintext record names, real reminder carried and applied, replay refused, stale `seq` refused, unpaired sender refused, changed signing key refused |
| Refusals | 5 | clock skew, oversize bundle, malformed JSON, missing file, unknown collection |
| Conflicts | 4 | profile never overwritten, new profile created, flow kept under `local`, flow replaced under `remote` |
| Coordination | 5 | request pending until decided, accept creates a real reminder, deny executes nothing, unknown kind refused at the sender, double decision refused |
| Transport/ledger | 6 | `tick` export+import both ways, ledger records both directions, honest folder/limit status, missing folder reported, diagnostics + doctor lines, retention skips stale bundles |
| Product wiring | 6 | release audit has 30 checks incl. `device_sync`, phase identity (0.78.0 / v18 / 18 tabs), Local Service surface + events, 12 HTTP routes (incl. 400 and DELETE), CLI + GUI surfaces, export suffix rule |
| Hygiene | 1 | bundle envelope contains no URL/network fields |

### What Phase 16 does not claim

- Not a cloud service: the transport is a folder the user already syncs. No relay,
  no real-time push, no server, no account.
- Metadata (filenames, sizes, sender device id, sequence) is visible to whoever can
  list the folder; only content is encrypted.
- Identity storage is filesystem permissions, not an OS keystore yet; no key
  rotation UI; unpairing does not recall data already delivered.
- Native Windows multi-device evidence for this phase has not been produced yet.

## Transformation Phase 17 (0.79.1) - Accessibility and Multilingual UX

**44 new tests** in `tests/test_transformation_phase17_accessibility.py`; full suite
**993 passed, 1 skipped** in 0.79.1 (the one skip is the real-Tk-window test that
needs a display; `ruff check` clean). Release audit: **31 checks, 0 failures, 1
warning**. Verified three times under different host timezones - `TZ=UTC`,
`TZ=Asia/Karachi`, `TZ=America/New_York`: 993 passed, 1 skipped each time.

The Phase 17 tests are written to catch dishonesty, not just regressions:

| Area | Tests | What is proven |
| --- | --- | --- |
| Catalogs | 7 | identical key sets in all four locales, **no locale may copy English text**, placeholders preserved exactly, locale names in their own script, declared directions, computed coverage, locale rows |
| Translator | 8 | real Urdu/Arabic output, placeholder substitution, missing value refused, unknown key echoed, unsupported locale refused, `en-US`/`UR_pk` normalised, honest fallback for a partial locale (monkeypatched), `as_dict` contents |
| WCAG maths | 8 | white/black = 21.0, identical colours = 1.0, luminance references, bad colour refusal, **both shipped palettes pass AA**, high-contrast really differs |
| Settings | 6 | English/LTR defaults, environment override, five bad-value refusals, `ui.json` persistence, environment-still-wins, corrupt file tolerated, `apply_ui_settings` validation |
| Report honesty | 7 | five disclosed limits, no overclaim strings, report follows the locale (Urdu claim text), single-line doctor summary, diagnostics section, release check 31 pass, broken palette flips it to warn |
| Surfaces | 8 | service set/report/persist/event, refusals, messages, `perf_bench` shape, CLI list/set/refusal exit codes, `a11y check` exit 0 with `[PASS] contrast_aa`, doctor line, 5 HTTP routes, GUI constant/controller/source markers, web ARIA/RTL markers |

### What Phase 17 does not claim

- Not a screen-reader-certified desktop application: Tk 8.6 publishes no
  accessibility tree and has no bidi/shaping; both are printed in the product.
- Not "fully accessible": the report lists the five real limits instead of a badge.
- Translations cover the 61 interface keys this phase introduces; text from earlier
  panels stays English until those keys exist.
- No machine translation of user content, ever.
- Native Windows evidence for this phase is still pending.

## Transformation Phase 17.1 (0.79.1) - Windows acceptance hotfix

The 0.79.0 Windows run returned **12 failed, 919 passed, 8 skipped (1284 s)**.
All twelve are closed in 0.79.1, and the suite is now timezone-independent.

**10 new tests** in `tests/test_transformation_phase17_1_hotfix.py`:

| Area | Tests | What is proven |
| --- | --- | --- |
| Hourly cap | 3 | suppressed (deferred) rows no longer consume the budget, so waiting reminders are released on the next window; the cap still bites inside the same hour; a suppression row is stamped with the ledger instant, not the host clock |
| Timezone correctness | 2 | a naive ISO due time means local wall clock and is stored as UTC; a rescheduled reminder is comparable as an instant |
| Sandbox spawn | 4 | a host that cannot start any interpreter yields a structured `spawn_error` (no exception, no plugin blame); a requested interpreter that cannot start is substituted and the substitution is reported; the real `-I -B` worker runs end to end; the child env keeps the Windows/POSIX essentials and leaks no `*_API_KEY` |
| Permissions | 1 | `private_file_report()` matches the mechanism the host actually has (POSIX mode vs verified Windows ACL) |

Corrections to existing tests (no intent weakened, no skips added):

* Phase 14 - two assertions compared ISO strings that could carry different UTC
  offsets; both now compare `datetime` instants.
* Phase 15 - the "escape" plugin fixture used the POSIX path `/tmp`; it now uses
  `tempfile.gettempdir()`, and the plugin still writes outside its capability
  roots.
* Phase 16 - a POSIX mode assertion became `_assert_owner_only()`, which checks
  the mode on POSIX and the *verified ACL* on Windows.

Regression evidence runs:

```
TZ=UTC              .venv/bin/python -m pytest -q  ->  993 passed, 1 skipped
TZ=Asia/Karachi     .venv/bin/python -m pytest -q  ->  993 passed, 1 skipped
TZ=America/New_York .venv/bin/python -m pytest -q  ->  993 passed, 1 skipped
tests/test_transformation_phase14_proactive.py under 6 timezones -> 24 passed each
ruff check .  ->  All checks passed
pc-agent release audit  ->  READY; failures=0; warnings=1; checks=31
```

Remaining Windows-only expectations: the 8 skips observed in 0.79.0 stay skips
(symlink privilege, POSIX shell execution, RPM/Debian/systemd/loginctl oracles).

## Transformation Phase 18 (0.80.1) - Final production hardening

**52 new tests** in three new modules; the whole suite is **1123 passed,
1 skipped** on a headless host (`ruff check` clean, `release audit` READY with
**32** checks and 0 failures, `release production-check` READY with 10/10 steps).

| Module | Tests | What it proves |
| --- | --- | --- |
| `tests/test_transformation_phase18_hardening.py` | 24 | the ten chain steps run and pass; the chain's honesty rules (ephemeral key, nothing left behind, byte-level tree hashes, refusal for the right reason); stored-report states (missing / passed / failed / unreadable) are three different answers; the CI workflow exists, covers both platforms, runs the same four gates a developer runs, needs no secret and cannot write; the CLI, doctor line, diagnostics and 20th dashboard tab are wired to the real implementation; the four new documents exist and reference commands that work; the phase identity (0.80.1 / phase 18 / schema v18 / 20 tabs / 32 checks) |
| `tests/test_security_suite.py` | 19 | SSRF against loopback, link-local, RFC1918, `0.0.0.0` and a public name that resolves to loopback; non-web schemes and credential URLs refused; shell metacharacters never execute; non-allow-listed and path-traversal executables refused; traversal entries in an archive refused without extraction; configured secrets never appear in diagnostics, audit output or chain reports; emergency stop engages and clears; tool schemas reject unknown fields and unknown tools |
| `tests/test_performance_suite.py` | 9 | measured ceilings for settings load, CLI import, `health()`, `system_status()`, event listing, `a11y_status()`, heap growth over 200 status calls, database initialisation and the release scan; `perf bench` returns real measurements with real notes |

Updated assertions where an honest counter moved: audit checks 31 -> 32,
dashboard tabs 19 -> 20, phase identity 17 -> 18 (across 60 phase test modules).

Skip policy (unchanged and declared): 1 skip on a headless host (a real Tk window
needs a desktop session); on Windows 8 platform-oracle skips remain by design
(symlink privilege, POSIX shell, RPM/Debian/systemd/loginctl oracles).

### What Phase 18 does not claim

- CI has been validated **structurally** and by running its exact commands
  locally; it has not yet executed on GitHub.
- The chain check covers the local bundle + detached-signature transport, not a
  live update feed.
- The chain re-installs the running version (`allow_downgrade=True`) so the
  version-comparison branch is covered by its own unit tests instead.
- Performance ceilings catch order-of-magnitude regressions, not micro-regressions.

## Windows acceptance evidence gate (0.80.1)

`tests/test_windows_acceptance_evidence.py`, **19 tests**, all platform-neutral so
they run on the POSIX CI host:

| Property | Tests |
| --- | --- |
| No record → `pending_user_windows_evidence` (baseline included) | 1 |
| A verified passed Windows report flips the baseline to `verified:<date>` | 1 |
| An edited body is refused by the reader (hash) | 1 |
| Non-Windows / non-passed recorded files are refused | 1 |
| `verify` accepts a well-formed report | 1 |
| Six rejection cases: unknown status, wrong platform, missing gate, failing gate, wrong version, key material | 6 (parametrised) |
| Missing file reported, not raised | 1 |
| Default run off Windows claims nothing (`skipped`, gates `not-run`) | 1 |
| `--self-test` runs the real gates and says `self-test` | 1 |
| A `self-test` report can never be recorded | 1 |
| Record path, and refusal of an unverified file | 1 |
| No secrets in the written report | 1 |
| CLI status / self-test / verify / record / `--json` | 2 |
| `doctor` line carries the acceptance state | 1 |

Full suite after this change: **1123 passed, 1 skipped** under `TZ=UTC`,
`TZ=Asia/Karachi` and `TZ=America/New_York` (the skip is the Tk window test on a
headless host). Ruff clean.

### What this does not test

- No test can prove the gates pass on Windows from this host. That is the point of
  the record: it is produced there and verified here.
- The seal is tamper-evidence, not authentication - a test asserts the refusals,
  not a third-party signature.

## Windows launcher-shim sandbox fix (0.80.1)

`tests/test_windows_launcher_sandbox.py`, **13 tests**, all platform-neutral:

| Property | Tests |
| --- | --- |
| One launcher slot, other limits untouched | 1 |
| No slack → identical limits object | 1 |
| Job limits applied before the process joins the job (ordering) | 1 |
| Probe cache keyed by `(interpreter, limit)` | 1 |
| Probe returns a real reason, not a blank string | 1 |
| Strict pass first, launcher slot only after it fails | 1 |
| Used slot reported as `+launcher-slot(active-process-limit=2)` | 1 |
| Ladder disabled when subprocesses are allowed | 1 |
| Chosen interpreter runs the worker end to end | 1 |
| Candidate order still prefers the running interpreter | 1 |
| Base interpreter derived from `base_prefix` / `pyvenv.cfg` | 2 |
| Probe survives a stale working directory | 1 |

Verified after the change: **1123 passed, 1 skipped** under `TZ=UTC`,
`TZ=Asia/Karachi` and `TZ=America/New_York`; Ruff clean.

### Honest limits of this fix

- The `+launcher-slot` path grants a second process to the *interpreter chain*.
  It is only used when no candidate can run under the strict limit; the job then
  allows the shim and the worker and nothing else, and the worker's audit hook
  still refuses `subprocess`/`os.system` from plugin code.
- A launcher shim is an environment detail: on a stdlib `venv` (a copied
  `python.exe`) the strict single-process limit stays in force and no slot is
  reported. The result's `mechanism` string always states which policy ran.
- Plugins therefore must stay stdlib-only: with `-I` isolation they never saw the
  environment's site-packages, and the worker may now run under the base
  interpreter of a virtual environment. This is documented in
  `PLUGIN_DEVELOPMENT_GUIDE.md`.



## Voice assistant completion and Authenticode (0.81.0)

`tests/test_voice_assistant_completion.py`, **29 tests**:

| Property | Tests |
| --- | --- |
| 13 utterances route to the expected intent | 13 |
| Reminder splits title from due time | 1 |
| Wake phrase stripped only at the start | 1 |
| Help text matches implemented capabilities | 1 |
| Free text becomes an ordinary task that still needs approval | 1 |
| Notes go through the injected sink | 1 |
| Executor reports failures without raising | 1 |
| `stop` ends the session; `speak` reads text back | 1 |
| Loop refuses to start without the privacy gate / wake opt-in / with emergency stop | 1 |
| Full session: 5 intents in order, replies spoken | 1 |
| Speech without the wake word is ignored | 1 |
| Loop is bounded and `stop()` aborts before the next capture | 1 |
| Loop can run without a wake phrase | 1 |
| A capture error does not kill the session | 1 |
| Acceptance report is sealed, honest on a headless host, and restores the user's mic setting | 1 |
| Acceptance verification catches an edited report | 1 |
| CLI surfaces exist and are wired | 1 |

`tests/test_windows_signing_tooling.py`, **17 tests**:

| Property | Tests |
| --- | --- |
| Thumbprint mode carries no password; hashing and timestamp flags are right | 1 |
| PFX password refused unless the argv exposure is accepted | 1 |
| No certificate means a refusal, never a generated key | 1 |
| Thumbprint normalisation and length validation | 1 |
| Verification answers `unverifiable` off Windows | 1 |
| Missing file reported, not raised | 1 |
| Tool output scrubbed of secrets | 1 |
| Missing `signtool` is a structured result | 1 |
| Real command construction and verification through an injected runner | 1 |
| A failing `signtool` is reported with its message | 1 |
| PFX password redacted from the recorded command and report | 1 |
| `signtool_status` never echoes the thumbprint value | 1 |
| Audit reports unsigned as a fact; 33 checks | 1 |
| Audit reports a recorded verified signature | 1 |
| Audit warns when a recorded attempt did not verify | 1 |
| CLI surfaces and installation guide are wired | 1 |
| The module never holds or writes key material | 1 |

Full suite: **1123 passed, 1 skipped** under `TZ=UTC`, `TZ=Asia/Karachi` and
`TZ=America/New_York`; Ruff clean; `release audit` READY with **33** checks.

### What 0.81.0 does not claim

* No real microphone or speech engine was available on the build host, so the
  hardware gates are reported as `not-run` there - the Windows machine must show
  them `ok` for the acceptance to read `PASSED`.
* Authenticode signing was exercised with an injected runner and by command
  construction; an actual signed EXE requires the user's certificate.
* The loop was exercised with a fake voice object, not with live audio.
\n

## 0.81.1 Windows hotfix

The 0.81.0 Windows run reported `1064 passed / 4 failed / 9 skipped`. The four
failures were mine, not the product's behaviour:

| Test | What was wrong | What it asserts now |
| --- | --- | --- |
| `test_probe_cache_is_keyed_by_the_process_limit` | assumed `sys.executable` starts under `max_processes=1`; on a launcher-shim venv it cannot | relaxed probe succeeds, strict probe succeeds or explains itself, both cached separately |
| `test_the_probe_survives_a_stale_working_directory` | same strict-limit assumption | same check with the relaxed limit |
| `test_cli_status_reports_pending_evidence` | used the no-verb command, which runs the gates on Windows | uses the explicit `status` verb |
| `test_cli_self_test_run_and_verify_and_record` | parsed stdout as JSON while the command printed a status line too | asserts a single JSON document and that the exit code matches the written report |

Two real defects were fixed alongside: `--json` now emits one document, and the
verb `status` never runs the gates. One property was added:
`test_a_launcher_shim_host_still_has_a_working_candidate`.


## 0.82.0 Command Center UI evidence

Suite: **1147 passed, 1 skipped** (the skip is still the real-Tk-window test with
no display), run on Linux under `TZ=UTC`, `Asia/Karachi` and `America/New_York`
with `--basetemp` outside `/tmp`. `ruff check .` clean.
`release audit`: READY (33 checks, 0 failures, the usual mock-provider warning).

New automated tests - `tests/test_transformation_phase19_command_center_ui.py`
(17):

| Test | What it proves |
|---|---|
| `test_build_is_present_and_self_contained` | a build exists, is < 4 MB, every referenced asset resolves, the only absolute URL is the SVG namespace |
| `test_token_meta_is_never_empty_or_duplicated` | exactly one token meta tag |
| `test_static_path_refuses_traversal` | `../`, absolute paths, NUL bytes and symlink escapes are refused |
| `test_inline_script_hashes_match_the_shell` | CSP hashes exist and none is `'unsafe-inline'` |
| `test_spa_shell_is_served_with_strict_headers` | CSP directives, X-Frame-Options, token appears exactly once |
| `test_assets_are_served_with_types_and_cache` | JS/CSS MIME, `immutable`, `nosniff` |
| `test_asset_route_refuses_traversal_and_missing_files` | 404 for all three shapes |
| `test_legacy_panel_still_available_for_rollback` | `/legacy` unchanged |
| `test_missing_build_is_reported_honestly` | the not-built page, with the real fix and no fake dashboard |
| `test_event_stream_emits_ready_frame_and_real_events` | NDJSON headers, `stream.ready`, then a `task.created` the service really wrote |
| `test_event_stream_requires_the_token` | 401 without the bearer |
| `test_event_stream_duration_is_bounded` | the server, not the client, ends the stream |
| `test_event_stream_rejects_garbage_instead_of_guessing` | `since_id=abc` / `duration=xyz` are 400s |
| `test_frontend_sources_are_shipped_for_rebuilds` | sources + lock file ship, build typechecks and cleans |
| `test_both_themes_are_defined_and_contrast_checked` | dark + light tokens, `@tailwind`, reduced motion, and the contrast script passes |
| `test_ui_verification_script_is_checked_in` | the browser verifier and its route list exist |
| `test_no_simulated_metrics_in_ui_sources` | anti-fake-data guard over every view/widget source |

Browser verification (not part of pytest, because it needs Chromium):
`frontend/scripts/ui-verify.js` - **46/46 checks** against the real API, including
17 routes rendering real content, measured contrast in both themes (light body
min 4.73:1, dark 5.99:1), palette open/filter/navigate, live-stream update,
persisted collapse, widget reorder, no overflow at 1280/1024, reduced motion,
no console errors, no failed or cross-origin requests, bounded DOM, FCP 194 ms.

Adjusted for the redesign, with the reason on the test itself:
`test_web_ui_served_with_security_headers` (Phase 5) now accepts the SPA shell at
`/` and relies on the 0.82 suite for the `/legacy` rollback assertion.

Windows: unchanged claim - **not run on Windows** for this build. `not-run` is
not `passed`; the acceptance harness (`pc-agent release evidence`) is the only
thing that can change that status.

### 0.82.1 - voice acceptance hardening (Windows finding)

The 0.82.0 Windows run failed one test; the test was correct to fail.

| Item | Result |
|---|---|
| Reported failure | `test_voice_acceptance_is_sealed_and_honest_on_a_headless_host` - assertion `report.not_run` was empty, i.e. all 7 gates passed on that host |
| Root cause | `push_to_talk` passed on any backend name (`winmm`) without checking that an input device exists |
| Source fix | gate now requires backend **and** `input_devices.count >= 1`; otherwise `not-run` with the scan reason; pass detail states presence-only |
| Test fix | renamed to `..._honest_about_the_host`; asserts `not-run` exactly for hardware the host lacks, `passed` only with backend + device count recorded, evidence required on every gate |
| New regression test | `test_voice_acceptance_does_not_pass_a_backend_without_a_device` (backend + 0 devices -> `not-run`, status `incomplete`) |
| Linux evidence | `INCOMPLETE; gates=7; passed=4; failures=0; not-run=push_to_talk,stt_engine,tts_engine` |
| Simulated Windows evidence | `PASSED; gates=7; passed=7; failures=0`; host `microphone_backend=winmm`, `input_devices=1` |
| Suite | **1148 passed, 1 skipped** on Linux; on Windows the same suite reports 9 skips (symlink privilege, POSIX shell, live systemd/logind). `ruff` clean; `release audit` READY (33 checks) |

The Windows figure above is a **simulation** of that host class produced on Linux by
overriding only the hardware fields of the live status; the real Windows re-run is
still the user's to perform and is recorded as `pending_user_windows_evidence`.

### 0.82.2 - default entry point, RTL mirroring, desktop icons

| Item | Evidence |
|---|---|
| `pc-agent ui` / `pc-agent gui` open the Command Center | `test_command_center_launcher_opens_the_api_not_tk` (parser + source), `test_command_center_launcher_serves_and_stops` (real bounded server run, exits cleanly) |
| `pc-agent gui --tk` still reaches the Tk panel | same test asserts `tk=True` parses; the Tk suite (`test_phase5_gui.py`, 20 tests) is unchanged and green |
| Logical direction utilities only | `test_ui_sources_use_logical_direction_utilities` scans every `.tsx` for `ml-/mr-/pl-/pr-/border-l/border-r/text-left/text-right/left-N/right-N` |
| RTL in the real browser | `ui-verify.js` 54/54: `html dir` -> rtl, sidebar at the right edge (1354 px of 1600), content opposite it, no overflow, screenshot `07-dashboard-rtl.png` |
| `auto` follows the engine | harness reads `/v1/i18n/status` -> `data.direction` and compares it to `document.documentElement.dir` |
| Desktop shell is complete on the asset side | `test_desktop_shell_config_and_icons_are_real` (version match, 6 icon files with real magic bytes), `test_desktop_shell_grants_no_extra_capabilities` (`core:default`, no shell/fs/http plugin, single-instance present) |
| Suite | **1154 passed, 1 skipped** on Linux; `ruff` clean; `release audit` READY (33 checks) |

Tauri is still **not built or verified** (no Rust toolchain / WebView2 on this
machine); the icons and configuration are ready for the machine that can.

### 0.82.8 - EV / hardware-key signing, timestamp fallbacks, SmartScreen honesty

| Item | Evidence |
|---|---|
| Key storage is reported | certificate listing parses `ProviderName` + `HardwareDevice` into `hardware-token` / `software-store` / `unknown`, and counts `hardware_backed` |
| `--require-hardware` refuses a PFX | blocker names it "an exportable copy of that key"; `sign_bundle` returns early and never runs signtool |
| `--require-hardware` accepts a token | READY with a note naming the provider |
| `--require-hardware` refuses a software key | blocked with "check the token is plugged in and its middleware is running" |
| Unreadable provider is a note, not a pass | `unknown` storage keeps the plan READY and records why it could not tell |
| No key material anywhere | source contains no `Export-PfxCertificate`, no `certutil -exportPFX`, no `Export(`; test asserts it |
| Timestamp fallback order | `digicert` -> `sectigo` -> `globalsign`; a fake failure on the first is retried on the second and the report names the one that worked |
| All authorities failing | `failed`, detail says so, and every attempt is recorded |
| Custom authority | tried first, then the fallbacks |
| Reputation report | `not-measurable-here` + note; expected first-run text switches between unsigned / OV / EV from the certificate's own metadata |
| Unreadable signing report | reported as `unreadable`, not swallowed |
| CLI | `release signing --reputation [--json]` exits 1 while anything remains, and lists each blocker as `REMAINING:` |
| Docs | `docs/smartscreen_reputation.md` linked from `INSTALLATION_GUIDE.md` §12.2d and from `docs/authenticode_signing.md` |
| Suite | **1282 passed, 2 skipped** (Linux); `ruff` clean; release audit READY (33 checks) |
| Allow-list | **488** files |

Not verified: no certificate, no token and no `signtool.exe` exist on this
machine, so `release sign-bundle --check` and `release signing --reputation` report
`NOT READY` / `not-run` with the real reasons (measured), and the Win32 trust path
remains unexercised here.

### 0.82.7 - installer command + touch-friendly widget grid

| Item | Evidence |
|---|---|
| `release installer --check` runs nothing | test doubles the runner and fails the test if it is called; build/signing steps report `not-run` |
| Missing toolchain is named | `missing: cargo, rustc` + one hint per tool; build step `skip`; exit `1` |
| Unbuilt UI is a blocker with the fix | `frontendDist` resolving to a directory with no `index.html` -> `run: cd frontend; npm run build` |
| Build order | fake runner observed exactly `npm ci --no-audit --no-fund` then `npm run tauri:build` |
| A broken `npm ci` stops early | `tauri:build` recorded as `skip`; the npm error tail is kept |
| Timeout and missing-binary | `timed out after Ns` with partial output; `could not start` for a missing npm |
| Signing blocks when it cannot work | no artifacts -> step `fail` with the real plan blockers, `signing == {}` |
| Signing after a build | fake certificate + fake signtool -> 2 artifacts signed and verified, report `ok` |
| Report file | `data/installer-build.json` with steps/timings/artifacts; `write_report=False` writes nothing |
| API server tests are port-safe | the regression tests bind `api_port=0`, so a running `pc-agent api serve` cannot break them |
| Arrange mode in the browser | `ui-verify.js` **57/57**: opacity `0 → 1` without hover, control height `15px → 32px` (floor asserted at 30), mode survives a reload |
| Arrange mode is not a layout setting | `store/ui.ts` has no `arranging` field; `Reset layout` cannot toggle the mode |
| Contrast | `check_contrast.py` PASS in both themes (0 failing pairs) |
| Suite | **1252 passed, 2 skipped** (Linux); `ruff` clean; release audit READY (33 checks) |
| Allow-list | **485** files; `check_desktop_shell.py` READY |

Not verified: no installer was built here (no Rust/WebView2) and no certificate
was used. `pc-agent release installer --check` says so on this machine and the
docs repeat it.

### 0.82.6 - Windows suite regressions: two product bugs, one wrong assertion, one mixed tree

The 0.82.5 cycle ended with a real Windows run: **1161 passed, 9 skipped,
6 failed** in 1499 s. Nothing in the failures was a Linux/Windows feature gap -
they were a shutdown race, a silent failure mode, a POSIX literal, and a patched
install that was not one release.

| Failure (Windows) | Cause | Fix | Test |
|---|---|---|---|
| `test_app_window_argv_has_no_chrome_and_its_own_profile` | asserted `argv[0] == "/opt/chrome"`; `str(Path("/opt/chrome"))` is `\opt\chrome` on Windows | assertion is now platform-correct (`os.fspath`); code passes a **string** executable through verbatim instead of re-wrapping it in `Path` | `test_argv_keeps_the_executable_the_caller_gave`, `test_a_windows_path_string_is_passed_through_verbatim` |
| `test_cli_production_check_json_is_machine_readable` | a failing chain printed the reason only inside the JSON and exited 1 | every failing step is named on stderr (`Release chain step failed -> …`), stdout stays parseable; `copy2`/read are retried on WinError 5/32/33 | `test_json_report_stays_parseable_and_the_reason_goes_to_stderr`, `test_retry_survives_a_transient_windows_sharing_violation` |
| `test_readme_documents_the_hardening_entry_points`, `test_desktop_shell_config_and_icons_are_real`, `test_real_project_satisfies_baseline_document_and_dependency_contract`, `test_transformation_phase2_scope_and_documentation` | mixed tree: `pyproject.toml` = 0.82.4, `import pc_agent` = 0.82.3, `README.md` = 0.82.3 | version is now **0.82.6** everywhere; the patcher reads every file back and fails on a mismatch (`verified=480/480`); post-install steps print `pc_agent.__version__` **and** `__file__` so a stale `site-packages` copy is visible | `test_the_work_directory_prefix_is_short`, patcher `verified=` line in the install log |
| `PytestUnhandledThreadExceptionWarning` `[WinError 10038]` | `stop()` closed the socket while `serve_forever()` ran on the caller's thread | `stop()` shuts the loop down first when any thread is serving; the loop absorbs a socket error | `test_stop_while_serve_forever_runs_does_not_raise`, `test_stop_is_immediate_when_the_blocking_loop_never_started`, `test_a_socket_error_does_not_kill_the_serving_thread` |

Regression suite: `tests/test_windows_platform_regressions.py` - **14 passed,
1 skipped** (the Windows-only MAX_PATH check). Working tree: **1216 passed,
2 skipped**; `ruff` clean; release audit READY (33 checks); allow-list **480**.

Not claimed: the Windows suite has not been re-run against 0.82.6 from this
side. The four failing assertions are now platform-correct, so a Windows run is
the confirmation - it is a check, not an assumption.

### 0.82.5 - Authenticode installer runbook + configurable widget grid

Two things were missing and both are now executable here: signing the installer
you actually download, and letting the dashboard be rearranged (not just
reordered).

| Item | Evidence |
|---|---|
| Installer discovery | `discover_installer_artifacts()` returns only `.exe` inside `bundle/nsis/` and `.msi` inside `bundle/msi/`; a stray `notes.txt` and the app binary at `target/release/*.exe` are excluded (asserted) |
| Certificate listing (Windows) | `list_signing_certificates()` parses PowerShell `Cert:\CurrentUser\My` JSON into subject / issuer / thumbprint / validity / private-key; `usable` counts certificates that can actually sign |
| Certificate listing (Linux) | `pc-agent release signing --certificates` -> `not-run` + "the certificate store only exists on Windows", exit **1** - deliberately not an empty list |
| Preflight | `pc-agent release sign-bundle --check` -> `NOT READY`, exit 1, 3 blockers (no installer / no `signtool.exe` / no certificate), 2 notes (not Windows / timestamp URL); signs nothing (test asserts no `signtool` call) |
| Preflight negatives | short thumbprint -> "too short"; missing PFX -> "PFX file not found"; a full tree + signtool + thumbprint -> `READY` with 2 artefacts |
| Bundle signing | `sign_bundle()` signs both artefact types with `/sha1`, `/fd sha256`, `/tr` timestamp, then verifies each through the trust API; 2 signtool invocations asserted |
| Verification is not `signtool`'s exit code | a fake runner returning "Successfully signed" plus an untrusted verification result gives `ok=False`, `signed=0` |
| Nothing is half-signed | when the plan is blocked, `sign_bundle()` returns before running signtool (`results == []`) |
| Secret handling | PFX password only from `PC_AGENT_SIGN_PFX_PASSWORD` (missing -> exit **2**, message on stderr); `--allow-argv-password` is refused for bundle runs; no `BEGIN CERTIFICATE`/`EXPORT-PFX`/private-key code in the module (asserted) |
| Runbook shipped | `docs/authenticode_signing.md` (4 steps, blocker table, PFX path, what it does *not* do) linked from `INSTALLATION_GUIDE.md` §12.3 |
| Tests | `tests/test_installer_signing_runbook.py` - **18 tests**, all passing (fake runners, no Windows needed) |
| Widget resize | ⤢ toggles span; harness measured `325px -> 660px` and the value is persisted in `omni.ui.v1` |
| Widget hide/restore | ✕ removes it: `9 -> 8` widgets, still `8` after a full page reload; the **Widgets (8/9)** menu and **Reset layout** restore `9 of 9` |
| UI harness | `ui-verify.js` now **54/54** (51 + 3 new widget checks), including "no console errors", "no failed requests", "no cross-origin requests", contrast PASS in both themes |
| Contrast | `frontend/scripts/check_contrast.py` -> PASS, 0 failing pairs (dark and light) |
| Suite (working tree) | **1202 passed, 1 skipped** (Linux; the skip is the real-Tk-window test with no display); `ruff` clean; `release audit` READY (33 checks) |
| Allow-list | **479** files (new: `docs/authenticode_signing.md`, `tests/test_installer_signing_runbook.py`); `scripts/check_desktop_shell.py` -> `DESKTOP SHELL: READY` |

Not claimed: no installer was built, signed or verified on Windows here. The
signing code is tested against fake `signtool`/PowerShell output only; the real
`WinVerifyTrust` path needs the user's machine and certificate.

### 0.82.4 - desktop installer path: preflight + Windows CI

The Tauri binary still cannot be built in this sandbox (no Rust, no WebView2).
Everything around the build is now executable here, and the preflight found a
real defect that would have failed the first genuine build.

| Item | Evidence |
|---|---|
| Config defect found and fixed | `build.frontendDist` was `../src/pc_agent/webui`, which resolves to `frontend/src/pc_agent/webui` (does not exist). Now `../../src/pc_agent/webui`; `beforeBuildCommand` rebuilds the SPA |
| Preflight | `python scripts/check_desktop_shell.py` -> `DESKTOP SHELL: READY`, 18 checks, 1 warning (`cargo` absent here) |
| Negative case 1 | broken `frontendDist` in a copied tree -> exit 1, `NOT READY`, message names `frontendDist` |
| Negative case 2 | window URL changed to `https://example.com/` -> exit 1, "loopback origin" |
| Preflight robustness | missing `Cargo.toml`/`main.rs`/capabilities are reported as findings, not tracebacks |
| Windows CI | `.github/workflows/desktop-windows.yml`: preflight -> `npm ci` -> `npm run build` -> Rust -> `tauri build` -> list artefacts -> upload NSIS/MSI |
| CI does not sign | test asserts the workflow contains no `signtool`, no `PC_AGENT_SIGN_*`, no `secrets.` |
| Tests | `tests/test_desktop_shell_preflight.py` - 9 tests, all passing |
| Allow-list gap closed | `.github/` added to `release._INCLUDE_DIRS`; without it a patched install failed 9 tests (CI-workflow assertions + the `production_hardening` audit check) |
| Patched-install run | patcher applied into an empty folder -> `created=477`, `pip install -e ".[dev]"`, suite there: **1184 passed, 1 skipped**, `ruff` clean, `pc-agent --version` = 0.82.4 |
| Suite (working tree) | **1184 passed, 1 skipped** (Linux); `ruff` clean; `release audit` READY (33 checks) |

Unchanged claim: the installer has **not** been built or run. The docs say so, and
the preflight prints the exact commands for the machine that will.
