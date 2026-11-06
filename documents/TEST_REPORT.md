# Test Report

## Phase 0 audit evidence (GitHub Phase 58)

Audited commit: `89610f3d18f9348b6fe8ead679bab81ee34155df`.

- Ruff: clean.
- Full pytest: **603 passed, 1 skipped** in 85.83 s.
- Coverage rerun: **603 passed, 1 skipped** in 118.75 s; line coverage
  76.8% (27,497 of 35,797 statements).
- `doctor`: pass.
- `gui --check`: ready without opening a real window.
- `release audit`: READY, 0 failures, 1 mock-provider warning, 15 checks.
- Deterministic Folder A/B PDF merge-then-extract acceptance: pass.
- The one skip was the real Tk window test in a headless Linux sandbox.

## Transformation Phase 1 additional evidence

- Non-shallow GitHub history secret-pattern scan: 9 commits and 533
  reachable blobs. No high-confidence AWS, GitHub, OpenAI-style, Google or
  Slack token patterns were found. The only private-key-header match is a
  source-code rejection check in `update_feed_hosting.py`.
- Native Windows acceptance: **pending**. Linux results must not be used as
  Windows GUI, UI Automation, Credential Manager, installer, Task Scheduler
  or live-feed proof.

## Transformation Phase 1 Linux verification

Staging tree (version 0.63.0, Python 3.13, Linux sandbox):

- Full pytest: **621 passed, 1 skipped** in 84.59 s. The one skip is the
  real Tk window test in the headless sandbox.
- Ruff: clean.
- `pc-agent --version`: `pc-agent 0.63.0`.
- `pc-agent doctor`: pass; reports the Transformation Phase 1 baseline
  line with `documents_missing=none`, `env_example_ignored=no`,
  `direct_cryptography=yes` and
  `windows_acceptance=pending_user_windows_evidence`.
- `pc-agent gui --check`: READY without opening a window; 62 tools.
- `pc-agent release audit`: READY, 0 failures, 1 expected mock-only
  warning, 16 checks including `transformation_baseline` PASS;
  270 allow-listed bundle files.

Clean-room patcher verification for
`TRANSFORMATION_PHASE_01_CANONICAL_BASELINE.mjs` (216 embedded files,
cumulative Phase 9 -> 0.63.0 delta; two consecutive builder runs produced
a byte-identical patcher):

- Phase 9 (0.11.0) base tree: patcher applied; all 269 allow-listed
  files byte-identical to staging; full pytest **621 passed, 1 skipped**;
  Ruff clean; `gui --check` READY; release audit READY (16 checks).
- Delivered Phase 60 (0.62.0) base tree: patcher reported
  created=10, updated=62, skipped=144; all 269 allow-listed files
  byte-identical to staging; full pytest **621 passed, 1 skipped**;
  Ruff clean; doctor baseline line present; `gui --check` READY;
  release audit READY (16 checks).
- Audited GitHub Phase 58 (0.60.0, commit
  `89610f3d18f9348b6fe8ead679bab81ee34155df`) base tree: patcher reported
  created=14, updated=64, skipped=138; all 269 allow-listed files
  byte-identical to staging; full pytest **621 passed, 1 skipped**;
  Ruff clean; doctor baseline line present; `gui --check` READY;
  release audit READY (16 checks).

No Windows count is claimed here; native Windows acceptance remains
pending real evidence (see `docs/windows_acceptance.md`).
