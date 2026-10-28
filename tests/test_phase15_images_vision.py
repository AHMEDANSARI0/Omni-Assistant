"""Phase 15: bounded local image tools and approval-gated provider vision."""

from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.domain.task import Task, TaskStatus
from pc_agent.errors import ConfigError, PlanningError, ProviderError, ToolError
from pc_agent.gui.controller import GuiController
from pc_agent.images import (
    compare_images,
    convert_image,
    inspect_image,
    prepare_vision_image,
    transform_image,
)
from pc_agent.orchestrator import TaskOrchestrator
from pc_agent.planning import DeterministicPlanner, PlannerOptions, plan_with_provider
from pc_agent.providers import CompletionRequest, CompletionResponse, ImageInput
from pc_agent.providers.base import HttpJsonRequest
from pc_agent.providers.gemini_adapter import GeminiProvider
from pc_agent.providers.mock import MockProvider
from pc_agent.providers.ollama import OllamaProvider
from pc_agent.providers.openai_compatible import OpenAICompatibleProvider
from pc_agent.providers.router import ModelRouter
from pc_agent.storage.db import Database
from pc_agent.storage.repositories import (
    ExecutionRepository,
    MemoryRepository,
    RecoveryRepository,
    TaskRepository,
)
from pc_agent.tools import build_default_registry
from pc_agent.tools.registry import ToolContext


def _write_sample(path: Path, *, right: tuple[int, int, int] = (0, 0, 255)) -> Path:
    image = Image.new("RGB", (8, 4))
    pixels = image.load()
    for x in range(8):
        for y in range(4):
            pixels[x, y] = (255, 0, 0) if x < 4 else right
    image.save(path)
    image.close()
    return path


class StubVisionRouter:
    provider_names = ("fake-vision",)
    providers: tuple[Any, ...] = ()

    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest):
        self.calls += 1
        self.requests.append(request)
        assert request.has_images
        return SimpleNamespace(
            response=CompletionResponse(
                content="A red and blue test image.",
                provider="fake-vision",
                model="fake-vision-1",
            ),
            attempts=(object(),),
        )


class FakeTransport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.requests: list[HttpJsonRequest] = []

    def __call__(self, request: HttpJsonRequest) -> dict[str, Any]:
        self.requests.append(request)
        return self.response


def _image_request() -> CompletionRequest:
    return CompletionRequest.from_prompt(
        "Describe it",
        images=(ImageInput(data=b"tiny-image", mime_type="image/png"),),
    )


def _setup(
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


def _run(tasks, orchestrator, instruction: str, **kwargs):
    task = tasks.create(Task(instruction=instruction))
    return task, orchestrator.execute(task.id, **kwargs)


def _approve(_request) -> bool:
    return True


def _deny(_request) -> bool:
    return False


# ---------------------------------------------------------------------- tests


def test_image_core_inspect_convert_transform_compare(tmp_path: Path) -> None:
    source = _write_sample(tmp_path / "sample.png")

    info = inspect_image(source)
    assert (info.format, info.width, info.height, info.mode) == ("PNG", 8, 4, "RGB")
    assert info.average_color == "#800080"
    assert set(info.dominant_colors[:2]) == {"#f01010", "#1010f0"}
    assert len(info.sha256) == 64

    converted = convert_image(source, tmp_path / "sample.jpg", quality=92)
    assert converted.format == "JPEG"
    assert (converted.width, converted.height) == (8, 4)

    transformed, operations = transform_image(
        source,
        tmp_path / "rotated.png",
        rotate_degrees=90,
        grayscale=True,
    )
    assert operations == ("rotate", "grayscale")
    assert (transformed.width, transformed.height, transformed.mode) == (4, 8, "L")

    identical = compare_images(source, source)
    assert identical.byte_identical is True
    assert identical.pixel_identical is True
    assert identical.similarity_percent == 100.0

    different_path = _write_sample(
        tmp_path / "different.png", right=(0, 255, 0)
    )
    different = compare_images(source, different_path)
    assert different.byte_identical is False
    assert different.pixel_identical is False
    assert different.similarity_percent < 100.0

    with pytest.raises(ToolError, match="never overwrites"):
        convert_image(source, tmp_path / "sample.jpg")


def test_image_core_rejects_corrupt_and_prepares_bmp_for_vision(
    tmp_path: Path,
) -> None:
    corrupt = tmp_path / "bad.png"
    corrupt.write_text("not a png", encoding="utf-8")
    with pytest.raises(ToolError, match="Could not decode image"):
        inspect_image(corrupt)

    bmp = tmp_path / "sample.bmp"
    Image.new("RGB", (12, 6), (20, 40, 60)).save(bmp)
    payload = prepare_vision_image(bmp, max_bytes=100_000)
    assert payload.mime_type == "image/jpeg"
    assert payload.converted_or_resized is True
    assert payload.data.startswith(b"\xff\xd8")

    png = _write_sample(tmp_path / "sample.png")
    direct = prepare_vision_image(png, max_bytes=100_000)
    assert direct.mime_type == "image/png"
    assert direct.converted_or_resized is False
    assert direct.data == png.read_bytes()


def test_config_vision_gate_and_limit(tmp_path: Path) -> None:
    default = Settings.from_env(tmp_path, environ={})
    assert default.allow_vision is False
    assert default.vision_max_image_bytes == 10_000_000

    enabled = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_ALLOW_VISION": "yes",
            "PC_AGENT_VISION_MAX_IMAGE_BYTES": "123456",
        },
    )
    assert enabled.allow_vision is True
    assert enabled.vision_max_image_bytes == 123456

    with pytest.raises(ConfigError, match="ALLOW_VISION"):
        Settings.from_env(tmp_path, environ={"PC_AGENT_ALLOW_VISION": "maybe"})
    with pytest.raises(ConfigError, match="VISION_MAX_IMAGE_BYTES"):
        Settings.from_env(
            tmp_path, environ={"PC_AGENT_VISION_MAX_IMAGE_BYTES": "99999999"}
        )


def test_provider_image_payload_formats() -> None:
    request = _image_request()
    encoded = base64.b64encode(b"tiny-image").decode("ascii")

    gemini_transport = FakeTransport(
        {"status": "completed", "output_text": "Gemini saw it", "model": "g-test"}
    )
    GeminiProvider(
        api_key="secret",
        model="g-test",
        base_url="https://example.test/v1beta",
        timeout_seconds=5,
        transport=gemini_transport,
    ).complete(request)
    gemini_input = gemini_transport.requests[0].payload["input"]
    assert gemini_input[0] == {"type": "text", "text": "Describe it"}
    assert gemini_input[1] == {
        "type": "image",
        "data": encoded,
        "mime_type": "image/png",
    }

    openai_transport = FakeTransport(
        {
            "model": "oa-test",
            "choices": [{"message": {"content": "OpenAI-compatible saw it"}}],
        }
    )
    OpenAICompatibleProvider(
        name="openrouter",
        api_key="secret",
        model="oa-test",
        base_url="https://example.test/v1",
        timeout_seconds=5,
        transport=openai_transport,
    ).complete(request)
    content = openai_transport.requests[0].payload["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Describe it"}
    assert content[1]["image_url"]["url"] == f"data:image/png;base64,{encoded}"

    ollama_transport = FakeTransport(
        {"model": "vision-test", "message": {"content": "Ollama saw it"}}
    )
    OllamaProvider(
        api_key=None,
        model="vision-test",
        base_url="http://localhost:11434",
        timeout_seconds=5,
        transport=ollama_transport,
    ).complete(request)
    assert ollama_transport.requests[0].payload["messages"][0]["images"] == [encoded]

    with pytest.raises(ProviderError, match="offline mock cannot inspect images"):
        MockProvider().complete(request)

    class VisionProvider:
        name = "fake"
        model = "fake-vision"
        implemented = True
        supports_vision = True

        @property
        def configured(self) -> bool:
            return True

        def complete(self, _request: CompletionRequest) -> CompletionResponse:
            return CompletionResponse("seen", self.name, self.model)

    routed = ModelRouter(
        [MockProvider(), VisionProvider()], max_retries=0, sleeper=lambda _: None
    ).complete(request)
    assert routed.response.provider == "fake"


def test_direct_vision_tool_gate_and_stub(tmp_path: Path) -> None:
    _write_sample(tmp_path / "sample.png")
    registry = build_default_registry()

    disabled = ToolContext.for_workspace(tmp_path)
    result = registry.invoke(
        "image.analyze",
        {"path": "sample.png", "question": "What colors are visible?"},
        disabled,
    )
    assert not result.success
    assert "disabled by policy" in (result.error or "")

    router = StubVisionRouter()
    enabled = ToolContext.for_workspace(
        tmp_path, vision_allowed=True, vision_router_factory=lambda: router
    )
    result = registry.invoke(
        "image.analyze",
        {"path": "sample.png", "question": "What colors are visible?"},
        enabled,
    )
    assert result.success, result.error
    assert result.output is not None
    assert result.output["answer"] == "A red and blue test image."
    assert result.output["provider"] == "fake-vision"
    assert router.calls == 1
    assert len(router.requests[0].images) == 1


def test_orchestrated_local_image_flow(tmp_path: Path) -> None:
    settings, tasks, _execution, orchestrator = _setup(tmp_path)
    _write_sample(settings.workspace_dir / "sample.png")
    _write_sample(settings.workspace_dir / "other.png", right=(0, 255, 0))

    _task, inspected = _run(tasks, orchestrator, "Inspect image sample.png")
    assert inspected.success, inspected.error
    assert inspected.verification_valid is True

    _task, converted = _run(tasks, orchestrator, "Convert sample.png to jpg")
    assert converted.success, converted.error
    assert (settings.workspace_dir / "sample-converted.jpg").is_file()
    assert converted.verification_valid is True

    _task, transformed = _run(
        tasks,
        orchestrator,
        "Resize image",
        options=PlannerOptions(
            target_path="sample.png",
            output_path="thumb.png",
            image_width=4,
            image_grayscale=True,
        ),
    )
    assert transformed.success, transformed.error
    thumb = inspect_image(settings.workspace_dir / "thumb.png")
    assert (thumb.width, thumb.height, thumb.mode) == (4, 2, "L")

    _task, compared = _run(
        tasks, orchestrator, "Compare sample.png and other.png"
    )
    assert compared.success, compared.error
    assert compared.tool_result is not None
    assert compared.tool_result.output is not None
    assert compared.tool_result.output["pixel_identical"] is False


def test_orchestrated_vision_approval_and_denial(tmp_path: Path) -> None:
    router = StubVisionRouter()
    settings, tasks, execution, orchestrator = _setup(tmp_path, router=router)
    _write_sample(settings.workspace_dir / "sample.png")
    options = PlannerOptions(
        target_path="sample.png", image_question="What colors are visible?"
    )

    denied_task, denied = _run(
        tasks,
        orchestrator,
        "Analyze image",
        options=options,
        approval_callback=_deny,
    )
    assert denied.task.status is TaskStatus.CANCELLED
    assert router.calls == 0
    approvals = execution.list_approvals(denied_task.id)
    assert approvals and approvals[0].action == "vision_analysis"
    assert approvals[0].scope["tool_id"] == "image.analyze"

    _task, report = _run(
        tasks,
        orchestrator,
        "Analyze image",
        options=options,
        approval_callback=_approve,
    )
    assert report.success, report.error
    assert report.verification_valid is True
    assert router.calls == 1
    assert report.tool_result is not None
    assert report.tool_result.output is not None
    assert report.tool_result.output["answer"] == "A red and blue test image."


def test_orchestrated_vision_disabled_is_not_retried(tmp_path: Path) -> None:
    router = StubVisionRouter()
    settings, tasks, _execution, orchestrator = _setup(
        tmp_path, environ={}, router=router
    )
    _write_sample(settings.workspace_dir / "sample.png")

    _task, report = _run(
        tasks,
        orchestrator,
        "Analyze image",
        options=PlannerOptions(
            target_path="sample.png", image_question="Describe it"
        ),
        approval_callback=_approve,
    )
    assert report.task.status is TaskStatus.FAILED
    assert "disabled by policy" in (report.error or "")
    assert len(report.recovery_attempts) == 1
    assert router.calls == 0


class _PlanRouter:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def complete(self, _request):
        return SimpleNamespace(
            response=SimpleNamespace(
                content=json.dumps(self._payload), provider="stub", model="test"
            ),
            attempts=(),
        )


def test_ai_may_inspect_or_compare_images_but_not_send_or_write() -> None:
    planned = plan_with_provider(
        _PlanRouter(
            {
                "intent": "image.inspect",
                "tool_id": "image.inspect",
                "arguments": {"path": "sample.png", "include_color_stats": True},
                "summary": "inspect image",
            }
        ),
        "Inspect sample.png",
        PlannerOptions(),
    )
    assert planned.plan is not None
    assert planned.plan.tool_id == "image.inspect"

    for tool_id, arguments in (
        ("image.analyze", {"path": "sample.png", "question": "Describe it"}),
        ("image.convert", {"source": "sample.png", "output": "sample.jpg"}),
        (
            "image.transform",
            {"source": "sample.png", "output": "small.png", "width": 10},
        ),
    ):
        with pytest.raises(PlanningError, match="not allow-listed"):
            plan_with_provider(
                _PlanRouter(
                    {
                        "intent": tool_id,
                        "tool_id": tool_id,
                        "arguments": arguments,
                        "summary": "unsafe image plan",
                    }
                ),
                "Use the image",
                PlannerOptions(),
            )


def test_cli_image_commands_and_vision_confirmation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    _write_sample(settings.workspace_dir / "sample.png")
    _write_sample(settings.workspace_dir / "other.png", right=(0, 255, 0))
    root = ["--root", str(tmp_path)]

    assert main([*root, "doctor"]) == 0
    doctor = capsys.readouterr().out
    assert "PHASE 16.1 Credential Manager Hotfix" in doctor
    assert "built-ins=54" in doctor
    assert "Vision: disabled" in doctor

    assert main([*root, "image", "inspect", "sample.png"]) == 0
    assert "Dimensions: 8x4" in capsys.readouterr().out

    assert main([*root, "image", "convert", "sample.png", "converted.jpg"]) == 0
    assert "converted.jpg" in capsys.readouterr().out
    assert (settings.workspace_dir / "converted.jpg").is_file()

    assert (
        main(
            [
                *root,
                "image",
                "transform",
                "sample.png",
                "small.png",
                "--width",
                "4",
                "--grayscale",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert inspect_image(settings.workspace_dir / "small.png").mode == "L"

    assert main([*root, "image", "compare", "sample.png", "other.png"]) == 0
    assert "Similarity:" in capsys.readouterr().out

    monkeypatch.setenv("PC_AGENT_ALLOW_VISION", "1")
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    assert (
        main(
            [
                *root,
                "image",
                "analyze",
                "sample.png",
                "--question",
                "What colors are visible?",
            ]
        )
        == 4
    )
    denied = capsys.readouterr().out
    assert "pc-agent image analyze" in denied and "--yes" in denied

    router = StubVisionRouter()
    monkeypatch.setattr(
        "pc_agent.cli.build_model_router", lambda _settings: router
    )
    assert (
        main(
            [
                *root,
                "image",
                "analyze",
                "sample.png",
                "--question",
                "What colors are visible?",
                "--yes",
            ]
        )
        == 0
    )
    assert "A red and blue test image." in capsys.readouterr().out
    assert router.calls == 1


def test_gui_controller_images_and_doctor(tmp_path: Path) -> None:
    settings = Settings.from_env(
        tmp_path, environ={"PC_AGENT_ALLOW_VISION": "1"}
    )
    controller = GuiController(settings, desktop_opener=lambda _target: None)
    _write_sample(settings.workspace_dir / "sample.png")

    summary = controller.doctor_summary()
    assert "PHASE 16.1 Credential Manager Hotfix" in summary.to_text()
    assert summary.builtin_tools == 54
    assert "Vision: enabled" in summary.to_text()

    router = StubVisionRouter()
    controller._router = router  # test seam: same object used by _context()

    inspected = controller.image_inspect("sample.png")
    assert inspected.success, inspected.error
    converted = controller.image_convert("sample.png", "gui.jpg")
    assert converted.success, converted.error
    transformed = controller.image_transform(
        "sample.png", "gui-small.png", max_dimension=4, grayscale=True
    )
    assert transformed.success, transformed.error
    compared = controller.image_compare("sample.png", "sample.png")
    assert compared.success, compared.error
    assert compared.output is not None
    assert compared.output["pixel_identical"] is True
    analyzed = controller.image_analyze(
        "sample.png", "What colors are visible?"
    )
    assert analyzed.success, analyzed.error
    assert analyzed.output is not None
    assert analyzed.output["provider"] == "fake-vision"
    assert router.calls == 1


def test_image_input_validation_and_png_bytes() -> None:
    buffer = BytesIO()
    Image.new("RGB", (1, 1), (255, 255, 255)).save(buffer, format="PNG")
    image = ImageInput(data=buffer.getvalue(), mime_type="IMAGE/PNG")
    assert image.mime_type == "image/png"
    assert image.data_url.startswith("data:image/png;base64,")

    with pytest.raises(ValueError, match="MIME type"):
        ImageInput(data=b"x", mime_type="image/bmp")
