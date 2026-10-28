# Phase 11 Multi-Step Planner (0.13.0, schema v6 unchanged)

A task can now chain several tools into one verified pipeline instead of
stopping after a single step:

```cmd
pc-agent run "Merge PDFs from Folder A and Folder B then extract pages 1-2 from merged.pdf"
pc-agent run "Convert sales.csv to xlsx then set cell D5 to \"=SUM(C2:C4)\" then verify formulas"
```

## How planning works

- **Deterministic chains:** the instruction is split on the word `then`
  outside quotes (quoted find/replace text, paragraphs or search queries are
  never split). Each clause is planned with the exact same single-step
  planner, so every existing deterministic intent can be a step. At most
  **5 steps** per task.
- Path options (`--input-folder`, `--input-file`, `--target`, `--output`)
  belong to the **first** step; edit options (`--pages`, `--find`, `--cell`,
  ...) are available to the clause that needs them.
- **Promised outputs:** later clauses may reference files that earlier steps
  will create; plan-time existence checks know about them. Output-name
  defaults also avoid names promised to earlier steps.
- **Implicit hand-off:** a clause that names no file ("then extract pages
  1-2", "then verify formulas") inherits the previous step's planned output
  as its target when that plans cleanly and the format matches.
- **Run-time bindings:** when a step's `path`/`input_path` equals an earlier
  step's planned `output_path`, it is bound to that step's *actual* output.
  If bounded recovery renames a colliding output (e.g. `merged-recovered.pdf`),
  the next step automatically consumes the renamed file — never a stale name.
- **AI plans:** with `--use-ai`, a provider may return a `steps` array
  (2–5). Every step passes the same strict validation as single-step AI
  plans; the only cross-step reference allowed is the sentinel
  `$step<N>.output_path` (an earlier step), which becomes a run-time binding.
  Forward references, unknown fields and non-allow-listed tools are rejected.

## How execution works

- Each step is an ordinary single-tool plan run through the unchanged
  pipeline: **fresh approval per step** for external read roots (never
  inherited), acceptance criteria built from the step, deterministic
  verification, a checkpoint under the step's own index, and the same
  bounded recovery classification. The `--max-recovery-attempts` budget
  applies **per step**; attempt numbers stay unique across the pipeline.
- The pipeline stops at the first step that fails, needs input, or is
  cancelled/denied. The task succeeds only when the **final** step verifies.
- Reports show every step: `Step 1/3 [succeeded] pdf.merge_folders - ...`
  in CLI text and the GUI, plus a `steps` array in `--json` output.
- **Resume:** an interrupted multi-step task resumes at the first
  unfinished step. Steps whose checkpointed result still matches the plan
  fingerprint and whose artifacts exist are verified again without
  re-running their tool; later steps resolve their bindings from the
  checkpointed outputs.
- **Recovery clones:** `task recover` on a failed multi-step task re-runs
  the stored chain from step 1 in the linked clone (writers pick fresh
  outputs on collision, and bindings follow).

## Workflow memory (Phase 10 + 11)

A verified chain is recorded as one workflow (`intent: multi_step`) storing
every step's exact arguments plus the bindings. `pc-agent workflow run latest`
and the planner fallback replay the whole chain after live revalidation of
**every** step against the registry and orchestration allow-list — a forged
step rejects the record before any tool runs.

## Honest boundaries

- Bindings cover single `path`/`input_path` arguments fed from an earlier
  step's `output_path`. List inputs (e.g. merging several earlier outputs)
  and `pdf.split` folder outputs are not bindable yet.
- Steps run strictly in order; there is no branching, looping or parallel
  execution, and a failed step is retried only by the existing bounded
  recovery — the chain is never re-planned mid-flight.
- Files moved/renamed away by an earlier step still look present to later
  plan-time checks; such a chain then fails honestly at run time.
- No new dependency and no database migration: plans, checkpoints and
  approvals were already stored per step index as JSON.
