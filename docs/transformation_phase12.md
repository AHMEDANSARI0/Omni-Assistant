# Transformation Phase 12 — Visual Workflow Builder, Triggers, Schedules, Import/Export

Version **0.74.0** · label **TRANSFORMATION PHASE 12 Visual Workflow Builder** ·
schema **v14** · release audit **26 checks** · registered tools **69** (unchanged:
a flow is compiled into ordinary Mission Center tasks, it is *not* a new tool) ·
GUI tabs **14** · HTTP routes **+18** · CLI group `flows`

Phase 12 lets a user build a repeatable automation **without writing code**: a
small JSON flow document with named nodes and edges is validated, versioned,
stored in SQLite, compiled into a Mission Center mission (so every node keeps the
same Safety Kernel, approvals, token budgets, hard timeouts, verification and
recovery as a hand-written mission), and can be fired by a trigger, exported to
a checksummed file, or re-imported on another machine.

---

## 1. What existed before this phase (0.73.0)

| Area | State |
| --- | --- |
| Mission Center | `missions`, `mission_tasks`, `mission_dependencies`; `execution_levels()` parallel levels; `MissionRunner` with approvals, verification, cancellation, recovery |
| Scheduling | `task_schedules` for single tasks; `app_events` event log with typed `event_type` |
| Automation | browser/desktop/vision tools behind opt-in gates + `automation_runs` ledger (schema v13) |
| Surfaces | CLI (`missions`, `schedules`, `automation`), HTTP API, GUI (13 tabs), web command center |
| Composing work | Only by hand: a task instruction, or a mission created through the API/CLI with explicit `task_specs` |

## 2. What was missing

1. **No reusable multi-step definition.** Every automation was a one-off task
   instruction; there was no stored, named, versioned graph a user could run
   again, edit, or share.
2. **No validation contract.** Nothing refused a graph with a cycle, a duplicate
   node id, an unknown key or a typo'd variable — the classic way a "workflow
   builder" produces a mission that fails halfway.
3. **No triggers for missions.** `task_schedules` covered single tasks only;
   nothing could say "run this graph every 15 minutes" or "run it when a task
   completes".
4. **No portability.** No export/import, so a flow could not move between
   machines and there was no tamper evidence.
5. **No run ledger for graphs.** Mission Center showed missions, but there was
   no per-flow history answering "how many times did this run, triggered by
   what, and did it start?".
6. **No UI.** Building a graph meant editing JSON in a terminal.

## 3. What was built

### 3.1 Engine — `src/pc_agent/flows.py` (new, ~1,000 lines)

* `FLOW_SCHEMA = 1` — version of the portable document format.
* `LIMITS`: `max_nodes 20`, `max_variables 8`, `max_name 120`,
  `max_description 2000`, `max_triggers_per_flow 8`, `min_schedule_seconds 60`,
  `max_schedule_seconds 30 d`, `max_import_bytes 64 000`, `max_runs_listed 500`.
* `validate_definition()` — strict: unknown keys are **refused** (not ignored),
  `flow_schema` must be 1, node ids must match `^[a-z0-9][a-z0-9_-]{0,31}$`,
  duplicate ids/edges and edges to unknown nodes are refused, `{{variable}}`
  placeholders must resolve against `variables`, and every problem names the
  exact field (`flow.nodes[2].id`).
* `FlowDefinition` — `dependencies()`, `topological_levels()` (Kahn; a cycle
  raises `FlowError("The flow graph contains a cycle.")`) and `mission_specs()`
  which yields `[{key, instruction, depends_on}]` with `{{var}}` substituted.
* `FlowStore` — CRUD + **immutable versions** (`flow_versions` keeps every
  version with its SHA-256), enable/disable, triggers, runs, `summary()`,
  `due_triggers(now=…)`, `pending_events()` watermark, `has_active_run()`.
* `FlowRunner` — `run_flow()` creates a run row, compiles the mission through an
  injected `start_mission`, records `started`/`failed` and emits
  `flow.run_started`; `reconcile()` closes runs whose mission already finished
  (re-read from Mission Center, never guessed from a timer); `tick(now=…)` fires
  each due schedule trigger and each event trigger **at most once per tick**, with
  a hard budget (`limit <= 50`).
* `export_payload()` / `load_export_payload()` — portable document
  `{flow_schema, exported_by, flow, triggers, checksum}` where `checksum` is
  SHA-256 over canonical JSON of `{flow, triggers}`; a document edited after
  export is refused, and unknown top-level/trigger keys are refused too.

### 3.2 Storage — schema **v14**

| Table | Purpose |
| --- | --- |
| `flow_definitions` | current definition, name, version, SHA-256, enabled flag |
| `flow_versions` | every version, `UNIQUE(flow_id, version)`, definition + hash |
| `flow_triggers` | `kind` (`manual`/`schedule`/`event`), interval / `run_at` / `event_type`, `next_run_at`, `last_event_id` watermark, `run_count`, enabled |
| `flow_runs` | one row per run: flow name/version, trigger kind/id, mission id, status, node count, redacted error, timestamps |

Indexes: `idx_flow_definitions_name`, `idx_flow_versions_flow`,
`idx_flow_triggers_due`, `idx_flow_triggers_flow`, `idx_flow_runs_flow`,
`idx_flow_runs_status`. Migration 14 is applied **after** migration 13 (the
automation ledger from Phase 11 stays intact — verified by test).

### 3.3 Service — 22 methods

`list_flows`, `get_flow`, `validate_flow`, `create_flow`, `update_flow`,
`set_flow_enabled`, `delete_flow`, `export_flow`, `import_flow`, `run_flow`,
`flow_runs`, `flow_add_trigger`, `flow_triggers`,
`flow_set_trigger_enabled`, `flow_delete_trigger`, `flow_tick`, `flow_stats`,
`resolve_flow_id` (full id, unique id prefix, or exact name) and the internal
`_flow_store`, `_flow_runner`, `_flow_mission_state`, `_start_flow_mission`.

### 3.4 CLI — `pc-agent flows …`

```
flows list [--limit N] [--json]      flows show REF [--json]
flows create --file F [--json]       flows update REF --file F [--json]
flows validate --file F [--json]     flows enable|disable REF [--json]
flows delete REF [--json]            flows run REF [--json]
flows runs [REF] [--limit N] [--json]
flows export REF [--out F] [--json]  flows import --file F [--overwrite] [--json]
flows trigger add REF --kind schedule --interval-seconds N | --run-at ISO | --event-type T [--json]
flows trigger list [REF] | enable ID | disable ID | delete ID [--json]
flows tick [--limit N] [--json]      flows stats [--json]
```

`pc-agent doctor` now prints a live `Flows: flows=…; triggers_enabled=…;
runs=… (by status); last=…` line directly after `Automation:`.

### 3.5 HTTP API (local only, bearer token, loopback)

| Method + path | Result |
| --- | --- |
| `GET /v1/flows?limit=` | list + summary |
| `POST /v1/flows` | 201, validated create |
| `POST /v1/flows/validate` | dry validation (levels + node count) |
| `POST /v1/flows/import` `{document, overwrite}` | 201, checksum-verified import |
| `POST /v1/flows/tick` `{limit}` | bounded trigger sweep |
| `GET /v1/flows/stats` | flow/trigger/run counters |
| `GET /v1/flow-runs?flow_id=&limit=` | run ledger |
| `GET /v1/flows/{ref}` · `…/export` · `…/runs` · `…/triggers` | read surfaces |
| `POST /v1/flows/{ref}/run` | 202, mission started |
| `POST /v1/flows/{ref}/triggers` | 201, new trigger |
| `POST /v1/flows/{ref}/enabled` `{enabled}` | enable/disable |
| `POST /v1/flows/{ref}/definition` | update (version bump) |
| `POST /v1/flow-triggers/{id}/enabled` `{enabled}` | toggle one trigger |
| `DELETE /v1/flows/{ref}` · `DELETE /v1/flow-triggers/{id}` | delete |

Updates use `POST …/definition` because the server deliberately implements only
`GET`/`POST`/`DELETE` (no `PUT`/`PATCH` handler exists — an earlier `PUT` attempt
returned HTTP 501 and is documented as a dead end).

### 3.6 GUI — 14th tab `Workflows`

Live, real-state panel: summary line, flow list (`id · version · enabled · nodes ·
name`), a JSON document box, and buttons that call the same service methods —
Validate, Save new, Update, Import, Show, Run, Export (loads the exported,
checksummed document into the box), Tick triggers, Add schedule, Add event
trigger — plus a "Recent flow runs" list. Every button performs a real action and
long calls run on the worker thread (`_run_async`), so the window never freezes.

### 3.7 Web command center

A `Workflow Builder` panel with the same live state (`/v1/flows`, `/v1/flow-runs`),
inline create/import/run/show/tick/export controls and a JSON result area.

### 3.8 Diagnostics, release audit, security hardening

* `diagnostics.flows_summary_line()` + a `flows` section in `build_diagnostics`.
* Release audit check 26 `flow_builder` (PASS) — audit is now **26 checks**.
* `redact_text()` was widened to catch bare provider keys (`sk-…`, `ghp_…`,
  `AIza…`, `xai-…`, `gsk_…`) with no `api_key=` label; flow run errors pass
  through it, so a rejected key can never be persisted in `flow_runs.error`.
* `FlowRunner.reconcile()` replaces a would-be "flow locked forever" bug: the
  one-active-run guard now asks Mission Center whether the previous run's mission
  finished instead of trusting a timestamp.

## 4. Files changed

New: `src/pc_agent/flows.py`, `docs/transformation_phase12.md`,
`tests/test_transformation_phase12_flows.py`.

Modified: `errors.py` (`FlowError`), `storage/db.py` (v14 + ordered migration),
`service.py` (flow surface + mission-state injection), `cli.py` (`flows` group,
`--json` on every sub-command, doctor line), `api_server.py` (18 routes),
`diagnostics.py`, `release.py` (check 26), `gui/controller.py` (flow actions),
`gui/app.py` (14th tab), `web_ui.py` (Workflow Builder panel), `logging_config.py`
(redaction), `__init__.py`, `pyproject.toml`, `baseline.py`, all delivery docs,
`tests/*` (identity sweep), `room_check.sh`.

## 5. Verification

* `tests/test_transformation_phase12_flows.py` — **36 tests**: validation
  positives/negatives (10 parametrised refusals), cycle detection, limits, id
  slugs, export checksum + tamper refusal, import size/unknown-key refusal,
  immutable versions, delete cascades, disabled-flow refusal, one-active-run
  guard, `reconcile()` on mission completion, mission compilation with
  dependencies, failed-start recording with secret redaction, schedule interval
  firing, event watermark (no replay), trigger bounds and per-flow cap, service
  end-to-end, 404 for unknown flow, HTTP route sweep, CLI sweep, diagnostics +
  release check, schema v14 tables, identity, GUI controller actions.
* Full suite on Linux sandbox: **793 passed, 1 skipped** (0.74.0).

## 6. Security implications

* A flow can only produce a **mission** — it cannot execute a shell, open a
  socket, or bypass a tool gate; the Safety Kernel, approval model and per-task
  timeouts apply unchanged.
* Strict schemas: unknown keys refused at every level; node ids are slugs so
  they can never be injected into a log line or a template.
* Import is bounded (64 KB), checksum-verified and key-strict; overwrite needs an
  explicit flag.
* Triggers are bounded (8 per flow, per-tick budget, one firing per trigger per
  tick, no replay storms) and disabled flows are skipped, never executed late.
* Run errors are redacted before storage; no flow document, transcript or secret
  is written to logs.
* The API stays loopback + bearer token; no new network listener, no PUT/PATCH.

## 7. Performance implications (measured, Linux sandbox, 0.74.0)

| Operation | Median | Max |
| --- | --- | --- |
| `validate_definition` (5 nodes) | 0.043 ms | 0.171 ms |
| `FlowStore.get` | 0.711 ms | 1.05 ms |
| `list_flows(limit=20)` | 0.727 ms | 1.06 ms |
| `run_flow` (compile + start + ledger) | 7.6 ms | 12.9 ms |
| `tick` with 8 due triggers | 1.42 ms | 2.33 ms |
| `reconcile` over 50 runs | 0.63 ms | — |

Cost is dominated by SQLite writes, not by Python; validation is microseconds.

## 8. Limitations (honest)

1. **Linear DAG only.** Nodes are instructions with edges; there are no
   conditions, loops, joins-with-merge-logic or per-node model/tool selection.
2. **No drag-and-drop canvas.** The GUI/web editor is a JSON document plus
   buttons; a visual node canvas is *MEDIUM* future work.
3. **Event triggers poll `app_events`.** No webhook or push until Phase 13
   connectors; the watermark guarantees no double firing.
4. **Schedules need a running assistant.** `flows tick` is called by the CLI, the
   API, the GUI or the scheduler loop; a closed app fires nothing (and claims
   nothing).
5. **Single-process assumption.** Two instances pointed at one database could
   both tick; the one-active-run guard limits the damage but a DB lock is BARA
   work for the cloud/coordination phase.
6. **Import replaces by name.** Two different flows with the same name collide
   unless `--overwrite` is used; naming is the user's contract.

## 8b. Windows evidence folded in (0.73.0 build -> 0.74.0)

The user's first native Windows run of 0.73.0 produced 746 passed / 8 skipped /
1 failed. The failure was genuine:

* **Symptom:** `test_live_snapshot_on_this_machine` failed with
  `assert second.top_processes` -> `[]`, even though CPU, memory, network, disk
  and the agent's own RSS were all live (`GetSystemTimes`, `GetIfTable`,
  `K32GetProcessMemoryInfo`).
* **Cause:** `WindowsProcessApi._resolve()` deliberately returns
  `(function, export_name)` because several DLL/export spellings are probed. The
  memory entry points unwrapped `entry[0]`; the enumeration entry did not, so
  `enum_processes` was a tuple. Calling it raised `TypeError`, and the scan's
  `except Exception` turned that into "no processes" forever.
* **Fix:** unwrap the enumeration entry; request
  `PROCESS_QUERY_LIMITED_INFORMATION` as a fallback when the full query rights
  are refused; accept an injected DLL loader so the behaviour is testable
  anywhere.
* **Coverage:** three new tests — `callable(api.enum_processes)` (the exact
  regression), working-set sort + limit mapping via `_windows_process_rows`, and
  skipping denied handles. They run on Linux and Windows in CI and in every room.

The 8 skips were environment-gated (4 symlink-privilege, 4 Linux-only oracles),
not defects. After this patcher, `pc-agent doctor` also prints the
`Flows:` line and the database migrates to schema v14.

## 9. Next tasks (Phase 13)

Calendar, email, chat, cloud-drive and webhook connectors — including turning the
event-trigger source from polling into signed webhooks, and adding connector
actions that flows can use as nodes.
