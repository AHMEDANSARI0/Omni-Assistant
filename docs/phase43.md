# Phase 43 — Windows POSIX Test Hotfix

Phase 43 fixes the two remaining Windows-only test failures reported after
Phase 42. Both occurred in Phase 41 POSIX-package tests while the package
was being generated on a Windows host; neither showed a broken POSIX payload
or a change to the generated script contents.

| Item | Value |
|---|---|
| version | 0.45.0 |
| reported failures | POSIX execute-bit assertion; missing `sh` executable |
| affected area | `tests/test_phase41_posix_package.py` only |
| runtime behavior | unchanged |
| registered tools | 62 built-ins; no new tool |
| database schema | v6; no migration |
| new dependency | none |

## Execute-bit check

Python on Windows/NTFS reports ordinary files without the POSIX user-execute
bit, typically mode `0o666` (decimal 438). The Phase 41 deterministic-package
test unconditionally asserted `S_IXUSR`, so it failed even though the package
contents and hashes were correct.

The deterministic, inventory, hash, payload-list, and script-content checks
still run on Windows. The filesystem execute-bit assertion now runs only on
a POSIX host, matching `verify_posix_package`. A package generated on
Windows is started on its target with `sh ./install.sh`; once the installer
copies the payload, its existing `chmod +x` step normalizes the installed
POSIX scripts and generated launch wrappers.

## Shell execution check

The install/uninstall integration test assumed a `sh` executable. Windows
does not provide one by default, so subprocess creation failed with
`FileNotFoundError: [WinError 2]`. That execution test now skips on a
non-POSIX host or when `sh` is unavailable. Installing Git Bash or WSL is not
required to run the Windows test suite.

On Linux and other POSIX hosts with `sh`, the same test still executes the
generated installer and uninstaller in a fake home and proves that `.env`,
workspace files, and data files are preserved. The `sh -n` syntax checks in
the deterministic-package test likewise run only when a POSIX `sh` is
available.

## Security boundary

Phase 43 does not weaken package verification. Script bytes remain
hash-matched to the manifest, root and payload scripts must match each
other, forbidden shell wiring is rejected, and POSIX hosts must still have
executable generated scripts. No installer, provider, credential, PDF, GUI,
schema, dependency, or registered-tool behavior changes.
