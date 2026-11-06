# Phase 56 — Background Auto-Upgrade

Phase 56 adds the separately requested background layer on top of the
existing signed update stack:

- Phase 21 verifies and applies signed portable bundles with backups;
- Phase 24 downloads one bundle from a strict online feed;
- Phase 40 publishes a signed static feed;
- Phase 55 provides the Windows HTTPS hosting contract and live checker;
- Phase 56 periodically checks that signed HTTPS feed in the background.

This is **Windows-first**. The implemented scheduler is a per-user
**Windows Scheduled Task**. **Linux systemd user timer** and
**macOS LaunchAgent** profiles are present in the registry as **planned**
and fail closed; they are not claimed as implemented in this phase.

## Safety defaults

Background auto-upgrade is off by default. Installing the Windows task by
itself does not authorize network access or application: the worker still
reads `.env` on every wake-up and refuses to work unless all of these are
true:

1. `PC_AGENT_ONLINE_UPDATES=1`;
2. `PC_AGENT_AUTO_UPGRADE=1`;
3. the update feed is an **HTTPS** URL;
4. `PC_AGENT_REQUIRE_SIGNED_UPDATES=1`;
5. both `PC_AGENT_RELEASE_PUBLIC_KEY` and `PC_AGENT_RELEASE_KEY_ID` are set;
6. the advertised version is strictly newer than the installed version.

The worker never accepts HTTP, an unsigned bundle, a signature/key mismatch,
a bundle hash mismatch, a same-version bundle or a downgrade. It reuses the
Phase 24 strict feed parser and downloader and the Phase 21 manifest,
allow-list, backup, hash-verification and rollback-receipt paths. A single
instance lock prevents concurrent workers. The planner receives no new tool
and untrusted feed data cannot loosen local policy.

## Modes

### `stage-only` — recommended first

Stage-only mode checks the signed feed at the configured interval,
downloads and verifies a newer bundle, stages it, records state/report
evidence and stops. It never applies the staged bundle. The operator can
review and apply it with the existing `pc-agent update apply latest-staged`
flow.

### `apply-when-idle`

Apply mode first follows the same stage-only verification. It may apply only
when every local gate passes:

- local time is inside the optional maintenance window;
- no task is in `planning`, `running`, `verifying` or `retrying` state;
- the operating-system idle sensor reports at least the configured idle
  minutes (Windows Last Input Info, or Linux logind since Phase 60);
- the operating-system power sensor reports AC power when
  `PC_AGENT_AUTO_UPGRADE_REQUIRE_AC_POWER=1` (Windows power status, or
  Linux power-supply/logind state since Phase 60);
- the staged receipt says `signature_status=verified` with the configured key.

Unknown required idle/power state defers application on every platform.
If a verified update
is already staged, later hourly wake-ups re-check the gates before making a
new network request. Application uses the existing backed-up apply path and
sets `restart_required=true` in state/report evidence.

Optional `PC_AGENT_AUTO_UPGRADE_REINSTALL=1` runs the bounded command
`python -m pip install --no-deps --no-build-isolation -e .` from the
application root after a successful apply. It refreshes editable metadata
without resolving or downloading dependencies. If it is off, reinstall and
restart manually before trusting the new version.

## Configuration

Add the exact feed/key values already proven by `pc-agent update
hosting-check`, then add the background opt-in. A conservative staged
rollout is:

```dotenv
PC_AGENT_ONLINE_UPDATES=1
PC_AGENT_UPDATE_FEED_URL=https://updates.example.com/self-healing-pc-agent/stable.json
PC_AGENT_UPDATE_CHANNEL=stable
PC_AGENT_REQUIRE_SIGNED_UPDATES=1
PC_AGENT_AUTO_UPGRADE=1
PC_AGENT_AUTO_UPGRADE_MODE=stage-only
PC_AGENT_AUTO_UPGRADE_INTERVAL_HOURS=24
PC_AGENT_AUTO_UPGRADE_IDLE_MINUTES=15
PC_AGENT_AUTO_UPGRADE_MAINTENANCE_WINDOW=02:00-05:00
PC_AGENT_AUTO_UPGRADE_REQUIRE_AC_POWER=1
PC_AGENT_AUTO_UPGRADE_REINSTALL=0
```

Also set the exact `PC_AGENT_RELEASE_PUBLIC_KEY` and
`PC_AGENT_RELEASE_KEY_ID` values from the vendor key material. Never put a
release private key in a customer `.env`, report, command line or hosted
feed tree.

The maintenance window uses local `HH:MM-HH:MM` and may cross midnight. The
Windows task wakes at logon and hourly; the configured interval and window
inside the worker decide whether any network work is due. Failures use
bounded exponential backoff up to seven days.

## Windows operator flow

Run these one at a time from `D:\omni-assistant` after updating `.env`:

```powershell
.\.venv\Scripts\pc-agent.exe doctor
```

```powershell
.\.venv\Scripts\pc-agent.exe update auto-status
```

```powershell
.\.venv\Scripts\pc-agent.exe update auto-run --force --report data\updates\auto-upgrade-reports\first-auto-run.json
```

```powershell
.\.venv\Scripts\pc-agent.exe update auto-verify-report data\updates\auto-upgrade-reports\first-auto-run.json
```

Only after that foreground pass behaves as expected, install the task:

```powershell
.\.venv\Scripts\pc-agent.exe update auto-install-schedule --yes
```

```powershell
.\.venv\Scripts\pc-agent.exe update auto-schedule-status
```

To stop future wake-ups without deleting reports/state/staged receipts:

```powershell
.\.venv\Scripts\pc-agent.exe update auto-uninstall-schedule --yes
```

To enable actual application later, change only the mode (and optionally the
reinstall flag), then run `pc-agent update auto-status` and one foreground
`auto-run` again:

```dotenv
PC_AGENT_AUTO_UPGRADE_MODE=apply-when-idle
PC_AGENT_AUTO_UPGRADE_REINSTALL=1
```

After an applied update, restart `pc-agent` and run `pc-agent doctor`.
Rollback remains the explicit existing command:

```powershell
.\.venv\Scripts\pc-agent.exe update rollback latest-applied
```

## State, reports and GUI

The worker retains:

- `data\updates\auto-upgrade-state.json` — self-hashed latest status,
  next-check time, failure count, staged/applied IDs and restart flag;
- `data\updates\auto-upgrade-reports\` — one strict, self-hashed JSON
  report per run with checks for configuration, lock, due time, window,
  feed, staging, gates, apply, reinstall and state recording;
- the normal Phase 21/24 staged-update and download receipts.

Reports contain no API keys, release private keys or response bodies.
`pc-agent update auto-verify-report` verifies a retained report offline.
Reports created by the older worker immediately before it applies a newer
version remain verifiable after restart.

The Windows GUI Signed Updates section can show auto status, run one
explicit pass under the saved `.env` policy, show task state and
install/remove the Windows task after confirmation. The GUI does not edit
`.env`, so mode/feed/key changes remain deliberate file changes.

## Proof boundary

Sandbox tests prove the signed-feed state machine, stage-only behavior,
idle/active-task/maintenance deferrals, gated apply, optional reinstall hook,
report verification and encoded Windows scheduler commands with fake
PowerShell/task runners. They do not prove the user's real Windows task,
public DNS/certificate/host or an end-to-end upgrade from that public host.
That proof exists only after Phase 55 `hosting-check` passes against the real
URL, the Windows task is installed, and a retained real `auto-run` report is
verified.

## Phase 58 follow-up — **Windows Auto-Upgrade Lock Hotfix**

Real Windows testing of repeated `run_auto_upgrade` calls found that the
persistent lock file could be locked at its EOF byte and then incorrectly
unlocked at byte 0, raising `PermissionError` on a later run. Phase 58
normalizes Windows `msvcrt.locking` to byte 0 for both operations and adds
an offset-faithful regression. See `docs/phase58.md`.

## Phase 59 follow-up — **Linux systemd-user Auto-Upgrade Scheduler**

Phase 59 later implements this document's reserved Linux profile with a
per-user systemd one-shot service and hourly persistent timer behind the
same direct CLI/GUI schedule controls. The signed worker policy is
unchanged; Linux `stage-only` is functional, while Linux
`apply-when-idle` remains conservative until its sensor readings are
available. The macOS LaunchAgent remains planned. See
`docs/phase59.md`.

## Phase 60 follow-up — **Linux Idle and AC Power Sensors**

Phase 60 implements Linux idle readings from systemd-logind interactive
sessions and AC readings from `/sys/class/power_supply` with logind's
external-power fallback. Unknown required readings now defer on every
platform. See `docs/phase60.md`.
