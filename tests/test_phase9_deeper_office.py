"""Phase 9: deeper Office — PDF split/extract, DOCX edit, XLSX cells/formulas."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.documents.office import (
    build_docx,
    build_xlsx,
    evaluate_workbook_formulas,
    extract_office_text,
    read_xlsx_cells,
    replace_docx_text,
)
from pc_agent.documents.pdf import (
    parse_page_selection,
    verify_pdf,
    write_blank_pdf,
)
from pc_agent.domain.task import Task
from pc_agent.errors import ToolError
from pc_agent.gui.controller import GuiController
from pc_agent.orchestrator import TaskOrchestrator
from pc_agent.planning import DeterministicPlanner, PlannerOptions
from pc_agent.storage.db import Database
from pc_agent.storage.repositories import (
    ExecutionRepository,
    MemoryRepository,
    RecoveryRepository,
    TaskRepository,
)
from pc_agent.tools import build_default_registry
from pc_agent.tools.registry import ToolContext


def _invoke(tmp_path: Path, tool_id: str, arguments: dict):
    return build_default_registry().invoke(
        tool_id, arguments, ToolContext.for_workspace(tmp_path)
    )


# ----------------------------------------------------------------------
# PDF split / extract
# ----------------------------------------------------------------------
def test_parse_page_selection() -> None:
    assert parse_page_selection("1-3,5", 6) == [1, 2, 3, 5]
    assert parse_page_selection(" 2 , 4-5 ", 6) == [2, 4, 5]
    with pytest.raises(ToolError):
        parse_page_selection("1,1", 6)
    with pytest.raises(ToolError):
        parse_page_selection("7", 6)
    with pytest.raises(ToolError):
        parse_page_selection("3-1", 6)
    with pytest.raises(ToolError):
        parse_page_selection("a-b", 6)


def test_extract_and_split_pdf_tools(tmp_path: Path) -> None:
    write_blank_pdf(tmp_path / "book.pdf", 4)

    extracted = _invoke(
        tmp_path,
        "pdf.extract_pages",
        {"path": "book.pdf", "pages": "2-3", "output_path": "part.pdf"},
    )
    assert extracted.success, extracted.error
    assert extracted.output is not None
    assert extracted.output["page_count"] == 2
    assert verify_pdf(tmp_path / "part.pdf", expected_pages=2).valid

    again = _invoke(
        tmp_path,
        "pdf.extract_pages",
        {"path": "book.pdf", "pages": "1", "output_path": "part.pdf"},
    )
    assert not again.success
    assert "already exists" in (again.error or "")

    split = _invoke(
        tmp_path,
        "pdf.split",
        {"path": "book.pdf", "output_folder": "pages"},
    )
    assert split.success, split.error
    assert split.output is not None
    assert split.output["page_count"] == 4
    for name in split.output["outputs"]:
        assert verify_pdf(tmp_path / name, expected_pages=1).valid
    assert (tmp_path / "pages" / "book-page-001.pdf").is_file()


# ----------------------------------------------------------------------
# DOCX editing
# ----------------------------------------------------------------------
def test_docx_replace_and_append_tools(tmp_path: Path) -> None:
    (tmp_path / "in.docx").write_bytes(
        build_docx(title="Report", paragraphs=["Quarterly draft numbers", "draft only"])
    )

    replaced = _invoke(
        tmp_path,
        "office.docx_replace_text",
        {
            "input_path": "in.docx",
            "output_path": "out.docx",
            "find": "draft",
            "replace": "final",
        },
    )
    assert replaced.success, replaced.error
    assert replaced.output is not None
    assert replaced.output["replacements"] == 2
    text = extract_office_text(tmp_path / "out.docx").text
    assert "final only" in text and "draft" not in text

    appended = _invoke(
        tmp_path,
        "office.docx_append_paragraphs",
        {
            "input_path": "out.docx",
            "output_path": "out2.docx",
            "paragraphs": ["Appendix line"],
        },
    )
    assert appended.success, appended.error
    assert appended.output is not None
    assert appended.output["paragraphs_added"] == 1
    assert "Appendix line" in extract_office_text(tmp_path / "out2.docx").text


def test_docx_replace_across_split_runs(tmp_path: Path) -> None:
    original = build_docx(title="T", paragraphs=["quarterly draft report"])
    with zipfile.ZipFile(io.BytesIO(original)) as package:
        parts = {info.filename: package.read(info.filename) for info in package.infolist()}
    document = parts["word/document.xml"].decode("utf-8")
    # Split the word "draft" across two runs, like real Word output often does.
    split_document = document.replace(
        "quarterly draft report",
        "quarterly dra</w:t></w:r><w:r><w:t>ft report",
        1,
    )
    assert split_document != document
    parts["word/document.xml"] = split_document.encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        for name, data in parts.items():
            package.writestr(name, data)
    (tmp_path / "split.docx").write_bytes(buffer.getvalue())

    result = replace_docx_text(tmp_path / "split.docx", "draft", "final")
    assert result.replacements == 1
    (tmp_path / "fixed.docx").write_bytes(result.data)
    assert "quarterly final report" in extract_office_text(tmp_path / "fixed.docx").text


# ----------------------------------------------------------------------
# XLSX cells + formula evaluation
# ----------------------------------------------------------------------
def _workbook(tmp_path: Path) -> None:
    rows = [["Item", "Qty"], ["Pens", "2"], ["Pads", "3"]]
    (tmp_path / "book.xlsx").write_bytes(build_xlsx(rows, sheet_name="Data"))


def test_xlsx_set_cells_and_cached_formula_values(tmp_path: Path) -> None:
    _workbook(tmp_path)

    result = _invoke(
        tmp_path,
        "office.xlsx_set_cells",
        {
            "input_path": "book.xlsx",
            "output_path": "edited.xlsx",
            "cells": [
                {"reference": "A4", "value": "Total"},
                {"reference": "B4", "value": "=SUM(B2:B3)"},
                {"reference": "B5", "value": "=B4*2+AVERAGE(B2:B3)"},
            ],
        },
    )
    assert result.success, result.error
    assert result.output is not None
    computed = {
        item["reference"]: item["computed"] for item in result.output["formulas"]
    }
    assert computed == {"B4": 5.0, "B5": 12.5}

    workbook = read_xlsx_cells(tmp_path / "edited.xlsx")
    sheet = workbook.sheets[0]
    assert sheet.name == "Data"
    assert sheet.cells["B4"].formula == "SUM(B2:B3)"
    assert sheet.cells["B4"].value == "5"  # cached value written for other readers
    assert sheet.cells["A4"].value == "Total"
    # Untouched cells survived.
    assert sheet.cells["B2"].value == "2"


def test_formula_evaluator_unsupported_and_circular(tmp_path: Path) -> None:
    _workbook(tmp_path)
    result = _invoke(
        tmp_path,
        "office.xlsx_set_cells",
        {
            "input_path": "book.xlsx",
            "output_path": "bad.xlsx",
            "cells": [
                {"reference": "C2", "value": "=VLOOKUP(B2,A2:B3,2)"},
                {"reference": "C3", "value": "=C4+1"},
                {"reference": "C4", "value": "=C3+1"},
                {"reference": "C5", "value": "=1/0"},
            ],
        },
    )
    assert result.success, result.error
    assert result.output is not None
    errors = {item["reference"]: item["error"] for item in result.output["formulas"]}
    assert "unsupported function VLOOKUP" in str(errors["C2"])
    assert "circular" in str(errors["C3"])
    assert errors["C5"] == "#DIV/0!"

    verified = _invoke(tmp_path, "office.xlsx_verify_formulas", {"path": "bad.xlsx"})
    assert verified.success, verified.error
    assert verified.output is not None
    assert verified.output["formula_count"] == 4
    assert verified.output["computed_count"] == 0


def test_evaluate_workbook_formulas_directly(tmp_path: Path) -> None:
    _workbook(tmp_path)
    edited = _invoke(
        tmp_path,
        "office.xlsx_set_cells",
        {
            "input_path": "book.xlsx",
            "output_path": "math.xlsx",
            "cells": [{"reference": "B4", "value": "=(B2+B3)*MAX(B2:B3)-MIN(B2:B3)"}],
        },
    )
    assert edited.success, edited.error
    evaluations = evaluate_workbook_formulas(read_xlsx_cells(tmp_path / "math.xlsx"))
    assert len(evaluations) == 1
    assert evaluations[0].value == pytest.approx((2 + 3) * 3 - 2)


# ----------------------------------------------------------------------
# Planner
# ----------------------------------------------------------------------
def test_planner_deeper_office_intents(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_blank_pdf(workspace / "book.pdf", 5)
    (workspace / "doc.docx").write_bytes(build_docx(title="T", paragraphs=["x"]))
    (workspace / "data.xlsx").write_bytes(build_xlsx([["1"]], sheet_name="S"))
    planner = DeterministicPlanner(workspace)

    extract = planner.plan("Extract pages 2-3 from book.pdf")
    assert extract is not None and extract.plan is not None
    assert extract.plan.tool_id == "pdf.extract_pages"
    assert extract.plan.arguments["pages"] == "2-3"
    assert extract.plan.expected_pages == 2

    missing = planner.plan("Extract pages from book.pdf")
    assert missing is not None and missing.plan is None

    split = planner.plan("Split book.pdf")
    assert split is not None and split.plan is not None
    assert split.plan.tool_id == "pdf.split"

    replace = planner.plan(
        'Replace "draft" with "final" in the document',
        PlannerOptions(target_path="doc.docx"),
    )
    assert replace is not None and replace.plan is not None
    assert replace.plan.tool_id == "office.docx_replace_text"
    assert replace.plan.arguments["find"] == "draft"

    set_cell = planner.plan(
        'Set cell B2 to "=SUM(A1:A3)"',
        PlannerOptions(target_path="data.xlsx"),
    )
    assert set_cell is not None and set_cell.plan is not None
    assert set_cell.plan.tool_id == "office.xlsx_set_cells"
    assert set_cell.plan.arguments["cells"] == [
        {"reference": "B2", "value": "=SUM(A1:A3)"}
    ]

    verify = planner.plan(
        "Verify formulas", PlannerOptions(target_path="data.xlsx")
    )
    assert verify is not None and verify.plan is not None
    assert verify.plan.tool_id == "office.xlsx_verify_formulas"


# ----------------------------------------------------------------------
# Orchestrator end to end
# ----------------------------------------------------------------------
def _setup(tmp_path: Path):
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    tasks = TaskRepository(database)
    orchestrator = TaskOrchestrator(
        settings=settings,
        task_repository=tasks,
        execution_repository=ExecutionRepository(database),
        tool_registry=build_default_registry(),
        deterministic_planner=DeterministicPlanner(settings.workspace_dir),
        recovery_repository=RecoveryRepository(database),
        memory_repository=MemoryRepository(database),
        recovery_backoff_seconds=0,
    )
    return settings, tasks, orchestrator


def test_orchestrated_extract_xlsx_and_docx_tasks(tmp_path: Path) -> None:
    settings, tasks, orchestrator = _setup(tmp_path)
    write_blank_pdf(settings.workspace_dir / "book.pdf", 4)
    (settings.workspace_dir / "data.xlsx").write_bytes(
        build_xlsx([["2"], ["3"]], sheet_name="S")
    )
    (settings.workspace_dir / "doc.docx").write_bytes(
        build_docx(title="T", paragraphs=["draft body"])
    )

    extract_task = tasks.create(Task(instruction="Extract pages 1-2 from book.pdf"))
    extract_report = orchestrator.execute(extract_task.id)
    assert extract_report.success, extract_report.error
    assert extract_report.verification_report is not None
    kinds = [
        item.criterion.kind for item in extract_report.verification_report.results
    ]
    assert kinds == ["tool_verification", "output_artifact", "output_hash", "pdf_pages"]

    set_task = tasks.create(Task(instruction="Set the cell"))
    set_report = orchestrator.execute(
        set_task.id,
        options=PlannerOptions(
            target_path="data.xlsx", cell_reference="A3", cell_value="=SUM(A1:A2)"
        ),
    )
    assert set_report.success, set_report.error
    assert set_report.tool_result is not None and set_report.tool_result.output
    formulas = set_report.tool_result.output["formulas"]
    assert formulas[0]["computed"] == 5.0

    replace_task = tasks.create(Task(instruction="Replace text in the document"))
    replace_report = orchestrator.execute(
        replace_task.id,
        options=PlannerOptions(
            target_path="doc.docx", find_text="draft", replace_text="final"
        ),
    )
    assert replace_report.success, replace_report.error

    split_task = tasks.create(Task(instruction="Split book.pdf"))
    split_report = orchestrator.execute(split_task.id)
    assert split_report.success, split_report.error


# ----------------------------------------------------------------------
# CLI + GUI controller
# ----------------------------------------------------------------------
def test_cli_deeper_office_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    root = ["--root", str(tmp_path)]
    write_blank_pdf(settings.workspace_dir / "book.pdf", 3)
    (settings.workspace_dir / "data.xlsx").write_bytes(
        build_xlsx([["2"], ["3"]], sheet_name="S")
    )
    (settings.workspace_dir / "doc.docx").write_bytes(
        build_docx(title="T", paragraphs=["draft body"])
    )

    assert main([*root, "pdf", "extract", "book.pdf", "--pages", "1-2",
                 "--output", "part.pdf"]) == 0
    assert "Pages: 2 of 3" in capsys.readouterr().out

    assert main([*root, "pdf", "split", "book.pdf",
                 "--output-folder", "pages"]) == 0
    assert "book-page-003.pdf" in capsys.readouterr().out

    assert main([*root, "office", "docx-replace", "doc.docx", "--output",
                 "edited.docx", "--find", "draft", "--replace", "final"]) == 0
    capsys.readouterr()

    assert main([*root, "office", "xlsx-set", "data.xlsx", "--output",
                 "edited.xlsx", "--cell", "A3==SUM(A1:A2)"]) == 0
    capsys.readouterr()

    assert main([*root, "office", "xlsx-formulas", "edited.xlsx"]) == 0
    formulas_out = capsys.readouterr().out
    assert "SUM(A1:A2)" in formulas_out

    assert main([*root, "doctor"]) == 0
    doctor = capsys.readouterr().out
    assert "PHASE 27 PDF Signature Inspection" in doctor
    assert "built-ins=62" in doctor and "office=11" in doctor


def test_gui_controller_deeper_office(tmp_path: Path) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    controller = GuiController(settings, desktop_opener=lambda _target: None)
    write_blank_pdf(settings.workspace_dir / "book.pdf", 3)
    (settings.workspace_dir / "data.xlsx").write_bytes(
        build_xlsx([["2"], ["3"]], sheet_name="S")
    )

    extracted = controller.pdf_extract_pages("book.pdf", "2", "one.pdf")
    assert extracted.success, extracted.error

    set_result = controller.office_xlsx_set_cell(
        "data.xlsx", "edited.xlsx", "A3", "=SUM(A1:A2)"
    )
    assert set_result.success, set_result.error
    verified = controller.office_xlsx_verify_formulas("edited.xlsx")
    assert verified.success, verified.error
    assert verified.output is not None
    assert verified.output["computed_count"] == 1

    summary = controller.doctor_summary()
    assert summary.builtin_tools == 62
    assert summary.office_tools == 11
