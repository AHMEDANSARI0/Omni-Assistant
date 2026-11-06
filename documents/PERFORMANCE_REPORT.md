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

- Orchestrated tools run sequentially in-process.
- The GUI uses one worker thread and a 100 ms event poll.
- Built-in tool timeouts do not yet provide hard worker interruption.
- No CPU/RAM quotas exist for dynamic tools.
- SQLite opens a connection per repository operation; large-database and
  multi-process performance have not been benchmarked.
- Provider latency/cost is not persisted or used for routing.

## Required Windows measurements

Before performance claims are made, record on the target Windows machine:

- cold/warm CLI and GUI startup;
- GUI idle CPU/RAM over at least 10 minutes;
- representative PDF merge/extract and Office tasks;
- provider latency by model without storing prompt content;
- database growth and task-list latency;
- installer and update apply/rollback duration;
- worker CPU/RAM under a deliberately slow tool after Phase 2.
