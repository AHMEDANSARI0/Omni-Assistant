# Omni-Assistant Local API (Transformation Phase 3 — v1, Mission Center added in Phase 4)

The local API is a loopback-only HTTP front-end over the same service the
CLI and GUI use. It exists for the future JARVIS Command Center, local
automation, and tests that need a network-visible health/status surface.

## Running it

- Status (no listener started): `pc-agent api status`
- Serve in the foreground: `pc-agent api serve` (add `--show-token` once
  to print the bearer token; keep it private).

Settings: `PC_AGENT_API_ENABLED` (default `true`),
`PC_AGENT_API_HOST` (default `127.0.0.1`; must be loopback),
`PC_AGENT_API_PORT` (default `8765`), `PC_AGENT_API_TOKEN` (optional,
≥16 characters). Without an environment token the API generates one into
`data/api-token` (owner-only permissions where the platform supports
them).

## Authentication & envelope

Every route except `GET /v1/health` requires
`Authorization: Bearer TOKEN`. Responses are JSON. (`GET /` serves the
built-in Command Center page — static HTML with no data; it talks to
these same authenticated endpoints.)

- success: `{"ok": true, "data": {...}}`
- failure: `{"ok": false, "error": {"code": "...", "message": "..."}}`

Request bodies must be JSON objects ≤ 256 KiB. There are no CORS headers;
browsers are not API clients in this phase.

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| GET | `/v1/health` | Unauthenticated health: version, schema, emergency stop, running tasks. |
| GET | `/v1/overview` | Task counts by status, events total, pending approvals, tool count, health. |
| GET | `/v1/system` | Live system telemetry (Phase 6): `{snapshot, history}` — CPU/memory/disk percents, network rates, agent process RSS, GPU (via `nvidia-smi` when present), battery, top processes. Unreadable sensors report `null`/`available: false`, never invented values. |
| GET | `/v1/providers` | Configured order, routing strategy, effective route, and per-provider capability flags (model, local, streaming, vision, context tokens, priced). |
| GET | `/v1/providers/capabilities` | Full Provider Platform v2 capability catalog plus the route explanation (Phase 7). |
| GET | `/v1/providers/usage` | Local usage analytics: calls, success rate, token totals, median/p95 latency per model, priced cost sums. No prompt text is stored. |
| GET | `/v1/providers/evaluations` | Latest saved evaluation report (or `null`). |
| POST | `/v1/providers/evaluations/run` | Run the built-in evaluation cases against the configured route and save the report. Makes real provider calls when external providers are configured. |
| POST | `/v1/providers/stream` | Stream one chat completion as NDJSON events (`start`, `delta`, `done`, `error`). Body: `{"prompt": "...", "system": optional, "max_output_tokens": optional}`. Plain model call — no tools or approvals are reachable. |
| GET | `/v1/knowledge/documents` | Ingested knowledge documents (id, title, kind, chunk count, embedding model). |
| POST | `/v1/knowledge/ingest` | Body `{"path": "workspace-relative file"}` (workspace-contained) **or** `{"title": "...", "text": "..."}`. SHA-256 dedupe returns `already_ingested: true`. |
| DELETE | `/v1/knowledge/documents/{id}` | Remove one knowledge document and its chunks. |
| GET | `/v1/knowledge/search` | Query `q`, `limit` (1-25). Hybrid vector+keyword results over documents + embedded notes; `mode` is `keyword-fallback` if embeddings fail. |
| POST | `/v1/knowledge/ask` | Body `{"question": "..."}`. Grounded answer with `[S#]`-cited sources; `answer: null` when nothing matches (no provider call made). |
| GET | `/v1/knowledge/stats` | Document/chunk counts, active embedding model, note counts, supported suffixes. |
| GET | `/v1/memory/search` | Query `q`, `limit`. Memory v2 hybrid search over embedded notes only. |
| GET | `/v1/agents` | Agent Studio definitions (builtins seeded; includes tool allow-lists and enabled state). |
| POST | `/v1/agents` | Create/update one agent (upsert by name). Body: `name`, `role`, `description`, `system_prompt`, `allowed_tools` (list or `*`), `provider`, `max_steps`, `token_budget`, `enabled`. Unknown tools rejected. |
| GET | `/v1/agents/{name}` | One agent definition. |
| DELETE | `/v1/agents/{name}` | Delete a custom agent; builtins are rejected (disable instead). |
| POST | `/v1/agents/{name}/run` | Body `{"instruction": "...", "use_ai": false}` → `202`; runs as a background task under the agent's filtered registry. Poll `/v1/agents/runs`. |
| POST | `/v1/agents/team` | Body `{"goal": "...", "assignments": optional, "use_ai": false}` → `202` with `team_run_id`. Without assignments the coordinator proposes them (provider call). |
| GET | `/v1/agents/team/{id}` | Team progress: running flag, final result when done, and member runs. |
| GET | `/v1/agents/runs` | Query `agent`, `team_run_id`, `limit`. Agent run ledger (status, task id, summary). |
| GET | `/v1/agents/stats` | Agent counts (total/enabled) and run total. |
| GET | `/v1/automation/capabilities` | Browser (enabled/connected/version), desktop (enabled/backend/available), vision (local analysis always on, provider vision gated), the automation tool ids, and the ledger summary. |
| GET | `/v1/automation/runs` | Query `kind`, `status`, `limit` (<=500). Recorded automation actions, newest first, including refused ones (`allowed=false`). |
| GET | `/v1/automation/stats` | Ledger summary `{total, by_kind, refused, last_at}`. |
| POST | `/v1/vision/screen` | Body `{"path": "shot.png"\|null, "grid": 1-12}`. Local-only measurement: size, brightness, contrast, dominant colours, ink grid, `busy_cells`, `text_likely`, `region_hint`. Null path = live screen capture where the platform supports it. |
| POST | `/v1/vision/locate` | Body `{"screen_path": ..., "template_path": ..., "threshold": 0.9}`. Verified template match with `found`, verified `score`, `matches[]` (x/y/width/height/score), `search_steps`. A flat template is refused. |
| POST | `/v1/browser/wait` | Body `selector\|text\|url_contains` + `timeout_seconds` 1-120. Polls a page condition; a timeout is an error carrying the observed URL. |
| POST | `/v1/browser/tabs` | Body `{"action": "list"\|"activate"\|"close", "target_id": ...}`. Closing the last page tab is refused. |
| POST | `/v1/desktop/window-state` | Body `{"state": "minimize\|maximize\|restore\|move\|resize", "bounds": [l,t,r,b]?, "window_title_contains\|window_handle\|...": ...}`. Bounds within +/-32000, minimum 120x120, clamped to the virtual screen. |
| GET | `/v1/voice/status` | Live voice state: microphone indicator/state, retention and consent flags, selected STT/TTS engines with reasons, device counts, session summary, emergency-stop flag. |
| GET | `/v1/voice/devices` | Input and output devices with the probe method used (`winmm`/ALSA) and a reason when empty. |
| GET | `/v1/voice/privacy` | Microphone privacy view: consent, retention, transcript storage, cloud-STT allowance, capture bound. |
| POST | `/v1/voice/enable` | Body `{"enabled": true\|false}`. Persists microphone consent (off by default) and audits the change. |
| POST | `/v1/voice/wake-word` | Body `{"enabled": bool}`. Second, separate opt-in; refused while the microphone is off. |
| POST | `/v1/voice/capture/start` | Body `{"mode": "push_to_talk"\|"wake_word"}` → `202`. Refused when the mic is off or a stop is engaged. |
| POST | `/v1/voice/capture/stop` | Body `{"cloud_consent": bool}`. Stops, transcribes, records a session row, and discards audio unless retention is on. |
| POST | `/v1/voice/capture/cancel` | Aborts the current capture without transcribing. |
| POST | `/v1/voice/speak` | Body `{"text": "...", "output_path": optional}`. Offline TTS; `output_path` writes a WAV instead of playing. |
| POST | `/v1/voice/transcribe` | Body `{"path": "...", "cloud_consent": bool}`. Transcribe an existing WAV inside the workspace. |
| GET | `/v1/voice/sessions` | Query `limit`. Voice session ledger (mode, duration, engines, chars, retention, status). |
| POST | `/v1/voice/purge` | Deletes every retained recording and clears its reference. |
| POST | `/v1/voice/clear` | Purges audio first, then deletes the whole voice ledger. |
| GET | `/v1/settings` | Public non-secret settings summary (same as the CLI config view). |
| GET | `/v1/tools` | Runtime tool catalog (built-ins + approved dynamic tools), permissions and timeouts. |
| GET | `/v1/providers` | Provider order and whether each has a credential (never the credential). |
| GET | `/v1/safety` | Safety Kernel settings, emergency stop state, pending approvals. |
| POST | `/v1/safety/stop` | Body `{"reason": "..."}`. Engages the shared emergency stop file. |
| POST | `/v1/safety/resume` | Clears the file-backed emergency stop (environment override still applies). |
| GET | `/v1/events` | Query `since_id`, `type`, `task_id`, `limit`. Cursor-friendly (oldest first); response includes `cursor`. |
| GET | `/v1/tasks` | Query `limit`, `offset`, `status`. |
| POST | `/v1/tasks` | Body `{"instruction": "..."}` → `201` with the queued task. |
| GET | `/v1/tasks/{id-or-prefix-or-latest}` | Task + task events + latest run report summary. |
| POST | `/v1/tasks/{id}/run` | Body `{"use_ai": false}` → `202`; poll the task URL or `/v1/events`. |
| POST | `/v1/tasks/{id}/cancel` | Body `{"reason": "..."}`; stops at the next stage boundary. |
| DELETE | `/v1/tasks/{id}` | Terminal tasks only; removes the record, never workspace files. |
| POST | `/v1/run` | Convenience: create + run in one call → `202`. |
| GET | `/v1/approvals` | Query `status` (default `pending`), `limit`. |
| POST | `/v1/approvals/{request_key}/decide` | Body `{"approve": true, "note": "..."}`. Decisions stay exact-scope and consume-once as in the CLI. |
| GET | `/v1/profiles` | Names, active profile, and `restart_required_to_apply`. |
| POST | `/v1/profiles/save-current` | Body `{"name": "work"}`; snapshots whitelisted settings only. |
| POST | `/v1/profiles/activate` | Body `{"name": "work"}` (or `null` to clear). Applies to future processes. |
| DELETE | `/v1/profiles/{name}` | Deletes the profile file (and clears the marker if active). |
| GET | `/v1/missions` | Missions with per-status task progress. |
| POST | `/v1/missions` | Body `{"name": "...", "tasks": [{"key": "a", "instruction": "...", "depends_on": ["a"]}]}` → `201`. |
| GET | `/v1/missions/{id}` | Mission + tasks (with `depends_on`) + execution graph. |
| POST | `/v1/missions/{id}/run` | Body `{"use_ai": false}` → `202`; poll the mission URL or `/v1/events`. |
| POST | `/v1/missions/{id}/cancel` | Body `{"reason": "..."}`; member tasks stop at stage boundaries. |
| POST | `/v1/missions/{id}/tasks` | Body `{"instruction": "..."}` or `{"task_id": "..."}`, plus optional `depends_on` task ids. |
| GET | `/v1/missions/{id}/graph` | Nodes, edges, topological levels, and `max_parallel`. |
| DELETE | `/v1/missions/{id}` | Deletes the mission record only; tasks are kept. |
| GET | `/v1/schedules` | Schedules plus `scheduler_interval_seconds`. |
| POST | `/v1/schedules` | Body `{"name": "...", "instruction": "...", "kind": "once", "run_at": "ISO-8601"}` or `{"kind": "interval", "interval_seconds": 3600}`. |
| POST | `/v1/schedules/run-due` | Fires every enabled schedule whose time has come. |
| POST | `/v1/schedules/{id}/enabled` | Body `{"enabled": true}`. |
| DELETE | `/v1/schedules/{id}` | Deletes the schedule. |

## Plugin routes (Transformation Phase 15, 0.77.0)

Same envelope and bearer-token rule as every other `/v1` route. A refused bundle
or a refused run answers `plugin_error` with the exact reason ("signed by key
'x', which is not in the trusted key set", "plugin is installed; enable it
before running tools"), never a generic failure. **No route installs from a URL**
- `bundle` is always a local path the caller chose.

| Method | Path | Body / query | Real effect |
| --- | --- | --- | --- |
| GET | `/v1/plugins/status` | - | Gate, signature policy, trusted key ids, sandbox mechanism and limits, counts, installed plugins |
| GET | `/v1/plugins` | - | Installed plugins with capabilities, tools, skills and signature provenance |
| POST | `/v1/plugins/verify` | `{"bundle": "C:/path/p.zip"}` | Validates manifest, content checksums and signature **without installing** |
| POST | `/v1/plugins/install` | `{"bundle": "C:/path/p.zip"}` | Verifies then extracts into `data/plugins/installed/<id>/<version>` and records the install |
| GET | `/v1/plugins/runs` | `?plugin=<id>&limit=25` | Sandbox run ledger (status, duration, mechanism, reason) |
| GET | `/v1/plugins/{id}` | - | Full detail including version history kept for rollback |
| GET | `/v1/plugins/{id}/skills` | - | The plugin's Markdown skills as bounded text (never executed) |
| POST | `/v1/plugins/{id}/run` | `{"tool": "count_words", "values": {"text": "a b"}, "confirm": "<id>"}` | Runs one declared tool in the OS sandbox; `confirm` must equal the plugin id |
| POST | `/v1/plugins/{id}/enabled` | `{"enabled": true}` | Enables or disables the plugin (both are recorded events) |
| POST | `/v1/plugins/{id}/rollback` | `{"version": "1.0.0"}` optional | Switches the active version to an earlier installed one |
| DELETE | `/v1/plugins/{id}` | - | Uninstalls the plugin and deletes its extracted files |

## Proactive routes (Transformation Phase 14, 0.76.0)

Same envelope (`{"ok": true, "data": …}`) and bearer-token rule as every other
`/v1` route. A refused policy action answers `proactive_error` with the exact
reason ("already 200 open reminders", "today's briefing was already delivered"),
never a generic failure.

| Method | Path | Body / query | Real effect |
| --- | --- | --- | --- |
| GET | `/v1/proactive/status` | - | Engine state: open reminders, next due, notifications in the last hour against the cap, quiet window, briefing schedule, outbound switch |
| POST | `/v1/proactive/tick` | `{"limit": 25}` optional | Runs one tick now: fires due reminders, applies the policy, emits the briefing when due |
| GET | `/v1/proactive/reminders` | `?status=open&limit=50` | Stored reminders with computed next due |
| POST | `/v1/proactive/reminders` | `{"title": "...", "due": "+15m", "body": "...", "severity": "normal", "repeat_seconds": 3600}` | Creates a durable reminder; `due` accepts offsets, `HH:MM` and ISO-8601 |
| POST | `/v1/proactive/reminders/{id}/done` | - | Marks the reminder done (repeats reschedule) |
| POST | `/v1/proactive/reminders/{id}/dismiss` | - | Marks it dismissed without completing it |
| POST | `/v1/proactive/reminders/{id}/snooze` | `{"for": "+15m"}` | Moves the due time forward by the given offset |
| DELETE | `/v1/proactive/reminders/{id}` | - | Deletes the reminder row |
| GET | `/v1/proactive/notifications` | `?kind=briefing&limit=25` | The notification ledger (quiet hours, caps and cooldowns already applied) |
| POST | `/v1/proactive/notify` | `{"title": "...", "body": "...", "severity": "high"}` | Goes through the full delivery policy; outbound stays off unless enabled |
| POST | `/v1/proactive/briefing` | `{"force": false}` | Builds/stores today's briefing (force ignores the schedule switch, not the once-per-day rule) |
| GET | `/v1/proactive/briefings` | `?limit=10` | Stored briefings, newest first |
| POST | `/v1/proactive/notifications/clear` | - | Clears the notification ledger and reports how many rows were removed |

## Connector routes (Transformation Phase 13, 0.75.1)

All routes answer in the standard envelope (`{"ok": true, "data": …}`) and honour
the same bearer-token rule as every other `/v1` route. A closed connector gate
returns a `connector_error` with the exact setting that is missing.

| Method | Path | Body / query | Real effect |
| --- | --- | --- | --- |
| GET | `/v1/connectors/status` | `probe=1` (optional) | Gate state, per-connector configuration, ledger counts; `probe` reads the local calendar/drive targets |
| GET | `/v1/connectors/events` | `connector`, `limit` | Inbound ledger, newest first |
| GET | `/v1/connectors/deliveries` | `connector`, `limit` | Outbound attempts, including failures |
| GET | `/v1/connectors/calendar` | `days`, `limit` | Upcoming events from the `.ics` file |
| POST | `/v1/connectors/calendar` | `summary`, `start`, `duration_minutes`, `location`, `description` | Appends one event (atomic write) and records `calendar/event_added` |
| POST | `/v1/connectors/drive/sync` | – | Re-hashes the folder, stores the manifest, records added/modified/removed |
| GET | `/v1/connectors/mail/inbox` | `limit` | Newest message **headers** only |
| POST | `/v1/connectors/mail/send` | `to`, `subject`, `body`, `confirm` | Sends one message; `confirm` must be `SEND`; records the delivery |
| POST | `/v1/connectors/chat/send` | `text`, `target` | Signed POST to the configured chat endpoint; records the delivery |
| POST | `/v1/connectors/webhook` | `body`, `timestamp`, `signature` | Verifies an HMAC-signed event, refuses replay/stale/forged, records it |

Example: a chat notification with a signed body.

```bash
curl -X POST http://127.0.0.1:8765/v1/connectors/chat/send \
  -H "Authorization: Bearer $PC_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "Nightly backup finished", "target": "ops"}'
```

Example: verifying an inbound webhook (the receiver computes
`sha256=HMAC(secret, "<timestamp>.<raw body>")`).

```bash
printf '%s' '{"kind":"build","id":"42","summary":"CI green"}' > body.json
python -m pc_agent connectors webhook receive --body-file body.json \
  --timestamp 1770000000 --signature sha256=<hex>
```

## Workflow Builder routes (Transformation Phase 12, 0.74.0)

Loopback only, bearer token, enveloped replies `{"ok": true, "data": …}`. The
server implements `GET`, `POST` and `DELETE` (there is no `PUT`/`PATCH`).

| Method | Path | Body / query | Notes |
| --- | --- | --- | --- |
| GET | `/v1/flows` | `?limit=` | list + summary |
| POST | `/v1/flows` | flow document | `201`, validated create (v1) |
| POST | `/v1/flows/validate` | flow document | dry validation: `valid`, `levels`, `nodes` |
| POST | `/v1/flows/import` | `{"document": …, "overwrite": bool}` | `201`, checksum verified |
| POST | `/v1/flows/tick` | `{"limit": n}` | bounded due-trigger sweep |
| GET | `/v1/flows/stats` | — | flow/trigger/run counters |
| GET | `/v1/flow-runs` | `?flow_id=&limit=` | run ledger |
| GET | `/v1/flows/{ref}` | — | one flow + versions + triggers + runs |
| GET | `/v1/flows/{ref}/export` | — | portable document with checksum |
| GET | `/v1/flows/{ref}/runs` | — | runs of one flow |
| GET | `/v1/flows/{ref}/triggers` | — | triggers of one flow |
| POST | `/v1/flows/{ref}/run` | `{}` | `202`, mission started |
| POST | `/v1/flows/{ref}/triggers` | `{"kind": "schedule", "interval_seconds": 600}` or `{"kind": "event", "event_type": "task.completed"}` | `201` |
| POST | `/v1/flows/{ref}/enabled` | `{"enabled": bool}` | enable/disable |
| POST | `/v1/flows/{ref}/definition` | flow document | update (version bump) |
| POST | `/v1/flow-triggers/{id}/enabled` | `{"enabled": bool}` | toggle one trigger |
| DELETE | `/v1/flows/{ref}` | — | delete flow + triggers |
| DELETE | `/v1/flow-triggers/{id}` | — | delete one trigger |

`{ref}` accepts a full id, a unique id prefix or an exact flow name. Unknown flow
-> `404`. A refused document returns the exact field, for example
`flow.nodes[1].id must be lowercase letters, digits, - or _ (max 32).`

```bash
curl -s -X POST http://127.0.0.1:8765/v1/flows/validate \
  -H "Authorization: Bearer $PC_AGENT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"flow_schema":1,"name":"Morning report","nodes":[{"id":"gather","instruction":"Summarise today"}],"edges":[]}'
```


## Safety semantics

- Task runs through the API pass through the orchestrator, the Safety
  Kernel (hard timeouts, worker isolation, emergency stop), budgets,
  verification, and the Approval Center. The API cannot approve a tool
  call inline: sensitive tools file an Approval Center request that a
  human must decide (CLI, GUI, or the authenticated approvals endpoint).
- Profile values never raise capability: capability gates and secrets
  are not in the profile whitelist, and explicit environment values
  always override profile values.

## Versioning

The API is versioned by the `/v1` prefix. Additive fields may appear;
removing or renaming fields requires a new version prefix.

## Device sync routes (Transformation Phase 16, 0.78.0)

Same envelope (`{"data": ..., "error": ...}`) and bearer-token rule as every other
`/v1` route. All twelve routes refuse with `sync_error` and the exact reason when
`PC_AGENT_ALLOW_SYNC` is off, when a peer is unknown/paused, or when a bundle fails
verification - the reason is never a generic failure and never contains key
material. `POST /v1/sync/pair` requires `confirm: "PAIR"`. No route accepts a URL:
`path` is always a local bundle file the caller chose.

| Method | Path | Body / query | Real effect |
|---|---|---|---|
| GET | `/v1/sync/status` | - | Device id/fingerprint/public keys, transport, sync-folder state, conflict policy, limits, counts, peers |
| GET | `/v1/sync/identity` | - | This device's public document only (never a seed) |
| GET | `/v1/sync/peers` | - | Paired devices with fingerprint, status, last seen, bundle count |
| POST | `/v1/sync/pair` | `{"name","sign_public","encrypt_public","confirm":"PAIR"}` | Records trust for a peer; 400 on bad keys, missing `PAIR`, or self-pairing |
| POST | `/v1/sync/peers/enabled` | `{"device","enabled"}` | Pause/resume a peer (`paused` peers' bundles are refused) |
| DELETE | `/v1/sync/peers/{id}` | - | Unpairs the device (future bundles refused; nothing already received is recalled) |
| POST | `/v1/sync/export` | `{"path"?: "..."}` | Writes an encrypted, signed `.pcasync` bundle for every paired peer |
| POST | `/v1/sync/import` | `{"path": "..."}` | Verifies, decrypts and applies a bundle; refusals are explicit |
| POST | `/v1/sync/tick` | `{}` | Exports one bundle and imports everything new in the folder |
| GET | `/v1/sync/bundles` | `?direction=in|out&limit=25` | Bundle ledger (both directions) |
| GET | `/v1/sync/requests` | `?status=pending&limit=25` | Peer coordination requests |
| POST | `/v1/sync/request` | `{"kind":"remind|notify|ping","title","body"?,"due"?,"severity"?}` | Exports a bundle carrying one coordination request |
| POST | `/v1/sync/requests/{id}/decide` | `{"accept": true|false, "note"?}` | Human decision; accepting runs the reminder/notification locally |

## Interface and accessibility routes (Transformation Phase 17, 0.80.1)

| Method | Path | Body / query | Real effect |
|---|---|---|---|
| GET | `/v1/i18n/status` | - | Active locale, name, direction, computed coverage and the full locale table |
| GET | `/v1/i18n/messages` | `?locale=ur` | Every translated interface key for one locale (falls back to the active locale) |
| POST | `/v1/i18n/locale` | `{"locale"?, "scale"?, "high_contrast"?, "reduce_motion"?, "cli_color"?}` | Validates and persists the choice to `data/ui.json`; 400 on an unsupported value |
| GET | `/v1/a11y/status` | - | Measured WCAG 2.1 contrast table, shortcuts, claims and disclosed limitations |
| GET | `/v1/perf/bench` | `?iterations=5` | Local wall-clock timings for health, events, a11y and system status |

These routes only change presentation. `locale` accepts `en`, `es`, `ur`, `ar`
(region tags such as `ur-PK` are normalised); `scale` must be 0.8-2.0; an
environment variable still overrides the stored preference.

## Phase 18 - release hardening surface

There is deliberately **no HTTP route** for the release chain: it is an
operator action with filesystem side effects (temp bundles, staged updates) and
belongs to the CLI and the dashboard on the machine, not to a loopback API that
any local process can call with a bearer token. The state it produces is
available to read:

| Surface | Command | Returns |
| --- | --- | --- |
| CLI | `pc-agent release audit [--strict] [--json]` | 32 checks, failures/warnings |
| CLI | `pc-agent release production-check [--out PATH] [--keep] [--json]` | ten steps with status and detail |
| CLI | `pc-agent doctor` | `Hardening: ci=... last-release-chain=... unsigned-updates-allowed=... feed=...` |
| Diagnostics | `pc-agent diagnostics export --out diagnostics.json` | the same hardening state, secret-free |
| Dashboard | tab **Production** | the stored report and a button that runs the real chain |

Report shape (`data/production-check.json`):

```json
{
  "version": "0.80.1",
  "generated_at": "2026-11-06T08:28:15+00:00",
  "ok": true,
  "failures": 0,
  "duration_ms": 1732,
  "environment": {"platform": "...", "python": "3.13.x", "schema": 18,
                  "signing_key": "ephemeral self-test key (no production key is used)"},
  "steps": [{"name": "release_catalog", "status": "pass", "detail": "...", "duration_ms": 14}]
}
```

## Voice completion endpoints (0.81.0)

### `POST /v1/voice/intent`

Body: `{"text": "jarvis remind me to call Ali at 18:00", "execute": false, "wake_word": "jarvis"}`

Returns the deterministic parse:

```json
{"intent": {"intent": "reminder", "text": "call Ali", "due": "18:00",
            "addressed": true, "heard": "jarvis remind me to call Ali at 18:00"}}
```

With `"execute": true` the intent is performed and an `outcome` object is added
(`executed`, `ok`, `reply`, `task_id`, `needs_approval`, `stop_session`). Free-text
intents create a normal task, so approvals still apply - the voice route grants no
extra authority.

### `GET /v1/voice/acceptance`

Runs the seven voice gates against this install and returns the sealed report
(`status` is `passed`, `incomplete` or `failed`, plus `body_sha256`). The same
report is available offline from the CLI as `pc-agent voice acceptance`, and can be
re-verified with `pc-agent voice acceptance-verify <path>`.


## Command Center UI surface (0.82.0)

These routes serve the interface itself and its live feed. All of them are
same-origin and loopback-only; the shell routes need no bearer token, the stream
route does.

### `GET /`

Returns the built SPA shell (`Content-Type: text/html`), with the API token
injected as `<meta name="pc-agent-token" content="...">` and this CSP:

```
default-src 'self'; script-src 'self' 'sha256-<hash of the inline bootstrap>';
style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:;
worker-src 'self' blob:; connect-src 'self'; media-src 'self' blob:;
frame-ancestors 'none'; base-uri 'none'; form-action 'none'
```

If the bundle is absent, the response is a 200 page that says the UI is not
built and prints `cd frontend; npm ci; npm run build`. It never serves a stub
dashboard.

### `GET /assets/<path>`

Static bundle files. Hashed names get `Cache-Control: public, max-age=31536000,
immutable`; `X-Content-Type-Options: nosniff` is always set. Path traversal,
symlinks that escape the package and missing files are `404`.

### `GET /favicon.ico`

The inlined favicon the shell references (`204` when no file is present).

### `GET /legacy`

The pre-0.82 Command Center panel, unchanged, including its own CSP. Rollback
path for anyone who needs the old surface.

### `GET /v1/events/stream`

Live application events as NDJSON (`application/x-ndjson`).

| Query | Meaning |
|---|---|
| `since_id` | integer cursor; events with a greater id are streamed (default `0`) |
| `duration` | seconds, clamped to **5-900** (default `120`) |

Frames, one JSON object per line:

```
{"type":"stream.ready","since_id":0}
{"id":42,"event_type":"task.created","source":"api","task_id":"...","summary":"...","payload":{},"created_at":"..."}
{"type":"stream.heartbeat"}
```

Guarantees: the first line is always `stream.ready`; a heartbeat is written after
10 s of silence so proxies and browsers keep the socket; the server closes the
response itself when `duration` elapses; `Connection: close` and `no-store` are
sent. A non-numeric `since_id` or `duration` is a **400** (`Expected an integer`
/ `Expected a number`) rather than a silent default, because on this route a
default means an unexpectedly long-lived socket.

Errors use the standard envelope: `{"ok": false, "error": {"code", "message"}}`.
