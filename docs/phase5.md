# Phase 5 — Desktop GUI

Phase 5 adds a local Windows-first desktop window for the Self-Healing PC
Agent. It uses Python's standard-library Tkinter, so this phase adds **no new
runtime dependency** and no database migration. The database remains schema
v4.

The GUI is a front-end, not a second agent engine. Its headless
`GuiController` calls the same orchestrator, recovery service, provider
router, tool registry, policy contexts and SQLite repositories as the CLI.
It does not shell out to `pc-agent` commands.

## Start the GUI

Run from the project folder so the current folder is the application root:

```powershell
cd D:\omni-assistant
.\.venv\Scripts\Activate.ps1
pc-agent gui
```

In Windows CMD, after the virtual environment is active, the launch command
is the same:

```bat
pc-agent gui
```

The installer metadata also creates a windowed launcher after reinstalling:

```bat
.\.venv\Scripts\pc-agent-gui.exe
```

That launcher still uses the current folder or `PC_AGENT_HOME` as the
application root unless `--root` is supplied. Check the root shown in the GUI
header before running work. A packaged desktop shortcut/installer remains a
later production-hardening item.

To test readiness without opening a window:

```powershell
pc-agent gui --check
pc-agent doctor
```

`doctor` now also reports whether Tkinter is importable.

## Tabs

| Tab | What it does |
|---|---|
| Dashboard | Doctor/TLS summary, recovery scan, deterministic PDF/Office demos, open the workspace folder |
| Tasks | Create/run orchestrated tasks; view/filter history and details; diagnose and recover selected tasks; approve exact external read-only scopes in a dialog |
| Documents | Office inspection/extraction/conversions and DOCX creation; PDF inspection, verification and folder merge |
| Web | Fetch one exact HTTP(S) page as bounded text and optionally save it to a new workspace `.txt`/`.md` file |
| Desktop | System information; open one exact workspace file/folder or HTTP(S) URL in the default app/browser |
| Tools | Inspect built-in and approved dynamic tool contracts; invoke approved dynamic `custom.*` tools with JSON arguments |
| Providers | Provider route/status table and an explicit provider **Ask** action; keys are never shown or edited |

Long-running GUI operations run one at a time in a worker thread so the
window stays responsive. External-folder task approvals are bridged back to
the GUI thread and require a decision on the exact paths shown.

## Safety boundaries preserved

- Orchestrated tasks still use the deterministic planner first and the same
  allow-list, verification and bounded recovery as the CLI.
- Network authority is granted only by the Web tab's exact **Fetch Page**
  action. Allowing a private/local address requires an additional checkbox
  and confirmation for that one fetch.
- Desktop-open authority is granted only by the Desktop tab's exact open
  actions or Dashboard's confirmed **Open Workspace** action.
- Document inputs/outputs remain workspace-bound, and outputs are not
  overwritten by the underlying tools.
- The Tools tab cannot generically invoke built-ins. It invokes only
  approved dynamic tools; built-ins run through Tasks or their dedicated
  tabs.
- Provider keys stay in the local environment/`.env`. The GUI displays no
  key material and provides no credential editor.
- There is still no shell execution, screenshot capture, clicking/typing in
  other applications, arbitrary program launch or full browser automation.

## Quick smoke test

1. Open the GUI and confirm the header root is your project folder.
2. On **Dashboard**, run **Create PDF Demo**.
3. On **Tasks**, keep the sample merge instruction and folders, choose a
   new output such as `gui-merged-2.pdf`, and click **Run New Task**.
   The task table should show `succeeded` after deterministic verification.
4. On **Documents**, create the Office demo from Dashboard if needed, then
   run **CSV Inspect** for `Office Demo/sales.csv`.
5. On **Web**, fetch `https://example.com/` and save to a new output name.
6. On **Tools**, select `custom.text_word_count`, keep the sample JSON, and
   invoke it. The word count should be 3.

If an output already exists, choose a new output name; the tools never
overwrite by design.

## Troubleshooting

- **“could not open a window”**: run from a signed-in Windows desktop
  session, not a headless/service shell. Run `pc-agent gui --check` and
  `pc-agent doctor` and share those two outputs.
- **Tkinter unavailable**: use the official Windows Python installer build
  with the Tcl/Tk component, or keep using the CLI. Do not install random
  GUI packages as a substitute.
- **Wrong project data**: close the GUI, `cd` into the intended project
  folder (or pass `--root`) and relaunch. The header/root and Dashboard
  database path identify the active install.
- **Closing during a task**: the GUI asks first. A task left active can be
  found later with recovery scan rather than being silently deleted.

## Not included in Phase 5

- Dynamic Tool Builder generation/repair/approval lifecycle screens; those
  remain CLI-only in this release.
- Settings/API-key editor, licensing/subscriptions, cloud sync or installer.
- Screenshots, keyboard/mouse control, external-app automation, shell access
  or arbitrary browser control.
