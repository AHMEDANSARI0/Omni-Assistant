# Transformation Phase 9 — Specialized Multi-Agent Runtime & Agent Studio

Version **0.71.0** · label **TRANSFORMATION PHASE 9 Multi-Agent Studio** ·
database **schema v11** · builtin tools **64** (unchanged).

## What existed before

- One generalist execution path: every task, mission branch, and plan had
  access to the full builtin registry (subject to policy/approvals).
- Mission Center (Phase 4) already provided dependency graphs with bounded
  parallel branches, and Memory v2/knowledge (Phase 8) gave agents something
  durable to read and write.

## What this phase adds

### Specialist agents (least privilege by construction)
- An agent is a named record (schema v11 `agent_definitions`): role,
  description, persona system prompt, a hard allow-list of builtin tools
  (or `*`), optional preferred provider, step cap (1–5), optional token
  budget, builtin/enabled flags.
- Every agent run is an ordinary task executed by a `TaskOrchestrator`
  built with a **filtered `ToolRegistry`** containing only that agent's
  tools. Planning, Safety Kernel deadlines/worker isolation, approvals,
  verification, recovery, and budgets are unchanged — a disallowed tool
  simply does not exist for that run (verified in tests: the read-only
  `reviewer` attempting a PDF merge ends `needs_input` with zero side
  effects). Dynamic tools are never included in an agent registry.
- Six builtin specialists seed on first use (idempotent, never overwriting
  user edits): `coordinator` (no tools; decomposition only), `researcher`,
  `document-worker`, `data-analyst`, `knowledge-keeper`, `reviewer`.
  Builtins can be edited/disabled but not deleted; custom agents are full
  CRUD (`agents create|show|delete|enable|disable`).

### Persona-aware AI planning
- `PlannerOptions` gains `agent_system_prompt` and `allowed_tool_ids`.
  The AI planner's tool catalog is narrowed to the agent's tools, the
  persona is appended to the constrained-planner system instruction, and
  a returned plan naming an excluded tool is rejected before execution.
  Deterministic planning is unchanged; restriction is enforced at the
  registry either way.

### Team runs (depth-1 delegation)
- A team run executes ≤6 assignments `{id, agent, instruction,
  depends_on}` as a topological level graph (reusing the Mission
  Center's `execution_levels`): levels run with bounded parallelism
  (`mission_max_parallel`), each branch gets its own orchestrator, and a
  failed/`needs_input` dependency marks dependents `blocked` without
  creating their tasks.
- Assignments are explicit (CLI `--assignments file.json`, API body) or
  proposed by the `coordinator` via one strictly validated JSON router
  completion: unknown/disabled agents, coordinator-as-worker, forward
  dependencies, duplicates, oversized plans, and non-JSON replies are
  all rejected with `AgentError`. The goal is treated as untrusted data.
- Parallel branches intentionally run **without** an interactive approval
  callback (a shared prompt cannot be raced); Approval Center/policy
  rules still apply and surface as `needs_input`.
- Every run is ledgered in `agent_runs` (solo + team members) and linked
  to its real task id; events `agent.run_started/run_completed/
  team_started` flow into the Phase 3 event system.

### Surfaces
- CLI: `agents list|show|create|delete|enable|disable|run|team|runs|stats`;
  doctor `Agents:` line. `--ai` (and coordinator decomposition) are
  license-gated like other provider use.
- API (background, 202-style like tasks): `GET/POST /v1/agents`,
  `GET/DELETE /v1/agents/{name}`, `POST /v1/agents/{name}/run`,
  `POST /v1/agents/team`, `GET /v1/agents/team/{id}`,
  `GET /v1/agents/runs|stats`.
- GUI: **Agents tab** (11th) — Agent Studio editor, enable/disable/delete,
  solo run, team run, recent runs.
- Web Command Center: Agent Studio panel (live list/stats/runs, run/team
  actions). Diagnostics `agents` section; release-audit check 23
  (`agent_studio`).

## Security notes

- No new tools and no tool-reachable delegation: agents cannot spawn
  agents, and a running task gains no way to widen its own registry.
- The coordinator only *proposes*; local validation decides. Instructions
  inside assignments remain ordinary task instructions under the same
  policy/approval/budget machinery — team membership grants nothing.
- Preferred-provider is a reordering of the user's configured route only.

## Limitations (honest)

- Delegation depth is 1 and team size ≤ 6; no agent-to-agent chat,
  shared scratchpad, or result-passing beyond the ledger/knowledge base
  (a dependent reads files/memory its predecessor produced, like any task).
- The coordinator's decomposition quality is only as good as the
  configured model; with the offline mock it cannot decompose (explicit
  assignments still work fully offline).
- Agent personas affect AI planning only; deterministic intents behave
  identically for every agent (restriction still applies).

## Windows acceptance

Still **UNVERIFIED** — no Windows run has been reported for Transformation
Phases 1–9. Run the delivered patcher on `D:\omni-assistant`, then the usual
commands (one at a time) from the delivery message.
