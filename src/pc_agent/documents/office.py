"""Bounded Office/CSV document services built on the Python standard library.

Supported input formats are DOCX, XLSX, PPTX and CSV. The OOXML readers treat
files as untrusted ZIP/XML payloads: archive size, entry count, decompressed
size and each XML part are capped, macro-enabled variants are rejected, and
XML documents containing a DOCTYPE are refused. Writers emit small, valid
DOCX/XLSX packages without adding a runtime dependency.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from ..errors import ToolError

MAX_OFFICE_FILE_BYTES = 25_000_000
MAX_CSV_FILE_BYTES = 10_000_000
MAX_ZIP_ENTRIES = 2_000
MAX_ZIP_UNCOMPRESSED_BYTES = 100_000_000
MAX_XML_PART_BYTES = 10_000_000
MAX_CSV_ROWS = 100_000
MAX_CSV_COLUMNS = 100
MAX_XLSX_ROWS = 100_000
MAX_XLSX_COLUMNS = 256
MAX_XLSX_CELLS = 250_000

_OOXML_SUFFIXES = {".docx", ".xlsx", ".pptx"}
_MACRO_SUFFIXES = {".docm", ".xlsm", ".pptm", ".xlsb"}
_NUMBER_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")
_CELL_RE = re.compile(r"^([A-Z]+)([0-9]+)$", re.IGNORECASE)
_WORD_RE = re.compile(r"\S+")

_NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}


@dataclass(frozen=True, slots=True)
class OfficeInspection:
    path: Path
    format: str
    size_bytes: int
    sha256: str
    title: str | None
    creator: str | None
    unit_name: str
    unit_count: int
    words: int
    characters: int
    details: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExtractedText:
    format: str
    text: str
    total_characters: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class CsvData:
    columns: tuple[str, ...]
    rows: tuple[dict[str, str], ...]
    raw_rows: tuple[tuple[str, ...], ...]
    has_header: bool
    delimiter: str
    malformed_rows: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CsvColumnStats:
    name: str
    nonempty: int
    numeric_count: int
    distinct_count: int
    minimum: float | None
    maximum: float | None
    mean: float | None


@dataclass(frozen=True, slots=True)
class CsvInspection:
    data: CsvData
    sample_rows: tuple[dict[str, str], ...]
    column_stats: tuple[CsvColumnStats, ...]


@dataclass(slots=True)
class _SheetData:
    name: str
    rows: list[list[str]] = field(default_factory=list)
    nonempty_cells: int = 0
    formulas: int = 0


@dataclass(slots=True)
class _SlideData:
    title: str | None
    lines: list[str] = field(default_factory=list)
    shapes: int = 0
    pictures: int = 0


@dataclass(frozen=True, slots=True)
class DemoOfficeData:
    created: tuple[str, ...]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_office(path: Path) -> OfficeInspection:
    suffix = _require_supported_input(path)
    if suffix == ".csv":
        inspection = inspect_csv(path, sample_rows=0)
        data = inspection.data
        text = _csv_text(data)
        return OfficeInspection(
            path=path,
            format="csv",
            size_bytes=data.size_bytes,
            sha256=data.sha256,
            title=None,
            creator=None,
            unit_name="rows",
            unit_count=len(data.rows),
            words=len(_WORD_RE.findall(text)),
            characters=len(text),
            details={
                "columns": list(data.columns),
                "column_count": len(data.columns),
                "row_count": len(data.rows),
                "has_header": data.has_header,
                "delimiter": data.delimiter,
                "malformed_rows": data.malformed_rows,
            },
        )

    with _open_ooxml(path) as package:
        title, creator = _core_properties(package)
        if suffix == ".docx":
            lines, details = _parse_docx(package)
            text = "\n".join(lines)
            unit_name = "paragraphs"
            unit_count = int(details["paragraphs"])
            file_format = "docx"
        elif suffix == ".xlsx":
            sheets = _parse_xlsx(package)
            text = _xlsx_text(sheets)
            details = {
                "sheets": [
                    {
                        "name": sheet.name,
                        "rows": len(sheet.rows),
                        "columns": max((len(row) for row in sheet.rows), default=0),
                        "nonempty_cells": sheet.nonempty_cells,
                        "formulas": sheet.formulas,
                    }
                    for sheet in sheets
                ],
                "sheet_count": len(sheets),
            }
            unit_name = "sheets"
            unit_count = len(sheets)
            file_format = "xlsx"
        else:
            slides = _parse_pptx(package)
            text = _pptx_text(slides)
            details = {
                "slides": [
                    {
                        "title": slide.title,
                        "text_lines": len(slide.lines),
                        "shapes": slide.shapes,
                        "pictures": slide.pictures,
                    }
                    for slide in slides
                ],
                "slide_count": len(slides),
            }
            unit_name = "slides"
            unit_count = len(slides)
            file_format = "pptx"

    return OfficeInspection(
        path=path,
        format=file_format,
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
        title=title,
        creator=creator,
        unit_name=unit_name,
        unit_count=unit_count,
        words=len(_WORD_RE.findall(text)),
        characters=len(text),
        details=details,
    )


def extract_office_text(path: Path, *, max_chars: int = 100_000) -> ExtractedText:
    if max_chars < 1 or max_chars > 200_000:
        raise ToolError("max_chars must be between 1 and 200000.")
    suffix = _require_supported_input(path)
    if suffix == ".csv":
        full_text = _csv_text(read_csv(path))
        file_format = "csv"
    else:
        with _open_ooxml(path) as package:
            if suffix == ".docx":
                lines, _details = _parse_docx(package)
                full_text = "\n".join(lines)
                file_format = "docx"
            elif suffix == ".xlsx":
                full_text = _xlsx_text(_parse_xlsx(package))
                file_format = "xlsx"
            else:
                full_text = _pptx_text(_parse_pptx(package))
                file_format = "pptx"
    return ExtractedText(
        format=file_format,
        text=full_text[:max_chars],
        total_characters=len(full_text),
        truncated=len(full_text) > max_chars,
    )


def read_csv(path: Path) -> CsvData:
    if not path.is_file():
        raise ToolError(f"CSV file not found: {path.name}")
    if path.suffix.lower() != ".csv":
        raise ToolError(f"Not a CSV file: {path.name}")
    size = path.stat().st_size
    if size > MAX_CSV_FILE_BYTES:
        raise ToolError("CSV file exceeds the 10 MB safety limit.")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ToolError("CSV file must be valid UTF-8 text.") from exc

    sample = text[:8192]
    sniffer = csv.Sniffer()
    try:
        dialect = sniffer.sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    delimiter = _detect_csv_delimiter(
        sample, str(getattr(dialect, "delimiter", ",") or ",")
    )
    try:
        has_header = bool(sniffer.has_header(sample)) if sample.strip() else False
    except csv.Error:
        has_header = False

    raw_rows: list[tuple[str, ...]] = []
    try:
        reader = csv.reader(
            io.StringIO(text, newline=""),
            dialect=dialect,
            delimiter=delimiter,
            strict=True,
        )
        for row in reader:
            raw_rows.append(tuple(str(cell) for cell in row))
            if len(raw_rows) > MAX_CSV_ROWS + 1:
                raise ToolError("CSV file exceeds the 100000-row safety limit.")
    except csv.Error as exc:
        raise ToolError(f"Could not parse CSV: {exc}") from exc
    if not raw_rows:
        raise ToolError("CSV file is empty.")

    width = max(len(row) for row in raw_rows)
    if width < 1 or width > MAX_CSV_COLUMNS:
        raise ToolError("CSV column count must be between 1 and 100.")
    malformed = sum(1 for row in raw_rows if len(row) != width)
    padded = tuple(row + ("",) * (width - len(row)) for row in raw_rows)
    if not has_header and len(padded) >= 2:
        first_row = padded[0]
        has_header = (
            all(value.strip() for value in first_row)
            and not any(_is_number(value) for value in first_row)
            and any(_is_number(value) for row in padded[1:] for value in row)
        )
    if has_header:
        columns = _normalize_columns(padded[0])
        data_rows = padded[1:]
    else:
        columns = tuple(f"column_{index}" for index in range(1, width + 1))
        data_rows = padded
    records = tuple(
        {columns[index]: row[index] for index in range(width)} for row in data_rows
    )
    if len(records) > MAX_CSV_ROWS:
        raise ToolError("CSV file exceeds the 100000-row safety limit.")
    return CsvData(
        columns=columns,
        rows=records,
        raw_rows=padded,
        has_header=has_header,
        delimiter=delimiter,
        malformed_rows=malformed,
        size_bytes=len(raw),
        sha256=sha256_bytes(raw),
    )


def _detect_csv_delimiter(sample: str, fallback: str) -> str:
    first_line = next(
        (line for line in sample.splitlines() if line.strip()), ""
    )
    if not first_line:
        return fallback if fallback in {",", ";", "\t", "|"} else ","
    candidates = [",", ";", "\t", "|"]
    widths: dict[str, int] = {}
    for candidate in candidates:
        try:
            parsed = next(
                csv.reader(io.StringIO(first_line, newline=""), delimiter=candidate)
            )
        except (csv.Error, StopIteration):
            continue
        widths[candidate] = len(parsed)
    if not widths:
        return fallback if fallback in candidates else ","
    best_width = max(widths.values())
    if best_width <= 1:
        return fallback if fallback in candidates else ","
    if widths.get(fallback) == best_width:
        return fallback
    return max(candidates, key=lambda candidate: widths.get(candidate, 0))


def _is_number(value: str) -> bool:
    try:
        return math.isfinite(float(value.strip()))
    except ValueError:
        return False


def inspect_csv(path: Path, *, sample_rows: int = 5) -> CsvInspection:
    if sample_rows < 0 or sample_rows > 20:
        raise ToolError("sample_rows must be between 0 and 20.")
    data = read_csv(path)
    stats: list[CsvColumnStats] = []
    for column in data.columns:
        values = [row[column] for row in data.rows]
        nonempty_values = [value for value in values if value.strip()]
        numbers: list[float] = []
        for value in nonempty_values:
            try:
                number = float(value.strip())
            except ValueError:
                continue
            if math.isfinite(number):
                numbers.append(number)
        stats.append(
            CsvColumnStats(
                name=column,
                nonempty=len(nonempty_values),
                numeric_count=len(numbers),
                distinct_count=len(set(nonempty_values)),
                minimum=min(numbers) if numbers else None,
                maximum=max(numbers) if numbers else None,
                mean=(sum(numbers) / len(numbers)) if numbers else None,
            )
        )
    return CsvInspection(
        data=data,
        sample_rows=data.rows[:sample_rows],
        column_stats=tuple(stats),
    )


def build_docx(*, title: str, paragraphs: list[str]) -> bytes:
    clean_title = _xml_safe(title.strip())
    if not clean_title or len(clean_title) > 200:
        raise ToolError("A DOCX title of 1 to 200 characters is required.")
    if len(paragraphs) > 1000:
        raise ToolError("A generated DOCX may contain at most 1000 paragraphs.")
    clean_paragraphs = [_xml_safe(str(item)) for item in paragraphs]
    if any(len(item) > 10_000 for item in clean_paragraphs):
        raise ToolError("Each generated DOCX paragraph is limited to 10000 characters.")

    for prefix, uri in _NS.items():
        ET.register_namespace(prefix, uri)
    w = _NS["w"]
    document = ET.Element(f"{{{w}}}document")
    body = ET.SubElement(document, f"{{{w}}}body")
    _docx_paragraph(body, clean_title, title=True)
    for paragraph in clean_paragraphs:
        _docx_paragraph(body, paragraph, title=False)
    section = ET.SubElement(body, f"{{{w}}}sectPr")
    page_size = ET.SubElement(section, f"{{{w}}}pgSz")
    page_size.set(f"{{{w}}}w", "12240")
    page_size.set(f"{{{w}}}h", "15840")
    margins = ET.SubElement(section, f"{{{w}}}pgMar")
    for attr in ("top", "right", "bottom", "left"):
        margins.set(f"{{{w}}}{attr}", "1440")

    core = ET.Element(f"{{{_NS['cp']}}}coreProperties")
    title_element = ET.SubElement(core, f"{{{_NS['dc']}}}title")
    title_element.text = clean_title
    creator_element = ET.SubElement(core, f"{{{_NS['dc']}}}creator")
    creator_element.text = "Self-Healing PC Agent"

    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>"""
    relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>"""
    return _zip_package(
        {
            "[Content_Types].xml": content_types.encode("utf-8"),
            "_rels/.rels": relationships.encode("utf-8"),
            "word/document.xml": _xml_bytes(document),
            "docProps/core.xml": _xml_bytes(core),
        }
    )


def build_xlsx(rows: list[list[str]], *, sheet_name: str = "Sheet1") -> bytes:
    clean_sheet = _validate_sheet_name(sheet_name)
    if not rows:
        raise ToolError("A generated XLSX needs at least one row.")
    if len(rows) > MAX_CSV_ROWS + 1:
        raise ToolError("A generated XLSX exceeds the row safety limit.")
    width = max(len(row) for row in rows)
    if width < 1 or width > MAX_CSV_COLUMNS:
        raise ToolError("A generated XLSX must have between 1 and 100 columns.")

    for prefix, uri in _NS.items():
        ET.register_namespace(prefix, uri)
    main = _NS["main"]
    rel = _NS["r"]
    worksheet = ET.Element(f"{{{main}}}worksheet")
    dimension = ET.SubElement(worksheet, f"{{{main}}}dimension")
    dimension.set("ref", f"A1:{_column_letters(width)}{len(rows)}")
    sheet_data = ET.SubElement(worksheet, f"{{{main}}}sheetData")
    for row_index, row in enumerate(rows, start=1):
        row_element = ET.SubElement(sheet_data, f"{{{main}}}row")
        row_element.set("r", str(row_index))
        for col_index in range(width):
            value = _xml_safe(str(row[col_index])) if col_index < len(row) else ""
            cell = ET.SubElement(row_element, f"{{{main}}}c")
            cell.set("r", f"{_column_letters(col_index + 1)}{row_index}")
            if _NUMBER_RE.fullmatch(value.strip()):
                value_element = ET.SubElement(cell, f"{{{main}}}v")
                value_element.text = value.strip()
            else:
                cell.set("t", "inlineStr")
                inline = ET.SubElement(cell, f"{{{main}}}is")
                text_element = ET.SubElement(inline, f"{{{main}}}t")
                if value[:1].isspace() or value[-1:].isspace():
                    text_element.set(
                        "{http://www.w3.org/XML/1998/namespace}space", "preserve"
                    )
                text_element.text = value

    workbook = ET.Element(f"{{{main}}}workbook")
    sheets = ET.SubElement(workbook, f"{{{main}}}sheets")
    sheet = ET.SubElement(sheets, f"{{{main}}}sheet")
    sheet.set("name", clean_sheet)
    sheet.set("sheetId", "1")
    sheet.set(f"{{{rel}}}id", "rId1")

    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""
    relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
    workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    return _zip_package(
        {
            "[Content_Types].xml": content_types.encode("utf-8"),
            "_rels/.rels": relationships.encode("utf-8"),
            "xl/workbook.xml": _xml_bytes(workbook),
            "xl/_rels/workbook.xml.rels": workbook_rels.encode("utf-8"),
            "xl/worksheets/sheet1.xml": _xml_bytes(worksheet),
        }
    )


def create_demo_office_files(workspace_dir: Path) -> DemoOfficeData:
    folder = workspace_dir / "Office Demo"
    folder.mkdir(parents=True, exist_ok=True)
    rows = [
        ["product", "units", "revenue"],
        ["Alpha", "12", "1200"],
        ["Beta", "7", "875"],
        ["Gamma", "20", "3100"],
    ]
    csv_text = "".join(",".join(row) + "\n" for row in rows)
    records = [dict(zip(rows[0], row, strict=True)) for row in rows[1:]]
    files: list[tuple[Path, bytes]] = [
        (folder / "sales.csv", csv_text.encode("utf-8")),
        (
            folder / "sales.json",
            (json.dumps(records, indent=2) + "\n").encode("utf-8"),
        ),
        (
            folder / "sales.xlsx",
            build_xlsx(rows, sheet_name="Sales"),
        ),
        (
            folder / "summary.docx",
            build_docx(
                title="Sales Summary",
                paragraphs=[
                    "Gamma leads with 20 units and revenue of 3100.",
                    "This deterministic demo document was generated locally.",
                ],
            ),
        ),
    ]
    created: list[str] = []
    for target, data in files:
        if not target.exists():
            with target.open("xb") as handle:
                handle.write(data)
        created.append(target.relative_to(workspace_dir).as_posix())
    return DemoOfficeData(created=tuple(created))


def _require_supported_input(path: Path) -> str:
    if not path.is_file():
        raise ToolError(f"Office file not found: {path.name}")
    suffix = path.suffix.lower()
    if suffix in _MACRO_SUFFIXES:
        raise ToolError("Macro-enabled or binary Office formats are not supported.")
    if suffix not in _OOXML_SUFFIXES | {".csv"}:
        raise ToolError("Only DOCX, XLSX, PPTX and CSV files are supported.")
    limit = MAX_CSV_FILE_BYTES if suffix == ".csv" else MAX_OFFICE_FILE_BYTES
    if path.stat().st_size > limit:
        raise ToolError("Office file exceeds its safety size limit.")
    return suffix


def _open_ooxml(path: Path) -> zipfile.ZipFile:
    try:
        package = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ToolError(f"Not a valid Office ZIP package: {path.name}") from exc
    infos = package.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        package.close()
        raise ToolError("Office package contains too many entries.")
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        package.close()
        raise ToolError("Office package contains duplicate entries.")
    total = 0
    for info in infos:
        if info.flag_bits & 0x1:
            package.close()
            raise ToolError("Encrypted Office packages are not supported.")
        total += info.file_size
        if info.file_size > MAX_XML_PART_BYTES and info.filename.lower().endswith(".xml"):
            package.close()
            raise ToolError("Office package contains an oversized XML part.")
    if total > MAX_ZIP_UNCOMPRESSED_BYTES:
        package.close()
        raise ToolError("Office package decompresses beyond the safety limit.")
    return package


def _xml_root(package: zipfile.ZipFile, part: str, *, required: bool = True) -> Any:
    try:
        info = package.getinfo(part)
    except KeyError:
        if required:
            raise ToolError(f"Office package is missing a required part: {part}") from None
        return None
    if info.file_size > MAX_XML_PART_BYTES:
        raise ToolError("Office package contains an oversized XML part.")
    data = package.read(part)
    if len(data) > MAX_XML_PART_BYTES:
        raise ToolError("Office package contains an oversized XML part.")
    if b"<!doctype" in data.lower():
        raise ToolError("Office XML containing a DOCTYPE is rejected for safety.")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ToolError(f"Office XML could not be parsed: {part}") from exc


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _iter_local(root: Any, name: str):
    return (element for element in root.iter() if _local(element.tag) == name)


def _first_text(root: Any, name: str) -> str | None:
    for element in _iter_local(root, name):
        text = "".join(element.itertext()).strip()
        if text:
            return text
    return None


def _core_properties(package: zipfile.ZipFile) -> tuple[str | None, str | None]:
    root = _xml_root(package, "docProps/core.xml", required=False)
    if root is None:
        return None, None
    return _first_text(root, "title"), _first_text(root, "creator")


def _paragraph_text(paragraph: Any) -> str:
    pieces: list[str] = []
    for node in paragraph.iter():
        name = _local(node.tag)
        if name == "t":
            pieces.append(node.text or "")
        elif name in {"tab"}:
            pieces.append("\t")
        elif name in {"br", "cr"}:
            pieces.append("\n")
    return "".join(pieces).strip()


def _parse_docx(package: zipfile.ZipFile) -> tuple[list[str], dict[str, Any]]:
    root = _xml_root(package, "word/document.xml")
    paragraphs = [_paragraph_text(item) for item in _iter_local(root, "p")]
    lines = [item for item in paragraphs if item]
    details = {
        "paragraphs": len(paragraphs),
        "nonempty_paragraphs": len(lines),
        "tables": sum(1 for _ in _iter_local(root, "tbl")),
    }
    return lines, details


def _relationships(package: zipfile.ZipFile, part: str) -> dict[str, str]:
    root = _xml_root(package, part, required=False)
    if root is None:
        return {}
    result: dict[str, str] = {}
    for item in _iter_local(root, "Relationship"):
        rel_id = item.attrib.get("Id")
        target = item.attrib.get("Target")
        if rel_id and target:
            result[rel_id] = target
    return result


def _worksheet_part(target: str) -> str:
    clean = target.replace("\\", "/").lstrip("/")
    if clean.startswith("xl/"):
        return clean
    return f"xl/{clean}"


def _cell_column(reference: str | None, fallback: int) -> int:
    if not reference:
        return fallback
    match = _CELL_RE.fullmatch(reference.strip())
    if not match:
        return fallback
    value = 0
    for character in match.group(1).upper():
        value = value * 26 + (ord(character) - ord("A") + 1)
    return value


def _parse_xlsx(package: zipfile.ZipFile) -> list[_SheetData]:
    workbook = _xml_root(package, "xl/workbook.xml")
    rels = _relationships(package, "xl/_rels/workbook.xml.rels")
    shared: list[str] = []
    shared_root = _xml_root(package, "xl/sharedStrings.xml", required=False)
    if shared_root is not None:
        for item in _iter_local(shared_root, "si"):
            shared.append(
                "".join(
                    str(node.text or "")
                    for node in item.iter()
                    if _local(node.tag) == "t"
                )
            )

    sheets: list[_SheetData] = []
    total_rows = 0
    total_cells = 0
    for sheet_element in _iter_local(workbook, "sheet"):
        name = str(sheet_element.attrib.get("name") or f"Sheet{len(sheets) + 1}")
        rel_id = sheet_element.attrib.get(f"{{{_NS['r']}}}id")
        target = rels.get(str(rel_id)) if rel_id else None
        part = _worksheet_part(target) if target else f"xl/worksheets/sheet{len(sheets) + 1}.xml"
        sheet_root = _xml_root(package, part)
        parsed_rows: dict[int, dict[int, str]] = {}
        nonempty = 0
        formulas = 0
        for row_element in _iter_local(sheet_root, "row"):
            total_rows += 1
            if total_rows > MAX_XLSX_ROWS:
                raise ToolError("XLSX workbook exceeds the 100000-row safety limit.")
            try:
                row_number = int(row_element.attrib.get("r", "0"))
            except ValueError:
                row_number = max(parsed_rows, default=0) + 1
            if row_number < 1:
                row_number = max(parsed_rows, default=0) + 1
            values = parsed_rows.setdefault(row_number, {})
            fallback_column = 1
            for cell in row_element:
                if _local(cell.tag) != "c":
                    continue
                total_cells += 1
                if total_cells > MAX_XLSX_CELLS:
                    raise ToolError("XLSX workbook exceeds the 250000-cell safety limit.")
                column = _cell_column(cell.attrib.get("r"), fallback_column)
                if column > MAX_XLSX_COLUMNS:
                    raise ToolError("XLSX workbook exceeds the 256-column safety limit.")
                fallback_column = column + 1
                if any(_local(child.tag) == "f" for child in cell):
                    formulas += 1
                cell_type = str(cell.attrib.get("t") or "")
                raw_value = _first_text(cell, "v")
                if cell_type == "s" and raw_value is not None:
                    try:
                        value = shared[int(float(raw_value))]
                    except (ValueError, IndexError):
                        value = raw_value
                elif cell_type == "inlineStr":
                    value = "".join(
                        str(node.text or "")
                        for node in cell.iter()
                        if _local(node.tag) == "t"
                    )
                elif cell_type == "b":
                    value = "TRUE" if raw_value == "1" else "FALSE"
                else:
                    value = raw_value or ""
                values[column] = value
                if value.strip():
                    nonempty += 1
        rows: list[list[str]] = []
        for row_number in sorted(parsed_rows):
            values = parsed_rows[row_number]
            if not any(value.strip() for value in values.values()):
                continue
            width = max(values, default=0)
            rows.append([values.get(index, "") for index in range(1, width + 1)])
        sheets.append(
            _SheetData(
                name=name,
                rows=rows,
                nonempty_cells=nonempty,
                formulas=formulas,
            )
        )
    return sheets


def _xlsx_text(sheets: list[_SheetData]) -> str:
    sections: list[str] = []
    for sheet in sheets:
        lines = [f"# Sheet: {sheet.name}"]
        lines.extend("\t".join(row) for row in sheet.rows)
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _pptx_slide_parts(package: zipfile.ZipFile) -> list[str]:
    fallback = [
        name
        for name in package.namelist()
        if re.fullmatch(r"ppt/slides/slide[0-9]+\.xml", name)
    ]
    fallback.sort(
        key=lambda item: int(re.search(r"([0-9]+)\.xml$", item).group(1))  # type: ignore[union-attr]
    )
    presentation = _xml_root(
        package, "ppt/presentation.xml", required=False
    )
    if presentation is None:
        return fallback
    relationships = _relationships(
        package, "ppt/_rels/presentation.xml.rels"
    )
    available = set(package.namelist())
    ordered: list[str] = []
    for item in _iter_local(presentation, "sldId"):
        rel_id = item.attrib.get(f"{{{_NS['r']}}}id") or item.attrib.get("r:id")
        target = relationships.get(str(rel_id)) if rel_id else None
        if not target:
            continue
        clean = target.replace("\\", "/").lstrip("/")
        part = clean if clean.startswith("ppt/") else f"ppt/{clean}"
        if part in available and part not in ordered:
            ordered.append(part)
    return ordered or fallback


def _parse_pptx(package: zipfile.ZipFile) -> list[_SlideData]:
    names = _pptx_slide_parts(package)
    slides: list[_SlideData] = []
    for name in names:
        root = _xml_root(package, name)
        lines: list[str] = []
        for paragraph in _iter_local(root, "p"):
            # Presentation paragraphs use a:p; only collect paragraphs that
            # contain DrawingML text nodes to avoid other p:* structures.
            text = _paragraph_text(paragraph)
            if text:
                lines.append(text)
        slides.append(
            _SlideData(
                title=lines[0] if lines else None,
                lines=lines,
                shapes=sum(1 for _ in _iter_local(root, "sp")),
                pictures=sum(1 for _ in _iter_local(root, "pic")),
            )
        )
    return slides


def _pptx_text(slides: list[_SlideData]) -> str:
    sections: list[str] = []
    for index, slide in enumerate(slides, start=1):
        sections.append("\n".join([f"# Slide {index}", *slide.lines]))
    return "\n\n".join(sections)


def _csv_text(data: CsvData) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter="\t", lineterminator="\n")
    writer.writerow(data.columns)
    for row in data.rows:
        writer.writerow([row[column] for column in data.columns])
    return output.getvalue().rstrip("\n")


def _normalize_columns(raw: tuple[str, ...]) -> tuple[str, ...]:
    columns: list[str] = []
    seen: dict[str, int] = {}
    for index, value in enumerate(raw, start=1):
        base = value.strip() or f"column_{index}"
        count = seen.get(base, 0) + 1
        seen[base] = count
        columns.append(base if count == 1 else f"{base}_{count}")
    return tuple(columns)


def _validate_sheet_name(name: str) -> str:
    clean = _xml_safe(name.strip())
    if not clean or len(clean) > 31 or any(char in clean for char in "[]:*?/\\"):
        raise ToolError("Sheet name must be 1-31 characters and avoid []:*?/\\.")
    return clean


def _column_letters(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _xml_safe(value: str) -> str:
    return "".join(
        character
        for character in value
        if character in {"\t", "\n", "\r"}
        or (ord(character) >= 0x20 and not 0xD800 <= ord(character) <= 0xDFFF)
    )


def _xml_bytes(root: Any) -> bytes:
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def _zip_package(parts: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name, data in parts.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            package.writestr(info, data)
    return buffer.getvalue()


def _docx_paragraph(body: Any, text: str, *, title: bool) -> None:
    w = _NS["w"]
    paragraph = ET.SubElement(body, f"{{{w}}}p")
    if title:
        properties = ET.SubElement(paragraph, f"{{{w}}}pPr")
        alignment = ET.SubElement(properties, f"{{{w}}}jc")
        alignment.set(f"{{{w}}}val", "center")
    for index, line in enumerate(text.splitlines() or [""]):
        run = ET.SubElement(paragraph, f"{{{w}}}r")
        if title:
            run_properties = ET.SubElement(run, f"{{{w}}}rPr")
            ET.SubElement(run_properties, f"{{{w}}}b")
            size = ET.SubElement(run_properties, f"{{{w}}}sz")
            size.set(f"{{{w}}}val", "32")
        if index:
            ET.SubElement(run, f"{{{w}}}br")
        text_element = ET.SubElement(run, f"{{{w}}}t")
        if line[:1].isspace() or line[-1:].isspace():
            text_element.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        text_element.text = line


# ---------------------------------------------------------------------------
# Phase 9: DOCX editing, XLSX cell editing and formula evaluation
# ---------------------------------------------------------------------------

_MAX_FORMULA_RANGE_CELLS = 10_000
_MAX_CELL_EDITS = 100


def _read_package_parts(path: Path, expected_suffix: str) -> dict[str, bytes]:
    suffix = _require_supported_input(path)
    if suffix != expected_suffix:
        raise ToolError(f"Expected a {expected_suffix} file: {path.name}")
    with _open_ooxml(path) as package:
        return {
            info.filename: package.read(info.filename)
            for info in package.infolist()
            if not info.is_dir()
        }


def _register_namespaces_from(data: bytes) -> None:
    for prefix, uri in _NS.items():
        ET.register_namespace(prefix, uri)
    try:
        for _event, namespace in ET.iterparse(io.BytesIO(data), events=("start-ns",)):
            prefix, uri = namespace
            if prefix and prefix != "xml":
                try:
                    ET.register_namespace(prefix, uri)
                except ValueError:
                    pass
    except ET.ParseError:
        pass


def _parse_xml_bytes(data: bytes, part: str) -> Any:
    if b"<!doctype" in data.lower():
        raise ToolError("Office XML containing a DOCTYPE is rejected for safety.")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ToolError(f"Office XML could not be parsed: {part}") from exc


@dataclass(frozen=True, slots=True)
class DocxReplaceResult:
    data: bytes
    replacements: int
    paragraphs_changed: int


@dataclass(frozen=True, slots=True)
class DocxAppendResult:
    data: bytes
    paragraphs_added: int
    total_paragraphs: int


def replace_docx_text(path: Path, find: str, replace: str) -> DocxReplaceResult:
    """Replace exact text inside DOCX body paragraphs (run-boundary safe)."""

    if not find:
        raise ToolError("The text to find must not be empty.")
    if len(find) > 1000 or len(replace) > 10_000:
        raise ToolError("Find/replace text exceeds its safety limit.")
    parts = _read_package_parts(path, ".docx")
    document_bytes = parts.get("word/document.xml")
    if document_bytes is None:
        raise ToolError("DOCX package is missing word/document.xml.")
    _register_namespaces_from(document_bytes)
    root = _parse_xml_bytes(document_bytes, "word/document.xml")

    replacements = 0
    changed = 0
    for paragraph in _iter_local(root, "p"):
        text_nodes = [node for node in paragraph.iter() if _local(node.tag) == "t"]
        if not text_nodes:
            continue
        joined = "".join(node.text or "" for node in text_nodes)
        occurrences = joined.count(find)
        if not occurrences:
            continue
        new_text = joined.replace(find, replace)
        first = text_nodes[0]
        if new_text[:1].isspace() or new_text[-1:].isspace():
            first.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        first.text = new_text
        for extra in text_nodes[1:]:
            extra.text = ""
        replacements += occurrences
        changed += 1

    parts["word/document.xml"] = _xml_bytes(root)
    return DocxReplaceResult(
        data=_zip_package(parts),
        replacements=replacements,
        paragraphs_changed=changed,
    )


def append_docx_paragraphs(path: Path, paragraphs: list[str]) -> DocxAppendResult:
    clean = [_xml_safe(str(item)) for item in paragraphs]
    if not clean or len(clean) > 100:
        raise ToolError("Append between 1 and 100 paragraphs at a time.")
    if any(len(item) > 10_000 for item in clean):
        raise ToolError("Each appended paragraph is limited to 10000 characters.")

    parts = _read_package_parts(path, ".docx")
    document_bytes = parts.get("word/document.xml")
    if document_bytes is None:
        raise ToolError("DOCX package is missing word/document.xml.")
    _register_namespaces_from(document_bytes)
    root = _parse_xml_bytes(document_bytes, "word/document.xml")
    body = next((child for child in root if _local(child.tag) == "body"), None)
    if body is None:
        raise ToolError("DOCX document has no body element.")
    section = next((child for child in body if _local(child.tag) == "sectPr"), None)
    for paragraph in clean:
        _docx_paragraph(body, paragraph, title=False)
    if section is not None:
        body.remove(section)
        body.append(section)  # section properties must stay last
    total = sum(1 for _ in _iter_local(root, "p"))
    parts["word/document.xml"] = _xml_bytes(root)
    return DocxAppendResult(
        data=_zip_package(parts),
        paragraphs_added=len(clean),
        total_paragraphs=total,
    )


@dataclass(frozen=True, slots=True)
class XlsxCell:
    reference: str
    value: str
    formula: str | None
    kind: str  # "number" | "string" | "boolean" | "empty"


@dataclass(frozen=True, slots=True)
class XlsxSheetCells:
    name: str
    cells: dict[str, XlsxCell]


@dataclass(frozen=True, slots=True)
class XlsxWorkbookCells:
    sheets: tuple[XlsxSheetCells, ...]


@dataclass(frozen=True, slots=True)
class FormulaEvaluation:
    sheet: str
    reference: str
    formula: str
    value: float | None
    error: str | None


@dataclass(frozen=True, slots=True)
class XlsxEditResult:
    data: bytes
    sheet: str
    cells_set: tuple[str, ...]
    evaluations: tuple[FormulaEvaluation, ...]


def _reference_parts(reference: str) -> tuple[int, int]:
    match = _CELL_RE.fullmatch(reference.strip().replace("$", ""))
    if not match:
        raise ToolError(f"Invalid cell reference: {reference!r}")
    column = 0
    for character in match.group(1).upper():
        column = column * 26 + (ord(character) - ord("A") + 1)
    return column, int(match.group(2))


def _normalize_reference(reference: str) -> str:
    column, row = _reference_parts(reference)
    if column > MAX_XLSX_COLUMNS or row < 1 or row > MAX_XLSX_ROWS:
        raise ToolError(f"Cell reference is outside the safety limits: {reference!r}")
    return f"{_column_letters(column)}{row}"


def read_xlsx_cells(path: Path) -> XlsxWorkbookCells:
    """Read every populated cell (value + formula text) keyed by reference."""

    suffix = _require_supported_input(path)
    if suffix != ".xlsx":
        raise ToolError(f"Expected a .xlsx file: {path.name}")
    with _open_ooxml(path) as package:
        workbook = _xml_root(package, "xl/workbook.xml")
        rels = _relationships(package, "xl/_rels/workbook.xml.rels")
        shared: list[str] = []
        shared_root = _xml_root(package, "xl/sharedStrings.xml", required=False)
        if shared_root is not None:
            for item in _iter_local(shared_root, "si"):
                shared.append(
                    "".join(
                        str(node.text or "")
                        for node in item.iter()
                        if _local(node.tag) == "t"
                    )
                )

        sheets: list[XlsxSheetCells] = []
        total_cells = 0
        for sheet_element in _iter_local(workbook, "sheet"):
            name = str(sheet_element.attrib.get("name") or f"Sheet{len(sheets) + 1}")
            rel_id = sheet_element.attrib.get(f"{{{_NS['r']}}}id")
            target = rels.get(str(rel_id)) if rel_id else None
            part = (
                _worksheet_part(target)
                if target
                else f"xl/worksheets/sheet{len(sheets) + 1}.xml"
            )
            sheet_root = _xml_root(package, part)
            cells: dict[str, XlsxCell] = {}
            for row_element in _iter_local(sheet_root, "row"):
                try:
                    row_number = int(row_element.attrib.get("r", "0"))
                except ValueError:
                    row_number = 0
                fallback_column = 1
                for cell in row_element:
                    if _local(cell.tag) != "c":
                        continue
                    total_cells += 1
                    if total_cells > MAX_XLSX_CELLS:
                        raise ToolError("XLSX workbook exceeds the 250000-cell safety limit.")
                    raw_reference = str(cell.attrib.get("r") or "")
                    try:
                        reference = _normalize_reference(raw_reference)
                        column, _row = _reference_parts(reference)
                    except ToolError:
                        if row_number < 1:
                            continue
                        column = fallback_column
                        reference = f"{_column_letters(column)}{row_number}"
                    fallback_column = column + 1
                    formula_text: str | None = None
                    for child in cell:
                        if _local(child.tag) == "f":
                            formula_text = "".join(child.itertext()).strip() or None
                            break
                    cell_type = str(cell.attrib.get("t") or "")
                    raw_value = _first_text(cell, "v")
                    if cell_type == "s" and raw_value is not None:
                        try:
                            value = shared[int(float(raw_value))]
                        except (ValueError, IndexError):
                            value = raw_value
                        kind = "string"
                    elif cell_type == "inlineStr":
                        value = "".join(
                            str(node.text or "")
                            for node in cell.iter()
                            if _local(node.tag) == "t"
                        )
                        kind = "string"
                    elif cell_type == "b":
                        value = "TRUE" if raw_value == "1" else "FALSE"
                        kind = "boolean"
                    elif cell_type == "str":
                        value = raw_value or ""
                        kind = "string"
                    else:
                        value = raw_value or ""
                        kind = (
                            "number"
                            if value and _NUMBER_RE.fullmatch(value.strip())
                            else "empty"
                            if not value
                            else "string"
                        )
                    cells[reference] = XlsxCell(
                        reference=reference, value=value, formula=formula_text, kind=kind
                    )
            sheets.append(XlsxSheetCells(name=name, cells=cells))
    return XlsxWorkbookCells(sheets=tuple(sheets))


class _FormulaError(Exception):
    """Internal: a formula cell cannot be computed deterministically."""


_TOKEN_RE = re.compile(
    r"\s*(?:(?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"|(?P<ref>\$?[A-Za-z]{1,3}\$?[0-9]+)"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_.]*)"
    r"|(?P<op>[+\-*/(),:]))"
)


def _tokenize_formula(formula: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(formula):
        if formula[position].isspace():
            position += 1
            continue
        match = _TOKEN_RE.match(formula, position)
        if not match:
            raise _FormulaError(f"unsupported formula syntax near {formula[position:position + 10]!r}")
        position = match.end()
        kind = match.lastgroup or ""
        tokens.append((kind, match.group(kind)))
    return tokens


_AGGREGATE_FUNCTIONS = frozenset({"SUM", "AVERAGE", "MIN", "MAX", "COUNT", "PRODUCT"})


class _FormulaEvaluator:
    """Deterministic evaluator for the common arithmetic/aggregate subset."""

    def __init__(self, cells: dict[str, XlsxCell]) -> None:
        self._cells = cells
        self._cache: dict[str, float] = {}
        self._visiting: set[str] = set()

    def evaluate_cell(self, reference: str) -> float:
        if reference in self._cache:
            return self._cache[reference]
        cell = self._cells.get(reference)
        if cell is None or cell.formula is None:
            value = self._plain_number(reference, aggregate=False)
            return 0.0 if value is None else value
        if reference in self._visiting:
            raise _FormulaError("circular reference")
        self._visiting.add(reference)
        try:
            tokens = _tokenize_formula(cell.formula)
            parser = _FormulaParser(tokens, self)
            value = parser.parse()
        finally:
            self._visiting.discard(reference)
        self._cache[reference] = value
        return value

    def _plain_number(self, reference: str, *, aggregate: bool) -> float | None:
        cell = self._cells.get(reference)
        if cell is None:
            return None
        if cell.formula is not None:
            return self.evaluate_cell(reference)
        if cell.kind == "number":
            try:
                return float(cell.value)
            except ValueError as exc:
                raise _FormulaError("#VALUE!") from exc
        if cell.value.strip() == "":
            return None
        if aggregate:
            return None  # aggregate functions ignore text like Excel does
        raise _FormulaError("#VALUE!")

    def scalar(self, reference: str) -> float:
        value = self._plain_number(reference, aggregate=False)
        return 0.0 if value is None else value

    def range_values(self, start: str, end: str) -> list[float]:
        start_col, start_row = _reference_parts(start)
        end_col, end_row = _reference_parts(end)
        low_col, high_col = min(start_col, end_col), max(start_col, end_col)
        low_row, high_row = min(start_row, end_row), max(start_row, end_row)
        if (high_col - low_col + 1) * (high_row - low_row + 1) > _MAX_FORMULA_RANGE_CELLS:
            raise _FormulaError("formula range exceeds the safety limit")
        values: list[float] = []
        for row in range(low_row, high_row + 1):
            for column in range(low_col, high_col + 1):
                number = self._plain_number(
                    f"{_column_letters(column)}{row}", aggregate=True
                )
                if number is not None:
                    values.append(number)
        return values

    def call(self, name: str, arguments: list[list[float]]) -> float:
        function = name.upper()
        flat = [value for group in arguments for value in group]
        if function == "ABS":
            if len(flat) != 1:
                raise _FormulaError("ABS needs exactly one value")
            return abs(flat[0])
        if function == "ROUND":
            if len(flat) not in {1, 2}:
                raise _FormulaError("ROUND needs one or two values")
            digits = int(flat[1]) if len(flat) == 2 else 0
            return round(flat[0], digits)
        if function not in _AGGREGATE_FUNCTIONS:
            raise _FormulaError(f"unsupported function {function}")
        if function == "SUM":
            return math.fsum(flat)
        if function == "PRODUCT":
            result = 1.0
            for value in flat:
                result *= value
            return result if flat else 0.0
        if function == "COUNT":
            return float(len(flat))
        if function == "AVERAGE":
            if not flat:
                raise _FormulaError("#DIV/0!")
            return math.fsum(flat) / len(flat)
        if function == "MIN":
            return min(flat) if flat else 0.0
        return max(flat) if flat else 0.0


class _FormulaParser:
    def __init__(
        self, tokens: list[tuple[str, str]], evaluator: _FormulaEvaluator
    ) -> None:
        self._tokens = tokens
        self._evaluator = evaluator
        self._position = 0

    def parse(self) -> float:
        value = self._expression()
        if self._position != len(self._tokens):
            raise _FormulaError("unexpected extra formula content")
        return value

    def _peek(self) -> tuple[str, str] | None:
        return self._tokens[self._position] if self._position < len(self._tokens) else None

    def _take(self) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise _FormulaError("formula ended unexpectedly")
        self._position += 1
        return token

    def _expression(self) -> float:
        value = self._term()
        while True:
            token = self._peek()
            if token == ("op", "+"):
                self._take()
                value += self._term()
            elif token == ("op", "-"):
                self._take()
                value -= self._term()
            else:
                return value

    def _term(self) -> float:
        value = self._factor()
        while True:
            token = self._peek()
            if token == ("op", "*"):
                self._take()
                value *= self._factor()
            elif token == ("op", "/"):
                self._take()
                divisor = self._factor()
                if divisor == 0:
                    raise _FormulaError("#DIV/0!")
                value /= divisor
            else:
                return value

    def _factor(self) -> float:
        kind, text = self._take()
        if kind == "op" and text == "-":
            return -self._factor()
        if kind == "op" and text == "(":
            value = self._expression()
            if self._take() != ("op", ")"):
                raise _FormulaError("missing closing parenthesis")
            return value
        if kind == "number":
            return float(text)
        if kind == "ref":
            reference = _normalize_reference(text)
            if self._peek() == ("op", ":"):
                raise _FormulaError("a range is only allowed inside a function")
            return self._evaluator.scalar(reference)
        if kind == "name":
            if self._take() != ("op", "("):
                raise _FormulaError(f"unsupported name {text!r}")
            arguments: list[list[float]] = []
            if self._peek() != ("op", ")"):
                while True:
                    arguments.append(self._argument())
                    if self._peek() == ("op", ","):
                        self._take()
                        continue
                    break
            if self._take() != ("op", ")"):
                raise _FormulaError("missing closing parenthesis")
            return self._evaluator.call(text, arguments)
        raise _FormulaError(f"unsupported formula token {text!r}")

    def _argument(self) -> list[float]:
        token = self._peek()
        following = (
            self._tokens[self._position + 1]
            if self._position + 1 < len(self._tokens)
            else None
        )
        if token is not None and token[0] == "ref" and following == ("op", ":"):
            start = _normalize_reference(self._take()[1])
            self._take()  # consume ":"
            end_kind, end_text = self._take()
            if end_kind != "ref":
                raise _FormulaError("a range must end with a cell reference")
            return self._evaluator.range_values(start, _normalize_reference(end_text))
        if token is not None and token[0] == "ref":
            reference = _normalize_reference(token[1])
            value = self._evaluator._plain_number(reference, aggregate=True)
            self._take()
            return [] if value is None else [value]
        return [self._expression()]


def evaluate_workbook_formulas(
    workbook: XlsxWorkbookCells,
) -> tuple[FormulaEvaluation, ...]:
    results: list[FormulaEvaluation] = []
    for sheet in workbook.sheets:
        evaluator = _FormulaEvaluator(sheet.cells)
        for reference in sorted(sheet.cells, key=lambda ref: _reference_parts(ref)[::-1]):
            cell = sheet.cells[reference]
            if cell.formula is None:
                continue
            try:
                value: float | None = evaluator.evaluate_cell(reference)
                error = None
            except (_FormulaError, ToolError) as exc:
                value = None
                error = str(exc)
            results.append(
                FormulaEvaluation(
                    sheet=sheet.name,
                    reference=reference,
                    formula=cell.formula,
                    value=value,
                    error=error,
                )
            )
    return tuple(results)


def _format_number(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return format(value, ".10g")


def set_xlsx_cells(
    path: Path,
    edits: dict[str, str],
    *,
    sheet_name: str | None = None,
) -> XlsxEditResult:
    """Set cells (values or ``=formulas``) in one sheet of an existing XLSX.

    Only the target worksheet XML is rewritten; every other package part is
    preserved byte-for-byte. Formula cells get a freshly computed cached
    value when the deterministic evaluator supports the formula.
    """

    if not edits or len(edits) > _MAX_CELL_EDITS:
        raise ToolError(f"Set between 1 and {_MAX_CELL_EDITS} cells at a time.")
    normalized_edits: dict[str, str] = {}
    for reference, raw_value in edits.items():
        normalized = _normalize_reference(str(reference))
        value = str(raw_value)
        if len(value) > 5000:
            raise ToolError("A cell value exceeds the 5000-character safety limit.")
        normalized_edits[normalized] = _xml_safe(value)

    parts = _read_package_parts(path, ".xlsx")
    workbook_bytes = parts.get("xl/workbook.xml")
    if workbook_bytes is None:
        raise ToolError("XLSX package is missing xl/workbook.xml.")
    _register_namespaces_from(workbook_bytes)
    workbook_root = _parse_xml_bytes(workbook_bytes, "xl/workbook.xml")
    rels: dict[str, str] = {}
    rels_bytes = parts.get("xl/_rels/workbook.xml.rels")
    if rels_bytes is not None:
        rels_root = _parse_xml_bytes(rels_bytes, "xl/_rels/workbook.xml.rels")
        for item in _iter_local(rels_root, "Relationship"):
            if item.attrib.get("Id") and item.attrib.get("Target"):
                rels[item.attrib["Id"]] = item.attrib["Target"]

    sheet_targets: list[tuple[str, str]] = []
    for sheet_element in _iter_local(workbook_root, "sheet"):
        name = str(sheet_element.attrib.get("name") or f"Sheet{len(sheet_targets) + 1}")
        rel_id = sheet_element.attrib.get(f"{{{_NS['r']}}}id")
        target = rels.get(str(rel_id)) if rel_id else None
        part = (
            _worksheet_part(target)
            if target
            else f"xl/worksheets/sheet{len(sheet_targets) + 1}.xml"
        )
        sheet_targets.append((name, part))
    if not sheet_targets:
        raise ToolError("XLSX workbook contains no worksheets.")
    if sheet_name is None:
        chosen_name, chosen_part = sheet_targets[0]
    else:
        matches = [item for item in sheet_targets if item[0] == sheet_name]
        if not matches:
            available = ", ".join(name for name, _part in sheet_targets)
            raise ToolError(f"Sheet {sheet_name!r} not found. Available: {available}.")
        chosen_name, chosen_part = matches[0]
    if chosen_part not in parts:
        raise ToolError(f"XLSX package is missing the worksheet part: {chosen_part}")

    # Model first so cached formula values and the report agree exactly.
    model = read_xlsx_cells(path)
    model_sheet = next(
        (sheet for sheet in model.sheets if sheet.name == chosen_name), None
    )
    cells: dict[str, XlsxCell] = dict(model_sheet.cells) if model_sheet else {}
    for reference, value in normalized_edits.items():
        if value.startswith("="):
            cells[reference] = XlsxCell(reference, "", value[1:].strip(), "number")
        elif _NUMBER_RE.fullmatch(value.strip()):
            cells[reference] = XlsxCell(reference, value.strip(), None, "number")
        elif value.strip().upper() in {"TRUE", "FALSE"}:
            cells[reference] = XlsxCell(reference, value.strip().upper(), None, "boolean")
        elif value == "":
            cells[reference] = XlsxCell(reference, "", None, "empty")
        else:
            cells[reference] = XlsxCell(reference, value, None, "string")
    evaluations = evaluate_workbook_formulas(
        XlsxWorkbookCells(
            sheets=tuple(
                XlsxSheetCells(sheet.name, cells if sheet.name == chosen_name else sheet.cells)
                for sheet in model.sheets
            )
        )
    )
    computed = {
        item.reference: item for item in evaluations if item.sheet == chosen_name
    }

    sheet_bytes = parts[chosen_part]
    _register_namespaces_from(sheet_bytes)
    sheet_root = _parse_xml_bytes(sheet_bytes, chosen_part)
    main = _NS["main"]
    sheet_data = next(
        (child for child in sheet_root if _local(child.tag) == "sheetData"), None
    )
    if sheet_data is None:
        sheet_data = ET.SubElement(sheet_root, f"{{{main}}}sheetData")

    def find_row(row_number: int) -> Any:
        rows = [child for child in sheet_data if _local(child.tag) == "row"]
        for row in rows:
            try:
                if int(row.attrib.get("r", "0")) == row_number:
                    return row
            except ValueError:
                continue
        row = ET.Element(f"{{{main}}}row")
        row.set("r", str(row_number))
        index = len(rows)
        for position, existing in enumerate(rows):
            try:
                if int(existing.attrib.get("r", "0")) > row_number:
                    index = position
                    break
            except ValueError:
                continue
        sheet_data.insert(index, row)
        return row

    for reference, value in sorted(
        normalized_edits.items(), key=lambda item: _reference_parts(item[0])[::-1]
    ):
        column, row_number = _reference_parts(reference)
        row = find_row(row_number)
        cell = next(
            (
                child
                for child in row
                if _local(child.tag) == "c" and child.attrib.get("r") == reference
            ),
            None,
        )
        if cell is None:
            cell = ET.Element(f"{{{main}}}c")
            cell.set("r", reference)
            position = len(list(row))
            for index, existing in enumerate(row):
                if _local(existing.tag) != "c":
                    continue
                try:
                    existing_column, _ = _reference_parts(existing.attrib.get("r", ""))
                except ToolError:
                    continue
                if existing_column > column:
                    position = index
                    break
            row.insert(position, cell)
        for child in list(cell):
            cell.remove(child)
        cell.attrib.pop("t", None)
        if value.startswith("="):
            formula_element = ET.SubElement(cell, f"{{{main}}}f")
            formula_element.text = value[1:].strip()
            evaluation = computed.get(reference)
            if evaluation is not None and evaluation.value is not None:
                value_element = ET.SubElement(cell, f"{{{main}}}v")
                value_element.text = _format_number(evaluation.value)
        elif value == "":
            pass  # cleared cell
        elif _NUMBER_RE.fullmatch(value.strip()):
            value_element = ET.SubElement(cell, f"{{{main}}}v")
            value_element.text = value.strip()
        elif value.strip().upper() in {"TRUE", "FALSE"}:
            cell.set("t", "b")
            value_element = ET.SubElement(cell, f"{{{main}}}v")
            value_element.text = "1" if value.strip().upper() == "TRUE" else "0"
        else:
            cell.set("t", "inlineStr")
            inline = ET.SubElement(cell, f"{{{main}}}is")
            text_element = ET.SubElement(inline, f"{{{main}}}t")
            if value[:1].isspace() or value[-1:].isspace():
                text_element.set(
                    "{http://www.w3.org/XML/1998/namespace}space", "preserve"
                )
            text_element.text = value

    dimension = next(
        (child for child in sheet_root if _local(child.tag) == "dimension"), None
    )
    if dimension is not None:
        max_column = 1
        max_row = 1
        for cell_element in _iter_local(sheet_data, "c"):
            try:
                col, row_idx = _reference_parts(cell_element.attrib.get("r", ""))
            except ToolError:
                continue
            max_column = max(max_column, col)
            max_row = max(max_row, row_idx)
        dimension.set("ref", f"A1:{_column_letters(max_column)}{max_row}")

    parts[chosen_part] = _xml_bytes(sheet_root)
    if "xl/calcChain.xml" in parts:
        del parts["xl/calcChain.xml"]
        content_types = parts.get("[Content_Types].xml")
        if content_types is not None:
            _register_namespaces_from(content_types)
            types_root = _parse_xml_bytes(content_types, "[Content_Types].xml")
            for override in list(types_root):
                if override.attrib.get("PartName") == "/xl/calcChain.xml":
                    types_root.remove(override)
            parts["[Content_Types].xml"] = _xml_bytes(types_root)
    calc_properties = next(
        (child for child in workbook_root if _local(child.tag) == "calcPr"), None
    )
    if calc_properties is not None:
        calc_properties.set("fullCalcOnLoad", "1")
        parts["xl/workbook.xml"] = _xml_bytes(workbook_root)

    return XlsxEditResult(
        data=_zip_package(parts),
        sheet=chosen_name,
        cells_set=tuple(sorted(normalized_edits)),
        evaluations=evaluations,
    )
