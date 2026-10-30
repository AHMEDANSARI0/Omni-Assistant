# Phase 33: System Trust-Store Anchors for PDF Signatures

Phase 33 lets a direct CLI or GUI user add CA certificates from the local
operating-system/OpenSSL trust configuration to the existing direct PDF
signature validator. It builds on the Phase 29 local chain/CRL validator,
Phase 30 RFC 3161 trust, Phase 31 bounded online revocation and Phase 32
certificate policies/name constraints.

## Direct use

System anchors are **off by default**. Signer and timestamp/TSA chains have
separate switches:

```text
pc-agent pdf validate-signatures signed.pdf --system-trust-anchors
pc-agent pdf validate-signatures signed.pdf --timestamp-system-trust-anchors
```

The GUI exposes matching “Also use system CA store” checkboxes for signer and
TSA chains. Explicit workspace anchors and CRL files can still be supplied in
the same run; duplicate anchors are removed by SHA-256 fingerprint.

## Platform source

- Windows: the current user's/system **Windows ROOT certificate store**,
  enumerated with Python's Windows-only `ssl.enum_certificates("ROOT")`.
- Other platforms: CA certificates loaded into Python/OpenSSL's default
  context (`ssl.create_default_context(...)` and DER `get_ca_certs`).

The result reports the source name, local/effective anchor counts, separate
signer/TSA system-anchor counts and skipped/non-CA/duplicate entries. It does
not print or export the full system root list, certificate bytes, private
keys, passwords or the PDF.

The loader is bounded at 2,000 usable CA certificates. Malformed entries,
non-CA certificates, CA certificates whose present KeyUsage does not allow
certificate signing, and duplicates are skipped. If the store cannot be
loaded or yields no usable CA certificate, validation fails visibly and does
not silently fall back to only local anchors.

## Security boundary

Enabling system anchors only adds candidate **trust anchors**. A signature
must still pass all existing ByteRange/CMS, certificate signature, validity,
BasicConstraints/path-length, EKU/KeyUsage where checked, certificate-policy,
name-constraint and revocation rules. It grants no registered tool, task,
provider, dynamic-tool or model authority.

System-store loading itself makes **no network call**. The pre-existing
online CRL/OCSP option remains a separate explicit choice.

## Honest scope

This is **not Adobe trust-program validation** and the summary says
`adobe_trust_status=not_evaluated`. It is not a full WebPKI ceremony, PAdES
baseline/LTV evaluator, jurisdiction-specific qualified-signature service or
legal-validity determination. System roots can contain enterprise or locally
installed CAs, so use them only when that machine's trust configuration is
appropriate for the document being checked.

Phase 33 adds no registered tool, database schema change, background service,
OS-store modification, certificate import/removal, Adobe evaluation,
PAdES/LTV support, incremental multi-signature signing or live public-CA
interoperability claim.
