"""Phase 1C command-line interface.

Adds provider inspection (`providers`, `ask`) and natural-language task
execution (`run`, `task run`) to the Phase 1A/1B commands. Supported tasks
still pass through stored state transitions, registered tools, applicable
approvals and deterministic verification before success is reported.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings
from .documents.pdf import create_demo_pdfs
from .domain.task import Task
from .errors import ApprovalUnavailableError, PcAgentError
from .logging_config import configure_logging
from .orchestrator import ApprovalRequest, ExecutionReport, TaskOrchestrator
from .planning import DeterministicPlanner, PlannerOptions
from .providers import CompletionRequest, build_model_router, provider_statuses
from .storage.db import Database
from .storage.repositories import ExecutionRepository, TaskRepository
from .tools import ToolContext, ToolResult, build_default_registry


def _add_execution_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--input-folder",
        action="append",
        default=[],
        help="Exact input folder (repeatable; external folders need task approval)",
    )
    parser.add_argument(
        "--input-file",
        action="append",
        default=[],
        help="Exact input file (repeatable; external files need task approval)",
    )
    parser.add_argument("--output", default=None, help="Output path inside the workspace")
    parser.add_argument("--target", default=None, help="Exact target file/folder for the task")
    parser.add_argument("--expected-pages", type=int, default=None)
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument(
        "--use-ai",
        action="store_true",
        help="If deterministic planning cannot handle the instruction, ask the configured provider route for a constrained plan",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Approve the exact read-only external folders shown in the generated plan",
    )
    parser.add_argument("--json", action="store_true", help="Print the execution report as JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pc-agent",
        description="Self-Healing PC Agent - local core (PHASE 1C providers + orchestration)",
    )
    parser.add_argument("--version", action="version", version=f"pc-agent {__version__}")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Application root folder (default: PC_AGENT_HOME or current folder)",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("doctor", help="Check configuration, folders and the local database")

    task_parser = subparsers.add_parser("task", help="Manage and execute local task records")
    task_subparsers = task_parser.add_subparsers(dest="task_command")
    create_parser = task_subparsers.add_parser("create", help="Record a new queued task")
    create_parser.add_argument("instruction", nargs="+", help="The task instruction text")
    list_parser = task_subparsers.add_parser("list", help="List recent local task records")
    list_parser.add_argument("--limit", type=int, default=20)
    show_parser = task_subparsers.add_parser("show", help="Show one task record")
    show_parser.add_argument("task_id")
    run_task_parser = task_subparsers.add_parser("run", help="Execute a queued task by id")
    run_task_parser.add_argument("task_id")
    _add_execution_options(run_task_parser)

    run_parser = subparsers.add_parser(
        "run", help="Create and execute a natural-language task"
    )
    run_parser.add_argument("instruction", nargs="+", help="The natural-language instruction")
    _add_execution_options(run_parser)

    providers_parser = subparsers.add_parser(
        "providers", help="Show configured AI provider slots and fallback route (no network call)"
    )
    providers_subparsers = providers_parser.add_subparsers(dest="providers_command")
    providers_subparsers.add_parser("list", help="List provider statuses without secrets")

    ask_parser = subparsers.add_parser(
        "ask", help="Ask the configured provider route (uses the network unless only mock is available)"
    )
    ask_parser.add_argument("prompt", nargs="+", help="Question or prompt")
    ask_parser.add_argument("--system", default=None, help="Optional system instruction")
    ask_parser.add_argument("--temperature", type=float, default=0.2)
    ask_parser.add_argument("--max-tokens", type=int, default=1024)
    ask_parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="Use only the first provider in the configured route",
    )

    tools_parser = subparsers.add_parser("tools", help="Inspect the registered built-in tools")
    tools_subparsers = tools_parser.add_subparsers(dest="tools_command")
    tools_subparsers.add_parser("list", help="List registered tools and versions")

    demo_parser = subparsers.add_parser("demo", help="Create deterministic local demo data")
    demo_subparsers = demo_parser.add_subparsers(dest="demo_command")
    demo_subparsers.add_parser(
        "pdf", help="Create Folder A/Folder B demo PDFs inside the workspace"
    )

    pdf_parser = subparsers.add_parser("pdf", help="Run deterministic PDF tools")
    pdf_subparsers = pdf_parser.add_subparsers(dest="pdf_command")
    inspect_parser = pdf_subparsers.add_parser("inspect", help="Inspect a workspace PDF")
    inspect_parser.add_argument("path", help="PDF path relative to the workspace")
    verify_parser = pdf_subparsers.add_parser("verify", help="Verify a workspace PDF")
    verify_parser.add_argument("path", help="PDF path relative to the workspace")
    verify_parser.add_argument("--expected-pages", type=int, default=None)
    merge_parser = pdf_subparsers.add_parser("merge", help="Merge explicit workspace PDFs")
    merge_parser.add_argument("inputs", nargs="+", help="Input PDFs relative to the workspace")
    merge_parser.add_argument("--output", required=True, help="New output PDF in the workspace")
    folders_parser = pdf_subparsers.add_parser(
        "merge-folders", help="Merge PDFs found in selected workspace folders"
    )
    folders_parser.add_argument("folders", nargs="+", help="Folders relative to the workspace")
    folders_parser.add_argument("--output", required=True, help="New output PDF in the workspace")
    folders_parser.add_argument("--recursive", action="store_true")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    try:
        settings = Settings.from_env(root_dir=args.root)
    except PcAgentError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)

    try:
        if args.command == "doctor":
            return _run_doctor(settings)
        if args.command == "task":
            return _run_task_command(settings, args)
        if args.command == "run":
            return _run_new_task(settings, args)
        if args.command == "providers":
            return _run_providers_command(settings, args)
        if args.command == "ask":
            return _run_ask_command(settings, args)
        if args.command == "tools":
            return _run_tools_command(args)
        if args.command == "demo":
            return _run_demo_command(settings, args)
        if args.command == "pdf":
            return _run_pdf_command(settings, args)
    except ApprovalUnavailableError as exc:
        print(f"Approval needed: {exc}", file=sys.stderr)
        return 3
    except PcAgentError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 0


def _open_database(settings: Settings) -> Database:
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    return database


def _run_doctor(settings: Settings) -> int:
    database = _open_database(settings)
    summary = settings.public_summary()
    route = build_model_router(settings).provider_names
    print(f"Self-Healing PC Agent {__version__} (PHASE 1C providers + orchestration)")
    print(f"Python: {platform.python_version()}")
    print(f"Environment: {summary['environment']}")
    print(f"Root: {summary['root']}")
    print(f"Workspace: {summary['workspace']}")
    print(f"Database: {summary['database']} (schema v{database.schema_version()})")
    print(f"Provider order: {summary['provider_order']}")
    print(f"Providers with credentials: {summary['providers_with_credentials']}")
    print(f"Usable provider route: {' -> '.join(route)}")
    print("Note: deterministic supported tasks run without an AI provider call.")
    print("Status: OK")
    return 0


def _run_task_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.task_command is None:
        print("Usage: pc-agent task create|list|show|run ...", file=sys.stderr)
        return 2

    if args.task_command == "run":
        return _execute_task(settings, args.task_id, args)

    database = _open_database(settings)
    repository = TaskRepository(database)

    if args.task_command == "create":
        task = repository.create(Task(instruction=" ".join(args.instruction)))
        print(f"Task recorded: {task.id} [{task.status.value}]")
        print(f"This task has only been queued locally. Execute it with: pc-agent task run {task.id}")
        return 0

    if args.task_command == "list":
        tasks = repository.list(limit=args.limit)
        if not tasks:
            print("No tasks recorded yet.")
            return 0
        for task in tasks:
            print(f"{task.id}  {task.status.value:<10}  {task.instruction}")
        return 0

    if args.task_command == "show":
        task = repository.get(args.task_id)
        if task is None:
            print(f"Task not found: {args.task_id}", file=sys.stderr)
            return 1
        print(f"Id: {task.id}")
        print(f"Status: {task.status.value}")
        print(f"Instruction: {task.instruction}")
        print(f"Created: {task.created_at.isoformat()}")
        print(f"Updated: {task.updated_at.isoformat() if task.updated_at else '-'}")
        if task.error:
            print(f"Error: {task.error}")
        execution = ExecutionRepository(database)
        plan = execution.get_plan(task.id)
        if plan:
            print(f"Plan: {plan.get('summary', '-')}")
            print(f"Planner: {plan.get('planner', '-')}")
            print(f"Tool: {plan.get('tool_id', '-')}")
        invocations = execution.list_tool_invocations(task.id)
        if invocations:
            latest = invocations[-1]
            state = "success" if latest.success else "failed"
            print(f"Latest tool invocation: {latest.tool_id} v{latest.tool_version} [{state}]")
        return 0

    return 2


def _tool_context(settings: Settings) -> ToolContext:
    settings.ensure_directories()
    return ToolContext.for_workspace(settings.workspace_dir)


def _run_tools_command(args: argparse.Namespace) -> int:
    if args.tools_command != "list":
        print("Usage: pc-agent tools list", file=sys.stderr)
        return 2
    for tool in build_default_registry().list_tools():
        state = "enabled" if tool.enabled else "disabled"
        permissions = ",".join(action.value for action in tool.spec.permissions)
        print(
            f"{tool.spec.id} v{tool.spec.version} [{state}; {tool.spec.status.value}; "
            f"{permissions}] - {tool.spec.description}"
        )
    return 0


def _run_demo_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.demo_command != "pdf":
        print("Usage: pc-agent demo pdf", file=sys.stderr)
        return 2
    settings.ensure_directories()
    demo = create_demo_pdfs(settings.workspace_dir)
    print("Demo PDF data is ready in the workspace:")
    for relative_path in demo.created:
        suffix = " (deliberately corrupt; must be skipped)" if relative_path == demo.corrupt_file else ""
        print(f"  {relative_path}{suffix}")
    print(f"Expected merged page count: {demo.expected_pages}")
    print('Try: pc-agent pdf merge-folders "Folder A" "Folder B" --output merged.pdf')
    return 0


def _report_tool_failure(result: ToolResult) -> int:
    print(f"Tool failed: {result.error}", file=sys.stderr)
    return 1


def _run_pdf_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.pdf_command is None:
        print("Usage: pc-agent pdf inspect|verify|merge|merge-folders ...", file=sys.stderr)
        return 2

    registry = build_default_registry()
    context = _tool_context(settings)

    if args.pdf_command == "inspect":
        result = registry.invoke("pdf.inspect", {"path": args.path}, context)
        if not result.success or result.output is None:
            return _report_tool_failure(result)
        print(f"Path: {result.output['path']}")
        print(f"Pages: {result.output['pages']}")
        print(f"Size: {result.output['size_bytes']} bytes")
        return 0

    if args.pdf_command == "verify":
        arguments: dict[str, object] = {"path": args.path}
        if args.expected_pages is not None:
            arguments["expected_pages"] = args.expected_pages
        result = registry.invoke("pdf.verify", arguments, context)
        if not result.success or result.output is None:
            return _report_tool_failure(result)
        print(f"Path: {result.output['path']}")
        print(f"Valid: {result.output['valid']}")
        print(f"Pages: {result.output['pages']}")
        print(f"SHA-256: {result.output['sha256']}")
        if result.output["error"]:
            print(f"Error: {result.output['error']}")
        return 0 if result.output["valid"] else 1

    if args.pdf_command == "merge":
        result = registry.invoke(
            "pdf.merge",
            {"input_paths": args.inputs, "output_path": args.output},
            context,
        )
    else:
        result = registry.invoke(
            "pdf.merge_folders",
            {
                "input_folders": args.folders,
                "output_path": args.output,
                "recursive": args.recursive,
            },
            context,
        )
    if not result.success or result.output is None:
        return _report_tool_failure(result)

    print(f"Merged PDF: {result.output['output_path']}")
    print(f"Verified: {result.output['verified']}")
    print(f"Pages: {result.output['page_count']}")
    print("Inputs:")
    for file_path, pages in zip(
        result.output["input_files"], result.output["input_pages"], strict=False
    ):
        print(f"  {file_path} ({pages} pages)")
    skipped = result.output["skipped_files"]
    if skipped:
        print("Skipped:")
        for item in skipped:
            print(f"  {item['path']} - {item['reason']}")
    else:
        print("Skipped: none")
    print(f"SHA-256: {result.output['sha256']}")
    return 0


def _planner_options(args: argparse.Namespace) -> PlannerOptions:
    return PlannerOptions(
        input_folders=tuple(getattr(args, "input_folder", []) or ()),
        input_files=tuple(getattr(args, "input_file", []) or ()),
        output_path=getattr(args, "output", None),
        target_path=getattr(args, "target", None),
        recursive=bool(getattr(args, "recursive", False)),
        expected_pages=getattr(args, "expected_pages", None),
    )


def _approval_callback(*, preapproved: bool):
    def decide(request: ApprovalRequest) -> bool:
        print("Approval required for this exact, read-only external scope:")
        print(f"Task: {request.task_id}")
        print(f"Plan: {request.plan.summary}")
        print("Allowed only for this run (list/read; no write or delete):")
        for root in request.read_roots:
            print(f"  {root}")
        output_path = request.plan.arguments.get("output_path")
        if output_path:
            print(f"Output stays inside the workspace: {output_path}")
        if preapproved:
            print("Decision: APPROVED by --yes for the scope above.")
            return True
        try:
            answer = input("Type APPROVE to continue, anything else to cancel: ")
        except EOFError as exc:
            raise ApprovalUnavailableError(
                "No interactive approval input is available. The task remains "
                "waiting_for_approval; run it again in an interactive terminal or with --yes."
            ) from exc
        approved = answer.strip().upper() == "APPROVE"
        print(f"Decision: {'APPROVED' if approved else 'DENIED'}")
        return approved

    return decide


def _build_orchestrator(settings: Settings) -> TaskOrchestrator:
    database = _open_database(settings)
    return TaskOrchestrator(
        settings=settings,
        task_repository=TaskRepository(database),
        execution_repository=ExecutionRepository(database),
        tool_registry=build_default_registry(),
        deterministic_planner=DeterministicPlanner(settings.workspace_dir),
        model_router=build_model_router(settings),
    )


def _execute_task(settings: Settings, task_id: str, args: argparse.Namespace) -> int:
    settings.ensure_directories()
    report = _build_orchestrator(settings).execute(
        task_id,
        options=_planner_options(args),
        approval_callback=_approval_callback(preapproved=bool(args.yes)),
        use_ai=bool(args.use_ai),
    )
    return _print_execution_report(report, as_json=bool(args.json))


def _run_new_task(settings: Settings, args: argparse.Namespace) -> int:
    settings.ensure_directories()
    database = _open_database(settings)
    task = TaskRepository(database).create(
        Task(instruction=" ".join(args.instruction))
    )
    orchestrator = TaskOrchestrator(
        settings=settings,
        task_repository=TaskRepository(database),
        execution_repository=ExecutionRepository(database),
        tool_registry=build_default_registry(),
        deterministic_planner=DeterministicPlanner(settings.workspace_dir),
        model_router=build_model_router(settings),
    )
    report = orchestrator.execute(
        task.id,
        options=_planner_options(args),
        approval_callback=_approval_callback(preapproved=bool(args.yes)),
        use_ai=bool(args.use_ai),
    )
    return _print_execution_report(report, as_json=bool(args.json))


def _report_to_dict(report: ExecutionReport) -> dict[str, Any]:
    return {
        "task_id": report.task.id,
        "status": report.task.status.value,
        "plan": report.plan.to_dict() if report.plan else None,
        "tool_result": _tool_result_to_dict(report.tool_result),
        "verification": {
            "valid": report.verification_valid,
            "message": report.verification_message,
            "result": _tool_result_to_dict(report.verification_result),
        },
        "missing_information": list(report.missing_information),
        "provider_attempts": [
            {
                "provider": attempt.provider,
                "model": attempt.model,
                "success": attempt.success,
                "attempt": attempt.attempt,
                "error": attempt.error,
                "elapsed_ms": round(attempt.elapsed_ms, 3),
            }
            for attempt in report.provider_attempts
        ],
        "error": report.error,
    }


def _tool_result_to_dict(result: ToolResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "tool_id": result.tool_id,
        "version": result.version,
        "success": result.success,
        "output": dict(result.output) if result.output is not None else None,
        "error": result.error,
        "elapsed_ms": round(result.elapsed_ms, 3),
    }


def _print_execution_report(report: ExecutionReport, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(_report_to_dict(report), indent=2, default=str))
    else:
        print(f"Task: {report.task.id}")
        print(f"Status: {report.task.status.value}")
        if report.plan:
            print(f"Plan: {report.plan.summary}")
            print(f"Planner: {report.plan.planner}")
            print(f"Tool: {report.plan.tool_id}")
        for attempt in report.provider_attempts:
            state = "success" if attempt.success else "failed"
            suffix = f" - {attempt.error}" if attempt.error else ""
            print(f"Provider attempt: {attempt.provider}/{attempt.model} [{state}]{suffix}")
        if report.missing_information:
            print("Needs input:")
            for item in report.missing_information:
                print(f"  - {item}")
        if report.tool_result and report.tool_result.output:
            _print_tool_output(report)
        if report.verification_message:
            print(f"Verification: {report.verification_valid} - {report.verification_message}")
        if report.error and report.task.status.value not in {"needs_input"}:
            print(f"Error: {report.error}", file=sys.stderr)

    if report.success:
        return 0
    if report.task.status.value == "needs_input":
        return 3
    if report.task.status.value == "cancelled":
        return 4
    return 1


def _print_tool_output(report: ExecutionReport) -> None:
    if report.tool_result is None or report.tool_result.output is None:
        return
    output = report.tool_result.output
    tool_id = report.tool_result.tool_id
    if tool_id in {"pdf.merge", "pdf.merge_folders"}:
        print(f"Output PDF (workspace): {output['output_path']}")
        print(f"Pages: {output['page_count']}")
        print("Inputs:")
        for file_path, pages in zip(output["input_files"], output["input_pages"], strict=False):
            print(f"  {file_path} ({pages} pages)")
        skipped = output.get("skipped_files") or []
        if skipped:
            print("Skipped:")
            for item in skipped:
                print(f"  {item['path']} - {item['reason']}")
        else:
            print("Skipped: none")
        print(f"SHA-256: {output['sha256']}")
    elif tool_id == "workspace.list_files":
        print(f"Directory: {output['directory']}")
        for file_path in output["files"]:
            print(f"  {file_path}")
        print(f"Count: {output['count']}")
    elif tool_id in {"pdf.inspect", "pdf.verify"}:
        print(f"Path: {output['path']}")
        if "valid" in output:
            print(f"Valid: {output['valid']}")
        print(f"Pages: {output['pages']}")


def _run_providers_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.providers_command != "list":
        print("Usage: pc-agent providers list", file=sys.stderr)
        return 2
    for status in provider_statuses(settings):
        route = "in-route" if status.in_route else "not-in-route"
        configured = "configured" if status.configured else "not-configured"
        implemented = "adapter" if status.implemented else "reserved"
        print(
            f"{status.name} [{route}; {configured}; {implemented}] "
            f"model={status.model} - {status.detail}"
        )
    route = build_model_router(settings).provider_names
    print(f"Fallback route: {' -> '.join(route)}")
    print("No provider network call was made by this command.")
    return 0


def _run_ask_command(settings: Settings, args: argparse.Namespace) -> int:
    router = build_model_router(settings)
    request = CompletionRequest.from_prompt(
        " ".join(args.prompt),
        system_instruction=args.system,
        temperature=args.temperature,
        max_output_tokens=args.max_tokens,
    )
    routed = router.complete(request, allow_fallback=not args.no_fallback)
    for attempt in routed.attempts:
        state = "success" if attempt.success else "failed"
        print(
            f"Provider: {attempt.provider}/{attempt.model} [{state}]",
            file=sys.stderr,
        )
        if attempt.error:
            print(f"Provider error: {attempt.error}", file=sys.stderr)
    print(routed.response.content)
    return 0
