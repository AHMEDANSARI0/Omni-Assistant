# Phase 25: PDF Form Workflows

Phase 25 adds deterministic inspection and filling for interactive fields in
an **existing AcroForm**. It does not infer fields from page text or pixels,
create a form design, OCR a scanned form, accept a PDF password, flatten a
form, or create/verify a digital signature. It does not sign a PDF.

## Tools

Phase 25 adds two built-ins, for **62 tools** total:

- `pdf.inspect_form` reports each AcroForm field's qualified name, type,
  current value, choice options, button states, required/read-only state,
  text/button/choice flags, maximum length, alternate name, first widget
  page and widget count.
- `pdf.fill_form` fills named text, choice, checkbox and radio/button fields
  into a new PDF. It **never overwrites** an output and never modifies the
  source.

Supported values are exact field names with:

- a string for text and single-choice fields;
- a list of strings for multi-select fields;
- `true` or `false` for a one-state checkbox;
- an exact state name, or null/`Off`, for radio and other multi-state
  buttons.

Read-only fields, password-flagged text fields, signature fields,
pushbuttons and unknown field types are reported by inspection but refused
by filling, so a secret is not entered through recorded tool arguments.
Unknown names, duplicate
assignments, invalid choices/states, values over a field's maximum length,
and clearing a required field are also refused.

## Bounds and verification

- At most 500 pages and 200 form fields are inspected.
- At most 200 supplied values, 10,000 characters per value and 100,000 total
  supplied characters are accepted.
- At most 1,000 options per choice field are reported.
- Encrypted PDFs are refused; no password is accepted through a tool, task,
  CLI field value or GUI field in this phase.
- Filling writes to a temporary PDF first. The source hash is compared
  before publication, and the temporary output is independently reopened to
  compare page count, field names, every changed value and every untouched
  value.
- Orchestrated tasks repeat source/output AcroForm inspection, compare the
  planned normalized values and output SHA-256, and require all normal
  acceptance criteria to pass.
- If the requested output already exists, output-collision recovery can use
  a fresh output while preserving the existing file.

The writer asks PDF readers to regenerate field appearances. A field whose
widget/appearance structure cannot be updated and verified is rejected
rather than reported as filled.

## Direct CLI

Inspect a workspace PDF:

```powershell
pc-agent pdf inspect-form form.pdf
```

Fill exact fields into a new workspace PDF:

```powershell
pc-agent pdf fill-form form.pdf --output filled.pdf --field FullName=Ali --field Agree=true
```

For a multi-select field in the direct CLI, separate chosen options with
`|`, for example `--field Interests=Reading|Music`. Both direct commands
support `--json`.

## Deterministic tasks

Exact values stay option-driven and are never guessed from prose:

```powershell
pc-agent run "Fill the PDF form form.pdf" --target form.pdf --output filled.pdf --form-field FullName=Ali
```

`--form-field` is repeatable. Inspection uses an instruction such as
“Inspect the PDF form fields in form.pdf”. AI planning cannot propose either
Phase 25 form tool.

## GUI

The Documents > PDF panel adds **Inspect Form** and **Fill Form** buttons and
a form-values box using one `Name=Value` line per field. Checkbox values use
`true`/`false`; multi-select values use `|` between options. The existing
Input file is the source PDF and Output file is the new filled PDF.

## Limitations

- Only AcroForm fields already present in the PDF are supported. XFA-only
  forms, scanned paper forms and visually drawn boxes are not converted into
  interactive fields.
- The phase does not add/remove/reorder fields, change field design, flatten
  a completed form, submit a form, run JavaScript/actions, or evaluate
  calculation scripts.
- Complex appearance behavior still depends on the PDF reader regenerating
  appearances. Review legally or financially important completed forms in a
  PDF reader before relying on them.
- PDF password entry/removal/encryption and digital-signature creation or
  validation remain separate future work.
- Version `0.27.0`; database schema stays v6; no new runtime dependency.

## Remaining work after Phase 25

- Secure direct-only PDF password workflows and digital-signature creation/
  validation remain separate.
- A signed Windows installer/Authenticode flow remains future work.
- A real public online-update feed still needs vendor HTTPS hosting and the
  matching release public key configured locally.
- macOS/Linux packaging, Keychain/Secret Service credentials and desktop
  automation parity remain platform work.
- Live Anthropic verification still needs a user-provided Anthropic key.
