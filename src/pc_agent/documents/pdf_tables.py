"""Bounded deterministic table extraction from embedded PDF text.

Phase 23 uses PDFium's embedded-text character boxes to reconstruct words,
lines and column-like cells. It never renders a page image and never performs
OCR: scanned/image-only PDFs simply have no embedded characters to extract.
The geometry heuristic is intentionally conservative and is reported as
text-geometry extraction, not a promise to reproduce complex ruled, merged,
rotated or multi-line cell layouts perfectly.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from ..errors import ToolError
from .pdf import _selected_pages, _write_bytes_atomically, inspect_pdf

MAX_PDF_TABLE_PAGES = 100
MAX_PDF_TABLES = 200
MAX_PDF_TABLE_ROWS_PER_TABLE = 1_000
MAX_PDF_TABLE_TOTAL_ROWS = 10_000
MAX_PDF_TABLE_COLUMNS = 50
MAX_PDF_TABLE_CELLS = 100_000
MAX_PDF_TABLE_CHARACTERS = 2_000_000
MAX_PDF_TABLE_CELL_CHARACTERS = 10_000
DEFAULT_PDF_TABLE_MIN_ROWS = 2
DEFAULT_PDF_TABLE_MIN_COLUMNS = 2


@dataclass(frozen=True, slots=True)
class _CharBox:
    index: int
    char: str
    left: float
    bottom: float
    right: float
    top: float

    @property
    def width(self) -> float:
        return max(0.0, self.right - self.left)

    @property
    def height(self) -> float:
        return max(0.0, self.top - self.bottom)

    @property
    def center_y(self) -> float:
        return (self.bottom + self.top) / 2.0


@dataclass(frozen=True, slots=True)
class _Word:
    text: str
    left: float
    bottom: float
    right: float
    top: float

    @property
    def height(self) -> float:
        return max(0.0, self.top - self.bottom)


@dataclass(frozen=True, slots=True)
class _CellCandidate:
    text: str
    left: float
    bottom: float
    right: float
    top: float

    @property
    def height(self) -> float:
        return max(0.0, self.top - self.bottom)

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2.0


@dataclass(frozen=True, slots=True)
class _Line:
    cells: tuple[_CellCandidate, ...]
    left: float
    bottom: float
    right: float
    top: float

    @property
    def height(self) -> float:
        return max(0.0, self.top - self.bottom)


@dataclass(frozen=True, slots=True)
class PdfTable:
    page: int
    index: int
    rows: tuple[tuple[str, ...], ...]
    bbox: tuple[float, float, float, float]

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return max((len(row) for row in self.rows), default=0)

    @property
    def cell_count(self) -> int:
        return sum(len(row) for row in self.rows)

    @property
    def characters(self) -> int:
        return sum(len(cell) for row in self.rows for cell in row)

    def summary(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "index": self.index,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "cell_count": self.cell_count,
            "characters": self.characters,
        }

    def payload(self) -> dict[str, Any]:
        left, bottom, right, top = self.bbox
        return {
            **self.summary(),
            "bbox": {
                "left": round(left, 2),
                "bottom": round(bottom, 2),
                "right": round(right, 2),
                "top": round(top, 2),
            },
            "rows": [list(row) for row in self.rows],
        }


@dataclass(frozen=True, slots=True)
class PdfTableDocument:
    source_pages: int
    selected_pages: tuple[int, ...]
    text_pages: int
    characters: int
    tables: tuple[PdfTable, ...]

    @property
    def has_embedded_text(self) -> bool:
        return self.text_pages > 0

    @property
    def table_count(self) -> int:
        return len(self.tables)

    @property
    def total_rows(self) -> int:
        return sum(table.row_count for table in self.tables)

    @property
    def total_cells(self) -> int:
        return sum(table.cell_count for table in self.tables)

    @property
    def max_columns(self) -> int:
        return max((table.column_count for table in self.tables), default=0)


@dataclass(frozen=True, slots=True)
class PdfTableExtractionResult:
    output_path: Path
    document: PdfTableDocument
    output_format: str
    size_bytes: int
    sha256: str

    @property
    def source_pages(self) -> int:
        return self.document.source_pages

    @property
    def selected_pages(self) -> tuple[int, ...]:
        return self.document.selected_pages


def _load_pdfium() -> Any:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # Dependency is declared in pyproject.toml.
        raise ToolError(
            'PDF table extraction needs the pypdfium2 package. Run: '
            'python -m pip install -e ".[dev]"'
        ) from exc
    return pdfium


def validate_table_minimum(value: int, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ToolError(f"PDF table {name} must be between 1 and {maximum}.")
    return value


def _median(values: Sequence[float], *, default: float) -> float:
    clean = [float(value) for value in values if value and value > 0]
    return float(median(clean)) if clean else default


def _extract_chars(textpage: Any) -> tuple[tuple[_CharBox, ...], set[int], int]:
    count = int(textpage.count_chars())
    if count < 0:
        raise ToolError("PDF text page reported an invalid character count.")
    if count == 0:
        return (), set(), 0
    full_text = str(textpage.get_text_range(0, count))
    if len(full_text) == count:
        raw_chars = list(full_text)
    else:
        raw_chars = [str(textpage.get_text_range(index, 1)) for index in range(count)]

    chars: list[_CharBox] = []
    space_indices: set[int] = set()
    for index, char in enumerate(raw_chars):
        if char.isspace():
            space_indices.add(index)
            continue
        if not char or ord(char) < 32:
            continue
        box = textpage.get_charbox(index)
        if box is None or len(box) != 4:
            continue
        left, bottom, right, top = (float(value) for value in box)
        left, right = min(left, right), max(left, right)
        bottom, top = min(bottom, top), max(bottom, top)
        if right <= left and top <= bottom:
            continue
        chars.append(_CharBox(index, char, left, bottom, right, top))
    return tuple(chars), space_indices, count


def _same_line(char: _CharBox, line: list[_CharBox]) -> bool:
    bottom = min(item.bottom for item in line)
    top = max(item.top for item in line)
    overlap = min(char.top, top) - max(char.bottom, bottom)
    smallest_height = max(1.0, min(char.height, top - bottom))
    if overlap >= 0.35 * smallest_height:
        return True
    center = (bottom + top) / 2.0
    return abs(char.center_y - center) <= max(2.0, 0.45 * smallest_height)


def _words_from_line(chars: list[_CharBox], space_indices: set[int]) -> tuple[_Word, ...]:
    ordered = sorted(chars, key=lambda item: (item.left, item.index))
    median_height = _median([item.height for item in ordered], default=10.0)
    word_gap = max(3.0, 0.35 * median_height)
    words: list[_Word] = []
    current: list[_CharBox] = []

    def flush() -> None:
        if not current:
            return
        words.append(
            _Word(
                text="".join(item.char for item in current),
                left=min(item.left for item in current),
                bottom=min(item.bottom for item in current),
                right=max(item.right for item in current),
                top=max(item.top for item in current),
            )
        )
        current.clear()

    for char in ordered:
        if current:
            previous = current[-1]
            low, high = sorted((previous.index, char.index))
            explicit_space = any(low < index < high for index in space_indices)
            if explicit_space or char.left - previous.right > word_gap:
                flush()
        current.append(char)
    flush()
    return tuple(words)


def _cells_from_words(words: tuple[_Word, ...]) -> tuple[_CellCandidate, ...]:
    if not words:
        return ()
    median_height = _median([word.height for word in words], default=10.0)
    cell_gap = max(7.5, 0.8 * median_height)
    cells: list[_CellCandidate] = []
    current: list[_Word] = []

    def flush() -> None:
        if not current:
            return
        text = " ".join(word.text for word in current).strip()
        if text:
            cells.append(
                _CellCandidate(
                    text=text,
                    left=min(word.left for word in current),
                    bottom=min(word.bottom for word in current),
                    right=max(word.right for word in current),
                    top=max(word.top for word in current),
                )
            )
        current.clear()

    for word in words:
        if current and word.left - current[-1].right > cell_gap:
            flush()
        current.append(word)
    flush()
    return tuple(cells)


def _lines_from_chars(
    chars: tuple[_CharBox, ...], space_indices: set[int]
) -> tuple[_Line, ...]:
    clusters: list[list[_CharBox]] = []
    for char in sorted(chars, key=lambda item: (-item.center_y, item.left, item.index)):
        for cluster in clusters:
            if _same_line(char, cluster):
                cluster.append(char)
                break
        else:
            clusters.append([char])

    lines: list[_Line] = []
    for cluster in clusters:
        words = _words_from_line(cluster, space_indices)
        cells = _cells_from_words(words)
        if not cells:
            continue
        lines.append(
            _Line(
                cells=cells,
                left=min(cell.left for cell in cells),
                bottom=min(cell.bottom for cell in cells),
                right=max(cell.right for cell in cells),
                top=max(cell.top for cell in cells),
            )
        )
    lines.sort(key=lambda line: (-line.top, line.left))
    return tuple(lines)


def _group_lines(lines: tuple[_Line, ...], min_columns: int) -> list[tuple[_Line, ...]]:
    groups: list[tuple[_Line, ...]] = []
    current: list[_Line] = []

    def flush() -> None:
        if current:
            groups.append(tuple(current))
            current.clear()

    for line in lines:
        if len(line.cells) < min_columns:
            flush()
            continue
        if current:
            previous = current[-1]
            vertical_gap = previous.bottom - line.top
            typical_height = _median(
                [item.height for item in [*current, line]], default=10.0
            )
            if vertical_gap > max(30.0, 3.0 * typical_height):
                flush()
        current.append(line)
    flush()
    return groups


def _table_from_group(
    lines: tuple[_Line, ...],
    *,
    page: int,
    index: int,
    min_rows: int,
    min_columns: int,
) -> PdfTable | None:
    if len(lines) < min_rows:
        return None
    if len(lines) > MAX_PDF_TABLE_ROWS_PER_TABLE:
        raise ToolError(
            "A detected PDF table exceeds the "
            f"{MAX_PDF_TABLE_ROWS_PER_TABLE:,}-row safety limit."
        )
    all_cells = [cell for line in lines for cell in line.cells]
    tolerance = max(3.0, 0.35 * _median([cell.height for cell in all_cells], default=10.0))
    clusters: list[list[float]] = []
    for cell in sorted(all_cells, key=lambda item: (item.left, item.center_x)):
        for cluster in clusters:
            if cell.left <= cluster[1] + tolerance and cell.right >= cluster[0] - tolerance:
                cluster[0] = min(cluster[0], cell.left)
                cluster[1] = max(cluster[1], cell.right)
                break
        else:
            clusters.append([cell.left, cell.right])
    clusters.sort(key=lambda item: item[0])
    if len(clusters) < min_columns:
        return None
    if len(clusters) > MAX_PDF_TABLE_COLUMNS:
        raise ToolError(
            f"A detected PDF table exceeds the {MAX_PDF_TABLE_COLUMNS}-column safety limit."
        )

    rows: list[tuple[str, ...]] = []
    for line in lines:
        values = [""] * len(clusters)
        for cell in line.cells:
            best_index = 0
            best_score: tuple[float, float] | None = None
            for column_index, (left, right) in enumerate(clusters):
                overlap = min(cell.right, right) - max(cell.left, left)
                center_distance = abs(cell.center_x - ((left + right) / 2.0))
                score = (overlap, -center_distance)
                if best_score is None or score > best_score:
                    best_score = score
                    best_index = column_index
            values[best_index] = (
                f"{values[best_index]} {cell.text}".strip()
                if values[best_index]
                else cell.text
            )
            if len(values[best_index]) > MAX_PDF_TABLE_CELL_CHARACTERS:
                raise ToolError(
                    "A detected PDF table cell exceeds the "
                    f"{MAX_PDF_TABLE_CELL_CHARACTERS:,}-character safety limit."
                )
        rows.append(tuple(values))

    useful_rows = [
        row for row in rows if sum(bool(cell.strip()) for cell in row) >= min_columns
    ]
    if len(useful_rows) < min_rows:
        return None
    return PdfTable(
        page=page,
        index=index,
        rows=tuple(useful_rows),
        bbox=(
            min(line.left for line in lines),
            min(line.bottom for line in lines),
            max(line.right for line in lines),
            max(line.top for line in lines),
        ),
    )


def extract_pdf_tables_document(
    input_path: Path,
    pages: Sequence[int] | None = None,
    *,
    min_rows: int = DEFAULT_PDF_TABLE_MIN_ROWS,
    min_columns: int = DEFAULT_PDF_TABLE_MIN_COLUMNS,
) -> PdfTableDocument:
    """Detect bounded tables from embedded PDF text geometry (never OCR)."""

    clean_min_rows = validate_table_minimum(
        min_rows, name="min_rows", maximum=MAX_PDF_TABLE_ROWS_PER_TABLE
    )
    clean_min_columns = validate_table_minimum(
        min_columns, name="min_columns", maximum=MAX_PDF_TABLE_COLUMNS
    )
    info = inspect_pdf(input_path)
    if info.encrypted or info.pages is None:
        raise ToolError("Encrypted PDFs are not supported without a password.")
    selected = _selected_pages(info.pages, pages, limit=MAX_PDF_TABLE_PAGES)

    pdfium = _load_pdfium()
    document: Any | None = None
    tables: list[PdfTable] = []
    text_pages = 0
    total_characters = 0
    try:
        document = pdfium.PdfDocument(str(input_path))
        if len(document) != info.pages:
            raise ToolError("PDF page count changed between inspection and table extraction.")
        for page_number in selected:
            page = document[page_number - 1]
            textpage: Any | None = None
            try:
                textpage = page.get_textpage()
                chars, space_indices, raw_count = _extract_chars(textpage)
            except ToolError:
                raise
            except Exception as exc:
                raise ToolError(
                    f"Could not read embedded text geometry from PDF page "
                    f"{page_number}: {type(exc).__name__}"
                ) from exc
            finally:
                if textpage is not None:
                    textpage.close()
                page.close()

            total_characters += raw_count
            if total_characters > MAX_PDF_TABLE_CHARACTERS:
                raise ToolError(
                    "Embedded PDF text exceeds the "
                    f"{MAX_PDF_TABLE_CHARACTERS:,}-character table safety limit. "
                    "Select a smaller page range."
                )
            if chars:
                text_pages += 1
            lines = _lines_from_chars(chars, space_indices)
            page_index = 1
            for group in _group_lines(lines, clean_min_columns):
                table = _table_from_group(
                    group,
                    page=page_number,
                    index=page_index,
                    min_rows=clean_min_rows,
                    min_columns=clean_min_columns,
                )
                if table is None:
                    continue
                tables.append(table)
                page_index += 1
                if len(tables) > MAX_PDF_TABLES:
                    raise ToolError(
                        f"Detected PDF tables exceed the {MAX_PDF_TABLES}-table safety limit."
                    )
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"Could not open PDF for table extraction: {type(exc).__name__}") from exc
    finally:
        if document is not None:
            document.close()

    result = PdfTableDocument(
        source_pages=info.pages,
        selected_pages=selected,
        text_pages=text_pages,
        characters=total_characters,
        tables=tuple(tables),
    )
    if result.total_rows > MAX_PDF_TABLE_TOTAL_ROWS:
        raise ToolError(
            f"Detected PDF table rows exceed the {MAX_PDF_TABLE_TOTAL_ROWS:,}-row safety limit."
        )
    if result.total_cells > MAX_PDF_TABLE_CELLS:
        raise ToolError(
            f"Detected PDF table cells exceed the {MAX_PDF_TABLE_CELLS:,}-cell safety limit."
        )
    return result


def render_pdf_tables_output(document: PdfTableDocument, output_format: str) -> bytes:
    """Render a table document deterministically as JSON or single-table CSV."""

    normalized = output_format.strip().lower()
    if normalized == "json":
        payload = {
            "method": "pdf-tables-v1",
            "source_pages": document.source_pages,
            "selected_pages": list(document.selected_pages),
            "text_pages": document.text_pages,
            "has_embedded_text": document.has_embedded_text,
            "characters": document.characters,
            "table_count": document.table_count,
            "total_rows": document.total_rows,
            "total_cells": document.total_cells,
            "tables": [table.payload() for table in document.tables],
        }
        return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if normalized == "csv":
        if document.table_count != 1:
            raise ToolError(
                "CSV PDF table output needs exactly one detected table; "
                f"found {document.table_count}. Use JSON output for zero or multiple tables."
            )
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerows(document.tables[0].rows)
        return buffer.getvalue().encode("utf-8")
    raise ToolError("PDF table output format must be 'json' or 'csv'.")


def extract_pdf_tables(
    input_path: Path,
    output_path: Path,
    *,
    pages: Sequence[int] | None = None,
    output_format: str = "json",
    min_rows: int = DEFAULT_PDF_TABLE_MIN_ROWS,
    min_columns: int = DEFAULT_PDF_TABLE_MIN_COLUMNS,
) -> PdfTableExtractionResult:
    """Extract embedded-text PDF tables into a new verified JSON or CSV file."""

    normalized = output_format.strip().lower()
    suffix = output_path.suffix.lower()
    if normalized == "json" and suffix != ".json":
        raise ToolError("JSON PDF table output must end with .json.")
    if normalized == "csv" and suffix != ".csv":
        raise ToolError("CSV PDF table output must end with .csv.")
    if normalized not in {"json", "csv"}:
        raise ToolError("PDF table output format must be 'json' or 'csv'.")
    if input_path.expanduser().resolve() == output_path.expanduser().resolve():
        raise ToolError("The output path must not be the input PDF.")
    if output_path.exists():
        raise ToolError(
            "Output file already exists. PDF tools never overwrite; "
            "choose a new output path."
        )

    document = extract_pdf_tables_document(
        input_path,
        pages,
        min_rows=min_rows,
        min_columns=min_columns,
    )
    content = render_pdf_tables_output(document, normalized)
    size, digest = _write_bytes_atomically(
        content,
        output_path,
        output_format="json" if normalized == "json" else "text",
    )
    return PdfTableExtractionResult(
        output_path=output_path,
        document=document,
        output_format=normalized,
        size_bytes=size,
        sha256=digest,
    )
