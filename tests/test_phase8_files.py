"""Phase 8: general workspace file operations (copy/move/rename/organize/search)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.domain.task import Task
from pc_agent.gui.controller import GuiController
from pc_agent.orchestrator import TaskOrchestrator
from pc_agent.planning import DeterministicPlanner, PlannerOptions
from pc_agent.policies.policy import Action, ActionRequest, PolicyEngine, PolicyOutcome
from pc_agent.storage.db import Database
from pc_agent.storage.repositories import (
    ExecutionRepository,
    MemoryRepository,
    RecoveryRepository,
    TaskRepository,
)
from pc_agent.tools import build_default_registry
from pc_agent.tools.registry import ToolContext


def _context(tmp_path: Path) -> ToolContext:
    return ToolContext.for_workspace(tmp_path)


def _invoke(tmp_path: Path, tool_id: str, arguments: dict):
    return build_default_registry().invoke(tool_id, arguments, _context(tmp_path))


# ----------------------------------------------------------------------
# Policy
# ----------------------------------------------------------------------
def test_policy_move_file_is_write_like_inside_workspace(tmp_path: Path) -> None:
    engine = PolicyEngine(tmp_path)
    inside = engine.evaluate(ActionRequest(Action.MOVE_FILE, tmp_path / "a.txt"))
    assert inside.outcome is PolicyOutcome.ALLOW
    outside = engine.evaluate(ActionRequest(Action.MOVE_FILE, tmp_path.parent / "a.txt"))
    assert outside.outcome is PolicyOutcome.APPROVAL_REQUIRED
    # A real delete is still approval-gated even inside the workspace.
    delete = engine.evaluate(ActionRequest(Action.DELETE_FILE, tmp_path / "a.txt"))
    assert delete.outcome is PolicyOutcome.APPROVAL_REQUIRED


# ----------------------------------------------------------------------
# Direct tools
# ----------------------------------------------------------------------
def test_copy_preserves_bytes_and_never_overwrites(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello\x00world")

    result = _invoke(tmp_path, "files.copy", {"path": "a.txt", "output_path": "sub/b.txt"})

    assert result.success, result.error
    assert (tmp_path / "sub" / "b.txt").read_bytes() == b"hello\x00world"
    assert (tmp_path / "a.txt").is_file()
    assert result.output is not None
    assert result.output["sha256"] == hashlib.sha256(b"hello\x00world").hexdigest()

    again = _invoke(tmp_path, "files.copy", {"path": "a.txt", "output_path": "sub/b.txt"})
    assert not again.success
    assert "already exists" in (again.error or "")
    assert (tmp_path / "sub" / "b.txt").read_bytes() == b"hello\x00world"


def test_copy_rejects_escape_and_folder_source(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "folder").mkdir()

    escape = _invoke(
        tmp_path, "files.copy", {"path": "a.txt", "output_path": "../outside.txt"}
    )
    assert not escape.success
    assert not (tmp_path.parent / "outside.txt").exists()

    folder = _invoke(
        tmp_path, "files.copy", {"path": "folder", "output_path": "b.txt"}
    )
    assert not folder.success
    assert "is a folder" in (folder.error or "")


def test_move_and_rename(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("move me", encoding="utf-8")

    moved = _invoke(
        tmp_path, "files.move", {"path": "a.txt", "output_path": "deep/nest/a.txt"}
    )
    assert moved.success, moved.error
    assert not (tmp_path / "a.txt").exists()
    assert (tmp_path / "deep" / "nest" / "a.txt").read_text(encoding="utf-8") == "move me"

    renamed = _invoke(
        tmp_path,
        "files.rename",
        {"path": "deep/nest/a.txt", "new_name": "final.txt"},
    )
    assert renamed.success, renamed.error
    assert (tmp_path / "deep" / "nest" / "final.txt").is_file()

    bad_name = _invoke(
        tmp_path,
        "files.rename",
        {"path": "deep/nest/final.txt", "new_name": "../evil.txt"},
    )
    assert not bad_name.success
    assert "path separators" in (bad_name.error or "")


def test_organize_by_category_and_extension_with_conflict_skip(
    tmp_path: Path,
) -> None:
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "photo.png").write_bytes(b"png")
    (loose / "notes.txt").write_text("n", encoding="utf-8")
    (loose / "data.csv").write_text("a\n", encoding="utf-8")
    (loose / "documents").mkdir()
    (loose / "documents" / "notes.txt").write_text("existing", encoding="utf-8")

    result = _invoke(
        tmp_path, "files.organize", {"directory": "loose", "by": "category"}
    )
    assert result.success, result.error
    assert result.output is not None
    assert result.output["moved_count"] == 2
    assert result.output["skipped_count"] == 1
    assert (loose / "images" / "photo.png").is_file()
    assert (loose / "data" / "data.csv").is_file()
    # Conflict was skipped, never overwritten.
    assert (loose / "notes.txt").is_file()
    assert (loose / "documents" / "notes.txt").read_text(encoding="utf-8") == "existing"

    other = tmp_path / "byext"
    other.mkdir()
    (other / "a.LOG").write_text("l", encoding="utf-8")
    (other / "noext").write_text("x", encoding="utf-8")
    by_ext = _invoke(
        tmp_path, "files.organize", {"directory": "byext", "by": "extension"}
    )
    assert by_ext.success, by_ext.error
    assert (other / "log" / "a.LOG").is_file()
    assert (other / "no_extension" / "noext").is_file()


def test_search_names_wildcards_and_content(tmp_path: Path) -> None:
    (tmp_path / "Report 2026.txt").write_text("quarterly numbers\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("the secretword is here\n", encoding="utf-8")
    (tmp_path / ".env").write_text("secretword=hunter2\n", encoding="utf-8")
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "report-old.csv").write_text("a,b\n", encoding="utf-8")

    by_name = _invoke(tmp_path, "files.search", {"query": "report"})
    assert by_name.success, by_name.error
    assert by_name.output is not None
    paths = {item["path"] for item in by_name.output["matches"]}
    assert paths == {"Report 2026.txt", "sub/report-old.csv"}

    wildcard = _invoke(tmp_path, "files.search", {"query": "*.md"})
    assert wildcard.output is not None
    assert [item["path"] for item in wildcard.output["matches"]] == ["notes.md"]

    content = _invoke(
        tmp_path,
        "files.search",
        {"query": "secretword", "search_content": True},
    )
    assert content.output is not None
    # notes.md matches by content; the credential-like .env file is listed by
    # name-only tools elsewhere but its *content* is never searched or shown.
    assert [item["path"] for item in content.output["matches"]] == ["notes.md"]
    assert content.output["matches"][0]["first_matching_line"] == "the secretword is here"


# ----------------------------------------------------------------------
# Deterministic planner
# ----------------------------------------------------------------------
def test_planner_file_intents(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("x", encoding="utf-8")
    planner = DeterministicPlanner(workspace)

    copy_outcome = planner.plan(
        "Copy the file",
        PlannerOptions(target_path="a.txt", output_path="backup/a.txt"),
    )
    assert copy_outcome is not None and copy_outcome.plan is not None
    assert copy_outcome.plan.tool_id == "files.copy"
    assert copy_outcome.plan.arguments == {
        "path": "a.txt",
        "output_path": "backup/a.txt",
    }

    missing = planner.plan("Copy the file", PlannerOptions(target_path="a.txt"))
    assert missing is not None and missing.plan is None
    assert "--output" in missing.missing_information[0]

    rename_outcome = planner.plan(
        "Rename the file",
        PlannerOptions(target_path="a.txt", output_path="b.txt"),
    )
    assert rename_outcome is not None and rename_outcome.plan is not None
    assert rename_outcome.plan.tool_id == "files.rename"
    assert rename_outcome.plan.arguments["new_name"] == "b.txt"

    organize_outcome = planner.plan("Organize this folder by extension")
    assert organize_outcome is not None and organize_outcome.plan is not None
    assert organize_outcome.plan.tool_id == "files.organize"
    assert organize_outcome.plan.arguments["by"] == "extension"

    search_outcome = planner.plan('Search files containing "budget"')
    assert search_outcome is not None and search_outcome.plan is not None
    assert search_outcome.plan.tool_id == "files.search"
    assert search_outcome.plan.arguments["query"] == "budget"
    assert search_outcome.plan.arguments["search_content"] is True


def test_planner_refuses_external_source(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    planner = DeterministicPlanner(workspace)

    outcome = planner.plan(
        "Copy the file",
        PlannerOptions(target_path=str(outside), output_path="b.txt"),
    )
    assert outcome is not None and outcome.plan is None
    assert "workspace-only" in outcome.missing_information[0]


# ----------------------------------------------------------------------
# Orchestrator end to end
# ----------------------------------------------------------------------
def _setup(tmp_path: Path):
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
        recovery_repository=RecoveryRepository(database),
        memory_repository=MemoryRepository(database),
        recovery_backoff_seconds=0,
    )
    return settings, tasks, execution, orchestrator


def test_orchestrated_copy_verifies_criteria_and_checkpoints(tmp_path: Path) -> None:
    settings, tasks, execution, orchestrator = _setup(tmp_path)
    (settings.workspace_dir / "report.txt").write_text("payload", encoding="utf-8")
    task = tasks.create(Task(instruction="Copy the file"))

    report = orchestrator.execute(
        task.id,
        options=PlannerOptions(
            target_path="report.txt", output_path="archive/report.txt"
        ),
    )

    assert report.success, report.error
    assert (settings.workspace_dir / "archive" / "report.txt").is_file()
    assert report.verification_report is not None and report.verification_report.passed
    kinds = [item.criterion.kind for item in report.verification_report.results]
    assert kinds == ["tool_verification", "output_artifact", "output_hash"]
    checkpoint = execution.get_checkpoint(task.id, 0)
    assert checkpoint is not None
    # Both the destination and the (still required) source are checkpoint
    # artifacts, so a resume re-checks that both still exist.
    assert set(checkpoint.artifacts) == {"archive/report.txt", "report.txt"}


def test_orchestrated_copy_collision_recovers_to_fresh_output(tmp_path: Path) -> None:
    settings, tasks, _execution, orchestrator = _setup(tmp_path)
    (settings.workspace_dir / "report.txt").write_text("new bytes", encoding="utf-8")
    existing = settings.workspace_dir / "archive" / "report.txt"
    existing.parent.mkdir(parents=True)
    existing.write_text("keep me", encoding="utf-8")
    task = tasks.create(Task(instruction="Copy the file"))

    report = orchestrator.execute(
        task.id,
        options=PlannerOptions(
            target_path="report.txt", output_path="archive/report.txt"
        ),
    )

    assert report.success, report.error
    assert existing.read_text(encoding="utf-8") == "keep me"
    assert report.tool_result is not None and report.tool_result.output is not None
    recovered = settings.workspace_dir / report.tool_result.output["output_path"]
    assert recovered != existing
    assert recovered.read_text(encoding="utf-8") == "new bytes"


def test_orchestrated_move_and_empty_copy(tmp_path: Path) -> None:
    settings, tasks, _execution, orchestrator = _setup(tmp_path)
    (settings.workspace_dir / "a.txt").write_text("m", encoding="utf-8")
    (settings.workspace_dir / "empty.txt").write_bytes(b"")

    move_task = tasks.create(Task(instruction="Move the file"))
    move_report = orchestrator.execute(
        move_task.id,
        options=PlannerOptions(target_path="a.txt", output_path="moved/a.txt"),
    )
    assert move_report.success, move_report.error
    assert not (settings.workspace_dir / "a.txt").exists()

    empty_task = tasks.create(Task(instruction="Copy the file"))
    empty_report = orchestrator.execute(
        empty_task.id,
        options=PlannerOptions(target_path="empty.txt", output_path="empty-copy.txt"),
    )
    assert empty_report.success, empty_report.error
    assert (settings.workspace_dir / "empty-copy.txt").stat().st_size == 0


# ----------------------------------------------------------------------
# CLI and GUI controller
# ----------------------------------------------------------------------
def test_cli_files_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    (settings.workspace_dir / "a.txt").write_text("cli", encoding="utf-8")
    root = ["--root", str(tmp_path)]

    assert main([*root, "files", "copy", "a.txt", "copies/a.txt"]) == 0
    assert "Copied: a.txt -> copies/a.txt" in capsys.readouterr().out

    assert main([*root, "files", "rename", "copies/a.txt", "b.txt"]) == 0
    capsys.readouterr()
    assert (settings.workspace_dir / "copies" / "b.txt").is_file()

    assert main([*root, "files", "search", "b.txt"]) == 0
    assert "copies/b.txt" in capsys.readouterr().out

    (settings.workspace_dir / "pic.png").write_bytes(b"png")
    assert main([*root, "files", "organize", "."]) == 0
    assert "moved=2" in capsys.readouterr().out
    assert (settings.workspace_dir / "images" / "pic.png").is_file()
    assert (settings.workspace_dir / "documents" / "a.txt").is_file()

    assert main([*root, "run", "Move the file", "--target", "images/pic.png",
                 "--output", "pic-final.png"]) == 0
    out = capsys.readouterr().out
    assert "Status: succeeded" in out
    assert "[PASS]" in out

    assert main([*root, "doctor"]) == 0
    doctor = capsys.readouterr().out
    assert "PHASE 10 Workflow Reuse" in doctor
    assert "built-ins=29" in doctor and "files=5" in doctor


def test_gui_controller_file_tools(tmp_path: Path) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    controller = GuiController(settings, desktop_opener=lambda _target: None)
    (settings.workspace_dir / "gui.txt").write_text("gui", encoding="utf-8")

    copied = controller.files_copy("gui.txt", "gui-copy.txt")
    assert copied.success, copied.error

    found = controller.files_search("gui", search_content=True)
    assert found.success, found.error
    assert found.output is not None and found.output["count"] >= 2

    renamed = controller.files_rename("gui-copy.txt", "gui-final.txt")
    assert renamed.success, renamed.error
    assert (settings.workspace_dir / "gui-final.txt").is_file()

    summary = controller.doctor_summary()
    assert summary.builtin_tools == 29
    assert summary.file_tools == 5
    assert "files=5" in summary.to_text()
