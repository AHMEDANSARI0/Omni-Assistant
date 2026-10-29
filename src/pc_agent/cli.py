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
import getpass
import json
import os
import platform
import shlex
import shutil
import ssl
import sys
import textwrap
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .browser_cdp import probe_browser
from .config import Settings
from .credentials import (
    SUPPORTED_CREDENTIAL_PROVIDERS,
    build_credential_store,
    canonical_credential_provider,
)
from .desktop_automation import probe_desktop_automation
from .diagnostics import build_diagnostics, export_diagnostics
from .documents.office import create_demo_office_files
from .documents.pdf import create_demo_pdfs
from .documents.pdf_security import (
    DEFAULT_PDF_PERMISSIONS,
    PDF_PERMISSION_NAMES,
    inspect_pdf_security,
    protect_pdf,
    unprotect_pdf,
)
from .documents.pdf_signatures import inspect_pdf_signatures
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
from .maintenance import DatabaseMaintenanceService
from .online_updates import (
    DownloadedUpdate,
    OnlineStageResult,
    OnlineUpdateCheck,
    check_online_update,
    download_online_update,
    stage_online_update,
)
from .orchestrator import ApprovalRequest, ExecutionReport, TaskOrchestrator
from .planning import DeterministicPlanner, PlannerOptions
from .providers import CompletionRequest, build_model_router, provider_statuses
from .recovery import RecoveryService, TaskDiagnosis
from .release import audit_install, build_release_bundle, verify_release_bundle
from .self_repair import SelfRepairService
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
from .updates import (
    apply_staged_update,
    check_update,
    get_update,
    list_updates,
    rollback_update,
    stage_update,
)


def _add_budget_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--token-budget", type=int, default=None,
        help="Per-task provider-token budget (0 means unlimited; default from config)",
    )
    parser.add_argument(
        "--provider-call-budget", type=int, default=None,
        help="Per-task provider-call budget (0 means unlimited; default from config)",
    )
    parser.add_argument(
        "--tool-budget", type=int, default=None,
        help="Per-task tool-invocation budget (0 means unlimited; default from config)",
    )
    parser.add_argument(
        "--step-budget", type=int, default=None,
        help="Per-task planned-step budget, 1 to 20 (default from config)",
    )
    parser.add_argument(
        "--task-time-budget-seconds", type=float, default=None,
        help="Per-task wall-time budget in seconds (0 means unlimited; default from config)",
    )


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
        "--pages", default=None, help='PDF page selection/order, e.g. "1-3,5", "3,1,2" or reverse'
    )
    parser.add_argument(
        "--find", default=None, help="Exact text to find (DOCX replace or PDF text search)"
    )
    parser.add_argument(
        "--pdf-rotate", dest="pdf_rotate_degrees", type=int, default=None,
        help="Rotate selected PDF pages by -270, -180, -90, 90, 180 or 270 degrees",
    )
    parser.add_argument(
        "--case-sensitive", dest="pdf_case_sensitive", action="store_true",
        help="Make a PDF embedded-text search case-sensitive",
    )
    parser.add_argument(
        "--max-results", dest="pdf_max_results", type=int, default=None,
        help="Maximum PDF text-search matches to return (1-200)",
    )
    parser.add_argument(
        "--pdf-ocr-dpi", type=int, default=None,
        help="PDF OCR render DPI (72-300; default 200)",
    )
    parser.add_argument(
        "--pdf-ocr-language", default=None,
        help="Optional PDF OCR language hint, e.g. Urdu or English",
    )
    parser.add_argument(
        "--pdf-ocr-all-pages", action="store_true",
        help="OCR even selected pages that already have embedded text",
    )
    parser.add_argument(
        "--pdf-ocr-max-output-tokens", type=int, default=None,
        help="Maximum provider output tokens per OCR page (256-4096)",
    )
    parser.add_argument(
        "--pdf-table-min-rows", type=int, default=None,
        help="Minimum rows for a detected PDF table (1-1000; default 2)",
    )
    parser.add_argument(
        "--pdf-table-min-columns", type=int, default=None,
        help="Minimum columns for a detected PDF table (1-50; default 2)",
    )
    parser.add_argument(
        "--form-field",
        dest="form_fields",
        action="append",
        default=[],
        help="PDF form value as Name=Value (repeatable; checkbox true/false; multi-select uses |)",
    )
    parser.add_argument("--replace", default=None, help="Replacement DOCX text")
    parser.add_argument("--cell", default=None, help="Spreadsheet cell reference, e.g. B2")
    parser.add_argument(
        "--value", default=None, help='Spreadsheet cell value/formula, e.g. "=SUM(A1:A3)"'
    )
    parser.add_argument(
        "--command",
        dest="run_command_text",
        default=None,
        help='Allow-listed command to run with approval, e.g. "python -m pytest -q" (needs PC_AGENT_ALLOW_SUBPROCESS=1)',
    )
    parser.add_argument(
        "--command-timeout",
        type=int,
        default=None,
        help="Timeout seconds for --command (capped by PC_AGENT_COMMAND_TIMEOUT_SECONDS)",
    )
    parser.add_argument(
        "--browser-open",
        default=None,
        metavar="URL",
        help="Open this exact URL in the debug browser with approval (needs PC_AGENT_ALLOW_BROWSER=1)",
    )
    parser.add_argument(
        "--browser-click-selector",
        default=None,
        help="CSS selector to click in the debug browser with approval",
    )
    parser.add_argument(
        "--browser-click-text",
        default=None,
        help="Exact visible text to click in the debug browser with approval",
    )
    parser.add_argument(
        "--browser-type-selector",
        default=None,
        help="CSS selector of a text field to type into with approval",
    )
    parser.add_argument(
        "--browser-type-text",
        default=None,
        help="Text for --browser-type-selector (never a password field)",
    )
    parser.add_argument(
        "--desktop-window",
        dest="desktop_window_title_contains",
        default=None,
        metavar="TEXT",
        help="Exact title text contained in the target desktop window",
    )
    parser.add_argument(
        "--desktop-window-handle",
        default=None,
        help="Exact desktop window handle from pc-agent desktop windows",
    )
    parser.add_argument(
        "--desktop-click-name",
        default=None,
        help="Exact UIA element name to click in the selected desktop window",
    )
    parser.add_argument(
        "--desktop-click-automation-id",
        default=None,
        help="Exact UIA AutomationId to click in the selected desktop window",
    )
    parser.add_argument(
        "--desktop-type-name",
        default=None,
        help="Exact UIA text element name in the selected desktop window",
    )
    parser.add_argument(
        "--desktop-type-automation-id",
        default=None,
        help="Exact UIA text element AutomationId in the selected desktop window",
    )
    parser.add_argument(
        "--desktop-type-text",
        default=None,
        help="Text for the selected desktop element (password elements are refused)",
    )
    parser.add_argument(
        "--desktop-keys",
        default=None,
        metavar="CHORD",
        help="One allow-listed desktop key chord for the selected window, e.g. Ctrl+S",
    )
    parser.add_argument("--image-width", type=int, default=None, help="Exact output image width")
    parser.add_argument("--image-height", type=int, default=None, help="Exact output image height")
    parser.add_argument(
        "--image-max-dimension",
        type=int,
        default=None,
        help="Shrink an image so neither side exceeds this many pixels",
    )
    parser.add_argument("--image-crop-left", type=int, default=None, help="Image crop left pixel")
    parser.add_argument("--image-crop-top", type=int, default=None, help="Image crop top pixel")
    parser.add_argument("--image-crop-right", type=int, default=None, help="Image crop right pixel")
    parser.add_argument(
        "--image-crop-bottom", type=int, default=None, help="Image crop bottom pixel"
    )
    parser.add_argument(
        "--image-rotate",
        dest="image_rotate_degrees",
        type=float,
        default=None,
        help="Rotate an image clockwise by these degrees (-360 to 360)",
    )
    parser.add_argument(
        "--image-flip",
        choices=["horizontal", "vertical", "both"],
        default=None,
        help="Flip an image horizontally, vertically, or both ways",
    )
    parser.add_argument(
        "--image-grayscale",
        action="store_true",
        help="Convert the transformed image to grayscale",
    )
    parser.add_argument(
        "--image-quality",
        type=int,
        default=None,
        help="JPEG/WebP quality for image conversion/transformation (1-100)",
    )
    parser.add_argument(
        "--image-question",
        default=None,
        help="Exact question for approved provider vision analysis of --target image",
    )
    parser.add_argument(
        "--image-compare",
        dest="image_compare_path",
        default=None,
        help="Second exact image path for an image comparison task",
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
    _add_budget_options(parser)
    parser.add_argument("--json", action="store_true", help="Print the execution report as JSON")


def _add_desktop_window_selector_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--window-handle", default=None, help="Exact window handle")
    parser.add_argument("--window-title", default=None, help="Exact window title")
    parser.add_argument(
        "--window-title-contains", default=None, help="Text contained in the window title"
    )
    parser.add_argument("--window-class", default=None, help="Exact window class name")
    parser.add_argument(
        "--window-process-id", type=int, default=None, help="Exact owning process id"
    )


def _add_desktop_element_selector_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--element-runtime-id", default=None, help="Exact UIA runtime id")
    parser.add_argument("--element-name", default=None, help="Exact UIA element name")
    parser.add_argument(
        "--element-name-contains", default=None, help="Text contained in the UIA element name"
    )
    parser.add_argument(
        "--element-automation-id", default=None, help="Exact UIA AutomationId"
    )
    parser.add_argument("--element-class", default=None, help="Exact UIA class name")
    parser.add_argument(
        "--element-control-type",
        default=None,
        help="UIA control type name or numeric id, e.g. Button or Edit",
    )
    parser.add_argument(
        "--element-index",
        type=int,
        default=None,
        help="1-based choice when the other selector fields match several elements",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pc-agent",
        description="Self-Healing PC Agent - personal local core + GUI + licensing (PHASE 27)",
    )
    parser.add_argument("--version", action="version", version=f"pc-agent {__version__}")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Application root folder (default: PC_AGENT_HOME or current folder)",
    )
    subparsers = parser.add_subparsers(dest="command")

    doctor_parser = subparsers.add_parser(
        "doctor", help="Check configuration, folders and the local database"
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Print redacted structured diagnostics instead of the text report",
    )

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

    git_parser = subparsers.add_parser(
        "git", help="Local Git tools (approved subprocess; no network operations)"
    )
    git_subparsers = git_parser.add_subparsers(dest="git_command")
    git_status_parser = git_subparsers.add_parser(
        "status", help="Show repository status (read-only)"
    )
    git_status_parser.add_argument("--repo", default=".")
    git_status_parser.add_argument("--json", action="store_true")
    git_log_parser = git_subparsers.add_parser(
        "log", help="Show recent commits (read-only)"
    )
    git_log_parser.add_argument("--repo", default=".")
    git_log_parser.add_argument("--count", type=int, default=10)
    git_log_parser.add_argument("--json", action="store_true")
    git_diff_parser = git_subparsers.add_parser(
        "diff", help="Summarize changes as a diffstat (read-only)"
    )
    git_diff_parser.add_argument("--repo", default=".")
    git_diff_parser.add_argument("--staged", action="store_true")
    git_diff_parser.add_argument("--json", action="store_true")
    git_init_parser = git_subparsers.add_parser(
        "init", help="Initialize a repository in a workspace folder"
    )
    git_init_parser.add_argument("directory", nargs="?", default=".")
    git_init_parser.add_argument("--yes", action="store_true")
    git_init_parser.add_argument("--json", action="store_true")
    git_add_parser = git_subparsers.add_parser(
        "add", help="Stage repository-relative paths"
    )
    git_add_parser.add_argument("paths", nargs="+")
    git_add_parser.add_argument("--repo", default=".")
    git_add_parser.add_argument("--yes", action="store_true")
    git_add_parser.add_argument("--json", action="store_true")
    git_commit_parser = git_subparsers.add_parser(
        "commit", help="Commit staged changes with a one-line message"
    )
    git_commit_parser.add_argument("--message", "-m", required=True)
    git_commit_parser.add_argument("--repo", default=".")
    git_commit_parser.add_argument("--yes", action="store_true")
    git_commit_parser.add_argument("--json", action="store_true")

    browser_parser = subparsers.add_parser(
        "browser",
        help="Drive a user-started Edge/Chrome debug session (Phase 13)",
    )
    browser_subparsers = browser_parser.add_subparsers(dest="browser_command")
    browser_status_parser = browser_subparsers.add_parser(
        "status", help="Show connected debug browser and its tabs (read-only)"
    )
    browser_status_parser.add_argument("--json", action="store_true")
    browser_open_parser = browser_subparsers.add_parser(
        "open", help="Open one exact URL in a new tab (approval required)"
    )
    browser_open_parser.add_argument("url")
    browser_open_parser.add_argument(
        "--wait", type=int, default=None, help="Seconds to wait for load (1-60)"
    )
    browser_open_parser.add_argument(
        "--target-id", default=None, help="Navigate this existing tab instead"
    )
    browser_open_parser.add_argument(
        "--allow-private",
        action="store_true",
        help="Allow a private/local address for this exact open",
    )
    browser_open_parser.add_argument("--yes", action="store_true")
    browser_open_parser.add_argument("--json", action="store_true")
    browser_snapshot_parser = browser_subparsers.add_parser(
        "snapshot", help="Read the current tab's text and links (read-only)"
    )
    browser_snapshot_parser.add_argument("--max-chars", type=int, default=None)
    browser_snapshot_parser.add_argument("--target-id", default=None)
    browser_snapshot_parser.add_argument("--json", action="store_true")
    browser_click_parser = browser_subparsers.add_parser(
        "click", help="Click one exact element (approval required)"
    )
    browser_click_parser.add_argument("--selector", default=None)
    browser_click_parser.add_argument("--text", default=None)
    browser_click_parser.add_argument("--target-id", default=None)
    browser_click_parser.add_argument("--yes", action="store_true")
    browser_click_parser.add_argument("--json", action="store_true")
    browser_type_parser = browser_subparsers.add_parser(
        "type", help="Type into one text field, never passwords (approval required)"
    )
    browser_type_parser.add_argument("selector")
    browser_type_parser.add_argument("--text", required=True)
    browser_type_parser.add_argument("--target-id", default=None)
    browser_type_parser.add_argument("--yes", action="store_true")
    browser_type_parser.add_argument("--json", action="store_true")
    browser_shot_parser = browser_subparsers.add_parser(
        "screenshot", help="Save a PNG screenshot of the current tab"
    )
    browser_shot_parser.add_argument(
        "output", nargs="?", default="browser-screenshot.png"
    )
    browser_shot_parser.add_argument("--target-id", default=None)
    browser_shot_parser.add_argument("--json", action="store_true")

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
    _add_budget_options(workflow_run_parser)
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

    credentials_parser = subparsers.add_parser(
        "credentials",
        help="Manage provider API keys in Windows Credential Manager (values never printed)",
    )
    credentials_subparsers = credentials_parser.add_subparsers(
        dest="credentials_command"
    )
    credential_status_parser = credentials_subparsers.add_parser(
        "status", help="Show credential-store status and stored provider names"
    )
    credential_status_parser.add_argument("--json", action="store_true")
    credential_list_parser = credentials_subparsers.add_parser(
        "list", help="List stored/effective provider-key sources without values"
    )
    credential_list_parser.add_argument("--json", action="store_true")
    credential_provider_choices = (
        *SUPPORTED_CREDENTIAL_PROVIDERS,
        "openai_compatible",
    )
    credential_set_parser = credentials_subparsers.add_parser(
        "set", help="Store one provider API key from a hidden prompt"
    )
    credential_set_parser.add_argument("provider", choices=credential_provider_choices)
    credential_delete_parser = credentials_subparsers.add_parser(
        "delete", help="Delete one stored provider API key"
    )
    credential_delete_parser.add_argument(
        "provider", choices=credential_provider_choices
    )
    credential_delete_parser.add_argument("--yes", action="store_true")
    credential_migrate_parser = credentials_subparsers.add_parser(
        "migrate",
        help="Copy existing environment/.env provider keys into Credential Manager",
    )
    credential_migrate_parser.add_argument(
        "provider", nargs="?", default=None, choices=credential_provider_choices
    )
    credential_migrate_parser.add_argument("--yes", action="store_true")

    self_repair_parser = subparsers.add_parser(
        "self-repair",
        help="Diagnose and safely restore the core source tree from a known-good baseline",
    )
    self_repair_subparsers = self_repair_parser.add_subparsers(
        dest="self_repair_command"
    )
    self_repair_baseline_parser = self_repair_subparsers.add_parser(
        "baseline", help="Capture a verified known-good core baseline"
    )
    self_repair_baseline_parser.add_argument(
        "--check", choices=("static", "import", "full"), default="import"
    )
    self_repair_baseline_parser.add_argument("--force", action="store_true")
    self_repair_baseline_parser.add_argument("--yes", action="store_true")
    self_repair_baseline_parser.add_argument("--json", action="store_true")
    self_repair_diagnose_parser = self_repair_subparsers.add_parser(
        "diagnose", help="Read-only core-source diagnosis against the baseline"
    )
    self_repair_diagnose_parser.add_argument("--json", action="store_true")
    self_repair_stage_parser = self_repair_subparsers.add_parser(
        "stage", help="Stage and verify a deterministic repair without applying it"
    )
    self_repair_stage_parser.add_argument(
        "--check", choices=("static", "import", "full"), default="import"
    )
    self_repair_stage_parser.add_argument("--json", action="store_true")
    self_repair_apply_parser = self_repair_subparsers.add_parser(
        "apply", help="Apply one verified staged repair run with automatic backup"
    )
    self_repair_apply_parser.add_argument("run_id", help="Exact run ID or latest")
    self_repair_apply_parser.add_argument(
        "--check", choices=("static", "import", "full"), default=None
    )
    self_repair_apply_parser.add_argument("--yes", action="store_true")
    self_repair_apply_parser.add_argument("--json", action="store_true")
    self_repair_rollback_parser = self_repair_subparsers.add_parser(
        "rollback", help="Roll back one applied self-repair run from its backup"
    )
    self_repair_rollback_parser.add_argument("run_id", help="Exact run ID or latest")
    self_repair_rollback_parser.add_argument("--yes", action="store_true")
    self_repair_rollback_parser.add_argument("--json", action="store_true")
    self_repair_list_parser = self_repair_subparsers.add_parser(
        "list", help="List recorded self-repair runs"
    )
    self_repair_list_parser.add_argument("--limit", type=int, default=20)
    self_repair_list_parser.add_argument("--json", action="store_true")
    self_repair_show_parser = self_repair_subparsers.add_parser(
        "show", help="Show one self-repair run"
    )
    self_repair_show_parser.add_argument("run_id", help="Exact run ID or latest")
    self_repair_show_parser.add_argument("--json", action="store_true")

    release_parser = subparsers.add_parser(
        "release",
        help="Audit production readiness and build/verify portable release bundles",
    )
    release_subparsers = release_parser.add_subparsers(dest="release_command")
    release_audit_parser = release_subparsers.add_parser(
        "audit", help="Run read-only production-readiness checks (no secrets shown)"
    )
    release_audit_parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as a failing exit status",
    )
    release_audit_parser.add_argument("--json", action="store_true")
    release_build_parser = release_subparsers.add_parser(
        "build",
        help="Build a reproducible portable ZIP (excludes .env, data and workspace contents)",
    )
    release_build_parser.add_argument(
        "--output", type=Path, default=None, help="Output .zip path (must not exist)"
    )
    release_build_parser.add_argument("--json", action="store_true")
    release_verify_parser = release_subparsers.add_parser(
        "verify", help="Verify a portable release ZIP without extracting it"
    )
    release_verify_parser.add_argument("bundle", type=Path)
    release_verify_parser.add_argument("--json", action="store_true")
    release_info_parser = release_subparsers.add_parser(
        "info", help="Verify and summarize a portable release ZIP"
    )
    release_info_parser.add_argument("bundle", type=Path)
    release_info_parser.add_argument(
        "--files", action="store_true", help="Also list every bundled file"
    )
    release_info_parser.add_argument("--json", action="store_true")

    update_parser = subparsers.add_parser(
        "update",
        help="Check, download, stage, apply or roll back signed release updates",
    )
    update_subparsers = update_parser.add_subparsers(dest="update_command")
    update_check_parser = update_subparsers.add_parser(
        "check", help="Verify a bundle and compare its version/signature"
    )
    update_check_parser.add_argument("bundle", type=Path)
    update_check_parser.add_argument("--signature", type=Path, default=None)
    update_check_parser.add_argument("--json", action="store_true")
    update_stage_parser = update_subparsers.add_parser(
        "stage", help="Verify and stage a bundle without applying it"
    )
    update_stage_parser.add_argument("bundle", type=Path)
    update_stage_parser.add_argument("--signature", type=Path, default=None)
    update_stage_parser.add_argument("--allow-unsigned", action="store_true")
    update_stage_parser.add_argument("--allow-downgrade", action="store_true")
    update_stage_parser.add_argument("--json", action="store_true")
    update_online_check_parser = update_subparsers.add_parser(
        "online-check",
        help="Fetch only the configured update feed and compare its version",
    )
    update_online_check_parser.add_argument("--feed-url", default=None)
    update_online_check_parser.add_argument("--channel", default=None)
    update_online_check_parser.add_argument("--json", action="store_true")
    update_online_download_parser = update_subparsers.add_parser(
        "online-download",
        help="Download and verify one online bundle without staging it",
    )
    update_online_download_parser.add_argument("--feed-url", default=None)
    update_online_download_parser.add_argument("--channel", default=None)
    update_online_download_parser.add_argument("--allow-downgrade", action="store_true")
    update_online_download_parser.add_argument("--json", action="store_true")
    update_online_stage_parser = update_subparsers.add_parser(
        "online-stage",
        help="Download, verify and stage one online bundle without applying it",
    )
    update_online_stage_parser.add_argument("--feed-url", default=None)
    update_online_stage_parser.add_argument("--channel", default=None)
    update_online_stage_parser.add_argument("--allow-unsigned", action="store_true")
    update_online_stage_parser.add_argument("--allow-downgrade", action="store_true")
    update_online_stage_parser.add_argument("--json", action="store_true")
    update_apply_parser = update_subparsers.add_parser(
        "apply", help="Apply one staged update with backup and verification"
    )
    update_apply_parser.add_argument(
        "update_id",
        nargs="?",
        default="latest-staged",
        help="Exact update ID or latest-staged",
    )
    update_apply_parser.add_argument("--yes", action="store_true")
    update_apply_parser.add_argument("--json", action="store_true")
    update_rollback_parser = update_subparsers.add_parser(
        "rollback", help="Roll back one applied update from its receipt backup"
    )
    update_rollback_parser.add_argument(
        "update_id",
        nargs="?",
        default="latest-applied",
        help="Exact update ID or latest-applied",
    )
    update_rollback_parser.add_argument("--yes", action="store_true")
    update_rollback_parser.add_argument("--json", action="store_true")
    update_list_parser = update_subparsers.add_parser(
        "list", help="List staged, applied, rolled-back and failed updates"
    )
    update_list_parser.add_argument("--limit", type=int, default=20)
    update_list_parser.add_argument("--json", action="store_true")
    update_show_parser = update_subparsers.add_parser(
        "show", help="Show one recorded update receipt"
    )
    update_show_parser.add_argument(
        "update_id", nargs="?", default="latest", help="Exact update ID or latest"
    )
    update_show_parser.add_argument("--json", action="store_true")

    maintenance_parser = subparsers.add_parser(
        "maintenance",
        help="Create, verify and restore local SQLite database backups",
    )
    maintenance_subparsers = maintenance_parser.add_subparsers(
        dest="maintenance_command"
    )
    maintenance_backup_parser = maintenance_subparsers.add_parser(
        "backup", help="Create and verify a consistent database backup"
    )
    maintenance_backup_parser.add_argument("--label", default="manual")
    maintenance_backup_parser.add_argument("--json", action="store_true")
    maintenance_list_parser = maintenance_subparsers.add_parser(
        "list", help="List database backups"
    )
    maintenance_list_parser.add_argument("--json", action="store_true")
    maintenance_verify_parser = maintenance_subparsers.add_parser(
        "verify", help="Verify one database backup"
    )
    maintenance_verify_parser.add_argument(
        "backup", help="Exact backup ID, filename, or latest"
    )
    maintenance_verify_parser.add_argument("--json", action="store_true")
    maintenance_restore_parser = maintenance_subparsers.add_parser(
        "restore",
        help="Restore one verified database backup (creates a pre-restore backup first)",
    )
    maintenance_restore_parser.add_argument(
        "backup", help="Exact backup ID, filename, or latest"
    )
    maintenance_restore_parser.add_argument("--yes", action="store_true")
    maintenance_restore_parser.add_argument("--json", action="store_true")

    diagnostics_parser = subparsers.add_parser(
        "diagnostics",
        help="Export redacted support diagnostics (no secrets or task content)",
    )
    diagnostics_subparsers = diagnostics_parser.add_subparsers(
        dest="diagnostics_command"
    )
    diagnostics_export_parser = diagnostics_subparsers.add_parser(
        "export", help="Write a redacted diagnostics JSON file"
    )
    diagnostics_export_parser.add_argument(
        "--output", type=Path, default=None, help="Output .json path (must not exist)"
    )
    diagnostics_export_parser.add_argument("--json", action="store_true")

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
        "desktop",
        help="System info, exact desktop open, and gated Windows UI Automation (Phase 14)",
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
    desktop_windows_parser = desktop_subparsers.add_parser(
        "windows", help="List visible desktop windows (read-only; Phase 14 gate)"
    )
    desktop_windows_parser.add_argument("--max-windows", type=int, default=100)
    desktop_windows_parser.add_argument("--include-untitled", action="store_true")
    desktop_windows_parser.add_argument("--json", action="store_true")
    desktop_inspect_parser = desktop_subparsers.add_parser(
        "inspect", help="Inspect one window's UIA elements (read-only; Phase 14 gate)"
    )
    _add_desktop_window_selector_args(desktop_inspect_parser)
    desktop_inspect_parser.add_argument("--max-elements", type=int, default=200)
    desktop_inspect_parser.add_argument("--include-offscreen", action="store_true")
    desktop_inspect_parser.add_argument("--json", action="store_true")
    desktop_focus_parser = desktop_subparsers.add_parser(
        "focus", help="Focus one exact desktop window (approval required)"
    )
    _add_desktop_window_selector_args(desktop_focus_parser)
    desktop_focus_parser.add_argument("--yes", action="store_true")
    desktop_focus_parser.add_argument("--json", action="store_true")
    desktop_click_parser = desktop_subparsers.add_parser(
        "click", help="Click one exact UIA element (approval required)"
    )
    _add_desktop_window_selector_args(desktop_click_parser)
    _add_desktop_element_selector_args(desktop_click_parser)
    desktop_click_parser.add_argument("--yes", action="store_true")
    desktop_click_parser.add_argument("--json", action="store_true")
    desktop_type_parser = desktop_subparsers.add_parser(
        "type", help="Set one exact UIA text element, never passwords (approval required)"
    )
    _add_desktop_window_selector_args(desktop_type_parser)
    _add_desktop_element_selector_args(desktop_type_parser)
    desktop_type_parser.add_argument("--text", required=True)
    desktop_type_parser.add_argument("--yes", action="store_true")
    desktop_type_parser.add_argument("--json", action="store_true")
    desktop_keys_parser = desktop_subparsers.add_parser(
        "keys", help="Send one allow-listed key chord to one window (approval required)"
    )
    _add_desktop_window_selector_args(desktop_keys_parser)
    desktop_keys_parser.add_argument("keys", help="One key chord, e.g. Ctrl+S or Enter")
    desktop_keys_parser.add_argument("--repeat", type=int, default=1)
    desktop_keys_parser.add_argument("--yes", action="store_true")
    desktop_keys_parser.add_argument("--json", action="store_true")
    desktop_screenshot_parser = desktop_subparsers.add_parser(
        "screenshot", help="Save a full-desktop or selected-window PNG screenshot"
    )
    desktop_screenshot_parser.add_argument(
        "output", nargs="?", default="desktop-screenshot.png"
    )
    _add_desktop_window_selector_args(desktop_screenshot_parser)
    desktop_screenshot_parser.add_argument("--json", action="store_true")

    image_parser = subparsers.add_parser(
        "image",
        help="Local image tools and gated provider vision (Phase 15)",
    )
    image_subparsers = image_parser.add_subparsers(dest="image_command")
    image_inspect_parser = image_subparsers.add_parser(
        "inspect", help="Inspect one workspace image locally (read-only)"
    )
    image_inspect_parser.add_argument("path")
    image_inspect_parser.add_argument(
        "--no-color-stats",
        dest="include_color_stats",
        action="store_false",
        help="Skip average/dominant color statistics",
    )
    image_inspect_parser.set_defaults(include_color_stats=True)
    image_inspect_parser.add_argument("--json", action="store_true")
    image_convert_parser = image_subparsers.add_parser(
        "convert", help="Convert an image to a new file chosen by output extension"
    )
    image_convert_parser.add_argument("source")
    image_convert_parser.add_argument("output")
    image_convert_parser.add_argument("--quality", type=int, default=90)
    image_convert_parser.add_argument("--json", action="store_true")
    image_transform_parser = image_subparsers.add_parser(
        "transform", help="Create a resized/cropped/rotated/flipped/grayscale copy"
    )
    image_transform_parser.add_argument("source")
    image_transform_parser.add_argument("output")
    image_transform_parser.add_argument("--width", type=int, default=None)
    image_transform_parser.add_argument("--height", type=int, default=None)
    image_transform_parser.add_argument("--max-dimension", type=int, default=None)
    image_transform_parser.add_argument("--crop-left", type=int, default=None)
    image_transform_parser.add_argument("--crop-top", type=int, default=None)
    image_transform_parser.add_argument("--crop-right", type=int, default=None)
    image_transform_parser.add_argument("--crop-bottom", type=int, default=None)
    image_transform_parser.add_argument(
        "--rotate", dest="rotate_degrees", type=float, default=0.0,
        help="Clockwise degrees (-360 to 360)",
    )
    image_transform_parser.add_argument(
        "--flip", choices=["none", "horizontal", "vertical", "both"], default="none"
    )
    image_transform_parser.add_argument("--grayscale", action="store_true")
    image_transform_parser.add_argument("--quality", type=int, default=90)
    image_transform_parser.add_argument("--json", action="store_true")
    image_compare_parser = image_subparsers.add_parser(
        "compare", help="Deterministically compare two workspace images"
    )
    image_compare_parser.add_argument("first")
    image_compare_parser.add_argument("second")
    image_compare_parser.add_argument("--json", action="store_true")
    image_analyze_parser = image_subparsers.add_parser(
        "analyze",
        help="Ask a configured vision provider about one image (gate + approval required)",
    )
    image_analyze_parser.add_argument("path")
    image_analyze_parser.add_argument("--question", required=True)
    image_analyze_parser.add_argument("--max-output-tokens", type=int, default=1024)
    image_analyze_parser.add_argument("--yes", action="store_true")
    image_analyze_parser.add_argument("--json", action="store_true")

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
    extract_text_parser = pdf_subparsers.add_parser(
        "extract-text", help="Extract embedded PDF text (not OCR) to .txt/.md/.json"
    )
    extract_text_parser.add_argument("path", help="PDF path relative to the workspace")
    extract_text_parser.add_argument(
        "--output", required=True, help="New .txt, .md or .json output in the workspace"
    )
    extract_text_parser.add_argument(
        "--pages", default=None, help='Optional page selection like "1-3,5" (1-based)'
    )
    extract_text_parser.add_argument(
        "--format", choices=("text", "json"), default=None,
        help="Output format (default follows the output extension)",
    )
    extract_tables_parser = pdf_subparsers.add_parser(
        "extract-tables",
        help="Extract embedded-text PDF tables (never OCR) to .json or single-table .csv",
    )
    extract_tables_parser.add_argument("path", help="PDF path relative to the workspace")
    extract_tables_parser.add_argument(
        "--output", required=True, help="New .json or .csv output in the workspace"
    )
    extract_tables_parser.add_argument(
        "--pages", default=None, help='Optional page selection like "1-3,5" (maximum 100)'
    )
    extract_tables_parser.add_argument(
        "--format", choices=("json", "csv"), default=None,
        help="Output format (default follows the output extension; CSV needs exactly one table)",
    )
    extract_tables_parser.add_argument(
        "--min-rows", type=int, default=2, help="Minimum rows per detected table"
    )
    extract_tables_parser.add_argument(
        "--min-columns", type=int, default=2, help="Minimum columns per detected table"
    )
    extract_tables_parser.add_argument("--json", action="store_true")

    search_text_parser = pdf_subparsers.add_parser(
        "search", help="Search embedded PDF text (not OCR) for one exact phrase"
    )
    search_text_parser.add_argument("path", help="PDF path relative to the workspace")
    search_text_parser.add_argument("query", help="Exact phrase to find")
    search_text_parser.add_argument(
        "--pages", default=None, help='Optional page selection like "1-3,5" (1-based)'
    )
    search_text_parser.add_argument("--case-sensitive", action="store_true")
    search_text_parser.add_argument("--max-results", type=int, default=50)
    reorder_parser = pdf_subparsers.add_parser(
        "reorder", help="Write a new PDF with pages in a new full order"
    )
    reorder_parser.add_argument("path", help="PDF path relative to the workspace")
    reorder_parser.add_argument(
        "--pages", required=True,
        help='Every page once in the new order, e.g. "3,1,2", or reverse',
    )
    reorder_parser.add_argument(
        "--output", required=True, help="New output PDF in the workspace"
    )
    rotate_parser = pdf_subparsers.add_parser(
        "rotate", help="Write a new PDF with selected pages rotated by a quarter-turn"
    )
    rotate_parser.add_argument("path", help="PDF path relative to the workspace")
    rotate_parser.add_argument(
        "--degrees", type=int, required=True,
        help="Rotation: -270, -180, -90, 90, 180 or 270",
    )
    rotate_parser.add_argument(
        "--output", required=True, help="New output PDF in the workspace"
    )
    rotate_parser.add_argument(
        "--pages", default=None, help='Optional page selection like "1-3,5" (default all)'
    )

    inspect_form_parser = pdf_subparsers.add_parser(
        "inspect-form", help="Inspect existing interactive PDF form fields"
    )
    inspect_form_parser.add_argument("path", help="PDF path relative to the workspace")
    inspect_form_parser.add_argument("--json", action="store_true")
    fill_form_parser = pdf_subparsers.add_parser(
        "fill-form", help="Fill existing interactive PDF form fields into a new PDF"
    )
    fill_form_parser.add_argument("path", help="PDF path relative to the workspace")
    fill_form_parser.add_argument(
        "--output", required=True, help="New output PDF in the workspace"
    )
    fill_form_parser.add_argument(
        "--field",
        dest="form_fields",
        action="append",
        default=[],
        help="Field value as Name=Value (repeatable; checkbox true/false; multi-select uses |)",
    )
    fill_form_parser.add_argument("--json", action="store_true")

    security_parser = pdf_subparsers.add_parser(
        "security",
        help="Inspect PDF encryption status (optional hidden-password test)",
    )
    security_parser.add_argument("path", help="PDF path relative to the workspace")
    security_parser.add_argument(
        "--unlock",
        action="store_true",
        help="Prompt hiddenly for the current password and test it in memory",
    )
    security_parser.add_argument("--json", action="store_true")

    protect_parser = pdf_subparsers.add_parser(
        "protect",
        help="Write a new AES-256 password-protected PDF (hidden prompts only)",
    )
    protect_parser.add_argument("path", help="PDF path relative to the workspace")
    protect_parser.add_argument(
        "--output", required=True, help="New output PDF in the workspace"
    )
    protect_parser.add_argument(
        "--permission",
        dest="permissions",
        choices=PDF_PERMISSION_NAMES,
        action="append",
        default=[],
        help=(
            "Allowed user permission (repeatable); default: "
            + ", ".join(DEFAULT_PDF_PERMISSIONS)
        ),
    )
    protect_parser.add_argument("--json", action="store_true")

    unprotect_parser = pdf_subparsers.add_parser(
        "unprotect",
        help="Write a new unencrypted PDF after a hidden current-password prompt",
    )
    unprotect_parser.add_argument("path", help="PDF path relative to the workspace")
    unprotect_parser.add_argument(
        "--output", required=True, help="New output PDF in the workspace"
    )
    unprotect_parser.add_argument("--json", action="store_true")

    signatures_parser = pdf_subparsers.add_parser(
        "inspect-signatures",
        help="Inspect PDF digital signatures (trust/revocation not evaluated)",
    )
    signatures_parser.add_argument("path", help="PDF path relative to the workspace")
    signatures_parser.add_argument("--json", action="store_true")

    ocr_parser = pdf_subparsers.add_parser(
        "ocr",
        help="OCR up to 10 scanned PDF pages with gated vision (approval required)",
    )
    ocr_parser.add_argument("path", help="PDF path relative to the workspace")
    ocr_parser.add_argument(
        "--output", required=True, help="New .txt, .md or .json output in the workspace"
    )
    ocr_parser.add_argument(
        "--pages", default=None, help='Optional page selection like "1-3,5" (maximum 10)'
    )
    ocr_parser.add_argument(
        "--format", choices=("text", "json"), default=None,
        help="Output format (default follows the output extension)",
    )
    ocr_parser.add_argument("--dpi", type=int, default=200, help="Render DPI (72-300)")
    ocr_parser.add_argument("--language", default=None, help="Optional language hint")
    ocr_parser.add_argument(
        "--include-text-pages", action="store_true",
        help="OCR even selected pages that already have embedded text",
    )
    ocr_parser.add_argument(
        "--max-output-tokens", type=int, default=2048,
        help="Maximum provider output tokens per page (256-4096)",
    )
    ocr_parser.add_argument("--yes", action="store_true")
    ocr_parser.add_argument("--json", action="store_true")

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
            return _run_doctor(settings, as_json=bool(getattr(args, "json", False)))
        if args.command == "gui":
            return _run_gui_command(settings, args)
        if args.command == "license":
            return _run_license_command(settings, args)
        if args.command == "memory":
            return _run_memory_command(settings, args)
        if args.command == "git":
            return _run_git_command(settings, args)
        if args.command == "browser":
            return _run_browser_command(settings, args)
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
        if args.command == "credentials":
            return _run_credentials_command(settings, args)
        if args.command == "self-repair":
            return _run_self_repair_command(settings, args)
        if args.command == "release":
            return _run_release_command(settings, args)
        if args.command == "update":
            return _run_update_command(settings, args)
        if args.command == "maintenance":
            return _run_maintenance_command(settings, args)
        if args.command == "diagnostics":
            return _run_diagnostics_command(settings, args)
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
        if args.command == "image":
            return _run_image_command(settings, args)
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
            print("This removes the locally activated license file.")
            outcome = _ask_confirmation(
                "DEACTIVATE",
                prompt="Type DEACTIVATE to continue: ",
                case_sensitive=True,
            )
            if outcome is None:
                raise ApprovalUnavailableError(
                    "Deactivation needs interactive confirmation or --yes."
                )
            if not outcome:
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


def _run_doctor(settings: Settings, *, as_json: bool = False) -> int:
    database = _open_database(settings)
    if as_json:
        print(json.dumps(build_diagnostics(settings, database), indent=2))
        return 0
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
    print(
        f"Self-Healing PC Agent {__version__} "
        "(PHASE 27 PDF Signature Inspection)"
    )
    print(f"Python: {platform.python_version()}")
    print(f"Environment: {summary['environment']}")
    print(f"Root: {summary['root']}")
    print(f"Workspace: {summary['workspace']}")
    print(f"Database: {summary['database']} (schema v{database.schema_version()})")
    print(f"Provider order: {summary['provider_order']}")
    print(f"Providers with credentials: {summary['providers_with_credentials']}")
    print(f"Usable provider route: {' -> '.join(route)}")
    print(
        "Credential store: "
        f"{_credential_status_text(settings)}; "
        f"sources={summary['provider_credential_sources']}"
    )
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
        "web.fetch=public-only by default, desktop=open/info plus gated Windows UIA, "
        "images=local Pillow tools plus gated provider vision, "
        "provider keys=environment first, then Windows Credential Manager"
    )
    print(
        "Web limits: "
        f"timeout={settings.web_timeout_seconds:.1f}s, max_bytes={settings.web_max_bytes}"
    )
    if settings.allow_subprocess:
        print(
            "Subprocess: enabled; "
            f"allowlist={', '.join(settings.command_allowlist) or 'none'}; "
            f"timeout cap={settings.command_timeout_seconds:.0f}s; "
            f"git={'found' if shutil.which('git') else 'not found on PATH'}"
        )
    else:
        print(
            "Subprocess: disabled (set PC_AGENT_ALLOW_SUBPROCESS=1 in .env "
            "to enable approved git/command tools)"
        )
    if settings.allow_browser:
        browser_info = probe_browser(port=settings.browser_port)
        connected = (
            f"connected ({browser_info.get('Browser') or 'Chromium'})"
            if browser_info
            else "not reachable (start Edge/Chrome with --remote-debugging-port)"
        )
        print(
            "Browser: enabled; "
            f"debug endpoint=http://127.0.0.1:{settings.browser_port}; {connected}"
        )
    else:
        print(
            "Browser: disabled (set PC_AGENT_ALLOW_BROWSER=1 in .env to "
            "enable debug-browser automation)"
        )
    if settings.allow_desktop_automation:
        desktop_probe = probe_desktop_automation()
        runtime = (
            str(desktop_probe.get("detail") or "available")
            if desktop_probe.get("available")
            else f"unavailable ({desktop_probe.get('detail') or 'runtime missing'})"
        )
        print(
            "Desktop automation: enabled; "
            f"runtime={runtime}; "
            f"timeout={settings.desktop_automation_timeout_seconds:.1f}s; "
            "focus/click/type/keys require approval"
        )
    else:
        print(
            "Desktop automation: disabled (set "
            "PC_AGENT_ALLOW_DESKTOP_AUTOMATION=1 in .env to enable Windows UIA tools)"
        )
    if settings.allow_vision:
        vision_router = build_model_router(settings)
        vision_providers = [
            provider.name
            for provider in vision_router.providers
            if getattr(provider, "supports_vision", False) and provider.configured
        ]
        print(
            "Vision: enabled; "
            f"providers={', '.join(vision_providers) or 'none configured'}; "
            f"max_image_bytes={settings.vision_max_image_bytes}; "
            "each exact image send requires approval"
        )
    else:
        print(
            "Vision: disabled (set PC_AGENT_ALLOW_VISION=1 in .env to enable "
            "approved image analysis; local image tools remain available)"
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
    self_repair = SelfRepairService(settings.root_dir, settings.data_dir)
    baseline = self_repair.baseline_status()
    latest_repair = next(iter(self_repair.list_runs(limit=1)), None)
    print(
        "Task budgets: "
        f"tokens={settings.task_token_budget}, "
        f"provider_calls={settings.task_provider_call_budget}, "
        f"tools={settings.task_tool_budget}, steps={settings.task_step_budget}, "
        f"time={settings.task_time_budget_seconds:.0f}s"
    )
    print(
        "Self-repair: "
        f"{'enabled' if settings.allow_self_repair else 'disabled'}; "
        f"{baseline.summary()}"
        + (
            f"; latest run={latest_repair.run_id} [{latest_repair.status}]"
            if latest_repair
            else "; latest run=none"
        )
    )
    print(
        "Signed updates: "
        f"public_key={summary['release_public_key_configured']}; "
        f"key_id={summary['release_key_id']}; "
        f"required={summary['require_signed_updates']}; "
        f"online={summary['online_updates']}; "
        f"feed_host={summary['update_feed_host']}; "
        f"channel={summary['update_channel']}"
    )
    production_audit = audit_install(settings)
    database_backups = DatabaseMaintenanceService(
        settings.database_path, settings.data_dir
    ).list_backups()
    print(
        "Production: "
        f"release audit {production_audit.summary()}; "
        f"database backups={len(database_backups)}"
        + (
            f"; latest backup={database_backups[0].backup_id}"
            if database_backups
            else "; latest backup=none"
        )
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
        subprocess_allowed=settings.allow_subprocess,
        command_allowlist=settings.command_allowlist,
        command_timeout_seconds=settings.command_timeout_seconds,
        browser_allowed=settings.allow_browser,
        browser_port=settings.browser_port,
        browser_timeout_seconds=settings.browser_timeout_seconds,
        desktop_automation_allowed=settings.allow_desktop_automation,
        desktop_automation_timeout_seconds=settings.desktop_automation_timeout_seconds,
        vision_allowed=settings.allow_vision,
        vision_max_image_bytes=settings.vision_max_image_bytes,
        vision_router_factory=lambda: build_model_router(settings),
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


def _workspace_pdf_path(
    settings: Settings, raw_path: str, *, must_exist: bool
) -> Path:
    settings.ensure_directories()
    candidate = Path(raw_path.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = settings.workspace_dir / candidate
    resolved = candidate.resolve()
    workspace = settings.workspace_dir.resolve()
    if not resolved.is_relative_to(workspace):
        raise PcAgentError("PDF password paths must stay inside the workspace.")
    if resolved.suffix.lower() != ".pdf":
        raise PcAgentError("PDF password paths must end with .pdf.")
    if must_exist and not resolved.is_file():
        raise PcAgentError(f"PDF file not found: {raw_path}")
    return resolved


def _relative_workspace_path(settings: Settings, path: Path) -> str:
    return path.resolve().relative_to(settings.workspace_dir.resolve()).as_posix()


def _prompt_pdf_password(label: str, *, confirm: bool = False) -> str:
    try:
        password = getpass.getpass(f"{label}: ")
        if confirm:
            repeated = getpass.getpass(f"Repeat {label[0].lower() + label[1:]}: ")
            if repeated != password:
                raise PcAgentError("PDF passwords do not match.")
    except (EOFError, KeyboardInterrupt) as exc:
        raise PcAgentError(
            "PDF password input was cancelled or is unavailable."
        ) from exc
    return password


def _pdf_security_failure(exc: PcAgentError, *, as_json: bool) -> int:
    if as_json:
        print(
            json.dumps(
                {"success": False, "output": None, "error": str(exc)},
                indent=2,
            )
        )
    else:
        print(f"Error: {exc}", file=sys.stderr)
    return 1


def _run_pdf_security_command(settings: Settings, args: argparse.Namespace) -> int:
    """Run Phase 26 direct-only operations without a registered tool/task."""

    as_json = bool(getattr(args, "json", False))
    try:
        source = _workspace_pdf_path(settings, args.path, must_exist=True)
        source_label = _relative_workspace_path(settings, source)

        if args.pdf_command == "security":
            password = (
                _prompt_pdf_password("Current PDF password")
                if args.unlock
                else None
            )
            info = inspect_pdf_security(source, password=password)
            payload = {"path": source_label, **info.summary()}
            success = not (
                args.unlock
                and info.encrypted
                and info.password_accepted is not True
            )
            if as_json:
                print(
                    json.dumps(
                        {"success": success, "output": payload, "error": None},
                        indent=2,
                    )
                )
            else:
                print(f"PDF security: {source_label}")
                print(f"Encrypted: {info.encrypted}")
                print(f"Pages: {info.pages}")
                print(f"Unlocked: {info.unlocked}")
                if info.encrypted:
                    print(
                        f"Algorithm: {info.algorithm} | key bits: {info.key_bits} | "
                        f"handler: V{info.handler_version}/R{info.handler_revision}"
                    )
                    print(
                        "Permissions: "
                        + (", ".join(info.permissions) if info.permissions else "none")
                    )
                if args.unlock and info.encrypted:
                    print(f"Password accepted: {info.password_accepted}")
                    print(f"Password type: {info.password_type}")
                print(f"SHA-256: {info.sha256}")
            return 0 if success else 1

        output = _workspace_pdf_path(settings, args.output, must_exist=False)
        output_label = _relative_workspace_path(settings, output)

        if args.pdf_command == "unprotect":
            current_password = _prompt_pdf_password("Current PDF password")
            result = unprotect_pdf(source, output, password=current_password)
            payload = {
                "path": source_label,
                "output_path": output_label,
                **result.summary(),
            }
            if as_json:
                print(
                    json.dumps(
                        {"success": True, "output": payload, "error": None},
                        indent=2,
                    )
                )
            else:
                print(f"Unprotected PDF: {output_label}")
                print(
                    f"Pages: {result.output_pages} of {result.source_pages} | "
                    "encrypted: false"
                )
                print(f"SHA-256: {result.sha256}")
            return 0

        if args.pdf_command == "protect":
            before = inspect_pdf_security(source)
            current_password: str | None = None
            if before.encrypted:
                current_password = _prompt_pdf_password("Current PDF password")
                unlocked = inspect_pdf_security(
                    source, password=current_password
                )
                if unlocked.password_accepted is not True:
                    raise PcAgentError("Incorrect PDF password.")
            user_password = _prompt_pdf_password(
                "New PDF user password (minimum 8 characters)", confirm=True
            )
            owner_password = _prompt_pdf_password(
                "New PDF owner password (different from user)", confirm=True
            )
            chosen_permissions = (
                tuple(args.permissions)
                if args.permissions
                else DEFAULT_PDF_PERMISSIONS
            )
            result = protect_pdf(
                source,
                output,
                user_password=user_password,
                owner_password=owner_password,
                current_password=current_password,
                permissions=chosen_permissions,
            )
            payload = {
                "path": source_label,
                "output_path": output_label,
                **result.summary(),
            }
            if as_json:
                print(
                    json.dumps(
                        {"success": True, "output": payload, "error": None},
                        indent=2,
                    )
                )
            else:
                print(f"Protected PDF: {output_label}")
                print(
                    f"Pages: {result.output_pages} of {result.source_pages} | "
                    f"algorithm: {result.algorithm}"
                )
                print(
                    "Permissions: "
                    + (", ".join(result.permissions) if result.permissions else "none")
                )
                print(f"SHA-256: {result.sha256}")
            return 0

        raise PcAgentError(f"Unknown PDF security command: {args.pdf_command}")
    except PcAgentError as exc:
        return _pdf_security_failure(exc, as_json=as_json)


def _run_pdf_signature_command(
    settings: Settings, args: argparse.Namespace
) -> int:
    """Run the Phase 27 direct-only signature inspection."""

    as_json = bool(getattr(args, "json", False))
    try:
        source = _workspace_pdf_path(settings, args.path, must_exist=True)
        source_label = _relative_workspace_path(settings, source)
        inspection = inspect_pdf_signatures(source)
    except PcAgentError as exc:
        return _pdf_security_failure(exc, as_json=as_json)

    payload = {"path": source_label, **inspection.summary()}
    success = inspection.invalid_count == 0
    if as_json:
        print(
            json.dumps(
                {
                    "success": success,
                    "output": payload,
                    "error": (
                        None
                        if success
                        else "One or more PDF signatures are invalid or malformed."
                    ),
                },
                indent=2,
            )
        )
        return 0 if success else 1

    print(f"PDF signatures: {source_label}")
    print(
        f"Pages: {inspection.pages} | signature fields: "
        f"{inspection.signature_count} | signed: {inspection.signed_count} | "
        f"cryptographically valid: {inspection.valid_count} | invalid: "
        f"{inspection.invalid_count} | unsupported: "
        f"{inspection.unsupported_count} | unsigned: {inspection.unsigned_count}"
    )
    print("Trust/revocation: not evaluated")
    for signature in inspection.signatures:
        page_text = str(signature.page) if signature.page is not None else "?"
        print(
            f"  {signature.field_name} (page {page_text}): "
            f"{signature.status} - {signature.status_detail}"
        )
        if signature.signer_name:
            print(f"    Signer: {signature.signer_name}")
        if signature.reason:
            print(f"    Reason: {signature.reason}")
        if signature.location:
            print(f"    Location: {signature.location}")
        if signature.signature_date:
            print(f"    PDF date: {signature.signature_date}")
        if signature.sub_filter:
            print(
                f"    Type: {signature.filter_name or 'unknown'} / "
                f"{signature.sub_filter}"
            )
        if signature.byte_range:
            print(
                "    ByteRange: "
                + ", ".join(str(value) for value in signature.byte_range)
                + f" | covers whole file: {signature.covers_whole_file}"
            )
        if signature.docmdp_permission is not None:
            print(f"    DocMDP permission: P{signature.docmdp_permission}")
        for signer in signature.signers:
            certificate = signer.certificate
            subject = certificate.subject if certificate else "not embedded"
            print(
                "    CMS: "
                f"digest={signer.digest_algorithm} match={signer.digest_matches} | "
                f"signature={signer.signature_algorithm} valid="
                f"{signer.signature_valid} | signer={subject}"
            )
            if signer.signing_time:
                print(f"    CMS signing time: {signer.signing_time}")
        for certificate in signature.certificates:
            print(
                "    Certificate: "
                f"{certificate.subject} | issuer={certificate.issuer} | "
                f"serial={certificate.serial_number} | self-signed="
                f"{certificate.self_signed} | expired now={certificate.expired_now}"
            )
    print(f"SHA-256: {inspection.sha256}")
    return 0 if success else 1


def _run_pdf_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.pdf_command is None:
        print(
            "Usage: pc-agent pdf inspect|verify|merge|merge-folders|extract|split|"
            "extract-text|extract-tables|ocr|search|reorder|rotate|inspect-form|"
            "fill-form|security|protect|unprotect|inspect-signatures ...",
            file=sys.stderr,
        )
        return 2
    _require_license(settings, FEATURE_DOCUMENTS)
    if args.pdf_command in {"security", "protect", "unprotect"}:
        return _run_pdf_security_command(settings, args)
    if args.pdf_command == "inspect-signatures":
        return _run_pdf_signature_command(settings, args)
    if args.pdf_command == "ocr":
        _require_license(settings, FEATURE_PROVIDERS)

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


    if args.pdf_command == "extract-text":
        arguments: dict[str, object] = {
            "path": args.path,
            "output_path": args.output,
        }
        if args.pages:
            arguments["pages"] = args.pages
        if args.format:
            arguments["format"] = args.format
        result = registry.invoke("pdf.extract_text", arguments, context)
        if not result.success or result.output is None:
            return _report_tool_failure(result)
        print(f"Extracted PDF text: {result.output['output_path']}")
        print(
            f"Pages: {result.output['page_count']} of {result.output['source_pages']} | "
            f"text pages: {result.output['text_pages']} | "
            f"characters: {result.output['characters']}"
        )
        print(
            "Embedded text: "
            f"{'yes' if result.output['has_embedded_text'] else 'no (OCR is not performed)'}"
        )
        print(f"SHA-256: {result.output['sha256']}")
        return 0



    if args.pdf_command == "extract-tables":
        arguments = {
            "path": args.path,
            "output_path": args.output,
            "min_rows": args.min_rows,
            "min_columns": args.min_columns,
        }
        if args.pages:
            arguments["pages"] = args.pages
        if args.format:
            arguments["format"] = args.format
        result = registry.invoke("pdf.extract_tables", arguments, context)
        if getattr(args, "json", False):
            print(json.dumps(_tool_result_to_dict(result), indent=2, default=str))
            return 0 if result.success else 1
        if not result.success or result.output is None:
            return _report_tool_failure(result)
        print(f"Extracted PDF tables: {result.output['output_path']}")
        print(
            f"Pages: {result.output['page_count']} selected of {result.output['source_pages']} | "
            f"tables: {result.output['table_count']} | rows: {result.output['total_rows']} | "
            f"cells: {result.output['total_cells']}"
        )
        print(
            "Embedded text: "
            f"{'yes' if result.output['has_embedded_text'] else 'no (OCR is not performed)'}"
        )
        for table in result.output["tables"]:
            print(
                f"  Page {table['page']} table {table['index']}: "
                f"{table['row_count']} rows x {table['column_count']} columns"
            )
        print(f"SHA-256: {result.output['sha256']}")
        return 0

    if args.pdf_command == "inspect-form":
        result = registry.invoke(
            "pdf.inspect_form", {"path": args.path}, context
        )
        if getattr(args, "json", False):
            print(json.dumps(_tool_result_to_dict(result), indent=2, default=str))
            return 0 if result.success else 1
        if not result.success or result.output is None:
            return _report_tool_failure(result)
        print(f"PDF form: {result.output['path']}")
        print(
            f"Pages: {result.output['page_count']} | "
            f"fields: {result.output['field_count']}"
        )
        for field in result.output["fields"]:
            flags = []
            if field["required"]:
                flags.append("required")
            if field["read_only"]:
                flags.append("read-only")
            if field["radio"]:
                flags.append("radio")
            if field["pushbutton"]:
                flags.append("pushbutton")
            if field["multi_select"]:
                flags.append("multi-select")
            extras = f" ({', '.join(flags)})" if flags else ""
            print(
                f"  {field['name']}: {field['type']} = {field['value']!r}{extras}"
            )
            if field["options"]:
                print(f"    options: {', '.join(field['options'])}")
            if field["states"]:
                print(f"    states: {', '.join(field['states'])}")
        print(f"SHA-256: {result.output['sha256']}")
        return 0

    if args.pdf_command == "fill-form":
        parsed_fields = _parse_form_fields(args.form_fields)
        if not parsed_fields:
            raise PcAgentError(
                "PDF form filling needs at least one --field Name=Value."
            )
        arguments = {
            "path": args.path,
            "output_path": args.output,
            "values": [
                {"name": name, "value": value}
                for name, value in parsed_fields
            ],
        }
        result = registry.invoke("pdf.fill_form", arguments, context)
        if getattr(args, "json", False):
            print(json.dumps(_tool_result_to_dict(result), indent=2, default=str))
            return 0 if result.success else 1
        if not result.success or result.output is None:
            return _report_tool_failure(result)
        print(f"Filled PDF form: {result.output['output_path']}")
        print(
            f"Pages: {result.output['page_count']} | fields: "
            f"{result.output['field_count']} | changed: "
            f"{len(result.output['changed_fields'])}"
        )
        for entry in result.output["values"]:
            print(f"  {entry['name']} = {entry['value']!r}")
        print(f"SHA-256: {result.output['sha256']}")
        return 0

    if args.pdf_command == "ocr":
        arguments = {
            "path": args.path,
            "output_path": args.output,
            "dpi": args.dpi,
            "skip_text_pages": not args.include_text_pages,
            "max_output_tokens": args.max_output_tokens,
        }
        if args.pages:
            arguments["pages"] = args.pages
        if args.format:
            arguments["format"] = args.format
        if args.language:
            arguments["language"] = args.language
        if getattr(args, "json", False):
            confirmed = bool(args.yes)
            if not confirmed:
                print(
                    "PDF OCR needs approval. Re-run with --yes; JSON mode stays non-interactive.",
                    file=sys.stderr,
                )
        else:
            route = " -> ".join(build_model_router(settings).provider_names)
            summary = (
                f"OCR PDF {args.path!r}, pages {args.pages or 'all (maximum 10)'}, "
                f"into new file {args.output!r} at {args.dpi} DPI via provider "
                f"route ({route}). Pages with embedded text are "
                f"{'also sent for OCR' if args.include_text_pages else 'reused locally without a provider send'}; "
                "rendered scanned-page images will be sent to external providers on the route. "
                "Requires PC_AGENT_ALLOW_VISION=1."
            )
            retry_parts = [
                "pc-agent pdf ocr",
                _hint_quote(args.path),
                "--output",
                _hint_quote(args.output),
                "--dpi",
                str(args.dpi),
                "--max-output-tokens",
                str(args.max_output_tokens),
            ]
            if args.pages:
                retry_parts.extend(["--pages", _hint_quote(args.pages)])
            if args.format:
                retry_parts.extend(["--format", args.format])
            if args.language:
                retry_parts.extend(["--language", _hint_quote(args.language)])
            if args.include_text_pages:
                retry_parts.append("--include-text-pages")
            retry_parts.append("--yes")
            confirmed = _confirm_direct_write(
                summary,
                preapproved=bool(args.yes),
                retry_hint=" ".join(retry_parts),
                heading="Approval required to send rendered PDF pages to a vision provider:",
            )
        if not confirmed:
            print("Nothing was executed.")
            return 4
        result = registry.invoke("pdf.ocr", arguments, context)
        if getattr(args, "json", False):
            print(json.dumps(_tool_result_to_dict(result), indent=2, default=str))
            return 0 if result.success else 1
        if not result.success or result.output is None:
            return _report_tool_failure(result)
        print(f"OCR PDF text: {result.output['output_path']}")
        print(
            f"Pages: {result.output['page_count']} selected of {result.output['source_pages']} | "
            f"OCR pages: {', '.join(str(page) for page in result.output['ocr_pages']) or 'none'} | "
            f"embedded pages reused: {', '.join(str(page) for page in result.output['embedded_pages']) or 'none'}"
        )
        print(
            f"Characters: {result.output['characters']} | DPI: {result.output['dpi']} | "
            f"providers: {', '.join(result.output['providers']) or 'none'} | "
            f"models: {', '.join(result.output['models']) or 'none'}"
        )
        print("Note: OCR transcription is provider output; output bytes/hash/counts are verified.")
        print(f"SHA-256: {result.output['sha256']}")
        return 0

    if args.pdf_command == "search":
        arguments = {
            "path": args.path,
            "query": args.query,
            "case_sensitive": args.case_sensitive,
            "max_results": args.max_results,
        }
        if args.pages:
            arguments["pages"] = args.pages
        result = registry.invoke("pdf.search_text", arguments, context)
        if not result.success or result.output is None:
            return _report_tool_failure(result)
        print(f"PDF: {result.output['path']}")
        print(
            f"Occurrences: {result.output['total_occurrences']} | "
            f"matching pages: {', '.join(str(page) for page in result.output['matching_pages']) or 'none'}"
        )
        if not result.output["has_embedded_text"]:
            print("Embedded text: no (OCR is not performed)")
        for match in result.output["matches"]:
            print(f"  Page {match['page']}: {match['snippet']}")
        if result.output["truncated"]:
            print("More matches exist; raise --max-results (maximum 200).")
        return 0

    if args.pdf_command == "reorder":
        result = registry.invoke(
            "pdf.reorder_pages",
            {"path": args.path, "pages": args.pages, "output_path": args.output},
            context,
        )
        if not result.success or result.output is None:
            return _report_tool_failure(result)
        print(f"Reordered PDF: {result.output['output_path']}")
        print(f"Page order: {', '.join(str(page) for page in result.output['page_order'])}")
        print(f"SHA-256: {result.output['sha256']}")
        return 0

    if args.pdf_command == "rotate":
        arguments = {
            "path": args.path,
            "degrees": args.degrees,
            "output_path": args.output,
        }
        if args.pages:
            arguments["pages"] = args.pages
        result = registry.invoke("pdf.rotate_pages", arguments, context)
        if not result.success or result.output is None:
            return _report_tool_failure(result)
        print(f"Rotated PDF: {result.output['output_path']}")
        print(
            f"Rotated pages: {', '.join(str(page) for page in result.output['rotated_pages'])} "
            f"by {result.output['degrees']} degrees"
        )
        print(f"Final rotations: {', '.join(str(value) for value in result.output['page_rotations'])}")
        print(f"SHA-256: {result.output['sha256']}")
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


def _stdin_is_interactive() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except Exception:  # noqa: BLE001 - no usable console attached
        return False


def _flush_pending_console_input() -> bool:
    """Discard console input typed/pasted before a confirmation prompt.

    Returns True when pending typed text was found (and discarded), so the
    caller can tell the user it was ignored. Only interactive terminals are
    touched: piped stdin keeps its bytes. This stops a multi-line paste
    (where the next pasted command lands in the input buffer) - or a
    pre-pasted APPROVE - from answering the prompt; every confirmation
    must be typed after the prompt is shown.
    """

    if not _stdin_is_interactive():
        return False
    try:
        if os.name == "nt":
            return _flush_pending_windows_input()
        import select
        import termios

        readable, _w, _x = select.select([sys.stdin], [], [], 0)
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        return bool(readable)
    except Exception:  # noqa: BLE001 - best effort only; prompting still works
        return False


def _flush_pending_windows_input() -> bool:
    """Discard already-queued key presses via msvcrt; never raises.

    msvcrt is used instead of console-buffer APIs because those do not
    work reliably under Windows Terminal (ConPTY). Two drain rounds with
    a short settle in between catch paste chunks still in flight.
    Returns True when real text (not just Enter keys) was discarded.
    """

    try:
        import msvcrt

        found_text = False
        for _round in range(2):
            while msvcrt.kbhit():
                char = msvcrt.getwch()
                if char in ("\x00", "\xe0"):
                    msvcrt.getwch()  # swallow the scan code
                elif char not in ("\r", "\n"):
                    found_text = True
            time.sleep(0.15)
        return found_text
    except Exception:  # noqa: BLE001 - best effort; the key loop still guards
        return False


def _key_available() -> bool:
    """True when another key press is already queued (a paste signature)."""

    try:
        if os.name == "nt":
            import msvcrt

            return bool(msvcrt.kbhit())
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        previous = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd, termios.TCSANOW)
            readable, _w, _x = select.select([fd], [], [], 0)
            return bool(readable)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)
    except Exception:  # noqa: BLE001 - if unsure, treat the key as lone
        return False


def _read_single_key() -> str | None:
    """Read one key press without waiting for Enter; None if unavailable."""

    try:
        if os.name == "nt":
            import msvcrt

            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):  # special key: swallow scan code
                msvcrt.getwch()
                return ""
            return char
        import termios
        import tty

        fd = sys.stdin.fileno()
        previous = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd, termios.TCSANOW)
            data = os.read(fd, 1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)
        if not data:
            return None
        return data.decode("utf-8", errors="ignore")
    except KeyboardInterrupt:
        raise
    except Exception:  # noqa: BLE001 - fall back to "no input available"
        return None


def _drain_available_keys(limit: int = 256) -> str:
    """Read already-queued keys without blocking; returns what was drained."""

    drained: list[str] = []
    try:
        while len(drained) < limit and _key_available():
            char = _read_single_key()
            if char is None:
                break
            drained.append(char)
    except KeyboardInterrupt:
        drained.append("\x03")
    return "".join(drained)


def _ask_key_confirmation(expected: str) -> bool | None:
    """One-key confirmation for interactive terminals.

    Only a lone Y confirms. If more keys are already queued behind the
    Y (the signature of pasted text), the whole burst is discarded and
    the user is asked again, so a pasted "--yes" can never approve.
    Trailing Enter presses (from typing "y" then Enter out of habit)
    are tolerated. Returns None when no input is available at all.
    """

    if _flush_pending_console_input():
        print(
            "Note: text that was already typed or pasted was ignored, so "
            "it cannot answer this prompt - please answer now."
        )
    verb = "approve" if expected.upper() == "APPROVE" else "confirm"
    prompt = f"Press Y to {verb}, N or Ctrl+C to cancel: "
    print(prompt, end="", flush=True)
    junk = 0
    hinted = False
    last_was_enter = False
    while True:
        try:
            key = _read_single_key()
        except KeyboardInterrupt:
            print()
            print("Cancelled with Ctrl+C.")
            return False
        if key is None:
            print()
            return None
        if key == "\x03":  # Ctrl+C delivered as a character
            print()
            print("Cancelled with Ctrl+C.")
            return False
        lowered = key.lower()
        if lowered in ("y", "n"):
            trailing = _drain_available_keys()
            leftovers = [
                char
                for char in trailing
                if char not in ("\r", "\n") and char.lower() != lowered
            ]
            if leftovers:
                print()
                print(
                    "That looked like pasted text, so it was ignored. "
                    "Please press a single key now."
                )
                print(prompt, end="", flush=True)
                hinted = True
                last_was_enter = False
                continue
            print(key)
            return lowered == "y"
        junk += 1
        if junk > 1000:
            print()
            return False
        is_enter = key in ("\r", "\n")
        if is_enter and last_was_enter:
            continue
        if is_enter or not hinted:
            print()
            print("Just press Y or N - there is no need to press Enter.")
            print(prompt, end="", flush=True)
            hinted = True
        last_was_enter = is_enter


def _ask_word_confirmation(
    expected: str,
    *,
    prompt: str,
    case_sensitive: bool = False,
    max_wrong: int = 3,
    max_empty: int = 5,
) -> bool | None:
    """Ask until the user types ``expected``; never trust pre-typed text.

    Returns True when confirmed, False when denied/cancelled (too many
    wrong or empty answers, or Ctrl+C), and None when no input is
    available at all (EOF). An empty line - typically the leftover
    newline of a pasted command - re-asks instead of denying instantly,
    and a wrong non-empty answer is shown back, so a pasted command is
    visibly rejected instead of being silently swallowed. Only the
    exact word ever confirms; asking again grants nothing.
    """

    if _flush_pending_console_input():
        print(
            "Note: text that was already typed or pasted was ignored, so "
            "it cannot answer this prompt - please type your answer now."
        )
    wanted = expected if case_sensitive else expected.upper()
    wrong = 0
    empties = 0
    while True:
        try:
            answer = input(prompt).strip()
        except EOFError:
            return None
        except KeyboardInterrupt:
            print()
            print("Cancelled with Ctrl+C.")
            return False
        candidate = answer if case_sensitive else answer.upper()
        if candidate == wanted:
            return True
        if not answer:
            empties += 1
            if empties > max_empty:
                return False
            print(
                f"Empty answer - nothing confirmed yet. Type {expected} "
                "to continue, or press Ctrl+C to cancel."
            )
            continue
        wrong += 1
        shown = answer if len(answer) <= 60 else answer[:57] + "..."
        print(f"Received {shown!r} - that is not {expected}.")
        if wrong >= max_wrong:
            return False
        print(
            f"Type {expected} to continue, or press Ctrl+C to cancel. "
            f"({max_wrong - wrong} attempt(s) left)"
        )


_APPROVE_PROMPT = "Type APPROVE and press Enter to continue (Ctrl+C cancels): "


def _ask_confirmation(
    expected: str,
    *,
    prompt: str,
    case_sensitive: bool = False,
) -> bool | None:
    """Confirm interactively: one Y keypress on a terminal, else the word.

    Line-based ``input()`` is unreliable under modern Windows terminals
    (ConPTY can deliver phantom empty lines), so interactive terminals
    use single-key reads; pipes and redirected stdin keep the typed
    word, which scripts and tests already understand.
    """

    if _stdin_is_interactive():
        return _ask_key_confirmation(expected)
    return _ask_word_confirmation(
        expected, prompt=prompt, case_sensitive=case_sensitive
    )


def _confirm_direct_write(
    summary: str,
    *,
    preapproved: bool,
    retry_hint: str | None = None,
    heading: str = "Approval required for this exact Git write operation:",
) -> bool:
    print(heading)
    print(f"  {summary}")
    if preapproved:
        print("Decision: APPROVED by --yes.")
        return True
    outcome = _ask_confirmation("APPROVE", prompt=_APPROVE_PROMPT)
    if outcome is None:
        print("No interactive input available; re-run with --yes to approve.")
        if retry_hint:
            print(f"  {retry_hint}")
        return False
    print(f"Decision: {'APPROVED' if outcome else 'DENIED'}")
    if not outcome and retry_hint:
        print("To approve this exact operation without a prompt, re-run:")
        print(f"  {retry_hint}")
    return outcome


def _print_git_output(command: str, output: dict[str, Any]) -> None:
    if command == "status":
        print(f"Repository: {output['repository']}")
        print(
            f"Branch: {output['branch'] or '(detached HEAD)'} "
            f"(ahead {output['ahead']}, behind {output['behind']})"
        )
        print(f"Clean: {output['clean']}")
        for label in ("staged", "unstaged", "untracked"):
            items = output.get(label) or []
            print(f"{label.capitalize()}: {len(items)}")
            for item in items:
                print(f"  {item}")
    elif command == "log":
        print(f"Repository: {output['repository']}")
        for commit in output.get("commits") or []:
            print(
                f"{commit['short_hash']} {commit['subject']} "
                f"({commit['author']}, {commit['date']})"
            )
        print(f"Count: {output['count']}")
    elif command == "diff":
        print(output.get("stat") or "(no changes)")
    elif command == "init":
        print(f"Initialized Git repository: {output['repository']}")
    elif command == "add":
        print("Staged:")
        for item in output.get("added") or []:
            print(f"  {item}")
    elif command == "commit":
        print(f"Committed: {output['commit']} - {output['message']}")


def _run_git_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.git_command is None:
        print(
            "Usage: pc-agent git status|log|diff|init|add|commit ...",
            file=sys.stderr,
        )
        return 2
    registry = build_default_registry()
    context = _tool_context(settings)
    command = args.git_command
    if command == "status":
        result = registry.invoke(
            "git.status", {"repository": args.repo}, context
        )
    elif command == "log":
        result = registry.invoke(
            "git.log",
            {"repository": args.repo, "max_count": args.count},
            context,
        )
    elif command == "diff":
        result = registry.invoke(
            "git.diff_summary",
            {"repository": args.repo, "staged": bool(args.staged)},
            context,
        )
    elif command in {"init", "add", "commit"}:
        if command == "init":
            tool_id = "git.init"
            arguments: dict[str, Any] = {"directory": args.directory}
            summary = f"Initialize a Git repository in {args.directory}"
            retry_hint = f"pc-agent git init {args.directory} --yes"
        elif command == "add":
            tool_id = "git.add"
            arguments = {"repository": args.repo, "paths": list(args.paths)}
            summary = f"Stage {len(args.paths)} path(s) in {args.repo}"
            retry_hint = (
                "pc-agent git add " + " ".join(args.paths)
                + f" --repo {args.repo} --yes"
            )
        else:
            tool_id = "git.commit"
            arguments = {"repository": args.repo, "message": args.message}
            summary = f"Commit staged changes in {args.repo}: {args.message}"
            safe_message = args.message.replace(chr(34), chr(39))
            retry_hint = (
                f'pc-agent git commit -m "{safe_message}" '
                f"--repo {args.repo} --yes"
            )
        if not _confirm_direct_write(
            summary, preapproved=bool(args.yes), retry_hint=retry_hint
        ):
            print("Nothing was executed.")
            return 4
        result = registry.invoke(tool_id, arguments, context)
    else:
        return 2
    if args.json:
        print(json.dumps(_tool_result_to_dict(result), indent=2, default=str))
    elif result.success and result.output is not None:
        _print_git_output(command, dict(result.output))
    if not result.success:
        print(f"Error: {result.error}", file=sys.stderr)
        return 1
    return 0


def _hint_quote(value: str) -> str:
    cleaned = value.replace(chr(34), chr(39))
    return f'"{cleaned}"' if " " in cleaned else cleaned


def _print_browser_output(command: str, output: dict[str, Any]) -> None:
    if command == "status":
        print(f"Browser: {output['browser']} at {output['endpoint']}")
        print(f"Pages: {output['page_count']}")
        for target in output.get("targets") or []:
            print(
                f"  [{target['type']}] {target['title'] or '(untitled)'}"
                f" - {target['url']} (id: {target['id']})"
            )
    elif command == "open":
        print(f"Opened: {output['url']}")
        print(f"Title: {output['title']}")
        print(f"Target: {output['target_id']}")
    elif command == "snapshot":
        print(f"URL: {output['url']}")
        print(f"Title: {output['title']}")
        print(output.get("text") or "(no visible text)")
        links = output.get("links") or []
        if links:
            print("Links:")
            for link in links:
                print(f"  {link['text'] or '(no text)'} -> {link['href']}")
    elif command == "click":
        print(f"Clicked <{output['tag']}> {output['text']!r}")
        print(f"URL: {output['url']}")
    elif command == "type":
        print(
            f"Typed into {output['selector']}; "
            f"field value is now {output['value']!r}"
        )
    elif command == "screenshot":
        print(
            f"Screenshot saved: {output['output_path']} "
            f"({output['size_bytes']} bytes)"
        )
        print(f"SHA-256: {output['sha256']}")


def _run_browser_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.browser_command is None:
        print(
            "Usage: pc-agent browser status|open|snapshot|click|type|screenshot ...",
            file=sys.stderr,
        )
        return 2
    registry = build_default_registry()
    command = args.browser_command
    context = _tool_context(
        settings,
        allow_private_network=bool(getattr(args, "allow_private", False)),
    )
    target_id = getattr(args, "target_id", None)
    if command == "status":
        result = registry.invoke("browser.status", {}, context)
    elif command == "snapshot":
        arguments: dict[str, Any] = {}
        if args.max_chars:
            arguments["max_chars"] = args.max_chars
        if target_id:
            arguments["target_id"] = target_id
        result = registry.invoke("browser.snapshot", arguments, context)
    elif command == "screenshot":
        arguments = {"output_path": args.output}
        if target_id:
            arguments["target_id"] = target_id
        result = registry.invoke("browser.screenshot", arguments, context)
    elif command in {"open", "click", "type"}:
        if command == "open":
            tool_id = "browser.open"
            arguments = {"url": args.url}
            if args.wait:
                arguments["wait_seconds"] = args.wait
            summary = f"Open {args.url} in the debug browser"
            retry_hint = f"pc-agent browser open {_hint_quote(args.url)} --yes"
        elif command == "click":
            tool_id = "browser.click"
            arguments = {}
            if args.selector:
                arguments["selector"] = args.selector
                summary = f"Click selector {args.selector!r} in the debug browser"
                retry_hint = (
                    f"pc-agent browser click --selector {_hint_quote(args.selector)} --yes"
                )
            else:
                arguments["text"] = args.text
                summary = f"Click text {args.text!r} in the debug browser"
                retry_hint = (
                    f"pc-agent browser click --text {_hint_quote(args.text or '')} --yes"
                )
        else:
            tool_id = "browser.type"
            arguments = {"selector": args.selector, "text": args.text}
            summary = f"Type into {args.selector!r} in the debug browser"
            retry_hint = (
                f"pc-agent browser type {_hint_quote(args.selector)} "
                f"--text {_hint_quote(args.text)} --yes"
            )
        if not _confirm_direct_write(
            summary,
            preapproved=bool(args.yes),
            retry_hint=retry_hint,
            heading="Approval required for this exact browser action:",
        ):
            print("Nothing was executed.")
            return 4
        if target_id:
            arguments["target_id"] = target_id
        result = registry.invoke(tool_id, arguments, context)
    else:
        return 2
    if args.json:
        print(json.dumps(_tool_result_to_dict(result), indent=2, default=str))
    elif result.success and result.output is not None:
        _print_browser_output(command, dict(result.output))
    if not result.success:
        print(f"Error: {result.error}", file=sys.stderr)
        return 1
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
        steps = content.get("steps")
        if isinstance(steps, list) and steps:
            print(f"Multi-step plan: {len(steps)} step(s)")
            print(json.dumps(steps, indent=2, sort_keys=True, default=str))
        elif isinstance(arguments, dict):
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
            budget_options=_planner_options(args),
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


def _desktop_window_arguments(args: argparse.Namespace) -> dict[str, Any]:
    values = {
        "window_handle": getattr(args, "window_handle", None),
        "window_title": getattr(args, "window_title", None),
        "window_title_contains": getattr(args, "window_title_contains", None),
        "window_class_name": getattr(args, "window_class", None),
        "window_process_id": getattr(args, "window_process_id", None),
    }
    return {key: value for key, value in values.items() if value not in (None, "")}


def _desktop_element_arguments(args: argparse.Namespace) -> dict[str, Any]:
    values = {
        "element_runtime_id": getattr(args, "element_runtime_id", None),
        "element_name": getattr(args, "element_name", None),
        "element_name_contains": getattr(args, "element_name_contains", None),
        "element_automation_id": getattr(args, "element_automation_id", None),
        "element_class_name": getattr(args, "element_class", None),
        "element_control_type": getattr(args, "element_control_type", None),
        "element_index": getattr(args, "element_index", None),
    }
    return {key: value for key, value in values.items() if value not in (None, "")}


def _desktop_selector_hint(args: argparse.Namespace, *, include_element: bool = False) -> str:
    flag_by_key = {
        "window_handle": "--window-handle",
        "window_title": "--window-title",
        "window_title_contains": "--window-title-contains",
        "window_class_name": "--window-class",
        "window_process_id": "--window-process-id",
        "element_runtime_id": "--element-runtime-id",
        "element_name": "--element-name",
        "element_name_contains": "--element-name-contains",
        "element_automation_id": "--element-automation-id",
        "element_class_name": "--element-class",
        "element_control_type": "--element-control-type",
        "element_index": "--element-index",
    }
    values = _desktop_window_arguments(args)
    if include_element:
        values.update(_desktop_element_arguments(args))
    return " ".join(
        f"{flag_by_key[key]} {_hint_quote(str(value))}" for key, value in values.items()
    )


def _format_desktop_window(window: dict[str, Any]) -> str:
    bounds = window.get("bounds") or {}
    foreground = " foreground" if window.get("is_foreground") else ""
    minimized = " minimized" if window.get("is_minimized") else ""
    return (
        f"handle={window.get('handle')} pid={window.get('process_id')} "
        f"{window.get('process_name') or '-'} "
        f"[{window.get('class_name') or '-'}] {window.get('title') or '(untitled)'!r} "
        f"bounds=({bounds.get('left')},{bounds.get('top')})-"
        f"({bounds.get('right')},{bounds.get('bottom')}){foreground}{minimized}"
    )


def _print_desktop_automation_output(command: str, output: dict[str, Any]) -> None:
    if command == "windows":
        print(f"Desktop windows: {output['count']}")
        for window in output.get("windows") or []:
            print(f"  {_format_desktop_window(window)}")
    elif command == "inspect":
        print(f"Window: {_format_desktop_window(output['window'])}")
        print(
            f"Elements: {output['element_count']} shown; "
            f"visited={output['visited_count']}; reported={output['reported_count']}; "
            f"truncated={output['truncated']}"
        )
        for element in output.get("elements") or []:
            patterns = ",".join(element.get("patterns") or []) or "none"
            print(
                f"  [{element['index']}] {element['control_type']} "
                f"name={element['name']!r} automation_id={element['automation_id']!r} "
                f"class={element['class_name']!r} runtime={element['runtime_id'] or '-'} "
                f"patterns={patterns}"
            )
            if element.get("value") is not None:
                print(f"      value={element['value']!r}")
    elif command == "focus":
        print(f"Focused: {_format_desktop_window(output['window'])}")
    elif command == "click":
        element = output["element"]
        print(
            f"Clicked {element['control_type']} {element['name']!r} "
            f"via {output['action_method']}"
        )
        after = output.get("window_after")
        print("Window after: closed" if after is None else f"Window after: {_format_desktop_window(after)}")
    elif command == "type":
        print(
            f"Typed into {output['element']['control_type']} via {output['value_method']}; "
            f"value is now {output['value']!r}"
        )
    elif command == "keys":
        print(f"Sent {output['keys']} x{output['sent_count']}")
        after = output.get("window_after")
        print("Window after: closed" if after is None else f"Window after: {_format_desktop_window(after)}")
    elif command == "screenshot":
        print(
            f"Desktop screenshot saved: {output['output_path']} "
            f"({output['size_bytes']} bytes; {output['width']}x{output['height']})"
        )
        print(f"Target: {output['target']}")
        print(f"SHA-256: {output['sha256']}")


def _print_image_output(command: str, output: dict[str, Any]) -> None:
    if command == "compare":
        print(f"First: {output['first_path']} [{output['first_format']}] ")
        print(
            f"  {output['first_width']}x{output['first_height']} "
            f"sha256={output['first_sha256']}"
        )
        print(f"Second: {output['second_path']} [{output['second_format']}] ")
        print(
            f"  {output['second_width']}x{output['second_height']} "
            f"sha256={output['second_sha256']}"
        )
        print(
            "Comparison: "
            f"bytes={'identical' if output['byte_identical'] else 'different'}; "
            f"pixels={'identical' if output['pixel_identical'] else 'different'}; "
            f"same_dimensions={output['same_dimensions']}"
        )
        print(
            f"Similarity: {output['similarity_percent']}% "
            f"(mean absolute error {output['mean_absolute_error']}; "
            f"average-hash distance {output['average_hash_distance']}/64)"
        )
        return

    if output.get("source_path"):
        print(f"Source: {output['source_path']} [{output['source_format']}]")
    print(f"Image: {output['path']} [{output['format']} {output['mime_type']}]")
    print(
        f"Dimensions: {output['width']}x{output['height']} | mode: {output['mode']} | "
        f"frames: {output['frame_count']} | alpha: {output['has_alpha']}"
    )
    print(f"Size: {output['size_bytes']} bytes | sha256: {output['sha256']}")
    if output.get("operations"):
        print(f"Operations: {', '.join(output['operations'])}")
    if command == "inspect":
        print(
            f"Average color: {output['average_color']} | "
            f"dominant: {', '.join(output.get('dominant_colors') or []) or '(none)'}"
        )
        print(f"Average hash: {output['average_hash']}")
        return
    if command == "analyze":
        print(
            f"Sent: {output['sent_size_bytes']} bytes as {output['sent_mime_type']} "
            f"(converted/resized: {output['sent_converted_or_resized']})"
        )
        print(f"Provider: {output['provider']} | model: {output['model']}")
        print("Answer:")
        print(output["answer"])


def _run_image_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.image_command is None:
        print(
            "Usage: pc-agent image inspect|convert|transform|compare|analyze ...",
            file=sys.stderr,
        )
        return 2
    _require_license(settings, FEATURE_DOCUMENTS)
    if args.image_command == "analyze":
        _require_license(settings, FEATURE_PROVIDERS)

    registry = build_default_registry()
    context = _tool_context(settings)
    command = args.image_command
    if command == "inspect":
        tool_id = "image.inspect"
        arguments: dict[str, Any] = {
            "path": args.path,
            "include_color_stats": bool(args.include_color_stats),
        }
    elif command == "convert":
        tool_id = "image.convert"
        arguments = {
            "source": args.source,
            "output": args.output,
            "quality": args.quality,
        }
    elif command == "transform":
        tool_id = "image.transform"
        arguments = {"source": args.source, "output": args.output}
        for field_name in (
            "width",
            "height",
            "max_dimension",
            "crop_left",
            "crop_top",
            "crop_right",
            "crop_bottom",
        ):
            value = getattr(args, field_name)
            if value is not None:
                arguments[field_name] = value
        if args.rotate_degrees:
            arguments["rotate_degrees"] = args.rotate_degrees
        if args.flip != "none":
            arguments["flip"] = args.flip
        if args.grayscale:
            arguments["grayscale"] = True
        arguments["quality"] = args.quality
    elif command == "compare":
        tool_id = "image.compare"
        arguments = {"first": args.first, "second": args.second}
    elif command == "analyze":
        route = " -> ".join(build_model_router(settings).provider_names)
        summary = (
            f"Send workspace image {args.path!r} and exact question "
            f"{args.question!r} to the configured provider route ({route}). "
            "External providers will receive the image bytes; the offline mock cannot analyze it."
        )
        retry_hint = (
            f"pc-agent image analyze {_hint_quote(args.path)} "
            f"--question {_hint_quote(args.question)} "
            f"--max-output-tokens {args.max_output_tokens} --yes"
        )
        if not _confirm_direct_write(
            summary,
            preapproved=bool(args.yes),
            retry_hint=retry_hint,
            heading="Approval required to send this exact image to a vision provider:",
        ):
            print("Nothing was executed.")
            return 4
        tool_id = "image.analyze"
        arguments = {
            "path": args.path,
            "question": args.question,
            "max_output_tokens": args.max_output_tokens,
        }
    else:  # pragma: no cover - argparse limits choices.
        return 2

    result = registry.invoke(tool_id, arguments, context)
    if args.json:
        print(json.dumps(_tool_result_to_dict(result), indent=2, default=str))
    elif result.success and result.output is not None:
        _print_image_output(command, dict(result.output))
    if not result.success:
        print(f"Error: {result.error}", file=sys.stderr)
        return 1
    return 0


def _run_desktop_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.desktop_command is None:
        print(
            "Usage: pc-agent desktop info|open|open-url|windows|inspect|focus|click|type|keys|screenshot ...",
            file=sys.stderr,
        )
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

    if args.desktop_command in {"open", "open-url"}:
        context = _tool_context(settings, allow_desktop_open=True)
        if args.desktop_command == "open":
            result = registry.invoke(
                "desktop.open_path", {"path": args.path}, context
            )
        else:
            result = registry.invoke(
                "desktop.open_url", {"url": args.url}, context
            )
        if not result.success or result.output is None:
            return _report_tool_failure(result)
        _print_direct_tool_output(result.output)
        return 0

    command = args.desktop_command
    context = _tool_context(settings)
    if command == "windows":
        arguments: dict[str, Any] = {
            "max_windows": args.max_windows,
            "include_untitled": bool(args.include_untitled),
        }
        tool_id = "desktop.windows"
    elif command == "inspect":
        arguments = _desktop_window_arguments(args)
        arguments.update(
            {
                "max_elements": args.max_elements,
                "include_offscreen": bool(args.include_offscreen),
            }
        )
        tool_id = "desktop.window_inspect"
    elif command == "screenshot":
        arguments = {"output_path": args.output, **_desktop_window_arguments(args)}
        tool_id = "desktop.screenshot"
    elif command in {"focus", "click", "type", "keys"}:
        arguments = _desktop_window_arguments(args)
        include_element = command in {"click", "type"}
        selector_hint = _desktop_selector_hint(args, include_element=include_element)
        if command == "focus":
            tool_id = "desktop.window_focus"
            summary = f"Focus the desktop window selected by: {selector_hint or '(no selector)'}"
            retry_hint = f"pc-agent desktop focus {selector_hint} --yes".strip()
        elif command == "click":
            arguments.update(_desktop_element_arguments(args))
            tool_id = "desktop.window_click"
            summary = f"Click the desktop UIA element selected by: {selector_hint or '(no selector)'}"
            retry_hint = f"pc-agent desktop click {selector_hint} --yes".strip()
        elif command == "type":
            arguments.update(_desktop_element_arguments(args))
            arguments["text"] = args.text
            tool_id = "desktop.window_type"
            summary = (
                f"Replace the selected desktop element value with exact text "
                f"{args.text!r}; selectors: {selector_hint or '(no selector)'}"
            )
            retry_hint = (
                f"pc-agent desktop type {selector_hint} --text {_hint_quote(args.text)} --yes"
            ).strip()
        else:
            arguments.update({"keys": args.keys, "repeat": args.repeat})
            tool_id = "desktop.send_keys"
            summary = (
                f"Send exact key chord {args.keys!r} x{args.repeat} to the desktop "
                f"window selected by: {selector_hint or '(no selector)'}"
            )
            retry_hint = (
                f"pc-agent desktop keys {selector_hint} {_hint_quote(args.keys)} "
                f"--repeat {args.repeat} --yes"
            ).strip()
        if not _confirm_direct_write(
            summary,
            preapproved=bool(args.yes),
            retry_hint=retry_hint,
            heading="Approval required for this exact desktop action:",
        ):
            print("Nothing was executed.")
            return 4
        result = registry.invoke(tool_id, arguments, context)
        if args.json:
            print(json.dumps(_tool_result_to_dict(result), indent=2, default=str))
        elif result.success and result.output is not None:
            _print_desktop_automation_output(command, dict(result.output))
        if not result.success:
            print(f"Error: {result.error}", file=sys.stderr)
            return 1
        return 0
    else:
        return 2

    result = registry.invoke(tool_id, arguments, context)
    if args.json:
        print(json.dumps(_tool_result_to_dict(result), indent=2, default=str))
    elif result.success and result.output is not None:
        _print_desktop_automation_output(command, dict(result.output))
    if not result.success:
        print(f"Error: {result.error}", file=sys.stderr)
        return 1
    return 0


def _parse_form_fields(raw_fields: Any) -> tuple[tuple[str, str], ...]:
    parsed: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in raw_fields or ():
        text = str(raw)
        if "=" not in text:
            raise PcAgentError("--form-field must use Name=Value.")
        name, value = text.split("=", 1)
        name = name.strip()
        if not name:
            raise PcAgentError("--form-field name must not be empty.")
        if name in seen:
            raise PcAgentError(f"Duplicate --form-field value for {name!r}.")
        seen.add(name)
        parsed.append((name, value))
    return tuple(parsed)


def _planner_options(args: argparse.Namespace) -> PlannerOptions:
    raw_command = getattr(args, "run_command_text", None)
    try:
        command_parts = shlex.split(raw_command) if raw_command else []
    except ValueError as exc:
        raise PcAgentError(f"--command could not be parsed: {exc}") from exc
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
        command_executable=command_parts[0] if command_parts else None,
        command_arguments=tuple(command_parts[1:]),
        command_timeout=getattr(args, "command_timeout", None),
        browser_url=getattr(args, "browser_open", None),
        browser_click_selector=getattr(args, "browser_click_selector", None),
        browser_click_text=getattr(args, "browser_click_text", None),
        browser_type_selector=getattr(args, "browser_type_selector", None),
        browser_type_text=getattr(args, "browser_type_text", None),
        desktop_window_title_contains=getattr(
            args, "desktop_window_title_contains", None
        ),
        desktop_window_handle=getattr(args, "desktop_window_handle", None),
        desktop_click_name=getattr(args, "desktop_click_name", None),
        desktop_click_automation_id=getattr(
            args, "desktop_click_automation_id", None
        ),
        desktop_type_name=getattr(args, "desktop_type_name", None),
        desktop_type_automation_id=getattr(
            args, "desktop_type_automation_id", None
        ),
        desktop_type_text=getattr(args, "desktop_type_text", None),
        desktop_keys=getattr(args, "desktop_keys", None),
        image_width=getattr(args, "image_width", None),
        image_height=getattr(args, "image_height", None),
        image_max_dimension=getattr(args, "image_max_dimension", None),
        image_crop_left=getattr(args, "image_crop_left", None),
        image_crop_top=getattr(args, "image_crop_top", None),
        image_crop_right=getattr(args, "image_crop_right", None),
        image_crop_bottom=getattr(args, "image_crop_bottom", None),
        image_rotate_degrees=getattr(args, "image_rotate_degrees", None),
        image_flip=getattr(args, "image_flip", None),
        image_grayscale=bool(getattr(args, "image_grayscale", False)),
        image_quality=getattr(args, "image_quality", None),
        image_question=getattr(args, "image_question", None),
        image_compare_path=getattr(args, "image_compare_path", None),
        pdf_rotate_degrees=getattr(args, "pdf_rotate_degrees", None),
        pdf_case_sensitive=bool(getattr(args, "pdf_case_sensitive", False)),
        pdf_max_results=getattr(args, "pdf_max_results", None),
        pdf_ocr_dpi=getattr(args, "pdf_ocr_dpi", None),
        pdf_ocr_language=getattr(args, "pdf_ocr_language", None),
        pdf_ocr_skip_text_pages=not bool(getattr(args, "pdf_ocr_all_pages", False)),
        pdf_ocr_max_output_tokens=getattr(
            args, "pdf_ocr_max_output_tokens", None
        ),
        pdf_table_min_rows=getattr(args, "pdf_table_min_rows", None),
        pdf_table_min_columns=getattr(args, "pdf_table_min_columns", None),
        pdf_form_values=_parse_form_fields(getattr(args, "form_fields", [])),
        token_budget=getattr(args, "token_budget", None),
        provider_call_budget=getattr(args, "provider_call_budget", None),
        tool_budget=getattr(args, "tool_budget", None),
        step_budget=getattr(args, "step_budget", None),
        task_time_budget_seconds=getattr(args, "task_time_budget_seconds", None),
    )


def _approval_callback(*, preapproved: bool):
    def decide(request: ApprovalRequest) -> bool:
        if request.action in {
            "run_command",
            "browser_action",
            "desktop_automation_action",
            "vision_analysis",
        }:
            headings = {
                "run_command": "Approval required to run this exact command (no shell):",
                "browser_action": "Approval required for this exact action in your debug browser:",
                "desktop_automation_action": "Approval required for this exact action in a Windows application:",
                "vision_analysis": "Approval required to send this exact image to a vision provider:",
            }
            if request.plan.tool_id == "pdf.ocr":
                headings["vision_analysis"] = (
                    "Approval required to send rendered PDF pages to a vision provider:"
                )
            print(headings[request.action])
            print(f"Task: {request.task_id}")
            print(f"Plan: {request.plan.summary}")
            print(f"Tool: {request.plan.tool_id}")
            print("Arguments:")
            print(
                json.dumps(
                    dict(request.plan.arguments),
                    indent=2,
                    sort_keys=True,
                    default=str,
                )
            )
            details = {
                "run_command": (
                    "The working directory stays inside the workspace; "
                    "time and output are bounded."
                ),
                "browser_action": (
                    "This acts only in the browser session you started "
                    "with a debugging port."
                ),
                "desktop_automation_action": (
                    "This acts only on the exact selected application window "
                    "through Windows UI Automation."
                ),
                "vision_analysis": (
                    "Only the exact selected PDF pages that need OCR are rendered and sent to the configured "
                    "provider route; external providers receive those page-image bytes."
                    if request.plan.tool_id == "pdf.ocr"
                    else "Only this exact image/question is sent to the configured "
                    "provider route; external providers receive the image bytes."
                ),
            }
            print(details[request.action])
        else:
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
        outcome = _ask_confirmation("APPROVE", prompt=_APPROVE_PROMPT)
        if outcome is None:
            raise ApprovalUnavailableError(
                "No interactive approval input is available. The task remains "
                "waiting_for_approval; run it again in an interactive terminal or with --yes."
            )
        print(f"Decision: {'APPROVED' if outcome else 'DENIED'}")
        if not outcome:
            print(
                "Tip: only the word APPROVE (typed after the prompt) "
                "approves; or re-run the same command with --yes."
            )
        return outcome

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
        "plan_summary": report.plan_summary,
        "steps_total": report.steps_total,
        "budget": report.budget.to_dict() if report.budget else None,
        "steps": [
            {
                "step": step.index + 1,
                "tool_id": step.tool_id,
                "summary": step.summary,
                "status": step.status,
                "verification_valid": step.verification_valid,
                "verification_message": step.verification_message,
                "error": step.error,
                "tool_result": _tool_result_to_dict(step.tool_result),
                "resumed_from_checkpoint": step.resumed_from_checkpoint,
            }
            for step in report.step_reports
        ],
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
        if report.step_reports:
            print(f"Plan: {report.plan_summary}")
            if report.plan:
                print(f"Planner: {report.plan.planner}")
            for step in report.step_reports:
                print(
                    f"Step {step.index + 1}/{report.steps_total} "
                    f"[{step.status}] {step.tool_id} - {step.summary}"
                )
        elif report.plan:
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
        if report.budget is not None:
            print(f"Budget: {report.budget.summary()}")
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
    elif tool_id == "pdf.extract_text":
        print(f"Extracted PDF text: {output['output_path']}")
        print(
            f"Pages: {output['page_count']} of {output['source_pages']} | "
            f"characters: {output['characters']} | SHA-256: {output['sha256']}"
        )
    elif tool_id == "pdf.search_text":
        print(
            f"PDF occurrences: {output['total_occurrences']} | matching pages: "
            f"{', '.join(str(page) for page in output['matching_pages']) or 'none'}"
        )
        for match in output["matches"]:
            print(f"  Page {match['page']}: {match['snippet']}")
    elif tool_id in {"pdf.reorder_pages", "pdf.rotate_pages"}:
        print(f"Transformed PDF: {output['output_path']}")
        print(
            f"Pages: {output['page_count']} | rotations: "
            f"{', '.join(str(value) for value in output['page_rotations'])} | "
            f"SHA-256: {output['sha256']}"
        )


def _credential_store(settings: Settings):
    return build_credential_store(settings.credential_store)


def _settings_api_key(settings: Settings, provider: str) -> str | None:
    canonical = canonical_credential_provider(provider)
    return {
        "gemini": settings.gemini_api_key,
        "openrouter": settings.openrouter_api_key,
        "openai": settings.openai_api_key,
        "openai-compatible": settings.openai_compatible_api_key,
        "ollama": settings.ollama_api_key,
        "anthropic": settings.anthropic_api_key,
    }[canonical]


def _credential_status_text(settings: Settings) -> str:
    try:
        return _credential_store(settings).status().summary()
    except PcAgentError as exc:
        return (
            f"mode={settings.credential_store}; "
            f"backend={settings.credential_store_backend}; "
            f"available=no; stored=unknown; {exc}"
        )


def _confirm_self_repair_action(summary: str, *, assume_yes: bool) -> bool:
    return _confirm_direct_write(
        summary,
        preapproved=assume_yes,
        heading="Approval required for this exact self-repair operation:",
    )


def _print_self_repair_diagnosis(diagnosis: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(diagnosis.to_dict(), indent=2))
        return
    print(f"Self-repair diagnosis: {diagnosis.summary()}")
    if diagnosis.issues:
        print("Issues:")
        for issue in diagnosis.issues:
            print(f"  - [{issue.severity}] {issue.code} {issue.path}: {issue.detail}")
    else:
        print("Issues: none")
    if diagnosis.operations:
        print("Planned deterministic repair operations:")
        for operation in diagnosis.operations:
            print(f"  - {operation.kind} {operation.path}")
    if diagnosis.extra_files:
        print("Extra eligible files (reported only; never deleted automatically):")
        for path in diagnosis.extra_files:
            print(f"  - {path}")
    if diagnosis.blocked:
        print("Stage/apply are blocked by the baseline issue above.")


def _print_self_repair_run(run: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(run.to_dict(), indent=2))
        return
    print(f"Self-repair run: {run.run_id}")
    print(f"Status: {run.status}")
    print(f"Check profile: {run.check_profile}")
    if run.operations:
        print("Operations:")
        for operation in run.operations:
            print(f"  - {operation.kind} {operation.path}")
    for label, checks in (("Staged", run.checks), ("Post-apply", run.post_checks)):
        if checks:
            print(f"{label} checks:")
            for check in checks:
                state = "PASS" if check.success else "FAIL"
                detail = check.output.strip().splitlines()[-1] if check.output.strip() else ""
                print(f"  - [{state}] {check.name}: {detail[:500]}")
    if run.backups:
        print(f"Backed-up files: {len(run.backups)}")
    if run.error:
        print(f"Error: {run.error}", file=sys.stderr)


def _run_self_repair_command(settings: Settings, args: argparse.Namespace) -> int:
    if not settings.allow_self_repair:
        raise PcAgentError(
            "Self-repair is disabled. Set PC_AGENT_ALLOW_SELF_REPAIR=1 in .env "
            "to enable core baseline diagnosis and repair."
        )
    service = SelfRepairService(settings.root_dir, settings.data_dir)
    command = getattr(args, "self_repair_command", None) or "diagnose"
    if command == "baseline":
        if not _confirm_self_repair_action(
            "capture self-repair baseline "
            f"(checks={args.check}, force={'yes' if args.force else 'no'})",
            assume_yes=bool(args.yes),
        ):
            print("Self-repair baseline capture cancelled.")
            return 3
        baseline = service.create_baseline(
            force=bool(args.force), check_profile=args.check
        )
        if args.json:
            print(json.dumps(baseline.to_dict(), indent=2))
        else:
            print(f"Self-repair baseline captured: {baseline.summary()}")
        return 0
    if command == "diagnose":
        diagnosis = service.diagnose()
        _print_self_repair_diagnosis(diagnosis, as_json=bool(args.json))
        return 1 if diagnosis.blocked else 0
    if command == "stage":
        run = service.stage(check_profile=args.check)
        _print_self_repair_run(run, as_json=bool(args.json))
        return 0 if run.status == "staged" else 1
    if command == "apply":
        run_id = service.resolve_run_id(args.run_id)
        if not _confirm_self_repair_action(
            f"apply self-repair run {run_id}", assume_yes=bool(args.yes)
        ):
            print("Self-repair apply cancelled.")
            return 3
        run = service.apply(run_id, post_check_profile=args.check)
        _print_self_repair_run(run, as_json=bool(args.json))
        return 0 if run.status == "applied" else 1
    if command == "rollback":
        run_id = service.resolve_run_id(args.run_id)
        if not _confirm_self_repair_action(
            f"rollback self-repair run {run_id}", assume_yes=bool(args.yes)
        ):
            print("Self-repair rollback cancelled.")
            return 3
        run = service.rollback(run_id)
        _print_self_repair_run(run, as_json=bool(args.json))
        return 0
    if command == "list":
        runs = service.list_runs(limit=args.limit)
        if args.json:
            print(json.dumps([run.to_dict() for run in runs], indent=2))
        elif not runs:
            print("No self-repair runs recorded.")
        else:
            for run in runs:
                print(f"- {run.run_id} [{run.status}] operations={len(run.operations)}")
        return 0
    if command == "show":
        run_id = service.resolve_run_id(args.run_id)
        _print_self_repair_run(service.load_run(run_id), as_json=bool(args.json))
        return 0
    raise PcAgentError(f"Unknown self-repair command: {command}")


def _resolve_cli_path(
    settings: Settings, raw: Path, *, existing: bool = False
) -> Path:
    path = raw.expanduser()
    if path.is_absolute():
        return path.resolve()
    if existing:
        for base in (Path.cwd(), settings.root_dir, settings.workspace_dir):
            candidate = (base / path).resolve()
            if candidate.is_file():
                return candidate
    return (settings.root_dir / path).resolve()


def _print_release_audit(audit: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(audit.to_dict(), indent=2))
        return
    print(f"Release audit: {audit.summary()}")
    for check in audit.checks:
        print(f"  [{check.status.upper()}] {check.name}: {check.detail}")


def _print_release_bundle(
    info: Any, *, as_json: bool, files: bool = False, built: bool = False
) -> None:
    if as_json:
        print(json.dumps(info.to_dict(), indent=2))
        return
    verb = "built and verified" if built else "verified"
    print(f"Release bundle {verb}: {info.path}")
    print(f"Version: {info.version}")
    print(f"Files: {info.file_count}")
    print(f"Bundled bytes: {info.total_bytes}")
    print(f"Bundle SHA-256: {info.bundle_sha256}")
    if files:
        print("Bundled files:")
        for record in info.files:
            print(f"  - {record.path} ({record.size} bytes)")


def _run_release_command(settings: Settings, args: argparse.Namespace) -> int:
    command = getattr(args, "release_command", None)
    if command is None:
        print(
            "Usage: pc-agent release audit|build|verify|info ...",
            file=sys.stderr,
        )
        return 2
    if command == "audit":
        audit = audit_install(settings)
        _print_release_audit(audit, as_json=bool(args.json))
        if audit.failures or (args.strict and audit.warnings):
            return 1
        return 0
    if command == "build":
        settings.ensure_directories()
        output = (
            _resolve_cli_path(settings, args.output)
            if args.output is not None
            else settings.workspace_dir
            / f"self-healing-pc-agent-v{__version__}-portable.zip"
        )
        info = build_release_bundle(settings.root_dir, output)
        _print_release_bundle(info, as_json=bool(args.json), built=True)
        return 0
    if command in {"verify", "info"}:
        bundle = _resolve_cli_path(settings, args.bundle, existing=True)
        info = verify_release_bundle(bundle)
        _print_release_bundle(
            info,
            as_json=bool(args.json),
            files=bool(getattr(args, "files", False)),
        )
        return 0
    raise PcAgentError(f"Unknown release command: {command}")


def _confirm_update_action(summary: str, *, assume_yes: bool) -> bool:
    return _confirm_direct_write(
        summary,
        preapproved=assume_yes,
        heading="Approval required for this exact offline update operation:",
    )


def _print_update_check(check: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(check.to_dict(), indent=2))
        return
    print(f"Update check: {check.summary()}")
    print(f"Bundle: {check.bundle.path}")
    print(f"Files: {check.bundle.file_count}")
    print(f"Bundled bytes: {check.bundle.total_bytes}")
    print(f"Bundle SHA-256: {check.bundle.bundle_sha256}")
    print(f"Signature: {check.signature.summary()}")
    if check.signature.path:
        print(f"Signature file: {check.signature.path}")


def _print_staged_update(update: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(update.to_dict(), indent=2))
        return
    print(f"Offline update: {update.summary()}")
    print(f"Receipt: {update.receipt_path}")
    print(f"Staging: {update.staging_dir}")
    print(f"Backup: {update.backup_dir}")
    print(f"Bundle SHA-256: {update.bundle_sha256}")
    if update.target_root:
        print(f"Target root: {update.target_root}")
    if update.status in {"applied", "rolled-back", "failed"}:
        print(
            "Files changed by receipt: "
            f"created={update.created_count}; replaced={update.replaced_count}"
        )
    if update.stale_files:
        print(
            "Existing release files not present in this bundle were preserved "
            f"(not deleted): {len(update.stale_files)}"
        )
        for path in update.stale_files[:20]:
            print(f"  - {path}")
        if len(update.stale_files) > 20:
            print("  - ... see the update receipt for the complete list")
    if update.last_error:
        print(f"Last error: {update.last_error}", file=sys.stderr)


def _print_online_update_check(
    check: OnlineUpdateCheck, *, as_json: bool
) -> None:
    if as_json:
        print(json.dumps(check.to_dict(), indent=2))
        return
    print(f"Online update check: {check.summary()}")
    print(f"Feed manifest: {check.manifest.summary()}")
    print(f"Feed SHA-256: {check.feed_sha256}")
    print(
        "Bundle signature: not verified by online-check; "
        "run online-download or online-stage to download and verify it."
    )


def _print_downloaded_update(
    downloaded: DownloadedUpdate, *, as_json: bool
) -> None:
    if as_json:
        print(json.dumps(downloaded.to_dict(), indent=2))
        return
    print(f"Online update download: {downloaded.summary()}")
    print(f"Bundle: {downloaded.bundle_path}")
    if downloaded.signature_path:
        print(f"Signature: {downloaded.signature_path}")
    print(f"Feed: {downloaded.feed_path}")
    print(f"Receipt: {downloaded.receipt_path}")
    print(f"Verified bundle SHA-256: {downloaded.check.bundle.bundle_sha256}")
    print(f"Signature check: {downloaded.check.signature.summary()}")


def _print_online_stage_result(
    result: OnlineStageResult, *, as_json: bool
) -> None:
    if as_json:
        print(json.dumps(result.to_dict(), indent=2))
        return
    _print_downloaded_update(result.downloaded, as_json=False)
    _print_staged_update(result.staged, as_json=False)


def _run_update_command(settings: Settings, args: argparse.Namespace) -> int:
    command = getattr(args, "update_command", None)
    if command is None:
        print(
            "Usage: pc-agent update check|stage|online-check|online-download|"
            "online-stage|apply|rollback|list|show ...",
            file=sys.stderr,
        )
        return 2
    if command == "check":
        bundle = _resolve_cli_path(settings, args.bundle, existing=True)
        signature = (
            _resolve_cli_path(settings, args.signature, existing=True)
            if args.signature is not None
            else None
        )
        check = check_update(bundle, settings, signature_path=signature)
        _print_update_check(check, as_json=bool(args.json))
        return 1 if check.signature.status in {"invalid", "key-mismatch"} else 0
    if command == "stage":
        settings.ensure_directories()
        bundle = _resolve_cli_path(settings, args.bundle, existing=True)
        signature = (
            _resolve_cli_path(settings, args.signature, existing=True)
            if args.signature is not None
            else None
        )
        staged = stage_update(
            bundle,
            settings,
            signature_path=signature,
            allow_unsigned=bool(args.allow_unsigned),
            allow_downgrade=bool(args.allow_downgrade),
        )
        _print_staged_update(staged, as_json=bool(args.json))
        if not args.json:
            print(f"Next: pc-agent update apply {staged.update_id}")
        return 0
    if command == "online-check":
        online_check = check_online_update(
            settings,
            feed_url=args.feed_url,
            channel=args.channel,
        )
        _print_online_update_check(online_check, as_json=bool(args.json))
        return 0
    if command == "online-download":
        settings.ensure_directories()
        downloaded = download_online_update(
            settings,
            feed_url=args.feed_url,
            channel=args.channel,
            allow_downgrade=bool(args.allow_downgrade),
        )
        _print_downloaded_update(downloaded, as_json=bool(args.json))
        if not args.json and downloaded.status == "downloaded":
            print(
                "Next: pc-agent update stage "
                f"{downloaded.bundle_path} --signature {downloaded.signature_path}"
                if downloaded.signature_path
                else f"Next: pc-agent update stage {downloaded.bundle_path}"
            )
        return 0 if downloaded.status == "downloaded" else 1
    if command == "online-stage":
        settings.ensure_directories()
        result = stage_online_update(
            settings,
            feed_url=args.feed_url,
            channel=args.channel,
            allow_unsigned=bool(args.allow_unsigned),
            allow_downgrade=bool(args.allow_downgrade),
        )
        _print_online_stage_result(result, as_json=bool(args.json))
        if not args.json:
            print(f"Next: pc-agent update apply {result.staged.update_id}")
        return 0
    if command == "apply":
        settings.ensure_directories()
        staged = get_update(settings, args.update_id, status="staged")
        summary = (
            f"apply staged offline update {staged.update_id} "
            f"({staged.current_version} -> {staged.bundle_version}, "
            f"files={staged.file_count}) to {settings.root_dir}"
        )
        confirmed = bool(args.yes) if args.json else _confirm_update_action(
            summary, assume_yes=bool(args.yes)
        )
        if not confirmed:
            print("Offline update apply cancelled.")
            return 3
        applied = apply_staged_update(settings, staged.update_id)
        _print_staged_update(applied, as_json=bool(args.json))
        if not args.json:
            print(
                "Next: reinstall editable metadata with "
                "pip install -e .[dev,gui], restart pc-agent, then run "
                "pc-agent doctor."
            )
        return 0
    if command == "rollback":
        settings.ensure_directories()
        applied = get_update(settings, args.update_id, status="applied")
        summary = (
            f"roll back applied offline update {applied.update_id} "
            f"(bundle version {applied.bundle_version}, "
            f"files={applied.file_count}) in {settings.root_dir}"
        )
        confirmed = bool(args.yes) if args.json else _confirm_update_action(
            summary, assume_yes=bool(args.yes)
        )
        if not confirmed:
            print("Offline update rollback cancelled.")
            return 3
        rolled_back = rollback_update(settings, applied.update_id)
        _print_staged_update(rolled_back, as_json=bool(args.json))
        if not args.json:
            print(
                "Next: reinstall editable metadata with "
                "pip install -e .[dev,gui], restart pc-agent, then run "
                "pc-agent doctor."
            )
        return 0
    if command == "list":
        if args.limit < 1 or args.limit > 200:
            raise PcAgentError("Update list --limit must be between 1 and 200.")
        updates = list_updates(settings)[: args.limit]
        if args.json:
            print(json.dumps([update.to_dict() for update in updates], indent=2))
        elif not updates:
            print("No offline updates recorded.")
        else:
            for update in updates:
                print(f"- {update.summary()}")
        return 0
    if command == "show":
        update = get_update(settings, args.update_id)
        _print_staged_update(update, as_json=bool(args.json))
        return 0
    raise PcAgentError(f"Unknown update command: {command}")


def _run_maintenance_command(settings: Settings, args: argparse.Namespace) -> int:
    command = getattr(args, "maintenance_command", None)
    if command is None:
        print(
            "Usage: pc-agent maintenance backup|list|verify|restore ...",
            file=sys.stderr,
        )
        return 2
    settings.ensure_directories()
    service = DatabaseMaintenanceService(settings.database_path, settings.data_dir)
    if command == "backup":
        _open_database(settings)
        info = service.create_backup(args.label)
        if args.json:
            print(json.dumps(info.to_dict(), indent=2))
        else:
            print(f"Database backup created and verified: {info.summary()}")
            print(f"Backup file: {info.path}")
        return 0
    if command == "list":
        backups = service.list_backups()
        if args.json:
            print(json.dumps([backup.to_dict() for backup in backups], indent=2))
        elif not backups:
            print("No database backups recorded.")
        else:
            for backup in backups:
                print(f"- {backup.summary()} label={backup.label} created={backup.created_at}")
        return 0
    if command == "verify":
        info = service.verify_backup(args.backup)
        if args.json:
            print(json.dumps(info.to_dict(), indent=2))
        else:
            print(f"Database backup verified: {info.summary()}")
        return 0
    if command == "restore":
        info = service.verify_backup(args.backup)
        confirmed = bool(args.yes) if args.json else _confirm_direct_write(
            "restore database backup "
            f"{info.backup_id} (schema v{info.schema_version}); "
            "a pre-restore backup of the live database will be created first",
            preapproved=bool(args.yes),
            heading="Approval required for this exact database restore:",
        )
        if not confirmed:
            print("Database restore cancelled; the live database is unchanged.")
            return 3
        result = service.restore_backup(info.backup_id)
        database = _open_database(settings)
        payload = result.to_dict()
        payload["live_schema_version"] = database.schema_version()
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"Database restored: {result.restored.summary()}")
            print(f"Pre-restore backup: {result.pre_restore_backup_id or 'none'}")
            print(f"Live database schema: v{database.schema_version()}")
        return 0
    raise PcAgentError(f"Unknown maintenance command: {command}")


def _run_diagnostics_command(settings: Settings, args: argparse.Namespace) -> int:
    command = getattr(args, "diagnostics_command", None) or "export"
    if command != "export":
        raise PcAgentError(f"Unknown diagnostics command: {command}")
    database = _open_database(settings)
    output = (
        _resolve_cli_path(settings, args.output) if args.output is not None else None
    )
    info = export_diagnostics(settings, output, database)
    if args.json:
        print(json.dumps(info.to_dict(), indent=2))
    else:
        print(f"Diagnostics exported: {info.path}")
        print(f"Bytes: {info.size}")
        print(f"SHA-256: {info.sha256}")
        print(
            "Privacy check: the file contains paths and counts, but no API keys, "
            "task instructions, memory values or document contents. Inspect it "
            "before sharing."
        )
    return 0


def _run_credentials_command(settings: Settings, args: argparse.Namespace) -> int:
    command = args.credentials_command
    if command is None:
        print(
            "Usage: pc-agent credentials status|list|set|delete|migrate ...",
            file=sys.stderr,
        )
        return 2

    store = _credential_store(settings)
    sources = dict(settings.credential_sources)

    if command in {"status", "list"}:
        status = store.status()
        effective_sources = {
            provider: sources.get(provider, "missing")
            for provider in SUPPORTED_CREDENTIAL_PROVIDERS
        }
        payload = {
            "mode": status.mode,
            "backend": status.backend,
            "available": status.available,
            "detail": status.detail,
            "stored_providers": list(status.stored_providers),
            "effective_sources": effective_sources,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
            return 0
        print(f"Credential store: {status.summary()}")
        if command == "status":
            print(
                "Effective credential sources: "
                + ", ".join(
                    f"{provider}={source}"
                    for provider, source in effective_sources.items()
                    if source != "missing"
                )
                if any(source != "missing" for source in effective_sources.values())
                else "Effective credential sources: none"
            )
        else:
            for provider in SUPPORTED_CREDENTIAL_PROVIDERS:
                print(
                    f"{provider}: stored="
                    f"{'yes' if provider in status.stored_providers else 'no'}; "
                    f"effective={effective_sources[provider]}"
                )
        print(
            "Environment/.env values take precedence; credential values "
            "were not printed."
        )
        return 0

    if command == "set":
        provider = canonical_credential_provider(args.provider)
        if store.contains(provider):
            print(f"A stored {provider} API key already exists and will be replaced.")
        try:
            first = getpass.getpass(f"Enter new {provider} API key (hidden): ")
            second = getpass.getpass(f"Re-enter {provider} API key (hidden): ")
        except (EOFError, KeyboardInterrupt):
            print("Credential entry cancelled; nothing was stored.")
            return 4
        if first != second:
            print("The two API key entries do not match; nothing was stored.", file=sys.stderr)
            return 1
        store.set_api_key(provider, first)
        print(f"Stored {provider} API key in {store.backend_name}.")
        if sources.get(provider) == "environment":
            print(
                "Note: an environment/.env key for this provider still "
                "takes precedence until you remove it."
            )
        print("The credential value was not printed.")
        return 0

    if command == "delete":
        provider = canonical_credential_provider(args.provider)
        if not store.contains(provider):
            print(f"No stored {provider} API key was found; nothing was deleted.")
            return 0
        if not _confirm_direct_write(
            f"Delete the stored {provider} API key from {store.backend_name}",
            preapproved=bool(args.yes),
            retry_hint=f"pc-agent credentials delete {provider} --yes",
            heading="Approval required to delete this stored API key:",
        ):
            print("Nothing was deleted.")
            return 4
        deleted = store.delete_api_key(provider)
        print(
            f"Deleted stored {provider} API key."
            if deleted
            else f"No stored {provider} API key was found; nothing was deleted."
        )
        return 0

    if command == "migrate":
        selected = (
            (canonical_credential_provider(args.provider),)
            if args.provider
            else SUPPORTED_CREDENTIAL_PROVIDERS
        )
        candidates = tuple(
            provider
            for provider in selected
            if sources.get(provider) == "environment"
            and _settings_api_key(settings, provider)
        )
        if not candidates:
            print(
                "No environment/.env provider API keys are available to migrate."
            )
            return 0
        retry_hint = "pc-agent credentials migrate"
        if args.provider:
            retry_hint += f" {selected[0]}"
        retry_hint += " --yes"
        if not _confirm_direct_write(
            "Copy environment/.env API keys for "
            + ", ".join(candidates)
            + f" into {store.backend_name}",
            preapproved=bool(args.yes),
            retry_hint=retry_hint,
            heading="Approval required to copy these API keys into secure OS storage:",
        ):
            print("Nothing was migrated.")
            return 4
        for provider in candidates:
            secret = _settings_api_key(settings, provider)
            assert secret is not None  # checked when building candidates
            store.set_api_key(provider, secret)
            print(f"Migrated {provider} API key.")
        print(
            "Your .env was not changed. After verifying the provider route, "
            "remove the migrated *_API_KEY lines from .env yourself."
        )
        print("Credential values were not printed.")
        return 0

    return 2


def _run_providers_command(settings: Settings, args: argparse.Namespace) -> int:
    if args.providers_command != "list":
        print("Usage: pc-agent providers list", file=sys.stderr)
        return 2
    for status in provider_statuses(settings):
        route = "in-route" if status.in_route else "not-in-route"
        configured = "configured" if status.configured else "not-configured"
        implemented = "adapter" if status.implemented else "reserved"
        source = dict(settings.credential_sources).get(status.name, "missing")
        if status.name == "mock":
            source = "not-applicable"
        print(
            f"{status.name} [{route}; {configured}; {implemented}] "
            f"model={status.model} source={source} - {status.detail}"
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
