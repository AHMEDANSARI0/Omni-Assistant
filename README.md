# Self-Healing PC Agent (working title)

A Windows-first AI PC agent being built in verified phases. The goal is a
secure, lightweight, extensible agent that plans and executes user tasks with
explicit permissions, verifies results deterministically, and reports honestly
what it did.

## Current status: PHASE 8 - General file operations (this build)

| Status | Capability |
|---|---|
| Implemented + tested (1A) | Configuration with validation and secret redaction; task state machine; SQLite migrations; deterministic policy engine; `doctor` and task-record CLI |
| Implemented + tested (1B) | Versioned built-in tool registry with strict schemas; workspace tools; PDF inspect/verify/merge tools; skipped-file reporting; synthetic demo data |
| Implemented + tested (1C) | Provider adapters (Gemini, OpenRouter, OpenAI/generic OpenAI-compatible, Ollama, offline mock), bounded fallback router, deterministic-first structured planning, optional constrained AI planning, stored task orchestration, exact read-only approvals for external input folders, SQLite plan/approval/tool-invocation records |
| Implemented + tested (Phase 2) | Dynamic pure-compute tool candidates from reviewed templates/import/provider generation; strict proposal/schema validation; Python AST security analysis; restricted subprocess tests; immutable versioned SQLite registry with provenance/source hashes; explicit approval; invocation; enable/disable; supersede/rollback; bounded provider repairs |
| Implemented + tested (Phase 3) | Deterministic failure diagnosis and bounded recovery for orchestrated tasks; fresh-output retry after collisions or merge verification failure; uniquely corrected workspace input paths; transient same-plan retries with backoff; SQLite recovery ledger; linked recovery clones for failed/stale tasks; stale-task and orphan-temp-PDF scan (report-only) |
| Implemented + tested (Phase 4) | Standard-library Office engine for DOCX/XLSX/PPTX/CSV inspection and text extraction, CSV statistics, CSV↔JSON, CSV→XLSX and simple DOCX creation; one-page SSRF-guarded web text fetch; desktop system info and explicit default-app opening; task `latest`/`latest-failed` aliases, status filtering and exact failed-task next commands; quieter known-corrupt-PDF handling |
| Hotfix (Phase 4.1) | Verified Windows TLS trust using system + Windows-store + Certifi CA sources for web/provider HTTPS; duplicate successful-recovery clone guard; terminal-readable multi-line `tools list` |
| Implemented + tested (Phase 5) | Standard-library Tkinter desktop GUI with Dashboard, Tasks, Documents, Web, Desktop, Tools and Providers tabs; same orchestrator/recovery/provider/policy services as the CLI; worker-thread operations and exact external-read approval dialogs; `pc-agent gui --check` and windowed `pc-agent-gui` launcher |
| Implemented + tested (Phase 6) | Offline Ed25519-signed licensing: trial/standard/pro/enterprise plans mapped to feature gates, expiring or perpetual terms, optional machine binding, built-in configurable trial, vendor `pc-agent-license-admin` issuing tool, customer `pc-agent license` CLI + GUI Dashboard activation; enforcement opt-in (`off` default) |
| Implemented + tested (Phase 7) | Generalized acceptance-criteria verification persisted per attempt (artifact existence, independent SHA-256, PDF pages, CSV/JSON row recounts); durable step checkpoints with safe in-place `task resume`; cooperative CLI/GUI cancellation at stage boundaries; inspectable memory (preferences, notes, automatic verified-workflow records) with secret guards and explicit task-history deletion; schema v6 |
| Implemented + tested (Phase 8) | General workspace file operations — `files.copy/move/rename/organize/search` (23 built-ins) with a dedicated `MOVE_FILE` policy action, no-overwrite semantics, hash-verified transfers, conflict-skipping folder organization, bounded name/content search that never reads credential-like contents, deterministic planner intents, direct `pc-agent files` CLI, and a GUI Documents > Files panel; copy collisions heal to a fresh `-recovered` output |
| Planned next phases | Deeper Office tools (DOCX edit, XLSX formulas, PDF split/extract), approved subprocess/Git workflows, browser automation, Windows app/UI automation, images, multi-step planning, verified workflow reuse |
| Later | Full core self-repair with isolated staging/rollback, secure OS credential storage, production hardening, macOS/Linux |

This build **does not** claim arbitrary computer control, arbitrary generated
programs, full browser automation, external-app GUI clicking/typing, or
autonomous repair of the core runtime. Phase 5's GUI is this application's
own window; it does not automate other applications. Phase 4 web fetching reads one exact page as text without
executing scripts, and desktop support is system information plus opening one
exact workspace item or URL. Self-healing remains limited to known safe
failure patterns in the orchestrated local allow-list. Dynamic tools are
invoked explicitly and remain JSON-in/JSON-out pure computation with no file,
network, shell or desktop permission. Phase 6 licensing is offline and local:
there is no payment processing, online activation/subscription-billing
server, or cloud account yet.

## Setup / upgrade (Windows PowerShell)

New installs apply the patchers in order: 1A, 1B, 1C, Phase 2, Phase 3,
Phase 4, the Phase 4.1 hotfix, Phase 5, Phase 6, Phase 7, then Phase 8. An
existing Phase 7 (0.9.0) install only needs the Phase 8 patcher:

```powershell
cd D:\omni-assistant   # your project folder
node PHASE_8_FILES.mjs

.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check
pc-agent --version
pc-agent doctor
pc-agent files search report --content
pc-agent gui --check
```

Phase 8 adds no runtime dependency and no database migration (schema stays
**v6**). Reinstalling refreshes the editable package metadata to 0.10.0.
License enforcement still defaults to `off`. See `docs/phase8.md` for the
new copy/move/rename/organize/search behavior and `docs/phase7.md` for the
acceptance-criteria, checkpoint/resume, cancellation and memory behavior it
builds on.

## Desktop GUI (Phase 5)

Open the local window from the project folder:

```powershell
pc-agent gui
```

The GUI provides Dashboard, Tasks, Documents, Web, Desktop, Tools and
Providers tabs over the same policy-bound services as the CLI. Run
`pc-agent gui --check` for a no-window readiness test. Dynamic Tool Builder
generation/approval and API-key editing remain outside the GUI in this
release. See `docs/phase5.md` for the tab guide, smoke test and
troubleshooting.

## Licensing (Phase 6)

Licensing is offline and opt-in. With the default
`PC_AGENT_LICENSE_ENFORCEMENT=off`, nothing changes for local development;
`pc-agent license status` simply reports the unconfigured state. Commercial
builds configure the vendor **public** key in `.env` and choose `warn` or
`enforce`, which enables the built-in trial (14 days by default) and signed
license activation:

```powershell
pc-agent license status
pc-agent license machine-id
pc-agent license activate --file C:\path\to\customer.license
```

The vendor issues licenses on a trusted machine with
`pc-agent-license-admin keygen` and `pc-agent-license-admin issue`; the
private key must never reach a customer machine. The GUI Dashboard has the
same status plus activation/deactivation buttons. Plans, feature gates,
trial and clock-rollback rules, and the vendor quickstart are documented in
`docs/phase6.md`. Online payments/activation and subscription billing are not
part of this phase.

## Configure AI providers (keys stay on your PC)

The local core, deterministic tools and offline Tool Builder templates work
without keys. Provider-backed tool generation/repair requires a real provider;
the offline mock cannot generate code. For external AI, create/edit **your
local** `.env` (do not send keys in chat):

```dotenv
PC_AGENT_PROVIDER_ORDER=gemini,openrouter
GEMINI_API_KEY=your-local-key
OPENROUTER_API_KEY=your-local-key
```

Then check the route without a network call:

```powershell
pc-agent providers list
pc-agent ask "Reply briefly: provider test successful"
```

`ask` uses the configured route and may use the network/API quota. Supported
PDF/file-list `run` commands are deterministic first and make **no provider
call**. Provider planning is used only if you add `--use-ai` and deterministic
planning does not recognize the instruction. See `docs/providers.md`.

## Dynamic Tool Builder demo (offline, no API key)

This exercises a complete reviewed-template lifecycle: candidate -> sandbox
tests -> human review -> explicit approval -> registry invocation.

```powershell
pc-agent tool-builder suggest "I need to count words"
pc-agent tool-builder template word_count --run-tests
pc-agent tool-builder show custom.text_word_count@1.0.0 --show-source
pc-agent tool-builder approve custom.text_word_count@1.0.0 --yes
pc-agent tools list
pc-agent tools invoke custom.text_word_count --arguments-file examples\dynamic-tools\word_count_arguments.json
```

Expected output includes `"words": 3`. The tool has no permissions and runs
each invocation in the restricted subprocess harness. Use
`pc-agent tools disable custom.text_word_count` to turn it off without
deleting its approval/test history; `tools enable` restores it.

Phase 3 also hardens this demo: relative `--arguments-file` paths are searched
in the current folder, then the app root, then the workspace. If that one
known bundled word-count example is missing from disk, the CLI uses its
embedded copy, prints a warning, and `doctor` reports that the bundled file
should be restored by rerunning the latest patcher. Other missing argument
files list every path that was checked. `tool-builder approve --yes` now
prints the final approved/enabled state after approval.

With Gemini/OpenRouter/OpenAI/Ollama configured, `tool-builder generate` can
propose a candidate and `tool-builder repair` can propose a bounded repair.
Generated code is still untrusted until the same static checks, sandbox tests
and your approval pass. See `docs/tool_builder.md` for the proposal format,
security layers, lifecycle and limitations.

## Office, web fetch and desktop tools (Phase 4)

Create deterministic local demo files, then use direct commands:

```powershell
pc-agent demo office
pc-agent office inspect "Office Demo/summary.docx"
pc-agent office csv-inspect "Office Demo/sales.csv"
pc-agent office extract "Office Demo/summary.docx" --output "Office Demo/summary.txt"
pc-agent office csv-to-json "Office Demo/sales.csv" --output "Office Demo/sales-converted.json"
pc-agent office csv-to-xlsx "Office Demo/sales.csv" --output "Office Demo/sales-book.xlsx" --sheet-name Sales
pc-agent office create-docx --output "Office Demo/release-note.docx" --title "Phase 4" --paragraph "Office tools are ready for local testing."
```

Supported reads are DOCX, XLSX, PPTX and CSV. JSON is supported as a
conversion source/target. Macro-enabled/legacy Office files, encrypted
packages, oversized archives/XML and `DOCTYPE` XML are rejected. Every writer
creates a new workspace file and never overwrites.

Web fetch is deliberately narrower than a browser. It performs one bounded
GET for the exact URL in that direct command, validates public resolved IPs
and every redirect, caps bytes/time, accepts text/HTML/JSON/XML, removes
scripts/styles and never sends cookies or credentials:

```powershell
pc-agent web fetch https://example.com/
pc-agent web fetch https://example.com/ --output example.txt
```

Private/loopback addresses and non-standard ports are blocked unless the same
direct command includes `--allow-private`. Generic `run`, AI planning and
`tools invoke web.fetch` do **not** receive network authority.

Desktop support can report local system information or open one exact target
in the default application. It does not click, type, take screenshots or run
executable/script/installer files:

```powershell
pc-agent desktop info
pc-agent desktop open "Office Demo/summary.docx"
pc-agent desktop open-url https://example.com/
```

Safe local Office work can also be orchestrated and verified:

```powershell
pc-agent run "Convert CSV to JSON" --target "Office Demo/sales.csv" --output "Office Demo/task-sales.json"
pc-agent run "Show system information"
```

See `docs/phase4.md` for the command reference, limits and honest boundaries.

## Self-healing and recovery (Phase 3)

Orchestrated `run`/`task run` executions record every attempt. By default, at
most two healing retries are allowed (absolute cap: three). Safe examples:

- The planned merge output already exists -> preserve it and retry to a
  `*-recovered.pdf` workspace file.
- A workspace input moved -> retry only if exactly one matching file/folder
  is found; ambiguous or absent inputs become `needs_input`.
- A merge does not pass verification -> retry to a fresh output and verify
  again.
- A timeout/locked-file style failure -> retry the exact same validated plan
  after bounded exponential backoff.

Policy, approval, corrupt/encrypted and other invalid-input failures are
never bypassed. If a revised plan still reads approved external folders, the
exact revised plan is approved again before another tool runs.

```powershell
pc-agent run "Merge PDFs from Folder A and Folder B" --output merged.pdf
pc-agent run "Merge PDFs" --no-recovery          # disable healing for one run
pc-agent task diagnose latest-failed             # no angle brackets to type
pc-agent task recover latest-failed
pc-agent task list --status failed
pc-agent recovery scan
pc-agent recovery list
pc-agent recovery show latest-failed
```

A failed human-readable `run` report prints its exact diagnose/recover
commands with the real ID already inserted. Task commands also accept
`latest`, `latest-failed` and `latest-<status>` (for example
`latest-needs_input`); the CLI prints which ID the alias resolved to. Do not
type documentation placeholders such as `<TASK_ID>` literally—Windows treats
`<` and `>` as redirection before the agent can run.

Failed terminal tasks are not reopened. Manual recovery creates a linked
clone, copies a valid stored plan when available (otherwise it replans), and
preserves the source task for audit. A stale/interrupted active task is first
closed as failed with an interruption reason, then its clone runs. Use
`task recover --force` only if you are sure no other `pc-agent` process is
still running that task. `recovery scan` only reports stale tasks and old
temporary merge files; it deletes nothing.

Recovery defaults can be changed locally in `.env` with
`PC_AGENT_MAX_RECOVERY_ATTEMPTS` (0-3),
`PC_AGENT_RECOVERY_BACKOFF_SECONDS` (0-5), and
`PC_AGENT_RECOVERY_STALE_AFTER_MINUTES` (1-10080). See `docs/recovery.md`.

## Resume, cancel and memory (Phase 7)

Phase 7 checkpoints every successful tool step. If a task is interrupted
(Ctrl+C, crash, closed window), continue the **same** task instead of
cloning it — a valid checkpoint is reused and fully re-verified instead of
silently re-running completed work:

```powershell
pc-agent task resume latest
pc-agent task cancel latest
pc-agent task delete latest --yes          # terminal tasks only; files stay
pc-agent memory stats
pc-agent memory prefs
pc-agent memory pref-set editor notepad
pc-agent memory note-set home "call office tomorrow"
pc-agent memory notes
pc-agent memory workflows                   # verified-success evidence
pc-agent memory clear --yes                 # keeps task history
```

Use `task resume --force` only if you are sure no other `pc-agent` process
is running that task. Cancellation stops a running task safely at the next
stage boundary (never mid-write) from another terminal or the GUI Tasks
tab. Every run now also prints explicit acceptance criteria with
`[PASS]`/`[FAIL]` evidence. See `docs/phase7.md`.

## File operations (Phase 8)

Workspace-confined copy/move/rename/organize/search. Nothing is ever
overwritten or deleted; transfers are SHA-256 verified:

```powershell
pc-agent files copy report.txt "Archive/report.txt"
pc-agent files move "Archive/report.txt" report-final.txt
pc-agent files rename report-final.txt report.txt
pc-agent files organize . --by category
pc-agent files search budget --content
pc-agent run "Copy the file" --target "report.txt" --output "Archive/report.txt"
pc-agent run "Organize this folder by extension"
pc-agent run "Search files containing \"budget\""
```

See `docs/phase8.md`.

## Natural-language PDF task (safe demo)

```powershell
pc-agent demo pdf
pc-agent run "Merge PDFs from Folder A and Folder B" --output demo-merged.pdf
pc-agent task list
```

Expected: task status `succeeded`, a verified 6-page
`workspace\demo-merged.pdf`, and the deliberately corrupt demo file reported
under Skipped. Repeating the same command preserves that first output and,
through Phase 3 recovery, creates `workspace\demo-merged-recovered.pdf`.
Or execute an existing queued task:

```powershell
pc-agent task create "Merge PDFs from Folder A and Folder B into queued-demo.pdf"
pc-agent task run latest
```

For exact user-selected folders outside `workspace\`, pass each path. The
agent prints the exact read-only scope and asks you to type `APPROVE`; outputs
still stay in the workspace:

```powershell
pc-agent run "Merge PDFs" `
  --input-folder "C:\Users\You\Desktop\Folder A" `
  --input-folder "C:\Users\You\Desktop\Folder B" `
  --output desktop-merged.pdf
```

Use `--yes` only when you intentionally pre-approve the exact plan printed by
that same command. Approval never grants write/delete access outside the
workspace and never allows credential-like files.

Other supported examples:

```powershell
pc-agent run "Verify merged.pdf has 6 pages"
pc-agent run "List PDF files in Folder A"
pc-agent run "Convert CSV to JSON" --target "Office Demo/sales.csv" --output "Office Demo/orchestrated.json"
pc-agent run "Merge PDFs from Folder A and Folder B" --json
```

Direct deterministic commands from 1B remain available:

```powershell
pc-agent tools list
pc-agent pdf merge-folders "Folder A" "Folder B" --output direct-merged.pdf
pc-agent pdf verify direct-merged.pdf --expected-pages 6
```

Direct `pdf` commands remain workspace-only; external paths belong to the
approval-based `run` flow. Automatic recovery wraps the orchestrated
`run`/`task run` lifecycle, not direct one-shot `pdf`, `office`, `web` or
`desktop` commands. Known corrupt PDFs are still listed under structured
`Skipped` reasons, but pypdf's duplicate low-level header/EOF warnings are
suppressed in normal operation; use `--log-level DEBUG` only when parser
diagnostics are intentionally needed.

## Configuration and data

Copy `.env.example` to `.env` locally when needed. The patcher updates
`.env.example` but never overwrites your `.env`. Local data remains in
`data\` and artifacts in `workspace\`; both are git-ignored.

See `docs/phase4.md`, `docs/phase5.md`, `docs/phase6.md`, `docs/phase7.md`,
`docs/phase8.md`,
`docs/providers.md`, `docs/tools.md`, `docs/tool_builder.md`,
`docs/recovery.md`, `docs/architecture.md`, and `CHANGELOG.md` for detailed
contracts and honest limitations.
