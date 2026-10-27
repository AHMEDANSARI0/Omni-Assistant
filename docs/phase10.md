# Phase 10 Verified Workflow Reuse (0.12.0, schema v6 unchanged)

Phase 7 recorded *evidence* of every verified success. Phase 10 turns that
evidence into safe reuse: a previously verified plan can be re-run as a new
task, and the planner can fall back to it — but **memory still never grants
authority**. Every reused plan is rebuilt, checked against the live tool
registry and orchestration allow-list, saved, approved, executed, verified
and checkpointed through exactly the same pipeline as a fresh plan.

## What is recorded now

On each verified success the workflow record (`memory_items`, kind
`workflow`) stores the exact reusable plan material:

- normalized instruction (case/whitespace-insensitive identity),
- intent + tool id + the exact validated `arguments`,
- external read roots and expected page count,
- `success_count` and the last verified task id.

The key is derived from intent + tool + normalized instruction + arguments
excluding `output_path`, so repeating the same work to a fresh output grows
the same workflow's verified count. Phase 7 legacy records (no stored
arguments) remain usable: their plan is resolved from the last verified
task's stored plan.

## Ways to reuse

```cmd
pc-agent workflow list
pc-agent workflow show latest
pc-agent workflow run latest
pc-agent workflow run wf-1e5fb79487738cfe
pc-agent workflow delete latest --yes
```

- Keys accept an exact key, a unique prefix, or `latest`.
- `workflow run` creates a **new task** from the recorded instruction,
  revalidates the stored plan live, and runs it with normal approvals
  (external folders are approved again — never inherited), acceptance
  criteria, checkpoints and bounded recovery. Output collisions heal to a
  fresh `-recovered` output for writer tools.
- **Planner fallback:** if deterministic (and optional AI) planning cannot
  produce a plan for an instruction, the orchestrator looks for a verified
  workflow with the same normalized instruction. If a still-valid plan
  exists it is reused (reported as `Verified workflow reused: wf-...`)
  instead of returning `needs_input`. This covers repeats whose original
  run needed CLI options or an AI plan.
- Normal successful runs print `Verified workflow recorded: wf-...
  (success #N)` so learning is always visible.
- The GUI Tasks tab gains **Run Verified Workflow...**, which lists known
  keys and runs the chosen one as a new task.

## Safety rules

- A record whose tool left the allow-list/registry, or whose stored plan is
  unreadable, is rejected with `no longer maps to a valid allowed plan`;
  no tool runs.
- Approvals are never inherited from the original run: external read roots
  go through the exact-scope approval flow again (denial cancels safely).
- All reused outputs still obey workspace confinement at tool execution
  time, no-overwrite rules, criteria and verification. If the world changed
  (inputs moved/deleted), the run fails or heals exactly like a normal task
  — it is never force-succeeded from memory.
- Deleting a workflow record deletes only the record; task history and
  workspace files are untouched.

## Honest boundaries

- Matching is exact normalized instruction only — no fuzzy/semantic
  matching and no argument re-binding (e.g. "same merge but other folders"
  is a new instruction and plans normally).
- Single-step plans only, like everything else until the multi-step
  planner phase. Reuse is a faster, evidence-backed repeat — not yet
  learned generalization.
- Secure OS credential storage (the second half of the original memory
  item) remains future work; secret-like keys are still refused in memory.
