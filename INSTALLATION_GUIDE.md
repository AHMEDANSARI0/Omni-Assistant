# Installation guide

Windows-first, local-first. A fresh install works **offline**, needs **no
account** and **no paid provider**. Everything below was run on the verification
host at version 0.80.1.

---

## 1. Requirements

| Item | Minimum | Notes |
| --- | --- | --- |
| Windows | 10 (1909+) or 11 | primary target |
| Python | 3.11 | 3.13 verified; must be the same interpreter you build with |
| Disk | ~400 MB | install + venv + dependencies |
| RAM | 4 GB | 8 GB for local vision or large PDFs |
| Optional | Ollama | for fully local models; not required |

Linux/macOS are supported for development and packaging (POSIX source, Debian
and RPM recipes) but are not the shipping target for this phase.

## 2. Install from a portable bundle (recommended)

1. Copy `omni-assistant.zip` and any `.sig.json` file to the machine.
2. If a signature file is present, verify **before** extracting:

```
python -m pc_agent.cli update check omni-assistant.zip
```

3. Extract to a folder you control, for example `D:\omni-assistant`.
4. Create the environment and install:

```
cd D:\omni-assistant
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

5. Confirm the install:

```
pc-agent --version
pc-agent doctor
pc-agent release audit
```

`doctor` prints one line per subsystem; `release audit` must end with
`READY; failures=0`.

### Upgrading from an older tree

Use the cumulative patcher for the current phase, then re-install the package:

```
node TRANSFORMATION_PHASE_18_HARDENING.mjs
```

```
.\.venv\Scripts\Activate.ps1
```

```
python -m pip install -e ".[dev]"
```

The patcher writes only files it knows, never touches `.env`, `data/` or
`workspace/` contents, prints a per-file summary, and is idempotent: a second run
reports `created=0 updated=0`.

## 3. First run

```
pc-agent run "Summarise my Downloads folder"
pc-agent missions list
pc-agent gui
```

The dashboard has 20 tabs; the **Dashboard** tab shows live state and the
**Production** tab can run the release chain check from the UI.

A local model keeps everything offline (optional):

```
ollama pull llama3.2
setx PC_AGENT_PROVIDER ollama
pc-agent doctor
```

Cloud providers are optional and their keys go into the OS credential store, not
into a file:

```
pc-agent credentials set openai
```

## 4. Configuration

All configuration is `PC_AGENT_*` environment variables or `.env` (never
committed). Start from the example:

```
copy .env.example .env
```

Values are validated at load: an unknown choice, an out-of-range number or a
malformed key fails loudly with the accepted values instead of silently defaulting.

Important gates that default to **off** and must be enabled deliberately:

| Gate | Enables |
| --- | --- |
| `PC_AGENT_ALLOW_BROWSER` | browser automation |
| `PC_AGENT_ALLOW_DESKTOP` | desktop automation |
| `PC_AGENT_ALLOW_PLUGINS` | third-party plugins |
| `PC_AGENT_ALLOW_SYNC` | encrypted device sync |
| `PC_AGENT_ALLOW_CONNECTORS` | calendar/drive/email/chat/webhook |
| `PC_AGENT_VOICE_MIC_ENABLED` | microphone (push-to-talk) |

## 5. Verify the install

```
pc-agent release audit
```
```
pc-agent release production-check
```
```
python -m pytest -q
```

Expected: `READY; failures=0` (audit), `READY; steps=10; passed=10` (chain
check), and a full green suite. The chain check builds, signs, applies and rolls
back a release **inside a temporary folder** — your install is not modified.

## 6. Uninstall

```
pc-agent update rollback latest-applied     # if you applied an update you regret
```

Then delete the folder. User data lives in `data/` (database, settings, ledgers)
and `workspace/` (your files); remove them explicitly if you want a clean slate.
On Windows, a scheduled auto-upgrade task is removed with:

```
pc-agent update auto-uninstall-schedule
```

## 7. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `pc-agent: command not found` | venv not active | activate `.venv`, or run `python -m pc_agent.cli` |
| `Unauthorized` from the local API | token not passed | read `data/api-token`, send `Authorization: Bearer <token>` |
| Plugin tool returns a sandbox start error | the host cannot launch a Python child process, or the interpreter is an unusable shim | re-install the venv, check `pc-agent plugins status` |
| `release audit` warns `transformation_baseline` | Windows evidence not recorded | run the suite on Windows and record the output |
| GUI will not open | no desktop session (headless) | use `pc-agent gui --check`, or run on a desktop |
| Microphone never records | default-off gate | set `PC_AGENT_VOICE_MIC_ENABLED=1` and consent in the Voice tab |
| Anything else | collect evidence first | `pc-agent diagnostics export --out diagnostics.json` |

Diagnostics never include secrets: keys appear as `configured=yes`.

## 8. Security notes for the installer

* Nothing is installed as a service; the local API binds loopback only.
* A bearer token is generated on first use (`data/api-token`) and is required for
  every route except `/health`.
* Background auto-upgrade is OS-scheduled and only applies **signed** bundles.
* `data/`, `workspace/`, `.env` and caches are excluded from every release
  artefact by an allow-list, so a bundle cannot leak your state.

## 9. Post-install Windows acceptance (recommended, one command)

`pc-agent release evidence` runs the acceptance gates on this machine and writes
`workspace\windows-acceptance.json`. Then `pc-agent release evidence record`
records it, and `pc-agent doctor` shows
`Hardening: ...; windows-acceptance=verified:<date>`. Until that record exists the
status stays `pending_user_windows_evidence`, everywhere, on purpose.

## 10. Signing the installer (Authenticode)

Two different signatures exist in this product, and they are not interchangeable:

| Signature | Who makes it | What it protects | Status |
| --- | --- | --- | --- |
| Update bundle (Ed25519) | the project's update key | the update the app applies | **always enforced** (`pc-agent update verify`, chain step 6) |
| Authenticode (installer EXE) | **your** code-signing certificate | what Windows/SmartScreen evaluates before install | optional, tool shipped in 0.81.0 |

Nothing in this product generates a code-signing key. To sign the installer you
supply a certificate you already own.

### Check what this machine can do

```
pc-agent release signing
```

It reports the `signtool` it found (PATH or the Windows SDK), whether a
certificate is configured, the timestamp authority, and the last recorded result.

### Sign with a certificate thumbprint (recommended - no secret on the command line)

```
set PC_AGENT_SIGN_THUMBPRINT=YOUR_CERTIFICATE_THUMBPRINT
```

```
pc-agent release sign "D:\build\omni-assistant-setup.exe"
```

Then, at any time, on any machine that has the file:

```
pc-agent release signature "D:\build\omni-assistant-setup.exe"
```

Verification does not trust a log line: on Windows it calls the operating system
API `WinVerifyTrust`, and only reports `TRUSTED-SIGNED` when Windows itself
accepts the chain. On a non-Windows host it answers `UNVERIFIABLE` - it does not
guess.

A timestamp is requested by default (`--timestamp-url` to change it), because a
timestamp is what keeps the signature valid after the certificate expires.

### PFX fallback, and its honest cost

```
set PC_AGENT_SIGN_PFX_PASSWORD=...   (never as a command-line argument)
```

```
pc-agent release sign "D:\build\omni-assistant-setup.exe" --pfx "D:\keys\codesign.pfx" --allow-argv-password
```

`signtool` accepts a PFX password only as `/p <password>` in `argv`, and `argv`
is readable by other processes on the same machine. That is why the flag is
required: without it the command is **refused** with that explanation. Use the
thumbprint mode unless you have no choice; the password is scrubbed from every
captured output and from the stored report either way.

### What the audit says about it

`pc-agent release audit` runs check **33** `windows_authenticode`:

* no certificate configured → `PASS` with the detail "the installer is unsigned",
  because that is a scope decision, not a broken build;
* a recorded signing attempt that does not verify → `WARN` with the reason;
* signed and verified → `PASS` with the verification text.

## 11. "No module named 'cryptography'" - use the project interpreter

That error means the **wrong** `pc-agent` ran: the global one in
`%LOCALAPPDATA%\Programs\Python\Python311\Scripts`, where this project's
dependencies are not installed. Two facts make it obvious - the traceback path
(`...\Python311\Scripts\pc-agent.exe`) and the `pc-agent --version` output.

Fix it either way:

```
.\.venv\Scripts\Activate.ps1
pc-agent gui
```

```
.\.venv\Scripts\python.exe -m pc_agent gui
```

Since 0.81.2 the command itself says all of this instead of printing a traceback.
If the venv is genuinely missing a dependency, repair it with:

```
python -m pip install -e ".[dev]"
```

