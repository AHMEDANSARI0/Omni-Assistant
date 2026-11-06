# Phase 60 — Linux Idle and AC Power Sensors

Phase 59 added the Linux systemd-user scheduler, but the worker still had
only Windows user-idle and AC-power sensors. Phase 60 implements the
**Linux Idle and AC Power Sensors** used by the existing signed
`apply-when-idle` gate. It does not change the scheduler units, update-feed
trust policy, staged-update format, report format, database schema or tool
registry.

## Linux idle sensor

`get_idle_seconds()` on Linux asks **systemd-logind** through `loginctl`:

1. list sessions as bounded JSON and select only sessions owned by the
   current UID;
2. read each selected session's `Class`, `State`, `IdleHint` and
   `IdleSinceHintMonotonic` properties;
3. count only interactive `user`, `user-early` and `lock-screen` sessions;
4. return `0` as soon as an interactive session explicitly reports not
   idle;
5. otherwise convert the monotonic idle-since timestamp into seconds and
   use the smallest value across the user's interactive sessions.

The per-user manager and background sessions are not treated as keyboard
or pointer activity. Closing sessions are ignored. Missing logind data,
malformed output, a vanished session, no interactive session, an unknown
state, or a zero/future/sentinel timestamp returns *unknown* rather than
a guessed idle interval.

The worker invokes `loginctl` directly without a shell, with a five-second
timeout and bounded output. The canonical system bus is used; a stray
`DBUS_SYSTEM_BUS_ADDRESS` is not allowed to redirect this safety sensor.

## Linux AC-power sensor

`get_ac_power()` on Linux first reads the kernel ABI under
**`/sys/class/power_supply`**:

- any `Mains` supply with `online=1` means external power;
- known mains supplies that are all offline mean battery power;
- without a mains reading, a present battery that is `Charging` or `Full`
  means external power, while `Discharging` means battery power;
- `Not charging`, `Unknown`, malformed values and missing supplies remain
  unknown at this layer.

Only when sysfs is inconclusive does the sensor fall back to logind's
`OnExternalPower` manager property. This covers desktops and virtual
machines that expose no battery/mains power-supply entries. A determinate
sysfs result is never overridden by the fallback.

## Fail-closed gate

The shared apply gate is also hardened. Whenever the configured idle
threshold is above zero, unknown idle state produces `idle_unknown` on
every platform. Whenever AC power is required, unknown power state
produces `ac_power_unknown` on every platform. Both outcomes defer the
verified staged update; neither is treated as permission to apply.

An operator can still explicitly set the idle threshold to zero or disable
the AC-power requirement. Those are deliberate configuration choices, not
sensor guesses. `stage-only` remains the default and never applies.

## Validation

Automated tests cover:

- multi-session logind parsing and the minimum idle value;
- manager/background/closing session handling;
- active sessions and malformed or ambiguous logind output;
- mains and battery sysfs combinations;
- logind external-power fallback only after inconclusive sysfs data;
- unknown sensors deferring on Linux, Windows, macOS-labelled and unknown
  platform paths;
- a Linux worker run applying a verified fake pending update only after
  valid idle and AC readings, then deferring when idle becomes unknown;
- a live `loginctl` oracle on Linux proving the real command path returns
  either a valid reading or unknown without crashing.

The live sandbox used for this phase has no logind login session and no
power-supply entries, so it proves the command path and external-power
fallback return safely, not a real desktop's input-idle duration. A target
Linux desktop is fully proven only when its own graphical session reports
idle state and a retained `auto-run` report records the expected gate
values.
