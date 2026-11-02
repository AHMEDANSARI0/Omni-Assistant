# Phase 47 — Native Debian Package (0.49.0)

Phase 47 turns the verified Phase 41 POSIX installer source into a real,
deterministic Debian binary package. It is the first native distribution
package in the remaining DEB/RPM/PKG/DMG group;
RPM, PKG and DMG remain future work.

## Direct commands

```text
pc-agent release debian-package [--output PATH.deb] [--maintainer "Name <address>"] [--json]
pc-agent release debian-verify PACKAGE.deb [--json]
```

Without `--output`, the builder writes
`workspace/self-healing-pc-agent_<version>-1_all.deb`. Existing output is
never overwritten, and output inside the project must remain under
`workspace/`. The default local maintainer is
`Self-Healing PC Agent project <self-healing-pc-agent@localhost>`; use
`--maintainer` for a distribution-specific build identity.

## Package layout

The `.deb` is an `ar` archive containing, in order, `debian-binary` (`2.0`),
`control.tar.gz`, and `data.tar.gz`. Control metadata declares:

- package `self-healing-pc-agent`;
- package version `<application-version>-1`;
- architecture `all`;
- section `utils`, priority `optional`;
- dependencies `python3 (>= 3.11)`, `python3-venv`, and `python3-tk`;
- recommendations `ca-certificates` and `secret-tool`;
- calculated `Installed-Size` and a complete `md5sums` inventory.

There are **no maintainer scripts**. dpkg therefore never runs project code
as root during package installation or removal. The data archive contains:

- the complete verified Phase 41 POSIX package under
  `/usr/share/self-healing-pc-agent/posix-package/`;
- `/usr/bin/self-healing-pc-agent` and
  `/usr/bin/self-healing-pc-agent-gui` launchers;
- `/usr/share/applications/self-healing-pc-agent.desktop`;
- package documentation and a JSON manifest with each payload file's mode,
  size and SHA-256.

All tar entries are sorted, root-owned, timestamp-zero and limited to modes
`0644`/`0755`; repeated builds produce the same `.deb` bytes for the same
source tree and maintainer.

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
credentials. Removing the Debian package removes the system payload,
launchers and desktop entry, but intentionally does not delete the per-user
application copy or user files.

## Verification

`debian-verify` is implemented in Python and does not invoke `dpkg` or shell
code. It fails closed for malformed/truncated `ar`, gzip or tar archives;
wrong member order; duplicate, unsafe, linked or oversized entries; unsafe
modes; missing/extra control files or maintainer scripts; wrong control
fields; wrong `Installed-Size`; `md5sums` mismatches; embedded-manifest
inventory/hash/mode mismatches; incomplete launchers/desktop entry; or an
embedded POSIX package that fails the existing Phase 41 verifier.

Automated tests also build two packages byte-for-byte deterministically,
ask the host `dpkg-deb --info`/`--contents` to read one when `dpkg-deb` is
available, extract and syntax-check the system launcher, and execute that
launcher against a fake POSIX installer to prove the first-launch target and
delegation behavior.

## Boundaries

This package does not bundle Python wheels. Installing the `.deb` itself
does not contact the network, but the first launch still uses the existing
per-user POSIX setup and may need network access while pip resolves the
project's declared Python dependencies for that machine. It is therefore a
native Debian delivery/bootstrap package, not yet an offline
distribution-native Python dependency package.

No RPM, Apple PKG/DMG, repository metadata, apt repository hosting,
repository signing, package signing, Debian archive policy/upload review,
or live `dpkg -i` transcript on a separate Debian/Ubuntu machine is claimed.
The package is unsigned for repository purposes; installing a local file
with dpkg/apt still requires the operator to trust the file they built or
received and verify its SHA-256.

No registered tool (still 62), database schema change (still v6), provider,
credential behavior, approval rule, Python dependency, or Windows/macOS
packaging behavior is introduced by Phase 47.

## Phase 48 follow-up — **Native RPM Package**

Phase 48 adds the RPM counterpart of this package: a deterministic
**Native RPM Package** built and verified by
`pc-agent release rpm-package` / `rpm-verify`, reusing the same verified
POSIX package and per-user first-launch model. See `docs/phase48.md`.

## Phase 50 follow-up — **Debian APT Repository**

Phase 50 publishes this verified `.deb` in a deterministic static
**Debian APT Repository** with `pool/`, `Packages`/`Packages.gz`, a
hash-complete `Release`, optional system-GnuPG `Release.gpg`/`InRelease`
signing, and a strict repository manifest. See `docs/phase50.md`.
