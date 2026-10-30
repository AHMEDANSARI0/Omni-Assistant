# Self-Healing PC Agent (working title)

A Windows-first AI PC agent being built in verified phases. The goal is a
secure, lightweight, extensible agent that plans and executes user tasks with
explicit permissions, verifies results deterministically, and reports honestly
what it did.

## Current status: PHASE 39 - Secure `.env` provider-key cleanup (this build)

| Status | Capability |
|---|---|
| Implemented + tested (1A) | Configuration with validation and secret redaction; task state machine; SQLite migrations; deterministic policy engine; `doctor` and task-record CLI |
| Implemented + tested (1B) | Versioned built-in tool registry with strict schemas; workspace tools; PDF inspect/verify/merge tools; skipped-file reporting; synthetic demo data |
| Implemented + tested (1C/Phase 19) | Provider adapters (Gemini, OpenRouter, Anthropic Messages API, OpenAI/generic OpenAI-compatible, Ollama, offline mock), bounded fallback router, deterministic-first structured planning, optional constrained AI planning, stored task orchestration, exact read-only approvals for external input folders, SQLite plan/approval/tool-invocation records |
| Implemented + tested (Phase 2) | Dynamic pure-compute tool candidates from reviewed templates/import/provider generation; strict proposal/schema validation; Python AST security analysis; restricted subprocess tests; immutable versioned SQLite registry with provenance/source hashes; explicit approval; invocation; enable/disable; supersede/rollback; bounded provider repairs |
| Implemented + tested (Phase 3) | Deterministic failure diagnosis and bounded recovery for orchestrated tasks; fresh-output retry after collisions or merge verification failure; uniquely corrected workspace input paths; transient same-plan retries with backoff; SQLite recovery ledger; linked recovery clones for failed/stale tasks; stale-task and orphan-temp-PDF scan (report-only) |
| Implemented + tested (Phase 4) | Standard-library Office engine for DOCX/XLSX/PPTX/CSV inspection and text extraction, CSV statistics, CSV↔JSON, CSV→XLSX and simple DOCX creation; one-page SSRF-guarded web text fetch; desktop system info and explicit default-app opening; task `latest`/`latest-failed` aliases, status filtering and exact failed-task next commands; quieter known-corrupt-PDF handling |
| Hotfix (Phase 4.1) | Verified Windows TLS trust using system + Windows-store + Certifi CA sources for web/provider HTTPS; duplicate successful-recovery clone guard; terminal-readable multi-line `tools list` |
| Implemented + tested (Phase 5) | Standard-library Tkinter desktop GUI with Dashboard, Tasks, Documents, Web, Desktop, Tools and Providers tabs; same orchestrator/recovery/provider/policy services as the CLI; worker-thread operations and exact external-read approval dialogs; `pc-agent gui --check` and windowed `pc-agent-gui` launcher |
| Implemented + tested (Phase 6) | Offline Ed25519-signed licensing: trial/standard/pro/enterprise plans mapped to feature gates, expiring or perpetual terms, optional machine binding, built-in configurable trial, vendor `pc-agent-license-admin` issuing tool, customer `pc-agent license` CLI + GUI Dashboard activation; enforcement opt-in (`off` default) |
| Implemented + tested (Phase 7) | Generalized acceptance-criteria verification persisted per attempt (artifact existence, independent SHA-256, PDF pages, CSV/JSON row recounts); durable step checkpoints with safe in-place `task resume`; cooperative CLI/GUI cancellation at stage boundaries; inspectable memory (preferences, notes, automatic verified-workflow records) with secret guards and explicit task-history deletion; schema v6 |
| Implemented + tested (Phase 8) | General workspace file operations — `files.copy/move/rename/organize/search` (23 built-ins) with a dedicated `MOVE_FILE` policy action, no-overwrite semantics, hash-verified transfers, conflict-skipping folder organization, bounded name/content search that never reads credential-like contents, deterministic planner intents, direct `pc-agent files` CLI, and a GUI Documents > Files panel; copy collisions heal to a fresh `-recovered` output |
| Implemented + tested (Phase 9) | Deeper Office — `pdf.extract_pages`/`pdf.split`, run-safe `office.docx_replace_text`, `office.docx_append_paragraphs`, `office.xlsx_set_cells` with deterministic formula calculation (arithmetic + SUM/AVERAGE/MIN/MAX/COUNT/PRODUCT/ABS/ROUND) and `office.xlsx_verify_formulas` (29 built-ins); deterministic planner intents with `--pages/--find/--replace/--cell/--value`, direct `pdf extract|split` + `office docx-*/xlsx-*` CLI, and GUI Documents panel controls |
| Implemented + tested (Phase 10) | Verified workflow reuse — workflow records now store the exact validated arguments; `pc-agent workflow list|show|run|delete` re-runs a verified plan as a new task after live revalidation (approvals never inherited), the planner falls back to a verified workflow when it cannot plan an instruction, legacy Phase 7 records resolve through their last task's stored plan, and the GUI Tasks tab can run a workflow |
| Implemented + tested (Phase 11) | Multi-step plans (up to 5 chained tools via 'then', deterministic or AI `steps` arrays) with run-time output bindings, per-step approvals/criteria/checkpoints/bounded recovery, resume-from-first-unfinished-step, and multi-step workflow recording + replay |
| Implemented + tested (Phase 12) | Approved subprocess & Git — `git.status/log/diff_summary/init/add/commit` + `commands.run` (36 built-ins) behind the default-off `PC_AGENT_ALLOW_SUBPROCESS` gate; allow-listed bare program names only, argv-array execution (never a shell; Windows npm-style shims go through a validated `cmd` bridge), scrubbed environment, hard timeout with process kill, capped output; commands and Git writes always need a fresh per-argument approval and are never auto-retried; deterministic planner intents, AI limited to read-only Git, direct `pc-agent git` CLI, GUI Git Status button |
| Hotfix (Phase 12.1-12.3) | CLI confirmations on interactive terminals now take a single `Y` keypress (no Enter): text typed/pasted before the prompt is discarded, pasted bursts (even `--yes`) can never answer, Enter presses just show a hint, Ctrl+C cancels cleanly; pipes keep the typed-word prompt with re-asking; a denied Git write prints the exact command to re-run with `--yes` |
| Implemented + tested (Phase 13) | Browser automation over CDP — `browser.status/open/snapshot/click/type/screenshot` (42 built-ins) against a user-started Edge/Chrome debug session behind the default-off `PC_AGENT_ALLOW_BROWSER` gate; standard-library WebSocket/CDP client; fixed JS templates (values only via JSON encoding); actions need fresh per-argument approvals and are never auto-retried; typing refuses password/file fields and is verified by read-back; screenshots are hash-verified workspace PNGs; AI limited to read-only browser tools; direct `pc-agent browser` CLI, GUI Web-tab read buttons, live-verified against Chromium 131 |
| Hotfix (Phase 13.1) | Gemini provider could never route (config lowercases provider names but the router factory compared `"Gemini"`), so it showed as an unknown/reserved slot; fixed to the canonical lowercase name end-to-end. Three CLI tests that relied on the working directory are now rooted at a temp dir, so a developer's real `.env` (with provider keys) can no longer leak into offline tests |
| Implemented + tested (Phase 14) | Windows desktop UI Automation - `desktop.windows/window_inspect/window_focus/window_click/window_type/send_keys/screenshot` (49 built-ins) through Win32 + Microsoft UI Automation; default-off `PC_AGENT_ALLOW_DESKTOP_AUTOMATION` gate; exact window/element selectors, fresh focus/click/type/key approvals, password refusal, typed read-back, bounded chords, verified workspace PNGs, no automatic retries; AI limited to read-only desktop tools; direct `pc-agent desktop` CLI and GUI Desktop-tab controls. Live-confirmed on the target Windows PC for window listing and Notepad UIA inspection |
| Implemented + tested (Phase 15) | Local image tools + provider vision - `image.inspect/convert/transform/compare/analyze` (54 built-ins). Pillow inspection/conversion/cropping/resizing/thumbnails/rotation/flips/grayscale/comparison stay on-PC, are size/pixel bounded and never overwrite. `image.analyze` sends one exact workspace image/question to a vision-capable Gemini/OpenRouter/OpenAI-compatible/Ollama route only when `PC_AGENT_ALLOW_VISION=1` and that exact send is approved; the offline mock refuses images; AI is limited to local inspect/compare; vision is never auto-retried. Direct `pc-agent image` CLI and a GUI Images tab are included |
| Implemented + tested (Phase 16) | Windows Credential Manager storage for provider API keys. Settings resolve environment/`.env` first, then one generic Windows credential per provider through the native `CredReadW`/`CredWriteW`/`CredDeleteW` APIs; `pc-agent credentials status|list|set|delete|migrate` never prints values, `set` uses a hidden prompt and no key argument, and there is no credential tool or AI/task access. Doctor and GUI show only backend/stored-provider/source status. Existing `.env` installs keep working unchanged; test runs are isolated from the real OS vault |
| Hotfix (Phase 16.1) | Real-Windows test fixes: an injected/simulated Win32 credential backend's `ERROR_NOT_FOUND` is no longer masked by `ctypes.get_last_error()` returning 0, and the live GUI construction test now expects the Phase 15 Images tab (8 notebook tabs). Production Credential Manager calls through the real `advapi32` handle are unchanged |
| Implemented + tested (Phase 17) | Deterministic core self-repair + per-task budgets. A default-off `PC_AGENT_ALLOW_SELF_REPAIR` gate protects baseline capture/diagnosis/staging/apply/rollback for eligible `src/` + `tests/` + `pyproject.toml`; repairs can only restore a user-captured known-good baseline or delete generated Python caches, are checked in an isolated tree with a scrubbed environment, block on stale/corrupt/version-mismatched baselines, back up live files and auto-roll back failed applies. Orchestrated tasks also enforce token/provider-call/tool/step/time budgets with secret-free report evidence; no AI tool or planner path can repair core code |
| Implemented + tested (Phase 18) | Production hardening and portable Windows release support: read-only release audit; deterministic hash-manifested portable ZIPs that exclude `.env`, databases and workspace contents; verified SQLite backup/restore with a pre-restore backup; redacted diagnostics and `doctor --json`; conservative PowerShell install/uninstall/release-build scripts plus GUI/CLI launchers; GUI Release Audit. No new tool, schema migration or runtime dependency |
| Implemented + tested (Phase 19) | Direct Anthropic Messages API provider: HTTPS Bearer authentication, `2026-10-21` top-level system blocks and required `max_tokens`, no removed sampling fields, inline base64 vision, usage propagation into task budgets, and clean error/refusal handling. Existing environment/Credential Manager resolution, deterministic routing and planner policy are unchanged; no new tool, schema migration or runtime dependency |
| Implemented + tested (Phase 20) | PDF text + page transforms — `pdf.extract_text`, `pdf.search_text`, `pdf.reorder_pages`, `pdf.rotate_pages` (58 built-ins). Embedded text only (never OCR), bounded at 500 pages/5,000,000 characters; text/Markdown/JSON extraction, exact-phrase page search with snippets, full/reverse page reordering and selected quarter-turn rotations. Writers never overwrite, sources stay read-only, outputs are hash/page-fingerprint/rotation verified, and direct CLI, deterministic tasks and the GUI Documents PDF panel are included; AI planning is limited to read-only PDF search. No schema migration or runtime dependency |
| Implemented + tested (Phase 21) | Signed offline updates from Phase 18 portable ZIPs: detached Ed25519 bundle signatures verified against a configured public key, strict version gates, hash-verified staging receipts, approved overlay apply with complete backups, failure auto-restore and rollback that preserves created files; `pc-agent update check|stage|apply|rollback|list|show`, vendor-only `pc-agent-release-admin keygen|sign`, GUI Dashboard update panel and release-audit trust checks. No network download, planner tool, schema migration or runtime dependency |
| Implemented + tested (Phase 22) | Scanned PDF OCR — `pdf.ocr` (59 built-ins) renders at most 10 selected pages locally with PDFium, reuses embedded-text pages by default, sends only scanned page JPEGs through the Phase 15 vision gate/provider route after exact approval, and writes a new hash-verified .txt/.md/.json with per-page source/provider/model and sent-image metadata. DPI/pixel/byte/token/character bounds, task budgets, direct CLI, deterministic tasks and GUI controls are included; verification checks structure/counts/embedded text/hash without pretending provider wording is deterministically re-graded, and OCR is never auto-retried or AI-planned. No schema migration; new `pypdfium2` runtime dependency |
| Implemented + tested (Phase 23) | PDF table extraction — `pdf.extract_tables` (60 built-ins) reconstructs bounded tables from embedded PDF character geometry on up to 100 selected pages into a new hash-verified JSON file, or CSV when exactly one table is detected. Word/line/cell/column grouping, minimum row/column thresholds, per-table page/index/row/cell/bounding-box metadata, direct CLI, deterministic tasks and GUI controls are included. It never OCRs or overwrites; scanned PDFs honestly return zero tables, orchestrated verification independently reruns local extraction, collision recovery preserves the original output, and AI planning cannot propose the writer. No schema migration or new runtime dependency |
| Implemented + tested (Phase 24) | Explicit online signed update downloads: default-off configured HTTPS feed, strict format-1 JSON metadata, same-origin bundle/signature URLs, global-address and redirect checks, bounded responses, feed SHA/expected-size checks, downloaded-bundle portable/signature verification, download receipts under `data/updates/downloads`, and reuse of Phase 21 staging/apply/rollback. `online-check` is feed-only and does not claim bundle authentication; `online-download` does not stage; `online-stage` never auto-applies. Direct CLI + GUI controls and Doctor/release-audit reporting are included. No background updater, planner tool, schema migration or new runtime dependency; tool count stays 60 |
| Implemented + tested (Phase 25) | PDF form workflows — `pdf.inspect_form` and `pdf.fill_form` (62 built-ins) inspect/fill interactive fields already present in a bounded AcroForm. Text, single/multi-select choice, checkbox and radio/button states are validated; unknown, read-only, signature and pushbutton fields are refused for filling. The source is never modified and the output is never overwritten; temporary output is reopened to verify changed and untouched values, orchestrated tasks repeat source/output inspection, and output-collision recovery preserves existing files. Direct CLI, deterministic task flags and GUI Documents controls are included. No form creation/design, password flow, OCR, flattening, digital signing, schema migration or new runtime dependency |
| Implemented + tested (Phase 26) | Direct-only PDF password security: `pc-agent pdf security|protect|unprotect` and GUI masked dialogs inspect encryption, write new AES-256 protected copies, change user/owner passwords, and write new unencrypted copies after the current password is verified. Passwords are collected only through hidden prompts and are never tool/task/CLI arguments or persisted results; no registered tool is added (62 built-ins). Sources/outputs are never overwritten, digitally signed PDFs are refused because rewriting can invalidate signatures, and temporary outputs are verified with right/wrong passwords, permissions and page counts before publication. No password recovery/cracking, digital signing or schema migration; `pypdf[crypto]` supplies AES |
| Implemented + tested (Phase 27) | Direct-only PDF digital-signature inspection: `pc-agent pdf inspect-signatures` and a GUI Documents control report signature fields, ByteRange coverage, DocMDP permissions, CMS digest/CMS-signature verification, signing time and embedded X.509 certificate details. A locally consistent signature is labelled `cryptographically_valid_untrusted`; trusted-root, revocation, RFC 3161 timestamp and legal-policy checks are explicitly not evaluated. No registered tool (62 built-ins), signature creation, network access, schema migration or new dependency |
| Implemented + tested (Phase 28) | Direct-only PDF digital-signature creation: `pc-agent pdf sign` and a GUI **Sign PDF** control sign an unencrypted unsigned PDF into a new file with a workspace PKCS#12 identity. The PKCS#12 password is collected only through a hidden prompt/masked dialog; no registered tool or planner surface exists. SHA-256/384/512 detached CMS, existing or newly created visible/invisible fields and optional DocMDP P1-P3 are supported. The source is unchanged, output is never overwritten, and publication requires the Phase 27 verifier to confirm the CMS/ByteRange and expected certificate fingerprint. Trust/revocation remains not evaluated; no schema migration or new dependency |
| Implemented + tested (Phase 29) | Direct-only offline PDF signature trust validation: `pc-agent pdf validate-signatures` and a GUI **Validate Signature Trust** control build a bounded basic X.509 chain from each cryptographically valid signer to explicit workspace trust anchors and evaluate explicit local CRL files. Validity at CMS signing time, CA/path-length/key-usage basics, exact issuer signatures, fresh CRL signatures and revocation at signing time are checked. `trusted_valid` requires a trusted chain plus passing CRL state; no OS/browser trust store, network, OCSP, RFC 3161, policy/PAdES-LTV/legal evaluation, registered tool (62 built-ins), schema migration or new dependency |
| Implemented + tested (Phase 30) | Optional direct RFC 3161 timestamping for new PDF signatures: an exact user-supplied HTTPS TSA receives only the generated CMS signature-value hash (never PDF/key/password), the nonce-bound token is verified for TSTInfo imprint/CMS signature/TSA EKU and inserted as a CMS unsigned attribute, and publication re-verifies it. Inspection also recognizes `ETSI.RFC3161` document timestamps. Phase 29 can chain the TSA to explicit local anchors/CRLs and use a trusted token time instead of local CMS time. No default TSA, OS/Adobe trust program, OCSP/online CRL, incremental DocTimeStamp creation, PAdES/LTV/legal evaluation, registered tool (62), schema migration or new dependency |
| Implemented + tested (Phase 31) | Opt-in online revocation for direct PDF signature-trust validation: local CRLs remain first and offline is the default. When explicitly enabled, bounded CRL distribution-point downloads are verified like local CRLs, then OCSP responses are checked for issuer hashes/serial, freshness, signature and issuer-or-delegated `OCSPSigning` responder authority. Network or authorization failures remain `unknown`, never trusted. CLI/GUI expose timeout and private-PKI endpoint controls; no OS/Adobe trust store, certificate-policy/name-constraint engine, PAdES/LTV/legal evaluation, registered tool (62), schema migration or new dependency |
| Implemented + tested (Phase 32) | Bounded certificate-policy and name-constraint validation for direct PDF signer/TSA chains: required signer/TSA policy OIDs, explicit-policy and any-policy/mapping inhibition, PolicyConstraints and DER PolicyMappings are processed, and DNS/email/URI/IP/directory/registered-OID/otherName constraints are always checked. Violations produce dedicated non-trusted statuses and cannot be bypassed by accepting unknown revocation. CLI/GUI controls are included; policy qualifiers, full WebPKI/qualified policy programs, OS/Adobe trust stores, PAdES/LTV/legal evaluation, registered tool (62), schema migration and new dependencies remain out of scope |
| Implemented + tested (Phase 33) | Opt-in system trust-store anchors for direct PDF signature validation: separate signer/TSA CLI/GUI switches merge bounded CA certificates from the Windows ROOT store or OpenSSL default certificate store with explicit workspace anchors, deduplicate by fingerprint and report source/local/system/effective counts. Store loading failures fail closed and make no network call; all chain, policy/name-constraint and revocation checks still apply. Adobe trust remains explicitly not evaluated; no OS-store modification, registered tool (62), schema migration or dependency |
| Implemented + tested (Phase 34) | Incremental multi-signature PDF signing in the existing direct CLI/GUI workflow: an already signed PDF receives one more CMS signature as a true incremental update, preserving the complete source revision as an exact byte prefix and re-verifying every previous signature, ByteRange and signer fingerprint before publication. Existing blank fields or new fields are supported subject to DocMDP P1/P2/P3 rules; invalid/unsupported existing signatures and document timestamps are refused. No registered tool (62), schema migration or dependency |
| Implemented + tested (Phase 35) | Direct-only DocTimeStamp incremental revisions: `pc-agent pdf timestamp` and a GUI **Add DocTimeStamp** control append one ETSI.RFC3161 `/DocTimeStamp` dictionary without a signer signature or PKCS#12 identity. The exact source remains a byte prefix, only the new revision's ByteRange digest is sent to the user-selected HTTPS TSA, and publication re-verifies every prior approval/timestamp plus the whole-file token. DocMDP P1/P2/P3 is enforced, renewal by repeated operations is supported, and signing after a DocTimeStamp remains refused. TSA trust/PAdES-LTV/legal validity remains not evaluated; no registered tool (62), schema migration or dependency |
| Implemented + tested (Phase 36) | Direct-only LTV DSS embedding: `pc-agent pdf inspect-ltv` / `pdf add-ltv` and GUI **Inspect/Add LTV DSS** controls append or inspect an incremental catalog Document Security Store with DER signer/TSA certificates, explicit local CRLs/OCSP responses and VRI entries keyed by the complete signature Contents value. Existing DSS material is merged, irrelevant/stale/unauthorized or revoked-at-signing evidence refuses publication, the source remains an exact byte prefix, and an optional Phase 35 DocTimeStamp can seal the new DSS. It verifies material relevance but does not evaluate certificate/TSA trust or claim PAdES B-LT/B-LTA, Adobe LTV-enabled or legal status; no registered tool (62), schema migration or dependency |
| Implemented + tested (Phase 37) | Optional direct PAdES Baseline B-B-oriented signature profile in the existing CLI/GUI signer: `--profile pades-bb` creates an `ETSI.CAdES.detached` CMS with exactly content-type, message-digest and ESS signing-certificate-v2 signed attributes, no CMS signing-time, `/M` as the claimed UTC signing time, ESIC ExtensionLevel 2 and whole-file ByteRange coverage. RSA/ECDSA with SHA-256/384/512, incremental signing and optional RFC 3161 signature timestamps are supported. Inspection verifies the ESS certificate binding and reports `bb_structure_verified` or `bb_structure_incomplete`; trust and external PAdES/Adobe/legal conformance remain not evaluated. The legacy `adbe` profile stays the default; no registered tool (62), schema migration or dependency |
| Implemented + tested (Phase 38) | Deterministic unsigned Windows installer source package: `pc-agent release installer-source` stages the runtime release subset (no tests, real `.env`, data or private keys), generates an explicit per-user Inno Setup 6 script and SHA-256 manifest, and `installer-verify` fails closed on tamper/unsafe wiring. A guarded PowerShell helper can compile it with locally installed ISCC on Windows. The installer runs the existing venv setup, creates GUI/CLI shortcuts, and uninstall preserves user data/workspace/`.env`/credentials. No compiled EXE, Authenticode, SmartScreen reputation, registered tool (62), schema migration or dependency |
| Implemented + tested (Phase 39) | Secure direct-only `.env` provider-key cleanup: `pc-agent credentials remove-env-keys` plans or removes a provider line only when its identical effective value is already verified in the OS credential store. Mismatched, missing, empty or unavailable-store keys stay untouched; approved changes are revalidated and atomically applied without a plaintext backup, with a secret-free receipt. No registered tool (62), schema migration or dependency |
| Later | Authenticode signing, real vendor HTTPS update-feed hosting/configuration, optional true background auto-update service, macOS/Linux packaging, Adobe trust-program integration and externally verified full WebPKI/qualified-policy or PAdES workflows |

This build **does not** claim arbitrary computer control, arbitrary generated
programs, full browser automation, password/coordinate UI control, AI
image generation/editing, or autonomous/model-authored repair of the core
runtime. Phase 17 self-repair is narrower: user-enabled, deterministic
restoration from a known-good local baseline. Phase 5's GUI is this application's
own window. Phase 14 desktop automation is narrower than general computer
control: only opt-in, exact UIA window/element selectors on already-open,
same-session Windows apps, with fresh approvals for driving actions. Phase 4
web fetching reads one exact page as text without executing scripts, and the
older desktop tools still only report system information or open one exact
workspace item/URL. Self-healing remains limited to known safe
failure patterns in the orchestrated local allow-list. Dynamic tools are
invoked explicitly and remain JSON-in/JSON-out pure computation with no file,
network, shell or desktop permission. Phase 6 licensing is offline and local:
there is no payment processing, online activation/subscription-billing
server, or cloud account yet.

## Setup / upgrade (Windows PowerShell)

New installs apply the patchers in order: 1A, 1B, 1C, Phase 2, Phase 3,
Phase 4, the Phase 4.1 hotfix, Phase 5, Phase 6, Phase 7, Phase 8, Phase 9,
Phase 10, Phase 11, Phase 12, Phase 13, Phase 14, Phase 15, Phase 16,
the Phase 16.1 hotfix, Phase 17, Phase 18, Phase 19, Phase 20, Phase 21, Phase 22, Phase 23, Phase 24, Phase 25, Phase 26, Phase 27, Phase 28, Phase 29, Phase 30, Phase 31, Phase 32, Phase 33, Phase 34, Phase 35, Phase 36, Phase 37, Phase 38, then Phase 39. The
Phase 39 patcher is cumulative: it upgrades an existing Phase 9 (0.11.0)
through Phase 38 (0.40.0) install:

```powershell
cd D:\omni-assistant   # your project folder
node PHASE_39_SECURE_ENV_KEY_CLEANUP.mjs

.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check
pc-agent --version
pc-agent doctor
pc-agent demo pdf
pc-agent run "Merge PDFs from Folder A and Folder B then extract pages 1-2"
pc-agent workflow list
pc-agent gui --check
```

Phase 39 adds secure `.env` provider-key cleanup without a registered tool,
so the count stays 62 built-ins and the database schema stays **v6**.
Reinstalling refreshes the editable package metadata to 0.41.0; no new
dependency is added beyond Phase 26's `pypdf[crypto]`. Your current `.env`
keys keep taking precedence. Run `pc-agent credentials migrate` only when
you want to copy them into Windows Credential Manager; that command still
does not edit `.env`. Afterwards, `pc-agent credentials remove-env-keys`
can remove only lines whose identical value is verified in Credential
Manager. See `docs/phase39.md` for secure cleanup,
`docs/phase38.md` for the Windows installer source
package, `docs/phase37.md` for the PAdES B-B signature
profile, `docs/phase36.md` for LTV DSS embedding,
`docs/phase35.md` for DocTimeStamp incremental
revisions, `docs/phase34.md` for incremental multi-signature
signing, `docs/phase33.md` for system trust-store anchors,
`docs/phase32.md` for certificate policies/name constraints and remaining
work, `docs/phase31.md` for online CRL/OCSP
revocation, `docs/phase30.md` for RFC 3161 timestamping,
`docs/phase29.md` for local trust-chain/CRL validation,
`docs/phase28.md` for signature creation,
`docs/phase27.md` for signature inspection,
`docs/phase26.md` for PDF password workflows,
`docs/phase25.md` for PDF form workflows,
`docs/phase24.md` for the online update trust model,
`docs/phase23.md` for PDF table extraction,
`docs/phase22.md` for scanned PDF OCR,
`docs/phase21.md` for signed offline updates,
`docs/phase20.md` for PDF text/page transforms,
`docs/phase19.md` for direct Anthropic configuration,
`docs/phase18.md` for production/release operations,
`docs/phase17.md` for self-repair and budgets, `docs/phase16.md` for the
migration and security model, `docs/phase15.md`
for images/vision, `docs/phase14.md` for desktop safety, and
`docs/phase13.md` for browser safety. License enforcement still defaults
to `off`.

## Desktop GUI (Phase 5)

Open the local window from the project folder:

```powershell
pc-agent gui
```

The GUI provides Dashboard, Tasks, Documents, Images, Web, Desktop, Tools
and Providers tabs over the same policy-bound services as the CLI. Run
`pc-agent gui --check` for a no-window readiness test. Dynamic Tool Builder
generation/approval and API-key entry/deletion remain outside the GUI;
the GUI shows only credential-store and source status. See `docs/phase5.md` for the tab guide, smoke test and
troubleshooting.

## Licensing (Phase 6)

Licensing is offline and opt-in. With the default
`PC_AGENT_LICENSE_ENFORCEMENT=off`, nothing changes for local development;
`pc-agent license status` simply reports the unconfigured state. Commercial
builds configure the vendor **public** key in `.env` and choose `warn` or
`enforce`, which enables the built-in trial (14 days by default) and signed
license activation:

```powershell
pc-agent license status
pc-agent license machine-id
pc-agent license activate --file C:\path\to\customer.license
```

The vendor issues licenses on a trusted machine with
`pc-agent-license-admin keygen` and `pc-agent-license-admin issue`; the
private key must never reach a customer machine. The GUI Dashboard has the
same status plus activation/deactivation buttons. Plans, feature gates,
trial and clock-rollback rules, and the vendor quickstart are documented in
`docs/phase6.md`. Online payments/activation and subscription billing are not
part of this phase.

## Configure AI providers (keys stay on your PC)

The local core, deterministic tools and offline Tool Builder templates work
without keys. Provider-backed tool generation/repair requires a real provider;
the offline mock cannot generate code. For external AI, create/edit **your
local** `.env` (do not send keys in chat):

```dotenv
PC_AGENT_PROVIDER_ORDER=gemini,openrouter
GEMINI_API_KEY=your-local-key
OPENROUTER_API_KEY=your-local-key
```

Then check the route without a network call:

```powershell
pc-agent providers list
pc-agent ask "Reply briefly: provider test successful"
```

`ask` uses the configured route and may use the network/API quota. Supported
PDF/file-list `run` commands are deterministic first and make **no provider
call**. Provider planning is used only if you add `--use-ai` and deterministic
planning does not recognize the instruction. See `docs/providers.md`.

## Dynamic Tool Builder demo (offline, no API key)

This exercises a complete reviewed-template lifecycle: candidate -> sandbox
tests -> human review -> explicit approval -> registry invocation.

```powershell
pc-agent tool-builder suggest "I need to count words"
pc-agent tool-builder template word_count --run-tests
pc-agent tool-builder show custom.text_word_count@1.0.0 --show-source
pc-agent tool-builder approve custom.text_word_count@1.0.0 --yes
pc-agent tools list
pc-agent tools invoke custom.text_word_count --arguments-file examples\dynamic-tools\word_count_arguments.json
```

Expected output includes `"words": 3`. The tool has no permissions and runs
each invocation in the restricted subprocess harness. Use
`pc-agent tools disable custom.text_word_count` to turn it off without
deleting its approval/test history; `tools enable` restores it.

Phase 3 also hardens this demo: relative `--arguments-file` paths are searched
in the current folder, then the app root, then the workspace. If that one
known bundled word-count example is missing from disk, the CLI uses its
embedded copy, prints a warning, and `doctor` reports that the bundled file
should be restored by rerunning the latest patcher. Other missing argument
files list every path that was checked. `tool-builder approve --yes` now
prints the final approved/enabled state after approval.

With Gemini/OpenRouter/OpenAI/Ollama configured, `tool-builder generate` can
propose a candidate and `tool-builder repair` can propose a bounded repair.
Generated code is still untrusted until the same static checks, sandbox tests
and your approval pass. See `docs/tool_builder.md` for the proposal format,
security layers, lifecycle and limitations.

## Office, web fetch and desktop tools (Phase 4)

Create deterministic local demo files, then use direct commands:

```powershell
pc-agent demo office
pc-agent office inspect "Office Demo/summary.docx"
pc-agent office csv-inspect "Office Demo/sales.csv"
pc-agent office extract "Office Demo/summary.docx" --output "Office Demo/summary.txt"
pc-agent office csv-to-json "Office Demo/sales.csv" --output "Office Demo/sales-converted.json"
pc-agent office csv-to-xlsx "Office Demo/sales.csv" --output "Office Demo/sales-book.xlsx" --sheet-name Sales
pc-agent office create-docx --output "Office Demo/release-note.docx" --title "Phase 4" --paragraph "Office tools are ready for local testing."
```

Supported reads are DOCX, XLSX, PPTX and CSV. JSON is supported as a
conversion source/target. Macro-enabled/legacy Office files, encrypted
packages, oversized archives/XML and `DOCTYPE` XML are rejected. Every writer
creates a new workspace file and never overwrites.

Web fetch is deliberately narrower than a browser. It performs one bounded
GET for the exact URL in that direct command, validates public resolved IPs
and every redirect, caps bytes/time, accepts text/HTML/JSON/XML, removes
scripts/styles and never sends cookies or credentials:

```powershell
pc-agent web fetch https://example.com/
pc-agent web fetch https://example.com/ --output example.txt
```

Private/loopback addresses and non-standard ports are blocked unless the same
direct command includes `--allow-private`. Generic `run`, AI planning and
`tools invoke web.fetch` do **not** receive network authority.

Desktop support can report local system information or open one exact target
in the default application. It does not click, type, take screenshots or run
executable/script/installer files:

```powershell
pc-agent desktop info
pc-agent desktop open "Office Demo/summary.docx"
pc-agent desktop open-url https://example.com/
```

Safe local Office work can also be orchestrated and verified:

```powershell
pc-agent run "Convert CSV to JSON" --target "Office Demo/sales.csv" --output "Office Demo/task-sales.json"
pc-agent run "Show system information"
```

See `docs/phase4.md` for the command reference, limits and honest boundaries.

## Self-healing and recovery (Phase 3)

Orchestrated `run`/`task run` executions record every attempt. By default, at
most two healing retries are allowed (absolute cap: three). Safe examples:

- The planned merge output already exists -> preserve it and retry to a
  `*-recovered.pdf` workspace file.
- A workspace input moved -> retry only if exactly one matching file/folder
  is found; ambiguous or absent inputs become `needs_input`.
- A merge does not pass verification -> retry to a fresh output and verify
  again.
- A timeout/locked-file style failure -> retry the exact same validated plan
  after bounded exponential backoff.

Policy, approval, corrupt/encrypted and other invalid-input failures are
never bypassed. If a revised plan still reads approved external folders, the
exact revised plan is approved again before another tool runs.

```powershell
pc-agent run "Merge PDFs from Folder A and Folder B" --output merged.pdf
pc-agent run "Merge PDFs" --no-recovery          # disable healing for one run
pc-agent task diagnose latest-failed             # no angle brackets to type
pc-agent task recover latest-failed
pc-agent task list --status failed
pc-agent recovery scan
pc-agent recovery list
pc-agent recovery show latest-failed
```

A failed human-readable `run` report prints its exact diagnose/recover
commands with the real ID already inserted. Task commands also accept
`latest`, `latest-failed` and `latest-<status>` (for example
`latest-needs_input`); the CLI prints which ID the alias resolved to. Do not
type documentation placeholders such as `<TASK_ID>` literally—Windows treats
`<` and `>` as redirection before the agent can run.

Failed terminal tasks are not reopened. Manual recovery creates a linked
clone, copies a valid stored plan when available (otherwise it replans), and
preserves the source task for audit. A stale/interrupted active task is first
closed as failed with an interruption reason, then its clone runs. Use
`task recover --force` only if you are sure no other `pc-agent` process is
still running that task. `recovery scan` only reports stale tasks and old
temporary merge files; it deletes nothing.

Recovery defaults can be changed locally in `.env` with
`PC_AGENT_MAX_RECOVERY_ATTEMPTS` (0-3),
`PC_AGENT_RECOVERY_BACKOFF_SECONDS` (0-5), and
`PC_AGENT_RECOVERY_STALE_AFTER_MINUTES` (1-10080). See `docs/recovery.md`.

## Resume, cancel and memory (Phase 7)

Phase 7 checkpoints every successful tool step. If a task is interrupted
(Ctrl+C, crash, closed window), continue the **same** task instead of
cloning it — a valid checkpoint is reused and fully re-verified instead of
silently re-running completed work:

```powershell
pc-agent task resume latest
pc-agent task cancel latest
pc-agent task delete latest --yes          # terminal tasks only; files stay
pc-agent memory stats
pc-agent memory prefs
pc-agent memory pref-set editor notepad
pc-agent memory note-set home "call office tomorrow"
pc-agent memory notes
pc-agent memory workflows                   # verified-success evidence
pc-agent memory clear --yes                 # keeps task history
```

Use `task resume --force` only if you are sure no other `pc-agent` process
is running that task. Cancellation stops a running task safely at the next
stage boundary (never mid-write) from another terminal or the GUI Tasks
tab. Every run now also prints explicit acceptance criteria with
`[PASS]`/`[FAIL]` evidence. See `docs/phase7.md`.

## File operations (Phase 8)

Workspace-confined copy/move/rename/organize/search. Nothing is ever
overwritten or deleted; transfers are SHA-256 verified:

```powershell
pc-agent files copy report.txt "Archive/report.txt"
pc-agent files move "Archive/report.txt" report-final.txt
pc-agent files rename report-final.txt report.txt
pc-agent files organize . --by category
pc-agent files search budget --content
pc-agent run "Copy the file" --target "report.txt" --output "Archive/report.txt"
pc-agent run "Organize this folder by extension"
pc-agent run "Search files containing \"budget\""
```

See `docs/phase8.md`.

## Deeper Office (Phase 9)

PDF split/extract, DOCX text editing and XLSX cell writing with real,
deterministic formula calculation — still workspace-confined and
no-overwrite:

```powershell
pc-agent pdf extract book.pdf --pages "1-3,5" --output part.pdf
pc-agent pdf split book.pdf --output-folder pages
pc-agent office docx-replace doc.docx --output edited.docx --find "draft" --replace "final"
pc-agent office xlsx-set data.xlsx --output edited.xlsx --cell B4==SUM(B2:B3)
pc-agent office xlsx-formulas edited.xlsx
pc-agent run "Extract pages 1-2 from merged.pdf"
pc-agent run "Set the cell" --target data.xlsx --cell A3 --value "=SUM(A1:A2)"
```

See `docs/phase9.md`.

## Verified workflow reuse (Phase 10)

Every verified success is recorded as a reusable workflow. Re-run one as a
fresh task (live-revalidated; approvals are never inherited), or let the
planner fall back to it automatically when an instruction cannot be planned
fresh:

```powershell
pc-agent workflow list
pc-agent workflow show latest
pc-agent workflow run latest
pc-agent workflow delete latest --yes
```

See `docs/phase10.md`.

## Multi-step tasks (Phase 11)

Chain tools in one instruction with **then** (up to 5 steps). Every step
runs the full approval/verification/checkpoint/recovery pipeline, and a
step automatically consumes the previous step's actual output:

```powershell
pc-agent run "Merge PDFs from Folder A and Folder B then extract pages 1-2"
pc-agent run "Convert sales.csv to xlsx then set cell D5 to \"=SUM(C2:C4)\" then verify formulas"
```

See `docs/phase11.md`.

## Natural-language PDF task (safe demo)

```powershell
pc-agent demo pdf
pc-agent run "Merge PDFs from Folder A and Folder B" --output demo-merged.pdf
pc-agent task list
```

Expected: task status `succeeded`, a verified 6-page
`workspace\demo-merged.pdf`, and the deliberately corrupt demo file reported
under Skipped. Repeating the same command preserves that first output and,
through Phase 3 recovery, creates `workspace\demo-merged-recovered.pdf`.
Or execute an existing queued task:

```powershell
pc-agent task create "Merge PDFs from Folder A and Folder B into queued-demo.pdf"
pc-agent task run latest
```

For exact user-selected folders outside `workspace\`, pass each path. The
agent prints the exact read-only scope and asks you to type `APPROVE`; outputs
still stay in the workspace:

```powershell
pc-agent run "Merge PDFs" `
  --input-folder "C:\Users\You\Desktop\Folder A" `
  --input-folder "C:\Users\You\Desktop\Folder B" `
  --output desktop-merged.pdf
```

Use `--yes` only when you intentionally pre-approve the exact plan printed by
that same command. Approval never grants write/delete access outside the
workspace and never allows credential-like files.

Other supported examples:

```powershell
pc-agent run "Verify merged.pdf has 6 pages"
pc-agent run "List PDF files in Folder A"
pc-agent run "Convert CSV to JSON" --target "Office Demo/sales.csv" --output "Office Demo/orchestrated.json"
pc-agent run "Merge PDFs from Folder A and Folder B" --json
```

Direct deterministic commands from 1B remain available:

```powershell
pc-agent tools list
pc-agent pdf merge-folders "Folder A" "Folder B" --output direct-merged.pdf
pc-agent pdf verify direct-merged.pdf --expected-pages 6
```

Direct `pdf` commands remain workspace-only; external paths belong to the
approval-based `run` flow. Automatic recovery wraps the orchestrated
`run`/`task run` lifecycle, not direct one-shot `pdf`, `office`, `web` or
`desktop` commands. Known corrupt PDFs are still listed under structured
`Skipped` reasons, but pypdf's duplicate low-level header/EOF warnings are
suppressed in normal operation; use `--log-level DEBUG` only when parser
diagnostics are intentionally needed.

## Core self-repair and task budgets (Phase 17)

Self-repair is off until `PC_AGENT_ALLOW_SELF_REPAIR=1` is set locally.
Capture a baseline only after a verified test run, then use the isolated
stage/apply/rollback flow:

```powershell
pc-agent self-repair baseline --check full --yes
pc-agent self-repair diagnose
pc-agent self-repair stage --check full
pc-agent self-repair apply latest --check import --yes
```

Only baseline restoration of `src/`, `tests/` and `pyproject.toml`, plus
generated-cache deletion, can be staged. Extra files are reported and never
deleted. The GUI provides read-only diagnosis and budget status; repair
mutations stay CLI-only. If a damaged core file prevents the full
`pc-agent` CLI from starting, `pc-agent-self-repair` provides the same
repair commands through a minimal bootstrap entry point.

Orchestrated tasks enforce per-task provider token/call, planned-tool,
planned-step and wall-time budgets. Configure defaults in `.env` or use
`--token-budget`, `--provider-call-budget`, `--tool-budget`, `--step-budget`
and `--task-time-budget-seconds` on an execution command. Reports include a
secret-free budget snapshot. See `docs/phase17.md`.

## Production hardening and portable release (Phase 18)

Doctor and the GUI Dashboard expose a read-only production audit. The CLI
can build and verify a deterministic portable ZIP whose manifest hashes
every allow-listed file while excluding `.env`, `data`, workspace contents,
virtual environments and caches:

```powershell
pc-agent release audit
pc-agent release build
pc-agent release verify workspace\self-healing-pc-agent-v0.41.0-portable.zip
pc-agent release installer-source
pc-agent release installer-verify workspace\windows-installer-v0.41.0
```

Use `pc-agent maintenance backup|list|verify|restore` for verified local
SQLite backups; restore requires approval and first makes a pre-restore
backup. Use `pc-agent diagnostics export` or `pc-agent doctor --json` for a
redacted support file with counts and statuses but no keys, task
instructions, memory values or document contents. Plain-text Windows
install, uninstall and release-build scripts live in `scripts\`. See
`docs/phase18.md` for the exact safety boundaries and limitations.

## Windows installer source package (Phase 38, unsigned)

`pc-agent release installer-source` creates a hash-manifested runtime payload
and an explicit per-user Inno Setup 6 script; `installer-verify` re-checks it
without compiling. On Windows, `scripts\build_windows_installer.ps1` rebuilds
and verifies that source, then uses a locally installed `ISCC.exe` to create
an unsigned setup EXE and prints its SHA-256. Inno Setup is never downloaded
automatically. The compiled installer still uses the existing
`install_windows.ps1` Python environment flow and uninstall preserves
non-empty `data`, `workspace`, `.env` and Credential Manager entries. There
is no Authenticode certificate or SmartScreen reputation claim yet. See
`docs/phase38.md`.

## Secure `.env` provider-key cleanup (Phase 39)

After `pc-agent credentials migrate`, run
`pc-agent credentials remove-env-keys --dry-run` to see which duplicated
provider values are exactly verified in secure OS storage. Applying the
command requires approval (or `--yes`), removes only `verified_match`
lines, revalidates against concurrent changes, and writes a secret-free
receipt under `data\credential-cleanups\`. It never makes a plaintext backup.
Mismatched or unstored keys remain in `.env`. See `docs/phase39.md`.

## Direct Anthropic provider (Phase 19)

Anthropic is now a real Messages API route rather than a reserved slot.
Store its key with `pc-agent credentials set anthropic`, include
`anthropic` in `PC_AGENT_PROVIDER_ORDER`, and optionally override the
default `claude-sonnet-5-5` with `PC_AGENT_ANTHROPIC_MODEL`. An unkeyed
route entry is skipped. The adapter follows API version `2026-10-21`, so
it sends system text separately and never sends the removed `temperature`,
`top_p` or `top_k` fields. See `docs/phase19.md` and `docs/providers.md`.

## PDF text and page transforms (Phase 20)

Use `pc-agent pdf extract-text`, `search`, `reorder` and `rotate` for
embedded-text extraction/search and verified page-order or quarter-turn
rotation changes. These tools never OCR a scanned PDF and never overwrite
an output; a page with no embedded text is reported honestly as such.
The same operations are available through deterministic tasks and the GUI
Documents tab. See `docs/phase20.md` and `docs/tools.md`.

## Signed offline updates (Phase 21)

Use `pc-agent update check|stage|apply|rollback|list|show` with a local
Phase 18 portable ZIP. A detached Ed25519 signature is verified only
against `PC_AGENT_RELEASE_PUBLIC_KEY` from local configuration; staging
extracts hashed files under `data\updates`, apply requires approval and
backs up replaced files, and rollback refuses to overwrite later edits.
The vendor-only `pc-agent-release-admin keygen|sign` commands create and
use the signing key, which must never be shipped to a customer PC. The
GUI Dashboard has the same check/stage/apply/rollback controls. See
`docs/phase21.md`.

## Online signed update downloads (Phase 24)

After configuring `PC_AGENT_ONLINE_UPDATES=1` and an HTTPS
`PC_AGENT_UPDATE_FEED_URL`, use `pc-agent update online-check`,
`online-download` and `online-stage`, or the matching GUI Doctor-tab
buttons. The feed is strict format-1 JSON; its bundle/signature links must
stay on the same origin, downloads are bounded and redirected only on that
origin, and the downloaded ZIP must match the feed size/SHA-256 before the
existing portable-bundle and Ed25519 checks run. `online-check` does not
authenticate the bundle, `online-download` does not stage it, and
`online-stage` never applies it. There is no background updater or planner
tool. See `docs/phase24.md`.

## Scanned PDF OCR (Phase 22)

Use `pc-agent pdf ocr scan.pdf --output scan-ocr.txt --pages 1-3` for a
scanned/image-only PDF, or the matching deterministic task/GUI Documents
controls. Pages are rendered locally with PDFium; embedded-text pages are
reused by default and only pages needing OCR are sent as bounded JPEGs to
the configured vision route. OCR requires `PC_AGENT_ALLOW_VISION=1`, a
fresh exact approval and a real vision-capable provider. The output marks
embedded versus OCR pages and records provider/model plus sent-image
metadata. Verification checks output structure/counts/hash and embedded
text, but does not claim every provider-transcribed word is
deterministically correct. See `docs/phase22.md`.

## PDF table extraction (Phase 23)

Use `pc-agent pdf extract-tables report.pdf --output report-tables.json`
for born-digital PDFs with embedded table text, or the matching
deterministic task/GUI Documents controls. Character geometry is grouped
locally into rows and columns; JSON can contain zero or multiple tables,
while CSV deliberately requires exactly one detected table. This tool
never OCRs a scanned PDF, never overwrites an output, and reports
image-only input honestly as having no embedded text. Merged cells,
rotated text and tightly spaced borderless layouts still need review.
See `docs/phase23.md`.

## Configuration and data

Copy `.env.example` to `.env` locally when needed. The patcher updates
`.env.example` but never overwrites your `.env`. Local data remains in
`data\` and artifacts in `workspace\`; both are git-ignored.

See `docs/phase4.md`, `docs/phase5.md`, `docs/phase6.md`, `docs/phase7.md`,
`docs/phase8.md`, `docs/phase9.md`, `docs/phase10.md`, `docs/phase11.md`,
`docs/phase12.md`, `docs/phase13.md`, `docs/phase14.md`,
`docs/phase15.md`,
`docs/phase16.md`, `docs/phase17.md`, `docs/phase18.md`, `docs/phase19.md`, `docs/phase20.md`, `docs/phase21.md`, `docs/phase22.md`, `docs/phase23.md`, `docs/phase24.md`, `docs/providers.md`, `docs/tools.md`, `docs/tool_builder.md`,
`docs/recovery.md`, `docs/architecture.md`, and `CHANGELOG.md` for detailed
contracts and honest limitations.
