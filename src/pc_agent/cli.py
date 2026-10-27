"""Phase 7 command-line interface, desktop-GUI launcher and licensing.

Adds the personal-assistant foundation from the revised master prompt -
explicit acceptance criteria, task checkpoints/resume, cooperative
cancellation and inspectable local memory - to the Phase 1 task CLI,
Phase 2 Dynamic Tool Builder, Phase 3 recovery, Phase 4 Office/web/desktop
tools, Phase 5 GUI and Phase 6 offline licensing.  The GUI is loaded lazily
and delegates to the same services; network and desktop-open actions still
require an exact direct user action, while recovery remains deterministic,
recorded and policy-bound.  License enforcement defaults to off.
"""

from __future__ import annotations

import argparse
import copy
import json
import platform
import ssl
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings
from .documents.office import create_demo_office_files
from .documents.pdf import create_demo_pdfs
from .domain.task import Task, TaskStatus
from .errors import ApprovalUnavailableError, PcAgentError, ToolBuilderError
from .http_tls import tls_trust_info
from .licensing import (
    FEATURE_CORE_RECOVERY,
    FEATURE_CORE_TASKS,
    FEATURE_DESKTOP,
    FEATURE_DOCUMENTS,
    FEATURE_PROVIDERS,
    FEATURE_TOOL_BUILDER,
    FEATURE_WEB,
    LicenseService,
    LicenseStatus,
)
from .logging_config import configure_logging
from .orchestrator import ApprovalRequest, ExecutionReport, TaskOrchestrator
from .planning import DeterministicPlanner, PlannerOptions
from .providers import CompletionRequest, build_model_router, provider_statuses
from .recovery import RecoveryService, TaskDiagnosis
from .storage.db import Database
from .storage.repositories import (
    DynamicToolRecord,
    DynamicToolRepository,
    ExecutionRepository,
    MemoryRepository,
    PreferenceRepository,
    RecoveryRepository,
    TaskRepository,
)
from .tooling.models import DynamicToolStatus
from .tooling.service import ToolBuilderService, build_runtime_registry
from .tooling.templates import TEMPLATES
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
    parser.add_argument(
        "--pages", default=None, help='PDF page selection for extract tasks, e.g. "1-3,5"'
    )
    parser.add_argument("--find", default=None, help="Exact DOCX text to replace")
    parser.add_argument("--replace", default=None, help="Replacement DOCX text")
    parser.add_argument("--cell", default=None, help="Spreadsheet cell reference, e.g. B2")
    parser.add_argument(
        "--value", default=None, help='Spreadsheet cell value/formula, e.g. "=SUM(A1:A3)"'
    )
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
    parser.add_argument(
        "--max-recovery-attempts",
        type=int,
        choices=[0, 1, 2, 3],
        default=None,
        help="Maximum bounded self-healing retries for this run (default from .env/config)",
    )
    parser.add_argument(
        "--no-recovery",
        action="store_const",
        const=0,
        dest="max_recovery_attempts",
        help="Disable automatic self-healing retries for this run",
    )
    parser.add_argument("--json", action="store_true", help="Print the execution report as JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pc-agent",
        description="Self-Healing PC Agent - personal local core + GUI + licensing (PHASE 10)",
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

    gui_parser = subparsers.add_parser(
        "gui", help="Open the local desktop GUI (same services and policy as the CLI)"
    )
    gui_parser.add_argument(
        "--check",
        action="store_true",
        help="Check GUI/controller readiness without opening a window",
    )

    license_parser = subparsers.add_parser(
        "license", help="Show or manage the local offline license (Phase 6)"
    )
    license_subparsers = license_parser.add_subparsers(dest="license_command")
    license_status_parser = license_subparsers.add_parser(
        "status", help="Show license/trial state, plan and licensed features"
    )
    license_status_parser.add_argument("--json", action="store_true")
    activate_parser = license_subparsers.add_parser(
        "activate", help="Verify and install a signed license token"
    )
    activate_group = activate_parser.add_mutually_exclusive_group(required=True)
    activate_group.add_argument("--file", type=Path, default=None)
    activate_group.add_argument("--key", default=None)
    deactivate_parser = license_subparsers.add_parser(
        "deactivate", help="Remove the locally activated license file"
    )
    deactivate_parser.add_argument("--yes", action="store_true")
    license_subparsers.add_parser(
        "machine-id", help="Show this machine's privacy-preserving license id"
    )

    memory_parser = subparsers.add_parser(
        "memory", help="Inspect and manage local preferences, notes and workflow memory"
    )
    memory_subparsers = memory_parser.add_subparsers(dest="memory_command")
    memory_subparsers.add_parser("stats", help="Show local memory/history counts")
    memory_subparsers.add_parser("prefs", help="List saved user preferences")
    pref_set_parser = memory_subparsers.add_parser(
        "pref-set", help="Save one user preference (never store secrets)"
    )
    pref_set_parser.add_argument("key")
    pref_set_parser.add_argument("value", nargs="+")
    pref_get_parser = memory_subparsers.add_parser(
        "pref-get", help="Show one saved user preference"
    )
    pref_get_parser.add_argument("key")
    pref_delete_parser = memory_subparsers.add_parser(
        "pref-delete", help="Delete one saved user preference"
    )
    pref_delete_parser.add_argument("key")
    memory_subparsers.add_parser("notes", help="List saved memory notes")
    note_set_parser = memory_subparsers.add_parser(
        "note-set", help="Save a memory note under a key (never store secrets)"
    )
    note_set_parser.add_argument("key")
    note_set_parser.add_argument("text", nargs="+")
    note_show_parser = memory_subparsers.add_parser("note-show", help="Show one memory note")
    note_show_parser.add_argument("key")
    note_delete_parser = memory_subparsers.add_parser(
        "note-delete", help="Delete one memory note"
    )
    note_delete_parser.add_argument("key")
    memory_subparsers.add_parser(
        "workflows", help="List verified workflow evidence recorded from successful tasks"
    )
    memory_clear_parser = memory_subparsers.add_parser(
        "clear", help="Delete all preferences, notes and workflow memory (tasks are kept)"
    )
    memory_clear_parser.add_argument("--yes", action="store_true")

    workflow_parser = subparsers.add_parser(
        "workflow", help="Inspect and re-run verified workflows (Phase 10)"
    )
    workflow_subparsers = workflow_parser.add_subparsers(dest="workflow_command")
    workflow_subparsers.add_parser(
        "list", help="List verified workflows that can be re-run"
    )
    workflow_show_parser = workflow_subparsers.add_parser(
        "show", help="Show one verified workflow (key, unique prefix, or latest)"
    )
    workflow_show_parser.add_argument("key")
    workflow_run_parser = workflow_subparsers.add_parser(
        "run",
        help="Re-run a verified workflow as a new task after live revalidation",
    )
    workflow_run_parser.add_argument("key", help="Workflow key, unique prefix, or latest")
    workflow_run_parser.add_argument(
        "--yes",
        action="store_true",
        help="Approve the exact read-only external folders in the stored plan",
    )
    workflow_run_parser.add_argument(
        "--max-recovery-attempts", type=int, choices=[0, 1, 2, 3], default=None
    )
    workflow_run_parser.add_argument("--json", action="store_true")
    workflow_delete_parser = workflow_subparsers.add_parser(
        "delete", help="Delete a verified workflow record (tasks are kept)"
    )
    workflow_delete_parser.add_argument("key")
    workflow_delete_parser.add_argument("--yes", action="store_true")

    task_parser = subparsers.add_parser("task", help="Manage and execute local task records")
    task_subparsers = task_parser.add_subparsers(dest="task_command")
    create_parser = task_subparsers.add_parser("create", help="Record a new queued task")
    create_parser.add_argument("instruction", nargs="+", help="The task instruction text")
    list_parser = task_subparsers.add_parser("list", help="List recent local task records")
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.add_argument(
        "--status",
        choices=[status.value for status in TaskStatus],
        default=None,
        help="Only show tasks in this status",
    )
    show_parser = task_subparsers.add_parser("show", help="Show one task record")
    show_parser.add_argument(
        "task_id", help="Exact task ID, latest, latest-failed or latest-<status>"
    )
    run_task_parser = task_subparsers.add_parser("run", help="Execute a queued task by id")
    run_task_parser.add_argument(
        "task_id", help="Exact task ID, latest, latest-failed or latest-<status>"
    )
    _add_execution_options(run_task_parser)
    diagnose_parser = task_subparsers.add_parser(
        "diagnose", help="Diagnose a failed, stale or interrupted task without changing it"
    )
    diagnose_parser.add_argument(
        "task_id", help="Exact task ID, latest, latest-failed or latest-<status> (no angle brackets)"
    )
    diagnose_parser.add_argument("--json", action="store_true")
    recover_parser = task_subparsers.add_parser(
        "recover", help="Resume a task or create a linked recovery task for failed/interrupted work"
    )
    recover_parser.add_argument(
        "task_id", help="Exact task ID, latest, latest-failed or latest-<status> (no angle brackets)"
    )
    _add_execution_options(recover_parser)
    recover_parser.add_argument(
        "--force",
        action="store_true",
        help="Recover an active-looking task before the stale threshold (only if no other pc-agent process is running it)",
    )
    resume_parser = task_subparsers.add_parser(
        "resume",
        help="Resume an interrupted task in place from its stored plan/checkpoint",
    )
    resume_parser.add_argument(
        "task_id", help="Exact task ID, latest, latest-failed or latest-<status> (no angle brackets)"
    )
    _add_execution_options(resume_parser)
    resume_parser.add_argument(
        "--force",
        action="store_true",
        help="Resume an active-looking task before the stale threshold (only if no other pc-agent process is running it)",
    )
    cancel_parser = task_subparsers.add_parser(
        "cancel", help="Cancel a queued task or request cooperative cancellation of a running one"
    )
    cancel_parser.add_argument(
        "task_id", help="Exact task ID, latest, latest-failed or latest-<status> (no angle brackets)"
    )
    cancel_parser.add_argument("--reason", default=None, help="Optional audit note")
    delete_task_parser = task_subparsers.add_parser(
        "delete", help="Permanently delete one terminal task record and its local history"
    )
    delete_task_parser.add_argument(
        "task_id", help="Exact task ID, latest, latest-failed or latest-<status> (no angle brackets)"
    )
    delete_task_parser.add_argument(
        "--yes", action="store_true", help="Confirm permanent deletion of the local record"
    )

    run_parser = subparsers.add_parser(
        "run", help="Create and execute a natural-language task"
    )
    run_parser.add_argument("instruction", nargs="+", help="The natural-language instruction")
    _add_execution_options(run_parser)

    recovery_parser = subparsers.add_parser(
        "recovery", help="Inspect recovery checkpoints, stale tasks and orphan temp artifacts"
    )
    recovery_subparsers = recovery_parser.add_subparsers(dest="recovery_command")
    scan_parser = recovery_subparsers.add_parser(
        "scan", help="Report stale active tasks and old temporary PDF artifacts (no deletion)"
    )
    scan_parser.add_argument("--older-than-minutes", type=int, default=None)
    scan_parser.add_argument("--json", action="store_true")
    recovery_list_parser = recovery_subparsers.add_parser(
        "list", help="List recent recovery attempts/checkpoints"
    )
    recovery_list_parser.add_argument("--limit", type=int, default=20)
    recovery_show_parser = recovery_subparsers.add_parser(
        "show", help="Show recovery diagnosis and history for one task"
    )
    recovery_show_parser.add_argument(
        "task_id", help="Exact task ID, latest, latest-failed or latest-<status>"
    )
    recovery_show_parser.add_argument("--json", action="store_true")

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

    tools_parser = subparsers.add_parser(
        "tools", help="Inspect and invoke built-in plus approved dynamic tools"
    )
    tools_subparsers = tools_parser.add_subparsers(dest="tools_command")
    tools_subparsers.add_parser("list", help="List registered tools and versions")
    tool_show_parser = tools_subparsers.add_parser("show", help="Show a registered tool contract")
    tool_show_parser.add_argument("tool_id")
    invoke_parser = tools_subparsers.add_parser("invoke", help="Invoke a registered tool")
    invoke_parser.add_argument("tool_id")
    invoke_parser.add_argument("--version", default=None, help="Exact tool version")
    invoke_parser.add_argument(
        "--arguments", default=None, help="JSON object with tool arguments"
    )
    invoke_parser.add_argument(
        "--arguments-file",
        type=Path,
        default=None,
        help="UTF-8 JSON file containing one arguments object",
    )
    invoke_parser.add_argument(
        "--json", action="store_true", help="Print the complete tool result as JSON"
    )
    for command, help_text in (
        ("enable", "Enable the approved dynamic version of a tool"),
        ("disable", "Disable the approved dynamic version without deleting it"),
        ("rollback", "Roll an approved dynamic tool back to its latest tested prior version"),
    ):
        lifecycle_parser = tools_subparsers.add_parser(command, help=help_text)
        lifecycle_parser.add_argument("tool_id")

    builder_parser = subparsers.add_parser(
        "tool-builder",
        help="Propose, validate, sandbox-test and approve pure-compute dynamic tools",
    )
    builder_subparsers = builder_parser.add_subparsers(dest="tool_builder_command")

    suggest_parser = builder_subparsers.add_parser(
        "suggest", help="Search existing tools and suggest a template/tool id"
    )
    suggest_parser.add_argument("requirement", nargs="+", help="Missing capability description")
    suggest_parser.add_argument("--json", action="store_true")

    builder_subparsers.add_parser("templates", help="List reviewed offline tool templates")

    template_parser = builder_subparsers.add_parser(
        "template", help="Create a candidate from a reviewed offline template"
    )
    template_parser.add_argument("template_id", choices=sorted(TEMPLATES))
    template_parser.add_argument("--run-tests", action="store_true")

    import_parser = builder_subparsers.add_parser(
        "import", help="Import one untrusted JSON tool proposal as a candidate"
    )
    import_parser.add_argument("path", type=Path, help="Proposal JSON file")
    import_parser.add_argument("--run-tests", action="store_true")

    generate_parser = builder_subparsers.add_parser(
        "generate", help="Ask a configured real provider for an untrusted candidate"
    )
    generate_parser.add_argument("requirement", nargs="+", help="Missing capability description")
    generate_parser.add_argument("--tool-id", default=None, help="Exact custom.* tool id")
    generate_parser.add_argument("--run-tests", action="store_true")

    candidates_parser = builder_subparsers.add_parser(
        "list", help="List dynamic-tool candidates and lifecycle states"
    )
    candidates_parser.add_argument("--tool-id", default=None)
    candidates_parser.add_argument(
        "--status", choices=[status.value for status in DynamicToolStatus], default=None
    )
    candidates_parser.add_argument("--limit", type=int, default=50)

    search_parser = builder_subparsers.add_parser(
        "search", help="Search dynamic-tool candidates by id, name or description"
    )
    search_parser.add_argument("query", nargs="+")

    candidate_show_parser = builder_subparsers.add_parser(
        "show", help="Inspect one candidate before testing or approval"
    )
    candidate_show_parser.add_argument("candidate", help="Candidate key or tool_id@version")
    candidate_show_parser.add_argument(
        "--show-source", action="store_true", help="Also print the immutable candidate source"
    )
    candidate_show_parser.add_argument("--json", action="store_true")

    candidate_test_parser = builder_subparsers.add_parser(
        "test", help="Run a candidate's tests in the restricted subprocess sandbox"
    )
    candidate_test_parser.add_argument("candidate", help="Candidate key or tool_id@version")

    approve_parser = builder_subparsers.add_parser(
        "approve", help="Approve a tested candidate for runtime registration"
    )
    approve_parser.add_argument("candidate", help="Candidate key or tool_id@version")
    approve_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm approval after reviewing the candidate contract and source",
    )

    reject_parser = builder_subparsers.add_parser(
        "reject", help="Reject a non-approved candidate"
    )
    reject_parser.add_argument("candidate", help="Candidate key or tool_id@version")
    reject_parser.add_argument("--reason", default=None)

    repair_parser = builder_subparsers.add_parser(
        "repair", help="Ask a configured provider for one bounded repair candidate"
    )
    repair_parser.add_argument("candidate", help="Failed candidate key or tool_id@version")
    repair_parser.add_argument(
        "--requirement", default=None, help="Original requirement, if available"
    )
    repair_parser.add_argument("--run-tests", action="store_true")

    office_parser = subparsers.add_parser(
        "office", help="Inspect, extract and convert workspace Office/CSV documents"
    )
    office_subparsers = office_parser.add_subparsers(dest="office_command")
    office_inspect_parser = office_subparsers.add_parser(
        "inspect", help="Inspect a DOCX, XLSX, PPTX or CSV file"
    )
    office_inspect_parser.add_argument("path", help="Office path relative to workspace")
    office_inspect_parser.add_argument("--json", action="store_true")
    office_extract_parser = office_subparsers.add_parser(
        "extract", help="Extract bounded plain text from an Office/CSV file"
    )
    office_extract_parser.add_argument("path", help="Office path relative to workspace")
    office_extract_parser.add_argument("--output", default=None, help="New .txt/.md output")
    office_extract_parser.add_argument("--max-chars", type=int, default=100_000)
    office_extract_parser.add_argument("--json", action="store_true")
    csv_inspect_parser = office_subparsers.add_parser(
        "csv-inspect", help="Inspect CSV columns, sample rows and numeric statistics"
    )
    csv_inspect_parser.add_argument("path", help="CSV path relative to workspace")
    csv_inspect_parser.add_argument("--sample-rows", type=int, default=5)
    csv_inspect_parser.add_argument("--json", action="store_true")
    csv_json_parser = office_subparsers.add_parser(
        "csv-to-json", help="Convert a CSV file to a new JSON array"
    )
    csv_json_parser.add_argument("input", help="CSV path relative to workspace")
    csv_json_parser.add_argument("--output", required=True, help="New JSON output")
    json_csv_parser = office_subparsers.add_parser(
        "json-to-csv", help="Convert a JSON object/array to a new CSV file"
    )
    json_csv_parser.add_argument("input", help="JSON path relative to workspace")
    json_csv_parser.add_argument("--output", required=True, help="New CSV output")
    csv_xlsx_parser = office_subparsers.add_parser(
        "csv-to-xlsx", help="Convert a CSV file to a new verified XLSX workbook"
    )
    csv_xlsx_parser.add_argument("input", help="CSV path relative to workspace")
    csv_xlsx_parser.add_argument("--output", required=True, help="New XLSX output")
    csv_xlsx_parser.add_argument("--sheet-name", default="Sheet1")
    create_docx_parser = office_subparsers.add_parser(
        "create-docx", help="Create a simple verified DOCX from title and paragraphs"
    )
    create_docx_parser.add_argument("--output", required=True, help="New DOCX output")
    create_docx_parser.add_argument("--title", required=True)
    create_docx_parser.add_argument(
        "--paragraph", action="append", default=[], help="Paragraph text (repeatable)"
    )
    docx_replace_parser = office_subparsers.add_parser(
        "docx-replace", help="Replace exact text in a DOCX into a new file"
    )
    docx_replace_parser.add_argument("input", help="DOCX path relative to workspace")
    docx_replace_parser.add_argument("--output", required=True, help="New DOCX output")
    docx_replace_parser.add_argument("--find", required=True, help="Exact text to find")
    docx_replace_parser.add_argument("--replace", required=True, help="Replacement text")
    docx_append_parser = office_subparsers.add_parser(
        "docx-append", help="Append paragraphs to a DOCX into a new file"
    )
    docx_append_parser.add_argument("input", help="DOCX path relative to workspace")
    docx_append_parser.add_argument("--output", required=True, help="New DOCX output")
    docx_append_parser.add_argument(
        "--paragraph", action="append", required=True, help="Paragraph text (repeatable)"
    )
    xlsx_set_parser = office_subparsers.add_parser(
        "xlsx-set", help="Set XLSX cell values/formulas into a new workbook"
    )
    xlsx_set_parser.add_argument("input", help="XLSX path relative to workspace")
    xlsx_set_parser.add_argument("--output", required=True, help="New XLSX output")
    xlsx_set_parser.add_argument("--sheet", default=None, help="Sheet name (default first)")
    xlsx_set_parser.add_argument(
        "--cell",
        action="append",
        required=True,
        dest="cells",
        help='Cell assignment REF=VALUE, e.g. B2==SUM(A1:A3) (repeatable)',
    )
    xlsx_formulas_parser = office_subparsers.add_parser(
        "xlsx-formulas", help="Recompute and list every formula in an XLSX"
    )
    xlsx_formulas_parser.add_argument("input", help="XLSX path relative to workspace")

    web_parser = subparsers.add_parser(
        "web", help="Fetch one user-requested web page as text (no script execution)"
    )
    web_subparsers = web_parser.add_subparsers(dest="web_command")
    web_fetch_parser = web_subparsers.add_parser(
        "fetch", help="Fetch one exact HTTP(S) URL with SSRF/size limits"
    )
    web_fetch_parser.add_argument("url", help="Exact http:// or https:// URL")
    web_fetch_parser.add_argument("--output", default=None, help="New .txt/.md output")
    web_fetch_parser.add_argument("--max-chars", type=int, default=50_000)
    web_fetch_parser.add_argument(
        "--allow-private",
        action="store_true",
        help="Allow loopback/private IPs and non-standard ports for this exact direct fetch",
    )
    web_fetch_parser.add_argument("--json", action="store_true")

    desktop_parser = subparsers.add_parser(
        "desktop", help="Read system info or open one exact workspace item/URL"
    )
    desktop_subparsers = desktop_parser.add_subparsers(dest="desktop_command")
    desktop_info_parser = desktop_subparsers.add_parser(
        "info", help="Show local system and workspace-disk information"
    )
    desktop_info_parser.add_argument("--json", action="store_true")
    desktop_open_parser = desktop_subparsers.add_parser(
        "open", help="Open one workspace document/folder in its default app"
    )
    desktop_open_parser.add_argument("path", help="Workspace-relative file or folder")
    desktop_open_url_parser = desktop_subparsers.add_parser(
        "open-url", help="Open one exact HTTP(S) URL in the default browser"
    )
    desktop_open_url_parser.add_argument("url", help="Exact http:// or https:// URL")

    demo_parser = subparsers.add_parser("demo", help="Create deterministic local demo data")
    demo_subparsers = demo_parser.add_subparsers(dest="demo_command")
    demo_subparsers.add_parser(
        "pdf", help="Create Folder A/Folder B demo PDFs inside the workspace"
    )
    demo_subparsers.add_parser(
        "office", help="Create deterministic CSV/JSON/XLSX/DOCX demo files"
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
    extract_pages_parser = pdf_subparsers.add_parser(
        "extract", help="Extract selected pages into a new PDF"
    )
    extract_pages_parser.add_argument("path", help="PDF path relative to the workspace")
    extract_pages_parser.add_argument(
        "--pages", required=True, help='Page selection like "1-3,5" (1-based)'
    )
    extract_pages_parser.add_argument(
        "--output", required=True, help="New output PDF in the workspace"
    )
    split_parser = pdf_subparsers.add_parser(
        "split", help="Split a PDF into one single-page PDF per page"
    )
    split_parser.add_argument("path", help="PDF path relative to the workspace")
    split_parser.add_argument(
        "--output-folder", required=True, help="Workspace folder for the page PDFs"
    )
    split_parser.add_argument("--prefix", default=None, help="Output name prefix")

    files_parser = subparsers.add_parser(
        "files", help="Run deterministic workspace file operations"
    )
    files_subparsers = files_parser.add_subparsers(dest="files_command")
    copy_parser = files_subparsers.add_parser("copy", help="Copy one workspace file")
    copy_parser.add_argument("source", help="Source file relative to the workspace")
    copy_parser.add_argument("destination", help="New destination file in the workspace")
    move_parser = files_subparsers.add_parser("move", help="Move one workspace file")
    move_parser.add_argument("source", help="Source file relative to the workspace")
    move_parser.add_argument("destination", help="New destination file in the workspace")
    rename_parser = files_subparsers.add_parser("rename", help="Rename one workspace file")
    rename_parser.add_argument("source", help="Source file relative to the workspace")
    rename_parser.add_argument("new_name", help="Plain new file name (no folders)")
    organize_parser = files_subparsers.add_parser(
        "organize", help="Organize a workspace folder into subfolders"
    )
    organize_parser.add_argument(
        "folder", nargs="?", default=".", help="Workspace folder (default: workspace root)"
    )
    organize_parser.add_argument("--by", choices=("category", "extension"), default="category")
    organize_parser.add_argument("--max-files", type=int, default=500)
    search_parser = files_subparsers.add_parser("search", help="Search workspace files")
    search_parser.add_argument("query", help="Name substring/wildcard or content text")
    search_parser.add_argument(
        "--folder", default=".", help="Workspace folder to search (default: workspace root)"
    )
    search_parser.add_argument("--content", action="store_true", help="Also search text content")
    search_parser.add_argument("--max-results", type=int, default=100)

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
        if args.command == "gui":
            return _run_gui_command(settings, args)
        if args.command == "license":
            return _run_license_command(settings, args)
        if args.command == "memory":
            return _run_memory_command(settings, args)
        if args.command == "workflow":
            return _run_workflow_command(settings, args)
        if args.command == "task":
            return _run_task_command(settings, args)
        if args.command == "run":
            return _run_new_task(settings, args)
        if args.command == "recovery":
            return _run_recovery_command(settings, args)
        if args.command == "providers":
            return _run_providers_command(settings, args)
        if args.command == "ask":
            return _run_ask_command(settings, args)
        if args.command == "tools":
            return _run_tools_command(settings, args)
        if args.command == "tool-builder":
            return _run_tool_builder_command(settings, args)
        if args.command == "office":
            return _run_office_command(settings, args)
        if args.command == "web":
            return _run_web_command(settings, args)
        if args.command == "desktop":
            return _run_desktop_command(settings, args)
        if args.command == "demo":
            return _run_demo_command(settings, args)
        if args.command == "pdf":
            return _run_pdf_command(settings, args)
        if args.command == "files":
            return _run_files_command(settings, args)
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


def _license_service(
    settings: Settings, *, database: Database | None = None
) -> LicenseService:
    return LicenseService(settings, database or _open_database(settings))


def _require_license(settings: Settings, feature: str) -> None:
    """Phase 6 feature gate. A no-op while enforcement stays off (default)."""

    if settings.license_enforcement == "off":
        return
    _license_service(settings).require(
        feature,
        warn=lambda message: print(f"License warning: {message}", file=sys.stderr),
    )


def _print_license_status(status: LicenseStatus, *, machine_id: str) -> None:
    print(
        f"License state: {status.state} "
        f"(enforcement={status.enforcement}, configured={status.configured})"
    )
    print(f"Detail: {status.detail}")
    if status.plan:
        print(f"Plan: {status.plan}")
    if status.licensee:
        print(f"Licensee: {status.licensee}")
    if status.license_id:
        print(f"License ID: {status.license_id}")
    if status.key_id:
        print(f"Key ID: {status.key_id}")
    if status.issued_at:
        print(f"Issued: {status.issued_at.isoformat()}")
    if status.expires_at:
        print(f"Expires: {status.expires_at.isoformat()}")
    elif status.state == "licensed":
        print("Expires: never (perpetual license)")
    if status.days_remaining is not None:
        print(f"Days remaining: {status.days_remaining}")
    print(f"Machine bound: {status.machine_bound}")
    print(f"Licensed features: {', '.join(status.features) or 'none'}")
    print(f"License file: {status.license_file}")
    print(f"Machine ID: {machine_id}")
    if status.enforcement == "off":
        print(
            "Note: license enforcement is off, so nothing is blocked. "
            "Commercial builds set PC_AGENT_LICENSE_ENFORCEMENT=warn or enforce."
        )


def _run_license_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.license_command is None:
        print(
            "Usage: pc-agent license status|activate|deactivate|machine-id ...",
            file=sys.stderr,
        )
        return 2
    service = _license_service(settings)
    if args.license_command == "status":
        status = service.status()
        if args.json:
            payload = status.to_dict()
            payload["machine_id"] = service.machine_id()
            print(json.dumps(payload, indent=2))
        else:
            _print_license_status(status, machine_id=service.machine_id())
        return 0
    if args.license_command == "machine-id":
        print(f"Machine ID: {service.machine_id()}")
        print(
            "Share this value with your vendor only if they issue "
            "machine-bound licenses. It is a one-way hash."
        )
        return 0
    if args.license_command == "activate":
        if args.file is not None:
            status = service.activate_file(args.file.expanduser())
        else:
            status = service.activate(str(args.key))
        print("License activated.")
        _print_license_status(status, machine_id=service.machine_id())
        return 0
    if args.license_command == "deactivate":
        if not args.yes:
            try:
                answer = input(
                    "This removes the locally activated license file. "
                    "Type DEACTIVATE to continue: "
                )
            except EOFError as exc:
                raise ApprovalUnavailableError(
                    "Deactivation needs interactive confirmation or --yes."
                ) from exc
            if answer.strip() != "DEACTIVATE":
                print("Deactivation cancelled; the license file is unchanged.")
                return 1
        status = service.deactivate()
        print("License deactivated (local license file removed).")
        _print_license_status(status, machine_id=service.machine_id())
        return 0
    return 2


_SECRET_MEMORY_WORDS = ("api_key", "apikey", "token", "password", "secret", "credential")


def _reject_secret_memory_key(key: str) -> None:
    lowered = key.strip().lower()
    if any(word in lowered for word in _SECRET_MEMORY_WORDS):
        raise PcAgentError(
            "Refusing to store secret-like data in local memory. "
            "Keep credentials only in your local .env file."
        )


def _run_memory_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.memory_command is None:
        print(
            "Usage: pc-agent memory stats|prefs|pref-set|pref-get|pref-delete|"
            "notes|note-set|note-show|note-delete|workflows|clear ...",
            file=sys.stderr,
        )
        return 2
    database = _open_database(settings)
    preferences = PreferenceRepository(database)
    memory = MemoryRepository(database)
    command = args.memory_command

    if command == "stats":
        tasks = TaskRepository(database)
        execution = ExecutionRepository(database)
        counts = memory.counts()
        print(f"Tasks recorded: {tasks.count()}")
        by_status = ", ".join(
            f"{status.value}={tasks.count(status=status)}" for status in TaskStatus
        )
        print(f"Tasks by status: {by_status}")
        print(f"Task checkpoints: {execution.count_checkpoints()}")
        print(f"Verification reports: {execution.count_verifications()}")
        print(f"Preferences: {len(preferences.all())}")
        print(f"Memory notes: {counts['note']}")
        print(f"Workflow memory records: {counts['workflow']}")
        print(f"Database: {settings.database_path}")
        return 0
    if command == "prefs":
        values = preferences.all()
        if not values:
            print("No preferences saved yet.")
            return 0
        for key, value in values.items():
            print(f"{key} = {value}")
        return 0
    if command == "pref-set":
        _reject_secret_memory_key(args.key)
        preferences.set(args.key.strip(), " ".join(args.value))
        print(f"Preference saved: {args.key.strip()}")
        return 0
    if command == "pref-get":
        value = preferences.get(args.key.strip())
        if value is None:
            print(f"Preference not found: {args.key}", file=sys.stderr)
            return 1
        print(f"{args.key.strip()} = {value}")
        return 0
    if command == "pref-delete":
        if preferences.delete(args.key.strip()):
            print(f"Preference deleted: {args.key.strip()}")
            return 0
        print(f"Preference not found: {args.key}", file=sys.stderr)
        return 1
    if command in {"notes", "workflows"}:
        kind = "note" if command == "notes" else "workflow"
        items = memory.list(kind=kind, limit=100)
        if not items:
            print(f"No {kind} memory recorded yet.")
            return 0
        for item in items:
            extra = ""
            if kind == "workflow":
                extra = (
                    f" tool={item.content.get('tool_id', '-')}"
                    f" successes={item.content.get('success_count', '-')}"
                )
            print(f"{item.key}  {item.title}{extra}  (updated {item.updated_at.isoformat()})")
        return 0
    if command == "note-set":
        _reject_secret_memory_key(args.key)
        text = " ".join(args.text).strip()
        record = memory.upsert(
            kind="note",
            key=args.key.strip(),
            title=text[:80] or args.key.strip(),
            content={"text": text},
        )
        print(f"Note saved: {record.key}")
        return 0
    if command == "note-show":
        record = memory.get("note", args.key.strip())
        if record is None:
            print(f"Note not found: {args.key}", file=sys.stderr)
            return 1
        print(f"Key: {record.key}")
        print(f"Updated: {record.updated_at.isoformat()}")
        print(str(record.content.get("text", "")))
        return 0
    if command == "note-delete":
        if memory.delete("note", args.key.strip()):
            print(f"Note deleted: {args.key.strip()}")
            return 0
        print(f"Note not found: {args.key}", file=sys.stderr)
        return 1
    if command == "clear":
        if not args.yes:
            print(
                "Refusing to clear memory without --yes. This deletes preferences, "
                "notes and workflow records (task history is kept).",
                file=sys.stderr,
            )
            return 1
        removed_preferences = preferences.clear()
        removed_items = memory.clear()
        print(
            f"Memory cleared: preferences={removed_preferences}, items={removed_items}. "
            "Task history was kept."
        )
        return 0
    return 2


def _run_gui_command(settings: Settings, args: argparse.Namespace) -> int:
    # Import the GUI lazily so ordinary CLI commands do not load Tkinter.
    from .gui.app import gui_check, launch_gui

    if args.check:
        ready, text = gui_check(settings)
        print(text)
        return 0 if ready else 1
    return launch_gui(settings)


def _run_doctor(settings: Settings) -> int:
    database = _open_database(settings)
    summary = settings.public_summary()
    route = build_model_router(settings).provider_names
    approved_dynamic = DynamicToolRepository(database).approved_candidates()
    enabled_dynamic = sum(1 for record in approved_dynamic if record.enabled)
    recovery = RecoveryRepository(database)
    service = _build_recovery_service(settings, database=database)
    scan = service.scan()
    builtin_tools = build_default_registry().list_tools()
    office_tools = sum(1 for tool in builtin_tools if tool.spec.id.startswith("office."))
    file_tools = sum(1 for tool in builtin_tools if tool.spec.id.startswith("files."))
    bundled_example = (
        settings.root_dir
        / "examples"
        / "dynamic-tools"
        / "word_count_arguments.json"
    )
    print(f"Self-Healing PC Agent {__version__} (PHASE 10 Workflow Reuse)")
    print(f"Python: {platform.python_version()}")
    print(f"Environment: {summary['environment']}")
    print(f"Root: {summary['root']}")
    print(f"Workspace: {summary['workspace']}")
    print(f"Database: {summary['database']} (schema v{database.schema_version()})")
    print(f"Provider order: {summary['provider_order']}")
    print(f"Providers with credentials: {summary['providers_with_credentials']}")
    print(f"Usable provider route: {' -> '.join(route)}")
    print(
        "Approved dynamic tools: "
        f"{len(approved_dynamic)} ({enabled_dynamic} enabled)"
    )
    print(
        "Bundled dynamic-tool example arguments: "
        + (
            "present"
            if bundled_example.is_file()
            else "missing (embedded fallback available; rerun the latest patcher)"
        )
    )
    print(
        "Recovery: "
        f"max {settings.max_recovery_attempts} healing attempt(s), "
        f"recorded attempts={recovery.count_attempts()}, "
        f"stale tasks={len(scan.stale_tasks)}, "
        f"orphan temp PDFs={len(scan.orphan_temp_pdfs)}"
    )
    print(
        "Built-in tools: "
        f"built-ins={len(builtin_tools)}, office={office_tools}, files={file_tools}, "
        "web.fetch=public-only by default, desktop=open/info only"
    )
    print(
        "Web limits: "
        f"timeout={settings.web_timeout_seconds:.1f}s, max_bytes={settings.web_max_bytes}"
    )
    tls_ok = False
    try:
        tls_info = tls_trust_info()
        tls_sources = ["system default"]
        if tls_info.certifi_loaded:
            tls_sources.append("Certifi Mozilla bundle")
        if tls_info.windows_certificates_loaded:
            tls_sources.append(
                f"Windows store (+{tls_info.windows_certificates_loaded})"
            )
        tls_ok = tls_info.ca_certificates > 0
        print(
            "TLS trust: "
            f"verified; CA certificates={tls_info.ca_certificates}; "
            f"sources={', '.join(tls_sources)}"
        )
    except (OSError, ssl.SSLError, ValueError) as exc:
        print(
            "TLS trust: unavailable "
            f"({exc.__class__.__name__}); HTTPS will fail closed"
        )
    try:
        from .gui.app import gui_runtime_info

        gui_available, gui_detail = gui_runtime_info()
    except Exception as exc:  # noqa: BLE001 - doctor should report, not crash.
        gui_available, gui_detail = False, f"{exc.__class__.__name__}: {exc}"
    print(
        "Desktop GUI: "
        f"{'available' if gui_available else 'unavailable'} ({gui_detail}); "
        "launch: pc-agent gui"
    )
    try:
        license_status = LicenseService(settings, database).status()
        print(f"License: {license_status.summary}")
    except PcAgentError as exc:
        print(f"License: unavailable ({exc}); run: pc-agent license status")
    memory_counts = MemoryRepository(database).counts()
    execution_repo = ExecutionRepository(database)
    print(
        "Memory: "
        f"preferences={len(PreferenceRepository(database).all())}, "
        f"notes={memory_counts['note']}, workflows={memory_counts['workflow']}, "
        f"checkpoints={execution_repo.count_checkpoints()}, "
        f"verifications={execution_repo.count_verifications()}"
    )
    print("Note: deterministic supported tasks run without an AI provider call.")
    if scan.stale_tasks or scan.orphan_temp_pdfs:
        print("Status: ATTENTION - run: pc-agent recovery scan")
    elif not tls_ok:
        print("Status: ATTENTION - no TLS CA certificates could be loaded")
    else:
        print("Status: OK")
    return 0


def _resolve_task_reference(settings: Settings, reference: str) -> str:
    """Resolve exact IDs plus copy-friendly latest/latest-failed aliases."""

    value = reference.strip()
    if len(value) >= 2 and value.startswith("<") and value.endswith(">"):
        raise PcAgentError(
            "That looks like a documentation placeholder. Copy the exact Task ID "
            "printed by the previous command, or use latest-failed without angle brackets."
        )

    repository = TaskRepository(_open_database(settings))
    exact = repository.get(value) or repository.get(value.lower())
    if exact is not None:
        return exact.id

    normalized = value.lower().replace("_", "-")
    status: TaskStatus | None = None
    if normalized == "latest":
        status = None
    elif normalized.startswith("latest-"):
        status_value = normalized.removeprefix("latest-").replace("-", "_")
        try:
            status = TaskStatus(status_value)
        except ValueError:
            return value
    else:
        return value

    tasks = repository.list(limit=1, status=status)
    if not tasks:
        label = status.value if status else "any status"
        raise PcAgentError(f"No task was found for alias {value!r} ({label}).")
    task = tasks[0]
    print(
        f"Resolved {value} -> {task.id} [{task.status.value}]",
        file=sys.stderr,
    )
    return task.id


def _run_task_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.task_command is None:
        print(
            "Usage: pc-agent task create|list|show|run|diagnose|recover|resume|cancel|delete ...",
            file=sys.stderr,
        )
        return 2

    if args.task_command == "run":
        return _execute_task(
            settings, _resolve_task_reference(settings, args.task_id), args
        )
    if args.task_command == "diagnose":
        diagnosis = _build_recovery_service(settings).diagnose(
            _resolve_task_reference(settings, args.task_id)
        )
        if args.json:
            print(json.dumps(diagnosis.to_dict(), indent=2, default=str))
        else:
            _print_task_diagnosis(diagnosis)
        return 0
    if args.task_command == "recover":
        return _recover_task(
            settings, _resolve_task_reference(settings, args.task_id), args
        )
    if args.task_command == "resume":
        return _resume_task(
            settings, _resolve_task_reference(settings, args.task_id), args
        )
    if args.task_command == "cancel":
        resolved_id = _resolve_task_reference(settings, args.task_id)
        task, action = _build_orchestrator(settings).request_cancellation(
            resolved_id, reason=args.reason
        )
        if action == "cancelled":
            print(f"Task cancelled: {task.id} [{task.status.value}]")
        else:
            print(
                f"Cancellation requested for task {task.id} "
                f"(currently {task.status.value}). It will stop at the next "
                "stage boundary; check with: pc-agent task show " + task.id
            )
        return 0
    if args.task_command == "delete":
        if not args.yes:
            print(
                "Refusing to delete a task record without --yes. Deletion removes "
                "the local task history only; workspace files are never deleted.",
                file=sys.stderr,
            )
            return 1
        resolved_id = _resolve_task_reference(settings, args.task_id)
        repository = TaskRepository(_open_database(settings))
        existing = repository.get(resolved_id)
        if existing is None:
            print(f"Task not found: {resolved_id}", file=sys.stderr)
            return 1
        if not existing.is_terminal:
            print(
                f"Task {existing.id} is {existing.status.value}, not terminal. "
                "Cancel it first, then delete the record.",
                file=sys.stderr,
            )
            return 1
        repository.delete(existing.id)
        print(f"Task record deleted: {existing.id} (workspace files were not touched)")
        return 0

    database = _open_database(settings)
    repository = TaskRepository(database)

    if args.task_command == "create":
        task = repository.create(Task(instruction=" ".join(args.instruction)))
        print(f"Task recorded: {task.id} [{task.status.value}]")
        print(f"This task has only been queued locally. Execute it with: pc-agent task run {task.id}")
        return 0

    if args.task_command == "list":
        status = TaskStatus(args.status) if args.status else None
        tasks = repository.list(limit=args.limit, status=status)
        if not tasks:
            print("No matching tasks recorded yet.")
            return 0
        for task in tasks:
            print(f"{task.id}  {task.status.value:<10}  {task.instruction}")
        return 0

    if args.task_command == "show":
        resolved_id = _resolve_task_reference(settings, args.task_id)
        task = repository.get(resolved_id)
        if task is None:
            print(f"Task not found: {resolved_id}", file=sys.stderr)
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
        checkpoints = execution.list_checkpoints(task.id)
        if checkpoints:
            latest_checkpoint = checkpoints[-1]
            print(
                f"Checkpoints: {len(checkpoints)} "
                f"(latest: {latest_checkpoint.step_key} at "
                f"{latest_checkpoint.updated_at.isoformat()})"
            )
            if not task.is_terminal:
                print(f"Resume with: pc-agent task resume {task.id}")
        if execution.cancellation_requested(task.id):
            print("Cancellation: requested (takes effect at the next stage boundary)")
        latest_verification = execution.get_latest_verification(task.id)
        if latest_verification is not None:
            criteria = latest_verification.report.get("criteria") or []
            outcome = "passed" if latest_verification.passed else "failed"
            print(f"Acceptance verification: {outcome} ({len(criteria)} criteria)")
            for criterion in criteria:
                if isinstance(criterion, dict):
                    mark = "PASS" if criterion.get("passed") else "FAIL"
                    print(
                        f"  [{mark}] {criterion.get('description', '-')}"
                        f" - {criterion.get('evidence', '')}"
                    )
        recovery = RecoveryRepository(database)
        attempts = recovery.list_attempts(task_id=task.id, limit=100)
        if attempts:
            latest_attempt = attempts[-1]
            print(f"Recovery attempts recorded: {len(attempts)}")
            print(
                "Latest recovery checkpoint: "
                f"#{latest_attempt.attempt_number} [{latest_attempt.status}; "
                f"{latest_attempt.action}; category={latest_attempt.category or '-'}] "
                f"{latest_attempt.message or ''}".rstrip()
            )
        links = recovery.list_links_for(task.id)
        for link in links:
            print(
                "Recovery link: "
                f"{link.source_task_id} -> {link.recovery_task_id}"
                + (f" ({link.reason})" if link.reason else "")
            )
        return 0

    return 2


def _print_task_diagnosis(diagnosis: TaskDiagnosis) -> None:
    task = diagnosis.task
    print(f"Task: {task.id}")
    print(f"Status: {task.status.value}")
    print(f"Instruction: {task.instruction}")
    print(f"Age since update: {diagnosis.age_seconds:.0f}s")
    print(f"Stale/interrupted candidate: {diagnosis.stale}")
    if task.error:
        print(f"Error: {task.error}")
    if diagnosis.plan:
        print(f"Stored plan: {diagnosis.plan.summary}")
        print(f"Stored tool: {diagnosis.plan.tool_id}")
    elif diagnosis.plan_error:
        print(f"Stored plan: invalid ({diagnosis.plan_error})")
    else:
        print("Stored plan: none")
    if diagnosis.latest_invocation:
        latest = diagnosis.latest_invocation
        state = "success" if latest.success else "failed"
        print(f"Latest tool invocation: {latest.tool_id} v{latest.tool_version} [{state}]")
        if latest.error:
            print(f"Latest invocation error: {latest.error}")
    if diagnosis.diagnosis:
        item = diagnosis.diagnosis
        print(f"Diagnosis category: {item.category.value}")
        print(f"Safe recovery action: {item.action.value}")
        print(f"Auto-retryable: {item.retryable}")
        print(f"Diagnosis: {item.message}")
        if item.revised_arguments is not None:
            print(
                "Proposed revised arguments: "
                + json.dumps(item.revised_arguments, sort_keys=True, default=str)
            )
    else:
        print("Diagnosis: no failure diagnosis is needed for this status.")
    if diagnosis.attempts:
        print("Recovery checkpoints:")
        for attempt in diagnosis.attempts:
            print(
                f"  #{attempt.attempt_number} [{attempt.status}; {attempt.stage}; "
                f"{attempt.action}; category={attempt.category or '-'}] "
                f"{attempt.message or ''}".rstrip()
            )
    if diagnosis.links:
        print("Recovery links:")
        for link in diagnosis.links:
            print(f"  {link.source_task_id} -> {link.recovery_task_id}")


def _resume_task(settings: Settings, task_id: str, args: argparse.Namespace) -> int:
    _require_license(settings, FEATURE_CORE_TASKS)
    if bool(args.use_ai):
        _require_license(settings, FEATURE_PROVIDERS)
    settings.ensure_directories()
    try:
        report = _build_orchestrator(settings).resume(
            task_id,
            options=_planner_options(args),
            approval_callback=_approval_callback(preapproved=bool(args.yes)),
            use_ai=bool(args.use_ai),
            max_recovery_attempts=args.max_recovery_attempts,
            force=bool(args.force),
        )
    except KeyboardInterrupt:
        print(
            "Interrupted. The task may be left mid-step; check it with "
            f"'pc-agent task show {task_id}', then resume or cancel it.",
            file=sys.stderr,
        )
        return 130
    return _print_execution_report(report, as_json=bool(args.json))


def _recover_task(settings: Settings, task_id: str, args: argparse.Namespace) -> int:
    _require_license(settings, FEATURE_CORE_RECOVERY)
    if bool(args.use_ai):
        _require_license(settings, FEATURE_PROVIDERS)
    outcome = _build_recovery_service(settings).recover(
        task_id,
        approval_callback=_approval_callback(preapproved=bool(args.yes)),
        options=_planner_options(args),
        use_ai=bool(args.use_ai),
        force=bool(args.force),
        max_recovery_attempts=args.max_recovery_attempts,
    )
    if outcome.cloned:
        print(f"Source task: {outcome.source_task.id} [{outcome.source_task.status.value}]")
        print(f"Recovery task: {outcome.recovery_task.id}")
        print(outcome.message)
    else:
        print(outcome.message)
    if outcome.report is None:
        print("Recovery task was created but not executed.")
        return 0
    return _print_execution_report(outcome.report, as_json=bool(args.json))


def _run_recovery_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.recovery_command is None:
        print("Usage: pc-agent recovery scan|list|show ...", file=sys.stderr)
        return 2

    if args.recovery_command == "scan":
        scan = _build_recovery_service(settings).scan(
            stale_after_minutes=args.older_than_minutes
        )
        if args.json:
            print(
                json.dumps(
                    scan.to_dict(workspace_dir=settings.workspace_dir),
                    indent=2,
                    default=str,
                )
            )
            return 0
        print(f"Stale threshold: {scan.stale_after_minutes} minute(s)")
        if scan.stale_tasks:
            print("Stale/interrupted candidate tasks:")
            for task in scan.stale_tasks:
                print(f"  {task.id}  {task.status.value:<10}  {task.instruction}")
                print(f"    Recover with: pc-agent task recover {task.id}")
        else:
            print("Stale/interrupted candidate tasks: none")
        if scan.orphan_temp_pdfs:
            print("Old temporary PDF artifacts (reported only; nothing was deleted):")
            for path in scan.orphan_temp_pdfs:
                try:
                    shown = path.relative_to(settings.workspace_dir).as_posix()
                except ValueError:
                    shown = str(path)
                print(f"  {shown}")
        else:
            print("Old temporary PDF artifacts: none")
        return 0

    if args.recovery_command == "list":
        attempts = RecoveryRepository(_open_database(settings)).list_attempts(
            limit=args.limit
        )
        if not attempts:
            print("No recovery attempts recorded yet.")
            return 0
        for attempt in reversed(attempts):
            print(
                f"{attempt.task_id}  attempt #{attempt.attempt_number} "
                f"[{attempt.status}; {attempt.stage}; {attempt.action}; "
                f"category={attempt.category or '-'}] {attempt.message or ''}".rstrip()
            )
        return 0

    if args.recovery_command == "show":
        diagnosis = _build_recovery_service(settings).diagnose(
            _resolve_task_reference(settings, args.task_id)
        )
        if args.json:
            print(json.dumps(diagnosis.to_dict(), indent=2, default=str))
        else:
            _print_task_diagnosis(diagnosis)
        return 0

    return 2


def _tool_context(
    settings: Settings,
    *,
    allow_network: bool = False,
    allow_private_network: bool = False,
    allow_desktop_open: bool = False,
) -> ToolContext:
    settings.ensure_directories()
    return ToolContext.for_workspace(
        settings.workspace_dir,
        network_access_allowed=allow_network,
        allow_private_network=allow_private_network,
        desktop_open_allowed=allow_desktop_open,
        web_timeout_seconds=settings.web_timeout_seconds,
        web_max_bytes=settings.web_max_bytes,
    )


def _tool_builder_service(
    settings: Settings, *, with_router: bool = False
) -> ToolBuilderService:
    database = _open_database(settings)
    return ToolBuilderService(
        database,
        data_dir=settings.data_dir,
        model_router=build_model_router(settings) if with_router else None,
    )


def _run_tools_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.tools_command is None:
        print(
            "Usage: pc-agent tools list|show|invoke|enable|disable|rollback ...",
            file=sys.stderr,
        )
        return 2

    if args.tools_command in {"enable", "disable", "rollback"}:
        _require_license(settings, FEATURE_TOOL_BUILDER)
    service = _tool_builder_service(settings)
    if args.tools_command == "enable":
        record = service.set_enabled(args.tool_id, enabled=True)
        print(f"Dynamic tool enabled: {record.tool_id} v{record.version}")
        return 0
    if args.tools_command == "disable":
        record = service.set_enabled(args.tool_id, enabled=False)
        print(f"Dynamic tool disabled: {record.tool_id} v{record.version}")
        return 0
    if args.tools_command == "rollback":
        record = service.rollback(args.tool_id)
        print(f"Dynamic tool rolled back: {record.tool_id} v{record.version}")
        return 0

    registry = build_runtime_registry(
        _open_database(settings), data_dir=settings.data_dir
    )
    if args.tools_command == "list":
        for tool in registry.list_tools():
            state = "enabled" if tool.enabled else "disabled"
            permissions = ",".join(action.value for action in tool.spec.permissions) or "none"
            description = " ".join(tool.spec.description.split())
            print(f"{tool.spec.id} v{tool.spec.version} [{state}; {tool.spec.status.value}]")
            print(f"  permissions={permissions}")
            print(f"  provenance={tool.spec.provenance}")
            for line in textwrap.wrap(description, width=88) or [description]:
                print(f"  {line}")
        return 0

    if args.tools_command == "show":
        tool = registry.get(args.tool_id)
        if tool is None:
            print(f"Tool not found: {args.tool_id}", file=sys.stderr)
            return 1
        print(f"Tool: {tool.spec.id} v{tool.spec.version}")
        print(f"Name: {tool.spec.name}")
        print(f"Description: {' '.join(tool.spec.description.split())}")
        print(f"Enabled: {tool.enabled}")
        print(f"Status: {tool.spec.status.value}")
        print(f"Provenance: {tool.spec.provenance}")
        permissions = ", ".join(action.value for action in tool.spec.permissions) or "none"
        print(f"Permissions: {permissions}")
        print(f"Network required: {tool.spec.network_required}")
        print("Input schema:")
        print(json.dumps(tool.spec.input_schema, indent=2, sort_keys=True))
        print("Output schema:")
        print(json.dumps(tool.spec.output_schema, indent=2, sort_keys=True))
        return 0

    if args.tools_command == "invoke":
        selected_tool = registry.get(args.tool_id)
        if selected_tool is not None and selected_tool.spec.provenance.startswith(
            "dynamic:"
        ):
            _require_license(settings, FEATURE_TOOL_BUILDER)
        arguments = _parse_tool_arguments(settings, args)
        result = registry.invoke(
            args.tool_id,
            arguments,
            _tool_context(settings),
            version=args.version,
        )
        if args.json:
            print(json.dumps(_tool_result_to_dict(result), indent=2, default=str))
        elif result.success and result.output is not None:
            print(json.dumps(result.output, indent=2, sort_keys=True, default=str))
        else:
            return _report_tool_failure(result)
        return 0 if result.success else 1

    return 2


# The Phase 2 smoke-test arguments are also embedded in code. Some Windows
# installs were observed with the project example file missing even though the
# patcher manifest contains it; invocation should still work for this one
# known bundled file and tell the user to rerun the latest patcher.
_BUNDLED_ARGUMENT_EXAMPLES: dict[str, dict[str, Any]] = {
    "examples/dynamic-tools/word_count_arguments.json": {"text": "one two three"},
    "word_count_arguments.json": {"text": "one two three"},
}


def _parse_tool_arguments(
    settings: Settings, args: argparse.Namespace
) -> dict[str, Any]:
    if args.arguments is not None and args.arguments_file is not None:
        raise ToolBuilderError("Use only one of --arguments or --arguments-file.")
    raw: str | None = args.arguments
    if args.arguments_file is not None:
        requested = args.arguments_file.expanduser()
        candidates: list[Path] = []
        if requested.is_absolute():
            candidates.append(requested.resolve())
        else:
            for base in (Path.cwd(), settings.root_dir, settings.workspace_dir):
                candidate = (base / requested).resolve()
                if candidate not in candidates:
                    candidates.append(candidate)

        resolved = next((candidate for candidate in candidates if candidate.is_file()), None)
        if resolved is None:
            normalized = str(requested).replace("\\", "/").strip("/").lower()
            embedded = _BUNDLED_ARGUMENT_EXAMPLES.get(normalized)
            if embedded is None and "/" in normalized:
                embedded = _BUNDLED_ARGUMENT_EXAMPLES.get(normalized.rsplit("/", 1)[-1])
            if embedded is not None:
                print(
                    "Warning: bundled example arguments file was not found; using the "
                    "embedded copy. Rerun the latest patcher to restore the project file.",
                    file=sys.stderr,
                )
                return copy.deepcopy(embedded)
            searched = "; ".join(str(candidate) for candidate in candidates)
            raise ToolBuilderError(
                "Arguments file does not exist. Checked, in order: " + searched
            )

        if resolved.stat().st_size > 1_000_000:
            raise ToolBuilderError("Tool arguments file exceeds the 1 MB limit.")
        try:
            raw = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolBuilderError("Tool arguments file must be valid UTF-8 JSON.") from exc
        except OSError as exc:
            raise ToolBuilderError(f"Could not read tool arguments file: {exc}") from exc
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ToolBuilderError("Tool arguments must be one JSON object.") from exc
    if not isinstance(parsed, dict):
        raise ToolBuilderError("Tool arguments must be one JSON object.")
    return parsed


def _candidate_to_dict(
    record: DynamicToolRecord,
    *,
    include_source: bool = False,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    proposal = record.proposal.to_dict()
    if not include_source:
        proposal.pop("source_code", None)
    return {
        "candidate_key": record.candidate_key,
        "status": record.status,
        "enabled": record.enabled,
        "provenance": record.provenance,
        "generator_model": record.generator_model,
        "repair_attempt": record.repair_attempt,
        "source_sha256": record.proposal.source_sha256,
        "proposal": proposal,
        "static_report": dict(record.static_report),
        "test_report": dict(record.test_report) if record.test_report else None,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "approved_at": record.approved_at.isoformat() if record.approved_at else None,
        "events": events or [],
    }


def _print_candidate_summary(record: DynamicToolRecord) -> None:
    proposal = record.proposal
    print(f"Candidate: {record.candidate_key}")
    print(f"Tool: {record.tool_id} v{record.version}")
    print(f"Name: {proposal.name}")
    print(f"Status: {record.status}")
    print(f"Enabled: {record.enabled}")
    print(f"Provenance: {record.provenance}")
    if record.generator_model:
        print(f"Generator: {record.generator_model}")
    print(f"Repair attempt: {record.repair_attempt}")
    print(f"Dependencies: {', '.join(proposal.dependencies) or 'none'}")
    print("Permissions: none (pure-compute only)")
    print("Network required: False")
    print(f"Source SHA-256: {proposal.source_sha256}")
    static_passed = record.static_report.get("passed") is True
    print(f"Static validation: {'passed' if static_passed else 'failed'}")
    for issue in record.static_report.get("issues", []):
        line = f" line {issue['line']}" if issue.get("line") else ""
        print(f"  [{issue.get('severity')}:{issue.get('code')}{line}] {issue.get('message')}")
    if record.test_report is None:
        print("Sandbox tests: not run")
    else:
        passed = record.test_report.get("passed") is True
        print(f"Sandbox tests: {'passed' if passed else 'failed'}")
        for case in record.test_report.get("cases", []):
            state = "passed" if case.get("passed") else "failed"
            suffix = f" - {case['error']}" if case.get("error") else ""
            print(f"  {case.get('name')}: {state}{suffix}")


def _maybe_test_candidate(
    service: ToolBuilderService, record: DynamicToolRecord, *, run_tests: bool
) -> DynamicToolRecord:
    return service.test_candidate(record.candidate_key) if run_tests else record


def _run_tool_builder_command(settings: Settings, args: argparse.Namespace) -> int:
    command = args.tool_builder_command
    if command is None:
        print(
            "Usage: pc-agent tool-builder suggest|templates|template|import|generate|"
            "list|search|show|test|approve|reject|repair ...",
            file=sys.stderr,
        )
        return 2

    if command == "templates":
        for template_id, proposal in sorted(TEMPLATES.items()):
            print(f"{template_id}: {proposal['tool_id']} - {proposal['description']}")
        return 0

    if command in {"template", "import", "generate", "test", "approve", "reject", "repair"}:
        _require_license(settings, FEATURE_TOOL_BUILDER)

    service = _tool_builder_service(
        settings, with_router=command in {"generate", "repair"}
    )

    if command == "suggest":
        suggestion = service.suggest(" ".join(args.requirement))
        if args.json:
            print(json.dumps(suggestion, indent=2))
            return 0
        print(f"Suggested tool id: {suggestion['suggested_tool_id']}")
        print(f"Suggested template: {suggestion['suggested_template'] or 'none'}")
        if suggestion["builtin_matches"]:
            print("Possible built-in matches:")
            for match in suggestion["builtin_matches"]:
                print(f"  {match['tool_id']} v{match['version']}")
        else:
            print("Possible built-in matches: none")
        if suggestion["dynamic_matches"]:
            print("Possible dynamic candidates:")
            for match in suggestion["dynamic_matches"]:
                print(
                    f"  {match['candidate_key']} {match['tool_id']} "
                    f"v{match['version']} [{match['status']}]"
                )
        else:
            print("Possible dynamic candidates: none")
        print(
            "No candidate was created. Use a template, import a proposal, "
            "or generate one with a configured real provider."
        )
        return 0

    if command == "template":
        record = service.create_from_template(args.template_id)
        record = _maybe_test_candidate(service, record, run_tests=args.run_tests)
        _print_candidate_summary(record)
        return 0

    if command == "import":
        path = args.path.expanduser()
        if not path.is_absolute():
            path = settings.root_dir / path
        record = service.import_proposal_file(path)
        record = _maybe_test_candidate(service, record, run_tests=args.run_tests)
        _print_candidate_summary(record)
        return 0

    if command == "generate":
        record = service.generate_candidate(
            " ".join(args.requirement), tool_id=args.tool_id
        )
        record = _maybe_test_candidate(service, record, run_tests=args.run_tests)
        _print_candidate_summary(record)
        print("Generated code is untrusted until tests pass and you approve it.")
        return 0

    if command == "list":
        records = service.list_candidates(
            tool_id=args.tool_id, status=args.status, limit=args.limit
        )
        if not records:
            print("No dynamic tool candidates recorded yet.")
            return 0
        for record in records:
            enabled = "enabled" if record.enabled else "disabled"
            print(
                f"{record.candidate_key}  {record.tool_id} v{record.version} "
                f"[{record.status}; {enabled}] - {record.proposal.name}"
            )
        return 0

    if command == "search":
        records = service.search_candidates(" ".join(args.query))
        if not records:
            print("No matching dynamic tool candidates.")
            return 0
        for record in records:
            print(
                f"{record.candidate_key}  {record.tool_id} v{record.version} "
                f"[{record.status}] - {record.proposal.description}"
            )
        return 0

    if command == "show":
        record = service.get_candidate(args.candidate)
        if args.json:
            print(
                json.dumps(
                    _candidate_to_dict(
                        record,
                        include_source=args.show_source,
                        events=service.events(args.candidate),
                    ),
                    indent=2,
                )
            )
            return 0
        _print_candidate_summary(record)
        print("Description:")
        print(f"  {record.proposal.description}")
        print("Input schema:")
        print(json.dumps(record.proposal.input_schema, indent=2, sort_keys=True))
        print("Output schema:")
        print(json.dumps(record.proposal.output_schema, indent=2, sort_keys=True))
        print("Test cases:")
        for test in record.proposal.test_cases:
            print(f"  {test.name}: arguments={json.dumps(test.arguments, sort_keys=True)}")
            print(f"    expected={json.dumps(test.expected_output, sort_keys=True)}")
        if args.show_source:
            print("Source:")
            print(record.proposal.normalized_source, end="")
        else:
            print("Source: hidden (use --show-source before approval)")
        return 0

    if command == "test":
        record = service.test_candidate(args.candidate)
        _print_candidate_summary(record)
        return 0 if record.status == DynamicToolStatus.TESTED.value else 1

    if command == "approve":
        record = service.get_candidate(args.candidate)
        if not args.yes:
            print("Current candidate state (before approval):")
            _print_candidate_summary(record)
            print(
                "Approval was NOT recorded. Review the schemas/tests/source with "
                f"'tool-builder show {record.candidate_key} --show-source', then repeat approve with --yes.",
                file=sys.stderr,
            )
            return 2
        approved = service.approve_candidate(record.candidate_key)
        print("Final candidate state (after approval):")
        _print_candidate_summary(approved)
        print(
            f"Approved for runtime use: {approved.tool_id} v{approved.version} "
            f"[{approved.candidate_key}]"
        )
        return 0

    if command == "reject":
        record = service.reject_candidate(args.candidate, reason=args.reason)
        print(f"Candidate rejected: {record.candidate_key}")
        return 0

    if command == "repair":
        record = service.repair_candidate(
            args.candidate, requirement=args.requirement
        )
        record = _maybe_test_candidate(service, record, run_tests=args.run_tests)
        _print_candidate_summary(record)
        print("Repair created a new untrusted version; it is not approved automatically.")
        return 0

    return 2


def _run_demo_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.demo_command == "office":
        settings.ensure_directories()
        office_demo = create_demo_office_files(settings.workspace_dir)
        print("Demo Office data is ready in the workspace:")
        for relative_path in office_demo.created:
            print(f"  {relative_path}")
        print('Try: pc-agent office csv-inspect "Office Demo/sales.csv"')
        print('Try: pc-agent office extract "Office Demo/summary.docx"')
        return 0
    if args.demo_command != "pdf":
        print("Usage: pc-agent demo pdf|office", file=sys.stderr)
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
        print("Usage: pc-agent pdf inspect|verify|merge|merge-folders|extract|split ...", file=sys.stderr)
        return 2
    _require_license(settings, FEATURE_DOCUMENTS)

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

    if args.pdf_command == "extract":
        result = registry.invoke(
            "pdf.extract_pages",
            {"path": args.path, "pages": args.pages, "output_path": args.output},
            context,
        )
        if not result.success or result.output is None:
            return _report_tool_failure(result)
        print(f"Extracted PDF: {result.output['output_path']}")
        print(f"Pages: {result.output['page_count']} of {result.output['source_pages']}")
        print(f"Extracted: {', '.join(str(p) for p in result.output['extracted_pages'])}")
        print(f"SHA-256: {result.output['sha256']}")
        return 0

    if args.pdf_command == "split":
        split_arguments: dict[str, object] = {
            "path": args.path,
            "output_folder": args.output_folder,
        }
        if args.prefix:
            split_arguments["prefix"] = args.prefix
        result = registry.invoke("pdf.split", split_arguments, context)
        if not result.success or result.output is None:
            return _report_tool_failure(result)
        print(
            f"Split {result.output['path']} into {result.output['page_count']} "
            f"page PDF(s) in {result.output['output_folder']}:"
        )
        for output_path in result.output["outputs"]:
            print(f"  {output_path}")
        return 0

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


def _run_workflow_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.workflow_command is None:
        print("Usage: pc-agent workflow list|show|run|delete ...", file=sys.stderr)
        return 2
    settings.ensure_directories()
    Database = _open_database(settings)
    orchestrator = _build_orchestrator(settings, database=Database)

    if args.workflow_command == "list":
        records = orchestrator.workflow_records()
        if not records:
            print("Verified workflows: none (run a task to completion to record one).")
            return 0
        print(f"Verified workflows: {len(records)}")
        for record in records:
            content = record.content
            successes = content.get("success_count")
            successes = successes if isinstance(successes, int) else 0
            print(
                f"{record.key}  {record.title} tool={content.get('tool_id', '-')} "
                f"successes={successes}  (updated {record.updated_at.isoformat()})"
            )
        return 0

    if args.workflow_command == "show":
        record = orchestrator.resolve_workflow(args.key)
        content = record.content
        print(f"Workflow: {record.key}")
        print(f"Title: {record.title}")
        print(f"Tool: {content.get('tool_id', '-')} (intent {content.get('intent', '-')})")
        print(f"Instruction: {content.get('instruction', '-')}")
        print(f"Verified successes: {content.get('success_count', 0)}")
        print(f"Last task: {content.get('last_task_id', '-')}")
        arguments = content.get("arguments")
        if isinstance(arguments, dict):
            print("Stored arguments:")
            print(json.dumps(arguments, indent=2, sort_keys=True, default=str))
        else:
            print("Stored arguments: (legacy record; resolved from the last task plan)")
        return 0

    if args.workflow_command == "delete":
        record = orchestrator.resolve_workflow(args.key)
        if not args.yes:
            print(
                f"Refusing to delete workflow {record.key} without --yes.",
                file=sys.stderr,
            )
            return 1
        orchestrator.delete_workflow(record.key)
        print(f"Deleted workflow record {record.key}. Task history was kept.")
        return 0

    # run
    _require_license(settings, FEATURE_CORE_TASKS)
    record = orchestrator.resolve_workflow(args.key)
    instruction = record.content.get("instruction")
    task = TaskRepository(Database).create(
        Task(
            instruction=instruction
            if isinstance(instruction, str) and instruction
            else f"Reuse verified workflow {record.key}"
        )
    )
    print(f"Resolved workflow -> {record.key}", file=sys.stderr)
    try:
        report = orchestrator.run_workflow(
            task.id,
            record.key,
            approval_callback=_approval_callback(preapproved=bool(args.yes)),
            max_recovery_attempts=args.max_recovery_attempts,
        )
    except KeyboardInterrupt:
        print(
            "Interrupted. The task may be left mid-step; check it with "
            f"'pc-agent task show {task.id}', then resume or cancel it.",
            file=sys.stderr,
        )
        return 130
    return _print_execution_report(report, as_json=bool(args.json))


def _run_files_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.files_command is None:
        print(
            "Usage: pc-agent files copy|move|rename|organize|search ...",
            file=sys.stderr,
        )
        return 2
    _require_license(settings, FEATURE_CORE_TASKS)

    registry = build_default_registry()
    context = _tool_context(settings)

    if args.files_command in {"copy", "move"}:
        result = registry.invoke(
            f"files.{args.files_command}",
            {"path": args.source, "output_path": args.destination},
            context,
        )
        if not result.success or result.output is None:
            return _report_tool_failure(result)
        verb = "Copied" if args.files_command == "copy" else "Moved"
        print(f"{verb}: {result.output['path']} -> {result.output['output_path']}")
        print(f"Size: {result.output['size_bytes']} bytes")
        print(f"SHA-256: {result.output['sha256']}")
        return 0

    if args.files_command == "rename":
        result = registry.invoke(
            "files.rename",
            {"path": args.source, "new_name": args.new_name},
            context,
        )
        if not result.success or result.output is None:
            return _report_tool_failure(result)
        print(f"Renamed: {result.output['path']} -> {result.output['output_path']}")
        print(f"SHA-256: {result.output['sha256']}")
        return 0

    if args.files_command == "organize":
        result = registry.invoke(
            "files.organize",
            {
                "directory": args.folder,
                "by": args.by,
                "max_files": args.max_files,
            },
            context,
        )
        if not result.success or result.output is None:
            return _report_tool_failure(result)
        print(
            f"Organized {result.output['directory']} by {result.output['by']}: "
            f"moved={result.output['moved_count']}, "
            f"skipped={result.output['skipped_count']}"
        )
        for item in result.output["moved"]:
            print(f"  {item['from']} -> {item['to']}")
        for item in result.output["skipped"]:
            print(f"  SKIPPED {item['path']} - {item['reason']}")
        return 0

    result = registry.invoke(
        "files.search",
        {
            "query": args.query,
            "directory": args.folder,
            "search_content": args.content,
            "max_results": args.max_results,
        },
        context,
    )
    if not result.success or result.output is None:
        return _report_tool_failure(result)
    print(
        f"Search '{result.output['query']}' in {result.output['directory']}: "
        f"{result.output['count']} match(es), "
        f"scanned={result.output['scanned_files']}, "
        f"truncated={result.output['truncated']}"
    )
    for item in result.output["matches"]:
        line = f"  {item['path']}"
        if item["first_matching_line"]:
            line += f"  | {item['first_matching_line']}"
        print(line)
    return 0


def _run_office_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.office_command is None:
        print(
            "Usage: pc-agent office inspect|extract|csv-inspect|csv-to-json|"
            "json-to-csv|csv-to-xlsx|create-docx|docx-replace|docx-append|"
            "xlsx-set|xlsx-formulas ...",
            file=sys.stderr,
        )
        return 2
    _require_license(settings, FEATURE_DOCUMENTS)

    registry = build_default_registry()
    context = _tool_context(settings)
    tool_id: str
    arguments: dict[str, Any]
    if args.office_command == "inspect":
        tool_id = "office.inspect"
        arguments = {"path": args.path}
    elif args.office_command == "extract":
        tool_id = "office.extract_text"
        arguments = {"path": args.path, "max_chars": args.max_chars}
        if args.output:
            arguments["output_path"] = args.output
    elif args.office_command == "csv-inspect":
        tool_id = "office.csv_inspect"
        arguments = {"path": args.path, "sample_rows": args.sample_rows}
    elif args.office_command == "csv-to-json":
        tool_id = "office.csv_to_json"
        arguments = {"input_path": args.input, "output_path": args.output}
    elif args.office_command == "json-to-csv":
        tool_id = "office.json_to_csv"
        arguments = {"input_path": args.input, "output_path": args.output}
    elif args.office_command == "csv-to-xlsx":
        tool_id = "office.csv_to_xlsx"
        arguments = {
            "input_path": args.input,
            "output_path": args.output,
            "sheet_name": args.sheet_name,
        }
    elif args.office_command == "create-docx":
        tool_id = "office.create_docx"
        arguments = {
            "output_path": args.output,
            "title": args.title,
            "paragraphs": list(args.paragraph or []),
        }
    elif args.office_command == "docx-replace":
        tool_id = "office.docx_replace_text"
        arguments = {
            "input_path": args.input,
            "output_path": args.output,
            "find": args.find,
            "replace": args.replace,
        }
    elif args.office_command == "docx-append":
        tool_id = "office.docx_append_paragraphs"
        arguments = {
            "input_path": args.input,
            "output_path": args.output,
            "paragraphs": list(args.paragraph or []),
        }
    elif args.office_command == "xlsx-set":
        tool_id = "office.xlsx_set_cells"
        cells: list[dict[str, str]] = []
        for assignment in args.cells or []:
            reference, separator, value = str(assignment).partition("=")
            if not separator or not reference.strip():
                print(
                    "Each --cell must look like B2==SUM(A1:A3) or B2=42.",
                    file=sys.stderr,
                )
                return 2
            cells.append({"reference": reference.strip(), "value": value})
        arguments = {
            "input_path": args.input,
            "output_path": args.output,
            "cells": cells,
        }
        if args.sheet:
            arguments["sheet_name"] = args.sheet
    elif args.office_command == "xlsx-formulas":
        tool_id = "office.xlsx_verify_formulas"
        arguments = {"path": args.input}
    else:
        return 2

    result = registry.invoke(tool_id, arguments, context)
    if not result.success or result.output is None:
        return _report_tool_failure(result)
    if getattr(args, "json", False):
        print(json.dumps(result.output, indent=2, sort_keys=True, default=str))
    else:
        _print_direct_tool_output(result.output)
    return 0


def _run_web_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.web_command != "fetch":
        print(
            "Usage: pc-agent web fetch https://example.com/ [--output file.txt]",
            file=sys.stderr,
        )
        return 2
    _require_license(settings, FEATURE_WEB)

    registry = build_default_registry()
    context = _tool_context(
        settings,
        allow_network=True,
        allow_private_network=args.allow_private,
    )
    result = registry.invoke(
        "web.fetch", {"url": args.url, "max_chars": args.max_chars}, context
    )
    if not result.success or result.output is None:
        return _report_tool_failure(result)

    if args.output:
        output = _safe_web_output_path(settings, args.output)
        if output.exists():
            print(
                "Error: Web output already exists. This command never overwrites; "
                "choose a new --output path.",
                file=sys.stderr,
            )
            return 1
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with output.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(str(result.output["text"]))
                handle.write("\n")
        except FileExistsError:
            print(
                "Error: Web output already exists. This command never overwrites; "
                "choose a new --output path.",
                file=sys.stderr,
            )
            return 1
        except OSError as exc:
            print(f"Error: Could not save web output: {exc}", file=sys.stderr)
            return 1
        print(f"Web text saved: {output.relative_to(settings.workspace_dir).as_posix()}")
        print(f"Final URL: {result.output['final_url']}")
        print(f"Body sha256: {result.output['body_sha256']}")
        return 0

    if args.json:
        print(json.dumps(result.output, indent=2, sort_keys=True, default=str))
    else:
        _print_direct_tool_output(result.output)
    return 0


def _safe_web_output_path(settings: Settings, raw_output: str) -> Path:
    candidate = Path(raw_output).expanduser()
    if not candidate.is_absolute():
        candidate = settings.workspace_dir / candidate
    resolved = candidate.resolve()
    workspace = settings.workspace_dir.resolve()
    if not resolved.is_relative_to(workspace):
        raise PcAgentError("Web output must stay inside the workspace.")
    if resolved.suffix.lower() not in {".txt", ".md"}:
        raise PcAgentError("Web output must end with .txt or .md.")
    return resolved


def _run_desktop_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.desktop_command is None:
        print("Usage: pc-agent desktop info|open|open-url ...", file=sys.stderr)
        return 2
    _require_license(settings, FEATURE_DESKTOP)

    registry = build_default_registry()
    if args.desktop_command == "info":
        result = registry.invoke(
            "desktop.system_info", {}, _tool_context(settings)
        )
        if not result.success or result.output is None:
            return _report_tool_failure(result)
        if args.json:
            print(json.dumps(result.output, indent=2, sort_keys=True, default=str))
        else:
            _print_direct_tool_output(result.output)
        return 0

    context = _tool_context(settings, allow_desktop_open=True)
    if args.desktop_command == "open":
        result = registry.invoke(
            "desktop.open_path", {"path": args.path}, context
        )
    elif args.desktop_command == "open-url":
        result = registry.invoke(
            "desktop.open_url", {"url": args.url}, context
        )
    else:
        return 2
    if not result.success or result.output is None:
        return _report_tool_failure(result)
    _print_direct_tool_output(result.output)
    return 0


def _planner_options(args: argparse.Namespace) -> PlannerOptions:
    return PlannerOptions(
        input_folders=tuple(getattr(args, "input_folder", []) or ()),
        input_files=tuple(getattr(args, "input_file", []) or ()),
        output_path=getattr(args, "output", None),
        target_path=getattr(args, "target", None),
        recursive=bool(getattr(args, "recursive", False)),
        expected_pages=getattr(args, "expected_pages", None),
        page_selection=getattr(args, "pages", None),
        find_text=getattr(args, "find", None),
        replace_text=getattr(args, "replace", None),
        cell_reference=getattr(args, "cell", None),
        cell_value=getattr(args, "value", None),
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


def _build_orchestrator(
    settings: Settings, *, database: Database | None = None
) -> TaskOrchestrator:
    database = database or _open_database(settings)
    return TaskOrchestrator(
        settings=settings,
        task_repository=TaskRepository(database),
        execution_repository=ExecutionRepository(database),
        tool_registry=build_default_registry(),
        deterministic_planner=DeterministicPlanner(settings.workspace_dir),
        model_router=build_model_router(settings),
        recovery_repository=RecoveryRepository(database),
        memory_repository=MemoryRepository(database),
    )


def _build_recovery_service(
    settings: Settings, *, database: Database | None = None
) -> RecoveryService:
    database = database or _open_database(settings)
    return RecoveryService(
        settings=settings,
        task_repository=TaskRepository(database),
        execution_repository=ExecutionRepository(database),
        recovery_repository=RecoveryRepository(database),
        orchestrator=_build_orchestrator(settings, database=database),
    )


def _execute_task(settings: Settings, task_id: str, args: argparse.Namespace) -> int:
    _require_license(settings, FEATURE_CORE_TASKS)
    if bool(args.use_ai):
        _require_license(settings, FEATURE_PROVIDERS)
    settings.ensure_directories()
    try:
        report = _build_orchestrator(settings).execute(
            task_id,
            options=_planner_options(args),
            approval_callback=_approval_callback(preapproved=bool(args.yes)),
            use_ai=bool(args.use_ai),
            max_recovery_attempts=args.max_recovery_attempts,
        )
    except KeyboardInterrupt:
        print(
            "Interrupted. The task may be left mid-step; check it with "
            f"'pc-agent task show {task_id}', then resume or cancel it.",
            file=sys.stderr,
        )
        return 130
    return _print_execution_report(report, as_json=bool(args.json))


def _run_new_task(settings: Settings, args: argparse.Namespace) -> int:
    _require_license(settings, FEATURE_CORE_TASKS)
    if bool(args.use_ai):
        _require_license(settings, FEATURE_PROVIDERS)
    settings.ensure_directories()
    database = _open_database(settings)
    task = TaskRepository(database).create(
        Task(instruction=" ".join(args.instruction))
    )
    orchestrator = _build_orchestrator(settings, database=database)
    try:
        report = orchestrator.execute(
            task.id,
            options=_planner_options(args),
            approval_callback=_approval_callback(preapproved=bool(args.yes)),
            use_ai=bool(args.use_ai),
            max_recovery_attempts=args.max_recovery_attempts,
        )
    except KeyboardInterrupt:
        print(
            "Interrupted. The task may be left mid-step; check it with "
            f"'pc-agent task show {task.id}', then resume or cancel it.",
            file=sys.stderr,
        )
        return 130
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
        "recovery_attempts": [
            attempt.to_dict() for attempt in report.recovery_attempts
        ],
        "recovered": report.recovered,
        "source_task_id": report.source_task_id,
        "error": report.error,
        "acceptance_criteria": (
            report.verification_report.to_dict() if report.verification_report else None
        ),
        "resumed_from_checkpoint": report.resumed_from_checkpoint,
        "workflow_key": report.workflow_key,
        "workflow_success_count": report.workflow_success_count,
        "workflow_reused": report.workflow_reused,
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
        if report.source_task_id:
            print(f"Source task: {report.source_task_id}")
        for attempt in report.recovery_attempts:
            print(
                f"Recovery attempt {attempt.attempt_number}: "
                f"[{attempt.status}; {attempt.action}; category={attempt.category or '-'}] "
                f"{attempt.message or ''}".rstrip()
            )
        if report.recovered:
            print("Recovered: True (a bounded self-healing retry succeeded)")
        if report.missing_information:
            print("Needs input:")
            for item in report.missing_information:
                print(f"  - {item}")
        if report.tool_result and report.tool_result.output:
            _print_tool_output(report)
        if report.resumed_from_checkpoint:
            print("Resumed from checkpoint: the completed tool step was not re-run.")
        if report.workflow_key:
            if report.workflow_reused:
                print(
                    f"Verified workflow reused: {report.workflow_key} "
                    "(stored plan revalidated live before running)."
                )
            else:
                print(
                    f"Verified workflow recorded: {report.workflow_key} "
                    f"(success #{report.workflow_success_count})."
                )
        if report.verification_message:
            print(f"Verification: {report.verification_valid} - {report.verification_message}")
        if report.verification_report is not None:
            print("Acceptance criteria:")
            for criterion_result in report.verification_report.results:
                mark = "PASS" if criterion_result.passed else "FAIL"
                print(
                    f"  [{mark}] {criterion_result.criterion.description}"
                    f" - {criterion_result.evidence}"
                )
        if report.error and report.task.status.value not in {"needs_input"}:
            print(f"Error: {report.error}", file=sys.stderr)
        if report.task.status is TaskStatus.FAILED:
            print("Next commands (copy exactly; no placeholder editing needed):")
            print(f"  pc-agent task diagnose {report.task.id}")
            print(f"  pc-agent task recover {report.task.id}")
            print("  Shortcut for the newest failed task: pc-agent task recover latest-failed")

    if report.success:
        return 0
    if report.task.status.value == "needs_input":
        return 3
    if report.task.status.value == "cancelled":
        return 4
    return 1


def _format_byte_count(value: Any) -> str:
    if not isinstance(value, int):
        return "-"
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(value)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{value} B"
    return f"{amount:.1f} {unit} ({value} bytes)"


def _print_direct_tool_output(output: dict[str, Any]) -> None:
    if set(output).issuperset({"path", "format", "unit_name", "unit_count"}):
        print(f"Office: {output['path']} [{str(output['format']).upper()}]")
        print(
            f"{output['unit_name']}: {output['unit_count']} | words: {output['words']} | "
            f"characters: {output['characters']}"
        )
        print(
            f"Size: {_format_byte_count(output['size_bytes'])} | "
            f"sha256: {output['sha256']}"
        )
        if output.get("title"):
            print(f"Title: {output['title']}")
        details = output.get("details") or {}
        if isinstance(details, dict) and details:
            print("Details:")
            for key, value in details.items():
                print(f"  {key}: {value}")
        return
    if set(output).issuperset({"path", "format", "text", "characters"}):
        print(f"Extracted text: {output['path']} [{str(output['format']).upper()}]")
        print(f"Characters: {output['characters']} | truncated: {output['truncated']}")
        if output.get("output_path"):
            print(
                f"Saved: {output['output_path']} "
                f"({output.get('output_size_bytes')} bytes)"
            )
            print(f"Output sha256: {output.get('output_sha256')}")
        else:
            print("---")
            print(str(output["text"]))
            print("---")
        return
    if set(output).issuperset({"path", "columns", "row_count", "column_stats"}):
        print(f"CSV: {output['path']}")
        print(
            f"Rows: {output['row_count']} | columns: "
            f"{', '.join(output.get('columns', []))}"
        )
        print(
            f"Header: {output['has_header']} | delimiter: {output['delimiter']!r} | "
            f"malformed rows: {output['malformed_rows']}"
        )
        print(
            f"Size: {_format_byte_count(output['size_bytes'])} | "
            f"sha256: {output['sha256']}"
        )
        print("Column statistics:")
        for stat in output.get("column_stats", []):
            line = (
                f"  {stat['name']}: nonempty={stat['nonempty']} "
                f"distinct={stat['distinct_count']}"
            )
            if stat.get("numeric_count"):
                line += (
                    f" numeric={stat['numeric_count']} min={stat['minimum']} "
                    f"max={stat['maximum']} mean={stat['mean']}"
                )
            print(line)
        sample = output.get("sample_rows") or []
        if sample:
            print("Sample rows:")
            for row in sample:
                print(f"  {row}")
        return
    if set(output).issuperset({"input_path", "output_path", "row_count", "verified"}):
        print(f"Converted: {output['input_path']} -> {output['output_path']}")
        print(f"Format: {str(output['format']).upper()} | rows: {output['row_count']}")
        print(
            f"Size: {_format_byte_count(output['size_bytes'])} | "
            f"sha256: {output['sha256']}"
        )
        print(f"Verified: {output['verified']}")
        return
    if set(output).issuperset({"output_path", "format", "paragraphs", "verified"}):
        print(f"Created DOCX: {output['output_path']}")
        print(f"Title: {output.get('title') or '-'} | paragraphs: {output['paragraphs']}")
        print(
            f"Size: {_format_byte_count(output['size_bytes'])} | "
            f"sha256: {output['sha256']}"
        )
        print(f"Verified: {output['verified']}")
        return
    if set(output).issuperset({"requested_url", "final_url", "status", "text"}):
        print(f"Web: {output['final_url']}")
        print(
            f"Status: {output['status']} | content-type: {output['content_type']} | "
            f"size: {output['size_bytes']} bytes"
        )
        if output.get("title"):
            print(f"Title: {output['title']}")
        print(f"Body sha256: {output['body_sha256']}")
        print(f"Characters: {output['characters']} | truncated: {output['truncated']}")
        print("---")
        print(str(output["text"]))
        print("---")
        links = output.get("links") or []
        if links:
            print("Links (first 20):")
            for link in links[:20]:
                label = f" {link['text']}" if link.get("text") else ""
                print(f"  {link['url']}{label}")
        return
    if set(output).issuperset({"target", "kind", "opened"}):
        print(f"Opened {output['kind']}: {output['target']}")
        return
    if set(output).issuperset({"operating_system", "workspace", "cpu_count"}):
        print(f"Agent: {output['agent_version']}")
        print(
            f"OS: {output['operating_system']} {output['os_release']} "
            f"({output['os_version']})"
        )
        print(f"Machine: {output['machine']} | processor: {output['processor']}")
        print(f"Python: {output['python_version']} | CPUs: {output['cpu_count']}")
        print(
            "Memory: "
            f"{_format_byte_count(output['memory_total_bytes'])} total, "
            f"{_format_byte_count(output['memory_available_bytes'])} available"
        )
        print(f"Workspace: {output['workspace']}")
        print(
            "Workspace disk: "
            f"{_format_byte_count(output['disk_total_bytes'])} total, "
            f"{_format_byte_count(output['disk_used_bytes'])} used, "
            f"{_format_byte_count(output['disk_free_bytes'])} free"
        )
        return
    print(json.dumps(output, indent=2, default=str))


def _print_tool_output(report: ExecutionReport) -> None:
    if report.tool_result is None or report.tool_result.output is None:
        return
    output = report.tool_result.output
    tool_id = report.tool_result.tool_id
    if tool_id.startswith(("office.", "web.", "desktop.")):
        _print_direct_tool_output(output)
    elif tool_id in {"pdf.merge", "pdf.merge_folders"}:
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
    _require_license(settings, FEATURE_PROVIDERS)
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
