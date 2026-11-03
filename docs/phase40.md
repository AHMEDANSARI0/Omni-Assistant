# Phase 40 — Signed Update Feed Publisher

Phase 40 completes the vendor side of the Phase 24 online-update design.
The customer application could already download and verify a strict
format-1 feed, and `pc-agent-release-admin sign` could sign one portable
bundle, but the vendor still had to assemble the HTTPS directory manually.
The publisher now creates and verifies that entire static package in one
operation.

| Item | Value |
|---|---|
| version | 0.42.0 |
| vendor commands | `pc-agent-release-admin publish-feed`, `verify-feed` |
| customer feed format | existing Phase 24 format 1 |
| signing | existing Ed25519 detached bundle signature |
| registered tools | 62 built-ins; no new tool |
| database schema | v6; no migration |
| new dependency | none |
| upload/network call by publisher | none |

## Package layout

For a verified portable bundle, the publisher creates a new directory
containing:

```text
stable.json
releases/release-<version>.zip
releases/release-<version>.zip.sig.json
customer-config.env.example
HOSTING.txt
publisher-manifest.json
```

- `stable.json` is the exact strict feed consumed by Phase 24.
- The ZIP is copied byte-for-byte from the verified input bundle under a
  canonical versioned name.
- The detached Ed25519 signature is created for that published filename,
  bundle hash, version, and key ID.
- `customer-config.env.example` contains only public online-update values,
  with signed updates required.
- `HOSTING.txt` gives static-host deployment constraints.
- `publisher-manifest.json` inventories the other files by relative path,
  size, and SHA-256 and records the base/feed URLs, channel, public key,
  key ID, version, and publication timestamp.

The private Ed25519 seed is read only from the vendor-selected local key
file. The builder scans generated output for both the raw seed and its
base64url form and refuses publication if either appears. The private key
is never copied into the package, printed, or included in a receipt.

## Vendor commands

Build and verify a fresh package:

```text
pc-agent-release-admin publish-feed BUNDLE.zip \
  --private-key release-private-key.txt \
  --key-id release-1 \
  --base-url https://updates.example.com/self-healing-pc-agent \
  --channel stable \
  --output update-feed-stable
```

The command refuses an existing output directory. It builds in a temporary
sibling, verifies the complete tree, and only then publishes the final
directory name. `--published-at` may provide a timezone-aware ISO timestamp
for reproducible vendor records; otherwise current UTC is used.

Verify an existing package without network access:

```text
pc-agent-release-admin verify-feed update-feed-stable \
  --public-key-file release-public-key.txt \
  --key-id release-1
```

`--public-key` may be used instead of a file. Optional `--base-url` and
`--channel` checks bind verification to the vendor's intended deployment.

## Verification

Verification fails closed unless:

- the inventory contains no missing, extra, duplicate, unsafe, or symlinked
  files;
- every inventoried size and SHA-256 matches;
- publisher metadata has the exact supported shape and normalized base URL;
- the strict Phase 24 feed parser accepts `stable.json` for the channel;
- feed version, relative bundle/signature URLs, bundle size, bundle hash,
  and timestamp match the publisher manifest;
- the published ZIP passes the existing portable-bundle verifier;
- its detached Ed25519 signature verifies against the expected public key
  and key ID;
- customer configuration and hosting notes are byte-for-byte the canonical
  documents for those verified values.

Base URLs must be public HTTPS (plain HTTP is loopback-only), identify a
directory rather than a feed/ZIP file, and contain no credentials, query
string, or fragment. Customer downloads retain all Phase 24 same-origin,
redirect, TLS, size, hash, bundle, and signature checks.

## Hosting boundary

The package is ready to upload unchanged to a static HTTPS host, and the
existing consumer has an integration test against the published files.
However, this program does **not** create a hosting account, configure DNS,
obtain a TLS certificate, upload files, or prove that a chosen public host
is currently live. The vendor must still upload this exact tree to their
own HTTPS origin and may use the generated customer configuration.

Phase 55 later added **Windows HTTPS Update-Feed Hosting** around this
exact package: `build-hosting`/`verify-hosting`, a Windows-only built-in
TLS host and scheduled-task scripts, a versioned IIS deployment/rollback
profile, and retained `hosting-check` live evidence. That follow-up still
does not create DNS, a publicly trusted certificate, firewall/NAT access
or a public Windows machine; Linux/macOS hosting profiles are planned.
See `docs/phase55.md`.

This phase also does not add background polling, silent downloads, silent
staging/application, Authenticode, or a model-callable update tool. Online
updates remain disabled unless the customer explicitly enables them, and
applying a staged update remains a separate reviewed action.
