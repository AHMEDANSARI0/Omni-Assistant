from __future__ import annotations

from pathlib import Path

import pytest

from pc_agent.documents.pdf import sha256_file, write_blank_pdf
from pc_agent.tools import ToolContext, build_default_registry


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture()
def context(workspace: Path) -> ToolContext:
    return ToolContext.for_workspace(workspace)


@pytest.fixture()
def registry():
    return build_default_registry()


def _make_pdf(path: Path, pages: int, *, password: str | None = None) -> Path:
    if password is None:
        write_blank_pdf(path, pages)
        return path
    from pypdf import PdfWriter

    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    writer.encrypt(user_password=password)
    with path.open("wb") as handle:
        writer.write(handle)
    writer.close()
    return path


def test_inspect_and_verify_pdf(workspace, context, registry) -> None:
    _make_pdf(workspace / "sample.pdf", 3)

    inspected = registry.invoke("pdf.inspect", {"path": "sample.pdf"}, context)
    verified = registry.invoke(
        "pdf.verify", {"path": "sample.pdf", "expected_pages": 3}, context
    )
    mismatch = registry.invoke(
        "pdf.verify", {"path": "sample.pdf", "expected_pages": 2}, context
    )

    assert inspected.success and inspected.output is not None
    assert inspected.output["pages"] == 3
    assert verified.output is not None and verified.output["valid"] is True
    assert mismatch.output is not None and mismatch.output["valid"] is False


def test_explicit_merge_verifies_pages_and_preserves_sources(workspace, context, registry) -> None:
    first = _make_pdf(workspace / "first.pdf", 2)
    second = _make_pdf(workspace / "second.pdf", 3)
    before = {path: sha256_file(path) for path in (first, second)}

    result = registry.invoke(
        "pdf.merge",
        {"input_paths": ["first.pdf", "second.pdf"], "output_path": "merged.pdf"},
        context,
    )

    assert result.success, result.error
    assert result.output is not None
    assert result.output["page_count"] == 5
    assert result.output["verified"] is True
    assert {path: sha256_file(path) for path in before} == before

    verified = registry.invoke(
        "pdf.verify", {"path": "merged.pdf", "expected_pages": 5}, context
    )
    assert verified.output is not None and verified.output["valid"] is True


def test_merge_never_overwrites_existing_output(workspace, context, registry) -> None:
    _make_pdf(workspace / "a.pdf", 1)
    _make_pdf(workspace / "b.pdf", 1)
    _make_pdf(workspace / "merged.pdf", 1)
    before = sha256_file(workspace / "merged.pdf")

    result = registry.invoke(
        "pdf.merge",
        {"input_paths": ["a.pdf", "b.pdf"], "output_path": "merged.pdf"},
        context,
    )

    assert not result.success
    assert sha256_file(workspace / "merged.pdf") == before


def test_explicit_merge_rejects_corrupt_input_without_output(workspace, context, registry) -> None:
    _make_pdf(workspace / "good.pdf", 1)
    (workspace / "bad.pdf").write_text("not a pdf", encoding="utf-8")

    result = registry.invoke(
        "pdf.merge",
        {"input_paths": ["good.pdf", "bad.pdf"], "output_path": "merged.pdf"},
        context,
    )

    assert not result.success
    assert not (workspace / "merged.pdf").exists()


def test_folder_merge_skips_corrupt_and_encrypted_but_reports_them(
    workspace, context, registry
) -> None:
    _make_pdf(workspace / "Folder A" / "a1.pdf", 2)
    _make_pdf(workspace / "Folder A" / "a2.pdf", 1)
    _make_pdf(workspace / "Folder B" / "b1.pdf", 3)
    _make_pdf(workspace / "Folder B" / "locked.pdf", 4, password="secret")
    (workspace / "Folder B" / "broken.pdf").write_text("broken", encoding="utf-8")

    result = registry.invoke(
        "pdf.merge_folders",
        {
            "input_folders": ["Folder A", "Folder B"],
            "output_path": "combined.pdf",
        },
        context,
    )

    assert result.success, result.error
    assert result.output is not None
    assert result.output["input_files"] == [
        "Folder A/a1.pdf",
        "Folder A/a2.pdf",
        "Folder B/b1.pdf",
    ]
    assert result.output["page_count"] == 6
    skipped_paths = {item["path"] for item in result.output["skipped_files"]}
    assert skipped_paths == {"Folder B/broken.pdf", "Folder B/locked.pdf"}


def test_folder_merge_fails_honestly_with_fewer_than_two_valid_pdfs(
    workspace, context, registry
) -> None:
    _make_pdf(workspace / "Only" / "one.pdf", 1)
    (workspace / "Only" / "bad.pdf").write_text("bad", encoding="utf-8")

    result = registry.invoke(
        "pdf.merge_folders",
        {"input_folders": ["Only"], "output_path": "out.pdf"},
        context,
    )

    assert not result.success
    assert result.error is not None and "bad.pdf" in result.error
    assert not (workspace / "out.pdf").exists()


def test_pdf_paths_cannot_escape_workspace(workspace, context, registry) -> None:
    result = registry.invoke("pdf.inspect", {"path": "../outside.pdf"}, context)

    assert not result.success
