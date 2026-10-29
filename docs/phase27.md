# Phase 27: PDF Digital-Signature Inspection

Phase 27 adds read-only, direct-only inspection of PDF signature fields and
detached CMS signatures. It verifies the local cryptographic consistency
that can be checked from the file and embedded certificates, while making
the remaining trust boundary explicit: **trust was not evaluated**.

There is **no registered tool** for signature inspection. The built-in tool
count stays **62**, and neither deterministic task planning nor AI planning
can invoke this operation. Phase 27 **does not create** a digital
signature, timestamp, certificate, or signed PDF.

## What is inspected

For each AcroForm signature field, inspection reports:

- field name, first widget page and widget count;
- unsigned versus signed state;
- signature dictionary name, reason, location, contact information and
  PDF signature date;
- filter, SubFilter and DocMDP permission (`P1`, `P2` or `P3`) when
  present;
- **ByteRange** values, whole-file coverage and unsigned trailing bytes;
- signature Contents and parsed CMS size;
- CMS digest algorithm and whether the digest of the exact ByteRange bytes
  matches the CMS `messageDigest` signed attribute;
- CMS signature algorithm and whether the signature verifies with the
  embedded signer certificate;
- CMS signing time and whether the signer certificate was within its
  validity period at that time;
- embedded certificate subjects, issuers, serial numbers, SHA-256
  fingerprints, validity dates, key types/sizes and self-signed status.

Supported signature containers are `adbe.pkcs7.detached` and
`ETSI.CAdES.detached` with a bounded DER SignedData structure. RSA,
RSA-PSS, ECDSA, DSA, Ed25519 and Ed448 signer keys can be checked when the
certificate and supported algorithm parameters are embedded. MD5 and SHA-1
CMS digests are reported as unsupported rather than valid.

## Status meanings

- `cryptographically_valid_untrusted`: every embedded CMS signer had a
  matching ByteRange digest and verified CMS signature, and any known
  signing-time certificate validity check passed. This means the signed
  byte ranges are cryptographically consistent with the embedded signer
  certificate. It does **not** prove the signer identity is trusted.
- `invalid`: a digest or CMS signature did not verify, or the signer
  certificate was outside its validity period at the CMS signing time.
- `malformed`: the signature dictionary, ByteRange or CMS structure could
  not be parsed safely.
- `unsupported`: the SubFilter, digest/signature algorithm, signer
  certificate or CMS structure is outside this phase's verifier.
- `unsigned`: the field exists but has no signature value.

Unsigned bytes after a signature's ByteRange may be a later incremental
update. They are reported explicitly rather than silently treated as signed
bytes. DocMDP data is reported; Phase 27 does not claim a complete
certification-policy or incremental-change analysis.

## Trust boundary

Phase 27 performs no network access and does not check:

- whether a chain terminates in an operating-system, Adobe, EU or other
  trust store;
- certificate revocation through CRL or OCSP;
- RFC 3161 timestamp tokens or long-term validation material;
- whether the person named by a self-asserted certificate is really who
  the user expects;
- legal admissibility or organisation signature policy.

A verified self-signed certificate therefore remains
`cryptographically_valid_untrusted`, not “trusted”.

## Safety and bounds

- The file is opened read-only and its hash is checked during inspection.
- Encrypted PDFs are refused. Use the direct Phase 26 unprotect workflow
  to make an unencrypted copy first when that is appropriate.
- Inspection is limited to 500 pages, 50 signature fields, 100 MB PDF
  files, 2 MB of signature Contents per field, 20 embedded certificates
  and 10 CMS signers.
- The local DER parser rejects indefinite lengths, excessive nesting,
  excessive element counts and non-zero trailing bytes after the CMS.
- Certificate key/signature bytes, raw CMS bytes and private material are
  never printed.

## Direct CLI

```powershell
pc-agent pdf inspect-signatures signed.pdf
```

JSON output uses the same direct operation:

```powershell
pc-agent pdf inspect-signatures signed.pdf --json
```

The process returns non-zero when one or more signatures are `invalid` or
`malformed`. Unsupported and unsigned fields are reported without turning a
successful inspection into a cryptographic failure.

## GUI

Documents > PDF adds **Inspect Signatures**. It uses the same direct
inspection service and shows the local status plus trust boundary. It
cannot sign a PDF or change trust settings.

## Remaining signature work

Digital-signature creation with a user certificate/key, trusted-root
policy, revocation checking, RFC 3161 timestamps, PAdES long-term
validation and complete incremental-change certification analysis remain
separate future work. Version 0.29.0; database schema stays v6; no new
runtime dependency beyond the Phase 26 `pypdf[crypto]` requirement.

## Remaining work after Phase 27

- PDF signature creation and production trust/revocation/timestamp
  validation remain separate.
- A signed Windows installer/Authenticode flow remains future work.
- A real public online-update feed still needs vendor HTTPS hosting and the
  matching release public key configured locally.
- macOS/Linux packaging, Keychain/Secret Service credentials and desktop
  automation parity remain platform work.
- Live Anthropic verification still needs a user-provided Anthropic key.
