# Phase 42 — Windows Symlink Test Hotfix

Phase 42 fixes the one Windows-only test failure reported after the Phase 40
patch: `WinError 1314` while the Phase 39 test itself tried to create a
symbolic link. The failure happened before the `.env` cleanup code was
called, so it did not show that production cleanup touched `.env` or accepted
an unsafe path.

| Item | Value |
|---|---|
| version | 0.44.0 |
| reported error | `OSError: [WinError 1314] A required privilege is not held by the client` |
| affected area | `tests/test_phase39_env_cleanup.py` only |
| runtime behavior | unchanged |
| registered tools | 62 built-ins; no new tool |
| database schema | v6; no migration |
| new dependency | none |

## Fix

The symlink-rejection scenario is now the separate
`test_phase39_cleanup_rejects_symlink_env_file` test:

1. It creates a real temporary `.env` file and attempts to create
   `linked.env` as a symbolic link to it.
2. If Windows refuses link creation with `OSError` because Developer Mode or
   the required privilege is unavailable, only that symlink-specific test is
   skipped with the operating-system reason.
3. If link creation succeeds, the test still proves that
   `plan_env_key_cleanup` rejects the symlink and leaves the target `.env`
   byte-for-byte unchanged.

Splitting the scenario also preserves the other Phase 39 fail-closed checks
as passed tests on an unprivileged Windows account: unavailable credential
store, changed `.env`, changed vault value, and malformed `.env` entry. They
are no longer hidden behind a later environment-permission failure.

## Security boundary

Phase 42 does not weaken production behavior. The Phase 39 cleanup planner
still refuses a symlink `.env`, still requires an identical credential-store
value before a provider line can be removed, and still never creates a
plaintext backup or prints a secret. Running pytest as Administrator solely
to create symlinks is not required; enabling Developer Mode merely allows
the dedicated symlink test to execute instead of skip.

This hotfix adds no installer, provider, network, credential, PDF, GUI,
schema, dependency, or registered-tool behavior.
