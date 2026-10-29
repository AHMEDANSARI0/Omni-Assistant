# Phase 17: Core Self-Repair + Token/Task Budgets

Phase 17 adds two deterministic safety systems:

1. **Core self-repair** for restoring the agent's own eligible source files
   from a user-captured known-good baseline; and
2. **Per-task budgets** for provider tokens/calls, planned tool executions,
   planned steps and wall-clock time.

It deliberately does **not** let an AI model rewrite core code. There is no
self-repair tool in the tool registry, no planner intent for it, and provider
output cannot create, stage, apply or roll back a repair.

## Core self-repair safety model

Self-repair is disabled until the user sets this in the local `.env`:

```dotenv
PC_AGENT_ALLOW_SELF_REPAIR=1
```

Only these project paths are baseline-eligible:

- files under `src/`;
- files under `tests/`;
- `pyproject.toml`.

Generated Python caches are handled separately and are never part of the
baseline. Diagnosis can produce only two operation types:

- `restore` one eligible file from the baseline snapshot;
- `delete_cache` for a generated `.pyc`/`.pyo` file or a file under a
  `__pycache__` folder inside `src/` or `tests/`.

Unknown or newly added eligible files are reported as **extra files**. They
are never deleted automatically. Files outside the eligible set—including
`.env`, `data/`, `workspace/`, the virtual environment, docs and examples—are
never restored or deleted by self-repair.

The baseline is stored under `data/self-repair/baseline/`, and each run is
stored under `data/self-repair/runs/`. Every baseline file has a SHA-256 and
size in the manifest. A missing/corrupt snapshot, an invalid baseline path,
or a baseline captured for a different package version blocks staging. The
baseline is a local known-good snapshot, not a signed release artifact: it is
intended to detect and reverse accidental local damage under the same user
account, not to prove provenance against an attacker who can already rewrite
both the project and its `data` folder consistently.

## Baseline and repair workflow

Capture a baseline only after the installed version passes its tests. The
baseline command refuses a tree with Python compile/version errors and then
runs the selected fixed health-check profile. `--check full` is recommended
for a release baseline.

```powershell
pc-agent doctor
pc-agent self-repair baseline --check full --yes
pc-agent self-repair diagnose
```

For a later release, capture a fresh baseline after that release passes:

```powershell
pc-agent self-repair baseline --check full --force --yes
```

When damage is suspected:

```powershell
pc-agent self-repair diagnose
pc-agent self-repair stage --check full
pc-agent self-repair show latest
pc-agent self-repair apply latest --check import --yes
```

Rollback remains available for an applied run:

```powershell
pc-agent self-repair rollback latest --yes
```

### If `pc-agent` itself will not start

Use the minimal bootstrap entry point, which does not import the planner,
orchestrator, budgets or full CLI:

```powershell
pc-agent-self-repair diagnose
pc-agent-self-repair stage --check import
pc-agent-self-repair apply latest --check import --yes
```

It accepts the same self-repair subcommands and the same
`PC_AGENT_ALLOW_SELF_REPAIR` gate. If the bootstrap/service modules
themselves (`self_repair_cli.py`, `self_repair.py`, `errors.py` or
`logging_config.py`) are damaged, re-run the verified cumulative patcher
first; that external patcher remains the final fallback.

The available commands are `baseline`, `diagnose`, `stage`, `apply`,
`rollback`, `list` and `show`. Baseline capture, apply and rollback require
an interactive approval or the explicit `--yes` shown above. `latest` is an
alias for the newest recorded run; inspect it with `show latest` before
applying when in doubt.

### Staging and checks

`stage` copies the project into an isolated run folder, applies the
deterministic operations only there, and runs one fixed check profile:

- `static`: compile every eligible Python file and compare the
  `pyproject.toml` and package versions;
- `import`: static checks plus a fixed Python import/CLI-parser smoke check;
- `full`: import checks plus fixed `pytest -q` and `Ruff check` argv vectors.

No shell is used. Check subprocesses receive a small allow-listed
environment: provider API keys and `PC_AGENT_ALLOW_*` gates are not inherited,
Python bytecode writing is disabled, and credential lookup is forced to the
environment-only mode inside the child. `.env` files are excluded from the
staged copy.

Apply is blocked when:

- the staged checks failed;
- the live diagnosis changed after staging (a stale plan);
- the staged tree is missing;
- the baseline is corrupt, invalid or for another package version.

Before changing live files, apply records whether each target existed and
copies existing targets into the run backup. It then applies from the staged
tree and runs post-apply checks. If application or post-checks fail, it
restores the backup automatically and records the failure. Manual rollback
restores the exact pre-apply state for the run's operations, including
deleting a file that the repair had recreated and restoring a cache file that
the repair had deleted.

## GUI boundary

The GUI Dashboard shows the self-repair gate, baseline status, latest run and
configured task budgets. Its **Self-Repair Diagnose** button is read-only.
Baseline capture, stage, apply and rollback remain CLI-only so a repair
cannot be triggered from provider output or an incidental GUI task action.

## Per-task budgets

Task budgets apply to orchestrated execution paths: `execute`, `resume` and
verified-workflow replay. Direct one-shot tools, `pc-agent ask`, provider
status commands and Dynamic Tool Builder generation are not task executions
and therefore do not consume a task budget.

| Budget | Default | Environment variable | CLI override |
|---|---:|---|---|
| Provider tokens | 20,000 | `PC_AGENT_TASK_TOKEN_BUDGET` | `--token-budget` |
| Logical provider calls | 10 | `PC_AGENT_TASK_PROVIDER_CALL_BUDGET` | `--provider-call-budget` |
| Planned tool executions | 25 | `PC_AGENT_TASK_TOOL_BUDGET` | `--tool-budget` |
| Planned steps | 5 | `PC_AGENT_TASK_STEP_BUDGET` | `--step-budget` |
| Wall-clock seconds | 600 | `PC_AGENT_TASK_TIME_BUDGET_SECONDS` | `--task-time-budget-seconds` |

For token, provider-call, tool and time budgets, `0` means unlimited. The
step budget is always 1–20. The GUI Tasks tab exposes the same five per-run
overrides; blank fields use the configured defaults.

### Token accounting

Budgets are local safety ceilings, not billing quotes.

- Before a provider request is sent, the tracker reserves an estimated input
  count plus the request's maximum output tokens. If that reserve cannot fit,
  no provider request is sent.
- When a provider reports usage, common total/prompt/completion fields are
  counted (including OpenAI-style, Gemini-style and Ollama-style counters).
- Without reported usage, text is estimated at roughly four characters per
  token. Each inline image receives a conservative 1,024-token reserve
  because image accounting differs by provider/model.
- A **provider call** is one logical routed completion. Retries/fallbacks are
  reported separately as provider attempts, and failed attempts before a
  reported successful usage are conservatively estimated.
- If final reported/estimated usage exceeds the token budget, the AI
  planning/vision result is rejected instead of being allowed to drive
  further work.

The tool budget counts planned registry-tool executions, including bounded
recovery re-executions of a planned step. Internal verification helpers and
acceptance-criteria re-checks are part of proving the planned step and do
not consume additional tool budget. Step-budget checks run before the first
tool in every execute/resume/workflow path. Time and tool checks run before
each planned invocation; a checkpoint-resumed result that does not invoke
its tool again is not charged again.

Every orchestrated report includes a secret-free budget snapshot in text and
JSON output, and the task event log records `budget` or `budget_exceeded`.
Budget errors are policy-like for recovery classification: they are not
auto-retried, so a task cannot loop through recovery merely to spend the
same budget repeatedly.

Example:

```powershell
pc-agent run "Merge PDFs from Folder A and Folder B then extract pages 1-2" --step-budget 1 --json
```

That two-step plan fails before any tool runs. A normal deterministic task
still makes no provider call and reports zero provider tokens/calls.

## Deliberate limitations

- Self-repair cannot invent a fix for a logic bug; it can only restore the
  user's captured known-good files or remove generated Python caches.
- Capture a new baseline after every verified upgrade. An older baseline is
  intentionally blocked rather than silently downgrading changed files.
- Dependencies, the virtual environment, SQLite data, `.env`, workspace
  artifacts and non-eligible project files are outside self-repair.
- Token estimates are intentionally transparent approximations when a
  provider does not report usage. They bound local task behavior but do not
  predict an invoice.
- A budget does not interrupt a provider/tool call already in progress;
  time/token limits are enforced at deterministic boundaries before and
  after calls.
