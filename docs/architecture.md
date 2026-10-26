# Architecture - through PHASE 1C

## Shape

One installable Python package (modular monolith). Runtime third-party code is
limited to `pypdf`; provider HTTP adapters use the Python standard library.

```text
src/pc_agent/
|-- config.py              Environment/.env config, provider models/endpoints
|-- errors.py              Config, policy, storage, tool, provider, planning errors
|-- logging_config.py      Logging setup + secret redaction
|-- cli.py                 doctor/task/run/providers/ask + direct tools/pdf/demo
|-- planning.py            Deterministic planner + validated AI-plan parser
|-- orchestrator.py        Stored lifecycle: plan -> approve -> tool -> verify
|-- domain/task.py         TaskStatus state machine + Task record
|-- policies/              Risk policy; workspace + task-specific read roots
|-- providers/
|   |-- base.py            Provider contracts + bounded JSON HTTP transport
|   |-- Gemini_adapter.py  Gemini Interactions REST
|   |-- openai_compatible.py  OpenRouter/OpenAI/generic compatible endpoints
|   |-- ollama.py          Local/cloud Ollama chat
|   |-- mock.py            Offline mock
|   `-- router.py          Provider factory, statuses, bounded retries/fallback
|-- storage/               SQLite migrations (v2) + task/execution repositories
|-- documents/pdf.py       PDF inspect/merge/verify + synthetic demo data
`-- tools/
    |-- schema.py          Strict mini schema validator
    |-- registry.py        ToolSpec, ToolContext, ToolRegistry
    |-- workspace_tools.py / pdf_tools.py / builtins.py
```

SQLite migration v2 adds `task_plans`, `approval_records`, and
`tool_invocations` next to the v1 task/event/preference tables.

## Orchestrated lifecycle

```text
QUEUED/NEEDS_INPUT
  -> PLANNING (deterministic first; provider only with --use-ai fallback)
  -> WAITING_FOR_APPROVAL (only exact external read roots)
  -> RUNNING (one allow-listed registered tool)
  -> VERIFYING (deterministic re-check)
  -> SUCCEEDED | FAILED | CANCELLED
```

Unclear instructions become `NEEDS_INPUT`. A task interrupted while waiting
can be run again from its stored plan. Denied approval becomes `CANCELLED`
and no tool is invoked.

## Safety rules encoded

- **No success without verification.** The orchestrator independently
  re-verifies merged PDFs by reopening them and matching page count/SHA-256.
- **Models do not execute.** AI planning returns JSON proposals. Local code
  allow-lists five tools, validates paths/types, then invokes the registry.
- **Approvals are parameter-specific.** External roots in a saved plan are
  printed exactly. Approval grants list/read under those roots for that run;
  writes still belong inside the workspace and deletes do not exist.
- **Execution-time policy remains authoritative.** Sensitive paths are denied
  even under an approved root; symlink/junction results are re-resolved to
  the selected directory during listing/discovery.
- **Provider fallback is user-configured only.** Retries are bounded by
  `PC_AGENT_MAX_RETRIES`; no provider is contacted merely because it exists
  as a slot name.
- **Audit without secrets.** Plans, approval decisions, tool args/results and
  provider attempt outcomes are stored locally; secret-like fields/content
  are redacted from execution JSON and keys are never summary output.

## Important Phase 1C boundaries

- Natural-language orchestration supports PDF merge/folders, PDF verify,
  PDF inspect, and file listing. It cannot yet write arbitrary text through
  the orchestrator, run shell commands, control desktop apps, or browse.
- Direct Anthropic integration is a reserved provider slot only.
- Tool timeouts are declared/measured, not hard process kills. Isolated
  execution is Phase 2 territory.
- There is no dynamic tool creation, GUI, cloud worker, licensing, billing,
  or self-repair of the core runtime yet.
