# Transformation Phase 4 — Mission Center, dependencies, scheduler, execution graph

## 1. Existing findings

- Tasks were independent records; the orchestrator chained 2–5 planner
  steps inside one task but there was no way to express "task B runs
  after task A succeeds", run independent tasks in parallel, or run a
  task on a schedule. Phase 3's Local Service exposed single-task runs
  only.
- Existing machinery that could be reused safely: repositories open a
  fresh SQLite connection per call (WAL), `TaskOrchestrator.execute`
  scopes budgets per thread via ContextVars, and each orchestrator owns
  its own Safety Kernel/worker.

## 2. What changed

### Schema v9: missions, membership, dependencies, schedules
- `missions` (id, name, description, status, timestamps).
- `mission_tasks` (mission_id, task_id, position) — a task may belong
  to several missions; deleting a mission never deletes its tasks.
- `mission_dependencies` (mission-scoped `task_id → depends_on_task_id`
  edges with FKs into `mission_tasks`). Cycles are rejected when an
  edge is added and again before a run.
- `task_schedules` (`sch-…` ids): `once` (absolute time) or `interval`
  (60 s – 1 year), instruction, `use_ai`, enabled flag, next/last run,
  last task id, run count.

### Execution graph + parallel runner (`src/pc_agent/missions.py`)
- `execution_levels()` computes topological levels for display/API.
- `MissionRunner` executes with a ready-set ThreadPool bounded by
  `PC_AGENT_MISSION_MAX_PARALLEL` (1–8, default 3): a task starts as
  soon as all dependencies have SUCCEEDED. Each branch gets its own
  orchestrator (own Safety Kernel + isolated worker), so hard timeouts,
  emergency stop, Approval Center, budgets, verification and recovery
  behave exactly like single runs; approvals deny inline and file
  Approval Center requests as in Phase 3.
- If a dependency ends FAILED/CANCELLED/NEEDS_INPUT, dependents stay
  QUEUED with a `mission_blocked` task event (never silently skipped
  or re-planned) and the mission ends `failed`. Re-running a mission
  resumes: already-terminal task outcomes are reused, non-terminal
  tasks execute when their dependencies allow.
- Mission statuses: `draft → running → succeeded | failed | cancelled`.
  Cancel sets the mission cancel flag and requests cancellation for
  every non-terminal member task through the orchestrator.
- Events: `mission.created`, `mission.task_added`,
  `mission.run_started`, `mission.run_finished`,
  `mission.cancel_requested`, `mission.deleted`,
  `schedule.created/updated/deleted/triggered`.

### Scheduler
- `LocalService.run_due_schedules()`: for each enabled schedule whose
  time has come, create + start a task (existing background machinery),
  then advance it (interval: from the fire time — missed runs are not
  replayed; once: disabled after firing) and emit `schedule.triggered`.
- While the local API runs (`api serve` or an embedded server), a
  scheduler thread ticks every `PC_AGENT_SCHEDULER_INTERVAL_SECONDS`
  (default 30; `0` disables). Without the API, `pc-agent schedules
  tick` fires due schedules once and waits for those tasks.
- There is deliberately no OS-level cron/Task Scheduler registration in
  this phase: schedules fire only while the app (API) is running or
  when ticked. That is stated plainly rather than implied.

### Surfaces
- CLI: `pc-agent missions create|list|show|add-task|link|run|cancel|delete`
  and `pc-agent schedules add|list|toggle|delete|tick`
  (`--in-minutes` / `--every-minutes` keep commands placeholder-free).
- API: `/v1/missions` (GET/POST with inline `tasks:[{key, instruction,
  depends_on}]`), `/v1/missions/{id}` (GET/DELETE),
  `/v1/missions/{id}/run|cancel|tasks|graph`,
  `/v1/schedules` (GET/POST), `/v1/schedules/run-due`,
  `/v1/schedules/{id}/enabled`, DELETE `/v1/schedules/{id}`.
  `/v1/overview` + `/v1/health` now include mission/schedule counts and
  running missions.
- Doctor prints a `Mission center:` line; diagnostics gains a
  `missions` section; the release audit gains a `mission_center` check
  (19 total); the GUI doctor summary gains `mission_summary`.
- Profiles may now carry `mission_max_parallel` and
  `scheduler_interval_seconds` (same whitelist rules).

## 3. What was intentionally reused

- Task/orchestrator/Safety Kernel execution path unchanged; the Phase 3
  service run/cancel machinery; event system; config parser style.

## 4. Deferred

- Visual graph editing + JARVIS Mission Center page (Phase 5 UI will
  consume `/v1/missions/{id}/graph`).
- OS-level schedule registration, cron syntax, and missed-run replay
  policies.
- Cross-mission dependencies and shared-resource locks between
  concurrent missions (branches today bound only by `max_parallel` per
  mission; two missions may run simultaneously through the API).
