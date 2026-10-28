# PHASE 14 - Windows Desktop UI Automation

Phase 14 lets the agent inspect and operate **already-open Windows
application windows** through Microsoft UI Automation (UIA): list windows,
inspect their accessible elements, focus one exact window, click or set one
exact element, send one bounded key chord, and save a verified screenshot.

It is deliberately not unrestricted computer control. There are no
coordinate-click, drag, scroll, arbitrary-key, app-launch, window-close or
password-typing tools in this phase.

## Turning it on

In your local `.env`, set:

```dotenv
PC_AGENT_ALLOW_DESKTOP_AUTOMATION=1
# Optional: PC_AGENT_DESKTOP_AUTOMATION_TIMEOUT_SECONDS=30
```

Then restart the CLI/GUI. `pc-agent doctor` should show a **Desktop
automation: enabled** line and the Microsoft UI Automation runtime state.

The native backend is Windows-only. It uses Win32 for top-level windows,
focus and screenshots, and Microsoft UI Automation through the
Windows-only `comtypes` package for element inspection and control
patterns. The dependency is skipped automatically on macOS/Linux, where
live desktop tools fail closed with a Windows-only message.

> Phase 14's automated test suite uses a simulated desktop backend, so
> selector, approval, verification, CLI and GUI behavior are covered
> deterministically without a Windows desktop in the build sandbox. Run
> `pc-agent desktop windows` on the target Windows machine as the first
> live check after enabling the gate.

## What can it do

| Tool | What it does | Fresh approval in tasks |
|---|---|---|
| `desktop.windows` | Lists visible top-level windows: handle, title, class, process, bounds and foreground state | No (read-only) |
| `desktop.window_inspect` | Lists one window's bounded UIA elements, control types, AutomationIds, runtime ids, patterns and readable editor values | No (read-only) |
| `desktop.window_focus` | Restores/foregrounds exactly one selected window | Yes, every time |
| `desktop.window_click` | Clicks exactly one selected UIA element, preferring Invoke/Toggle/Selection/ExpandCollapse/Legacy patterns | Yes, every time |
| `desktop.window_type` | Replaces one readable text element's value and verifies it by read-back; password elements are refused | Yes, every time |
| `desktop.send_keys` | Sends one allow-listed chord such as `Ctrl+S` or `Enter` to one foreground-verified window | Yes, every time |
| `desktop.screenshot` | Saves the full desktop or one selected window as a new workspace PNG | No |

All seven tools require `PC_AGENT_ALLOW_DESKTOP_AUTOMATION=1`. They are in
recovery's no-retry set: a desktop action is never automatically replayed.

## Selecting windows and elements

A window must resolve to **exactly one** visible top-level window. Use one
or more of these selectors; every supplied selector must match:

- `--window-handle`
- `--window-title`
- `--window-title-contains`
- `--window-class`
- `--window-process-id`

First run `pc-agent desktop windows`. Then inspect the chosen window to see
stable selectors for its elements:

```powershell
pc-agent desktop inspect --window-title-contains Notepad
```

An element must also resolve exactly, using one or more of:

- `--element-runtime-id`
- `--element-name`
- `--element-name-contains`
- `--element-automation-id`
- `--element-class`
- `--element-control-type` (`Button`, `Edit`, `Document`, ...)
- `--element-index` (only as a 1-based tie-breaker among the other matches)

If several windows or elements match and no deterministic tie-breaker was
approved, the tool refuses to guess.

## Direct CLI examples

Run these one at a time. Mutating commands print an approval prompt unless
you add `--yes` for that exact already-reviewed command.

```powershell
pc-agent desktop windows
```

```powershell
pc-agent desktop inspect --window-title-contains Notepad
```

```powershell
pc-agent desktop focus --window-title-contains Notepad
```

```powershell
pc-agent desktop click --window-title-contains Notepad --element-name Save
```

```powershell
pc-agent desktop type --window-title-contains Notepad --element-control-type Document --text "Hello from PC Agent"
```

```powershell
pc-agent desktop keys --window-title-contains Notepad Ctrl+S
```

```powershell
pc-agent desktop screenshot desktop.png
```

```powershell
pc-agent desktop screenshot notepad.png --window-title-contains Notepad
```

Add `--json` to any Phase 14 desktop command for the structured tool result.

## Task examples

Read-only requests can be planned from plain instructions. Mutating desktop
actions use exact `run` options so the approval shows the exact submitted
arguments:

```powershell
pc-agent run "List desktop windows"
```

```powershell
pc-agent run "Inspect the Notepad window"
```

```powershell
pc-agent run "Focus the Notepad window"
```

```powershell
pc-agent run "Click Save in Notepad" --desktop-window Notepad --desktop-click-name Save
```

```powershell
pc-agent run "Type in Notepad" --desktop-window Notepad --desktop-type-name "Text editor" --desktop-type-text "Hello"
```

```powershell
pc-agent run "Save in Notepad" --desktop-window Notepad --desktop-keys Ctrl+S
```

```powershell
pc-agent run "Screenshot the desktop" --output desktop.png
```

AI planning may propose only the read-only `desktop.windows` and
`desktop.window_inspect` tools. It cannot propose focus, click, type, send
keys or screenshot actions.

## GUI

The GUI Desktop tab adds:

- **List Windows** — read-only `desktop.windows`
- **Inspect Window** — `desktop.window_inspect` for the title text entered
- **Desktop Screenshot** — confirmation, then a full-desktop screenshot or
  one for the entered window title

Driving actions from the GUI go through the normal task box and its exact
approval dialog. The agent refuses focus/click/type/key actions aimed at its
own process window.

## Safety and verification model

- **Off by default** behind `PC_AGENT_ALLOW_DESKTOP_AUTOMATION`.
- **Fresh approvals:** focus, click, type and keys require a new approval
  for the exact arguments on every orchestrated run, including resume and
  workflow replay. Direct CLI commands use their own confirmation or
  an explicit `--yes` for that exact command.
- **No auto-retry:** all desktop automation tools are excluded from
  automatic recovery retries.
- **Exact selectors only:** ambiguous windows/elements are refused.
- **No password typing:** an element whose UIA `IsPassword` property is
  true is refused before any text is sent.
- **Read-back typing:** writable elements must expose a readable UIA Value,
  LegacyIAccessible or Text contract; the final value must equal the
  approved text. Elements that cannot be read back are refused before
  keyboard fallback is used.
- **Bounded keyboard input:** one allow-listed chord per call, at most 10
  repeats. The Windows key and system/window-switching chords such as
  `Alt+F4`, `Alt+Tab`, `Alt+Escape`, `Ctrl+Alt+Delete` and
  `Ctrl+Shift+Escape` are refused.
- **Foreground proof:** key sending re-checks that the approved window is
  still foreground before every repeat.
- **Own-window refusal:** actions do not target the agent process itself.
- **Independent verification:** orchestrated runs re-list or re-inspect the
  desktop, compare target handles/values/chords, and verify screenshots by
  PNG signature, size and SHA-256.
- **Screenshots stay local:** the only file written is a new PNG inside the
  workspace; existing files are never overwritten.

## Limitations

- UIA quality depends on the target app. Some custom/canvas/game surfaces
  expose few or no meaningful elements; inspection will show that honestly.
- An elevated/admin application may be inaccessible to a non-elevated agent
  because of Windows UIPI. The tool reports the failure rather than
  escalating privileges.
- Lock screens, secure desktops, UAC prompts and other users' sessions are
  not automated.
- Window handles and runtime ids are session-local evidence, not permanent
  identities. Re-list/re-inspect after restarting an app.
- Click uses UIA patterns first, then a physical click at the element's
  UIA clickable point after foregrounding its window. Overlapping windows
  and rapidly changing UIs can still make a target stale; verification may
  fail rather than guessing or retrying.
- Screenshots respect the current process DPI behavior of the host Python
  process. The saved PNG's IHDR records its actual pixel dimensions.
- No app launching/closing, drag/drop, scrolling, arbitrary mouse paths,
  clipboard automation, OCR or image-template clicking is included.
