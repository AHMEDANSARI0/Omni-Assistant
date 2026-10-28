"""Structured local Git tools (Phase 12).

Each tool builds one fixed argv for the allow-listed ``git`` executable -
callers can choose the repository, paths and commit message, never flags.
Network operations (clone/fetch/pull/push) are intentionally absent: they
need credentials and happen outside this phase's trust boundary. All tools
fail closed while approved subprocess execution is disabled.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..commands import CommandResult, require_command_policy, run_command
from ..errors import ToolError
from ..policies.policy import Action
from .registry import ToolContext, ToolHandler, ToolRegistry, ToolSpec
from .workspace_tools import _resolve_allowed, relative_posix

_DIFF_SUMMARY = re.compile(
    r"(\d+) files? changed"
    r"(?:, (\d+) insertions?\(\+\))?"
    r"(?:, (\d+) deletions?\(-\))?"
)
_AHEAD_BEHIND = re.compile(r"ahead (\d+)")
_BEHIND = re.compile(r"behind (\d+)")


def _repo_dir(context: ToolContext, raw: str | None, *, must_exist: bool = True) -> Path:
    target = _resolve_allowed(context, raw or ".", Action.LIST_FILES)
    if must_exist and not target.is_dir():
        raise ToolError(f"Repository folder was not found: {raw or '.'}")
    return target


def _git(context: ToolContext, repo: Path, argv: list[str]) -> CommandResult:
    require_command_policy(context.policy_engine, repo)
    result = run_command(
        executable="git",
        argv=["-C", str(repo), *argv],
        cwd=repo,
        timeout_seconds=context.command_timeout_seconds,
        allowlist=context.command_allowlist,
    )
    if result.timed_out:
        raise ToolError(
            f"git {argv[0]} timed out after "
            f"{context.command_timeout_seconds:.0f}s and was killed."
        )
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout).strip()[:500]
        raise ToolError(f"git {argv[0]} failed (exit {result.exit_code}): {detail}")
    return result


def _git_soft(context: ToolContext, repo: Path, argv: list[str]) -> CommandResult:
    """Run git without raising on a non-zero exit (probes only)."""

    require_command_policy(context.policy_engine, repo)
    return run_command(
        executable="git",
        argv=["-C", str(repo), *argv],
        cwd=repo,
        timeout_seconds=context.command_timeout_seconds,
        allowlist=context.command_allowlist,
    )


def parse_status_porcelain(text: str) -> dict[str, Any]:
    branch: str | None = None
    ahead = 0
    behind = 0
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            header = line[3:]
            if header.startswith("No commits yet on "):
                branch = header.removeprefix("No commits yet on ").strip() or None
            elif header.startswith("HEAD"):
                branch = None
            else:
                branch = header.split("...", 1)[0].split(" [", 1)[0].strip() or None
            ahead_match = _AHEAD_BEHIND.search(header)
            behind_match = _BEHIND.search(header)
            ahead = int(ahead_match.group(1)) if ahead_match else 0
            behind = int(behind_match.group(1)) if behind_match else 0
            continue
        if len(line) < 4:
            continue
        index_code, worktree_code, path = line[0], line[1], line[3:]
        if index_code == "?" and worktree_code == "?":
            untracked.append(path)
            continue
        if index_code not in " ?":
            staged.append(path)
        if worktree_code not in " ?":
            unstaged.append(path)
    return {
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "clean": not (staged or unstaged or untracked),
    }


def _status(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    repo = _repo_dir(context, arguments.get("repository"))
    result = _git(context, repo, ["status", "--porcelain=v1", "--branch"])
    parsed = parse_status_porcelain(result.stdout)
    return {"repository": relative_posix(context, repo), **parsed}


def _log(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    repo = _repo_dir(context, arguments.get("repository"))
    count = int(arguments.get("max_count") or 10)
    head = _git_soft(context, repo, ["rev-parse", "--verify", "--quiet", "HEAD"])
    if head.timed_out:
        raise ToolError("git rev-parse timed out.")
    if head.exit_code != 0:
        return {"repository": relative_posix(context, repo), "commits": [], "count": 0}
    result = _git(
        context,
        repo,
        [
            "--no-pager",
            "log",
            f"--max-count={count}",
            "--pretty=format:%H%x1f%h%x1f%s%x1f%an%x1f%ad",
            "--date=iso",
        ],
    )
    commits = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        commits.append(
            {
                "hash": parts[0],
                "short_hash": parts[1],
                "subject": parts[2],
                "author": parts[3],
                "date": parts[4],
            }
        )
    return {
        "repository": relative_posix(context, repo),
        "commits": commits,
        "count": len(commits),
    }


def _diff_summary(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    repo = _repo_dir(context, arguments.get("repository"))
    staged = bool(arguments.get("staged"))
    argv = ["diff", "--stat"] + (["--cached"] if staged else [])
    result = _git(context, repo, argv)
    files_changed: int | None = None
    insertions: int | None = None
    deletions: int | None = None
    match = _DIFF_SUMMARY.search(result.stdout)
    if match is not None:
        files_changed = int(match.group(1))
        insertions = int(match.group(2)) if match.group(2) else 0
        deletions = int(match.group(3)) if match.group(3) else 0
    elif not result.stdout.strip():
        files_changed = 0
        insertions = 0
        deletions = 0
    return {
        "repository": relative_posix(context, repo),
        "staged": staged,
        "stat": result.stdout,
        "files_changed": files_changed,
        "insertions": insertions,
        "deletions": deletions,
    }


def _init(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    repo = _repo_dir(context, arguments.get("directory"), must_exist=False)
    if (repo / ".git").exists():
        raise ToolError(f"Already a git repository: {relative_posix(context, repo)}")
    repo.mkdir(parents=True, exist_ok=True)
    _git(context, repo, ["init", "-q"])
    return {"repository": relative_posix(context, repo), "initialized": True}


def _checked_repo_paths(repo: Path, raw_paths: Any) -> list[str]:
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ToolError("At least one repository-relative path is required.")
    paths: list[str] = []
    for item in raw_paths:
        text = str(item).strip()
        candidate = Path(text)
        if not text or candidate.is_absolute() or ".." in candidate.parts:
            raise ToolError(f"Path must stay inside the repository: {text!r}")
        if not (repo / candidate).exists():
            raise ToolError(f"Path was not found in the repository: {text}")
        paths.append(candidate.as_posix())
    return paths


def _add(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    repo = _repo_dir(context, arguments.get("repository"))
    paths = _checked_repo_paths(repo, arguments.get("paths"))
    _git(context, repo, ["add", "--", *paths])
    return {"repository": relative_posix(context, repo), "added": paths}


def _commit(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    repo = _repo_dir(context, arguments.get("repository"))
    message = str(arguments.get("message") or "").strip()
    if not message or "\n" in message or "\r" in message:
        raise ToolError("Commit message must be one non-empty line.")
    status = parse_status_porcelain(
        _git(context, repo, ["status", "--porcelain=v1", "--branch"]).stdout
    )
    if not status["staged"]:
        raise ToolError("Nothing is staged for commit; stage files with git.add first.")
    _git(context, repo, ["commit", "-q", "-m", message])
    head = _git(context, repo, ["rev-parse", "--short", "HEAD"])
    return {
        "repository": relative_posix(context, repo),
        "commit": head.stdout.strip(),
        "message": message,
    }


_REPO_INPUT = {"type": "string", "minLength": 1, "maxLength": 500}
_REPO_OUTPUT = {"type": "string"}
_PATH_LIST = {
    "type": "array",
    "items": {"type": "string", "maxLength": 500},
    "maxItems": 1000,
}


def _specs() -> list[tuple[ToolSpec, ToolHandler]]:
    status_output = {
        "type": "object",
        "properties": {
            "repository": _REPO_OUTPUT,
            "branch": {"type": ["string", "null"]},
            "ahead": {"type": "integer"},
            "behind": {"type": "integer"},
            "staged": _PATH_LIST,
            "unstaged": _PATH_LIST,
            "untracked": _PATH_LIST,
            "clean": {"type": "boolean"},
        },
        "required": [
            "repository", "branch", "ahead", "behind",
            "staged", "unstaged", "untracked", "clean",
        ],
    }
    return [
        (
            ToolSpec(
                id="git.status",
                name="Git status",
                version="1.0.0",
                description="Report branch and staged/unstaged/untracked paths of a workspace Git repository (read-only).",
                input_schema={"type": "object", "properties": {"repository": _REPO_INPUT}},
                output_schema=status_output,
                permissions=(Action.RUN_SHELL, Action.LIST_FILES),
                timeout_seconds=120.0,
            ),
            _status,
        ),
        (
            ToolSpec(
                id="git.log",
                name="Git log",
                version="1.0.0",
                description="List recent commits (hash, subject, author, date) of a workspace Git repository (read-only).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "repository": _REPO_INPUT,
                        "max_count": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "repository": _REPO_OUTPUT,
                        "commits": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "hash": {"type": "string"},
                                    "short_hash": {"type": "string"},
                                    "subject": {"type": "string"},
                                    "author": {"type": "string"},
                                    "date": {"type": "string"},
                                },
                                "required": ["hash", "short_hash", "subject", "author", "date"],
                            },
                            "maxItems": 50,
                        },
                        "count": {"type": "integer"},
                    },
                    "required": ["repository", "commits", "count"],
                },
                permissions=(Action.RUN_SHELL, Action.LIST_FILES),
                timeout_seconds=120.0,
            ),
            _log,
        ),
        (
            ToolSpec(
                id="git.diff_summary",
                name="Git diff summary",
                version="1.0.0",
                description="Summarize unstaged (or staged) changes as a diffstat (read-only).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "repository": _REPO_INPUT,
                        "staged": {"type": "boolean"},
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "repository": _REPO_OUTPUT,
                        "staged": {"type": "boolean"},
                        "stat": {"type": "string", "maxLength": 70000},
                        "files_changed": {"type": ["integer", "null"]},
                        "insertions": {"type": ["integer", "null"]},
                        "deletions": {"type": ["integer", "null"]},
                    },
                    "required": [
                        "repository", "staged", "stat",
                        "files_changed", "insertions", "deletions",
                    ],
                },
                permissions=(Action.RUN_SHELL, Action.LIST_FILES),
                timeout_seconds=120.0,
            ),
            _diff_summary,
        ),
        (
            ToolSpec(
                id="git.init",
                name="Git init",
                version="1.0.0",
                description="Initialize a new Git repository in a workspace folder (refuses existing repositories).",
                input_schema={"type": "object", "properties": {"directory": _REPO_INPUT}},
                output_schema={
                    "type": "object",
                    "properties": {
                        "repository": _REPO_OUTPUT,
                        "initialized": {"type": "boolean"},
                    },
                    "required": ["repository", "initialized"],
                },
                permissions=(Action.RUN_SHELL, Action.WRITE_FILE),
                timeout_seconds=120.0,
            ),
            _init,
        ),
        (
            ToolSpec(
                id="git.add",
                name="Git add",
                version="1.0.0",
                description="Stage explicit repository-relative paths for the next commit.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "repository": _REPO_INPUT,
                        "paths": {**_PATH_LIST, "minItems": 1, "maxItems": 100},
                    },
                    "required": ["paths"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "repository": _REPO_OUTPUT,
                        "added": {**_PATH_LIST, "maxItems": 100},
                    },
                    "required": ["repository", "added"],
                },
                permissions=(Action.RUN_SHELL, Action.WRITE_FILE),
                timeout_seconds=120.0,
            ),
            _add,
        ),
        (
            ToolSpec(
                id="git.commit",
                name="Git commit",
                version="1.0.0",
                description="Commit staged changes with a single-line message (no amend, no flags).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "repository": _REPO_INPUT,
                        "message": {"type": "string", "minLength": 1, "maxLength": 500},
                    },
                    "required": ["message"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "repository": _REPO_OUTPUT,
                        "commit": {"type": "string"},
                        "message": {"type": "string"},
                    },
                    "required": ["repository", "commit", "message"],
                },
                permissions=(Action.RUN_SHELL, Action.WRITE_FILE),
                timeout_seconds=120.0,
            ),
            _commit,
        ),
    ]


def register_git_tools(registry: ToolRegistry) -> None:
    for spec, handler in _specs():
        registry.register(spec, handler)
