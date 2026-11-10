# Security Report

## Scope

This report summarizes the Phase 0 audit at GitHub commit
`89610f3d18f9348b6fe8ead679bab81ee34155df`, reconciled with the delivered
Phase 60 implementation baseline. It is not a penetration test and does not
claim absolute security.

## Controls already present

- Deterministic planning before AI planning; local validation of every
  model-proposed plan.
- Workspace path policy and traversal/symlink defenses.
- Default-off subprocess, browser, desktop-automation, vision, self-repair
  and online-update gates.
- Exact approvals for designated external, command, Git-write, browser,
  desktop and vision actions.
- OS credential stores on Windows, Linux and macOS; credential values are
  not printed by status commands.
- Bounded web, provider, document, PDF, image and dynamic-tool inputs.
- Signed release/update verification, downgrade refusal and rollback paths.
- Dynamic pure-compute tools require static validation, restricted
  subprocess tests, approval, versioning and rollback.
- Transformation Phase 2 Safety Kernel: hard tool deadlines, isolated
  worker subprocess for data-context tools, global emergency stop,
  digest-bound single-use Approval Center decisions for non-interactive
  runs, approval audit records without raw arguments, and optional
  passphrase-encrypted database backups (PBKDF2-HMAC-SHA256 + Fernet).

## Transformation Phase 3 notes (0.65.0)

- The local API binds loopback only, requires a bearer token except for
  the minimal `/v1/health` route, compares tokens in constant time, caps
  request bodies at 256 KiB, and never returns raw exception text. The
  auto-generated token lives in `data/api-token` with owner-only
  permissions where the platform supports them.
- API task runs cannot approve tools inline; consent still flows through
  the Approval Center with digest-bound consume-once semantics. The
  emergency stop file is shared across CLI/GUI/API.
- Settings profiles accept only a whitelist of non-secret, non-gate
  fields; unknown fields (including capability gates or API keys) are
  rejected at save and at load, so a copied-in profile cannot raise
  privileges. Structured logs pass messages and exceptions through the
  same redaction as text logs.

## Transformation Phase 4 notes (0.66.0)

- Parallel mission branches each run through their own orchestrator and
  Safety Kernel; `mission_max_parallel` is capped at 8 so a mission
  cannot fan out unbounded worker processes. Mission/schedule inputs
  create ordinary tasks — no new tool authority is introduced, and
  approvals still cannot be granted inline (API or mission runs).
- Schedules execute only while the API process runs or when a user runs
  `schedules tick`; nothing is registered with the OS scheduler, so
  there is no hidden persistence mechanism.

## Transformation Phase 5 notes (0.67.0)

- The Command Center page is served only on the same loopback-only API
  listener, with CSP (`frame-ancestors 'none'`, self-only connections)
  and anti-framing headers. All data endpoints still require the
  bearer token; the token is pasted by the user and kept in
  `sessionStorage` only — never in a URL, cookie, or server state.
  All rendered API text is escaped in the page script.

## Transformation Phase 6 notes (0.68.0)

- System monitoring is read-only and local: `/proc`, documented Win32
  query APIs, and `nvidia-smi` discovery via `shutil.which` with a fixed
  argv (no user input reaches the command line). Process enumeration on
  Windows uses query-limited access and skips protected processes
  instead of requesting elevation. `/v1/system` sits behind the same
  bearer authentication as every other data endpoint; telemetry is never
  persisted or transmitted.

## Transformation Phase 7 notes (0.69.0)

- Streaming and evaluation calls are ordinary provider completions:
  they receive no tool objects, filesystem access, or approval
  authority, and task planning still validates complete JSON before
  any tool runs. `/v1/providers/stream` is behind the same bearer
  authentication, with prompt/token bounds. Usage analytics store no
  prompt or response text (provider/model, token counts, latency,
  redacted errors only), stay on local disk, and can be disabled.
  Prices/context sizes come only from explicit user configuration —
  the platform never invents cost data.

## Transformation Phase 8 notes (0.70.0)

- Knowledge ingestion is always explicit; nothing is indexed in the
  background and OCR is never run silently. API/GUI file ingestion is
  confined to the workspace; the CLI keeps its established
  explicit-path resolver. File, text, and chunk counts are bounded.
- Ingested document text is treated as untrusted data in grounded
  answers: the RAG prompt labels sources as data (never
  instructions), answers must cite them, and a no-source question
  makes no provider call at all. Agent tools can ingest and search
  but deliberately cannot ask (no provider call is tool-reachable).
- Embeddings stay on-device by default (offline hashing embedder,
  vectors in local SQLite). A user-configured remote embedding
  backend necessarily receives chunk text, exactly as a normal
  completion would; that choice is explicit configuration, never
  automatic. Cross-model vector comparison is impossible by
  construction (same-tag-only).

## Transformation Phase 10 notes (0.72.0)

Voice adds an input device, so the privacy contract is enforced in code, not in
copy:

- **Microphone off by default**, persisted as `voice.enabled`. Turning it on is
  an explicit user action on every surface (CLI/API/GUI/web), and every change
  writes an audit event (`voice.microphone_enabled`/`_disabled`).
- **Wake-word listening is a second, separate opt-in** (`voice.wake_word_enabled`)
  and is refused while the microphone is off. The release audit's 24th check
  (`voice_privacy`) **warns** when continuous listening is enabled, because the
  microphone then opens without a key press.
- **Cloud STT is doubly gated:** `PC_AGENT_VOICE_CLOUD_STT=1` *and* a per-call
  `cloud_consent` flag. Without both, `SpeechToText.transcribe` raises before any
  bytes are read, so audio cannot leak to a provider by accident.
- **Audio is discarded right after transcription** unless
  `PC_AGENT_VOICE_RETAIN_AUDIO=1`; retained recordings live under
  `data/voice/retained/` and are removed by
  `pc-agent voice privacy --purge-audio`.
- **Transcripts are opt-in** (`PC_AGENT_VOICE_STORE_TRANSCRIPTS=1`) and pass
  through `redact_text` before being stored. Events record counts, not content.
- **Bounded capture:** `PC_AGENT_VOICE_MAX_SECONDS` (1–300, default 60) sizes the
  buffer and caps the recording; only one capture can exist at a time.
- **Injection-resistant TTS:** text is delivered to PowerShell via a temp UTF-8
  file and to espeak via stdin — never concatenated into a command line, so
  quotes, `$( )` and backticks stay data. The temp file is deleted in `finally`.
- **No new tool, no new privilege:** voice is a user-initiated surface. The tool
  registry stays at 64, so an agent cannot open the microphone, and the Safety
  Kernel / Approval Center are untouched.
- **Emergency stop integration:** an engaged stop forces the microphone to
  `disabled` and refuses both capture and speech output.
- **Windows crash-proofing:** the monitoring fix resolves Win32 process-memory
  entry points with explicit ctypes signatures (no truncated 64-bit handles) and
  fails soft to `None` with the method recorded, instead of guessing a number.

Residual risks: any locally installed STT/TTS binary runs with the user's
privileges (no OS-level sandbox yet — Phase 15 scope); Windows System.Speech
dictation accuracy is modest; wake-word detection is not implemented, so the
"always listening" risk does not exist yet, and the flag is reported honestly.

## Transformation Phase 9 notes (0.71.0)

- Specialist agents get strictly less power than a normal task, never
  more: each run's orchestrator is built with a filtered registry of
  the agent's builtin allow-list, so planning, the Safety Kernel, and
  verification physically cannot reach other tools. Dynamic tools are
  excluded from agent registries entirely.
- Delegation is depth-1 and user/coordinator-invoked only; no tool
  can create, run, or widen an agent. The coordinator's JSON is
  validated like any AI plan (known enabled agents, earlier-only
  dependencies, size caps) and the team goal is untrusted data.
- Team branches execute as ordinary tasks: identical approval,
  budget, timeout, and emergency-stop rules. Parallel branches never
  race an interactive prompt; they rely on the Approval Center.

## Scanner and history evidence

- Bandit on the Phase 58 audit tree: 95 findings (77 Low, 16 Medium, 2
  High), manually triaged. The High SHA-1 findings are standards-required
  WebSocket and PDF LTV identifiers. The dynamic-tool `exec` boundary,
  XML parsing and update-host network exposure remain review priorities.
- `pip-audit --local` found no known vulnerabilities in auditable installed
  packages at audit time; the local project itself was skipped because it
  is not on PyPI.
- Transformation Phase 1 performed a non-shallow GitHub history scan: 9
  commits, 533 reachable blobs and 11,420,515 blob bytes. High-confidence
  AWS, GitHub, OpenAI-style, Google and Slack token patterns had zero hits.
  The only private-key-header hit is source code that rejects private keys
  from an update-hosting package. This pattern scan is not an entropy scan
  and cannot prove that no low-pattern secret was ever present.

## Residual risks

1. Worker-eligible tools are hard-killed at their deadline since Phase 2,
   but browser/desktop/vision tools still run on an in-process thread:
   Python cannot force-kill a thread, so a hung handler's result is
   discarded while the thread may finish in the background. The worker is
   a user-privilege process, not an OS sandbox (Phase 15).
2. SQLite data, memory and task history are plaintext to the local OS
   account; backups are plaintext unless created with `--encrypt`, and
   the automatic pre-restore safety backup is unencrypted.
3. Dynamic tools use restricted subprocess isolation, not a complete OS
   sandbox or CPU/RAM quota system.
4. Approved commands and Git operations run with user privileges.
5. A user-launched browser DevTools port is a powerful local control
   surface while open.
6. Desktop screenshots and provider vision/OCR can expose sensitive content;
   approvals and data minimization must be preserved.
7. Web fetch is SSRF-reduced, not a pinned-IP client; DNS rebinding remains
   a test priority.
8. The vendor update-feed serving command can bind beyond loopback and must
   be deployed only with deliberate TLS/firewall decisions.
9. Vendor license/release admin entry points ship in normal packaging even
   though private keys must remain off customer machines.
10. Native Windows security behavior requires exact-build Windows evidence.

## Security rule for later phases

No later phase may silently weaken approvals, path policy, credential
storage, signed updates, provider/data boundaries or audit records. New
network listeners, connectors, plugins, voice capture and cloud features
require explicit threat models and tests before enablement.

## Transformation Phase 15 notes (0.77.0)

Plugins are the first code in the product that we did not write, so this is the
strictest threat model in the transformation so far.

| Threat | Control |
| --- | --- |
| Malicious or tampered bundle | Content checksum bound into the signed manifest; every file's SHA-256 is recomputed at install and any mismatch is refused |
| Impersonating a publisher | Ed25519 signature over the exact manifest bytes; only key ids present in `PC_AGENT_PLUGIN_TRUSTED_KEYS` are accepted, and the signature must also name the content checksum |
| Accidental unsigned install | Unsigned bundles are refused unless `PC_AGENT_PLUGIN_ALLOW_UNSIGNED=1`, and the install row records `signature_status=unsigned` |
| Zip-slip / path traversal | Every entry goes through `safe_relative_path`; absolute paths, backslashes and `..` segments are refused, symlink entries are refused, extraction happens into a fresh staging dir that is then moved |
| Zip bomb / resource exhaustion | Bundle ≤ 8 MB, ≤ 200 entries, ≤ 2 MB per entry; the extracted tree is bounded by the same checks |
| Privilege escalation through declared capabilities | `network` and `subprocess` are refused at install; `ctypes` loading, `os.system`, `os.exec*`, `os.spawn*` are denied inside the worker |
| Data exfiltration | No sockets (audit hook + dead proxies), no subprocesses, no writes outside the granted roots, no `.env`, keys or provider secrets in the worker environment |
| Hung or crashing plugin | Hard timeout (default 30 s) kills the whole process group / Job Object; the run is recorded as `timeout` and nothing is retried |
| Memory/CPU abuse | `RLIMIT_AS` 512 MB (Windows: `PROCESS_MEMORY`), `RLIMIT_CPU` 30 s, `RLIMIT_FSIZE` 32 MB, `RLIMIT_NOFILE` 64, `RLIMIT_NPROC`/`ACTIVE_PROCESS` 1 |
| Silent third-party execution | `confirm` must equal the plugin id; the tool must be declared; the plugin must be enabled; the master gate must be on |
| Ledger growth as an attack surface | `plugin_runs` capped at 500 rows, versions capped at 5 per plugin, detail strings truncated |
| Secret leakage into reports | Doctor and diagnostics print counts, mechanisms and key **ids** only - never a key value, never a path inside the bundle |

Release-audit check **29** (`plugin_ecosystem`) reports the gate state, the
trusted key count, the sandbox mechanism actually available on the host and the
limits in force. It is a structural check and a warning once plugins are on; it
is not a claim that any plugin is safe.

Honest limitation, stated in the docs and the check detail: the audit hook is a
CPython mechanism, so hand-written native code inside a plugin could bypass it.
The OS limits remain in force in that case, and native plugins are not accepted
by this build.

## Transformation Phase 14 notes (0.76.0)

Proactive intelligence adds a timer, a policy engine and one optional outbound
channel. The threat model is therefore about *unsolicited* behaviour and about
*volume*, not about new privileged operations.

| Threat | Control |
| --- | --- |
| Unwanted outbound traffic on a fresh install | `PC_AGENT_ALLOW_PROACTIVE_OUTBOUND` defaults to **off**; with it off the engine still records the notification in-app and reports the deferral reason |
| Exfiltration through a notification body | Outbound re-uses the Phase 13 chat/webhook path - SSRF-validated endpoint, redirects refused, HMAC-signed body, no credentials in the URL - so a notification cannot become a new egress channel |
| Notification flooding (log/storage growth, user annoyance) | Rolling hourly cap (default 20, hard maximum 500), per-key cooldown (default 300 s) and a bounded ledger (500 notifications, 200 briefings) trimmed in the same transaction |
| Interrupting the user at night | Quiet hours are wraparound-aware (`22:00-07:00`); only `critical` severity may cross them, and only when `PC_AGENT_QUIET_HOURS_ALLOW_CRITICAL` is explicitly enabled |
| Silent data loss when delivery is refused | The in-app copy and its event are written *before* any outbound attempt; an exception in the outbound path is recorded as a deferral, never as success |
| Injection via briefing content | Every section value is sanitised and length-bounded (`MAX_TITLE` 120, `MAX_BODY` 1 000) before storage; connector/flow data is displayed as data and never executed as instructions |
| Misfiring / duplicated reminders | `mark_fired` is transactional and repeats reschedule from the same row; the simulated 24-hour test asserts exactly one briefing per local date and no double-fire |
| Secret leakage into logs or reports | The `Proactive:` doctor line and the `proactive` diagnostics section expose counts, clocks and switch states only; no title, body or recipient is printed |
| Unbounded work on a hostile ledger | `proactive_tick_limit` (default 25, max 200) bounds one tick; indexes on `(status, due_at)` keep the due query flat |

Release-audit check **28** (`proactive_intelligence`) verifies the engine is
present, off-adjacent outbound, quiet/cap/cooldown bounded, the ledger tables
exist at the current schema version and the briefing is idempotent per day. It
is a structural check, not a claim of absolute safety.

## Transformation Phase 13 notes (0.75.1)

Connectors are the first feature that can send data *off the machine* and the
first that accepts data *from outside*, so they get their own threat model.

| Threat | Control |
| --- | --- |
| Accidental data exfiltration | Master gate `PC_AGENT_ALLOW_CONNECTORS` plus a second gate for mail (`PC_AGENT_ALLOW_EMAIL_CONNECTOR`), both default off; every outbound attempt is a ledger row |
| Unconfirmed external action | Mail sending requires `confirm="SEND"` in addition to the gate; the CLI exposes it as `--confirm SEND`, the API as `confirm` |
| SSRF / internal network probing | Chat endpoints pass the browser tool's validator (scheme, IDNA host, port, credential-free URL, resolved-address block) and redirects are refused; LAN/loopback targets need `PC_AGENT_CONNECTOR_ALLOW_PRIVATE_ENDPOINTS=1` |
| Message forgery / spoofing | Outbound chat bodies are signed (`sha256=HMAC(secret, timestamp.body)`); inbound webhooks must present a matching signature |
| Replay | Inbound events are refused when `kind:external_id` is already in the ledger, and stale timestamps outside `connector_webhook_skew_seconds` are refused before any parsing |
| Timing attacks on the signature | `hmac.compare_digest` only, never `==` |
| Header/CRLF injection | Subject and body are rejected if they contain CRLF or NUL; recipients are parsed and format-validated |
| Path traversal / root escape | The drive connector resolves every file and requires it to stay under the configured root; symlinks are skipped by default; `.ics` writes only next to the configured file |
| Reading secrets on disk | Well-known secret directories and suffixes are skipped by name (`.ssh`, `.gnupg`, `.aws`, `.azure`, `.kube`, `.password-store`, `.env`, `*.key`, `*.pem`, `*.pfx`, `*.p12`, `*.kdbx`) |
| Prompt injection through inbound content | Payloads are truncated, control characters stripped, depth and key counts bounded before storage; connector data is never executed as instructions |
| Secret leakage into logs/reports | `public_summary()` and `connector status` show masked labels only; chat/webhook URLs are echoed as `scheme://host/path` with the query string removed; the diagnostics export contains no secret |
| Ledger as an attack surface | Rows are bounded, values sanitised, and the trim happens in the same transaction |
| Unbounded resource use | Calendar file <= 2 MB, webhook body <= 64 KiB, mail <= 50 headers, drive <= 1 000 files / 32 MiB per file, SMTP <= 5 recipients, per-call timeout |

Release-audit check **27** (`connectors`) passes while everything is off and
warns - naming the exact variables to review - as soon as a connector is
enabled, so an enabled connector can never look like a default install.

Residual risk, stated plainly: a hostile chat endpoint, a compromised `.env`, a
malicious IMAP server, or an attacker who already has code execution on the
machine are outside what these controls can stop. The connectors do not claim
"secure"; they claim bounded, gated, signed and audited.

## Transformation Phase 12 notes (0.74.0)

A user-authored workflow is *the* most tempting place to hide a bypass, so the
boundary is stated as a rule and enforced in code: **a flow can only produce a
mission.** `flows.py` may validate, persist, compile to `task_specs` and call the
injected `start_mission`; it imports no tool module, opens no socket and never
touches `MissionRunner` internals.

- **No privilege escalation through data.** Every node becomes an ordinary
  Mission Center task, so approvals, Safety Kernel timeouts, token budgets,
  verification and recovery apply unchanged. There is no flow-only tool, no
  "flow admin" role, and no flag that raises a capability.
- **Strict schema, not permissive parsing.** Unknown keys are refused at document,
  node, edge and trigger level; `flow_schema` must be `1`; node ids must match
  `^[a-z0-9][a-z0-9_-]{0,31}$`, so an id can never carry a newline, a quote or a
  template marker into a log line, task key or event payload.
- **Bounded graphs.** <=20 nodes, <=8 variables, <=8 triggers per flow, schedule
  interval 60 s..30 days, `run_at` must be in the future, <=64 000-byte imports,
  <=500 listed runs. A cycle is refused (Kahn) instead of being executed until the
  timeout kills it.
- **No injection through variables.** `{{name}}` is substituted from the declared
  `variables` map only; an unresolved placeholder is a validation error, so a
  template can never pull an environment value or a secret.
- **Import is checksummed.** `export_payload()` signs the canonical JSON of
  `{flow, triggers}` with SHA-256; `load_export_payload()` refuses anything edited
  after export, and overwrite of an existing name needs an explicit flag.
  A shared workflow file is therefore either intact or rejected - never silently
  reinterpreted.
- **Triggers cannot create a storm.** Each trigger fires at most once per tick,
  the tick has a hard budget (`<=50`), the watermark/`next_run_at` advances even
  when a flow is skipped, disabled flows are skipped (not deferred), and only one
  run per flow may be active. `reconcile()` closes runs by *reading Mission
  Center*, not by trusting a timestamp, so a crashed mission cannot block a flow
  forever or be silently counted as success.
- **Refusals are recorded.** `flow_runs` stores `failed` rows with a redacted
  error, and every mutation emits an app event (`flow.run_started`,
  `flow.imported`, `flow.tick`), so a denied or failed workflow is as visible as a
  successful one.
- **Secrets never reach the ledger.** `finish_run()` passes the error through
  `redact_text()`; that helper was widened in this phase to catch bare provider
  keys (`sk-…`, `ghp_…`, `AIza…`, `xai-…`, `gsk_…`) as well as the labelled
  forms, because a provider rejection message can echo the key itself.
- **API unchanged in shape.** 18 new routes on the existing loopback + bearer
  token listener; there is still no `PUT`/`PATCH` handler, no CORS relaxation and
  no new port.

Not claimed: a flow that references a sensitive tool still depends on the user
approving it at run time (that is the point); flows do not sandbox an
already-compromised provider, and two processes sharing one database are not
protected by a lock yet.


## Transformation Phase 11 notes (0.73.0)

Automation makes the agent *act* on a real desktop, so the boundaries are
explicit and enforced in code:

- **Gates unchanged and fail-closed.** Browser tools still require
  `PC_AGENT_ALLOW_BROWSER`; desktop tools `PC_AGENT_ALLOW_DESKTOP_AUTOMATION`;
  provider vision `PC_AGENT_ALLOW_VISION` plus a key. The vision tools added in
  this phase are **local only** - they read a file inside the workspace and
  never upload pixels. A refusal names the setting to change.
- **New actions are bounded, not unbounded.** `browser.wait` is capped at 120 s
  with a poll loop and reports its attempt count; `desktop.window_state` rejects
  bounds outside +/-32000, refuses windows smaller than 120x120, clamps the
  rectangle to the virtual screen and refuses the assistant's own window (as the
  existing UIA layer already did for protected system classes).
- **Destructive-ish action refused:** `browser.tabs` will not close the last page
  tab, because that ends the user's browsing session.
- **Injection surface deliberately small.** The wait probe interpolates values
  only through `json.dumps`, so a selector containing quotes or `);` cannot break
  out of the script. Window titles are truncated to 200 characters before they
  reach the ledger.
- **Audit covers refusals.** `automation_runs` records `allowed=false` rows with
  the refusal reason, so "the agent tried and was denied" is as visible as
  "the agent acted".
- **No content in the audit trail.** The ledger stores kind, tool id, action,
  target label, status, short detail, duration and timestamp - never page text,
  screenshots, transcripts or file contents. Retention is capped at 5 000 rows.
  Writes are best-effort: an audit failure cannot block or fake a user action.
- **Honest measurement instead of confident guessing.** A template match is
  reported only after full-pixel verification on the full-size screenshot, so a
  "found at (x, y)" answer is measured. A flat-colour template is refused outright
  because it would "match" everywhere.
- **Resource caps** protect against a malicious or accidental large input:
  <= 20 MB / 40 MP source images, <= 250 000 px templates, <= 12x12 analysis
  grid, <= 40 000 sampled search steps.
- **Release audit check 25 (`automation_audit`)** reports the gate states and the
  ledger policy, so an install with automation silently enabled is visible in
  `pc-agent release audit` and `pc-agent doctor`.
- **Not claimed:** these controls reduce risk, they do not make desktop
  automation safe against a hostile local application. A window the agent is
  allowed to drive can still be a window full of attacker-controlled text.

## Transformation Phase 16 notes (0.78.0)

**Optional encrypted device sync - what it protects and what it does not.**

Protections:

- Off by default (`PC_AGENT_ALLOW_SYNC`); every sync entry point refuses when the
  gate is closed, with a message that says exactly which variable to set.
- Pairing is an explicit human trust decision (`confirm="PAIR"` in CLI, HTTP and
  GUI) that records the peer's Ed25519 and X25519 public keys plus a grouped
  fingerprint for out-of-band comparison.
- Bundle confidentiality and integrity: ephemeral-static X25519 ECDH per recipient,
  HKDF-SHA256 (`info = pc-agent-sync-v1`), AES-256-GCM with the canonical header as
  AAD. A modified header fails authentication *and* the signature.
- Sender authenticity: Ed25519 signature over the context string, the header and the
  sorted `{device_id, ephemeral_public, nonce, sha256(ciphertext)}` of every
  recipient, so recipient entries cannot be added, removed, duplicated or swapped.
- A bundle is refused from an unpaired sender, from a paused peer, or from a peer
  whose advertised signing key differs from the one recorded at pairing (no silent
  re-trust, no TOFU upgrade).
- Replay: processed `bundle_id`s are never re-processed and the per-sender `seq` must
  strictly increase; `created_at` must be inside the +/- 900 s clock window.
- Resource bounds: bundle bytes (65 536 - 32 000 000, default 4 MB), records per
  bundle (default 500), <= 20 coordination requests, retention days, and table caps.
- A peer can never execute anything. Only `remind`, `notify`, `ping` exist; all wait
  in `sync_requests` for a local human accept, and accepting only calls the already
  governed reminder/notification paths. Unknown kinds are refused at both ends.
- Profiles are create-only on import, so a peer can never rewrite local settings; a
  profile can only carry whitelisted settings fields (existing validator unchanged).
- Identity file is written with mode `0600`; no key, plaintext payload or record
  content is written to logs, events, diagnostics or error messages - only device
  ids, fingerprints, counts, sequences and sizes.

Honest limits (stated in the UI and docs):

- The transport folder is not trusted. Whoever can read it learns bundle filenames,
  sizes, the sender device id and sequence numbers; content stays encrypted.
- A compromised device holding `identity.json` can impersonate that device. The file
  is protected by filesystem permissions, **not** by an OS keystore yet.
- There is no forward secrecy across bundles beyond per-recipient ephemeral keys, no
  key rotation UI, and no way to recall a bundle already copied elsewhere.
- Unpairing stops future acceptance but cannot delete data a peer already received.
- Like every other phase, this is user-privilege software, not a hardened
  multi-tenant service.

## Transformation Phase 17 notes (0.80.1)

**Accessibility and interface language add no new attack surface.**

- No network call, no provider call, no new secret, no new privileged action. The
  translation layer is a dictionary lookup; the accessibility report is arithmetic.
- `POST /v1/i18n/locale` sits behind the existing bearer token and validates every
  field: an unknown locale, a scale outside 0.8-2.0 or an unknown colour mode is
  refused with the accepted list. It can change presentation only - never a
  capability gate, path, credential or approval.
- `data/ui.json` holds six presentation values. Unknown keys are ignored, invalid
  values are refused, and a corrupt file falls back to defaults, so a hostile file
  cannot enable a feature or crash startup.
- Interface text cannot alter behaviour: `{placeholder}` substitution uses an
  explicit mapping (never `str.format` over caller text), so a translation cannot
  inject or reformat user content.
- No user text, path or secret is written to logs or events by this layer;
  `i18n_set` records only which preference keys changed.
- Honest limits stated in the product and docs: the desktop accessibility tree is
  not published, Tk 8.6 has no bidi/shaping, and there is no automatic OS theme
  detection. None of these are security issues, but none are hidden either.

## Transformation Phase 17.1 notes (0.80.1) - owner-only files and a honest sandbox

**File permissions (Phase 16 gap, fixed).** `os.chmod(path, 0o600)` is a no-op on
NTFS: the mode always reads `0o666`, so "owner only" was a POSIX-only guarantee
that the tests silently assumed. Sync artefacts are now protected by
`pc_agent.sync.perms`:

* POSIX - `chmod 0600`, mode read back (`mechanism: posix-mode`).
* Windows - `icacls <file> /inheritance:r /grant:r <user>:(F)`, then the ACL is
  read back and an inherited ACE is treated as a failure
  (`mechanism: windows-acl`).
* No ACL support (FAT32/exFAT, some network shares) - reported as
  `applied: False` with the reason. The user is told; nothing is claimed.

The report is stored with the artefact, returned in the sync export payload and
asserted in tests, so the guarantee cannot drift back into fiction.

**Plugin sandbox spawn path (Phase 15 blocker, fixed).** The worker process could
not be created on Windows and the failure was reported as a *plugin* error. The
sandbox now always supplies a fixed, non-secret allow-list of Windows variables
(`SystemRoot`, `SystemDrive`, `windir`, `ComSpec`, `PATH`, `PATHEXT`, `TEMP`,
`TMP`, `USERPROFILE`, `HOMEDRIVE`, `HOMEPATH`, `APPDATA`, `LOCALAPPDATA`,
`PROCESSOR_ARCHITECTURE`, `NUMBER_OF_PROCESSORS`, `OS`), probes each interpreter
candidate once and caches the answer, and reports a host-level `spawn_error`
instead of blaming the plugin.

Isolation is unchanged and re-verified by the existing Phase 15 tests: same
scrubbed environment (regression-tested to contain no `*_API_KEY`), same `-I -B`
isolated interpreter, same audit hook that refuses sockets/subprocesses and file
access outside the capability roots, same Windows Job Object applied immediately
after creation, same hard timeout that kills the job. The fallback never trades
isolation for a successful start - it only changes *which* Python starts.

**Notification policy (Phase 14 defect, fixed).** The rolling hourly cap counted
its own `deferred` rows, so each suppressed tick re-armed the budget and could
starve reminders for the rest of the hour. The cap now counts `status =
'delivered'` rows only; suppression stays in the ledger as an audit record.

## Transformation Phase 18 notes (0.80.1) - hardened, and provably so

**The release chain is now evidence, not a claim.** `pc-agent release
production-check` builds a real bundle, verifies it without extracting, corrupts
a copy and requires the verifier to refuse it, signs with an ephemeral Ed25519
key, requires an unsigned bundle to be refused while
`PC_AGENT_REQUIRE_SIGNED_UPDATES=1`, then stages, applies and rolls back against
a throw-away copy of the tree, comparing tree hashes before and after. Ten steps,
recorded in `data/production-check.json`.

* No production key is used or written: the self-test key dies with the work
  directory, and a test asserts the report never contains it or the key path.
* A refusal for the *wrong reason* is a failed step, not a pass - the
  unsigned-bundle step asserts the signature policy is what refused it.
* An unexpected exception inside the chain becomes an `aborted` failure row, so a
  crash can never look like a clean run.

**Security checks are executable.** `tests/test_security_suite.py` states the
attacker's move and the refusal: private-network and DNS-rebinding SSRF, non-web
schemes, credential-bearing URLs, shell metacharacters that must stay arguments,
non-allow-listed and path-traversal executables, archive traversal entries,
secret leakage into diagnostics/audit/reports, emergency-stop enforcement, and
tool-schema pollution.

**Continuous integration adds no new trust boundary.** The workflow runs with
`permissions: contents: read` and references no secret, so a fork cannot exfiltrate
credentials through it; a failing platform cannot mask another (`fail-fast:
false`).

**Unchanged and re-verified:** approvals, emergency stop, capability roots, the
`-I -B` plugin sandbox with audit hook and Windows job object, owner-only sync
files (POSIX mode / verified Windows ACL), allow-listed release contents, and
constant-time signature checks.

**Still honest:** the chain check covers the local transport (bundle + detached
signature), not a live feed; Authenticode signing of the Windows installer needs a
user-supplied certificate; and the CI workflow has not yet run on GitHub - both
facts are stated in `AUDIT_REPORT.md` rather than implied away.

## Windows acceptance evidence (0.80.1) - what it is and is not

`data/windows-acceptance.json` is written by `pc-agent release evidence record`
and read by `baseline.windows_acceptance_evidence()`.

- **Tamper-evident:** the report body is sealed with a SHA-256 hash computed by
  `baseline.acceptance_body_sha256()`; the reader recomputes it and treats any
  mismatch, unknown status, unknown platform, missing gate, failing gate, wrong
  version or credential-looking string as "not evidence". A failed check leaves
  the status at `pending_user_windows_evidence`; it never degrades silently to a
  weaker claim.
- **Not tamper-proof, and not authentication:** the file is produced and stored by
  the user's own account. An attacker who can already write to `data/` can write a
  self-consistent report. It records *what this install observed on this machine*,
  which is exactly what it is used for (closing a platform-acceptance gap), and it
  must not be treated as an external attestation or as a supply-chain signature.
- **No secrets:** the record contains version, platform facts (system, release,
  machine, Python build, CPU count, timezone, install root), gate verdicts and
  timings. The verifier rejects files that look like they contain key material, and
  `tests/test_windows_acceptance_evidence.py` asserts the absence of secrets.
- **No privilege change:** the gate runs the same commands a user would run, in the
  same install, with no elevation, no network listener and no production key. The
  only file written besides the report is a temporary file used to prove that
  owner-only hardening works, deleted immediately afterwards.

## Plugin sandbox on Windows: job-object ordering (0.80.1)

The 0.80.1 acceptance run showed every plugin call failing on a machine whose
virtual environment uses a launcher shim. The cause was enforcement ordering, not
a missing control: `ACTIVE_PROCESS` was applied *after* the process joined the job
(so a shim's child could slip in, or fail unpredictably) and the pre-flight probe
ran outside the job, so it could not detect the difference.

Now: limits are set before `AssignProcessToJobObject`, the probe runs under the
same policy, and one extra slot may be granted *only* for the launcher chain, with
`mechanism` reporting it. The security posture is unchanged or better:

- total processes never exceed 2 in that mode (shim + worker) and 1 otherwise;
- plugin code still cannot create processes: the worker's audit hook denies
  `subprocess.*`, `os.system`, `os.exec*`, `os.spawn*`, `pty.spawn`,
  `ctypes.dlopen`;
- `KILL_ON_JOB_CLOSE` and the memory cap apply from the moment of assignment, so a
  timeout kills the whole chain;
- no environment variable, credential or network path was added to make this work;
- `spawn_error` still distinguishes "this host cannot start Python" from "the
  plugin misbehaved", so a host limitation is never reported as a plugin verdict.

