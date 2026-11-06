# Phase 59 — Linux systemd-user Auto-Upgrade Scheduler

Phase 56 implemented the signed background worker and the Windows
Scheduled Task, while reserving Linux and macOS scheduler profiles. Phase
59 implements the **Linux systemd-user Auto-Upgrade Scheduler** without
changing the worker's signed HTTPS policy, staged-update trust rules or
report format. The **macOS LaunchAgent** profile remains planned.

## Commands

The existing direct commands now dispatch by platform:

- `pc-agent update auto-install-schedule`
- `pc-agent update auto-schedule-status`
- `pc-agent update auto-uninstall-schedule`
- `pc-agent update auto-status` (includes the current OS scheduler)

On Windows they still manage the Scheduled Task. On Linux they manage a
per-user systemd service/timer pair. The GUI Signed Updates schedule
buttons use the same generic dispatcher. No planner or registered tool
can install, remove or inspect the schedule.

`--task-name` remains the shared CLI option. On Linux the default Windows
task name maps to the unit base `self-healing-pc-agent-auto-upgrade`; a
custom Linux value must be a safe unit base name without `.service` or
`.timer`.

## Units installed

The managed files are installed below the systemd user unit directory
(`$XDG_CONFIG_HOME/systemd/user`, or `~/.config/systemd/user`):

- `self-healing-pc-agent-auto-upgrade.service`
- `self-healing-pc-agent-auto-upgrade.timer`

The service is a one-shot invocation of the existing
`pc-agent --root ... update auto-run` command with the application root as
its working directory, a one-hour start timeout, `NoNewPrivileges=true`,
`PrivateTmp=true` and `UMask=0077`. It is not a resident daemon written by
this project.

The timer uses:

- `OnBootSec=5min`
- `OnUnitInactiveSec=1h`
- `RandomizedDelaySec=5min`
- `AccuracySec=1min`
- `Persistent=true`
- `WantedBy=timers.target`

A systemd user timer runs while that user's systemd manager is active. If
it must continue after logout, the operator must separately enable user
linger through the operating system; this installer never uses sudo and
does not alter linger policy.

## Safety and failure behavior

Installation first proves that `systemctl --user` can reach the user
manager. It refuses to overwrite a same-named service or timer unless the
file contains this application's managed marker. Unit files are written
atomically. If daemon-reload or enable fails, previously managed unit
contents are restored (or newly written files removed) and daemon-reload
is attempted again.

Status combines managed-file state with `systemctl --user show`. If the
user manager is unavailable, status returns `unavailable` instead of
crashing the whole auto-upgrade status command. Uninstall disables the
timer when possible and removes only managed unit files plus this timer's
`timers.target.wants` link; state, reports, staged updates and `.env`
are preserved.

## Linux apply boundary

The systemd timer wakes exactly the same one-shot worker. **stage-only**
is the functional and recommended Linux mode: it can check, download,
verify and stage, but never applies.

At the Phase 59 boundary, Phase 56's user-idle and AC-power sensors were
still Windows-only, so Linux **apply-when-idle** remained a conservative
defer path rather than guessing that the machine was idle or on AC power.
Phase 60 subsequently implements the **Linux Idle and AC Power Sensors**
described in `docs/phase60.md`; unknown readings still defer.

## Validation boundaries

Automated tests use an offset-faithful fake `systemctl`, prove unmanaged
unit refusal and rollback, and run the real `systemd-analyze verify`
oracle when Linux systemd tools are present. A target machine is proven
only when its own user manager accepts the install, `auto-schedule-status`
reports the timer installed/active, a foreground `auto-run` report is
verified, and uninstall removes the managed timer.
