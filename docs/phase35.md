# Phase 35 — DocTimeStamp Incremental Revisions

| Item | Value |
| --- | --- |
| Version | 0.37.0 |
| Schema | v6 |
| Tools | 62 |

## Summary

Phase 35 adds a direct-only workflow that appends one RFC 3161
`/Type /DocTimeStamp` revision to an unencrypted PDF. The new signature
dictionary uses `/Filter /Adobe.PPKLite` and
`/SubFilter /ETSI.RFC3161`. It does not create a signer CMS signature, does
not use a PKCS#12 identity or password, and does not insert a local `/M`
signing time.

The operation is available from:

- the direct CLI command `pc-agent pdf timestamp`;
- the GUI **Add DocTimeStamp** button, which reuses the existing TSA URL,
  hash, timeout and private-network controls.

It is not a registered tool, task action, dynamic-tool action or provider
authority. The registered-tool count and database schema do not change.

## Incremental guarantees

- The source PDF remains an exact byte prefix of the placeholder, temporary
  verification file and final output.
- The RFC 3161 message imprint covers the new field's complete PDF
  `ByteRange`, excluding only the reserved timestamp-token bytes.
- The exact user-supplied HTTPS TSA receives only the digest of those covered
  bytes. The workflow never sends the PDF, a private key or a password.
- There is no default TSA. Private or non-standard-port TSA resolution still
  requires the explicit private-network option.
- The source is never modified and an existing output is never overwritten.
- A newly created timestamp field is an invisible zero-rectangle `/Sig`
  widget. Automatic naming uses a blank `DocTimeStamp` field when available,
  otherwise `DocTimeStamp`, `DocTimeStamp2`, and so on.
- Contents capacity starts at 32,768 bytes and can grow to the existing
  262,144-byte token limit. If placeholder bytes change, a fresh TSA request
  is made for the new revision bytes.

Unsigned PDFs, PDFs with valid approval signatures, and PDFs with valid
earlier document timestamps may receive a new DocTimeStamp. Repeated
operations therefore support timestamp renewal while preserving every earlier
revision.

## Fail-closed validation

Before contacting the TSA and again before publication, every existing
approval signature and document timestamp must be supported and
cryptographically valid. Publication requires unchanged page and form-field
structure, preserved ByteRanges, content sizes, statuses, signer fingerprints,
DocMDP permissions and prior timestamp identities. The newly appended field
must cover the whole output file and verify as `verified_untrusted` with a
matching message imprint.

Invalid, malformed or unsupported existing fields stop the operation and no
final output is published.

## DocMDP behavior

| Existing permission | DocTimeStamp behavior |
| --- | --- |
| P=1 | Refuses; no changes are permitted. |
| P=2 | Permits an existing blank signature field only; field creation is refused. |
| P=3 or no DocMDP | Permits creation of a dedicated invisible timestamp field. |

Collect all signer approvals before adding a DocTimeStamp. In this phase, the
signer workflow still refuses PDFs with existing document timestamps rather
than risk weakening their whole-file coverage with a later signer revision.

## Trust boundary

Successful local verification proves only that the returned RFC 3161 token is
cryptographically consistent with the new PDF revision. TSA trust remains
`not_evaluated` in the creation result. Use the existing signature-trust
workflow with explicit anchors, policies, revocation evidence or the optional
system store when a separate trust decision is required.

This is not PAdES/LTV, Adobe trust-program validation, legal acceptance or a
claim that a particular public TSA will accept the request. Phase 35 evidence
uses synthetic PDFs and a local fake TSA; no live public TSA or Adobe Reader
interoperability is claimed.

## Limits

- Unencrypted PDFs only, with the existing 100 MB and signing page limits.
- SHA-256, SHA-384 and SHA-512 timestamp hashes.
- TSA timeout from 1 to 60 seconds.
- One DocTimeStamp per operation.
- No schema migration, new dependency, registered tool, background network
  activity or automatic timestamp renewal.
