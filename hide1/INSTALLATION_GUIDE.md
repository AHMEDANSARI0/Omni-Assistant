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


## 12. Command Center UI (0.82.0)

The new interface ships **pre-built** inside the package: `src/pc_agent/webui/`
contains `index.html` and 21 asset files. You do not need Node.js to use it.

```powershell
.\.venv\Scripts\Activate.ps1
pc-agent ui
```

That starts the local API and opens the Command Center. If the browser does not
open by itself, use `pc-agent api serve` and visit `http://127.0.0.1:8765`. `/legacy` still opens the pre-0.82 panel if you
prefer it or need to roll back; `pc-agent gui` still opens the desktop Tkinter GUI.

If the page says the UI is not built, the bundle is missing from your install
(an older tree or a partial copy). Fix it from the sources that ship alongside:

```powershell
cd D:\omni-assistant\frontend
npm ci
npm run build
```

Node 20 or newer is required for that step only. After it finishes, reload
`http://127.0.0.1:8765`.

Common points:

* **Blank page / 404 on `/assets/...`** - the build did not finish. Re-run
  `npm run build`; it cleans stale hashed files before bundling.
* **"Not authorised" panels** - you opened a saved copy of the page or used a
  different port. Reload from `pc-agent api serve`; the token is injected per
  page load and is not stored in a URL.
* **A panel says "unavailable"** - that is the measured truth (no GPU, no speech
  engine, connector gate closed, no provider key). The panel prints the reason and
  the install step; it is not a broken UI.
* **Tauri desktop shell** - `frontend/src-tauri/` is a scaffold. Building it
  requires the Rust toolchain, WebView2 and an icon set, and is not covered by
  this release's verification. Use the browser path above.

### 12.1 Which command opens which interface (0.82.3)

| You type | You get |
|---|---|
| `pc-agent ui` | Command Center as a **desktop window** (API started for you) |
| `pc-agent gui` | the same window - this is the default |
| `pc-agent ui --browser` | the same UI in an ordinary browser tab |
| `pc-agent ui --explain` | tells you which window it would use, opens nothing |
| `pc-agent gui --tk` | the classic Tkinter panel (kept for rollback) |
| `pc-agent api serve` | the API only; open the printed address yourself |
| `http://127.0.0.1:8765/legacy` | the pre-0.82 web panel |

**How the window is chosen** (first one available wins):

1. **pywebview** - install it once with `python -m pip install -e ".[desktop]"`.
   On Windows this uses the WebView2 runtime that Windows already has, so the
   window is a real application window, not a browser.
2. **Edge / Chrome / Brave / Chromium in app mode** - nothing to install; Edge
   is part of Windows. The window has no address bar and no tabs, and it runs in
   its own profile (`data\\ui-window`), so your normal browser session is
   untouched and closing the Command Center does not close your other tabs.
3. **Your default browser** - only if neither of the above exists.

`Ctrl+C` in the terminal (or closing the window after `--duration`) stops the
server and closes the window it opened.

If you ever want the old window back, `pc-agent gui --tk` is the whole rollback -
nothing was deleted.

### 12.2 Right-to-left layout

Settings → Display → **Text direction**: `auto` (follows the locale the engine
reports), `ltr`, or `rtl`. The choice is stored in your browser and applied
before the first frame. The UI's own wording stays English; the direction only
mirrors the layout.

### 12.2b One-command verification on Windows

Instead of running five commands and reading five outputs, run this from the
install root:

```powershell
python scripts\verify_windows.py
```

It runs, in this order: the interpreter and version, **which** `pc_agent` was
imported (and from where), `ruff`, `doctor`, `release audit`,
`scripts/check_desktop_shell.py`, the full `pytest -q` suite, and
`release production-check`. Each gate keeps its exit code and the tail of its
output, and the result is written to `data\windows-verification.json`.

It prints `WINDOWS VERIFICATION: PASS` or `FAIL (suite)` and, on failure, the
tail of the failing gate - so the reason is on screen instead of buried.

| Flag | Effect |
|---|---|
| `--only doctor,audit` | run just those gates |
| `--skip-suite` | everything except the full suite (fast) |
| `--json` | machine-readable report on stdout |
| `--report-dir D:\somewhere` | write the report elsewhere |
| `--no-report` | do not write a file |

Nothing is silently skipped: the installer-signing check appears as `N/R` with
the reason and the exact next command, and a gate that cannot start is a
failure, not a note.

### 12.2c Building the installer with one command

`pc-agent release installer` does the three things the manual steps did, and
writes down what it did.

Check first - this runs nothing that installs or compiles:

```powershell
pc-agent release installer --check
```

It reports, with versions, whether `node`, `npm`, `cargo` and `rustc` were found,
whether the WebView2 runtime is installed, whether `tauri.conf.json` points at a
built UI, and whether an installer already exists in `bundle\nsis` /
`bundle\msi`. Anything missing is listed as a blocker with the fix, and the exit
code is `1`.

Build:

```powershell
pc-agent release installer --build
```

That runs `npm ci` and then `npm run tauri:build` as argv lists (no shell), keeps
the tail of each output, and lists the installers it found.

Build and sign in one step:

```powershell
pc-agent release installer --build --sign --thumbprint <your-thumbprint>
```

Signing requires a certificate (`--thumbprint` from
`pc-agent release signing --certificates`, or `--pfx`); without one the command
refuses with exit `2` before doing anything. With one, it signs and verifies both
the NSIS `.exe` and the MSI, exactly like `release sign-bundle`.

Every run appends its outcome to `data\installer-build.json` (steps, timings, the
output tails, artifacts, blockers and signing results). `--json` prints the same
report, `--report-dir` writes it elsewhere, `--timeout` bounds each npm step.

### 12.2d Signing, and what a signature does not buy you

Signing the installer is necessary, not sufficient. Read
[`docs/smartscreen_reputation.md`](docs/smartscreen_reputation.md) once, because
it is the difference between *signed* and *SmartScreen stops warning*:

* **no certificate** - "Windows protected your PC", *Unknown publisher*;
* **OV** - your publisher name is shown, but SmartScreen may still warn until
  reputation accrues over downloads;
* **EV** (hardware key) - typically trusted immediately.

Ask this machine where it stands:

```powershell
pc-agent release signing --reputation
```

It prints the certificates found, whether each key is hardware-backed, the last
recorded signing run, each installer's verification result, the timestamp
authorities that will be tried, and everything still blocking a trustworthy
release. SmartScreen reputation is reported as `not-measurable-here`, because it
is Microsoft's system and buying or faking it is not on the table.

Every signature is `sha256` **with an RFC 3161 timestamp**, and if the first
authority fails the next one is tried (`digicert` -> `sectigo` -> `globalsign`),
with each attempt recorded. Without a timestamp a signature dies with the
certificate, and every installer you shipped starts warning again.

### 12.3 Building the desktop installer (optional, own machine)

`pc-agent ui` gives you a desktop *window* today. A real installer (Start-menu
entry, own icon, no Python visible) is the Tauri bundle in
`frontend/src-tauri/`. It is **not built or verified by this release** - the
development machine had no Rust toolchain or WebView2 - so build it yourself:

Prerequisites: **Node 20+**, **Rust (stable, MSVC toolchain)**, **WebView2**
(already present on current Windows 10/11), and the *Visual Studio C++ build
tools* that rustup offers to install.

First check the shell definition (uses only Python, runs in a second):

```powershell
cd D:\omni-assistant
```

```powershell
python scripts\check_desktop_shell.py
```

It must print `DESKTOP SHELL: READY` and list every icon it validated. If it
prints `NOT READY`, it names the exact problem (wrong `frontendDist`, missing
icon, remote URL, version mismatch) - fix that before spending ten minutes on a
Rust compile.

Then build:

```powershell
cd D:\omni-assistant\frontend
```

```powershell
npm ci
```

```powershell
npm run tauri:build
```

Output lands in `frontend\src-tauri\target\release\bundle\`:

| Folder | File |
|---|---|
| `nsis\` | `Omni-Assistant Command Center_0.82.x_x64-setup.exe` |
| `msi\` | `Omni-Assistant Command Center_0.82.x_x64_en-US.msi` |

The installer is **unsigned** when it leaves the build. Signing it needs your
own certificate: this project never generates, stores or transmits a signing
key, and no signing secret exists in CI.

The whole runbook is in [`docs/authenticode_signing.md`](docs/authenticode_signing.md).
In short, from the repository root:

```powershell
pc-agent release signing --certificates
```

That lists the code-signing certificates in `Cert:\CurrentUser\My` with their
thumbprint and whether a private key is present, then prints the two lines to
paste. Set the thumbprint it printed:

```powershell
$env:PC_AGENT_SIGN_THUMBPRINT = "<the thumbprint it printed>"
```

Check every prerequisite without signing anything:

```powershell
pc-agent release sign-bundle --check
```

It must print `READY`; otherwise it names the exact blocker (no installer, no
`signtool.exe`, no certificate) and exits `1`. Then sign **and verify** both
installers - the `.exe` and the `.msi`:

```powershell
pc-agent release sign-bundle
```

Each file is signed with `sha256` plus an RFC 3161 timestamp and then verified
through the Windows trust API, so "signed" in the report means Windows would
actually trust it. The report is written to `data\installer-signing.json`; the
command exits `1` if any installer is unsigned or does not verify.

A PFX works too (`--pfx`), but its password is read **only** from
`PC_AGENT_SIGN_PFX_PASSWORD` - never from the command line.

To confirm afterwards, by hand:

```powershell
Get-AuthenticodeSignature .\frontend\src-tauri\target\release\bundle\nsis\*.exe | Format-List
```

Uninstall is the normal Windows entry; your `data\` and `workspace\` folders are
not part of the bundle and survive an uninstall.
