"""Phase 25: bounded PDF AcroForm inspection and filling."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    RectangleObject,
    TextStringObject,
)

from pc_agent import __version__
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.documents.pdf import write_blank_pdf
from pc_agent.documents.pdf_forms import fill_pdf_form, inspect_pdf_form
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


def _stream(writer: PdfWriter, data: str = ""):
    stream = DecodedStreamObject()
    stream.set_data(data.encode("ascii"))
    return writer._add_object(stream)


def _widget(rect: list[int], **entries: Any) -> DictionaryObject:
    widget = DictionaryObject()
    widget.update(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/Rect"): RectangleObject(rect),
            NameObject("/F"): NumberObject(4),
        }
    )
    widget.update(entries)
    return widget


def _write_form_pdf(path: Path, *, include_password: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    try:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject()
        font.update(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
        fonts = DictionaryObject()
        fonts[NameObject("/Helv")] = writer._add_object(font)
        resources = DictionaryObject()
        resources[NameObject("/Font")] = fonts

        fields: list[Any] = []
        annotations: list[Any] = []

        def add_field(widget: DictionaryObject) -> None:
            reference = writer._add_object(widget)
            fields.append(reference)
            annotations.append(reference)

        add_field(
            _widget(
                [60, 700, 300, 730],
                **{
                    NameObject("/FT"): NameObject("/Tx"),
                    NameObject("/T"): TextStringObject("FullName"),
                    NameObject("/V"): TextStringObject("Old"),
                    NameObject("/DA"): TextStringObject("/Helv 12 Tf 0 g"),
                    NameObject("/MaxLen"): NumberObject(10),
                },
            )
        )
        add_field(
            _widget(
                [60, 650, 300, 680],
                **{
                    NameObject("/FT"): NameObject("/Ch"),
                    NameObject("/T"): TextStringObject("City"),
                    NameObject("/V"): TextStringObject("Lahore"),
                    NameObject("/Opt"): ArrayObject(
                        [TextStringObject("Karachi"), TextStringObject("Lahore")]
                    ),
                    NameObject("/DA"): TextStringObject("/Helv 12 Tf 0 g"),
                    NameObject("/Ff"): NumberObject(2),
                },
            )
        )
        add_field(
            _widget(
                [60, 600, 300, 640],
                **{
                    NameObject("/FT"): NameObject("/Ch"),
                    NameObject("/T"): TextStringObject("Interests"),
                    NameObject("/V"): ArrayObject([TextStringObject("Reading")]),
                    NameObject("/Opt"): ArrayObject(
                        [
                            TextStringObject("Reading"),
                            TextStringObject("Sports"),
                            TextStringObject("Music"),
                        ]
                    ),
                    NameObject("/DA"): TextStringObject("/Helv 12 Tf 0 g"),
                    NameObject("/Ff"): NumberObject(2097152),
                },
            )
        )

        normal = DictionaryObject()
        normal[NameObject("/Yes")] = _stream(writer, "0 0 20 20 re f")
        normal[NameObject("/Off")] = _stream(writer)
        appearance = DictionaryObject()
        appearance[NameObject("/N")] = normal
        add_field(
            _widget(
                [60, 560, 80, 580],
                **{
                    NameObject("/FT"): NameObject("/Btn"),
                    NameObject("/T"): TextStringObject("Agree"),
                    NameObject("/V"): NameObject("/Off"),
                    NameObject("/AS"): NameObject("/Off"),
                    NameObject("/AP"): appearance,
                },
            )
        )

        parent = DictionaryObject()
        parent.update(
            {
                NameObject("/FT"): NameObject("/Btn"),
                NameObject("/T"): TextStringObject("Contact"),
                NameObject("/V"): NameObject("/Email"),
                NameObject("/Ff"): NumberObject(16384),
            }
        )
        parent_ref = writer._add_object(parent)
        radio_kids: list[Any] = []
        for index, state in enumerate(("Email", "Phone")):
            kid_normal = DictionaryObject()
            kid_normal[NameObject(f"/{state}")] = _stream(
                writer, "0 0 20 20 re f"
            )
            kid_normal[NameObject("/Off")] = _stream(writer)
            kid_appearance = DictionaryObject()
            kid_appearance[NameObject("/N")] = kid_normal
            kid = _widget(
                [60, 510 - index * 30, 80, 530 - index * 30],
                **{
                    NameObject("/Parent"): parent_ref,
                    NameObject("/AS"): NameObject(
                        f"/{state}" if state == "Email" else "/Off"
                    ),
                    NameObject("/AP"): kid_appearance,
                },
            )
            kid_ref = writer._add_object(kid)
            radio_kids.append(kid_ref)
            annotations.append(kid_ref)
        parent[NameObject("/Kids")] = ArrayObject(radio_kids)
        fields.append(parent_ref)

        add_field(
            _widget(
                [60, 430, 300, 460],
                **{
                    NameObject("/FT"): NameObject("/Tx"),
                    NameObject("/T"): TextStringObject("Locked"),
                    NameObject("/V"): TextStringObject("Keep"),
                    NameObject("/DA"): TextStringObject("/Helv 12 Tf 0 g"),
                    NameObject("/Ff"): NumberObject(1),
                },
            )
        )
        if include_password:
            add_field(
                _widget(
                    [60, 350, 300, 380],
                    **{
                        NameObject("/FT"): NameObject("/Tx"),
                        NameObject("/T"): TextStringObject("Secret"),
                        NameObject("/V"): TextStringObject("hidden"),
                        NameObject("/DA"): TextStringObject("/Helv 12 Tf 0 g"),
                        NameObject("/Ff"): NumberObject(8192),
                    },
                )
            )
        add_field(
            _widget(
                [60, 390, 300, 420],
                **{
                    NameObject("/FT"): NameObject("/Sig"),
                    NameObject("/T"): TextStringObject("Signature"),
                },
            )
        )

        page[NameObject("/Annots")] = ArrayObject(annotations)
        acro_form = DictionaryObject()
        acro_form.update(
            {
                NameObject("/Fields"): ArrayObject(fields),
                NameObject("/DR"): resources,
                NameObject("/DA"): TextStringObject("/Helv 12 Tf 0 g"),
                NameObject("/NeedAppearances"): BooleanObject(True),
            }
        )
        writer._root_object.update(
            {NameObject("/AcroForm"): writer._add_object(acro_form)}
        )
        with path.open("wb") as handle:
            writer.write(handle)
    finally:
        writer.close()


def _invoke(tool_id: str, workspace: Path, arguments: dict[str, Any]):
    return build_default_registry().invoke(
        tool_id, arguments, ToolContext.for_workspace(workspace)
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


def test_pdf_form_core_inspect_fill_and_rejections(tmp_path: Path) -> None:
    source = tmp_path / "form.pdf"
    _write_form_pdf(source)
    before_hash = _sha256(source)

    inspected = inspect_pdf_form(source)
    fields = inspected.by_name()
    assert inspected.pages == 1
    assert inspected.field_count == 7
    assert fields["FullName"].value == "Old"
    assert fields["FullName"].max_length == 10
    assert fields["City"].required is True
    assert fields["City"].options == ("Karachi", "Lahore")
    assert fields["Interests"].multi_select is True
    assert fields["Interests"].value == ["Reading"]
    assert fields["Agree"].value is False
    assert fields["Contact"].radio is True
    assert fields["Contact"].value == "Email"
    assert fields["Contact"].states == ("Email", "Phone")
    assert fields["Contact"].widget_count == 2
    assert fields["Locked"].read_only is True
    assert fields["Signature"].field_type == "signature"

    output = tmp_path / "filled.pdf"
    result = fill_pdf_form(
        source,
        output,
        {
            "FullName": "Ali",
            "City": "Karachi",
            "Interests": ["Reading", "Music"],
            "Agree": True,
            "Contact": "Phone",
        },
    )
    assert _sha256(source) == before_hash
    assert result.changed_fields == (
        "FullName",
        "City",
        "Interests",
        "Agree",
        "Contact",
    )
    filled = inspect_pdf_form(output).by_name()
    assert filled["FullName"].value == "Ali"
    assert filled["City"].value == "Karachi"
    assert filled["Interests"].value == ["Reading", "Music"]
    assert filled["Agree"].value is True
    assert filled["Contact"].value == "Phone"
    assert filled["Locked"].value == "Keep"

    with pytest.raises(ToolError, match="never overwrite"):
        fill_pdf_form(source, output, {"FullName": "Again"})
    rejected = tmp_path / "rejected.pdf"
    cases = [
        ({"Missing": "x"}, "not found"),
        ({"Locked": "new"}, "read-only"),
        ({"City": "Quetta"}, "not an option"),
        ({"City": ""}, "cannot be empty"),
        ({"FullName": "12345678901"}, "10-character"),
        ({"Signature": "x"}, "cannot be filled"),
        ({}, "At least one"),
    ]
    for values, message in cases:
        with pytest.raises(ToolError, match=message):
            fill_pdf_form(source, rejected, values)
        assert not rejected.exists()

    password_form = tmp_path / "password-form.pdf"
    _write_form_pdf(password_form, include_password=True)
    assert inspect_pdf_form(password_form).by_name()["Secret"].password is True
    with pytest.raises(ToolError, match="recorded tool arguments"):
        fill_pdf_form(
            password_form,
            tmp_path / "password-filled.pdf",
            {"Secret": "new-secret"},
        )

    blank = tmp_path / "blank.pdf"
    write_blank_pdf(blank, pages=1)
    assert inspect_pdf_form(blank).field_count == 0
    with pytest.raises(ToolError, match="no interactive AcroForm"):
        fill_pdf_form(blank, tmp_path / "blank-filled.pdf", {"Name": "x"})

    encrypted = tmp_path / "encrypted.pdf"
    writer = PdfWriter()
    try:
        writer.append(str(source))
        writer.encrypt("phase25-test")
        with encrypted.open("wb") as handle:
            writer.write(handle)
    finally:
        writer.close()
    with pytest.raises(ToolError, match="Encrypted PDFs"):
        inspect_pdf_form(encrypted)


def test_pdf_form_registered_tools(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_form_pdf(workspace / "form.pdf")

    inspected = _invoke("pdf.inspect_form", workspace, {"path": "form.pdf"})
    assert inspected.success, inspected.error
    assert inspected.output is not None
    assert inspected.output["field_count"] == 7

    filled = _invoke(
        "pdf.fill_form",
        workspace,
        {
            "path": "form.pdf",
            "output_path": "filled.pdf",
            "values": [
                {"name": "FullName", "value": "Sara"},
                {"name": "Agree", "value": "true"},
                {"name": "Interests", "value": ["Sports"]},
            ],
        },
    )
    assert filled.success, filled.error
    assert filled.output is not None
    assert filled.output["values"] == [
        {"name": "FullName", "value": "Sara"},
        {"name": "Agree", "value": True},
        {"name": "Interests", "value": ["Sports"]},
    ]

    duplicate = _invoke(
        "pdf.fill_form",
        workspace,
        {
            "path": "form.pdf",
            "output_path": "duplicate.pdf",
            "values": [
                {"name": "FullName", "value": "One"},
                {"name": "FullName", "value": "Two"},
            ],
        },
    )
    assert not duplicate.success
    assert "Duplicate" in (duplicate.error or "")


def test_pdf_form_planner_ai_boundary_and_recovery(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_form_pdf(workspace / "form.pdf")
    planner = DeterministicPlanner(workspace)

    inspected = planner.plan(
        "Inspect the PDF form fields in form.pdf",
        PlannerOptions(target_path="form.pdf"),
    )
    assert inspected is not None and inspected.plan is not None
    assert inspected.plan.tool_id == "pdf.inspect_form"

    filled = planner.plan(
        "Fill the PDF form form.pdf",
        PlannerOptions(
            target_path="form.pdf",
            output_path="filled.pdf",
            pdf_form_values=(("FullName", "Ali"), ("Agree", "true")),
        ),
    )
    assert filled is not None and filled.plan is not None
    assert filled.plan.tool_id == "pdf.fill_form"
    assert filled.plan.arguments == {
        "path": "form.pdf",
        "output_path": "filled.pdf",
        "values": [
            {"name": "FullName", "value": "Ali"},
            {"name": "Agree", "value": "true"},
        ],
    }

    missing = planner.plan(
        "Fill the PDF form form.pdf",
        PlannerOptions(target_path="form.pdf"),
    )
    assert missing is not None and missing.plan is None
    assert "--form-field" in missing.missing_information[0]

    response = SimpleNamespace(provider="fake", model="fake-model")
    for tool_id, arguments in (
        ("pdf.inspect_form", {"path": "form.pdf"}),
        (
            "pdf.fill_form",
            {
                "path": "form.pdf",
                "output_path": "ai-filled.pdf",
                "values": [{"name": "FullName", "value": "Ali"}],
            },
        ),
    ):
        with pytest.raises(PlanningError, match="not allow-listed"):
            _validate_ai_plan(
                json.dumps(
                    {
                        "intent": tool_id,
                        "tool_id": tool_id,
                        "arguments": arguments,
                        "expected_pages": None,
                        "summary": "Use PDF form",
                        "missing_information": [],
                    }
                ),
                response,
            )

    (workspace / "collision.pdf").write_bytes(b"keep")
    plan = TaskPlan(
        intent="pdf.fill_form",
        tool_id="pdf.fill_form",
        arguments={
            "path": "form.pdf",
            "output_path": "collision.pdf",
            "values": [{"name": "FullName", "value": "Ali"}],
        },
        summary="Fill form",
        planner="deterministic",
    )
    diagnosis = diagnose_tool_failure(
        plan, "Output file already exists", workspace_dir=workspace
    )
    assert diagnosis.retryable is True
    assert diagnosis.revised_arguments is not None
    assert diagnosis.revised_arguments["output_path"] != "collision.pdf"


def test_pdf_form_orchestrated_verification_and_collision_recovery(
    tmp_path: Path,
) -> None:
    settings, tasks, orchestrator = _orchestrator(tmp_path)
    _write_form_pdf(settings.workspace_dir / "form.pdf")

    task = tasks.create(Task(instruction="Fill the PDF form form.pdf"))
    report = orchestrator.execute(
        task.id,
        options=PlannerOptions(
            target_path="form.pdf",
            output_path="filled.pdf",
            pdf_form_values=(
                ("FullName", "Ali"),
                ("City", "Karachi"),
                ("Agree", "true"),
            ),
        ),
    )
    assert report.success, report.error
    assert report.task.status is TaskStatus.SUCCEEDED
    assert report.verification_valid is True
    assert report.verification_report is not None
    assert report.verification_report.passed
    assert "independent source/output field inspection" in (
        report.verification_message or ""
    )

    collision = settings.workspace_dir / "collision.pdf"
    collision.write_bytes(b"keep me")
    collision_task = tasks.create(
        Task(instruction="Fill the PDF form form.pdf")
    )
    recovered = orchestrator.execute(
        collision_task.id,
        options=PlannerOptions(
            target_path="form.pdf",
            output_path="collision.pdf",
            pdf_form_values=(("FullName", "Sara"),),
        ),
    )
    assert recovered.success, recovered.error
    assert recovered.recovered is True
    assert collision.read_bytes() == b"keep me"
    assert recovered.tool_result is not None
    assert recovered.tool_result.output is not None
    assert recovered.tool_result.output["output_path"] != "collision.pdf"


def test_pdf_form_direct_cli_task_and_gui_controller(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    _write_form_pdf(workspace / "form.pdf")
    root = ["--root", str(tmp_path)]

    assert main([*root, "pdf", "inspect-form", "form.pdf"]) == 0
    text = capsys.readouterr().out
    assert "FullName: text = 'Old'" in text
    assert "City: choice" in text

    assert main([*root, "pdf", "inspect-form", "form.pdf", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["output"]["field_count"] == 7

    assert main(
        [
            *root,
            "pdf",
            "fill-form",
            "form.pdf",
            "--output",
            "direct-filled.pdf",
            "--field",
            "FullName=Ali",
            "--field",
            "Agree=true",
            "--field",
            "Interests=Reading|Music",
        ]
    ) == 0
    text = capsys.readouterr().out
    assert "Filled PDF form: direct-filled.pdf" in text
    assert "Agree = True" in text

    assert main(
        [
            *root,
            "run",
            "Fill the PDF form form.pdf",
            "--target",
            "form.pdf",
            "--output",
            "task-filled.pdf",
            "--form-field",
            "City=Karachi",
        ]
    ) == 0
    capsys.readouterr()
    assert inspect_pdf_form(workspace / "task-filled.pdf").by_name()["City"].value == "Karachi"

    settings = Settings.from_env(tmp_path, environ={})
    controller = GuiController(settings)
    gui_inspect = controller.pdf_inspect_form("form.pdf")
    assert gui_inspect.success, gui_inspect.error
    gui_fill = controller.pdf_fill_form(
        "form.pdf", "gui-filled.pdf", {"Contact": "Phone"}
    )
    assert gui_fill.success, gui_fill.error
    summary = controller.doctor_summary()
    assert summary.version == __version__ == "0.29.0"
    assert summary.builtin_tools == 62
    assert "PHASE 27 PDF Signature Inspection" in summary.to_text()


def test_phase25_scope_docs_schema_and_release_candidates() -> None:
    project_root = Path(__file__).resolve().parents[1]
    candidates = {path.as_posix() for path in release_candidate_files(project_root)}
    tool_ids = {item.spec.id for item in build_default_registry().list_tools()}

    assert __version__ == "0.29.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(tool_ids) == 62
    assert {"pdf.inspect_form", "pdf.fill_form"} <= tool_ids
    assert "src/pc_agent/documents/pdf_forms.py" in candidates
    assert "docs/phase25.md" in candidates
    assert "tests/test_phase25_pdf_forms.py" in candidates

    phase_doc = (project_root / "docs" / "phase25.md").read_text(encoding="utf-8")
    assert "existing acroform" in phase_doc.lower()
    assert "never overwrites" in phase_doc.lower()
    assert "does not sign" in phase_doc.lower()
