# PHASE 13 - Browser automation (DevTools protocol)

Phase 13 lets the agent drive a **real browser session that you start
yourself**: open pages, read them, click, type and screenshot — through
the Chromium DevTools Protocol (CDP), with the same rules as the rest of
the agent: off by default, approvals for actions, independent
verification, no automatic retries.

## Turning it on

1. In `.env` set:

   ```
   PC_AGENT_ALLOW_BROWSER=1
   # Optional: PC_AGENT_BROWSER_PORT=9222
   # Optional: PC_AGENT_BROWSER_TIMEOUT_SECONDS=20
   ```

2. Start Edge or Chrome **with a debugging port and a throwaway
   profile** (separate from your everyday browser). PowerShell, from
   your project folder:

   ```powershell
   & "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222 --user-data-dir="$PWD\browser-profile"
   ```

   Chrome equivalent:

   ```powershell
   & "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$PWD\browser-profile"
   ```

   (If the path differs on your machine, adjust it — check where the
   browser is installed.)

3. Check `pc-agent doctor` — the Browser line should say
   `enabled; ... connected (...)`.

⚠️ A debugging port lets **any program on this machine** drive that
browser window. Use the throwaway profile above, do not sign into
banking/important accounts in it, and close that browser window when
you are done. The agent only ever connects to `127.0.0.1:<port>`.

## What can it do

| Tool | What it does | Approval |
|---|---|---|
| `browser.status` | Connected browser + open tabs | No (read-only) |
| `browser.snapshot` | URL, title, visible text + links of the current tab | No |
| `browser.open` | Open one exact http(s) URL (new tab) and wait for load | Yes, every time |
| `browser.click` | Click the unique element matching a CSS selector or exact visible text | Yes, every time |
| `browser.type` | Set one text field's value (password/file fields refused) | Yes, every time |
| `browser.screenshot` | Save a PNG of the current tab into the workspace (never overwrites) | No |

"The current tab" is the first real page tab in the debug browser
(most recently opened); pass `--target-id` (from `browser status`) to
pin a specific tab. Reads act on that tab; `browser.open` creates a
new tab unless `--target-id` navigates an existing one.

## Safety model

- **Off by default** and bound to localhost: the agent cannot attach
  to a browser you did not explicitly start with a debugging port.
- **Fresh approvals** for open/click/type, bound to the exact
  arguments (URL, selector, text) — including on resume/workflow
  replay, like commands. Browser actions are **never auto-retried**.
- **URL rules**: http(s) only, no credentials in URLs; private/local
  addresses are blocked in tasks (the direct CLI accepts
  `--allow-private` for one exact open of your local dev server).
  Page-initiated requests after load are *not* filtered — that is what
  the throwaway profile is for.
- **No password typing**: password, file and similar fields are
  refused by design. Never use the agent to log in somewhere for you.
- **Verified**: opened tabs are re-read (host check), clicks are
  followed by a fresh snapshot, typed text is read back from the
  field and compared with the approved text, screenshots are
  size/hash-checked as real PNGs.
- **AI planning** may only propose the read-only `browser.status` /
  `browser.snapshot` — never open/click/type.
- Clicks dispatch a DOM click on the unique matching element; if
  several elements match, the tool refuses to guess.

## Ways to use it

Directly (actions prompt unless `--yes`):

```powershell
pc-agent browser status
pc-agent browser open https://example.com
pc-agent browser snapshot
pc-agent browser click --text "More information..."
pc-agent browser type "#searchInput" --text "Karachi"
pc-agent browser screenshot homepage.png
pc-agent browser open http://127.0.0.1:8000 --allow-private
```

As tasks:

```powershell
pc-agent run "Open Wikipedia in the browser" --browser-open https://en.wikipedia.org
pc-agent run "Read the browser page"
pc-agent run "Type in the search box" --browser-type-selector "#searchInput" --browser-type-text "Karachi"
pc-agent run "Screenshot the browser page" --output wiki.png
pc-agent run "Open https://example.com in the browser then read the browser page" --yes
```

In the GUI's Web tab, **Browser Status** and **Read Current Page**
buttons use the same session (read-only). Driving actions from the GUI
works through the normal task box with the usual approval dialog.

## Known limitations

- One debug browser/port at a time; the agent does not start or stop
  the browser for you (by design).
- Heavy single-page apps that replace their DOM on click can need a
  moment; the tools wait for load events with the configured timeout
  but there is no arbitrary "wait for selector" step yet.
- No downloads, uploads, tabs management (close/reorder), PDF
  printing, or cookie/storage inspection in this phase.
- Screenshots capture the visible viewport of the current tab.
