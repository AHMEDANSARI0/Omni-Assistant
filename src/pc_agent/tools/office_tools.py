"""Registered Office/CSV tools with workspace-only outputs."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import uuid
from pathlib import Path
from typing import Any

from ..documents.office import (
    MAX_CSV_FILE_BYTES,
    append_docx_paragraphs,
    build_docx,
    build_xlsx,
    evaluate_workbook_formulas,
    extract_office_text,
    inspect_csv,
    inspect_office,
    read_csv,
    read_xlsx_cells,
    replace_docx_text,
    set_xlsx_cells,
)
from ..errors import ToolError
from ..policies.policy import Action
from .registry import ToolContext, ToolHandler, ToolRegistry, ToolSpec
from .workspace_tools import _resolve_allowed, relative_posix


def _inspect(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    target = _resolve_allowed(context, str(arguments["path"]), Action.READ_FILE)
    info = inspect_office(target)
    return {
        "path": relative_posix(context, target),
        "format": info.format,
        "size_bytes": info.size_bytes,
        "sha256": info.sha256,
        "title": info.title,
        "creator": info.creator,
        "unit_name": info.unit_name,
        "unit_count": info.unit_count,
        "words": info.words,
        "characters": info.characters,
        "details": info.details,
    }


def _extract_text(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    target = _resolve_allowed(context, str(arguments["path"]), Action.READ_FILE)
    max_chars = int(arguments.get("max_chars", 100_000))
    extracted = extract_office_text(target, max_chars=max_chars)
    output_path: str | None = None
    output_size: int | None = None
    output_sha: str | None = None
    raw_output = arguments.get("output_path")
    if raw_output:
        output = _resolve_allowed(context, str(raw_output), Action.WRITE_FILE)
        _require_different(target, output)
        if output.suffix.lower() not in {".txt", ".md"}:
            raise ToolError("Extracted text output must end with .txt or .md.")
        data = (extracted.text + ("\n" if extracted.text else "")).encode("utf-8")
        output_size, output_sha = _write_new_bytes(output, data)
        output_path = relative_posix(context, output)
    return {
        "path": relative_posix(context, target),
        "format": extracted.format,
        "text": extracted.text,
        "characters": extracted.total_characters,
        "truncated": extracted.truncated,
        "output_path": output_path,
        "output_size_bytes": output_size,
        "output_sha256": output_sha,
    }


def _csv_inspect(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    target = _resolve_allowed(context, str(arguments["path"]), Action.READ_FILE)
    inspection = inspect_csv(
        target, sample_rows=int(arguments.get("sample_rows", 5))
    )
    data = inspection.data
    return {
        "path": relative_posix(context, target),
        "format": "csv",
        "size_bytes": data.size_bytes,
        "sha256": data.sha256,
        "columns": list(data.columns),
        "row_count": len(data.rows),
        "has_header": data.has_header,
        "delimiter": data.delimiter,
        "malformed_rows": data.malformed_rows,
        "sample_rows": [dict(row) for row in inspection.sample_rows],
        "column_stats": [
            {
                "name": stat.name,
                "nonempty": stat.nonempty,
                "numeric_count": stat.numeric_count,
                "distinct_count": stat.distinct_count,
                "minimum": stat.minimum,
                "maximum": stat.maximum,
                "mean": stat.mean,
            }
            for stat in inspection.column_stats
        ],
    }


def _csv_to_json(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    source = _resolve_allowed(context, str(arguments["input_path"]), Action.READ_FILE)
    output = _resolve_allowed(context, str(arguments["output_path"]), Action.WRITE_FILE)
    _require_different(source, output)
    if output.suffix.lower() != ".json":
        raise ToolError("CSV-to-JSON output must end with .json.")
    data = read_csv(source)
    records = [dict(row) for row in data.rows]
    payload = (json.dumps(records, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if json.loads(payload.decode("utf-8")) != records:
        raise ToolError("Generated JSON failed its round-trip verification.")
    size, digest = _write_new_bytes(output, payload)
    return {
        "input_path": relative_posix(context, source),
        "output_path": relative_posix(context, output),
        "format": "json",
        "row_count": len(records),
        "columns": list(data.columns),
        "size_bytes": size,
        "sha256": digest,
        "verified": True,
    }


def _json_to_csv(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    source = _resolve_allowed(context, str(arguments["input_path"]), Action.READ_FILE)
    output = _resolve_allowed(context, str(arguments["output_path"]), Action.WRITE_FILE)
    _require_different(source, output)
    if source.suffix.lower() != ".json" or output.suffix.lower() != ".csv":
        raise ToolError("JSON-to-CSV requires a .json input and .csv output.")
    if not source.is_file():
        raise ToolError(f"JSON file not found: {relative_posix(context, source)}")
    if source.stat().st_size > MAX_CSV_FILE_BYTES:
        raise ToolError("JSON file exceeds the 10 MB safety limit.")
    try:
        parsed = json.loads(source.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ToolError("JSON file must be valid UTF-8.") from exc
    except json.JSONDecodeError as exc:
        raise ToolError("JSON file could not be parsed.") from exc

    if isinstance(parsed, dict) and isinstance(parsed.get("rows"), list):
        parsed = parsed["rows"]
    records_input = [parsed] if isinstance(parsed, dict) else parsed
    if not isinstance(records_input, list) or not records_input:
        raise ToolError("JSON-to-CSV needs an object or a non-empty array of objects.")
    records: list[dict[str, Any]] = []
    columns: list[str] = []
    for item in records_input:
        if not isinstance(item, dict):
            raise ToolError("JSON-to-CSV array items must all be objects.")
        record = {str(key): value for key, value in item.items()}
        records.append(record)
        for key in record:
            if key not in columns:
                columns.append(key)
    if len(columns) > 100:
        raise ToolError("JSON-to-CSV output would exceed 100 columns.")

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow({column: _csv_scalar(record.get(column)) for column in columns})
    payload = buffer.getvalue().encode("utf-8")
    parsed_rows = list(csv.reader(io.StringIO(buffer.getvalue(), newline="")))
    if len(parsed_rows) != len(records) + 1 or parsed_rows[0] != columns:
        raise ToolError("Generated CSV failed its round-trip verification.")
    size, digest = _write_new_bytes(output, payload)
    return {
        "input_path": relative_posix(context, source),
        "output_path": relative_posix(context, output),
        "format": "csv",
        "row_count": len(records),
        "columns": columns,
        "size_bytes": size,
        "sha256": digest,
        "verified": True,
    }


def _create_docx(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    output = _resolve_allowed(context, str(arguments["output_path"]), Action.WRITE_FILE)
    if output.suffix.lower() != ".docx":
        raise ToolError("Generated document output must end with .docx.")
    paragraphs = [str(item) for item in arguments.get("paragraphs", [])]
    payload = build_docx(title=str(arguments["title"]), paragraphs=paragraphs)
    size, digest, info = _write_verified_package(output, payload, expected_format="docx")
    return {
        "output_path": relative_posix(context, output),
        "format": "docx",
        "title": info.title,
        "paragraphs": int(info.details.get("paragraphs", 0)),
        "size_bytes": size,
        "sha256": digest,
        "verified": True,
    }


def _csv_to_xlsx(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    source = _resolve_allowed(context, str(arguments["input_path"]), Action.READ_FILE)
    output = _resolve_allowed(context, str(arguments["output_path"]), Action.WRITE_FILE)
    _require_different(source, output)
    if output.suffix.lower() != ".xlsx":
        raise ToolError("CSV-to-XLSX output must end with .xlsx.")
    data = read_csv(source)
    rows = [list(data.columns)] + [
        [row[column] for column in data.columns] for row in data.rows
    ]
    payload = build_xlsx(rows, sheet_name=str(arguments.get("sheet_name") or "Sheet1"))
    size, digest, info = _write_verified_package(output, payload, expected_format="xlsx")
    return {
        "input_path": relative_posix(context, source),
        "output_path": relative_posix(context, output),
        "format": "xlsx",
        "sheet_name": str(arguments.get("sheet_name") or "Sheet1").strip(),
        "row_count": len(data.rows),
        "columns": list(data.columns),
        "size_bytes": size,
        "sha256": digest,
        "verified": info.format == "xlsx",
    }


def _docx_replace(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    source = _resolve_allowed(context, str(arguments["input_path"]), Action.READ_FILE)
    output = _resolve_allowed(context, str(arguments["output_path"]), Action.WRITE_FILE)
    _require_different(source, output)
    if source.suffix.lower() != ".docx" or output.suffix.lower() != ".docx":
        raise ToolError("DOCX replace needs a .docx input and a .docx output.")
    result = replace_docx_text(
        source, str(arguments["find"]), str(arguments.get("replace", ""))
    )
    size, digest, _info = _write_verified_package(
        output, result.data, expected_format="docx"
    )
    return {
        "input_path": relative_posix(context, source),
        "output_path": relative_posix(context, output),
        "format": "docx",
        "find": str(arguments["find"]),
        "replace": str(arguments.get("replace", "")),
        "replacements": result.replacements,
        "paragraphs_changed": result.paragraphs_changed,
        "size_bytes": size,
        "sha256": digest,
        "verified": True,
    }


def _docx_append(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    source = _resolve_allowed(context, str(arguments["input_path"]), Action.READ_FILE)
    output = _resolve_allowed(context, str(arguments["output_path"]), Action.WRITE_FILE)
    _require_different(source, output)
    if source.suffix.lower() != ".docx" or output.suffix.lower() != ".docx":
        raise ToolError("DOCX append needs a .docx input and a .docx output.")
    paragraphs = [str(item) for item in arguments.get("paragraphs", [])]
    result = append_docx_paragraphs(source, paragraphs)
    size, digest, _info = _write_verified_package(
        output, result.data, expected_format="docx"
    )
    return {
        "input_path": relative_posix(context, source),
        "output_path": relative_posix(context, output),
        "format": "docx",
        "paragraphs_added": result.paragraphs_added,
        "total_paragraphs": result.total_paragraphs,
        "size_bytes": size,
        "sha256": digest,
        "verified": True,
    }


def _formula_items(evaluations: Any) -> list[dict[str, Any]]:
    return [
        {
            "sheet": item.sheet,
            "reference": item.reference,
            "formula": item.formula,
            "computed": item.value,
            "error": item.error,
        }
        for item in evaluations
    ]


def _xlsx_set_cells(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    source = _resolve_allowed(context, str(arguments["input_path"]), Action.READ_FILE)
    output = _resolve_allowed(context, str(arguments["output_path"]), Action.WRITE_FILE)
    _require_different(source, output)
    if source.suffix.lower() != ".xlsx" or output.suffix.lower() != ".xlsx":
        raise ToolError("XLSX set-cells needs a .xlsx input and a .xlsx output.")
    raw_cells = arguments.get("cells")
    if not isinstance(raw_cells, list) or not raw_cells:
        raise ToolError(
            "cells must be a non-empty list like "
            "[{\"reference\": \"B2\", \"value\": \"=SUM(A1:A3)\"}]."
        )
    edits: dict[str, str] = {}
    for item in raw_cells:
        if not isinstance(item, dict):
            raise ToolError("Each cells entry must be {\"reference\": ..., \"value\": ...}.")
        reference = str(item.get("reference") or "").strip()
        if not reference or "value" not in item:
            raise ToolError("Each cells entry needs a reference and a value.")
        edits[reference] = str(item["value"])
    sheet_name = arguments.get("sheet_name")
    result = set_xlsx_cells(
        source, edits, sheet_name=str(sheet_name) if sheet_name else None
    )
    size, digest, _info = _write_verified_package(
        output, result.data, expected_format="xlsx"
    )
    return {
        "input_path": relative_posix(context, source),
        "output_path": relative_posix(context, output),
        "format": "xlsx",
        "sheet": result.sheet,
        "cells_set": list(result.cells_set),
        "formulas": _formula_items(result.evaluations),
        "size_bytes": size,
        "sha256": digest,
        "verified": True,
    }


def _xlsx_verify_formulas(
    arguments: dict[str, Any], context: ToolContext
) -> dict[str, Any]:
    source = _resolve_allowed(context, str(arguments["path"]), Action.READ_FILE)
    if source.suffix.lower() != ".xlsx":
        raise ToolError("Formula verification needs a .xlsx file.")
    workbook = read_xlsx_cells(source)
    evaluations = evaluate_workbook_formulas(workbook)
    computed_count = sum(1 for item in evaluations if item.value is not None)
    return {
        "path": relative_posix(context, source),
        "formulas": _formula_items(evaluations),
        "formula_count": len(evaluations),
        "computed_count": computed_count,
        "unsupported_count": len(evaluations) - computed_count,
    }


def _csv_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _require_different(source: Path, output: Path) -> None:
    if source.resolve() == output.resolve():
        raise ToolError("Output path must be different from the input path.")


def _write_new_bytes(target: Path, data: bytes) -> tuple[int, str]:
    if target.exists():
        raise ToolError(
            "Output file already exists. Office tools never overwrite; choose a new output path."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as handle:
            handle.write(data)
    except FileExistsError as exc:
        raise ToolError(
            "Output file already exists. Office tools never overwrite; choose a new output path."
        ) from exc
    except OSError as exc:
        raise ToolError(f"Could not write Office output: {target.name}") from exc
    return len(data), hashlib.sha256(data).hexdigest()


def _write_verified_package(
    target: Path, data: bytes, *, expected_format: str
) -> tuple[int, str, Any]:
    if target.exists():
        raise ToolError(
            "Output file already exists. Office tools never overwrite; choose a new output path."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.stem}.tmp-{uuid.uuid4().hex}{target.suffix}"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
        info = inspect_office(temporary)
        if info.format != expected_format:
            raise ToolError("Generated Office package failed verification.")
        if target.exists():
            raise ToolError(
                "Output file already exists. Office tools never overwrite; "
                "choose a new output path."
            )
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return len(data), hashlib.sha256(data).hexdigest(), info


def _formula_items_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "sheet": {"type": "string"},
                "reference": {"type": "string"},
                "formula": {"type": "string"},
                "computed": {"type": ["number", "null"]},
                "error": {"type": ["string", "null"]},
            },
            "required": ["sheet", "reference", "formula", "computed", "error"],
        },
    }


def _docx_edit_schema(*, extra_replacements: bool) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "input_path": {"type": "string"},
        "output_path": {"type": "string"},
        "format": {"type": "string"},
        "size_bytes": {"type": "integer"},
        "sha256": {"type": "string"},
        "verified": {"type": "boolean"},
    }
    if extra_replacements:
        properties.update(
            {
                "find": {"type": "string"},
                "replace": {"type": "string"},
                "replacements": {"type": "integer"},
                "paragraphs_changed": {"type": "integer"},
            }
        )
        required = [
            "input_path", "output_path", "format", "find", "replace",
            "replacements", "paragraphs_changed", "size_bytes", "sha256", "verified",
        ]
    else:
        properties.update(
            {
                "paragraphs_added": {"type": "integer"},
                "total_paragraphs": {"type": "integer"},
            }
        )
        required = [
            "input_path", "output_path", "format", "paragraphs_added",
            "total_paragraphs", "size_bytes", "sha256", "verified",
        ]
    return {"type": "object", "properties": properties, "required": required}


def _xlsx_set_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "input_path": {"type": "string"},
            "output_path": {"type": "string"},
            "format": {"type": "string"},
            "sheet": {"type": "string"},
            "cells_set": {"type": "array", "items": {"type": "string"}},
            "formulas": _formula_items_schema(),
            "size_bytes": {"type": "integer"},
            "sha256": {"type": "string"},
            "verified": {"type": "boolean"},
        },
        "required": [
            "input_path", "output_path", "format", "sheet", "cells_set",
            "formulas", "size_bytes", "sha256", "verified",
        ],
    }


def _conversion_output_schema(format_type: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "input_path": {"type": "string"},
            "output_path": {"type": "string"},
            "format": {"type": "string"},
            "row_count": {"type": "integer"},
            "columns": {"type": "array", "items": {"type": "string"}},
            "size_bytes": {"type": "integer"},
            "sha256": {"type": "string"},
            "verified": {"type": "boolean"},
        },
        "required": [
            "input_path",
            "output_path",
            "format",
            "row_count",
            "columns",
            "size_bytes",
            "sha256",
            "verified",
        ],
    }


def _specs() -> list[tuple[ToolSpec, ToolHandler]]:
    string_path = {"type": "string", "minLength": 1}
    return [
        (
            ToolSpec(
                id="office.inspect",
                name="Inspect Office document",
                version="1.0.0",
                description="Inspect a workspace DOCX, XLSX, PPTX or CSV file and report deterministic metadata/counts.",
                input_schema={
                    "type": "object",
                    "properties": {"path": string_path},
                    "required": ["path"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "format": {"type": "string"},
                        "size_bytes": {"type": "integer"},
                        "sha256": {"type": "string"},
                        "title": {"type": ["string", "null"]},
                        "creator": {"type": ["string", "null"]},
                        "unit_name": {"type": "string"},
                        "unit_count": {"type": "integer"},
                        "words": {"type": "integer"},
                        "characters": {"type": "integer"},
                        "details": {},
                    },
                    "required": [
                        "path",
                        "format",
                        "size_bytes",
                        "sha256",
                        "title",
                        "creator",
                        "unit_name",
                        "unit_count",
                        "words",
                        "characters",
                        "details",
                    ],
                },
                permissions=(Action.READ_FILE,),
                timeout_seconds=60.0,
            ),
            _inspect,
        ),
        (
            ToolSpec(
                id="office.extract_text",
                name="Extract Office text",
                version="1.0.0",
                description="Extract bounded plain text from a workspace DOCX, XLSX, PPTX or CSV file, optionally saving a new text file.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": string_path,
                        "output_path": string_path,
                        "max_chars": {"type": "integer", "minimum": 1, "maximum": 200000},
                    },
                    "required": ["path"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "format": {"type": "string"},
                        "text": {"type": "string"},
                        "characters": {"type": "integer"},
                        "truncated": {"type": "boolean"},
                        "output_path": {"type": ["string", "null"]},
                        "output_size_bytes": {"type": ["integer", "null"]},
                        "output_sha256": {"type": ["string", "null"]},
                    },
                    "required": [
                        "path",
                        "format",
                        "text",
                        "characters",
                        "truncated",
                        "output_path",
                        "output_size_bytes",
                        "output_sha256",
                    ],
                },
                permissions=(Action.READ_FILE, Action.WRITE_FILE),
                timeout_seconds=60.0,
            ),
            _extract_text,
        ),
        (
            ToolSpec(
                id="office.csv_inspect",
                name="Inspect CSV",
                version="1.0.0",
                description="Inspect a workspace CSV with column statistics and a bounded sample.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": string_path,
                        "sample_rows": {"type": "integer", "minimum": 0, "maximum": 20},
                    },
                    "required": ["path"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "format": {"type": "string"},
                        "size_bytes": {"type": "integer"},
                        "sha256": {"type": "string"},
                        "columns": {"type": "array", "items": {"type": "string"}},
                        "row_count": {"type": "integer"},
                        "has_header": {"type": "boolean"},
                        "delimiter": {"type": "string"},
                        "malformed_rows": {"type": "integer"},
                        "sample_rows": {"type": "array", "items": {}},
                        "column_stats": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "nonempty": {"type": "integer"},
                                    "numeric_count": {"type": "integer"},
                                    "distinct_count": {"type": "integer"},
                                    "minimum": {"type": ["number", "null"]},
                                    "maximum": {"type": ["number", "null"]},
                                    "mean": {"type": ["number", "null"]},
                                },
                                "required": [
                                    "name",
                                    "nonempty",
                                    "numeric_count",
                                    "distinct_count",
                                    "minimum",
                                    "maximum",
                                    "mean",
                                ],
                            },
                        },
                    },
                    "required": [
                        "path",
                        "format",
                        "size_bytes",
                        "sha256",
                        "columns",
                        "row_count",
                        "has_header",
                        "delimiter",
                        "malformed_rows",
                        "sample_rows",
                        "column_stats",
                    ],
                },
                permissions=(Action.READ_FILE,),
                timeout_seconds=60.0,
            ),
            _csv_inspect,
        ),
        (
            ToolSpec(
                id="office.csv_to_json",
                name="Convert CSV to JSON",
                version="1.0.0",
                description="Convert a workspace CSV to a new JSON array of objects; values remain strings.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "input_path": string_path,
                        "output_path": string_path,
                    },
                    "required": ["input_path", "output_path"],
                },
                output_schema=_conversion_output_schema("json"),
                permissions=(Action.READ_FILE, Action.WRITE_FILE),
                timeout_seconds=60.0,
            ),
            _csv_to_json,
        ),
        (
            ToolSpec(
                id="office.json_to_csv",
                name="Convert JSON to CSV",
                version="1.0.0",
                description="Convert a workspace JSON object or array of flat objects to a new CSV file.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "input_path": string_path,
                        "output_path": string_path,
                    },
                    "required": ["input_path", "output_path"],
                },
                output_schema=_conversion_output_schema("csv"),
                permissions=(Action.READ_FILE, Action.WRITE_FILE),
                timeout_seconds=60.0,
            ),
            _json_to_csv,
        ),
        (
            ToolSpec(
                id="office.create_docx",
                name="Create DOCX",
                version="1.0.0",
                description="Create and verify a simple new DOCX document from a title and paragraphs.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "output_path": string_path,
                        "title": {"type": "string", "minLength": 1, "maxLength": 200},
                        "paragraphs": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 10000},
                            "maxItems": 1000,
                        },
                    },
                    "required": ["output_path", "title"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "output_path": {"type": "string"},
                        "format": {"type": "string"},
                        "title": {"type": ["string", "null"]},
                        "paragraphs": {"type": "integer"},
                        "size_bytes": {"type": "integer"},
                        "sha256": {"type": "string"},
                        "verified": {"type": "boolean"},
                    },
                    "required": [
                        "output_path",
                        "format",
                        "title",
                        "paragraphs",
                        "size_bytes",
                        "sha256",
                        "verified",
                    ],
                },
                permissions=(Action.WRITE_FILE,),
                timeout_seconds=60.0,
            ),
            _create_docx,
        ),
        (
            ToolSpec(
                id="office.csv_to_xlsx",
                name="Convert CSV to XLSX",
                version="1.0.0",
                description="Convert a workspace CSV to a new verified one-sheet XLSX workbook.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "input_path": string_path,
                        "output_path": string_path,
                        "sheet_name": {"type": "string", "minLength": 1, "maxLength": 31},
                    },
                    "required": ["input_path", "output_path"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "input_path": {"type": "string"},
                        "output_path": {"type": "string"},
                        "format": {"type": "string"},
                        "sheet_name": {"type": "string"},
                        "row_count": {"type": "integer"},
                        "columns": {"type": "array", "items": {"type": "string"}},
                        "size_bytes": {"type": "integer"},
                        "sha256": {"type": "string"},
                        "verified": {"type": "boolean"},
                    },
                    "required": [
                        "input_path",
                        "output_path",
                        "format",
                        "sheet_name",
                        "row_count",
                        "columns",
                        "size_bytes",
                        "sha256",
                        "verified",
                    ],
                },
                permissions=(Action.READ_FILE, Action.WRITE_FILE),
                timeout_seconds=60.0,
            ),
            _csv_to_xlsx,
        ),
        (
            ToolSpec(
                id="office.docx_replace_text",
                name="Replace text in DOCX",
                version="1.0.0",
                description="Replace exact paragraph text in a DOCX and write a new verified document. Never overwrites.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "input_path": string_path,
                        "output_path": string_path,
                        "find": {"type": "string", "minLength": 1},
                        "replace": {"type": "string"},
                    },
                    "required": ["input_path", "output_path", "find"],
                },
                output_schema=_docx_edit_schema(extra_replacements=True),
                permissions=(Action.READ_FILE, Action.WRITE_FILE),
                timeout_seconds=60.0,
            ),
            _docx_replace,
        ),
        (
            ToolSpec(
                id="office.docx_append_paragraphs",
                name="Append paragraphs to DOCX",
                version="1.0.0",
                description="Append paragraphs to a DOCX body and write a new verified document. Never overwrites.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "input_path": string_path,
                        "output_path": string_path,
                        "paragraphs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 100,
                        },
                    },
                    "required": ["input_path", "output_path", "paragraphs"],
                },
                output_schema=_docx_edit_schema(extra_replacements=False),
                permissions=(Action.READ_FILE, Action.WRITE_FILE),
                timeout_seconds=60.0,
            ),
            _docx_append,
        ),
        (
            ToolSpec(
                id="office.xlsx_set_cells",
                name="Set XLSX cells (values/formulas)",
                version="1.0.0",
                description="Set cell values or =formulas in one sheet of an existing XLSX, writing a new workbook with freshly computed cached formula values. Never overwrites.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "input_path": string_path,
                        "output_path": string_path,
                        "sheet_name": {"type": "string"},
                        "cells": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "reference": {"type": "string", "minLength": 2},
                                    "value": {"type": "string"},
                                },
                                "required": ["reference", "value"],
                            },
                            "minItems": 1,
                            "maxItems": 100,
                        },
                    },
                    "required": ["input_path", "output_path", "cells"],
                },
                output_schema=_xlsx_set_schema(),
                permissions=(Action.READ_FILE, Action.WRITE_FILE),
                timeout_seconds=60.0,
            ),
            _xlsx_set_cells,
        ),
        (
            ToolSpec(
                id="office.xlsx_verify_formulas",
                name="Verify XLSX formulas",
                version="1.0.0",
                description="Recompute every spreadsheet formula deterministically (arithmetic + SUM/AVERAGE/MIN/MAX/COUNT/PRODUCT/ABS/ROUND) and report each value or the exact unsupported reason.",
                input_schema={
                    "type": "object",
                    "properties": {"path": string_path},
                    "required": ["path"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "formulas": _formula_items_schema(),
                        "formula_count": {"type": "integer"},
                        "computed_count": {"type": "integer"},
                        "unsupported_count": {"type": "integer"},
                    },
                    "required": [
                        "path",
                        "formulas",
                        "formula_count",
                        "computed_count",
                        "unsupported_count",
                    ],
                },
                permissions=(Action.READ_FILE,),
            ),
            _xlsx_verify_formulas,
        ),
    ]


def register_office_tools(registry: ToolRegistry) -> None:
    for spec, handler in _specs():
        registry.register(spec, handler)
