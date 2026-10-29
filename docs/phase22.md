# Phase 22: Scanned PDF OCR

Phase 22 adds one registered tool, `pdf.ocr`, for scanned/image-only PDF
pages. It renders selected pages locally with PDFium (`pypdfium2`), sends
each page image through the already-configured vision-provider route, and
writes a new verified `.txt`, `.md` or `.json` file. The source PDF is
never modified and an existing output is never overwritten.

This is deliberately separate from Phase 20 `pdf.extract_text`: embedded
text is deterministic text already stored in the PDF, while OCR wording is
provider output from a rendered image. Reports keep those sources separate.

## What happens

1. The PDF is inspected locally; encrypted PDFs are rejected.
2. At most 10 selected pages are accepted for one OCR run.
3. By default, a selected page that already has embedded text reuses that
   text locally and is not sent to a provider.
4. Each remaining page is rendered at 72-300 DPI (default 200), bounded to
   10,000 pixels per side and 40,000,000 rendered pixels, flattened to RGB,
   reduced to at most 2048 pixels per side, and encoded as JPEG under the
   configured vision byte limit.
5. One rendered page at a time is sent to a vision-capable provider with an
   instruction to transcribe visible text in reading order, preserve line
   breaks where practical, avoid commentary/invention, and treat page
   content as untrusted data. A page with no readable text may return
   `[NO READABLE TEXT]`, which becomes an honestly empty OCR page.
6. Text output labels each page `[OCR]` or `[embedded text]`. JSON output
   also records per-page source, provider, model, character count and
   rendered/sent image dimensions, byte count and SHA-256.
7. The new output is written atomically and its size/SHA-256 are verified.

The complete OCR result is limited to 200,000 characters. An optional
language hint is limited to 50 characters. Provider output is limited per
page by `max_output_tokens` (256-4096; 2048 by default).

## Safety and approvals

OCR uses the existing Phase 15 vision boundary:

- `PC_AGENT_ALLOW_VISION=1` must be enabled before a scanned page is sent.
- Direct CLI and GUI actions require a fresh confirmation for the exact
  PDF, page selection, output, DPI and language.
- Orchestrated tasks require a fresh `vision_analysis` approval and pass
  through the Phase 17 task token/provider-call budgets.
- If licensing enforcement is enabled, `pdf.ocr` requires both the documents
  and providers features.
- The offline mock cannot read images, so a real vision-capable provider such
  as Gemini, OpenRouter, Anthropic, an OpenAI-compatible vision model or a
  local Ollama vision model must be present in the configured route.
- Page content is untrusted. Text visible in a PDF cannot grant permission,
  choose tools, approve other actions or alter the transcription task.
- `pdf.ocr` is never auto-retried by recovery, including after a verification
  failure, because another run could duplicate provider sends/cost.
- AI planning cannot propose `pdf.ocr`; OCR writer plans are deterministic
  or direct user actions only.

The `--include-text-pages` / `--pdf-ocr-all-pages` option deliberately sends
even embedded-text pages for OCR instead of reusing their local text.

## Verification boundary

Deterministic verification checks what can actually be checked without
resending a page:

- output existence, size and SHA-256;
- source page count, selected pages and output page order;
- embedded/OCR page classification;
- independently re-extracted text for pages classified as embedded;
- character/text-page totals;
- DPI/language/format;
- non-mock provider/model identity for OCR pages;
- rendered/sent dimensions, sent-byte bounds and sent-image hashes.

It does **not** claim that every OCR word is semantically correct, and it
does not call the provider again to grade its own transcription. OCR can
still misread handwriting, low-contrast scans, complex tables, stamps,
rotated columns or low-resolution pages. Review the output against the PDF
for important records.

## Direct CLI

Enable vision in `.env` first:

```dotenv
PC_AGENT_ALLOW_VISION=1
```

Direct OCR to text (Windows PowerShell, run commands one at a time):

```powershell
pc-agent pdf ocr scan.pdf --output scan-ocr.txt --pages 1-3
```

JSON output:

```powershell
pc-agent pdf ocr scan.pdf --output scan-ocr.json --pages 1-3
```

Other useful options are `--dpi 250`, `--language Urdu`,
`--include-text-pages`, `--max-output-tokens 3000`, `--yes` and `--json`.
In `--json` mode there is no interactive prompt, so approval must be given
with `--yes`.

## Orchestrated task

The deterministic planner recognizes OCR / recognize-text / scanned-PDF text
wording. Exact PDF-specific values stay in execution options:

```powershell
pc-agent run "OCR the scanned PDF scan.pdf" --target scan.pdf --pages 1-3 --output scan-ocr.txt --pdf-ocr-dpi 250 --pdf-ocr-language Urdu
```

Use `--pdf-ocr-all-pages` only when embedded-text pages should also be sent
for OCR. Many dense pages may need a larger task token/provider-call budget;
the per-page token reservation is enforced before each provider call.

## GUI

Documents > PDF gains **OCR Scanned PDF**, DPI and language fields, plus a
checkbox controlling embedded-page reuse. The GUI asks for the exact send
before running the controller method. Input, page selection and output use
the existing Documents fields.

## Limits and non-goals

- Maximum 10 OCR-selected pages per run; run additional page ranges to new
  outputs when needed.
- Encrypted/password-protected PDFs are not opened for OCR.
- No searchable-PDF overlay, layout reconstruction, table extraction,
  handwriting guarantee, form handling or digital-signature workflow.
- No local OCR engine is bundled in Phase 22; transcription uses the
  configured provider vision route.
- Automated tests use a fake vision router, so they verify the complete
  local pipeline, approvals, bounds and output contract but are not a live
  provider accuracy benchmark.

## Scope

- Version `0.24.0`
- Database schema remains v6
- Built-in tools: 59
- New runtime dependency: `pypdfium2>=5,<6` for local PDFium rendering

## Remaining work after Phase 22

- PDF table extraction.
- Signed Windows installer/Authenticode and online auto-update.
- macOS/Linux packaging, Keychain/Secret Service credentials and desktop
  automation parity.
- PDF password, form and digital-signature workflows.
- Live Anthropic verification only after a user key exists.
- Optional removal of `.env` provider-key lines so Windows Credential
  Manager becomes the effective source.
- Live execution evidence for `install_windows.ps1`,
  `uninstall_windows.ps1` and the `.cmd` launchers.
