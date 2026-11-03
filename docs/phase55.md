# Phase 55 — Windows HTTPS Update-Feed Hosting

Phase 55 turns the verified Phase 40 static update-feed package into an
operational Windows hosting deployment. It is intentionally **Windows-first**:
the shared profile structure already names Linux/macOS targets, but those
targets are marked `planned` and fail closed until their own deployment and
live acceptance work is done after the Windows path passes.

| Item | Value |
|---|---|
| Version / phase label | 0.57.0 / PHASE 55 Windows HTTPS Update-Feed Hosting |
| Implemented profiles | `windows-https-task`, `windows-iis` |
| Planned profiles | `linux-nginx`, `linux-caddy`, `macos-nginx`, `macos-caddy` |
| Vendor commands | `pc-agent-release-admin hosting-profiles`, `build-hosting`, `verify-hosting`, `serve-hosting` |
| Customer/operator commands | `pc-agent update hosting-check`, `pc-agent update hosting-check-verify` |
| Feed format | Existing Phase 24 format 1 |
| Registered tools | 62 built-ins; no new tool |
| Database schema | v6; no migration |
| New dependency | None |
| Public host created by the patcher | No |

## Operational structure

`build-hosting` first re-verifies the complete Phase 40 feed package,
including its inventory, portable bundle and Ed25519 signature. It then
creates a new hosting directory containing the exact feed package under
`site/`, Windows deployment files under `windows/`, a Windows operator
README, and `hosting-manifest.json`. The hosting manifest binds the profile,
public base/feed URLs, deployment name/root, host/port/path prefix, release
public key/key ID, feed and bundle hashes, publisher-manifest hash, and a
SHA-256 inventory of every other file.

Verification rejects missing, extra, duplicated, unsafe or symlinked files;
recomputes the embedded Phase 40 verification; and requires the generated
Windows files to be byte-for-byte canonical for the recorded profile and
deployment settings. Existing output directories are refused. The package
contains no release-signing private key, TLS certificate or TLS private key.

## Windows built-in HTTPS host

The default `windows-https-task` profile includes:

- `Start-UpdateFeedHost.ps1` for a foreground start;
- `Register-UpdateFeedHost.ps1` for an AtStartup SYSTEM scheduled task;
- `Unregister-UpdateFeedHost.ps1` for stopping/removing that task;
- `Verify-LiveUpdateFeed.ps1` for the post-deployment live check.

`pc-agent-release-admin serve-hosting` is enabled on Windows only in this
phase. Before opening a socket it re-verifies the hosting package and its
embedded site. It serves only the verified feed files and a small
`healthz` JSON response below the recorded URL prefix. Directory listing,
traversal, query strings and non-GET/HEAD methods are rejected. It supports
ETag revalidation and one bounded byte range for a bundle download.

Cache policy is deliberate:

- `stable.json`: `public, max-age=60, must-revalidate`;
- files under `releases/`: `public, max-age=31536000, immutable`;
- `healthz`: `no-store`.

The server requires TLS 1.2 or newer and an operator-supplied certificate
chain/private-key pair. Their contents are loaded by the operating system's
TLS stack and are never copied into the hosting package, printed, or written
to a report. Restrict the private-key file to SYSTEM and Administrators
when the scheduled task is used.

## Windows IIS profile

The `windows-iis` profile is for a Windows host that already runs IIS. It
includes root and `releases/` `web.config` files, plus deployment and
rollback scripts. Deployment verifies the hosted `site/` hashes, copies the
site into a new versioned release directory, grants IIS read access, creates
or updates the No Managed Code application pool and HTTPS site binding,
binds an existing `LocalMachine\My` certificate by thumbprint, records the
previous physical path in `hosting-state.json`, and only then starts the
site. Rollback switches the site back to that recorded previous directory.

The IIS configuration disables directory browsing/default documents, allows
only the public feed file extensions, sets JSON/ZIP MIME types, gives feeds a
one-minute client cache, gives versioned release files a one-year client
cache, and sends `nosniff`, no-referrer, restrictive CSP and HSTS headers.

## Live evidence

Building a hosting package proves only that the local files are correct; it
does **not** prove that DNS, a publicly trusted TLS certificate, firewall or
NAT, the Windows service/task, or the public endpoint works. After the
Windows host is deployed, run `pc-agent update hosting-check` as one
explicit operator action. It:

1. requires an HTTPS feed URL and a verified TLS connection;
2. fetches the strict Phase 24 feed with bounded redirects and sizes;
3. requires a short/no-cache feed policy;
4. requires feed, bundle and signature URLs to stay on the same origin;
5. downloads the signature and bundle into memory;
6. matches bundle size and SHA-256 against the feed;
7. verifies the portable ZIP structure in a temporary folder;
8. verifies the detached Ed25519 signature against the configured public
   key and key ID;
9. requires a long-lived cache policy for immutable release files; and
10. writes a secret-safe, self-hashed JSON report.

The report stores only public URLs, HTTP status/cache/content-type/ETag
metadata, byte counts and SHA-256 values. It does not store response bodies,
the public-key value, any private key, or an API key. The downloaded update
is **not staged or applied** by the hosting check. 
`pc-agent update hosting-check-verify` strictly revalidates a passed
current-version report offline.

## Operator infrastructure still required

No patcher can truthfully create the operator's public infrastructure. For
a public Windows deployment the operator must supply:

- a Windows machine that remains available for the intended service window;
- a DNS name pointing to that machine or its public reverse proxy;
- a publicly trusted TLS certificate for that exact name;
- firewall/NAT permission for the selected HTTPS port;
- the release-signing keypair already used by Phase 40, with the private
  key kept off customer machines and outside both packages.

The software now provides the complete Windows deployment package, host,
IIS alternative, rollback path and retained live evidence command. A passed
live report from the operator's real public URL is the point at which the
selected host is proven operational.

## Linux/macOS boundary

The profile registry lists `linux-nginx`, `linux-caddy`, `macos-nginx` and
`macos-caddy` so the package format, feed verification and report shape do
not need to be redesigned later. Their status is `planned`, package builds
and the built-in `serve-hosting` command reject them in Phase 55, and no
Linux/macOS deployment or live acceptance is claimed. They will be handled
after the Windows hosting path passes on the user's Windows machine.

**Phase 56** later added the Windows background consumer for a feed hosted
by this contract. It does not relax Phase 55: the worker accepts only the
same signed HTTPS feed/key and retains its own state and run reports. A
public host still needs a passed Phase 55 `hosting-check` before it is used
for background upgrades. See `docs/phase56.md`.
