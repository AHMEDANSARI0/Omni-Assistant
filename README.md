# Self-Healing PC Agent (working title)

A Windows-first AI PC agent being built in verified phases. The goal is a
secure, lightweight, extensible agent that plans and executes user tasks with
explicit permissions, verifies results deterministically, and reports honestly
what it did.

## Current status: PHASE 1B - Tools + PDF (this build)

| Status | Capability |
|---|---|
| Implemented + tested (1A) | Configuration with validation and secret redaction; task state machine; SQLite migrations; deterministic policy engine; `doctor` and task-record CLI |
| Implemented + tested (1B) | Versioned built-in tool registry with strict input/output schemas; workspace file tools; PDF inspect/verify/merge tools; folder PDF merge with explicit skipped-file reporting; synthetic demo data |
| Planned PHASE 1C | AI provider adapters (Gemini, OpenRouter, OpenAI-compatible, offline mock), model router with configured fallback, natural-language orchestration that executes a queued task through planning -> tools -> verification |
| Planned later phases | Dynamic tool builder, self-healing, office/browser/desktop tools, desktop UI, licensing/subscriptions, cloud coordination |

Nothing in the "Planned" rows should be treated as working functionality yet.
In this build, PDF tools run **only inside the configured workspace** and
never overwrite an existing file.

## Setup / upgrade (Windows PowerShell)

New installs run `PHASE_1A_FOUNDATION.mjs` first, then
`PHASE_1B_TOOLS_PDF.mjs`. Existing Phase 1A installs only need the 1B patcher:

```powershell
cd D:\omni-assistant   # your project folder
node PHASE_1B_TOOLS_PDF.mjs

.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"   # required again: 1B adds the pypdf dependency
python -m pytest -q
pc-agent doctor
pc-agent tools list
```

If PowerShell refuses to run `Activate.ps1`, call the tools directly, for
example `.\.venv\Scripts\python.exe -m pytest -q` and
`.\.venv\Scripts\pc-agent.exe doctor`.

## PDF demo (safe, synthetic data)

```powershell
pc-agent demo pdf
pc-agent pdf merge-folders "Folder A" "Folder B" --output merged.pdf
pc-agent pdf verify merged.pdf --expected-pages 6
```

Expected result: a verified `workspace\merged.pdf` with 6 pages. The demo's
`Folder B\broken.pdf` is deliberately corrupt and must appear under
"Skipped" - never silently inside the merged file. Source PDFs are not
modified. If `merged.pdf` already exists, the tool refuses to overwrite it;
choose a new output name such as `merged-2.pdf`.

Other direct commands:

```powershell
pc-agent pdf inspect "Folder A\a1.pdf"
pc-agent pdf merge "Folder A\a1.pdf" "Folder B\b1.pdf" --output pair.pdf
```

All PDF paths are relative to `workspace\` in Phase 1B. Reading user-selected
folders outside the workspace arrives with the approval flow in Phase 1C.

## Configuration and data

Copy `.env.example` to `.env` locally when provider configuration is needed.
Never commit `.env` and never share API keys. Phase 1B still makes **no AI
provider calls**. Local data remains in `data\` and task artifacts in
`workspace\`; both are git-ignored.

See `docs/tools.md` for the registered tool catalog and safety limits,
`docs/architecture.md` for the module map, and `CHANGELOG.md` for each build.
