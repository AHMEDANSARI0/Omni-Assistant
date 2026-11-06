# Developer guide

This is the working manual for the tree: how to get it running, how it is laid
out, how to add a capability without breaking a contract, and how to prove the
change before handing it over.

Targets: **Python 3.11+** (the suite is verified on 3.11 and 3.13), Windows 10/11
primary, POSIX supported for development and packaging.

---

## 1. Get a working tree

```
git clone https://github.com/AHMEDANSARI0/Omni-Assistant.git
cd Omni-Assistant
python -m venv .venv
```

Windows:

```
.\.venv\Scripts\Activate.ps1
```

POSIX:

```
. .venv/bin/activate
```

Then, once per checkout:

```
python -m pip install -e ".[dev]"
python -m pytest -q
```

`-e` matters: the tests import `pc_agent` from `src/`, and the `pc-agent`
console script is generated from `pyproject.toml`.

## 2. The five commands that define "green"

```
python -m ruff check .          # style, dead code, unused imports
python -m pytest -q             # 1002 tests, 1 skipped on a headless host
pc-agent release audit          # 32 read-only readiness checks
pc-agent release production-check   # build > verify > sign > apply > rollback
pc-agent doctor                 # one-line-per-subsystem live state
```

If any of the first four is red, the change is not ready. `doctor` is a
diagnostic, not a gate.

## 3. Layout

```
src/pc_agent/
  config.py            Settings (slots dataclass), env parsing, validation
  errors.py            one exception base per failure family
  storage/db.py        schema + migrations (currently v18)
  storage/repositories.py  typed accessors, no SQL in feature code
  safety.py            SafetyKernel, hard timeouts, EmergencyStop
  policies/policy.py   Action enum + PolicyEngine (approvals)
  tools/               ToolSpec/ToolRegistry + builtin tool families
  missions.py          Mission Center: dependencies, scheduler, graph
  orchestrator.py      plan -> execute -> verify loop
  agents.py            multi-agent runtime, depth-1 delegation
  providers/           provider adapters + capability-aware router
  knowledge.py, embeddings.py   memory v2 / RAG
  voice/               capture, STT/TTS engines, privacy gates
  flows.py             visual workflow definitions -> missions
  connectors/          calendar/drive/email/chat/webhook (all gated)
  proactive.py         reminders, notifications, briefings
  plugins/             manifest, sandbox, worker, service
  sync/                crypto, perms, service, store
  i18n/, a11y.py, ui_prefs.py   languages, WCAG maths, UI persistence
  gui/                 Tk dashboard (20 tabs) + controller
  api_server.py, web_ui.py     local service + web Command Center
  release.py, updates.py, production_check.py   packaging, updates, chain check
tests/                 1002 tests, phase-by-phase acceptance + security/perf suites
docs/                  per-phase reports (the "why", including limitations)
```

Rules that keep this readable:

* **Feature code never writes SQL.** It calls a repository or store method.
* **One `Settings` object.** No module reads `os.environ` for product values;
  everything goes through `config.py`, which validates and fails loudly.
* **No network in tests.** Providers are local or mocked by name; URL resolution
  takes an injectable resolver.
* **Nothing silent.** A refusal raises `PcAgentError` with a sentence that says
  what was refused and why.

## 4. Adding a capability

1. **Model it first.** If it needs storage, add a migration in `storage/db.py`
   and bump `LATEST_SCHEMA_VERSION`; migrations are append-only.
2. **Gate it.** New side effects start **off** by default and get a `PC_AGENT_*`
   setting parsed and validated in `config.py`.
3. **Bound it.** Every loop, subprocess, download or file write needs a limit
   (time, count, bytes) that the user can see.
4. **Expose it once per surface.** A capability usually needs four wirings:
   service method, CLI command, HTTP route, dashboard control — each calling the
   same service method, none reimplementing the logic.
5. **Record it.** Anything consequential writes an event row so
   `pc-agent events list` explains what happened.
6. **Prove it.** A test that fails when the capability is removed, plus an
   honest limitation list.

## 5. Test conventions

* Real code paths: no mocks for our own modules. Two exceptions are documented
  where they are used — a URL resolver (SSRF determinism) and a local fake
  provider (no external API).
* Names say what is proven, e.g. `test_unsigned_refused`,
  `test_private_network_targets_are_refused`.
* Platform-conditional behaviour is asserted **through the mechanism the platform
  has** (POSIX mode vs Windows ACL), never skipped and never faked.
* Timing tests assert generous ceilings (`tests/test_performance_suite.py`) so a
  slow shared runner does not fail a correct build.
* Every phase ships a `tests/test_transformation_phaseN_*.py` module that also
  asserts the phase identity (version, phase number, schema, tab count, audit
  check count).

## 6. Release and packaging

```
pc-agent release build --output dist/omni-assistant.zip
pc-agent release verify dist/omni-assistant.zip
pc-agent release installer-source --output dist/installer-source
pc-agent release production-check --out dist/production-check.json
```

The bundle is a ZIP with a SHA-256 manifest built from an allow-list
(`release.py`): `.env`, `data/`, `workspace/` contents, `.git`, `.venv`, caches
and binaries can never enter it. Verification reads the archive **without
extracting**, so inspecting a hostile archive cannot write a file.

Updates:

```
pc-agent update check dist/omni-assistant.zip.sig.json   # optional detached signature
pc-agent update stage dist/omni-assistant.zip
pc-agent update apply latest-staged
pc-agent update rollback latest-applied
```

Signing is Ed25519. The public key goes in `PC_AGENT_RELEASE_PUBLIC_KEY`
(unpadded base64url, 32 bytes) and, with
`PC_AGENT_REQUIRE_SIGNED_UPDATES=1`, an unsigned bundle is refused.

## 7. Debugging

```
pc-agent doctor                     # subsystem lines, live state
pc-agent diagnostics export --out diagnostics.json
pc-agent events list --limit 50     # the audit trail, newest last
pc-agent release audit --json
```

Logs are structured (`logging_config.py`) and never contain secrets: values
passed through `credentials.py` are held in an OS store and printed as
`configured=yes`.

## 8. Definition of done for a change

* `ruff check .` clean
* full suite green, including the phase acceptance tests
* `release audit` has 0 failures
* `release production-check` is READY
* new behaviour is documented in the relevant phase report **and** its
  limitations are listed
* nothing was removed silently: if a feature changed shape, the migration path is
  written down in `CHANGELOG.md`

## 9. Contributing rules that are not negotiable

1. Never weaken a test to make it pass. Fix the product, or assert the
   platform-correct property and document the difference.
2. Never add a control that looks functional but is not wired to real state.
3. Never claim full WCAG compliance, perfect RTL rendering, or "impossible to
   bypass" security. State the mechanism and its limit.
4. Never print, log, commit or bundle a secret.
5. Keep the local-first promise: a fresh install must work offline with no
   account and no paid provider.

## 10. Windows acceptance evidence

`src/pc_agent/windows_acceptance.py` is the only place that may turn a Windows
run into a claim, and it is deliberately boring:

- `run_windows_acceptance(settings, ...)` runs seven gates and returns a frozen
  `AcceptanceReport`. Every gate carries the text it measured. On a non-Windows
  host the default run returns `status="skipped"` with the platform gate failed
  and the rest `not-run`; it never claims a pass.
- `AcceptanceReport.body()` is sealed with
  `baseline.acceptance_body_sha256()` (sorted keys, compact separators). Reader and
  writer share that function, so an edit is visible.
- `verify_acceptance_report(path)` re-checks the hash, the status vocabulary, the
  gate set, the version, and refuses anything that looks like key material.
- `record_acceptance_report()` is the only path to `data/windows-acceptance.json`,
  and it requires `status="passed"` **and** `platform="windows"`.

Rules for extending it: add a gate to `REQUIRED_GATES` *and* to the runner in the
same commit (a report missing a required gate is rejected by the verifier); keep
gate details free of paths that identify the user's documents; never add a flag
that lets a non-Windows host produce a `passed` report.

CLI: `pc-agent release evidence` (status / run on Windows), `... verify PATH`,
`... record [PATH]`, `--self-test`, `--no-suite`, `--out PATH`, `--json`.

