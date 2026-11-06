# Windows Acceptance Pack

Run this pack on the real Windows target after applying the latest patcher.
Record pass/fail, exact test counts, Windows edition/build, Python version
and application version. Never paste API keys, `.env` contents, license keys,
release private keys or document contents into evidence.

## 1. Installation and core suite

From the project folder in PowerShell:

1. Reinstall the editable project with development dependencies.
2. Run the full pytest suite.
3. Run Ruff.
4. Record `pc-agent --version`.
5. Run `pc-agent doctor` and confirm the Transformation Phase 1 label.
6. Run `pc-agent gui --check`.
7. Run `pc-agent release audit` and record failures/warnings/check count.
8. Run the deterministic PDF demo and Folder A/B merge-then-extract task.
9. Confirm `pc-agent workflow list` shows the new verified workflow.

## 2. Real GUI

1. Launch the GUI from a signed-in desktop session.
2. Confirm all eight tabs render without clipping at the user display scale.
3. Run one harmless deterministic task.
4. Trigger one approval dialog and deny it; confirm no action runs.
5. Trigger the same approval and approve it; confirm one exact action runs.
6. Start a longer harmless task, request cancellation, and record whether it
   stops at the next stage boundary.

## 3. Credential Manager

1. Run credential status without printing values.
2. Store a disposable test provider value through the hidden prompt.
3. Confirm status lists the provider as stored.
4. Delete the disposable value.
5. Confirm no value appears in console output, diagnostics or project files.

## 4. Browser and desktop automation

Use a throwaway browser profile and harmless test applications only.

1. Start Edge/Chrome with a temporary profile and local debug port.
2. Confirm browser status/snapshot.
3. Approve one exact click/type on a disposable page and verify read-back.
4. Close the debug browser afterward.
5. With desktop automation explicitly enabled, inspect Notepad or another
   harmless test app.
6. Approve one exact focus/type action and verify read-back.
7. Confirm password fields are refused.

## 5. Installer and updates

1. Compile the Inno installer source with a locally installed Inno Setup.
2. Install into an isolated per-user acceptance location.
3. Launch CLI and GUI shortcuts.
4. Upgrade over the same install and confirm user data is preserved.
5. Uninstall and confirm `.env`, credentials, workspace and data policy match
   the documented behavior.
6. With a test signing key kept outside the project, stage a signed update,
   apply it in an isolated copy, then roll it back.
7. Register the Windows auto-upgrade scheduled task, inspect status, run one
   harmless due/not-due pass, then unregister it if not intended for use.

## 6. Evidence to return

Return only:

- Windows edition/build and Python version;
- application version;
- pytest/Ruff counts;
- doctor/GUI-check/release-audit summaries;
- pass/fail for each numbered section;
- redacted error text for failures.

Do not return secrets, private documents, screenshots containing personal
data, or full environment dumps.

## 7. Recording the evidence (0.80.1)

1. On the Windows machine, run the gate. It runs the suite, the release audit,
   the end-to-end release chain, a real plugin worker, file-permission hardening
   and the measured accessibility report, and writes
   `workspace\windows-acceptance.json`:

```
pc-agent release evidence
```

2. Read the gate lines. Every line is real state: `[ok]`, `[FAIL]` or `[skip]`.
   `[skip]` means the gate did not run and the report cannot pass.

3. If every gate is `[ok]`, record the sealed report into the install:

```
pc-agent release evidence record
```

4. Confirm the recorded state, on any platform:

```
pc-agent release evidence
pc-agent doctor
```

`doctor` then prints
`Hardening: ...; windows-acceptance=verified:YYYY-MM-DD` instead of
`windows-acceptance=pending_user_windows_evidence`.

### What the record proves, and what it does not

- It proves: on that machine, on that date, with that Python and that version,
  the suite passed, the audit had zero failures, the end-to-end release chain
  passed, the plugin sandbox really started an isolated worker, owner-only file
  hardening worked with the host's real mechanism, and the accessibility report
  was measured.
- It is **tamper-evident, not tamper-proof**: the body hash catches an edited file,
  but the file is written by the same user account that runs the assistant, so it
  is a self-attestation, not a cryptographic attestation by a third party. The
  verifier says so and `SECURITY_REPORT.md` records the limit.
- Recording a report from a *different* version is refused, so the evidence cannot
  silently describe an older build.
- To re-verify an older file without recording it:
  `pc-agent release evidence verify workspace\windows-acceptance.json`.

