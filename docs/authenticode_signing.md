# Authenticode: signing the installer you actually ship

Signing the installer is the step that turns *"Windows SmartScreen says this app
is unknown"* into *"verified publisher"*. It is also the one step this project
cannot do for you: the certificate is yours, the private key stays on your
machine, and this product never generates, exports or transmits it.

The flow below is the whole flow - four commands, in order.

| Step | Command | What it proves |
| --- | --- | --- |
| 1 | `cd frontend; npm run tauri:build` | the installers exist |
| 2 | `pc-agent release signing --certificates` | which thumbprint to use |
| 3 | `pc-agent release sign-bundle --check` | every prerequisite is present |
| 4 | `pc-agent release sign-bundle` | both installers are signed **and verified** |

---

## 1. Build the installers

The Tauri build produces two artifacts under `frontend/src-tauri/target/release/bundle/`:

```
nsis/Omni_<version>_x64-setup.exe
msi/Omni_<version>_x64_en-US.msi
```

Nothing else in that folder is treated as an installer. A stray `.exe` elsewhere
in `target/release/` (the application binary itself, for example) is never
signed by these commands - the search is limited to the two bundle folders and
to the `.exe` / `.msi` extensions.

## 2. Find your code-signing certificate

```powershell
pc-agent release signing --certificates
```

This reads `Cert:\CurrentUser\My` and prints, for every code-signing certificate:
subject, issuer, thumbprint, validity window, and whether a **private key** is
present. Then it prints the exact lines to paste:

```powershell
$env:PC_AGENT_SIGN_THUMBPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"
pc-agent release sign-bundle
```

Notes:

* a certificate **without a private key** can verify a signature but cannot
  create one - the listing says `private key: NO` so you find out here, not at
  signing time;
* an expired certificate is still listed (with its validity window) because you
  usually want to sign with the renewed one and understand why the old one
  stopped working;
* on Linux and macOS the command reports `not-run` with the reason and exits **1**.
  That is deliberate: a silent empty list would look like "you have no
  certificates" when the truth is "this store only exists on Windows".

## 3. Check before you sign

```powershell
pc-agent release sign-bundle --check
```

This signs nothing. It reports **READY** or **NOT READY** plus every blocker it
found:

| Blocker | Meaning |
| --- | --- |
| `no installer found` | run step 1; the bundle folders are empty |
| `signtool.exe not found` | install the Windows SDK, or pass `--signtool <path>` |
| `no certificate selected` | pass `--thumbprint`, `--pfx`, or set the env var from step 2 |
| `thumbprint ... too short` | a SHA-1 thumbprint is 40 hex characters |
| `PFX file not found` | the `--pfx` path is wrong |

Exit code is `1` when not ready, `0` when ready - so a release script can gate on
it. `--json` gives the same information as machine-readable output.

## 4. Sign and verify

```powershell
pc-agent release sign-bundle
```

For each installer this runs:

```
signtool sign /sha1 <thumbprint> /fd sha256 /tr <timestamp-url> /td sha256 <file>
signtool verify /pa /v <file>
```

Both signatures are `sha256` with an RFC 3161 timestamp, so the signature stays
valid after the certificate expires. Then **each file is verified with the Win32
trust API** (`WinVerifyTrust`), not merely by `signtool`'s own exit code. The
report distinguishes:

* `signature verified` - Windows would trust it;
* `untrusted` / `not-signed` - Windows would still warn;
* `refused` - the product declined to attempt (for example, no certificate).

A report is only `ok` when **every** installer is signed *and* verifies. It is
written to `data/installer-signing.json`:

```json
{
  "ok": true,
  "ready": true,
  "artifacts": 2,
  "signed": 2,
  "results": [
    {"path": "...setup.exe", "ok": true, "status": "signed", "verify": "signature verified"}
  ]
}
```

Exit codes: `0` every installer signed and verified, `1` one or more not,
`2` usage problem (see below).

## PFX instead of a certificate-store thumbprint

Exporting to PFX is riskier - it is a file containing your key - but supported:

```powershell
$env:PC_AGENT_SIGN_PFX_PASSWORD = "..."   # set interactively, do not echo it
pc-agent release sign-bundle --pfx D:\certs\codesign.pfx
```

* The password is read **only** from `PC_AGENT_SIGN_PFX_PASSWORD`. There is no
  flag that accepts the password itself, because command lines are visible to
  other users on the same machine and land in shell history. Running without the
  variable exits `2` before touching anything.
* `--allow-argv-password` exists for single-update signing on a throwaway VM and
  is **refused** by `sign-bundle`.
* The reported command redacts the password.

## Record the result in the evidence bundle

```powershell
pc-agent release signature --record
pc-agent release evidence
```

`release signature`/`release evidence` never claim a signature is good on their
own - they report exactly what the verification step observed, including
"unsigned" as a fact rather than a failure.

## EV / post-2023 OV certificates: the key must stay on hardware

Since **June 2023** a publicly-trusted code-signing certificate must keep its
private key in a hardware module - a USB token, an HSM, or a TPM-backed
container. That has one practical consequence: a **PFX is an exportable copy** of
a key you are no longer allowed to copy, so the "export to PFX" step some guides
still describe is exactly what the CA rules forbid.

For those certificates, add one flag:

```powershell
pc-agent release signing --certificates
```

The listing now also shows where each key lives:

```
  private key: yes
  key storage: hardware-token via eToken Base Cryptographic Provider
```

Then:

```powershell
pc-agent release sign-bundle --check --require-hardware
pc-agent release sign-bundle --require-hardware
```

With `--require-hardware`:

| Situation | Result |
|---|---|
| `--pfx` given | **refused** - an exportable copy of the key, with the reason |
| key storage = `hardware-token` | READY on that check, and the note names the provider |
| key storage = `software-store` | **blocked** - the key is not on a token; check that it is plugged in and its middleware is running |
| key storage = `unknown` | a **note**, not a pass: a custom CNG/KSP or HSM provider often hides these fields, so `signtool` stays the judge and the signature is still verified afterwards |

`unknown` is deliberate. Reporting "hardware confirmed" for a provider this
product cannot inspect would be a guess about your signing identity, and the
whole point of the flag is to not guess.

Nothing in this path exports, reads or unlocks the private key: it reads the
provider name and the `HardwareDevice` flag, both public metadata.

## Reputation is a different thing from a signature

A valid signature does not stop SmartScreen from warning on a new
certificate. `pc-agent release signing --reputation` reports where this
machine stands, and [`smartscreen_reputation.md`](smartscreen_reputation.md)
explains the OV/EV difference, the reputation timeline, and exactly what this
product cannot do about it.

## What this does not do

* It does not obtain a certificate for you (an OV/EV certificate comes from a
  certificate authority, and since June 2023 private keys for publicly-trusted
  code-signing certificates must live in hardware).
* It does not sign the *update bundle* - that has its own separate flow
  (`pc-agent release sign`), covered in the Phase 21 report.
* It does not push anything to CI. No signing secret is stored in the repository
  or in GitHub Actions; `.github/workflows/desktop-windows.yml` builds and tests
  the installers, and signing stays a local, deliberate step.

## Verifying by hand, if you prefer

```powershell
Get-AuthenticodeSignature .\Omni_0.82.5_x64-setup.exe | Format-List
pc-agent release sign-bundle --check
```

`Status` should read `Valid` and `SignerCertificate.Subject` should name you.
