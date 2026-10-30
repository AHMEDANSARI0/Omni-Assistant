# Phase 36 — LTV DSS Embedding

| Item | Value |
| --- | --- |
| Version | 0.38.0 |
| Schema | v6 |
| Tools | 62 |

## Summary

Phase 36 adds direct-only inspection and embedding of a PDF Document
Security Store (DSS) for long-term validation material. The direct commands
are `pc-agent pdf inspect-ltv` and `pc-agent pdf add-ltv`; the GUI adds
**Inspect LTV DSS** and **Add LTV DSS** controls. No registered tool, task
action, dynamic-tool action, provider authority, schema migration or new
dependency is added.

The DSS is appended as a true incremental revision. The source remains an
exact byte prefix of the DSS candidate and final output. Every existing
approval signature and document timestamp must verify again before
publication, with its original ByteRange and signer/timestamp identity
preserved.

## DSS and VRI structure

The output catalog contains `/DSS` with DER streams under `/Certs`, `/CRLs`
and `/OCSPs`, plus `/VRI` entries for signed fields. A VRI key is the
uppercase SHA-1 digest of the complete hexadecimal representation of the
signature `/Contents`, including zero padding. This identifier follows the
ETSI DSS convention; SHA-1 is used only as that prescribed identifier, not
as a new signature digest.

Created or changed VRI entries use `/TU` for the DSS creation time unless
the same operation seals the DSS with a subsequent DocTimeStamp. Existing
DSS materials and VRI associations are merged and retained. An operation
that would produce the same DSS and does not request a timestamp seal is
refused as a no-op. The ESIC extension marker is added when absent.

## Material collection and fail-closed checks

Material can come from:

- certificates already embedded in valid CMS signatures and RFC 3161
  tokens;
- an existing DSS in the source PDF;
- explicit workspace certificate, CRL and OCSP-response files or folders.

Supplied certificates must be in, or cryptographically issue, a collected
signer/TSA chain. Supplied CRLs must be fresh at augmentation, signed by a
collected issuer, and relevant to a signed field. Supplied OCSP responses
must be fresh, match a collected certificate/issuer pair, and be signed by
the issuer or an authorized delegated responder whose certificate is
embedded in the response. CRL or OCSP evidence proving revocation at or
before the relevant signing/timestamp time refuses publication. Irrelevant,
stale, malformed or unauthorized supplied material also fails closed.

Phase 36 does not fetch CRLs or OCSP responses from the network during DSS
embedding. Online revocation remains the separate Phase 31 validation
option; a downloaded response is not silently captured into a DSS.

## Optional DocTimeStamp seal

`add-ltv` can optionally seal the new DSS by immediately appending the
Phase 35 DocTimeStamp revision. The exact user-supplied HTTPS TSA receives
only the sealed revision hash; the workflow never sends the PDF. There is no
default TSA.

DSS/VRI incremental updates are permitted for DocMDP P=1, P=2 and P=3
because they add validation data rather than document content or a signer
signature. The optional DocTimeStamp seal retains the existing Phase 35
DocMDP behavior, including its conservative P=1 refusal.

## Trust and conformance boundary

Success reports `material_status=verified_relevant`. That means the local
CMS/timestamp cryptography, certificate linkage and supplied revocation
evidence passed the checks above. Certificate trust remains
`not_evaluated`, TSA trust remains `not_evaluated`, and PAdES status remains
`not_evaluated`.

Embedding a DSS does not convert this product's existing
`adbe.pkcs7.detached` signatures into PAdES baseline signatures; it is
not a claim of PAdES B-LT/B-LTA, Adobe LTV-enabled status, qualified-signature
status or legal acceptance. Use **Validate Signature Trust** for the
separate bounded chain, revocation, policy and name-constraint decision.

Phase 36 evidence uses synthetic PDFs, certificates, CRLs, OCSP responses
and a local fake TSA. No live public CA, public TSA, Adobe Reader or
external PAdES validator interoperability is claimed.

## Limits

- Unencrypted PDFs only, within the existing 100 MB and page limits.
- At least one cryptographically valid signed field is required.
- At most 100 DSS certificates, 50 CRLs, 50 OCSP responses and 100 VRI
  entries; total embedded validation material is limited to 25,000,000
  bytes.
- DER material only inside DSS streams; PEM input files are converted to
  DER.
- The source is never modified and an existing output is never overwritten.
