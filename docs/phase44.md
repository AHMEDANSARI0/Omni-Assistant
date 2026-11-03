# Phase 44 — Linux Secret Service Credentials

Phase 44 gives the Phase 41 Linux install the same kind of OS-backed
provider-key storage that Phase 16 provided on Windows. Linux provider keys
can now live in the current desktop user's freedesktop Secret Service
collection (for example GNOME Keyring or KWallet's Secret Service provider)
instead of a plaintext `.env` line.

| Item | Value |
|---|---|
| version | 0.46.0 |
| backend | `linux-secret-service` via system `secret-tool` (libsecret) |
| new modes | `PC_AGENT_CREDENTIAL_STORE=auto|windows|linux|env` |
| secret transport | stdin to `secret-tool store`; never an argv value |
| registered tools | 62 built-ins; no new tool |
| database schema | v6; no migration |
| new Python dependency | none |

## Selection and resolution

The existing resolution order is unchanged:

1. a real environment variable or `.env` value;
2. the selected OS credential store;
3. missing.

`auto` now selects Windows Credential Manager on Windows and Linux Secret
Service on Linux. Other platforms report `unsupported-platform` and continue
with environment values only. `linux` selects the Linux backend explicitly;
`env` disables all OS-store reads. Existing `auto`, `windows`, and `env`
installations remain compatible.

Doctor, provider lists, diagnostics, and release audit expose only the
backend/provider/source names. A Linux-stored effective key is labelled
`linux-secret-service`; no command prints its value.

## Secret Service mapping

Each provider is one Secret Service item with the stable attributes:

```text
application=self-healing-pc-agent
provider=<canonical-provider>
purpose=provider-api-key
```

The backend invokes `secret-tool` as an argv array with a bounded timeout:

- a `search` for an intentionally unsupported probe provider checks service
  reachability without reading a real key;
- `lookup` returns no key when libsecret uses its documented empty-stderr
  “no matching item” result;
- `store` receives the exact validated key on stdin, without adding a
  newline, and the backend reads it back before reporting success;
- `clear` is accepted only after a follow-up lookup confirms removal.

`secret-tool` stderr is deliberately not copied into exceptions or status
text because external-process diagnostics are not a safe place to echo
credential-adjacent data. Service errors, oversized/invalid stored values,
and timeouts fail closed with generic credential errors.

## Linux use

Install the distribution package that provides `secret-tool` ( commonly
`libsecret-tools`) and sign in to a desktop session with a running Secret
Service provider. The application never downloads or installs it. Then use
the same direct commands as Windows, one at a time:

```sh
pc-agent credentials status
pc-agent credentials set gemini
pc-agent credentials migrate
pc-agent credentials remove-env-keys --dry-run
```

`credentials set` still uses a hidden prompt and has no command-line key
argument. `credentials migrate` does not edit `.env`; Phase 39 cleanup can
remove a duplicated `.env` line only after the identical value verifies in
the Secret Service.

## Limits

- A headless host without a session D-Bus/Secret Service provider reports the
  backend unavailable and keeps environment behavior.
- A locked collection may make the OS prompt to unlock or make an operation
  time out; the agent does not bypass the lock.
- Applications running as the same Linux desktop user may be able to request
  the item if they know its attributes. This is the normal Secret Service
  user-session boundary, not protection from malware already running as
  that user.
- Automated tests use injected/fake `secret-tool` processes and do not read
  a developer's real keyring. The Linux verification host used for this
  phase had no live Secret Service session, so live GNOME Keyring/KWallet
  evidence is not claimed.
- macOS Keychain is not implemented in this phase, and no desktop
  automation parity is claimed.

## Phase 54 follow-up — **Live GNOME Keyring/KWallet Evidence**

Phase 54 supplies the live evidence path not claimed above:
`pc-agent credentials linux-secret-service-acceptance` identifies the
session-bus owner of `org.freedesktop.secrets`, accepts only GNOME
Keyring or KWallet/KSecretService, round-trips/deletes a unique isolated
random probe, and proves real provider keys unchanged without recording
any secret. Its report can be revalidated offline. See
`docs/phase54.md`.
