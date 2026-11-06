# Transformation Phase 14 — Proactive Intelligence (0.76.0)

Reminders, a notification policy the user can reason about, and daily briefings
built only from state the product can actually see. Windows-first, local-first,
silent by default.

- Version: **0.76.0**
- Database schema: **v16** (`reminders`, `notifications`, `proactive_briefings`,
  `proactive_state`)
- Release audit checks: **28** (`proactive_intelligence` is the new one)
- Suite: **851 passed, 1 skipped** (skip = no desktop display for a real Tk window)
- New tests: `tests/test_transformation_phase14_proactive.py` (24 tests)

---

## 1. What already existed

- Phase 3 `LocalService` facade, the event log, structured logging, profiles and
  the settings surface every later phase reuses.
- Phase 4 Mission Center: dependency levels, parallel execution, task ledger,
  `Scheduler` loop inside `api_server._start_scheduler`.
- Phase 6 live monitoring (`SystemMonitor` snapshots, CPU/RAM/disk/processes).
- Phase 12 flows (`FlowStore`, run ledger, schedule/event triggers).
- Phase 13 connectors (`ConnectorService`: calendar, drive, email, chat,
  webhook) plus their ledgers.
- Safety Kernel, Approval Center, budgets, emergency stop, bounded tools,
  the 27-check release audit and `pc-agent doctor`.

## 2. What was missing

1. **Nothing could remind the user of anything.** Missions and flows ran, then
   finished; there was no durable "tell me at 07:30".
2. **No notification policy.** Any future notification feature would have
   flooded the user at 03:00 and there was no cap, cooldown or quiet window.
3. **No daily briefing.** The product knew the calendar, the synced drive, the
   flow runs, the open tasks and the machine health, but the user had to ask
   five separate questions every morning.
4. **No outbound gate.** Delivering a notification anywhere outside the
   process (webhook, chat) had no switch, no refusal reason and no ledger.
5. **The scheduler loop had no proactive step**, so nothing time-driven happened
   unless a flow happened to be scheduled.

## 3. What changed in this phase

### 3.1 New engine: `src/pc_agent/proactive.py`

| Part | Responsibility |
|---|---|
| `parse_due` | ISO-8601, `HH:MM` (next local occurrence), `+15m` / `-1m` / `+2h` / `+3d` offsets |
| `parse_quiet_window` / `in_quiet_window` / `next_quiet_end` | wraparound-aware quiet hours (`22:00-07:00` behaves correctly across midnight) |
| `ProactiveStore` | all SQLite access for the four v16 tables, every row mapped to a frozen dataclass |
| `ProactiveEngine` | `tick`, reminder CRUD, `notify`, `build_briefing`, `run_briefing`, `_deliver` policy, six briefing sections |

Delivery policy order inside `_deliver`, applied per notification:

1. quiet hours → defer (unless severity is `critical` **and**
   `quiet_hours_allow_critical` is on) and reschedule to the window end;
2. rolling-hour cap (`notification_max_per_hour`) → defer with reason;
3. cooldown per `dedupe_key` (`notification_cooldown_seconds`) → suppress;
4. in-app notification + event (`proactive.notification`) — always written;
5. optional outbound (`allow_proactive_outbound`) — a failure or refusal there
   never removes the in-app copy.

The in-app copy is therefore the floor, and outbound is strictly additive.

### 3.2 Briefing sections (all from live state, never mocked)

| Section | Source | Degrades to |
|---|---|---|
| `reminders` | due + upcoming open reminders | "no open reminders" |
| `calendar` | `ConnectorService.list_calendar_events()` for today | "connectors are off" / "no calendar file configured" |
| `drive` | drive connector index vs. last briefing snapshot | "drive connector is off" |
| `flows` | `FlowStore` run ledger, last 24 h | "no flow runs in 24 h" |
| `tasks` | open Mission Center tasks | "no open tasks" |
| `system` | `SystemMonitor` live snapshot (`cpu/memory/disk` as percentages) | "monitor unavailable" |

One briefing per local date, enforced by `briefing_for_date` — rerunning the
same day updates nothing and never double-delivers.

### 3.3 Configuration (`src/pc_agent/config.py`)

| Key | Default | Range / note |
|---|---|---|
| `allow_proactive` | on | master switch for the whole engine |
| `allow_proactive_outbound` | **off** | any delivery outside the process |
| `quiet_hours` | `""` (none) | `HH:MM-HH:MM`, wraparound, refused if malformed |
| `quiet_hours_allow_critical` | off | only `critical` may cross the window |
| `notification_max_per_hour` | 20 | 1–500 |
| `notification_cooldown_seconds` | 300 | 0–86400 |
| `briefing_enabled` | off | daily briefing switch |
| `briefing_time` | `08:00` | 24-hour clock, refused if malformed |
| `reminder_max_open` | 200 | 1–1000 |
| `proactive_tick_limit` | 25 | 1–200 per tick |

Bad values are **refused at load**, not silently coerced: `_parse_clock` and
`_parse_quiet_hours` raise `PcAgentError` with the offending key.

### 3.4 Wiring

- `service.py`: `proactive_status/summary/tick`, `add_reminder`, `reminders`,
  `resolve_reminder`, `dismiss_reminder`, `snooze_reminder`, `enable_reminder`,
  `delete_reminder`, `notifications`, `clear_notifications`, `run_briefing`,
  `briefings`, `notify_user`.
- `api_server.py`: 13 `/v1/proactive/*` routes, and the real scheduler loop now
  calls `service.proactive_tick(source="scheduler")` when `allow_proactive`.
- `cli.py`: `pc-agent proactive …` tree, plus `_fold_negative_values` so
  `--due -1m` is accepted by `argparse` instead of looking like a flag.
- GUI: 16th **Proactive** notebook tab with controller methods; the web
  dashboard has the same panel (`reminder-add`, `proactive-tick`,
  `proactive-briefing` markers) driving the same service calls.
- `diagnostics.py` / `doctor`: `Proactive:` line (open reminders, next due,
  notifications in the last hour against the cap, quiet window, briefing,
  outbound).
- `release.py`: 28th audit check `proactive_intelligence`.

### 3.5 CLI surface

```
pc-agent proactive status
pc-agent proactive tick [--limit N]
pc-agent proactive reminders list [--status open|done|dismissed] [--limit N]
pc-agent proactive reminders add --title T [--body B] --due WHEN [--severity S] [--repeat SECONDS]
pc-agent proactive reminders done|dismiss|delete REF
pc-agent proactive reminders snooze REF [--for +15m]
pc-agent proactive notifications list [--kind K] [--limit N]
pc-agent proactive notifications clear
pc-agent proactive briefing show
pc-agent proactive briefing run [--force]
pc-agent proactive notify --title T --body B [--severity S]
```

`WHEN` accepts `+15m`, `-1m`, `+2h`, `+3d`, `07:30` and full ISO-8601.

### 3.6 API surface

| Method | Path |
|---|---|
| GET | `/v1/proactive/status`, `/reminders`, `/notifications`, `/briefings` |
| POST | `/v1/proactive/tick`, `/reminders`, `/reminders/{id}/done`, `/reminders/{id}/dismiss`, `/reminders/{id}/snooze`, `/notify`, `/briefing`, `/notifications/clear` (13 endpoints in total; enable/disable is CLI-only and pauses a reminder without deleting it) |
| DELETE | `/v1/proactive/reminders/{id}` |

## 4. Files touched

New:
- `src/pc_agent/proactive.py`
- `tests/test_transformation_phase14_proactive.py`
- `docs/transformation_phase14.md`

Modified:
- `src/pc_agent/storage/db.py` (schema v16 + 4 tables + 5 indexes)
- `src/pc_agent/config.py`, `errors.py` (`ProactiveError`), `service.py`,
  `api_server.py`, `cli.py`, `diagnostics.py`, `release.py`, `baseline.py`,
  `gui/controller.py`, `gui/app.py`, `web_ui.py`, `__init__.py`, `pyproject.toml`
- `CHANGELOG.md`, `TEST_REPORT.md`, `FEATURE_MATRIX.md`, `ARCHITECTURE.md`,
  `API_DOCUMENTATION.md`, `IMPLEMENTATION_ROADMAP.md`, `SECURITY_REPORT.md`,
  `PERFORMANCE_REPORT.md`, `README.md`
- 63 test files (identity/version/schema/GUI-tab/release-check sweep to 0.76.0)

## 5. Tests and results

`tests/test_transformation_phase14_proactive.py` — 24 tests:

| Area | Test |
|---|---|
| identity | `test_phase14_identity_is_wired_through_the_product` |
| schema | `test_schema_v16_creates_the_proactive_ledger` |
| parsing | `test_due_parsing_accepts_iso_offsets_and_local_times`, `test_quiet_window_math_is_wraparound_aware`, `test_bad_quiet_hours_and_briefing_times_are_refused_not_ignored` |
| CRUD/limits | `test_reminder_crud_round_trip`, `test_reminder_validation_and_limits`, `test_proactive_engine_can_be_switched_off` |
| firing | `test_tick_fires_due_reminders_once_and_reschedules_repeats` |
| policy | `test_quiet_hours_defer_and_then_release`, `test_critical_notifications_may_pass_quiet_hours_only_when_allowed`, `test_hourly_cap_defers_instead_of_flooding`, `test_cooldown_suppresses_duplicate_keys`, `test_outbound_delivery_is_gated_and_never_loses_the_in_app_copy` |
| briefings | `test_briefing_composes_real_sections_and_is_idempotent_per_day`, `test_briefing_respects_its_schedule_and_switch`, `test_tick_generates_the_briefing_once_per_day` |
| surfaces | `test_local_service_exposes_the_proactive_surface`, `test_cli_reminder_and_briefing_commands`, `test_api_routes_for_proactive`, `test_gui_controller_and_web_panel_are_wired` |
| audit/ledgers | `test_diagnostics_and_release_cover_proactive`, `test_ledgers_stay_bounded` |
| determinism | `test_a_full_day_of_policy_is_deterministic` (simulated 24 h: night defer → morning release → 09:00 burst capped at 3 with 2 deferred "hourly limit" → the two deferred fire when the window rolls over → exactly one briefing per local date) |

Full suite: **851 passed, 1 skipped**. `ruff check` clean. Release audit
`READY`, `failures=0`, `warnings=1`, `checks=28`.

Manual end-to-end smoke (sandbox, real files, no mocks): CLI add/list/tick/
snooze/done/delete, invalid due → `proactive error: 'nonsense' is not a due
time…` exit 1; API status/create/snooze/done/notify/briefing/briefings/tick/
clear over a real `LocalApiServer`; `pc-agent doctor` printed
`Proactive: reminders=0 next=none; notifications=2 (last hour 2/20);
quiet=none(open); briefing=off; outbound=off`.

## 6. Skipped / deliberately not done

- **No OS-level toast notifications on Windows** (WinRT/`windows-toasts`). It is
  a real external delivery channel; it belongs behind `allow_proactive_outbound`
  with its own test, not bolted on late.
- No e-mail digest of the briefing — the email connector exists, but sending is
  a Phase 13 gate (`PC_AGENT_ALLOW_EMAIL_CONNECTOR` + confirm token) and is out
  of scope here.
- No NL/cron reminder syntax (`every weekday at 07:30`) — only fixed offsets,
  `HH:MM`, ISO and a repeat interval.
- No cross-device sync of reminders (Phase 16).
- No adaptive/threshold notifications ("tell me if disk > 90 %") — the briefing
  reports the values, but there is no rule engine yet.

## 7. Security implications

- Everything is **off-adjacent**: the engine is on, but *outbound* delivery is
  off by default, so a fresh install cannot emit traffic.
- `ProactiveError` raised with the offending value only — never the full
  settings object, never a token.
- Outbound delivery re-uses the Phase 13 chat/webhook gate, so the existing
  SSRF validation, HMAC signing, timestamp freshness and replay protection all
  apply unchanged. A refusal or exception is recorded as a deferral reason and
  the in-app copy survives.
- Quiet hours hold **everything** except `critical` and only when the user has
  explicitly permitted that crossover.
- Every fire, deferral, suppression, briefing and outbound attempt is written to
  the ledger, and the ledgers are capped (500 notifications, 200 briefings) so
  they cannot grow without bound.
- No new privileged operation, no new file path, no new secret. The `.env`
  sentinel is untouched by install and by the suite.

## 8. Performance implications

- One scheduler tick per existing loop iteration; `due_reminders` uses the
  `reminders(status, due_at)` index and is bounded by `proactive_tick_limit`
  (default 25), so a pathological ledger cannot stall the loop.
- Briefings compose from in-memory/service state; the drive section compares a
  content index it already has, so no extra filesystem walk is introduced.
- Guarded by hourly cap and cooldown, so notification volume — and therefore
  event-log growth — is bounded regardless of caller behaviour.
- Measured on the sandbox (same host used for the 0.75.x numbers): full suite
  173 s for 852 tests; the proactive file alone 2.3 s for 24 tests. Idle CPU/RAM
  deltas are below the noise floor of the existing monitor.

## 9. Limitations

- Times are local and DST-naive in the sense that a `HH:MM` reminder is
  re-anchored on the local clock each day; a DST jump can shift one occurrence.
- The scheduler tick only runs while `pc-agent serve` (or the GUI, which embeds
  the same server) is running. A closed app does not fire reminders; they fire
  on next start and are reported as late.
- Quiet hours are a single daily window, not per-day-of-week.
- Briefing "yesterday" comparison is limited to the drive index snapshot.
- Outbound delivery targets the chat/webhook connector only.

## 10. Next tasks

- **Phase 15** — Plugin/Skill Ecosystem v2: signed manifests, OS-level sandbox,
  capability declarations, install/uninstall/rollback, and the plugin store UI.
- Carried forward: Windows confirmation of `processes=8 (<source>)` in the live
  snapshot, and mic `input=0` after enabling Windows microphone privacy.
- (MEDIUM) flow editor drag-and-drop and conditions; (BARA) connectors-as-tools
  plus Windows polish for proactive outbound; (SAB SE BARA) Phases 16–18.
