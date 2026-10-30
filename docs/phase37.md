# Phase 37 — PAdES Baseline B-B Signature Profile

| Item | Value |
| --- | --- |
| Version | 0.39.0 |
| Schema | v6 |
| Tools | 62 |

## Summary

Phase 37 extends the existing direct-only PDF signing workflow with an
explicit `pades-bb` profile. The direct CLI choice is
`pc-agent pdf sign --profile pades-bb`; the GUI signing panel adds a
**Profile** selector with `adbe` and `pades-bb`. The legacy default remains
`adbe.pkcs7.detached`, so existing direct commands and GUI behavior do not
change unless the user selects the new profile. No registered tool, schema
migration or new dependency is added.

The new profile writes `/ETSI.CAdES.detached`, embeds the signing
certificate in CMS `SignedData.certificates`, covers the whole current file
with the PDF ByteRange excluding only `/Contents`, and adds the ESIC
ExtensionLevel 2 marker. Its CMS signed attributes are exactly:

- `content-type` with value `id-data`;
- `message-digest` for the ByteRange bytes;
- ESS `signing-certificate-v2`, containing the signing-certificate hash and
  issuer/serial reference.

The CMS intentionally contains no `signing-time` attribute. The claimed UTC
signing time is the `/M` entry in the PDF Signature Dictionary, as required
by ETSI EN 319 142-1 Table 1. The signature dictionary contains no `/Cert`
entry.

## Inspection and verification

Signature inspection now parses and verifies ESS signing-certificate-v2
when present. A mismatched certificate hash or issuer/serial reference
makes the CMS signer invalid. For an `ETSI.CAdES.detached` signature the
inspector additionally reports:

- `pades_profile_status=bb_structure_verified` when the locally checkable
  mandatory B-B structure above verifies; or
- `pades_profile_status=bb_structure_incomplete` with the missing or
  inconsistent items.

It also exposes the signed-attribute OIDs, ESS hash algorithm, CMS content
type and the claimed signing time source. A newly generated `pades-bb`
signature is published only after this structure verification, the normal
ByteRange/CMS verification and signer-certificate fingerprint check pass.
An optional Phase 30 RFC 3161 signature timestamp can still be attached as
an unsigned CMS attribute; that does not by itself produce a trusted B-T
result because TSA trust is evaluated separately.

`bb_structure_verified` is deliberately narrower than a standards or legal
verdict. Every result keeps
`pades_conformance_status=not_externally_evaluated`. Phase 37 does not claim
ETSI certification, EU qualified-signature status, Adobe Acrobat approval,
Adobe AATL trust or legal acceptance. Applying Phase 36 DSS material to a
new PAdES-oriented signature likewise does not automatically prove B-LT or
B-LTA, because trust and validation-material completeness remain separate.

## Supported generation subset

- RSA PKCS#1 v1.5 and ECDSA private keys;
- SHA-256, SHA-384 and SHA-512;
- one signer per generated CMS object;
- existing direct visible/invisible fields, incremental signing and DocMDP
  restrictions from Phases 28 and 34.

DSA and EdDSA identities are refused for `pades-bb` and remain available
through the legacy `adbe` profile where the existing CMS builder supports
them. Certificate trust, revocation, certificate policies and name
constraints remain the separate direct Phase 29–33 validation workflow. For
a PAdES signature without trusted timestamp evidence, that workflow labels
its verification-time source as `pdf_signature_dictionary_m` rather than
pretending that `/M` came from a CMS signed attribute.

Phase 37 evidence is synthetic: locally generated EC and RSA identities, a
fake local TSA and hand-built mismatch cases. No live CA, public TSA,
Adobe Reader, EU DSS validator or other external PAdES validator
interoperability is claimed.
