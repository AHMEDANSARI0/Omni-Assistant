"""Deterministic document-processing services (no AI involved)."""

from .office import (
    CsvData,
    CsvInspection,
    DemoOfficeData,
    ExtractedText,
    OfficeInspection,
    build_docx,
    build_xlsx,
    create_demo_office_files,
    extract_office_text,
    inspect_csv,
    inspect_office,
    read_csv,
)
from .pdf import (
    PdfInfo,
    PdfMergeResult,
    PdfVerification,
    create_demo_pdfs,
    inspect_pdf,
    merge_pdf_files,
    verify_pdf,
    write_blank_pdf,
)

__all__ = [
    "CsvData",
    "CsvInspection",
    "DemoOfficeData",
    "ExtractedText",
    "OfficeInspection",
    "PdfInfo",
    "PdfMergeResult",
    "PdfVerification",
    "build_docx",
    "build_xlsx",
    "create_demo_office_files",
    "create_demo_pdfs",
    "extract_office_text",
    "inspect_csv",
    "inspect_office",
    "inspect_pdf",
    "merge_pdf_files",
    "read_csv",
    "verify_pdf",
    "write_blank_pdf",
]
