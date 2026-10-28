# PHASE 12 - Approved subprocess & Git

Phase 12 lets the agent run **specific, approved local programs** for the
first time: local Git operations and one general allow-listed command
runner. Execution is off by default and every layer fails closed.

## Turning it on

In `.env`:

```
PC_AGENT_ALLOW_SUBPROCESS=1
# Optional overrides:
# PC_AGENT_COMMAND_ALLOWLIST=git,python,python3,py,node,npm
# PC_AGENT_COMMAND_TIMEOUT_SECONDS=60
```

With the gate off (the default), all seven new tools refuse to run with a
clear policy error. `pc-agent doctor` shows the gate state, the
allow-list and whether `git` is on PATH. Git must be installed separately
for the `git.*` tools; commits use **your own** configured Git identity
(`git config user.email` / `user.name`) — the agent never invents one.

## What can run

| Tool | What it does | Approval |
|---|---|---|
| `git.status` | Branch, staged/unstaged/untracked lists, ahead/behind | No (read-only) |
| `git.log` | Last 1-50 commits (hash, subject, author, date) | No |
| `git.diff_summary` | Unstaged changes: files changed, +/- lines | No |
| `git.init` | `git init` in a workspace folder (refuses inside a repo) | Yes, every time |
| `git.add` | Stage explicit existing paths | Yes, every time |
| `git.commit` | Commit staged changes with a single-line message | Yes, every time |
| `commands.run` | Run one allow-listed program with fixed arguments | Yes, every time |

All repositories must be inside the workspace. There is no clone,
fetch, pull, push, branch, checkout, merge, stash or reset — no network
Git at all.

## Safety model

- **Never a shell.** Commands run as an argv array (`shell=False`).
  Pipes, redirection, `&&` and variable expansion do not work because
  nothing interprets them — they would be literal argument text.
- **Allow-listed bare names only.** The program must be a bare name like
  `python` or `git` (no paths), present on `PC_AGENT_COMMAND_ALLOWLIST`
  and found on PATH.
- **Windows shims are handled safely.** npm-style `.cmd`/`.bat` shims run
  through `cmd.exe /d /s /c`, but only when every argument is free of
  `% ^ & | < > "` and newlines; otherwise the command is refused.
- **Approvals bind to exact arguments.** Approving
  `python -m pytest -q` does not approve any other command, and workflow
  replay or task resume always asks again. Git writes behave the same.
- **Scrubbed environment.** Children inherit only PATH/system essentials
  — your API keys and other `.env` secrets are never forwarded.
- **Bounded.** Hard timeout with process kill (default 60s, per-command
  1-300s), stdin closed, stdout/stderr capped at 64 KiB each with a
  `truncated` flag.
- **Verified, never retried.** Git state is re-read independently
  (a commit must match HEAD and its subject line). Commands and Git
  writes are excluded from automatic recovery retries, because they are
  not idempotent. Non-zero exit codes still return the full output so
  you can see exactly what happened.

`commands.run` can still do whatever the approved program itself can do
(e.g. `python` code). The approval dialog shows the exact program,
arguments and folder — read it. Note that an approved program can write
outside the workspace; that is why approval is fresh for every run.

## Ways to use it

As tasks (deterministic planner; `--target` selects the repo folder,
default `.` = workspace root):

```powershell
pc-agent run "Show git status" --target my-project
pc-agent run "Initialize a git repository" --target my-project
pc-agent run "Git add the file" --target my-project --input-file readme.txt
pc-agent run "Git commit 'Initial import'" --target my-project
pc-agent run "Run the test suite" --command "python -m pytest -q" --target my-project
```

On an interactive terminal, approvals take a **single `Y` keypress**
(no Enter needed; `N` or Ctrl+C cancels). Text typed or pasted before
the prompt — for example when you paste several commands at once — is
discarded with a note, and even a pasted `--yes` arriving during the
prompt is detected as a paste burst and ignored, so only a deliberate
lone `Y` ever approves. Pressing Enter just shows a hint. With piped
(non-interactive) stdin the prompt instead asks you to type the word
`APPROVE`, re-asking after empty/wrong answers. Use `--yes` to approve
without any prompt; a denied prompt prints the exact `--yes` command
to re-run.

`--command` is one quoted string; on Windows prefer forward slashes in
any paths inside it. `--command-timeout 120` raises the timeout for that
task. Commands combine with Phase 11 chains, e.g.
`pc-agent run "Show git status then show the git log" --target my-project`.

Directly (writes prompt for APPROVE unless `--yes`):

```powershell
pc-agent git status --repo my-project
pc-agent git log --repo my-project
pc-agent git diff --repo my-project
pc-agent git init my-project
pc-agent git add readme.txt --repo my-project
pc-agent git commit -m "Initial import" --repo my-project
```

In the GUI, the Tasks tab has a **Git Status...** button (choose a
workspace folder; read-only). Running commands or Git writes from the
GUI text prompt goes through the same approval dialog as the CLI.

AI planning (Gemini/OpenRouter/…) may only propose the **read-only**
Git tools. It can never propose `commands.run` or a Git mutation; those
remain deterministic-only and user-approved.

## Known limitations

- No interactive commands (stdin is closed); anything waiting for input
  will hang until the timeout kills it.
- On Windows, because commands are passed as argv, quote arguments that
  contain spaces; arguments containing cmd-special characters cannot run
  through `.cmd` shims.
- Multi-step `then` chains cannot feed command stdout into later steps
  (bindings carry file paths only, as in Phase 11).
