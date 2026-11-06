# Feature Matrix

Status values are from the Phase 0 audit of GitHub commit
`89610f3d18f9348b6fe8ead679bab81ee34155df`, reconciled against the delivered
Phase 60 tree. **COMPLETE** means complete only within the bounded scope in
the notes.

| Capability | Status | Current boundary / next phase |
|---|---|---|
| Local CLI/GUI/SQLite/tool core | PARTIAL | Strong reusable foundation; service and safety boundaries evolve in Phases 2–3 |
| JARVIS command center | PARTIAL | Web Command Center at the loopback API root + Tkinter Command Center tab with live tasks/missions/approvals (Phase 5, 0.67.0) and a real System telemetry panel (Phase 6, 0.68.0); polish/accessibility in Phase 17 |
| Accessibility/localization/RTL | PARTIAL | Since Phase 17 (0.80.1): four interface locales (en/es/ur/ar) with 61 real translations per locale, no user-content translation, counted English fallback, real `dir`/RTL mirroring in the web dashboard and the desktop header, text scale 80-200%, high-contrast palette, persisted reduced-motion setting, documented keyboard shortcuts, and WCAG 2.1 contrast measured for every pair. Still open: Tk bidi/shaping (platform limit, disclosed), a desktop accessibility tree, OS theme detection, captioning, more locales, translating the remaining pre-Phase-17 panel strings |
| Voice/wake word | PARTIAL | Since Phase 10 (0.72.0): push-to-talk capture (native Windows `winmm`, ALSA `arecord`), local-first STT (Windows System.Speech, faster-whisper, whisper.cpp) with an opt-in cloud engine that needs per-call consent, offline TTS (System.Speech, espeak-ng), persisted microphone consent (off by default), separate wake-word opt-in, session ledger with discard-by-default audio, purge/clear commands and an emergency-stop block. Wake-word *detection* itself is not implemented (reported unavailable); no cloud TTS yet |
| Orchestration/planning | PARTIAL | Sequential five-step maximum; graph in Phase 4 |
| Parallel/multi-agent delegation | PARTIAL | Since Phase 9 (0.71.0): six builtin specialists + custom agents with filtered-registry least privilege, persona AI planning, explicit or coordinator-proposed team graphs (<=6 assignments, bounded parallelism, blocked dependents); depth-1 only, no agent-to-agent chat; richer collaboration deferred |
| Task/Mission Center | PARTIAL | Lifecycle exists; metadata/scheduler in Phase 4 |
| Structured memory/workflows | PARTIAL | Notes/preferences/exact workflows; Memory v2 since Phase 8 (0.70.0): model-tagged note embeddings + tags, hybrid `memory search`, edit-invalidated vectors, bounded backfill/reindex |
| Knowledge/RAG/vector search | PARTIAL | Since Phase 8 (0.70.0): explicit text/PDF/Office ingestion (sha256 dedupe, no silent OCR), offline hashing embeddings by default (lexical, not neural) or configured OpenAI-compatible/Ollama backends, hybrid vector+keyword search with labelled keyword fallback, grounded cited answers that skip the provider when nothing matches; no OCR/image ingestion, planning does not auto-consult memory yet |
| Provider router | PARTIAL | Six external slots plus mock; Provider Platform v2 since Phase 7 (0.69.0): capability catalog, strategy routing (local-first / measured-fastest / cheapest by user prices), local usage analytics, adapter streaming, built-in evaluations |
| Live provider compatibility | UNVERIFIED | Secret-safe live matrix still required; offline evaluation cases + parsers fixture-tested since Phase 7 (0.69.0); Gemini streaming not implemented (reported honestly) |
| Built-in tools | PARTIAL | 69 bounded tools (64 at 0.72.0); Safety Kernel hard timeouts + isolated worker since Phase 2 (0.64.0); Local Service/API, global events, structured logging, and settings profiles since Phase 3 (0.65.0); Mission Center dependency graphs, parallel execution, and schedules since Phase 4 (0.66.0); JARVIS Command Center web UI + Tkinter tab with live state since Phase 5 (0.67.0); stdlib live system monitoring (CPU/memory/disk/network/GPU/processes) since Phase 6 (0.68.0); Provider Platform v2 (capabilities, strategy routing, usage analytics, streaming, evaluations) since Phase 7 (0.69.0); knowledge ingest/search tools (worker-excluded) since Phase 8 (0.70.0); Agent Studio filtered registries since Phase 9 (0.71.0) |
| Office/PDF/image tools | PARTIAL | Strong bounded engines; local OCR/knowledge extraction later |
| PDF trust/PAdES external acceptance | UNVERIFIED | External validator/reader evidence required |
| Dynamic tools/plugins | PARTIAL | Pure-compute restricted subprocess; plugin v2 in Phase 15 |
| Visual workflow builder | PARTIAL | Since Phase 12 (0.74.0): strict validated flow documents (nodes/edges/variables), immutable versions, compile-to-mission, `{{var}}` substitution, checksummed export/import, `flows` CLI + 18 API routes + GUI Workflows tab + web panel. Linear DAG only; no drag-and-drop canvas, conditions or loops |
| Browser automation | PARTIAL | Local CDP only. Since Phase 11 (0.73.0): bounded `browser.wait` (selector/text/URL, <=120s) and `browser.tabs` list/activate/close (last tab refused); native browser matrix still unverified |
| Desktop automation/CV | PARTIAL | Exact Windows UIA subset. Since Phase 11 (0.73.0): `desktop.window_state` (minimise/maximise/restore/move/resize, clamped to the virtual screen) and offline perception (`vision.screen` measured facts, `vision.locate` verified template match, no OCR, no image upload); macOS/Linux desktop backends deferred |
| Native Windows acceptance | UNVERIFIED | User Windows evidence required for exact builds |
| Visual workflow builder | MISSING | Phase 12 (delivered in 0.74.0) |
| Calendar / drive / mail / chat / webhook connectors | PARTIAL | Since Phase 13 (0.75.1): iCalendar file read/write, SHA-256 folder index with add/modify/remove detection, IMAP header-only reads, approved SMTP send, HMAC-signed chat delivery, verified inbound webhooks with replay protection, and a durable `connector_events`/`connector_deliveries`/`connector_state` ledger (schema v15). All off by default behind `PC_AGENT_ALLOW_CONNECTORS` (mail also needs `PC_AGENT_ALLOW_EMAIL_CONNECTOR`). No OAuth/CalDAV, no mail bodies or attachments, not exposed as agent tools until a later phase (the Phase 14 proactive engine reads the calendar and drive sections through the connector service) |
| Plugin/skill ecosystem | PARTIAL | Since Phase 15 (0.77.0): Ed25519-signed manifest bundles with checksum-bound content, closed capability list (`network`/`subprocess` never granted), verify/install/enable/disable/rollback/uninstall, sandboxed tool execution (POSIX `setrlimit` or Windows Job Object + isolated interpreter + audit hook + hard timeout), Markdown skills as text only, schema v17 registry and run ledger, `pc-agent plugins …`, 12 HTTP routes, 17th GUI tab, web panel. Still open: marketplace/registry fetch, native plugins, plugin tools for the AI planner, read-path confinement |
| Native OS toast notifications | MISSING | Phase 14 (candidate follow-up; needs the outbound gate plus a Windows-specific test) |
| Proactive intelligence/notifications | PARTIAL | Since Phase 14 (0.76.0): durable reminders (one-shot/repeating, `HH:MM`/ISO/`+15m` offsets, snooze/dismiss/enable), a real notification policy (wraparound quiet hours with optional `critical` crossover, rolling hourly cap, per-key cooldown), daily briefings composed from live calendar/drive/flow/task/system state, a scheduler tick, 13 HTTP routes, `pc-agent proactive …`, 16th GUI tab, web panel, schema v16 ledger. Outbound delivery is default-off; OS toasts, rule/threshold alerts and per-weekday quiet hours are still open |
| Live system monitoring | PARTIAL | Aggregate CPU/memory/disk/network rates + NVIDIA GPU via nvidia-smi + top processes since Phase 6 (0.68.0); AMD/Intel GPU, temperatures, history/alerts deferred (Phase 14) |
| Triggers and schedules | PARTIAL | Since Phase 12 (0.74.0): per-flow `manual`/`schedule` (interval 60 s..30 d or one-shot `run_at`)/`event` triggers with an `app_events` watermark, 8-trigger cap, bounded `tick`; event triggers poll (webhooks in Phase 13), schedules need a running app |
| Diagnostics/self-healing | PARTIAL | Task/app recovery exists; doctor/diagnostics gained a live `system` section in Phase 6 (0.68.0), a `voice` section in Phase 10 (0.72.0), an `automation` section plus `Automation:` doctor line in Phase 11 (0.73.0), a `flows` section plus `Flows:` doctor line in Phase 12 (0.74.0) and a `connectors` section plus `Connectors:` doctor line in Phase 13 (0.75.1); proactive alerts plus a `proactive` section and a `Proactive:` doctor line in Phase 14 (0.76.0), a `plugins` section plus a `Plugins:` doctor line in Phase 15 (0.77.0), a `sync` section plus a `Sync:` doctor line in Phase 16 (0.78.0), and an `a11y` section plus an `Accessibility:` doctor line in Phase 17 (0.80.1) |
| Policy/approvals/emergency stop | PARTIAL | Exact approvals + digest-bound Approval Center + global emergency stop since Phase 2; graphical Approval Center in Phase 5 |
| Hard timeout/isolation | PARTIAL | Hard deadlines enforced since Phase 2: worker process kill for data-context tools; browser/desktop/vision keep a bounded thread path (results discarded, thread cannot be force-killed); OS sandbox in Phase 15 |
| OS credential storage | PARTIAL | Win/Linux/macOS backends; native proof varies by platform |
| Encryption/privacy governance | PARTIAL | Optional passphrase-encrypted backups + digest-only approval records since Phase 2; live SQLite remains plaintext to the local OS account |
| Database/backups/migrations | PARTIAL | Schema v15 (v11 at 0.71.0; v12 voice ledger at 0.72.0; v13 automation ledger at 0.73.0; v14 flow definitions/versions/triggers/runs at 0.74.0; v15 connector events/deliveries/state at 0.75.1; v16 reminders/notifications/briefings/proactive state at 0.76.0; v17 plugin registry/versions/runs/state at 0.77.0), verified + optionally encrypted backups; concurrency/upgrade hardening later |
| General application API | PARTIAL | Loopback token-authenticated `/v1` API since Phase 3 (0.65.0), missions/schedules (Phase 4), Command Center page (Phase 5), `/v1/system` (Phase 6), provider platform endpoints (Phase 7), knowledge/memory-search endpoints (Phase 8), agents/team endpoints (Phase 9); remote access deferred to Phase 16 |
| Cloud/device sync | PARTIAL | Since Phase 16 (0.78.0): off-by-default, network-free shared-folder sync of reminders/flows/profiles between the user's own devices, with per-device Ed25519+X25519 identities, explicit pairing, X25519+HKDF-SHA256+AES-256-GCM per-recipient sealing, Ed25519 signatures over the header and recipient digests, replay ids + monotonic per-sender sequence + a 900 s clock window, bounded bundles, and a conflict policy that never overwrites local data; a peer can only file remind/notify/ping requests that wait for a human accept. Still open: relay or real-time transport, delta bundles, key rotation, OS-keystore identity storage, metadata confidentiality |
| SaaS accounts/tenancy/billing | MISSING | Deferred beyond personal local transformation |
| Offline licensing | COMPLETE | Bounded signed-token licensing; not accounts or payments |
| Logging/metrics/tracing | PARTIAL | SQLite records strong; structured events in Phase 3 |
| Analytics/evaluation/cost | PARTIAL | Local usage analytics (no prompt text), built-in evaluation cases, user-priced cost estimates since Phase 7 (0.69.0); judge-model/golden-dataset evals deferred to Phase 9+ |
| Settings/profiles/resource limits | PARTIAL | Environment settings and task budgets; Phase 3 |
| Performance/resource governance | PARTIAL | Linux smoke data only; Windows benchmarks in Phases 1/17 |
| Test/quality system | PARTIAL | Large deterministic suite (887 passed, 1 skipped at 0.77.0); CI/native/E2E layers in Phase 18 |
| Packaging/update/rollback | PARTIAL | Signed bundles/feeds and package builders; signing/native proof later |
| Supply-chain integrity | PARTIAL | Release signing exists; lock/SBOM/CI/Authenticode later |
| Required documentation | PARTIAL | Phase 1 root set started; complete set in Phase 18 |
| Cross-platform parity | PARTIAL | Windows-first; Linux partial; macOS packaging absent |

The detailed evidence, source modules and test references are in the Phase 0
`AUDIT_REPORT.md` retained with the project audit records.

## Transformation Phase 18 - production hardening

| Feature | State | Where | Honest limit |
| --- | --- | --- | --- |
| Continuous integration (ubuntu + windows, py3.11 + 3.13) | Delivered | `.github/workflows/ci.yml` | validated locally; not yet run on GitHub |
| Accessibility / i18n / perf CI job | Delivered | same workflow | prints measurements, does not gate on them |
| Tag-only packaging job | Delivered | same workflow | unsigned installer; Authenticode needs a user certificate |
| End-to-end release chain check | Delivered | `pc-agent release production-check`, `production_check.py` | local transport only, same-version reinstall |
| Tamper detection proof | Delivered | chain step 4 | - |
| Signature policy proof (unsigned refused) | Delivered | chain step 6 | - |
| Byte-level apply/rollback proof | Delivered | chain steps 8-9 | applied to a copy, not the live install |
| Release check 32 `production_hardening` | Delivered | `release.py`, `release audit` | status only; it does not run the chain |
| `Hardening:` doctor line | Delivered | `pc-agent doctor` | - |
| 20th dashboard tab "Production" | Delivered | `gui/app.py` | runs the real check on demand, no fake progress |
| Security regression suite | Delivered | `tests/test_security_suite.py` | 19 checks, not a fuzzing campaign |
| Performance budget suite | Delivered | `tests/test_performance_suite.py` | ceilings, not micro-benchmarks |
| Audit / developer / installation / plugin guides | Delivered | four root documents | written from the real commands |
| Windows acceptance evidence harness | Delivered | `windows_acceptance.py`, `pc-agent release evidence` | must run on Windows; refuses to claim otherwise |
| Evidence seal + verifier | Delivered | `acceptance_body_sha256`, `release evidence verify` | tamper-evident, not authentication |
| Baseline flips only on verified evidence | Delivered | `baseline.windows_acceptance_evidence()` | stays `pending_user_windows_evidence` until recorded |
| `Hardening:` line carries acceptance state | Delivered | `diagnostics.production_summary_line` | - |
| Self-test mode for the harness | Delivered | `release evidence --self-test` | produced report can never be recorded |
| Voice intent routing (9 intents, deterministic) | Delivered | `voice/intents.py` | rule-based; unknown text becomes an ordinary task |
| Voice intent execution (note/reminder/task/speak) | Delivered | `VoiceIntentExecutor` | tasks still pass the approval centre |
| Hands-free bounded voice loop | Delivered | `voice/listener.py`, `pc-agent voice loop` | transcript-based wake phrase, one capture per cycle |
| Voice acceptance report (sealed) | Delivered | `voice/acceptance.py`, `pc-agent voice acceptance` | hardware gates report `not-run` without a device |
| Voice intent/acceptance HTTP routes | Delivered | `/v1/voice/intent`, `/v1/voice/acceptance` | same service object as the CLI |
| Authenticode signing tooling | Delivered | `windows_signing.py`, `pc-agent release sign` | needs the user's certificate; no key is generated |
| Authenticode verification | Delivered | `WinVerifyTrust` via ctypes, `pc-agent release signature` | answers only on Windows |
| Release check 33 `windows_authenticode` | Delivered | `release.py` | reports unsigned honestly; warns on a failed signing attempt |

