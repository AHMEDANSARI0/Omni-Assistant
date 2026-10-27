# Self-healing and recovery — Phase 3, extended in Phases 4-5

Phase 3 adds **deterministic, bounded** recovery to orchestrated tasks. It
does not ask a model to invent recovery actions, execute shell commands, edit
its own source code, bypass approvals or weaken policy. Phase 4 keeps those
rules and extends the safe workspace-output/input corrections to the new
orchestrated local Office plans; it also adds copy-friendly task aliases.
Phase 5's GUI Tasks tab calls the same recovery service and therefore keeps
the same duplicate-successful-clone guard and audit records.

## Scope

Automatic recovery wraps `pc-agent run ...`, `pc-agent task run latest`, and
the execution started by `pc-agent task recover latest-failed` (or the same
commands with a real task ID). Those flows use the Phase 4 local allow-list:

- `pdf.merge_folders`, `pdf.merge`, `pdf.verify`, `pdf.inspect`
- `workspace.list_files`
- `office.inspect`, `office.extract_text`, `office.csv_inspect`
- `office.csv_to_json`, `office.json_to_csv`, `office.csv_to_xlsx`
- `desktop.system_info`

Direct one-shot commands such as `pc-agent pdf merge-folders ...`,
`pc-agent office ...`, `pc-agent web fetch ...`, `pc-agent desktop ...` and
`pc-agent tools invoke ...` intentionally keep single-attempt behaviour.
Web fetching and desktop opening are never generic orchestrated tools.

## Automatic recovery loop

```text
plan -> approve exact external roots if any -> attempt 1 -> verify
                                           | failure
                                           v
                              deterministic local diagnosis
                                           |
             safe rule + attempts remain   |   no safe rule / limit reached
                                           v
                         RETRYING -> optional revised plan
                              -> fresh approval if external reads remain
                              -> bounded backoff -> next attempt -> verify
```

Every attempt is stored in SQLite `recovery_attempts`, including the plan
before the attempt and the revised plan after a retryable failure. A task can
only become `succeeded` after the final attempt also passes deterministic
verification.

### Rules

| Diagnosis | Action | Boundaries |
|---|---|---|
| Planned PDF/Office output already exists | Retry to a fresh `*-recovered...` workspace output with the same suffix | The existing file is never overwritten |
| Locked/unavailable workspace output (`WinError 5`, access denied or `PermissionError`) | Retry to a fresh workspace output | Only for orchestrated output-producing PDF/Office plans |
| Workspace input is missing | Retry with a corrected path | Only when exactly one same-named file/folder is found under the workspace; ambiguous paths are never guessed |
| Input is missing with no unique correction | End as `needs_input` | No speculative substitution |
| Generated output fails deterministic re-verification | Retry to a fresh output | The new output is verified again |
| File-list verification observes a changing workspace | Retry the exact read-only plan | Bounded like every other retry |
| Timeout, temporary, locked, sharing-violation, busy or connection-reset style tool failure | Retry the exact validated plan | Backoff is bounded |
| Policy or sensitive-path failure | Do not retry | Healing never weakens policy |
| Approval denied/unavailable | Do not retry automatically | A human decision is required |
| Corrupt, encrypted, insufficient-valid-input, schema or unknown failure | Do not retry automatically | Repeating the same call would not repair the cause |

Path corrections are disabled when a plan has approved external read roots;
substituting a workspace path would silently change the approved scope. A
merge output may still change inside the workspace, but the revised exact
plan is presented for approval again.

### Limits and configuration

Defaults:

```dotenv
PC_AGENT_MAX_RECOVERY_ATTEMPTS=2
PC_AGENT_RECOVERY_BACKOFF_SECONDS=0.25
PC_AGENT_RECOVERY_STALE_AFTER_MINUTES=10
```

- `PC_AGENT_MAX_RECOVERY_ATTEMPTS`: healing retries **after** the first
  attempt; allowed `0`–`3`.
- `PC_AGENT_RECOVERY_BACKOFF_SECONDS`: first retry delay; allowed `0`–`5`.
  Later delays double but are capped at five seconds.
- `PC_AGENT_RECOVERY_STALE_AFTER_MINUTES`: active-task inactivity threshold;
  allowed `1`–`10080`.

Per-command overrides:

```powershell
pc-agent run "Merge PDFs from Folder A and Folder B" --max-recovery-attempts 1
pc-agent run "Merge PDFs from Folder A and Folder B" --no-recovery
pc-agent task run latest --no-recovery
```

Execution reports (human and JSON) include each recovery attempt, whether a
retry ultimately recovered the task, and the source task ID for a clone.

## Diagnosis without changes

```powershell
pc-agent task diagnose latest-failed
pc-agent task diagnose latest-failed --json
pc-agent recovery show latest-failed
pc-agent task list --status failed
```

Diagnosis is read-only. It reports the task status/error, stored plan (or why
that plan is invalid), latest tool invocation, inferred failure category,
safe action, prior checkpoints and recovery links.

## Manual recovery

```powershell
pc-agent task recover latest-failed
pc-agent task recover latest-failed --input-folder "Inputs" --output fixed.pdf
pc-agent task recover latest-failed --force
```

Aliases resolve to the newest matching task and print the real ID they
selected. `latest-<status>` accepts statuses such as `latest-failed` and
`latest-needs_input`. Never type an angle-bracket placeholder literally:
Windows shells interpret `<` and `>` as redirection before `pc-agent` starts.
Failed human-readable reports print the exact diagnose/recover commands for
their own task ID.

Behaviour depends on the source status:

- `queued`, `needs_input`, `waiting_for_approval`: resume the same task. A
  valid stored plan is reused (except `needs_input`, which replans using any
  newly supplied options).
- `failed`: create a linked clone. A valid stored plan is copied; a missing
  or invalid stored plan causes a fresh plan from the original instruction.
- `planning`, `running`, `verifying`, `retrying`: refuse while the task looks
  recently active. If it has passed the stale threshold—or `--force` is used
  after confirming no other process owns it—mark the source `failed` as
  interrupted and run a linked clone.
- `succeeded`, `cancelled`: refuse; create a new task instead.

Links are stored in `task_recovery_links`. A task may be manually cloned
through at most three recovery generations; after that, fix the underlying
cause and create a fresh task. Since Phase 4.1, manual recovery stops without
creating another task if one linked clone already succeeded; diagnosis
reports that successful recovery clone instead of proposing a duplicate.

## Health scan

```powershell
pc-agent recovery scan
pc-agent recovery scan --older-than-minutes 30 --json
pc-agent recovery list
pc-agent doctor
```

The scan reports active tasks whose `updated_at` is older than the threshold
and workspace files matching the PDF merger's own temporary pattern
(`.<name>.tmp-<32 hex>.pdf`) with an old modification time.

**The scan never deletes or modifies files.** A temporary file might still
belong to a process outside this database, so cleanup remains an explicit
human action after confirming no merge is running.

`doctor` prints the recovery-attempt count, stale-task count, orphan-temp
count and whether the bundled dynamic-tool example arguments file is present.
It reports `Status: ATTENTION - run: pc-agent recovery scan` when stale items
exist; otherwise it reports `Status: OK`.

## Phase 7: resume in place vs recover clone

Phase 7 adds `pc-agent task resume` for **interrupted but not failed**
tasks. It is different from manual recovery: resume keeps the same task ID,
reuses a validated step checkpoint when one exists, and re-runs verification
plus all acceptance criteria. It refuses tasks updated within the last 120
seconds (possible live worker) unless `--force`, revalidates the stored plan
against the live registry and policy, and re-asks approval unless an earlier
approved identical scope plus a valid checkpoint prove the approved work
already ran. If a checkpoint artifact is gone, the checkpoint is invalidated
with an event and the tool runs again. Failed tasks still use
diagnose/recover clones so their failure history remains intact.

`pc-agent task cancel` cooperatively stops a task at the next stage
boundary. Queued tasks cancel immediately; running tasks record a durable
request that any process can set. Cancellation never interrupts a tool
mid-call and never deletes workspace files.

## Honest boundaries

- Recovery rules cover known patterns in the orchestrated local allow-list,
  not arbitrary applications or future tools automatically. They do not make
  direct web/desktop-open commands retryable.
- A crash in the middle of a tool call can leave an attempt in `started` and
  the task active. That is detectable by the stale scan, but this build does
  not include a background daemon that continuously repairs tasks while the
  app is closed.
- Recovery does not rewrite corrupt PDFs/Office packages, guess passwords,
  restore deleted source files, delete orphan files, modify dynamic-tool
  code, or repair the Python runtime/database binary.
- AI providers are not consulted by automatic recovery. An explicitly
  requested `--use-ai` manual recovery can still produce a fresh plan, but it
  passes through the same allow-list, policy, approval and verification.
