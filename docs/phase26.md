# Phase 26: PDF Password Security

Phase 26 adds direct-only password workflows for workspace PDFs: inspect
encryption status, write a new AES-256 protected copy, change passwords by
protecting an already encrypted source, and write a new unencrypted copy
after the current password is verified.

There is **no registered tool** for a PDF password. The built-in tool count
stays **62**, and no deterministic or AI planner can request, receive or use
one. Passwords are not command-line arguments, environment settings, task
options, GUI form fields, database records or result fields.

## Password entry

Every password is collected through a **hidden prompt**:

- the CLI uses Python's hidden terminal password prompt;
- the GUI uses masked password dialogs;
- new user and owner passwords are entered twice for confirmation;
- cancelling a prompt stops the operation before an output is published.

Python strings cannot promise perfect memory erasure, so Phase 26 does not
claim secure-memory wiping. It does keep passwords out of process arguments,
shell history created by this application, persisted task/tool records,
JSON/text results and error messages produced by the security core.

## Security inspection

`pc-agent pdf security report.pdf` reports whether the PDF is encrypted,
its page count when unlocked, the detected Standard-handler algorithm/key
size/version/revision, declared user permissions, file size and SHA-256.

Adding `--unlock` prompts hiddenly for the current password and reports
only whether it was accepted and whether it was the user or owner password.
A wrong password returns a non-zero status and does not print the password.

## Protect and change passwords

`pc-agent pdf protect` clones up to 500 pages into a new PDF encrypted with
**AES-256**. It asks for:

1. the current password first when the source is already encrypted;
2. a new user password twice; and
3. a different new owner password twice.

New passwords need at least 8 characters and at most 127 UTF-8 bytes. The
default user permissions are `print`, `extract` and `fill_forms`.
Repeatable direct-CLI `--permission` options can select `print`, `modify`,
`extract`, `annotate`, `fill_forms`, `accessibility`, `assemble` and
`print_high_quality`. PDF permissions are reader-enforced restrictions, not
a substitute for the opening password or access control to the file itself.

The temporary output is verified locked without a password, unlocked with
both new passwords, rejected with a generated wrong password, checked for
AES-256 and the requested permissions, and checked for permission
integrity before publication.

## Unprotect

`pc-agent pdf unprotect` prompts once for the current user or owner
password, including an empty user password for restrictions-only PDFs. It
writes a new unencrypted copy only after the temporary copy is reopened as
unlocked with the same page count.

## Safety boundaries

- The operation **never overwrites** an output and never modifies the
  source; source SHA-256 is compared before publication.
- Source and output paths must be PDFs inside the workspace.
- PDFs containing an actual digital-signature value or DocMDP permission
  signature are refused because cloning/re-encrypting can invalidate
  signature evidence. A blank, unsigned signature field is not by itself a
  digital signature.
- Forgotten-password recovery, password guessing/cracking and bypassing a
  password the user does not know are not included.
- Existing PDF tools still reject encrypted inputs. Unprotect a copy first,
  run the existing tool on that copy, then protect its output if required.
- Phase 26 does not create, inspect or validate digital signatures, add a
  certificate workflow, submit a form, or call an AI provider.
- `pypdf[crypto]` supplies the AES implementation. Database schema stays
  v6; version is 0.28.0.

## Examples

Inspect encryption metadata:

```powershell
pc-agent pdf security report.pdf
```

Test the current password:

```powershell
pc-agent pdf security report.pdf --unlock
```

Create a protected copy with custom allowed permissions:

```powershell
pc-agent pdf protect report.pdf --output report-locked.pdf --permission print --permission fill_forms
```

Create an unencrypted copy after entering the current password:

```powershell
pc-agent pdf unprotect report-locked.pdf --output report-open.pdf
```

## GUI

Documents > PDF adds **PDF Security**, **Protect PDF** and **Unprotect PDF**.
The GUI masks every password dialog. Protect uses the default
print/extract/fill-forms permissions; custom permission selection is a
direct-CLI option in this phase.

## Remaining work after Phase 26

- PDF digital-signature creation and standards-based validation remain
  separate certificate workflows.
- A signed Windows installer/Authenticode flow remains future work.
- A real public online-update feed still needs vendor HTTPS hosting and the
  matching release public key configured locally.
- macOS/Linux packaging, Keychain/Secret Service credentials and desktop
  automation parity remain platform work.
- Live Anthropic verification still needs a user-provided Anthropic key.
