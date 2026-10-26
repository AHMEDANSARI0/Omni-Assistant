# Self-Healing PC Agent (working title)

A Windows-first AI PC agent being built in verified phases. The goal is a
secure, lightweight, extensible agent that plans and executes user tasks with
explicit permissions, verifies results deterministically, and reports honestly
what it did.

## Current status: PHASE 1C - Providers + orchestration (this build)

| Status | Capability |
|---|---|
| Implemented + tested (1A) | Configuration with validation and secret redaction; task state machine; SQLite migrations; deterministic policy engine; `doctor` and task-record CLI |
| Implemented + tested (1B) | Versioned built-in tool registry with strict schemas; workspace tools; PDF inspect/verify/merge tools; skipped-file reporting; synthetic demo data |
| Implemented + tested (1C) | Provider adapters (Gemini, OpenRouter, OpenAI/generic OpenAI-compatible, Ollama, offline mock), bounded fallback router, deterministic-first structured planning, optional constrained AI planning, stored task orchestration, exact read-only approvals for external input folders, SQLite plan/approval/tool-invocation records |
| Planned next phase | Phase 2 dynamic tool builder: candidate generation, static checks, isolated tests, approval, versioned registration, disable/rollback |
| Planned later phases | Self-healing, more office/browser/desktop tools, desktop UI, licensing/subscriptions, cloud coordination, production hardening |

Phase 1C **does not** claim arbitrary computer control. Natural-language
execution currently covers an allow-list of PDF merge/verify/inspect and
file-list tasks. `ask` is a text-only provider request, not tool execution.

## Setup / upgrade (Windows PowerShell)

New installs apply the patchers in order: 1A, then 1B, then 1C.
An existing Phase 1B install only needs the 1C patcher:

```powershell
cd D:\omni-assistant   # your project folder
node PHASE_1C_CLI_PROVIDERS.mjs

.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
pc-agent doctor
pc-agent providers list
```

No new runtime library is added in 1C, but reinstalling refreshes the editable
package metadata to 0.3.0. The database migrates automatically from schema v1
to v2, so `doctor` should show **schema v2**.

## Configure AI providers (keys stay on your PC)

Phase 1C works without keys through deterministic tools and the offline mock.
For external AI, create/edit **your local** `.env` (do not send keys in chat):

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

## Natural-language PDF task (safe demo)

```powershell
pc-agent demo pdf
pc-agent run "Merge PDFs from Folder A and Folder B" --output demo-merged.pdf
pc-agent task list
```

Expected: task status `succeeded`, a verified 6-page
`workspace\demo-merged.pdf`, and the deliberately corrupt demo file reported
under Skipped. Or execute an existing queued task:

```powershell
pc-agent task create "Merge PDFs from Folder A and Folder B into queued-demo.pdf"
pc-agent task run <TASK_ID>
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
pc-agent run "Merge PDFs from Folder A and Folder B" --json
```

Direct deterministic commands from 1B remain available:

```powershell
pc-agent tools list
pc-agent pdf merge-folders "Folder A" "Folder B" --output direct-merged.pdf
pc-agent pdf verify direct-merged.pdf --expected-pages 6
```

Direct `pdf` commands remain workspace-only; external paths belong to the
approval-based `run` flow.

## Configuration and data

Copy `.env.example` to `.env` locally when needed. The patcher updates
`.env.example` but never overwrites your `.env`. Local data remains in
`data\` and artifacts in `workspace\`; both are git-ignored.

See `docs/providers.md`, `docs/tools.md`, `docs/architecture.md`, and
`CHANGELOG.md` for detailed contracts and honest limitations.
