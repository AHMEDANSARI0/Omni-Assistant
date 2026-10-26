# Registered tools - through Phase 1C

All tools are reviewed built-ins with a stable id, semantic version, strict
input/output schemas, declared permissions and a declared timeout. They run
through `ToolRegistry.invoke`, which validates arguments before execution and
validates the returned object afterwards.

| Tool id | Version | Purpose | Permissions |
|---|---|---|---|
| `workspace.list_files` | 1.0.0 | Stable sorted file listing inside the workspace or an approved read root | list_files |
| `workspace.read_text` | 1.0.0 | Read a UTF-8 file (max 200,000 chars) | read_file |
| `workspace.write_text` | 1.0.0 | Create a new workspace UTF-8 text file; never overwrites | write_file |
| `pdf.inspect` | 1.0.0 | Open a PDF and report page count/size | read_file |
| `pdf.verify` | 1.0.0 | Re-open a PDF and optionally compare page count | read_file |
| `pdf.merge` | 1.0.0 | Merge an explicit ordered list into a new verified workspace PDF | read_file, write_file |
| `pdf.merge_folders` | 1.0.0 | Merge PDFs discovered in selected folders | list_files, read_file, write_file |

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
- **Verification is deterministic.** A merged PDF is re-opened and its page
  count must equal the sum of validated inputs. In orchestrated runs the
  final file is verified again before the task becomes `succeeded`.
- **Bad inputs are explicit.** Explicit merge fails on corrupt/encrypted
  input. Folder merge reports each skipped corrupt/encrypted file with a
  reason and fails if fewer than two valid PDFs remain.
- **Merge order:** folders in the order given, then case-insensitive path
  order inside each folder; non-recursive unless requested.
- **Encrypted/password PDFs are not supported yet.**
- **Network:** these local tools do not use the network. Provider calls live
  outside the tool registry and cannot invoke tools directly.

## Orchestration allow-list

Phase 1C natural-language execution may use only `pdf.merge_folders`,
`pdf.merge`, `pdf.verify`, `pdf.inspect`, and `workspace.list_files`.
`workspace.read_text` and `workspace.write_text` remain direct reviewed
tools for future workflows, but an AI plan cannot select them yet.

## Known limitations

- Tool timeouts are declared and measured, but synchronous in-process
  handlers cannot be hard-killed at the deadline. Hard isolation is Phase 2.
- Built-in definitions live in reviewed source code. Persisted/dynamic tool
  registration, candidate testing and approval are Phase 2.
- External approval covers exact read/list roots only; it is not a reusable
  folder allow-list and does not authorize external writes.
