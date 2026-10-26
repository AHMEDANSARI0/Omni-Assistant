"""Safe, workspace-scoped file tools.

Every handler resolves its target inside the configured workspace and asks
the PolicyEngine for a fresh decision at execution time. Metadata on the tool
spec never grants permission by itself.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..errors import ToolError
from ..policies.policy import Action, ActionRequest
from .registry import ToolContext, ToolHandler, ToolRegistry, ToolSpec


def _resolve_allowed(context: ToolContext, raw_path: str, action: Action) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = context.workspace_dir / candidate
    target = candidate.resolve()
    decision = context.policy_engine.evaluate(ActionRequest(action, target))
    if not decision.allowed:
        raise ToolError(f"Policy {decision.outcome.value}: {decision.reason}")
    return target


def relative_posix(context: ToolContext, target: Path) -> str:
    """Workspace-relative path when possible; approved external paths stay absolute."""
    resolved = target.resolve()
    try:
        relative = resolved.relative_to(context.workspace_dir)
    except ValueError:
        return str(resolved)
    return "." if str(relative) == "." else relative.as_posix()


def _list_files(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    raw_directory = str(arguments.get("directory") or ".")
    pattern = str(arguments.get("pattern") or "*")
    recursive = bool(arguments.get("recursive", False))
    max_results = int(arguments.get("max_results", 200))

    if pattern in {"", ".", ".."} or "/" in pattern or "\\" in pattern:
        raise ToolError("Pattern must be a filename pattern without path separators.")
    if max_results < 1 or max_results > 1000:
        raise ToolError("max_results must be between 1 and 1000.")

    directory = _resolve_allowed(context, raw_directory, Action.LIST_FILES)
    if not directory.is_dir():
        raise ToolError(f"Folder not found: {relative_posix(context, directory)}")

    iterator = directory.rglob(pattern) if recursive else directory.glob(pattern)
    files = sorted(
        (
            candidate.resolve()
            for candidate in iterator
            # Re-resolve each result so a symlink/junction cannot escape the
            # directory that policy just approved.
            if candidate.is_file() and candidate.resolve().is_relative_to(directory)
        ),
        key=lambda candidate: relative_posix(context, candidate).casefold(),
    )
    shown = files[:max_results]
    return {
        "directory": relative_posix(context, directory),
        "files": [relative_posix(context, file_path) for file_path in shown],
        "count": len(shown),
        "truncated": len(files) > len(shown),
    }


def _read_text(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    raw_path = str(arguments["path"])
    max_chars = int(arguments.get("max_chars", 100_000))
    if max_chars < 1 or max_chars > 200_000:
        raise ToolError("max_chars must be between 1 and 200000.")

    target = _resolve_allowed(context, raw_path, Action.READ_FILE)
    if not target.is_file():
        raise ToolError(f"File not found: {relative_posix(context, target)}")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError("File is not valid UTF-8 text.") from exc
    except OSError as exc:
        raise ToolError(f"Could not read file: {target.name}") from exc

    return {
        "path": relative_posix(context, target),
        "content": content[:max_chars],
        "truncated": len(content) > max_chars,
        "size_bytes": target.stat().st_size,
    }


def _write_text(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    raw_path = str(arguments["path"])
    content = str(arguments["content"])
    create_parents = bool(arguments.get("create_parents", True))

    target = _resolve_allowed(context, raw_path, Action.WRITE_FILE)
    if target.exists():
        raise ToolError(
            "File already exists. This tool never overwrites; choose a new output path."
        )
    if create_parents:
        target.parent.mkdir(parents=True, exist_ok=True)
    elif not target.parent.is_dir():
        raise ToolError(f"Parent folder not found: {relative_posix(context, target.parent)}")

    data = content.encode("utf-8")
    try:
        with target.open("xb") as handle:  # xb = exclusive create, cannot clobber a race winner.
            handle.write(data)
    except FileExistsError as exc:
        raise ToolError(
            "File already exists. This tool never overwrites; choose a new output path."
        ) from exc
    except OSError as exc:
        raise ToolError(f"Could not write file: {target.name}") from exc

    return {
        "path": relative_posix(context, target),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _specs() -> list[tuple[ToolSpec, ToolHandler]]:
    return [
        (
            ToolSpec(
                id="workspace.list_files",
                name="List workspace files",
                version="1.0.0",
                description="List files inside one workspace folder in stable sorted order.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "directory": {"type": "string"},
                        "pattern": {"type": "string"},
                        "recursive": {"type": "boolean"},
                        "max_results": {"type": "integer"},
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "directory": {"type": "string"},
                        "files": {"type": "array", "items": {"type": "string"}},
                        "count": {"type": "integer"},
                        "truncated": {"type": "boolean"},
                    },
                    "required": ["directory", "files", "count", "truncated"],
                },
                permissions=(Action.LIST_FILES,),
            ),
            _list_files,
        ),
        (
            ToolSpec(
                id="workspace.read_text",
                name="Read workspace text file",
                version="1.0.0",
                description="Read a UTF-8 text file from the workspace with a strict size limit.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "max_chars": {"type": "integer"},
                    },
                    "required": ["path"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "truncated": {"type": "boolean"},
                        "size_bytes": {"type": "integer"},
                    },
                    "required": ["path", "content", "truncated", "size_bytes"],
                },
                permissions=(Action.READ_FILE,),
            ),
            _read_text,
        ),
        (
            ToolSpec(
                id="workspace.write_text",
                name="Write new workspace text file",
                version="1.0.0",
                description="Create a new UTF-8 text file in the workspace. Never overwrites.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "content": {"type": "string"},
                        "create_parents": {"type": "boolean"},
                    },
                    "required": ["path", "content"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "size_bytes": {"type": "integer"},
                        "sha256": {"type": "string"},
                    },
                    "required": ["path", "size_bytes", "sha256"],
                },
                permissions=(Action.WRITE_FILE,),
            ),
            _write_text,
        ),
    ]


def register_workspace_tools(registry: ToolRegistry) -> None:
    for spec, handler in _specs():
        registry.register(spec, handler)
