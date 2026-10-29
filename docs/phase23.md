# Phase 23: PDF Table Extraction

Phase 23 adds `pdf.extract_tables`, a deterministic local tool for
born-digital PDFs whose table text is embedded in the file. It uses PDFium
character boxes to reconstruct words, lines and column-like cells, then
writes a new verified JSON file—or a CSV file when exactly one table is
detected. The source PDF is never modified and an existing output is never
overwritten.

This phase does not perform OCR. If a scanned PDF needs text first, use
Phase 22 `pdf.ocr` and review its transcription; `pdf.extract_tables` only
reads embedded text geometry.

## How detection works

For each selected page, the extractor:

1. reads embedded characters and their PDFium bounding boxes;
2. groups characters into visual lines and words;
3. splits a line into candidate cells at larger horizontal gaps;
4. groups nearby multi-cell lines into a possible table;
5. clusters recurring cell x-ranges into columns;
6. pads missing cells with empty strings; and
7. records the page, per-page table index, rows, columns, cell count,
   character count and bounding box.

JSON output uses method `pdf-tables-v1` and contains every detected table's
rows. CSV is intentionally stricter: it succeeds only when exactly one
table is detected, so a multi-table document is not silently flattened
into a misleading single CSV. Use JSON for zero or multiple tables.

## Bounds

- Maximum 100 selected pages per run.
- Maximum 200 detected tables.
- Maximum 1,000 rows per table and 10,000 detected rows total.
- Maximum 50 columns and 100,000 detected cells.
- Maximum 2,000,000 embedded PDF characters examined.
- Maximum 10,000 characters in one reconstructed cell.
- `min_rows` default: 2 (allowed 1-1000).
- `min_columns` default: 2 (allowed 1-50).
- Encrypted/password-protected PDFs are rejected.

An image-only PDF can validly produce JSON with `has_embedded_text=false`
and `table_count=0`. That is an honest no-table result, not an OCR attempt.

## Verification and recovery

The tool writes atomically and verifies the output bytes/SHA-256. In an
orchestrated task, verification independently reruns the embedded-text
geometry extraction and compares the complete rendered JSON/CSV, page
selection, table summaries, totals, minimum thresholds, size and hash.

Because this is a local deterministic writer with a surviving source, an
output collision may be recovered once to a fresh output under the existing
bounded recovery rules. The original collision file is preserved.

AI planning cannot propose `pdf.extract_tables`; this writer is available
through the deterministic planner, direct CLI and GUI only.

## Direct CLI

JSON output (Windows PowerShell, run commands one at a time):

```powershell
pc-agent pdf extract-tables report.pdf --output report-tables.json --pages 1-3
```

CSV for a page/selection where exactly one table is expected:

```powershell
pc-agent pdf extract-tables report.pdf --output report-table.csv --pages 1
```

Optional thresholds:

```powershell
pc-agent pdf extract-tables report.pdf --output report-tables.json --min-rows 3 --min-columns 3
```

Add `--json` when the direct command's own result should be machine-readable.

## Orchestrated task

The deterministic planner recognizes table-extraction wording. Exact page,
output and threshold values stay in execution options:

```powershell
pc-agent run "Extract tables from report.pdf" --target report.pdf --pages 1 --output report-tables.json --pdf-table-min-rows 3 --pdf-table-min-columns 3
```

## GUI

Documents > PDF gains **Extract Tables** plus minimum-row and
minimum-column fields. Input, page selection and output use the existing
Documents fields. Choose a `.json` output for multiple tables, or `.csv`
only for a selection expected to contain exactly one table.

## Limitations

Text-geometry extraction is useful for ordinary born-digital row/column
text, but it is not a complete layout engine. Review important output for:

- merged or spanning cells;
- multi-line text inside one visual cell;
- dense columns with very small gaps;
- borderless tables whose spacing resembles normal paragraphs;
- rotated or vertical text;
- tables drawn only as scanned images;
- numbers or labels intentionally positioned across column boundaries.

The extractor reports no table rather than inventing one when its minimum
row/column evidence is absent.

## Scope

- Version `0.25.0`
- Database schema remains v6
- Built-in tools: 60
- No new runtime dependency; Phase 22's `pypdfium2` is reused

## Remaining work after Phase 23

- Signed Windows installer/Authenticode and online auto-update.
- macOS/Linux packaging, Keychain/Secret Service credentials and desktop
  automation parity.
- PDF password, form and digital-signature workflows.
- Live Anthropic verification only after a user key exists.
- Optional removal of `.env` provider-key lines so Windows Credential
  Manager becomes the effective source.
- Live execution evidence for `install_windows.ps1`,
  `uninstall_windows.ps1` and the `.cmd` launchers.
