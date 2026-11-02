# Phase 52 — Windows Repository Timestamp Hotfix (0.54.0)

Phase 52 fixes a Windows-only failure reported after Phase 50. The Debian
APT repository builder normalized every output timestamp with
`os.utime(path, (0, 0), follow_symlinks=False)`. CPython on Windows does not
implement `follow_symlinks` for `os.utime`, so each repository build raised
`NotImplementedError: utime: follow_symlinks unavailable on this platform`.
The Phase 51 RPM repository builder contained the same call and would have
failed the same way on Windows.

## Fix

Both builders now use one guarded helper. It rejects a symbolic link before
changing any timestamp, uses `follow_symlinks=False` only when
`os.utime in os.supports_follow_symlinks`, and otherwise uses the portable
`os.utime(path, (0, 0))` form. The builder-created output tree has already
been scanned and rejected if it contains a symbolic link, so the fallback
does not weaken symlink handling.

Regression tests simulate a platform without `os.utime`
`follow_symlinks` support, prove files and directories still receive the
zero timestamp, prove a symlink target is not retimestamped, and build and
verify both repositories under that simulated capability set.

## Boundary

This is a platform-compatibility hotfix only. No repository format, package
content, signing behavior, CLI command, registered tool (62 built-ins),
schema (v6), provider behavior, approval rule, or Python dependency changes.
