# Architecture

This document freezes the architecture boundary for Transformation Phase 1.
Detailed historical contracts remain in `docs/architecture.md` and the phase
documents. This root document records what is canonical now and which
boundaries later transformation phases must preserve.

## Canonical baseline

- Audited GitHub comparison point: `main` at
  `89610f3d18f9348b6fe8ead679bab81ee34155df` (Phase 58, 0.60.0).
- Canonical implementation source after reconciliation: the delivered
  Phase 60 tree (0.62.0), including Phase 59 and Phase 60.
- Transformation Phase 1 application version: 0.63.0.
- Transformation Phase 2 (Safety Kernel) application version: 0.64.0.
- Transformation Phase 3 (Local Service and API) application version: 0.65.0.
- Transformation Phase 4 (Mission Center) application version: 0.66.0.
- Transformation Phase 5 (JARVIS Command Center) application version: 0.67.0.
- Transformation Phase 6 (Live Monitoring) application version: 0.68.0.
- Transformation Phase 7 (Provider Platform) application version: 0.69.0.
- Transformation Phase 8 (Memory and Knowledge) application version: 0.70.0.
- Transformation Phase 9 (Multi-Agent Studio) application version: 0.71.0.
- Transformation Phase 13 (Connectors) application version: 0.75.1.
- Transformation Phase 14 (Proactive Intelligence) application version: 0.76.0.
- Transformation Phase 15 (Plugin Ecosystem) application version: 0.77.0.
- Transformation Phase 16 (Encrypted Device Sync) application version: 0.78.0.
- Transformation Phase 17 (Accessibility and Multilingual UX) application version: 0.80.1.
- Windows acceptance for this exact build remains pending real Windows
  evidence. Linux test success is not a Windows platform claim.

## Current logical layers

1. **Presentation**: `pc_agent.cli`, `pc_agent.gui.app`.
2. **Application facades**: `pc_agent.service` (the Local Service the
   loopback HTTP API fronts), `pc_agent.gui.controller`, CLI command
   handlers, diagnostics and release operations.
3. **Orchestration**: deterministic/AI planning, orchestration, verification,
   budgets and recovery.
4. **Tools**: schema-validated built-in registry, workspace policy and the
   restricted dynamic-tool subsystem.
5. **Providers**: synchronous provider contracts, adapters and fallback router.
   Providers propose text/JSON only; they never receive tool handles or
   approval authority.
6. **Storage**: SQLite schema v11 repositories for tasks, plans, approvals,
   Approval Center requests, invocations, checkpoints, recovery, licenses,
   memory (v2: model-tagged embeddings + tags), knowledge documents/chunks,
   events, missions/schedules, agent definitions/runs and dynamic tools.
7. **Platform adapters**: browser CDP, Windows UI Automation, OS credential
   stores, signed updates, packaging and operating-system schedulers.

## Safety Kernel boundary (Transformation Phase 2)

Orchestration never calls the tool registry directly. `pc_agent.safety`
sits between the orchestrator and the registry: it enforces hard tool
deadlines, runs data-context tools in the `pc_agent.safety_worker`
subprocess (killed at the deadline), keeps factory-bound browser/desktop/
vision tools on a bounded in-process thread path, and honours the
file-backed emergency stop. Approval decisions for non-interactive runs
flow through the digest-bound Approval Center (`approval_center_requests`)
and are consumed exactly once. The worker is a separate process running
with user privileges, not an OS sandbox.

## Memory and knowledge boundary (Transformation Phase 8)

`pc_agent.knowledge.KnowledgeService` owns ingestion, chunking, hybrid
search and grounded answers over two stores: Memory v2 notes and the
knowledge base. Ingestion is explicit-only and reuses the proven PDF/Office
extractors; it never runs OCR and never indexes in the background.
`pc_agent.embeddings` provides an offline deterministic hashing embedder
(default) plus optional OpenAI-compatible and Ollama backends. Vectors are
stored packed-float32 with their producing model tag and are only compared
within the same tag. Grounded answers retrieve first, then make one normal
metered router completion with sources labelled as untrusted data; agents
get ingest/search tools only, never an ask tool.

## Multi-agent boundary (Transformation Phase 9)

`pc_agent.agents.AgentStudio` turns named specialists into ordinary tasks
whose orchestrators receive a filtered `ToolRegistry` (the agent's builtin
allow-list only). Restriction is therefore enforced identically by the AI
planner catalog, deterministic plans, the Safety Kernel, and verification.
Team runs reuse the Mission Center level computation with per-branch
orchestrators; the coordinator only proposes assignments that local
validation may reject wholesale. Delegation depth is one and no agent
surface is reachable from a tool.

## Trust boundaries

- Model output, web pages, browser content, imported workflows and dynamic
  tool source are untrusted until locally validated.
- The local same-user operating-system account is currently the principal
  trust boundary. Local SQLite data is not encrypted at rest; database
  backups can be passphrase-encrypted since Transformation Phase 2.
- High-impact gates default off. Designated browser, desktop, command,
  Git-write and vision actions require exact approval in the orchestrated
  path.
- Signed update artifacts are verified locally; TLS and release-key custody
  remain operator responsibilities.
- The vendor update-feed host is not the future application API.

## Frozen decisions for later phases

- Preserve deterministic local execution whenever a safe deterministic plan
  exists.
- Do not claim hard tool timeout, OS sandboxing, Windows acceptance or
  provider capability until implemented and tested at that boundary.
- Introduce the isolated worker and hard cancellation before parallel or
  multi-agent side effects. (Done in Transformation Phase 2 for the
  sequential executor; parallel execution must reuse the same kernel.)
- Introduce a versioned local service/API before a web or multi-process UI.
- Add semantic memory only with provenance, user review and deletion controls.
- Keep cloud/device features optional and local-first.
- Do not build SaaS tenancy or billing into the personal local core during
  the 18-phase transformation; preserve replaceable interfaces only.

## Transformation Phase 15 additions (0.77.0)

Third-party code is the only part of the product we did not write, so it gets
its own layer, its own gate and its own process.

```
CLI / API / GUI / web  ->  LocalService  ->  PluginService
                                                 |        |
                                          PluginStore      sandbox.run_sandboxed()
                                          (schema v17)             |
                                                          worker.py (python -I -B)
                                                          audit hook + rlimits/job object
```

- `PluginService` is the only place a bundle is accepted and the only place a
  plugin tool is executed. Verification is *inside* the install path, not a
  separate "check" a caller could skip.
- `manifest.py` owns the contract, `store.py` owns SQL, `sandbox.py` owns OS
  limits, `worker.py` owns the child process. None of them decide policy.
- The worker imports **only** the plugin's own entry file; it never imports
  `pc_agent`, so plugin code cannot reach the service, the database or the
  provider router even in-process.
- Capabilities are enforced twice: at install (refused outright if this build
  cannot grant them) and at run time (audit hook denies the syscall-level
  event). Defence in depth means a bug in one layer is not sufficient.
- Plugin side effects reach the rest of the product only as `plugin.*` events
  on the Phase 3 event log.

## Transformation Phase 14 additions (0.76.0)

Time-driven behaviour is a separate layer above the service facade. It reads
other layers; nothing reads it.

```
scheduler loop (api_server)  ->  LocalService.proactive_tick()
CLI / HTTP / GUI / web       ->  LocalService  ->  ProactiveEngine
                                                        |        |
                                                 ProactiveStore  connectors / flows / tasks / monitor
                                                 (schema v16)    (read-only, each section degrades)
```

- `ProactiveEngine` owns policy; `ProactiveStore` owns SQL. The engine never
  builds SQL strings outside the store, and the store never decides policy.
- The clock is injectable (`clock=` on the constructor) because every policy
  rule - quiet hours, rolling hour cap, cooldown, one-briefing-per-local-date -
  is otherwise untestable and would silently become wall-clock behaviour.
- Delivery is layered and each layer only ever *narrows*: quiet hours, then the
  rolling hourly cap, then the per-key cooldown, then the in-app event, then the
  optional outbound call. A refusal at any layer is reported as a deferral
  reason and never deletes the in-app record.
- Briefing sections ask the *existing* services (`ConnectorService`, `FlowStore`,
  task ledger, `SystemMonitor`) for current state. No section invents a number,
  and a missing subsystem produces an explicit "off"/"unavailable" line.
- Side effects reach the rest of the product only as `proactive.*` events on the
  Phase 3 event log, which is the same channel Phase 12 triggers and the live
  monitoring UI already consume.

## Transformation Phase 13 additions (0.75.1)

A new `connectors` layer sits beside the tools, not inside them. The dependency
direction is one-way and explicit:

```
CLI / API / GUI / web  ->  LocalService  ->  ConnectorService  ->  connector class
                                                   |                    |
                                            ConnectorStore        stdlib only
                                         (schema v15 ledger)   (imaplib, smtplib,
                                                                urllib, hashlib, hmac)
```

- `ConnectorService` is the **only** gate keeper and the **only** writer of the
  connector ledger. Every method re-reads the live configuration, so a closed
  gate is an actionable error rather than a silent no-op.
- Connector classes are transport adapters with no database or settings access:
  `CalendarConnector` (files), `DriveConnector` (filesystem walk), `EmailConnector`
  (IMAP/SMTP with injectable factories), `ChatConnector` (signed HTTPS POST with
  an injectable transport and resolver) and `WebhookConnector` (pure
  verification).
- `ConnectorStore` owns `connector_events`, `connector_deliveries` and
  `connector_state`. Rows are bounded and payloads are sanitised before they are
  stored, so the ledger cannot become an injection surface.
- Application events (`connector.*`) go through the existing Phase 3 event log,
  which is what Phase 12 event triggers and the Phase 14 proactive engine read.
- Configuration is read-only from the environment: connectors never write
  secrets anywhere, and `Settings.public_summary()` exposes masked labels only.

## Transformation Phase 12 additions (0.74.0)

```
gui/app.py (14th tab)  web_ui.py (panel)  cli.py (flows)  api_server.py (18 routes)
        \                    |                  |                  /
         \                   v                  v                 /
          +---------------> LocalService (22 flow methods) <------+
                                    |
              +---------------------+----------------------+
              v                                            v
      flows.FlowStore / FlowRunner                 Mission Center
   (schema v14: definitions, versions,        (missions, mission_tasks,
    triggers, runs)                            dependencies, approvals)
              |
              +--> flows.validate_definition (strict, bounded physics)
              +--> flows.export_payload / load_export_payload (checksum)
```

**Layering rule (unchanged, now also enforced for flows).** A flow is *data*, not
an executor. `flows.py` may only: validate a document, persist it, compile it into
`task_specs` and call the injected `start_mission` callable. It never imports a
tool module, never touches the network, and never bypasses `MissionRunner`, so
approvals, per-task timeouts, token budgets, verification and recovery are
identical to a hand-written mission.

**Compilation contract.** `FlowDefinition.mission_specs()` yields
`[{"key": node_id, "instruction": <substituted>, "depends_on": [...]}]` in
document order; `MissionRunner` then derives the parallel levels itself. Node ids
are slugs, so they are safe as task keys, log fields and event payload values.

**Trigger evaluation.** `FlowRunner.tick(now=...)` is the only place time and
events are read: due `schedule` triggers are selected by `next_run_at <= now`,
`event` triggers by `app_events.id > last_event_id AND event_type = ?`. Each
trigger fires at most once per tick, the tick has a hard budget, and
`mark_trigger_fired()` advances the watermark/next-run *even when the flow was
skipped*, so a broken flow can never build a backlog storm. `reconcile()` asks
Mission Center for the state of each `started` run instead of trusting a timer.

**Portability.** `export_payload()` produces
`{flow_schema, exported_by, flow, triggers, checksum}`; the checksum is SHA-256
over the canonical JSON of `{flow, triggers}`. `load_export_payload()` refuses
unknown keys, oversized documents and any document whose checksum no longer
matches, so a shared flow file is either intact or rejected.

## Transformation Phase 11 additions (0.73.0)

```
pc_agent/
├── perception.py                  # offline CV: analyse_image, locate_template,
│                                  #   image_crop, read_image_file, region_hint
├── browser_cdp.py                 # + PageSession.wait_for / _wait_probe_js,
│                                  #   BrowserClient.activate_tab / close_tab
├── desktop_automation.py          # + WindowStateChange, set_window_state
├── tools/perception_tools.py      # vision.screen, vision.locate
├── tools/browser_tools.py         # + browser.wait, browser.tabs
├── tools/desktop_automation_tools.py  # + desktop.window_state
└── flows.py                       # FlowDefinition, FlowStore, FlowRunner,
                                   #   validate_definition, export/import + checksum
└── storage/                       # schema v14: flow_definitions, flow_versions,
                                   #   flow_triggers, flow_runs (+6 indexes); v13 kept
                                   #   automation_runs; FlowStore, AutomationRepository
```

Layer placement, unchanged boundaries:

* **Tools** stay the only execution contract. The five new tools are ordinary
  registered tools with strict input *and* output schemas, so the planner, the
  Safety Kernel, the approval model and the agent registry apply to them exactly
  as before (`69` built-ins).
* **Perception** is a pure library: bytes in, measured facts out. It opens no
  socket, spawns no process and writes nothing; file access stays inside the
  workspace and is size-capped.
* **Auditing** is injected, not imported: tools call
  `ToolContext.automation_recorder(payload)` when the caller provided one, and
  `LocalService`/`GuiController`/CLI wire that to `AutomationRepository`. A
  missing recorder degrades to "no ledger", never to "no action".
* **Surfaces** (CLI `automation`, HTTP `/v1/automation|vision|browser|desktop`,
  Tk Automation tab, web panel, diagnostics, doctor) all call the same service
  methods; no surface re-implements a probe or a decision.

Data flow for a vision request:

```
CLI/API/GUI -> LocalService.vision_screen -> ToolRegistry.invoke("vision.screen")
            -> perception.analyse_image -> ImageFacts -> tool output schema
            -> automation_recorder -> AutomationRepository.record -> automation_runs
```


## Transformation Phase 16 additions (0.78.0)

Device sync is a *transport-free* layer: nothing in it opens a socket. The only
thing that moves data is the operating system copying a file into a folder the user
already syncs, so the module can be reasoned about as pure crypto plus a ledger.

```
CLI / API / GUI / web  ->  LocalService  ->  SyncService
                                                |        |
                                         SyncStore        crypto.seal_bundle / open_bundle
                                         (schema v18)             |
                                                          data/sync/identity.json (0600)
                                                          Ed25519 sign + X25519 encrypt
```

- `SyncService` is the only gate keeper: every entry point calls `_require()`
  (`PC_AGENT_ALLOW_SYNC`) first, and `import_file()` performs the 15-step refusal
  chain in order - size, JSON, replay id, trusted peer + key match, Ed25519
  signature, clock skew, recipient match, AES-GCM open, payload schema/device,
  collection subset, record cap, request cap/kind, sequence - before applying
  anything. A payload is never partially applied ahead of a refusal that can be
  decided up front.
- `crypto.py` has no state and no I/O beyond loading/creating the identity file, so
  it is directly unit-testable and reusable if a relay transport is added later.
- Apply functions call the *public* domain APIs (`ProactiveEngine.add_reminder`,
  `FlowStore.create/update`, `save_profile`), which means every synced record passes
  the same validation as a locally created one - there is no second, weaker path.
- Peers are data, never code: `sync_requests` holds three request kinds that a human
  must accept, and accepting routes into the existing reminder/notification policy.
- `tick()` is the only folder-scanning entry point; it is bounded by retention days,
  skips already-processed ids and reports refusals instead of failing the whole run.

### Storage

`sync_devices` (trusted peers, status `paired`/`paused`, fingerprint, last seen),
`sync_bundles` (both directions, id, peer, seq, record count, size, path, detail),
`sync_requests` (peer coordination ledger with `pending|accepted|denied`),
`sync_state` (per-device export/import sequence + high-water marks). Tables are
capped at 400 bundles and 200 requests.

## Transformation Phase 17 additions (0.80.1)

Presentation is a layer, not a cross-cutting concern: nothing in the domain knows
about languages or colours.

```
CLI / API / GUI / web  ->  LocalService  ->  a11y.a11y_status()   (measured report)
                                       ->  i18n.Translator        (interface text)
                                       ->  ui_prefs               (data/ui.json)
```

- `i18n` is a pure lookup: a catalog per locale, a translator per request, no I/O
  and no provider call. Only interface keys exist here; user content is never
  translated.
- `a11y` is pure computation over 9 palette constants plus the settings, so a
  contrast number can be recomputed by any caller (and by the tests) without
  trusting a stored value.
- `ui_prefs` is a six-key JSON file in the data directory, read during
  `Settings.from_env` with **environment > file > default** precedence. No schema
  change, no migration, and a corrupt file degrades to defaults.
- Surfaces resolve the settings once (`resolve_ui`) and apply them: the GUI scales
  its fonts and mirrors its header, the web page sets `lang`/`dir` and toggles
  classes, the CLI prints text only.

## Release and hardening layer (Transformation Phase 18)

```
release.py         allow-list, portable ZIP + SHA-256 manifest, verify-without-extract,
                   audit_install() -> 32 read-only readiness checks
production_check.py the real chain: catalog > build > verify > tamper > sign >
                   stage > apply > rollback > cleanup, against a throw-away copy
updates.py         Ed25519 detached signatures, staging receipts, atomic apply,
                   verified rollback, OS-scheduled background upgrades
.github/workflows/ci.yml   ubuntu + windows x py3.11 + py3.13, plus a11y/perf and
                   a tag-only packaging job; contents: read, no secrets
```

Design rules for this layer:

1. **Verification never extracts.** A hostile entry can be inspected safely.
2. **Evidence is timestamped and stored.** `data/production-check.json` is what the
   audit check, the doctor line and the dashboard read - so "never ran" and
   "passed" are different answers.
3. **Self-tests never use production keys.** The chain generates an ephemeral
   key and deletes it with the work directory.
4. **The live install is never the test subject.** Apply/rollback run against a
   copy; tree hashes prove both directions.
