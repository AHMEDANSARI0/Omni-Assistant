# Architecture - through PHASE 17

## Shape

One installable Python package (modular monolith). Runtime third-party code is
limited to `pypdf` plus `certifi` (the Mozilla CA bundle used by the Phase 4.1
verified-TLS helper); provider/web HTTP transports, the Phase 5 Tkinter
GUI and the Phase 6 offline licensing use the Python standard library.

```text
src/pc_agent/
|-- config.py              Environment/.env config, provider models/endpoints
|-- errors.py              Config, policy, storage, tool, provider, planning errors
|-- logging_config.py      Logging setup + secret redaction
|-- cli.py                 doctor/gui/license/task/run/recovery/providers/ask + direct tools/pdf/office/web/desktop/demo
|-- gui/
|   |-- controller.py      Headless GUI operations over orchestrator/recovery/providers/tools/licenses
|   |-- app.py             Tkinter window, worker queue and approval broker
|   `-- __main__.py        `python -m pc_agent.gui` entry point
|-- licensing/
|   |-- ed25519.py         Pure-Python RFC 8032 Ed25519 verify (+ vendor signing)
|   |-- models.py          Plans/features, signed token format, verification
|   |-- service.py         Trial/clock state, activation lifecycle, feature gates
|   `-- admin.py           Vendor-only keygen/issue (`pc-agent-license-admin`)
|-- planning.py            Deterministic planner + validated AI-plan parser
|-- http_tls.py            Shared verified TLS context (system + Windows store + Certifi)
|-- web.py                 SSRF-guarded one-page HTTP(S) text fetcher
|-- recovery.py            Deterministic diagnosis + stale scan/manual recovery service
|-- orchestrator.py        Plan -> approve -> tool -> verify, with bounded RETRYING loop
|-- domain/task.py         TaskStatus state machine + Task record
|-- policies/              Risk policy; workspace + task-specific read roots
|-- providers/
|   |-- base.py            Provider contracts + bounded JSON HTTP transport
|   |-- Gemini_adapter.py  Gemini Interactions REST
|   |-- openai_compatible.py  OpenRouter/OpenAI/generic compatible endpoints
|   |-- ollama.py          Local/cloud Ollama chat
|   |-- mock.py            Offline mock
|   `-- router.py          Provider factory, statuses, bounded retries/fallback
|-- storage/               SQLite migrations (v5) + task/execution/recovery/dynamic-tool/license repositories
|-- documents/pdf.py       PDF inspect/merge/verify + synthetic demo data
|-- documents/office.py    Standard-library DOCX/XLSX/PPTX/CSV parser/writers + demo data
|-- tooling/
|   |-- models.py          Immutable proposal/test-case contracts
|   |-- validation.py      Schema-definition + Python AST security policy
|   |-- templates.py       Reviewed offline pure-compute templates
|   |-- generator.py       Strict-JSON provider generation/repair prompts
|   |-- sandbox.py         Restricted subprocess orchestration
|   |-- sandbox_runner.py  Trusted stdlib harness copied per execution
|   `-- service.py         Candidate lifecycle + runtime registry loading
`-- tools/
    |-- schema.py          Strict mini schema validator
    |-- registry.py        ToolSpec, ToolContext, ToolRegistry
    |-- workspace_tools.py / pdf_tools.py / office_tools.py
    |-- web_tools.py / desktop_tools.py / builtins.py
```

SQLite migration v2 adds `task_plans`, `approval_records`, and
`tool_invocations`. Migration v3 adds `dynamic_tool_candidates`,
`dynamic_tool_test_runs`, and `dynamic_tool_events`. Migration v4 adds
`recovery_attempts` and `task_recovery_links`. Phases 4, 4.1 and 5 remain on
schema v4. Migration v5 (Phase 6) adds `license_state` and `license_events`;
raw license tokens are stored only in the license file, never in SQLite.

`ToolContext` now carries explicit, default-deny grants for network access,
private-network access and desktop opening, plus bounded web settings and an
injectable opener for tests. Only dedicated CLI commands and the GUI's Web /
Desktop tab operations set the network/desktop grants; orchestrated contexts
leave them false.

## Desktop GUI architecture

`pc_agent.gui.controller.GuiController` is the GUI's headless service layer.
It constructs one orchestrator, recovery service, provider router and tool
registry from `Settings` and exposes typed operations for tasks, direct
document/web/desktop tools, approved dynamic tools and provider status.
`pc_agent.gui.app` owns Tk widgets only: button handlers capture form values
on the GUI thread, run one controller operation at a time in a daemon worker,
and apply results through a queue. An `ApprovalBroker` blocks that worker on
a ticket while the GUI thread shows the exact external read-only scope, so a
GUI approval is the same orchestrator callback and audit record as a CLI
approval.

## Licensing architecture (Phase 6)

`pc_agent.licensing` is an offline, default-deny-when-enforced layer in front
of the same CLI/GUI entry points; it never changes policy or tool contexts.
`models.py` owns the plan/feature tables and deterministic token
verification, `service.py` owns the license-file lifecycle, the built-in
trial and last-seen-clock anti-rollback check, activation audit events and
`require(feature)` gates. Both the CLI (`_require_license`) and the GUI
controller call that one service, so a feature is gated identically in both
front-ends. Enforcement defaults to `off`; `warn` reports and continues,
`enforce` raises `LicenseError` before work starts. Status/diagnostic
surfaces (`doctor`, license commands, listing/showing records) are never
gated. `admin.py` is vendor-side tooling only and is never imported by the
client runtime. See `docs/phase6.md`.

## Phase 11 multi-step planning

`MultiStepPlan` is an ordered tuple of ordinary single-tool `TaskPlan` steps
plus run-time bindings (`path`/`input_path` <- earlier step's `output_path`).
The deterministic planner splits a `then` instruction outside quotes and
plans each clause with the single-step planner; promised outputs make
not-yet-created inputs plannable, and the AI planner emits the same shape
via a validated `steps` array. Execution extracts the original attempt
loop into `_execute_step` (which never completes the task) and drives it
per step: fresh approval, criteria, checkpoint at the step's index,
per-step recovery budget, and stop-on-first-failure. Resume consults
per-step checkpoints and continues at the first unfinished step; workflow
records store the whole chain for revalidated replay.

## Phase 10 verified workflow reuse

Workflow records became execution-grade evidence: normalized instruction,
exact validated arguments and approval roots under a precise key. The
orchestrator can resolve a record (exact/prefix/latest), rebuild its plan
(content first, legacy stored-plan fallback), revalidate it against the
live registry + allow-list, and run it as a new task through the unchanged
approval/verification/criteria/checkpoint/recovery pipeline. The same
lookup is the planner's last-resort fallback before `needs_input`. Memory
remains evidence only — approvals are always re-asked and nothing is
force-succeeded from history.

## Phase 9 deeper Office

The PDF engine gained atomic page extraction/splitting on the same
temp-file + re-verify pattern as merging. The Office engine gained a
reference-addressed XLSX cell model, a bounded deterministic formula
evaluator (tokenizer + recursive-descent parser, cycle detection), a
surgical single-sheet XLSX cell writer (other zip parts preserved,
calcChain dropped, computed cached values written), and zip-surgical DOCX
paragraph replace/append operating on run-concatenated paragraph text.
All six tools ride the existing registry, policy, criteria, checkpoint and
fresh-output recovery rails.

## Phase 8 file operations

`tools/file_tools.py` adds workspace-confined copy/move/rename/organize/
search on the existing registry/policy/verification rails: a dedicated
`MOVE_FILE` policy action (write-like inside the workspace, never a silent
delete), hash-verified transfers, conflict-skipping organization, bounded
content search that refuses credential-like files, deterministic planner
intents, and copy-collision healing through the existing fresh-output
recovery. No schema change was needed; checkpoints, criteria and audit
records reuse the Phase 7 tables.

## Phase 7 foundation

- `verification.py` turns each selected plan into deterministic acceptance
  criteria (tool verification, output artifact, independent SHA-256, PDF
  page count, CSV/JSON row recounts). The orchestrator persists every
  evaluation (`task_verifications`) and only succeeds when all criteria
  pass; criteria failures feed the same bounded recovery loop.
- `task_checkpoints` records each successful tool step with a SHA-256 plan
  fingerprint and artifact paths. `resume` continues the same task after an
  interruption, revalidates plan/policy/approval, invalidates checkpoints
  whose artifacts vanished, and always re-verifies — checkpoints skip tool
  re-execution, never verification.
- `task_cancellations` plus an optional in-process callback provide
  cooperative stage-boundary cancellation from CLI, GUI, or another process.
- `memory_items` holds user notes and automatic verified-workflow records;
  preferences live in the earlier `preferences` table. Memory is inspectable
  evidence managed through `pc-agent memory ...`; it never grants authority,
  and secret-like keys are refused.
- Schema v6 is appended by one migration; all earlier migrations are
  immutable and earlier data is preserved.

## Orchestrated lifecycle

```text
QUEUED/NEEDS_INPUT
  -> PLANNING (deterministic first; provider only with --use-ai fallback)
  -> WAITING_FOR_APPROVAL (only exact external read roots)
  -> RUNNING (one allow-listed registered tool)
  -> VERIFYING (deterministic re-check)
  -> SUCCEEDED | FAILED | CANCELLED

RUNNING/VERIFYING
  -> RETRYING (only a known safe diagnosis, attempts remain)
  -> PLANNING/WAITING_FOR_APPROVAL (only when a revised external-read plan
     needs fresh exact approval)
  -> RUNNING -> VERIFYING
```

Unclear instructions and uncorrectable missing inputs become `NEEDS_INPUT`.
A task interrupted while waiting can be run again from its stored plan.
Denied approval becomes `CANCELLED` and no further tool is invoked. Failed
terminal tasks are never reopened: `RecoveryService` creates a linked clone
and either copies a valid stored plan or replans the original instruction.

## Recovery architecture

`recovery.py` contains pure deterministic diagnosis helpers plus
`RecoveryService`. Diagnosis may propose only two argument changes: a fresh
workspace PDF/Office output, or a workspace input path when exactly one
same-named candidate exists. It may also propose retrying the exact plan for
recognized transient failures or after a generated-output verification
mismatch.

`TaskOrchestrator` owns the execution loop. Before every tool invocation it
starts a `recovery_attempts` checkpoint with the exact plan JSON. After tool
execution or verification it finalizes that row as `succeeded`, `retrying` or
`failed`, including category/action and any revised plan. The default limit
is two healing retries (hard cap three) with bounded exponential backoff.

Manual recovery never mutates a terminal source back to an active status.
`task_recovery_links` records source -> clone lineage and caps the chain at
three clones. Stale scans inspect task timestamps and old merger-temporary
filenames; they are report-only and never delete artifacts. See
`docs/recovery.md` for the rule table and CLI contract.

## Dynamic-tool lifecycle

```text
template / import / provider JSON (untrusted)
  -> strict proposal + schema-definition validation
  -> AST allow-list and dependency check
  -> immutable proposed candidate in SQLite
  -> restricted subprocess tests (exact expected outputs)
  -> explicit approval
  -> runtime registry (approved versions only; enabled flag honored)
  -> new version supersedes old; tested superseded version can roll back
```

Dynamic handlers do not run candidate code in the parent process. The service
copies a trusted harness and candidate into a fresh folder under the configured
data directory, starts `sys.executable -I -B -s` without a shell, sends JSON on
stdin, enforces the candidate timeout, and accepts one bounded JSON response.
This is restricted subprocess isolation, not a kernel/VM sandbox.

## Safety rules encoded

- **No success without verification.** The orchestrator independently
  re-verifies merged PDFs by reopening them and matching page count/SHA-256,
  and re-parses Office conversions/extractions to compare hashes, counts and
  source identity.
- **Models do not execute.** AI planning returns JSON proposals. Local code
  allow-lists only local PDF/list/Office-inspection/conversion/system-info
  tools, validates paths/types, then invokes the registry. It cannot grant
  web or desktop-open authority.
- **Approvals are parameter-specific.** External roots in a saved plan are
  printed exactly. Approval grants list/read under those roots for that run;
  writes still belong inside the workspace and deletes do not exist.
- **Execution-time policy remains authoritative.** Sensitive paths are denied
  even under an approved root; symlink/junction results are re-resolved to
  the selected directory during listing/discovery.
- **Network and desktop-open are direct-action capabilities.** Policy
  requires an exact URL/path and a context grant created only by
  `web fetch`, `desktop open` / `open-url`, or the GUI's dedicated Web /
  Desktop tab operations. Office XML/ZIP parsing rejects DOCTYPE,
  encrypted/duplicate entries and oversized structures; web fetch rejects
  private/non-standard targets by default and revalidates redirects.
- **Recovery cannot expand authority.** It never retries policy/approval
  failures, never overwrites an existing output, never guesses between
  multiple missing-input candidates, and does not substitute inputs inside an
  approved external scope. A revised external-read plan requires a new exact
  approval before the next invocation.
- **Provider fallback is user-configured only.** Retries are bounded by
  `PC_AGENT_MAX_RETRIES`; no provider is contacted merely because it exists
  as a slot name.
- **Generated code has no authority.** Dynamic proposals cannot declare
  permissions, request pip packages, import outside the computational
  allow-list, approve themselves, or enter the runtime registry before tests
  and explicit approval. Source hashes and provenance are persisted.
- **Repairs cannot weaken the contract.** Provider repair creates a new
  version and is capped at three attempts per chain; local code preserves the
  parent schemas and expected outputs.
- **Audit without secrets.** Plans, approval decisions, tool args/results and
  provider attempt outcomes are stored locally; secret-like fields/content
  are redacted from execution JSON and keys are never summary output.

## Important Phase 6 boundaries

- Natural-language orchestration supports the earlier PDF/file-list tools,
  safe local Office inspection/extraction/conversions, and system info.
  Web fetch and desktop opening are dedicated direct actions only. Dynamic
  tools are explicit registry invocations, not additional AI-planner choices.
- Web support is a one-page text fetcher, not browser automation. Desktop
  support is information/open only. The Phase 5 GUI is this application's own
  window; it provides no external-app GUI observation, clicking or typing.
  Office creation is simple and does not edit existing documents in place.
- Automatic healing covers known patterns in orchestrated local tools only.
  Direct CLI document/web/desktop/tool actions and the corresponding direct
  GUI tab actions remain single-attempt, and there is no background daemon
  repairing tasks while the application is closed.
- Dynamic tools are Python standard-library-only pure computation. They cannot
  read/write files, use the network, run shell commands, control desktop apps,
  browse, or install dependencies.
- Direct Anthropic integration is a reserved provider slot only.
- Built-in tool timeouts are declared/measured. Dynamic tools receive a hard
  subprocess timeout, but not a hard memory cap or OS/VM confinement.
- Licensing is offline and tamper-evident, not DRM: there is no payment
  processing, online activation/subscription-billing or revocation server,
  cloud worker, or account system yet. A local admin can still patch a
  local install; stronger protection belongs to the cloud/hardening phases.
- There is no Tool Builder lifecycle GUI, settings/API-key editor,
  production installer/shortcut, autonomous repair of the core
  runtime/source code, or deletion-based cleanup yet.

## Phase 12 approved subprocess and Git

The `commands.py` engine is the only place built-in tools may start a
child process. It executes argv arrays with `shell=False` after
requiring (a) the policy gate (`PolicyEngine` RUN_SHELL with
subprocess enabled and the working directory inside the workspace),
(b) a bare program name on the configured allow-list resolved via
`shutil.which`, and (c) bounded, NUL-free arguments. Windows
`.cmd`/`.bat` shims are bridged through `cmd.exe /d /s /c` only when
every argument is free of cmd-special characters. Children get a
scrubbed environment (PATH/system essentials only), closed stdin, a
hard timeout enforced by killing the process (group), and 64 KiB
output caps per stream.

Tools never accept raw flags: the six `git.*` tools and `commands.run`
build fixed argv from validated inputs. `commands.run`, `git.init`,
`git.add` and `git.commit` raise `ApprovalRequest(action="run_command")`
on every execution; the stored grant only matches identical arguments,
so resume and workflow replay re-ask. All seven ids sit in recovery's
no-retry set: a failed command never re-executes automatically.
Verification is independent: Git reads are re-invoked and compared,
commits must match HEAD and subject, `commands.run` must echo the
approved program/argv with the expected exit code while still
returning full output for non-zero exits. AI planning only allows the
read-only Git trio; deterministic Git intents and `run --command`
are the other entry points, and direct `pc-agent git` commands carry
their own visible confirmation.

## Phase 13 browser automation (CDP)

`browser_cdp.py` is a standard-library Chromium DevTools client: a
minimal RFC 6455 WebSocket implementation plus CDP request/event
routing with per-request deadlines and message-size caps. It only
connects to a localhost DevTools endpoint of a browser the user
started with `--remote-debugging-port` (never launches one). Page
JavaScript is a set of fixed templates; user values enter exclusively
through `json.dumps`, so page text and arguments cannot become code.

`browser.status/snapshot` are read-only; `browser.open/click/type`
raise `ApprovalRequest(action="browser_action")` bound to exact
arguments, and all six tools sit in recovery's no-retry set. Typing
uses the native value setter + input/change events (framework-safe)
with an `Input.insertText` fallback, refuses password/file fields,
and is verified by reading the field back. Screenshots are the only
file writes: new workspace PNGs checked by magic bytes, size and
SHA-256 like other artifacts. The whole surface is gated by
`PC_AGENT_ALLOW_BROWSER` (off by default); AI planning can only
propose the two read-only tools.
## Phase 14 Windows desktop UI Automation

`desktop_automation.py` is the native boundary. Win32 enumerates top-level
windows, restores/foregrounds one exact HWND and captures screenshots
through GDI + a standard-library PNG encoder. Microsoft UI Automation is
reached through the Windows-only `comtypes` package and the operating
system's `UIAutomationCore.dll` type library; no element search is performed
through coordinates or image templates.

Window and element selectors are data, not grants. A selector must resolve
uniquely, passwords are refused, typing requires a readable UIA value/text
contract and exact read-back, and key input is limited to one allow-listed
chord rechecked against the foreground window before every repeat. The
surface is gated by `PC_AGENT_ALLOW_DESKTOP_AUTOMATION` and the
`DESKTOP_AUTOMATION` policy action. `desktop.window_focus`,
`desktop.window_click`, `desktop.window_type` and `desktop.send_keys` raise
`ApprovalRequest(action="desktop_automation_action")` on every orchestrated
execution, and all seven tools sit in recovery's no-retry set. AI planning
can only propose `desktop.windows` / `desktop.window_inspect`. The backend
is injectable through `ToolContext`, which lets the automated suite exercise
all task/verification behavior with a simulated desktop on non-Windows
hosts while the live backend fails closed off Windows.

## Phase 15 image tools and provider vision

`images.py` is the local Pillow boundary. It imposes file/pixel caps,
normalizes EXIF orientation, uses only the first frame for processing,
discards embedded metadata in outputs, and creates outputs through a
temporary file plus hard link so an existing file is never overwritten.
Inspection, conversion, transformation and comparison make no provider
call. Their task results are verified by independently reopening the image
and checking format/dimensions/mode/statistics.

Provider requests gained immutable `ImageInput` values. Gemini receives an
Interactions image part, OpenAI-compatible providers receive an `image_url`
data URL, native Ollama receives base64 in `images`, and the offline mock
refuses image input. The router restricts image-carrying requests to
providers declaring vision capability. `image.analyze` additionally needs
`PC_AGENT_ALLOW_VISION=1`, a context-supplied router, and the orchestrator's
`vision_analysis` approval before the handler can read-and-send the exact
workspace image. It is excluded from AI planning and automatic recovery, so
no failed path silently resends image bytes.

## Phase 16 Windows Credential Manager

`credentials.py` is the only OS-secret boundary. Its Windows backend stores
one generic credential per provider through `CredWriteW`, reads it with
`CredReadW`/`CredFree`, and deletes it with `CredDeleteW`. Fixed-width
Win32 types are used for the `CREDENTIALW` layout so tests on LP64 hosts
check the same 4-byte DWORD/BOOL ABI as Windows. The backend is injectable
and the suite simulates `advapi32`; production uses no third-party secret
package.

Settings resolves each key from environment/``.env`` first and only then
from the credential store. This preserves existing installs and makes
migration non-breaking. Production CLI/GUI loads may consult the auto
Windows backend, while an explicitly supplied test environment mapping
does not silently read the developer's vault unless a store is injected;
the test suite additionally defaults to ``PC_AGENT_CREDENTIAL_STORE=env``.
Credential values never enter tool schemas, task plans, logs or status
objects—only provider names and source labels are exposed.

## Phase 17 core self-repair and task budgets

`pc_agent.self_repair.SelfRepairService` is a maintenance service, not a
registered tool. It snapshots only `src/`, `tests/` and `pyproject.toml`
under `data/self-repair/baseline/`, diagnoses compile/version errors and
baseline drift, and can emit only baseline-restore or generated-cache-delete
operations. A repair is staged in an isolated copy (`.env` excluded), checked
with fixed static/import/full argv vectors in a scrubbed environment, and
applied only through the separately approved CLI apply step after a stale-plan
check and per-file backup. Failed post-checks restore the backup; manual
rollback uses the same records. Corrupt/invalid/version-mismatched baselines
block staging, and extra files are evidence only. The separate
`pc-agent-self-repair` bootstrap imports only this service (not the full CLI,
planner, orchestrator or budgets), so it can repair most core files even
when the normal CLI cannot start.

`pc_agent.budgets` owns one mutable `TaskBudgetTracker` per orchestrated
execute/resume/workflow replay. The orchestrator stores it in a context
variable so the same tracker covers AI planning, provider vision inside a
tool, multistep execution and bounded recovery re-executions without passing
budget state through tool schemas. `BudgetedModelRouter` wraps the configured
router: it reserves estimated input + maximum output before a call, prefers
reported usage afterwards, and records fallback attempts separately. Step
checks sit before every execution entry point (`_run_plan`, deterministic
single/multi outcomes and stored-plan resume); time/tool checks sit before
each planned invocation. Snapshots are attached to execution reports and task
events, while recovery treats budget exhaustion as policy/non-retryable.


## Phase 18 production hardening and portable releases

`pc_agent.release` is a user-invoked maintenance boundary, not a tool. Its
audit reads settings, version files, folder state and SQLite through a
read-only connection; it does not initialize the database or call a
provider. Bundle construction starts from a fixed allow-list, writes a
sorted SHA-256 manifest and fixed-timestamp ZIP entries, and verifies the
temporary archive before publishing it without overwrite. Bundle
verification hashes entries in memory and never extracts them, so unsafe
paths, duplicates, symlinks and forbidden private areas fail closed.

`pc_agent.maintenance` uses SQLite's backup API for consistent database
copies below `data/backups`. A strict sidecar manifest binds each copy to
its size, hash, app version and schema. Restore verifies the selected copy,
creates a pre-restore backup, checkpoints/removes exact WAL sidecars,
stages and re-verifies the replacement, and leaves normal schema migration
to the existing `Database.initialize()` call after the CLI approval step.

`pc_agent.diagnostics` composes only public settings, provider statuses,
tool IDs, counts and status objects. It never reads task instructions,
preference/memory values, prompts or workspace contents into the export.
The GUI reuses the same read-only audit and otherwise leaves bundle build,
diagnostics export and backup/restore as explicit CLI operations. Windows
scripts are packaging helpers around those same entry points; uninstall
removal is exact-path-checked and preserves user data by default.
