# Phase 17.1 - Windows acceptance hotfix (0.79.1)

The 0.79.0 phase report ended with "Windows acceptance pending". That acceptance
run was executed on the user's machine and returned:

```
12 failed, 919 passed, 8 skipped in 1284.17s
```

This document is the report for the hotfix that closes those twelve failures.
The phase number stays **17**; only the patch version moved (0.79.0 → 0.79.1).

---

## 1. What already existed before this hotfix

* The complete Phase 17 feature set (i18n catalogs, accessibility palette and
  contrast maths, UI preferences, 19th GUI tab, web accessibility, CLI/API
  surface, perf bench) and its 44 acceptance tests.
* Phase 14 proactive intelligence (reminders, quiet hours, rolling cap, cooldown,
  briefings), Phase 15 plugin ecosystem (signed bundles, sandbox, capability
  audit hook), Phase 16 encrypted device sync (identity, sealed bundles, pairing,
  remote coordination requests).
* A suite that was green in the sandbox at `TZ=UTC`: 982 passed, 1 skipped.

## 2. What was missing

* The suite had never been executed on a **non-UTC** host or on **Windows**. Both
  assumptions were baked in: two Phase 14 assertions compared ISO *strings* of
  different offsets, one Phase 15 fixture used the POSIX path `/tmp`, and two
  Phase 16 assertions read a POSIX file mode on NTFS.
* Two genuine product defects were invisible under the sandbox conditions and
  only surfaced under a different clock/timezone.
* The plugin sandbox had no diagnostic path for "the host cannot start a child
  process at all" - the host failure was reported as a plugin failure.

## 3. What was built / changed

### 3.1 Proactive hourly cap (product defect)

`ProactiveStore.count_notifications_since()` counted every row in
`notifications`, including the `deferred` rows written *by the cap itself*.
Every suppressed tick therefore re-armed its own budget:

```
tick 1 -> hourly limit reached (3/3)
tick 2 -> hourly limit reached (4/3)   # the ledger contains 4 rows, 1 delivered
tick 3 -> reminders never released
```

The budget now counts `status = 'delivered'` rows only. Deferred rows remain in
the ledger (they are the audit record of a suppression) but never consume the
hour. The cap is still enforced inside the same hour - a new test proves both
directions.

### 3.2 Suppression rows used the wall clock (product defect)

`_deliver()` stamped the quiet-hours row with the tick instant but the cap and
cooldown rows with `datetime.now()`. Any tick driven by an injected clock (the
tests, the scheduler's catch-up path) wrote rows into a different timeline, so
the rolling window was evaluated against a mixture of two "now"s. All
suppression rows now carry the tick instant.

### 3.3 Plugin sandbox spawn path (Windows blocker)

* A fixed, non-secret allow-list of Windows variables (`SystemRoot`,
  `SystemDrive`, `windir`, `ComSpec`, `PATH`, `PATHEXT`, `TEMP`, `TMP`,
  `USERPROFILE`, `HOMEDRIVE`, `HOMEPATH`, `APPDATA`, `LOCALAPPDATA`,
  `PROCESSOR_ARCHITECTURE`, `NUMBER_OF_PROCESSORS`, `OS`) is always supplied,
  with `GetWindowsDirectoryW`/`tempfile` fallbacks. None of these can carry a
  credential, and the environment is still built from an allow-list - never
  inherited.
* `interpreter_candidates()` returns `sys.executable`, `sys._base_executable`,
  then `python3`/`python` found on `PATH`. Each candidate is started once with
  `-I -B -c "import sys; sys.exit(0)"` under the *same* scrubbed environment and
  the answer is cached per process.
* `_spawn()` tries the requested interpreter first, then the candidates, and
  collects the reason for every failure (`not found`, `PermissionError`,
  `exit code N: <stderr>`).
* If nothing can start, `SandboxResult.spawn_error` carries the reason, the
  service returns `status="error"` with that text and `returncode=-1`, and the
  plugin is not blamed.
* The interpreter that actually ran is reported as `sandbox.interpreter`.
* **Isolation is not weakened:** every attempt uses the same scrubbed
  environment, the same working directory, the same `-I -B` isolated interpreter
  and the same audit hook, and the Job Object is still assigned immediately
  after the process is created.

### 3.4 Owner-only files on Windows (real gap)

`os.chmod(path, 0o600)` cannot make a file owner-only on NTFS - the mode always
reads `0o666`. New module `src/pc_agent/sync/perms.py`:

* POSIX: `chmod 0600`, then read the mode back (`mechanism: posix-mode`).
* Windows: `icacls <file> /inheritance:r /grant:r <user>:(F)`, then read the ACL
  back and require that **no inherited ACE remains** (`mechanism: windows-acl`).
* Anything else, or a volume/filesystem without ACL support (FAT32, exFAT, a
  network share without permission mapping): `applied: False` plus the reason -
  never a silent pass.

`load_or_create_identity()` and `SyncService.export()` use it, and the export
payload now carries a `permissions` report next to the bundle path.

### 3.5 Test corrections (intent preserved, not weakened)

| Test | Before | After |
| --- | --- | --- |
| `test_due_parsing_accepts_iso_offsets_and_local_times` | asserted a UTC string prefix `2026-12-01T09:30` | asserts the instant equals local 09:30 as UTC, and that it round-trips to `09:30` locally |
| `test_tick_fires_due_reminders_once_and_reschedules_repeats` | compared `"+00:00"` and `"+05:00"` ISO strings | compares `datetime` instants |
| `test_plugin_write_outside_its_roots_is_refused` (+ the plugin body) | wrote to `/tmp` (POSIX-only) | writes to `tempfile.gettempdir()`; still outside the capability roots |
| `test_identity_file_is_owner_only` | `mode == 0o600` on every OS | `_assert_owner_only()`: POSIX mode on POSIX, verified ACL on Windows |
| `test_export_creates_a_single_recipient_bundle_on_disk` | `mode == 0o600` on every OS | `_assert_owner_only()` plus `permissions.applied is True` |

No `pytest.skip` was added, and no assertion was deleted - the same property is
checked through the mechanism the platform actually provides.

## 4. Files added / changed

**Added**

* `src/pc_agent/sync/perms.py` - cross-platform owner-only hardening + report.
* `tests/test_transformation_phase17_1_hotfix.py` - 10 regression tests.
* `docs/transformation_phase17_1.md` - this report.

**Changed**

* `src/pc_agent/proactive.py` - cap counts delivered rows; suppression rows use
  the tick instant.
* `src/pc_agent/plugins/sandbox.py` - Windows env allow-list, interpreter
  candidates, per-interpreter probe cache, structured `spawn_error`,
  `interpreter` in the result.
* `src/pc_agent/plugins/service.py` - maps `spawn_error` to a host-level error
  and exposes `sandbox.spawn_error` / `sandbox.interpreter`.
* `src/pc_agent/sync/crypto.py`, `src/pc_agent/sync/service.py` - use
  `harden_private_file()`, export `permissions`.
* `tests/test_transformation_phase14_proactive.py`,
  `tests/test_transformation_phase15_plugins.py`,
  `tests/test_transformation_phase16_sync.py` - platform/timezone-correct
  assertions.
* Version stamps: `pyproject.toml`, `src/pc_agent/__init__.py`, 59 test modules,
  CHANGELOG, README, ARCHITECTURE, FEATURE_MATRIX, SECURITY_REPORT,
  PERFORMANCE_REPORT, TEST_REPORT, API_DOCUMENTATION, IMPLEMENTATION_ROADMAP.

## 5. Deliberately not included

* No new feature surface: no new CLI command, no new route, no schema change
  (still **v18**, no migration).
* No change to the plugin sandbox's guarantees (job object, rlimits, `-I -B`,
  audit hook, capability roots) - the hotfix only changes *how the interpreter is
  found and reported*.
* No attempt to make `os.chmod` "work" on Windows: the ACL path is the honest
  fix, and the remaining limitation is documented.

## 6. Tests and results

| Run | Result |
| --- | --- |
| `TZ=UTC pytest -q` | **993 passed, 1 skipped** |
| `TZ=Asia/Karachi pytest -q` | **993 passed, 1 skipped** |
| `TZ=America/New_York pytest -q` | **993 passed, 1 skipped** |
| `pytest tests/test_transformation_phase14_proactive.py` under 6 timezones (UTC, Asia/Karachi, America/New_York, Pacific/Kiritimati, Europe/Berlin, Asia/Kolkata) | 24 passed in every one |
| `ruff check .` | All checks passed |
| `pc-agent release audit` | `READY; failures=0; warnings=1; checks=31` |

The single skip is `tests/test_phase5_gui.py::…` - a real Tk window cannot be
created on a headless host. On Windows the 8 skips observed in 0.79.0 stay
skips by design (symlink privilege, POSIX shell, RPM/Debian/systemd/loginctl
oracles).

## 7. Security implications

* **Unchanged and re-verified:** the approval centre, safety kernel, emergency
  stop, capability roots, `-I -B` isolation, audit hook, job object, rlimits,
  Ed25519 signature requirement, and the default-off gates
  (`PC_AGENT_ALLOW_PLUGINS`, `PC_AGENT_ALLOW_SYNC`, outbound delivery).
* **Improved:** plugin worker start-up failures can no longer be mistaken for
  plugin misbehaviour - they are surfaced with the OS reason and the interpreter
  that was tried.
* **Improved:** sync key material and exported bundles are now genuinely
  owner-only on Windows through an ACL, verified by reading the ACL back.
* **Still honest:** on a FAT32/exFAT volume there is no ACL support, so the
  report says `applied: False`. The user is told; nothing is claimed.
* The Windows environment allow-list contains no credential-bearing variable, and
  a regression test asserts no `*_API_KEY` value crosses into a plugin worker.

## 8. Performance implications

Measured in the sandbox (CPython 3.13, Linux):

* Interpreter probe: **one** extra process per interpreter per application run
  (cached), ~25-40 ms on first plugin tool call; zero afterwards. Full plugin
  test module went from ~3.8 s to ~4.1 s.
* Cap counting: one extra `AND status = 'delivered'` on an already-indexed scan
  over a bounded table (500 rows max). No measurable change in `proactive tick`
  timings.
* `icacls` runs once per created artefact (identity file, exported bundle), not on
  a hot path - a Windows-only cost of one short subprocess per export.
* No change to startup, idle CPU/RAM, or the GUI/API paths.

## 9. Limitations

1. The sandbox cannot *prove* a start will work in advance; it starts the
   interpreter once and caches the answer. A host that becomes restricted later
   (policy, antivirus, AppLocker) surfaces the failure on the next call.
2. Interpreter substitution is reported but not configurable; if
   `PC_AGENT_PLUGINS` should pin one interpreter, that is a future setting.
3. `icacls` verification reads the ACL text; a localized Windows build prints
   localized names, so verification only depends on the `(I)` inheritance marker
   and the process exit code - deliberately locale-tolerant, but not a full
   security-descriptor comparison.
4. Filesystems without ACL support degrade to `applied: False` with a reason;
   the identity is still generated and used, with the limitation reported.
5. Tk still has no bidi/complex-script shaping, so Urdu/Arabic *desktop* labels
   remain visually imperfect (the web Command Center renders them correctly).
   Unchanged from Phase 17, restated here so it is not forgotten.

## 10. Next tasks

* Re-run `python -m pytest -q` on Windows and confirm **0 failed**; the 8 skips
  are expected.
* Only **Phase 18 - final production hardening** remains: CI, security and
  performance test suites, signed packaging, update feed and rollback
  verification, and the four documents still missing (`AUDIT_REPORT.md`,
  `DEVELOPER_GUIDE.md`, `INSTALLATION_GUIDE.md`,
  `PLUGIN_DEVELOPMENT_GUIDE.md`).
* After the Windows run, replace `windows_acceptance=…pending_user_windows_evidence`
  with the recorded evidence for Phases 13-17.
