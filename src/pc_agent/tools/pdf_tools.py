"""Registered PDF tools for the workspace and explicitly approved read roots."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..documents.pdf import inspect_pdf, merge_pdf_files, verify_pdf
from ..errors import ToolError
from ..policies.policy import Action
from .registry import ToolContext, ToolHandler, ToolRegistry, ToolSpec
from .workspace_tools import _resolve_allowed, relative_posix


def _inspect(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    target = _resolve_allowed(context, str(arguments["path"]), Action.READ_FILE)
    info = inspect_pdf(target)
    if info.encrypted:
        raise ToolError("Encrypted PDFs are not supported without a password.")
    return {
        "path": relative_posix(context, target),
        "pages": info.pages,
        "encrypted": False,
        "size_bytes": info.size_bytes,
    }


def _verify(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    target = _resolve_allowed(context, str(arguments["path"]), Action.READ_FILE)
    expected = arguments.get("expected_pages")
    result = verify_pdf(target, expected_pages=int(expected) if expected is not None else None)
    return {
        "path": relative_posix(context, target),
        "valid": result.valid,
        "pages": result.pages,
        "expected_pages": result.expected_pages,
        "sha256": result.sha256,
        "size_bytes": result.size_bytes,
        "error": result.error,
    }


def _merge(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    raw_inputs = [str(item) for item in arguments["input_paths"]]
    inputs = [
        _resolve_allowed(context, raw_path, Action.READ_FILE) for raw_path in raw_inputs
    ]
    output = _resolve_allowed(context, str(arguments["output_path"]), Action.WRITE_FILE)
    result = merge_pdf_files(inputs, output)
    return {
        "output_path": relative_posix(context, output),
        "input_files": [relative_posix(context, item) for item in inputs],
        "input_pages": [info.pages for info in result.inputs],
        "page_count": result.page_count,
        "size_bytes": result.size_bytes,
        "sha256": result.sha256,
        "verified": True,
        "skipped_files": [],
    }


def _merge_folders(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    raw_folders = [str(item) for item in arguments["input_folders"]]
    recursive = bool(arguments.get("recursive", False))
    folders = [
        _resolve_allowed(context, raw_folder, Action.LIST_FILES) for raw_folder in raw_folders
    ]
    for folder in folders:
        if not folder.is_dir():
            raise ToolError(f"Input folder not found: {relative_posix(context, folder)}")

    discovered = _discover_pdfs(context, folders, recursive)
    if not discovered:
        raise ToolError("No PDF files were found in the selected folder(s).")

    valid: list[Path] = []
    skipped: list[dict[str, str]] = []
    for candidate in discovered:
        # Re-check read permission for every individual file at execution time.
        _resolve_allowed(context, relative_posix(context, candidate), Action.READ_FILE)
        try:
            info = inspect_pdf(candidate)
        except ToolError as exc:
            skipped.append({"path": relative_posix(context, candidate), "reason": str(exc)})
            continue
        if info.encrypted:
            skipped.append(
                {
                    "path": relative_posix(context, candidate),
                    "reason": "Encrypted PDFs are not supported without a password.",
                }
            )
            continue
        valid.append(candidate)

    if len(valid) < 2:
        detail = "; ".join(f"{item['path']} ({item['reason']})" for item in skipped)
        suffix = f" Skipped: {detail}" if detail else ""
        raise ToolError(f"At least two valid PDFs are required to merge.{suffix}")

    output = _resolve_allowed(context, str(arguments["output_path"]), Action.WRITE_FILE)
    result = merge_pdf_files(valid, output)
    return {
        "output_path": relative_posix(context, output),
        "input_files": [relative_posix(context, item) for item in valid],
        "input_pages": [info.pages for info in result.inputs],
        "page_count": result.page_count,
        "size_bytes": result.size_bytes,
        "sha256": result.sha256,
        "verified": True,
        "skipped_files": skipped,
    }


def _discover_pdfs(context: ToolContext, folders: list[Path], recursive: bool) -> list[Path]:
    """Stable order: folders in the order given, then path order inside each folder."""
    discovered: list[Path] = []
    seen: set[Path] = set()
    for folder in folders:
        iterator = folder.rglob("*") if recursive else folder.glob("*")
        candidates = [
            candidate.resolve()
            for candidate in iterator
            if candidate.is_file()
            and candidate.suffix.lower() == ".pdf"
            and candidate.resolve().is_relative_to(folder)
        ]
        candidates.sort(key=lambda candidate: relative_posix(context, candidate).casefold())
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            discovered.append(resolved)
    return discovered


_MERGE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "output_path": {"type": "string"},
        "input_files": {"type": "array", "items": {"type": "string"}},
        "input_pages": {"type": "array", "items": {"type": "integer"}},
        "page_count": {"type": "integer"},
        "size_bytes": {"type": "integer"},
        "sha256": {"type": "string"},
        "verified": {"type": "boolean"},
        "skipped_files": {
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
    },
    "required": [
        "output_path",
        "input_files",
        "input_pages",
        "page_count",
        "size_bytes",
        "sha256",
        "verified",
        "skipped_files",
    ],
}


def _specs() -> list[tuple[ToolSpec, ToolHandler]]:
    string_path = {"type": "string", "minLength": 1}
    return [
        (
            ToolSpec(
                id="pdf.inspect",
                name="Inspect PDF",
                version="1.0.0",
                description="Open a workspace PDF and report its page count and size.",
                input_schema={
                    "type": "object",
                    "properties": {"path": string_path},
                    "required": ["path"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "pages": {"type": "integer"},
                        "encrypted": {"type": "boolean"},
                        "size_bytes": {"type": "integer"},
                    },
                    "required": ["path", "pages", "encrypted", "size_bytes"],
                },
                permissions=(Action.READ_FILE,),
            ),
            _inspect,
        ),
        (
            ToolSpec(
                id="pdf.verify",
                name="Verify PDF",
                version="1.0.0",
                description="Re-open a PDF and optionally compare its page count with an expected value.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": string_path,
                        "expected_pages": {"type": "integer", "minimum": 1},
                    },
                    "required": ["path"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "valid": {"type": "boolean"},
                        "pages": {"type": ["integer", "null"]},
                        "expected_pages": {"type": ["integer", "null"]},
                        "sha256": {"type": ["string", "null"]},
                        "size_bytes": {"type": ["integer", "null"]},
                        "error": {"type": ["string", "null"]},
                    },
                    "required": [
                        "path",
                        "valid",
                        "pages",
                        "expected_pages",
                        "sha256",
                        "size_bytes",
                        "error",
                    ],
                },
                permissions=(Action.READ_FILE,),
            ),
            _verify,
        ),
        (
            ToolSpec(
                id="pdf.merge",
                name="Merge PDFs",
                version="1.0.0",
                description="Merge an explicit ordered list of workspace PDFs into a new verified file.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "input_paths": {
                            "type": "array",
                            "items": string_path,
                            "minItems": 2,
                            "maxItems": 500,
                        },
                        "output_path": string_path,
                    },
                    "required": ["input_paths", "output_path"],
                },
                output_schema=_MERGE_OUTPUT_SCHEMA,
                permissions=(Action.READ_FILE, Action.WRITE_FILE),
                timeout_seconds=120.0,
            ),
            _merge,
        ),
        (
            ToolSpec(
                id="pdf.merge_folders",
                name="Merge PDFs from folders",
                version="1.0.0",
                description=(
                    "Merge PDFs discovered in selected workspace folders. Invalid or encrypted "
                    "files are reported in skipped_files, never included silently."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "input_folders": {
                            "type": "array",
                            "items": string_path,
                            "minItems": 1,
                            "maxItems": 50,
                        },
                        "output_path": string_path,
                        "recursive": {"type": "boolean"},
                    },
                    "required": ["input_folders", "output_path"],
                },
                output_schema=_MERGE_OUTPUT_SCHEMA,
                permissions=(Action.LIST_FILES, Action.READ_FILE, Action.WRITE_FILE),
                timeout_seconds=180.0,
            ),
            _merge_folders,
        ),
    ]


def register_pdf_tools(registry: ToolRegistry) -> None:
    for spec, handler in _specs():
        registry.register(spec, handler)
