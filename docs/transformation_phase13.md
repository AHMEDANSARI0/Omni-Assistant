# Transformation Phase 13 — Connectors (0.75.1)

Calendar, e-mail, chat, cloud-drive and webhook connectors, plus the ledger that
makes every one of them auditable. Windows-first, local-first, and off by
default.

- Version: **0.75.1** (0.75.0 shipped the connectors; 0.75.1 fixes the Windows
  process scan found on the user's machine)
- Database schema: **v15** (`connector_events`, `connector_deliveries`, `connector_state`)
- Release audit checks: **27**
- Suite: **827 passed, 1 skipped** (skip = no desktop display for a real Tk window)
- New tests: `tests/test_transformation_phase13_connectors.py` (26 tests)

---

## 1. What already existed

- Phase 3 event system + `LocalService` facade, structured logging, profiles.
- Phase 4 Mission Center (dependency levels, parallel execution, scheduler).
- Phase 12 flows: versioned instruction graphs compiled into missions, with
  schedule/event/manual triggers and a run ledger.
- Phase 10 voice, Phase 11 browser/desktop/vision, provider platform, memory,
  RAG, agents — all independent of any external account.
- Safety Kernel, Approval Center, budgets, emergency stop, bounded tools.

## 2. What was missing

1. No way to read what is on the user's calendar, mailbox or chat without
   handing an OAuth token to a cloud service.
2. No way for the agent to *notify* the user outside the app window.
3. No way for an external system to safely tell the agent "something happened".
4. No content-hashed view of a synced folder, so "what changed?" was guesswork.
5. Nothing in the product could prove *which* external interaction happened,
   when, and with what result — the audit trail stopped at the tool boundary.

## 3. What changed in this phase

### 3.1 Five connectors (`src/pc_agent/connectors/`)

| Module | Connector | Transport | Gate |
|---|---|---|---|
| `calendar_ics.py` | `CalendarConnector` | local `.ics` file (RFC 5545) | `PC_AGENT_ALLOW_CONNECTORS` + `PC_AGENT_CONNECTOR_CALENDAR` |
| `drive.py` | `DriveConnector` | local folder, SHA-256 content index | `…ALLOW_CONNECTORS` + `PC_AGENT_CONNECTOR_DRIVE_ROOT` |
| `email.py` | `EmailConnector` | IMAP4-SSL headers / SMTP-StartTLS send | `…ALLOW_CONNECTORS` + `PC_AGENT_ALLOW_EMAIL_CONNECTOR` + mailbox settings |
| `chat.py` | `ChatConnector` | signed HTTPS POST to one webhook | `…ALLOW_CONNECTORS` + `PC_AGENT_CONNECTOR_CHAT_URL` + `…CHAT_SECRET` |
| `webhook.py` | `WebhookConnector` | inbound HMAC verification | `…ALLOW_CONNECTORS` + `PC_AGENT_CONNECTOR_WEBHOOK_SECRET` |

Supporting modules: `base.py` (bounded text, redaction, timestamps),
`store.py` (`ConnectorStore` over the three v15 tables) and `service.py`
(`ConnectorService` — the only gate keeper and the only writer of the ledger).

### 3.2 Real behaviour, no placeholders

- **Calendar** parses and writes genuine iCalendar text: line folding at 75
  octets, escaped `,`/`;`/`\`, `VEVENT` blocks with `UID` + `DTSTART`
  validation, atomic write (temp file + replace), existing events preserved,
  upcoming events filtered by a bounded window and sorted by real instants.
  Naive timestamps are written as local time converted to UTC — never as a
  fabricated "Z".
- **Drive** walks the folder without following symlinks/junctions outside the
  root, skips secret-bearing names (`.ssh`, `.gnupg`, `.aws`, `.kube`, `.env`,
  `*.key`, `*.pem`, `*.kdbx`, …), hashes up to 32 MiB per file, caps the file
  count, and reports `added` / `modified` / `removed` / `unchanged` / `skipped` /
  `truncated` against the manifest stored from the previous run.
- **Mail** reads headers only (`BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE
  MESSAGE-ID)]`), decodes RFC 2047 subjects, never downloads bodies or
  attachments, and refuses to send without `confirm="SEND"`.
- **Chat** posts a deterministic JSON body, signs `timestamp.body` with
  HMAC-SHA256, sends the signature in `X-PcAgent-Signature: sha256=…`, and logs
  the delivery with status code, byte count and duration.
- **Webhook** verifies a fresh timestamp inside a configurable skew window, a
  constant-time signature comparison, and replay protection keyed on
  `kind:external_id` from the ledger.

### 3.3 Product surfaces

| Surface | Phase 13 additions |
|---|---|
| CLI | `pc-agent connectors status/probe/events/deliveries`, `connectors calendar list/add`, `connectors drive sync`, `connectors mail inbox/send`, `connectors chat send`, `connectors webhook receive` |
| API | `/v1/connectors/status`, `/events`, `/deliveries`, `GET|POST /v1/connectors/calendar`, `POST /v1/connectors/drive/sync`, `GET /v1/connectors/mail/inbox`, `POST /v1/connectors/mail/send`, `POST /v1/connectors/chat/send`, `POST /v1/connectors/webhook` |
| GUI | new **Connectors** tab (15th): gate/status line, probe of the local calendar and drive targets, calendar list/add, drive sync, mail headers, chat send, webhook verification, live ledger |
| Web dashboard | live **Connectors** panel + buttons that call the real API |
| `doctor` | `Connectors: enabled=…; events=…; deliveries=… (failed=…); last=…` |
| `release audit` | check 27 `connectors` — `pass` when every connector is off, `warn` when one is enabled |
| Diagnostics export | `connectors` section (gates, per-connector state, ledger counts) |

### 3.4 Configuration (all off by default)

```
PC_AGENT_ALLOW_CONNECTORS=0                     # master switch
PC_AGENT_ALLOW_EMAIL_CONNECTOR=0                # second gate, mail only
PC_AGENT_CONNECTOR_CALENDAR=                    # path to a .ics file
PC_AGENT_CONNECTOR_DRIVE_ROOT=                  # folder to index
PC_AGENT_CONNECTOR_CHAT_URL=                    # https://chat.example.com/hooks/…
PC_AGENT_CONNECTOR_CHAT_SECRET=                 # HMAC key for outbound chat
PC_AGENT_CONNECTOR_WEBHOOK_SECRET=              # HMAC key for inbound events
PC_AGENT_CONNECTOR_EMAIL_HOST=                  # IMAP host
PC_AGENT_CONNECTOR_EMAIL_USER=
PC_AGENT_CONNECTOR_EMAIL_PASSWORD=
PC_AGENT_CONNECTOR_EMAIL_FOLDER=INBOX
PC_AGENT_CONNECTOR_SMTP_HOST=                   # send is disabled without it
PC_AGENT_CONNECTOR_SMTP_PORT=587
PC_AGENT_CONNECTOR_MAX_ITEMS=25                 # 1–100
PC_AGENT_CONNECTOR_TIMEOUT_SECONDS=15           # 1–120
PC_AGENT_CONNECTOR_WEBHOOK_SKEW_SECONDS=300     # 30–86400
PC_AGENT_CONNECTOR_ALLOW_PRIVATE_ENDPOINTS=0    # opt-in LAN/loopback chat targets
```

Out-of-range values raise a `ConfigError`; they are never silently clamped.
`Settings.public_summary()` exposes only masked labels (`connector_chat:
configured`, `connector_webhook_secret: set`) — never a secret or a tokenised
URL.

### 3.5 Files

New: `src/pc_agent/connectors/{__init__,base,calendar_ics,chat,drive,email,store,webhook,service}.py`,
`tests/test_transformation_phase13_connectors.py`, this document.

Modified: `src/pc_agent/{config,errors,service,cli,api_server,diagnostics,release,web_ui,baseline,__init__}.py`,
`src/pc_agent/storage/db.py` (v15), `src/pc_agent/gui/{app,controller}.py`,
`pyproject.toml`, and the delivery documents listed in §9.

## 4. Complete features deliberately not rebuilt

Mission Center, scheduler, approvals, Safety Kernel, voice, browser/desktop
automation, vision, provider platform, memory/RAG, agents and flows were reused
unchanged. Connectors are additive: no existing behaviour was replaced, and no
existing setting changed meaning.

## 5. Tests and results

```
tests/test_transformation_phase13_connectors.py .... 26 passed
full suite ....................................... 827 passed, 1 skipped
hotfix (0.75.1) .................................. +8 monitoring tests
  (`tests/test_transformation_phase6_monitoring.py` 10 -> 18: fallback ordering,
  reason preservation, locale-tolerant `tasklist` parsing, PowerShell parsing,
  enum counters, missing-export reason, Toolhelp names, honest empty scan)
ruff check ........................................ clean
```

The new suite asserts, among others: gates closed by default with an actionable
reason; a second gate for mail; no secret or tokenised URL in any public
output; bounded configuration values; calendar round-trip, folding, ordering
and refusal of bad input/oversized files; drive add/modify/remove detection,
symlink-escape refusal, secret-name skipping and truncation; IMAP header-only
reads with decoded subjects; SMTP refusal without `SEND`, header-injection
refusal, recipient cap; signed chat delivery with a verified signature, recorded
failure, and refusal of a loopback endpoint; webhook acceptance, replay refusal,
stale-timestamp refusal, forged-signature refusal, oversized/non-JSON refusal;
`LocalService`, CLI, diagnostics, release audit and ledger bounding.

## 6. Security implications

- **Least privilege:** every connector is off until the user sets an environment
  variable; mail sending needs a *second* gate plus a per-message token.
- **SSRF:** chat endpoints pass the same validator the browser tool uses
  (scheme, IDNA host, port, credential-free URL, resolved-address block), and
  redirects are refused outright.
- **Authenticity:** outbound messages are signed; inbound events are verified
  with constant-time comparison, a bounded clock skew and replay protection.
- **Injection:** inbound payloads are truncated and stripped of control
  characters before storage, headers are validated against CRLF/NUL injection,
  and message text reaches the shell nowhere (no subprocess is used at all).
- **Path traversal:** the drive connector resolves each file and requires it to
  stay under the configured root; symlinks are skipped by default; `.ics` writes
  happen next to the configured calendar file only.
- **Data minimisation:** mail bodies are never fetched; transcripts/payloads are
  capped; the ledger keeps a bounded number of rows; secrets live in the
  environment/`.env`/OS store, never in the database or the reports.
- **Audit:** each attempt is a row (`connector_events` / `connector_deliveries`)
  plus an application event, so "what left this machine" is answerable.
- **No absolute security claim:** a compromised `.env`, a hostile chat endpoint,
  or an OS-level keylogger is outside what these controls can prevent.

## 7. Performance implications

Measured on the sandbox (Linux, Python 3.13.16, warm cache, median of 10–200
rounds):

| Operation | Median | Max |
|---|---|---|
| Calendar `add_event` (atomic write) | 0.18 ms | 2.16 ms |
| Calendar `list_events` (40 events) | 0.51 ms | 0.96 ms |
| Drive scan (51 files, 1.6 MB) | 4.11 ms | 4.31 ms |
| Webhook verify (HMAC + JSON) | 0.02 ms | 0.13 ms |
| `ConnectorService.status()` (no probe) | 0.71 ms | 1.18 ms |
| Ledger read, 25 events | 0.63 ms | 0.73 ms |

Costs are I/O-bound only when the user actually asks for mail/chat; nothing
polls in the background, so idle cost is zero. Network operations are bounded by
`connector_timeout_seconds` (default 15 s) and by item caps
(`connector_max_items`, default 25).

## 8. Limitations

1. Calendar is iCalendar-file based: CalDAV/Google/Microsoft sync requires the
   Phase 16 or a future connector; there is no attendee/reminder/permission
   semantics.
2. Mail reads headers only and sends plain text; there is no HTML body, no
   attachment handling and no per-folder search.
3. Chat supports one endpoint and one message shape; provider-specific payload
   formats (Slack blocks, Teams cards) are a mapping layer not shipped yet.
4. The drive connector indexes one root and does not download/upload anything —
   "cloud drive" means the folder the user's client already syncs.
5. Connectors are **not** registered as agent tools, so an autonomous mission
   cannot read the calendar or send a chat message by itself yet; that
   deliberately waits for the Phase 14 proactive engine and its approval policy.
6. Webhooks are verified through CLI/API, not by a public listener; exposing one
   is the Phase 16 remote-coordination job.
7. No OAuth, so nothing here can act on behalf of a third-party account.

## 9. Documentation updated

`CHANGELOG.md`, `TEST_REPORT.md`, `FEATURE_MATRIX.md`, `ARCHITECTURE.md`,
`API_DOCUMENTATION.md`, `IMPLEMENTATION_ROADMAP.md`, `SECURITY_REPORT.md`,
`PERFORMANCE_REPORT.md`, `README.md`, and this file. `AUDIT_REPORT.md` keeps the
pre-transformation baseline and is not rewritten by a later phase.

## 10. Next tasks (Phase 14 — proactive intelligence)

- Reminders and daily briefings that read the calendar/drive ledgers and notify
  through the chat connector, with explicit quiet hours and rate limits.
- Notification policy: what may be sent without asking, what needs an approval
  row, and how the user mutes it.
- Connector-aware triggers for flows (a webhook event or a drive change starting
  a workflow) using the Phase 12 event triggers already in place.
- Per-connector budgets and a "do not disturb" window, then Windows acceptance
  on the user's machine.
