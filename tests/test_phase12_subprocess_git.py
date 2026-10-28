"""Phase 12: approved subprocess execution + local Git tools."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pc_agent.cli import main
from pc_agent.commands import run_command, validate_windows_batch_argv
from pc_agent.config import Settings
from pc_agent.domain.task import Task, TaskStatus
from pc_agent.errors import ConfigError, PlanningError, ToolError
from pc_agent.gui.controller import GuiController
from pc_agent.orchestrator import TaskOrchestrator
from pc_agent.planning import (
    DeterministicPlanner,
    PlannerOptions,
    plan_with_provider,
)
from pc_agent.storage.db import Database
from pc_agent.storage.repositories import (
    ExecutionRepository,
    MemoryRepository,
    RecoveryRepository,
    TaskRepository,
)
from pc_agent.tools import build_default_registry

_PY = Path(sys.executable).name
requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


def _setup(tmp_path: Path, environ: dict[str, str] | None = None):
    settings = Settings.from_env(
        tmp_path,
        environ=(
            {"PC_AGENT_ALLOW_SUBPROCESS": "1"} if environ is None else environ
        ),
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


def _git_identity(repo: Path) -> None:
    for args in (
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Tester"),
    ):
        result = run_command(
            executable="git",
            argv=["-C", str(repo), *args],
            cwd=repo,
            timeout_seconds=10,
            allowlist=("git",),
        )
        assert result.exit_code == 0, result.stderr


def _init_repo_with_file(settings, tasks, orchestrator) -> Path:
    _task, init = _run(
        tasks,
        orchestrator,
        "Initialize a git repository",
        options=PlannerOptions(target_path="repo"),
        approval_callback=_approve,
    )
    assert init.success, init.error
    repo = settings.workspace_dir / "repo"
    _git_identity(repo)
    (repo / "readme.txt").write_text("hello\n", encoding="utf-8")
    return repo


# ---------------------------------------------------------------------- engine


def test_engine_runs_captures_and_bounds(tmp_path: Path) -> None:
    ok = run_command(
        executable=_PY,
        argv=["-c", "print(40 + 2)"],
        cwd=tmp_path,
        timeout_seconds=10,
        allowlist=("python", "python3"),
    )
    assert ok.exit_code == 0 and ok.stdout.strip() == "42" and not ok.timed_out

    failing = run_command(
        executable=_PY,
        argv=["-c", "import sys; sys.exit(3)"],
        cwd=tmp_path,
        timeout_seconds=10,
        allowlist=("python", "python3"),
    )
    assert failing.exit_code == 3

    slow = run_command(
        executable=_PY,
        argv=["-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        timeout_seconds=1,
        allowlist=("python", "python3"),
    )
    assert slow.timed_out and slow.exit_code is None

    noisy = run_command(
        executable=_PY,
        argv=["-c", "print('x' * 200000)"],
        cwd=tmp_path,
        timeout_seconds=10,
        allowlist=("python", "python3"),
    )
    assert noisy.truncated and len(noisy.stdout) <= 64 * 1024


def test_engine_rejects_paths_and_unlisted_programs(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="bare program name"):
        run_command(
            executable="./python",
            argv=[],
            cwd=tmp_path,
            timeout_seconds=5,
            allowlist=("python",),
        )
    with pytest.raises(ToolError, match="not on the approved program list"):
        run_command(
            executable="curl",
            argv=[],
            cwd=tmp_path,
            timeout_seconds=5,
            allowlist=("git",),
        )
    with pytest.raises(ToolError, match="not found on PATH"):
        run_command(
            executable="definitely-not-a-program-xyz",
            argv=[],
            cwd=tmp_path,
            timeout_seconds=5,
            allowlist=("definitely-not-a-program-xyz",),
        )


def test_engine_scrubs_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PC_AGENT_TEST_SECRET_TOKEN", "topsecret")
    result = run_command(
        executable=_PY,
        argv=[
            "-c",
            "import os; print(os.environ.get('PC_AGENT_TEST_SECRET_TOKEN'))",
        ],
        cwd=tmp_path,
        timeout_seconds=10,
        allowlist=("python", "python3"),
    )
    assert result.stdout.strip() == "None"
    assert "topsecret" not in result.stdout
    assert os.environ["PC_AGENT_TEST_SECRET_TOKEN"] == "topsecret"  # parent kept


def test_windows_batch_argv_validation() -> None:
    validate_windows_batch_argv(["-m", "pytest", "-q"])
    for bad in ("a&b", "%PATH%", "a|b", 'say "hi"'):
        with pytest.raises(ToolError, match="batch shim"):
            validate_windows_batch_argv([bad])


def test_config_parsing(tmp_path: Path) -> None:
    default = Settings.from_env(tmp_path, environ={})
    assert default.allow_subprocess is False
    assert "git" in default.command_allowlist
    assert default.command_timeout_seconds == 60.0

    enabled = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_ALLOW_SUBPROCESS": "yes",
            "PC_AGENT_COMMAND_ALLOWLIST": "git, python",
            "PC_AGENT_COMMAND_TIMEOUT_SECONDS": "30",
        },
    )
    assert enabled.allow_subprocess is True
    assert enabled.command_allowlist == ("git", "python")

    with pytest.raises(ConfigError, match="PC_AGENT_ALLOW_SUBPROCESS"):
        Settings.from_env(tmp_path, environ={"PC_AGENT_ALLOW_SUBPROCESS": "maybe"})
    with pytest.raises(ConfigError, match="bare program names"):
        Settings.from_env(
            tmp_path, environ={"PC_AGENT_COMMAND_ALLOWLIST": "tools/git"}
        )


# ------------------------------------------------------------------------ tasks


@requires_git
def test_subprocess_disabled_by_default(tmp_path: Path) -> None:
    _s, tasks, _e, orchestrator = _setup(tmp_path, environ={})

    _task, report = _run(tasks, orchestrator, "Show git status")

    assert report.task.status is TaskStatus.FAILED
    assert "disabled by policy" in (report.error or "")
    assert len(report.recovery_attempts) == 1  # never retried


@requires_git
def test_git_full_workflow(tmp_path: Path) -> None:
    settings, tasks, _e, orchestrator = _setup(tmp_path)
    repo = _init_repo_with_file(settings, tasks, orchestrator)
    target = PlannerOptions(target_path="repo")

    _t, status = _run(tasks, orchestrator, "Show git status", options=target)
    assert status.success, status.error
    assert status.tool_result is not None and status.tool_result.output
    assert status.tool_result.output["untracked"] == ["readme.txt"]
    assert status.tool_result.output["clean"] is False

    _t, added = _run(
        tasks,
        orchestrator,
        "Git add the file",
        options=PlannerOptions(
            target_path="repo", input_files=("readme.txt",)
        ),
        approval_callback=_approve,
    )
    assert added.success, added.error

    _t, committed = _run(
        tasks,
        orchestrator,
        'Git commit "Initial import"',
        options=target,
        approval_callback=_approve,
    )
    assert committed.success, committed.error
    assert committed.verification_valid is True

    _t, log = _run(tasks, orchestrator, "Show the git log", options=target)
    assert log.success, log.error
    assert log.tool_result is not None and log.tool_result.output
    assert log.tool_result.output["commits"][0]["subject"] == "Initial import"

    _t, clean = _run(tasks, orchestrator, "Show git status", options=target)
    assert clean.tool_result is not None and clean.tool_result.output
    assert clean.tool_result.output["clean"] is True

    (repo / "readme.txt").write_text("hello\nworld\n", encoding="utf-8")
    _t, diff = _run(tasks, orchestrator, "Show the git diff", options=target)
    assert diff.success, diff.error
    assert diff.tool_result is not None and diff.tool_result.output
    assert diff.tool_result.output["files_changed"] == 1


@requires_git
def test_git_commit_approval_denied(tmp_path: Path) -> None:
    settings, tasks, execution, orchestrator = _setup(tmp_path)
    _init_repo_with_file(settings, tasks, orchestrator)
    _t, added = _run(
        tasks,
        orchestrator,
        "Git add the file",
        options=PlannerOptions(
            target_path="repo", input_files=("readme.txt",)
        ),
        approval_callback=_approve,
    )
    assert added.success, added.error

    task, denied = _run(
        tasks,
        orchestrator,
        'Git commit "Initial import"',
        options=PlannerOptions(target_path="repo"),
        approval_callback=_deny,
    )
    assert denied.task.status is TaskStatus.CANCELLED
    approvals = execution.list_approvals(task.id)
    assert approvals and approvals[0].scope["tool_id"] == "git.commit"
    assert approvals[0].decision == "denied"

    _t, log = _run(
        tasks,
        orchestrator,
        "Show the git log",
        options=PlannerOptions(target_path="repo"),
    )
    assert log.tool_result is not None and log.tool_result.output
    assert log.tool_result.output["count"] == 0  # nothing was committed


def _command_options(code: str, *, timeout: int | None = None) -> PlannerOptions:
    return PlannerOptions(
        command_executable=_PY,
        command_arguments=("-c", code),
        command_timeout=timeout,
    )


def test_commands_run_requires_approval_then_runs(tmp_path: Path) -> None:
    settings, tasks, _e, orchestrator = _setup(tmp_path)
    marker = settings.workspace_dir / "marker.txt"
    code = "open('marker.txt', 'w').write('ran')"

    _t, denied = _run(
        tasks,
        orchestrator,
        "Run the approved command",
        options=_command_options(code),
        approval_callback=_deny,
    )
    assert denied.task.status is TaskStatus.CANCELLED
    assert not marker.exists()

    _t, approved = _run(
        tasks,
        orchestrator,
        "Run the approved command",
        options=_command_options(code),
        approval_callback=_approve,
    )
    assert approved.success, approved.error
    assert marker.read_text(encoding="utf-8") == "ran"
    assert approved.verification_valid is True


def test_commands_run_exit_mismatch_fails_but_keeps_output(tmp_path: Path) -> None:
    _s, tasks, _e, orchestrator = _setup(tmp_path)

    _t, report = _run(
        tasks,
        orchestrator,
        "Run the approved command",
        options=_command_options("import sys; print('oops'); sys.exit(3)"),
        approval_callback=_approve,
    )

    assert report.task.status is TaskStatus.FAILED
    assert report.verification_valid is False
    assert report.tool_result is not None and report.tool_result.output
    assert report.tool_result.output["exit_code"] == 3
    assert "oops" in report.tool_result.output["stdout"]


def test_commands_run_timeout_is_never_retried(tmp_path: Path) -> None:
    _s, tasks, _e, orchestrator = _setup(tmp_path)

    _t, report = _run(
        tasks,
        orchestrator,
        "Run the approved command",
        options=_command_options("import time; time.sleep(5)", timeout=1),
        approval_callback=_approve,
    )

    assert report.task.status is TaskStatus.FAILED
    assert "timed out" in (report.error or "")
    assert len(report.recovery_attempts) == 1


def test_commands_run_allowlist_enforced(tmp_path: Path) -> None:
    _s, tasks, _e, orchestrator = _setup(tmp_path)

    _t, report = _run(
        tasks,
        orchestrator,
        "Run the approved command",
        options=PlannerOptions(command_executable="curl"),
        approval_callback=_approve,
    )

    assert report.task.status is TaskStatus.FAILED
    assert "not on the approved program list" in (report.error or "")


def test_command_workflow_replay_requires_fresh_approval(tmp_path: Path) -> None:
    _s, tasks, _e, orchestrator = _setup(tmp_path)
    _t, first = _run(
        tasks,
        orchestrator,
        "Run the approved command",
        options=_command_options("print('workflow-ran')"),
        approval_callback=_approve,
    )
    assert first.success, first.error
    assert first.workflow_key is not None

    repeat = tasks.create(Task(instruction="Run the approved command"))
    denied = orchestrator.run_workflow(
        repeat.id, first.workflow_key, approval_callback=_deny
    )
    assert denied.task.status is TaskStatus.CANCELLED  # memory grants nothing


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


def test_ai_may_read_git_but_never_run_commands() -> None:
    outcome = plan_with_provider(
        _StubRouter(
            {
                "intent": "git.status",
                "tool_id": "git.status",
                "arguments": {"repository": "repo"},
                "summary": "status",
            }
        ),
        "How does the repo look?",
        PlannerOptions(),
    )
    assert outcome.plan is not None
    assert outcome.plan.tool_id == "git.status"

    with pytest.raises(PlanningError, match="not allow-listed"):
        plan_with_provider(
            _StubRouter(
                {
                    "intent": "commands.run",
                    "tool_id": "commands.run",
                    "arguments": {"executable": "python"},
                    "summary": "run it",
                }
            ),
            "Run something",
            PlannerOptions(),
        )
    with pytest.raises(PlanningError, match="not allow-listed"):
        plan_with_provider(
            _StubRouter(
                {
                    "intent": "git.commit",
                    "tool_id": "git.commit",
                    "arguments": {"message": "x"},
                    "summary": "commit",
                }
            ),
            "Commit for me",
            PlannerOptions(),
        )


# ------------------------------------------------------------------- CLI + GUI


@requires_git
def test_cli_git_and_command_flow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PC_AGENT_ALLOW_SUBPROCESS", "1")
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    root = ["--root", str(tmp_path)]

    assert main([*root, "doctor"]) == 0
    assert "Subprocess: enabled" in capsys.readouterr().out

    assert main([*root, "git", "init", "repo", "--yes"]) == 0
    capsys.readouterr()
    repo = settings.workspace_dir / "repo"
    _git_identity(repo)
    (repo / "a.txt").write_text("data\n", encoding="utf-8")

    assert main([*root, "git", "status", "--repo", "repo"]) == 0
    assert "a.txt" in capsys.readouterr().out

    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    assert main([*root, "git", "add", "a.txt", "--repo", "repo"]) == 4  # no --yes
    capsys.readouterr()
    assert main([*root, "git", "add", "a.txt", "--repo", "repo", "--yes"]) == 0
    capsys.readouterr()
    assert main([*root, "git", "commit", "-m", "First", "--repo", "repo", "--yes"]) == 0
    assert "Committed:" in capsys.readouterr().out
    assert main([*root, "git", "log", "--repo", "repo"]) == 0
    assert "First" in capsys.readouterr().out

    assert main(
        [
            *root,
            "run",
            "Run the approved command",
            "--command",
            f"{_PY} -c print(6*7)",
            "--yes",
        ]
    ) == 0
    out = capsys.readouterr().out
    assert "Status: succeeded" in out


@requires_git
def test_gui_controller_git_status_and_doctor(tmp_path: Path) -> None:
    settings = Settings.from_env(
        tmp_path, environ={"PC_AGENT_ALLOW_SUBPROCESS": "1"}
    )
    controller = GuiController(settings, desktop_opener=lambda _target: None)

    summary = controller.doctor_summary()
    assert "Subprocess: enabled" in summary.to_text()

    missing = controller.git_status(".")
    assert not missing.success  # workspace root is not a repository

    repo = settings.workspace_dir / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    init = run_command(
        executable="git",
        argv=["-C", str(repo), "init", "-q"],
        cwd=repo,
        timeout_seconds=10,
        allowlist=("git",),
    )
    assert init.exit_code == 0
    status = controller.git_status("repo")
    assert status.success, status.error
