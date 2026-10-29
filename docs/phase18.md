# Phase 18: Production Hardening + Portable Windows Release

Phase 18 turns the verified personal build into something that can be
audited, backed up, diagnosed and copied to another Windows folder without
carrying private local state with it. It adds no registered AI tool, no
database migration and no runtime dependency. The application remains
versioned source installed into a local virtual environment; there is no
compiled executable, code-signed installer, auto-updater or cloud service.

## What is included

- `pc_agent.release`: production-readiness audit plus deterministic,
  hash-manifested portable ZIP bundles.
- `pc_agent.maintenance`: verified SQLite database backup, list, verify and
  approval-gated restore operations.
- `pc_agent.diagnostics`: redacted JSON diagnostics for user-controlled
  support sharing.
- `scripts/install_windows.ps1`: virtual-environment creation, editable
  install, Doctor/GUI checks, optional dev tests and optional shortcuts.
- `scripts/uninstall_windows.ps1`: shortcut removal; virtual-environment and
  user-data removal only behind explicit, exact-path switches.
- `scripts/build_release.ps1`: tests, Ruff, bundle build and verification.
- `scripts/launch_gui.cmd` and `scripts/launch_cli.cmd`: small local
  launchers for an installed portable tree.
- CLI `release`, `maintenance`, `diagnostics` and `doctor --json` commands.
- GUI Dashboard **Release Audit** plus Doctor production/backup summaries.

The schema remains **v6**, the built-in tool count remains **54**, and all
Phase 17 budgets and self-repair gates keep their previous defaults.

## Production audit

Run the read-only audit with:

```powershell
pc-agent release audit
```

It checks Python and version consistency, SQLite integrity/schema state,
folder writability, TLS trust, the usable provider route, credential-source
hygiene, explicitly enabled optional gates, finite task budgets,
self-repair baseline state when that gate is enabled, licensing posture and
the number of allow-listed release files.

Warnings are not hidden. For example, a mock-only route, an unlimited task
budget or an enabled optional gate is safe to inspect but is still reported.
Use `--strict` when warnings should produce a failing exit code and
`--json` for automation.

After upgrading with self-repair enabled, an old baseline is expected to be
reported as a version mismatch. Run the full test suite first, then capture
a new Phase 18 baseline through the normal approved Phase 17 workflow.

## Portable release bundles

Build a bundle with:

```powershell
pc-agent release build
```

The default output is
`workspace\self-healing-pc-agent-v0.20.0-portable.zip`. An explicit
`--output` path must end in `.zip` and must not already exist.

Only these project areas can enter a bundle:

- `.env.example`, `.gitignore`, `CHANGELOG.md`, `README.md` and
  `pyproject.toml`;
- `src/`, `tests/`, `docs/`, `examples/` and `scripts/`;
- `workspace/.gitkeep` only.

The builder excludes real `.env` files, databases, `data/`, user workspace
contents, patchers, virtual environments, egg-info output, Python caches,
patcher backups and symbolic links. Every bundled file has a size and
SHA-256 record in `release-manifest.json`. Entries use a fixed ZIP
timestamp and sorted order, so identical inputs produce an identical ZIP.

Verify or inspect a bundle without extracting it:

```powershell
pc-agent release verify workspace\self-healing-pc-agent-v0.20.0-portable.zip
pc-agent release info workspace\self-healing-pc-agent-v0.20.0-portable.zip --files
```

Verification rejects duplicates, traversal or non-canonical entry names,
symbolic-link entries, forbidden private/generated paths, oversized
entries, manifest mismatches, file-hash mismatches and version disagreement
between the manifest, `pyproject.toml` and `pc_agent.__version__`.

A hash manifest detects accidental or partial tampering, but it is not a
publisher signature. Someone who can replace both a bundle and its manifest
can create a different self-consistent bundle. Compare the printed bundle
SHA-256 with a value received through a trusted channel when that matters.

## Database backups and restore

Create a consistent SQLite backup with:

```powershell
pc-agent maintenance backup --label before-change
```

Backups are written below `data\backups` as a `.db` file plus a JSON
manifest containing the creation time, app/schema versions, size and
SHA-256. Creation uses SQLite's backup API and verifies integrity, required
tables and schema before publishing the backup.

List and verify backups with:

```powershell
pc-agent maintenance list
pc-agent maintenance verify latest
```

A backup contains the complete local database, which may include task
history, preferences, notes and workflow evidence. Treat it as private. It
is never included in a portable release bundle. Diagnostics are the safer
support artifact; do not send a database backup for ordinary support.

Restore is deliberately separate and approval-gated:

```powershell
pc-agent maintenance restore latest
```

Close the GUI and any other `pc-agent` process before restoring. The
service verifies the selected backup, creates a `pre-restore` backup of the
live database, checkpoints SQLite WAL state, stages and verifies the copy,
replaces the live database and verifies it again. The CLI then opens the
database through the normal migration path. JSON automation must pass
`--yes`; without it JSON mode does not prompt and the restore is cancelled.

## Redacted diagnostics

Export diagnostics with:

```powershell
pc-agent diagnostics export
```

The JSON includes application/platform versions, local paths, database
schema/size and backup count, public settings, provider names/models and
credential-source labels, built-in tool IDs, counts by task status/memory
kind, license state when enforcement is enabled, self-repair status and the
production audit.

It excludes:

- API keys and license keys;
- task instructions;
- preference and memory values;
- provider prompts;
- tool arguments and outputs;
- document and workspace file contents.

Local usernames can still appear inside the exported paths. Open and
inspect the file before sharing it. `pc-agent doctor --json` prints the
same redacted document to the terminal.

## Windows portable install

After verifying and extracting a portable ZIP to a new folder, run the
installer from PowerShell inside that folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_windows.ps1 -Dev -CreateShortcuts
```

Review the script before using an execution-policy bypass. The scripts are
plain text and are not code-signed. They require Python 3.11 or newer but do
not require administrator rights.

The installer:

1. finds the project root from its own `scripts` folder;
2. creates `.venv` with `py -3` or `python` when missing;
3. upgrades pip inside that virtual environment;
4. installs the project in editable mode (`.[dev]` with `-Dev`);
5. runs `pc-agent doctor` and `pc-agent-gui --check`;
6. with `-Dev`, runs pytest and Ruff unless `-SkipTests` is supplied;
7. with `-CreateShortcuts`, creates exact Desktop and Start Menu GUI
   shortcuts.

Editable installation means the extracted source folder is part of the
installation. Do not move or delete it while keeping its `.venv`; rerun the
installer after moving the folder.

## Release build script

From an already installed development tree:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_release.ps1
```

The script runs pytest and Ruff, builds the default portable ZIP in
`workspace\`, and verifies it. Supplying an explicit existing output path is
an error because release creation never overwrites a bundle.

## Uninstall helper

Default uninstall removes only the two exact shortcuts created by the
installer and preserves the source tree, virtual environment, `data`,
`workspace` and Windows Credential Manager entries:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall_windows.ps1
```

`-RemoveVirtualEnvironment` removes only the exact project `.venv`.
`-RemoveUserData` additionally requires `-Yes` and removes only the exact
project `data` and `workspace` folders. Remove stored provider keys first
with the normal `pc-agent credentials delete ...` commands if that is also
desired; the uninstall helper does not silently alter the OS vault.

## GUI boundary

The GUI Dashboard can run the read-only release audit and shows production
and database-backup summaries in Doctor. Building bundles, exporting
diagnostics, and backing up or restoring the database remain explicit CLI
operations. No registered tool, planner or provider path can invoke these
maintenance services.

## Deliberate limitations

- There is no signed `.exe`, MSIX/MSI installer, auto-update mechanism or
  publisher certificate.
- The portable bundle intentionally includes tests and documentation so an
  installation can be verified locally.
- The scripts do not install Python itself.
- Database restore must not run while another app process is actively
  writing to the same database.
- The audit is local and deterministic; it does not call providers, browse
  the web or prove that user-configured external services are online.
- Scripts and documentation are included in release bundles, but Phase 17
  self-repair still only baseline-restores `src/`, `tests/` and
  `pyproject.toml`.
