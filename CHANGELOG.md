# Changelog

## 0.18.1 - PHASE 16.1 Credential Manager Hotfix

- **Simulated Win32 `ERROR_NOT_FOUND` on real Windows**: the credential
  backend checked `ctypes.get_last_error()` before an injected library's
  own `last_error`. A simulated `CredReadW` miss therefore appeared as
  Win32 error 0 on Windows (but passed on Linux, where ctypes has no
  `get_last_error`). Injected test doubles are now authoritative; the
  real uninjected `advapi32` path still uses ctypes' captured last error.
- **Live GUI tab count**: the Windows-only GUI construction test still
  expected 7 notebook tabs after Phase 15 added Images. It now expects 8.
- No production feature, schema, dependency or credential-format change.
  Automated suite: 335 passed, 1 skipped; Ruff clean.

## 0.18.0 - PHASE 16 Windows Credential Manager

- **Secure provider-key storage**: one Windows generic credential per
  provider slot, accessed through native `CredReadW`, `CredWriteW`,
  `CredDeleteW` and `CredFree` calls in `advapi32.dll`. No new runtime
  dependency is added.
- **Non-breaking resolution**: environment/`.env` keys still win. When a
  key is absent there, production Settings loads fall back to Windows
  Credential Manager according to `PC_AGENT_CREDENTIAL_STORE=auto|windows|env`.
- **Dedicated CLI**: `pc-agent credentials status|list|set|delete|migrate`.
  `set` uses a hidden double prompt and accepts no secret argument;
  `migrate` copies existing environment keys after approval but never
  edits `.env`; status output contains provider names/sources only.
- **Doctor/GUI visibility**: backend availability, stored provider names
  and effective credential sources are shown without values. No task or
  AI tool can read credentials, and there is no credential `get/show`
  command.
- **Test isolation**: the suite defaults to `PC_AGENT_CREDENTIAL_STORE=env`
  and exercises OS storage through injected in-memory/simulated Win32
  backends, never a developer's real vault. Automated suite: 334 passed,
  1 skipped; Ruff clean.

## 0.17.0 - PHASE 15 Image Tools + Vision

- **Five new image tools**: `image.inspect`, `image.convert`,
  `image.transform`, `image.compare` and `image.analyze`; the built-in
  count grows from 49 to 54.
- **Local Pillow engine**: PNG/JPEG/GIF/BMP/WebP/TIFF input inspection;
  PNG/JPEG/WebP/BMP/TIFF outputs; crop/resize/thumbnail/rotate/flip/
  grayscale; 20 MB, 10,000 px and 40 megapixel caps; EXIF orientation
  normalization; metadata-free, never-overwrite outputs.
- **Provider vision**: `CompletionRequest` now carries bounded image input.
  Gemini receives an inline Interactions image part,
  OpenRouter/OpenAI-compatible providers receive data URLs, and Ollama
  receives its native base64 `images` array. The offline mock refuses
  image requests, and routing skips providers that do not declare vision
  capability.
- **New default-off settings**: `PC_AGENT_ALLOW_VISION` and
  `PC_AGENT_VISION_MAX_IMAGE_BYTES`. Every orchestrated or direct vision
  call requires approval for the exact image/question before bytes leave
  the PC. AI planning may propose only local inspect/compare; vision is
  excluded from automatic recovery retries.
- **Surfaces**: `pc-agent image inspect|convert|transform|compare|analyze`,
  task `--image-*` options, doctor diagnostics, and a GUI Images tab.
- **Verification**: local outputs are independently re-inspected. Vision
  answers are bound to a re-inspection of the exact approved image without
  sending it a second time. Automated suite: 325 passed, 1 skipped; Ruff
  clean.

## 0.16.0 - PHASE 14 Windows Desktop UI Automation

- **Seven new desktop tools**: `desktop.windows`, `desktop.window_inspect`,
  `desktop.window_focus`, `desktop.window_click`, `desktop.window_type`,
  `desktop.send_keys` and `desktop.screenshot`; the built-in count grows
  from 42 to 49.
- **Native Windows backend**: Win32 enumerates/foregrounds windows and
  captures screenshots; Microsoft UI Automation control patterns inspect,
  click and set exact elements. Windows installs now also depend on
  `comtypes>=1.4,<2`; that dependency is skipped on macOS/Linux.
- **New default-off settings**: `PC_AGENT_ALLOW_DESKTOP_AUTOMATION` and
  `PC_AGENT_DESKTOP_AUTOMATION_TIMEOUT_SECONDS`.
- **Direct and task surfaces**: `pc-agent desktop
  windows|inspect|focus|click|type|keys|screenshot`, deterministic task
  options, doctor diagnostics, and GUI Desktop-tab windows/inspect/
  screenshot controls.
- **Safety**: focus/click/type/keys require a fresh approval bound to exact
  arguments. Windows/elements must resolve exactly; password elements,
  agent self-targeting, unreadable editors and dangerous key chords are
  refused. Typing is read back. All desktop automation tools are excluded
  from automatic recovery retries, and AI planning may propose only the two
  read-only desktop tools.
- **Verification**: automated suite 313 passed, 1 skipped; Ruff clean.
  Desktop tests use a simulated UIA backend because the build sandbox is
  Linux, so `pc-agent desktop windows` on the target Windows machine is the
  first live native confirmation.

## 0.15.1 - PHASE 13.1 Gemini Routing Fix

- **Gemini could never be routed**: config parsing lowercases provider
  names (`gemini`), but the router factory/status compared against
  `"Gemini"`, so a configured Gemini key was silently skipped and
  `providers list` showed it as an unknown, reserved slot. Names are
  now canonical lowercase end-to-end (factory, model lookup, status,
  adapter name), with a regression test that builds the route from
  settings. Found on a real Windows install whose `.env` had a live
  provider order.
- Tests are hermetic again: three CLI tests that invoked `main()`
  without `--root` now run against a temp root, so a developer's real
  `.env` (provider keys, gates) cannot leak into offline tests.

## 0.15.0 - PHASE 13 Browser Automation

- The agent can now drive a **real browser session you start yourself**
  (Edge/Chrome with `--remote-debugging-port` and a throwaway profile)
  through a standard-library Chromium DevTools client — off until
  `PC_AGENT_ALLOW_BROWSER=1`, localhost-only. Six tools (42 built-ins):
  `browser.status`, `browser.snapshot` (read-only), `browser.open`,
  `browser.click` (unique selector/exact text; refuses to guess),
  `browser.type` (framework-safe fill with read-back; password/file
  fields refused) and `browser.screenshot` (new verified workspace
  PNGs). Actions need fresh per-argument approvals and are never
  auto-retried; AI planning may propose only the two read-only tools.
- New direct `pc-agent browser ...` commands (actions confirm unless
  `--yes`; `--allow-private` for one exact local open), `run`
  `--browser-open/--browser-click-*/--browser-type-*` options,
  deterministic browser intents, GUI Web-tab status/page-read buttons,
  and `doctor` reports the gate plus live connectivity.
- Verified against a real Chromium 131 (local test pages + Wikipedia:
  open/click/type/screenshot, password refusal, approval and
  read-back verification) plus 14 deterministic fake-CDP/WebSocket
  tests. No new dependency; schema stays v6.

Still not included: launching/stopping the browser for you, downloads
or uploads, tab management, login automation (password typing stays
refused by design), Windows UI automation of other apps, image
analysis, or secure OS credential storage.

## 0.14.3 - PHASE 12.3 One-Key Confirmations

- Interactive-terminal confirmations now read a **single keypress**
  (`Y` approves, `N`/Ctrl+C cancels, no Enter). Line-based `input()`
  proved unreliable under Windows Terminal (ConPTY could feed phantom
  empty lines), so terminals use `msvcrt` (Windows) / cbreak reads
  (POSIX), while piped stdin keeps the typed-word prompt from 12.2.
- Paste bursts cannot approve: keys already queued before the prompt
  are drained via `msvcrt` (replacing the ConPTY-unfriendly
  console-buffer approach), and a `Y` with more text queued behind it
  (e.g. a pasted `--yes`) is discarded with a note. A trailing Enter
  after a lone `Y` is tolerated; stray Enter presses show a hint
  instead of cancelling.
- 6 additional prompt tests (19 in the prompt-fix file) plus live
  terminal (PTY) verification of the paste, Enter-mashing and
  pasted-`yes` scenarios. No dependency or schema change.

## 0.14.2 - PHASE 12.2 Approval Prompt Robustness

- Confirmation prompts no longer deny instantly on an empty or wrong
  answer. An empty line (typically the leftover newline of a pasted
  command) re-asks; a wrong non-empty answer is shown back and re-asked
  up to 3 times; Ctrl+C cancels cleanly without a traceback. Only the
  exact word (`APPROVE`, or case-sensitive `DEACTIVATE` for license
  removal) ever confirms, so re-asking grants nothing.
- The Windows console-input flush now uses fully declared ctypes
  signatures (`HANDLE` return type, typed arguments) and validates the
  console handle, so the 12.1 paste guard works reliably; the re-ask
  loop above protects every platform even if flushing is unavailable.
- 5 additional prompt tests (13 in the prompt-fix file); no dependency
  or schema change.

## 0.14.1 - PHASE 12.1 Approval Prompt Fix

- CLI confirmation prompts (Git writes, task approvals, license
  deactivation) now discard console input that was typed or pasted
  *before* the prompt appeared, so pasting several commands at once can
  no longer feed the next command into an approval prompt (and a
  pre-pasted `APPROVE` cannot approve anything either). When pending
  text is discarded, the prompt says so. Piped stdin is untouched;
  `--yes` remains the non-interactive path.
- A denied direct Git write now prints the exact command to re-run with
  `--yes`, and a denied task approval explains that only the word
  `APPROVE` typed after the prompt counts. Matching stays
  case-insensitive for `APPROVE`. 8 new tests; no dependency or schema
  change.

## 0.14.0 - PHASE 12 Approved Subprocess & Git

- Command execution is now possible behind a **default-off** policy gate
  (`PC_AGENT_ALLOW_SUBPROCESS=1`). Only bare program names on the
  allow-list (`PC_AGENT_COMMAND_ALLOWLIST`, default
  `git,python,python3,py,node,npm`) can run, always as an argv array with
  `shell=False`; Windows `.cmd`/`.bat` shims (e.g. npm) run through
  `cmd.exe /d /s /c` only after rejecting cmd-special characters. The
  engine scrubs the child environment (no API keys/secrets), silences
  stdin, enforces a hard timeout with process kill (1-300s,
  `PC_AGENT_COMMAND_TIMEOUT_SECONDS`) and caps captured output at 64 KiB
  per stream.
- Six local Git tools (`git.status/log/diff_summary` read-only,
  `git.init/add/commit` writes) plus `commands.run` — 36 built-ins in
  total. Commands and Git writes always need a **fresh approval bound to
  the exact arguments** (never inherited, never from workflow memory) and
  are **never auto-retried** by recovery; verification re-reads Git state
  (e.g. HEAD must match the new commit) or the exit code independently.
- Deterministic planner understands Git requests (`Show git status`,
  `Git add the file`, `Git commit "message"`); `commands.run` is only
  reachable via `run --command "..."`. AI planning may propose read-only
  Git queries only. New direct `pc-agent git ...` commands prompt before
  writes (`--yes` pre-approves), and the GUI gains a Git Status button;
  `doctor` reports the subprocess gate and git availability.
- No new dependency or database migration (schema stays v6); 16 new
  tests covering the engine, policy gate, approvals, timeouts, Git
  end-to-end workflow, CLI and GUI paths.

Still not included: remote Git operations (clone/fetch/pull/push),
branch/checkout/merge/reset, arbitrary shells or pipes, browser or
Windows UI automation, image tools, or secure OS credential storage.

## 0.13.0 - PHASE 11 Multi-Step Planner

- Tasks can now chain 2-5 tools in one instruction with **then**. Each
  clause is planned by the existing single-step deterministic planner;
  later clauses may reference earlier steps' promised outputs, inherit the
  previous output implicitly ("then extract pages 1-2"), and are bound at
  run time to the previous step's actual output (recovery renames are
  followed). AI planning accepts a strictly-validated `steps` array with
  `$step<N>.output_path` references as the only allowed binding syntax.
- Every step runs the unchanged pipeline: fresh per-step approvals for
  external roots (never inherited), acceptance criteria, deterministic
  verification, per-step checkpoints and the bounded recovery budget
  (applied per step, attempt numbers unique across the pipeline). Reports
  (CLI text/JSON, GUI) list each step's status; resume continues at the
  first unfinished step without re-running checkpointed tools.
- Verified chains are recorded as multi-step workflows and replayed by
  `workflow run` / the planner fallback after live revalidation of every
  step. Manual `task recover` clones re-run the stored chain.
- No new dependency or database migration (schema stays v6); 16 new tests
  including crash-resume, approval denial mid-chain, AI forward-reference
  rejection and a forged multi-step workflow.

Still not included: list/folder output bindings, branching or parallel
steps, mid-flight re-planning, subprocess/Git adapters, browser or Windows
UI automation, image tools, or secure OS credential storage.

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
