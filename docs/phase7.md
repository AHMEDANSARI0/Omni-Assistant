# Phase 7 Foundation (0.9.0, schema v6)

Phase 7 implements the personal-use foundation that the master prompt
requires before adding more tools: **independent acceptance-criteria
verification, step checkpoints with safe resume, cooperative cancellation,
and an inspectable memory layer**. It adds no new external tools and does
not weaken any existing safety rule. Licensing (Phase 6) is unchanged and
still off by default; the product direction is personal-use first.

## What changed

### 1. Acceptance-criteria verification (`src/pc_agent/verification.py`)

Every execution attempt now builds deterministic acceptance criteria from
the selected plan:

| Criterion | What it checks |
|---|---|
| `tool_verification` | The existing independent tool-specific verification still passes |
| `output_artifact` | The written output exists inside the workspace and is non-empty (path re-confined during evaluation) |
| `output_hash` | SHA-256 recomputed from the actual file matches the tool report |
| `pdf_pages` | Page count positive (and equal to plan expectation when known) |
| `row_count` | CSV/JSON data rows are recounted from the actual output file |

A task now only succeeds when **both** the legacy verification and **all**
criteria pass. Criterion failures flow through the same bounded recovery
loop as verification failures. Every evaluation is persisted to
`task_verifications` (migration v6) and shown as `[PASS]`/`[FAIL]` lines in
CLI reports, `task show`, and the GUI.

### 2. Checkpoints and resume

After a tool invocation succeeds, the orchestrator saves a checkpoint
(`task_checkpoints`, keyed by task + step) containing the tool output, the
output artifact paths, and a SHA-256 fingerprint of the exact plan.
`resume` continues the **same task** when it is non-terminal:

```cmd
pc-agent task resume latest
pc-agent task resume <task-id> --force
```

Safety rules:

- Terminal tasks cannot resume; failed tasks keep using diagnose/recover.
- A task updated within the last 120 seconds is treated as possibly running
  in another process and is refused unless `--force` is given.
- The stored plan is revalidated against the live registry, version checks,
  parameter validation, workspace confinement, and policy allow-lists.
- A checkpoint is trusted only when the plan fingerprint matches and every
  recorded artifact still exists in the workspace. Otherwise it is
  invalidated (`checkpoint_invalidated` event) and the tool runs again.
- Approval is requested again unless the same task already holds an approved
  identical tool/root/plan scope **and** a valid checkpoint proves the
  approved work already ran.
- Even with a valid checkpoint, verification and all acceptance criteria run
  again independently — a checkpoint never skips verification.

### 3. Cooperative cancellation

```cmd
pc-agent task cancel latest
```

- Queued tasks cancel immediately; an actively running task records a
  persistent request (`task_cancellations`) and stops at the next stage
  boundary (before planning is saved, after approval, after a tool call,
  between retries). It never kills a tool mid-call and never deletes files.
- The GUI **Cancel** button also works while the GUI itself is running the
  task (in-process flag + database flag).
- `Ctrl+C` in the CLI prints resume/cancel recovery guidance and exits 130;
  the checkpoint lets you continue later.

### 4. Memory layer

```cmd
pc-agent memory stats
pc-agent memory prefs
pc-agent memory pref-set editor notepad
pc-agent memory pref-get editor
pc-agent memory pref-delete editor
pc-agent memory note-set home "call office tomorrow"
pc-agent memory notes
pc-agent memory note-show home
pc-agent memory note-delete home
pc-agent memory workflows
pc-agent memory clear --yes
pc-agent task delete <task-id> --yes
```

- `memory_items` stores user **notes** and automatic **workflow records**.
  Every verified task success upserts a workflow record (tool, plan
  fingerprint, success count, last task). It is evidence for future learned
  reuse — prompts and memory can never grant authority.
- Secret-like preference/note keys (`api_key`, `token`, `password`, …) are
  refused; credentials never belong in memory.
- `memory clear --yes` removes preferences/notes/workflow records but keeps
  task history. `task delete --yes` removes one terminal task's database
  records (workspace files are never touched); a task referenced by recovery
  history is kept to preserve the audit trail.

## Verification evidence

- New `tests/test_phase7_foundation.py` (17 tests): criteria building,
  independent hash/row recounts, checkpoint + verification + workflow
  persistence, cancellation before/after the tool stage, simulated process
  crash during verification followed by checkpoint-reusing resume, stale
  guard + force, artifact-missing invalidation and tool re-run, terminal
  rejection, memory CRUD/secret guard, CLI cancel/delete/show, GUI
  controller cancel/resume/details, and schema v5→v6 upgrade.
- A cross-process smoke also crashed a task mid-verification in one process
  and resumed it in another (`task resume latest`) without re-running the
  merge tool.

## Honest remaining work

- Plans are still single-tool, so checkpoints are step-0 only. Multi-step
  plans with dependencies/deadlines are a later phase.
- Cancellation is stage-boundary only; a long tool call cannot be
  interrupted mid-call, and recovery clones started from the GUI cannot yet
  be cancelled from the GUI (they can via `task cancel` from another CLI).
- Workflow memory is recorded but never auto-reused yet; verified reuse,
  preference-driven planning, and secure OS credential storage are future
  phases.
- Planner still cannot select custom dynamic tools; no new tool adapters
  (file operations, richer Office editing, subprocess/Git, browser,
  Windows UI, images) were added in this foundation phase.
- Live real-provider (Gemini/OpenRouter) generation remains unverified on
  the user's machine; no real provider keys have been supplied.
