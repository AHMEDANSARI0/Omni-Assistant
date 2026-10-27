"""General workspace file-operation tools (Phase 8).

Copy, move, rename, organize and search files strictly inside the
workspace. Nothing here overwrites or deletes: a destination that already
exists is always an error (copy/move/rename) or a reported skip (organize),
and a move preserves the file's bytes, which are hash-checked before and
after the rename. Every path is re-resolved and re-evaluated by the policy
engine at execution time, so symlink/junction escapes are refused exactly
like the other built-in tools.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path
from typing import Any

from ..errors import ToolError
from ..policies.paths import is_sensitive_path
from ..policies.policy import Action
from .registry import ToolContext, ToolHandler, ToolRegistry, ToolSpec
from .workspace_tools import _resolve_allowed, relative_posix

_CHUNK_BYTES = 64 * 1024
_MAX_ORGANIZE_FILES = 2000
_MAX_SEARCH_SCAN = 5000
_MAX_CONTENT_BYTES = 1_000_000

_CATEGORIES: dict[str, frozenset[str]] = {
    "images": frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico"}),
    "documents": frozenset({".pdf", ".docx", ".doc", ".txt", ".md", ".rtf", ".odt", ".pptx"}),
    "data": frozenset({".csv", ".json", ".xlsx", ".xls", ".xml", ".db", ".sqlite"}),
    "audio": frozenset({".mp3", ".wav", ".ogg", ".flac", ".m4a"}),
    "video": frozenset({".mp4", ".mkv", ".avi", ".mov", ".webm"}),
    "archives": frozenset({".zip", ".7z", ".rar", ".tar", ".gz"}),
    "code": frozenset({".py", ".js", ".ts", ".html", ".css", ".java", ".c", ".cpp", ".h"}),
}
_SUFFIX_TO_CATEGORY = {
    suffix: category for category, suffixes in _CATEGORIES.items() for suffix in suffixes
}
_TEXT_SUFFIXES = frozenset(
    {
        ".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".log", ".html",
        ".css", ".xml", ".ini", ".toml", ".yaml", ".yml",
    }
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_file(context: ToolContext, raw_path: str, action: Action) -> Path:
    source = _resolve_allowed(context, raw_path, action)
    if source.is_dir():
        raise ToolError(
            f"'{relative_posix(context, source)}' is a folder. "
            "Phase 8 file operations handle single files; use files.organize "
            "for a folder of files."
        )
    if not source.is_file():
        raise ToolError(f"File not found: {relative_posix(context, source)}")
    return source


def _destination(
    context: ToolContext, raw_path: str, *, create_parents: bool
) -> Path:
    destination = _resolve_allowed(context, raw_path, Action.WRITE_FILE)
    if destination == context.workspace_dir or destination.is_dir():
        raise ToolError("The destination must be a file path, not a folder.")
    if destination.exists():
        raise ToolError(
            "Output file already exists. This tool never overwrites; "
            "choose a new output path."
        )
    if create_parents:
        destination.parent.mkdir(parents=True, exist_ok=True)
    elif not destination.parent.is_dir():
        raise ToolError(
            f"Destination folder not found: {relative_posix(context, destination.parent)}"
        )
    return destination


def _copy(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    source = _source_file(context, str(arguments["path"]), Action.READ_FILE)
    destination = _destination(
        context,
        str(arguments["output_path"]),
        create_parents=bool(arguments.get("create_parents", True)),
    )
    if destination == source:
        raise ToolError("Source and destination are the same file.")

    digest = hashlib.sha256()
    written = 0
    try:
        with source.open("rb") as reader, destination.open("xb") as writer:
            for chunk in iter(lambda: reader.read(_CHUNK_BYTES), b""):
                writer.write(chunk)
                digest.update(chunk)
                written += len(chunk)
    except FileExistsError as exc:  # lost a race after the existence check
        raise ToolError(
            "Output file already exists. This tool never overwrites; "
            "choose a new output path."
        ) from exc
    except OSError as exc:
        destination.unlink(missing_ok=True)  # remove our own partial copy
        raise ToolError(f"Could not copy '{source.name}': {exc}") from exc

    return {
        "path": relative_posix(context, source),
        "output_path": relative_posix(context, destination),
        "size_bytes": written,
        "sha256": digest.hexdigest(),
    }


def _move_resolved(
    context: ToolContext, source: Path, destination: Path
) -> dict[str, Any]:
    if destination == source:
        raise ToolError("Source and destination are the same file.")
    before = _sha256(source)
    try:
        os.rename(source, destination)
    except OSError as exc:
        raise ToolError(f"Could not move '{source.name}': {exc}") from exc
    if _sha256(destination) != before:
        raise ToolError(
            "Moved file failed its post-move hash check; inspect both paths."
        )
    return {
        "path": relative_posix(context, source),
        "output_path": relative_posix(context, destination),
        "size_bytes": destination.stat().st_size,
        "sha256": before,
    }


def _move(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    source = _source_file(context, str(arguments["path"]), Action.MOVE_FILE)
    destination = _destination(
        context,
        str(arguments["output_path"]),
        create_parents=bool(arguments.get("create_parents", True)),
    )
    return _move_resolved(context, source, destination)


def _rename(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    new_name = str(arguments["new_name"]).strip()
    if (
        not new_name
        or new_name in {".", ".."}
        or "/" in new_name
        or "\\" in new_name
        or Path(new_name).is_absolute()
    ):
        raise ToolError("new_name must be a plain file name without path separators.")
    raw_source = str(arguments["path"])
    source = _source_file(context, raw_source, Action.MOVE_FILE)
    normalized = raw_source.replace("\\", "/")
    parent = normalized.rsplit("/", 1)[0] if "/" in normalized else ""
    raw_destination = f"{parent}/{new_name}" if parent else new_name
    destination = _destination(context, raw_destination, create_parents=False)
    return _move_resolved(context, source, destination)


def _organize(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    by = str(arguments.get("by") or "category")
    if by not in {"category", "extension"}:
        raise ToolError("by must be 'category' or 'extension'.")
    max_files = int(arguments.get("max_files", 500))
    if max_files < 1 or max_files > _MAX_ORGANIZE_FILES:
        raise ToolError(f"max_files must be between 1 and {_MAX_ORGANIZE_FILES}.")

    directory = _resolve_allowed(
        context, str(arguments.get("directory") or "."), Action.LIST_FILES
    )
    if not directory.is_dir():
        raise ToolError(f"Folder not found: {relative_posix(context, directory)}")

    files = sorted(
        (
            candidate.resolve()
            for candidate in directory.iterdir()
            if candidate.is_file() and candidate.resolve().is_relative_to(directory)
        ),
        key=lambda candidate: candidate.name.casefold(),
    )
    if len(files) > max_files:
        raise ToolError(
            f"Folder has {len(files)} files; the organize limit is {max_files}. "
            "Organize a smaller folder or raise max_files."
        )

    moved: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for source in files:
        suffix = source.suffix.lower()
        if by == "extension":
            folder_name = suffix.removeprefix(".") or "no_extension"
        else:
            folder_name = _SUFFIX_TO_CATEGORY.get(suffix, "other")
        target_dir = directory / folder_name
        target = target_dir / source.name
        if target.exists():
            skipped.append(
                {
                    "path": relative_posix(context, source),
                    "reason": f"destination already exists: {relative_posix(context, target)}",
                }
            )
            continue
        # Re-evaluate policy for this exact source/destination pair.
        _resolve_allowed(context, relative_posix(context, source), Action.MOVE_FILE)
        _resolve_allowed(context, relative_posix(context, target), Action.WRITE_FILE)
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(source, target)
        except OSError as exc:
            skipped.append(
                {"path": relative_posix(context, source), "reason": f"move failed: {exc}"}
            )
            continue
        moved.append(
            {
                "from": relative_posix(context, source),
                "to": relative_posix(context, target),
            }
        )

    return {
        "directory": relative_posix(context, directory),
        "by": by,
        "moved": moved,
        "moved_count": len(moved),
        "skipped": skipped,
        "skipped_count": len(skipped),
    }


def _search(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    query = str(arguments["query"]).strip()
    if not query:
        raise ToolError("query must not be empty.")
    search_content = bool(arguments.get("search_content", False))
    max_results = int(arguments.get("max_results", 100))
    if max_results < 1 or max_results > 500:
        raise ToolError("max_results must be between 1 and 500.")

    directory = _resolve_allowed(
        context, str(arguments.get("directory") or "."), Action.LIST_FILES
    )
    if not directory.is_dir():
        raise ToolError(f"Folder not found: {relative_posix(context, directory)}")

    lowered_query = query.casefold()
    wildcard = any(token in query for token in "*?[")
    matches: list[dict[str, Any]] = []
    scanned = 0
    scan_truncated = False
    for candidate in directory.rglob("*"):
        if scanned >= _MAX_SEARCH_SCAN:
            scan_truncated = True
            break
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if not resolved.is_relative_to(directory):
            continue  # symlink/junction escape; policy approved the folder only
        scanned += 1
        name_match = (
            fnmatch.fnmatchcase(resolved.name.casefold(), lowered_query)
            if wildcard
            else lowered_query in resolved.name.casefold()
        )
        content_match = False
        snippet: str | None = None
        if (
            search_content
            and not is_sensitive_path(resolved)
            and resolved.suffix.lower() in _TEXT_SUFFIXES
            and resolved.stat().st_size <= _MAX_CONTENT_BYTES
        ):
            try:
                text = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                text = ""
            for line in text.splitlines():
                if lowered_query in line.casefold():
                    content_match = True
                    snippet = line.strip()[:160]
                    break
        if name_match or content_match:
            matches.append(
                {
                    "path": relative_posix(context, resolved),
                    "name_match": name_match,
                    "content_match": content_match,
                    "first_matching_line": snippet,
                    "size_bytes": resolved.stat().st_size,
                }
            )

    matches.sort(key=lambda item: str(item["path"]).casefold())
    shown = matches[:max_results]
    return {
        "directory": relative_posix(context, directory),
        "query": query,
        "matches": shown,
        "count": len(shown),
        "truncated": scan_truncated or len(matches) > len(shown),
        "scanned_files": scanned,
    }


def _transfer_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "output_path": {"type": "string"},
            "size_bytes": {"type": "integer"},
            "sha256": {"type": "string"},
        },
        "required": ["path", "output_path", "size_bytes", "sha256"],
    }


def _specs() -> list[tuple[ToolSpec, ToolHandler]]:
    transfer_input = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "output_path": {"type": "string", "minLength": 1},
            "create_parents": {"type": "boolean"},
        },
        "required": ["path", "output_path"],
    }
    return [
        (
            ToolSpec(
                id="files.copy",
                name="Copy workspace file",
                version="1.0.0",
                description="Copy one workspace file to a new path. Never overwrites.",
                input_schema=transfer_input,
                output_schema=_transfer_output_schema(),
                permissions=(Action.READ_FILE, Action.WRITE_FILE),
            ),
            _copy,
        ),
        (
            ToolSpec(
                id="files.move",
                name="Move workspace file",
                version="1.0.0",
                description="Move one workspace file to a new path with a before/after SHA-256 check. Never overwrites.",
                input_schema=transfer_input,
                output_schema=_transfer_output_schema(),
                permissions=(Action.MOVE_FILE, Action.WRITE_FILE),
            ),
            _move,
        ),
        (
            ToolSpec(
                id="files.rename",
                name="Rename workspace file",
                version="1.0.0",
                description="Rename one workspace file in place to a plain new file name. Never overwrites.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "new_name": {"type": "string", "minLength": 1},
                    },
                    "required": ["path", "new_name"],
                },
                output_schema=_transfer_output_schema(),
                permissions=(Action.MOVE_FILE, Action.WRITE_FILE),
            ),
            _rename,
        ),
        (
            ToolSpec(
                id="files.organize",
                name="Organize workspace folder",
                version="1.0.0",
                description="Move a folder's top-level files into category or extension subfolders. Conflicts are skipped and reported; nothing is overwritten.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "directory": {"type": "string"},
                        "by": {"type": "string"},
                        "max_files": {"type": "integer"},
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "directory": {"type": "string"},
                        "by": {"type": "string"},
                        "moved": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "from": {"type": "string"},
                                    "to": {"type": "string"},
                                },
                                "required": ["from", "to"],
                            },
                        },
                        "moved_count": {"type": "integer"},
                        "skipped": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "reason": {"type": "string"},
                                },
                                "required": ["path", "reason"],
                            },
                        },
                        "skipped_count": {"type": "integer"},
                    },
                    "required": [
                        "directory", "by", "moved", "moved_count", "skipped", "skipped_count",
                    ],
                },
                permissions=(Action.LIST_FILES, Action.MOVE_FILE, Action.WRITE_FILE),
            ),
            _organize,
        ),
        (
            ToolSpec(
                id="files.search",
                name="Search workspace files",
                version="1.0.0",
                description="Search workspace file names (substring or wildcard) and optionally UTF-8 text content, within strict scan limits.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "directory": {"type": "string"},
                        "search_content": {"type": "boolean"},
                        "max_results": {"type": "integer"},
                    },
                    "required": ["query"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "directory": {"type": "string"},
                        "query": {"type": "string"},
                        "matches": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "name_match": {"type": "boolean"},
                                    "content_match": {"type": "boolean"},
                                    "first_matching_line": {"type": ["string", "null"]},
                                    "size_bytes": {"type": "integer"},
                                },
                                "required": [
                                    "path",
                                    "name_match",
                                    "content_match",
                                    "first_matching_line",
                                    "size_bytes",
                                ],
                            },
                        },
                        "count": {"type": "integer"},
                        "truncated": {"type": "boolean"},
                        "scanned_files": {"type": "integer"},
                    },
                    "required": [
                        "directory", "query", "matches", "count", "truncated", "scanned_files",
                    ],
                },
                permissions=(Action.LIST_FILES, Action.READ_FILE),
            ),
            _search,
        ),
    ]


def register_file_tools(registry: ToolRegistry) -> None:
    for spec, handler in _specs():
        registry.register(spec, handler)
