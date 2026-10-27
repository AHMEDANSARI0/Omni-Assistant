# Phase 9 Deeper Office (0.11.0, schema v6 unchanged)

Phase 9 implements the "deeper Office" roadmap item: **PDF split/extract,
DOCX editing, and XLSX cell writing with deterministic formula
calculation and verification**. Everything stays standard-library only,
workspace-confined, and no-overwrite. No new dependency, no database
migration.

## New tools (29 built-ins total)

| Tool | Purpose | Verification |
|---|---|---|
| `pdf.extract_pages` | Extract pages like `1-3,5` (order preserved, duplicates rejected) into a new PDF | Re-opened with expected page count; SHA-256 + page-count acceptance criteria |
| `pdf.split` | One single-page PDF per page in a folder (max 200 pages) | Every page PDF re-opened and verified; count matched against the source; partial outputs cleaned up on failure |
| `office.docx_replace_text` | Replace exact paragraph text — safe across Word run boundaries | Source/output text re-read and occurrences counted independently |
| `office.docx_append_paragraphs` | Append paragraphs to the document body | Every appended paragraph found in the re-read output |
| `office.xlsx_set_cells` | Set values or `=formulas` in one sheet of an existing workbook | Cells + formulas re-read from the written file; every formula recomputed and compared |
| `office.xlsx_verify_formulas` | Recompute every formula in a workbook | Independent evaluator; unsupported formulas are reported with the exact reason, never guessed |

### Formula calculation

A bounded recursive-descent evaluator (no `eval`) supports cell references,
ranges, `+ - * /`, parentheses, unary minus, and the functions `SUM`,
`AVERAGE`, `MIN`, `MAX`, `COUNT`, `PRODUCT`, `ABS`, `ROUND`. Aggregates
ignore text like Excel, division by zero yields `#DIV/0!`, circular
references and unsupported functions (e.g. `VLOOKUP`) are explicit errors.
When a formula computes, `office.xlsx_set_cells` also writes the cached
value into the file so other readers see the result immediately; stale
`calcChain.xml` is dropped and LibreOffice/Excel are asked to recalculate
on load.

## Interfaces

Direct CLI:

```cmd
pc-agent pdf extract book.pdf --pages "1-3,5" --output part.pdf
pc-agent pdf split book.pdf --output-folder pages
pc-agent office docx-replace doc.docx --output edited.docx --find "draft" --replace "final"
pc-agent office docx-append doc.docx --output edited.docx --paragraph "Appendix line"
pc-agent office xlsx-set data.xlsx --output edited.xlsx --cell B4==SUM(B2:B3)
pc-agent office xlsx-formulas edited.xlsx
```

Orchestrated tasks (checkpoints, recovery, acceptance criteria included):

```cmd
pc-agent run "Extract pages 1-2 from merged.pdf"
pc-agent run "Split book.pdf"
pc-agent run "Replace text in the document" --target doc.docx --find "draft" --replace "final"
pc-agent run "Set the cell" --target data.xlsx --cell A3 --value "=SUM(A1:A2)"
pc-agent run "Verify formulas" --target edited.xlsx
```

New run options: `--pages`, `--find`, `--replace`, `--cell`, `--value`
(quoted instruction text also works where unambiguous). Deeper-office
intents are deterministic-only, like Phase 8 file operations.

GUI: the Documents tab gains Extract Pages / Split PDF (PDF panel), DOCX
find/replace + XLSX cell set/verify (Office panel), and Append Paragraphs
(Create DOCX panel), all through the same controller services.

## Recovery and criteria integration

- `pdf.extract_pages` and the three Office editors write a brand-new
  `output_path` from a surviving source, so destination collisions and
  verification failures reuse the existing bounded fresh-output recovery,
  and transfers get the full artifact + independent SHA-256 criteria.
- `pdf.split` checkpoints its reported outputs; resume re-verifies every
  produced page.

## Honest boundaries

- DOCX replace works per paragraph (including text split across runs);
  matches spanning two paragraphs, headers/footers, text boxes and fields
  are out of scope. Replaced paragraphs keep the first run's formatting.
- XLSX editing rewrites only the target sheet's XML (other parts preserved
  byte-for-byte); it does not create charts/pivots/styles, and the formula
  evaluator intentionally covers the common arithmetic/aggregate subset —
  anything else is reported as unsupported instead of being mis-computed.
- PDF page reordering beyond selection order, rotation and text extraction
  from PDFs remain future work.
