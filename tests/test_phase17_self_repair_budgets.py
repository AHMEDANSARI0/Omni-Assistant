"""Phase 17: deterministic core self-repair and per-task budgets."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from pc_agent.budgets import (
    BudgetedModelRouter,
    BudgetExceededError,
    TaskBudgetLimits,
    TaskBudgetTracker,
    estimate_request_tokens,
    reported_usage_tokens,
)
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.documents.pdf import create_demo_pdfs
from pc_agent.domain.task import Task, TaskStatus
from pc_agent.errors import ConfigError, PcAgentError
from pc_agent.gui.controller import (
    GuiController,
    TaskRunOptions,
    format_self_repair_diagnosis,
)
from pc_agent.orchestrator import TaskOrchestrator
from pc_agent.planning import DeterministicPlanner, PlannerOptions, TaskPlan
from pc_agent.providers.base import (
    CompletionRequest,
    CompletionResponse,
    ImageInput,
)
from pc_agent.providers.router import ModelRouter
from pc_agent.recovery import FailureCategory, diagnose_tool_failure
from pc_agent.self_repair import SelfRepairService
from pc_agent.self_repair_cli import main as self_repair_bootstrap_main
from pc_agent.storage.db import Database
from pc_agent.storage.repositories import (
    ExecutionRepository,
    MemoryRepository,
    RecoveryRepository,
    TaskRepository,
)
from pc_agent.tools import build_default_registry

_CHAIN = "Merge PDFs from Folder A and Folder B then extract pages 1-2 from merged.pdf"
_AI_PLAN = {
    "intent": "pdf.merge_folders",
    "tool_id": "pdf.merge_folders",
    "arguments": {
        "input_folders": ["Folder A", "Folder B"],
        "output_path": "ai-merged.pdf",
        "recursive": False,
    },
    "expected_pages": None,
    "summary": "Merge demo folders",
    "missing_information": [],
}


@pytest.fixture(autouse=True)
def clean_phase17_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PC_AGENT_HOME",
        "PC_AGENT_ALLOW_SELF_REPAIR",
        "PC_AGENT_TASK_TOKEN_BUDGET",
        "PC_AGENT_TASK_PROVIDER_CALL_BUDGET",
        "PC_AGENT_TASK_TOOL_BUDGET",
        "PC_AGENT_TASK_STEP_BUDGET",
        "PC_AGENT_TASK_TIME_BUDGET_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def _orchestrator_setup(
    tmp_path: Path,
    *,
    environ: dict[str, str] | None = None,
    router: Any | None = None,
):
    settings = Settings.from_env(tmp_path, environ={} if environ is None else environ)
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


def _run(tasks: TaskRepository, orchestrator: TaskOrchestrator, instruction: str, **kwargs):
    task = tasks.create(Task(instruction=instruction))
    return task, orchestrator.execute(task.id, **kwargs)


class UsageProvider:
    implemented = True
    supports_vision = False
    name = "fake-ai"
    model = "fake-model"

    def __init__(self, *, usage: dict[str, Any] | None = None) -> None:
        self.usage = usage or {}
        self.calls = 0

    @property
    def configured(self) -> bool:
        return True

    def complete(self, _request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        return CompletionResponse(
            json.dumps(_AI_PLAN), self.name, self.model, self.usage
        )


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


def _write_sample(path: Path) -> None:
    image = Image.new("RGB", (8, 4), (255, 0, 0))
    image.save(path)
    image.close()


# ----------------------------------------------------------------------
# Budget accounting
# ----------------------------------------------------------------------


def test_reported_usage_token_shapes_and_image_estimate() -> None:
    assert reported_usage_tokens({"total_tokens": 19, "prompt_tokens": 100}) == 19
    assert (
        reported_usage_tokens(
            {"promptTokenCount": 11, "candidatesTokenCount": 7}
        )
        == 18
    )
    assert reported_usage_tokens({"prompt_eval_count": 3, "eval_count": 4}) == 7
    assert reported_usage_tokens({"unknown": 9}) is None

    request = CompletionRequest.from_prompt(
        "abcd",
        images=(ImageInput(data=b"tiny", mime_type="image/png"),),
    )
    assert estimate_request_tokens(request) == 1025


class _DelegateRouter:
    def __init__(self, usage: dict[str, Any]) -> None:
        self.usage = usage
        self.calls = 0

    def complete(self, _request: CompletionRequest):
        self.calls += 1
        return SimpleNamespace(
            response=CompletionResponse(
                "answer", "fake", "fake-1", dict(self.usage)
            ),
            attempts=(object(), object()),
        )


def test_budgeted_router_counts_attempts_and_blocks_next_call() -> None:
    delegate = _DelegateRouter({"promptTokenCount": 20, "candidatesTokenCount": 5})
    tracker = TaskBudgetTracker(
        TaskBudgetLimits(token_budget=1000, provider_call_budget=1)
    )
    router = BudgetedModelRouter(delegate, tracker)
    request = CompletionRequest.from_prompt("abcd", max_output_tokens=10)

    router.complete(request)
    snapshot = tracker.snapshot()
    assert snapshot.provider_calls == 1
    assert snapshot.provider_attempts == 2
    assert snapshot.tokens_reported == 25
    # One failed/fallback attempt's input is conservatively estimated.
    assert snapshot.tokens_estimated == 1
    assert snapshot.tokens_used == 26

    with pytest.raises(BudgetExceededError, match="provider-call budget"):
        router.complete(request)
    assert delegate.calls == 1


def test_budget_tracker_reserve_reported_overage_tools_steps_and_time() -> None:
    tracker = TaskBudgetTracker(TaskBudgetLimits(token_budget=10))
    request = CompletionRequest.from_prompt("abcd", max_output_tokens=20)
    with pytest.raises(BudgetExceededError, match="estimated reserve"):
        tracker.before_provider_call(request)
    assert tracker.provider_calls == 0

    over = _DelegateRouter({"total_tokens": 31})
    over_tracker = TaskBudgetTracker(TaskBudgetLimits(token_budget=30))
    with pytest.raises(BudgetExceededError, match="above the task token budget"):
        BudgetedModelRouter(over, over_tracker).complete(
            CompletionRequest.from_prompt("abcd", max_output_tokens=10)
        )
    assert over_tracker.tokens_used == 32  # reported 31 + one fallback estimate

    limited = TaskBudgetTracker(
        TaskBudgetLimits(tool_budget=1, step_budget=1, time_budget_seconds=0)
    )
    limited.record_tool()
    with pytest.raises(BudgetExceededError, match="tool budget"):
        limited.record_tool()
    with pytest.raises(BudgetExceededError, match="step budget"):
        limited.check_steps(2)

    now = [100.0]
    timed = TaskBudgetTracker(
        TaskBudgetLimits(time_budget_seconds=5), clock=lambda: now[0]
    )
    now[0] = 106.0
    with pytest.raises(BudgetExceededError, match="time budget"):
        timed.check_time()


def test_config_task_budgets_and_self_repair_gate(tmp_path: Path) -> None:
    defaults = Settings.from_env(tmp_path, environ={})
    assert defaults.allow_self_repair is False
    assert defaults.task_token_budget == 20_000
    assert defaults.task_provider_call_budget == 10
    assert defaults.task_tool_budget == 25
    assert defaults.task_step_budget == 5
    assert defaults.task_time_budget_seconds == 600.0

    custom = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_ALLOW_SELF_REPAIR": "yes",
            "PC_AGENT_TASK_TOKEN_BUDGET": "0",
            "PC_AGENT_TASK_PROVIDER_CALL_BUDGET": "3",
            "PC_AGENT_TASK_TOOL_BUDGET": "4",
            "PC_AGENT_TASK_STEP_BUDGET": "2",
            "PC_AGENT_TASK_TIME_BUDGET_SECONDS": "12.5",
        },
    )
    assert custom.allow_self_repair is True
    assert custom.task_token_budget == 0
    assert custom.public_summary()["task_provider_call_budget"] == "3"

    for environ in (
        {"PC_AGENT_ALLOW_SELF_REPAIR": "maybe"},
        {"PC_AGENT_TASK_TOKEN_BUDGET": "-1"},
        {"PC_AGENT_TASK_STEP_BUDGET": "0"},
        {"PC_AGENT_TASK_TIME_BUDGET_SECONDS": "999999"},
        {"PC_AGENT_TASK_TIME_BUDGET_SECONDS": "nan"},
        {"PC_AGENT_TASK_TIME_BUDGET_SECONDS": "inf"},
    ):
        with pytest.raises(ConfigError):
            Settings.from_env(tmp_path, environ=environ)


def test_task_budget_limits_reject_nonfinite_time() -> None:
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="time_budget_seconds"):
            TaskBudgetLimits(time_budget_seconds=value)


def test_budget_errors_are_policy_non_retryable(tmp_path: Path) -> None:
    plan = TaskPlan(
        intent="pdf.verify",
        tool_id="pdf.verify",
        arguments={"path": "report.pdf"},
        summary="Verify report.",
        planner="test",
    )
    diagnosis = diagnose_tool_failure(
        plan,
        "Task budget exceeded: tool invocation would exceed the task tool budget.",
        workspace_dir=tmp_path,
    )
    assert diagnosis.category is FailureCategory.POLICY
    assert diagnosis.retryable is False


# ----------------------------------------------------------------------
# Orchestrator budgets
# ----------------------------------------------------------------------


def test_deterministic_task_reports_budget_without_provider_call(tmp_path: Path) -> None:
    settings, tasks, _execution, orchestrator = _orchestrator_setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)

    task, report = _run(
        tasks, orchestrator, "Merge PDFs from Folder A and Folder B"
    )

    assert report.success, report.error
    assert report.budget is not None
    assert report.budget.provider_calls == 0
    assert report.budget.tokens_used == 0
    assert report.budget.tool_invocations == 1
    assert report.budget.steps_planned == 1
    events = [event.event_type for event in tasks.list_events(task.id)]
    assert events.count("budget") == 1


def test_ai_task_uses_reported_provider_usage(tmp_path: Path) -> None:
    provider = UsageProvider(usage={"prompt_tokens": 11, "completion_tokens": 7})
    router = ModelRouter([provider], backoff_seconds=0)
    settings, tasks, _execution, orchestrator = _orchestrator_setup(
        tmp_path, router=router
    )
    create_demo_pdfs(settings.workspace_dir)

    _task, report = _run(
        tasks,
        orchestrator,
        "Please prepare the folder PDF output",
        use_ai=True,
    )

    assert report.success, report.error
    assert provider.calls == 1
    assert report.budget is not None
    assert report.budget.provider_calls == 1
    assert report.budget.provider_attempts == 1
    assert report.budget.tokens_reported == 18
    assert report.budget.tokens_used == 18
    assert report.budget.tool_invocations == 1


def test_token_reserve_blocks_provider_before_any_request(tmp_path: Path) -> None:
    provider = UsageProvider()
    router = ModelRouter([provider], backoff_seconds=0)
    settings, tasks, _execution, orchestrator = _orchestrator_setup(
        tmp_path, router=router
    )
    create_demo_pdfs(settings.workspace_dir)

    _task, report = _run(
        tasks,
        orchestrator,
        "Please prepare the folder PDF output",
        options=PlannerOptions(token_budget=10),
        use_ai=True,
    )

    assert report.task.status is TaskStatus.FAILED
    assert provider.calls == 0
    assert report.budget is not None
    assert report.budget.exceeded == "tokens"
    assert report.budget.provider_calls == 0
    assert not (settings.workspace_dir / "ai-merged.pdf").exists()


def test_reported_usage_over_budget_discards_ai_plan(tmp_path: Path) -> None:
    provider = UsageProvider(usage={"total_tokens": 3000})
    router = ModelRouter([provider], backoff_seconds=0)
    settings, tasks, _execution, orchestrator = _orchestrator_setup(
        tmp_path, router=router
    )
    create_demo_pdfs(settings.workspace_dir)

    _task, report = _run(
        tasks,
        orchestrator,
        "Please prepare the folder PDF output",
        options=PlannerOptions(token_budget=2500),
        use_ai=True,
    )

    assert report.task.status is TaskStatus.FAILED
    assert provider.calls == 1
    assert report.budget is not None
    assert report.budget.tokens_used == 3000
    assert report.budget.exceeded == "tokens"
    assert not (settings.workspace_dir / "ai-merged.pdf").exists()


def test_step_budget_blocks_before_any_tool(tmp_path: Path) -> None:
    settings, tasks, execution, orchestrator = _orchestrator_setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)

    task, report = _run(
        tasks,
        orchestrator,
        _CHAIN,
        options=PlannerOptions(step_budget=1),
    )

    assert report.task.status is TaskStatus.FAILED
    assert "Task budget exceeded" in (report.error or "")
    assert report.budget is not None and report.budget.exceeded == "steps"
    assert execution.list_tool_invocations(task.id) == []
    assert not (settings.workspace_dir / "merged.pdf").exists()


def test_tool_budget_stops_second_multistep_tool(tmp_path: Path) -> None:
    settings, tasks, _execution, orchestrator = _orchestrator_setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)

    _task, report = _run(
        tasks,
        orchestrator,
        _CHAIN,
        options=PlannerOptions(tool_budget=1),
    )

    assert report.task.status is TaskStatus.FAILED
    assert "Task budget exceeded" in (report.error or "")
    assert report.budget is not None
    assert report.budget.tool_invocations == 1
    assert report.budget.exceeded == "tools"
    assert (settings.workspace_dir / "merged.pdf").is_file()
    assert not (settings.workspace_dir / "merged-extracted.pdf").exists()


def test_time_budget_blocks_first_tool(tmp_path: Path) -> None:
    settings, tasks, execution, orchestrator = _orchestrator_setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)

    task, report = _run(
        tasks,
        orchestrator,
        "Merge PDFs from Folder A and Folder B",
        options=PlannerOptions(task_time_budget_seconds=0.000001),
    )

    assert report.task.status is TaskStatus.FAILED
    assert report.budget is not None and report.budget.exceeded == "time"
    assert execution.list_tool_invocations(task.id) == []
    assert not (settings.workspace_dir / "merged.pdf").exists()


def test_vision_call_is_counted_in_task_budget(tmp_path: Path) -> None:
    router = StubVisionRouter()
    settings, tasks, _execution, orchestrator = _orchestrator_setup(
        tmp_path,
        environ={"PC_AGENT_ALLOW_VISION": "1"},
        router=router,
    )
    _write_sample(settings.workspace_dir / "sample.png")

    _task, report = _run(
        tasks,
        orchestrator,
        "Analyze image",
        options=PlannerOptions(
            target_path="sample.png", image_question="What colors are visible?"
        ),
        approval_callback=lambda _request: True,
    )

    assert report.success, report.error
    assert router.calls == 1
    assert report.budget is not None
    assert report.budget.provider_calls == 1
    assert report.budget.tool_invocations == 1
    assert report.budget.tokens_estimated >= 1024


def test_workflow_replay_uses_budget_options(tmp_path: Path) -> None:
    settings, tasks, _execution, orchestrator = _orchestrator_setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)
    _task, recorded = _run(tasks, orchestrator, _CHAIN)
    assert recorded.success, recorded.error
    assert recorded.workflow_key is not None

    _settings2, tasks2, execution2, limited = _orchestrator_setup(tmp_path)
    replay_task = tasks2.create(Task(instruction="Replay the recorded chain"))
    replay = limited.run_workflow(
        replay_task.id,
        recorded.workflow_key,
        budget_options=PlannerOptions(step_budget=1),
    )

    assert replay.task.status is TaskStatus.FAILED
    assert replay.budget is not None
    assert replay.budget.exceeded == "steps"
    assert replay.budget.steps_planned == 2
    assert execution2.list_tool_invocations(replay_task.id) == []


def test_cli_budget_flags_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    create_demo_pdfs(settings.workspace_dir)
    root = ["--root", str(tmp_path)]

    assert main([*root, "run", _CHAIN, "--step-budget", "1", "--json"]) == 1
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["status"] == "failed"
    assert blocked["budget"]["exceeded"] == "steps"
    assert blocked["budget"]["steps_planned"] == 2
    assert not (settings.workspace_dir / "merged.pdf").exists()

    assert (
        main(
            [
                *root,
                "run",
                "Merge PDFs from Folder A and Folder B",
                "--token-budget",
                "0",
                "--provider-call-budget",
                "0",
                "--tool-budget",
                "1",
                "--json",
            ]
        )
        == 0
    )
    succeeded = json.loads(capsys.readouterr().out)
    assert succeeded["budget"]["token_budget"] == 0
    assert succeeded["budget"]["tool_invocations"] == 1


# ----------------------------------------------------------------------
# Self-repair
# ----------------------------------------------------------------------


class RecordingRunner:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, ...]] = []
        self.cwd_paths: list[Path] = []
        self.environments: list[dict[str, str]] = []

    def __call__(self, argv, cwd, environment, _timeout):
        self.calls.append(tuple(argv))
        self.cwd_paths.append(Path(cwd))
        self.environments.append(dict(environment))
        return (1, "forced check failure", 0.01) if self.fail else (0, "ok", 0.01)


def _write_project(root: Path) -> None:
    package = root / "src" / "pc_agent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        '__version__ = "9.9.9"\nVALUE = 1\n', encoding="utf-8"
    )
    (package / "module.py").write_text('TOKEN = "good"\n', encoding="utf-8")
    (package / "other.py").write_text('OTHER = "good"\n', encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text(
        "def test_ok() -> None:\n    assert True\n", encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "phase17-test"\nversion = "9.9.9"\n',
        encoding="utf-8",
    )


def _service(root: Path, data: Path, runner: RecordingRunner | None = None):
    return SelfRepairService(root, data, command_runner=runner)


def test_baseline_refuses_unhealthy_tree_and_duplicate_without_force(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    data = tmp_path / "data"
    _write_project(root)
    service = _service(root, data)
    (root / "src" / "pc_agent" / "module.py").write_text(
        "def broken(:\n", encoding="utf-8"
    )

    with pytest.raises(PcAgentError, match="unhealthy source"):
        service.create_baseline(check_profile="static")
    assert service.baseline_status().exists is False

    (root / "src" / "pc_agent" / "module.py").write_text(
        'TOKEN = "good"\n', encoding="utf-8"
    )
    baseline = service.create_baseline(check_profile="static")
    assert baseline.exists and baseline.file_count == 5
    with pytest.raises(PcAgentError, match="already exists"):
        service.create_baseline(check_profile="static")
    assert service.create_baseline(force=True, check_profile="static").exists


def test_diagnose_stage_apply_and_rollback_are_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    data = tmp_path / "data"
    _write_project(root)
    (root / ".env").write_text("GEMINI_API_KEY=never-stage-this\n", encoding="utf-8")
    runner = RecordingRunner()
    service = _service(root, data, runner)
    service.create_baseline(check_profile="static")

    module = root / "src" / "pc_agent" / "module.py"
    module.write_text("def broken(:\n", encoding="utf-8")
    sample_test = root / "tests" / "test_sample.py"
    sample_test.unlink()
    extra = root / "src" / "pc_agent" / "new_module.py"
    extra.write_text('EXTRA = "keep me"\n', encoding="utf-8")
    cache_dir = root / "src" / "pc_agent" / "__pycache__"
    cache_dir.mkdir()
    cache = cache_dir / "module.cpython-311.pyc"
    cache.write_bytes(b"cache-bytes")

    diagnosis = service.diagnose()
    operations = {(item.kind, item.path) for item in diagnosis.operations}
    assert ("restore", "src/pc_agent/module.py") in operations
    assert ("restore", "tests/test_sample.py") in operations
    assert (
        "delete_cache",
        "src/pc_agent/__pycache__/module.cpython-311.pyc",
    ) in operations
    assert diagnosis.extra_files == ("src/pc_agent/new_module.py",)
    assert all(item.path != "src/pc_agent/new_module.py" for item in diagnosis.operations)
    assert any(issue.code == "python_compile" for issue in diagnosis.issues)

    monkeypatch.setenv("GEMINI_API_KEY", "must-not-reach-checks")
    staged = service.stage(check_profile="import")
    assert staged.status == "staged"
    staging = data / "self-repair" / "runs" / staged.run_id / "staging"
    assert runner.cwd_paths == [staging]
    assert "GEMINI_API_KEY" not in runner.environments[0]
    assert runner.environments[0]["PC_AGENT_CREDENTIAL_STORE"] == "env"
    assert not (staging / ".env").exists()
    assert (staging / "src" / "pc_agent" / "module.py").read_text(
        encoding="utf-8"
    ) == 'TOKEN = "good"\n'

    applied = service.apply(staged.run_id, post_check_profile="static")
    assert applied.status == "applied"
    assert module.read_text(encoding="utf-8") == 'TOKEN = "good"\n'
    assert sample_test.is_file()
    assert extra.read_text(encoding="utf-8") == 'EXTRA = "keep me"\n'
    assert not cache.exists()

    rolled = service.rollback(staged.run_id)
    assert rolled.status == "rolled_back"
    assert module.read_text(encoding="utf-8") == "def broken(:\n"
    assert not sample_test.exists()
    assert cache.read_bytes() == b"cache-bytes"
    assert extra.is_file()


def test_failed_staged_check_blocks_apply(tmp_path: Path) -> None:
    root = tmp_path / "project"
    data = tmp_path / "data"
    _write_project(root)
    service = _service(root, data, RecordingRunner(fail=True))
    service.create_baseline(check_profile="static")
    module = root / "src" / "pc_agent" / "module.py"
    module.write_text('TOKEN = "changed"\n', encoding="utf-8")

    staged = service.stage(check_profile="import")

    assert staged.status == "stage_failed"
    with pytest.raises(PcAgentError, match="only a staged run"):
        service.apply(staged.run_id, post_check_profile="static")
    assert module.read_text(encoding="utf-8") == 'TOKEN = "changed"\n'


def test_stale_plan_blocks_apply(tmp_path: Path) -> None:
    root = tmp_path / "project"
    data = tmp_path / "data"
    _write_project(root)
    service = _service(root, data)
    service.create_baseline(check_profile="static")
    module = root / "src" / "pc_agent" / "module.py"
    module.write_text('TOKEN = "changed"\n', encoding="utf-8")
    staged = service.stage(check_profile="static")
    (root / "src" / "pc_agent" / "other.py").write_text(
        'OTHER = "changed later"\n', encoding="utf-8"
    )

    with pytest.raises(PcAgentError, match="stale"):
        service.apply(staged.run_id, post_check_profile="static")

    assert module.read_text(encoding="utf-8") == 'TOKEN = "changed"\n'


def test_corrupt_or_version_mismatched_baseline_blocks_stage(tmp_path: Path) -> None:
    root = tmp_path / "project"
    data = tmp_path / "data"
    _write_project(root)
    service = _service(root, data)
    service.create_baseline(check_profile="static")
    snapshot = (
        data
        / "self-repair"
        / "baseline"
        / "files"
        / "src"
        / "pc_agent"
        / "module.py"
    )
    snapshot.write_text('TOKEN = "tampered snapshot"\n', encoding="utf-8")

    diagnosis = service.diagnose()
    assert diagnosis.blocked is True
    assert any(issue.code == "baseline_snapshot_corrupt" for issue in diagnosis.issues)
    with pytest.raises(PcAgentError, match="blocked"):
        service.stage(check_profile="static")

    root2 = tmp_path / "project2"
    data2 = tmp_path / "data2"
    _write_project(root2)
    service2 = _service(root2, data2)
    service2.create_baseline(check_profile="static")
    (root2 / "src" / "pc_agent" / "__init__.py").write_text(
        '__version__ = "10.0.0"\nVALUE = 1\n', encoding="utf-8"
    )
    (root2 / "pyproject.toml").write_text(
        '[project]\nname = "phase17-test"\nversion = "10.0.0"\n',
        encoding="utf-8",
    )
    diagnosis2 = service2.diagnose()
    assert diagnosis2.blocked is True
    assert any(
        issue.code == "baseline_version_mismatch" for issue in diagnosis2.issues
    )


def test_tampered_run_manifest_path_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    data = tmp_path / "data"
    _write_project(root)
    service = _service(root, data)
    service.create_baseline(check_profile="static")
    (root / "src" / "pc_agent" / "module.py").write_text(
        'TOKEN = "changed"\n', encoding="utf-8"
    )
    staged = service.stage(check_profile="static")
    manifest = data / "self-repair" / "runs" / staged.run_id / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["operations"][0]["path"] = "../outside.py"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PcAgentError, match="Unsafe self-repair path"):
        service.load_run(staged.run_id)


def test_cli_self_repair_gate_and_full_flow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    _write_project(root)
    cli_root = ["--root", str(root)]

    assert main([*cli_root, "self-repair", "diagnose"]) == 1
    assert "Self-repair is disabled" in capsys.readouterr().err

    (root / ".env").write_text("PC_AGENT_ALLOW_SELF_REPAIR=1\n", encoding="utf-8")
    assert (
        main(
            [
                *cli_root,
                "self-repair",
                "baseline",
                "--check",
                "static",
                "--yes",
            ]
        )
        == 0
    )
    assert "baseline captured" in capsys.readouterr().out

    module = root / "src" / "pc_agent" / "module.py"
    module.write_text("def broken(:\n", encoding="utf-8")
    assert main([*cli_root, "self-repair", "diagnose"]) == 0
    diagnosis = capsys.readouterr().out
    assert "python_compile" in diagnosis
    assert "restore src/pc_agent/module.py" in diagnosis

    assert (
        main([*cli_root, "self-repair", "stage", "--check", "static"]) == 0
    )
    staged = capsys.readouterr().out
    assert "Status: staged" in staged

    assert (
        main(
            [
                *cli_root,
                "self-repair",
                "apply",
                "latest",
                "--check",
                "static",
                "--yes",
            ]
        )
        == 0
    )
    assert "Status: applied" in capsys.readouterr().out
    assert module.read_text(encoding="utf-8") == 'TOKEN = "good"\n'

    assert main([*cli_root, "self-repair", "list"]) == 0
    assert "[applied]" in capsys.readouterr().out
    assert main([*cli_root, "self-repair", "show", "latest", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "applied"


def test_gui_budget_options_and_read_only_self_repair(tmp_path: Path) -> None:
    _write_project(tmp_path)
    disabled = GuiController(Settings.from_env(tmp_path, environ={}))
    with pytest.raises(PcAgentError, match="Self-repair is disabled"):
        disabled.self_repair_diagnosis()

    service = SelfRepairService(tmp_path, tmp_path / "data")
    service.create_baseline(check_profile="static")
    (tmp_path / "src" / "pc_agent" / "module.py").write_text(
        'TOKEN = "changed"\n', encoding="utf-8"
    )
    enabled = GuiController(
        Settings.from_env(
            tmp_path, environ={"PC_AGENT_ALLOW_SELF_REPAIR": "1"}
        )
    )
    summary = enabled.doctor_summary()
    assert "Task budgets: tokens=20000" in summary.to_text()
    assert "Self-repair: enabled; baseline=present" in summary.to_text()

    diagnosis = enabled.self_repair_diagnosis()
    assert "restore src/pc_agent/module.py" in format_self_repair_diagnosis(
        diagnosis
    )

    options = TaskRunOptions(
        token_budget=123,
        provider_call_budget=2,
        tool_budget=3,
        step_budget=4,
        task_time_budget_seconds=5.5,
    ).planner_options()
    assert options.token_budget == 123
    assert options.provider_call_budget == 2
    assert options.tool_budget == 3
    assert options.step_budget == 4
    assert options.task_time_budget_seconds == 5.5
    with pytest.raises(PcAgentError, match="Step budget"):
        TaskRunOptions(step_budget=0)


def test_invalid_baseline_and_tampered_staged_status_fail_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    data = tmp_path / "data"
    _write_project(root)
    service = _service(root, data)
    service.create_baseline(check_profile="static")
    manifest = data / "self-repair" / "baseline" / "manifest.json"
    manifest.write_text("{not-json", encoding="utf-8")

    status = service.baseline_status()
    assert status.valid is False
    assert "baseline=invalid" in status.summary()
    diagnosis = service.diagnose()
    assert diagnosis.blocked is True
    assert any(issue.code == "baseline_invalid" for issue in diagnosis.issues)
    with pytest.raises(PcAgentError, match="blocked"):
        service.stage(check_profile="static")

    service.create_baseline(force=True, check_profile="static")
    failing = _service(root, data, RecordingRunner(fail=True))
    (root / "src" / "pc_agent" / "module.py").write_text(
        'TOKEN = "changed"\n', encoding="utf-8"
    )
    staged = failing.stage(check_profile="import")
    assert staged.status == "stage_failed"
    run_manifest = data / "self-repair" / "runs" / staged.run_id / "manifest.json"
    payload = json.loads(run_manifest.read_text(encoding="utf-8"))
    payload["status"] = "staged"
    run_manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PcAgentError, match="staged checks"):
        failing.load_run(staged.run_id)


def test_self_repair_refuses_eligible_symlink(tmp_path: Path) -> None:
    root = tmp_path / "project"
    data = tmp_path / "data"
    _write_project(root)
    service = _service(root, data)
    service.create_baseline(check_profile="static")
    module = root / "src" / "pc_agent" / "module.py"
    module.unlink()
    try:
        module.symlink_to(root / "src" / "pc_agent" / "other.py")
    except OSError as exc:  # Windows may require Developer Mode/privilege.
        pytest.skip(f"Cannot create a symlink on this machine: {exc}")

    diagnosis = service.diagnose()

    assert diagnosis.blocked is True
    assert any(issue.code == "eligible_symlink" for issue in diagnosis.issues)
    assert all(
        operation.path != "src/pc_agent/module.py"
        for operation in diagnosis.operations
    )


def test_self_repair_bootstrap_cli_repairs_without_full_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    _write_project(root)
    root_arg = ["--root", str(root)]

    assert (
        self_repair_bootstrap_main([*root_arg, "diagnose"], environ={}) == 1
    )
    assert "Self-repair is disabled" in capsys.readouterr().err

    environ = {"PC_AGENT_ALLOW_SELF_REPAIR": "1"}
    assert (
        self_repair_bootstrap_main(
            [*root_arg, "baseline", "--check", "static", "--yes"],
            environ=environ,
        )
        == 0
    )
    capsys.readouterr()
    module = root / "src" / "pc_agent" / "module.py"
    module.write_text("def broken(:\n", encoding="utf-8")

    assert (
        self_repair_bootstrap_main(
            [*root_arg, "stage", "--check", "static"], environ=environ
        )
        == 0
    )
    assert "Status: staged" in capsys.readouterr().out
    assert (
        self_repair_bootstrap_main(
            [*root_arg, "apply", "latest", "--check", "static", "--yes"],
            environ=environ,
        )
        == 0
    )
    assert "Status: applied" in capsys.readouterr().out
    assert module.read_text(encoding="utf-8") == 'TOKEN = "good"\n'

    bootstrap_source = (
        Path(__file__).parents[1] / "src" / "pc_agent" / "self_repair_cli.py"
    ).read_text(encoding="utf-8")
    assert "from .orchestrator" not in bootstrap_source
    assert "from .budgets" not in bootstrap_source
    assert "from .config" not in bootstrap_source
    assert "from .cli" not in bootstrap_source
