"""PDF inspection, merging, splitting/extracting and deterministic verification.

Every produced PDF is only reported as successful after it has been
re-opened and its page count verified. Source files are opened read-only
and are never modified.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ToolError

MAX_SPLIT_PAGES = 200


def _load_pypdf() -> tuple[Any, Any]:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:  # Dependency is declared in pyproject.toml.
        raise ToolError(
            'PDF support needs the pypdf package. Run: python -m pip install -e ".[dev]"'
        ) from exc
    pypdf_logger = logging.getLogger("pypdf")
    if pypdf_logger.level == logging.NOTSET:
        # Library/test callers get the same clean behaviour as the CLI unless
        # the host application has explicitly chosen a pypdf level itself.
        pypdf_logger.setLevel(logging.ERROR)
    return PdfReader, PdfWriter


@dataclass(frozen=True, slots=True)
class PdfInfo:
    path: Path
    pages: int | None
    encrypted: bool
    size_bytes: int


@dataclass(frozen=True, slots=True)
class PdfVerification:
    path: Path
    valid: bool
    pages: int | None
    expected_pages: int | None
    sha256: str | None
    size_bytes: int | None
    error: str | None


@dataclass(frozen=True, slots=True)
class PdfMergeResult:
    output_path: Path
    inputs: tuple[PdfInfo, ...]
    page_count: int
    size_bytes: int
    sha256: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_pdf(path: Path) -> PdfInfo:
    if not path.is_file():
        raise ToolError(f"PDF file not found: {path.name}")
    if path.suffix.lower() != ".pdf":
        raise ToolError(f"Not a PDF file: {path.name}")
    PdfReader, _ = _load_pypdf()
    try:
        reader = PdfReader(str(path))
        encrypted = bool(reader.is_encrypted)
        if encrypted:
            return PdfInfo(path=path, pages=None, encrypted=True, size_bytes=path.stat().st_size)
        pages = len(reader.pages)
        if pages < 1:
            raise ToolError(f"PDF has no pages: {path.name}")
        for page in reader.pages:
            box = page.mediabox
            if float(box.width) <= 0 or float(box.height) <= 0:
                raise ToolError(f"PDF contains an invalid page size: {path.name}")
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"Could not read PDF '{path.name}': {type(exc).__name__}") from exc
    return PdfInfo(path=path, pages=pages, encrypted=False, size_bytes=path.stat().st_size)


def verify_pdf(path: Path, expected_pages: int | None = None) -> PdfVerification:
    if expected_pages is not None and expected_pages < 1:
        return PdfVerification(path, False, None, expected_pages, None, None,
                               "expected_pages must be at least 1.")
    try:
        info = inspect_pdf(path)
    except ToolError as exc:
        size = path.stat().st_size if path.is_file() else None
        checksum = sha256_file(path) if path.is_file() else None
        return PdfVerification(path, False, None, expected_pages, checksum, size, str(exc))

    checksum = sha256_file(path)
    if info.encrypted:
        return PdfVerification(path, False, None, expected_pages, checksum,
                               info.size_bytes, "Encrypted PDFs are not supported.")
    if expected_pages is not None and info.pages != expected_pages:
        return PdfVerification(
            path,
            False,
            info.pages,
            expected_pages,
            checksum,
            info.size_bytes,
            f"Page count mismatch: expected {expected_pages}, found {info.pages}.",
        )
    return PdfVerification(path, True, info.pages, expected_pages, checksum,
                           info.size_bytes, None)


def merge_pdf_files(input_paths: list[Path], output_path: Path) -> PdfMergeResult:
    if len(input_paths) < 2:
        raise ToolError("At least two PDF files are required for a merge.")
    if output_path.suffix.lower() != ".pdf":
        raise ToolError("The merge output must be a .pdf file.")
    if output_path.exists():
        raise ToolError(
            "Output file already exists. PDF tools never overwrite; choose a new output path."
        )

    resolved_inputs = [candidate.expanduser().resolve() for candidate in input_paths]
    if len(set(resolved_inputs)) != len(resolved_inputs):
        raise ToolError("Duplicate PDF inputs are not allowed in one merge.")
    resolved_output = output_path.expanduser().resolve()
    if resolved_output in resolved_inputs:
        raise ToolError("The output path must not be one of the input PDFs.")

    infos: list[PdfInfo] = []
    for candidate in resolved_inputs:
        info = inspect_pdf(candidate)
        if info.encrypted:
            raise ToolError(f"Encrypted PDF cannot be merged without a password: {candidate.name}")
        infos.append(info)
    expected_pages = sum(info.pages or 0 for info in infos)

    PdfReader, PdfWriter = _load_pypdf()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.stem}.tmp-{uuid.uuid4().hex}.pdf")

    try:
        writer = PdfWriter()
        try:
            for candidate in resolved_inputs:
                writer.append(PdfReader(str(candidate)))
            with temporary.open("wb") as handle:
                writer.write(handle)
        except Exception as exc:
            raise ToolError(f"PDF merge failed while writing the output: {type(exc).__name__}") from exc
        finally:
            writer.close()

        verification = verify_pdf(temporary, expected_pages=expected_pages)
        if not verification.valid:
            raise ToolError(f"Merged PDF failed verification: {verification.error}")
        if output_path.exists():
            raise ToolError(
                "Output file already exists. PDF tools never overwrite; choose a new output path."
            )
        os.replace(temporary, output_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    final = verify_pdf(output_path, expected_pages=expected_pages)
    if not final.valid or final.pages is None or final.sha256 is None or final.size_bytes is None:
        raise ToolError(f"Merged PDF could not be re-verified: {final.error}")
    return PdfMergeResult(
        output_path=output_path,
        inputs=tuple(infos),
        page_count=final.pages,
        size_bytes=final.size_bytes,
        sha256=final.sha256,
    )


@dataclass(frozen=True, slots=True)
class PdfExtractResult:
    output_path: Path
    source_pages: int
    extracted_pages: tuple[int, ...]
    page_count: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class PdfSplitPart:
    page: int
    path: Path
    sha256: str
    size_bytes: int


def parse_page_selection(selection: str, total_pages: int) -> list[int]:
    """Parse a 1-based page selection like ``"1-3,5"`` into page numbers."""

    if total_pages < 1:
        raise ToolError("The PDF has no pages to select from.")
    if not selection or not selection.strip():
        raise ToolError('A page selection is required, for example "1-3,5".')
    pages: list[int] = []
    for raw_token in selection.split(","):
        token = raw_token.strip()
        if not token:
            raise ToolError(f"Invalid page selection: {selection!r}.")
        if "-" in token:
            start_text, _, end_text = token.partition("-")
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise ToolError(f"Invalid page range: {token!r}.") from exc
            if start > end:
                raise ToolError(f"Page range {token!r} is reversed.")
            pages.extend(range(start, end + 1))
        else:
            try:
                pages.append(int(token))
            except ValueError as exc:
                raise ToolError(f"Invalid page number: {token!r}.") from exc
    if not pages:
        raise ToolError('A page selection is required, for example "1-3,5".')
    if len(set(pages)) != len(pages):
        raise ToolError("The page selection contains duplicate pages.")
    for page in pages:
        if page < 1 or page > total_pages:
            raise ToolError(
                f"Page {page} is outside this PDF (1 to {total_pages} pages)."
            )
    return pages


def _write_pages_atomically(
    page_objects: list[Any], output_path: Path, *, expected_pages: int
) -> PdfVerification:
    _, PdfWriter = _load_pypdf()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.stem}.tmp-{uuid.uuid4().hex}.pdf")
    try:
        writer = PdfWriter()
        try:
            for page in page_objects:
                writer.add_page(page)
            with temporary.open("wb") as handle:
                writer.write(handle)
        except Exception as exc:
            raise ToolError(
                f"PDF write failed while producing the output: {type(exc).__name__}"
            ) from exc
        finally:
            writer.close()
        verification = verify_pdf(temporary, expected_pages=expected_pages)
        if not verification.valid:
            raise ToolError(f"Produced PDF failed verification: {verification.error}")
        if output_path.exists():
            raise ToolError(
                "Output file already exists. PDF tools never overwrite; "
                "choose a new output path."
            )
        os.replace(temporary, output_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    final = verify_pdf(output_path, expected_pages=expected_pages)
    if not final.valid or final.sha256 is None or final.size_bytes is None:
        raise ToolError(f"Produced PDF could not be re-verified: {final.error}")
    return final


def extract_pdf_pages(
    input_path: Path, pages: Sequence[int], output_path: Path
) -> PdfExtractResult:
    """Extract the given 1-based pages (in the given order) into a new PDF."""

    if output_path.suffix.lower() != ".pdf":
        raise ToolError("The extract output must be a .pdf file.")
    if output_path.exists():
        raise ToolError(
            "Output file already exists. PDF tools never overwrite; "
            "choose a new output path."
        )
    if input_path.expanduser().resolve() == output_path.expanduser().resolve():
        raise ToolError("The output path must not be the input PDF.")
    info = inspect_pdf(input_path)
    if info.encrypted or info.pages is None:
        raise ToolError("Encrypted PDFs are not supported without a password.")
    selected = list(pages)
    if not selected:
        raise ToolError("At least one page must be selected.")
    if len(set(selected)) != len(selected):
        raise ToolError("The page selection contains duplicate pages.")
    for page in selected:
        if page < 1 or page > info.pages:
            raise ToolError(f"Page {page} is outside this PDF (1 to {info.pages}).")

    PdfReader, _ = _load_pypdf()
    try:
        reader = PdfReader(str(input_path))
        page_objects = [reader.pages[page - 1] for page in selected]
    except Exception as exc:
        raise ToolError(f"Could not read PDF pages: {type(exc).__name__}") from exc
    final = _write_pages_atomically(
        page_objects, output_path, expected_pages=len(selected)
    )
    assert final.sha256 is not None and final.size_bytes is not None  # verified above
    return PdfExtractResult(
        output_path=output_path,
        source_pages=info.pages,
        extracted_pages=tuple(selected),
        page_count=len(selected),
        size_bytes=final.size_bytes,
        sha256=final.sha256,
    )


def split_pdf(
    input_path: Path, output_dir: Path, *, prefix: str | None = None
) -> list[PdfSplitPart]:
    """Split a PDF into one single-page PDF per page inside ``output_dir``."""

    info = inspect_pdf(input_path)
    if info.encrypted or info.pages is None:
        raise ToolError("Encrypted PDFs are not supported without a password.")
    if info.pages > MAX_SPLIT_PAGES:
        raise ToolError(
            f"Splitting is limited to {MAX_SPLIT_PAGES} pages; "
            f"this PDF has {info.pages}. Use extract-pages for a range instead."
        )
    clean_prefix = (prefix or input_path.stem).strip()
    if not clean_prefix or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.\-]*", clean_prefix):
        raise ToolError("The split prefix must be a simple file-name prefix.")
    targets = [
        output_dir / f"{clean_prefix}-page-{page:03d}.pdf"
        for page in range(1, info.pages + 1)
    ]
    for target in targets:
        if target.exists():
            raise ToolError(
                "Output file already exists. PDF tools never overwrite: "
                f"{target.name}. Choose a new output folder or prefix."
            )

    PdfReader, _ = _load_pypdf()
    try:
        reader = PdfReader(str(input_path))
        all_pages = list(reader.pages)
    except Exception as exc:
        raise ToolError(f"Could not read PDF pages: {type(exc).__name__}") from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[PdfSplitPart] = []
    try:
        for page_number, (page_object, target) in enumerate(
            zip(all_pages, targets, strict=True), start=1
        ):
            final = _write_pages_atomically(
                [page_object], target, expected_pages=1
            )
            assert final.sha256 is not None and final.size_bytes is not None
            written.append(
                PdfSplitPart(
                    page=page_number,
                    path=target,
                    sha256=final.sha256,
                    size_bytes=final.size_bytes,
                )
            )
    except Exception:
        # Remove only the partial outputs this call created.
        for part in written:
            part.path.unlink(missing_ok=True)
        raise
    return written


def write_blank_pdf(path: Path, pages: int) -> None:
    """Create a small valid PDF. Used for demo data and tests."""
    if pages < 1:
        raise ToolError("A generated PDF needs at least one page.")
    _, PdfWriter = _load_pypdf()
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    try:
        for _ in range(pages):
            writer.add_blank_page(width=612, height=792)
        with path.open("wb") as handle:
            writer.write(handle)
    finally:
        writer.close()


@dataclass(frozen=True, slots=True)
class DemoPdfData:
    created: tuple[str, ...]
    expected_pages: int
    corrupt_file: str


def create_demo_pdfs(workspace_dir: Path) -> DemoPdfData:
    """Create deterministic demo inputs inside the workspace (never overwrite)."""
    plan = [
        (workspace_dir / "Folder A" / "a1.pdf", 2),
        (workspace_dir / "Folder A" / "a2.pdf", 1),
        (workspace_dir / "Folder B" / "b1.pdf", 3),
    ]
    created: list[str] = []
    for target, pages in plan:
        if not target.exists():
            write_blank_pdf(target, pages)
        created.append(target.relative_to(workspace_dir).as_posix())

    corrupt = workspace_dir / "Folder B" / "broken.pdf"
    if not corrupt.exists():
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_text("This is deliberately not a PDF file.\n", encoding="utf-8")
    created.append(corrupt.relative_to(workspace_dir).as_posix())

    return DemoPdfData(
        created=tuple(created),
        expected_pages=6,
        corrupt_file=corrupt.relative_to(workspace_dir).as_posix(),
    )
