# Buying a code-signing certificate from Pakistan: the realistic path

This is the BARA item. Nothing in this repository can be done for you here, because
a code-signing certificate is an **identity document issued by a certificate
authority to a verified person or company**. It cannot be generated, borrowed,
guessed, or produced by a build script. What this document does is remove every
other unknown: which product to buy, what it costs, what the CA will ask for, what
to do when the token arrives, and how to prove the signature afterwards.

Read this together with `docs/smartscreen_reputation.md` (what a signature does
**not** buy) and `docs/authenticode_signing.md` (the signing mechanism).

---

## 1. The decision, in one table

Status in **Pakistan, 2026**, for a developer with no US/Canada/EU/UK entity:

| Option | Can you use it? | Cost | Hardware | Verdict |
|---|---|---|---|---|
| **Azure Artifact Signing** (formerly Trusted Signing) | **No.** Public-trust certificates are for organisations in the USA, Canada, the EU and the UK, and for **individuals only in the USA and Canada** | ~$9.99/month | none (cloud HSM) | Cheapest and best - and out of reach from Pakistan unless you have a registered entity in one of those regions |
| **OV / IV certificate from a CA** (Sectigo, Comodo, SSL.com, DigiCert, GlobalSign, Certum) | **Yes** - these are sold worldwide | **$150-300/year** (DigiCert ~$400) | USB token or cloud HSM, required since June 2023 | **This is your path** |
| **EV certificate** | **Yes** | $280-685/year | USB token or cloud HSM | Only worth it for kernel-mode drivers or if a customer demands it - see §3 |
| **Microsoft Store distribution** | Yes | free (dev account may have a fee) | none | No warning dialog at all, because the Store vouches for you; but it changes how you ship |
| **Self-signed certificate** | n/a | free | none | Useless: Windows does not trust it, so "Unknown publisher" stays |

**One-line answer:** buy an **individual/OV code-signing certificate** (`IV` where
the CA offers it) with a **cloud-HSM or USB-token** key, valid one year, and keep
the same certificate for years afterwards.

## 2. Individual (`IV`) vs company (`OV`)

The CA issues to a verified legal identity, and that identity is the name users
see in the warning dialog. Two ways to satisfy it:

* **IV - Individual Validation.** Sectigo and SSL.com are the two CAs that
  reliably publish an individual path (Sectigo calls it Individual Validation;
  SSL.com sells it as an individual code-signing certificate). What they verify
  is *you*: passport or CNIC, a live video/notarised identity check, a phone
  call to a number they can tie to you, and a proof of address.
  **This is the fastest route for a solo developer in Karachi.**
* **OV - Organisation Validation.** Requires a registered business (SECP
  incorporation or a sole-proprietor registration), a phone number listed against
  that business, and often a **DUNS number** (free from Dun & Bradstreet, takes
  days, and it is the single most common reason an OV application stalls). Worth
  it only if you intend to sell under a company name.

Either tier gives the same practical first-run behaviour now (§3).

## 3. Why EV is no longer the smart-man's upgrade

Until 2024, an EV certificate **bypassed SmartScreen entirely** on first download.
Microsoft removed that. EV- and OV-signed files now build reputation identically.
Microsoft's own current guidance lists EV as *no longer recommended specifically
for a SmartScreen bypass*. EV remains mandatory for **kernel-mode driver**
signing, and some enterprise procurement checklists still name it.

So for an installer that ships a desktop app: **OV/IV is enough.** Pay for EV only
if a driver or a contract forces it. The one thing not to do is buy EV expecting a
warning-free first download - that promise no longer exists.

## 4. What the CA will ask for (prepare these first)

Have all of this ready before you pay; otherwise the order sits in "awaiting
documents" for a week:

1. **Passport** (individual) and/or **CNIC**, plus a second photo ID if asked.
2. **Proof of address** dated within the last 3 months (utility bill, bank
   statement) - the name must match the ID.
3. **A phone number** the CA can call, tied to the address you gave.
4. **For OV only:** business registration certificate, a **DUNS number**, and a
   phone line published in a business directory (the CA will call the switchboard
   and ask for you by name).
5. **An email on your own domain** helps; a free mailbox sometimes slows the
   review down.
6. **A key-delivery choice:** USB token (posted to Pakistan - budget 1-2 weeks and
   customs) or **cloud HSM** (SSL.com eSigner, DigiCert KeyLocker, or your own
   Azure Key Vault / GCP KMS with key attestation). Cloud HSM avoids shipping and
   works from CI, which is the better choice if you ever build on a server.

Expect **1-7 business days** for issuance once the documents are accepted.

## 5. When the certificate arrives

### With a USB token

1. Install the token middleware (SafeNet/eToken drivers link comes with the
   certificate). Windows then shows it under *Personal > Certificates*.
2. Signing requires the token **plugged in** and its PIN entered - the private key
   cannot be exported, by design.

### With a cloud HSM

The CA's signing client (eSigner, KeyLocker) does the signing; `signtool` is
invoked with the vendor's custom `/dlib` or you upload the artifact to their tool.
Follow the vendor's page - the product's own command sequence below stays the
same, only the last step changes.

### Then, in order

```powershell
pc-agent release signing --certificates
```

That lists every code-signing certificate for this user with subject, thumbprint,
validity, and whether the key is hardware-backed. Take the **thumbprint** from it.

```powershell
pc-agent release sign-bundle --require-hardware --check
```

A dry run: it names every blocker (no installer, no `signtool.exe`, no
certificate) and signs nothing. `--require-hardware` refuses a certificate whose
key is exportable - that is not paranoia, it is the June 2023 CA/Browser Forum
rule and the difference between a certificate that can be stolen and one that
cannot.

Take the thumbprint from the previous command and put it in a variable first, so
the command below is copyable as-is:

```powershell
$thumbprint = "THE_THUMBPRINT_FROM_STEP_1"
```

```powershell
pc-agent release sign-bundle --require-hardware --thumbprint $thumbprint
```

Signs the NSIS `.exe` **and** the MSI with SHA-256 and an RFC 3161 timestamp, then
verifies each one through Windows' own trust chain. Exit code 1 if any artifact is
not signed *and* verified. The report lands in `data/installer-signing.json`.

```powershell
pc-agent release signing --reputation
```

Prints what your users will actually see on first run, next to the machine's real
state - and states plainly that SmartScreen reputation itself is assigned by
Microsoft and is not measurable or purchasable from here.

Independent second opinion (always worth doing once):

```powershell
signtool verify /pa /all "frontend\src-tauri\target\release\bundle\nsis\*.exe"
```

### Why the timestamp matters

A signature with a timestamp stays valid **after the certificate expires**; one
without it becomes "certificate expired" the day the cert dies. The product uses
`http://timestamp.digicert.com` and falls back to Sectigo and GlobalSign; all
three are RFC 3161 and free.

## 6. What this buys, precisely

* The UAC/install dialog shows **your verified name** instead of "Unknown
  publisher".
* Windows and browsers can prove the file was not modified after signing.
* Reputation **starts accruing** - and this is the part people get wrong: it
  accrues from downloads on *that* certificate. Renew the same certificate (or buy
  the renewal through the same CA account, keeping the same subject) and your
  history continues. Switch CA every year and you restart from zero each time.

It does **not** buy: an instant SmartScreen bypass, antivirus whitelisting,
immunity from "Windows protected your PC" while the file is new and unpopular, or
any influence over Microsoft's reputation service. Those are in
`docs/smartscreen_reputation.md`.

## 7. If you decide not to buy one yet

That is a legitimate choice for a personal AI operating system used on your own
machine. Everything works unsigned:

* `pc-agent gui` opens the desktop window with no certificate at all.
* An unsigned installer shows "Unknown publisher"; the user clicks through.
* `pc-agent release signing --reputation` will keep reporting
  `unsigned: 'Unknown publisher' warning`, which is the truth and not a defect.

Buy the certificate when — and only when — you start giving the installer to
people who do not know you, or you stop wanting to explain the warning.

## 8. The commands, condensed

```powershell
pc-agent release signing --certificates
```

```powershell
pc-agent release sign-bundle --require-hardware --check
```

```powershell
pc-agent release sign-bundle --require-hardware --thumbprint $thumbprint
```

```powershell
pc-agent release signing --reputation
```

```powershell
signtool verify /pa /all "frontend\src-tauri\target\release\bundle\nsis\*.exe"
```
