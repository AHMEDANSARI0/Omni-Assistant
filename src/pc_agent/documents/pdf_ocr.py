"""Bounded scanned-PDF OCR through PDFium rendering and provider vision.

Phase 22 deliberately separates the deterministic parts from the model call:
this module selects pages, reuses embedded text when asked, renders only the
remaining pages at a bounded DPI/pixel size, and writes a new text/JSON file.
The actual transcription is supplied by a caller-provided callback so the
tool layer can enforce the existing default-off vision gate, exact approval,
provider budgets and routing. OCR text is model output, not embedded text;
callers must report that distinction and must not claim its wording was
deterministically verified.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from ..errors import ToolError
from .pdf import (
    _normalize_pdf_text,
    _selected_pages,
    _write_bytes_atomically,
    inspect_pdf,
    read_pdf_text,
)

MAX_PDF_OCR_PAGES = 10
MAX_PDF_OCR_CHARACTERS = 200_000
MIN_PDF_OCR_DPI = 72
MAX_PDF_OCR_DPI = 300
DEFAULT_PDF_OCR_DPI = 200
MAX_PDF_OCR_LANGUAGE_CHARACTERS = 50
MAX_PDF_OCR_RENDER_DIMENSION = 10_000
MAX_PDF_OCR_RENDER_PIXELS = 40_000_000
_MAX_PDF_OCR_SENT_DIMENSION = 2048
_NO_READABLE_TEXT = "[NO READABLE TEXT]"


@dataclass(frozen=True, slots=True)
class PdfOcrRenderedPage:
    page: int
    width: int
    height: int
    sent_width: int
    sent_height: int
    dpi: int
    data: bytes
    mime_type: str
    sha256: str


@dataclass(frozen=True, slots=True)
class PdfOcrProviderOutput:
    text: str
    provider: str
    model: str
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class PdfOcrPage:
    page: int
    text: str
    source: str  # embedded | ocr
    provider: str | None = None
    model: str | None = None
    rendered_width: int | None = None
    rendered_height: int | None = None
    sent_width: int | None = None
    sent_height: int | None = None
    sent_size_bytes: int = 0
    sent_sha256: str | None = None

    @property
    def characters(self) -> int:
        return len(self.text)

    def metadata(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "source": self.source,
            "characters": self.characters,
            "provider": self.provider,
            "model": self.model,
            "rendered_width": self.rendered_width,
            "rendered_height": self.rendered_height,
            "sent_width": self.sent_width,
            "sent_height": self.sent_height,
            "sent_size_bytes": self.sent_size_bytes,
            "sent_sha256": self.sent_sha256,
        }


@dataclass(frozen=True, slots=True)
class PdfOcrResult:
    output_path: Path
    source_pages: int
    selected_pages: tuple[int, ...]
    text_pages: int
    embedded_pages: tuple[int, ...]
    ocr_pages: tuple[int, ...]
    characters: int
    output_format: str
    size_bytes: int
    sha256: str
    dpi: int
    language: str | None
    providers: tuple[str, ...]
    models: tuple[str, ...]
    provider_attempts: int
    pages: tuple[PdfOcrPage, ...]


def _load_pdfium() -> Any:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # Dependency is declared in pyproject.toml.
        raise ToolError(
            'PDF OCR rendering needs the pypdfium2 package. Run: '
            'python -m pip install -e ".[dev]"'
        ) from exc
    return pdfium


def validate_ocr_language(value: str | None) -> str | None:
    if value is None:
        return None
    language = " ".join(str(value).strip().split())
    if not language:
        return None
    if len(language) > MAX_PDF_OCR_LANGUAGE_CHARACTERS or any(
        ord(character) < 32 for character in language
    ):
        raise ToolError(
            "PDF OCR language must be a short language name of at most "
            f"{MAX_PDF_OCR_LANGUAGE_CHARACTERS} characters."
        )
    return language


def _validate_dpi(dpi: int) -> int:
    if isinstance(dpi, bool) or not isinstance(dpi, int) or not (
        MIN_PDF_OCR_DPI <= dpi <= MAX_PDF_OCR_DPI
    ):
        raise ToolError(
            f"PDF OCR DPI must be between {MIN_PDF_OCR_DPI} and {MAX_PDF_OCR_DPI}."
        )
    return dpi


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        try:
            flattened = Image.alpha_composite(background, rgba).convert("RGB")
        finally:
            rgba.close()
            background.close()
        return flattened
    return image.convert("RGB")


def _encode_rendered_page(
    image: Image.Image,
    *,
    page_number: int,
    dpi: int,
    max_bytes: int,
) -> PdfOcrRenderedPage:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not (
        1024 <= max_bytes <= 20_000_000
    ):
        raise ToolError("PDF OCR image byte limit must be between 1024 and 20000000.")
    rendered_width, rendered_height = image.size
    rgb = _flatten_to_rgb(image)
    try:
        for max_dimension, quality in (
            (_MAX_PDF_OCR_SENT_DIMENSION, 88),
            (1600, 82),
            (1280, 78),
            (800, 72),
        ):
            candidate = rgb.copy()
            try:
                candidate.thumbnail(
                    (max_dimension, max_dimension), Image.Resampling.LANCZOS
                )
                buffer = BytesIO()
                candidate.save(buffer, format="JPEG", quality=quality, optimize=True)
                data = buffer.getvalue()
                sent_width, sent_height = candidate.size
            finally:
                candidate.close()
            if len(data) <= max_bytes:
                return PdfOcrRenderedPage(
                    page=page_number,
                    width=rendered_width,
                    height=rendered_height,
                    sent_width=sent_width,
                    sent_height=sent_height,
                    dpi=dpi,
                    data=data,
                    mime_type="image/jpeg",
                    sha256=hashlib.sha256(data).hexdigest(),
                )
    finally:
        rgb.close()
    raise ToolError(
        f"Rendered PDF page {page_number} could not be reduced below the "
        "configured vision image byte limit."
    )


def _render_page(
    document: Any,
    page_number: int,
    *,
    dpi: int,
    max_bytes: int,
) -> PdfOcrRenderedPage:
    page = document[page_number - 1]
    bitmap: Any | None = None
    image: Image.Image | None = None
    try:
        width_points, height_points = page.get_size()
        scale = dpi / 72.0
        predicted_width = max(1, math.ceil(float(width_points) * scale))
        predicted_height = max(1, math.ceil(float(height_points) * scale))
        if int(page.get_rotation() or 0) % 180 == 90:
            predicted_width, predicted_height = predicted_height, predicted_width
        if (
            predicted_width > MAX_PDF_OCR_RENDER_DIMENSION
            or predicted_height > MAX_PDF_OCR_RENDER_DIMENSION
            or predicted_width * predicted_height > MAX_PDF_OCR_RENDER_PIXELS
        ):
            raise ToolError(
                f"PDF page {page_number} is too large to render safely at "
                f"{dpi} DPI; select a lower DPI."
            )
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        if (
            image.width > MAX_PDF_OCR_RENDER_DIMENSION
            or image.height > MAX_PDF_OCR_RENDER_DIMENSION
            or image.width * image.height > MAX_PDF_OCR_RENDER_PIXELS
        ):
            raise ToolError(
                f"Rendered PDF page {page_number} exceeds the OCR pixel limit."
            )
        return _encode_rendered_page(
            image,
            page_number=page_number,
            dpi=dpi,
            max_bytes=max_bytes,
        )
    except ToolError:
        raise
    except Exception as exc:  # PDFium/Pillow errors are reported without internals.
        raise ToolError(
            f"Could not render PDF page {page_number} for OCR: {type(exc).__name__}"
        ) from exc
    finally:
        if image is not None:
            image.close()
        if bitmap is not None:
            bitmap.close()
        page.close()


def _normalize_ocr_text(value: str) -> str:
    text = _normalize_pdf_text(value)
    lines = text.split("\n")
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        text = "\n".join(lines[1:-1]).strip("\n")
    if text.strip().upper() == _NO_READABLE_TEXT:
        return ""
    return text


def _render_ocr_output(
    *,
    source_pages: int,
    pages: tuple[PdfOcrPage, ...],
    output_format: str,
    dpi: int,
    language: str | None,
) -> bytes:
    if output_format == "text":
        sections: list[str] = []
        for page in pages:
            label = "embedded text" if page.source == "embedded" else "OCR"
            sections.append(
                f"--- Page {page.page} [{label}] ---\n{page.text}".rstrip()
            )
        return ("\n\n".join(sections) + "\n").encode("utf-8")
    if output_format == "json":
        payload = {
            "method": "pdf-ocr-v1",
            "source_pages": source_pages,
            "dpi": dpi,
            "language": language,
            "pages": [
                {**page.metadata(), "text": page.text}
                for page in pages
            ],
            "text_pages": sum(1 for page in pages if page.text.strip()),
            "characters": sum(page.characters for page in pages),
            "embedded_pages": [
                page.page for page in pages if page.source == "embedded"
            ],
            "ocr_pages": [page.page for page in pages if page.source == "ocr"],
        }
        return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
    raise ToolError("PDF OCR output format must be 'text' or 'json'.")


def ocr_pdf(
    input_path: Path,
    output_path: Path,
    *,
    pages: Sequence[int] | None = None,
    output_format: str = "text",
    dpi: int = DEFAULT_PDF_OCR_DPI,
    language: str | None = None,
    skip_text_pages: bool = True,
    max_image_bytes: int = 10_000_000,
    ocr_page: Callable[[PdfOcrRenderedPage], PdfOcrProviderOutput],
) -> PdfOcrResult:
    """OCR selected pages into a new verified text/Markdown/JSON artifact.

    Embedded-text pages are reused when ``skip_text_pages`` is true; only the
    other selected pages are rendered and passed to ``ocr_page``. The output
    bytes/hash are verified after writing, but provider transcription itself
    is model output and is not deterministically re-graded.
    """

    normalized_format = output_format.strip().lower()
    suffix = output_path.suffix.lower()
    if normalized_format == "text" and suffix not in {".txt", ".md"}:
        raise ToolError("Text PDF OCR output must end with .txt or .md.")
    if normalized_format == "json" and suffix != ".json":
        raise ToolError("JSON PDF OCR output must end with .json.")
    if normalized_format not in {"text", "json"}:
        raise ToolError("PDF OCR output format must be 'text' or 'json'.")
    if input_path.expanduser().resolve() == output_path.expanduser().resolve():
        raise ToolError("The output path must not be the input PDF.")
    if output_path.exists():
        raise ToolError(
            "Output file already exists. PDF tools never overwrite; "
            "choose a new output path."
        )
    clean_dpi = _validate_dpi(dpi)
    clean_language = validate_ocr_language(language)

    info = inspect_pdf(input_path)
    if info.encrypted or info.pages is None:
        raise ToolError("Encrypted PDFs are not supported without a password.")
    selected = _selected_pages(info.pages, pages, limit=MAX_PDF_OCR_PAGES)
    embedded_document = read_pdf_text(input_path, selected)
    embedded_text = {page.page: page.text for page in embedded_document.pages}
    needs_ocr = tuple(
        page
        for page in selected
        if not (skip_text_pages and embedded_text.get(page, "").strip())
    )

    document: Any | None = None
    if needs_ocr:
        pdfium = _load_pdfium()
        try:
            document = pdfium.PdfDocument(str(input_path))
            if len(document) != info.pages:
                raise ToolError(
                    "PDF page count changed between inspection and OCR rendering."
                )
        except ToolError:
            if document is not None:
                document.close()
            raise
        except Exception as exc:
            if document is not None:
                document.close()
            raise ToolError(
                f"Could not open PDF for OCR rendering: {type(exc).__name__}"
            ) from exc

    result_pages: list[PdfOcrPage] = []
    total_characters = 0
    provider_attempts = 0
    try:
        for page_number in selected:
            embedded = embedded_text.get(page_number, "")
            if skip_text_pages and embedded.strip():
                page_result = PdfOcrPage(
                    page=page_number,
                    text=embedded,
                    source="embedded",
                )
            else:
                assert document is not None
                rendered = _render_page(
                    document,
                    page_number,
                    dpi=clean_dpi,
                    max_bytes=max_image_bytes,
                )
                provider_output = ocr_page(rendered)
                if not isinstance(provider_output, PdfOcrProviderOutput):
                    raise ToolError("The OCR provider returned an invalid result.")
                provider = provider_output.provider.strip()
                model = provider_output.model.strip()
                if not provider or not model:
                    raise ToolError(
                        "The OCR provider result did not identify its provider/model."
                    )
                if (
                    isinstance(provider_output.attempts, bool)
                    or not isinstance(provider_output.attempts, int)
                    or provider_output.attempts < 0
                ):
                    raise ToolError("The OCR provider returned an invalid attempt count.")
                provider_attempts += provider_output.attempts
                page_result = PdfOcrPage(
                    page=page_number,
                    text=_normalize_ocr_text(provider_output.text),
                    source="ocr",
                    provider=provider,
                    model=model,
                    rendered_width=rendered.width,
                    rendered_height=rendered.height,
                    sent_width=rendered.sent_width,
                    sent_height=rendered.sent_height,
                    sent_size_bytes=len(rendered.data),
                    sent_sha256=rendered.sha256,
                )
            total_characters += page_result.characters
            if total_characters > MAX_PDF_OCR_CHARACTERS:
                raise ToolError(
                    "PDF OCR output exceeds the "
                    f"{MAX_PDF_OCR_CHARACTERS:,}-character safety limit."
                )
            result_pages.append(page_result)
    finally:
        if document is not None:
            document.close()

    pages_tuple = tuple(result_pages)
    content = _render_ocr_output(
        source_pages=info.pages,
        pages=pages_tuple,
        output_format=normalized_format,
        dpi=clean_dpi,
        language=clean_language,
    )
    size, digest = _write_bytes_atomically(
        content, output_path, output_format=normalized_format
    )
    providers: list[str] = []
    models: list[str] = []
    for page in pages_tuple:
        if page.provider and page.provider not in providers:
            providers.append(page.provider)
        if page.model and page.model not in models:
            models.append(page.model)
    return PdfOcrResult(
        output_path=output_path,
        source_pages=info.pages,
        selected_pages=selected,
        text_pages=sum(1 for page in pages_tuple if page.text.strip()),
        embedded_pages=tuple(
            page.page for page in pages_tuple if page.source == "embedded"
        ),
        ocr_pages=tuple(page.page for page in pages_tuple if page.source == "ocr"),
        characters=total_characters,
        output_format=normalized_format,
        size_bytes=size,
        sha256=digest,
        dpi=clean_dpi,
        language=clean_language,
        providers=tuple(providers),
        models=tuple(models),
        provider_attempts=provider_attempts,
        pages=pages_tuple,
    )
