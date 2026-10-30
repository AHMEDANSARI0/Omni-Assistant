# Phase 31 — Online CRL and OCSP Revocation (0.33.0)

Phase 31 extends the direct PDF signature-trust validator with an explicitly
opt-in online revocation fallback. Local validation remains the default and no
network transport is used unless the user enables online revocation in the CLI
or GUI.

## What was added

- Parsed HTTP/HTTPS URL full names from each certificate's
  `CRLDistributionPoints` extension.
- Parsed OCSP responder URLs from each certificate's
  `AuthorityInformationAccess` extension.
- When local CRL evidence leaves a non-anchor certificate `unknown`, the
  validator may download CRLs from certificate distribution points and verify
  them with exactly the same issuer, signature, key-usage, freshness and serial
  rules as workspace CRL files.
- If CRL evidence is still unknown, the validator may send a single-certificate
  OCSP request. It verifies successful response status, serial, issuer name/key
  hashes, response freshness, the response signature and responder authority.
  The responder must be the certificate issuer or a certificate directly issued
  by that issuer with the `OCSPSigning` extended key usage.
- Results report whether online revocation was enabled, whether the network was
  actually used, how many online CRLs/OCSP responses were accepted for parsing,
  and bounded host-level network errors. Endpoint paths and query strings are
  not placed in summaries.

## CLI and GUI

CLI example:

```text
pc-agent pdf validate-signatures workspace-relative-signed.pdf --trust-anchor trust/root.pem --online-revocation
```

Optional CLI flags:

- `--revocation-timeout SECONDS` — 1–60 seconds, default 10.
- `--allow-private-revocation-network` — permits private, loopback or otherwise
  non-global CRL/OCSP endpoints for a trusted local/intranet PKI.

The GUI exposes an **Enable online CRL/OCSP revocation fallback (network)**
checkbox, a private-network checkbox and a timeout field. Before an online
validation, it asks for confirmation and explains that only certificate
serial/issuer-hash information is sent; the PDF itself is not uploaded.

## Bounds and failure behavior

- Online revocation is off by default.
- At most 3 endpoints per certificate and 50 HTTP requests per validation.
- CRL responses are capped at 5,000,000 bytes; OCSP responses at 262,144 bytes.
- Default URL validation rejects credentials, fragments, non-HTTP(S) schemes,
  local hostnames, private/non-global literal addresses and non-standard ports
  unless the explicit private-network option is used. HTTPS remains preferred
  in practice, but CRL/OCSP objects are accepted over HTTP only after their own
  signatures and authority are verified.
- Bounded redirects may be followed by the HTTP client for CRL GET requests;
  redirect targets are revalidated. OCSP uses POST.
- Network, parse, freshness, authorization or signature errors leave revocation
  `unknown`; they never turn a certificate into trusted/not-revoked evidence.

## Explicit non-goals

- No operating-system, browser or Adobe trust-store integration.
- No certificate-policy, name-constraint, PAdES, LTV or legal-validity engine.
- No AI tool, autonomous task or planner can enable this network path.
- RSA-PSS-signed OCSP responses remain unknown because the installed
  `cryptography` OCSP response object does not expose the PSS parameters needed
  to verify them safely. RSA PKCS#1 v1.5, ECDSA, DSA, Ed25519 and Ed448 OCSP
  signatures are handled.
- Automated tests use a local synthetic PKI and injected fake HTTP openers; they
  do not claim a live public CA or public OCSP responder was contacted.
