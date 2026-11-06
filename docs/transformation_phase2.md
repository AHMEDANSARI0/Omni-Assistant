# Transformation Phase 2 - Safety Kernel

Version 0.64.0. This phase hardens the autonomous execution path. It adds
no registered tool (62 built-ins) and one additive database migration
(schema v7, `approval_center_requests`).

## What existed / what was missing

- Tool timeouts were **post-return only**: the registry measured elapsed
  time after a handler returned and discarded late results, so a hung
  tool could block its caller indefinitely (Phase 0 audit: BROKEN).
- Approvals existed interactively, but a non-interactive run simply
  failed with "no approval prompt"; there was no **Approval Center**, no
  pending queue, no out-of-band decision, and approval records stored
  raw tool arguments.
- Cancellation existed per task, but there was no global **emergency
  stop**.
- Database backups were verified but **plaintext**.
- Dynamic `custom.*` tools already ran in a subprocess sandbox with a
  timeout; that boundary is unchanged.

## What changed

### Safety Kernel (`src/pc_agent/safety.py`)

All 19 orchestrator tool-invocation sites (execution plus verification
re-checks) now route through `SafetyKernel.invoke`:

- **Hard timeout** = min(tool's declared `timeout_seconds`,
  `PC_AGENT_SAFETY_MAX_TOOL_SECONDS`). The caller is never blocked past
  the deadline.
- **Isolated worker** (`src/pc_agent/safety_worker.py`): tools whose
  context is pure data (files, PDF, Office, local images, Git,
  web.fetch, workspace, commands and approved dynamic tools) execute in
  a persistent subprocess speaking JSON lines. The worker rebuilds the
  runtime registry from the same database and reconstructs the tool
  context from a primitive snapshot, so policy is identical. On timeout
  the process is killed; on crash the tool is *not* re-run in-process.
- **In-process thread path** for factory-bound tools (`browser.*`,
  `desktop.*`, `image.analyze`, `pdf.ocr`) that need a live CDP client,
  UIA backend or the budget-metered provider router. The handler runs
  in a daemon thread; at the deadline the result is abandoned and
  discarded. These tools also carry their own client timeouts.
- A registry that overrides `invoke()` (test doubles, embedding hooks)
  keeps authority over its own execution and uses the thread path; a
  worker "tool not found" for an ad-hoc parent tool falls back
  in-process. If the worker cannot start at all, execution falls back
  to the thread path and the degradation is counted in kernel stats.
- Direct operator-invoked CLI/GUI commands (for example
  `pc-agent pdf inspect`) remain explicit synchronous user actions;
  the kernel guards autonomy, not the operator's own keystrokes.

### Emergency stop

`data/emergency_stop.json` (or `PC_AGENT_EMERGENCY_STOP=1`) blocks all
orchestrated execution: `execute`, `resume`, workflow reuse, every step
loop and the kernel itself check it. Tasks stop as `cancelled` with an
`emergency_stop` event. Control it with `pc-agent safety stop|resume|
status`; clearing requires explicit confirmation.

### Approval Center (schema v7)

- When no interactive prompt is available, the orchestrator records a
  pending request (`apr-...`) bound to the **SHA-256 digest of the exact
  arguments** plus read roots, with a redacted summary and an expiry
  (`PC_AGENT_SAFETY_APPROVAL_EXPIRY_MINUTES`, default 1440).
- `pc-agent approvals list|show|approve|deny` decides requests
  out-of-band. On resume, an unconsumed, unexpired decision whose digest
  matches the plan exactly is consumed once and written to the normal
  approval audit trail; otherwise execution stays blocked.
- Approval records no longer store raw arguments - only
  `arguments_sha256` (legacy raw-argument records still verify).
- Interactive approvals are unchanged.

### Data protection

- `pc-agent maintenance backup --encrypt` writes a `.db.enc` backup:
  PBKDF2-HMAC-SHA256 (480k iterations, random salt) + Fernet. The
  manifest keeps only salt/iterations and ciphertext/plaintext hashes;
  the passphrase is prompted hidden (or read from
  `PC_AGENT_BACKUP_PASSPHRASE` for automation) and never stored.
  `verify`/`restore` detect encrypted backups, require the passphrase,
  and check plaintext hash/size plus SQLite integrity before use.
  Pre-restore safety backups remain unencrypted by design and are
  created only with the same explicit restore confirmation as before.

### Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `PC_AGENT_SAFETY_HARD_TIMEOUTS` | `1` | Enforce hard tool deadlines |
| `PC_AGENT_SAFETY_WORKER_ISOLATION` | `1` | Use the isolated worker process |
| `PC_AGENT_SAFETY_MAX_TOOL_SECONDS` | `600` | User ceiling (1-600) on any tool timeout |
| `PC_AGENT_SAFETY_APPROVAL_EXPIRY_MINUTES` | `1440` | Approval Center request/decision lifetime (1-10080) |
| `PC_AGENT_EMERGENCY_STOP` | unset | Force-engage the emergency stop |
| `PC_AGENT_BACKUP_PASSPHRASE` | unset | Automation-only backup passphrase source |

`doctor`, GUI doctor text, diagnostics (`safety` section) and
`release audit` (`safety_kernel` check, 17 checks total) display the
live safety state.

## Security implications

- No gate was weakened: subprocess/browser/desktop/vision opt-ins and
  per-action approvals behave as before; the kernel only adds deadlines,
  isolation, a global stop and digest-bound out-of-band approvals.
- A compromised or buggy built-in can no longer hang the agent or crash
  the CLI/GUI process when it is worker-eligible. The worker is a
  separate OS process, **not** an OS sandbox: it runs with the user's
  privileges. True sandboxing of plugins remains Transformation
  Phase 15.
- In-process abandoned threads cannot be force-killed in Python; their
  results are discarded and never checkpointed, but a thread may finish
  a side effect in the background. This is the documented residual risk
  for `browser.*`/`desktop.*`/vision tools until they gain process
  boundaries of their own.
- Approval Center decisions are single-use and digest-bound, so an
  approval cannot be replayed for altered arguments.

## Performance implications

The worker adds one interpreter start per top-level task run and JSON
round-trips per eligible tool call; no benchmark claim is made here -
see `PERFORMANCE_REPORT.md` and `TEST_REPORT.md` for measured Linux
sandbox numbers. Native Windows overhead remains to be measured during
Windows acceptance.

## Limitations / unverified

- Native Windows behaviour (worker spawn under the real venv, GUI,
  UIA in the thread path, Task Scheduler) is **UNVERIFIED** until the
  Windows acceptance run - see `docs/windows_acceptance.md`.
- The worker requires a runnable `sys.executable -m
  pc_agent.safety_worker`; frozen/packaged builds must bundle it or they
  will run in the degraded thread mode (counted in kernel stats).
- Encrypted backup restore still writes one unencrypted pre-restore
  safety backup of the then-live database.

## Tests

New regression coverage lives in
`tests/test_transformation_phase2_safety.py` (hard thread timeout,
isolated-worker execution and worker kill on hang, emergency stop,
Approval Center decide-and-resume, digest scope records, encrypted
backup round-trip). Full Linux counts are recorded in `TEST_REPORT.md`.

## Next

Transformation Phase 3: Local Service/API, event system, structured
logging, settings and profiles - the boundary the future JARVIS Command
Center and other clients will consume instead of reaching into
internals.
