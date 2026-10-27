"""Phase 5 desktop GUI controller and launcher tests."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from pc_agent import __version__
from pc_agent.cli import build_parser, main
from pc_agent.config import Settings
from pc_agent.errors import PcAgentError, RecoveryError, ToolError
from pc_agent.gui.app import gui_runtime_info
from pc_agent.gui.controller import (
    ApprovalBroker,
    GuiController,
    TaskRunOptions,
    format_approval_request,
    format_execution_report,
    parse_json_object,
    parse_line_list,
)
from pc_agent.orchestrator import ApprovalRequest
from pc_agent.planning import TaskPlan


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PC_AGENT_HOME",
        "PC_AGENT_ENVIRONMENT",
        "PC_AGENT_WORKSPACE_DIR",
        "PC_AGENT_DATA_DIR",
        "PC_AGENT_DATABASE_PATH",
        "PC_AGENT_PROVIDER_ORDER",
        "PC_AGENT_MAX_RECOVERY_ATTEMPTS",
        "PC_AGENT_RECOVERY_BACKOFF_SECONDS",
        "PC_AGENT_RECOVERY_STALE_AFTER_MINUTES",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "OLLAMA_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def make_controller(tmp_path: Path, **kwargs: object) -> GuiController:
    settings = Settings.from_env(tmp_path, environ={})
    return GuiController(settings, **kwargs)  # type: ignore[arg-type]


def test_gui_controller_doctor_summary(tmp_path: Path) -> None:
    controller = make_controller(tmp_path)

    summary = controller.doctor_summary()

    assert __version__ == "0.10.0"
    assert summary.version == "0.10.0"
    assert summary.schema_version == 6
    assert summary.provider_route == ("mock",)
    assert summary.builtin_tools == 23
    assert summary.office_tools == 7
    assert summary.file_tools == 5
    assert summary.tls_verified
    assert summary.tls_ca_certificates > 50
    assert summary.status == "OK"
    assert "PHASE 8 File Operations" in summary.to_text()
    assert "License: state=unconfigured, enforcement=off" in summary.to_text()


def test_gui_controller_runs_and_verifies_pdf_task(tmp_path: Path) -> None:
    controller = make_controller(tmp_path)
    controller.create_pdf_demo()

    report = controller.run_new_task(
        "Merge PDFs from Folder A and Folder B",
        TaskRunOptions(
            input_folders=("Folder A", "Folder B"),
            output_path="gui-merged.pdf",
        ),
    )

    assert report.success
    assert "gui-merged.pdf" in format_execution_report(report)
    output = tmp_path / "workspace" / "gui-merged.pdf"
    assert output.is_file()

    verify = controller.pdf_verify("gui-merged.pdf", expected_pages=6)
    assert verify.success
    assert verify.output is not None
    assert verify.output["valid"] is True
    assert verify.output["pages"] == 6

    tasks = controller.list_tasks()
    assert [task.id for task in tasks] == [report.task.id]
    details = controller.get_task_details(report.task.id)
    assert details.plan is not None
    assert details.plan["tool_id"] == "pdf.merge_folders"
    assert details.invocations


def test_gui_controller_office_actions(tmp_path: Path) -> None:
    controller = make_controller(tmp_path)
    controller.create_office_demo()

    converted = controller.office_csv_to_json(
        "Office Demo/sales.csv", "Office Demo/gui-sales.json"
    )
    inspected = controller.office_inspect("Office Demo/summary.docx")

    assert converted.success
    assert converted.output is not None
    assert converted.output["verified"] is True
    assert (tmp_path / "workspace" / "Office Demo" / "gui-sales.json").is_file()
    assert inspected.success
    assert inspected.output is not None
    assert inspected.output["format"] == "docx"


def test_gui_controller_recovers_failed_task_once(tmp_path: Path) -> None:
    controller = make_controller(tmp_path)
    controller.create_pdf_demo()
    blocked = tmp_path / "workspace" / "blocked.pdf"
    blocked.write_bytes(b"existing")

    failed = controller.run_new_task(
        "Merge PDFs from Folder A and Folder B",
        TaskRunOptions(
            input_folders=("Folder A", "Folder B"),
            output_path="blocked.pdf",
            max_recovery_attempts=0,
        ),
    )
    assert not failed.success

    diagnosis = controller.diagnose_task(failed.task.id)
    assert diagnosis.diagnosis is not None
    assert diagnosis.diagnosis.retryable

    outcome = controller.recover_task(failed.task.id)
    assert outcome.cloned
    assert outcome.report is not None and outcome.report.success

    with pytest.raises(RecoveryError, match="already recovered successfully"):
        controller.recover_task(failed.task.id)


def test_gui_desktop_open_uses_injected_opener(tmp_path: Path) -> None:
    opened: list[str] = []
    controller = make_controller(tmp_path, desktop_opener=opened.append)
    target = controller.settings.workspace_dir / "hello.txt"
    target.write_text("hello\n", encoding="utf-8")

    path_result = controller.open_workspace_path("hello.txt")
    url_result = controller.open_url("https://example.com/")

    assert path_result.success
    assert url_result.success
    assert opened[0].endswith("hello.txt")
    assert opened[1] == "https://example.com/"


def test_gui_web_output_validation_happens_before_fetch(tmp_path: Path) -> None:
    controller = make_controller(tmp_path)
    existing = controller.settings.workspace_dir / "example.txt"
    existing.write_text("already here\n", encoding="utf-8")

    with pytest.raises(PcAgentError, match="already exists"):
        controller.fetch_web("https://example.com/", output_path="example.txt")
    with pytest.raises(PcAgentError, match="inside the workspace"):
        controller.fetch_web("https://example.com/", output_path="../outside.txt")
    with pytest.raises(PcAgentError, match=r"\.txt or \.md"):
        controller.fetch_web("https://example.com/", output_path="page.exe")


def test_gui_dynamic_tool_invocation_rejects_builtins(tmp_path: Path) -> None:
    controller = make_controller(tmp_path)

    with pytest.raises(ToolError, match="approved dynamic"):
        controller.invoke_dynamic_tool("workspace.list_files", {})

    assert parse_line_list(" Folder A \n\nFolder B\n") == (
        "Folder A",
        "Folder B",
    )
    assert parse_json_object('{"text": "one two"}') == {"text": "one two"}
    with pytest.raises(PcAgentError, match="JSON object"):
        parse_json_object("[1, 2, 3]")


def test_gui_task_run_options_validation() -> None:
    with pytest.raises(PcAgentError, match="Expected pages"):
        TaskRunOptions(expected_pages=0)
    with pytest.raises(PcAgentError, match="Max recovery attempts"):
        TaskRunOptions(max_recovery_attempts=4)


def test_approval_broker_bridges_worker_decision() -> None:
    plan = TaskPlan(
        intent="merge_pdfs",
        tool_id="pdf.merge_folders",
        arguments={"output_path": "merged.pdf"},
        summary="Merge PDFs from one external folder.",
        planner="test",
    )
    request = ApprovalRequest(
        task_id="task-123",
        plan=plan,
        read_roots=(Path("D:/Example Inputs"),),
    )
    broker = ApprovalBroker(timeout_seconds=5)
    result: dict[str, bool] = {}

    def worker() -> None:
        result["approved"] = broker.decide(request)

    thread = threading.Thread(target=worker)
    thread.start()
    ticket = None
    for _ in range(100):
        ticket = broker.poll()
        if ticket is not None:
            break
        thread.join(timeout=0.01)

    assert ticket is not None
    assert ticket.request is request
    assert "D:" in format_approval_request(request)
    broker.resolve(ticket, approved=True)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert result == {"approved": True}


def test_gui_provider_mock_ask(tmp_path: Path) -> None:
    controller = make_controller(tmp_path)

    routed = controller.ask_provider("Reply briefly: provider test successful")

    assert routed.response.provider == "mock"
    assert routed.response.content
    assert routed.attempts[0].success


def test_cli_gui_check_and_doctor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    available, _detail = gui_runtime_info()
    expected = 0 if available else 1

    assert main(["--root", str(tmp_path), "gui", "--check"]) == expected
    check_output = capsys.readouterr().out
    assert "Self-Healing PC Agent GUI 0.10.0" in check_output
    assert "schema v6" in check_output
    assert "License: state=unconfigured, enforcement=off" in check_output

    assert main(["--root", str(tmp_path), "doctor"]) == 0
    doctor_output = capsys.readouterr().out
    assert "PHASE 8 File Operations" in doctor_output
    assert "Desktop GUI:" in doctor_output
    assert "gui" in build_parser().format_help()


def test_gui_window_constructs_when_display_is_available(tmp_path: Path) -> None:
    import os

    if os.name != "nt" and not os.environ.get("DISPLAY"):
        pytest.skip("No desktop display is available for a real Tk window.")
    available, detail = gui_runtime_info()
    if not available:
        pytest.skip(detail)

    from pc_agent.gui.app import PcAgentGuiApp

    settings = Settings.from_env(tmp_path, environ={})
    app = PcAgentGuiApp(settings)
    try:
        app.root.update_idletasks()
        assert len(app.notebook.tabs()) == 7
        assert app.root.winfo_reqwidth() > 900
        assert app.root.winfo_reqheight() > 500
    finally:
        app.broker.close()
        app.root.destroy()
