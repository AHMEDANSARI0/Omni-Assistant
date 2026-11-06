# Transformation Phase 18 — Final production hardening (0.80.1)

The last of the eighteen phases. Nothing here adds a new user-facing capability;
this phase makes the previous seventeen **provable**: continuous integration on
both platforms, an end-to-end release chain that is executed rather than
described, security and performance suites with real assertions, and the four
documents the product was still missing.

---

## 1. What already existed before this phase

* Phases 1–17 delivered: safety kernel, local service/API, mission center,
  JARVIS dashboard (19 tabs), live monitoring, provider platform, memory/RAG,
  multi-agent runtime, voice, browser/desktop automation, visual workflows,
  connectors, proactive intelligence, plugin ecosystem, encrypted sync, and the
  accessibility/multilingual layer.
* Packaging already existed and worked: portable ZIP with a SHA-256 manifest,
  Inno Setup source, POSIX source, Debian and RPM recipes; signed updates with
  stage/apply/rollback and OS-scheduled background upgrades.
* A 32-check read-only release audit, 993 tests, and 17 phase reports.

## 2. What was missing

1. **No continuous integration at all.** Every gate was manual: nothing stopped a
   regression from being shipped except the operator's discipline.
2. **The release chain was never exercised end to end by a single command.** The
   pieces existed (build, verify, sign, stage, apply, rollback) and each had
   unit-level tests, but nothing proved that *this* tree, today, can be built,
   signed, applied and rolled back.
3. **Security checks were spread across phase tests** with no single suite that
   states the attacker's move and the refusal side by side.
4. **Performance numbers were measured ad hoc** and never asserted, so a
   regression could pass silently.
5. **Four required documents did not exist**: `AUDIT_REPORT.md`,
   `DEVELOPER_GUIDE.md`, `INSTALLATION_GUIDE.md`,
   `PLUGIN_DEVELOPMENT_GUIDE.md`.

## 3. What was built

### 3.1 `src/pc_agent/production_check.py` — the real release chain

`run_production_check(settings)` performs ten operations on real files and
returns a structured report:

| Step | Operation | What it proves |
| --- | --- | --- |
| `release_catalog` | scan the allow-list | no `.env`, `data`, `workspace` content, `.git` or `.venv` can enter a bundle |
| `bundle_build` | `build_release_bundle` | a real portable ZIP is produced; file count matches the allow-list |
| `bundle_verify` | `verify_release_bundle` | the archive verifies without being extracted; hashes agree |
| `tamper_detection` | corrupt one payload entry | verification **refuses** a modified bundle |
| `signature_verify` | sign with an ephemeral Ed25519 key, then `check_update` | the detached signature verifies against a configured public key |
| `unsigned_refused` | stage a bundle with no sidecar while signing is required | the signature policy is enforced, and for the right reason |
| `stage` | `stage_update` | files are staged into a unique update folder |
| `apply` | `apply_staged_update` against a copy | the applied tree equals the release tree byte for byte |
| `rollback` | `rollback_update` | the pre-apply tree is restored byte for byte |
| `cleanup` | remove the work directory | the self-test key, bundles and copies are gone |

Honesty rules baked into the module:

* the signing key is **ephemeral** and deleted; the report says
  `ephemeral self-test key (no production key is used)`;
* the update is applied to a **copy** inside the work directory, never to the
  running install;
* "same version" re-installs are explicit (`allow_downgrade=True`) so the only
  policy that may refuse the unsigned case is the signature rule, and a refusal
  for any other reason is recorded as a **failure** of that step;
* an unexpected exception becomes an `aborted` failure row — the chain can never
  stop silently;
* the JSON report lands in `data/production-check.json`.

### 3.2 Audit check 32 — `production_hardening`

A status check (fast, read-only) that reports CI coverage and the *last recorded
end-to-end result*, and distinguishes the three states that matter:

* no report → `pass` with "no recorded end-to-end run yet";
* report `ok` → `pass` with the timestamp and step count;
* report failed → **`fail`**; report unreadable → `warn`.

So "we never ran it" can never be mistaken for "it passed".

### 3.3 `.github/workflows/ci.yml`

* `test`: matrix `ubuntu-latest` × `windows-latest` and Python `3.11` × `3.13`,
  each running `ruff check`, the full suite, `release audit` and
  `release production-check`, uploading the chain report.
* `accessibility`: `a11y check`, `i18n status`, `perf bench` — the measured
  numbers are printed on every run.
* `package`: on `v*` tags, build and verify the portable ZIP plus the Inno source.
* `permissions: contents: read`, no `secrets.` reference, `fail-fast: false`.

### 3.4 `tests/test_security_suite.py`

19 checks that state the attack and the refusal: private-network and
DNS-rebinding SSRF, non-web schemes, credential-bearing URLs, shell
metacharacters in commands, non-allow-listed and path-traversal executables,
archive traversal, secret leakage into diagnostics/audit/reports, emergency stop
behaviour, and tool-schema pollution.

### 3.5 `tests/test_performance_suite.py`

9 measured checks with documented ceilings: settings load, `import pc_agent.cli`,
`health()`, `system_status()`, event listing, `a11y_status()`, heap growth over
200 status calls, database initialisation, and the release-candidate scan. The
same numbers are recorded in `PERFORMANCE_REPORT.md`, so a budget cannot move
without the report moving.

### 3.6 Surface wiring

* `pc-agent release production-check [--out PATH] [--keep] [--json]`
* `pc-agent doctor` gains a `Hardening:` line (CI coverage, last chain result,
  unsigned-update policy, feed state)
* a **20th dashboard tab, "Production"**, with a button that runs the *real*
  chain check and renders the stored report, and a refresh that shows the last
  result without re-running it
* `diagnostics.production_summary_line()` for the same state in machine form

### 3.7 The four missing documents

`AUDIT_REPORT.md` (measured posture + what is not proven),
`DEVELOPER_GUIDE.md` (layout, conventions, definition of done),
`INSTALLATION_GUIDE.md` (install, gates, verify, uninstall, troubleshooting),
`PLUGIN_DEVELOPMENT_GUIDE.md` (bundle format, capabilities, what the sandbox does
*not* do).

## 4. Files added / changed

**Added**

* `src/pc_agent/production_check.py`
* `.github/workflows/ci.yml`
* `tests/test_transformation_phase18_hardening.py` (24 tests)
* `tests/test_security_suite.py` (19 tests)
* `tests/test_performance_suite.py` (9 tests)
* `AUDIT_REPORT.md`, `DEVELOPER_GUIDE.md`, `INSTALLATION_GUIDE.md`,
  `PLUGIN_DEVELOPMENT_GUIDE.md`, `docs/transformation_phase18.md`

**Changed**

* `src/pc_agent/release.py` — check 32 (`production_hardening`)
* `src/pc_agent/cli.py` — `release production-check`, `Hardening:` doctor line
* `src/pc_agent/diagnostics.py` — `production_summary_line`
* `src/pc_agent/gui/app.py`, `gui/controller.py` — 20th tab + real handler
* `src/pc_agent/baseline.py` — phase 18 label
* version stamps: `pyproject.toml`, `__init__.py`, 69 test modules, README and
  the report trio
* existing tests updated for the honest counters that moved: audit checks 31→32,
  notebook tabs 19→20, phase identity 17→18

## 5. Deliberately not included

* **No schema change** — still v18, no migration.
* **No new permission or gate.** Hardening added no capability.
* **No code-signing certificate.** The pipeline builds and verifies an unsigned
  Inno source plus a detached Ed25519 signature for the update bundle; a
  Windows Authenticode certificate is a user-supplied artifact.
* **No macOS/Linux desktop packaging** — out of scope by the standing platform
  decision.
* **No fake CI badge or claimed remote run.** The workflow has never executed on
  GitHub; that is stated in the audit report instead of implied.

## 6. Tests and results

| Run | Result |
| --- | --- |
| `python -m pytest -q` (headless, `TZ=UTC`) | **1076 passed, 1 skipped** |
| same, `TZ=Asia/Karachi` and `TZ=America/New_York` | **1076 passed, 1 skipped** |
| `ruff check .` | clean |
| `pc-agent release audit` | `READY; failures=0; warnings=1; checks=33` |
| `pc-agent release production-check` | `READY; steps=10; passed=10; failures=0` (~1.7 s) |
| Clean-room patcher runs (Phase 9 base / GitHub 0.60.0 tree / current tree) | byte-identical to staging, suite green, audit READY, second run idempotent, `.env` untouched |

The single skip is the real-Tk-window test (no desktop session on a headless
host). On Windows the eight platform-oracle skips stay skipped by design.

## 7. Security implications

* The chain check **proves** tamper detection and the signature policy instead of
  trusting them: a modified bundle and an unsigned bundle are both refused in
  the run, and the refusal reason is asserted.
* No production key material is used or written; the self-test key dies with the
  work directory, and a test asserts the report never contains it.
* CI runs with `contents: read` and no secrets, so a compromised fork cannot
  exfiltrate credentials from this pipeline.
* The security suite makes previously implicit refusals executable, which means a
  regression that removes one is caught by name.
* Unchanged and re-verified: approvals, emergency stop, capability roots,
  `-I -B` sandbox with audit hook and job object, owner-only sync files,
  allow-listed release contents.

## 8. Performance implications

* Full chain check: **~1.7 s** wall clock (build 181 ms, verify 34 ms, tamper
  149 ms, sign 62 ms, stage 141 ms, apply 274 ms, rollback 235 ms).
* Audit check 32 adds one small file read and one JSON read: no measurable
  change to `release audit` (≈0.6 s end to end).
* The 20th tab costs no idle work: it loads the stored report on demand and runs
  the chain only when the button is pressed.
* Suite runtime is ~160 s locally; the 28 new tests add ~9 s.

## 9. Limitations

1. The chain check verifies the **local** transport (bundle + detached
   signature). It does not fetch a live feed; `update hosting-check` covers that
   separately and needs network and a hosted key.
2. It re-installs the **same** version, so the version-comparison branch of the
   policy is bypassed deliberately (`allow_downgrade=True`) — that branch has its
   own unit tests.
3. Rollback is verified against a **copy** of the tree; a real user edit made
   between apply and rollback is covered by "do not overwrite later user edits"
   logic which is tested separately, not by this chain.
4. CI is validated structurally and by running its exact commands locally; a
   remote GitHub run is external evidence still to be produced.
5. Performance ceilings are calibrated on a 2-vCPU container with ~10× headroom;
   they catch order-of-magnitude regressions, not micro-regressions.

## 10. Next tasks

* Windows acceptance run for 0.80.1, then record it and clear the
  `transformation_baseline` warning.
* First real GitHub Actions execution; paste the run URL into the audit report.
* Optional, user-supplied: Authenticode signing for the Windows installer and a
  hosted update feed with real key rotation.
* Future scope (not promised): macOS/Linux desktop packaging, SaaS readiness,
  plugin marketplace — all currently architectural readiness only.

**Phase balance at the end of this phase: 18 of 18 delivered. No phase remains.**
