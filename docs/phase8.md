# Phase 8 File Operations (0.10.0, schema v6 unchanged)

Phase 8 adds the general workspace file operations from the master-prompt
roadmap: **copy, move, rename, organize, search**. It is deliberately the
smallest remaining roadmap item that can be shipped complete in one patch —
one new tool module, one new policy action, no new dependencies, and no
database migration.

## New tools (23 built-ins total)

| Tool | Purpose | Safety |
|---|---|---|
| `files.copy` | Copy one file to a new workspace path | Streamed copy with SHA-256, exclusive create, partial copy removed on failure, never overwrites |
| `files.move` | Move one file to a new workspace path | Before/after SHA-256 must match; new policy action `MOVE_FILE` (write-like, not a delete) |
| `files.rename` | Rename one file in place | Plain file name only — path separators rejected |
| `files.organize` | Move a folder's top-level files into `images/documents/data/audio/video/archives/code/other` (or by extension) | Conflicts skipped + reported, never overwritten; bounded by `max_files` (default 500, max 2000) |
| `files.search` | Search file names (substring or wildcard) and optionally UTF-8 text content | Max 5,000 files scanned, content only for text-like files ≤ 1 MB, credential-like file contents never searched or shown |

All five are workspace-confined, re-resolve every path against live policy
at execution time, reject symlink/junction escapes, and handle **single
files** (a folder passed to copy/move/rename is a clear error pointing at
`files.organize`). There is intentionally **no delete tool**.

## Interfaces

Direct CLI:

```cmd
pc-agent files copy a.txt "Archive/a.txt"
pc-agent files move "Archive/a.txt" "a-final.txt"
pc-agent files rename "a-final.txt" "a.txt"
pc-agent files organize . --by category
pc-agent files search budget --content
```

Orchestrated tasks (with approvals, checkpoints, recovery and acceptance
criteria like every other tool):

```cmd
pc-agent run "Copy the file" --target "report.txt" --output "Archive/report.txt"
pc-agent run "Move the file" --target "report.txt" --output "Done/report.txt"
pc-agent run "Rename the file" --target "report.txt" --output "final.txt"
pc-agent run "Organize this folder by extension"
pc-agent run "Search files containing \"budget\""
```

The deterministic planner recognizes copy/duplicate, move, rename,
organize/tidy and search verbs; missing source/destination/query becomes a
clear `needs_input` prompt. File intents are planning-deterministic only in
this phase — AI plans for them are rejected by the existing AI allow-list.

The GUI Documents tab gains a **Files** panel (Copy/Move/Rename with
confirmation, Organize, Search) driving the same controller services.

## Verification and recovery integration

- `files.copy/move/rename` get the full Phase 7 criteria: independent
  artifact + recomputed SHA-256, plus legacy verification that the copy's
  source is unchanged or the move/rename source path is gone. The artifact
  criterion now also accepts an honestly reported 0-byte file (its hash is
  still recomputed), so copying an empty file verifies correctly.
- `files.organize` verification re-checks every moved destination and that
  no source remains; `files.search` verification re-checks match paths.
- A copy destination collision uses the existing bounded recovery: the
  preserved output is reported and the copy retries to a fresh
  `-recovered` name. Move/rename collisions fail cleanly instead (their
  source may already be gone after a partial attempt, so an automatic
  fresh-output retry would be unsafe).
- Successful steps checkpoint like any other tool, so interrupted file
  tasks can `task resume` after re-verification.

## Honest boundaries

- No delete, no overwrite modes, no folder copy/move, no external
  (outside-workspace) sources or destinations, no bulk/glob copy-move, no
  file-content editing here (DOCX/XLSX/PDF editing is a later phase).
- Search content snippets are first-matching-line only and stay inside the
  local audit records like other tool outputs.
