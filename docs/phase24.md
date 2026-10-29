# Phase 24: Online Signed Update Download

Phase 24 extends the Phase 21 signed-update system with an explicit,
operator-initiated HTTPS download path. There is **no background checker, no
silent download, and no silent installation**. Online staging still leaves
application separate: `online-stage` can download, verify and stage an update,
but it will **never auto-apply** it.

## Trust model

The update feed is untrusted metadata. It only tells the application which
version, bundle URL, optional signature URL, expected SHA-256 and expected
size to download. Trust is established later, locally:

1. The feed must be strict JSON with no duplicate or unknown fields.
2. The advertised application and channel must match this application and the
   configured channel.
3. The bundle and signature URLs must remain on the **same origin** as the
   final feed URL.
4. Public URLs must use HTTPS. Plain HTTP is accepted only for loopback test
   servers.
5. Resolved public addresses must be global; private, link-local, reserved
   and nonstandard-port targets are blocked outside the loopback exception.
6. Redirects are limited to three and cannot leave the feed origin or
   downgrade the scheme.
7. Feeds are capped at 65,536 bytes, signatures at 64,000 bytes, and bundles
   at `PC_AGENT_ONLINE_UPDATE_MAX_BYTES` (25,000,000 by default).
8. The downloaded bundle length and SHA-256 must match the feed before the
   bundle is written under `data/updates/downloads`.
9. The existing portable-bundle verifier and detached Ed25519 signature
   checker then re-check the exact downloaded file and version.
10. Existing Phase 21 signature policy still controls staging. Invalid or
    key-mismatched signatures cannot be staged. Unsigned or unverifiable
    bundles require the same explicit `--allow-unsigned` override and remain
    forbidden when a release public key or required-signatures policy make
    them ineligible.

A failed download keeps a `download-receipt.json` with the feed host/hash,
manifest, local paths, check result when available, and error. Feed URLs must
not contain credentials, query strings or fragments, so receipts and Doctor
output do not need URL secrets. Doctor reports only the configured feed host.

## Configuration

Online downloads are disabled by default:

```dotenv
PC_AGENT_ONLINE_UPDATES=0
PC_AGENT_UPDATE_FEED_URL=https://updates.example.com/self-healing-pc-agent/stable.json
PC_AGENT_UPDATE_CHANNEL=stable
PC_AGENT_ONLINE_UPDATE_TIMEOUT_SECONDS=30
PC_AGENT_ONLINE_UPDATE_MAX_BYTES=25000000
PC_AGENT_RELEASE_PUBLIC_KEY=vendor-public-key-goes-here
PC_AGENT_RELEASE_KEY_ID=release-1
PC_AGENT_REQUIRE_SIGNED_UPDATES=1
```

Set `PC_AGENT_ONLINE_UPDATES=1` only when the operator intentionally wants
the direct CLI/GUI online controls to contact the configured feed. A one-off
CLI or GUI feed override still does not bypass the enabled setting.

## Feed format 1

```json
{
  "format": 1,
  "app": "self-healing-pc-agent",
  "channel": "stable",
  "version": "0.27.0",
  "bundle_url": "release-0.27.0.zip",
  "signature_url": "release-0.27.0.zip.sig.json",
  "bundle_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "size_bytes": 2536319,
  "published_at": "2026-10-29T00:00:00Z"
}
```

Relative bundle and signature URLs are resolved against the final feed URL.
Absolute URLs are also accepted only when they use the exact same scheme,
host and port. `signature_url` and `published_at` are optional; every other
field above is required, and unknown fields are rejected.

## CLI

Fetch the feed only and compare versions. This does **not** authenticate the
bundle:

```powershell
pc-agent update online-check
```

Download and verify the bundle/signature without staging it:

```powershell
pc-agent update online-download
```

Download, verify and stage without applying:

```powershell
pc-agent update online-stage
```

Apply remains the existing explicit step after reviewing the staged update:

```powershell
pc-agent update apply latest-staged
```

Each online command accepts `--feed-url` and `--channel` overrides;
`online-download` and `online-stage` accept `--allow-downgrade`, and
`online-stage` accepts `--allow-unsigned` with the Phase 21 meaning. All
online commands support `--json`.

## GUI

The Doctor tab's signed-update panel now includes online feed and channel
fields plus **Check Online**, **Download Online**, and **Download + Stage**
buttons. The GUI uses the same core functions and policies as the CLI. It
does not poll for updates when opened.

## Boundaries

- No background service, scheduler, silent download, silent staging, silent
  apply, restart, package installation or telemetry was added.
- No planner/task tool can initiate an online update; tool count stays 60.
- No AI provider sees the feed, bundle or signature.
- The downloaded ZIP is still the Phase 18 portable bundle format. This is
  not a signed Windows installer and does not provide Authenticode.
- Rollback, backup, stale-file preservation and post-apply editable reinstall
  instructions remain the Phase 21 behavior.
- Version `0.26.0`; database schema remains v6; no new runtime dependency.

## Remaining work after Phase 24

- A signed Windows installer/Authenticode flow remains separate future work.
- A true background auto-update service was intentionally not implemented.
- macOS/Linux packaging, Keychain/Secret Service credentials and desktop
  automation parity remain future platform work.
- PDF password, form and digital-signature workflows remain separate.
- Live Anthropic verification still needs a user-provided Anthropic key.
- A real public vendor update feed still requires hosting the portable ZIP,
  its detached `.sig.json`, and a format-1 feed on one HTTPS origin, plus
  configuring the matching public key locally.
