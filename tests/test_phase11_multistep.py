"""Phase 11: multi-step plans (chained tools, per-step everything)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.documents.pdf import create_demo_pdfs
from pc_agent.domain.task import Task, TaskStatus
from pc_agent.gui.controller import GuiController, format_execution_report
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

_CHAIN = "Merge PDFs from Folder A and Folder B then extract pages 1-2 from merged.pdf"


def _setup(tmp_path: Path, *, planner=None, router=None):
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    tasks = TaskRepository(database)
    execution = ExecutionRepository(database)
    memory = MemoryRepository(database)
    registry = build_default_registry()
    orchestrator = TaskOrchestrator(
        settings=settings,
        task_repository=tasks,
        execution_repository=execution,
        tool_registry=registry,
        deterministic_planner=planner or DeterministicPlanner(settings.workspace_dir),
        model_router=router,
        recovery_repository=RecoveryRepository(database),
        memory_repository=memory,
        recovery_backoff_seconds=0,
    )
    return settings, tasks, execution, memory, registry, orchestrator


def _run(tasks, orchestrator, instruction: str, **kwargs):
    task = tasks.create(Task(instruction=instruction))
    return task, orchestrator.execute(task.id, **kwargs)


def test_multistep_merge_then_extract(tmp_path: Path) -> None:
    settings, tasks, execution, _m, _r, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)

    task, report = _run(tasks, orchestrator, _CHAIN)

    assert report.success, report.error
    assert [step.status for step in report.step_reports] == ["succeeded", "succeeded"]
    assert [step.tool_id for step in report.step_reports] == [
        "pdf.merge_folders",
        "pdf.extract_pages",
    ]
    assert report.steps_total == 2
    assert report.plan_summary.startswith("Multi-step plan (2 steps)")
    assert (settings.workspace_dir / "merged.pdf").is_file()
    assert (settings.workspace_dir / "merged-extracted.pdf").is_file()
    # One checkpoint per completed step, and the step event is audited.
    assert [cp.step_index for cp in execution.list_checkpoints(task.id)] == [0, 1]
    events = [event.event_type for event in tasks.list_events(task.id)]
    assert events.count("step_completed") == 2
    # Attempt numbers are unique across the whole pipeline.
    numbers = [attempt.attempt_number for attempt in report.recovery_attempts]
    assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers)


def test_multistep_implicit_previous_output(tmp_path: Path) -> None:
    settings, tasks, _e, _m, _r, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)

    _task, report = _run(
        tasks,
        orchestrator,
        "Merge PDFs from Folder A and Folder B then extract pages 1-2",
    )

    assert report.success, report.error
    assert (settings.workspace_dir / "merged-extracted.pdf").is_file()


def test_multistep_csv_xlsx_formula_chain(tmp_path: Path) -> None:
    settings, tasks, _e, _m, _r, orchestrator = _setup(tmp_path)
    (settings.workspace_dir / "sales.csv").write_text(
        "item,qty,price\na,2,10\nb,3,20\nc,5,30\n", encoding="utf-8"
    )

    _task, report = _run(
        tasks,
        orchestrator,
        'Convert sales.csv to xlsx then set cell D5 to "=SUM(C2:C4)" '
        "then verify formulas",
    )

    assert report.success, report.error
    assert report.steps_total == 3
    assert [step.status for step in report.step_reports] == ["succeeded"] * 3
    final = report.step_reports[-1].tool_result
    assert final is not None and final.output is not None
    formulas = final.output["formulas"]
    assert formulas[0]["reference"] == "D5"
    assert formulas[0]["computed"] == 60.0


def test_binding_follows_recovered_output_name(tmp_path: Path) -> None:
    settings, tasks, execution, _m, _r, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)
    # First a plain merge so merged.pdf already occupies the planned name.
    _t, first = _run(tasks, orchestrator, "Merge PDFs from Folder A and Folder B")
    assert first.success, first.error

    task, report = _run(
        tasks,
        orchestrator,
        _CHAIN,
        options=PlannerOptions(output_path="merged.pdf"),
    )

    assert report.success, report.error
    assert report.recovered  # step 1 healed to a fresh output
    extract_calls = [
        call
        for call in execution.list_tool_invocations(task.id)
        if call.tool_id == "pdf.extract_pages"
    ]
    assert extract_calls
    assert extract_calls[0].arguments["path"] == "merged-recovered.pdf"


def test_step_two_failure_stops_pipeline(tmp_path: Path) -> None:
    settings, tasks, execution, _m, _r, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)

    task, report = _run(
        tasks,
        orchestrator,
        "Merge PDFs from Folder A and Folder B then extract pages 50-60 "
        "from merged.pdf",
    )

    assert not report.success
    assert report.task.status is TaskStatus.FAILED
    assert [step.status for step in report.step_reports] == ["succeeded", "failed"]
    assert "Step 2 of 2" in (report.error or "")
    assert [cp.step_index for cp in execution.list_checkpoints(task.id)] == [0]
    assert (settings.workspace_dir / "merged.pdf").is_file()
    assert not (settings.workspace_dir / "merged-extracted.pdf").exists()


def test_unsupported_second_clause_needs_input_without_tools(tmp_path: Path) -> None:
    settings, tasks, execution, _m, _r, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)

    task, report = _run(
        tasks,
        orchestrator,
        "Merge PDFs from Folder A and Folder B then fly to the moon",
    )

    assert report.task.status is TaskStatus.NEEDS_INPUT
    assert any("Step 2" in item for item in report.missing_information)
    assert execution.list_tool_invocations(task.id) == []


def test_too_many_steps_rejected(tmp_path: Path) -> None:
    _settings, tasks, _e, _m, _r, orchestrator = _setup(tmp_path)

    _task, report = _run(tasks, orchestrator, " then ".join(["List files"] * 6))

    assert report.task.status is TaskStatus.NEEDS_INPUT
    assert any("at most 5" in item for item in report.missing_information)


def test_multistep_workflow_recorded_and_replayed(tmp_path: Path) -> None:
    settings, tasks, _e, memory, _r, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)
    _task, report = _run(tasks, orchestrator, _CHAIN)
    assert report.success, report.error
    assert report.workflow_key is not None
    record = memory.get("workflow", report.workflow_key)
    assert record is not None
    assert record.content["intent"] == "multi_step"
    assert len(record.content["steps"]) == 2
    assert record.content["bindings"][1][0]["argument"] == "path"

    repeat = tasks.create(Task(instruction=_CHAIN))
    replayed = orchestrator.run_workflow(repeat.id, "latest")

    assert replayed.success, replayed.error
    assert replayed.workflow_reused
    assert len(replayed.step_reports) == 2
    assert replayed.workflow_success_count == 2


class _NullPlanner:
    def plan(self, *_args, **_kwargs):  # planner "misses" everything
        return None


def test_planner_fallback_reuses_multistep_workflow(tmp_path: Path) -> None:
    settings, tasks, execution, memory, registry, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)
    _task, report = _run(tasks, orchestrator, _CHAIN)
    assert report.success, report.error

    blind = TaskOrchestrator(
        settings=settings,
        task_repository=tasks,
        execution_repository=execution,
        tool_registry=registry,
        deterministic_planner=_NullPlanner(),  # type: ignore[arg-type]
        recovery_repository=RecoveryRepository(
            Database(settings.database_path)
        ),
        memory_repository=memory,
        recovery_backoff_seconds=0,
    )
    repeat = tasks.create(Task(instruction=_CHAIN))
    reused = blind.execute(repeat.id)

    assert reused.success, reused.error
    assert reused.workflow_reused
    assert len(reused.step_reports) == 2


def test_resume_skips_checkpointed_step(tmp_path: Path) -> None:
    settings, tasks, execution, _m, registry, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)
    task = tasks.create(Task(instruction=_CHAIN))

    original_invoke = registry.invoke

    def flaky(tool_id, arguments, context):
        if tool_id == "pdf.extract_pages":
            registry.invoke = original_invoke
            raise RuntimeError("simulated crash")
        return original_invoke(tool_id, arguments, context)

    registry.invoke = flaky  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="simulated crash"):
        orchestrator.execute(task.id)
    interrupted = tasks.get(task.id)
    assert interrupted is not None and not interrupted.is_terminal

    report = orchestrator.resume(task.id, force=True)

    assert report.success, report.error
    assert report.resumed_from_checkpoint
    invocations = execution.list_tool_invocations(task.id)
    merges = [c for c in invocations if c.tool_id == "pdf.merge_folders"]
    extracts = [c for c in invocations if c.tool_id == "pdf.extract_pages"]
    assert len(merges) == 1  # checkpointed step 1 was not re-run
    assert len(extracts) == 1


def test_first_step_approval_denied_cancels_pipeline(tmp_path: Path) -> None:
    settings, tasks, execution, _m, _r, orchestrator = _setup(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    create_demo_pdfs(external)
    options = PlannerOptions(
        input_folders=(str(external / "Folder A"), str(external / "Folder B"))
    )
    instruction = "Merge PDFs then extract pages 1-2"

    denied_task, denied = _run(
        tasks,
        orchestrator,
        instruction,
        options=options,
        approval_callback=lambda _request: False,
    )
    assert denied.task.status is TaskStatus.CANCELLED
    assert denied.step_reports[0].status == "cancelled"
    assert not (settings.workspace_dir / "merged.pdf").exists()
    approvals = execution.list_approvals(denied_task.id)
    assert approvals and approvals[0].scope["tool_id"] == "pdf.merge_folders"

    _t2, approved = _run(
        tasks,
        orchestrator,
        instruction,
        options=options,
        approval_callback=lambda _request: True,
    )
    assert approved.success, approved.error
    assert [step.status for step in approved.step_reports] == ["succeeded"] * 2


class _StubRouter:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def complete(self, _request):
        return SimpleNamespace(
            response=SimpleNamespace(
                content=json.dumps(self._payload), provider="stub", model="test"
            ),
            attempts=(),
        )


_AI_STEPS = {
    "summary": "CSV to JSON, then back to CSV",
    "steps": [
        {
            "intent": "office.csv_to_json",
            "tool_id": "office.csv_to_json",
            "arguments": {"input_path": "data.csv", "output_path": "data.json"},
            "summary": "CSV to JSON",
        },
        {
            "intent": "office.json_to_csv",
            "tool_id": "office.json_to_csv",
            "arguments": {
                "input_path": "$step1.output_path",
                "output_path": "back.csv",
            },
            "summary": "JSON back to CSV",
        },
    ],
}


def test_ai_multistep_plan_executes(tmp_path: Path) -> None:
    settings, tasks, _e, _m, _r, orchestrator = _setup(
        tmp_path, router=_StubRouter(_AI_STEPS)
    )
    (settings.workspace_dir / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    _task, report = _run(
        tasks, orchestrator, "Reformat the numbers file twice", use_ai=True
    )

    assert report.success, report.error
    assert report.steps_total == 2
    assert (settings.workspace_dir / "back.csv").is_file()


def test_ai_multistep_forward_reference_rejected(tmp_path: Path) -> None:
    bad = json.loads(json.dumps(_AI_STEPS))
    bad["steps"] = [bad["steps"][1], bad["steps"][0]]
    settings, tasks, _e, _m, _r, orchestrator = _setup(
        tmp_path, router=_StubRouter(bad)
    )
    (settings.workspace_dir / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    _task, report = _run(
        tasks, orchestrator, "Reformat the numbers file twice", use_ai=True
    )

    assert report.task.status is TaskStatus.FAILED
    assert "earlier step" in (report.error or "")


def test_tampered_multistep_workflow_rejected(tmp_path: Path) -> None:
    settings, tasks, execution, memory, _r, orchestrator = _setup(tmp_path)
    create_demo_pdfs(settings.workspace_dir)
    step = {
        "intent": "pdf.merge_folders",
        "tool_id": "pdf.merge_folders",
        "arguments": {"input_folders": ["Folder A"], "output_path": "x.pdf"},
        "summary": "ok",
        "planner": "forged",
        "external_read_roots": [],
        "expected_pages": None,
    }
    evil = dict(step)
    evil["intent"] = "workspace.write_text"
    evil["tool_id"] = "workspace.write_text"
    evil["arguments"] = {"path": "evil.txt", "content": "nope"}
    memory.upsert(
        kind="workflow",
        key="wf-evilmultistep000",
        title="Forged multi-step",
        content={
            "instruction": "Merge then write",
            "intent": "multi_step",
            "tool_id": "pdf.merge_folders",
            "steps": [step, evil],
            "bindings": [[], []],
            "success_count": 5,
        },
    )
    task = tasks.create(Task(instruction="Merge then write"))
    report = orchestrator.run_workflow(task.id, "wf-evilmulti")

    assert not report.success
    assert "no longer maps to a valid allowed plan" in (report.error or "")
    assert execution.list_tool_invocations(task.id) == []


def test_cli_multistep_text_json_and_workflow_show(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    create_demo_pdfs(settings.workspace_dir)
    root = ["--root", str(tmp_path)]

    assert main([*root, "run", _CHAIN]) == 0
    out = capsys.readouterr().out
    assert "Step 1/2 [succeeded] pdf.merge_folders" in out
    assert "Step 2/2 [succeeded] pdf.extract_pages" in out

    assert main([*root, "run", _CHAIN, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["steps_total"] == 2
    assert [step["status"] for step in payload["steps"]] == ["succeeded"] * 2

    assert main([*root, "workflow", "show", "latest"]) == 0
    assert "Multi-step plan: 2 step(s)" in capsys.readouterr().out

    assert main([*root, "doctor"]) == 0
    assert "PHASE 16.1 Credential Manager Hotfix" in capsys.readouterr().out


def test_gui_controller_multistep(tmp_path: Path) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    controller = GuiController(settings, desktop_opener=lambda _target: None)
    controller.create_pdf_demo()

    report = controller.run_new_task(_CHAIN)

    assert report.success, report.error
    text = format_execution_report(report)
    assert "Step 1/2 [succeeded] pdf.merge_folders" in text
    assert "Step 2/2 [succeeded] pdf.extract_pages" in text
