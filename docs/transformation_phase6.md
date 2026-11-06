# Transformation Phase 6 — Live monitoring and advanced diagnostics

## 1. Existing findings

- The product had health *checks* (doctor, diagnostics counts, release
  audit) but no live *system* telemetry: no CPU/RAM/disk/network/GPU or
  process data anywhere. Phase 5's Command Center deliberately shipped
  without system widgets rather than show invented numbers.
- No new dependency was acceptable for a monitoring baseline
  (`psutil` would be convenient but adds a compiled wheel matrix to
  every installer); the standard library plus `/proc` and Win32 ctypes
  covers the required sensors.

## 2. What changed

### `src/pc_agent/system_monitor.py` (stdlib-only, fail-soft)
- **CPU %** between samples: Windows `GetSystemTimes`, Linux
  `/proc/stat`, otherwise a labelled load-average estimate. First
  sample honestly returns `null` (a rate needs a baseline).
- **Memory**: Windows `GlobalMemoryStatusEx`, Linux `/proc/meminfo`
  (uses `MemAvailable`, not just free), POSIX `sysconf` fallback.
- **Disk**: `shutil.disk_usage` for install + workspace volumes.
- **Agent process**: RSS (`GetProcessMemoryInfo` / `/proc/self/statm` /
  `getrusage` peak fallback, method always recorded), Python thread
  count, process uptime; **system uptime** (`GetTickCount64` /
  `/proc/uptime`).
- **Network**: byte counters + computed up/down rates between samples
  (Linux `/proc/net/dev` excluding loopback from totals; Windows
  `GetIfTable` best effort).
- **GPU**: `nvidia-smi --query-gpu=...` when present (name, %
  utilization, VRAM used/total). Non-NVIDIA or missing tools report
  `available: false` with the reason — AMD/Intel telemetry is deferred,
  not faked.
- **Battery**: Windows `GetSystemPowerStatus`, Linux
  `/sys/class/power_supply` (`null` on desktops/VMs without one).
- **Top processes by RSS** (8): Linux `/proc` scan; Windows
  `EnumProcesses` + `GetProcessMemoryInfo` + image names, skipping
  protected processes. Everything is wrapped so a sensor failure
  degrades to `null`/empty, never an exception or a made-up value.
- `SystemMonitor` is stateful per process (baselines + a 120-point
  in-memory history of cpu/memory/network for sparklines).

### Surfaces
- CLI: `pc-agent system status` (human + `--json`, two samples so
  rates are real) and `pc-agent system sample --count N --interval S`.
- API: `GET /v1/system` returning `{snapshot, history}`; the
  `LocalService` owns one monitor so rates are continuous across polls.
- Web Command Center: new **System (live)** panel — CPU/memory/disk
  bars (real percents, `n/a` when a baseline is missing), network
  rates, agent RSS, GPU line (or the honest unavailable reason),
  battery, and the top-process table.
- Tkinter Command Center: a `System:` live line inside the overview.
- Doctor prints a one-line `System:` summary (primed with a 150 ms
  baseline sample); diagnostics export gains a full `system` section
  (two samples, 200 ms apart); the release audit gains a
  `system_monitor` check (20 total).

## 3. What was intentionally reused

- Phase 3 service/API, Phase 5 UI shells, existing doctor/diagnostics/
  release plumbing. No schema change (telemetry is live state, not
  stored records — persistence/alerting belongs to Phase 14).

## 4. Deferred / honest limits

- AMD/Intel GPU utilization, temperatures, fan speeds: no reliable
  stdlib source; reported unavailable.
- Windows sensor code is implemented against documented Win32 APIs but
  is **UNVERIFIED** until the Windows acceptance run exercises it —
  Linux `/proc` paths are sandbox-verified in this phase.
- Historical metric storage, thresholds, and proactive alerts:
  Phase 14. Per-core CPU graphs: not collected (aggregate only).
