"""Phase 22: bounded scanned-PDF OCR through rendered pages and vision."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from pc_agent import __version__
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.documents.pdf import write_blank_pdf
from pc_agent.documents.pdf_ocr import (
    PdfOcrProviderOutput,
    PdfOcrRenderedPage,
    ocr_pdf,
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
from pc_agent.providers import CompletionRequest, CompletionResponse
from pc_agent.recovery import diagnose_tool_failure, diagnose_verification_failure
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION, Database
from pc_agent.storage.repositories import (
    ExecutionRepository,
    MemoryRepository,
    RecoveryRepository,
    TaskRepository,
)
from pc_agent.tools import build_default_registry
from pc_agent.tools.registry import ToolContext


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def _write_scanned_pdf(path: Path, labels: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    images: list[Image.Image] = []
    try:
        for label in labels:
            image = Image.new("RGB", (1000, 400), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((40, 40, 960, 360), outline="black", width=4)
            draw.text((80, 170), label, fill="black")
            images.append(image)
        images[0].save(
            path,
            save_all=True,
            append_images=images[1:],
            resolution=144.0,
        )
    finally:
        for image in images:
            image.close()


def _write_mixed_pdf(path: Path) -> None:
    folder = path.parent
    scanned = folder / "scanned-source.pdf"
    embedded = folder / "embedded-source.pdf"
    _write_scanned_pdf(scanned, ["SCAN ONE", "SCAN THREE"])
    _write_text_pdf(embedded, ["Embedded page two"])
    scan_reader = PdfReader(str(scanned))
    text_reader = PdfReader(str(embedded))
    writer = PdfWriter()
    try:
        writer.add_page(scan_reader.pages[0])
        writer.add_page(text_reader.pages[0])
        writer.add_page(scan_reader.pages[1])
        with path.open("wb") as handle:
            writer.write(handle)
    finally:
        writer.close()
        scan_reader.close()
        text_reader.close()
    scanned.unlink()
    embedded.unlink()


class StubOcrRouter:
    provider_names = ("fake-vision",)
    providers: tuple[Any, ...] = ()

    def __init__(
        self,
        texts: dict[int, str] | None = None,
        *,
        provider: str = "fake-vision",
    ) -> None:
        self.texts = texts or {
            1: "Scanned page one",
            2: "Scanned page two",
            3: "Scanned page three",
        }
        self.provider = provider
        self.calls = 0
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest):
        self.calls += 1
        self.requests.append(request)
        assert request.has_images
        assert request.images[0].mime_type == "image/jpeg"
        assert request.images[0].data.startswith(b"\xff\xd8")
        match = re.search(r"PDF page (\d+)", request.last_user_message)
        assert match is not None
        page = int(match.group(1))
        return SimpleNamespace(
            response=CompletionResponse(
                content=self.texts[page],
                provider=self.provider,
                model="fake-ocr-1",
            ),
            attempts=(object(),),
        )


def _context(workspace: Path, router: Any | None = None, **kwargs: Any) -> ToolContext:
    return ToolContext.for_workspace(
        workspace,
        vision_router_factory=(lambda: router) if router is not None else None,
        **kwargs,
    )


def _orchestrator(
    tmp_path: Path,
    *,
    environ: dict[str, str] | None = None,
    router: Any | None = None,
):
    settings = Settings.from_env(
        tmp_path,
        environ={"PC_AGENT_ALLOW_VISION": "1"} if environ is None else environ,
    )
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
        model_router=router,
        recovery_repository=RecoveryRepository(database),
        memory_repository=MemoryRepository(database),
        recovery_backoff_seconds=0,
    )
    return settings, tasks, execution, orchestrator


def test_pdf_ocr_core_skips_embedded_and_writes_text_and_json(tmp_path: Path) -> None:
    source = tmp_path / "mixed.pdf"
    _write_mixed_pdf(source)
    source_hash = _sha256(source)
    rendered: list[PdfOcrRenderedPage] = []

    def callback(page: PdfOcrRenderedPage) -> PdfOcrProviderOutput:
        rendered.append(page)
        assert page.data.startswith(b"\xff\xd8")
        assert page.sent_width <= 2048 and page.sent_height <= 2048
        assert page.sha256 == hashlib.sha256(page.data).hexdigest()
        return PdfOcrProviderOutput(
            text=f"OCR page {page.page}",
            provider="fake-vision",
            model="fake-ocr-1",
            attempts=1,
        )

    text_result = ocr_pdf(
        source,
        tmp_path / "mixed.txt",
        dpi=144,
        language="English",
        ocr_page=callback,
    )
    assert [page.page for page in rendered] == [1, 3]
    assert text_result.embedded_pages == (2,)
    assert text_result.ocr_pages == (1, 3)
    assert text_result.provider_attempts == 2
    assert text_result.sha256 == _sha256(tmp_path / "mixed.txt")
    assert _sha256(source) == source_hash
    assert (tmp_path / "mixed.txt").read_text(encoding="utf-8") == (
        "--- Page 1 [OCR] ---\nOCR page 1\n\n"
        "--- Page 2 [embedded text] ---\nEmbedded page two\n\n"
        "--- Page 3 [OCR] ---\nOCR page 3\n"
    )

    rendered.clear()
    json_result = ocr_pdf(
        source,
        tmp_path / "mixed.json",
        output_format="json",
        dpi=96,
        skip_text_pages=False,
        ocr_page=callback,
    )
    assert [page.page for page in rendered] == [1, 2, 3]
    assert json_result.embedded_pages == ()
    assert json_result.ocr_pages == (1, 2, 3)
    payload = json.loads((tmp_path / "mixed.json").read_text(encoding="utf-8"))
    assert payload["method"] == "pdf-ocr-v1"
    assert payload["pages"][1]["text"] == "OCR page 2"
    assert payload["pages"][1]["provider"] == "fake-vision"
    assert payload["pages"][1]["sent_size_bytes"] > 0


def test_pdf_ocr_core_bounds_sentinel_and_prechecks(tmp_path: Path) -> None:
    source = tmp_path / "mixed.pdf"
    _write_mixed_pdf(source)
    calls: list[int] = []

    def no_text(page: PdfOcrRenderedPage) -> PdfOcrProviderOutput:
        calls.append(page.page)
        return PdfOcrProviderOutput(
            "[NO READABLE TEXT]", "fake-vision", "fake-ocr-1", attempts=1
        )

    result = ocr_pdf(source, tmp_path / "blank-scan.txt", ocr_page=no_text)
    assert calls == [1, 3]
    assert result.text_pages == 1
    assert result.pages[0].text == ""
    assert result.pages[2].text == ""

    existing = tmp_path / "existing.txt"
    existing.write_text("keep", encoding="utf-8")
    calls.clear()
    with pytest.raises(ToolError, match="already exists"):
        ocr_pdf(source, existing, ocr_page=no_text)
    assert calls == []

    many = tmp_path / "many.pdf"
    write_blank_pdf(many, 11)
    with pytest.raises(ToolError, match="limited to 10 selected pages"):
        ocr_pdf(many, tmp_path / "many.txt", ocr_page=no_text)
    assert calls == []

    with pytest.raises(ToolError, match="DPI must be between 72 and 300"):
        ocr_pdf(source, tmp_path / "bad-dpi.txt", dpi=301, ocr_page=no_text)

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
        ocr_pdf(encrypted, tmp_path / "encrypted.txt", ocr_page=no_text)


def test_pdf_ocr_tool_gate_embedded_shortcut_and_fake_provider(tmp_path: Path) -> None:
    _write_mixed_pdf(tmp_path / "mixed.pdf")
    registry = build_default_registry()

    disabled = registry.invoke(
        "pdf.ocr",
        {"path": "mixed.pdf", "output_path": "disabled.txt", "pages": "1"},
        _context(tmp_path, StubOcrRouter()),
    )
    assert not disabled.success
    assert "disabled by policy" in (disabled.error or "")
    assert not (tmp_path / "disabled.txt").exists()

    embedded_only = registry.invoke(
        "pdf.ocr",
        {"path": "mixed.pdf", "output_path": "embedded.txt", "pages": "2"},
        _context(tmp_path),
    )
    assert embedded_only.success, embedded_only.error
    assert embedded_only.output is not None
    assert embedded_only.output["embedded_pages"] == [2]
    assert embedded_only.output["ocr_pages"] == []
    assert embedded_only.output["providers"] == []

    missing_router = registry.invoke(
        "pdf.ocr",
        {"path": "mixed.pdf", "output_path": "missing-router.txt", "pages": "1"},
        _context(tmp_path, vision_allowed=True),
    )
    assert not missing_router.success
    assert "No provider router" in (missing_router.error or "")

    router = StubOcrRouter()
    result = registry.invoke(
        "pdf.ocr",
        {
            "path": "mixed.pdf",
            "output_path": "tool.json",
            "pages": "1,3",
            "dpi": 120,
            "language": "Urdu",
        },
        _context(tmp_path, router, vision_allowed=True),
    )
    assert result.success, result.error
    assert result.output is not None
    assert result.output["selected_pages"] == [1, 3]
    assert result.output["providers"] == ["fake-vision"]
    assert result.output["provider_attempts"] == 2
    assert result.output["page_details"][0]["sent_sha256"]
    assert router.calls == 2
    assert "primary language is Urdu" in router.requests[0].last_user_message
    assert "untrusted data" in router.requests[0].system_instruction

    invalid_dpi = registry.invoke(
        "pdf.ocr",
        {"path": "mixed.pdf", "output_path": "bad.txt", "dpi": 301},
        _context(tmp_path, router, vision_allowed=True),
    )
    assert not invalid_dpi.success


def test_pdf_ocr_planner_ai_boundary_and_no_retry(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_mixed_pdf(workspace / "mixed.pdf")
    planner = DeterministicPlanner(workspace)

    outcome = planner.plan(
        "OCR the scanned PDF mixed.pdf",
        PlannerOptions(
            target_path="mixed.pdf",
            output_path="mixed-ocr.json",
            page_selection="1,3",
            pdf_ocr_dpi=180,
            pdf_ocr_language="Urdu",
            pdf_ocr_max_output_tokens=1500,
        ),
    )
    assert outcome is not None and outcome.plan is not None
    assert outcome.plan.tool_id == "pdf.ocr"
    assert outcome.plan.arguments == {
        "path": "mixed.pdf",
        "output_path": "mixed-ocr.json",
        "format": "json",
        "pages": "1,3",
        "dpi": 180,
        "language": "Urdu",
        "max_output_tokens": 1500,
    }
    assert outcome.plan.expected_pages == 2

    all_pages = planner.plan(
        "Recognize text in mixed.pdf",
        PlannerOptions(
            target_path="mixed.pdf",
            output_path="all.txt",
            page_selection="1-2",
            pdf_ocr_skip_text_pages=False,
        ),
    )
    assert all_pages is not None and all_pages.plan is not None
    assert all_pages.plan.arguments["skip_text_pages"] is False

    too_many = planner.plan(
        "OCR mixed.pdf",
        PlannerOptions(target_path="mixed.pdf", page_selection="1-11"),
    )
    assert too_many is not None and too_many.plan is None
    assert "limited to 10" in too_many.missing_information[0]

    response = SimpleNamespace(provider="fake", model="fake-model")
    with pytest.raises(PlanningError, match="not allow-listed"):
        _validate_ai_plan(
            json.dumps(
                {
                    "intent": "pdf.ocr",
                    "tool_id": "pdf.ocr",
                    "arguments": {
                        "path": "mixed.pdf",
                        "output_path": "ai.txt",
                    },
                    "expected_pages": None,
                    "summary": "OCR it",
                    "missing_information": [],
                }
            ),
            response,
        )

    plan = TaskPlan(
        intent="pdf.ocr",
        tool_id="pdf.ocr",
        arguments={"path": "mixed.pdf", "output_path": "out.txt"},
        summary="OCR",
        planner="deterministic",
    )
    assert not diagnose_tool_failure(
        plan, "Output file already exists", workspace_dir=workspace
    ).retryable
    assert not diagnose_verification_failure(
        plan, "hash mismatch", workspace_dir=workspace
    ).retryable


def test_pdf_ocr_orchestrated_approval_verification_and_denial(
    tmp_path: Path,
) -> None:
    router = StubOcrRouter()
    settings, tasks, execution, orchestrator = _orchestrator(tmp_path, router=router)
    _write_mixed_pdf(settings.workspace_dir / "mixed.pdf")
    options = PlannerOptions(
        target_path="mixed.pdf",
        output_path="denied.txt",
        page_selection="1-3",
        pdf_ocr_dpi=120,
        token_budget=100_000,
        provider_call_budget=10,
    )

    denied_task = tasks.create(Task(instruction="OCR the scanned PDF mixed.pdf"))
    denied = orchestrator.execute(
        denied_task.id,
        options=options,
        approval_callback=lambda _request: False,
    )
    assert denied.task.status is TaskStatus.CANCELLED
    assert router.calls == 0
    assert not (settings.workspace_dir / "denied.txt").exists()
    approvals = execution.list_approvals(denied_task.id)
    assert approvals[0].action == "vision_analysis"
    assert approvals[0].scope["tool_id"] == "pdf.ocr"

    approved_options = PlannerOptions(
        target_path="mixed.pdf",
        output_path="approved.json",
        page_selection="1-3",
        pdf_ocr_dpi=120,
        pdf_ocr_language="English",
        token_budget=100_000,
        provider_call_budget=10,
    )
    task = tasks.create(Task(instruction="OCR the scanned PDF mixed.pdf"))
    report = orchestrator.execute(
        task.id,
        options=approved_options,
        approval_callback=lambda _request: True,
    )
    assert report.success, report.error
    assert report.task.status is TaskStatus.SUCCEEDED
    assert report.verification_valid is True
    assert report.verification_report is not None and report.verification_report.passed
    assert "not deterministically re-graded" in (report.verification_message or "")
    assert router.calls == 2
    assert report.tool_result is not None and report.tool_result.output is not None
    assert report.tool_result.output["embedded_pages"] == [2]
    assert report.tool_result.output["ocr_pages"] == [1, 3]


def test_pdf_ocr_orchestrated_disabled_gate_is_not_retried(tmp_path: Path) -> None:
    router = StubOcrRouter()
    settings, tasks, _execution, orchestrator = _orchestrator(
        tmp_path, environ={}, router=router
    )
    _write_mixed_pdf(settings.workspace_dir / "mixed.pdf")
    task = tasks.create(Task(instruction="OCR the scanned PDF mixed.pdf"))
    report = orchestrator.execute(
        task.id,
        options=PlannerOptions(
            target_path="mixed.pdf",
            output_path="disabled.txt",
            page_selection="1",
        ),
        approval_callback=lambda _request: True,
    )
    assert report.task.status is TaskStatus.FAILED
    assert "disabled by policy" in (report.error or "")
    assert len(report.recovery_attempts) == 1
    assert router.calls == 0


def test_pdf_ocr_direct_cli_json_and_gui_controller(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    _write_mixed_pdf(workspace / "mixed.pdf")
    router = StubOcrRouter()
    monkeypatch.setenv("PC_AGENT_ALLOW_VISION", "1")
    monkeypatch.setattr("pc_agent.cli.build_model_router", lambda _settings: router)
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    denied = main(
        [
            "--root",
            str(tmp_path),
            "pdf",
            "ocr",
            "mixed.pdf",
            "--output",
            "denied.txt",
            "--pages",
            "1",
        ]
    )
    denied_out = capsys.readouterr().out
    assert denied == 4
    assert "pc-agent pdf ocr" in denied_out and "--yes" in denied_out
    assert router.calls == 0

    approved = main(
        [
            "--root",
            str(tmp_path),
            "pdf",
            "ocr",
            "mixed.pdf",
            "--output",
            "cli.json",
            "--pages",
            "1-3",
            "--dpi",
            "120",
            "--yes",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert approved == 0
    assert payload["success"] is True
    assert payload["output"]["ocr_pages"] == [1, 3]
    assert router.calls == 2

    settings = Settings.from_env(
        tmp_path, environ={"PC_AGENT_ALLOW_VISION": "1"}
    )
    controller = GuiController(settings)
    gui_router = StubOcrRouter()
    controller._router = gui_router  # test seam used by _context()
    gui_result = controller.pdf_ocr(
        "mixed.pdf",
        "gui.txt",
        pages="3",
        dpi=120,
        language="English",
    )
    assert gui_result.success, gui_result.error
    assert gui_router.calls == 1
    summary = controller.doctor_summary()
    assert summary.version == __version__ == "0.29.0"
    assert summary.builtin_tools == 62
    assert "PHASE 27 PDF Signature Inspection" in summary.to_text()


def test_phase22_scope_schema_dependency_and_release_candidates() -> None:
    project_root = Path(__file__).resolve().parents[1]
    candidates = {path.as_posix() for path in release_candidate_files(project_root)}
    tool_ids = {item.spec.id for item in build_default_registry().list_tools()}
    pyproject = (project_root / "pyproject.toml").read_text(encoding="utf-8")

    assert __version__ == "0.29.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(tool_ids) == 62
    assert "pdf.ocr" in tool_ids
    assert '"pypdfium2>=5,<6"' in pyproject
    assert "docs/phase22.md" in candidates
    assert "src/pc_agent/documents/pdf_ocr.py" in candidates
    assert "tests/test_phase22_pdf_ocr.py" in candidates
