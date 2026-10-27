# Phase 4 — Office, web fetch and desktop tools

Phase 4 adds useful local productivity tools while keeping the existing
policy, approval, verification and no-overwrite boundaries. It is deliberately
**not** full browser automation or desktop GUI control.

## What is included

- DOCX, XLSX, PPTX and CSV inspection/text extraction from the workspace.
- CSV column statistics and bounded sample rows.
- CSV → JSON, JSON → CSV and CSV → verified one-sheet XLSX.
- Simple verified DOCX creation from a title and paragraphs.
- One exact HTTP(S) page fetched as bounded text through the dedicated
  `pc-agent web fetch` command.
- Local system information and opening one exact workspace file/folder or
  HTTP(S) URL through dedicated `pc-agent desktop ...` commands.
- Copy-friendly task references: `latest`, `latest-failed` and
  `latest-<status>`.
- Quieter normal handling of known corrupt PDFs: the structured skip reason is
  kept, while duplicate pypdf header/EOF warnings are hidden unless DEBUG
  logging is selected.

The original Phase 4 (0.6.0) added no runtime dependency or database
migration, and Office formats are parsed/generated with the Python standard
library. The Phase 4.1 hotfix (0.6.1) adds only `certifi` for a portable
Mozilla CA bundle; the database remains schema v4.

## Demo data

```powershell
pc-agent demo office
```

This creates `workspace\Office Demo\sales.csv`, `sales.json`, `sales.xlsx`
and `summary.docx`. Existing files are preserved.

## Office commands

```powershell
pc-agent office inspect "Office Demo/summary.docx"
pc-agent office inspect "Office Demo/sales.xlsx" --json
pc-agent office extract "Office Demo/summary.docx"
pc-agent office extract "Office Demo/summary.docx" --output "Office Demo/summary.txt"
pc-agent office csv-inspect "Office Demo/sales.csv" --sample-rows 5
pc-agent office csv-to-json "Office Demo/sales.csv" --output "Office Demo/sales-converted.json"
pc-agent office json-to-csv "Office Demo/sales.json" --output "Office Demo/sales-roundtrip.csv"
pc-agent office csv-to-xlsx "Office Demo/sales.csv" --output "Office Demo/sales-book.xlsx" --sheet-name Sales
pc-agent office create-docx --output "Office Demo/release-note.docx" --title "Phase 4" --paragraph "First paragraph." --paragraph "Second paragraph."
```

### Format behavior

- **DOCX:** paragraphs and table-cell text are extracted; metadata, paragraph
  count, tables, words and characters are reported.
- **XLSX:** sheets are read in workbook order. Shared strings, inline strings,
  numbers, booleans and cached formula results are reported. Date/style
  formats are not interpreted, so a date cell may appear as its stored serial
  value. Formula cells are counted, but formulas are **not evaluated**.
- **PPTX:** slides are read in presentation order from
  `ppt/presentation.xml`; titles, text lines, shape and picture counts are
  reported.
- **CSV:** UTF-8/BOM text with comma, semicolon, tab or pipe delimiters.
  Header handling is detected deterministically, malformed-width rows are
  counted, and numeric columns receive min/max/mean statistics.
- **JSON conversion:** CSV values remain strings in JSON. JSON → CSV accepts
  one flat object, an array of flat objects, or an object containing a `rows`
  array. Nested values are serialized as compact JSON in their CSV cell.

### Safety limits

- OOXML packages: at most 25 MB, 2,000 ZIP entries, 100 MB total
  uncompressed data and 10 MB per parsed XML part.
- CSV/JSON-conversion inputs: at most 10 MB; CSV at most 100,000 rows and
  100 columns. XLSX parsing is capped at 100,000 rows, 256 columns and
  250,000 cells across the workbook.
- Duplicate ZIP entries, encrypted entries, `DOCTYPE` XML, macro-enabled and
  legacy binary Office formats are rejected.
- Inputs and outputs stay in the workspace. Outputs must be new files.
- Generated DOCX/XLSX packages are re-opened before the command reports
  success. JSON and CSV conversions are round-trip checked.

Phase 4 does not preserve Office styling when creating files, edit existing
workbooks/presentations in place, run macros, repair corrupt packages or
convert every possible Office feature.

## Web fetch

```powershell
pc-agent web fetch https://example.com/
pc-agent web fetch https://example.com/ --max-chars 20000
pc-agent web fetch https://example.com/ --output example.txt
pc-agent web fetch http://127.0.0.1:8765/health --allow-private
```

The command itself is the explicit network grant for that one URL. It:

- accepts only HTTP(S), with no embedded URL credentials;
- blocks loopback/private/link-local/reserved resolved addresses and
  non-standard ports by default;
- revalidates each redirect target and follows at most five redirects;
- sends no cookies, authorization headers or request body;
- accepts only text, HTML, JSON and XML responses;
- caps time and response size using `PC_AGENT_WEB_TIMEOUT_SECONDS`
  (default 15, allowed >0–60) and `PC_AGENT_WEB_MAX_BYTES`
  (default 2,000,000, allowed 1,024–10,000,000);
- strips scripts/styles from extracted HTML and lists at most 100 HTTP(S)
  links without following them;
- verifies HTTPS with a shared TLS context combining Python/OpenSSL defaults,
  Windows ROOT/CA server-auth certificates and Certifi's Mozilla bundle
  (Phase 4.1). Verification is never disabled; `doctor` prints the CA count
  and sources; and
- saves output only to a new workspace `.txt` or `.md` file.

`--allow-private` deliberately allows private/loopback addresses and
non-standard ports for the exact URL in that one direct command. Use it only
for a server you intended to contact.

This is an SSRF-reduced fetch, not a complete browser or a pinned-IP network
client. DNS is validated before the request and redirect, but the standard
library performs its own connection resolution afterwards, so a hostile DNS
rebinding race is a residual risk. JavaScript-rendered pages, login forms,
cookies, downloads, crawling, search and browser interaction are not included.

Generic tasks, `--use-ai` plans and `pc-agent tools invoke web.fetch` do not
receive the network grant. They fail closed with a message pointing to the
dedicated command.

## Desktop commands

```powershell
pc-agent desktop info
pc-agent desktop info --json
pc-agent desktop open "Office Demo/summary.docx"
pc-agent desktop open "Office Demo"
pc-agent desktop open-url https://example.com/
```

`desktop info` reports OS, machine, Python, CPU count, physical-memory totals
(when the OS exposes them) and workspace-disk usage. It does not run a shell.

The two open commands are the explicit desktop grant for one exact target.
Workspace opening accepts regular files/folders only and rejects executable,
installer, script, shortcut and similar extensions. Opening a document can
start its registered application; opening a URL uses the default browser.
Phase 4 does not click, type, read the screen, take screenshots, automate
windows or launch arbitrary programs.

## Orchestrated tasks

Only safe local Phase 4 work joins `pc-agent run`/`task run`:

- Office inspect, CSV inspect and bounded text extraction;
- CSV → JSON, JSON → CSV and CSV → XLSX;
- Desktop system information; and
- the previous PDF/file-list tools.

Examples:

```powershell
pc-agent run "Convert CSV to JSON" --target "Office Demo/sales.csv" --output "Office Demo/task-sales.json"
pc-agent run "Analyze this CSV" --target "Office Demo/sales.csv"
pc-agent run "Show system information"
```

The orchestrator re-parses/re-inspects outputs before marking a task
succeeded. Existing Phase 3 recovery can retry an orchestrated Office plan
to a fresh `*-recovered...` output or correct an input only when exactly one
matching workspace file exists. Direct Office/Web/Desktop commands remain
single-attempt.

Constrained AI planning may select Office inspection/conversions or system
info with workspace-relative paths. It may not select web fetch, desktop
opening, DOCX creation, shell access or external paths.

## Task references without placeholders

Never type `<TASK_ID>`, `<FAILED_TASK_ID>` or another angle-bracket
placeholder literally. In Windows CMD/PowerShell, `<` and `>` are redirection
operators and the shell can fail before `pc-agent` starts.

Use either the real ID printed by a command or an alias:

```powershell
pc-agent task list --status failed
pc-agent task diagnose latest-failed
pc-agent task recover latest-failed
pc-agent task show latest
pc-agent recovery show latest-failed
```

Aliases resolve to the newest matching task and the CLI prints the selected
real ID on stderr. Human-readable failed `run` reports also print exact
copy-ready diagnose/recover commands for that specific task. Since Phase 4.1,
if that failed source already has a succeeded linked recovery clone, diagnose
says so and `recover` stops instead of creating another duplicate clone.

## Not included in Phase 4

- Full browser DOM/search/form automation or authenticated browsing.
- Desktop GUI, screenshots, keyboard/mouse control or arbitrary app launch.
- Shell execution, credential access, email/messaging sends or downloads.
- Editing existing Office files in place or preserving rich formatting.
- Dynamic filesystem/network/shell/browser/desktop tools.
- Licensing, cloud coordination and production installer work.

Those remain later roadmap items and are not claimed by this build.
