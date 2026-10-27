# Architecture - through PHASE 8

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
