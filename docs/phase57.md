# Phase 57 — Live Debian/Ubuntu APT Evidence

Phase 47 built the native `.deb`, and Phase 50 built and verified the static
APT repository. Their automated APT oracle proved update/candidate/download
behavior in an isolated state directory, but deliberately stopped before a
system install. Phase 57 adds the direct live operation that closes that
specific evidence gap on a real Debian or Ubuntu host.

## Direct commands

Build the current package first:

```text
pc-agent release debian-package
```

Build its APT repository:

```text
pc-agent release debian-repository
```

Run the live install/verify/purge acceptance after reviewing the exact
confirmation prompt:

```text
pc-agent release debian-repository-acceptance
```

Verify the retained passed report offline:

```text
pc-agent release debian-repository-acceptance-verify workspace\debian-apt-acceptance-report.json
```

The acceptance command also accepts an explicit repository path,
`--report`, `--gpg` for signed-repository verification, `--yes` for the
exact direct-operation approval, and `--json`.

## What the live run proves

The one-shot acceptance performs these ordered checks:

1. `/etc/os-release` identifies the host exactly as Debian or Ubuntu;
2. `apt-get`, `apt-cache`, `dpkg` and `dpkg-query` are available;
3. the operator has root or passwordless `sudo` access;
4. the complete repository and its embedded current `.deb` pass the
   existing offline verifiers;
5. `self-healing-pc-agent` is absent before the run, so a real pre-existing
   installation is never replaced or purged by acceptance;
6. APT updates through a temporary source file and isolated APT
   state/cache;
7. `apt-cache policy` and `apt-cache show` select the exact package
   version, repository-relative filename, size and SHA-256;
8. `apt-get install --no-install-recommends` installs that package;
9. `dpkg-query` reports `install ok installed` for the exact version;
10. every installed file matches the verified Debian manifest SHA-256 and
    POSIX mode, and the installed manifest itself matches the verified
    package;
11. `apt-get purge` attempts cleanup even after an install/verification
    failure; and
12. `dpkg-query` proves the acceptance package absent again.

The host's permanent APT source files are not edited. The temporary main
source contains only the local repository; the host's normal
`sources.list.d` entries remain visible to APT so declared system
**dependencies** can be resolved. The candidate filename/hash check still
requires the application package itself to be the exact verified local
repository artifact.

## Repository trust

For a signed Phase 50 repository, the temporary source uses
`signed-by=` with a temporary copy of the repository's exported keyring.
The fingerprint must still be compared through a trusted channel before it
means production trust.

For an unsigned local repository, the temporary source uses `trusted=yes`.
That exception exists only for this bounded local acceptance source and is
recorded as `trusted-yes-local-acceptance-only`. It is not a recommendation
to configure production APT with `trusted=yes`.

## System-change boundary

This is a real system operation. APT may install the package's declared
system dependencies, such as Python venv/Tk support, and may need network
access for those distribution packages. Cleanup purges only
`self-healing-pc-agent`; it does not auto-remove dependencies. It refuses
to start if the application package already exists in any installed or
config-files state.

The `.deb` itself still has no maintainer scripts and does not bundle
Python wheels. This acceptance proves APT/dpkg installation and installed
file integrity. It does not claim that the first GUI/CLI launch has created
its per-user virtual environment or downloaded Python dependencies.

## Report

Passed and failed attempts write a strict self-hashed JSON report under
`data\debian-apt-acceptance\` by default. It records the host distribution
and tool versions, repository/package hashes, trust mode, APT candidate,
installed state, verified-file count, cleanup result, check details, and
for each command only its name, return code, elapsed time, output byte
counts and output SHA-256 values. Raw APT output and local repository/temp
paths are not retained in report fields.

`debian-repository-acceptance-verify` checks the exact current-version
shape, all-passed checks, internal consistency and self-hash offline. Like
the other local evidence reports, that detects later alteration; it is not
a cryptographic attestation by Debian, Ubuntu or APT and cannot make a
wholly fabricated file impossible.

## Scope

Phase 57 adds no registered tool, schema migration or Python dependency.
Fedora/RHEL DNF, embedded RPM signing, repository hosting, Apple packages
and desktop-platform parity remain separate work.
