# Transformation Phase 15 — Plugin Ecosystem (0.77.0)

A signed, versioned plugin and skill ecosystem: strict manifests, Ed25519
publisher signatures, checksum-verified bundles, capability declarations, and a
real OS-level sandbox for every plugin tool call. Windows-first, local-first,
off by default.

- Version: **0.77.0**
- Database schema: **v17** (`plugins`, `plugin_versions`, `plugin_runs`,
  `plugin_state`)
- Release audit checks: **29** (`plugin_ecosystem` is the new one)
- Suite: **887 passed, 1 skipped**
- New tests: `tests/test_transformation_phase15_plugins.py` (36 tests)

---

## 1. What already existed

- Phase 2 Safety Kernel with an isolated worker process for built-in tools
  (`safety_worker.py`), hard timeouts, budgets, approvals and emergency stop.
- Phase 3 `LocalService` facade, event log, structured logging, settings/profiles.
- Phase 12 flows: strict document validation, versioning, checksummed
  import/export — the pattern this phase reuses for bundles.
- Phase 13 connectors and Phase 14 proactive engine: gate → validate → act →
  ledger, with every refusal carrying an actionable reason.
- `updates.py` / `release_admin.py`: Ed25519 signing and verification already in
  the product (`cryptography` is a direct dependency).

## 2. What was missing

1. **No third-party extension point at all.** Every tool in the product was
   shipped by us; a user could not add one without editing the source tree.
2. **No trusted-publisher concept.** Nothing in the product could answer "who
   signed this code, and do I trust that key?".
3. **No sandbox for foreign code.** The existing worker isolates *our* tools; it
   was never designed to run code we did not write.
4. **No capability vocabulary**, so there was no way to say "this plugin may
   read the workspace but not the network" and enforce it.
5. **No install/rollback/uninstall story**, so a bad plugin could not be removed
   cleanly and an upgrade could not be undone.

## 3. What changed in this phase

### 3.1 New package `src/pc_agent/plugins/`

| Module | Responsibility |
|---|---|
| `manifest.py` | strict manifest contract, canonical JSON, content checksum, Ed25519 verification |
| `sandbox.py` | OS limits (POSIX `setrlimit` / Windows Job Object), isolated interpreter, scrubbed env, hard timeout |
| `worker.py` | the child process: audit hook, single declared tool call, JSON-only protocol |
| `store.py` | schema v17 registry, version history, run ledger, small state |
| `service.py` | the only gate keeper: verify → install → enable → run → rollback → uninstall |

### 3.2 Manifest contract (everything is a refusal point)

```json
{
  "schema": 1,
  "id": "com.example.wordcount",
  "name": "Word Count",
  "version": "1.0.0",
  "description": "Counts words in a string.",
  "publisher": "Example Ltd",
  "license": "MIT",
  "entry": "plugin.py",
  "capabilities": ["read_workspace"],
  "tools": [{"name": "count_words", "description": "…", "arguments": {"text": "string"}}],
  "skills": ["skills/howto.md"],
  "files": ["plugin.py", "skills/howto.md"],
  "checksum": "<sha256 of the content map>"
}
```

- Unknown fields, bad slug ids, non-semver versions, unknown capabilities,
  duplicate tools, oversized text and traversal paths are **refused**, not
  ignored.
- `files` must match the archive exactly; `checksum` must match the content
  digests (this is what catches a bundle modified after signing).
- The detached signature (`manifest.sig.json`) covers **the exact manifest
  bytes**, and names the content checksum, so code and publisher are bound
  together: `{algorithm: Ed25519, key_id, signature, payload_sha256, checksum}`.

### 3.3 Capabilities

| Capability | Granted today | Enforced by |
|---|---|---|
| `read_workspace` | yes | audit hook (write denial outside granted roots) + documentation of scope |
| `write_workspace` | yes | audit hook: writes outside workspace/plugin/sandbox dirs raise `PermissionError` |
| `read_plugin_files` | yes | plugin dir is the only import root |
| `network` | **never** (refused at install) | audit hook denies every `socket.*` event; proxies pointed at a dead port |
| `subprocess` | **never** (refused at install) | audit hook denies `subprocess.*`, `os.system`, `os.exec*`, `os.spawn*`, `ctypes.*` |

An install that declares `network` or `subprocess` fails with
`Plugin requests capability 'network', which this build cannot grant`. That is
deliberate: silently dropping a declared capability would be a lie.

### 3.4 The sandbox (two independent layers)

1. **OS limits** — POSIX: `RLIMIT_AS` (default 512 MB), `RLIMIT_CPU`,
   `RLIMIT_FSIZE`, `RLIMIT_NOFILE` 64, `RLIMIT_NPROC` 1, plus a new session so
   the whole process group can be killed. Windows: a Job Object with
   `PROCESS_MEMORY`, `ACTIVE_PROCESS=1` and `KILL_ON_JOB_CLOSE`, created with
   `ctypes` and reported honestly as best-effort.
2. **Interpreter-level denial** — the worker runs `python -I -B` in a fresh run
   directory with a scrubbed environment (no provider keys, no `.env`, no
   proxies), and installs a `sys.addaudithook` hook that refuses sockets,
   subprocesses, `os.system`/`os.exec*`/`os.spawn*`, `ctypes` loading and writes
   outside the capability roots.

`pc-agent plugins status` prints the mechanism this host actually applies
(`setrlimit + isolated-interpreter + audit-hook` on Linux,
`job-object + …` on Windows) instead of assuming one.

Hard timeout (default 30 s, 1–600 s) kills the group and records the run as
`timeout`. A plugin that prints to stdout cannot corrupt the protocol: the
worker moves the protocol channel aside before running plugin code.

### 3.5 Execution gate

A plugin tool call needs **all** of:

1. `PC_AGENT_ALLOW_PLUGINS=1`;
2. the plugin installed **and** `enabled`;
3. the tool declared in the manifest;
4. the caller to pass `confirm="<plugin id>"` (CLI `--confirm`, API `confirm`,
   GUI/web button, both of which set it explicitly);
5. the sandbox.

Failures are recorded in `plugin_runs` with a reason; nothing is retried
automatically.

### 3.6 Configuration

| Key | Default | Range / note |
|---|---|---|
| `PC_AGENT_ALLOW_PLUGINS` | **off** | master gate |
| `PC_AGENT_PLUGIN_ALLOW_UNSIGNED` | off | unsigned bundles refused unless set |
| `PC_AGENT_PLUGIN_TRUSTED_KEYS` | empty | JSON `{key_id: base64-or-hex 32 bytes}`, or a path to such a JSON file; validated at load |
| `PC_AGENT_PLUGIN_TIMEOUT_SECONDS` | 30 | 1–600 |
| `PC_AGENT_PLUGIN_MEMORY_LIMIT_MB` | 512 | 32–4096 |
| `PC_AGENT_PLUGIN_CPU_LIMIT_SECONDS` | 30 | 1–600 |
| `PC_AGENT_PLUGIN_MAX_BUNDLE_BYTES` | 8 000 000 | 1 MB–64 MB |
| `PC_AGENT_PLUGIN_MAX_FILES` | 200 | 1–1000 |
| `PC_AGENT_PLUGIN_MAX_FILE_BYTES` | 2 000 000 | 1 KiB–16 MB |

Malformed trust stores, unknown keys and out-of-range numbers are refused when
settings load, not coerced.

### 3.7 Surfaces

- CLI: `pc-agent plugins status|list|show|verify|install|enable|disable|rollback|remove|run|skills|runs`
  (`run --arg name=json --confirm <plugin id>`).
- API: `GET /v1/plugins/status`, `GET /v1/plugins`, `POST /v1/plugins/verify|install`,
  `GET /v1/plugins/runs`, `GET /v1/plugins/{id}`, `GET /v1/plugins/{id}/skills`,
  `POST /v1/plugins/{id}/run|enabled|rollback`, `DELETE /v1/plugins/{id}`
  (12 endpoints).
- GUI: 17th notebook tab **Plugins** (verify, install, enable/disable, run,
  rollback, remove, skills, run ledger).
- Web dashboard: **Plugins** panel driving the same routes.
- `pc-agent doctor`: `Plugins: installed=… enabled=… runs=…; signatures=…
  keys=…; sandbox=… timeout=…s`.
- Diagnostics: a `plugins` section with sandbox limits, trusted key ids and counts.
- Release audit: check **29** `plugin_ecosystem` (passes while off, warns once
  enabled).

## 4. Files

New:
- `src/pc_agent/plugins/__init__.py`, `manifest.py`, `sandbox.py`, `worker.py`,
  `store.py`, `service.py`
- `tests/test_transformation_phase15_plugins.py`
- `docs/transformation_phase15.md`

Modified:
- `src/pc_agent/storage/db.py` (schema v17, 4 tables, 5 indexes)
- `config.py`, `errors.py` (`PluginError`), `service.py`, `api_server.py`,
  `cli.py`, `diagnostics.py`, `release.py`, `baseline.py`, `gui/app.py`,
  `gui/controller.py`, `web_ui.py`, `__init__.py`, `pyproject.toml`
- `CHANGELOG.md`, `TEST_REPORT.md`, `FEATURE_MATRIX.md`, `ARCHITECTURE.md`,
  `API_DOCUMENTATION.md`, `IMPLEMENTATION_ROADMAP.md`, `SECURITY_REPORT.md`,
  `PERFORMANCE_REPORT.md`, `README.md`
- 69 test files (identity/version/schema/GUI-tab/release-check sweep to 0.77.0)

## 5. Tests and results

36 tests in `tests/test_transformation_phase15_plugins.py`, using **real**
Ed25519 keys, **real** zip bundles, **real** subprocesses — no mocked verifier
and no mocked sandbox:

| Area | Claim |
|---|---|
| identity | 0.77.0, phase 15, schema v17, 17 tabs, 29 release checks |
| schema | v17 creates the four plugin tables |
| manifest | bad ids, versions, unknown fields, unknown capabilities, bad tool names, non-`.md` skills and traversal entries are refused (8 parametrized cases) |
| gating | the service is off by default and the install path refuses before touching disk |
| signatures | signed install verifies; unsigned refused unless opted in; tampered content refused; wrong publisher key refused; missing manifest refused |
| archive safety | `../evil.py` traversal entry refused; symlink entry refused; file-count limit enforced |
| capabilities | `network` refused at install, `subprocess` likewise |
| lifecycle | install → enable → disable → rollback → uninstall, with version history and directory cleanup |
| sandbox | real tool call returns a real result and a `plugin_runs` row; undeclared tool refused; missing confirm refused; wrong argument type refused inside the worker |
| containment | 30 s sleep killed by a 1 s timeout; `socket.create_connection` refused; write to `/tmp` outside the granted roots refused and the file is not created |
| skills | Markdown skills are returned as bounded text and never executed |
| surfaces | CLI (status/verify/install/enable/run/list/runs/remove + bad-bundle exit 1), 12 API routes over a real `LocalApiServer`, GUI controller + web panel markers, diagnostics section, doctor line, release check |
| ledgers | 510 runs collapse to 500; version history capped at 5 |

Full suite: **887 passed, 1 skipped**. `ruff check` clean. Release audit
`READY`, 29 checks, 0 failures, 1 warning.

## 6. Skipped / deliberately not done

- **No marketplace/registry fetch.** Installing from a URL is a network feature
  with its own threat model; v1 installs a local `.zip` the user chose.
- **No native/binary plugins.** Only Python entry files, so the audit hook can
  see the work. (Documented limitation: hand-written native code could bypass
  the audit hook, which is why OS limits are the second layer.)
- No plugin-tools-as-agent-tools yet: a plugin tool is invoked explicitly, never
  selected by the planner. Wiring plugin tools into the AI planner needs prompt
  injection review first.
- No per-plugin network allowlist (network is refused outright).
- No code signing of `plugin.py` separately from the manifest — the checksum
  binds it, which is what the tests verify.

## 7. Security implications

- Third-party code is contained by **two** layers: OS limits (real) and the
  CPython audit hook (real for Python code, bypassable by native extensions).
- A bundle is refused if it is unsigned (by default), signed by an untrusted
  key, modified after signing, contains traversal/symlink entries, declares a
  capability this build cannot grant, or exceeds size/count limits.
- The worker starts from a scrubbed environment, so provider keys, `.env`
  contents and proxy settings never reach plugin code.
- Execution needs a per-call `confirm` token equal to the plugin id, so a
  scheduler, flow or agent cannot silently invoke third-party code.
- Every install, enable, run, rollback and uninstall is an event, and every run
  is a ledger row with status, duration and sandbox mechanism.
- The plugin directory is inside the data dir; nothing is written to the source
  tree or the workspace without a declared capability.
- `.env` is never read, written or bundled; the sentinel check in all three
  clean rooms confirms the patcher does not touch it.

## 8. Performance implications

Measured on the sandbox (Python 3.13.16, warm cache, no network):

| Operation | Median | Max |
|---|---|---|
| `verify_bundle` (zip + checksums + Ed25519) | 0.32 ms | 1.06 ms |
| `install` (extract + ledger) | 3.17 ms | 3.35 ms |
| `status()` | 1.52 ms | 1.67 ms |
| `run_tool` (spawn + limits + worker + result) | 43.7 ms | 59.6 ms |
| `runs(limit=25)` | 0.80 ms | 1.21 ms |

Reading: installation and verification are single-digit milliseconds; the only
expensive operation is starting a process (≈44 ms), which is the price of the
isolation and is paid per call, never in the background. Idle cost is zero —
nothing polls, no watcher, no timer.

## 9. Limitations

- Windows Job Object assignment happens immediately after spawn (a deliberate
  trade-off that avoids a suspended-process dance); the audit hook is already in
  place before plugin code runs.
- The audit hook is a CPython feature: a plugin that ships native code could
  escape the interpreter layer. OS limits still cap memory, CPU, processes and
  wall time.
- `read_workspace` is advisory: reads are not path-confined in this phase, only
  writes. Documented rather than overclaimed.
- Skills are context text for the user/agent; they are not injected into
  prompts automatically.
- No plugin update check; upgrading means installing a newer signed bundle.

## 10. Next tasks

- **Phase 16** — optional encrypted cloud/device sync and secure remote
  coordination.
- Carried forward: Windows evidence for 0.77.0, `processes=8 (<source>)`
  confirmation, mic `input=0` after Windows privacy settings.
- (CHOTA) plugin-tools-as-agent-tools behind a prompt-injection review;
  (MEDIUM) a plugin registry with signed index; (BARA) Phases 17–18.
