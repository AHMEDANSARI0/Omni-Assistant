# Changelog

## 0.82.12 - the certificate truth: a purchase guide, and a claim this project got wrong

The BARA item is a certificate, and a certificate cannot be generated here - it is
an identity document issued by a certificate authority to a verified person. So
this release does the two things that *can* be done: it tells you exactly which
product to buy from Pakistan, and it fixes a claim this project was making that
stopped being true.

**The correction.** `pc-agent release signing --reputation` said:

```
expected first run: EV: typically trusted immediately
```

and `docs/smartscreen_reputation.md` said EV "buys you reputation from day one".
**Microsoft removed the EV SmartScreen bypass in 2024.** EV- and OV-signed files
now build reputation the same way, so that line was telling a user to spend
2-3x more for a benefit that no longer exists. Both the code and the document now
say what is true: a certificate shows your publisher name and starts a reputation
history; nothing bypasses the warning dialog any more. `hardware_backed` is
described as what it is - post-June-2023 CA/Browser Forum compliance, true of EV
*and* OV - instead of as "the EV signal".

**The guide.** `docs/certificate_purchase_guide.md`:

* Azure Artifact Signing (~$9.99/month, no USB token) is **not available** from
  Pakistan: public-trust certificates cover organisations in the USA, Canada, the
  EU and the UK, and individuals only in the USA and Canada. Saying so saves a
  wasted afternoon.
* The realistic path is an **individual/OV certificate** ($150-300/year, cloud HSM
  or USB token, 1-7 business days), with the CA's actual document list (passport
  or CNIC, proof of address, a callable number, DUNS for OV) prepared beforehand.
* Why EV is no longer the smart upgrade, and when it still is (kernel-mode
  drivers, enterprise contracts).
* Why a **timestamp** keeps the signature valid after the certificate expires.
* What to do on the day the token arrives, using the commands this product already
  implements, ending with `signtool verify /pa /all` as the independent check.
* An explicit "if you decide not to buy one yet" section: everything works
  unsigned, and `--reputation` will keep saying so.

**Tests.** Two new tests pin the correction (`windows_signing.py` may not contain
"trusted immediately"; the document may not contain "Typically trusted
immediately") and check the guide: it must mention the 2024 change, the
US/Canada-only restriction, Pakistan, the "does not buy" section, and the three
product commands - with **no angle-bracket placeholders** in any copyable command.

## 0.82.11 - the installer build path: one command, and it cannot cost you the UI

This is the window-to-installer step. The window already works (`pc-agent gui`),
but shipping a `.exe` needs Node, Rust and WebView2, and the build has a
destructive edge that was not handled.

**The hazard this release removes.** `npm run build` deletes
`src/pc_agent/webui/assets` and `index.html` *before* it compiles, and
`tauri:build` runs that same command. A type error, a missing `node_modules`, a
Ctrl+C, or a Rust compile error therefore left the Python package with **no
Command Center at all**: the server stayed up and served a 404 where the app
should have been. It bit this project once already. `pc-agent release installer
--build` now snapshots that bundle first and puts it back when the build fails,
reporting `ui_bundle_restored`, and it deletes the snapshot after a good build.
`webui/__init__.py` is never touched.

**The runtime got an honest answer.** The Tauri window is only a window: it runs
`python -m pc_agent api serve` (honouring `OMNI_PYTHON`) and waits for the
loopback origin. On a PC with several interpreters, `python` is a coin flip, so
`--launcher` writes `Omni-Assistant.cmd` that pins `OMNI_PYTHON` to the
interpreter that actually imports this package, then starts the built `.exe` if
it exists and otherwise falls back to the built-in window. The `runtime` step in
the report states this instead of implying the exe is self-contained. It writes
no credentials, and it is CRLF because cmd.exe wants that.

**The command now tells you what to type.** Blockers were already facts, not
advice; now `--check` ends with the ordered, deduplicated fixes
(`winget install --id Rustlang.Rustup ...`, then `--check`, then
`--build --launcher`), with a single line for Rust even though `cargo` and
`rustc` are two separate blockers.

**Also:** `npm ci` is used when `frontend/package-lock.json` exists and
`npm install` otherwise (a tree without a lockfile used to fail with npm's own
cryptic message), and `npm` failing now skips the Tauri build instead of running
it anyway.

**Tests.** `tests/test_installer_build_path.py` (18) - lockfile choice, restore
after a failed build, no restore after a good one, no snapshot when there is no
bundle, `npm` failure skipping Tauri, launcher content (pin, exe branch,
fallback, CRLF, spaces in path, no credentials), the runtime step, and the
ordered fix commands. `--check` on this Linux machine still honestly reports
`FAIL toolchain: missing cargo, rustc` - this release does not claim a Rust build
happened anywhere.

## 0.82.10 - a closed Command Center window was reported as a server error

Found by simply running the app: `pc-agent gui` opened the window, then the
console filled with this every couple of minutes.

```
ERROR | pc_agent.api | Local API GET /v1/events/stream failed
ConnectionAbortedError: [WinError 10053] An established connection was aborted
  ... api_server.py:363 in _stream_events  self.wfile.write(b'{"type":"stream.heartbeat"}\n')
Exception occurred during processing of request from ('127.0.0.1', 51413)
```

**Cause.** `_stream_events` and `_dispatch` caught `BrokenPipeError` and
`ConnectionResetError` - the two errors POSIX raises when a reader disappears.
Windows raises `ConnectionAbortedError` (WinError 10053) instead, plus a bare
`OSError` on some Winsock paths. So a perfectly normal event - the browser
dropping the stream: the two-minute bounded stream ending, a reload, the window
closing, or `EventSource` reconnecting - fell through to the generic
`except Exception`, which logged at ERROR **and tried to send a 500 on the dead
socket**. That second write is the "Exception occurred during processing of
request" traceback; `socketserver` printed it because nothing swallowed it.

This is a Windows-only bug by construction: on Linux the same abort raises
`BrokenPipeError`, which was handled, so no test ever saw it.

**Fix.**

* `_CLIENT_GONE` = `BrokenPipeError, ConnectionResetError, ConnectionAbortedError`
  and `_DISCONNECT_ERRNOS` (EPIPE, ECONNRESET, ECONNABORTED, WSAECONNABORTED,
  WSAECONNRESET, ENOTCONN, ESHUTDOWN) cover the specialised classes and the bare
  `OSError` variants. Both stream writers now end quietly on any `OSError`.
* `_dispatch` treats a client hang-up as a debug line, and the 500 reply is
  itself guarded, so a failing request can no longer produce two tracebacks.
* New `_QuietThreadingHTTPServer` overrides `handle_error`: a hang-up is a debug
  line, anything else still goes to `socketserver`'s normal traceback. The local
  server no longer shouts at a user who closes a window, but it still reports
  real faults.

**Tests.** `tests/test_event_disconnect_is_quiet.py` (9) pins the four error
shapes Windows/POSIX raise, asserts the stream and dispatch paths stay silent,
asserts a genuine `RuntimeError` still yields a 500 **and** an ERROR log, and
that the server's `handle_error` override only silences disconnects. Against
0.82.9 exactly three of them fail with the reported traceback, which is how the
regression test was written.

**Not changed.** The stream stays bounded (120 s default, 900 s max), heartbeats
stay at 10 s, and nothing is retried server-side: reconnection is the browser's
job and `EventSource` already does it.

## 0.82.9 - one missing template file was breaking four installers

This release exists because a real Windows run failed 29 tests, and 27 of them
were the same file.

**What happened.** `.env.example` was not in `D:\omni-assistant`. That file is not
documentation: the Windows installer, the POSIX package, and therefore the Debian
and RPM packages, all require it as a payload file, because a package has to ship
the template the user copies to `.env`. With it gone:

* 27 tests failed across phases 24, 38, 41, 46, 47, 48, 50, 51, 52 and 57 - every
  one with "missing required files: .env.example", none saying what to do;
* `pc-agent release audit` still reported **READY**, so the one command a user
  runs to check their install said everything was fine.

That is a bad failure mode: a single 8 KB sample file, one error message repeated
twenty-seven times, and no stated fix.

**The fix, in three parts.**

1. **The template now lives in code.** `pc_agent/env_template.py` holds the exact
   shipped text as `ENV_TEMPLATE`, and both payload resolvers restore a missing
   `.env.example` before they complain about anything else - with a warning naming
   the path. Restoring it can never leak anything: it is the sample file, and the
   suite asserts every `*_KEY=` / `*_TOKEN=` line in it is empty.
2. **It is checkable on demand.** `pc-agent release env-template --check` reports
   present/missing and exits `1` when missing; `--write` restores it; `--json` is
   machine-readable. A file that four builders depend on should not be invisible.
3. **It is now actually audited.** `pc-agent release audit` gained check 34,
   `installer_payload`: all 19 required payload files, by name, and the command
   that restores the template. `scripts/verify_windows.py` gained a `payload` gate
   that runs before the four-minute suite, so this class of breakage is one line in
   the verification report instead of nineteen puzzle pieces in pytest output.

Self-healing is deliberately narrow: only `.env.example` is restored. A missing
`scripts/install_posix.sh` still fails - but the message now ends with how to
recover ("re-run the installer patcher", plus the `env-template --write` command),
and a test pins that wording so it cannot rot.

**The other two failures in that run were the older, already-fixed ones:**
`WinError 10038` from the API server shutdown race and the POSIX-literal argv
assertion both come from a tree that does not contain 0.82.6, which is why the
same run also produced the old `assert '\\opt\\chrome' == '/opt/chrome'`. The
version numbers matched because that tree is internally consistent - it is simply
older than the fixes. `python -c "import pc_agent; print(pc_agent.__version__)"`
answers that question in one second, and `scripts\\verify_windows.py` answers it
with the import origin.

**Numbers.** Suite **1298 passed, 2 skipped** on Linux; `ruff` clean; release
audit READY (**34** checks); allow-list **490** files.

## 0.82.8 - signing for EV / hardware keys, timestamps that survive, and SmartScreen told honestly

The last BARA item. Three parts, and the third is the one that matters most,
because it refuses to sell an outcome.

**EV / post-2023 OV certificates: the key must stay on hardware.**

Since June 2023 a publicly-trusted code-signing key has to live in a token, an HSM
or a TPM-backed container. That makes "export to PFX" not just risky but
forbidden - and it makes the "which certificate do I even have" question
decisive. So:

* `pc-agent release signing --certificates` now also reports **where each key
  lives** (`hardware-token` / `software-store` / `unknown`), the provider name,
  and the key type, and it counts the hardware-backed ones;
* `pc-agent release sign-bundle --require-hardware` enforces the rule: a PFX is
  **refused** with the reason (it is an exportable copy of a key you may not
  copy), a hardware-backed certificate is accepted with the provider named, and a
  software-stored key is **blocked** with "check the token is plugged in and its
  middleware is running";
* when the metadata cannot be read - a custom CNG/KSP provider, an HSM that does
  not expose the legacy CSP fields, or any non-Windows host - the result is a
  **note**, not a pass. Claiming "hardware confirmed" for something this product
  cannot inspect would be a guess about someone's signing identity.

Nothing in that path exports, reads or unlocks a private key: it reads the
provider name and the `HardwareDevice` flag, both public metadata.

**Timestamps: the difference between a signature and a signature that expires.**

Every signature was already `sha256` + RFC 3161, but a timestamp *authority* can
be down, rate-limited or unreachable - and an untimestamped signature dies with
the certificate, at which point every installer ever shipped starts warning
again. A failed request is now retried in order:
`timestamp.digicert.com` -> `timestamp.sectigo.com` ->
`timestamp.globalsign.com/tsa/r6advanced1`. The report names the authority that
worked, keeps the exit code and last line of every attempt, and a run where all
three fail is a **failure** whose message lists what was tried - not a signature
that quietly has no timestamp.

**SmartScreen: reported, never promised.**

`pc-agent release signing --reputation` prints the policy next to the real state
of the machine: which certificates exist, whether their keys are hardware-backed,
the last recorded signing run from `data/installer-signing.json`, and each
installer's verification result. It states the expectation that follows from the
certificate - *unsigned: "Unknown publisher"*, *OV: name shown, SmartScreen may
still warn until reputation accrues*, *EV: typically trusted immediately* - and it
reports reputation itself as `not-measurable-here`, because Microsoft decides it
from downloads, publisher identity and malware reports, and this product can
neither measure nor buy it.

The full runbook, including the checklist order and what a signature does not buy,
is `docs/smartscreen_reputation.md`.

**Numbers.** Suite **1282 passed, 2 skipped** on Linux; `ruff` clean; release
audit READY (33 checks); allow-list **488** files. Still not claimed: no
certificate exists on this machine, so every signing command here reports
`not-run` / `NOT READY` with the real reasons. The code is tested against fake
`signtool`, fake PowerShell and fake timestamps; the Win32 trust path and a real
token remain the user's step, by construction.

## 0.82.7 - the installer becomes a command, and the widget grid becomes touchable

Two MEDIUM items from the open list, both of which used to be "read the docs and
do it by hand".

**`pc-agent release installer` - check, build, sign.**

The desktop installer needed Node, Rust and WebView2, so the only way to find out
whether a machine could build it was to try and read the error. Now:

* `--check` runs nothing that installs or compiles. It reports `node`, `npm`,
  `cargo` and `rustc` **with their version strings**, whether the WebView2
  runtime is present, whether `tauri.conf.json`'s `frontendDist` actually resolves
  to a built UI with an `index.html`, and whether an installer already exists in
  `bundle/nsis` or `bundle/msi`. Missing pieces are named as blockers with the
  fix, and the exit code is `1`.
* `--build` runs `npm ci` then `npm run tauri:build` - argv lists, never a shell -
  with a timeout and the **tail of each output kept**. A failed `npm ci` does not
  start the ten-minute Rust compile; it is recorded and the build step is marked
  skipped.
* `--sign` reuses the Authenticode runbook, so both the NSIS `.exe` and the MSI
  are signed and verified; without a certificate the command refuses with exit
  `2` before touching anything.
* every run writes `data/installer-build.json`: steps, durations, output tails,
  artifacts with sizes, blockers, signing results.

Measured here: `pc-agent release installer --check` on Linux reports
`missing: cargo, rustc`, marks WebView2 `not-run` (off Windows), passes the shell
definition (the `frontendDist` path resolves to a real `index.html`), and exits
`1` with both blockers listed. That is the honest answer for this machine, and it
is the same answer the user will get until they install Rust.

**Arrange mode in the Command Center.**

Resize/hide/move lived in a hover toolbar, which is a mouse-only affordance: no
hover means no controls on a touch screen, and the same for a keyboard-only user.
One **Arrange** button now pins every widget control open at a **32 px** target
and reports itself via `aria-pressed`; **Done** puts it back, and the choice is
remembered across reloads (in its own key - arranging is a mode, not part of the
layout, so `Reset layout` never turns it on or off).

The browser harness now measures it instead of trusting it: 57/57 checks, of
which three are new - controls visible at `opacity 0 → 1` without hover, target
height `15px → 32px`, and the mode surviving a full page reload. Contrast still
passes in both themes.

**Numbers.** Suite **1252 passed, 2 skipped** on Linux; `ruff` clean; release
audit READY (33 checks); allow-list **485** files. Windows is still not claimed:
nothing here needs a Windows machine to be true, and nothing claiming Windows
(installer build, code signing, SmartScreen) is marked as verified.

## 0.82.6 - the Windows run of the suite is green, and the three reasons it was not

The 0.82.5 suite failed six tests on the target Windows machine and passed the
same six on Linux. Two were real product bugs, one was a test that asserted a
POSIX path, and three were a mixed install tree. All of it is fixed, and every
fix has a test that fails without it
(`tests/test_windows_platform_regressions.py`, 14 tests).

**Real bug 1 - the API server could not be stopped from the `api serve` path.**
`LocalApiServer.stop()` only called `shutdown()` when the server had been started
on *its own* thread. `pc-agent api serve` runs `serve_forever()` on the caller's
thread, so `stop()` skipped the shutdown and went straight to `server_close()`.
On Windows the loop then woke up selecting on a closed socket:
`OSError [WinError 10038] An operation was attempted on something that is not a
socket`, thrown out of the thread as an unhandled exception. The server now
tracks whether *any* thread is inside `serve_forever`, shuts that loop down
before closing the socket, and survives a socket error inside the loop instead of
killing the thread.

**Real bug 2 - a failing release chain was silent in `--json` mode.**
`pc-agent release production-check --json` printed a report, exited `1`, and put
the reason inside the JSON. A user (and the test that caught it) saw only
`assert 1 == 0`. Every failing step is now named on stderr -
`Release chain step failed -> apply: ...` - while stdout stays valid JSON. On top
of that, the two file operations in the chain (`copy2` per file, one read per
file in the tree hash) are retried when Windows reports a transient sharing
violation (WinError 5/32/33), which is what an indexer or antivirus holding a
freshly written file looks like. A real permission error is still raised.

**Test bug - a POSIX literal was asserted on every platform.**
`test_app_window_argv_has_no_chrome_and_its_own_profile` expected
`argv[0] == "/opt/chrome"`, but `str(Path("/opt/chrome"))` is `\opt\chrome` on
Windows: a `Path` carries host path semantics, so the *code* was right and the
assertion was wrong. It now asserts the platform-correct representation
(`os.fspath`), and a new test pins the contract that actually matters: an
executable passed as a **string** - e.g.
`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe` - reaches
`CreateProcess` byte for byte, never re-normalised. To make that true the code
stopped round-tripping strings through `Path`.

**The mixed tree - and the patcher no longer allows one.**
Three failures were version skew, not features: `pyproject.toml` said 0.82.4
while `import pc_agent` reported 0.82.3 and `README.md` still said 0.82.3. That
is a patched install that is not one release. Two changes:

* the patcher now **reads every shipped file back** after writing and compares it
  with the manifest: `created=… updated=… skipped=… verified=480/480`. Any
  mismatch is printed as `[MISMATCH] <path>`, the exit code becomes `1`, and the
  run says what to do next. A patcher that claims success over a mixed tree is
  worse than one that fails loudly.
* the post-install steps print
  `python -c "import pc_agent; print(pc_agent.__version__, pc_agent.__file__)"`
  with the expected answer, because a stale copy in `site-packages` can shadow an
  editable install and produce exactly this "impossible" version mismatch.

**Also in this build:** the release-chain work directory uses a short temp prefix
(`pcprod-` instead of `pc-agent-production-`), because 480 release paths up to 58
characters deep sit inside it and Windows MAX_PATH is 260; a test asserts the
longest shipped path stays under 120 characters.

**One command for the confirmation itself.** "Run the tests and paste the output"
is not a verification story, so `scripts/verify_windows.py` runs every gate in
order - interpreter and version, *which* `pc_agent` was imported and from where,
`ruff`, `doctor`, `release audit`, the desktop-shell preflight, the full
`pytest -q` suite, `release production-check` - keeps each exit code and the tail
of each output, and writes `data\windows-verification.json`. It prints
`WINDOWS VERIFICATION: PASS` or `FAIL (suite)` with the failing tail on screen,
exits `1` on any failure, and never quietly skips: the installer-signing check is
listed as `N/R` with its reason and the exact next command. The import gate is the
one that matters most - it fails when `pc_agent` resolves to a stale
`site-packages` copy instead of this tree, which is exactly the trap behind the
phantom version mismatch.

**Where this leaves the numbers.** Suite **1216 passed, 2 skipped** on Linux;
`ruff` clean; release audit READY (33 checks); allow-list **480** files. The
Windows run of 0.82.5 measured 1161 passed / 9 skipped / 6 failed - those six
are the ones above. The Windows suite has **not** been re-run against this build
from here (no Windows machine on this side); the assertions that failed are now
platform-correct or covered by tests that do not depend on the host, so a re-run
is the confirmation step, not a hope.

## 0.82.5 - the installers can now be signed for real, and the Command Center stops pretending

Two gaps closed, both of them things a user could see from the outside.

**Signing the thing you actually download.** 0.82.4 made the desktop build
verifiable but left the last mile as advice: the installer comes out unsigned,
so Windows SmartScreen shows "unknown publisher". Signing an *update bundle* was
already supported (Phase 21), but nobody downloads an update bundle. This release
adds the installer path end to end:

* `pc-agent release signing --certificates` reads `Cert:\CurrentUser\My`, prints
  every code-signing certificate with its **thumbprint, validity window and
  whether a private key is present**, then prints the two lines to paste. A
  certificate without a private key can verify but cannot sign, and this is where
  you find that out - not at signing time.
* `pc-agent release sign-bundle --check` signs nothing and reports `READY` /
  `NOT READY` with every blocker named: no installer found (build first), no
  `signtool.exe` (install the Windows SDK, or pass `--signtool`), no certificate,
  a thumbprint shorter than 40 hex characters, a missing PFX. Exit `1` when not
  ready, so a release script can gate on it.
* `pc-agent release sign-bundle` signs **both** the NSIS `.exe` and the `.msi`
  with `sha256` plus an RFC 3161 timestamp, then verifies each one through the
  Win32 trust API, not `signtool`'s exit code. Only a bundle where every file is
  signed *and* verifies reports `ok`; the report lands in
  `data/installer-signing.json` and the command exits `1` otherwise.
* The PFX password is read **only** from `PC_AGENT_SIGN_PFX_PASSWORD`. There is
  no flag that accepts the secret itself (command lines are visible to other
  users and land in shell history), and `--allow-argv-password` - which exists
  for throwaway VMs - is refused by the bundle command. Nothing here writes,
  exports or logs key material.

The full runbook, including what this deliberately does *not* do (obtain a
certificate, sign from CI, handle your key), is `docs/authenticode_signing.md`.

**The Command Center's widget grid is now actually configurable.** Drag-to-reorder
and two arrow buttons existed; width and visibility did not, so a dashboard full
of nine panels was fixed at whatever size the layout gave it. Now:

* **⤢** resizes a widget between half and full width (measured: 325px -> 660px in
  the verification harness, and it survives a reload because it is stored in
  `omni.ui.v1`);
* **✕** hides a widget, and the **Widgets (8/9)** menu restores it - a hidden
  widget keeps its position, so unhiding does not reshuffle the board;
* emptying the grid on purpose is allowed, and the empty state offers one button
  to bring the default layout back, which is also what **Reset layout** does.

Then the details that make it honest rather than decorative: the toolbar is a
hover/focus affordance so it never covers content at rest, every control has an
`aria-label` that names the widget, and the keyboard path (the arrow buttons)
does the same job as dragging. The UI verification harness grew three checks for
exactly this - width toggle, hide-and-survive-reload, reset - and now runs
**54/54**, with the measured contrast check still passing in both themes.

**Numbers for this build.** Suite **1202 passed, 1 skipped** (the Tk-only test,
no display here); `ruff` clean; release audit READY (33 checks); release
allow-list **479** files (the signing runbook doc and its test suite are new);
`scripts/check_desktop_shell.py` still prints `DESKTOP SHELL: READY`; the patched
install - apply the patcher into a clean folder, `pip install -e ".[dev]"`, run
the suite from inside it - is green with the same count.

**Still honest about what is not done.** No Windows installer was built or signed
on this machine: there is no Rust toolchain, no WebView2 and no certificate here,
so `release sign-bundle --check` prints `NOT READY` with three real blockers and
`release signing --certificates` prints `not-run` on Linux. The signing path is
implemented, unit-tested against fake `signtool`/certificate output, and
unverified against real Windows trust APIs - that remains the user's step, with
their own certificate. Widget resize should also be reachable without hovering
(touch targets), which is still on the list.

## 0.82.4 - the installer build is preflighted, and one real config bug is gone

The desktop installer was the last item nobody could check: `npm run tauri:build`
needs Rust and WebView2, which this machine does not have. So instead of
promising an installer, this release makes everything *around* the build
verifiable everywhere - and in doing so found a bug that would have failed the
first real build.

**The bug**

`tauri.conf.json` had `"frontendDist": "../src/pc_agent/webui"`. That path is
resolved **relative to `frontend/src-tauri/`**, so it pointed at
`frontend/src/pc_agent/webui`, a directory that does not exist. `tauri build`
would have failed at context generation, after the toolchain install and the Rust
compile. It now reads `../../src/pc_agent/webui`, and
`build.beforeBuildCommand` is set to `npm --prefix .. run build` so the bundle
can never be built from a stale SPA.

**New: `scripts/check_desktop_shell.py`**

Runs in about a second on any machine with Python, and validates each thing that
actually breaks a Tauri build:

| Check | Why |
|---|---|
| `frontendDist` resolves to a directory containing `index.html` | the exact bug above |
| `beforeBuildCommand` rebuilds the UI | no stale assets in the installer |
| window URL is a loopback origin, min size >= 1024x700, label `main` | the window is the local app, and capabilities target that label |
| every `bundle.icon` exists with real PNG/ICO/ICNS magic bytes | Tauri fails at the very end otherwise |
| `tauri.conf.json` and `Cargo.toml` versions equal `pc_agent.__version__` | About box and engine cannot disagree |
| capabilities are exactly `["core:default"]`, scoped to `main` | no shell/fs/http power reaches the webview |
| no `tauri-plugin-shell/fs/http`; single-instance wired; `lto`+`strip` set | the security and size posture the release claims |
| toolchain presence (`cargo`, `node`) | reported as a warning, never faked |

It fails a broken tree loudly (`DESKTOP SHELL: NOT READY` + the reason) instead
of crashing on a missing file - which is itself covered by a test.

**New: Windows CI job** (`.github/workflows/desktop-windows.yml`)

`workflow_dispatch` or a `v*` tag: preflight -> `npm ci` -> `npm run build` ->
Rust -> `tauri build` -> list the artefacts -> upload NSIS/MSI. It performs **no
code signing** (a test asserts no `signtool`, no `secrets.`, no signing env vars
in that workflow): Authenticode needs the user's certificate and stays a manual,
documented step.

**Also fixed: a patched install was not a green tree**

Applying the patcher into an empty folder and running the suite there failed
**9 tests** - every one of them because `.github/workflows/` was not part of the
release allow-list, while the production-hardening audit and
`tests/test_transformation_phase18_hardening.py` both treat a missing CI workflow
as a finding. A patched install was therefore not the same product as the
repository. `.github` is now in `_INCLUDE_DIRS` (workflows contain no secrets and
are read-only jobs), so a patch install equals the tree it was built from.
Verified by running the entire suite *inside* a freshly patched folder:
**1184 passed, 1 skipped**, `ruff` clean - see `TEST_REPORT.md`.

**Verified**

* 9 new tests in `tests/test_desktop_shell_preflight.py`, including two negative
  cases (broken `frontendDist`, remote window URL) and a check that CI does not
  sign.
* `python scripts/check_desktop_shell.py` -> `DESKTOP SHELL: READY` (18 checks,
  1 honest warning: `cargo` absent on this machine).
* Full suite and `ruff` results are recorded in `TEST_REPORT.md`; the Tauri
  binary itself remains **unbuilt here** - unchanged claim.

## 0.82.3 - the Command Center runs as PC software, not only in a browser tab

Fair question after 0.82.2: the new UI opened in a browser, so is it desktop
software at all? Yes - and now it opens as a **window without browser chrome** by
default, with no Rust toolchain required.

`pc-agent ui` (and `pc-agent gui`) pick the best window this machine can give:

| Order | Window | Needs |
|---|---|---|
| 1 | **pywebview** - a real OS webview (WebView2 on Windows, WebKit/WKWebView elsewhere) | `pip install -e ".[desktop]"` (optional extra) |
| 2 | **Installed Chromium-family browser in `--app` mode** - borderless app window, no address bar, no tabs | nothing: Edge ships with Windows; Chrome/Brave also detected |
| 3 | **Default browser tab** | nothing |

New module `desktop_window.py`:

* loopback-only: a non-loopback URL is refused with `ConfigError`, so the app
  window can never be pointed at a remote page and passed off as the product;
* argv list, `shell=False` - no shell strings, no quoting games;
* `--app=<url> --window-size=1560,980 --user-data-dir=<data>/ui-window` - a
  dedicated profile, so it is a separate instance and never merges with (or
  inherits extensions from) the user's own browser session;
* the process we spawn is ours: stopping the server (`Ctrl+C` or `--duration`)
  closes exactly that window;
* `--explain` prints the plan (kind, launcher, reason) and exits without binding
  a port or opening anything - useful when diagnosing a machine;
* the token still never appears in a URL: the server injects it into the page's
  `meta` tag as before.

`pywebview` stays **optional** (`[desktop]` extra) so `pip install -e ".[dev]"`
is unchanged and nothing new is needed to run the assistant.

**Verified**

* Linux: **1175 passed, 1 skipped**; `ruff` clean; `release audit` READY (33 checks).
* 21 new tests in `tests/test_desktop_app_window.py`: loopback acceptance/refusal,
  argv contract (`--app`, own profile, no shell metacharacters), the three
  fallback orders, `--explain` constructing no server, and a real child process
  that the test starts and terminates.
* `pc-agent ui --explain` prints the plan on a machine with no Chromium installed
  ("browser - no Chromium-family browser found") instead of pretending.
* Tauri remains the fourth option and is still **scaffolded, not built here** -
  the app-window path is what ships today.

## 0.82.2 - the Command Center becomes the default entry point, and it mirrors

Two things the 0.82.0 Windows run made obvious, fixed together.

**1. `pc-agent gui` opened the old Tkinter panel**

The new interface shipped, but the command a user has been typing since Phase 1
still opened the classic Tk window - so that is the UI they saw. The default is
now the Command Center:

| Command | Opens |
|---|---|
| `pc-agent ui` | the Command Center at `http://127.0.0.1:8765` (API started for you) |
| `pc-agent gui` | the same Command Center (**new default**) |
| `pc-agent gui --tk` | the classic Tkinter panel, unchanged, for rollback |
| `pc-agent gui --check` | readiness check, unchanged |
| `pc-agent api serve` | the API alone (no browser), unchanged |

`--no-browser` and `--duration N` are available on `ui`/`gui --web`. The launcher
binds loopback only, prints the origin (never a token in a URL), and stops the
server on Ctrl+C or when the duration expires. The Tk GUI, `/legacy` and every
`/v1` route are untouched - this is a default, not a removal.

**2. The shell did not mirror for right-to-left languages**

Phase 17 shipped RTL for the Tk panel and the engine reports `direction: rtl` for
a right-to-left locale, but the React shell had no direction handling at all, and
a dozen physical utilities (`ml-*`, `pr-*`, `border-l`, `text-left`, `left-0`)
would have kept pointing the wrong way even with `dir="rtl"` set.

* Every layout utility now uses the logical form (`ms/me`, `ps/pe`,
  `border-s/border-e`, `text-start/text-end`, `start/end`) - enforced by a test
  that fails on any physical one reappearing;
* `omni.ui.v1` gains `direction: "auto" | "ltr" | "rtl"` with a control in
  Settings → Display. `auto` follows the engine's locale report
  (`/v1/a11y/status`), never the browser guess;
* an explicit choice is applied by the pre-paint boot script, so RTL is the first
  frame rather than a flip after load;
* technical values are bidi-isolated, so numbers, ports and ids keep their own
  direction inside an RTL sentence.

**Desktop shell: real icons instead of a promise**

`scripts/make_app_icons.py` draws the product mark with Pillow at 8x
supersampling and writes the full set - `32x32.png`, `128x128.png`,
`128x128@2x.png`, `icon.png`, a multi-size `icon.ico` and `icon.icns` - into
`frontend/src-tauri/icons/`. They are reproducible from source and are now
committed, so `npm run tauri:build` on a build machine has everything it needs
except the Rust toolchain and WebView2, which this machine does not have. The
Tauri shell itself is still **scaffolded, not built here** - that claim does not
change.

**Verified**

* Linux: **1154 passed, 1 skipped** (real-Tk-window oracle); `ruff` clean;
  `release audit` READY (33 checks).
* Browser harness **51/51**, including four new RTL checks: `dir` switches, the
  sidebar mirrors to the right edge, content stays opposite it, no horizontal
  overflow, and `auto` matches the direction the engine reports.
* `pc-agent ui --no-browser --duration 8` served `/` with HTTP 200 and stopped on
  its own.

## 0.82.1 - voice acceptance stops passing a backend it cannot use

The 0.82.0 Windows run of the full suite failed exactly one test, and the test was
right to fail: on that machine the voice acceptance harness reported **every one
of its seven gates as passed**, including `push_to_talk`, on a host where the
gate had only proved that a capture *backend* (`winmm`) was importable.

**The defect (source, not test)**

`voice/acceptance.py` marked `push_to_talk` as `pass` whenever
`status.microphone.backend` was anything other than `none`. winmm, ALSA and the
`sounddevice` module all exist on machines with no input device at all, so a
microphone-less PC could print `PASSED` for a voice subsystem it cannot use -
a false acceptance pass, which is the one thing this programme does not ship.

Now the gate needs a real backend **and** at least one enumerated input device.
With a backend but no device it is `not-run` with the scan reason, and the pass
detail says what was proved: `backend winmm with 1 input device(s) enumerated
(presence only - no audio was recorded by this gate)`.

**The test (platform-correct instead of Linux-CI-correct)**

`test_voice_acceptance_is_sealed_and_honest_about_the_host` no longer assumes a
headless host. It reads the live service status and asserts the verdict matches
the hardware: `not-run` exactly for the gates whose hardware this host lacks, and
`passed` only when the report also carries the backend and input-device count it
measured. Every gate must state the evidence behind its verdict.

A new regression test, `test_voice_acceptance_does_not_pass_a_backend_without_a_device`,
pins the hardening: backend present + zero devices must stay `not-run`.

**Verified**

* Linux (this build machine, no audio hardware): `INCOMPLETE; passed=4;
  not-run=push_to_talk,stt_engine,tts_engine` - unchanged, honest.
* Simulated Windows host (winmm + 1 device + SAPI on both engines):
  `PASSED; gates=7; passed=7; failures=0`, host block records
  `microphone_backend=winmm`, `input_devices=1`.
* Linux full suite **1148 passed, 1 skipped** (the real-Tk-window oracle);
  `ruff` clean; `release audit` READY with 33 checks. On Windows the same suite
  has eight further documented skips (symlink privilege, POSIX shell, live
  systemd/logind, non-Windows-only assertions), so the expected Windows line is
  **1148 passed, 9 skipped** - that re-run is still the user's.

Version moves to **0.82.1** (`PREVIOUS_TRANSFORMATION_VERSION = "0.82.0"`);
patched installs get it from the same `TRANSFORMATION_PHASE_20_COMMAND_CENTER.mjs`
patcher, rebuilt below.

## 0.82.0 - Command Center UI redesign (the engine is unchanged)

The programme's last user-visible debt was the interface. 0.67.0 shipped a
Tkinter Command Center and a hand-written web panel; both worked, neither looked
like the product this assistant is meant to be. 0.82.0 replaces the presentation
with a React/TypeScript Command Center served by the local API - **and touches no
engine code**: the same ~80 `/v1` routes, the same safety kernel, the same
approvals, the same tools.

**New surface**

- `src/pc_agent/webui/` - the built bundle (23 files, 450 kB) plus
  `read_index()` (token injection), `static_path()` (traversal-safe resolution),
  `inline_script_hashes()` (CSP hashes) and `smoke()` (honest build status).
- `GET /` serves the built SPA; if it is missing, the response is a page that
  says so and prints the build command - never a blank 200 and never a mock
  dashboard. `GET /legacy` keeps the pre-0.82 panel as the rollback path.
- `GET /assets/*` with correct MIME types, `immutable` caching and `nosniff`;
  traversal attempts and missing files are 404.
- `GET /v1/events/stream?since_id=&duration=` - real NDJSON: a `stream.ready`
  frame, then events the service actually wrote, 10 s heartbeats, and a hard
  server-side bound of 5-900 s so a client cannot pin an idle socket. Garbage
  query values are now a **400** instead of silently falling back to a
  two-minute default.
- CSP stays strict: `script-src 'self' 'sha256-...'`. The pre-paint theme script
  is allowed by its exact hash, computed from the shell per response - no
  `'unsafe-inline'`, no nonce plumbing, no `'unsafe-eval'`.

**Frontend (`frontend/`)**

- React 18 + TypeScript + Tailwind + Framer Motion. No state library: a
  resource store with per-key subscriptions, explicit intervals, one in-flight
  request per resource, polling paused when the tab is hidden.
- 17 destinations: Command Center, AI Core, Agents, Missions, Tasks, Calendar,
  Memory, Conversations, Knowledge Base, Tools & Skills, Workflows, Model
  Center, Voice Assistant, Security Center, System Monitor, Analytics, Settings.
  All but the dashboard are code-split (3-7 kB each).
- The holographic AI core is a canvas-2D particle shell with real 3D rotation and
  depth sorting. States (`idle/thinking/executing/listening/speaking/error/
  offline`) are derived only from engine signals; nothing is simulated. Three.js
  was evaluated and **removed**: it would add ~600 kB and a WebGL failure mode
  for no visual gain over this shell.
- Dark **and** light themes as token sets, contrast-checked by
  `scripts/check_contrast.py` and by the Python suite.
- Widgets reorder by drag-and-drop or keyboard, and the order persists; sidebar
  state, theme and motion preference persist; `system` motion follows the OS.

**Verification (not "it compiles")**

- `frontend/scripts/ui-verify.js` drives real Chromium against the real API:
  **46/46 checks** - every route renders real content, both themes measured for
  contrast, palette opens/navigates, the live stream updates from a new task,
  collapse persists, widgets reorder, no horizontal overflow at 1280/1024,
  reduced motion honoured, no console error, no failed request, no cross-origin
  request, DOM bounded (1011 nodes), FCP 194 ms, 82 KB transferred, 9.5 MB heap.
- `tests/test_transformation_phase19_command_center_ui.py` - 17 tests inside the
  normal suite: shell + headers, hash-CSP, asset MIME/cache/traversal, honest
  missing-build page, `/legacy` rollback, NDJSON stream (ready frame, real event,
  auth, duration bound, 400 on garbage), bundle self-containment, both themes,
  and an anti-fake-data guard over the view sources.
- Full suite **1147 passed, 1 skipped** on Linux (UTC, Asia/Karachi,
  America/New_York); `ruff` clean; `release audit` READY (33 checks).
- Release allow-list **477 files** (adds the built bundle, the shipped
  `frontend/` sources; `node_modules`, `dist`, `data`, caches excluded).
  Patcher: `TRANSFORMATION_PHASE_20_COMMAND_CENTER.mjs`, rebuilt for 0.82.2 with
  473 embedded files, applied to an empty root and byte-verified against the
  tree, re-run reports `skipped=473`.

**Reference-layout fidelity pass**

- New widgets, all reading real endpoints: **AI core overview** (one row per
  subsystem - health, memory notes, voice, agents, LLM route, task counts) and
  **workflows & queue**; the roster now has 9 configurable widgets.
- **System monitor** switched from flat tiles to radial SVG gauges (CPU / RAM /
  disk / GPU) drawn from the measured snapshot; GPU and disk render `n/a` with the
  reason when the host cannot measure them.
- **Voice & attention** gained a real push-to-talk control (enable mic → start
  capture → stop and transcribe) and a "put in command bar" hand-off, which is
  the same `POST /v1/voice/*` path the command bar uses.
- **Model status** lists each configured provider with reachability and model
  count, and states plainly when only the offline mock engine is available.
- Saved widget layouts are migrated: unknown ids are dropped and widgets added by
  this release are appended instead of being hidden by an old layout.
- Both themes re-verified after the pass: light body text minimum **4.73:1**,
  dark **5.99:1** over 286 sampled elements; 46/46 browser checks still pass.

**Deliberately not done**: the Tauri 2 desktop shell is scaffolded
(`frontend/src-tauri/`) but not built or claimed as verified here - this build
machine has no Rust toolchain or WebView2. The shell maps the shell window onto
the loopback origin so the shipped path stays `pc-agent api serve` + browser.

## 0.81.2 - console-script bootstrap (broken-environment UX)

The Windows machine ran `pc-agent gui` and got a bare
`ModuleNotFoundError: No module named 'cryptography'` from the depth of the
import chain. The command was the **global** shim
(`C:\Users\UT\AppData\Local\Programs\Python\Python311\Scripts\pc-agent.exe`),
not `D:\omni-assistant\.venv`, so the project's declared dependencies were not
installed in the interpreter that was running. The product cannot fix a user's
PATH, but it can refuse to be unhelpful about it.

- **`src/pc_agent/entry.py`** is now the `pc-agent` console-script entry point. If
  the CLI cannot import, it prints the missing package, the **interpreter actually
  running**, whether that interpreter is inside a virtual environment, the path to
  the project's own interpreter when one exists, and the two commands that fix it -
  then exits **2** instead of dumping a traceback. It imports nothing heavy at
  module import time, so it works inside exactly the broken environment it
  describes.
- `pyproject.toml`: `pc-agent = "pc_agent.entry:main"` (the repair bootstrap
  `pc-agent-self-repair = "pc_agent.self_repair_cli:main"` is unchanged).
- **7 tests** in `tests/test_cli_bootstrap.py`: delegation to the CLI, the message
  names the package / interpreter / pip command, venv discovery on Windows and
  POSIX layouts, an ImportError without a `name`, an unrecognised failure, the
  console-script wiring, and a guard that the bootstrap stays import-safe.

Suite: **1130 passed, 1 skipped**; Ruff clean; `release audit` READY (33 checks).

## 0.81.1 - Windows acceptance hotfix for 0.81.0

The 0.81.0 Windows run finished at **1064 passed / 4 failed / 9 skipped**. All four
failures were in the tests added by 0.81.0, and two of them exposed a real output
bug.

### Product fixes

- **`--json` printed two documents.** `pc-agent release evidence` wrote the gate
  report as JSON and then appended a `Recorded evidence:` line, so the output
  could not be parsed. Every path now emits **exactly one** JSON document; when the
  command both runs the gates and knows the recorded state, the object is
  `{"report": ..., "recorded": ...}`.
- **`status` ran the gates.** On Windows, `release evidence` (no verb) runs the
  gates by design, but `release evidence status` is now an explicit verb that
  reports the recorded state and runs nothing - a status command that silently
  launches a 20-minute suite was a design mistake.
- **The exit status now matches the report**, in every branch: a run that records
  `failed` exits non-zero instead of printing the failures and returning success.

### Test fixes (no weakened intent)

- `test_probe_cache_is_keyed_by_the_process_limit` and
  `test_the_probe_survives_a_stale_working_directory` asserted that
  `sys.executable` can start **under the strict one-process limit**. On a host
  whose `python.exe` is a launcher shim that is exactly the thing that must fail -
  the test encoded the bug. They now assert the real property: the *relaxed* probe
  succeeds, the strict probe either succeeds or explains itself, and both answers
  are cached under different keys.
- A new test pins the property the Windows acceptance actually needs: at least one
  interpreter candidate must start **inside** the strict limit, so the sandbox does
  not depend on the last-resort launcher slot.
- `test_cli_status_reports_pending_evidence` and
  `test_cli_self_test_run_and_verify_and_record` now use the `status` verb, assert
  that the exit code agrees with the report that was just written, and assert that
  `--json` yields a single parseable document (the bug above, pinned as a test).

Suite: **1123 passed, 1 skipped** under `TZ=UTC`, `TZ=Asia/Karachi` and
`TZ=America/New_York`; Ruff clean; `release audit` READY with 33 checks.

## 0.80.1 - Final production hardening (Transformation Phase 18)

The eighteenth and last phase. It adds no new end-user capability: it makes the
previous seventeen provable.

### What is new
- **`src/pc_agent/production_check.py`** - the real release chain, executed end to
  end against a throw-away copy: allow-list scan, bundle build, verification
  without extraction, **tamper detection**, signing with an **ephemeral** Ed25519
  key, **unsigned-refusal** while signing is required, stage, apply (tree hash
  compared), rollback (pre-apply tree hash compared) and cleanup. Ten steps,
  ~1.7 s, `READY` on the verification host. The running install is never touched;
  no production key is used and the report says so.
- **`pc-agent release production-check [--out PATH] [--keep] [--json]`** plus
  release check **32** `production_hardening`, a `Hardening:` doctor line,
  `diagnostics.production_summary_line()` and a 20th dashboard tab
  **Production** whose button runs the real check and renders the stored report.
- **`.github/workflows/ci.yml`** - CI on `ubuntu-latest` + `windows-latest` ×
  Python `3.11` + `3.13` running `ruff`, the suite, `release audit` and
  `release production-check`; an accessibility/i18n/perf job; a tag-only
  packaging job. `permissions: contents: read`, no secrets, `fail-fast: false`.
- **`tests/test_security_suite.py`** (19 tests): SSRF including DNS rebinding,
  non-web schemes, credential URLs, shell metacharacters, non-allow-listed and
  path-traversal executables, archive traversal, secret leakage into
  diagnostics/audit/reports, emergency stop, tool-schema pollution.
- **`tests/test_performance_suite.py`** (9 tests): measured ceilings for settings
  load, CLI import, `health()`, `system_status()`, event listing, `a11y_status()`,
  heap growth over 200 status calls, database init and the release scan.
- **`tests/test_transformation_phase18_hardening.py`** (24 tests): the chain's ten
  steps, its honesty rules (no production key, nothing left behind, tree hashes),
  the stored-report states (missing / passed / failed / unreadable), the CI
  workflow's structure and gates, and the product wiring.
- **Four documents the programme still owed**: `AUDIT_REPORT.md`,
  `DEVELOPER_GUIDE.md`, `INSTALLATION_GUIDE.md`, `PLUGIN_DEVELOPMENT_GUIDE.md`.
- **Windows acceptance evidence gate (closes the last open item).**
  `src/pc_agent/windows_acceptance.py` plus `pc-agent release evidence
  [status|verify|record]`: the harness runs the real gates on the user's machine
  (platform, full suite, release audit, end-to-end release chain, a real
  `-I -B` plugin worker, owner-only file hardening, the measured accessibility
  report), writes `workspace/windows-acceptance.json` with a SHA-256 seal over the
  report body, and refuses to run - or to claim anything - anywhere but Windows.
  `pc-agent release evidence record` copies a **hash-verified, passed Windows**
  report to `data/windows-acceptance.json`; only then does
  `baseline.windows_acceptance_evidence()` return `verified:<date>` instead of
  `pending_user_windows_evidence`, and the `Hardening:` doctor line reports it.
  `--self-test` exercises the harness on any host and its report is marked
  `self-test`, which can never be recorded as Windows evidence.
- **19 tests** in `tests/test_windows_acceptance_evidence.py`: pending default,
  a verified report flips the baseline, an edited body is refused, non-Windows and
  non-passed reports are refused, six parametrised rejection cases (unknown
  status, wrong platform, missing gate, failing gate, wrong version, key material),
  the non-Windows refusal, the self-test, the record path, no secrets in the file,
  and the CLI status/self-test/verify/record flows.

### Full voice assistant: intent routing, hands-free loop, sealed acceptance

The Phase 10 voice pipeline could capture, transcribe and speak, but nothing in
the product turned a *transcript into an action*, and "full voice assistant" was
a claim with no acceptance artefact.

- **`src/pc_agent/voice/intents.py`** - a **deterministic** router: one utterance
  becomes exactly one of `stop`, `help`, `status`, `time`, `note`, `reminder`,
  `speak`, `task` or `empty`, with no model call and no guessing, so a spoken
  command is testable and auditable. The wake phrase is stripped and reported
  separately (`addressed`), and only at the **start** of an utterance, so
  "I told Jarvis yesterday" is not an address. `help` is generated from the same
  list the router implements, so the two cannot drift.
- **`VoiceIntentExecutor`** - performs an intent against the real services:
  notes go to the memory repository, reminders to the proactive engine, free text
  to `LocalService.create_task(..., source="voice")`. Voice therefore gains **no
  privilege**: a task that needs approval still stops at the approval centre, and
  the reply says "that needs your approval before it runs".
- **`src/pc_agent/voice/listener.py`** - the hands-free session loop: bounded
  `listen -> transcribe -> act -> speak` cycles with `max_cycles`, `max_seconds`
  and per-cycle `voice_max_seconds` caps, a **visible state**
  (`idle/listening/capturing/thinking/speaking/stopped/blocked`), an immediate
  `stop()` that aborts between phases, and a per-cycle record of what was heard,
  which intent ran and what was said back. It refuses to start when the
  persisted microphone gate is off, when wake-word mode has no second opt-in,
  when the emergency stop is engaged, or when no capture backend exists - each
  with its own reason. Wake-phrase matching is **transcript-based** and that
  limitation is written down rather than hidden.
- **`src/pc_agent/voice/acceptance.py`** - `pc-agent voice acceptance`: seven
  real gates (privacy gate enable/disable round trip with the user's setting
  restored, capture backend, wake-phrase parsing, intent routing over 8
  utterances, the STT engine in use, a **real synthesis** to a temporary file,
  retention policy), sealed with the same `acceptance_body_sha256` seal the
  Windows acceptance harness uses, and verified offline by
  `pc-agent voice acceptance-verify`.
- CLI: `pc-agent voice intent "<sentence>" [--execute]`, `pc-agent voice loop
  [--max-cycles N] [--cycle-seconds S] [--no-wake-word] [--no-speak]`,
  `pc-agent voice acceptance [--out PATH]`, `pc-agent voice acceptance-verify`.
- The report has three outcomes, not two: `passed` (every gate ran and passed),
  `incomplete` (none failed, at least one could not run - what a headless build
  machine sees) and `failed`. A missing microphone or speech engine never turns
  into a green tick.

### Signed installation tooling: Authenticode

The update bundle has been Ed25519-signed and enforced since Phase 21. The other
half - the Authenticode signature on the installer EXE, which Windows and
SmartScreen evaluate - is now shipped:

- **`src/pc_agent/windows_signing.py`**: `signtool` discovery (PATH, then the
  newest Windows SDK), command construction that **never puts a password on a
  command line** in the recommended thumbprint mode, a timestamp by default, and
  verification through the operating system's own `WinVerifyTrust` API (called
  through `ctypes`) instead of parsing a log line. On a non-Windows host the
  answer is `unverifiable`, never a guess.
- PFX files are supported as a fallback, but the password must come from the
  environment and the caller must pass `--allow-argv-password`, because
  `signtool` requires `/p <password>` in `argv` and `argv` is world-readable on
  the machine. The refusal message says exactly that. Passwords are scrubbed from
  captured output and never stored in the report.
- CLI: `pc-agent release signing`, `pc-agent release sign FILE
  [--thumbprint T | --pfx FILE --allow-argv-password] [--timestamp-url URL]`,
  `pc-agent release signature FILE`.
- Release audit check **33** `windows_authenticode` (audit now runs **33**
  checks): `PASS` when the installer is honestly unsigned, `PASS` with the
  verification text when it is signed and verified, `WARN` when a recorded
  attempt did not verify. The `Hardening:` doctor line gains
  `authenticode=<status>`.

### Version

0.80.1 → **0.81.0**; `baseline.PREVIOUS_TRANSFORMATION_VERSION` moves to `0.80.1`
and the future-version pins in the update tests move to `0.82.0`.

### Windows sandbox fix (found by the 0.80.1 acceptance run, fixed here)

The 0.80.0 tree finished the user's Windows run at **1027 passed / 10 failed / 8
skipped**. Nine failures had one cause: every plugin tool call died with

```
Unable to create process using '"C:\Users\UT\AppData\Local\Programs\Python\Python311\python.exe" -I -B D:\omni-assistant\src\pc_agent\plugins\worker.py'
```

Root cause: that install's `D:\omni-assistant\.venv\Scripts\python.exe` is a
**launcher shim** (a virtualenv-style redirector), so starting it is *two*
processes - the shim, then the real interpreter it creates. The sandbox put the
worker in a Windows Job Object with `ACTIVE_PROCESS` capped at 1, which forbids
exactly that second process; the shim then reported the refusal as its own error
and exited 101. Two defects made this invisible:

1. the job limits were set **after** the process joined the job, so the window in
   which a shim could start its child was a race the parent usually lost, and
2. the interpreter probe ran **outside** the sandbox (`subprocess.run` with no job
   object), so a shim probed green and failed every real call.

Fixed in `src/pc_agent/plugins/sandbox.py`:
- job limits (`KILL_ON_JOB_CLOSE`, `PROCESS_MEMORY`, `ACTIVE_PROCESS`) are set
  **before** `AssignProcessToJobObject`, so the policy is in force for the entire
  life of the child and the failure is deterministic instead of flaky;
- the probe runs **inside the same limits** as the real spawn, and its cache is
  keyed by `(interpreter, active-process-limit)` - a green probe at one limit can
  no longer vouch for another;
- interpreters are resolved better: `sys.executable`, `sys._base_executable`, the
  base prefix (`python.exe` / `bin/python3`) and `pyvenv.cfg`'s `home` are tried
  in order, so a shim environment reaches the *real* interpreter and runs the
  worker as a single process with the strict limit;
- if - and only if - no candidate can run under the strict limit, one extra slot
  is granted for the launcher chain (`passes = (0, 1)`), and the result says so:
  `mechanism` becomes `job-object + isolated-interpreter + audit-hook
  +launcher-slot(active-process-limit=2)`. Two processes is the total: the shim
  and the worker, with no room for a third, and the worker's audit hook still
  refuses subprocess creation from plugin code. Securing this did not weaken any
  limit - it removed a limit that was misapplied.

The tenth failure was a test defect: `test_commands_never_go_through_a_shell`
used `echo`, which is a `cmd` **builtin** on Windows and therefore cannot be
launched at all. It now uses the running interpreter and asserts the same
property (metacharacters arrive as `argv` items, the marker file is never
created).

**13 new tests** in `tests/test_windows_launcher_sandbox.py`: effective-limit
arithmetic, the "no slack means the same object" case, job-limits-before-assign
ordering, probe caching per limit, probe failure reasons, the strict-then-slack
ladder, the reported launcher slot, end-to-end worker execution through the
chosen interpreter, base-interpreter derivation from `base_prefix`/`pyvenv.cfg`,
and survival of a stale working directory.

### Changed
- `release audit` now runs **32** checks (was 31) and distinguishes "never ran the
  chain check" from "the chain check passed".
- Dashboard tab count **19 → 20**; tests, docs and the patcher updated together.
- `baseline.py`: phase **18**, label `TRANSFORMATION PHASE 18 Production Hardening`.
- No schema change: still **v18**, no migration.

## 0.79.1 - Windows acceptance hotfix (Phase 17.1)

The 0.79.0 Windows acceptance run finished at **919 passed / 12 failed / 8 skipped**.
Every one of those twelve failures is fixed here, and each fix is either a real
product defect or a test that encoded a POSIX/UTC-only assumption.

### Product fixes
- **Proactive hourly cap (real defect, Phase 14).** `count_notifications_since()`
  counted every notification row, including the `deferred` rows the cap itself
  writes. Each suppressed tick therefore refilled its own budget: the cap fired
  at `(3/3)`, then `(4/3)`, and reminders that should have been released an hour
  later were starved forever. The budget now counts `status = 'delivered'` rows
  only - suppression is a ledger fact, not a notification the user saw.
- **Suppressed rows were stamped with the wall clock (real defect, Phase 14).**
  The quiet-hours row already used the engine clock, but the cap and cooldown rows
  used `datetime.now()`, so the ledger mixed two timelines whenever a tick ran
  with an injected clock. All suppression rows now carry the tick instant.
- **Plugin sandbox could not start a worker on Windows (Phase 15).** The scrubbed
  child environment kept only `SYSTEMROOT`/`SYSTEMDRIVE`/`WINDIR`/`TEMP`/`TMP`/`PATH`
  when they happened to be set, and the spawn path had no fallback: a host that
  could not create the process reported
  `Unable to create process using '"…python.exe" -I -B …worker.py'` as a *plugin*
  failure. Now: a fixed non-secret allow-list of Windows essentials is always
  present (with `GetWindowsDirectoryW`/`tempfile` fallbacks), every interpreter
  candidate (`sys.executable`, `sys._base_executable`, `python3`, `python`) is
  probed once and cached, the first one that actually starts is used, the
  interpreter that ran is reported in the result, and a host that cannot start
  any of them produces `spawn_error` instead of an exception or a misleading
  plugin error. Isolation is unchanged: same scrubbed env, same job object, same
  `-I -B` isolated interpreter, same audit hook on every attempt.
- **Owner-only files on Windows (real gap, Phase 16).** `os.chmod(path, 0o600)` is
  a no-op on NTFS (the mode always reads `0o666`), so sync identity keys and
  bundles were only as private as the profile ACL. New `pc_agent.sync.perms`
  applies a real owner-only ACL on Windows (`icacls /inheritance:r /grant:r
  <user>:(F)`), reads the ACL back to verify no inherited ACE remains, and returns
  a report (`mechanism`, `applied`, `owner_only`, `detail`) that is stored with
  the artefact, returned in the export payload and asserted on. On a volume
  without ACL support the report says `applied: False` instead of pretending.

### Test fixes (no weakened intent)
- Phase 14: a naive ISO due time is *local* wall clock, so the assertion now
  compares instants (`== datetime(...,).astimezone(UTC)`) instead of a UTC
  string prefix; the reschedule assertion compares `datetime` instants instead of
  two ISO strings that carried different offsets.
- Phase 15: the "write outside the capability roots" plugin targeted `/tmp`,
  which does not exist on Windows. It now uses `tempfile.gettempdir()` - the
  property under test (a path outside the declared roots) is unchanged.
- Phase 16: the mode assertion became `_assert_owner_only()`, which checks the
  POSIX mode on POSIX and the verified ACL on Windows. No skip, no `chmod` lie.

### New regression tests
- 10 tests in `tests/test_transformation_phase17_1_hotfix.py`: cap counts only
  delivered rows, the cap still bites inside the same hour, suppression rows carry
  the ledger instant, naive-ISO/offset parsing is host-timezone independent,
  host spawn failures are structured (`spawn_error`), interpreter substitution is
  reported and not silent, a real `-I -B` worker runs end to end, the child
  environment keeps the essentials and leaks no `*_API_KEY`, and the permission
  report matches the mechanism the host actually has.
- Full suite verified three times under different host timezones
  (`TZ=UTC`, `TZ=Asia/Karachi`, `TZ=America/New_York`): **993 passed, 1 skipped**
  each time (the skip is the Tk window test on a headless host).

## 0.79.0 - Accessibility, multilingual UI, RTL and polish

### What is new
- **`src/pc_agent/i18n/`**: 61 interface keys in **four real locales** - English
  (source of truth), Spanish, Urdu and Arabic - with an explicit statement that
  user content is never machine-translated. `{placeholder}` substitution is done
  from a mapping (never `str.format` on caller text), a missing key falls back to
  English and is **counted**, an unknown key echoes itself, and `en-US`/`UR_pk`
  are normalised. A four-entry allow-list means a locale that simply copies the
  English string fails the test suite.
- **`src/pc_agent/a11y.py`**: real WCAG 2.1 relative-luminance and contrast-ratio
  maths, a new high-contrast palette, and `a11y_status()` that reports the
  language, direction, text scale, theme, motion state, every locale's computed
  coverage, the measured contrast table with `passing/total` and the resulting
  WCAG level, the documented keyboard shortcuts, the claims this build can make
  (each with how it is verified) and **five disclosed limitations**.
- **`src/pc_agent/ui_prefs.py`**: `data/ui.json` persistence (six presentation
  values) with precedence *environment → ui.json → default*, tolerant of a corrupt
  file. No schema migration: schema stays **v18**.
- **19th GUI tab Accessibility**: language picker, text size 80-200 %, high
  contrast, reduce motion, Save/Refresh/Run-check, and a report pane that renders
  the measured contrast table, shortcuts, claims and limits. Text styles scale from
  the base font and RTL **mirrors the header layout**.
- **Web Command Center**: real `lang`/`dir` switching with `[dir="rtl"]` mirroring,
  a skip link, `role="banner"`/`role="main"`, `aria-live` status regions,
  `:focus-visible` outlines, `prefers-reduced-motion`, a high-contrast class and a
  working Accessibility panel (apply-now and save).
- **CLI/API**: `pc-agent i18n status|list|strings|set`,
  `pc-agent a11y status|check|contrast`, `pc-agent perf bench`, an
  `Accessibility:` doctor line, an `a11y` diagnostics section, release check 31
  (`accessibility`) and five routes (`/v1/i18n/*`, `/v1/a11y/status`,
  `/v1/perf/bench`).
- 6 new settings (`PC_AGENT_UI_LOCALE`, `_SCALE`, `_HIGH_CONTRAST`, `_REDUCE_MOTION`,
  `_KEYBOARD_SHORTCUTS`, `_CLI_COLOR`), all validated at load with the accepted list.
- 44 new tests in `tests/test_transformation_phase17_accessibility.py`.

### Fixes found while building this phase
- The `Plugins:`/`Sync:` doctor lines referenced their import inside the wrong
  order; both now import before printing, so `pc-agent doctor` cannot raise a
  `NameError`.
- `unit.percent` ("%") was flagged as an untranslated copy of English; symbols are
  now part of the explicit `IDENTICAL_OK` allow-list instead of weakening the check.

## 0.78.0 - Encrypted device sync and secure remote coordination

### What is new
- **`src/pc_agent/sync/`**: optional, local-first, end-to-end encrypted sync
  between *your own* devices. `crypto.py` (Ed25519 device identity + X25519
  ephemeral-static ECDH + HKDF-SHA256 + AES-256-GCM with the header as AAD and an
  Ed25519 signature binding every recipient entry), `store.py` (schema v18 ledger,
  bounded to 400 bundles / 200 requests), `service.py` (the only gate keeper).
- **Transport is a folder you already sync** - OneDrive, Dropbox, a USB stick, a LAN
  share. No port is opened, no server runs, no account is needed, nothing is sent
  anywhere by this code.
- **Pairing** records another device's Ed25519 + X25519 public keys plus a grouped
  fingerprint; `confirm="PAIR"` is required everywhere. Pause, resume and unpair are
  real state changes; a bundle from an unpaired device, or from a paired device
  whose signing key changed, is refused.
- **Replay and clock safety**: a processed `bundle_id` is never processed again, the
  per-sender `seq` must strictly increase, and `created_at` must sit inside
  `PC_AGENT_SYNC_CLOCK_SKEW_SECONDS` (default 900 s).
- **Conflict handling that never destroys local data**: reminders de-duplicate on
  `(title, due)`, flows follow `local|newest|remote` (default `newest`), profiles are
  create-only. Every skipped record is reported as a conflict.
- **Coordination without execution**: `remind`, `notify` and `ping` requests travel
  inside a normal encrypted bundle and wait in `sync_requests` for a human accept;
  accepting calls only the existing governed reminder/notification paths.
- **`pc-agent sync status|identity|peers|pair|unpair|pause|resume|export|import|tick|bundles|requests|request|decide`**,
  **12 HTTP routes** under `/v1/sync/*`, the 18th GUI tab **Sync**, a web
  **Device sync** panel, a `sync` diagnostics section, a `Sync:` doctor line and
  release check 30 (`device_sync`).
- Schema **v18**: `sync_devices`, `sync_bundles`, `sync_requests`, `sync_state`.
- 8 new settings (`PC_AGENT_ALLOW_SYNC` off by default, device name, sync dir,
  bundle byte cap, record cap, retention days, conflict policy, clock skew), all
  validated at load.
- 51 new tests in `tests/test_transformation_phase16_sync.py`: two real device
  identities in two data directories, real bundles on disk, and real refusals for
  replay, tampering, key changes, unpaired senders, clock skew and oversize input.

### Fixes found while building this phase
- `_iso()` in the sync service had a required `moment` argument but was called with
  none; the default is now `None` (i.e. *now*).
- The sync apply path called `ProactiveStore.add_reminder()` (which wants a
  `datetime` and is internal) instead of the public, validating
  `ProactiveEngine.add_reminder(due=...)`; both the import and the coordination
  paths now use the public API, so `+30m`/ISO strings are parsed by the one parser.
- The `sync` CLI dispatcher and the `Plugins:`/`Sync:` doctor lines referenced names
  before their local imports; the import order is fixed.
- `pc_agent.sync` did not re-export `COLLECTIONS` / `REQUEST_KINDS` /
  `BUNDLE_SUFFIX`, so tests and integrations could not discover the closed sets.

## 0.77.0 - Plugin ecosystem (signed manifests + OS sandboxing)

### What is new
- **`src/pc_agent/plugins/`**: a complete third-party extension point -
  `manifest.py` (strict contract, canonical JSON, content checksum, Ed25519
  verification), `sandbox.py` (POSIX `setrlimit` / Windows Job Object, isolated
  interpreter, scrubbed environment, hard timeout), `worker.py` (audit-hook
  enforced child process, one declared tool call, JSON-only protocol),
  `store.py` (schema v17 registry, version history, run ledger) and
  `service.py` (the only gate keeper).
- **Signed bundles**: `manifest.json` + `manifest.sig.json`; the signature covers
  the exact manifest bytes and names the content checksum, so a modified bundle
  is refused. Unsigned bundles are refused unless `PC_AGENT_PLUGIN_ALLOW_UNSIGNED`
  is set, and only keys in `PC_AGENT_PLUGIN_TRUSTED_KEYS` are accepted.
- **Capabilities** are a closed list (`read_workspace`, `write_workspace`,
  `read_plugin_files`, `network`, `subprocess`); `network` and `subprocess` are
  refused at install time because this build cannot grant them, and the worker
  additionally denies sockets, subprocesses and writes outside the granted roots
  through a `sys.addaudithook` hook.
- **Lifecycle**: verify, install, enable/disable, rollback to an earlier version
  (5 kept), uninstall; every step is an event and the tool runs are a bounded
  ledger (500 rows).
- **12 HTTP routes** under `/v1/plugins/*`, a `pc-agent plugins …` CLI (with
  `run --arg name=json --confirm <plugin id>`), the 17th GUI tab **Plugins**, a
  web dashboard panel, a `Plugins:` doctor line, a `plugins` diagnostics section
  and release check 29 (`plugin_ecosystem`).
- Schema **v17**: `plugins`, `plugin_versions`, `plugin_runs`, `plugin_state`.
- 9 new settings (gate, unsigned opt-in, trust store, timeout, memory, CPU,
  bundle size, file count, per-file size), all validated at load.
- 36 new tests in `tests/test_transformation_phase15_plugins.py` with real
  Ed25519 keys, real zips and real subprocesses.

### Fixes found while building this phase
- The worker's `os.setsid()` collided with `start_new_session=True`
  (`Exception occurred in preexec_fn`); the preexec hook now only applies
  rlimits and the session is created by `subprocess`.
- The plugin `INSERT` had 17 columns against 15 bound values; rewritten with
  explicit column/value alignment.
- A `NOTEBOOK_TAB_COUNT` constant was first inserted inside an import block
  (SyntaxError); it now lives below the Tk import guard.
- The doctor line lost its `Proactive:` import during the edit and raised
  `NameError`; restored and covered by the doctor test.

### Verification
- Full suite **887 passed, 1 skipped**; `ruff check` clean; release audit
  `READY`, 29 checks, 0 failures, 1 warning.
- End-to-end by hand: signed bundle verify/install/enable/run/skills/runs/remove
  through the CLI, and the same flow over a real `LocalApiServer` HTTP listener.
- Containment proven, not asserted: a 30 s sleep is killed by a 1 s timeout, a
  `socket.create_connection` is refused, and a write outside the granted roots
  raises `PermissionError` without creating the file.

### Known limits
- The audit hook protects Python-level work; native code could bypass it, which
  is why OS limits are the second layer.
- Reads are not path-confined in this phase (writes are).
- No registry/marketplace fetch, no native plugins, and plugin tools are not yet
  offered to the AI planner.

## 0.76.0 - Proactive intelligence

### What is new
- **Reminders** (`src/pc_agent/proactive.py`): durable, time-driven, one-shot or
  repeating, with `HH:MM`, ISO-8601 and `+15m`/`+2h`/`+3d` due formats;
  snooze/dismiss/done/delete/enable, and a bounded open-reminder count.
- **Notification policy**: quiet hours (wraparound-aware, optional `critical`
  crossover), rolling hourly cap (default 20, max 500), per-key cooldown
  (default 300 s) and an outbound switch that is **off by default**. Deferrals
  carry an explicit reason instead of being dropped silently.
- **Daily briefings**: reminders, calendar, drive, flows, tasks and system —
  composed from live state only, one per local date, idempotent.
- **Scheduler step**: the real `api_server` scheduler now calls
  `service.proactive_tick(source="scheduler")` so reminders fire without a UI.
- **13 HTTP routes** under `/v1/proactive/*`, a `pc-agent proactive …` CLI tree,
  the 16th GUI tab **Proactive**, a web dashboard panel, a `Proactive:` doctor
  line, a `proactive` diagnostics section and release check 28
  (`proactive_intelligence`).
- Schema **v16**: `reminders`, `notifications`, `proactive_briefings`,
  `proactive_state` (+5 indexes).
- 10 new settings: `PC_AGENT_ALLOW_PROACTIVE`, `PC_AGENT_ALLOW_PROACTIVE_OUTBOUND`,
  `PC_AGENT_QUIET_HOURS`, `PC_AGENT_QUIET_HOURS_ALLOW_CRITICAL`,
  `PC_AGENT_NOTIFICATION_MAX_PER_HOUR`, `PC_AGENT_NOTIFICATION_COOLDOWN_SECONDS`,
  `PC_AGENT_BRIEFING_ENABLED`, `PC_AGENT_BRIEFING_TIME`,
  `PC_AGENT_REMINDER_MAX_OPEN`, `PC_AGENT_PROACTIVE_TICK_LIMIT`.
- 24 new tests in `tests/test_transformation_phase14_proactive.py`, including a
  simulated 24-hour determinism test.

### Fixes found while building this phase
- `argparse` treated `--due -1m` as a flag, so negative offsets were
  unusable; `_fold_negative_values` now folds `--due`/`--for` into
  `--flag=value` before parsing.
- Notification writes used the wall clock, which made the hourly cap and the
  cooldown untestable (and, on a slow write, slightly wrong); the engine clock
  is now threaded into `record_notification`.
- The connectors release-check detail hard-coded `schema v15`; it now reads
  `LATEST_SCHEMA_VERSION` so it cannot go stale again.
- Test-suite identity sweep to 0.76.0 (label, previous version, schema version,
  future-feed version so update-gate tests keep failing closed on a real
  mismatch).

### Verification
- Full suite **851 passed, 1 skipped**; `ruff check` clean; release audit
  `READY`, 28 checks, 0 failures, 1 warning.
- End-to-end smokes over real files, a real `LocalApiServer` HTTP listener and
  the real CLI (not mocks): reminder add/list/tick/snooze/done/delete, forced
  briefing, notify, notification clear, invalid due time refused with exit 1.

### Known limits
- No OS toast delivery (deliberate: it is an outbound channel and belongs behind
  the outbound gate with its own tests).
- Reminders only fire while the service (or GUI, which embeds it) is running;
  a closed app reports them late on next start.
- Quiet hours are one daily window, not per weekday.

## 0.75.1 - Process scan hotfix (real Windows evidence)

### The defect
- On the user's Windows machine `pytest` failed with
  `test_live_snapshot_on_this_machine`: `top_processes == []` while every
  other metric was correct. `_top_processes_windows()` wrapped the whole
  scan in one broad `except`, so *any* failure - a missing PSAPI export, a
  refused `OpenProcess`, an empty enumeration - returned an empty list that
  looked exactly like "this machine has no processes".

### The fix
- `top_processes` is now produced by an explicit, layered chain in
  `src/pc_agent/system_monitor.py`:
  1. `win32-enum` - EnumProcesses + GetProcessMemoryInfo;
  2. `win32-toolhelp` - CreateToolhelp32Snapshot/Process32FirstW for PIDs and
     image names plus the same memory reader (independent of PSAPI);
  3. `tasklist` - the OS's own `tasklist /FO CSV /NH`, parsed by shape so
     localised headers and `1,234 K` / `1.234 K` / `12 345 K` all work;
  4. `powershell` - `Get-Process ... WorkingSet64` as the last resort.
- Nothing is guessed: each layer returns a `ProcessScan(rows, source, detail)`
  and the snapshot now carries `top_processes_source` and
  `top_processes_detail` (`opened=250/312 unreadable=3`, `snapshot=300 …`,
  `GetProcessMemoryInfo export missing`, …) plus a `processes=<n> (<source>)`
  field in the `System:` doctor line, so an empty scan can never again be
  mistaken for a healthy one.
- Helper processes are invoked with a fixed, argument-free argv and
  `CREATE_NO_WINDOW`, an 8 s timeout and capped output; they run only after
  the in-process API produced nothing.

### Tests
- `tests/test_transformation_phase6_monitoring.py` grows from 10 to 18 tests:
  fallback ordering, reason preservation, locale-tolerant `tasklist` parsing
(comma/dot/space separators, MB, malformed rows), PowerShell parsing, the
  enum layer's opened/unreadable counters, a missing-export reason, the
  Toolhelp layer, and a snapshot that reports `unavailable` honestly.
- Full suite **827 passed, 1 skipped**; ruff clean; release audit unchanged at
  27 checks.

## 0.75.0 - TRANSFORMATION PHASE 13 Connectors

### Connectors (new)
- `src/pc_agent/connectors/` - five local-first connectors behind explicit,
  default-off gates, plus one durable ledger for every interaction:
  - **calendar** (`calendar_ics.py`): reads and writes a real `.ics` file
    (RFC 5545 line folding, escaped text, `UID` + `DTSTART` validation, atomic
    temp-file replace, existing events preserved). Naive input times are treated
    as local time and written as UTC - never relabelled as UTC.
  - **drive** (`drive.py`): SHA-256 content index of one local folder (the folder
    the user's own cloud client already syncs). Symlinks/junctions escaping the
    root are skipped, well-known secret names/directories are skipped
    (`.ssh`, `.gnupg`, `.aws`, `.kube`, `.env`, `*.key`, `*.pem`, `*.kdbx`, …),
    per-file and total caps are enforced and truncation is reported.
  - **email** (`email.py`): IMAP4-SSL **headers only**
    (`BODY.PEEK[HEADER.FIELDS (...)])`, RFC 2047 subject decoding, never a body
    or attachment; SMTP StartTLS sending behind a **second** gate
    (`PC_AGENT_ALLOW_EMAIL_CONNECTOR`) plus a per-message `confirm="SEND"` token,
    CRLF/NUL header-injection refusal and a 5-recipient cap.
  - **chat** (`chat.py`): one signed HTTPS POST (`X-PcAgent-Signature:
    sha256=HMAC(secret, timestamp.body)`), SSRF-validated with the browser
    tool's validator, redirects refused, delivery recorded with status code,
    bytes and duration.
  - **webhook** (`webhook.py`): inbound verification only - bounded body,
    numeric timestamp inside a configurable skew window, constant-time signature
    comparison, replay protection keyed on `kind:external_id`, control characters
    and lengths sanitised before storage.
- `ConnectorService` is the single gate keeper: a closed gate raises an
  actionable `ConnectorError` naming the exact setting, never a silent no-op.

### Storage - schema v15
- New tables `connector_events` (inbound items + refusals), `connector_deliveries`
  (outbound attempts, including failures) and `connector_state` (drive manifest,
  last sync), with three indexes and bounded row counts.

### Configuration (all default off)
- `PC_AGENT_ALLOW_CONNECTORS`, `PC_AGENT_ALLOW_EMAIL_CONNECTOR`,
  `PC_AGENT_CONNECTOR_CALENDAR`, `…_DRIVE_ROOT`, `…_CHAT_URL`,
  `…_CHAT_SECRET`, `…_WEBHOOK_SECRET`, `…_EMAIL_HOST/USER/PASSWORD/FOLDER`,
  `…_SMTP_HOST/PORT`, `…_MAX_ITEMS` (1-100), `…_TIMEOUT_SECONDS` (1-120),
  `…_WEBHOOK_SKEW_SECONDS` (30-86400) and `…_ALLOW_PRIVATE_ENDPOINTS` (opt-in
  LAN chat targets). Out-of-range values raise `ConfigError` instead of being
  silently clamped; `public_summary()` exposes only masked labels.

### Surfaces
- Service: 12 connector methods (`connector_status/summary/events/deliveries/
  calendar_events/calendar_add/drive_sync/mail_inbox/mail_send/chat_send/
  webhook_receive`).
- CLI: `pc-agent connectors status|events|deliveries`, `connectors calendar
  list|add`, `connectors drive sync`, `connectors mail inbox|send`,
  `connectors chat send`, `connectors webhook receive`.
- 9 HTTP routes under `/v1/connectors/…`; GUI **Connectors** tab (15th) with
  probe, calendar, drive, mail, chat and webhook controls; a live web dashboard
  panel; a `Connectors:` doctor line; a `connectors` diagnostics section and
  release-audit check **27**.

### Documentation
- `docs/transformation_phase13.md` records what existed, what was missing, the
  changes, files, tests, security/performance implications, limitations and the
  next tasks.

## 0.74.0 - TRANSFORMATION PHASE 12 Visual Workflow Builder

### Visual workflow builder (new)
- `src/pc_agent/flows.py` — a flow is a **versioned, validated graph of
  instruction nodes** that compiles into an ordinary Mission Center mission, so
  every node keeps the same Safety Kernel, approvals, token budgets, hard
  timeouts, verification and recovery as a hand-written mission.
- Strict validation: unknown keys are refused (never ignored), `flow_schema`
  must be 1, node ids must match `^[a-z0-9][a-z0-9_-]{0,31}$`, duplicate
  ids/edges and edges to unknown nodes are refused, `{{variable}}` placeholders
  must resolve, and a cycle is refused by Kahn's algorithm with
  `The flow graph contains a cycle.` Every message names the exact field.
- Limits: <=20 nodes, <=8 variables, <=8 triggers per flow, schedule interval
  60 s .. 30 days, import <=64 000 bytes, <=500 runs listed.
- `FlowStore` keeps **immutable versions** (`flow_versions` with per-version
  SHA-256), triggers and a run ledger; `FlowRunner` compiles + starts the
  mission, records `started`/`failed`, reconciles runs whose mission already
  finished, and `tick()` fires each due trigger **at most once per tick** with a
  hard budget and an `app_events` watermark for event triggers (no replay).
- Portable export/import: `{flow_schema, exported_by, flow, triggers, checksum}`
  where the checksum is SHA-256 over canonical JSON of `{flow, triggers}`; an
  edited document is refused, and overwrite requires an explicit flag.

### Triggers and schedules
- `manual`, `schedule` (interval or one-shot future `run_at`) and `event`
  (exact `app_events.event_type` matched above `last_event_id`); disabled flows
  are skipped, never executed late; one active run per flow.

### Storage - schema v14
- New tables `flow_definitions`, `flow_versions`, `flow_triggers`, `flow_runs`
  (+6 indexes). Migration 14 is applied after migration 13, so the Phase 11
  `automation_runs` ledger survives (covered by a test).

### Surfaces
- Service: 22 flow methods, including `resolve_flow_id` (full id, unique prefix
  or exact name) and mission-state injection for run reconciliation.
- CLI: `pc-agent flows list|show|create|update|validate|enable|disable|delete|
  run|runs|export|import|trigger add|list|enable|disable|delete|tick|stats`,
  `--json` on every sub-command, plus a live `Flows:` line in `pc-agent doctor`.
- HTTP API (loopback + bearer token): 18 flow routes including
  `POST /v1/flows/validate`, `POST /v1/flows/import`, `POST /v1/flows/tick`,
  `GET /v1/flows/stats`, `GET /v1/flow-runs`, `POST /v1/flows/{ref}/run` (202),
  `POST /v1/flows/{ref}/definition` (update) and the trigger routes. There is
  still no `PUT`/`PATCH` handler.
- GUI: 14th tab **Workflows** with real state (summary, flow list, JSON document,
  validate/save/update/import/show/run/export/tick, schedule and event trigger
  creation, recent runs) on the worker thread.
- Web command center: a **Workflow Builder** panel wired to the same endpoints.
- Diagnostics: `flows` section + `flows_summary_line()`; release audit check 26
  `flow_builder` (PASS).

### Windows fix folded in from real user evidence (0.73.0 build)
- The first native Windows run of 0.73.0 reported **746 passed, 8 skipped, 1
  failed**: `test_transformation_phase6_monitoring.py::test_live_snapshot_on_this_machine`
  failed because `top_processes` came back `[]` on Windows.
- Root cause: `system_monitor.WindowsProcessApi._resolve()` returns a
  `(function, export_name)` pair, and the memory entry points unwrapped
  `entry[0]` while `EnumProcesses` stored the raw tuple. Calling a tuple raised
  `TypeError`, the scan's `except` swallowed it, and every Windows snapshot
  silently reported zero processes. Fixed by unwrapping the enumeration entry
  the same way.
- Hardening from the same pass: the scan now retries `OpenProcess` with the
  Vista+ `PROCESS_QUERY_LIMITED_INFORMATION` right when the full query rights
  are refused, so protected-but-readable processes are still reported; the DLL
  loader is injectable, so the mapping (`_windows_process_rows`) and the
  callable-vs-tuple contract are covered by tests that run on every platform.
- `voice devices` now names the Windows privacy setting when no audio endpoint
  is reported, instead of implying the probe simply found nothing.

### Security hardening
- `redact_text()` now also removes bare provider keys (`sk-…`, `ghp_…`,
  `AIza…`, `xai-…`, `gsk_…`) that appear without an `api_key=` label, so a
  provider rejection message can never be persisted in `flow_runs.error`.
- Flows cannot run a shell, open a socket or skip a tool gate; import is
  bounded + checksum-verified + key-strict; triggers are capped and budgeted.

## 0.73.0 - TRANSFORMATION PHASE 11 Advanced Automation

### Offline computer vision (new)
- `src/pc_agent/perception.py` (Pillow only, no network, no OCR claim):
  `analyse_image` measures width/height/mode/format/size, mean brightness,
  contrast, five dominant colours and a `rows x columns` grid (<= 12) of mean
  brightness **and** ink density, plus `busy_cells`, `text_likely` and a
  plain-language `region_hint`.
- Ink density is **resolution-normalised** (each cell reduced to a 128 px working
  width and filtered with a gradient kernel in Pillow's C code), so the same page
  reads comparably at 1366x768 and 2560x1440; the old averaged-ink threshold that
  always reported `text_likely=False` is gone.
- `locate_template` is a two-stage **verified** search: coarse sampled scan ->
  non-neighbouring peak nomination -> full-pixel re-score at reduced size ->
  pixel refinement and full-pixel verification on the full-size screenshot. Only
  verified positions at or above the threshold are reported, and the reported
  `score` is that verified measurement.
- Caps: source <= 20 MB / 40 MP, grid <= 12, template <= 250 000 px, <= 40 000
  sampled search steps, scales 1.0/0.75/0.5, threshold 0.90, verification
  tolerance 24/255. A flat template is refused (`MIN_TEMPLATE_CONTRAST`).
- `image_crop` / `read_image_file` stay inside the workspace.

### Browser: real waiting and tab control
- `PageSession.wait_for(selector|text|url_contains, timeout<=120s, poll)` with
  `_wait_probe_js` (every interpolated value JSON-quoted).
- New tools `browser.wait` and `browser.tabs` (list/activate/close;
  `BrowserClient.close_tab` refuses to close the last page tab; the handler
  normalises backend replies so a non-mapping reply cannot crash it).

### Desktop: window state control (Windows)
- `WindowStateChange` + `set_window_state(selector, state, bounds=None)` for
  `minimize|maximize|restore|move|resize`; bounds within +/-32000, minimum
  120x120, clamped to the virtual screen, own window refused, `SetWindowPos`
  declared with explicit argtypes.
- New tool `desktop.window_state` with schema-level bounds validation.

### Automation ledger (schema v13)
- `automation_runs` table + two indexes; `AutomationRepository`
  (`record`, `get`, `list_runs`, `summary`) and `AutomationRunRecord`.
- Records allowed **and** refused actions; stores action/target label/short
  detail only - no page text, screenshots or file contents; 5 000-row retention;
  writes are best-effort so an audit failure never blocks a user action.

### Surfaces
- CLI `pc-agent automation capabilities|runs|stats|screen|locate` (`--json`).
- HTTP `GET /v1/automation/capabilities|runs|stats`,
  `POST /v1/vision/screen|locate`, `/v1/browser/wait`, `/v1/browser/tabs`,
  `/v1/desktop/window-state`.
- GUI 13th tab **Automation** (capabilities, ledger, screen analysis, template
  search, browser wait, recent runs) with startup refresh.
- Web Command Center panel reusing the same routes.
- Diagnostics `automation` section; new `pc-agent doctor` `Automation:` line;
  release audit grows to **25 checks** (`automation_audit`).

### Identity
- `__version__ = 0.73.0`, schema **v13**, transformation phase **11**,
  label `TRANSFORMATION PHASE 11 Advanced Automation`, previous transformation
  version `0.72.0`, **69** built-in tools.

### Tests
- `tests/test_transformation_phase11_automation.py` (23 tests).
- Full suite **754 passed, 1 skipped** (the skip needs a real display).

## 0.72.0 - TRANSFORMATION PHASE 10 Voice Pipeline

**Specialized voice, microphone privacy, two Windows acceptance fixes.**

### Voice pipeline (new, schema v12)

- **Push-to-talk by default.** The microphone is *off* until the user turns it
  on, and that consent is persisted (`voice.enabled` preference) so a restart
  never silently re-opens it. Wake-word listening is a separate, second opt-in.
- **Real capture backends, no fake audio:** native Windows `winmm` `waveIn*`
  through `ctypes` (no bundled binary, no third-party audio package) and ALSA
  `arecord` on Linux. When neither exists the subsystem reports
  `backend=none` with the reason instead of pretending to listen.
- **Local-first speech-to-text:** Windows System.Speech (built in, offline),
  `faster-whisper`, `whisper.cpp` (explicit model path). A cloud
  OpenAI-compatible `/audio/transcriptions` engine is optional and requires
  **both** `PC_AGENT_VOICE_CLOUD_STT=1` and a per-call consent flag; without
  consent the audio is never uploaded.
- **Offline text-to-speech:** Windows System.Speech and `espeak-ng`. Text
  reaches the engine through a temporary UTF-8 file (PowerShell) or stdin
  (espeak), never through a command line, so quoted text and shell
  metacharacters cannot turn into script.
- **Privacy ledger:** every capture writes a `voice_sessions` row (mode,
  duration, engine, character count, status) - transcripts only when
  `PC_AGENT_VOICE_STORE_TRANSCRIPTS=1`, audio only when
  `PC_AGENT_VOICE_RETAIN_AUDIO=1`. `pc-agent voice privacy --purge-audio` and
  `--clear-history` delete retained recordings and the ledger.
- **Silence is reported, not transcribed:** RMS/peak analysis marks an
  empty recording as "no speech detected".
- **Emergency stop integration:** an engaged stop refuses captures and forces
  the microphone state to `disabled`.

### Surfaces

- CLI: `pc-agent voice status|devices|enable|disable|wake-word|ptt|transcribe|say|sessions|privacy`.
- HTTP API: `/v1/voice/status`, `/v1/voice/devices`, `/v1/voice/privacy`,
  `/v1/voice/enable`, `/v1/voice/wake-word`, `/v1/voice/capture/{start,stop,cancel}`,
  `/v1/voice/speak`, `/v1/voice/transcribe`, `/v1/voice/sessions`,
  `/v1/voice/purge`, `/v1/voice/clear`.
- GUI: new **Voice** tab (12th) with persisted enable/wake-word toggles,
  push-to-talk start/stop/cancel, speak box, purge button and the live
  `● REC` / `○ ready` / `✕ off` indicator.
- Web Command Center: **Voice** panel with the same live state and controls.
- Diagnostics: `voice` section. Release audit: 24 checks, new
  `voice_privacy` check (warns if continuous wake-word listening is on).

### Windows acceptance fixes reported by the user

- **`test_worker_hard_timeout_kills_hung_tool_and_recovers` failed on Windows**
  because the probe hard-coded `python3`, which does not exist on Windows, so
  the allowlist rejected the call before the timeout path ran. The test now
  resolves a bare, PATH-resolvable interpreter (`python.exe` on Windows).
- **`test_live_snapshot_on_this_machine` failed on Windows**: process RSS was
  `None`, i.e. the monitor could not read `GetProcessMemoryInfo`. Root cause:
  `psapi!GetProcessMemoryInfo` was the only entry point tried and 64-bit
  HANDLEs were passed with ctypes' default 32-bit `int` restype, truncating
  them. `WindowsProcessApi` now probes `K32GetProcessMemoryInfo`,
  `psapi!GetProcessMemoryInfo` and the kernel32 forwarder in order, declares
  explicit `HANDLE`/buffer signatures, and is shared by the self-RSS reading
  and the top-process scan (which also gets `K32EnumProcesses`).

### Not changed

- Registered tools stay at **64** (voice is not a tool: it is a user-initiated
  surface, so an agent cannot open a microphone on its own).
- Phase 1-9 behaviour, schema migrations 1-11 and every safety gate are
  preserved; migration 12 only adds `voice_sessions`.


## 0.71.0 - TRANSFORMATION PHASE 9 Multi-Agent Studio

- Adds `src/pc_agent/agents.py` (specialized multi-agent runtime +
  Agent Studio). Schema **v11**: `agent_definitions` (role, persona
  prompt, builtin-tool allow-list, preferred provider, step/token
  budgets) and an `agent_runs` ledger linked to real tasks. Six
  builtin specialists seed idempotently (coordinator, researcher,
  document-worker, data-analyst, knowledge-keeper, reviewer);
  builtins are editable/disable-able but not deletable.
- Least privilege by construction: an agent run is an ordinary task
  whose orchestrator is built with a **filtered tool registry**, so
  disallowed tools do not exist for planning or execution (Safety
  Kernel, approvals, verification, recovery, budgets unchanged;
  dynamic tools never included). `PlannerOptions` gains
  `agent_system_prompt` + `allowed_tool_ids`: the AI planner sees a
  narrowed catalog plus the persona, and out-of-restriction plans
  are rejected pre-execution.
- Team runs: at most 6 assignments as a topological dependency
  graph (Mission Center level engine, bounded parallelism, failed
  dependencies block dependents without creating tasks). Explicit
  assignments work fully offline; otherwise the coordinator
  proposes them through one strictly validated JSON completion
  (goal treated as untrusted data). Delegation depth is exactly 1
  and no agent capability is exposed as a tool (registry stays 64).
- Surfaces: CLI `agents list|show|create|delete|enable|disable|run|
  team|runs|stats` + doctor `Agents:` line; API
  `GET/POST /v1/agents`, `GET/DELETE /v1/agents/{name}`,
  `POST /v1/agents/{name}/run` + `/v1/agents/team` (202 background),
  `GET /v1/agents/team/{id}|runs|stats`; GUI Agents tab (11th) with
  the Studio editor; web Agent Studio panel; diagnostics `agents`
  section; release audit gains a 23rd check (`agent_studio`).
  Parallel branches run without an interactive approval callback by
  design (Approval Center rules still apply). `AgentError` added to
  `pc_agent.errors`.


## 0.70.0 - TRANSFORMATION PHASE 8 Memory and Knowledge

- Adds `src/pc_agent/knowledge.py` + `src/pc_agent/embeddings.py`
  (Memory v2, knowledge base, embeddings, hybrid search, RAG). Schema
  **v10**: `memory_items` gains model-tagged packed-float32
  `embedding`/`embedding_model` and `tags` (a content edit atomically
  invalidates the vector); new `knowledge_documents`/`knowledge_chunks`
  tables store explicitly ingested text/PDF/Office files as bounded,
  SHA-256-deduped, model-tagged chunks (never silent OCR — a
  text-less PDF is an explicit error).
- Embeddings: offline deterministic signed feature-hashing embedder by
  default (`local-hashing-384`, light stemming + bigrams; honestly
  lexical, not neural); optional OpenAI/OpenRouter/OpenAI-compatible
  `/embeddings` or Ollama `/api/embed` backends via
  `PC_AGENT_EMBEDDING_PROVIDER|MODEL|DIMENSIONS` (64-2048). Vector
  search only compares same-model vectors; on embedder failure search
  degrades to a labelled `keyword-fallback`.
- Grounded answers: `knowledge ask` retrieves first and routes one
  cited (`[S#]`) prompt through the Phase 7 metering path with
  sources marked as untrusted data; no sources means no provider
  call. Ask stays user-invoked and license-gated on CLI/GUI; there is
  deliberately no agent ask tool.
- Surfaces: CLI `knowledge ingest|list|search|ask|remove|stats`,
  `memory search|reindex`, `note-set --tags` with fail-soft
  auto-embed; tools `knowledge.ingest`/`knowledge.search`
  (worker-excluded; registry 62 -> **64**); API
  `GET /v1/knowledge/documents|search|stats`, `POST
  /v1/knowledge/ingest|ask`, `DELETE /v1/knowledge/documents/{id}`,
  `GET /v1/memory/search` (API path ingestion workspace-contained);
  GUI Knowledge tab (10th); web Knowledge & Memory v2 panel; doctor
  `Knowledge:` line; diagnostics `knowledge` section; release audit
  gains a 22nd check (`knowledge_base`).
- `KnowledgeError` now lives in `pc_agent.errors` (re-exported from
  `pc_agent.knowledge`) to keep the tools import graph acyclic.

## 0.69.0 - TRANSFORMATION PHASE 7 Provider Platform

- Adds `src/pc_agent/provider_platform.py` (Provider Platform v2):
  a capability catalog (local/streaming/vision plus user-configured
  context sizes and per-1k prices via `PC_AGENT_MODEL_CONTEXT_TOKENS` /
  `PC_AGENT_PROVIDER_PRICES` — never guessed), strategy routing
  (`PC_AGENT_PROVIDER_STRATEGY`: default, local-first, fastest by
  measured median latency, cheapest by configured prices), local usage
  analytics (append-only `data/provider-usage.jsonl` with token counts
  from provider usage or clearly labelled estimates, latency, priced
  cost sums; no prompt text stored; `PC_AGENT_PROVIDER_ANALYTICS=0`
  disables), and a built-in 4-case evaluation suite with saved reports.
- Adds streaming completions: mock, OpenAI-compatible (SSE), Ollama
  (NDJSON), and Anthropic (SSE) adapters gain `stream_complete()`;
  `ModelRouter.stream_complete()` falls back only before the first
  delta. Gemini streaming is honestly reported as not implemented.
  Surfaces: `pc-agent ask --stream`, `POST /v1/providers/stream`
  (NDJSON), and the web Command Center Stream Ask box.
- New CLI: `providers capabilities|route|usage|evaluate`; new API:
  `GET /v1/providers/capabilities|usage|evaluations`,
  `POST /v1/providers/evaluations/run`; `/v1/providers` enriched with
  strategy/route/capability flags. Doctor gains a `Provider platform:`
  line; diagnostics gain a `provider_platform` section; the release
  audit gains a 21st check (`provider_platform`); the Tkinter
  Providers tab shows the platform summary and can run evaluations.
- No schema change (still v9); planning still uses non-streaming
  completions, so approvals, budgets, and verification are untouched.

## 0.68.0 - TRANSFORMATION PHASE 6 Live Monitoring

- Adds `src/pc_agent/system_monitor.py`: stdlib-only, fail-soft live
  telemetry — CPU % between samples (Windows `GetSystemTimes`, Linux
  `/proc/stat`), memory (`GlobalMemoryStatusEx`, `/proc/meminfo`, `sysconf`),
  disk per volume, agent process RSS/thread count/uptime, system uptime,
  network byte counters with up/down rates (`/proc/net/dev`, best-effort
  Windows `GetIfTable`), GPU via `nvidia-smi` when present (non-NVIDIA
  systems report an honest "unavailable" instead of an invented value),
  battery (`GetSystemPowerStatus`, `/sys/class/power_supply`), and a
  top-process-by-RSS table (Linux `/proc`; Windows `EnumProcesses` +
  `GetProcessMemoryInfo`, skipping protected processes).
- New surfaces: `pc-agent system status|sample`, authenticated
  `GET /v1/system` (`{snapshot, history}`, one stateful monitor per
  service), doctor `System:` line, a `system` section in diagnostics
  export, a 20th release-audit check (`system_monitor`), the web
  Command Center **System (live)** panel (real CPU/memory/disk bars,
  network rates, agent RSS, GPU, battery, top processes), and a
  `System:` live line in the Tkinter Command Center.
- No schema change (still v9): telemetry is live state, never stored or
  fabricated; first samples report rate fields as null until a baseline
  exists.

## 0.67.0 - TRANSFORMATION PHASE 5 JARVIS Command Center

- Adds `src/pc_agent/web_ui.py`: a self-contained web **Command Center**
  served by the local API at `/` (CSP + anti-framing headers; the page
  itself holds no data). It renders only live `/v1` state — overview,
  quick-run, tasks (run/cancel/delete), Mission Center (create, run,
  cancel, execution-graph levels by live status), Safety (emergency
  stop engage/clear), Approval Center approve/deny, schedules + run-due,
  a cursor-based live event tail, providers, tools, and public
  settings. The bearer token lives only in `sessionStorage`.
  `pc-agent api serve` now prints the Command Center URL.
- Adds a Tkinter **Command Center** tab with the same real state:
  4-second overview refresh, mission tree/detail, run/cancel through
  `MissionRunner`, emergency-stop controls, Approval Center decisions,
  and a live events tail, backed by new transport-independent
  `GuiController` methods. GUI mission runs deny inline approvals (the
  request appears in the Approval Center list); single-task GUI runs
  keep the existing in-band broker.
- No simulated widgets: CPU/GPU/monitoring panels deliberately wait
  for Phase 6 sensors.

## 0.66.0 - TRANSFORMATION PHASE 4 Mission Center

- Adds schema **v9** (`missions`, `mission_tasks`,
  `mission_dependencies`, `task_schedules`) and `src/pc_agent/missions.py`:
  missions are DAGs of existing tasks; cycles are rejected on link and
  before a run; `execution_levels()` exposes the graph; the ready-set
  runner executes with bounded parallelism
  (`PC_AGENT_MISSION_MAX_PARALLEL`, 1-8, default 3), giving every branch
  its own orchestrator/Safety Kernel/worker so approvals, budgets,
  verification, recovery, and the emergency stop behave exactly like
  single runs. A failed/cancelled/needs-input dependency leaves its
  dependents QUEUED with a `mission_blocked` event; the mission ends
  failed and can be re-run to resume.
- Adds `pc-agent missions create|list|show|add-task|link|run|cancel|delete`
  and API `/v1/missions` CRUD + `/run|cancel|tasks|graph`.
- Adds task schedules (`sch-…`): one-off and interval instructions via
  `pc-agent schedules add|list|toggle|delete|tick` and `/v1/schedules`;
  the API process ticks them every
  `PC_AGENT_SCHEDULER_INTERVAL_SECONDS` (0 disables). Interval schedules
  advance from the fire time; missed runs are not replayed. OS-level
  registration is intentionally out of scope.
- Doctor gains a `Mission center:` line, diagnostics a `missions`
  section, the release audit a `mission_center` check (19 total), the
  GUI doctor summary a `mission_summary` line, and profiles may carry
  the two new bounded settings.

## 0.65.0 - TRANSFORMATION PHASE 3 Local Service and API

- Adds `src/pc_agent/service.py`: one transport-independent **Local
  Service** that runs tasks on a background thread through the unchanged
  orchestrator/Safety Kernel/Approval Center stack (API approvals are
  never granted inline), plus task cancel/delete, approvals list/decide,
  safety stop/resume, events, settings, tools, and providers surfaces.
- Adds `src/pc_agent/api_server.py` and `pc-agent api status|serve`: a
  loopback-only token-authenticated JSON API (`/v1/...`, see
  `API_DOCUMENTATION.md`). The host is validated loopback in
  configuration; tokens come from `PC_AGENT_API_TOKEN` or a generated
  `data/api-token` file.
- Adds the durable **global event system** (schema **v8**, `app_events`,
  newest 10,000 rows retained) with producers for task lifecycle,
  approvals, safety, maintenance, and API startup; `pc-agent events list`
  and `GET /v1/events` poll with a `since_id` cursor.
- Adds optional **structured JSON logging** (`PC_AGENT_LOG_FORMAT=json`,
  rotating `data/logs/pc-agent.jsonl`, messages redacted).
- Adds **file-based settings profiles** (`src/pc_agent/profiles.py`,
  `pc-agent profiles list|show|save-current|activate|deactivate|delete`):
  a strict whitelist of non-secret, non-gate settings under
  `data/profiles/`, selected by `PC_AGENT_PROFILE` or
  `data/active-profile`; explicit environment values always win and a
  broken profile fails closed.
- Doctor gains a `Local service:` line, diagnostics gains a `service`
  section, the release audit gains a `local_service` check (18 total)
  and now requires `API_DOCUMENTATION.md`, and the GUI doctor summary
  gains a `service_summary` line.

## 0.64.0 - TRANSFORMATION PHASE 2 Safety Kernel

- Adds `src/pc_agent/safety.py` + `src/pc_agent/safety_worker.py`: every
  orchestrated tool call (including verification re-checks) now runs
  through the Safety Kernel with a **hard timeout** (tool timeout clamped
  by `PC_AGENT_SAFETY_MAX_TOOL_SECONDS`). Data-context tools execute in
  an isolated worker subprocess that is killed at the deadline;
  browser/desktop/vision tools keep their live factories on a bounded
  daemon-thread path whose late results are abandoned. Custom registries
  overriding `invoke()` and ad-hoc parent-only tools stay in-process.
- Adds a file-backed **emergency stop** (`pc-agent safety status|stop|
  resume`, `data/emergency_stop.json`, `PC_AGENT_EMERGENCY_STOP=1`
  override) checked on execute/resume/workflow/every step and inside
  the kernel; tasks stop as cancelled with an `emergency_stop` event.
- Adds the **Approval Center** (schema **v7**,
  `approval_center_requests`): non-interactive runs record a pending
  exact-scope request bound to the SHA-256 of the arguments;
  `pc-agent approvals list|show|approve|deny` decides it out-of-band and
  the decision is consumed once on resume. Approval records now store
  `arguments_sha256` instead of raw arguments (legacy records still
  verify).
- Adds optional **passphrase-encrypted database backups**
  (`maintenance backup --encrypt`, PBKDF2-HMAC-SHA256 + Fernet,
  `.db.enc`); verify/restore require the passphrase and re-check
  plaintext hash/size and SQLite integrity. The passphrase is never
  stored.
- New settings `PC_AGENT_SAFETY_HARD_TIMEOUTS`,
  `PC_AGENT_SAFETY_WORKER_ISOLATION`, `PC_AGENT_SAFETY_MAX_TOOL_SECONDS`
  and `PC_AGENT_SAFETY_APPROVAL_EXPIRY_MINUTES` (all safety-on by
  default). Doctor, GUI doctor text, diagnostics (`safety` section) and
  `release audit` (new `safety_kernel` check, 17 total) expose live
  safety state.
- No registered tool (62). The isolated worker is a separate process,
  not an OS sandbox; in-process abandoned threads cannot be
  force-killed (their results are discarded). Native Windows
  GUI/UIA/credential/installer/scheduler acceptance remains pending
  real Windows evidence.

## 0.63.0 - TRANSFORMATION PHASE 1 Canonical Baseline and Windows Acceptance

- Freezes the delivered Phase 60 tree as the canonical implementation
  source after reconciliation against audited GitHub Phase 58 commit
  `89610f3d18f9348b6fe8ead679bab81ee34155df`. Phase 59 Linux systemd-user
  scheduling and Phase 60 Linux idle/AC sensors are retained.
- Adds `src/pc_agent/baseline.py` as the single secret-free transformation
  identity. Doctor, GUI doctor text, diagnostics and `release audit` now
  expose the same Phase 1 label; release audit also checks the required
  baseline documents, `.env.example` ignore policy and direct dependency
  declaration.
- Repairs stale Phase 44 current-build labels, adopts `.env.local` as an
  ignored local secret file while keeping `.env.example` tracked, and
  declares `cryptography` directly for the PDF modules that import it.
- Adds root architecture, implementation-roadmap, feature-matrix,
  security, performance and test reports, plus the Transformation Phase 1
  reconciliation note and a native Windows acceptance pack.
- Adds no registered tool (62) and no schema migration (v6). Native
  Windows GUI/UIA/credential/installer/scheduler/live-update acceptance
  remains pending real Windows evidence; Linux results are not presented
  as Windows proof.

## 0.62.0 - PHASE 60 Linux Idle and AC Power Sensors

- Implements the Linux user-idle sensor behind the existing Phase 56
  signed `apply-when-idle` worker. It selects the current UID's
  interactive systemd-logind sessions, ignores manager/background and
  closing sessions, returns zero for an explicitly active session, and
  otherwise uses the minimum valid `IdleSinceHintMonotonic` age.
- Implements the Linux AC-power sensor from `/sys/class/power_supply`
  mains/battery state, falling back to logind `OnExternalPower` only when
  sysfs is absent or inconclusive. Sensor subprocesses are direct,
  bounded and shell-free.
- Hardens the shared apply gate so an unknown required idle or AC reading
  defers on every platform instead of being usable as implicit permission
  outside Windows. Default `stage-only`, signed HTTPS policy, report
  format and systemd units are unchanged.
- Adds no registered tool (62), schema migration (v6), configuration key or
  Python dependency.

## 0.61.0 - PHASE 59 Linux systemd-user Auto-Upgrade Scheduler

- Implements the reserved Linux scheduler profile behind the existing
  direct `update auto-install-schedule`, `auto-uninstall-schedule`,
  `auto-schedule-status` and `auto-status` commands and GUI controls. It
  installs a managed per-user systemd one-shot service plus a persistent
  hourly timer that wakes the unchanged signed `auto-run` worker.
- Linux installation preflights `systemctl --user`, refuses unmanaged
  same-named units or symlinks, writes units atomically, and restores the
  prior managed units if daemon-reload/enable fails. Status degrades to
  `unavailable` when no user manager is reachable; uninstall removes only
  managed units and this timer's enablement link.
- Keeps the signed HTTPS, downgrade, staging and report policies exactly
  as Phase 56. Linux `stage-only` is functional; `apply-when-idle`
  remains fail-closed on Linux until idle/power sensors are implemented.
  The macOS LaunchAgent profile remains planned.
- Adds no registered tool (62), schema migration (v6), configuration key or
  Python dependency.

## 0.60.0 - PHASE 58 Windows Auto-Upgrade Lock Hotfix

- Fixes the two real Windows Phase 56 failures in which a second
  `run_auto_upgrade` raised `PermissionError` while releasing the worker
  lock. An existing lock file opened with `a+b` starts at EOF, while
  `msvcrt.locking` acts from the current file position; the old code locked
  that EOF byte and then tried to unlock byte 0.
- The Windows worker now seeks to byte 0 before both `LK_NBLCK` and
  `LK_UNLCK` and records the exact lock offset. POSIX `flock`, the
  persistent lock file and all signed-update safety gates are unchanged.
- Adds an offset-faithful fake-`msvcrt` regression that reproduces the old
  unlock failure, proves repeated lock cycles and still refuses a nested
  concurrent worker.
- Adds no registered tool (62), schema migration (v6), configuration key or
  Python dependency.

## 0.59.0 - PHASE 57 Live Debian/Ubuntu APT Evidence

- Adds direct `pc-agent release debian-repository-acceptance` for a guarded
  live Debian/Ubuntu run: host/tool/admin checks, offline repository and
  embedded-package verification, an initially-absent package guard, APT
  update/candidate/install through a temporary source and isolated state,
  exact dpkg state, every installed file checked against the verified
  package manifest, and cleanup by purging only
  `self-healing-pc-agent`.
- Supports both repository trust modes honestly: signed repositories use a
  temporary `signed-by=` keyring, while an unsigned local repository is
  recorded as `trusted-yes-local-acceptance-only`. Declared distribution
  dependencies installed by APT are not auto-removed.
- Retains passed/failed self-hashed reports containing only command names,
  return codes, timing, output byte counts and output hashes; direct
  `debian-repository-acceptance-verify` strictly revalidates a passed
  current-version report offline.
- Adds no registered tool (62), schema migration (v6) or Python dependency.

## 0.58.0 - PHASE 56 Background Auto-Upgrade

- **Signed background worker**: new direct `pc-agent update auto-run`
  performs one bounded pass against the configured signed HTTPS feed. It
  requires a separate background opt-in plus online updates, a release
  public key/key ID and mandatory signed updates; downgrades,
  same-version bundles, HTTP feeds, unsigned bundles and hash/signature
  mismatches fail closed.
- **Safe modes and gates**: default `stage-only` downloads, verifies and
  stages but never applies. `apply-when-idle` may reuse the Phase 21
  backed-up apply path only inside the maintenance window with no active
  task, sufficient Windows user idle time, AC power when required and a
  verified staged signature. Optional no-dependency editable reinstall,
  bounded failure backoff and a single-instance lock are included.
- **Windows scheduler and evidence**: new `auto-status`,
  `auto-verify-report`, `auto-install-schedule`,
  `auto-uninstall-schedule` and `auto-schedule-status` commands manage a
  per-user Windows Scheduled Task and retained self-hashed state/run
  reports. GUI Signed Updates gains matching status/run/schedule controls.
  Linux systemd-user and macOS LaunchAgent profiles are structured as
  planned and fail closed. No registered tool (62 built-ins), schema or
  Python dependency change.

## 0.57.0 - PHASE 55 Windows HTTPS Update-Feed Hosting

- **Windows operational hosting package**: new vendor
  `pc-agent-release-admin build-hosting` and `verify-hosting` commands wrap
  a verified Phase 40 signed feed in a strict hash-manifest deployment
  bundle. The default profile includes a bounded built-in HTTPS static
  host plus Windows scheduled-task start/register/unregister scripts; the
  IIS profile includes hardened `web.config` files and versioned
  deployment/rollback scripts using an existing certificate thumbprint.
- **Live HTTPS evidence**: new direct `pc-agent update hosting-check`
  fetches one public feed, signature and portable ZIP, then verifies TLS,
  same-origin URLs, cache policy, size/SHA-256, portable-bundle structure
  and Ed25519 signature without staging or applying the update. Its
  secret-safe self-hashed report can be checked offline with
  `hosting-check-verify`.
- **Cross-platform structure, Windows-first delivery**: Linux Nginx/Caddy
  and macOS Nginx/Caddy profiles are registered as planned and fail closed;
  no Linux/macOS deployment or live acceptance is claimed in this phase.
  No registered tool (62 built-ins), schema or Python dependency change.

## 0.56.0 - PHASE 54 Live GNOME Keyring/KWallet Evidence

- **Live Linux Secret Service acceptance**: new direct
  `pc-agent credentials linux-secret-service-acceptance` command identifies
  the session-bus owner of `org.freedesktop.secrets` through `gdbus` or
  `busctl`, accepts only GNOME Keyring or KWallet/KSecretService, and then
  stores, reads back, deletes and proves absent one fresh random probe
  under acceptance-only Secret Service attributes.
- **Real-key safety proof**: existing provider keys are compared only in
  memory before and after the probe; neither their values nor hashes are
  recorded. The probe is supplied to `secret-tool` on stdin, uses separate
  attributes from provider keys, and cleanup is attempted after failures.
- **Retained offline evidence**: passed/failed runs write a secret-safe,
  self-hashed JSON report without overwriting an existing file;
  `linux-secret-service-acceptance-verify` strictly revalidates a passed
  report offline. No registered tool (62 built-ins), schema, credential
  resolution order or Python dependency change.

## 0.55.0 - PHASE 53 Live Anthropic Verification

- **Direct live Anthropic check**: new `pc-agent providers verify-anthropic`
  command makes one bounded 32-token Anthropic Messages API request using
  the already-configured credential, bypassing router fallback and retries.
  It sends only a fixed public connectivity prompt and requires the exact
  `PC_AGENT_ANTHROPIC_OK` response plus provider, model and usage evidence.
- **Secret-safe retained report**: passed and failed runs write a JSON report
  without the API key or response text; the report stores the credential
  source, endpoint origin, models, token usage, timing, response SHA-256,
  ordered checks and a report self-hash. Existing reports are not overwritten.
- **Offline report verifier**: `pc-agent providers verify-anthropic-report`
  strictly validates a passed current-version report and its self-hash with
  no network call. Vision remains explicitly `not_tested`; no registered
  tool (62 built-ins), schema, provider route or Python dependency change.

## 0.54.0 - PHASE 52 Windows Repository Timestamp Hotfix

- **Windows repository builder fix**: Phase 50 failed on Windows when the
  Debian APT repository builder called `os.utime(...,
  follow_symlinks=False)`, which CPython does not implement there. Both
  Debian and RPM repository builders now use a shared capability-checked
  helper that rejects symlinks before using the portable Windows timestamp
  call.
- **Regression coverage**: tests simulate a platform without
  `os.utime` follow-symlink support, verify zero file/directory timestamps,
  prove symlink targets are not modified, and build/verify both repository
  formats under the reduced capability set.
- **Boundaries preserved**: no repository format, package bytes, signing,
  CLI, registered tool (62 built-ins), schema, or Python dependency change.

## 0.53.0 - PHASE 51 RPM Repository

- **Deterministic RPM repository builder**: new direct
  `pc-agent release rpm-repository` command publishes a verified Phase 48
  `.rpm` under `Packages/`, derives standard primary/filelists/other XML
  from the real RPM header and payload, compresses it deterministically,
  and writes content-addressed `repodata` files plus a SHA-256-complete
  `repomd.xml` and strict JSON repository manifest.
- **Optional OpenPGP metadata signing**: `--signing-key` uses the
  operator's existing system GnuPG key to create `repomd.xml.asc` and an
  exported public key. The application does not generate, import, store,
  or accept a private key or passphrase; unsigned repositories and the
  unsigned RPM package itself are reported explicitly.
- **Strict offline verifier**: `pc-agent release rpm-repository-verify`
  rejects extra/missing files and symbolic links, revalidates the manifest,
  repomd and XML hashes/content, embedded `.rpm`, and—when present—the
  exported public key and repomd signature without invoking dnf/yum/rpm.
- **Honest boundary**: the builder creates static files only; it does not
  sign the RPM package itself, host/configure/install/upgrade from the
  repository, or claim live Fedora/RHEL evidence. No registered tool
  (62 built-ins), schema change, or Python dependency.

## 0.52.0 - PHASE 50 Debian APT Repository

- **Deterministic APT repository builder**: new direct
  `pc-agent release debian-repository` command publishes a verified
  Phase 47 `.deb` under `pool/`, generates identical `Packages` and
  `Packages.gz` indexes for the `all` and `amd64` views, and writes a
  `Release` file with MD5Sum, SHA1, SHA256, and SHA512 index hashes plus a
  strict JSON repository manifest.
- **Optional OpenPGP repository signing**: `--signing-key` uses the
  operator's existing system GnuPG key to create `Release.gpg`, `InRelease`,
  and an exported public keyring. The application does not generate, import,
  store, or accept a private key or passphrase; unsigned repositories are
  reported explicitly as unsigned.
- **Strict offline verifier**: `pc-agent release debian-repository-verify`
  rejects extra/missing files and symbolic links, revalidates the manifest,
  Release hashes, package indexes, embedded `.deb`, and—when present—the
  exported public key and both OpenPGP signature forms without invoking apt
  or dpkg.
- **Honest boundary**: the builder creates static files only; it does not
  host/configure/install/upgrade from the repository or claim live
  Debian/Ubuntu evidence. No registered tool (62 built-ins), schema change,
  or Python dependency.

## 0.51.0 - PHASE 49 Windows UTF-8 Test Hotfix

- **Windows test encoding fix**: the Phase 45 scope test failed on Windows
  because it read UTF-8 project documentation with Python's cp1252 locale
  default. All locale-dependent test `.read_text()` calls from Phases
  45-48, two Phase 40 JSON writes, and generated credential-helper state
  reads/writes now specify `encoding="utf-8"` explicitly.
- **Regression guard**: a new AST test scans checked Python under `src/`
  and `tests/` and fails if any `Path.read_text`/`Path.write_text` call
  lacks an explicit UTF-8-family encoding. The original failure was
  reproduced and then verified fixed under a forced non-UTF-8 locale.
- **Boundaries preserved**: tests/docs only; no runtime behavior,
  registered tool (62 built-ins), schema change, Python dependency,
  Administrator/Developer Mode requirement, or `PYTHONUTF8`/locale setting
  is needed.

## 0.50.0 - PHASE 48 Native RPM Package

- **Deterministic native `.rpm` builder**: new direct
  `pc-agent release rpm-package` command creates a standard RPM v4 binary
  package (Lead, signature header, main header, gzip `newc` cpio payload)
  with sealed immutable header regions, header SHA1/SHA256 and MD5
  signature digests, and per-file SHA-256 metadata. It packages the
  verified Phase 41 POSIX installer under `/usr/share/self-healing-pc-agent`,
  plus `/usr/bin` launchers and a desktop entry, with no scriptlets.
- **Strict offline verifier**: `pc-agent release rpm-verify` validates the
  lead, canonical header layout and region trailers, signature digests and
  sizes, provides/requires, header file metadata against the cpio payload,
  launcher/desktop wiring, and the embedded POSIX package without invoking
  rpm or shell code. The generated package was also checked with real
  RPM 4.20.1 tools: all header and MD5 digests verify OK.
- **Honest installation boundary**: rpm installation places only the
  system payload; first launch runs the existing per-user POSIX setup and
  may need network access for Python dependencies because wheels are not
  bundled. No PKG/DMG, OpenPGP signing, repository metadata or signing,
  registered tool (62 built-ins), schema change or Python dependency.

## 0.49.0 - PHASE 47 Native Debian Package

- **Deterministic native `.deb` builder**: new direct
  `pc-agent release debian-package` command creates a standard Debian `ar`
  archive with `debian-binary`, `control.tar.gz`, and `data.tar.gz`, strict
  control metadata, calculated Installed-Size, complete `md5sums`, and an
  embedded SHA-256 manifest. It packages the verified Phase 41 POSIX
  installer under `/usr/share/self-healing-pc-agent`, plus `/usr/bin`
  launchers and a desktop entry, with no maintainer scripts.
- **Strict offline verifier**: `pc-agent release debian-verify` validates
  archive order and bounds, tar paths/modes, control fields, package hashes,
  launcher/desktop wiring, and the embedded POSIX package without invoking
  dpkg or shell code. Automated tests also use host `dpkg-deb` when available
  and execute the system launcher against a fake per-user installer.
- **Honest installation boundary**: dpkg installation places only the
  system payload; first launch runs the existing per-user POSIX setup and
  may need network access for Python dependencies because wheels are not
  bundled. No RPM/PKG/DMG, apt repository, repository/package signing,
  registered tool (62 built-ins), schema change or Python dependency.

## 0.48.0 - PHASE 46 Windows Inno Acceptance Harness

- **Guarded live-Windows installer acceptance**: new direct
  `pc-agent release installer-acceptance` command builds a fresh Phase 38
  source package, compiles it with a locally installed Inno Setup 6
  `ISCC.exe`, silently installs it into an isolated acceptance directory,
  and silently uninstalls it after verification. It never downloads Inno
  Setup and never targets the product's default install directory.
- **Evidence and safety checks**: the harness refuses an existing stable
  AppId registry entry or Start Menu group, verifies every installed payload
  hash, shortcuts, uninstall registry state, installed CLI/GUI commands,
  and post-uninstall removal. Synthetic `.env`, `data`, and `workspace`
  sentinel hashes must survive byte-for-byte. Process/Inno logs and a JSON
  report are retained; `installer-acceptance-verify` revalidates a passed
  report, retained setup/source hashes, and sentinels.
- **Boundaries preserved**: the compiled setup remains unsigned;
  Authenticode, trusted timestamping, SmartScreen reputation, interactive
  wizard appearance, and a live Windows transcript are not claimed until
  the harness is run in a signed-in Windows session. No registered tool
  (62 built-ins), schema change or Python dependency. Automated suite: 541
  passed, 1 skipped; Ruff clean.

## 0.47.0 - PHASE 45 macOS Keychain Credentials

- **macOS OS credential backend**: `PC_AGENT_CREDENTIAL_STORE=auto` now
  selects the current user's macOS Keychain on Darwin, and the new explicit
  `macos` mode is available beside `auto`, `windows`, `linux`, and `env`.
  Provider keys use one fixed Keychain service with the canonical provider
  name as the account through Apple's system `/usr/bin/security`.
- **Secret-safe interactive writes**: stored keys are sent to
  `security -q -i` as one bounded hexadecimal command on stdin, never in
  argv. A versioned value prefix makes raw/hex lookup output unambiguous,
  writes are read back, deletes are confirmed, stderr is not echoed, and
  locked/denied/malformed/timeout cases fail closed. Effective macOS keys
  are labelled `macos-keychain` in Doctor/provider status.
- **Compatibility preserved**: environment/`.env` precedence, hidden direct
  credential prompts, Windows/Linux credential behavior, and Phase 39
  verified `.env` cleanup are unchanged. The backend is fake-tested without
  claiming a live macOS Keychain transcript. No registered tool (62
  built-ins), schema change or Python dependency. Automated suite: 536
  passed, 1 skipped; Ruff clean.

## 0.46.0 - PHASE 44 Linux Secret Service Credentials

- **Linux OS credential backend**: `PC_AGENT_CREDENTIAL_STORE=auto` now
  selects the freedesktop Secret Service on Linux, and the new explicit
  `linux` mode is available beside `auto`, `windows`, and `env`. Provider
  keys use stable application/provider/purpose attributes through the
  system `secret-tool` argv interface.
- **Secret-safe subprocess boundary**: stored keys are sent only on stdin,
  never as command-line arguments; service reachability is probed without
  reading a real key, writes are read back, deletes are confirmed, external
  stderr is not echoed, and errors/timeouts fail closed. Effective Linux
  keys are labelled `linux-secret-service` in Doctor/provider status.
- **Compatibility preserved**: environment/`.env` precedence, hidden direct
  credential prompts, Windows Credential Manager behavior, and Phase 39
  verified `.env` cleanup are unchanged. A Linux desktop Secret Service
  provider and `secret-tool` are prerequisites; live keyring evidence and
  macOS Keychain are not claimed. No registered tool (62 built-ins), schema
  change or Python dependency. Automated suite: 530 passed, 1 skipped;
  Ruff clean.

## 0.45.0 - PHASE 43 Windows POSIX Test Hotfix

- **Windows/NTFS execute-bit failure fixed**: the deterministic Phase 41
  POSIX package test still verifies hashes, inventory, payload list, and
  script contents on Windows, but asserts the POSIX `S_IXUSR` filesystem bit
  only on a POSIX host, matching production package verification.
- **Missing `sh` failure fixed**: POSIX `sh -n` checks run only when a POSIX
  `sh` is available, and the shell install/uninstall execution test skips on
  a non-POSIX host or missing `sh` instead of raising `WinError 2`. On a
  POSIX host it still executes install/reinstall/uninstall and proves user
  `.env`, workspace, and data files are preserved.
- **Runtime behavior unchanged**: generated POSIX packages are still started
  with `sh ./install.sh`, and the installer continues to normalize installed
  script permissions with `chmod +x`. No registered tool (62 built-ins),
  schema change or dependency. Automated suite on the Linux build host:
  525 passed, 1 skipped; Ruff clean.

## 0.44.0 - PHASE 42 Windows Symlink Test Hotfix

- **Windows `WinError 1314` fixed**: the Phase 39 `.env` symlink-rejection
  scenario is now a separate test. If an unprivileged Windows account cannot
  create a symbolic link without Developer Mode or the required privilege,
  only that dedicated test skips with the OS reason; it no longer fails the
  whole suite or hides the other fail-closed cleanup checks.
- **Security behavior unchanged**: when symlink creation succeeds, the test
  still proves that cleanup planning rejects a symlink `.env` and leaves its
  target unchanged. Production credential cleanup, providers, tools, GUI,
  PDF, installer, and update behavior are unchanged.
- No registered tool (62 built-ins), schema change or dependency.
  Automated suite on the Linux build host: 523 passed, 1 skipped; Ruff
  clean.

## 0.43.0 - PHASE 41 POSIX Installer Source Package

- **Deterministic POSIX package**: direct `pc-agent release posix-source`
  stages the runtime release subset with root/payload shell scripts, an
  exact payload file list, POSIX README, and SHA-256 manifest. It excludes
  tests, the real `.env`, user data, virtual environments, caches, private
  keys, and patcher artifacts; existing output is never overwritten.
- **Per-user Linux/macOS install**: `install.sh` requires Python 3.11+,
  creates `.venv`, installs the local package, runs doctor/GUI checks, and
  creates CLI/GUI wrappers plus a Linux desktop entry or macOS `.command`.
  It uses no administrator elevation and preserves `.env`, data, and
  workspace files during overlay reinstall.
- **Conservative uninstall**: default removal covers launchers and `.venv`;
  `--remove-app --yes` deletes only exact publisher-listed payload paths
  after unsafe-path rejection. User `.env`, data/workspace files, and OS
  credentials remain preserved.
- **Fail-closed verification and Linux execution tests**: `posix-verify`
  rejects inventory/hash/script-safety failures without executing shell
  code, while automated Linux tests run install/uninstall in an isolated
  fake home. This is not a native DEB/RPM/PKG/DMG, signed/notarized
  package, live macOS proof, Keychain/Secret Service support, or desktop
  automation parity. No registered tool (62 built-ins), schema change or
  dependency. Automated suite: 520 passed, 1 skipped; Ruff clean.

## 0.42.0 - PHASE 40 Signed Update Feed Publisher

- **Complete static feed package**: vendor-only
  `pc-agent-release-admin publish-feed` signs one verified portable bundle
  and creates `stable.json`, the versioned ZIP/signature, customer
  public-key configuration, hosting notes, and a SHA-256 publisher
  manifest in one new directory.
- **Offline package verification**: `pc-agent-release-admin verify-feed`
  rejects missing/extra/unsafe files and hash mismatches, parses the feed
  with the Phase 24 customer rules, verifies the portable bundle, and
  requires the detached Ed25519 signature to match the expected public key
  and key ID.
- **Private-key boundary**: publication refuses existing output, builds in
  a temporary sibling and verifies before publishing, scans output for the
  raw/base64url private seed, and never copies or prints it. Feed base URLs
  are HTTPS outside loopback and cannot contain credentials, queries, or
  fragments.
- **Honest hosting boundary**: the verified tree is upload-ready for a
  vendor's own static HTTPS host, but this phase does not create an
  account, configure DNS/TLS, upload files, prove a public host live, or add
  background/silent updating. No registered tool (62 built-ins), schema
  change or dependency. Automated suite: 514 passed, 1 skipped; Ruff clean.

## 0.41.0 - PHASE 39 Secure `.env` Provider-Key Cleanup

- **Verified duplicate removal**: direct `pc-agent credentials
  remove-env-keys [provider]` removes a project-root `.env` API-key line
  only when the identical effective value is already present in the
  configured OS credential store. `--dry-run`, approval/`--yes`, and
  secret-free `--json` output are supported.
- **Fail-closed statuses**: missing, empty, unstored, and mismatched keys are
  reported and left untouched. Duplicate lines are removed together only
  when the loader-effective last value matches; comments, other settings,
  line endings, and a UTF-8 BOM are preserved.
- **Atomic, revalidated apply**: the approved plan is checked again against
  both `.env` and the credential store, written through a verified
  same-directory temporary file, atomically replaced, and re-read before
  success is reported. Symlinked, malformed, oversized, unavailable-store,
  or concurrently changed inputs refuse.
- **No plaintext backup**: a secret-free receipt under
  `data/credential-cleanups/` records providers, statuses, line numbers and
  file hashes without API-key values or fingerprints. No registered tool
  (62 built-ins), schema change or dependency. Automated suite: 510 passed,
  1 skipped; Ruff clean.

## 0.40.0 - PHASE 38 Windows Installer Source Package (unsigned)

- **Deterministic installer source**: direct `pc-agent release
  installer-source` stages the runtime subset of the allow-listed release
  tree and generates an explicit Inno Setup 6 script plus a SHA-256 manifest.
  It excludes tests, the real `.env`, databases, user data, virtual
  environments, caches, private keys and patcher artifacts; existing output
  is never overwritten.
- **Per-user Windows setup wiring**: the generated script uses stable AppId
  `C80BA7A3-9651-46C2-9D45-D1DADF2AD063`, `PrivilegesRequired=lowest`, and
  `{localappdata}\Programs\Self-Healing PC Agent`; it runs the existing
  PowerShell venv installer, creates GUI/CLI Start Menu shortcuts, and
  uninstall preserves non-empty `data`, `workspace`, `.env` and Credential
  Manager entries.
- **Fail-closed verification**: direct `installer-verify` rejects wrong
  version/AppId, unsafe or duplicate paths, missing/extra files, hash/size
  mismatches, symbolic links, forbidden payload entries, administrator
  mode and incomplete Inno `[Files]` wiring. Compiled files under `output/`
  are counted but do not invalidate the source.
- **Guarded compile helper**: `scripts/build_windows_installer.ps1` runs
  tests/Ruff, builds/verifies fresh source, finds a locally installed Inno
  Setup 6 `ISCC.exe`, fails clearly without downloading anything if absent,
  and reports the compiled setup EXE SHA-256 on Windows.
- **Honest boundary**: this is a hash-verified **unsigned** installer source
  package, not a live-compiled/signed installer; no Authenticode,
  SmartScreen reputation, PyInstaller bundle, admin install or automatic
  Python/Inno download is claimed. No registered tool (62 built-ins), schema
  change or dependency. Automated suite: 505 passed, 1 skipped; Ruff clean.

## 0.39.0 - PHASE 37 PAdES Baseline B-B Signature Profile

- **Optional PAdES-oriented profile**: the existing direct CLI/GUI signer can
  create `ETSI.CAdES.detached` signatures with `--profile pades-bb` or the GUI
  Profile selector. The legacy `adbe.pkcs7.detached` profile remains the
  default. Generated CMS signed attributes are exactly content-type,
  message-digest and ESS signing-certificate-v2; there is no CMS signing-time,
  and the Signature Dictionary `/M` value provides the claimed UTC time.
- **Verified ESS binding and B-B structure**: inspection validates the
  signing-certificate-v2 hash and issuer/serial reference, exposes signed
  attribute OIDs and claimed-time source, and reports
  `bb_structure_verified` or `bb_structure_incomplete` for
  `ETSI.CAdES.detached` signatures. New PAdES output is published only after
  structure, ByteRange/CMS and signer-fingerprint verification pass.
- **Bounded algorithms and workflows**: RSA/ECDSA with SHA-256/384/512,
  ESIC ExtensionLevel 2, existing incremental/DocMDP handling and optional
  RFC 3161 signature timestamps are supported. DSA/EdDSA identities are
  refused for this profile and can still use the legacy signer.
- **Honest boundary**: `bb_structure_verified` is a local structural result,
  not ETSI certification, Adobe AATL/Acrobat trust, qualified-signature or
  legal validity; external PAdES conformance remains
  `not_externally_evaluated`. No registered tool (62 built-ins), schema change
  or dependency. Automated suite: 500 passed, 1 skipped; Ruff clean.

## 0.38.0 - PHASE 36 LTV DSS Embedding

- **Incremental DSS/VRI embedding**: direct `pc-agent pdf inspect-ltv` and
  `pc-agent pdf add-ltv`, plus GUI **Inspect/Add LTV DSS** controls, inspect or
  append a catalog Document Security Store containing DER certificates, CRLs
  and OCSP responses. Existing DSS entries are merged, the source remains an
  exact byte prefix, and every prior approval signature/DocTimeStamp must
  verify again before publication.
- **Relevant-material enforcement**: signer/TSA certificates embedded in CMS
  or timestamp tokens are collected through cryptographically verified issuer
  links. Explicit local certificates, fresh CRLs and fresh authorized OCSP
  responses must be relevant to a signed field; unrelated, stale, malformed,
  unauthorized or revoked-at-signing evidence refuses publication. VRI keys
  use the prescribed SHA-1 identifier of the complete signature Contents
  value, including padding.
- **Optional timestamp seal**: the new DSS can immediately be sealed with the
  Phase 35 DocTimeStamp workflow, which sends only the sealed revision hash
  to the exact user-selected HTTPS TSA and never sends the PDF. DSS embedding
  itself is allowed for DocMDP P=1/P=2/P=3; the optional seal retains the
  Phase 35 P=1 refusal.
- **Honest boundary**: certificate/TSA trust and PAdES status remain
  `not_evaluated`; DSS embedding is not a claim of PAdES B-LT/B-LTA, Adobe
  LTV-enabled status or legal validity. No registered tool (62 built-ins),
  schema change or dependency. Automated suite: 495 passed, 1 skipped; Ruff
  clean.

## 0.37.0 - PHASE 35 DocTimeStamp Incremental Revisions

- **DocTimeStamp-only append**: direct `pc-agent pdf timestamp` and the GUI
  **Add DocTimeStamp** control append one ETSI.RFC3161 `/DocTimeStamp`
  dictionary as a true incremental revision, without a signer CMS signature,
  PKCS#12 identity, password or local `/M` signing time.
- **Whole-revision RFC 3161 imprint**: only the digest of the new field's
  ByteRange bytes is sent to the exact user-selected HTTPS TSA. The source
  remains an exact byte prefix, prior approval signatures and document
  timestamps must verify again, and repeated operations support renewal.
- **Fail-closed DocMDP handling**: P=1 refuses, P=2 permits only an existing
  blank signature field, and P=3/no DocMDP permits a dedicated invisible
  field. Invalid or unsupported existing fields never publish output; signer
  signing after a DocTimeStamp remains refused.
- **Honest boundary**: TSA trust remains `not_evaluated`, and this is not
  PAdES/LTV, Adobe trust-program or legal validation. No registered tool
  (62 built-ins), schema change or dependency. Automated suite: 489 passed,
  1 skipped; Ruff clean.

## 0.36.0 - PHASE 34 Incremental Multi-Signature Signing

- **True incremental signing**: the existing direct CLI/GUI PDF signing
  workflow can now add one detached CMS signature to an already signed PDF
  without rewriting earlier revisions. The complete source file remains an
  exact byte prefix of the new output, and the previous XRef-stream object
  number is reserved to avoid reader/cache collisions on later revisions.
- **Preservation verification**: publication requires every existing
  signature to verify again with its original ByteRange, CMS/Contents sizes,
  DocMDP permission and signer fingerprints, while the new signature covers
  the whole output and matches the selected PKCS#12 certificate. Results
  report existing, preserved and total signature counts.
- **Fail-closed DocMDP handling**: P=1 blocks all changes, P=2 permits only
  an existing blank signature field, P=3 also permits creating a signature
  field, and a new certification signature is refused after any existing
  signature. Invalid, malformed, unsupported and document-timestamp fields
  also stop incremental signing without publishing output.
- **Honest boundary**: no registered tool (62 built-ins), schema change,
  dependency, trust/PAdES-LTV/legal evaluation or DocTimeStamp-only
  incremental revision. Automated suite: 483 passed, 1 skipped; Ruff clean.

## 0.35.0 - PHASE 33 System Trust Store

- **Opt-in system anchors**: direct PDF signature-trust validation can merge
  bounded CA certificates from the Windows ROOT certificate store or
  OpenSSL's default certificate store with explicit workspace anchors.
  Separate `--system-trust-anchors` and
  `--timestamp-system-trust-anchors` CLI flags/GUI checkboxes keep signer and
  TSA trust choices independent; both remain off by default.
- **Transparent effective trust**: output reports local, system-added and
  effective signer/TSA anchor counts plus store source and skipped entries.
  Duplicates are removed by SHA-256 fingerprint, malformed/non-CA/wrong-
  KeyUsage entries are skipped, and an unavailable/empty store fails closed.
- **Security boundary**: system roots only add candidate anchors; existing
  chain, validity, revocation, certificate-policy and name-constraint checks
  still decide trust. Store loading makes no network/provider call and never
  exports the full root list or certificate bytes.
- **Honest boundary**: this is not Adobe trust-program validation
  (`adobe_trust_status=not_evaluated`) or a complete WebPKI/PAdES/LTV/legal
  evaluator. No OS-store modification, registered tool (62 built-ins),
  schema change or new dependency. Automated suite: 478 passed, 1 skipped;
  Ruff clean.

## 0.34.0 - PHASE 32 Certificate Policies & Name Constraints

- **Bounded certificate-policy graph**: direct signer and TSA trust chains
  process certificate policies, required policy OIDs, explicit-policy and
  any-policy inhibition, `PolicyConstraints`, and DER `PolicyMappings`.
  CLI/GUI controls expose separate signer/TSA required policies; no
  registered tool is added (62 built-ins).
- **Always-on name constraints**: DNS, email, URI-host, IP, directory-name,
  registered-OID and exact otherName permitted/excluded subtrees are checked
  below each constraining CA. Unsupported candidate name forms fail closed
  when they cannot be proven to satisfy an active constraint.
- **Fail-closed overall statuses**: policy and name-constraint violations
  produce `trusted_chain_policy_invalid` or
  `trusted_chain_name_constraints_invalid`; accepting unknown revocation never
  bypasses either failure.
- **Honest boundary**: policy qualifiers are ignored, and this remains a
  bounded RFC 5280 subset—not a complete WebPKI, Adobe, qualified-policy,
  PAdES/LTV or legal validator. No OS trust store, schema change or new
  dependency. Automated suite: 474 passed, 1 skipped; Ruff clean.

## 0.33.0 - PHASE 31 Online Revocation

- **Opt-in online revocation**: direct PDF signature-trust validation remains
  offline by default and checks explicit local CRLs first. With
  `--online-revocation` (CLI) or the GUI network checkbox/confirmation, it may
  use certificate CRL distribution points and OCSP responders only when local
  evidence is unknown. No registered tool is added (62 built-ins).
- **Verified CRL/OCSP evidence**: downloaded CRLs pass the same issuer,
  signature, key-usage, freshness and serial checks as local files. OCSP
  responses must match the certificate/issuer hashes and freshness window and
  be signed by the issuer or a directly delegated `OCSPSigning` responder.
- **Bounded and fail-closed transport**: at most 3 endpoints per certificate,
  50 requests, 5,000,000-byte CRLs, 262,144-byte OCSP responses and a 1–60
  second timeout. Private endpoints require a separate explicit option.
  Network, signature, freshness or authorization failures remain `unknown`.
- **Honest boundary**: no OS/Adobe trust store, certificate-policy or
  name-constraint engine, PAdES/LTV or legal evaluation. RSA-PSS OCSP
  responses remain unknown because their PSS parameters are not exposed by
  the OCSP API. Schema stays v6 and no dependency is added. Automated suite:
  468 passed, 1 skipped; Ruff clean.

## 0.32.0 - PHASE 30 RFC 3161 Timestamping

- **Optional direct timestamping**: `pc-agent pdf sign --timestamp-url` and
  GUI TSA fields request an RFC 3161 token for the newly generated CMS
  signature value. Only its hash and a nonce are sent to the exact HTTPS TSA;
  the PDF, private key and PKCS#12 password are never sent. No registered
  tool is added (62 built-ins), and no TSA is contacted unless a URL is set.
- **Verified token attachment**: granted responses must echo the nonce and
  requested hash; TSTInfo imprint, token CMS signature, TSA validity and
  exact critical `timeStamping` EKU are verified before the token is inserted
  as a CMS unsigned attribute and the signed PDF is re-verified/published.
- **Inspection and trust**: signature timestamps and `ETSI.RFC3161`
  document timestamps are reported independently. Phase 29 accepts separate
  TSA trust anchors (or reuses signer anchors) and uses token time only when
  the TSA chain/CRL state is locally trusted.
- **Honest boundary**: no default TSA, OS/Adobe trust program, OCSP/online
  CRL fetching, incremental DocTimeStamp creation, qualified-policy,
  PAdES/LTV or legal evaluation. Schema stays v6 and no dependency is added.
  Automated suite: 462 passed, 1 skipped; Ruff clean.

## 0.31.0 - PHASE 29 PDF Trust Validation

- **Direct-only offline trust validation**: `pc-agent pdf validate-signatures`
  and a GUI **Validate Signature Trust** control evaluate signatures that
  pass Phase 27. No registered tool is added (62 built-ins), and no network,
  operating-system trust store, OCSP responder or CRL download is used.
- **Basic X.509 chain building**: embedded signer certificates are linked to
  explicit workspace trust anchors with issuer-signature, validity-at-signing,
  CA BasicConstraints/path-length and relevant KeyUsage checks. A self-signed
  certificate is trusted only when supplied exactly as an anchor.
- **Local CRL evaluation**: fresh CRLs must be signed by the selected issuer;
  revocation is judged against CMS signing time, with later revocations
  reported separately. `trusted_valid` requires complete passing CRL state,
  while trusted-chain/unknown-CRL requires an explicit acceptance option.
- **Honest boundary**: certificate policies, name constraints, OS/Adobe root
  programs, OCSP, online CRL fetching, RFC 3161 trust, PAdES/LTV and legal
  validity remain not evaluated. Raw certificate/CRL bytes are not reported;
  schema stays v6 and no dependency is added. Automated suite: 457 passed,
  1 skipped; Ruff clean.

## 0.30.0 - PHASE 28 PDF Signature Creation

- **Direct-only signature creation**: `pc-agent pdf sign` and a GUI
  **Sign PDF** control sign an unencrypted unsigned PDF into a new file with a
  workspace `.p12`/`.pfx` identity. No registered tool is added (62 built-ins),
  so neither the planner nor a task can request a signing key or password.
- **CMS signing**: SHA-256/384/512 detached `adbe.pkcs7.detached` signatures
  use a selected blank Sig field or a newly created visible/invisible field;
  optional reason, location, contact and DocMDP P1-P3 certification are
  supported. Additional PKCS#12 certificates are embedded in the CMS.
- **Secrets and verification**: the PKCS#12 password is hidden-prompt or
  masked-dialog only and is never persisted or reported. The source stays
  byte-identical, output is never overwritten, and the temporary signed PDF is
  published only after Phase 27 verification confirms the ByteRange digest,
  CMS signature and expected certificate fingerprint.
- **Honest boundary**: encrypted or already signed PDFs are refused; trusted
  roots, revocation, RFC 3161 timestamps, PAdES/LTV and legal validity remain
  not evaluated. Schema stays v6 and no dependency is added. Automated suite:
  453 passed, 1 skipped; Ruff clean.

## 0.29.0 - PHASE 27 PDF Signature Inspection

- **Direct-only signature inspection**: `pc-agent pdf inspect-signatures`
  and a GUI Documents control report signature fields, ByteRange coverage,
  DocMDP permission, CMS contents and embedded certificates. No registered
  tool is added (62 built-ins).
- **Local CMS verification**: a bounded DER parser checks the exact
  ByteRange digest against `messageDigest`, verifies CMS signatures with
  embedded RSA/RSA-PSS/ECDSA/DSA/Ed25519/Ed448 signer certificates, and
  reports signing time/certificate validity details.
- **Honest trust boundary**: locally consistent signatures are labelled
  `cryptographically_valid_untrusted`. Trusted roots, revocation, RFC 3161
  timestamps, PAdES/LTV and legal policy are not evaluated; no signature is
  created and no network call is made.
- **Safety**: read-only, encrypted PDFs refused, bounded file/pages/fields/
  CMS/certificates/signers, and raw CMS/certificate bytes are not printed.
  Schema stays v6. Automated suite: 448 passed, 1 skipped; Ruff clean.

## 0.28.0 - PHASE 26 PDF Password Security

- **Direct-only password workflows**: `pc-agent pdf security|protect|unprotect`
  and masked GUI dialogs inspect PDF encryption, create AES-256 protected
  copies, change user/owner passwords, and create unencrypted copies after
  the current password verifies.
- **Secrets stay off recorded surfaces**: passwords come only from hidden
  prompts, never from CLI/task/tool arguments, configuration, database
  records or text/JSON results. No registered tool is added (62 built-ins).
- **Verified no-overwrite output**: temporary PDFs are tested locked,
  unlocked with user/owner passwords, rejected with a wrong password, and
  checked for pages/permissions before publication; sources never change.
- **Boundaries**: digitally signed PDFs are refused, forgotten-password
  recovery/cracking is not included, and existing PDF tools still require an
  unencrypted copy. `pypdf[crypto]` supplies AES; schema stays v6.
  Automated suite: 443 passed, 1 skipped; Ruff clean.

## 0.27.0 - PHASE 25 PDF Form Workflows

- **Two new PDF tools (62 built-ins)**: `pdf.inspect_form` reports field
  names/types/values/options/states/flags/widgets in an existing AcroForm,
  and `pdf.fill_form` writes validated text, choice, checkbox and
  radio/button values into a new PDF.
- **No-overwrite verified fill**: source hashes are checked, filling happens
  in a temporary file, and the result is reopened to compare every changed
  and untouched field before publication. Read-only, signature, pushbutton,
  unknown fields and invalid choices/states are refused.
- **CLI, tasks and GUI**: `pc-agent pdf inspect-form|fill-form`, repeatable
  task `--form-field Name=Value`, and Documents-panel Inspect/Fill controls.
  AI planning cannot propose either form tool; orchestrated verification and
  output-collision recovery independently re-inspect source and output.
- **Boundaries**: no form creation/design, XFA conversion, password flow,
  OCR, flattening or digital signing. No runtime dependency or database
  migration (schema stays v6). Automated suite: 437 passed, 1 skipped; Ruff
  clean.

## 0.26.0 - PHASE 24 Online Signed Update Download

- **Explicit online downloads, still default-off**: new
  `PC_AGENT_ONLINE_UPDATES`, `PC_AGENT_UPDATE_FEED_URL`,
  `PC_AGENT_UPDATE_CHANNEL`, timeout and maximum-bundle settings gate
  direct CLI/GUI update-feed access. Nothing polls or downloads in the
  background.
- **Strict same-origin feed**: format-1 JSON rejects duplicate/unknown
  fields, wrong applications/channels, credential/query/fragment URLs,
  cross-origin bundle or signature links, public HTTP, unsafe resolved
  addresses, cross-origin redirects and oversized responses.
- **Verified download receipts**: downloaded bundles must match the feed
  size and SHA-256, then pass the existing portable-bundle and detached
  Ed25519 checks. Feed, bundle, signature and receipt are retained under
  `data/updates/downloads` for inspection.
- **CLI and GUI**: `pc-agent update online-check|online-download|online-stage`
  and matching Doctor-tab controls. Check-only mode does not claim bundle
  authentication, download mode does not stage, and online staging never
  auto-applies; Phase 21 apply/rollback remain separate explicit steps.
- **Trust reporting**: Doctor and release audit report online-update state,
  only the feed host/channel and whether the strongest signed policy is
  configured. Invalid/key-mismatched downloads cannot be staged.
- No new tool (60 built-ins), runtime dependency or database migration
  (schema stays v6). Automated suite: 431 passed, 1 skipped; Ruff clean.

## 0.25.0 - PHASE 23 PDF Table Extraction

- **One new PDF tool (60 built-ins)**: `pdf.extract_tables` reconstructs
  bounded tables from embedded PDF character geometry into a new verified
  JSON file, or CSV when exactly one table is detected.
- **Embedded text only, never OCR**: scanned/image-only PDFs honestly
  produce zero tables in JSON instead of inventing rows. Sources are never
  modified and outputs are never overwritten.
- **Deterministic geometry pipeline**: PDFium character boxes are grouped
  into words, lines, candidate cells and recurring x-range columns with
  configurable minimum rows/columns.
- **Bounds**: 100 selected pages, 200 tables, 1,000 rows per table, 50
  columns, 100,000 cells and 2,000,000 examined embedded characters.
- **CLI, tasks and GUI**: direct `pc-agent pdf extract-tables`,
  deterministic planner support with `--pdf-table-min-*` options, and
  Documents-tab controls. AI planning cannot propose the writer.
- **Verification and recovery**: orchestrated runs independently rerun the
  local extraction and compare rendered bytes/counts/hash; output
  collisions can recover to a fresh file while preserving the original.
- No new runtime dependency or database migration (schema stays v6);
  Phase 22's `pypdfium2` is reused. Automated suite: 423 passed, 1 skipped;
  Ruff clean.

## 0.24.0 - PHASE 22 Scanned PDF OCR

- **One new PDF tool (59 built-ins)**: `pdf.ocr` renders up to 10 selected
  pages locally with PDFium and transcribes scanned pages through the
  configured vision-provider route into a new verified .txt/.md/.json file.
- **Embedded pages stay local by default**: pages with embedded text reuse
  that text without a provider send; `--include-text-pages` /
  `--pdf-ocr-all-pages` can deliberately OCR them too. Sources are never
  modified and outputs are never overwritten.
- **Bounded, gated sends**: 72-300 DPI, rendered/sent pixel and byte bounds,
  per-page output-token bounds, a 200,000-character result cap, the
  default-off `PC_AGENT_ALLOW_VISION` gate, exact approvals and task budgets.
- **Honest verification**: output hash/size, selected/source pages,
  independently re-extracted embedded text, counts, provider/model and
  sent-image metadata are checked. Provider OCR wording is not resent or
  claimed to be deterministically word-verified, and OCR is never
  auto-retried.
- **CLI, tasks and GUI**: direct `pc-agent pdf ocr`, deterministic planner
  support with `--pdf-ocr-*` options, and Documents-tab OCR controls. AI
  planning cannot propose the OCR writer.
- New runtime dependency `pypdfium2>=5,<6`; no database migration (schema
  stays v6). Automated suite: 416 passed, 1 skipped; Ruff clean.

## 0.23.0 - PHASE 21 Signed Offline Updates

- **Detached Ed25519 release signatures**: portable bundle version and
  SHA-256 can be signed with a vendor-only release key and verified against
  `PC_AGENT_RELEASE_PUBLIC_KEY`. Optional key-ID pinning and
  `PC_AGENT_REQUIRE_SIGNED_UPDATES=1` fail closed; unsigned bundles need an
  explicit `--allow-unsigned` staging decision.
- **Safe staging, apply and rollback**: `pc-agent update
  check|stage|apply|rollback|list|show` verifies bundle and staged hashes,
  applies an approved overlay only after complete backups, auto-restores on
  apply failure, preserves stale/created files, and refuses rollback over
  later user edits.
- **Vendor signing tool**: `pc-agent-release-admin keygen|sign` creates a
  release Ed25519 keypair and signs verified bundles. Private-key files and
  key containers are now hard-excluded from release bundles.
- **GUI and audit integration**: the Dashboard gains check/stage/apply/
  rollback/list controls with confirmations, Doctor shows only public-key
  configuration status, and the release audit reports signed-update trust.
- No network updater, planner tool, database migration or runtime
  dependency (schema stays v6; built-ins stay at 58). Automated suite:
  408 passed, 1 skipped; Ruff clean.

## 0.22.0 - PHASE 20 PDF Text + Page Transforms

- **Four new PDF tools (58 built-ins)**: `pdf.extract_text`,
  `pdf.search_text`, `pdf.reorder_pages` and `pdf.rotate_pages` complete
  the PDF text/reordering/rotation work Phase 9 left for later.
- **Embedded text, never OCR**: bounded extraction to text/Markdown/JSON
  and exact-phrase search with page snippets. Image-only/scanned PDFs
  honestly report `has_embedded_text=false`; limits are 500 selected pages
  and 5,000,000 extracted characters.
- **Verified page transforms**: full/reverse reordering and selected
  quarter-turn rotations write only new PDFs. Core and orchestrator
  verification compare page count, SHA-256, per-page content fingerprints,
  exact output order and final rotations; sources are never modified.
- **CLI, tasks and GUI**: direct `pc-agent pdf extract-text|search|reorder|
  rotate`, deterministic planner/task support and Documents-tab controls.
  AI planning may propose only read-only `pdf.search_text`; the writers
  remain deterministic/direct.
- **Recovery and strict schemas**: new writers participate in fresh-output
  collision recovery, and the tool schema validator now enforces declared
  enums such as text format and quarter-turn degrees.
- No database migration or runtime dependency (schema stays v6).
  Automated suite: 395 passed, 1 skipped; Ruff clean.

## 0.21.0 - PHASE 19 Direct Anthropic Provider

- **Direct Anthropic Messages API**: `anthropic` is now a real provider
  rather than a reserved route slot. It uses HTTPS Bearer authentication,
  `anthropic-version: 2026-10-21`, top-level system blocks, bounded
  `max_tokens`, text-block extraction and the shared bounded HTTP transport.
- **Vision and budgets**: the adapter supports inline base64 PNG, JPEG,
  WebP and GIF blocks for the existing approved vision path and forwards
  Anthropic `input_tokens` / `output_tokens` usage into Phase 17 task
  budgets.
- **Current API semantics**: never sends `temperature`, `top_p` or `top_k`
  because API version `2026-10-21` removed them; Anthropic refusals,
  errors and empty responses fail as provider errors instead of fake
  successes.
- **Credentials and routing**: uses the existing `ANTHROPIC_API_KEY` / Windows
  Credential Manager resolution and is skipped when no key is configured.
  CLI, GUI, Doctor and diagnostics show status/model/source only, never a
  key. The default model is `claude-sonnet-5-5`.
- **Production-audit wording**: when a stored provider key still has an
  environment/`.env` copy, the existing warning now says to remove that
  `.env` line if Credential Manager should become the effective source.
- No new registered tool, runtime dependency or database migration (schema
  stays v6; built-ins stay at 54). Automated suite: 387 passed, 1 skipped;
  Ruff clean.

## 0.20.0 - PHASE 18 Production Hardening + Portable Windows Release

- **Production audit**: `pc-agent release audit` performs read-only,
  secret-free checks for versions, database integrity/schema, folders, TLS,
  provider route, credential sources, optional gates, finite budgets,
  self-repair baseline, licensing posture and release contents. Doctor and
  the GUI Dashboard expose the same audit summary.
- **Deterministic portable bundles**: `release build|verify|info` creates
  and verifies fixed-timestamp ZIPs from a strict allow-list with a sorted
  SHA-256 manifest. Real `.env` files, databases, data, workspace contents,
  virtual environments, patchers and caches are excluded; verification
  rejects traversal, duplicates, symlinks, forbidden paths and mismatches
  without extracting the archive.
- **Database maintenance**: `maintenance backup|list|verify|restore` uses
  SQLite's backup API with strict hash/schema manifests. Restore is
  approval-gated and creates a verified pre-restore backup before replacing
  the live database.
- **Redacted diagnostics**: `diagnostics export` and `doctor --json`
  include versions, paths, provider/tool names and counts/statuses while
  excluding API/license keys, task instructions, memory values, prompts,
  tool arguments/outputs and document contents.
- **Portable Windows helpers**: plain-text PowerShell install, conservative
  exact-path uninstall and release-build scripts, plus GUI/CLI `.cmd`
  launchers. Installation remains editable source in `.venv`; there is no
  signed executable or auto-updater.
- No new registered tool, runtime dependency or database migration (schema
  stays v6; built-ins stay at 54). Automated suite: 376 passed, 1 skipped;
  Ruff clean.

## 0.19.0 - PHASE 17 Core Self-Repair + Token/Task Budgets

- **Deterministic core self-repair**: a default-off
  `PC_AGENT_ALLOW_SELF_REPAIR` gate enables `pc-agent self-repair
  baseline|diagnose|stage|apply|rollback|list|show`. Only `src/`, `tests/`
  and `pyproject.toml` are baseline-eligible; only baseline restoration and
  generated Python-cache deletion can be staged. Extra files are reported,
  never deleted, and no AI tool/planner path can invoke repair.
- **Isolated, reversible repairs**: staging excludes `.env`, runs fixed
  static/import/full argv checks with a scrubbed environment, and blocks
  corrupt, invalid, stale or version-mismatched plans. Apply backs up live
  targets, post-checks, automatically rolls back on failure, and supports a
  later manual rollback.
- **Per-task budgets**: orchestrated execute/resume/workflow replay now
  enforce provider token, logical provider-call, planned-tool, planned-step
  and wall-time limits. Reported provider usage is preferred; otherwise a
  transparent estimate is used. A pre-call token reserve prevents sending a
  request that cannot fit. Vision calls use the same tracker, reports/events
  expose a secret-free snapshot, and budget failures are non-retryable.
- **Bootstrap repair entry point**: `pc-agent-self-repair` exposes the same
  gated repair commands without importing the full CLI/orchestrator, so a
  syntax-damaged core module can still be staged and restored.
- **CLI/GUI/Doctor visibility**: execution commands accept five budget
  override flags; Doctor reports budget defaults plus baseline/latest repair
  status; the GUI Tasks tab has budget fields and the Dashboard has a
  read-only Self-Repair Diagnose action. Repair mutations remain CLI-only.
- No new runtime dependency or database migration (schema stays v6).
  Automated suite: 362 passed, 1 skipped; Ruff clean.

## 0.18.1 - PHASE 16.1 Credential Manager Hotfix

- **Simulated Win32 `ERROR_NOT_FOUND` on real Windows**: the credential
  backend checked `ctypes.get_last_error()` before an injected library's
  own `last_error`. A simulated `CredReadW` miss therefore appeared as
  Win32 error 0 on Windows (but passed on Linux, where ctypes has no
  `get_last_error`). Injected test doubles are now authoritative; the
  real uninjected `advapi32` path still uses ctypes' captured last error.
- **Live GUI tab count**: the Windows-only GUI construction test still
  expected 7 notebook tabs after Phase 15 added Images. It now expects 8.
- No production feature, schema, dependency or credential-format change.
  Automated suite: 335 passed, 1 skipped; Ruff clean.

## 0.18.0 - PHASE 16 Windows Credential Manager

- **Secure provider-key storage**: one Windows generic credential per
  provider slot, accessed through native `CredReadW`, `CredWriteW`,
  `CredDeleteW` and `CredFree` calls in `advapi32.dll`. No new runtime
  dependency is added.
- **Non-breaking resolution**: environment/`.env` keys still win. When a
  key is absent there, production Settings loads fall back to Windows
  Credential Manager according to `PC_AGENT_CREDENTIAL_STORE=auto|windows|env`.
- **Dedicated CLI**: `pc-agent credentials status|list|set|delete|migrate`.
  `set` uses a hidden double prompt and accepts no secret argument;
  `migrate` copies existing environment keys after approval but never
  edits `.env`; status output contains provider names/sources only.
- **Doctor/GUI visibility**: backend availability, stored provider names
  and effective credential sources are shown without values. No task or
  AI tool can read credentials, and there is no credential `get/show`
  command.
- **Test isolation**: the suite defaults to `PC_AGENT_CREDENTIAL_STORE=env`
  and exercises OS storage through injected in-memory/simulated Win32
  backends, never a developer's real vault. Automated suite: 334 passed,
  1 skipped; Ruff clean.

## 0.17.0 - PHASE 15 Image Tools + Vision

- **Five new image tools**: `image.inspect`, `image.convert`,
  `image.transform`, `image.compare` and `image.analyze`; the built-in
  count grows from 49 to 54.
- **Local Pillow engine**: PNG/JPEG/GIF/BMP/WebP/TIFF input inspection;
  PNG/JPEG/WebP/BMP/TIFF outputs; crop/resize/thumbnail/rotate/flip/
  grayscale; 20 MB, 10,000 px and 40 megapixel caps; EXIF orientation
  normalization; metadata-free, never-overwrite outputs.
- **Provider vision**: `CompletionRequest` now carries bounded image input.
  Gemini receives an inline Interactions image part,
  OpenRouter/OpenAI-compatible providers receive data URLs, and Ollama
  receives its native base64 `images` array. The offline mock refuses
  image requests, and routing skips providers that do not declare vision
  capability.
- **New default-off settings**: `PC_AGENT_ALLOW_VISION` and
  `PC_AGENT_VISION_MAX_IMAGE_BYTES`. Every orchestrated or direct vision
  call requires approval for the exact image/question before bytes leave
  the PC. AI planning may propose only local inspect/compare; vision is
  excluded from automatic recovery retries.
- **Surfaces**: `pc-agent image inspect|convert|transform|compare|analyze`,
  task `--image-*` options, doctor diagnostics, and a GUI Images tab.
- **Verification**: local outputs are independently re-inspected. Vision
  answers are bound to a re-inspection of the exact approved image without
  sending it a second time. Automated suite: 325 passed, 1 skipped; Ruff
  clean.

## 0.16.0 - PHASE 14 Windows Desktop UI Automation

- **Seven new desktop tools**: `desktop.windows`, `desktop.window_inspect`,
  `desktop.window_focus`, `desktop.window_click`, `desktop.window_type`,
  `desktop.send_keys` and `desktop.screenshot`; the built-in count grows
  from 42 to 49.
- **Native Windows backend**: Win32 enumerates/foregrounds windows and
  captures screenshots; Microsoft UI Automation control patterns inspect,
  click and set exact elements. Windows installs now also depend on
  `comtypes>=1.4,<2`; that dependency is skipped on macOS/Linux.
- **New default-off settings**: `PC_AGENT_ALLOW_DESKTOP_AUTOMATION` and
  `PC_AGENT_DESKTOP_AUTOMATION_TIMEOUT_SECONDS`.
- **Direct and task surfaces**: `pc-agent desktop
  windows|inspect|focus|click|type|keys|screenshot`, deterministic task
  options, doctor diagnostics, and GUI Desktop-tab windows/inspect/
  screenshot controls.
- **Safety**: focus/click/type/keys require a fresh approval bound to exact
  arguments. Windows/elements must resolve exactly; password elements,
  agent self-targeting, unreadable editors and dangerous key chords are
  refused. Typing is read back. All desktop automation tools are excluded
  from automatic recovery retries, and AI planning may propose only the two
  read-only desktop tools.
- **Verification**: automated suite 313 passed, 1 skipped; Ruff clean.
  Desktop tests use a simulated UIA backend because the build sandbox is
  Linux, so `pc-agent desktop windows` on the target Windows machine is the
  first live native confirmation.

## 0.15.1 - PHASE 13.1 Gemini Routing Fix

- **Gemini could never be routed**: config parsing lowercases provider
  names (`gemini`), but the router factory/status compared against
  `"Gemini"`, so a configured Gemini key was silently skipped and
  `providers list` showed it as an unknown, reserved slot. Names are
  now canonical lowercase end-to-end (factory, model lookup, status,
  adapter name), with a regression test that builds the route from
  settings. Found on a real Windows install whose `.env` had a live
  provider order.
- Tests are hermetic again: three CLI tests that invoked `main()`
  without `--root` now run against a temp root, so a developer's real
  `.env` (provider keys, gates) cannot leak into offline tests.

## 0.15.0 - PHASE 13 Browser Automation

- The agent can now drive a **real browser session you start yourself**
  (Edge/Chrome with `--remote-debugging-port` and a throwaway profile)
  through a standard-library Chromium DevTools client — off until
  `PC_AGENT_ALLOW_BROWSER=1`, localhost-only. Six tools (42 built-ins):
  `browser.status`, `browser.snapshot` (read-only), `browser.open`,
  `browser.click` (unique selector/exact text; refuses to guess),
  `browser.type` (framework-safe fill with read-back; password/file
  fields refused) and `browser.screenshot` (new verified workspace
  PNGs). Actions need fresh per-argument approvals and are never
  auto-retried; AI planning may propose only the two read-only tools.
- New direct `pc-agent browser ...` commands (actions confirm unless
  `--yes`; `--allow-private` for one exact local open), `run`
  `--browser-open/--browser-click-*/--browser-type-*` options,
  deterministic browser intents, GUI Web-tab status/page-read buttons,
  and `doctor` reports the gate plus live connectivity.
- Verified against a real Chromium 131 (local test pages + Wikipedia:
  open/click/type/screenshot, password refusal, approval and
  read-back verification) plus 14 deterministic fake-CDP/WebSocket
  tests. No new dependency; schema stays v6.

Still not included: launching/stopping the browser for you, downloads
or uploads, tab management, login automation (password typing stays
refused by design), Windows UI automation of other apps, image
analysis, or secure OS credential storage.

## 0.14.3 - PHASE 12.3 One-Key Confirmations

- Interactive-terminal confirmations now read a **single keypress**
  (`Y` approves, `N`/Ctrl+C cancels, no Enter). Line-based `input()`
  proved unreliable under Windows Terminal (ConPTY could feed phantom
  empty lines), so terminals use `msvcrt` (Windows) / cbreak reads
  (POSIX), while piped stdin keeps the typed-word prompt from 12.2.
- Paste bursts cannot approve: keys already queued before the prompt
  are drained via `msvcrt` (replacing the ConPTY-unfriendly
  console-buffer approach), and a `Y` with more text queued behind it
  (e.g. a pasted `--yes`) is discarded with a note. A trailing Enter
  after a lone `Y` is tolerated; stray Enter presses show a hint
  instead of cancelling.
- 6 additional prompt tests (19 in the prompt-fix file) plus live
  terminal (PTY) verification of the paste, Enter-mashing and
  pasted-`yes` scenarios. No dependency or schema change.

## 0.14.2 - PHASE 12.2 Approval Prompt Robustness

- Confirmation prompts no longer deny instantly on an empty or wrong
  answer. An empty line (typically the leftover newline of a pasted
  command) re-asks; a wrong non-empty answer is shown back and re-asked
  up to 3 times; Ctrl+C cancels cleanly without a traceback. Only the
  exact word (`APPROVE`, or case-sensitive `DEACTIVATE` for license
  removal) ever confirms, so re-asking grants nothing.
- The Windows console-input flush now uses fully declared ctypes
  signatures (`HANDLE` return type, typed arguments) and validates the
  console handle, so the 12.1 paste guard works reliably; the re-ask
  loop above protects every platform even if flushing is unavailable.
- 5 additional prompt tests (13 in the prompt-fix file); no dependency
  or schema change.

## 0.14.1 - PHASE 12.1 Approval Prompt Fix

- CLI confirmation prompts (Git writes, task approvals, license
  deactivation) now discard console input that was typed or pasted
  *before* the prompt appeared, so pasting several commands at once can
  no longer feed the next command into an approval prompt (and a
  pre-pasted `APPROVE` cannot approve anything either). When pending
  text is discarded, the prompt says so. Piped stdin is untouched;
  `--yes` remains the non-interactive path.
- A denied direct Git write now prints the exact command to re-run with
  `--yes`, and a denied task approval explains that only the word
  `APPROVE` typed after the prompt counts. Matching stays
  case-insensitive for `APPROVE`. 8 new tests; no dependency or schema
  change.

## 0.14.0 - PHASE 12 Approved Subprocess & Git

- Command execution is now possible behind a **default-off** policy gate
  (`PC_AGENT_ALLOW_SUBPROCESS=1`). Only bare program names on the
  allow-list (`PC_AGENT_COMMAND_ALLOWLIST`, default
  `git,python,python3,py,node,npm`) can run, always as an argv array with
  `shell=False`; Windows `.cmd`/`.bat` shims (e.g. npm) run through
  `cmd.exe /d /s /c` only after rejecting cmd-special characters. The
  engine scrubs the child environment (no API keys/secrets), silences
  stdin, enforces a hard timeout with process kill (1-300s,
  `PC_AGENT_COMMAND_TIMEOUT_SECONDS`) and caps captured output at 64 KiB
  per stream.
- Six local Git tools (`git.status/log/diff_summary` read-only,
  `git.init/add/commit` writes) plus `commands.run` — 36 built-ins in
  total. Commands and Git writes always need a **fresh approval bound to
  the exact arguments** (never inherited, never from workflow memory) and
  are **never auto-retried** by recovery; verification re-reads Git state
  (e.g. HEAD must match the new commit) or the exit code independently.
- Deterministic planner understands Git requests (`Show git status`,
  `Git add the file`, `Git commit "message"`); `commands.run` is only
  reachable via `run --command "..."`. AI planning may propose read-only
  Git queries only. New direct `pc-agent git ...` commands prompt before
  writes (`--yes` pre-approves), and the GUI gains a Git Status button;
  `doctor` reports the subprocess gate and git availability.
- No new dependency or database migration (schema stays v6); 16 new
  tests covering the engine, policy gate, approvals, timeouts, Git
  end-to-end workflow, CLI and GUI paths.

Still not included: remote Git operations (clone/fetch/pull/push),
branch/checkout/merge/reset, arbitrary shells or pipes, browser or
Windows UI automation, image tools, or secure OS credential storage.

## 0.13.0 - PHASE 11 Multi-Step Planner

- Tasks can now chain 2-5 tools in one instruction with **then**. Each
  clause is planned by the existing single-step deterministic planner;
  later clauses may reference earlier steps' promised outputs, inherit the
  previous output implicitly ("then extract pages 1-2"), and are bound at
  run time to the previous step's actual output (recovery renames are
  followed). AI planning accepts a strictly-validated `steps` array with
  `$step<N>.output_path` references as the only allowed binding syntax.
- Every step runs the unchanged pipeline: fresh per-step approvals for
  external roots (never inherited), acceptance criteria, deterministic
  verification, per-step checkpoints and the bounded recovery budget
  (applied per step, attempt numbers unique across the pipeline). Reports
  (CLI text/JSON, GUI) list each step's status; resume continues at the
  first unfinished step without re-running checkpointed tools.
- Verified chains are recorded as multi-step workflows and replayed by
  `workflow run` / the planner fallback after live revalidation of every
  step. Manual `task recover` clones re-run the stored chain.
- No new dependency or database migration (schema stays v6); 16 new tests
  including crash-resume, approval denial mid-chain, AI forward-reference
  rejection and a forged multi-step workflow.

Still not included: list/folder output bindings, branching or parallel
steps, mid-flight re-planning, subprocess/Git adapters, browser or Windows
UI automation, image tools, or secure OS credential storage.

## 0.12.0 - PHASE 10 Verified Workflow Reuse

- Workflow memory now stores the exact reusable plan material (normalized
  instruction, validated arguments, external roots, expected pages) under a
  precise per-instruction key; repeats to fresh outputs grow the same
  workflow's verified count. Phase 7 legacy records stay usable through
  their last task's stored plan.
- New `pc-agent workflow list|show|run|delete` (exact key, unique prefix or
  `latest`). `workflow run` creates a new task and re-runs the stored plan
  only after live registry/allow-list revalidation, with fresh approvals
  (never inherited), acceptance criteria, checkpoints and bounded recovery.
- Planner fallback: when deterministic/AI planning cannot plan an
  instruction, a still-valid verified workflow for the same normalized
  instruction is reused instead of `needs_input`. Execution reports show
  `Verified workflow recorded/reused: wf-...` in CLI (text + JSON) and GUI.
- GUI Tasks tab gains **Run Verified Workflow...**. No new dependency or
  database migration (schema stays v6); 10 new tests including a forged
  workflow rejection and external-root approval re-check.

Still not included: fuzzy/semantic workflow matching or argument
re-binding, multi-step plans, secure OS credential storage, subprocess/Git
adapters, browser or Windows UI automation, or image tools.

## 0.11.0 - PHASE 9 Deeper Office

- PDF: `pdf.extract_pages` (validated `1-3,5` selections, order preserved)
  and `pdf.split` (one verified single-page PDF per page, max 200 pages,
  partial outputs cleaned on failure).
- DOCX: `office.docx_replace_text` (exact paragraph replacement that also
  works when Word split a word across runs) and
  `office.docx_append_paragraphs`, both writing new verified documents.
- XLSX: `office.xlsx_set_cells` writes values or `=formulas` into one sheet
  of an existing workbook (other package parts preserved, stale calcChain
  dropped) and `office.xlsx_verify_formulas` recomputes every formula with
  a bounded deterministic evaluator (arithmetic, ranges,
  SUM/AVERAGE/MIN/MAX/COUNT/PRODUCT/ABS/ROUND; circular/unsupported
  formulas reported explicitly). Computed cached values are written back
  so other readers see results immediately.
- Independent verification for all six tools, Phase 7 acceptance criteria
  for the writers (including PDF page-count expectations), checkpoints, and
  fresh-output collision recovery for the editors.
- Deterministic planner intents (`Extract pages...`, `Split...`,
  `Replace...`, `Append...`, `Set cell...`, `Verify formulas`) with new
  `--pages/--find/--replace/--cell/--value` run options; direct
  `pdf extract|split` and `office docx-replace|docx-append|xlsx-set|
  xlsx-formulas` CLI; new GUI Documents controls. 29 built-ins total, no
  new dependency, no database migration (schema stays v6). 11 new tests.

Still not included: cross-paragraph/header DOCX matches, XLSX styles/
charts/pivots creation, lookup/date/conditional formula functions, PDF
rotation or PDF text extraction, subprocess/Git adapters, browser or
Windows UI automation, image tools, or multi-step planning.

## 0.10.0 - PHASE 8 File Operations

- Five new workspace-confined tools (23 built-ins total): `files.copy`
  (streamed, hash-checked, exclusive-create), `files.move`, `files.rename`,
  `files.organize` (category/extension subfolders, conflicts skipped and
  reported), and `files.search` (name substring/wildcard + optional bounded
  text-content search; credential-like file contents are never searched).
  Nothing overwrites and there is no delete tool.
- New policy action `MOVE_FILE`: inside the workspace a move/rename is
  allowed like a write (bytes are preserved and hash-verified), while real
  deletes/overwrites remain approval-gated. No new dependency, no database
  migration (schema stays v6).
- Deterministic planner intents for copy/duplicate, move, rename,
  organize/tidy and search via `--target`/`--output`/quoted text; missing
  pieces become explicit `needs_input` prompts. File intents stay
  deterministic-only (AI plans for them are rejected by the allow-list).
- Independent verification for every new tool, Phase 7 acceptance criteria
  for transfers (including honestly-empty files), checkpoints on success,
  and bounded fresh-output recovery for copy collisions.
- New direct CLI `pc-agent files copy|move|rename|organize|search`, a GUI
  Documents > Files panel with confirmations, and doctor/GUI summaries now
  report the file-tool count. 13 new tests.

Still not included: file/folder deletion, folder copy/move, bulk glob
transfers, external-path transfers, DOCX/XLSX editing or formula
calculation, PDF split/extract, subprocess/Git adapters, browser or Windows
UI automation, image tools, multi-step planning, or automatic workflow
reuse.

## 0.9.0 - PHASE 7 Foundation (verification, checkpoints, cancellation, memory)

Personal-use foundation required by the master prompt. No new external tools
and no safety rule was weakened; licensing remains unchanged and off by
default.

- Generalized acceptance-criteria verification (`verification.py`): every
  attempt now checks tool verification plus output-artifact existence,
  independently recomputed SHA-256, PDF page counts, and CSV/JSON row
  recounts. A task succeeds only when all applicable criteria pass; each
  evaluation is persisted (`task_verifications`) and printed as
  `[PASS]`/`[FAIL]` evidence in CLI, `task show`, and the GUI.
- Durable step checkpoints (`task_checkpoints`) with a plan fingerprint and
  artifact validation, plus `pc-agent task resume` (and the GUI Resume
  button) that continues the same interrupted task in place, refuses
  recently-updated tasks unless `--force`, revalidates the stored plan and
  approvals, invalidates stale checkpoints, and always re-verifies.
- Cooperative cancellation (`task_cancellations`): `pc-agent task cancel`
  and the GUI Cancel button cancel queued tasks immediately and stop running
  tasks at the next stage boundary, including from another process and while
  the GUI worker is busy. CLI `Ctrl+C` prints resume/cancel guidance.
- Memory layer (`memory_items`, `pc-agent memory ...`): inspect/manage
  preferences and user notes, automatic verified-workflow records on task
  success, secret-like key refusal, explicit memory clear, and terminal
  task-history deletion (`task delete --yes`; workspace files untouched).
- SQLite schema v6 adds `task_checkpoints`, `task_cancellations`,
  `task_verifications` and `memory_items`; existing data is preserved. No
  new runtime dependency. 17 new tests incl. simulated crash + resume.

Still not included: multi-step planner, automatic workflow reuse, general
file copy/move/rename/organize tools, DOCX/XLSX editing or formula
calculation, PDF split/extract, subprocess/Git adapters, browser or Windows
UI automation, image tools, full core self-repair, or secure OS credential
storage.

## 0.8.0 - PHASE 6 Licensing and subscriptions (offline foundation)

- New offline licensing with Ed25519-signed license tokens, verified locally
  with the vendor public key (pure standard library; RFC 8032 test vectors).
  Plans (`trial`, `standard`, `pro`, `enterprise`) map to real feature gates;
  licenses may be subscription-style expiring, perpetual, seat-counted and
  optionally bound to a one-way machine fingerprint.
- Built-in trial (default 14 days, configurable 0-90) that only starts when
  enforcement is `warn` or `enforce`; moved-back system clocks fail closed.
- New customer CLI: `pc-agent license status|activate|deactivate|machine-id`.
  Activation verifies before atomically replacing the local license file and
  audits events with token fingerprints only. The GUI Dashboard gains a
  License panel with details, file-picker activation and deactivation.
- New vendor-only `pc-agent-license-admin` tool (`keygen`, `issue`) for
  signing keys and licenses; private keys never belong on customer machines.
- Enforcement is opt-in via `PC_AGENT_LICENSE_ENFORCEMENT` (`off` default,
  `warn`, `enforce`), so existing installs keep working unchanged. Read-only
  diagnostics such as `doctor` and license commands are never gated.
- `doctor` and `pc-agent gui --check` print a one-line license summary.
- SQLite schema v5 adds `license_state` and `license_events`; existing data
  is preserved. No new runtime dependency.

Still not included: payment processing, store checkout, online
activation/subscription-billing/revocation servers, cloud coordination,
Tool Builder lifecycle screens, a settings/API-key editor, screenshots,
keyboard/mouse or external-app automation, shell access, or a production
installer/shortcut.

## 0.7.0 - PHASE 5 Desktop GUI

- New standard-library Tkinter desktop GUI (`pc-agent gui` and the windowed
  `pc-agent-gui` launcher) with Dashboard, Tasks, Documents, Web, Desktop,
  Tools and Providers tabs.
- The GUI uses a headless controller over the same orchestrator, recovery
  service, provider router, registered tools, policy contexts and SQLite
  repositories as the CLI. Operations run one at a time in a worker thread;
  exact external read-only task approvals are decided in a GUI dialog.
- Tasks can be created/run, filtered, inspected, diagnosed and recovered
  from the GUI. Document/PDF actions remain workspace-bound; web fetch and
  desktop-open authority is created only by their dedicated tab actions.
- The Tools tab can invoke approved dynamic `custom.*` tools only; the
  Providers tab shows route status and an explicit Ask action without
  displaying or editing API keys.
- `doctor` reports desktop-GUI/Tkinter availability, and `pc-agent gui
  --check` verifies the controller and data without opening a window.
- Compact split layouts, scrollable task controls and document sub-tabs keep
  the window usable on smaller Windows displays.
- No new runtime dependency and no database migration; schema remains v4.

Still not included: Tool Builder lifecycle screens, a settings/API-key
editor, screenshots, keyboard/mouse or external-app automation, shell
access, licensing/subscriptions, cloud coordination or a production
installer/shortcut.

## 0.6.1 - PHASE 4.1 TLS and Recovery Hotfix

- Fixed Windows HTTPS failures such as `CERTIFICATE_VERIFY_FAILED: unable to
  get local issuer certificate` without disabling certificate verification.
  Web fetch and provider HTTP now share one verified TLS context combining
  Python/OpenSSL defaults, the Windows ROOT/CA server-auth stores when
  present, and Certifi's Mozilla CA bundle.
- Added `certifi` as the only new runtime dependency for that portable CA
  bundle. `doctor` now prints the loaded CA count and trust sources, and TLS
  failures produce a specific fail-closed diagnostic.
- Manual recovery no longer creates a duplicate clone when a source task
  already has a succeeded linked recovery task. Diagnosis now reports that
  state instead of proposing another recovered output.
- `tools list` now uses a wrapped multi-line layout so long tool descriptions
  remain readable and copy cleanly from narrower Windows terminals.
- No database migration; schema remains v4.

## 0.6.0 - PHASE 4 Office, Web Fetch and Desktop Tools

- Phase 3 usability hotfixes: task commands accept `latest`,
  `latest-failed` and `latest-<status>`; `task list --status` filters tasks;
  failed human-readable reports print exact copy-ready diagnose/recover
  commands; quoted angle-bracket placeholders produce a targeted explanation
  instead of a generic not-found error.
- Known corrupt demo/user PDFs still appear in structured `Skipped` results,
  but pypdf's duplicate low-level header/EOF warnings are suppressed outside
  DEBUG logging.
- New standard-library Office engine and seven registered tools for
  DOCX/XLSX/PPTX/CSV inspection/text extraction, CSV statistics, CSV↔JSON,
  CSV→XLSX and verified simple DOCX creation. OOXML/CSV parsing is bounded
  and rejects macro/legacy formats, encrypted or duplicate ZIP entries and
  `DOCTYPE` XML.
- New deterministic `demo office` data and direct `pc-agent office ...`
  commands. Safe local Office inspections/conversions and desktop system
  information also join the orchestrator's verified allow-list; Phase 3
  fresh-output and unique-workspace-input recovery now covers those plans.
- New dedicated `pc-agent web fetch` for one exact HTTP(S) URL: public-IP and
  redirect revalidation, no credentials/cookies/scripts, bounded
  time/bytes/content types, optional new workspace text output, and an
  explicit `--allow-private` direct-command exception. Generic tasks and AI
  plans remain network-denied.
- New dedicated desktop tools/commands for system information and opening one
  exact workspace file/folder or HTTP(S) URL in the default app. Executable,
  installer, script and shortcut targets are blocked; there is no shell,
  screenshot, clicking or typing capability.
- New bounded settings `PC_AGENT_WEB_TIMEOUT_SECONDS` and
  `PC_AGENT_WEB_MAX_BYTES`; `doctor` reports Phase 4 tool/web-limit status.
- No new runtime dependency and no database migration; schema remains v4.

Still not included: full browser DOM/search/form automation, desktop GUI or
keyboard/mouse control, editing existing Office files in place, dynamic
file/network/shell/browser/desktop tools, licensing/subscriptions, cloud
coordination, autonomous core-source repair, or a kernel/VM sandbox.

## 0.5.0 - PHASE 3 Self-Healing + Recovery

- Phase 2 hotfix: relative dynamic-tool `--arguments-file` paths now resolve
  from the current folder, app root, then workspace; missing arbitrary files
  report every searched path and invalid UTF-8 is reported cleanly.
- The known bundled `word_count_arguments.json` smoke file has an embedded
  fallback, so that documented invocation still works if the project example
  is missing; `doctor` reports the missing bundled file.
- `tool-builder approve --yes` now prints the post-approval approved/enabled
  snapshot instead of the stale pre-approval state, and tool descriptions are
  whitespace-normalized to one line in list/show output.
- Ruff now excludes generated `.patcher-backups`, so a root-level
  `python -m ruff check` validates the live project rather than old patch
  snapshots after a fresh patcher chain.
- SQLite migration v4: `recovery_attempts` checkpoints (including before/after
  plan JSON) and `task_recovery_links`, preserving all Phase 2 data.
- Deterministic diagnosis for output collisions, uniquely correctable missing
  workspace inputs, merge verification failures, transient lock/timeout-style
  failures, interrupted tasks, and non-retryable policy/approval/invalid
  failures.
- Bounded orchestrator recovery through `RETRYING`: default two healing
  retries, hard cap three, bounded backoff, and deterministic verification
  after every retry. Existing outputs are never overwritten.
- Revised external-read plans require fresh exact approval; corrected input
  substitution is disabled for external scopes.
- CLI: `--max-recovery-attempts`, `--no-recovery`, `task diagnose`,
  `task recover [--force]`, and `recovery scan|list|show`.
- Manual recovery resumes queued/needs-input/waiting tasks or creates a
  linked clone for failed/interrupted tasks. Invalid stored plans are
  reported and safely replanned; manual chains are capped at three clones.
- `doctor` and `recovery scan` report recorded attempts, stale active tasks
  and old temporary PDF artifacts. Scanning never deletes files.

Still not included: recovery for direct one-shot `pdf`/`tools` commands,
autonomous modification/repair of core source code, dynamic
file/network/shell/browser/desktop tools, GUI, licensing/subscriptions, cloud
coordination, or a kernel/VM sandbox.

## 0.4.0 - PHASE 2 Dynamic Tool Builder

- Dynamic `custom.*` candidates from reviewed offline templates, imported JSON
  proposals, or strict-JSON output from a configured real provider.
- Pure-compute security boundary: no candidate-declared filesystem, network,
  shell, desktop or pip-dependency permission is accepted.
- Strict proposal and schema-definition validation, exact dependency matching,
  Python AST allow-listing, immutable source hashes and provenance.
- Fresh restricted Python subprocess for candidate tests and approved runtime
  invocation, with JSON size limits, a minimal environment, restricted
  builtins/imports and hard timeouts. This is subprocess isolation, not an
  OS/VM sandbox guarantee.
- SQLite migration v3 for candidates, test runs and lifecycle events.
- Lifecycle: proposed -> tested/test_failed -> explicit approval, rejection,
  enable/disable, newer-version supersession and rollback to a tested prior
  version.
- Bounded provider repair: maximum three attempts per chain, new version only,
  with parent schemas and expected outputs preserved.
- CLI: `tool-builder suggest|templates|template|import|generate|list|search|show|test|approve|reject|repair`
  and `tools show|invoke|enable|disable|rollback`.
- Offline templates: word count, slugify, percentage and temperature conversion.

Still not included: dynamic file/network/shell/browser/desktop tools,
autonomous core self-healing, GUI, licensing/subscriptions, cloud
coordination, or a kernel/VM sandbox. Real-provider generation/repair requires
the user's configured provider account; automated tests cover it with a fake
strict-JSON provider.

## 0.3.0 - PHASE 1C Providers + Orchestration

- Provider abstraction with Gemini, OpenRouter, OpenAI/generic
  OpenAI-compatible, Ollama and offline mock adapters.
- Bounded retries and fallback only through the user-configured provider route.
- CLI: `providers list`, `ask`, `run`, and `task run`.
- Deterministic-first planner for PDF merge/verify/inspect and file listing;
  optional `--use-ai` planning is allow-list and schema validated.
- Stored task orchestration through planning, approval, one registered tool,
  deterministic verification and final succeeded/failed/cancelled states.
- Exact, task-specific read-only approvals for selected external input paths;
  outputs remain workspace-only and sensitive paths remain denied.
- SQLite migration v2: `task_plans`, `approval_records`, `tool_invocations`.
- Package metadata version corrected/aligned to 0.3.0.

Still not included: direct Anthropic adapter, dynamic tool creation,
self-healing, GUI, licensing/subscriptions, or cloud coordination.

## 0.2.0 - PHASE 1B Tools + PDF

- Versioned built-in tool registry with strict schemas and structured results.
- Workspace tools: list files, read text, write-new text (never overwrites).
- PDF tools: inspect, verify, explicit merge, folder merge (via pypdf).
- Merged PDFs are re-opened and page-count verified; sources are preserved.
- Folder merge reports corrupt/encrypted files as skipped with reasons.
- CLI: `tools list`, `demo pdf`, `pdf inspect|verify|merge|merge-folders`.
- Tool catalog and Phase 1B limitations documented in `docs/tools.md`.

Still not included: AI providers/model router and natural-language task
execution - that integration is PHASE 1C.

## 0.1.0 - PHASE 1A Foundation

- Project scaffold (src layout, pyproject, .gitignore, .env.example).
- Environment-based configuration with validation; API keys never printed.
- Task state machine with enforced transitions (success requires VERIFYING).
- SQLite storage with migration v1: `tasks`, `task_events`, `user_preferences`.
- Deterministic risk-based policy engine and workspace path-boundary checks.
- CLI: `doctor`, `task create/list/show` (local task records only - no execution yet).
- Test suite covering config, state transitions, policy/paths, storage, CLI.

Not included yet: tools/PDF merge (1B), AI providers and task execution (1C).
