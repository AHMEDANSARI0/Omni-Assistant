# Phase 30 — RFC 3161 PDF Timestamping (0.32.0)

Phase 30 adds optional RFC 3161 timestamping to the Phase 28 signer and
adds timestamp-token verification to the Phase 27 inspector and Phase 29
local trust validator.

## Creation workflow

When a direct user supplies an exact HTTPS Time-Stamp Authority (TSA) URL:

1. the detached PDF CMS signature is generated locally as in Phase 28;
2. only a SHA-256/384/512 hash of that CMS signature value is placed in a
   bounded RFC 3161 `TimeStampReq` with a random nonce and `certReq=true`;
3. the request is sent by direct HTTPS POST to that exact URL, with no
   redirect and a 256 KB response limit;
4. the returned `TimeStampResp` must be granted/granted-with-modifications,
   echo the nonce, use the requested imprint hash, and contain a token whose
   message imprint, CMS signature, generation time and TSA ExtendedKeyUsage
   verify locally;
5. the token is inserted as the CMS `id-aa-signatureTimeStampToken`
   unsigned attribute;
6. the signed PDF is published only after the full Phase 27 inspection
   verifies the PDF signature and attached timestamp.

The TSA receives the signature-value hash, nonce, requested hash algorithm
and ordinary HTTPS connection metadata. It does **not** receive the PDF,
document text, private key or PKCS#12 password. There is no default TSA:
leaving the URL blank keeps Phase 28 signing fully offline.

Public/global TSA addresses are accepted by default. A private, loopback,
reserved or non-standard-port TSA requires the explicit direct
`--timestamp-allow-private` option (or matching GUI checkbox). The URL is
never available to a registered tool — there is **no registered tool**,
task or AI-planner argument for timestamping.

## Inspection

For a normal CMS signature, `inspect-signatures` now reports an attached
signature timestamp separately from the document signature. A token is
`verified_untrusted` only when:

- its encapsulated content type is `id-ct-TSTInfo` and version is 1;
- the message imprint equals the selected hash of the CMS signature value;
- the token CMS digest and signature verify with the embedded TSA cert;
- that certificate was valid at the token generation time; and
- its critical ExtendedKeyUsage is exactly `timeStamping`.

An invalid unsigned timestamp does not retroactively invalidate an otherwise
valid document signature; both statuses are reported separately.

`ETSI.RFC3161` document timestamp dictionaries are also inspected. For
those, the token message imprint is checked against the exact PDF ByteRange
bytes.

## Trust validation

Timestamp creation does not by itself trust the TSA. Phase 29 now evaluates
the token's TSA certificate chain and CRL state locally. Use
`--timestamp-trust-anchor` to supply TSA anchors separately; when omitted,
the explicit signer `--trust-anchor` certificates are reused, which covers a
common root issuing both signer and TSA certificates.

Only when the token is cryptographically verified **and** its TSA chain is
trusted with passing CRL evidence does validation use the token generation
time as the signer verification time (`rfc3161_timestamp`). Otherwise it
falls back to the local CMS signing time. This prevents an arbitrary token
from moving the validation date.

## Honest limits

RFC 3161 support in this phase is signature timestamps plus inspection of
document timestamps. It does not create an incremental `/DocTimeStamp`
revision, evaluate operating-system/Adobe TSA trust programs, fetch OCSP or
CRLs online, apply certificate policies/name constraints, or establish
PAdES/LTV or legal acceptance. Qualified timestamp policy remains
not evaluated. The signed PDF workflow still refuses already signed PDFs,
so adding a timestamp-only revision to an existing signed file is future
work.

## CLI

```powershell
pc-agent pdf sign input.pdf --output signed.pdf --pkcs12 identity.pfx --timestamp-url https://timestamp.example.com/
```

Options are `--timestamp-hash sha256|sha384|sha512`,
`--timestamp-timeout 15`, and the explicit `--timestamp-allow-private`.
Validate with a distinct TSA root when needed:

```powershell
pc-agent pdf validate-signatures signed.pdf --trust-anchor root-ca.pem --timestamp-trust-anchor tsa-root.pem --crl intermediate.crl --crl root.crl
```

## GUI

Documents → PDF has optional RFC 3161 TSA URL/hash/timeout fields for
**Sign PDF** and an optional TSA trust-anchor box for **Validate Signature
Trust**. The sign confirmation states the exact TSA URL and that only the
CMS signature hash is sent. The PKCS#12 password still appears only in the
masked dialog.
