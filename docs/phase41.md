# Phase 41 — POSIX Installer Source Package

Phase 41 adds the Linux/macOS counterpart to the Phase 38 Windows installer
source package. It produces one deterministic, hash-manifested directory
with auditable POSIX shell scripts and the runtime subset of the existing
release tree.

| Item | Value |
|---|---|
| version | 0.43.0 |
| platforms | Linux and macOS POSIX shells |
| install mode | per-user; no administrator privilege |
| commands | `pc-agent release posix-source`, `posix-verify` |
| native package | none: no DEB, RPM, PKG, DMG, signing, or notarization |
| registered tools | 62 built-ins; no new tool |
| database schema | v6; no migration |
| new dependency | none |

## Generated package

```text
posix-package-manifest.json
README-POSIX.txt
install.sh
uninstall.sh
payload-files.txt
payload/
```

The payload is the runtime subset of `release_candidate_files()`: root
release files, `src/**`, `scripts/**`, `docs/**`, `examples/**`, and
`workspace/.gitkeep`. Tests, the real `.env`, databases, user data,
workspace contents other than `.gitkeep`, virtual environments, caches,
private keys, update staging/backups, and patcher artifacts are excluded.
Every root/payload file is listed in the manifest with size and SHA-256.

Without `--output`, `pc-agent release posix-source` creates
`workspace/posix-installer-v<version>`. Existing output is never
overwritten; an output inside the project must be under `workspace/`.
Generation uses a temporary sibling and publishes only after verification.
Root and payload shell scripts are marked executable on POSIX.

## Install behavior

The generated root installer can be run as `sh ./install.sh`. It accepts
`--prefix`, `--skip-python-setup`, and a guarded `--force` overlay option.

Defaults are:

- Linux: `${XDG_DATA_HOME:-$HOME/.local/share}/self-healing-pc-agent`
- macOS: `$HOME/Library/Application Support/Self-Healing PC Agent`

The installer:

1. refuses an unrelated non-empty target unless explicitly forced;
2. copies the payload while preserving existing `.env`, `data/`, and
   workspace files;
3. writes an install marker and the exact publisher payload file list;
4. requires Python 3.11+ (`python3` or `PYTHON_BIN`), creates `.venv`, and
   installs the local package into it;
5. runs `pc-agent doctor` and `pc-agent gui --check`;
6. creates `self-healing-pc-agent` and `self-healing-pc-agent-gui`
   wrappers under `${XDG_BIN_HOME:-$HOME/.local/bin}`;
7. creates a Linux `.desktop` entry or a macOS
   `Self-Healing PC Agent.command` launcher.

It never uses administrator elevation, downloads Python, edits the real
`.env`, changes system packages, or installs the Windows-only desktop
automation layer. Tkinter is still required for the GUI.

## Uninstall behavior

`sh ./uninstall.sh` removes only launchers/desktop integration and the
generated `.venv`. The application directory remains for inspection, and
`.env`, `data/`, workspace files, and OS credentials are preserved.

`sh ./uninstall.sh --remove-app --yes` additionally removes only the exact
paths in the publisher payload file list, after rejecting absolute or
parent-escaping entries. It then removes empty known application
directories. It still does not delete the real `.env`, user data/workspace
files, or credential-store entries; removing the remaining directory is a
separate manual decision.

## Verification

`pc-agent release posix-verify` does not execute shell code. It fails closed
for wrong version/platform, unsafe or duplicate manifest paths,
missing/extra files, hash/size mismatches, symbolic links, forbidden
payload entries, inconsistent payload lists/README, non-executable POSIX
scripts, or missing/unsafe install/uninstall/launcher wiring. Forbidden
script patterns include administrator elevation, shell download commands,
`eval`, system-package bypass flags, world-writable chmod, and broad root
or home deletion.

Automated tests additionally syntax-check every shell script and execute
the install/uninstall scripts on Linux in an isolated fake `HOME` with
Python setup skipped. That proves copy, marker, launcher, desktop-entry,
preservation, and exact payload-removal behavior; it does not prove a live
macOS desktop, Apple notarization/Gatekeeper acceptance, a Linux
distribution package manager, Keychain/Secret Service storage, or desktop
automation parity.

There is no GUI, planner/task/dynamic-tool, provider, schema, or
dependency change in this phase.

## Phase 47 follow-up — **Native Debian Package**

Phase 47 wraps this verified POSIX package in a deterministic
**Native Debian Package** with system launchers and a desktop entry. The
POSIX package remains the per-user bootstrap used on first launch, so the
preservation behavior above still applies. See `docs/phase47.md`.
