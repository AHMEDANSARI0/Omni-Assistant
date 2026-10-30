# Phase 38 — Windows Installer Source Package (unsigned)

Phase 38 turns the existing reviewed release tree into a deterministic,
hash-manifested **Inno Setup source package** that can be inspected and then
compiled on Windows. It does **not** add a new model-callable tool, planner
behavior, dynamic code loading, a schema migration, a runtime dependency,
Authenticode signing, SmartScreen reputation, or a signed installer.

| Item | Value |
|---|---|
| version | 0.40.0 |
| installer family | Inno Setup 6 |
| install mode | per-user, no administrator privilege required |
| Inno AppId | C80BA7A3-9651-46C2-9D45-D1DADF2AD063 |
| signing | unsigned; Authenticode not performed |
| compiled EXE in the source package | not included until compiled on Windows |

## What is generated

`pc-agent release installer-source` copies the runtime subset of the
allow-listed release candidate owned by `release_candidate_files()`:

- root release files such as `pyproject.toml`, `.env.example`, `README.md`,
  and `CHANGELOG.md`;
- `src/**`, `scripts/**`, `docs/**`, and `examples/**`;
- the empty `workspace/.gitkeep` marker.

It deliberately excludes `tests/**`, the real `.env`, databases, user data,
workspace contents other than `.gitkeep`, virtual environments, caches,
private keys, update staging/backups, `.mjs` patchers, and other files
already forbidden by the release policy.

The output directory contains:

- `payload/` — the exact application tree Inno Setup installs;
- `SelfHealingPCAgent.iss` — the generated Inno script with explicit
  one-file-per-line `[Files]` entries;
- `windows-installer-manifest.json` — version, stable AppId, file sizes, and
  SHA-256 hashes for the generated script and every payload file.

The checked-in `scripts/windows_installer.iss` is the token template. The
builder replaces its version and files tokens from the manifested payload;
compiled output later appears under the source package's `output/`
directory and is reported but ignored by source verification.

## Direct CLI

The existing direct release command group gains:

```text
pc-agent release installer-source [--output PATH] [--json]
pc-agent release installer-verify SOURCE_DIR [--json]
```

Without `--output`, installer source is created under
`workspace/windows-installer-v<version>`. Existing output is never
overwritten. Output inside the project must be under `workspace/`; output
outside the project is allowed. The builder writes to a temporary sibling,
verifies it completely, and only then publishes the final directory.

Verification fails closed for wrong version/AppId, unsafe or duplicate
manifest paths, missing/extra source files, size/hash mismatches,
symbolic links, forbidden payload files, unresolved template tokens,
explicit-file mismatch, administrator mode, external/download file flags,
or missing install/uninstall/start-menu wiring.

## Installer behavior

The generated script:

- uses a stable `AppId`, so Inno treats later versions as the same
  application for uninstall/repair bookkeeping;
- sets `PrivilegesRequired=lowest`;
- defaults to `{localappdata}\Programs\Self-Healing PC Agent`;
- creates Start Menu shortcuts for the existing GUI and CLI launchers, plus
  an optional desktop shortcut;
- after copying files, runs the existing `scripts/install_windows.ps1` so
  `.venv` creation, editable package installation, directory setup,
  `doctor`, and the GUI environment check remain the same reviewed Python
  workflow used by the portable install;
- on uninstall, runs the existing conservative uninstall helper with
  `-RemoveVirtualEnvironment` and removes install directories only if empty.

Uninstall therefore removes installer-tracked application files and the
generated virtual environment, but preserves non-empty `data/`,
`workspace/`, the real `.env`, and Windows Credential Manager entries.
Deleting user data, workspace files, shortcuts outside Inno's tracking, or
credentials remains a separate explicit action.

## Compile on Windows

`scripts/build_windows_installer.ps1`:

1. runs the test suite and Ruff unless `-SkipTests` is supplied;
2. builds and verifies a fresh installer-source directory;
3. locates Inno Setup's `ISCC.exe` from `INNO_SETUP_COMPILER`, `PATH`, or
   the conventional Inno Setup 6 Program Files locations;
4. fails with a clear message—without downloading anything—if Inno Setup is
   absent;
5. compiles `SelfHealingPCAgent-Setup-<version>.exe` and prints its
   SHA-256 when compilation succeeds.

Python and Inno Setup are prerequisites on the build machine. The installer
does not silently download either one, embed a Python runtime, or replace
the portable ZIP and `install_windows.ps1` paths.

## Honest boundaries

The Linux development/verification environment has no Inno compiler,
PowerShell, or Windows desktop, so this phase verifies source generation,
hashes, explicit Inno wiring, and CLI behavior only. It does **not** prove
that `ISCC.exe` compiled a setup EXE, that installation/uninstallation ran
on a live Windows account, that shortcuts launched the GUI/CLI, or that
Windows SmartScreen/Defender accepted an installer.

The build is also intentionally **unsigned**: no Authenticode signature,
trusted timestamp, publisher reputation, EV/OV certificate status, or
SmartScreen result is claimed. A future signing phase needs a real
Authenticode certificate and a Windows build/check run.

There is no registered model-callable installer tool, and no GUI, provider,
database/schema, task, planner, or dynamic-authority change in this phase.
