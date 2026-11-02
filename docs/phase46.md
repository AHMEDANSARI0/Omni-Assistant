# Phase 46 — Windows Inno Acceptance Harness (0.48.0)

Phase 46 adds the missing operator-run acceptance path for the Phase 38
Windows Inno Setup source package. On a real Windows user session it can
compile, install, verify, and uninstall the actual generated installer in an
isolated directory and retain a machine-checkable JSON report.

## Direct commands

```text
pc-agent release installer-acceptance [--output PATH] [--compiler ISCC_PATH] [--json]
pc-agent release installer-acceptance-verify REPORT [--json]
```

Without `--output`, the run uses
`workspace\windows-installer-acceptance-v<version>`. An existing output is
never overwritten. The compiler is resolved only from an explicit
`--compiler`, `INNO_SETUP_COMPILER`, `PATH`, or the normal Inno Setup 6
Program Files locations. The harness never downloads Inno Setup or Python.

## Acceptance sequence

The run is deliberately guarded and retains evidence at every stage:

1. **Preflight** proves the host is Windows, finds `ISCC.exe`, and refuses to
   continue if the stable Inno AppId `C80BA7A3-9651-46C2-9D45-D1DADF2AD063`
   already has a current-user uninstall entry or if the
   `Self-Healing PC Agent` Start Menu group already exists.
2. **Source build** creates and re-verifies a fresh Phase 38 installer-source
   package beneath the acceptance output.
3. **Compile** invokes `ISCC.exe` as an argv array, records its process log,
   and hashes the generated `SelfHealingPCAgent-Setup-<version>.exe`.
4. **Install** runs that setup with `/VERYSILENT`, `/SUPPRESSMSGBOXES`,
   `/NORESTART`, `/SP-`, `/DIR=` and `/LOG=`, targeting only
   `<acceptance-output>\app`. Inno documents these silent/setup options for
   programmatic installation.
5. **Installed verification** compares every manifested payload file with
   the installed copy, requires exactly one Inno uninstaller, checks the
   three Start Menu shortcuts and uninstall registry entry, and runs the
   installed `pc-agent.exe --version`, `pc-agent.exe --root <app> doctor`,
   and `pc-agent-gui.exe --root <app> --check` commands.
6. **Sentinels** created after install are a synthetic `.env`, one `data`
   file, and one `workspace` file. Their sizes and SHA-256 hashes are put in
   the report. They are not provider keys and contain no secret.
7. **Uninstall** runs Inno's generated uninstaller silently with logging
   and waits for the registry entry, shortcuts, virtual environment, and
   uninstaller to disappear.
8. **Post-uninstall verification** requires every payload file to be gone
   while all three sentinel hashes remain byte-identical.

If verification fails after installation begins, one cleanup uninstall is
attempted and recorded. Cleanup may only affect the acceptance install
created after the preflight proved that the AppId and shortcut group were
unused. The harness never installs into the default
`{localappdata}\Programs\Self-Healing PC Agent` location and never deletes
the acceptance report, source package, compiled setup, process logs, or
sentinel user files.

## Report

`<acceptance-output>\windows-installer-acceptance-report.json` records the
format, stable AppId, application version, paths, compiler, source and setup
hashes, registry observations, independent checks, sentinel hashes, each
step's status/exit code/duration, and the final error if any.
`installer-acceptance-verify` only accepts a passed report for the current
application version and, when the retained artifacts are present, re-verifies
the installer source, setup EXE hash, and sentinel hashes.

Process stdout/stderr and Inno logs are retained under
`<acceptance-output>\logs`. They may contain local paths, so treat the
acceptance folder as build evidence rather than a public release artifact.
The harness does not intentionally write provider keys or `.env` values to
those logs; installed-app `doctor`/`gui --check` output is the application's
existing secret-free status surface.

## Boundaries

The Linux development host used for this patcher does not provide a live Windows acceptance transcript.
Phase 46 is therefore verified here with a
fake Windows process/registry/shortcut environment driving the real harness,
plus the existing Phase 38 source tests. The remaining evidence step is for
the Windows user to install Inno Setup 6 if necessary and run
`pc-agent release installer-acceptance` in a signed-in desktop session.

Silent acceptance does not prove the interactive wizard's appearance, a
desktop-shortcut opt-in, Windows Defender behavior, or a SmartScreen result.
It also performs no Authenticode signing: the setup remains unsigned, with no
publisher certificate, trusted timestamp, or SmartScreen reputation claimed.

No registered tool (still 62), database schema change (still v6), provider,
credential behavior, approval rule, or Python dependency is introduced by
Phase 46.
