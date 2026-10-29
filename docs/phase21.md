# Phase 21: Signed Offline Updates

Phase 21 turns the verified Phase 18 portable ZIP into an explicit offline
update package. It does **not** download code, run a background updater,
silently install anything, sign a Windows EXE, or expose updating as an AI /
planner tool. The operator supplies the bundle locally and approves apply or
rollback as a separate step.

## Trust model

- A vendor-side Ed25519 private key signs the bundle's exact version and
  SHA-256 in a detached `bundle.zip.sig.json` file.
- The client trusts only `PC_AGENT_RELEASE_PUBLIC_KEY` from local
  configuration. A public key inside an untrusted file is never used as its
  own trust anchor.
- `PC_AGENT_RELEASE_KEY_ID` can pin the expected signing-key label.
- `PC_AGENT_REQUIRE_SIGNED_UPDATES=1` makes unsigned, unverified, invalid and
  key-mismatched bundles fail closed during staging.
- If no public key is configured, a signature is reported `unverified`, not
  `verified`. Staging an unsigned/unverified bundle requires the explicit
  `--allow-unsigned` operator decision and is forbidden when signed updates
  are required or a public key is configured.
- Invalid signatures and key-ID mismatches are always rejected.

The release signer is intentionally separate from the normal client CLI:

```powershell
pc-agent-release-admin keygen --out-dir D:\vendor-keys --key-id release-1
```

```powershell
pc-agent-release-admin sign workspace\self-healing-pc-agent-v0.24.0-portable.zip --private-key D:\vendor-keys\release-private-key.txt --key-id release-1
```

Keep `release-private-key.txt` only on the vendor signing machine. Release
bundles reject `.key`, `.pem`, `.p12`, `.pfx` and private-key-named files.

Client configuration uses only the public values printed by `keygen`:

```dotenv
PC_AGENT_RELEASE_PUBLIC_KEY=public-base64url-value
PC_AGENT_RELEASE_KEY_ID=release-1
PC_AGENT_REQUIRE_SIGNED_UPDATES=1
```

## Update flow

1. **Check** verifies the ZIP manifest and every file hash, strictly compares
   `MAJOR.MINOR.PATCH` versions, and inspects the detached signature without
   extracting files.
2. **Stage** re-verifies the bundle, enforces the signature/version policy,
   extracts only manifest-listed files under `data/updates/<update-id>/staging`,
   hashes them again, copies the signature for evidence, and writes a receipt.
3. **Apply** requires approval. It records whether each target existed and its
   previous SHA-256, backs up and verifies every replaced file before changing
   the root, atomically copies each staged file, then verifies every final
   file. A failure restores replaced files and preserves newly created files
   under `failed-created` for evidence.
4. **Roll back** requires approval and refuses to overwrite any file changed
   after apply. Replaced files are restored from the receipt backup; files
   created by the update are moved to `rolled-back-created`, not permanently
   deleted.

Existing release files that are absent from a newer bundle are listed as
stale in the receipt and preserved. Phase 21 is an overlay updater, not a
destructive synchronizer. User `.env`, databases, credentials, workspace
contents and other data are not bundle entries and are not update targets.

After apply or rollback the running process still has the old code loaded.
Reinstall editable package metadata and restart:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .[dev,gui]
```

```powershell
pc-agent doctor
```

## CLI

```powershell
pc-agent update check workspace\self-healing-pc-agent-v0.24.0-portable.zip
```

```powershell
pc-agent update stage workspace\self-healing-pc-agent-v0.24.0-portable.zip
```

The stage command prints the exact staged update ID and the next apply
command. It also accepts `latest-staged` so no placeholder has to be copied:

```powershell
pc-agent update apply latest-staged
```

```powershell
pc-agent update list
```

```powershell
pc-agent update show latest
```

```powershell
pc-agent update rollback latest-applied
```

`--allow-downgrade` on stage is required for a same-version reinstall or any
older bundle. JSON output is available on every update subcommand; JSON apply
and rollback require `--yes` instead of an interactive prompt.

## GUI

The Dashboard gains a **Signed Offline Updates (Phase 21)** panel with bundle
and signature pickers, update-ID field, allow-unsigned / allow-downgrade
checkboxes, and Check, Stage, Apply Staged, Roll Back and List Updates actions.
Apply and rollback ask for GUI confirmation before calling the same service
used by the CLI. Vendor signing remains CLI-only.

## Boundaries kept

- No network update check, download URL, background service or silent install.
- No `update.*` planner/orchestrator tool; built-in tools remain 58.
- No database schema change; schema remains v6.
- No new runtime dependency; the existing strict in-repo Ed25519 verifier and
  signer are reused.
- No Windows Authenticode, signed EXE/MSI or SmartScreen claim.
- This Ed25519 implementation's pre-existing non-constant-time limitation
  remains documented. Private keys belong only on a trusted vendor machine.

## Remaining work after Phase 21

- Signed Windows installer/Authenticode and any future online update channel.
- macOS/Linux packaging plus Keychain/Secret Service credential parity and
  desktop-automation parity.
- Scanned-PDF OCR, table extraction, PDF passwords, forms and digital
  signatures.
- Live Anthropic verification after a user key exists.
- Live execution evidence not yet shown for `install_windows.ps1`,
  `uninstall_windows.ps1` and the `.cmd` launchers.
