# Phase 51 — RPM Repository (0.53.0)

Phase 51 publishes the verified Phase 48 native RPM package as a small static
DNF/YUM repository. It adds deterministic repository metadata, strict
offline verification, and optional OpenPGP signing of the repository index
without adding a registered tool, a schema migration, or a Python dependency.

## Repository layout

The builder creates:

- `Packages/self-healing-pc-agent-0.53.0-1.noarch.rpm`
- `repodata/repomd.xml`
- `repodata/<sha256>-primary.xml.gz`
- `repodata/<sha256>-filelists.xml.gz`
- `repodata/<sha256>-other.xml.gz`
- `pc-agent-rpm-repository-manifest.json`

The primary metadata is derived from the verified RPM's own header: name,
version, release, architecture, summary, description, packager, license,
vendor, group, build host, installed/archive sizes, real header byte range,
provides/requires and the conventional primary file subset. Internal
`rpmlib(...)` requirements are omitted from repository metadata, as they are
by normal repository generators. The filelists metadata contains every
payload file and owned directory, including the embedded RPM manifest. The
package has no RPM changelog tags, so the other metadata contains only the
package/version identity.

`repomd.xml` records the SHA-256 checksum and size of each compressed XML
file plus the SHA-256 checksum and size of its uncompressed XML. XML gzip
files use a zero timestamp and both `repomd.xml` revision/timestamps and the
package `time` values are zero, so two unsigned builds from the same verified
`.rpm` produce byte-identical repository trees. No SQLite repository
databases are generated; modern DNF/YUM can use the XML metadata directly.

## Commands

Build an unsigned repository from the default workspace `.rpm`:

```text
pc-agent release rpm-repository
```

Verify the default output:

```text
pc-agent release rpm-repository-verify workspace\rpm-repository-v0.53.0
```

The builder also accepts an existing verified `.rpm` and an output directory
through `--output`. The output directory must not already exist; the builder
never merges into or overwrites an older repository tree.

## Optional signing

`--signing-key` selects an existing GnuPG key by fingerprint, key ID, or user
ID. `--gnupg-home` may select an existing GnuPG home, and `--gpg` may select
an executable name or path. When signing is requested, the builder uses the
system GnuPG program as a subprocess without a shell to create:

- `repodata/repomd.xml.asc`
- `RPM-GPG-KEY-self-healing-pc-agent`

The application does not generate, import, store, or accept a private key or
key passphrase. The selected signing key must already be available to GnuPG
without giving passphrase material to this application.

The RPM package itself is not OpenPGP-signed. This phase signs the repository
metadata index: with repository GPG checking enabled, DNF can authenticate
`repomd.xml`, its checksums authenticate the XML metadata, and the primary
metadata authenticates the package by SHA-256. Enabling separate RPM package
signature checking still requires a future package-signing process.

A repository public key downloaded from the same untrusted location as the
repository does not establish trust by itself; compare the fingerprint through a separate trusted channel before configuring DNF/YUM to trust the exported
key.

## Verification boundary

The verifier rejects symbolic links, unexpected or missing files, malformed
manifests, stale hashes, malformed XML, inconsistent metadata, and a package
that does not pass the complete Phase 48 package verifier. For a signed
repository, it imports only the exported public key into a temporary GnuPG
home and verifies `repomd.xml.asc` against the recorded primary-key
fingerprint.

The application does not host the repository, configure DNF/YUM sources,
invoke dnf/yum/rpm in its own verifier, install the package, or upgrade an
installed system. Development checks can compare the generated XML with
`createrepo_c` and read a signed local repository with DNF in a disposable
sandbox, but that is not a real Fedora/RHEL deployment transcript. Live Fedora/RHEL install/upgrade evidence remains future work.

Apple packages and repository/hosting operations also remain separate future
work.


## Phase 52 follow-up — **Windows Repository Timestamp Hotfix**

Phase 52 fixes this builder's identical latent Windows `os.utime`
compatibility failure by reusing the same capability-checked,
symlink-rejecting helper as the Debian repository builder. See
`docs/phase52.md`.
