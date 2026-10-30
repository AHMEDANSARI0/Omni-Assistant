# Phase 29 — PDF Signature Trust Validation (0.31.0)

Phase 29 adds direct-only, completely offline trust evaluation for PDF
signatures that already pass the Phase 27 CMS/ByteRange checks. Phase 27
remains the read-only cryptographic inspector; Phase 29 answers the separate
question: **does the embedded signer certificate build a basic valid chain
to an explicitly supplied local trust anchor, and what do explicitly
supplied local CRLs say?**

## Security boundary

- There is **no registered tool**, task, dynamic-tool or AI-planner surface.
  Only `pc-agent pdf validate-signatures` and the GUI **Validate Signature
  Trust** button run this code.
- **No network call is made.** The operating-system trust store, Adobe trust
  lists, CRL distribution points and OCSP responders are not contacted.
- Trust anchors come only from workspace `.pem`, `.crt`, `.cer` or `.der`
  files/folders chosen by the user. CRLs come only from workspace `.crl`,
  `.pem` or `.der` files/folders chosen by the user.
- A certificate embedded in a signed PDF is never trusted merely because it
  is embedded. It becomes a terminal trust anchor only when its exact
  certificate was also supplied locally.
- Raw certificate/CRL bytes are not included in text, JSON, GUI results,
  tasks or summaries; only names, serials, fingerprints, times and statuses
  are reported.

## Basic chain checks

For each cryptographically valid CMS signer, Phase 29:

1. uses the CMS signing time as the certificate verification time when one is
   present (otherwise current time);
2. checks signer-certificate validity and digital-signature key usage when a
   KeyUsage extension is present;
3. searches signer → issuer links through embedded certificates and supplied
   anchors, to a maximum of eight certificates;
4. verifies each issuer signature and exact issuer/subject name link;
5. checks every chain certificate's validity at the verification time;
6. requires issuer certificates to have `BasicConstraints CA=true`, respects
   a present path-length constraint, and requires certificate-signing key
   usage when KeyUsage is present;
7. stops as `trusted` only at an exact locally supplied trust anchor.

A self-signed signer can be accepted only when that exact self-signed
certificate is itself supplied as a local trust anchor.

## CRL checks

For every non-anchor certificate in the selected chain, Phase 29 looks for a
supplied CRL whose issuer matches and whose CRL signature verifies with the
issuer certificate. A CRL is treated as fresh when its `nextUpdate` has not
passed at the current time. Results distinguish:

- `not_revoked` — fresh signed CRLs cover the chain and list none of its
  certificates as revoked at the CMS signing time;
- `revoked` — a certificate's revocation date is on or before signing time;
- `revoked_after_signing` — a later revocation exists, so the signature can
  still be `trusted_valid` for its signing time, with that fact reported;
- `unknown` — a required issuer CRL was missing, stale, unsigned by the
  selected issuer, or otherwise unusable.

Overall `trusted_valid` requires cryptographic validity, a trusted chain,
and a passing CRL state. A trusted chain with `unknown` CRL state is
reported as `trusted_chain_revocation_unknown`; the CLI/GUI accept that as a
passing result only when the user explicitly selects the corresponding
option.

## Honest limits

This is **local basic chain and CRL validation only**. It does not evaluate
the operating-system or Adobe root programs, certificate policies, name
constraints, qualified-certificate rules, OCSP, CRL distribution-point
fetching, RFC 3161 timestamp-authority trust, PAdES profiles, LTV material or
legal acceptance. A machine-local CMS signing time is not an independent
trusted timestamp. Those remain **not evaluated**.

## CLI

```powershell
pc-agent pdf validate-signatures signed.pdf --trust-anchor root-ca.pem --crl intermediate.crl --crl root.crl
```

Repeat `--trust-anchor` and `--crl` for multiple files, or pass a workspace
folder containing supported files. Add `--accept-revocation-unknown` only
when a trusted chain without complete CRL evidence is intentionally enough
for a passing exit status. `--json` prints the same bounded summary without
certificate contents.

## GUI

Documents → PDF → **Validate Signature Trust** uses the normal Input PDF plus
the trust-anchor and CRL path boxes at the bottom of the PDF panel. Each box
accepts one workspace path per line. The **Accept trusted chain when CRL
status is unknown** checkbox mirrors the CLI option and is off by default.
