# Transformation Phase 16 - Optional Encrypted Device Sync and Secure Remote Coordination

Application version **0.78.0**, database schema **v18**, release audit **30 checks**,
**18** GUI tabs, tool registry unchanged at **69** built-ins.

This phase adds the one thing a local-first assistant is normally missing: a way to
carry your own state between *your own* devices without handing it to a cloud
service. It is off by default, it never opens a socket, and it is honest about the
limits of "encrypted sync in a shared folder".

---

## 1. What already existed before this phase

* A single local SQLite database (schema v17) with a safety kernel, mission center,
  memory, agents, flows, connectors, proactive reminders and the plugin registry.
* `pc-agent backup` - an encrypted **whole-database** backup, single-device only.
* The Local Service / HTTP API / GUI / web dashboard surfaces from Phases 3-15, all
  reading live state.
* No notion of a *second device*: no device identity, no peer trust, no transport,
  no conflict handling, no remote coordination of any kind.

## 2. What was missing

1. A stable, secret device identity that survives reinstall of the app but never
   leaves the machine.
2. A trust decision - *which* other device may send data here - that is explicit,
   auditable and revocable.
3. A transport that does not require us to run a server, open a port or buy a
   service: shared-folder sync (OneDrive/Dropbox/USB/LAN share).
4. End-to-end encryption so the folder (and whatever cloud is behind it) never sees
   plaintext, plus sender authentication so a file dropped into the folder by
   somebody else is refused.
5. Replay protection and a clock-skew window.
6. Deterministic conflict handling that cannot silently destroy local data.
7. A way to ask another of *your* devices to remind or notify you, without ever
   letting a peer execute anything.

## 3. What was built

### 3.1 Package `src/pc_agent/sync/`

| File | Responsibility |
| --- | --- |
| `crypto.py` | `DeviceIdentity` (Ed25519 sign seed + X25519 encrypt seed), `device_id_for()` = SHA-256 of both public keys, grouped human `fingerprint()`, `load_or_create_identity()` writing `data/sync/identity.json` with mode `0600`, `seal_bundle()`, `open_bundle()` |
| `store.py` | `SyncStore` over `sync_devices`, `sync_bundles`, `sync_requests`, `sync_state`; caps 400 bundles / 200 requests per table |
| `service.py` | `SyncService`: gates, pairing, pause/resume, `build_payload`, `export`, `import_file`, `tick`, `request`, `decide`, `status`, `bundles` |
| `__init__.py` | Public surface: `SyncService`, `SyncStore`, `SyncError`, `COLLECTIONS`, `REQUEST_KINDS`, `BUNDLE_SUFFIX` |

### 3.2 Bundle format (`.pcasync`, JSON, no network fields)

```
{
  "header": {
    "schema": 1, "bundle_id": ..., "created_at": ISO-8601+tz,
    "sender_device": <device id>, "sender_sign_public": <b64 Ed25519>,
    "sender_fingerprint": "XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX",
    "seq": <monotonic per sender>, "size_bytes": <plaintext byte length>
  },
  "recipients": [
    {"device_id": ..., "ephemeral_public": <b64 X25519>, "nonce": <b64 12B>,
     "ciphertext": <b64 AES-256-GCM(plaintext, aad = canonical header)>}
  ],
  "signature": <b64 Ed25519 over "pc-agent-sync-bundle-v1" + header + sorted recipient digests>
}
```

* Key agreement: ephemeral-static **X25519** ECDH per recipient.
* KDF: **HKDF-SHA256**, `info = "pc-agent-sync-v1"`, 32-byte key.
* Cipher: **AES-256-GCM**, 96-bit random nonce, AAD = the canonical header, so a
  header edit invalidates decryption *and* the signature.
* Signature covers every recipient entry's `device_id`, `ephemeral_public`, `nonce`
  and the SHA-256 of its ciphertext, so a recipient entry cannot be swapped,
  duplicated or stripped.

### 3.3 Import order (every step is a refusal, never a warning)

1. `PC_AGENT_ALLOW_SYNC` gate.
2. File exists and `size <= PC_AGENT_SYNC_MAX_BUNDLE_BYTES`.
3. Valid JSON.
4. Replay: `bundle_id` must never have been processed before.
5. Sender must be a **paired** peer **and** `sender_sign_public` must equal the key
   recorded at pairing time (a peer whose key changed is refused, not re-trusted).
6. Ed25519 signature over header + recipient digests.
7. `created_at` inside `PC_AGENT_SYNC_CLOCK_SKEW_SECONDS` (default 900 s).
8. This device must be one of the recipients (nothing is decrypted otherwise).
9. AES-GCM open with header AAD - tampering fails closed.
10. Payload `schema == 1` and `payload.device == header.sender_device`.
11. Collections must be a subset of `reminders | flows | profiles`.
12. Total records `<= PC_AGENT_SYNC_MAX_RECORDS` (default 500).
13. `<= 20` coordination requests, kind in `remind | notify | ping`.
14. Per-sender `seq` must be strictly newer than the last accepted one.
15. Then, and only then, apply.

### 3.4 Conflict policy (never destructive)

| Collection | Key | Behaviour |
| --- | --- | --- |
| `reminders` | `(lower(title), due_at)` | new only; duplicates skipped, never merged or deleted |
| `flows` | name / `sha256` | `PC_AGENT_SYNC_CONFLICT_POLICY` = `local` \| `newest` \| `remote` (default `newest`); a kept local copy is recorded as a conflict |
| `profiles` | name | **create-only**; an existing local profile is never overwritten (settings are local decisions) |

Every conflict is returned to the caller and written to the ledger, so the user can
see exactly what was *not* applied. A profile file can only carry whitelisted
settings fields - the existing profile validator applies unchanged.

### 3.5 Coordination requests

`pc-agent sync request remind --title "Call home" --due +30m` exports a normal
encrypted bundle carrying one request. On the other device the request lands in
`sync_requests` with status `pending`; a human must run
`pc-agent sync decide <id> --accept` (or the GUI/web button) before anything
happens. Accepting executes exactly one of:

* `remind` -> creates a local reminder through the real `ProactiveEngine`,
* `notify` -> delivers one notification through the real notification policy,
* `ping`   -> returns "ping acknowledged".

There is no code path by which a peer can execute a tool, run a flow, change a
setting, or make this device open a connection.

### 3.6 Surfaces

* **CLI**: `pc-agent sync status|identity|peers|pair|unpair|pause|resume|export|import|tick|bundles|requests|request|decide`.
  `pair` requires `--confirm PAIR`.
* **HTTP**: `GET /v1/sync/status|identity|peers|bundles|requests`,
  `POST /v1/sync/pair|peers/enabled|export|import|tick|request`,
  `POST /v1/sync/requests/{id}/decide`, `DELETE /v1/sync/peers/{id}`.
* **GUI**: 18th notebook tab **Sync** (device/peer/ledger view, pair form, export,
  import, tick, accept/deny).
* **Web dashboard**: a **Device sync** panel with the same live actions.
* **Diagnostics**: a `sync` section; **doctor** prints a `Sync:` line
  (`off (…)` when the gate is closed).
* **Release audit**: check 30 `device_sync`.

### 3.7 New configuration (all optional)

| Variable | Default | Range / values |
| --- | --- | --- |
| `PC_AGENT_ALLOW_SYNC` | off | `0`/`1` |
| `PC_AGENT_SYNC_DEVICE_NAME` | `platform.node()` | 1-60 chars, printable |
| `PC_AGENT_SYNC_DIR` | empty | path to the shared folder |
| `PC_AGENT_SYNC_MAX_BUNDLE_BYTES` | 4 000 000 | 65 536 - 32 000 000 |
| `PC_AGENT_SYNC_MAX_RECORDS` | 500 | 1 - 2 000 |
| `PC_AGENT_SYNC_RETENTION_DAYS` | 30 | 1 - 365 |
| `PC_AGENT_SYNC_CONFLICT_POLICY` | `newest` | `local` / `newest` / `remote` |
| `PC_AGENT_SYNC_CLOCK_SKEW_SECONDS` | 900 | 60 - 86 400 |

## 4. Files added / changed

* Added: `src/pc_agent/sync/{__init__,crypto,store,service}.py`,
  `tests/test_transformation_phase16_sync.py`, `docs/transformation_phase16.md`.
* Changed: `errors.py` (`SyncError`), `storage/db.py` (schema **v18** + 4 tables + 6
  indexes), `config.py` (8 settings + validators), `service.py` (12 `sync_*` methods),
  `api_server.py` (12 routes), `cli.py` (`sync` command tree + `Sync:` doctor line),
  `diagnostics.py` (`_sync_section`, `sync_summary_line`), `release.py` (check 30),
  `gui/app.py` (tab 18), `gui/controller.py` (sync controller methods),
  `web_ui.py` (Device sync panel), `baseline.py` (phase 16 label), `__init__.py`,
  `pyproject.toml`, plus the delivery docs.

## 5. Deliberately not included

* No relay server, no port, no account: transport is a folder the user already
  syncs. A cloud relay/CRDT server is future work.
* No delta sync: each bundle is a full snapshot of the three collections, bounded.
* No key rotation UI (unpair and re-pair instead) and no revoking a bundle that was
  already copied elsewhere.
* No metadata encryption: bundle filenames, size, sender device id and `seq` are
  visible to whoever can list the folder.
* Sync does not carry the database itself, credentials, plugin trust or
  approvals - only reminders, flows and profiles.
* Linux/macOS evidence remains out of scope for this phase (Windows-first).

## 6. Tests and results

`tests/test_transformation_phase16_sync.py` - **51 tests**, all passing, two real
device identities in two independent data directories and real bundles on disk:

* identity creation/reuse, `0600` permissions, public-document hashing, device-id
  derivation, Ed25519+X25519 key shapes;
* seal/open round-trip, a third device cannot read a bundle, header tamper, ciphertext
  tamper, HKDF/AES material;
* off-by-default gate, `confirm="PAIR"`, malformed keys, self-pairing refusal,
  fingerprint persistence, unpair, pause/resume;
* export (path, suffix, `0600`, single recipient, no plaintext record names),
  real state carried, idempotent replay refusal, stale `seq` refusal, unpaired-sender
  refusal, changed signing key refusal, clock-skew refusal, oversize refusal,
  malformed JSON, missing file, profile never overwritten, new profile created,
  flow conflict under `local` and `remote` policies, unknown collection refusal;
* coordination: pending until decided, accept creates a real reminder, deny executes
  nothing, unknown kind refused at the sender, double decision refused, `tick`
  export+import, retention skipping;
* ledger both directions, honest folder state, diagnostics + doctor lines, release
  audit 30 checks with `device_sync`, Local Service surface, 12 HTTP routes incl. a
  400 on bad pairing and `DELETE /v1/sync/peers/{id}`, CLI/GUI surface, no-network
  assertion.

Full suite: **938 passed, 1 skipped** (`ruff check` clean).

## 7. Security implications

* Off by default; enabling it is a deliberate `PC_AGENT_ALLOW_SYNC=1`.
* Pairing is a human trust decision with a fingerprint you can compare out of band;
  `confirm="PAIR"` is required by CLI, HTTP and GUI.
* A bundle from an unpaired device, or from a paired device whose signing key
  changed, is refused - no TOFU re-trust.
* Confidentiality and integrity come from X25519 + HKDF-SHA256 + AES-256-GCM, with
  the header as AAD, and an Ed25519 signature that binds every recipient entry.
* Replay protection: processed `bundle_id`s plus a strictly increasing per-sender
  `seq`, plus a +/- 900 s clock window.
* Resource bounds: bundle size, record count, request count, retention, table caps.
* A peer cannot execute anything: only three request kinds exist and all wait for a
  local human decision; accepting one only calls the existing, already-governed
  reminder/notification paths.
* No secrets in logs, events, errors or diagnostics: only device ids, fingerprints,
  counts, sequences and sizes.
* Honest limits: TLS-equivalent guarantees only apply to the *contents*; folder
  metadata, filenames and traffic volume are visible to the cloud provider hosting
  the folder; a compromised device with the identity file can impersonate it; the
  identity file is protected by filesystem permissions, not by an OS keystore yet.

## 8. Performance implications

Measured on Linux (CPython 3.13, this workspace; native Windows numbers still
pending), `tmp_path` filesystem, two paired devices, 30-iteration averages:

| Operation | Measured |
| --- | --- |
| `identity()` (load identity file) | 0.02 ms |
| `status()` (identity + counts + peers + folder probe) | 3.4 ms |
| `export()` empty snapshot, 1 recipient | 7.8 ms |
| `import_file()` of that bundle (verify + decrypt + apply 0 records) | 8.2 ms |
| `export()` with 200 reminders | 10.3 ms, 25 304 B bundle |
| `import_file()` applying 200 new reminders | 671 ms (3.3 ms per record, dominated by the per-record SQLite insert + proactive validation, not by crypto) |
| Per-record cost in a bundle (reminder) | ~127 B plaintext-equivalent, plus the 16 B AES-GCM tag |
| `tick()` (export + folder scan + imports) | 31 ms with 2 bundles in the folder |
| Empty-snapshot bundle on disk | 914 B |

No background thread, no timer, no network. `tick` is opt-in, bounded by
`PC_AGENT_SYNC_RETENTION_DAYS` and the number of `.pcasync` files, and skips
already-processed bundles before doing any crypto. Crypto itself is single-digit
milliseconds even at the 4 MB default cap; the only super-linear-looking number is
the deliberate per-record validation and durable insert, which is the same cost a
local user pays for the same reminder.

## 9. Limitations

* Shared-folder transport only; no NAT traversal, no push, no real-time delivery.
* Full-snapshot bundles; large reminder/flow sets are capped rather than chunked.
* Conflicts on reminders are skipped, not merged field-by-field.
* `newest` relies on peer clocks, bounded by the skew window.
* Windows end-to-end acceptance for this phase has not yet been run on a real
  Windows machine (same standing gap as Phases 12-15).

## 10. Next tasks

* **Phase 17** - accessibility, multilingual UI with RTL, performance/UX polish.
* **Phase 18** - final production hardening: CI, security/performance test gates,
  signed packaging, update/rollback rehearsal, complete documentation set.
* Sync follow-ups (not scheduled): key rotation, delta bundles, optional OS keystore
  for the identity file, Windows multi-device acceptance evidence.
