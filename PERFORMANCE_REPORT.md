# Performance Report

## Evidence status

Only Linux sandbox smoke measurements exist. They are useful regression
signals, not Windows startup, GUI, idle-resource or end-to-end task
benchmarks.

## Audit environment

- Linux x86_64, 2 vCPU, Intel Xeon at 2.60 GHz, approximately 2 GB RAM.
- Python 3.13.16.
- GitHub Phase 58 / 0.60.0 audit copy installed in an isolated venv.

## Measured CLI process medians (five runs)

| Command | Median |
|---|---:|
| `--version` | 0.437 s |
| `doctor` | 0.519 s |
| `gui --check` | 0.537 s |
| `tools list` | 0.435 s |
| `providers list` | 0.457 s |

Importing the CLI/GUI modules and building the 62-tool registry took 0.380 s
in one in-process run with a 54.6 MiB Linux maximum RSS. A wheel built from
the audit tree was 685,763 bytes.

## Known performance boundaries

- Orchestrated tools still run sequentially; since Transformation Phase 2
  the data-context subset runs in one persistent worker subprocess per
  top-level task run (one extra interpreter start, JSON round-trips per
  tool call), while browser/desktop/vision stay in-process.
- The GUI uses one worker thread and a 100 ms event poll.
- Hard deadlines now bound every orchestrated tool call; CPU/RAM quotas
  for tools still do not exist.
- No CPU/RAM quotas exist for dynamic tools.
- SQLite opens a connection per repository operation; large-database and
  multi-process performance have not been benchmarked.
- Provider latency/cost is not persisted or used for routing.

## Transformation Phase 2 sandbox observations (0.64.0)

Same Linux sandbox class as above, Python 3.13.16:

- Full test suite grew from 621 tests / 84.59 s (Phase 1) to 636 tests /
  125.79 s. The increase is dominated by deliberate timeout tests (one
  ~6 s worker kill, one ~2.3 s thread abandon) and one worker-process
  start per orchestrated test run; it is a test-time cost signal, not a
  user-task benchmark.
- One deterministic two-step Folder A/B PDF merge+extract run through
  the isolated worker reported 0.4 s task-budget elapsed (0.82 s total
  process wall time in the sandbox shell).
- Windows startup, worker-spawn cost and idle figures are still
  unmeasured and must come from the Windows acceptance run.

## Transformation Phase 3 sandbox observations (0.65.0)

- Local API request handling uses bounded threads; task runs through the
  service execute on one background thread per submission and return
  immediately (202-style). No throughput or latency benchmark was run in
  the sandbox; API latency on Windows must come from the acceptance run.
  Event inserts are single-row writes with a retention trim (10,000-row
  cap), designed to stay negligible beside tool execution time.

## Transformation Phase 4 sandbox observations (0.66.0)

- Mission parallelism is bounded by `mission_max_parallel` (default 3);
  each branch starts its own Safety Kernel worker, so wall-clock wins
  depend on tool mix and host CPU. No comparative benchmark was run in
  the sandbox; Windows timing remains an acceptance-run item.

## Required Windows measurements

Before performance claims are made, record on the target Windows machine:

- cold/warm CLI and GUI startup;
- GUI idle CPU/RAM over at least 10 minutes;
- representative PDF merge/extract and Office tasks;
- provider latency by model without storing prompt content;
- database growth and task-list latency;
- installer and update apply/rollback duration;
- worker CPU/RAM under a deliberately slow tool after Phase 2.

## Transformation Phase 6 monitoring cost (Linux sandbox, 0.68.0)

Measured 2026-11-04 in the audit sandbox (Linux x86_64, 2 vCPU,
~2 GB RAM, Python 3.13.16), five samples each:

| Measurement | Result |
|---|---:|
| `SystemMonitor.snapshot()` with top-process scan, median | 2.6 ms |
| `SystemMonitor.snapshot()` without process scan, median | 0.2 ms |
| `/v1/system` snapshot JSON size | 1,678 bytes |
| `pc-agent --version` process time | 0.470 s |
| `pc-agent system status` end-to-end (includes 0.25 s baseline pause) | 0.677 s |
| Agent process RSS during `system status` | 59.3 MB |

The monitor keeps at most 120 history points in memory and adds no
background thread: sampling happens only when a CLI/API/UI caller asks.

## Transformation Phase 7 provider platform cost (Linux sandbox, 0.69.0)

Measured 2026-11-04 in the audit sandbox (Linux x86_64, 2 vCPU,
~2 GB RAM, Python 3.13.16) against the offline mock provider — these
numbers prove local overhead only, not any live model's latency:

| Measurement | Result |
|---|---:|
| Mock stream time-to-first-delta, median of 5 | 0.01 ms |
| Built-in evaluation suite (4 cases, mock route) | 0.5 ms total |
| Usage metering | one JSONL append per provider attempt; file rotates at 4 MB |

Strategy ordering reads at most the last 8 MB of the usage file when a
router is built (`fastest` only); task execution paths are otherwise
unchanged, and planning still uses non-streaming completions.

## Transformation Phase 8 memory/knowledge cost (Linux sandbox, 0.70.0)

Measured 2026-11-04 in the same sandbox (Linux x86_64, 2 vCPU,
~2 GB RAM, Python 3.13) with the default offline hashing embedder:

| Measurement | Result |
|---|---:|
| Hashing embed, per ~900-char chunk | 0.107 ms |
| Hybrid search over 144 chunks (10 documents), median of 25 | 22.5 ms |
| Hybrid search over 144 chunks, p95 | 24.7 ms |
| Ingest one ~15 KB text document end-to-end (chunk+embed+store) | 6.3 ms |

Search cost is linear in stored chunks (SQLite load + per-chunk
keyword tokenization dominates; the cosine itself is negligible), so
expect roughly 150 µs per chunk per query on this class of hardware
until an index/caching layer is added — fine for personal-scale
knowledge bases (thousands of chunks), not yet for millions. No
background indexing exists: embedding work happens only on explicit
ingest, note writes, `memory reindex`, and a bounded (50/query) lazy
note backfill during search.

## Transformation Phase 9 multi-agent cost (Linux sandbox, 0.71.0)

Measured 2026-11-04 in the same sandbox (Linux x86_64, 2 vCPU,
~2 GB RAM, Python 3.13), deterministic planning only:

| Measurement | Result |
|---|---:|
| Default 64-tool registry build | 0.57 ms |
| Filtered agent registry build (subset of default) | 0.63 ms |
| Solo agent run, trivial list-files task (task + ledger + kernel) | ~253 ms median |
| 3-assignment team (1 PDF merge, then 2 parallel list branches) | ~576 ms total |

Agent overhead over a plain task is one registry build plus two
ledger writes — noise next to tool execution. Team wall-clock is the
sum of level maxima, bounded by `mission_max_parallel`; each branch
pays the same per-branch orchestrator/kernel startup as a mission
branch. Coordinator decomposition costs exactly one extra provider
completion (<=1200 output tokens) when used.

## Transformation Phase 10 voice cost (Linux sandbox, 0.72.0)

Environment: Linux x86-64 container, Python 3.13, SQLite WAL, no real audio
device (capture backend replaced by a deterministic fake that writes a valid
16 kHz mono WAV; STT/TTS replaced by fakes so the numbers measure *our*
overhead, not an engine's). 10-30 iterations, median and max reported.

| Operation | Median | Max |
| --- | ---: | ---: |
| `VoiceService` construction (repo + privacy gate + probes) | 0.57 ms | 0.76 ms |
| `voice status` (engine discovery + device scan + session summary) | 2.66 ms | 3.14 ms |
| `wav_statistics` on a 1 s / 16 kHz mono WAV (RMS + peak) | 1.83 ms | 1.97 ms |
| `wav_statistics` on a 5 s WAV | 9.01 ms | 10.26 ms |
| push-to-talk round trip (capture -> transcribe -> ledger -> event) | 6.27 ms | 6.77 ms |
| session list (20 rows) | 0.69 ms | 0.82 ms |

Interpretation: the voice *plumbing* is negligible compared with real audio.
Microphone capture is real time by definition; STT/TTS latency is entirely
engine-bound (Windows System.Speech is roughly real time, faster-whisper on CPU
depends on the chosen model, espeak-ng is near instant) and is therefore
reported by the engine, never estimated here. Capture runs on a worker thread,
so the GUI/HTTP caller never blocks while recording.

Cold-start impact: `import pc_agent.voice` is lazy - CLI/API/GUI import it only
when a voice command or the Voice tab is used, so startup time for non-voice
work is unchanged (measured `pc-agent --version` remains ~0.3 s).

## Transformation Phase 15 plugin cost (Linux sandbox, 0.77.0)

Measured on the delivery sandbox (Python 3.13.16, warm page cache, local
filesystem, no network), median/max over 10-50 rounds:

| Operation | Median | Max |
| --- | --- | --- |
| `verify_bundle` (zip read + SHA-256 + Ed25519) | 0.32 ms | 1.06 ms |
| `install` (extract + registry + version row) | 3.17 ms | 3.35 ms |
| `status()` | 1.52 ms | 1.67 ms |
| `run_tool` (spawn + rlimits + worker + result + ledger) | 43.7 ms | 59.6 ms |
| `runs(limit=25)` | 0.80 ms | 1.21 ms |

Reading:

- **Verification is sub-millisecond**; the signature is checked over the
  manifest bytes only, and file digests are computed while streaming the archive.
- **The only expensive operation is process creation** (~44 ms). That cost is the
  isolation itself and it is paid per tool call, never in the background - no
  daemon, no watcher, no idle CPU.
- **Cold start is unaffected**: `pc_agent.plugins` is imported only when a plugin
  command, route or tab is used (`pc-agent --version` stays ~0.3 s).
- The ledger read is indexed on `created_at`, so the dashboard refresh does not
  scan the run history.

## Transformation Phase 14 proactive cost (Linux sandbox, 0.76.0)

Measured on the delivery sandbox (Python 3.13.16, warm page cache, in-memory
SQLite on the local filesystem, no network, no configured connectors):

| Operation | Median | Max |
| --- | --- | --- |
| `add_reminder` (validate + insert + return) | 3.06 ms | 4.52 ms |
| `tick()`, nothing due | 0.71 ms | 1.30 ms |
| `tick()`, 20 due reminders | 1.46 ms | 1.72 ms |
| `status()` | 2.21 ms | 3.80 ms |
| `notifications(limit=25)` | 0.69 ms | 0.87 ms |
| `build_briefing()` (all six sections) | 4.85 ms | 5.56 ms |
| `run_briefing()` when today's already went out (guard path) | 0.76 ms | 1.27 ms |
| `notify()` in-app (policy + ledger + event) | 2.38 ms | 6.45 ms |

Reading:

- **One tick is sub-millisecond when nothing is due**, so adding it to the
  existing scheduler loop does not change idle behaviour measurably (the loop's
  own interval dominates). The first `tick` of a fresh process pays a one-off
  ~40 ms import/warm cost; it does not repeat.
- **Cost is bounded by configuration, not by history**: a tick reads at most
  `proactive_tick_limit` rows through the `(status, due_at)` index, and the
  hourly cap/cooldown are single indexed lookups.
- **Briefings are composed from state that already exists.** With connectors
  off, the calendar/drive sections short-circuit to a single line, which is why
  the measured 4.85 ms already includes the worst case of "every subsystem
  answering". No filesystem walk is added by this phase.
- **Guarded re-entry is cheap** (0.76 ms), so calling `run_briefing()` from a UI
  button or a scheduler tick cannot accidentally rebuild the briefing.
- Suite cost: 852 tests in 174 s on this sandbox; the 24 proactive tests alone
  run in 2.3 s, i.e. the simulated 24-hour determinism test costs well under a
  second.

## Transformation Phase 13 connector cost (Linux sandbox, 0.75.1)

Measured on the delivery sandbox (Python 3.13.16, warm page cache, median of
10-200 rounds per operation, no network):

| Operation | Median | Max |
| --- | --- | --- |
| Calendar `add_event` (parse + atomic write) | 0.18 ms | 2.16 ms |
| Calendar `list_events` over 40 events | 0.51 ms | 0.96 ms |
| Drive scan, 51 files / 1.6 MB | 4.11 ms | 4.31 ms |
| Webhook verify (HMAC + JSON parse + sanitise) | 0.02 ms | 0.13 ms |
| `ConnectorService.status()` (no probe) | 0.71 ms | 1.18 ms |
| Ledger read, 25 events | 0.63 ms | 0.73 ms |

Reading:

- **Idle cost is zero.** Nothing polls. There is no background thread, no timer
  and no watcher: a connector runs only when the user (or a flow) asks.
- **Filesystem work is linear and capped**: at most 1 000 files by default,
  32 MiB hashed per file, so a pathological folder degrades into a reported
  `truncated` flag instead of a hang.
- **Network calls are bounded twice**: `connector_timeout_seconds` (default
  15 s) and `connector_max_items` (default 25). Mail reads fetch headers only,
  which is what keeps an inbox query fast on a slow IMAP server.
- **The ledger is trimmed inside the same transaction** that inserts a row, so
  it cannot grow unbounded and never needs a maintenance pass.
- **Startup cost is unchanged**: the connectors package imports with stdlib
  modules only (`imaplib`, `smtplib`, `urllib`, `hashlib`, `hmac`) and no
  third-party dependency is added.

## Transformation Phase 12 workflow cost (Linux sandbox, 0.74.0)

Environment: same deterministic sandbox as earlier phases (no GPU, no display,
local SQLite on the sandbox filesystem). Medians of 200 runs (50 for the tick).

| Operation | Input | Median | Max |
| --- | --- | --- | --- |
| `validate_definition` (+`topological_levels` + `mission_specs`) | 5-node chain | 0.043 ms | 0.171 ms |
| `FlowStore.create` + `get` | 1 flow | 0.711 ms | 1.05 ms |
| `FlowStore.list_flows(limit=20)` | 20 flows | 0.727 ms | 1.06 ms |
| `FlowRunner.run_flow` (reconcile + run row + compile + start + ledger + event) | 5 nodes | 7.6 ms | 12.9 ms |
| `FlowRunner.tick` | 8 due schedule triggers | 1.42 ms | 2.33 ms |
| `FlowRunner.reconcile` | 50 runs | 0.63 ms | - |

Notes that matter for predictability:

- Cost is dominated by SQLite writes (three statements per run), not by Python;
  validation of a 20-node document stays well under a millisecond.
- Nothing in the request path is unbounded: nodes (20), triggers (8), tick budget
  (50) and listed runs (500) are capped, so the worst case is a constant.
- `tick` only touches indexes (`idx_flow_triggers_due` on
  `(enabled, next_run_at)`), so a database with hundreds of stored flows does not
  slow the sweep.
- The GUI runs every flow action on the existing worker thread, so a slow
  `run_flow` (a mission start) cannot freeze the window; the web panel awaits the
  same endpoints.
- No new dependency, no new daemon, no extra startup work: `flows.py` is imported
  lazily by the service, so `pc-agent --version`/`doctor` startup is unchanged.


## Transformation Phase 11 automation and vision cost (Linux sandbox, 0.73.0)

Environment: same deterministic sandbox as earlier phases (no GPU, no real
display, generated PNG fixtures). Medians of repeated runs.

| Operation | Input | Median |
| --- | --- | --- |
| `analyse_image(grid=4)` | 1366x768 PNG | 68 ms |
| `analyse_image(grid=4)` | 1920x1080 PNG | 126 ms |
| `analyse_image(grid=4)` | 2560x1440 PNG | 182 ms |
| `vision.screen` tool end-to-end (file read + analysis + schema) | 1920x1080 | 136 ms |
| `locate_template` (400x320 template) | 1920x1080 | 0.5-0.9 s |
| `locate_template` (400x320 template) | 2560x1440 | 0.9-1.4 s |
| `locate_template` (260x120 template) | 2560x1440 | ~0.55 s |
| `automation_runs` insert | 1 row | 1.4 ms |
| `AutomationRepository.summary()` | 200 rows | 0.6 ms |
| `automation capabilities` (probes + tool count + summary) | - | ~40 ms |

Notes that matter for predictability:

- Ink density uses Pillow's C filters (resize + `FIND_EDGES` + `histogram`), so
  cost scales with pixels but with a small constant; the previous pure-Python
  per-pixel loop was ~3x slower at 1080p for the same result.
- Template search cost is bounded by constants, not by screen size:
  `MAX_OFFSETS_PER_AXIS = 96`, `MAX_SEARCH_STEPS = 40 000`, `TEMPLATE_SAMPLES = 96`
  for nomination, `CANDIDATE_LIMIT = 16`, then **one** full-pixel verification per
  surviving candidate. A 4K screenshot therefore takes roughly the same time as a
  1440p one, and never minutes.
- `browser.wait` costs one bounded JS probe per poll plus the poll interval; the
  measured sandbox cost is negligible and the wall time is dominated by the page.
- Cold start is unaffected: `perception` imports Pillow lazily inside the
  functions that need it, and the GUI tab refresh is deferred (`after(700, ...)`),
  so a non-automation session pays nothing.

## Transformation Phase 16 encrypted sync cost (0.78.0, Linux/CPython 3.13)

Measured with two paired device identities on a real database and real bundles on
disk (`tmp_path`), 30 iterations for the fast paths:

| Operation | Result |
| --- | --- |
| identity load | 0.02 ms |
| `status()` | 3.4 ms |
| `export()` empty, 1 recipient | 7.8 ms |
| `import_file()` empty bundle | 8.2 ms |
| `export()` 200 reminders | 10.3 ms, 25 304 B |
| `import_file()` 200 new reminders | 671 ms total, 3.3 ms per record |
| bundle size per reminder | ~127 B |
| `tick()` with 2 bundles | 31 ms |

Interpretation: sealing is cheap (one X25519 ECDH + one HKDF + one AES-256-GCM
per recipient), so bundle cost is dominated by JSON canonicalisation at export and
by the ordinary validated SQLite insert at import. There is no background thread,
no scheduler hook and no network call; sync only runs when the user runs it. The
import path is deliberately all-or-nothing on refusals decided up front (size, JSON,
replay, trust, signature, clock, recipient, decryption, schema, caps) before any
record is written, so a refused bundle costs a few milliseconds and no partial
writes.

Windows numbers for this phase remain pending real Windows evidence, like Phases
12-15.

## Transformation Phase 17 interface cost (0.80.1, Linux/CPython 3.13)

Measured with `pc-agent perf bench` on this machine:

| Operation | Result |
| --- | --- |
| `a11y_status()` (7 contrast pairs + 4 locale coverages + report assembly) | 0.06 ms |
| `health()` warm | 0.64 - 0.80 ms |
| `health()` first call | 0.75 - 0.97 ms |
| `list_events(limit=20)` | 1.1 - 1.7 ms |
| `system_status()` | 2.6 - 3.4 ms |

The accessibility/multilingual layer is effectively free: it is computed only when
a surface asks for it, from constants in memory, and it adds no timer, thread,
background refresh or network call. Style and palette application happen once when
the desktop window is built, so the 19th tab costs a single extra frame build.

`pc-agent perf bench` now exists so these numbers can be reproduced on the user's
own machine instead of being quoted from a report. Provider latency, GUI paint time
and cold disk start are explicitly excluded from what it measures.

## Phase 18 hardening measurements (0.80.1)

Measured on the verification host (CPython 3.13, 2 vCPU container). These numbers
are **asserted** in `tests/test_performance_suite.py`; the ceiling column is the
test budget, chosen with roughly 10x headroom so a slow shared runner does not
fail a correct build.

| Operation | Observed | Budget (test) | Method |
| --- | --- | --- | --- |
| Settings load (cold) | ~7 ms | 250 ms | median of 5 |
| `import pc_agent.cli` | ~1.6 s | 12 s | fresh interpreter, wall clock |
| `health()` warm | ~1.0 ms | 400 ms | median of 7 |
| `system_status()` | ~3 ms | 900 ms | median of 3 |
| `list_events(limit=50)` | ~4 ms | 400 ms | median of 5 |
| `a11y_status()` | ~0.1 ms | 200 ms | median of 7 |
| Heap growth, 200x `health()` | < 40 KB | 512 KB | `tracemalloc` |
| Database initialisation | ~0.3 s | 20 s | single shot |
| Release-candidate scan | ~12 ms | 4 s | median of 3 |

Release chain (`pc-agent release production-check`), wall clock on the same host:

| Step | Time |
| --- | --- |
| release_catalog | ~14 ms |
| bundle_build | ~181 ms |
| bundle_verify | ~34 ms |
| tamper_detection | ~149 ms |
| signature_verify | ~62 ms |
| unsigned_refused | ~35 ms |
| stage | ~141 ms |
| apply | ~274 ms |
| rollback | ~235 ms |
| cleanup | ~5 ms |
| **total** | **~1.7 s** |

Notes and honest limits:

- single-process wall-clock timings; provider calls, GUI paint time and cold disk
  reads are excluded, exactly as the `perf bench` notes say;
- the ceilings catch order-of-magnitude regressions (an O(n^2) query, a heavy
  module pulled into start-up), not micro-regressions;
- the idle process holds no polling loop: proactive scheduling and background
  upgrades are OS-scheduled, so idle CPU stays at zero for this process.
