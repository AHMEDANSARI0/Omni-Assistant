# Phase 49 — Windows UTF-8 Test Hotfix (0.51.0)

Phase 49 fixes the single Windows-only test failure reported after
Phase 48:

```text
UnicodeDecodeError: 'charmap' codec can't decode byte 0x9d ...
tests/test_phase45_macos_keychain.py::test_phase45_scope_and_documentation
```

## Root cause

The project documentation is UTF-8. In particular,
`docs/providers.md` contains the curly-quoted text `“model not found”`.

The scope/documentation tests introduced across Phases 45–48 called
`Path.read_text()` without an `encoding` argument. Python then uses the
process locale encoding. That is UTF-8 on the Linux verification host, but
it is commonly cp1252 on a Windows Python installation. cp1252 cannot
decode every byte sequence in the UTF-8 documents; byte `0x9d`, from the
closing curly quote, is undefined there.

This was a test portability bug, not a macOS Keychain runtime failure and
not a problem in the user's Windows configuration. No Administrator mode,
Developer Mode, `PYTHONUTF8` setting, locale change, Git Bash, or WSL is
required.

## Fix

- Added `encoding="utf-8"` to all 34 locale-dependent `.read_text()` calls
  found in the tests, covering the Phase 45, Phase 46, Phase 47 and Phase 48
  scope/documentation tests.
- Added the same explicit encoding to the two remaining
  `.write_text()` calls in the Phase 40 update-feed tamper test.
- Made the generated fake Linux/macOS credential-helper state reads and
  writes explicitly UTF-8 as well.
- Added an AST regression test that scans every Python file under `src/`
  and `tests/` and fails if a `Path.read_text` or `Path.write_text` call
  lacks an explicit `utf-8` or `utf-8-sig` encoding.
- Added a direct UTF-8 byte-decode regression test for the provider,
  Phase 45, README and changelog documents that previously crossed the
  locale boundary.

An audit after the fix found 379 explicit UTF-8 text calls (plus one
existing deliberate `utf-8-sig` call) and no locale-dependent
`Path.read_text`/`Path.write_text` call in checked Python source.

## Locale-independent reproduction

The failure was reproduced on Linux before the fix by forcing a non-UTF-8
locale:

```text
LC_ALL=C LANG=C PYTHONCOERCECLOCALE=0 PYTHONUTF8=0 \
  python -m pytest tests/test_phase45_macos_keychain.py::test_phase45_scope_and_documentation -q
```

Before the fix this failed while decoding a UTF-8 project document. After
the fix, the Phase 45, Phase 46, Phase 47 and Phase 48
scope/documentation tests all pass under the same forced non-UTF-8 locale.

The five expected Windows skips reported by the user remain correct:
ordinary Windows accounts cannot always create symbolic links, and the
POSIX/Debian/RPM launcher-execution tests require a POSIX shell. This
hotfix does not weaken those guards or any production symlink rejection.

## Boundaries

Phase 49 changes tests and documentation only. It adds no registered tool
(still 62), no database schema change (still v6), no provider or
credential behavior, no approval rule, and no Python dependency. Production
text handling was already explicitly UTF-8 and is not behaviorally changed.
