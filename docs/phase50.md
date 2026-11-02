# Phase 50 — Debian APT Repository (0.52.0)

Phase 50 publishes the verified Phase 47 native Debian package as a small
static APT repository. It adds repository metadata, hash verification, and
optional OpenPGP signing without adding a registered tool, a schema
migration, or a Python dependency.

## Repository layout

For the default `stable` suite, the builder creates:

- `pool/main/s/self-healing-pc-agent/self-healing-pc-agent_0.52.0-1_all.deb`
- `dists/stable/Release`
- `dists/stable/main/binary-all/Packages` and `Packages.gz`
- `dists/stable/main/binary-amd64/Packages` and `Packages.gz`
- `pc-agent-repository-manifest.json`

The `.deb` is architecture `all`. Both architecture index views contain the
same exact package stanza so an amd64 APT client can resolve it through its
native index and an `all` index is also present. The package stanza is made
from the verified package's real control fields plus its repository filename,
size, MD5, SHA-1, SHA-256, and SHA-512 values.

The `Release` file advertises `all amd64`, component `main`, and
`Acquire-By-Hash: no`. It contains MD5Sum, SHA1, SHA256, and SHA512 entries
for every plain and gzipped package index. Index gzip data and the Release
date are deterministic, so two unsigned builds from the same verified `.deb`
produce byte-identical repository trees.

## Commands

Build an unsigned repository from the default workspace `.deb`:

```text
pc-agent release debian-repository
```

Verify the default output:

```text
pc-agent release debian-repository-verify workspace\apt-repository-v0.52.0
```

The builder also accepts an existing verified `.deb`, an output directory,
and a lowercase suite/codename through `--output` and `--suite`. The output
directory must not already exist; the builder never merges into or overwrites
an older repository tree.

## Optional signing

`--signing-key` selects an existing GnuPG key by fingerprint, key ID, or user
ID. `--gnupg-home` may select an existing GnuPG home, and `--gpg` may select
an executable name or path. When signing is requested, the builder uses the
system GnuPG program as a subprocess without a shell to create:

- `dists/stable/Release.gpg`
- `dists/stable/InRelease`
- `self-healing-pc-agent-archive-keyring.gpg`

The application does not generate, import, store, or accept a private key or
key passphrase. The selected signing key must already be available to GnuPG
without giving passphrase material to this application. The individual `.deb`
does not receive a debsigs-style embedded signature; APT authenticates the
signed Release file, the Release file authenticates the package indexes, and
each package stanza authenticates the `.deb` by size and hash.

A repository public key downloaded from the same untrusted location as the
repository does not establish trust by itself; compare the fingerprint through a separate trusted channel before configuring APT to trust the exported
keyring.

## Verification boundary

The verifier rejects symbolic links, unexpected or missing files, malformed
manifests, stale hashes, inconsistent Release entries, modified package
indexes, and a pool package that does not pass the complete Phase 47 package
verifier. For a signed repository, it imports only the exported public key
into a temporary GnuPG home and verifies both signature forms against the
recorded primary-key fingerprint.

The application does not host the repository, configure APT sources, invoke
apt/dpkg in its own verifier, install the package, or upgrade an installed
system. Automated tests can use a local APT client against a temporary file
repository when APT is available, but that sandbox evidence is not a real
Debian/Ubuntu deployment transcript. Live Debian/Ubuntu install/upgrade evidence remains future work.

RPM repository metadata/signing, Apple packages, and hosting operations also
remain separate future work.


## Phase 52 follow-up — **Windows Repository Timestamp Hotfix**

Phase 52 fixes this builder's Windows-incompatible `os.utime`
`follow_symlinks=False` call with a capability-checked, symlink-rejecting
helper. See `docs/phase52.md`.
