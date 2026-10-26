# Changelog

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
