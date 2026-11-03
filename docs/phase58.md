# Phase 58 — Windows Auto-Upgrade Lock Hotfix

Phase 58 fixes the two real Windows failures reported against the Phase 56
background auto-upgrade tests:

- `test_phase56_stage_only_downloads_verifies_and_never_applies`
- `test_phase56_apply_when_idle_gates_then_applies_and_reinstalls`

Both completed their first `run_auto_upgrade` call and then raised
`PermissionError: [Errno 13] Permission denied` from `msvcrt.locking(...,
msvcrt.LK_UNLCK, 1)` when a later call exited the worker lock.

## Root cause

The worker intentionally keeps a persistent `auto-upgrade.lock` file so
creating the lock does not race with deleting/recreating it. The old code
opened that file with `a+b` and immediately called
`msvcrt.locking(..., LK_NBLCK, 1)`.

On Windows, `msvcrt.locking` locks bytes beginning at the handle's
**current file position**. An existing non-empty file opened with `a+b`
starts positioned at EOF. The first run created an empty file, so EOF was
**byte 0** and its unlock worked. A later run locked the byte at the
then-current EOF, while `__exit__` correctly sought to byte 0 and tried to
unlock that different byte. Windows therefore rejected the unlock.

This was not a download, signature, staging, apply or permissions-policy
failure. It was a byte-range mismatch in the single-instance lock release.

## Fix

The Windows branch now:

1. seeks to **byte 0** before `LK_NBLCK` on every run;
2. records that lock offset; and
3. seeks to the same offset before `LK_UNLCK`.

The persistent lock file, non-blocking collision behavior, POSIX `flock`
branch, state/report formats and all update safety gates are unchanged.
The exception is not swallowed: lock and unlock now refer to the same byte
range.

## Regression evidence

phase58 adds a platform-independent fake `msvcrt` that models the relevant
Windows current-position byte-range semantics with real file offsets. It
pre-creates a non-empty lock file, performs two sequential lock cycles, and
verifies that a nested second worker still fails closed. The regression
reproduces `PermissionError` against the Phase 57 implementation and
passes against Phase 58; the two reported Phase 56 behavior tests also
remain in the full suite.

## Scope

Phase58 adds no registered tool (62), schema migration (v6), configuration
key or Python dependency. It does not change what background auto-upgrade
is allowed to download, stage or apply.
