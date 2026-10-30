# Phase 28 — PDF Digital Signature Creation (0.30.0)

Phase 28 adds direct-only creation of detached CMS PDF signatures with a
user-supplied PKCS#12 identity. It builds on the read-only Phase 27 verifier:
a signed output is published only after that verifier confirms the new
ByteRange digest, CMS signature, signer-certificate fingerprint, page count
and expected form fields.

## Security boundary

- There is **no registered tool**, task, dynamic tool or AI-planner surface for
  signing. Only the direct CLI command and direct GUI button call `pdf_sign`.
- The PKCS#12 file must be a `.p12` or `.pfx` file inside the workspace and is
  limited to 5 MB. Its password is requested only through a hidden CLI prompt
  or masked GUI dialog. The password is memory-only: it is not an argument,
  environment variable, task field, result field, log line or database value.
- The identity must contain a supported private key and matching certificate.
  RSA, EC, DSA, Ed25519 and Ed448 certificates are structurally accepted when
  the underlying CMS builder supports them. The certificate must currently be
  valid; a `KeyUsage` extension, when present, must allow digital signatures.
- The source must be an unencrypted, unsigned PDF of at most 100 MB and 500
  pages. Encrypted **already signed** PDFs are refused: Phase 28 uses a full
  document rewrite and will not invalidate an existing signature.
- Signing **never overwrites** an output and never modifies the source. The
  signed PDF is written to a temporary sibling, verified there, then published
  with one rename.

## What is created

- `adbe.pkcs7.detached` CMS under the `Adobe.PPKLite` handler name.
- SHA-256 by default; SHA-384 and SHA-512 are available.
- An existing blank signature field may be selected. If there is exactly one
  blank Sig field it is used automatically. Otherwise a field is created when
  requested, with a visible two-line appearance by default or an invisible
  zero-size widget on request.
- Optional reason, location and contact information (500 characters each),
  page and rectangle for a newly created visible field, plus optional DocMDP
  certification permission 1, 2 or 3.
- Any additional certificates stored in the PKCS#12 container are embedded in
  the CMS. This helps a verifier build a chain but does not assert trust.

## Honest limits

**Trust was not evaluated.** A successful result means the new CMS signature is
locally cryptographically valid and bound to the selected identity. It does
not establish trusted-root status, chain validation, revocation status,
RFC 3161 timestamp trust, PAdES profile/LTV conformance, or legal acceptance.
The signing date is the local machine time, not an independent trusted
timestamp. Signing an already signed PDF incrementally, multiple signatures
per command, visible-appearance customization beyond page/rectangle, hardware
tokens, cloud keys and operating-system certificate stores remain future work.

## CLI

```powershell
pc-agent pdf sign input.pdf --output signed.pdf --pkcs12 identity.pfx
```

Useful direct-only options are `--field NAME`, `--create-field`, `--page N`,
`--rect x,y,width,height`, `--invisible`, `--reason`, `--location`,
`--contact`, `--certify 1|2|3`, `--hash sha256|sha384|sha512`, and `--json`.
The program always asks for the PKCS#12 password without echoing it; there is
intentionally no password option.

## GUI

Documents → PDF → **Sign PDF** uses the normal Input/Output fields plus the
PKCS#12 path and optional signature fields at the bottom of the PDF panel. A
confirmation dialog shows the source, output, identity path, field, page and
hash before the masked password dialog appears.
