# Implementation Roadmap

Phase 0 was the repository audit. The remaining audited transformation scope
is planned as exactly 18 implementation phases. Historical application
Phases 1–60 remain the foundation and are not renumbered.

Delivered: Phase 1 in 0.63.0, Phase 2 in 0.64.0, Phase 3 in 0.65.0, Phase 4 in 0.66.0, Phase 5 in 0.67.0, Phase 6 in 0.68.0, Phase 7 in 0.69.0, Phase 8 in 0.70.0, Phase 9 in 0.71.0, Phase 10 in 0.72.0, Phase 11 in 0.73.0, Phase 12 in 0.74.0, Phase 13 in 0.75.1, Phase 14 in 0.76.0, Phase 15 in 0.77.0, Phase 16 in 0.78.0, and Phase 17 in 0.80.1 (all
Linux-verified; native Windows acceptance still pending real evidence).

| Transformation phase | Scope | Size |
|---:|---|---|
| 1 | Canonical baseline reconciliation, Windows acceptance pack, architecture freeze | MEDIUM |
| 2 | Safety kernel: isolated worker, hard timeout, unified approvals, emergency stop, data protection | BARA |
| 3 | Local service/API, event system, structured logging, settings and profiles | BARA |
| 4 | Mission Center, dependencies, scheduler, execution graph and parallel execution | BARA |
| 5 | Modern JARVIS Command Center with real live state | BARA |
| 6 | Live system monitoring and advanced diagnostics | MEDIUM |
| 7 | Provider Platform v2, analytics and evaluations | BARA |
| 8 | Memory v2, ingestion, embeddings, vector search and RAG | SAB SE BARA |
| 9 | Specialized multi-agent runtime and Agent Studio | SAB SE BARA |
| 10 | Voice: push-to-talk, STT, TTS, microphone privacy and optional wake word — **delivered in 0.72.0** (wake-word detection deferred and reported unavailable) | BARA |
| 11 | Advanced browser, Windows desktop automation and computer vision — **delivered in 0.73.0**: bounded `browser.wait`/`browser.tabs`, `desktop.window_state` placement, offline `vision.screen`/`vision.locate` with full-pixel verification, `automation_runs` audit ledger (schema v13), 69 tools, 25 release checks. Still open: OCR, element-level grounding, macOS/Linux desktop backends, native Windows evidence | SAB SE BARA |
| 13 | Calendar, email, chat, cloud-drive and webhook connectors - **delivered in 0.75.1**: five default-off connectors behind `PC_AGENT_ALLOW_CONNECTORS` (mail also `PC_AGENT_ALLOW_EMAIL_CONNECTOR`), iCalendar file read/write, SHA-256 folder index with add/modify/remove detection, IMAP header-only reads, approved SMTP send, HMAC-signed chat delivery, verified inbound webhooks with replay protection, schema **v15** (3 ledger tables), `connectors` CLI, 9 HTTP routes, 15th GUI tab, web panel, release check 27. Still open: OAuth/CalDAV, mail bodies, provider-specific chat payloads, connectors as agent tools (Phase 14), public webhook listener (Phase 16) | MEDIUM |
| 12 | Visual workflow builder, triggers, schedules, import/export — **delivered in 0.74.0**: strict validated flow documents, immutable versions, compile-to-mission, `manual`/`schedule`/`event` triggers with an `app_events` watermark and no replay, checksummed export/import, schema **v14** (4 tables), `flows` CLI, 18 HTTP routes, 14th GUI tab, web panel, 26 release checks. Still open: drag-and-drop canvas, conditions/loops, webhooks (Phase 13), multi-instance lock | MEDIUM |
| 14 | Proactive intelligence, notifications, reminders and daily briefings - **delivered in 0.76.0**: `ProactiveEngine` with parse-due (`HH:MM`/ISO/`+15m`) and wraparound quiet hours, a layered delivery policy (quiet window -> rolling hourly cap -> per-key cooldown -> in-app event -> optional default-off outbound), durable one-shot/repeating reminders with snooze/dismiss/enable/delete, six-section briefings built only from live connector/flow/task/system state and idempotent per local date, a real scheduler tick in `api_server`, schema **v16** (4 tables), `proactive` CLI, 13 HTTP routes, 16th GUI tab, web panel, `Proactive:` doctor line and release check 28. Still open: OS toasts, threshold/rule alerts, per-weekday quiet hours, cross-device sync (Phase 16) | BARA |
| 15 | Plugin/Skill Ecosystem v2 with signed manifests and OS sandboxing - **delivered in 0.77.0**: strict manifest contract with canonical JSON and content checksums, Ed25519 publisher signatures verified against an explicit trusted key set, unsigned bundles refused by default, closed capability list with `network`/`subprocess` refused at install, verify/install/enable/disable/rollback/uninstall with 5 versions kept, sandboxed tool execution (POSIX `setrlimit` or Windows Job Object, isolated interpreter, scrubbed env, `sys.addaudithook` denial of sockets/subprocesses/writes, hard timeout), Markdown skills as reviewed text, schema **v17** (4 tables), `plugins` CLI, 12 HTTP routes, 17th GUI tab, web panel, `Plugins:` doctor line and release check 29. Still open: registry/marketplace fetch, native plugins, plugin tools in the AI planner, read-path confinement | BARA |
| 16 | Optional encrypted cloud/device sync and secure remote coordination - **delivered in 0.78.0**: off-by-default, network-free shared-folder transport (no port, no server, no account), per-device Ed25519 signing + X25519 encryption identity at `data/sync/identity.json` (mode `0600`), explicit pairing with `confirm="PAIR"` and a grouped fingerprint, per-recipient ephemeral-static X25519 + HKDF-SHA256 + AES-256-GCM with the header as AAD, Ed25519 signature over the header and every recipient entry's digest, replay protection (processed bundle ids + strictly increasing per-sender sequence) inside a 900 s clock window, bounded bundle bytes/records/requests/retention, conflict handling that never overwrites local data (reminders de-dupe, flows `local|newest|remote`, profiles create-only), remind/notify/ping coordination requests that wait in a ledger for a human accept and can never execute a tool, schema **v18** (4 tables), `sync` CLI (14 subcommands), 12 HTTP routes, 18th GUI tab, web panel, `Sync:` doctor line and release check 30. Still open: relay/real-time transport, delta bundles, key rotation, OS-keystore identity, metadata confidentiality, native Windows multi-device evidence | SAB SE BARA |
| 17 | Accessibility, multilingual UI, RTL, performance and UX polish - **delivered in 0.80.1**: `i18n` layer with 61 real translations in en/es/ur/ar (no user-content translation, counted English fallback, `en-US`/`UR_pk` normalisation, fake-translation test), real WCAG 2.1 luminance/contrast maths with a high-contrast palette and `passing/total` reporting, declared `ltr`/`rtl` per locale with layout mirroring in the web dashboard and the desktop header, text scale 80-200 %, reduce-motion and keyboard-shortcut settings persisted in `data/ui.json` (environment wins, corrupt file tolerated), 19th GUI tab **Accessibility**, web Accessibility panel with skip link/landmarks/aria-live/focus-visible/prefers-reduced-motion, `i18n`+`a11y`+`perf bench` CLI, 5 HTTP routes, `a11y` diagnostics section, `Accessibility:` doctor line and release check 31. Schema stays **v18**. Still open: Tk bidi/shaping, desktop accessibility tree, OS theme detection, captioning, more locales, remaining pre-Phase-17 strings | MEDIUM |
| 18 | Final production hardening, CI, signed packaging, updates, rollback and complete documentation | BARA |

## Phase gates

Every phase must report:

- what already existed and was reused;
- what was partial, broken, missing or unverified;
- exact files changed;
- tests and clean-room results;
- security and performance implications;
- honest limitations;
- the next phase and remaining-work size.

No phase may silently remove a working capability or weaken an approval,
path, credential, update-signature or privacy boundary.

## Phase 18 - Final production hardening (0.80.1) - DELIVERED

- [x] CI workflow for both platforms and both Python versions, with the audit and
      the release chain gate
- [x] End-to-end release chain verification (`release production-check`)
- [x] Release check 32, `Hardening:` doctor line, `Production` dashboard tab
- [x] Security regression suite (19 checks) and performance budget suite (9 checks)
- [x] `AUDIT_REPORT.md`, `DEVELOPER_GUIDE.md`, `INSTALLATION_GUIDE.md`,
      `PLUGIN_DEVELOPMENT_GUIDE.md`
- [x] Honest counters updated everywhere (32 checks, 20 tabs, phase 18)

### Post-programme item, now executable (0.80.1)

- [x] `pc-agent release evidence` harness: seven real gates, sealed report,
      `verify` / `record`, self-test mode, 19 tests
- [x] Baseline and doctor reflect only a verified recorded Windows report

### Programme status

All eighteen phases are delivered. What remains is **external evidence**, not
development: the Windows acceptance run for 0.80.1 and the first real GitHub
Actions execution. Both are tracked in `AUDIT_REPORT.md` section 8. The Windows
item now has a one-command path (`pc-agent release evidence` then
`... record`); it still needs the user's machine, which is why it is listed as
outstanding rather than done.

## 0.81.0 - the two open capability gaps closed

- [x] Voice: deterministic intent routing, hands-free bounded loop, sealed
      acceptance report, CLI and HTTP surfaces
- [x] Signed installation tooling: Authenticode signing (user certificate),
      `WinVerifyTrust` verification, release check 33, doctor line
- [x] 46 new tests, docs page `docs/voice_and_authenticode.md`

Remaining external items are unchanged and listed in `AUDIT_REPORT.md` section 8:
the Windows acceptance run, the first real CI run, and a user-supplied
code-signing certificate.

