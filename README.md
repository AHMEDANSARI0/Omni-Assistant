# Self-Healing PC Agent (working title)

A Windows-first AI PC agent being built in verified phases. The goal is a
secure, lightweight, extensible agent that plans and executes user tasks with
explicit permissions, verifies results deterministically, and reports honestly
what it did.

## Current status: 0.82.12 - the certificate truth: what to buy from Pakistan, and what EV no longer buys (this build)

`python scripts\verify_windows.py` runs every Windows gate in one command and
writes `data\windows-verification.json` (see `INSTALLATION_GUIDE.md` §12.2b).

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
| Implemented + tested (Phase 40) | Vendor-side signed update-feed publisher: `pc-agent-release-admin publish-feed` signs a verified portable bundle and emits a complete static HTTPS directory with strict Phase 24 feed, ZIP/signature, customer public-key config, hosting notes and SHA-256 publisher manifest; `verify-feed` revalidates inventory, bundle and Ed25519 signature offline. It never copies/prints the private key and does not upload or add background updating. No registered tool (62), schema migration or dependency |
| Implemented + tested (Phase 41) | Linux/macOS POSIX installer source package: `pc-agent release posix-source` stages the runtime release subset with auditable per-user `install.sh`/`uninstall.sh`, GUI/CLI launchers, exact payload list and SHA-256 manifest; `posix-verify` fails closed without executing shell code. The installer creates `.venv`, runs doctor/GUI checks, adds a Linux desktop entry or macOS `.command`, and uninstall preserves `.env`, data, workspace and credentials. No native DEB/RPM/PKG/DMG, signing/notarization, registered tool (62), schema migration or dependency |
| Hotfix (Phase 42) | Windows `WinError 1314` test compatibility: the Phase 39 symlink-rejection scenario is isolated and skips only when the account cannot create symlinks without Developer Mode/privilege. Production `.env` cleanup still rejects symlinks and remains otherwise unchanged; no registered tool (62), schema migration or dependency |
| Hotfix (Phase 43) | Windows-host Phase 41 POSIX test compatibility: deterministic package hashes/inventory/content still run on Windows, while POSIX execute-bit and `sh` checks run only on a POSIX host with `sh`; shell install/uninstall execution skips on Windows instead of failing with `WinError 2`. Runtime POSIX packaging remains unchanged; no registered tool (62), schema migration or dependency |
| Implemented + tested (Phase 44) | Linux Secret Service provider-key storage: `auto` selects freedesktop Secret Service via bounded `secret-tool` argv calls on Linux, with stable application/provider/purpose attributes, stdin-only secret storage, read-back verification, delete confirmation, generic fail-closed errors, and `linux-secret-service` source labels. Environment/`.env` precedence and Windows Credential Manager behavior are unchanged; no macOS Keychain, registered tool (62), schema migration or Python dependency |
| Implemented + tested (Phase 45) | macOS Keychain provider-key storage: `auto` selects Apple's system `/usr/bin/security` on macOS, with a fixed service/provider account mapping, interactive stdin hex writes that never put a secret in argv, raw/hex read-back verification, delete confirmation, a bounded transport guard, generic fail-closed errors, and `macos-keychain` source labels. Environment/`.env` precedence and Windows/Linux credential behavior are unchanged; no macOS installer/signing/notarization, desktop parity, registered tool (62), schema migration or Python dependency |
| Implemented + tested (Phase 46) | **Windows Inno Acceptance Harness**: direct `pc-agent release installer-acceptance` builds/verifies fresh Phase 38 source, compiles it with locally installed `ISCC.exe`, silently installs only into an isolated acceptance directory, verifies payload hashes/shortcuts/registry/installed CLI and GUI, then silently uninstalls and proves synthetic `.env`/`data`/`workspace` sentinels survive. It refuses an existing AppId/shortcut group, retains logs plus a verifiable JSON report, and never downloads Inno or signs the setup; live Windows evidence still requires running it in a signed-in Windows session. No registered tool (62), schema migration or Python dependency |
| Implemented + tested (Phase 47) | **Native Debian Package**: direct `pc-agent release debian-package` builds a deterministic architecture-independent `.deb` containing the verified POSIX package, system launchers and desktop entry; `debian-verify` validates ar/control/data archives, `md5sums`, Installed-Size, embedded SHA-256 inventory and the embedded POSIX package without dpkg or shell execution. The package has no maintainer scripts; first launch prepares the existing per-user environment and may need network access for Python dependencies because wheels are not bundled. No RPM/PKG/DMG, repository/package signing, registered tool (62), schema migration or Python dependency |
| Implemented + tested (Phase 48) | **Native RPM Package**: direct `pc-agent release rpm-package` builds a deterministic `noarch` RPM v4 package (lead, signature header with SHA1/SHA256 header digests, main header with per-file SHA-256 digests, gzip cpio payload) around the verified POSIX package with system launchers and a desktop entry; `rpm-verify` validates headers/regions, signature digests, file metadata and the embedded POSIX package without rpm or shell execution. The package has no scriptlets; first launch prepares the existing per-user environment and may need network access for Python dependencies because wheels are not bundled. Real RPM 4.20.1 `rpmkeys --checksig` verified all digests. No PKG/DMG, OpenPGP/repository signing, registered tool (62), schema migration or Python dependency |
| Hotfix (Phase 49) | **Windows UTF-8 Test Hotfix**: Phase 45-48 scope tests now explicitly decode UTF-8 project documents instead of using the Windows locale encoding (cp1252), fixing the reported `UnicodeDecodeError` for `docs/providers.md`. An AST regression guard requires explicit UTF-8 on every checked `Path.read_text`/`write_text` call; no `PYTHONUTF8`, locale change, Administrator/Developer Mode, registered tool (62), schema migration or Python dependency |
| Implemented + tested (Phase 50) | **Debian APT Repository**: direct `pc-agent release debian-repository` publishes the verified architecture-`all` `.deb` under `pool/`, with deterministic `Packages`/`Packages.gz` indexes for `all` and `amd64`, a hash-complete `Release` file and a strict repository manifest; `debian-repository-verify` revalidates the complete tree and embedded package without apt/dpkg. Optional `--signing-key` uses the operator's existing system GnuPG key to create `Release.gpg`, `InRelease` and an exported public keyring—no private key/passphrase is accepted or stored. No repository hosting, live Debian/Ubuntu install claim, registered tool (62), schema migration or Python dependency |
| Implemented + tested (Phase 51) | **RPM Repository**: direct `pc-agent release rpm-repository` publishes the verified `noarch` `.rpm` under `Packages/`, with deterministic gzip primary/filelists/other XML, content-addressed repodata names, a hash-complete `repomd.xml` and a strict repository manifest; `rpm-repository-verify` revalidates the complete tree and embedded package without dnf/yum/rpm. Optional `--signing-key` uses the operator's existing system GnuPG key to sign `repomd.xml` and export its public key—no private key/passphrase is accepted or stored, and the RPM itself remains unsigned. No repository hosting, live Fedora/RHEL install claim, registered tool (62), schema migration or Python dependency |
| Hotfix (Phase 52) | **Windows Repository Timestamp Hotfix**: Phase 50/51 repository builders no longer call Windows-unsupported `os.utime(..., follow_symlinks=False)` unguarded; a capability-checked helper rejects symlinks and uses the portable Windows timestamp form, fixing four Windows Phase 50 test failures and the equivalent latent Phase 51 failures. No repository format, registered tool (62), schema migration or dependency |
| Implemented + tested (Phase 53) | **Live Anthropic Verification**: direct `pc-agent providers verify-anthropic` makes exactly one bounded 32-token Anthropic Messages API request with the already-configured key, bypassing router fallback/retries; it requires the fixed `PC_AGENT_ANTHROPIC_OK` response plus provider/model/usage checks and retains a secret-safe self-hashed JSON report. `verify-anthropic-report` revalidates it offline. Text-only (`vision_status: not_tested`); no registered tool (62), schema migration or dependency |
| Implemented + tested (Phase 54) | **Live GNOME Keyring/KWallet Evidence**: direct Linux `pc-agent credentials linux-secret-service-acceptance` identifies the D-Bus owner of `org.freedesktop.secrets`, accepts only GNOME Keyring or KWallet/KSecretService, round-trips one unique random acceptance probe through real `secret-tool`, deletes/proves it absent, and compares real provider keys unchanged in memory. A secret-safe self-hashed report is retained and can be verified offline. No registered tool (62), schema migration or dependency |
| Implemented + tested (Phase 55) | **Windows HTTPS Update-Feed Hosting**: vendor `pc-agent-release-admin build-hosting` wraps a verified Phase 40 feed in a hash-manifest Windows deployment package with a built-in bounded HTTPS static host/scheduled-task profile or a versioned IIS deployment/rollback profile; `serve-hosting` runs only on Windows with an operator-supplied trusted certificate. Direct `pc-agent update hosting-check` fetches and fully verifies the live feed, ZIP, signature and cache policy without staging/applying, retaining a secret-safe self-hashed report for offline verification. Linux/macOS profiles are structured but planned. No registered tool (62), schema migration or dependency |
| Implemented + tested (Phase 56) | **Background Auto-Upgrade**: a separately opted-in Windows Scheduled Task wakes a one-shot worker that checks only a signed HTTPS feed, verifies/downloads/stages a strictly newer bundle, and records self-hashed state/reports. Default `stage-only` never applies; `apply-when-idle` additionally requires the maintenance window, no active task, Windows idle time, AC power and a verified staged signature before using the existing backup/rollback apply path. Linux systemd-user and macOS LaunchAgent profiles are structured but planned. No registered tool (62), schema migration or dependency |
| Implemented + tested (Phase 57) | **Live Debian/Ubuntu APT Evidence**: direct `pc-agent release debian-repository-acceptance` reverifies the Phase 50 repository/current `.deb`, refuses a pre-existing package, uses a temporary APT source/state/cache to run real APT update/candidate/install, verifies dpkg state and every installed file against the package manifest, purges only the acceptance package, and retains a strict self-hashed report for `debian-repository-acceptance-verify`. Signed repositories use `signed-by`; unsigned local acceptance records its temporary `trusted=yes` exception honestly. No registered tool (62), schema migration or dependency |
| Hotfix (Phase 58) | **Windows Auto-Upgrade Lock Hotfix**: the Phase 56 worker now seeks to byte 0 before both Windows `msvcrt.locking` lock and unlock operations. This fixes the reported second-run `PermissionError` caused by an existing `a+b` lock file starting at EOF; an offset-faithful fake-`msvcrt` regression reproduces the old failure and proves repeated lock cycles plus nested-worker refusal. No registered tool (62), schema migration, configuration or dependency change |
| Implemented + tested (Phase 59) | **Linux systemd-user Auto-Upgrade Scheduler**: the existing direct schedule commands and GUI controls now dispatch to Windows Scheduled Task or a per-user Linux systemd one-shot service plus persistent hourly timer. Linux installation preflights the user manager, refuses unmanaged same-named units, writes managed units atomically with rollback, reports unavailable managers gracefully, and removes only managed units. The signed worker policy is unchanged; Linux `stage-only` is functional at that boundary while Linux idle/power sensors remain separate work. macOS LaunchAgent remains planned. No registered tool (62), schema migration or dependency |
| Implemented + tested (Phase 60) | **Linux Idle and AC Power Sensors**: Linux `apply-when-idle` now reads the current UID's interactive systemd-logind idle hints/monotonic timestamps and kernel `/sys/class/power_supply` state, with logind `OnExternalPower` only as an inconclusive-sysfs fallback. Malformed, absent or ambiguous readings remain unknown, and unknown required idle/power state now defers on every platform. The Phase 59 scheduler and signed worker policy are unchanged. No registered tool (62), schema migration or dependency |
| Implemented + tested (Transformation Phase 1) | **Canonical Baseline and Windows Acceptance**: the delivered Phase 60 tree is frozen as the implementation source of truth after reconciliation against audited GitHub Phase 58 commit `89610f3d18f9348b6fe8ead679bab81ee34155df`; Phase 59/60 are retained, stale Phase 44 build labels are repaired, `cryptography` is declared directly, doctor/diagnostics/release-audit expose one secret-free baseline identity and document check, and root architecture/roadmap/feature/security/performance/test documents plus a Windows acceptance pack are added. No registered tool (62), schema migration (v6) or new runtime capability is claimed; native Windows evidence remains pending |
| Implemented + tested (Transformation Phase 2) | **Safety Kernel**: all orchestrated tool calls pass through `SafetyKernel` with hard timeouts (isolated worker subprocess killed at the deadline for data-context tools; bounded daemon-thread timeout for browser/desktop/vision), a file-backed emergency stop (`pc-agent safety`), a digest-bound Approval Center for non-interactive exact-scope decisions (`pc-agent approvals`, schema v7), approval records that store argument SHA-256 digests instead of raw arguments, and optional passphrase-encrypted database backups (`maintenance backup --encrypt`). Doctor/GUI/diagnostics/release-audit expose live safety state. No registered tool (62); native Windows evidence remains pending |
| Implemented + tested (Transformation Phase 8) | **Memory v2 and Knowledge Base**: schema **v10** adds model-tagged note embeddings + tags (content edits invalidate vectors) and `knowledge_documents`/`knowledge_chunks`; explicit ingestion of text/PDF/Office files (never silent OCR, SHA-256 dedupe) with an offline deterministic hashing embedder by default and optional OpenAI-compatible/Ollama embedding backends; hybrid vector+keyword search over documents and notes with a labelled keyword fallback; grounded `knowledge ask` with `[S#]` citations that calls no provider when nothing matches. Two agent tools (`knowledge.ingest`, `knowledge.search`, worker-excluded) bring the registry to **64 built-ins**. Native Windows evidence remains pending |
| Implemented + tested (Transformation Phase 10) | **Voice Pipeline**: schema **v12** `voice_sessions` ledger; push-to-talk capture (native Windows `winmm`, ALSA `arecord`) with hard duration bounds; local-first STT (System.Speech, faster-whisper, whisper.cpp) plus a cloud engine gated by a global flag *and* per-call consent; offline TTS (System.Speech, espeak-ng) that delivers text via a temp file/stdin, never a command line; microphone off by default with persisted consent, separate wake-word opt-in, discard-by-default audio, opt-in redacted transcripts, purge/clear commands, emergency-stop block; no new tool (still **64 built-ins**). Also fixes the two Windows failures reported on 0.71.0 (hard-timeout probe interpreter, Windows process RSS). Native Windows evidence remains pending |
| Implemented + tested (Transformation Phase 9) | **Specialized Multi-Agent Runtime + Agent Studio**: schema **v11** stores agent definitions (role, persona, builtin-tool allow-list, provider preference, budgets) and a run ledger; six builtin specialists (coordinator, researcher, document-worker, data-analyst, knowledge-keeper, reviewer) plus full custom-agent CRUD. Every agent run is an ordinary Safety-Kernel task whose orchestrator gets a **filtered tool registry**, so least privilege is structural; team runs execute <=6 assignments as a bounded dependency graph (explicit, or coordinator-proposed through one strictly validated JSON completion), depth-1 only, no new tool (still **64 built-ins**). Native Windows evidence remains pending |
| Implemented + tested (Transformation Phase 11) | **Advanced Browser, Windows Desktop Automation and Computer Vision**: offline Pillow-only perception (`vision.screen` reports measured brightness/contrast/dominant colours plus a resolution-normalised ink grid, `vision.locate` finds a template with a coarse nomination pass and full-pixel verification on the full-size screenshot, and a flat template is refused instead of matching everywhere); `browser.wait` polls a selector/text/URL condition for at most 120s, `browser.tabs` lists/activates/closes tabs and refuses to close the last one; `desktop.window_state` minimises/maximises/restores/moves/resizes a window with bounds clamped to the virtual screen (minimum 120x120, own window refused). Schema **v13** adds an `automation_runs` audit ledger that records allowed *and* refused actions without page text, screenshots or file contents; the registry grows to **69 built-ins**, the release audit to **25 checks**, and CLI/HTTP/GUI/web/diagnostics/doctor surfaces are all wired to the same service. Native Windows evidence remains pending |
| Implemented + tested (Transformation Phase 12) | **Visual Workflow Builder**: a flow is a strict, versioned **JSON graph of instruction nodes** (`flow_schema` 1) that compiles into an ordinary Mission Center mission, so every node keeps the same Safety Kernel, approvals, token budgets, hard timeouts, verification and recovery. Unknown keys are refused, node ids are slugs (`^[a-z0-9][a-z0-9_-]{0,31}$`), `{{variable}}` placeholders must resolve, cycles are refused (Kahn), and limits are explicit (20 nodes, 8 variables, 8 triggers per flow, 60 s..30 d schedules, 64 KB imports). Immutable versions in `flow_versions`, portable `export`/`import` with a SHA-256 checksum over `{flow, triggers}` (an edited document is refused), `manual`/`schedule`/`event` triggers with an `app_events` watermark and one firing per trigger per tick, and `reconcile()` that closes runs by reading Mission Center instead of trusting a timer. Schema **v14** adds `flow_definitions`, `flow_versions`, `flow_triggers`, `flow_runs`; the release audit grows to **26 checks** (tool count stays **69** - a flow is data, not a tool); CLI `pc-agent flows …`, 18 HTTP routes, a 14th GUI tab **Workflows**, a web **Workflow Builder** panel, a `flows` diagnostics section and a `Flows:` doctor line. The same release widens `redact_text()` to catch bare provider keys (`sk-`, `ghp_`, `AIza`, `xai-`, `gsk_`) so a provider rejection can never be persisted in a run record |
| Implemented + tested (Transformation Phase 17) | **Accessibility, multilingual UI and polish**: a `src/pc_agent/i18n/` layer with **61 interface keys translated for real** into Spanish, **Urdu** and **Arabic** (English is the source of truth), explicit `{placeholder}` substitution, a counted English fallback for anything not translated yet, and a four-entry allow-list so a fake translation fails the suite - user content, task instructions and provider messages are **never** machine-translated. `src/pc_agent/a11y.py` implements the real WCAG 2.1 relative-luminance/contrast maths, adds a high-contrast palette, and reports the language, direction, text scale (80-200%), theme, motion state, every locale's computed coverage, the measured contrast table (passing/total + WCAG level), the documented keyboard shortcuts, the claims this build can make (each with how it is verified) and **five disclosed limits** (Tk 8.6 has no bidi/shaping, no desktop accessibility tree, no OS theme detection, no captioning, four locales only). Preferences persist in `data/ui.json` (environment still wins; a corrupt file cannot break startup). The web Command Center gains real `lang`/`dir` switching with RTL mirroring, a skip link, landmarks, `aria-live` regions, `:focus-visible`, `prefers-reduced-motion` and a working Accessibility panel; the desktop app gains a 19th **Accessibility** tab with scaled fonts and mirrored RTL header. Schema stays **v18** (no migration), release audit grows to **31 checks**, tool count unchanged at **69**, `pc-agent i18n …`, `pc-agent a11y …`, `pc-agent perf bench`, 5 HTTP routes, an `a11y` diagnostics section and an `Accessibility:` doctor line |
| Implemented + tested (Transformation Phase 16) | **Optional encrypted device sync**: a `src/pc_agent/sync/` package that carries reminders, flows and profiles between *your own* devices through a folder you already sync (OneDrive/Dropbox/USB/LAN share) - no port, no server, no account. Each device holds an Ed25519 signing key and an X25519 encryption key in `data/sync/identity.json` (mode `0600`); pairing records a peer's public keys plus a grouped fingerprint and needs `confirm="PAIR"`. Every bundle is sealed per recipient with ephemeral-static X25519 + HKDF-SHA256 + AES-256-GCM (header as AAD) and signed with Ed25519 over the header and the digests of all recipient entries, so a tampered header, a swapped recipient entry, an unpaired sender or a peer whose key changed is all refused. Replay protection uses the processed bundle id plus a strictly increasing per-sender sequence inside a 900 s clock window; size, record count, request count and retention are bounded. Conflicts never destroy local data (reminders de-dupe, flows follow `local|newest|remote`, profiles are create-only, every skip is reported). A peer can never execute anything: only `remind`/`notify`/`ping` coordination requests exist and each waits in a ledger for a local human accept. Schema **v18** (`sync_devices`, `sync_bundles`, `sync_requests`, `sync_state`), release audit grows to **30 checks**, tool count unchanged at **69**, `pc-agent sync …` CLI, 12 HTTP routes, an 18th GUI tab **Sync**, a web **Device sync** panel, a `sync` diagnostics section and a `Sync:` doctor line |
| Implemented + tested (Transformation Phase 15) | **Plugin ecosystem**: a `src/pc_agent/plugins/` package with a strict manifest contract (unknown fields, bad ids/versions, unknown capabilities and traversal paths refused), content checksums bound into the manifest, Ed25519 publisher signatures verified against `PC_AGENT_PLUGIN_TRUSTED_KEYS` (unsigned bundles refused unless explicitly opted in), a closed capability list where `network` and `subprocess` are never granted, verify/install/enable/disable/rollback/uninstall with 5 retained versions, and sandboxed tool execution - POSIX `setrlimit` or a Windows Job Object plus an isolated interpreter (`-I -B`), a scrubbed environment, a `sys.addaudithook` hook that denies sockets, subprocesses, `ctypes` and writes outside the granted roots, and a hard timeout. Every call needs `confirm=<plugin id>`. Markdown skills are returned as text and never executed. Schema **v17** (`plugins`, `plugin_versions`, `plugin_runs`, `plugin_state`), release audit grows to **29 checks**, tool count unchanged at **69**, `pc-agent plugins …` CLI, 12 HTTP routes, a 17th GUI tab **Plugins**, a web panel, a `plugins` diagnostics section and a `Plugins:` doctor line |
| Implemented + tested (Transformation Phase 14) | **Proactive intelligence**: a `ProactiveEngine` (`src/pc_agent/proactive.py`) with durable one-shot/repeating reminders (due as `+15m`/`+2h`/`+3d`, `HH:MM` or ISO-8601; snooze/dismiss/done/enable/delete), a notification policy the user can reason about (wraparound quiet hours with opt-in `critical` crossover, rolling hourly cap 1-500 default 20, per-key cooldown 0-86400 s default 300, and an outbound switch that stays **off** by default), and a daily briefing composed only from live state - reminders, calendar, drive, flows, tasks and system health - stored once per local date. Bad settings are refused at load, not coerced. Schema **v16** (`reminders`, `notifications`, `proactive_briefings`, `proactive_state`), release audit grows to **28 checks**, tool count unchanged at **69**, `pc-agent proactive …` CLI, 13 HTTP routes under `/v1/proactive/*`, a 16th GUI tab **Proactive**, a web dashboard panel, a `proactive` diagnostics section and a `Proactive:` doctor line; the real `api_server` scheduler now runs the proactive tick |
| Implemented + tested (Transformation Phase 13) | **Connectors**: five default-off, local-first connectors with one durable ledger. **Calendar** reads/writes a real `.ics` file (RFC 5545 folding, escaped text, `UID`+`DTSTART` validation, atomic replace, existing events preserved; naive input times are treated as local, never relabelled UTC). **Drive** indexes one local folder by SHA-256 content hash (add/modify/remove/unchanged, symlink-escape refusal, secret names skipped, bounded files and bytes, reported truncation). **Email** reads IMAP headers only (`BODY.PEEK[HEADER.FIELDS …]`, RFC 2047 subjects, no bodies or attachments) and sends over SMTP StartTLS behind a **second** gate plus a per-message `confirm="SEND"` token with CRLF-injection refusal. **Chat** posts one HMAC-signed JSON message (`X-PcAgent-Signature: sha256=…`) to an SSRF-validated endpoint with redirects refused. **Webhook** verifies inbound events (bounded body, clock skew, constant-time signature, replay protection keyed on `kind:external_id`). Schema **v15** adds `connector_events`, `connector_deliveries`, `connector_state`; release audit grows to **27 checks**, tool count unchanged (**69** - connectors are integrations, not tools until Phase 14); CLI `pc-agent connectors …`, 9 HTTP routes, a 15th GUI tab **Connectors**, a web **Connectors** panel, a `connectors` diagnostics section and a `Connectors:` doctor line |
| Later | Authenticode signing, native macOS packages, macOS desktop parity, Adobe trust-program integration and externally verified full WebPKI/qualified-policy or PAdES workflows |

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

## Command Center UI (0.82.x, default entry point since 0.82.2)

The interface is now a React/TypeScript Command Center served by the local API at
`http://127.0.0.1:8765` - the same process, the same ~80 `/v1` routes, the same
safety kernel. Nothing in the Python engine was rewritten to make it work.

**Run it**

```powershell
.\.venv\Scripts\Activate.ps1
pc-agent ui
```

That starts the local API and opens the Command Center in your browser. The
entry points are:

| Command | Opens |
|---|---|
| `pc-agent ui` | Command Center **as a desktop window** (starts the API too) |
| `pc-agent gui` | the same Command Center window - the default since 0.82.2 |
| `pc-agent gui --tk` | the classic Tkinter panel (rollback path) |
| `pc-agent api serve` | API only, no browser - open the address it prints |
| `http://127.0.0.1:8765/legacy` | the pre-0.82 web panel (rollback path) |

**It is PC software, not just a tab.** `pc-agent ui` opens a window with no
address bar and no tabs: an OS webview if you install the optional
`desktop` extra (`pip install -e ".[desktop]"` → WebView2 on Windows), otherwise
an installed Edge/Chrome app window, otherwise your default browser. Add
`--browser` if you want a normal tab, or `--explain` to see which one it would
pick on your machine. The Tauri shell in `frontend/src-tauri/` is the
installer-grade option and needs a Rust toolchain to build.

The shell mirrors for right-to-left locales: Settings → Display → *Text direction*
(`auto` follows the locale the engine reports, or force `ltr`/`rtl`). The built UI ships inside the release
bundle; you only need Node 20+ if you want to change it:

```powershell
cd D:\omni-assistant\frontend
npm ci
npm run build          # cleans stale hashed assets, typechecks, bundles into src/pc_agent/webui
npm run check          # WCAG contrast audit + typecheck
```

**What it contains** - left navigation with 17 destinations, a holographic AI core
whose state is derived from real engine signals, a right-hand intelligence panel
(activity, approvals, alerts, insights from measured values), seven configurable
widgets, and a bottom command bar wired to `POST /v1/run`, the real microphone
capture session and the real emergency stop. Dark **and** light themes, both
contrast-checked; keyboard palette (`Ctrl+K`), sidebar toggle (`Ctrl+B`), reduced
motion honoured from the OS.

**How it is verified** - `frontend/scripts/ui-verify.js` drives real Chromium
against the running API: 46 checks covering every route, both themes' measured
contrast, the live NDJSON stream, persistence, resizing, reduced motion, console
errors and resource use. `docs/command_center_ui.md` documents the design system,
the interaction contract and the limits (including that the Tauri desktop shell is
scaffolded but not built here).


## Setup / upgrade (Windows PowerShell)

For an existing install, use the latest cumulative patcher. The 0.82.0 patcher
upgrades any earlier install - including the audited GitHub Phase 58 tree and the
delivered Phase 60 tree - to the same canonical 0.82.0 files (every earlier
phase, including 0.81.x, the 0.79.1 Windows hotfix and the 0.80.1 sandbox fix,
is included; nothing else is required):

```powershell
cd D:\omni-assistant   # your project folder
node TRANSFORMATION_PHASE_20_COMMAND_CENTER.mjs

.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check
pc-agent --version
pc-agent api serve
pc-agent doctor
pc-agent demo pdf
pc-agent run "Merge PDFs from Folder A and Folder B then extract pages 1-2"
pc-agent workflow list
pc-agent agents list
pc-agent voice status
pc-agent voice devices
pc-agent voice privacy
pc-agent automation capabilities
pc-agent automation stats
pc-agent gui --check
pc-agent sync status
pc-agent sync identity
pc-agent i18n list
pc-agent a11y status
pc-agent perf bench
pc-agent release production-check
pc-agent i18n status
```

Transformation Phase 18 adds **final production hardening** and closes the
programme: `.github/workflows/ci.yml` runs the whole gate - `ruff`, the full
suite, `release audit` and the new `release production-check` - on
`ubuntu-latest` and `windows-latest` across Python 3.11 and 3.13, plus an
accessibility/i18n/perf job and a tag-only packaging job. No secrets are used
(`permissions: contents: read`). `pc-agent release production-check` is the
phase's centrepiece: it builds a real release bundle from this tree, verifies it
without extracting, proves a **tampered** archive is refused, signs with an
**ephemeral** Ed25519 key, proves an **unsigned** bundle is refused while
signing is required, then stages, applies and rolls back the update in a
throw-away copy and compares tree hashes - ten steps, ~1.7 s, and the running
install is never touched. Results are recorded in `data/production-check.json`
and surfaced as release check **32** (`production_hardening`), a `Hardening:`
doctor line and a 20th dashboard tab **Production** whose button runs the real
check. Two new suites make the posture executable: 19 security checks
(SSRF incl. DNS rebinding, command-injection, archive traversal, secret leakage,
emergency stop, tool-schema pollution) and 9 measured performance checks with
budgets written into `PERFORMANCE_REPORT.md`. The four documents the programme
still owed now exist: `AUDIT_REPORT.md`, `DEVELOPER_GUIDE.md`,
`INSTALLATION_GUIDE.md` and `PLUGIN_DEVELOPMENT_GUIDE.md`. Schema stays **v18**,
70 built-in tools, **34** release checks, **20** GUI tabs, **1327** tests,
**479** allow-listed release files.

The last open item has an executable path instead of a promise:
`pc-agent release evidence` runs the acceptance gates on the Windows machine
(suite, release audit, end-to-end release chain, a real `-I -B` plugin worker,
owner-only file hardening, measured accessibility) and seals the result;
`pc-agent release evidence record` stores it in `data/windows-acceptance.json`;
only then does the baseline and the `Hardening:` doctor line say
`windows-acceptance=verified:<date>`. On any other host the command refuses to
claim anything, and `--self-test` proves the harness while staying unrecordable.
The seal is tamper-evident, not authentication - see `SECURITY_REPORT.md`.

Transformation Phase 17 adds **accessibility and multilingual UI**
(`pc-agent i18n status|list|strings|set`, `pc-agent a11y status|check|contrast`,
`pc-agent perf bench`, `/v1/i18n/*`, `/v1/a11y/status`, `/v1/perf/bench`, the 19th
GUI tab **Accessibility** (now followed by Production), a web Accessibility panel; schema **v18**, still 69
built-in tools, **34** release checks). Four interface languages ship with real
translations - English, Spanish, Urdu and Arabic - and Urdu/Arabic are marked
right-to-left, which mirrors the web layout and the desktop header. Text size
(80-200%), high contrast and reduced motion are persisted in `data/ui.json` and
an environment variable still wins. Contrast is **measured**, not claimed: the
report prints the WCAG 2.1 ratio for every text/UI pair on both palettes, and the
five things this build cannot do (Tk bidi/shaping, desktop accessibility tree, OS
theme detection, captioning, more locales) are printed instead of hidden.

Transformation Phase 16 adds **optional encrypted device sync**
(`pc-agent sync status|identity|peers|pair|unpair|pause|resume|export|import|tick|bundles|requests|request|decide`,
`/v1/sync/*`, GUI Sync tab, web panel; schema **v18**, still 69 built-in tools).
It is off until you set `PC_AGENT_ALLOW_SYNC=1` and is deliberately not a cloud
service: bundles are written into a folder you already sync, encrypted to each
paired device's X25519 key and signed with the sender's Ed25519 key. A bundle from
an unpaired device, a tampered header, an old sequence number or a bundle outside
the clock window is refused, and a peer can only ever ask this device to create a
reminder or show a notification - both wait for your accept.

Transformation Phase 10 adds the **Voice Pipeline** (`pc-agent voice
status|devices|enable|disable|wake-word|ptt|transcribe|say|sessions|privacy`,
`/v1/voice/...`, GUI Voice tab, web Voice panel; schema **v12**, still 64
built-in tools): push-to-talk capture through native Windows `winmm` (or ALSA
`arecord` on Linux), local-first speech-to-text (Windows System.Speech,
faster-whisper, whisper.cpp) with an optional cloud engine that needs *both* a
global flag and per-call consent, offline text-to-speech (System.Speech,
espeak-ng), and a microphone that is **off until you explicitly enable it** -
with wake-word listening as a second, separate opt-in. Recordings are deleted
right after transcription unless you opt into retention, transcripts are opt-in
and redacted, and `pc-agent voice privacy --purge-audio` wipes anything kept.
This release also fixes two Windows test failures you reported: the Safety
Kernel hard-timeout probe now resolves `python.exe` (not `python3`), and Windows
process RSS is read through `K32GetProcessMemoryInfo`/PSAPI with explicit 64-bit
handle signatures instead of returning `None`.

Transformation Phase 11 adds **Advanced Automation and local vision**
(`pc-agent automation capabilities|runs|stats|screen|locate`,
`/v1/automation/...`, `/v1/vision/...`, `/v1/browser/wait`, `/v1/browser/tabs`,
`/v1/desktop/window-state`, GUI Automation tab, web panel; schema **v13**,
**69** built-in tools, **25** release checks). Browser runs can now *wait* for a
real page condition instead of sleeping, manage tabs (never closing the last
one), and desktop runs can place a real window - minimise, maximise, restore,
move or resize inside the virtual screen, with a 120x120 minimum and the
assistant's own window refused. Screen understanding is offline and needs no
API key: `vision.screen` measures an image (brightness, contrast, dominant
colours, a resolution-normalised ink grid over up to 12x12 cells) and
`vision.locate` finds a template using a coarse nomination pass followed by
full-pixel verification on the full-size screenshot, so a reported match is a
measured one; a flat-colour template is refused because it would match
everywhere. Every browser/desktop/vision action - allowed or refused - is
written to a local `automation_runs` ledger that stores the action, target label
and short detail only, never page text, screenshots or file contents.

Transformation Phase 9 adds the **Specialized Multi-Agent Runtime +
Agent Studio** (`pc-agent agents list|create|run|team|runs`,
`GET/POST /v1/agents...`, GUI Agents tab, web Agent Studio panel;
schema **v11**, still 64 built-in tools): six builtin specialists
(coordinator, researcher, document-worker, data-analyst,
knowledge-keeper, reviewer) plus your own custom agents, each
running tasks through a filtered tool registry so a specialist
can only ever use its own tools. Team runs execute a small
dependency graph of assignments - written by you or proposed by
the coordinator and validated locally. Transformation Phase 8 adds **Memory v2 and the local Knowledge Base**
(`pc-agent knowledge ingest|list|search|ask|remove|stats`,
`pc-agent memory search|reindex`, `GET /v1/knowledge/...`,
`GET /v1/memory/search`, GUI Knowledge tab, web Knowledge panel;
schema **v10**, 64 built-in tools): ingest text/PDF/Office files
explicitly (no silent OCR), search them together with your memory
notes by meaning-ish hybrid vector+keyword matching — offline by
default via a deterministic hashing embedder, or with a configured
OpenAI-compatible/Ollama embedding model — and ask grounded
questions answered only from retrieved sources with `[S#]`
citations. Transformation Phase 7 adds the **Provider Platform v2**
(`pc-agent providers capabilities|route|usage|evaluate`,
`pc-agent ask --stream`, `GET /v1/providers/capabilities|usage`,
`POST /v1/providers/stream`): per-provider capabilities, strategy
routing (default / local-first / fastest by measured latency /
cheapest by your configured prices), local usage analytics with no
prompt text stored, token streaming where adapters support it, and a
built-in evaluation suite. Transformation Phase 6 adds **live system monitoring**
(`pc-agent system status|sample`, authenticated `GET /v1/system`):
real CPU/memory/disk usage, network rates, agent process RSS, GPU via
`nvidia-smi` when present (an honest "unavailable" otherwise), battery,
and a top-process table — shown in the web Command Center's **System
(live)** panel, the desktop Command Center tab, doctor, and diagnostics
exports. Transformation Phase 5 adds the **JARVIS Command Center**: a built-in
web UI served by the local API (`pc-agent api serve`, then open the
printed `http://127.0.0.1:8765/` URL and paste the token once) with live
tasks, mission graphs, approvals, safety controls, schedules, and the
event tail — plus a matching **Command Center tab** in the desktop GUI.
Every number and button is real API/app state; nothing is simulated.
Transformation Phase 4 adds the **Mission Center**: missions group
tasks into a dependency graph (schema **v9**) that runs with bounded
parallelism (`pc-agent missions create|add-task|link|run|show|cancel`),
plus task schedules (`pc-agent schedules add|list|tick`; the API
process ticks them automatically) — every branch still runs through
the Safety Kernel. Transformation Phase 3 adds the **Local Service**: a loopback-only
token-authenticated local API (`pc-agent api status|serve`, schema **v8**)
over one shared service layer, a durable global event system
(`pc-agent events list`), optional structured JSON logging
(`PC_AGENT_LOG_FORMAT=json` writes `data/logs/pc-agent.jsonl`), and
file-based settings profiles (`pc-agent profiles ...`) that can never
carry secrets or capability gates. TRANSFORMATION PHASE 2 adds the **Safety Kernel**: every orchestrated
tool call now runs under a hard timeout, worker-eligible tools execute in
an isolated subprocess that is killed at the deadline, a file-backed
**emergency stop** (`pc-agent safety stop|resume|status`) can halt all
autonomous execution, non-interactive approvals flow through a
digest-bound **Approval Center** (`pc-agent approvals
list|show|approve|deny`, schema **v7**), approval records store argument
digests instead of raw arguments, and database backups can be
passphrase-encrypted (`pc-agent maintenance backup --encrypt`). It adds
no registered tool, so the count stayed 62 built-ins at that point
(Phase 8 raises it to 64). Reinstalling
refreshes the editable package metadata to 0.77.0. Transformation Phase 1
froze the delivered Phase 60 tree as canonical after reconciliation
against audited GitHub Phase 58 and declared `cryptography` directly
because PDF trust/signing modules import it. Phase 60's
Linux idle/power sensors, Phase 59's **Linux systemd-user Auto-Upgrade Scheduler**,
Phase 58's
**Windows Auto-Upgrade Lock Hotfix**, Phase 57's **Live Debian/Ubuntu
APT Evidence**, Phase 56's **Background Auto-Upgrade**, Phase 55's
**Windows HTTPS Update-Feed Hosting**, Phase 54's **Live
GNOME Keyring/KWallet Evidence**, Phase 53's **Live Anthropic
Verification**, Phase 52's **Windows
Repository Timestamp Hotfix**, Phase 51's **RPM Repository**, Phase 50's
**Debian APT Repository**, Phase 49's **Windows UTF-8 Test Hotfix**,
Phase 48's **Native RPM Package** and Phase 47's **Native Debian
Package** remain available unchanged. See
`docs/transformation_phase2.md` for the Safety Kernel design,
`docs/transformation_phase1.md` for the reconciliation decision,
`docs/windows_acceptance.md` for the still-pending native Windows evidence
pack, `docs/phase60.md` for the Linux idle/power sensors,
`docs/phase59.md` for the Linux systemd
scheduler, `docs/phase58.md` for the Windows lock hotfix,
`docs/phase57.md` for live Debian/Ubuntu APT
evidence, `docs/phase56.md` for background
auto-upgrade, `docs/phase55.md` for Windows
HTTPS update-feed hosting, `docs/phase54.md` for Linux
Secret Service evidence, `docs/phase53.md` for Anthropic verification,
`docs/phase52.md` for the repository hotfix and `docs/phase51.md` for
the RPM repository,
`docs/phase50.md` for the APT repository, `docs/phase49.md` for the hotfix,
`docs/phase48.md` for the RPM package, `docs/phase47.md` for the Debian
package, `docs/phase46.md` for the harness, `docs/phase45.md` for macOS
credentials, `docs/phase44.md` for Linux
credentials, `docs/phase43.md` for the Windows
POSIX test hotfix, `docs/phase42.md` for the symlink test hotfix,
`docs/phase41.md` for POSIX packaging, `docs/phase40.md`
for update-feed publishing, `docs/phase39.md` for secure cleanup,
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
pc-agent release verify workspace\self-healing-pc-agent-v0.77.0-portable.zip
pc-agent release installer-source
pc-agent release installer-verify workspace\windows-installer-v0.72.0
pc-agent release installer-acceptance
pc-agent release installer-acceptance-verify workspace\windows-installer-acceptance-v0.72.0\windows-installer-acceptance-report.json
pc-agent release posix-source
pc-agent release posix-verify workspace\posix-installer-v0.72.0
pc-agent release debian-package
pc-agent release debian-verify workspace\self-healing-pc-agent_0.72.0-1_all.deb
pc-agent release debian-repository
pc-agent release debian-repository-verify workspace\apt-repository-v0.72.0
pc-agent release rpm-package
pc-agent release rpm-verify workspace\self-healing-pc-agent-0.72.0-1.noarch.rpm
pc-agent release rpm-repository
pc-agent release rpm-repository-verify workspace\rpm-repository-v0.72.0
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

### **Windows Inno Acceptance Harness** (Phase 46)

On Windows with Inno Setup 6 installed, the **Windows Inno Acceptance
Harness** performs the missing end-to-end check in
`workspace\windows-installer-acceptance-v<version>`: fresh source build,
`ISCC.exe` compile, silent isolated install, payload/shortcut/registry and
installed CLI/GUI verification, silent uninstall, and sentinel
`.env`/`data`/`workspace` preservation. It refuses to run if the stable
AppId or Start Menu group already exists and leaves
`windows-installer-acceptance-report.json` plus process/Inno logs as
evidence. See `docs/phase46.md`.

## Secure `.env` provider-key cleanup (Phase 39)

After `pc-agent credentials migrate`, run
`pc-agent credentials remove-env-keys --dry-run` to see which duplicated
provider values are exactly verified in secure OS storage. Applying the
command requires approval (or `--yes`), removes only `verified_match`
lines, revalidates against concurrent changes, and writes a secret-free
receipt under `data\credential-cleanups\`. It never makes a plaintext backup.
Mismatched or unstored keys remain in `.env`. See `docs/phase39.md`.

## Signed update feed publisher (Phase 40)

The separate vendor tool can now turn a verified portable ZIP and local
release private key into a complete static update-feed directory with
`pc-agent-release-admin publish-feed`; `verify-feed` checks that tree
offline before upload. The package contains the strict feed, signed ZIP,
customer public-key settings, hosting notes and a SHA-256 publisher
manifest. It excludes the private key and performs no upload. Phase 55
supplies the Windows hosting package/server and IIS deployment around this
exact tree; the operator still supplies the Windows host, DNS and publicly
trusted TLS certificate. Phase 56 adds the separately opted-in background
consumer for that hosted signed feed. See `docs/phase40.md`,
`docs/phase55.md` and `docs/phase56.md`.

## POSIX installer source package (Phase 41)

`pc-agent release posix-source` creates a hash-manifested Linux/macOS
runtime payload with per-user `install.sh`, conservative `uninstall.sh`,
and GUI/CLI launchers; `posix-verify` checks it without executing shell
code. The installer creates `.venv`, runs doctor/GUI checks, and adds a
Linux desktop entry or macOS `.command`. Uninstall preserves `.env`, data,
workspace files and credentials. It is not a native DEB/RPM/PKG/DMG and is
not signed or Apple-notarized. See `docs/phase41.md`.

## Windows symlink test hotfix (Phase 42)

On an unprivileged Windows account, creating a test symlink can fail with
`WinError 1314` before the production `.env` cleanup code runs. Phase 42
isolates that case into its own test and skips only it when Windows refuses
symlink creation. The production cleanup planner still rejects a symlink
`.env` whenever the test can create one, and no runtime behavior changes.
See `docs/phase42.md`.

## Windows POSIX test hotfix (Phase 43)

Windows/NTFS does not expose POSIX execute bits and does not provide `sh`
by default. Phase 43 keeps the deterministic POSIX package hash, inventory,
and content checks running on Windows, while execute-bit, syntax, and shell
execution checks run only on a POSIX host with `sh`. POSIX package runtime
behavior is unchanged; installing Git Bash or WSL is not required for the
Windows test suite. See `docs/phase43.md`.

## Linux Secret Service credentials (Phase 44)

On Linux, `PC_AGENT_CREDENTIAL_STORE=auto` now selects the freedesktop
Secret Service through the system `secret-tool`; `linux` selects it
explicitly. The same direct `pc-agent credentials` commands work there,
secrets reach `secret-tool store` only on stdin, and no credential value is
printed. A running Secret Service provider and the distribution's
`secret-tool` package are prerequisites. See `docs/phase44.md`.

## macOS Keychain credentials (Phase 45)

On macOS, `PC_AGENT_CREDENTIAL_STORE=auto` selects the user's Keychain
through Apple's system `/usr/bin/security`; `macos` selects it explicitly.
Writes use interactive stdin with a hexadecimal value, never argv, and are
read back before success is reported. Effective stored keys are labelled
`macos-keychain`, and the same direct `pc-agent credentials` commands work
there. See `docs/phase45.md`.

## Native Debian package (Phase 47)

`pc-agent release debian-package` creates a deterministic `Architecture: all`
`.deb` around the verified Phase 41 POSIX package, with `/usr/bin` launchers
and a desktop entry but no maintainer scripts. `debian-verify` checks the
Debian archives, control metadata, `md5sums` and embedded SHA-256 inventory
without running dpkg or shell code. Installing the package places only the
system payload; first launch prepares the existing per-user `.venv` and may
need network access for Python wheels, which are not bundled. See
`docs/phase47.md`.

## Native RPM package (Phase 48)

`pc-agent release rpm-package` creates a deterministic `noarch` RPM v4
package around the same verified POSIX package, with `/usr/bin` launchers
and a desktop entry but no scriptlets. `rpm-verify` checks the lead,
signature/main headers and immutable regions, header digests, per-file
SHA-256 metadata and embedded SHA-256 inventory without running rpm or
shell code. Installing the package places only the system payload; first
launch prepares the existing per-user `.venv` and may need network access
for Python wheels, which are not bundled. See `docs/phase48.md`.

## Windows UTF-8 test hotfix (Phase 49)

Windows Python often defaults text reads to cp1252, while this project's
documentation is UTF-8. Phase 49 makes every checked test/document text
read explicitly UTF-8 and adds an AST regression guard, so the Phase 45
scope test no longer fails on curly quotes in `docs/providers.md`. No
`PYTHONUTF8` setting or machine locale change is needed. See
`docs/phase49.md`.

## Debian APT repository (Phase 50)

`pc-agent release debian-repository` builds a deterministic static
**Debian APT Repository** around the verified Phase 47 `.deb`: a `pool/`
package copy, `Packages`/`Packages.gz` indexes for `all` and `amd64`, a
hash-complete `Release`, and a strict JSON repository manifest.
`debian-repository-verify` checks the complete tree and embedded `.deb`
without running apt or dpkg. With `--signing-key`, the operator's existing
system GnuPG key creates `Release.gpg`, `InRelease`, and an exported public
keyring; the application never receives a private key or passphrase. The
builder does not host or install from the repository. See
`docs/phase50.md`.

## RPM repository (Phase 51)

`pc-agent release rpm-repository` builds a deterministic static
**RPM Repository** around the verified Phase 48 `.rpm`: `Packages/`,
gzip primary/filelists/other XML, a SHA-256-complete `repomd.xml`, and a
strict JSON repository manifest. `rpm-repository-verify` checks the
complete tree and embedded `.rpm` without running dnf, yum, or rpm. With
`--signing-key`, the operator's existing system GnuPG key signs
`repomd.xml` and exports its public key; the RPM itself remains unsigned
and the application never receives a private key or passphrase. The
builder does not host or install from the repository. See
`docs/phase51.md`.

## Windows repository timestamp hotfix (Phase 52)

Windows CPython does not support `follow_symlinks=False` for `os.utime`.
Phase 52's **Windows Repository Timestamp Hotfix** makes both repository
builders use a capability-checked timestamp helper that explicitly rejects
symlinks and falls back to the portable Windows call, preserving the same
repository bytes. See `docs/phase52.md`.

## Live Anthropic Verification (Phase 53)

Phase 53's **Live Anthropic Verification** provides the explicit live
evidence path for the Phase 19 adapter. `pc-agent providers
verify-anthropic` sends only a fixed public connectivity prompt in one
direct, bounded Anthropic request—no fallback, router retry, image, file or
task text—and retains a secret-safe report for
`providers verify-anthropic-report`. See `docs/phase53.md`.

## Live GNOME Keyring/KWallet Evidence (Phase 54)

On a signed-in Linux desktop, `pc-agent credentials
linux-secret-service-acceptance` proves that `org.freedesktop.secrets` is
owned by GNOME Keyring or KWallet/KSecretService, round-trips and deletes
one isolated random probe through `secret-tool`, and confirms every real
provider key is unchanged. The retained self-hashed report can be checked
with `credentials linux-secret-service-acceptance-verify`. See
`docs/phase54.md`.

## Windows HTTPS Update-Feed Hosting (Phase 55)

Use `pc-agent-release-admin build-hosting` to wrap a verified Phase 40
feed package in a Windows operational deployment, then run the built-in
HTTPS host as a scheduled task or deploy the IIS profile. After the
operator's DNS, firewall and trusted certificate are in place,
`pc-agent update hosting-check` downloads and verifies the live feed,
signature, portable ZIP and cache policy without staging or applying it;
`hosting-check-verify` revalidates the retained report offline. Linux and
macOS profiles are named in the registry but remain planned in this
Windows-first phase. See `docs/phase55.md`.

## Background Auto-Upgrade (Phase 56)

After the real Phase 55 host passes `hosting-check`, separately set
`PC_AGENT_AUTO_UPGRADE=1` with the signed HTTPS feed and release key.
Use `pc-agent update auto-status`, `auto-run` and `auto-verify-report` in
the foreground first, then `auto-install-schedule` to register the per-user
Windows task. The default Background auto-upgrade mode is `stage-only`:
it downloads, verifies and stages, but never applies.
`apply-when-idle` may apply only inside the maintenance window with no
active task, enough operating-system idle time, AC power and a verified
signature. Windows uses Win32 idle/power APIs; Linux uses the Phase 60
logind and power-supply sensors.
State and every run report are retained under `data\updates`. Linux
systemd-user scheduler is implemented in Phase 59; the macOS LaunchAgent
profile remains planned. See
`docs/phase56.md`.

## Live Debian/Ubuntu APT Evidence (Phase 57)

On a Debian or Ubuntu host where `self-healing-pc-agent` is not already
installed, `pc-agent release debian-repository-acceptance` performs a
guarded live APT install from the verified Phase 50 local repository,
checks the exact APT candidate and dpkg state, verifies all installed
files against the package manifest, then purges only the test package.
`debian-repository-acceptance-verify` revalidates the retained report
offline. It may install declared distribution dependencies and does not
auto-remove them. See `docs/phase57.md`.

## Windows Auto-Upgrade Lock Hotfix (Phase 58)

Repeated Phase 56 Windows worker runs now lock and unlock the persistent
`auto-upgrade.lock` first byte consistently. This removes the second-run
`msvcrt.locking` `PermissionError` seen in the two real Windows Phase 56
tests, without changing the signed-feed, staging or idle-apply policy.
See `docs/phase58.md`.

## Linux systemd-user Auto-Upgrade Scheduler (Phase 59)

On Linux, the same `update auto-install-schedule`,
`auto-schedule-status` and `auto-uninstall-schedule` commands manage a
per-user systemd one-shot service and persistent hourly timer. It wakes
the unchanged signed worker, refuses unmanaged same-named units, and
rolls unit files back if enable fails. Linux `stage-only` remains the
recommended first mode; Phase 60 supplies the sensors required by
`apply-when-idle`. macOS LaunchAgent remains planned. See
`docs/phase59.md`.

## Linux Idle and AC Power Sensors (Phase 60)

Linux `apply-when-idle` reads interactive systemd-logind session idle
hints for the current UID and kernel `/sys/class/power_supply` state,
falling back to logind `OnExternalPower` only when sysfs is inconclusive.
Missing, malformed or ambiguous readings stay unknown and defer the apply
gate; the same unknown-is-not-permission rule now applies on every
platform. See `docs/phase60.md`.

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
`online-stage` itself never applies it. Phase 56 later adds a separate,
default-off background worker and scheduler, but no planner tool. See
`docs/phase24.md` and `docs/phase56.md`.

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
`docs/phase16.md`, `docs/phase17.md`, `docs/phase18.md`, `docs/phase19.md`, `docs/phase20.md`, `docs/phase21.md`, `docs/phase22.md`, `docs/phase23.md`, `docs/phase24.md`, `docs/transformation_phase1.md` … `docs/transformation_phase12.md`, `docs/providers.md`, `docs/tools.md`, `docs/tool_builder.md`,
`docs/recovery.md`, `docs/architecture.md`, and `CHANGELOG.md` for detailed
contracts and honest limitations.
