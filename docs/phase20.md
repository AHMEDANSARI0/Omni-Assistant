# Phase 20: PDF Text + Page Transforms

Phase 20 completes the PDF work that Phase 9 explicitly left for later:
embedded-text extraction, embedded-text search, full page reordering and
quarter-turn page rotation. It adds four built-in tools, taking the total
from **54 to 58**. The database schema remains **v6** and no runtime
dependency is added; the existing `pypdf` dependency does the PDF work.

## New tools

| Tool | Purpose | Writes |
|---|---|---|
| `pdf.extract_text` | Extract embedded text from all or selected pages to `.txt`, `.md` or `.json` | New file only |
| `pdf.search_text` | Find one exact phrase in embedded text and report pages/snippets | No |
| `pdf.reorder_pages` | Write every source page exactly once in a new order, including `reverse` | New PDF only |
| `pdf.rotate_pages` | Rotate selected pages by -270, -180, -90, 90, 180 or 270 degrees | New PDF only |

These tools extract **embedded PDF text only**. They do not perform OCR.
A scanned/image-only PDF honestly reports `has_embedded_text=false` and a
search can succeed with zero occurrences; the application never pretends a
scanned page was read.

## Safety and bounds

- Sources and outputs stay on the existing workspace/approved-read-root
  policy rails. Source PDFs are opened read-only and never modified.
- Every writer refuses to overwrite an existing output. Orchestrated tasks
  can heal an output collision to a fresh `-recovered` file.
- Text extraction/search are capped at **500 selected pages** and
  **5,000,000 extracted characters**. Search queries are capped at 200
  characters and results at 200 snippets.
- Page transforms are capped at **500 pages** so every output page can be
  fingerprinted and verified deterministically.
- Encrypted PDFs remain unsupported without a password workflow.
- The deterministic planner handles all four operations. AI planning may
  propose only read-only `pdf.search_text` from this phase; PDF text/page
  writers stay deterministic/direct like the Phase 9 PDF editors.

Text output uses stable `--- Page N ---` markers. JSON output contains the
source page count plus one object per extracted page with its page number,
text and character count. Search normalizes line breaks/whitespace before
matching, so a phrase split across extracted lines can still be found.
Snippets are short excerpts from that normalized text.

Reorder and rotate verification is more than a page-count check: each page
is fingerprinted from its page box, extracted text and decoded content
stream, and the output sequence/rotations must match the exact requested
transform. The orchestrator independently repeats those checks before a
task can succeed.

## Direct CLI

Run each command separately:

```powershell
pc-agent pdf extract-text report.pdf --output report.txt
```

```powershell
pc-agent pdf search report.pdf "Invoice Total"
```

```powershell
pc-agent pdf reorder report.pdf --pages "3,1,2" --output reordered.pdf
```

```powershell
pc-agent pdf rotate report.pdf --degrees 90 --pages "2" --output rotated.pdf
```

The same operations are available through deterministic `pc-agent run`
instructions (with `--target`, `--output`, `--pages`, `--find` and
`--pdf-rotate` where exact values are needed) and from the GUI Documents
tab's PDF panel.

## Honest boundaries

- No OCR, scanned-document recognition or PDF table reconstruction.
- Extracted text order/layout is whatever the PDF's embedded text operators
  and `pypdf` extraction produce; it is not a visual layout engine.
- Reordering requires every page exactly once. Use `pdf.extract_pages`
  when the goal is a subset.
- Rotation changes each selected page's `/Rotate` value by a quarter-turn;
  it does not rasterize, crop or rewrite page content.
- Password-protected PDFs, digital-signature preservation/editing and
  form-filling remain unsupported.

## Remaining work after Phase 20

- A signed Windows installer, publisher code signing and online auto-update
  are not implemented; verified distribution remains editable source plus
  the deterministic portable ZIP.
- macOS/Linux packaging, Keychain/Secret Service credentials and desktop
  automation parity are not implemented.
- A live Anthropic call still needs a user-supplied Anthropic key/account;
  mocked transport and local HTTP integration are verified.
- `install_windows.ps1`, `uninstall_windows.ps1` and the `.cmd` launchers
  still warrant one live Windows execution. `build_release.ps1` was
  live-run successfully under Phase 18.
- OCR/table extraction for scanned PDFs would need a separately designed
  local engine or an approved provider-vision workflow; it is not implied
  by embedded-text extraction.
- Commercial SaaS, subscriptions, online billing and multi-tenancy remain
  outside the personal-use scope unless explicitly requested later.
