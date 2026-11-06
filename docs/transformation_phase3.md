# Transformation Phase 3 — Local Service, events, structured logging, and profiles

## 1. Existing findings

- Audit Finding 3J/Phase 3 scope: no local service boundary (CLI and GUI each
  built their own orchestrator stack), no HTTP/IPC API, no global event
  system (only per-task status events), plain-text logging only, and no
  settings profiles.
- Physical evidence: `TaskOrchestrator.execute(task_id, approval_callback,
  use_ai, cancellation_requested)` already wraps the Safety Kernel, budgets,
  Approval Center, verification, recovery, and memory, so a service layer
  could be added without changing execution semantics.

## 2. What changed

### LocalService (`src/pc_agent/service.py`)
- One transport-independent service that owns a database and one
  orchestrator built exactly like the CLI's (default registry, planner,
  model router, recovery, memory; the Safety Kernel is constructed inside
  the orchestrator).
- Task runs start on a background thread and return immediately (202-style);
  `wait_for_task` supports tests and future local clients. Approval
  callbacks inside the service deny in-band consent by design: sensitive
  work files an Approval Center request and must be decided through the
  approvals surface, never approved inline by an API caller.
- Service methods cover health/overview, task CRUD + run + cancel (through
  `TaskOrchestrator.request_cancellation`), Approval Center list/decide,
  safety status/stop/resume (same file-backed `EmergencyStop`), events,
  public settings, tool catalog (runtime registry incl. approved dynamic
  tools), and provider order/status. License enforcement mirrors the CLI
  gate (off by default).

### Global event system (schema v8)
- New `app_events` table with redacted summaries and secret-redacted JSON
  payloads, trimmed to the newest 10,000 rows on every insert.
- Producers: `task.created`, `task.run_started`, `task.run_finished`,
  `task.run_crashed`, `task.cancel_requested`, `task.deleted`,
  `approval.requested`, `approval.decided`,
  `safety.emergency_stop_engaged`, `safety.emergency_stop_released`,
  `maintenance.backup_created`, `maintenance.backup_restored`,
  `service.api_started`.
- Consumers poll with a `since_id` cursor (`pc-agent events list` or
  `GET /v1/events`); `--recent` returns newest-first for humans.
- Event-recording failures inside the service are logged, never allowed to
  crash a task run; repository-level approval events record synchronously.

### Local HTTP API (`src/pc_agent/api_server.py`, `pc-agent api`)
- Standard-library `ThreadingHTTPServer`; loopback-only host validated in
  configuration (`PC_AGENT_API_HOST` must be `127.0.0.0/8`, `::1`, or
  `localhost`), default `127.0.0.1:8765`.
- Bearer token on every route except `GET /v1/health`. Token comes from
  `PC_AGENT_API_TOKEN` (≥16 chars) or a generated `data/api-token` file
  (owner-only permissions where supported). Constant-time comparison,
  256 KiB body cap, structured `{ok, data|error}` envelope, no CORS.
- Endpoints are documented in `API_DOCUMENTATION.md`. The API never
  executes tools directly: runs still go through the orchestrator, the
  Safety Kernel, and the Approval Center; emergency-stop state is shared
  with the CLI and GUI via the same data-dir file.

### Structured logging
- `PC_AGENT_LOG_FORMAT=json` switches the CLI (and API process) to
  one-JSON-object-per-line logs on the stream handler and a rotating file
  handler at `data/logs/pc-agent.jsonl` (5 MiB × 3). Exceptions and
  messages pass through `redact_text`; `extra={"ctx_*": ...}` fields are
  included as structured context. Default remains human-readable text.

### Settings profiles (`src/pc_agent/profiles.py`, `pc-agent profiles`)
- JSON files under `data/profiles/<name>.json` with a strict whitelist:
  logging, provider order, `max_recovery_attempts`, task budgets, safety
  seconds/expiry, command allowlist, and web limits. Secrets, capability
  gates, and paths can never travel in a profile.
- Precedence: explicit environment/`.env` values > active profile >
  built-in defaults. Selection via `PC_AGENT_PROFILE` or the
  `data/active-profile` marker; invalid profiles fail closed with
  `ConfigError`. Applying a profile needs a process restart (settings are
  immutable per process); profile CRUD is available via CLI and API.

### Observability wiring
- `pc-agent doctor` prints a `Local service:` line (API host/port, log
  format, active profile, event count).
- Diagnostics export gains a `service` section. The release audit gains a
  `local_service` check (fails if the API host is not loopback) and
  requires `API_DOCUMENTATION.md`. The GUI doctor summary gains a
  `service_summary` line.

## 3. What was intentionally reused

- `TaskOrchestrator.execute/request_cancellation`, repositories, Safety
  Kernel approval flow, `public_summary()` redaction, and existing config
  parsers (profile values are validated by the same parsers as environment
  values).

## 4. Deferred

- Mission Center/task dependencies/parallel execution (Phase 4) — the
  service deliberately runs one task thread per submission without a queue
  or dependency graph yet.
- Streaming providers, RAG, voice, and the JARVIS UI remain their later
  phases; the API is the transport those phases will build on.
- `HEALTH` endpoint is intentionally unauthenticated but returns only
  non-secret state (version, schema, emergency stop flag, running ids).
