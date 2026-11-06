# Transformation Phase 1 — Canonical Baseline and Windows Acceptance

## Purpose

Phase 1 does not add voice, RAG, agents or a new UI. It removes the source
ambiguity found by the Phase 0 audit and freezes the contracts that the next
17 transformation phases must build on.

## Reconciliation decision

- GitHub comparison point: `main` at
  `89610f3d18f9348b6fe8ead679bab81ee34155df`, Phase 58 / 0.60.0.
- Canonical source: delivered Phase 60 tree, Phase 60 / 0.62.0.
- Resulting application version: 0.63.0.
- Phase 59 Linux systemd-user auto-upgrade scheduling and Phase 60 Linux
  idle/AC sensors are retained.
- The cumulative patcher can upgrade the audited GitHub Phase 58 tree and
  the delivered Phase 60 tree to the same Transformation Phase 1 files.
- `.env.local` is ignored. The tracked `.env.example` template is not
  ignored. Personal `hide` ignore patterns from the GitHub upload are not
  adopted as project policy.
- No registered tool is added or removed (62), and SQLite schema remains v6.

## Code and metadata repairs

- Single baseline identity in `src/pc_agent/baseline.py`.
- Doctor/GUI labels use the Transformation Phase 1 label instead of stale
  per-module phase strings.
- CLI help no longer labels the application as Phase 44.
- `cryptography` is declared as a direct dependency because PDF modules
  import it directly.
- Release audit includes a `transformation_baseline` check for required
  documents, `.env.example` ignore policy and the direct dependency.
- Diagnostics includes the secret-free baseline identity and status.
- Root architecture, roadmap, feature, security, performance and test
  reports are included in release-bundle candidates.

## Evidence completed in the build environment

- Full non-shallow GitHub history secret-pattern scan (9 commits, 533
  reachable blobs): no high-confidence cloud/provider token patterns; the
  only private-key-header match is a rejection check in source code.
- Linux full test, Ruff, doctor, GUI-check and clean-room patcher results
  are recorded in `TEST_REPORT.md` for the exact delivered patcher.

## Evidence still pending

Real Windows acceptance remains pending. Follow
`docs/windows_acceptance.md` on the target Windows machine and retain the
secret-free outputs. Do not mark native Windows GUI, UI Automation,
Credential Manager, installer, Task Scheduler or live signed-feed behavior
complete from Linux evidence.
