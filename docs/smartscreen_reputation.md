# SmartScreen reputation: what a signature does and does not buy

Signing your installer is necessary, and it is not sufficient. This document says
exactly where the line is, because "signed" and "SmartScreen stops warning" are
two different claims and only the first one is in your control today.

## The short version

| Certificate | First-run behaviour | What changes it |
|---|---|---|
| **None** | "Windows protected your PC" - *Unknown publisher* | nothing except signing |
| **OV** (organisation validation) | *Publisher name is shown*, but SmartScreen may still warn until reputation accrues | downloads over time, consistent publisher identity, no malware reports |
| **EV** (extended validation, hardware key) | *Same as OV since 2024* - no instant bypass | downloads over time, consistent publisher identity |

So: a certificate buys you a **name** instead of "Unknown publisher", and a path
to reputation. It does not buy the reputation itself.

**Corrected in 0.82.12.** Earlier versions of this document said an EV
certificate is "typically trusted immediately" and buys "reputation from day
one". That was true until 2024; Microsoft removed the EV SmartScreen bypass, so
EV- and OV-signed files now build reputation the same way. Microsoft's current
guidance lists EV as *no longer recommended specifically for a SmartScreen
bypass*; EV remains necessary for kernel-mode driver signing and is still
preferred by some enterprise buyers. Sources: Microsoft Learn, "Code signing
options for Windows app developers" (2026), and the CA/Browser Forum requirement
(June 2023) that OV keys live on an HSM or hardware token too - which is why
`hardware_backed` below no longer identifies EV.

## What `pc-agent` does about it

```powershell
pc-agent release signing --reputation
```

That prints the policy above next to **your** state, read from this machine:

* whether `signtool` and a code-signing certificate are present;
* for each certificate: subject, thumbprint, validity, and whether the key is
  hardware-backed (the key is not exportable - true of EV *and* post-2023
  OV, so it does not predict SmartScreen);
* the last recorded signing run (`data/installer-signing.json`) and, per
  installer, whether it is signed **and verified**;
* the exact next command, and what this product will never do.

It reports; it does not promise. There is no code path here that claims a
SmartScreen outcome.

### Timestamps, so the signature outlives the certificate

Every signature this product makes is `sha256` with an RFC 3161 timestamp. That
is not decoration: without a timestamp the signature becomes invalid the moment
the certificate expires, and every installer you ever shipped starts warning
again. If the first authority fails, the next one is tried:

| Order | Authority |
|---|---|
| 1 | `http://timestamp.digicert.com` |
| 2 | `http://timestamp.sectigo.com` |
| 3 | `http://timestamp.globalsign.com/tsa/r6advanced1` |

The report names the authority that actually worked, and keeps the exit code and
last line of each attempt. Override the first choice with
`--timestamp-url <url>` (tried first, then the fallbacks).

## What is genuinely outside this product

* **Obtaining the certificate.** A CA verifies your identity; OV takes days and
  paperwork, EV takes longer. Since June 2023 the private key must live on
  hardware they control.
* **SmartScreen reputation accrual.** It is Microsoft's system: installs over
  time, from many machines, with no malware reports. There is no API to submit an
  installer for reputation, and no way to buy it except an EV certificate.
* **Microsoft Store / MSIX.** A Store-signed package avoids SmartScreen entirely
  but is a different distribution channel, not a step in this pipeline.
* **Reputation for a *new* certificate.** Changing certificates resets it. That
  is why the runbook insists on one stable publisher identity.

## The checklist, in order

```powershell
# 1. is the installer built?
pc-agent release installer --check

# 2. which certificate, and is its key on hardware?
pc-agent release signing --certificates

# 3. preflight: nothing is signed unless this says READY
pc-agent release sign-bundle --check

# 4. sign + verify (add --require-hardware for EV or post-2023 OV)
pc-agent release sign-bundle --require-hardware

# 5. what does the machine say about it now?
pc-agent release signing --reputation
```

Then confirm by hand on the machine that will run it:

```powershell
Get-AuthenticodeSignature .\Omni_0.82.x_x64-setup.exe | Format-List Status, StatusMessage, SignerCertificate
```

`Status` must read `Valid`. Anything else - `NotSigned`, `HashMismatch`,
`UnknownError`, `NotTrusted` - means the file users download is not the file you
think you signed, and the report in `data/installer-signing.json` will say the
same thing.
