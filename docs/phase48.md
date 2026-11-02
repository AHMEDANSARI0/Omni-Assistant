# Phase 48 — Native RPM Package (0.50.0)

Phase 48 turns the verified Phase 41 POSIX installer source into a real,
deterministic RPM v4 binary package, mirroring the Phase 47 Debian package
for RPM-based distributions. Apple PKG and DMG remain future work.

## Direct commands

```text
pc-agent release rpm-package [--output PATH.rpm] [--packager "Name <address>"] [--json]
pc-agent release rpm-verify PACKAGE.rpm [--json]
```

Without `--output`, the builder writes
`workspace/self-healing-pc-agent-<version>-1.noarch.rpm`. Existing output is
never overwritten, and output inside the project must remain under
`workspace/`. The default local packager is
`Self-Healing PC Agent project <self-healing-pc-agent@localhost>`; use
`--packager` for a distribution-specific build identity.

## Package layout

The `.rpm` follows the documented RPM v4 on-disk format:

- a 96-byte Lead (`ed ab ee db`, format 3.0, binary, Linux);
- a signature header with a sealed immutable region (tag 62), zero-padded
  to eight bytes, carrying the header-only SHA1 (269) and SHA256 (273)
  digests, the header+payload SIZE (1000) and MD5 (1004), and the
  uncompressed PAYLOADSIZE (1007) — the tag rpm merges into the main
  header as ARCHIVESIZE, which is therefore deliberately not also baked
  into the main header;
- a main header with a sealed immutable region (tag 63) holding the package
  identity (name `self-healing-pc-agent`, version, release `1`, `noarch`,
  summary/description/group/vendor/license/packager, deterministic zero
  build time), the full compressed file list (basenames/dirnames/
  dirindexes) with per-file SHA-256 digests (`FILEDIGESTALGO` 8), modes,
  owners, sizes and verify flags, a self-provide, and the requirements
  `/bin/sh`, `python3 >= 3.11`, `python3-tkinter` plus the three format
  `rpmlib(...)` dependencies for compressed file names, file digests and
  prefixed payload names;
- a gzip-compressed SVr4 `newc` cpio payload (magic `070701`) whose entry
  order matches the header file list exactly, with `./`-prefixed names.

Header index entries are tag-sorted and their data is stored in the
canonical tight, type-aligned layout ending with the 16-byte immutable
region trailer, so repeated builds produce the same `.rpm` bytes for the
same source tree and packager. All file modes are `0644`/`0755`, owners are
`root:root`, and timestamps are zero.

There are **no scriptlets** (`%pre`, `%post`, `%preun`, `%postun`, trans
variants or their program tags). rpm therefore never runs project code as
root during package installation or removal. The payload contains:

- the complete verified Phase 41 POSIX package under
  `/usr/share/self-healing-pc-agent/posix-package/`;
- `/usr/bin/self-healing-pc-agent` and
  `/usr/bin/self-healing-pc-agent-gui` launchers;
- `/usr/share/applications/self-healing-pc-agent.desktop`;
- package documentation under `/usr/share/doc/self-healing-pc-agent/`
  (marked with the RPM doc file flag) and a JSON manifest with each
  payload file's mode, size and SHA-256.

## First-launch model

The application deliberately keeps its existing mutable per-user root
(`.venv`, `.env`, `data/`, workspace, self-repair state and OS credentials)
rather than pretending `/usr/share` is writable application state. A system
launcher therefore checks the bundled POSIX install marker under
`$PC_AGENT_HOME` or `${XDG_DATA_HOME:-~/.local/share}/self-healing-pc-agent`.
On first launch, or when the packaged version differs, it invokes the
already reviewed POSIX installer for that exact per-user target. Only then
does it delegate to the per-user CLI/GUI launcher.

The POSIX installer preserves `.env`, `data/`, workspace files and OS
credentials. Removing the RPM package removes the system payload,
launchers and desktop entry, but intentionally does not delete the per-user
application copy or user files.

## Verification

`rpm-verify` is implemented in Python and does not invoke `rpm`, `rpmbuild`
or shell code. It fails closed for a malformed lead; malformed, unsorted or
non-canonical signature/main headers; wrong immutable-region trailers;
signature SHA1/SHA256/MD5/SIZE/PAYLOADSIZE mismatches; forbidden scriptlet
tags; unexpected identity, provides or requires; gzip/cpio truncation or
unsafe names, modes or metadata; header file-list entries that disagree
with the cpio payload in order, size, mode or digest; embedded-manifest
inventory/hash/mode mismatches; incomplete launchers/desktop entry; or an
embedded POSIX package that fails the existing Phase 41 verifier.

During development the generated package was additionally checked with the
real RPM 4.20.1 tools: `rpmkeys --checksig` reported
`Header SHA256 digest: OK`, `Header SHA1 digest: OK` and `MD5 digest: OK`,
and `rpmquery` listed the files, dump metadata, provides and requires with
no scriptlets present. (Two real-format bugs found this way — the SHA256
signature tag is 273, and ARCHIVESIZE must not be baked into the main
header — are covered by the verifier so they cannot regress silently.)
The sandbox could not perform an actual `rpm --root` install because
chroot is not permitted there, so a live install/upgrade transcript on a
separate Fedora/RHEL-family machine remains operator evidence, exactly
like the live `dpkg -i` transcript for Phase 47.

Automated tests also build two packages byte-for-byte deterministically,
extract and syntax-check the system launcher from the payload, and execute
that launcher against a fake POSIX installer to prove the first-launch
target and delegation behavior. When the host provides an `rpm`/`rpmquery`
binary the test suite cross-checks the package identity with it.

## Boundaries

This package does not bundle Python wheels. Installing the `.rpm` itself
does not contact the network, but the first launch still uses the existing
per-user POSIX setup and may need network access while pip resolves the
project's declared Python dependencies for that machine. It is therefore a
native RPM delivery/bootstrap package, not yet an offline
distribution-native Python dependency package.

No OpenPGP package signing, dnf/yum repository metadata (`repodata`),
repository signing, distribution policy/upload review, source RPM, or live
`rpm -i/-U` transcript is claimed. The package is unsigned; installing a
local file still requires the operator to trust the file they built or
received and verify its SHA-256. Dependency names follow Fedora/RHEL
conventions (`python3-tkinter`); other RPM families may name the Tkinter
package differently.

No registered tool (still 62), database schema change (still v6), provider,
credential behavior, approval rule, Python dependency, or Windows/macOS/
Debian packaging behavior is introduced by Phase 48.

## Phase 51 follow-up — **RPM Repository**

Phase 51 publishes this verified `.rpm` in a deterministic static
**RPM Repository** with `Packages/`, primary/filelists/other XML,
`repomd.xml`, optional system-GnuPG `repomd.xml.asc` signing, and a strict
repository manifest. See `docs/phase51.md`.
