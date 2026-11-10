"""Transformation Phase 2: Safety Kernel, Approval Center, encrypted backups."""

from __future__ import annotations

import shutil
import sqlite3
import sys
import time
import tomllib
from datetime import timedelta
from pathlib import Path

import pytest

import pc_agent.storage.db as db_module
from pc_agent import __version__
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.diagnostics import build_diagnostics
from pc_agent.documents.pdf import write_blank_pdf
from pc_agent.domain.task import Task, TaskStatus, utc_now
from pc_agent.errors import ApprovalUnavailableError, ConfigError, PcAgentError, StorageError
from pc_agent.maintenance import DatabaseMaintenanceService
from pc_agent.orchestrator import TaskOrchestrator
from pc_agent.planning import DeterministicPlanner, PlannerOptions
from pc_agent.release import audit_install
from pc_agent.safety import EmergencyStop, SafetyKernel, arguments_digest
from pc_agent.storage.db import LATEST_SCHEMA_VERSION, Database
from pc_agent.storage.repositories import ExecutionRepository, TaskRepository
from pc_agent.tools import build_default_registry
from pc_agent.tools.registry import ToolContext, ToolRegistry, ToolSpec

_ROOT = Path(__file__).resolve().parents[1]


def _setup(tmp_path: Path, environ: dict[str, str] | None = None):
    settings = Settings.from_env(tmp_path, environ=environ or {})
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
    return settings, database, tasks, execution, orchestrator


def _outside_pdfs(tmp_path: Path) -> Path:
    outside = tmp_path / "outside" / "Folder A"
    write_blank_pdf(outside / "a.pdf", 2)
    write_blank_pdf(outside / "b.pdf", 1)
    return outside


def test_safety_settings_defaults_and_validation(tmp_path: Path) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    assert settings.safety_hard_timeouts is True
    assert settings.safety_worker_isolation is True
    assert settings.safety_max_tool_seconds == 600.0
    assert settings.safety_approval_expiry_minutes == 1440
    summary = settings.public_summary()
    assert summary["safety_hard_timeouts"] == "yes"
    assert summary["safety_worker_isolation"] == "yes"

    off = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_SAFETY_HARD_TIMEOUTS": "0",
            "PC_AGENT_SAFETY_WORKER_ISOLATION": "off",
            "PC_AGENT_SAFETY_MAX_TOOL_SECONDS": "30",
            "PC_AGENT_SAFETY_APPROVAL_EXPIRY_MINUTES": "15",
        },
    )
    assert off.safety_hard_timeouts is False
    assert off.safety_worker_isolation is False
    assert off.safety_max_tool_seconds == 30.0
    assert off.safety_approval_expiry_minutes == 15

    with pytest.raises(ConfigError):
        Settings.from_env(
            tmp_path, environ={"PC_AGENT_SAFETY_MAX_TOOL_SECONDS": "900"}
        )
    with pytest.raises(ConfigError):
        Settings.from_env(
            tmp_path, environ={"PC_AGENT_SAFETY_APPROVAL_EXPIRY_MINUTES": "0"}
        )
    with pytest.raises(ConfigError):
        Settings.from_env(
            tmp_path, environ={"PC_AGENT_SAFETY_HARD_TIMEOUTS": "maybe"}
        )


def test_arguments_digest_is_stable_and_scope_sensitive() -> None:
    assert arguments_digest({"b": 2, "a": 1}) == arguments_digest({"a": 1, "b": 2})
    assert arguments_digest({"a": 1}) != arguments_digest({"a": 2})
    assert len(arguments_digest({})) == 64


def _slow_spec() -> ToolSpec:
    return ToolSpec(
        id="browser.slow_echo",
        name="Slow Echo",
        version="1.0.0",
        description="Deliberately slow in-process tool for timeout tests.",
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string", "minLength": 1}},
            "required": ["message"],
        },
        output_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        permissions=(),
        timeout_seconds=0.3,
        provenance="test",
    )


def test_hard_timeout_thread_path_abandons_slow_tool(tmp_path: Path) -> None:
    settings, *_rest = _setup(tmp_path)
    registry = ToolRegistry()

    def slow(arguments, _context):
        time.sleep(4)
        return {"message": arguments["message"]}

    registry.register(_slow_spec(), slow)
    kernel = SafetyKernel(settings=settings, registry=registry)
    context = ToolContext.for_workspace(settings.workspace_dir)

    started = time.perf_counter()
    result = kernel.invoke("browser.slow_echo", {"message": "hi"}, context)
    elapsed = time.perf_counter() - started
    kernel.close()

    assert not result.success
    assert "hard timeout" in (result.error or "")
    assert elapsed < 3.5
    assert kernel.stats.hard_timeouts == 1
    assert kernel.stats.abandoned_threads == 1


def test_worker_executes_builtin_tool_in_isolation(tmp_path: Path) -> None:
    settings, *_rest = _setup(tmp_path)
    write_blank_pdf(settings.workspace_dir / "sample.pdf", 2)
    kernel = SafetyKernel(settings=settings, registry=build_default_registry())
    context = ToolContext.for_workspace(settings.workspace_dir)
    try:
        result = kernel.invoke("pdf.verify", {"path": "sample.pdf"}, context)
    finally:
        kernel.close()

    assert result.success, result.error
    assert result.output is not None and result.output.get("valid") is True
    assert kernel.stats.worker_invocations >= 1
    assert kernel.stats.in_process_invocations == 0


def _sleeping_interpreter() -> str:
    """A real, PATH-resolvable Python for the hung-command probe.

    Windows installs ``python.exe`` (never ``python3``), so hard-coding
    ``python3`` made the allowlist reject the call on Windows and the
    timeout path was never exercised. The interpreter running the tests is
    always a valid choice; ``shutil.which`` keeps it to a bare program name
    because the tool refuses absolute paths.
    """
    candidates = [Path(sys.executable).name, "python", "python3"]
    for candidate in candidates:
        if shutil.which(candidate):
            return candidate
    pytest.skip("no Python interpreter on PATH for the hung-command probe")


def test_worker_hard_timeout_kills_hung_tool_and_recovers(tmp_path: Path) -> None:
    settings, *_rest = _setup(
        tmp_path, environ={"PC_AGENT_SAFETY_MAX_TOOL_SECONDS": "1"}
    )
    interpreter = _sleeping_interpreter()
    kernel = SafetyKernel(settings=settings, registry=build_default_registry())
    context = ToolContext.for_workspace(
        settings.workspace_dir,
        subprocess_allowed=True,
        command_allowlist=(interpreter,),
        command_timeout_seconds=30.0,
    )
    try:
        started = time.perf_counter()
        result = kernel.invoke(
            "commands.run",
            {
                "executable": interpreter,
                "argv": ["-c", "import time; time.sleep(20)"],
                "timeout_seconds": 25,
            },
            context,
        )
        elapsed = time.perf_counter() - started
        assert not result.success
        assert "hard timeout" in (result.error or "")
        assert "killed" in (result.error or "")
        assert elapsed < 15
        assert kernel.stats.hard_timeouts == 1
        assert kernel.stats.worker_restarts >= 1

        # The next eligible call respawns a healthy worker.
        write_blank_pdf(settings.workspace_dir / "after.pdf", 1)
        recovered = kernel.invoke("pdf.verify", {"path": "after.pdf"}, context)
        assert recovered.success, recovered.error
    finally:
        kernel.close()


def test_emergency_stop_blocks_and_then_allows_execution(tmp_path: Path) -> None:
    settings, _database, tasks, _execution, orchestrator = _setup(tmp_path)
    from pc_agent.documents.pdf import create_demo_pdfs

    create_demo_pdfs(settings.workspace_dir)
    stop = EmergencyStop(settings.data_dir)
    state = stop.engage(reason="test", engaged_by="test")
    assert state.engaged and state.source == "file"

    blocked = tasks.create(Task(instruction="Merge PDFs from Folder A and Folder B"))
    report = orchestrator.execute(blocked.id)
    assert report.task.status is TaskStatus.CANCELLED
    assert "Emergency stop" in (report.error or "")
    events = [event.event_type for event in tasks.list_events(blocked.id)]
    assert "emergency_stop" in events
    assert not (settings.workspace_dir / "merged.pdf").exists()

    stop.clear()
    allowed = tasks.create(Task(instruction="Merge PDFs from Folder A and Folder B"))
    report = orchestrator.execute(allowed.id)
    assert report.success, report.error
    assert (settings.workspace_dir / "merged.pdf").is_file()


def test_emergency_stop_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PC_AGENT_EMERGENCY_STOP", "1")
    stop = EmergencyStop(tmp_path)
    state = stop.status()
    assert state.engaged and state.source == "environment"
    stop.clear()
    assert stop.engaged()  # environment still wins until unset
    monkeypatch.delenv("PC_AGENT_EMERGENCY_STOP")
    assert not stop.engaged()


def test_schema_v7_upgrade_and_approval_center_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "upgrade.db"
    original = db_module._MIGRATIONS
    monkeypatch.setattr(db_module, "_MIGRATIONS", original[:6])
    assert Database(path).initialize() == 6
    monkeypatch.setattr(db_module, "_MIGRATIONS", original)
    database = Database(path)
    assert database.initialize() == LATEST_SCHEMA_VERSION == 18
    with database.connect() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "approval_center_requests" in tables

    tasks = TaskRepository(database)
    execution = ExecutionRepository(database)
    task = tasks.create(Task(instruction="Merge PDFs"))
    digest = arguments_digest({"output_path": "x.pdf"})
    first = execution.create_approval_center_request(
        task.id,
        action="read_external_inputs",
        tool_id="pdf.merge_folders",
        arguments_sha256=digest,
        read_roots=("/outside",),
        summary="Approval required: Merge PDFs",
        expires_at=utc_now() + timedelta(hours=1),
    )
    again = execution.create_approval_center_request(
        task.id,
        action="read_external_inputs",
        tool_id="pdf.merge_folders",
        arguments_sha256=digest,
        read_roots=("/outside",),
        summary="Approval required: Merge PDFs",
        expires_at=utc_now() + timedelta(hours=1),
    )
    assert again.request_key == first.request_key  # deduped while pending
    assert execution.pending_approval_center_count() == 1

    decided = execution.decide_approval_center_request(
        first.request_key, approved=True, decided_by="test"
    )
    assert decided.status == "approved"
    found = execution.find_decided_approval_center_request(
        task.id,
        action="read_external_inputs",
        tool_id="pdf.merge_folders",
        arguments_sha256=digest,
        read_roots=("/outside",),
    )
    assert found is not None and found.request_key == first.request_key
    consumed = execution.consume_approval_center_request(first.request_key)
    assert consumed.status == "consumed"
    with pytest.raises(StorageError):
        execution.consume_approval_center_request(first.request_key)
    with pytest.raises(StorageError):
        execution.decide_approval_center_request(
            first.request_key, approved=True, decided_by="test"
        )

    stale = execution.create_approval_center_request(
        task.id,
        action="read_external_inputs",
        tool_id="pdf.merge_folders",
        arguments_sha256=arguments_digest({"output_path": "y.pdf"}),
        read_roots=(),
        summary="stale",
        expires_at=utc_now() - timedelta(minutes=1),
    )
    statuses = {
        request.request_key: request.status
        for request in execution.list_approval_center_requests(task_id=task.id)
    }
    assert statuses[stale.request_key] == "expired"


def test_approval_center_approve_then_resume_without_callback(
    tmp_path: Path,
) -> None:
    settings, _database, tasks, execution, orchestrator = _setup(tmp_path)
    outside = _outside_pdfs(tmp_path)
    options = PlannerOptions(
        input_folders=(str(outside),), output_path="external-merged.pdf"
    )
    task = tasks.create(Task(instruction="Merge PDFs"))

    with pytest.raises(ApprovalUnavailableError) as excinfo:
        orchestrator.execute(task.id, options=options, approval_callback=None)
    assert "Approval Center request apr-" in str(excinfo.value)
    waiting = tasks.get(task.id)
    assert waiting is not None and waiting.status is TaskStatus.WAITING_FOR_APPROVAL
    pending = execution.list_approval_center_requests(status="pending")
    assert len(pending) == 1
    assert not (settings.workspace_dir / "external-merged.pdf").exists()

    execution.decide_approval_center_request(
        pending[0].request_key, approved=True, decided_by="test-user"
    )
    report = orchestrator.resume(task.id, approval_callback=None, force=True)

    assert report.success, report.error
    assert (settings.workspace_dir / "external-merged.pdf").is_file()
    decided = execution.get_approval_center_request(pending[0].request_key)
    assert decided is not None and decided.status == "consumed"
    approvals = execution.list_approvals(task.id)
    assert approvals and approvals[-1].decision == "approved"
    assert approvals[-1].scope.get("via") == "approval_center"
    assert "arguments" not in approvals[-1].scope
    assert len(str(approvals[-1].scope.get("arguments_sha256"))) == 64


def test_approval_center_deny_then_resume_executes_nothing(tmp_path: Path) -> None:
    settings, _database, tasks, execution, orchestrator = _setup(tmp_path)
    outside = _outside_pdfs(tmp_path)
    options = PlannerOptions(
        input_folders=(str(outside),), output_path="external-merged.pdf"
    )
    task = tasks.create(Task(instruction="Merge PDFs"))
    with pytest.raises(ApprovalUnavailableError):
        orchestrator.execute(task.id, options=options, approval_callback=None)
    pending = execution.list_approval_center_requests(status="pending")
    execution.decide_approval_center_request(
        pending[0].request_key, approved=False, decided_by="test-user"
    )

    report = orchestrator.resume(task.id, approval_callback=None, force=True)

    assert report.task.status is TaskStatus.CANCELLED
    assert not (settings.workspace_dir / "external-merged.pdf").exists()
    approvals = execution.list_approvals(task.id)
    assert approvals and approvals[-1].decision == "denied"


def test_interactive_approval_scope_stores_digest_not_raw_arguments(
    tmp_path: Path,
) -> None:
    _settings, _database, tasks, execution, orchestrator = _setup(tmp_path)
    outside = _outside_pdfs(tmp_path)
    task = tasks.create(Task(instruction="Merge PDFs"))
    report = orchestrator.execute(
        task.id,
        options=PlannerOptions(
            input_folders=(str(outside),), output_path="external-merged.pdf"
        ),
        approval_callback=lambda _request: True,
    )
    assert report.success, report.error
    scope = execution.list_approvals(task.id)[0].scope
    assert "arguments" not in scope
    assert len(str(scope.get("arguments_sha256"))) == 64
    assert execution.list_approval_center_requests() == []


def test_encrypted_backup_round_trip(tmp_path: Path) -> None:
    settings, database, tasks, _execution, _orchestrator = _setup(tmp_path)
    tasks.create(Task(instruction="First task"))
    service = DatabaseMaintenanceService(settings.database_path, settings.data_dir)

    with pytest.raises(PcAgentError):
        service.create_backup("short", passphrase="tiny")
    info = service.create_backup("vault", passphrase="correct horse battery staple")
    assert info.encrypted is True
    assert info.filename.endswith(".db.enc")
    assert not info.path.read_bytes().startswith(b"SQLite format 3")
    listed = {backup.backup_id: backup for backup in service.list_backups()}
    assert listed[info.backup_id].encrypted is True

    with pytest.raises(PcAgentError):
        service.verify_backup(info.backup_id)
    with pytest.raises(PcAgentError):
        service.verify_backup(info.backup_id, passphrase="wrong passphrase")
    assert (
        service.verify_backup(
            info.backup_id, passphrase="correct horse battery staple"
        ).backup_id
        == info.backup_id
    )

    tasks.create(Task(instruction="Second task"))

    def live_task_count() -> int:
        connection = sqlite3.connect(settings.database_path)
        try:
            return int(connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        finally:
            connection.close()

    assert live_task_count() == 2
    result = service.restore_backup(
        info.backup_id, passphrase="correct horse battery staple"
    )
    assert result.restored.backup_id == info.backup_id
    assert result.pre_restore_backup_id
    assert live_task_count() == 1
    assert database.schema_version() == 18


def test_safety_and_approvals_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--root", str(tmp_path), "safety", "status"]) == 0
    assert "emergency_stop=off" in capsys.readouterr().out

    assert main(["--root", str(tmp_path), "safety", "stop", "--reason", "cli-test"]) == 0
    assert "EMERGENCY STOP ENGAGED" in capsys.readouterr().out

    assert main(["--root", str(tmp_path), "safety", "status", "--json"]) == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["emergency_stop"]["engaged"] is True
    assert payload["hard_timeouts"] is True

    assert main(["--root", str(tmp_path), "safety", "resume", "--yes"]) == 0
    assert "cleared" in capsys.readouterr().out

    assert main(["--root", str(tmp_path), "approvals", "list"]) == 0
    assert "No Approval Center requests" in capsys.readouterr().out
    assert main(["--root", str(tmp_path), "approvals", "show", "apr-missing"]) == 1
    capsys.readouterr()


def test_diagnostics_and_release_audit_expose_safety(tmp_path: Path) -> None:
    settings, database, _tasks, _execution, _orchestrator = _setup(tmp_path)
    payload = build_diagnostics(settings, database)
    safety = payload["safety"]
    assert safety["hard_timeouts"] is True
    assert safety["worker_isolation"] is True
    assert safety["emergency_stop"]["engaged"] is False
    assert safety["pending_approvals"] == 0
    assert safety["encrypted_backups"] == 0

    checks = {check.name: check for check in audit_install(settings).checks}
    assert checks["safety_kernel"].status == "pass"
    assert len(checks) == 34

    relaxed, *_rest = _setup(
        tmp_path / "relaxed", environ={"PC_AGENT_SAFETY_HARD_TIMEOUTS": "0"}
    )
    relaxed_checks = {check.name: check for check in audit_install(relaxed).checks}
    assert relaxed_checks["safety_kernel"].status == "warn"


def test_transformation_phase2_scope_and_documentation() -> None:
    assert __version__ == "0.82.12"
    assert LATEST_SCHEMA_VERSION == 18  # phase 3 added app_events
    assert len(build_default_registry().list_tools()) == 69

    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == __version__

    phase_doc = (_ROOT / "docs" / "transformation_phase2.md").read_text(
        encoding="utf-8"
    )
    for phrase in (
        "Safety Kernel",
        "Approval Center",
        "Emergency stop",
        "encrypted",
        "UNVERIFIED",
    ):
        assert phrase in phase_doc
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert "TRANSFORMATION PHASE 2" in readme
    assert "TRANSFORMATION_PHASE_20_COMMAND_CENTER.mjs" in readme
    changelog = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## 0.64.0 - TRANSFORMATION PHASE 2 Safety Kernel" in changelog
    safety_source = (_ROOT / "src" / "pc_agent" / "safety.py").read_text(
        encoding="utf-8"
    )
    assert "class SafetyKernel" in safety_source
    assert "class EmergencyStop" in safety_source
