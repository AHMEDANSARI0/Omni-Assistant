# PHASE 16 - Windows Credential Manager Storage

Phase 16 lets provider API keys live in the current Windows user's
Credential Manager instead of remaining as plaintext lines in `.env`.
Existing `.env`/environment keys keep working and take precedence, so this
is an opt-in migration rather than a breaking configuration change.

## What is stored

One Windows **generic credential** is used per provider slot:

- `gemini`
- `openrouter`
- `openai`
- `openai-compatible` (the CLI also accepts `openai_compatible`)
- `ollama`
- `anthropic` (reserved in Phase 16; direct adapter added in Phase 19)

The target names begin with `Self-Healing PC Agent/provider-api-key/`.
Credentials use `CRED_PERSIST_LOCAL_MACHINE`: they persist for later logon
sessions on this computer and are not enterprise-roamed by the agent.

License files, browser passwords, desktop passwords and arbitrary user
secrets are not stored by this phase.

## Resolution order

For each provider:

1. A real environment variable or `.env` value wins.
2. If that value is absent, Windows Credential Manager is consulted.
3. If neither exists, the provider is not configured (Ollama can still work
   without a key where its endpoint does not require one).

`PC_AGENT_CREDENTIAL_STORE` controls lookup:

| Value | Meaning |
|---|---|
| `auto` (default) | Use Windows Credential Manager on Windows; environment-only elsewhere |
| `windows` | Select the Windows backend explicitly; it reports unavailable off Windows |
| `env` | Never read the OS credential store |

Doctor and `pc-agent providers list` show only the effective **source**
(`environment`, `windows-credential-manager` or `missing`). They never
print a key.

## Recommended migration

Run every command separately.

First confirm the existing environment route is visible:

```powershell
pc-agent credentials status
```

Copy the currently loaded environment/`.env` keys into Credential Manager:

```powershell
pc-agent credentials migrate
```

Approve the exact provider list shown. Or migrate one provider:

```powershell
pc-agent credentials migrate gemini
```

The migration command **does not edit `.env`**. Verify the route:

```powershell
pc-agent providers list
```

Then manually remove the migrated `*_API_KEY` lines from `.env`. Run
Doctor again:

```powershell
pc-agent doctor
```

The credential-store line should list the providers as stored, and their
effective source should become `windows-credential-manager` after the
environment lines are removed. Finally, run one normal provider check such
as:

```powershell
pc-agent ask "Reply with exactly: credential test successful"
```

## Store a key directly

The key is requested twice through a hidden prompt. It is never accepted
as a command-line argument, so it does not enter PowerShell history through
this command:

```powershell
pc-agent credentials set gemini
```

Keys must be one line, contain no whitespace/control characters and be at
most 1024 characters. Windows generic credential blobs are capped by the
agent at the Win32 2560-byte limit; keys are encoded as UTF-16LE without a
trailing NUL.

## Inspect or delete stored names

List stored/effective state without values:

```powershell
pc-agent credentials list
```

Delete one exact stored key after confirmation:

```powershell
pc-agent credentials delete gemini
```

Deleting a stored key does not delete or change an environment/`.env` key.
If an environment key remains, the provider will continue using it.

## Security properties and limits

- The native implementation calls `CredReadW`, `CredWriteW`, `CredDeleteW`
  and `CredFree` in Windows `advapi32.dll` through `ctypes`; no new Python
  package stores or transmits secrets.
- No credential command prints, logs or returns a secret in JSON/status
  output.
- There is intentionally **no `credentials get/show` command** and no
  credential tool exposed to tasks or AI planning.
- GUI Dashboard/Providers screens display store/source status only. Secret
  entry and deletion remain CLI operations.
- Credential Manager protects keys under the Windows user account. Other
  programs running as the same Windows user may still be able to request a
  generic credential if they know its target; this is an OS user-account
  boundary, not protection from malware already running as that user.
- Automated tests force `PC_AGENT_CREDENTIAL_STORE=env` unless a test
  explicitly injects a fake/simulated backend, so the suite does not read a
  developer's real vault.
- macOS Keychain and Linux Secret Service are not implemented in this
  Windows-first phase.

## 16.1 hotfix

On a real Windows test run, the simulated `advapi32` backend's
`ERROR_NOT_FOUND` could be masked because `ctypes.get_last_error()` exists
on Windows and returned 0 for the Python test double. An injected
library's own `last_error` is now checked first; production calls through
the real `advapi32.dll` handle still use ctypes' `use_last_error` capture.
The live GUI test also now expects eight notebook tabs, including the
Phase 15 Images tab.
## Phase 19 note

The stored `anthropic` credential is now consumed by the direct Anthropic
Messages API adapter. The Phase 16 resolution order and non-secret output
rules are unchanged. See `docs/phase19.md`.
