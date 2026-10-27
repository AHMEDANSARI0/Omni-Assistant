# Registered tools - through Phase 5

Built-in tools are reviewed source-code handlers. Phase 2 adds approved
`custom.*` dynamic tools loaded from SQLite. Both kinds expose a stable id,
semantic version, strict input/output schemas, declared permissions and a
timeout, and both run through `ToolRegistry.invoke`, which validates
arguments before execution and validates the returned object afterwards.

## Reviewed built-ins

| Tool id | Version | Purpose | Permissions |
|---|---|---|---|
| `workspace.list_files` | 1.0.0 | Stable sorted file listing inside the workspace or an approved read root | list_files |
| `workspace.read_text` | 1.0.0 | Read a UTF-8 file (max 200,000 chars) | read_file |
| `workspace.write_text` | 1.0.0 | Create a new workspace UTF-8 text file; never overwrites | write_file |
| `files.copy` | 1.0.0 | Copy one workspace file (streamed SHA-256, exclusive create); never overwrites | read_file, write_file |
| `files.move` | 1.0.0 | Move one workspace file with before/after SHA-256 check; never overwrites | move_file, write_file |
| `files.rename` | 1.0.0 | Rename one workspace file in place (plain new name only) | move_file, write_file |
| `files.organize` | 1.0.0 | Move a folder's top-level files into category/extension subfolders; conflicts skipped + reported | list_files, move_file, write_file |
| `files.search` | 1.0.0 | Search file names and optionally bounded UTF-8 content; credential-like contents never searched | list_files, read_file |
| `pdf.inspect` | 1.0.0 | Open a PDF and report page count/size | read_file |
| `pdf.verify` | 1.0.0 | Re-open a PDF and optionally compare page count | read_file |
| `pdf.merge` | 1.0.0 | Merge an explicit ordered list into a new verified workspace PDF | read_file, write_file |
| `pdf.merge_folders` | 1.0.0 | Merge PDFs discovered in selected folders | list_files, read_file, write_file |
| `office.inspect` | 1.0.0 | Inspect a workspace DOCX/XLSX/PPTX/CSV file | read_file |
| `office.extract_text` | 1.0.0 | Extract bounded Office/CSV text, optionally to a new workspace text file | read_file, write_file |
| `office.csv_inspect` | 1.0.0 | Inspect CSV columns/statistics with a bounded sample | read_file |
| `office.csv_to_json` | 1.0.0 | Convert CSV to a new JSON array of string-valued objects | read_file, write_file |
| `office.json_to_csv` | 1.0.0 | Convert a flat JSON object/array to a new CSV | read_file, write_file |
| `office.create_docx` | 1.0.0 | Create and verify a simple new DOCX | write_file |
| `office.csv_to_xlsx` | 1.0.0 | Convert CSV to a new verified one-sheet XLSX | read_file, write_file |
| `web.fetch` | 1.0.0 | Fetch one exact HTTP(S) page as bounded text; only the direct web command or GUI Web tab supplies network authority | network_access |
| `desktop.system_info` | 1.0.0 | Report OS/CPU/memory/workspace-disk information without a shell | none |
| `desktop.open_path` | 1.0.0 | Open one workspace document/folder in its default app; direct desktop command/GUI tab context required | read_file, list_files, desktop_open |
| `desktop.open_url` | 1.0.0 | Open one exact HTTP(S) URL in the default browser; direct desktop command/GUI tab context required | desktop_open |

## Approved dynamic tools

Dynamic tools appear in `pc-agent tools list` only after approval. Their ids
start with `custom.`, permissions are always `none`, and provenance includes
the candidate key and source (`template:...`, `import:...`, provider
generation or repair).

| Template candidate | Tool id | Purpose |
|---|---|---|
| `word_count` | `custom.text_word_count` | Count words and characters |
| `slugify` | `custom.text_slugify` | Create a lowercase hyphenated slug |
| `percentage` | `custom.math_percentage` | Calculate part/total as a percentage |
| `temperature` | `custom.unit_temperature_convert` | Convert Celsius/Fahrenheit |

`pc-agent tools show <tool_id>` prints the exact runtime contract.
`pc-agent tools invoke` executes through the registry; dynamic code itself
runs in a fresh restricted Python subprocess. `tools disable`, `tools enable`
and `tools rollback` affect only the persisted approved dynamic version and
never delete its test/approval history.

See `docs/tool_builder.md` for candidate creation, validation, approval and
the non-OS-sandbox limitation.

## Safety contract

- **Workspace by default.** Relative paths resolve under the configured
  workspace. Traversal and sibling-prefix escapes are rejected by policy.
- **Exact external read exceptions.** In an orchestrated `run`, the user may
  approve specific external input files/folders. The resulting
  `ToolContext` allows only list/read below those exact resolved roots for
  that run. Direct `pc-agent pdf ...` commands do not receive this exception.
- **Policy runs at execution time.** Metadata never grants permission.
  Handlers resolve each actual target and request a fresh policy decision;
  credential-like paths (for example `.env`, `.ssh`, private keys) are denied
  even inside an approved root.
- **Link escapes are re-checked.** Listing/PDF discovery resolves results and
  omits entries whose real path leaves the selected directory.
- **No overwrite, no delete.** Writers use exclusive creation or reject an
  existing output. There is deliberately no delete or shell tool.
- **Sources are preserved.** PDF merge opens inputs read-only, writes a
  temporary workspace file, verifies it, and only then moves it in place.
  Generated DOCX/XLSX packages are likewise verified from a temporary file;
  CSV/JSON conversions round-trip their generated bytes before writing.
- **Verification is deterministic.** A merged PDF is re-opened and its page
  count must equal the sum of validated inputs. In orchestrated runs the
  final file is verified again before the task becomes `succeeded`; Office
  conversions are re-parsed and hash/row/sheet counts are compared.
- **Recovery is an orchestrator boundary, not hidden tool magic.** Recovery
  may diagnose an orchestrated tool/verification failure and retry the same
  registry tool with a fresh workspace output or a uniquely corrected
  workspace input. Tool handlers themselves still never overwrite, delete or
  silently substitute paths, and direct `pdf`/`office`/`web`/`desktop`/`tools`
  commands remain single-attempt. See `docs/recovery.md`.
- **Bad inputs are explicit.** Explicit merge fails on corrupt/encrypted
  input. Folder merge reports each skipped corrupt/encrypted file with a
  reason and fails if fewer than two valid PDFs remain.
- **Merge order:** folders in the order given, then case-insensitive path
  order inside each folder; non-recursive unless requested.
- **Encrypted/password PDFs and encrypted/macro Office packages are not
  supported.**
- **Office parsing is bounded.** DOCX/XLSX/PPTX packages are capped at 25 MB
  with ZIP-entry/decompressed/XML-part limits; `DOCTYPE`, duplicate entries
  and legacy binary/macro formats are rejected before handlers report data.
- **Network is a context grant, not tool metadata.** Only
  `pc-agent web fetch https://example.com/` and the GUI Web tab's exact
  **Fetch Page** action construct a context with network access, plus
  private-network access only after the direct private/local opt-in. The
  same `web.fetch` tool invoked generically fails policy. Web fetch validates
  resolved IPs/redirects and caps response type, bytes and time.
- **Desktop opening is also context-gated.** Only `pc-agent desktop open` or
  `open-url`, plus the GUI Desktop tab's confirmed exact open actions, grant
  that operation. Workspace targets are limited to files/folders and
  executable/script/installer/shortcut extensions are rejected. No handler
  exposes shell, screenshots or external-app GUI input.

## Orchestration allow-list

Natural-language execution may use the five Phase 1C PDF/list tools plus
`office.inspect`, `office.extract_text`, `office.csv_inspect`,
`office.csv_to_json`, `office.json_to_csv`, `office.csv_to_xlsx`, and
`desktop.system_info`. Phase 8 adds deterministic-only `files.copy`,
`files.move`, `files.rename`, `files.organize` and `files.search` (AI
plans for them are rejected). AI planning has a smaller Office subset
(inspection and conversions, not extraction or DOCX creation). `web.fetch`,
`desktop.open_path`, `desktop.open_url`, `workspace.read_text` and
`workspace.write_text` are never orchestrated generic-task choices.

## Known limitations

- Built-in timeouts are declared and measured, but synchronous in-process
  built-in handlers cannot be hard-killed at the deadline. Dynamic tools run
  in a subprocess with a hard timeout, although this build has no hard memory
  or OS/VM confinement guarantee.
- Dynamic registration is limited to approved Python pure-compute candidates.
  There are no dynamic filesystem, network, shell, browser or desktop tools.
- Office creation is intentionally simple: styles/formulas/charts embedded
  in source documents are not reproduced, formulas are not evaluated, and
  existing Office files are not edited in place.
- Web fetch is SSRF-reduced but not a pinned-IP client, so hostile DNS
  rebinding remains a residual risk; it is also not a browser or crawler.
- External approval covers exact read/list roots only; it is not a reusable
  folder allow-list and does not authorize external writes.
