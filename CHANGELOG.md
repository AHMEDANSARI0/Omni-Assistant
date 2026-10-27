# Changelog

## 0.12.0 - PHASE 10 Verified Workflow Reuse

- Workflow memory now stores the exact reusable plan material (normalized
  instruction, validated arguments, external roots, expected pages) under a
  precise per-instruction key; repeats to fresh outputs grow the same
  workflow's verified count. Phase 7 legacy records stay usable through
  their last task's stored plan.
- New `pc-agent workflow list|show|run|delete` (exact key, unique prefix or
  `latest`). `workflow run` creates a new task and re-runs the stored plan
  only after live registry/allow-list revalidation, with fresh approvals
  (never inherited), acceptance criteria, checkpoints and bounded recovery.
- Planner fallback: when deterministic/AI planning cannot plan an
  instruction, a still-valid verified workflow for the same normalized
  instruction is reused instead of `needs_input`. Execution reports show
  `Verified workflow recorded/reused: wf-...` in CLI (text + JSON) and GUI.
- GUI Tasks tab gains **Run Verified Workflow...**. No new dependency or
  database migration (schema stays v6); 10 new tests including a forged
  workflow rejection and external-root approval re-check.

Still not included: fuzzy/semantic workflow matching or argument
re-binding, multi-step plans, secure OS credential storage, subprocess/Git
adapters, browser or Windows UI automation, or image tools.

## 0.11.0 - PHASE 9 Deeper Office

- PDF: `pdf.extract_pages` (validated `1-3,5` selections, order preserved)
  and `pdf.split` (one verified single-page PDF per page, max 200 pages,
  partial outputs cleaned on failure).
- DOCX: `office.docx_replace_text` (exact paragraph replacement that also
  works when Word split a word across runs) and
  `office.docx_append_paragraphs`, both writing new verified documents.
- XLSX: `office.xlsx_set_cells` writes values or `=formulas` into one sheet
  of an existing workbook (other package parts preserved, stale calcChain
  dropped) and `office.xlsx_verify_formulas` recomputes every formula with
  a bounded deterministic evaluator (arithmetic, ranges,
  SUM/AVERAGE/MIN/MAX/COUNT/PRODUCT/ABS/ROUND; circular/unsupported
  formulas reported explicitly). Computed cached values are written back
  so other readers see results immediately.
- Independent verification for all six tools, Phase 7 acceptance criteria
  for the writers (including PDF page-count expectations), checkpoints, and
  fresh-output collision recovery for the editors.
- Deterministic planner intents (`Extract pages...`, `Split...`,
  `Replace...`, `Append...`, `Set cell...`, `Verify formulas`) with new
  `--pages/--find/--replace/--cell/--value` run options; direct
  `pdf extract|split` and `office docx-replace|docx-append|xlsx-set|
  xlsx-formulas` CLI; new GUI Documents controls. 29 built-ins total, no
  new dependency, no database migration (schema stays v6). 11 new tests.

Still not included: cross-paragraph/header DOCX matches, XLSX styles/
charts/pivots creation, lookup/date/conditional formula functions, PDF
rotation or PDF text extraction, subprocess/Git adapters, browser or
Windows UI automation, image tools, or multi-step planning.

## 0.10.0 - PHASE 8 File Operations

- Five new workspace-confined tools (23 built-ins total): `files.copy`
  (streamed, hash-checked, exclusive-create), `files.move`, `files.rename`,
  `files.organize` (category/extension subfolders, conflicts skipped and
  reported), and `files.search` (name substring/wildcard + optional bounded
  text-content search; credential-like file contents are never searched).
  Nothing overwrites and there is no delete tool.
- New policy action `MOVE_FILE`: inside the workspace a move/rename is
  allowed like a write (bytes are preserved and hash-verified), while real
  deletes/overwrites remain approval-gated. No new dependency, no database
  migration (schema stays v6).
- Deterministic planner intents for copy/duplicate, move, rename,
  organize/tidy and search via `--target`/`--output`/quoted text; missing
  pieces become explicit `needs_input` prompts. File intents stay
  deterministic-only (AI plans for them are rejected by the allow-list).
- Independent verification for every new tool, Phase 7 acceptance criteria
  for transfers (including honestly-empty files), checkpoints on success,
  and bounded fresh-output recovery for copy collisions.
- New direct CLI `pc-agent files copy|move|rename|organize|search`, a GUI
  Documents > Files panel with confirmations, and doctor/GUI summaries now
  report the file-tool count. 13 new tests.

Still not included: file/folder deletion, folder copy/move, bulk glob
transfers, external-path transfers, DOCX/XLSX editing or formula
calculation, PDF split/extract, subprocess/Git adapters, browser or Windows
UI automation, image tools, multi-step planning, or automatic workflow
reuse.

## 0.9.0 - PHASE 7 Foundation (verification, checkpoints, cancellation, memory)

Personal-use foundation required by the master prompt. No new external tools
and no safety rule was weakened; licensing remains unchanged and off by
default.

- Generalized acceptance-criteria verification (`verification.py`): every
  attempt now checks tool verification plus output-artifact existence,
  independently recomputed SHA-256, PDF page counts, and CSV/JSON row
  recounts. A task succeeds only when all applicable criteria pass; each
  evaluation is persisted (`task_verifications`) and printed as
  `[PASS]`/`[FAIL]` evidence in CLI, `task show`, and the GUI.
- Durable step checkpoints (`task_checkpoints`) with a plan fingerprint and
  artifact validation, plus `pc-agent task resume` (and the GUI Resume
  button) that continues the same interrupted task in place, refuses
  recently-updated tasks unless `--force`, revalidates the stored plan and
  approvals, invalidates stale checkpoints, and always re-verifies.
- Cooperative cancellation (`task_cancellations`): `pc-agent task cancel`
  and the GUI Cancel button cancel queued tasks immediately and stop running
  tasks at the next stage boundary, including from another process and while
  the GUI worker is busy. CLI `Ctrl+C` prints resume/cancel guidance.
- Memory layer (`memory_items`, `pc-agent memory ...`): inspect/manage
  preferences and user notes, automatic verified-workflow records on task
  success, secret-like key refusal, explicit memory clear, and terminal
  task-history deletion (`task delete --yes`; workspace files untouched).
- SQLite schema v6 adds `task_checkpoints`, `task_cancellations`,
  `task_verifications` and `memory_items`; existing data is preserved. No
  new runtime dependency. 17 new tests incl. simulated crash + resume.

Still not included: multi-step planner, automatic workflow reuse, general
file copy/move/rename/organize tools, DOCX/XLSX editing or formula
calculation, PDF split/extract, subprocess/Git adapters, browser or Windows
UI automation, image tools, full core self-repair, or secure OS credential
storage.

## 0.8.0 - PHASE 6 Licensing and subscriptions (offline foundation)

- New offline licensing with Ed25519-signed license tokens, verified locally
  with the vendor public key (pure standard library; RFC 8032 test vectors).
  Plans (`trial`, `standard`, `pro`, `enterprise`) map to real feature gates;
  licenses may be subscription-style expiring, perpetual, seat-counted and
  optionally bound to a one-way machine fingerprint.
- Built-in trial (default 14 days, configurable 0-90) that only starts when
  enforcement is `warn` or `enforce`; moved-back system clocks fail closed.
- New customer CLI: `pc-agent license status|activate|deactivate|machine-id`.
  Activation verifies before atomically replacing the local license file and
  audits events with token fingerprints only. The GUI Dashboard gains a
  License panel with details, file-picker activation and deactivation.
- New vendor-only `pc-agent-license-admin` tool (`keygen`, `issue`) for
  signing keys and licenses; private keys never belong on customer machines.
- Enforcement is opt-in via `PC_AGENT_LICENSE_ENFORCEMENT` (`off` default,
  `warn`, `enforce`), so existing installs keep working unchanged. Read-only
  diagnostics such as `doctor` and license commands are never gated.
- `doctor` and `pc-agent gui --check` print a one-line license summary.
- SQLite schema v5 adds `license_state` and `license_events`; existing data
  is preserved. No new runtime dependency.

Still not included: payment processing, store checkout, online
activation/subscription-billing/revocation servers, cloud coordination,
Tool Builder lifecycle screens, a settings/API-key editor, screenshots,
keyboard/mouse or external-app automation, shell access, or a production
installer/shortcut.

## 0.7.0 - PHASE 5 Desktop GUI

- New standard-library Tkinter desktop GUI (`pc-agent gui` and the windowed
  `pc-agent-gui` launcher) with Dashboard, Tasks, Documents, Web, Desktop,
  Tools and Providers tabs.
- The GUI uses a headless controller over the same orchestrator, recovery
  service, provider router, registered tools, policy contexts and SQLite
  repositories as the CLI. Operations run one at a time in a worker thread;
  exact external read-only task approvals are decided in a GUI dialog.
- Tasks can be created/run, filtered, inspected, diagnosed and recovered
  from the GUI. Document/PDF actions remain workspace-bound; web fetch and
  desktop-open authority is created only by their dedicated tab actions.
- The Tools tab can invoke approved dynamic `custom.*` tools only; the
  Providers tab shows route status and an explicit Ask action without
  displaying or editing API keys.
- `doctor` reports desktop-GUI/Tkinter availability, and `pc-agent gui
  --check` verifies the controller and data without opening a window.
- Compact split layouts, scrollable task controls and document sub-tabs keep
  the window usable on smaller Windows displays.
- No new runtime dependency and no database migration; schema remains v4.

Still not included: Tool Builder lifecycle screens, a settings/API-key
editor, screenshots, keyboard/mouse or external-app automation, shell
access, licensing/subscriptions, cloud coordination or a production
installer/shortcut.

## 0.6.1 - PHASE 4.1 TLS and Recovery Hotfix

- Fixed Windows HTTPS failures such as `CERTIFICATE_VERIFY_FAILED: unable to
  get local issuer certificate` without disabling certificate verification.
  Web fetch and provider HTTP now share one verified TLS context combining
  Python/OpenSSL defaults, the Windows ROOT/CA server-auth stores when
  present, and Certifi's Mozilla CA bundle.
- Added `certifi` as the only new runtime dependency for that portable CA
  bundle. `doctor` now prints the loaded CA count and trust sources, and TLS
  failures produce a specific fail-closed diagnostic.
- Manual recovery no longer creates a duplicate clone when a source task
  already has a succeeded linked recovery task. Diagnosis now reports that
  state instead of proposing another recovered output.
- `tools list` now uses a wrapped multi-line layout so long tool descriptions
  remain readable and copy cleanly from narrower Windows terminals.
- No database migration; schema remains v4.

## 0.6.0 - PHASE 4 Office, Web Fetch and Desktop Tools

- Phase 3 usability hotfixes: task commands accept `latest`,
  `latest-failed` and `latest-<status>`; `task list --status` filters tasks;
  failed human-readable reports print exact copy-ready diagnose/recover
  commands; quoted angle-bracket placeholders produce a targeted explanation
  instead of a generic not-found error.
- Known corrupt demo/user PDFs still appear in structured `Skipped` results,
  but pypdf's duplicate low-level header/EOF warnings are suppressed outside
  DEBUG logging.
- New standard-library Office engine and seven registered tools for
  DOCX/XLSX/PPTX/CSV inspection/text extraction, CSV statistics, CSV↔JSON,
  CSV→XLSX and verified simple DOCX creation. OOXML/CSV parsing is bounded
  and rejects macro/legacy formats, encrypted or duplicate ZIP entries and
  `DOCTYPE` XML.
- New deterministic `demo office` data and direct `pc-agent office ...`
  commands. Safe local Office inspections/conversions and desktop system
  information also join the orchestrator's verified allow-list; Phase 3
  fresh-output and unique-workspace-input recovery now covers those plans.
- New dedicated `pc-agent web fetch` for one exact HTTP(S) URL: public-IP and
  redirect revalidation, no credentials/cookies/scripts, bounded
  time/bytes/content types, optional new workspace text output, and an
  explicit `--allow-private` direct-command exception. Generic tasks and AI
  plans remain network-denied.
- New dedicated desktop tools/commands for system information and opening one
  exact workspace file/folder or HTTP(S) URL in the default app. Executable,
  installer, script and shortcut targets are blocked; there is no shell,
  screenshot, clicking or typing capability.
- New bounded settings `PC_AGENT_WEB_TIMEOUT_SECONDS` and
  `PC_AGENT_WEB_MAX_BYTES`; `doctor` reports Phase 4 tool/web-limit status.
- No new runtime dependency and no database migration; schema remains v4.

Still not included: full browser DOM/search/form automation, desktop GUI or
keyboard/mouse control, editing existing Office files in place, dynamic
file/network/shell/browser/desktop tools, licensing/subscriptions, cloud
coordination, autonomous core-source repair, or a kernel/VM sandbox.

## 0.5.0 - PHASE 3 Self-Healing + Recovery

- Phase 2 hotfix: relative dynamic-tool `--arguments-file` paths now resolve
  from the current folder, app root, then workspace; missing arbitrary files
  report every searched path and invalid UTF-8 is reported cleanly.
- The known bundled `word_count_arguments.json` smoke file has an embedded
  fallback, so that documented invocation still works if the project example
  is missing; `doctor` reports the missing bundled file.
- `tool-builder approve --yes` now prints the post-approval approved/enabled
  snapshot instead of the stale pre-approval state, and tool descriptions are
  whitespace-normalized to one line in list/show output.
- Ruff now excludes generated `.patcher-backups`, so a root-level
  `python -m ruff check` validates the live project rather than old patch
  snapshots after a fresh patcher chain.
- SQLite migration v4: `recovery_attempts` checkpoints (including before/after
  plan JSON) and `task_recovery_links`, preserving all Phase 2 data.
- Deterministic diagnosis for output collisions, uniquely correctable missing
  workspace inputs, merge verification failures, transient lock/timeout-style
  failures, interrupted tasks, and non-retryable policy/approval/invalid
  failures.
- Bounded orchestrator recovery through `RETRYING`: default two healing
  retries, hard cap three, bounded backoff, and deterministic verification
  after every retry. Existing outputs are never overwritten.
- Revised external-read plans require fresh exact approval; corrected input
  substitution is disabled for external scopes.
- CLI: `--max-recovery-attempts`, `--no-recovery`, `task diagnose`,
  `task recover [--force]`, and `recovery scan|list|show`.
- Manual recovery resumes queued/needs-input/waiting tasks or creates a
  linked clone for failed/interrupted tasks. Invalid stored plans are
  reported and safely replanned; manual chains are capped at three clones.
- `doctor` and `recovery scan` report recorded attempts, stale active tasks
  and old temporary PDF artifacts. Scanning never deletes files.

Still not included: recovery for direct one-shot `pdf`/`tools` commands,
autonomous modification/repair of core source code, dynamic
file/network/shell/browser/desktop tools, GUI, licensing/subscriptions, cloud
coordination, or a kernel/VM sandbox.

## 0.4.0 - PHASE 2 Dynamic Tool Builder

- Dynamic `custom.*` candidates from reviewed offline templates, imported JSON
  proposals, or strict-JSON output from a configured real provider.
- Pure-compute security boundary: no candidate-declared filesystem, network,
  shell, desktop or pip-dependency permission is accepted.
- Strict proposal and schema-definition validation, exact dependency matching,
  Python AST allow-listing, immutable source hashes and provenance.
- Fresh restricted Python subprocess for candidate tests and approved runtime
  invocation, with JSON size limits, a minimal environment, restricted
  builtins/imports and hard timeouts. This is subprocess isolation, not an
  OS/VM sandbox guarantee.
- SQLite migration v3 for candidates, test runs and lifecycle events.
- Lifecycle: proposed -> tested/test_failed -> explicit approval, rejection,
  enable/disable, newer-version supersession and rollback to a tested prior
  version.
- Bounded provider repair: maximum three attempts per chain, new version only,
  with parent schemas and expected outputs preserved.
- CLI: `tool-builder suggest|templates|template|import|generate|list|search|show|test|approve|reject|repair`
  and `tools show|invoke|enable|disable|rollback`.
- Offline templates: word count, slugify, percentage and temperature conversion.

Still not included: dynamic file/network/shell/browser/desktop tools,
autonomous core self-healing, GUI, licensing/subscriptions, cloud
coordination, or a kernel/VM sandbox. Real-provider generation/repair requires
the user's configured provider account; automated tests cover it with a fake
strict-JSON provider.

## 0.3.0 - PHASE 1C Providers + Orchestration

- Provider abstraction with Gemini, OpenRouter, OpenAI/generic
  OpenAI-compatible, Ollama and offline mock adapters.
- Bounded retries and fallback only through the user-configured provider route.
- CLI: `providers list`, `ask`, `run`, and `task run`.
- Deterministic-first planner for PDF merge/verify/inspect and file listing;
  optional `--use-ai` planning is allow-list and schema validated.
- Stored task orchestration through planning, approval, one registered tool,
  deterministic verification and final succeeded/failed/cancelled states.
- Exact, task-specific read-only approvals for selected external input paths;
  outputs remain workspace-only and sensitive paths remain denied.
- SQLite migration v2: `task_plans`, `approval_records`, `tool_invocations`.
- Package metadata version corrected/aligned to 0.3.0.

Still not included: direct Anthropic adapter, dynamic tool creation,
self-healing, GUI, licensing/subscriptions, or cloud coordination.

## 0.2.0 - PHASE 1B Tools + PDF

- Versioned built-in tool registry with strict schemas and structured results.
- Workspace tools: list files, read text, write-new text (never overwrites).
- PDF tools: inspect, verify, explicit merge, folder merge (via pypdf).
- Merged PDFs are re-opened and page-count verified; sources are preserved.
- Folder merge reports corrupt/encrypted files as skipped with reasons.
- CLI: `tools list`, `demo pdf`, `pdf inspect|verify|merge|merge-folders`.
- Tool catalog and Phase 1B limitations documented in `docs/tools.md`.

Still not included: AI providers/model router and natural-language task
execution - that integration is PHASE 1C.

## 0.1.0 - PHASE 1A Foundation

- Project scaffold (src layout, pyproject, .gitignore, .env.example).
- Environment-based configuration with validation; API keys never printed.
- Task state machine with enforced transitions (success requires VERIFYING).
- SQLite storage with migration v1: `tasks`, `task_events`, `user_preferences`.
- Deterministic risk-based policy engine and workspace path-boundary checks.
- CLI: `doctor`, `task create/list/show` (local task records only - no execution yet).
- Test suite covering config, state transitions, policy/paths, storage, CLI.

Not included yet: tools/PDF merge (1B), AI providers and task execution (1C).
