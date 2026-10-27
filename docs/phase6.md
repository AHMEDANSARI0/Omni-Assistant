# Phase 6 - Licensing and subscriptions (offline foundation)

Phase 6 adds commercially usable, **offline** licensing to the local core:

- Ed25519-signed license tokens that verify without any network call.
- Plans (`trial`, `standard`, `pro`, `enterprise`) mapped to real features.
- A built-in time-limited trial plus subscription-style expiring licenses and
  perpetual licenses.
- Optional per-machine binding using a one-way machine fingerprint.
- Customer activation from the CLI or the GUI Dashboard.
- A vendor-only issuing tool (`pc-agent-license-admin`).
- Opt-in enforcement (`off` / `warn` / `enforce`). The default remains **off**,
  so source/development installs and every earlier phase behave exactly as
  before until a commercial build turns licensing on.

There is still **no payment processing, store checkout, online activation
server, subscription billing/renewal automation, license revocation service
or cloud account** in this phase. Selling happens outside the app for now
(any storefront/invoice flow): the vendor issues a signed license file, the
customer activates it locally. Online coordination remains Phase 7.

## How a license works

A license is one text token:

```text
PCA1.<base64url canonical JSON payload>.<base64url Ed25519 signature>
```

The payload records the product id, license id, licensee, plan, issue/expiry
times (UTC), optional extra features, seats, an optional machine fingerprint
and the vendor `key_id`. The signature covers a fixed domain prefix plus the
canonical JSON, so editing any field (for example changing `standard` to
`pro`) invalidates it.

Verification is deterministic and local: token shape and size limits,
canonical re-encoding, known plan/feature names, signature against the
configured vendor **public** key, issue/expiry window (5-minute clock-skew
tolerance) and the optional machine binding. The pure-Python Ed25519 code in
`pc_agent.licensing.ed25519` is covered by the RFC 8032 test vectors and
rejects non-canonical points and out-of-range `S` values. Runtime only
verifies (public data). Signing is not constant-time in pure Python, so
private keys must only be used on a trusted offline vendor machine.

## Plans and features

| Feature key | Covers | trial | standard | pro | enterprise |
|---|---|---|---|---|---|
| `core.tasks` | `run`, `task run` | yes | yes | yes | yes |
| `core.recovery` | manual `task recover` | yes | yes | yes | yes |
| `documents` | direct `office`/`pdf` commands + GUI Documents | yes | yes | yes | yes |
| `web` | `web fetch` + GUI Web tab | yes | yes | yes | yes |
| `desktop` | `desktop info/open/open-url` + GUI Desktop | yes | yes | yes | yes |
| `gui` | opening the desktop GUI window | yes | yes | yes | yes |
| `providers` | `ask` and `--use-ai` planning | yes | yes | yes | yes |
| `tool_builder` | tool-builder mutations, dynamic invoke/enable/disable/rollback | yes | **no** | yes | yes |

A license may add individual extra features on top of its plan (additive
only). Read-only/status surfaces such as `doctor`, `license ...`, `providers
list`, `task list/show/diagnose`, `recovery scan`, `tools list/show`,
`tool-builder suggest/templates/candidates/show` and `demo` data are never
gated, so a customer can always inspect and fix their license state.

## Enforcement modes and the trial

Configured in `.env`:

```dotenv
PC_AGENT_LICENSE_ENFORCEMENT=off   # off | warn | enforce (default: off)
PC_AGENT_LICENSE_PUBLIC_KEY=       # vendor public key, base64url (never private)
PC_AGENT_LICENSE_KEY_ID=default    # must match the key id in issued licenses
PC_AGENT_LICENSE_FILE=data/license.key
PC_AGENT_TRIAL_DAYS=14             # 0-90; 0 disables the built-in trial
```

- `off`: license state is computed and shown, but nothing is ever blocked.
- `warn`: unlicensed features run, with a stderr/log warning.
- `enforce`: unlicensed features fail with an actionable error that names the
  feature and current state.

When enforcement is `warn`/`enforce` and no license is activated, the built-in
trial starts on first license evaluation, is stored in the local database, and
grants all current features for `PC_AGENT_TRIAL_DAYS`. With enforcement `off`
the trial never starts, so installing a development build cannot burn trial
days. The service also remembers the latest seen clock: moving the system
clock backwards by more than 24 hours makes license/trial time checks fail
closed instead of extending a term.

Honest limits of offline licensing: anyone with local admin control can edit
the database or patch the program. Phase 6 makes casual copying/tampering
fail deterministically; it is not DRM. Stronger anti-tamper and online
checks belong to later hardening/cloud phases and are not claimed here.

## Customer commands

```powershell
pc-agent license status            # state, plan, features, days remaining
pc-agent license status --json
pc-agent license machine-id        # one-way id for machine-bound licenses
pc-agent license activate --file C:\path\to\customer.license
pc-agent license deactivate        # asks for DEACTIVATE confirmation
```

Activation verifies first and only then replaces `data\license.key`
atomically; a bad or expired token never destroys the previously activated
license. In the GUI, the Dashboard **License** panel shows the same status
with **License Details**, **Activate License File...** (file picker) and
**Deactivate** buttons. Activation/deactivation events are audited in SQLite
(`license_events`) with only a SHA-256 fingerprint of the token, never the
token itself.

`doctor` and `pc-agent gui --check` each print a one-line license summary.
`doctor` stays usable when a license is expired or invalid so the problem can
be diagnosed.

## Vendor quickstart (do this on a trusted machine)

```powershell
# 1) Create the vendor signing keypair once. KEEP THE PRIVATE KEY SECRET.
pc-agent-license-admin keygen --key-id vendor-2026 --out-dir D:\vendor-keys

# 2) Put the two PUBLIC lines from license_public_key.txt into the customer
#    build's .env (public keys are safe to ship):
#    PC_AGENT_LICENSE_KEY_ID=vendor-2026
#    PC_AGENT_LICENSE_PUBLIC_KEY=...

# 3) Issue a one-year Pro subscription license:
pc-agent-license-admin issue `
  --private-key-file D:\vendor-keys\license_private_key.txt `
  --key-id vendor-2026 `
  --licensee "Acme Ltd" `
  --plan pro --days 365 `
  --out acme.license

# Optional machine binding: ask the customer for `pc-agent license machine-id`
# and pass the printed value as --machine-id VALUE_FROM_CUSTOMER.
# Perpetual licenses omit --days/--expires-at. Trial plans default to 14 days.
```

The private key can mint arbitrary licenses. Never store it in the customer
application folder, source control, chat or email. Customer production builds
should omit the `pc-agent-license-admin` launcher (Phase 8 packaging), and can
later embed the public key in `VENDOR_PUBLIC_KEYS` instead of `.env`.

## Data changes

- Version `0.8.0`; SQLite **schema v5** adds `license_state` and
  `license_events`. Existing tasks, recovery history and dynamic tools are
  preserved by the numbered migration.
- No new runtime dependency.
