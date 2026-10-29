"""Phase 23: bounded PDF table extraction from embedded text geometry."""

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
from pc_agent.documents.pdf import write_blank_pdf
from pc_agent.documents.pdf_tables import (
    extract_pdf_tables,
    extract_pdf_tables_document,
)
from pc_agent.domain.task import Task, TaskStatus
from pc_agent.errors import PlanningError, ToolError
from pc_agent.gui.controller import GuiController
from pc_agent.orchestrator import TaskOrchestrator
from pc_agent.planning import (
    DeterministicPlanner,
    PlannerOptions,
    TaskPlan,
    _validate_ai_plan,
)
from pc_agent.recovery import diagnose_tool_failure
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION, Database
from pc_agent.storage.repositories import ExecutionRepository, TaskRepository
from pc_agent.tools import build_default_registry
from pc_agent.tools.registry import ToolContext


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_positioned_pdf(
    path: Path, pages: list[list[tuple[str, int, int]]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    try:
        for items in pages:
            page = writer.add_blank_page(width=612, height=792)
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
            operations: list[str] = []
            for text, x, y in items:
                escaped = (
                    text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                )
                operations.append(f"BT /F1 14 Tf {x} {y} Td ({escaped}) Tj ET")
            stream = DecodedStreamObject()
            stream.set_data("\n".join(operations).encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
        with path.open("wb") as handle:
            writer.write(handle)
    finally:
        writer.close()


def _write_table_pdf(path: Path) -> None:
    _write_positioned_pdf(
        path,
        [
            [
                ("Quarterly report", 72, 735),
                ("Name", 72, 700),
                ("Qty", 220, 700),
                ("Price", 300, 700),
                ("Alpha One", 72, 670),
                ("2", 220, 670),
                ("10.50", 300, 670),
                ("Beta", 72, 640),
                ("3", 220, 640),
                ("7.25", 300, 640),
                ("End of report", 72, 600),
            ],
            [
                ("Code", 72, 720),
                ("Value", 220, 720),
                ("A1", 72, 690),
                ("10", 220, 690),
                ("A2", 72, 660),
                ("20", 220, 660),
                ("Second table", 72, 610),
                ("City", 72, 570),
                ("Count", 220, 570),
                ("Karachi", 72, 540),
                ("5", 220, 540),
                ("Lahore", 72, 510),
                ("4", 220, 510),
            ],
        ],
    )


def _invoke(workspace: Path, arguments: dict[str, Any]):
    return build_default_registry().invoke(
        "pdf.extract_tables", arguments, ToolContext.for_workspace(workspace)
    )


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
    return settings, tasks, orchestrator


def test_pdf_table_core_json_csv_and_multiple_tables(tmp_path: Path) -> None:
    source = tmp_path / "report.pdf"
    _write_table_pdf(source)
    source_hash = _sha256(source)

    document = extract_pdf_tables_document(source, [1])
    assert document.has_embedded_text is True
    assert document.table_count == 1
    assert document.tables[0].rows == (
        ("Name", "Qty", "Price"),
        ("Alpha One", "2", "10.50"),
        ("Beta", "3", "7.25"),
    )

    json_result = extract_pdf_tables(
        source, tmp_path / "report-tables.json", pages=[1]
    )
    assert json_result.sha256 == _sha256(tmp_path / "report-tables.json")
    assert _sha256(source) == source_hash
    payload = json.loads((tmp_path / "report-tables.json").read_text(encoding="utf-8"))
    assert payload["method"] == "pdf-tables-v1"
    assert payload["tables"][0]["rows"][1] == ["Alpha One", "2", "10.50"]
    assert payload["tables"][0]["bbox"]["left"] < payload["tables"][0]["bbox"]["right"]

    extract_pdf_tables(source, tmp_path / "report.csv", pages=[1], output_format="csv")
    assert (tmp_path / "report.csv").read_text(encoding="utf-8") == (
        "Name,Qty,Price\nAlpha One,2,10.50\nBeta,3,7.25\n"
    )

    page_two = extract_pdf_tables_document(source, [2])
    assert page_two.table_count == 2
    assert page_two.tables[0].rows[0] == ("Code", "Value")
    assert page_two.tables[1].rows[-1] == ("Lahore", "4")
    with pytest.raises(ToolError, match="exactly one detected table"):
        extract_pdf_tables(
            source,
            tmp_path / "multiple.csv",
            pages=[2],
            output_format="csv",
        )


def test_pdf_table_core_filters_bounds_and_no_embedded_text(tmp_path: Path) -> None:
    source = tmp_path / "report.pdf"
    _write_table_pdf(source)

    assert extract_pdf_tables_document(source, [1], min_rows=4).table_count == 0
    assert extract_pdf_tables_document(source, [1], min_columns=4).table_count == 0

    blank = tmp_path / "blank.pdf"
    write_blank_pdf(blank, 2)
    blank_document = extract_pdf_tables_document(blank)
    assert blank_document.has_embedded_text is False
    assert blank_document.table_count == 0
    extract_pdf_tables(blank, tmp_path / "blank.json")
    payload = json.loads((tmp_path / "blank.json").read_text(encoding="utf-8"))
    assert payload["has_embedded_text"] is False
    assert payload["tables"] == []
    with pytest.raises(ToolError, match="exactly one detected table"):
        extract_pdf_tables(blank, tmp_path / "blank.csv", output_format="csv")

    existing = tmp_path / "existing.json"
    existing.write_text("keep", encoding="utf-8")
    with pytest.raises(ToolError, match="already exists"):
        extract_pdf_tables(source, existing)
    with pytest.raises(ToolError, match=r"must end with \.json"):
        extract_pdf_tables(source, tmp_path / "wrong.txt")

    many = tmp_path / "many.pdf"
    write_blank_pdf(many, 101)
    with pytest.raises(ToolError, match="limited to 100 selected pages"):
        extract_pdf_tables_document(many)

    encrypted = tmp_path / "encrypted.pdf"
    writer = PdfWriter()
    try:
        writer.add_blank_page(width=200, height=200)
        writer.encrypt("secret")
        with encrypted.open("wb") as handle:
            writer.write(handle)
    finally:
        writer.close()
    with pytest.raises(ToolError, match="Encrypted PDFs"):
        extract_pdf_tables_document(encrypted)


def test_pdf_table_registered_tool(tmp_path: Path) -> None:
    _write_table_pdf(tmp_path / "report.pdf")
    before = _sha256(tmp_path / "report.pdf")

    result = _invoke(
        tmp_path,
        {
            "path": "report.pdf",
            "output_path": "tables.json",
            "pages": "1",
            "min_rows": 2,
            "min_columns": 3,
        },
    )
    assert result.success, result.error
    assert result.output is not None
    assert result.output["table_count"] == 1
    assert result.output["total_rows"] == 3
    assert result.output["total_cells"] == 9
    assert result.output["max_columns"] == 3
    assert result.output["tables"] == [
        {
            "page": 1,
            "index": 1,
            "row_count": 3,
            "column_count": 3,
            "cell_count": 9,
            "characters": 36,
        }
    ]
    assert result.output["sha256"] == _sha256(tmp_path / "tables.json")
    assert _sha256(tmp_path / "report.pdf") == before

    invalid = _invoke(
        tmp_path,
        {
            "path": "report.pdf",
            "output_path": "invalid.json",
            "min_columns": 51,
        },
    )
    assert not invalid.success


def test_pdf_table_planner_ai_boundary_and_recovery(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_table_pdf(workspace / "report.pdf")
    planner = DeterministicPlanner(workspace)

    outcome = planner.plan(
        "Extract tables from report.pdf",
        PlannerOptions(
            target_path="report.pdf",
            output_path="tables.csv",
            page_selection="1",
            pdf_table_min_rows=3,
            pdf_table_min_columns=3,
        ),
    )
    assert outcome is not None and outcome.plan is not None
    assert outcome.plan.tool_id == "pdf.extract_tables"
    assert outcome.plan.arguments == {
        "path": "report.pdf",
        "output_path": "tables.csv",
        "format": "csv",
        "pages": "1",
        "min_rows": 3,
        "min_columns": 3,
    }
    assert outcome.plan.expected_pages == 1

    too_many = planner.plan(
        "Extract tables from report.pdf",
        PlannerOptions(target_path="report.pdf", page_selection="1-101"),
    )
    assert too_many is not None and too_many.plan is None
    assert "limited to 100" in too_many.missing_information[0]

    response = SimpleNamespace(provider="fake", model="fake-model")
    with pytest.raises(PlanningError, match="not allow-listed"):
        _validate_ai_plan(
            json.dumps(
                {
                    "intent": "pdf.extract_tables",
                    "tool_id": "pdf.extract_tables",
                    "arguments": {
                        "path": "report.pdf",
                        "output_path": "ai.json",
                    },
                    "expected_pages": None,
                    "summary": "Extract tables",
                    "missing_information": [],
                }
            ),
            response,
        )

    (workspace / "collision.json").write_text("keep", encoding="utf-8")
    plan = TaskPlan(
        intent="pdf.extract_tables",
        tool_id="pdf.extract_tables",
        arguments={"path": "report.pdf", "output_path": "collision.json"},
        summary="Extract tables",
        planner="deterministic",
    )
    diagnosis = diagnose_tool_failure(
        plan, "Output file already exists", workspace_dir=workspace
    )
    assert diagnosis.retryable is True
    assert diagnosis.revised_arguments is not None
    assert diagnosis.revised_arguments["output_path"] != "collision.json"


def test_pdf_table_orchestrated_verification_and_collision_recovery(
    tmp_path: Path,
) -> None:
    settings, tasks, orchestrator = _orchestrator(tmp_path)
    _write_table_pdf(settings.workspace_dir / "report.pdf")

    task = tasks.create(Task(instruction="Extract tables from report.pdf"))
    report = orchestrator.execute(
        task.id,
        options=PlannerOptions(
            target_path="report.pdf",
            output_path="tables.json",
            page_selection="1",
        ),
    )
    assert report.success, report.error
    assert report.task.status is TaskStatus.SUCCEEDED
    assert report.verification_valid is True
    assert report.verification_report is not None and report.verification_report.passed
    assert "no OCR" in (report.verification_message or "")

    collision = settings.workspace_dir / "collision.json"
    collision.write_text("keep me", encoding="utf-8")
    collision_task = tasks.create(
        Task(instruction="Extract tables from report.pdf")
    )
    recovered = orchestrator.execute(
        collision_task.id,
        options=PlannerOptions(
            target_path="report.pdf",
            output_path="collision.json",
            page_selection="1",
        ),
    )
    assert recovered.success, recovered.error
    assert recovered.recovered is True
    assert collision.read_text(encoding="utf-8") == "keep me"
    assert recovered.tool_result is not None and recovered.tool_result.output is not None
    assert recovered.tool_result.output["output_path"] != "collision.json"


def test_pdf_table_direct_cli_and_gui_controller(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    _write_table_pdf(workspace / "report.pdf")

    assert main(
        [
            "--root",
            str(tmp_path),
            "pdf",
            "extract-tables",
            "report.pdf",
            "--output",
            "tables.csv",
            "--pages",
            "1",
        ]
    ) == 0
    text_output = capsys.readouterr().out
    assert "Extracted PDF tables: tables.csv" in text_output
    assert "Page 1 table 1: 3 rows x 3 columns" in text_output

    assert main(
        [
            "--root",
            str(tmp_path),
            "pdf",
            "extract-tables",
            "report.pdf",
            "--output",
            "tables.json",
            "--pages",
            "1",
            "--json",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["output"]["table_count"] == 1

    # Direct task flags flow through the deterministic planner.
    assert main(
        [
            "--root",
            str(tmp_path),
            "run",
            "Extract tables from report.pdf",
            "--target",
            "report.pdf",
            "--pages",
            "1",
            "--output",
            "task-tables.json",
            "--pdf-table-min-rows",
            "3",
            "--pdf-table-min-columns",
            "3",
        ]
    ) == 0
    capsys.readouterr()
    assert (workspace / "task-tables.json").is_file()

    settings = Settings.from_env(tmp_path, environ={})
    controller = GuiController(settings)
    gui_result = controller.pdf_extract_tables(
        "report.pdf", "gui-tables.json", pages="1", min_rows=3, min_columns=3
    )
    assert gui_result.success, gui_result.error
    summary = controller.doctor_summary()
    assert summary.version == __version__ == "0.29.0"
    assert summary.builtin_tools == 62
    assert "PHASE 27 PDF Signature Inspection" in summary.to_text()


def test_phase23_scope_schema_and_release_candidates() -> None:
    project_root = Path(__file__).resolve().parents[1]
    candidates = {path.as_posix() for path in release_candidate_files(project_root)}
    tool_ids = {item.spec.id for item in build_default_registry().list_tools()}

    assert __version__ == "0.29.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(tool_ids) == 62
    assert "pdf.extract_tables" in tool_ids
    assert "docs/phase23.md" in candidates
    assert "src/pc_agent/documents/pdf_tables.py" in candidates
    assert "tests/test_phase23_pdf_tables.py" in candidates
