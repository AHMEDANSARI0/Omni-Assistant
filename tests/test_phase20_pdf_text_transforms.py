"""Phase 20: PDF embedded-text extraction/search and page transforms."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from pc_agent import __version__
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.documents.pdf import (
    inspect_pdf_page_snapshots,
    read_pdf_text,
    verify_pdf,
    write_blank_pdf,
)
from pc_agent.domain.task import Task, TaskStatus
from pc_agent.errors import PlanningError, ToolError
from pc_agent.gui.controller import GuiController
from pc_agent.orchestrator import TaskOrchestrator
from pc_agent.planning import (
    DeterministicPlanner,
    PlannerOptions,
    _validate_ai_plan,
)
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION, Database
from pc_agent.storage.repositories import ExecutionRepository, TaskRepository
from pc_agent.tools import build_default_registry
from pc_agent.tools.registry import ToolContext


def _write_text_pdf(path: Path, texts: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    try:
        for text in texts:
            page = writer.add_blank_page(width=612, height=792)
            escaped = (
                text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            )
            font = DictionaryObject()
            font.update(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            fonts = DictionaryObject()
            fonts[NameObject("/F1")] = writer._add_object(font)
            resources = DictionaryObject()
            resources[NameObject("/Font")] = fonts
            page[NameObject("/Resources")] = resources
            stream = DecodedStreamObject()
            stream.set_data(
                f"BT /F1 16 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
            )
            page[NameObject("/Contents")] = writer._add_object(stream)
        with path.open("wb") as handle:
            writer.write(handle)
    finally:
        writer.close()


def _invoke(workspace: Path, tool_id: str, arguments: dict[str, Any]):
    return build_default_registry().invoke(
        tool_id, arguments, ToolContext.for_workspace(workspace)
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _orchestrator(tmp_path: Path):
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    tasks = TaskRepository(database)
    execution = ExecutionRepository(database)
    orchestrator = TaskOrchestrator(
        settings=settings,
        task_repository=tasks,
        execution_repository=execution,
        tool_registry=build_default_registry(),
        deterministic_planner=DeterministicPlanner(settings.workspace_dir),
    )
    return settings, tasks, execution, orchestrator


def test_pdf_extract_text_text_json_subset_and_no_overwrite(tmp_path: Path) -> None:
    _write_text_pdf(
        tmp_path / "report.pdf",
        ["Alpha page one", "Beta page two", "Gamma page three"],
    )

    text_result = _invoke(
        tmp_path,
        "pdf.extract_text",
        {"path": "report.pdf", "output_path": "report.txt", "pages": "2-3"},
    )
    assert text_result.success, text_result.error
    assert text_result.output is not None
    assert text_result.output["extracted_pages"] == [2, 3]
    assert text_result.output["text_pages"] == 2
    assert text_result.output["has_embedded_text"] is True
    assert text_result.output["sha256"] == _sha256(tmp_path / "report.txt")
    assert (tmp_path / "report.txt").read_text(encoding="utf-8") == (
        "--- Page 2 ---\nBeta page two\n\n"
        "--- Page 3 ---\nGamma page three\n"
    )

    json_result = _invoke(
        tmp_path,
        "pdf.extract_text",
        {"path": "report.pdf", "output_path": "report.json"},
    )
    assert json_result.success, json_result.error
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert [page["text"] for page in payload["pages"]] == [
        "Alpha page one",
        "Beta page two",
        "Gamma page three",
    ]

    again = _invoke(
        tmp_path,
        "pdf.extract_text",
        {"path": "report.pdf", "output_path": "report.txt"},
    )
    assert not again.success
    assert "already exists" in (again.error or "")

    mismatched = _invoke(
        tmp_path,
        "pdf.extract_text",
        {
            "path": "report.pdf",
            "output_path": "wrong.txt",
            "format": "json",
        },
    )
    assert not mismatched.success
    assert ".json" in (mismatched.error or "")


def test_pdf_search_text_counts_case_bounds_and_blank_pdf_honesty(
    tmp_path: Path,
) -> None:
    _write_text_pdf(
        tmp_path / "invoice.pdf",
        ["Invoice Total 42 Invoice", "Receipt Total 9", "No match here"],
    )

    found = _invoke(
        tmp_path,
        "pdf.search_text",
        {"path": "invoice.pdf", "query": "invoice", "max_results": 1},
    )
    assert found.success, found.error
    assert found.output is not None
    assert found.output["total_occurrences"] == 2
    assert found.output["matching_pages"] == [1]
    assert found.output["truncated"] is True
    assert found.output["matches"][0]["page"] == 1

    sensitive = _invoke(
        tmp_path,
        "pdf.search_text",
        {
            "path": "invoice.pdf",
            "query": "invoice",
            "case_sensitive": True,
        },
    )
    assert sensitive.success, sensitive.error
    assert sensitive.output is not None
    assert sensitive.output["total_occurrences"] == 0

    subset = _invoke(
        tmp_path,
        "pdf.search_text",
        {"path": "invoice.pdf", "query": "Total", "pages": "2"},
    )
    assert subset.success, subset.error
    assert subset.output is not None
    assert subset.output["searched_pages"] == [2]
    assert subset.output["matching_pages"] == [2]

    invalid = _invoke(
        tmp_path,
        "pdf.search_text",
        {"path": "invoice.pdf", "query": "Total", "max_results": 201},
    )
    assert not invalid.success

    write_blank_pdf(tmp_path / "blank.pdf", 2)
    blank = _invoke(
        tmp_path,
        "pdf.search_text",
        {"path": "blank.pdf", "query": "anything"},
    )
    assert blank.success, blank.error
    assert blank.output is not None
    assert blank.output["has_embedded_text"] is False
    assert blank.output["total_occurrences"] == 0

    blank_extract = _invoke(
        tmp_path,
        "pdf.extract_text",
        {"path": "blank.pdf", "output_path": "blank.txt"},
    )
    assert blank_extract.success, blank_extract.error
    assert blank_extract.output is not None
    assert blank_extract.output["has_embedded_text"] is False
    assert blank_extract.output["characters"] == 0


def test_pdf_reorder_and_rotate_tools_verify_content_and_rotations(
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.pdf"
    _write_text_pdf(
        source,
        ["Alpha page one", "Beta page two", "Gamma page three"],
    )
    source_hash = _sha256(source)

    reordered = _invoke(
        tmp_path,
        "pdf.reorder_pages",
        {"path": "report.pdf", "pages": "3,1,2", "output_path": "ordered.pdf"},
    )
    assert reordered.success, reordered.error
    assert [page.text for page in read_pdf_text(tmp_path / "ordered.pdf").pages] == [
        "Gamma page three",
        "Alpha page one",
        "Beta page two",
    ]
    assert verify_pdf(tmp_path / "ordered.pdf", expected_pages=3).valid

    reversed_result = _invoke(
        tmp_path,
        "pdf.reorder_pages",
        {"path": "report.pdf", "pages": "reverse", "output_path": "reversed.pdf"},
    )
    assert reversed_result.success, reversed_result.error
    assert reversed_result.output is not None
    assert reversed_result.output["page_order"] == [3, 2, 1]

    incomplete = _invoke(
        tmp_path,
        "pdf.reorder_pages",
        {"path": "report.pdf", "pages": "2,1", "output_path": "incomplete.pdf"},
    )
    assert not incomplete.success
    assert "every source page" in (incomplete.error or "")

    rotated = _invoke(
        tmp_path,
        "pdf.rotate_pages",
        {
            "path": "report.pdf",
            "degrees": 90,
            "pages": "2",
            "output_path": "rotated.pdf",
        },
    )
    assert rotated.success, rotated.error
    assert rotated.output is not None
    assert rotated.output["page_rotations"] == [0, 90, 0]
    assert [
        snapshot.rotation
        for snapshot in inspect_pdf_page_snapshots(tmp_path / "rotated.pdf")
    ] == [0, 90, 0]

    restored = _invoke(
        tmp_path,
        "pdf.rotate_pages",
        {
            "path": "rotated.pdf",
            "degrees": -90,
            "pages": "2",
            "output_path": "restored.pdf",
        },
    )
    assert restored.success, restored.error
    assert restored.output is not None
    assert restored.output["page_rotations"] == [0, 0, 0]

    invalid_degrees = _invoke(
        tmp_path,
        "pdf.rotate_pages",
        {"path": "report.pdf", "degrees": 45, "output_path": "invalid.pdf"},
    )
    assert not invalid_degrees.success
    assert _sha256(source) == source_hash


def test_pdf_text_tools_reject_encrypted_pdf(tmp_path: Path) -> None:
    source = tmp_path / "secret.pdf"
    writer = PdfWriter()
    try:
        writer.add_blank_page(width=612, height=792)
        writer.encrypt("correct horse battery staple")
        with source.open("wb") as handle:
            writer.write(handle)
    finally:
        writer.close()

    with pytest.raises(ToolError, match="Encrypted"):
        read_pdf_text(source)

    result = _invoke(
        tmp_path,
        "pdf.search_text",
        {"path": "secret.pdf", "query": "anything"},
    )
    assert not result.success
    assert "Encrypted" in (result.error or "")


def test_phase20_deterministic_and_ai_planner_boundaries(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_text_pdf(workspace / "report.pdf", ["Alpha", "Beta", "Gamma"])
    planner = DeterministicPlanner(workspace)

    extract = planner.plan(
        "Extract text from report.pdf",
        PlannerOptions(target_path="report.pdf", output_path="report.json"),
    )
    assert extract is not None and extract.plan is not None
    assert extract.plan.tool_id == "pdf.extract_text"
    assert extract.plan.arguments["format"] == "json"

    search = planner.plan('Search report.pdf for "Beta"')
    assert search is not None and search.plan is not None
    assert search.plan.tool_id == "pdf.search_text"
    assert search.plan.arguments["query"] == "Beta"

    reorder = planner.plan(
        "Reorder pages in report.pdf",
        PlannerOptions(target_path="report.pdf", page_selection="3,1,2"),
    )
    assert reorder is not None and reorder.plan is not None
    assert reorder.plan.tool_id == "pdf.reorder_pages"
    assert reorder.plan.expected_pages == 3

    reverse = planner.plan("Reverse pages in report.pdf")
    assert reverse is not None and reverse.plan is not None
    assert reverse.plan.arguments["pages"] == "reverse"

    rotate = planner.plan("Rotate pages 1-2 in report.pdf by 90 degrees")
    assert rotate is not None and rotate.plan is not None
    assert rotate.plan.tool_id == "pdf.rotate_pages"
    assert rotate.plan.arguments["degrees"] == 90
    assert rotate.plan.arguments["pages"] == "1-2"

    missing_query = planner.plan("Search report.pdf")
    assert missing_query is not None and missing_query.plan is None
    assert missing_query.missing_information

    response = SimpleNamespace(provider="fake", model="fake-model")
    ai_search = _validate_ai_plan(
        json.dumps(
            {
                "intent": "pdf.search_text",
                "tool_id": "pdf.search_text",
                "arguments": {
                    "path": "report.pdf",
                    "query": "Beta",
                    "pages": "1-2",
                    "max_results": 5,
                },
                "expected_pages": None,
                "summary": "Search the PDF",
                "missing_information": [],
            }
        ),
        response,
    )
    assert not isinstance(ai_search, type(planner.plan("unsupported")))
    assert ai_search.tool_id == "pdf.search_text"  # type: ignore[union-attr]

    with pytest.raises(PlanningError, match="not allow-listed"):
        _validate_ai_plan(
            json.dumps(
                {
                    "intent": "pdf.rotate_pages",
                    "tool_id": "pdf.rotate_pages",
                    "arguments": {
                        "path": "report.pdf",
                        "degrees": 90,
                        "output_path": "rotated.pdf",
                    },
                    "expected_pages": None,
                    "summary": "Rotate it",
                    "missing_information": [],
                }
            ),
            response,
        )


def test_phase20_orchestrated_tasks_and_fresh_output_recovery(
    tmp_path: Path,
) -> None:
    settings, tasks, _execution, orchestrator = _orchestrator(tmp_path)
    _write_text_pdf(
        settings.workspace_dir / "report.pdf",
        ["Alpha page one", "Beta page two", "Gamma page three"],
    )

    extract_task = tasks.create(Task(instruction="Extract text from report.pdf"))
    extract_report = orchestrator.execute(
        extract_task.id,
        options=PlannerOptions(
            target_path="report.pdf", output_path="report-text.txt"
        ),
    )
    assert extract_report.success, extract_report.error
    assert extract_report.task.status is TaskStatus.SUCCEEDED
    assert extract_report.verification_report is not None
    assert extract_report.verification_report.passed

    (settings.workspace_dir / "collision.txt").write_text(
        "keep me", encoding="utf-8"
    )
    collision_task = tasks.create(Task(instruction="Extract text from report.pdf"))
    collision_report = orchestrator.execute(
        collision_task.id,
        options=PlannerOptions(
            target_path="report.pdf", output_path="collision.txt"
        ),
    )
    assert collision_report.success, collision_report.error
    assert collision_report.recovered is True
    assert (settings.workspace_dir / "collision.txt").read_text(
        encoding="utf-8"
    ) == "keep me"
    assert collision_report.tool_result is not None
    assert collision_report.tool_result.output is not None
    recovered_output = collision_report.tool_result.output["output_path"]
    assert recovered_output != "collision.txt"
    assert (settings.workspace_dir / recovered_output).is_file()

    search_task = tasks.create(Task(instruction='Search report.pdf for "Beta"'))
    search_report = orchestrator.execute(search_task.id)
    assert search_report.success, search_report.error
    assert search_report.tool_result is not None
    assert search_report.tool_result.output is not None
    assert search_report.tool_result.output["matching_pages"] == [2]

    rotate_task = tasks.create(
        Task(instruction="Rotate pages 2 in report.pdf by 180 degrees")
    )
    rotate_report = orchestrator.execute(rotate_task.id)
    assert rotate_report.success, rotate_report.error
    assert rotate_report.tool_result is not None
    assert rotate_report.tool_result.output is not None
    assert rotate_report.tool_result.output["page_rotations"] == [0, 180, 0]


def test_phase20_direct_cli_and_gui_controller(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    _write_text_pdf(
        workspace / "report.pdf",
        ["Alpha page one", "Beta page two", "Gamma page three"],
    )

    assert main(
        [
            "--root",
            str(tmp_path),
            "pdf",
            "extract-text",
            "report.pdf",
            "--output",
            "report.txt",
        ]
    ) == 0
    assert "Extracted PDF text: report.txt" in capsys.readouterr().out

    assert main(
        [
            "--root",
            str(tmp_path),
            "pdf",
            "search",
            "report.pdf",
            "Beta page",
        ]
    ) == 0
    search_output = capsys.readouterr().out
    assert "Occurrences: 1" in search_output
    assert "Page 2: Beta page two" in search_output

    assert main(
        [
            "--root",
            str(tmp_path),
            "pdf",
            "reorder",
            "report.pdf",
            "--pages",
            "reverse",
            "--output",
            "reversed.pdf",
        ]
    ) == 0
    assert "Page order: 3, 2, 1" in capsys.readouterr().out

    assert main(
        [
            "--root",
            str(tmp_path),
            "pdf",
            "rotate",
            "report.pdf",
            "--degrees",
            "90",
            "--pages",
            "3",
            "--output",
            "rotated.pdf",
        ]
    ) == 0
    assert "Final rotations: 0, 0, 90" in capsys.readouterr().out

    settings = Settings.from_env(tmp_path, environ={})
    controller = GuiController(settings)
    gui_search = controller.pdf_search_text("report.pdf", "Gamma")
    assert gui_search.success, gui_search.error
    gui_extract = controller.pdf_extract_text(
        "report.pdf", "gui-report.json", pages="1"
    )
    assert gui_extract.success, gui_extract.error
    summary = controller.doctor_summary()
    assert summary.version == __version__ == "0.29.0"
    assert summary.builtin_tools == 62
    assert "PHASE 27 PDF Signature Inspection" in summary.to_text()


def test_phase20_scope_schema_and_release_candidates() -> None:
    project_root = Path(__file__).resolve().parents[1]
    candidates = {path.as_posix() for path in release_candidate_files(project_root)}
    tool_ids = {
        item.spec.id for item in build_default_registry().list_tools()
    }

    assert __version__ == "0.29.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(tool_ids) == 62
    assert {
        "pdf.extract_text",
        "pdf.search_text",
        "pdf.reorder_pages",
        "pdf.rotate_pages",
    } <= tool_ids
    assert "docs/phase20.md" in candidates
    assert "tests/test_phase20_pdf_text_transforms.py" in candidates
