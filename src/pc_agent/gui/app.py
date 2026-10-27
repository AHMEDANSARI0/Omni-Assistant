"""Tkinter desktop application for the Self-Healing PC Agent.

Phase 5 added a local graphical front-end over the already tested services;
Phase 6 adds license status/activation on the Dashboard.  It deliberately
does not add shell access, screenshots, clicking/typing in other
applications, credential/API-key editing or arbitrary GUI automation.
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import __version__
from ..config import Settings
from ..domain.task import TaskStatus
from ..errors import PcAgentError
from ..licensing import FEATURE_GUI, LicenseService
from ..logging_config import configure_logging
from ..storage.db import Database
from ..tools import ToolResult
from .controller import (
    ApprovalBroker,
    ApprovalTicket,
    GuiController,
    TaskRunOptions,
    WebFetchOutcome,
    format_approval_request,
    format_diagnosis,
    format_execution_report,
    format_license_status,
    format_provider_statuses,
    format_recovery_outcome,
    format_recovery_scan,
    format_task_details,
    format_tool,
    format_tool_result,
    parse_json_object,
    parse_line_list,
    parse_optional_int,
)

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk
except ImportError as exc:  # pragma: no cover - depends on the Python build
    tk = None  # type: ignore[assignment]
    filedialog = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    _TK_IMPORT_ERROR: BaseException | None = exc
else:
    _TK_IMPORT_ERROR = None


@dataclass(slots=True)
class _WorkerEvent:
    label: str
    value: Any = None
    error: str | None = None
    on_success: Callable[[Any], None] | None = None


def gui_runtime_info() -> tuple[bool, str]:
    """Report whether Tkinter can be imported, without opening a window."""

    if tk is None:
        return False, f"Tkinter import failed ({_TK_IMPORT_ERROR})"
    return True, f"Tk {tk.TkVersion}"


def gui_check(settings: Settings) -> tuple[bool, str]:
    """Build the GUI controller and summarize readiness without a window."""

    available, runtime = gui_runtime_info()
    controller = GuiController(settings)
    summary = controller.doctor_summary()
    license_summary = controller.license_status().summary
    tools = controller.list_tools(include_disabled=True)
    tasks = controller.list_tasks(limit=50)
    lines = [
        f"Self-Healing PC Agent GUI {__version__}",
        f"Tkinter: {'available' if available else 'unavailable'} ({runtime})",
        f"Root: {settings.root_dir}",
        f"Workspace: {settings.workspace_dir}",
        f"Database: {settings.database_path} (schema v{summary.schema_version})",
        f"Provider route: {' -> '.join(summary.provider_route)}",
        f"Registered tools loaded: {len(tools)}",
        f"Recent tasks loaded: {len(tasks)} (limit 50)",
        (
            "TLS trust: verified; "
            f"CA certificates={summary.tls_ca_certificates}; "
            f"sources={', '.join(summary.tls_sources)}"
            if summary.tls_verified
            else "TLS trust: unavailable; HTTPS will fail closed"
        ),
        f"License: {license_summary}",
        "Status: READY (no window opened)" if available else "Status: GUI UNAVAILABLE",
    ]
    return available, "\n".join(lines)


def launch_gui(settings: Settings) -> int:
    """Open the desktop GUI and block until the user closes it."""

    available, runtime = gui_runtime_info()
    if not available or tk is None:
        raise PcAgentError(
            "The desktop GUI is unavailable in this Python installation "
            f"({runtime}). The CLI remains available."
        )
    if settings.license_enforcement != "off":
        LicenseService(settings, Database(settings.database_path)).require(FEATURE_GUI)
    try:
        app = PcAgentGuiApp(settings)
    except tk.TclError as exc:
        raise PcAgentError(
            "The desktop GUI could not open a window. Run it from a signed-in "
            f"desktop session, not a headless shell. Detail: {exc}"
        ) from exc
    app.run()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the windowed ``pc-agent-gui`` launcher."""

    parser = argparse.ArgumentParser(prog="pc-agent-gui")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Application root folder (default: PC_AGENT_HOME or current folder)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check GUI readiness without opening a window",
    )
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env(root_dir=args.root)
    except PcAgentError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)

    try:
        if args.check:
            ready, text = gui_check(settings)
            print(text)
            return 0 if ready else 1
        return launch_gui(settings)
    except PcAgentError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


class PcAgentGuiApp:
    def __init__(self, settings: Settings) -> None:
        if tk is None or ttk is None or messagebox is None or filedialog is None:
            raise PcAgentError("Tkinter is unavailable in this Python installation.")

        self.settings = settings
        self.broker = ApprovalBroker()
        self.controller = GuiController(
            settings, approval_callback=self.broker.decide
        )

        self.root = tk.Tk()
        self.root.title(f"Self-Healing PC Agent {__version__}")
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        window_width = min(1240, max(1020, screen_width - 40))
        window_height = min(820, max(640, screen_height - 60))
        self.root.geometry(f"{window_width}x{window_height}")
        self.root.minsize(1020, 640)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._events: queue.Queue[_WorkerEvent] = queue.Queue()
        self._busy = False
        self._pending_approval: ApprovalTicket | None = None
        self._tools_by_id: dict[str, Any] = {}

        self.header_status_var = tk.StringVar(value="Loading local status...")
        self.status_var = tk.StringVar(value="Starting...")

        self._build_style()
        self._build_layout()
        self.root.after(100, self._poll_events)
        self.root.after(150, self._refresh_all)

    def run(self) -> None:
        self.root.mainloop()

    # ------------------------------------------------------------------
    # Layout construction
    # ------------------------------------------------------------------
    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Subtitle.TLabel", font=("Segoe UI", 9))
        style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

    def _build_layout(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=(12, 10, 12, 6))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(
            header, text="Self-Healing PC Agent", style="Title.TLabel"
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text=f"v{__version__}  |  Root: {self.settings.root_dir}",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w")
        ttk.Label(
            header, textvariable=self.header_status_var, style="Subtitle.TLabel"
        ).grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))

        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)

        self.dashboard_tab = ttk.Frame(self.notebook, padding=10)
        self.tasks_tab = ttk.Frame(self.notebook, padding=10)
        self.documents_tab = ttk.Frame(self.notebook, padding=10)
        self.web_tab = ttk.Frame(self.notebook, padding=10)
        self.desktop_tab = ttk.Frame(self.notebook, padding=10)
        self.tools_tab = ttk.Frame(self.notebook, padding=10)
        self.providers_tab = ttk.Frame(self.notebook, padding=10)

        self.notebook.add(self.dashboard_tab, text="Dashboard")
        self.notebook.add(self.tasks_tab, text="Tasks")
        self.notebook.add(self.documents_tab, text="Documents")
        self.notebook.add(self.web_tab, text="Web")
        self.notebook.add(self.desktop_tab, text="Desktop")
        self.notebook.add(self.tools_tab, text="Tools")
        self.notebook.add(self.providers_tab, text="Providers")

        status = ttk.Frame(self.root, padding=(10, 4, 10, 8))
        status.grid(row=2, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        ttk.Label(status, textvariable=self.status_var).grid(
            row=0, column=0, sticky="w"
        )
        self.progress = ttk.Progressbar(
            status, mode="indeterminate", length=170
        )
        self.progress.grid(row=0, column=1, sticky="e")

        self._build_dashboard_tab()
        self._build_tasks_tab()
        self._build_documents_tab()
        self._build_web_tab()
        self._build_desktop_tab()
        self._build_tools_tab()
        self._build_providers_tab()

    def _build_dashboard_tab(self) -> None:
        tab = self.dashboard_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        actions = ttk.Frame(tab)
        actions.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(actions, text="Refresh Doctor", command=self._refresh_doctor).pack(
            side="left"
        )
        ttk.Button(
            actions, text="Recovery Scan", command=self._run_recovery_scan
        ).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Create PDF Demo", command=self._create_pdf_demo).pack(
            side="left", padx=(18, 0)
        )
        ttk.Button(
            actions, text="Create Office Demo", command=self._create_office_demo
        ).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Open Workspace", command=self._open_workspace).pack(
            side="left", padx=(18, 0)
        )

        license_frame = ttk.LabelFrame(
            tab, text="License (Phase 6)", style="Section.TLabelframe", padding=6
        )
        license_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        license_frame.columnconfigure(0, weight=1)
        self.license_var = tk.StringVar(value="License: loading...")
        ttk.Label(license_frame, textvariable=self.license_var).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(
            license_frame, text="License Details", command=self._show_license_status
        ).grid(row=0, column=1, sticky="e", padx=(8, 0))
        ttk.Button(
            license_frame,
            text="Activate License File...",
            command=self._activate_license,
        ).grid(row=0, column=2, sticky="e", padx=(6, 0))
        ttk.Button(
            license_frame, text="Deactivate", command=self._deactivate_license
        ).grid(row=0, column=3, sticky="e", padx=(6, 0))

        self.doctor_text = self._make_text(tab, height=24, width=96)
        self.doctor_text.grid(row=2, column=0, sticky="nsew")

    def _build_tasks_tab(self) -> None:
        tab = self.tasks_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)

        pane = ttk.PanedWindow(tab, orient="horizontal")
        pane.grid(row=0, column=0, sticky="nsew")
        controls_outer, controls = self._make_scrollable_labelframe(
            pane, text="Run a task"
        )
        controls.columnconfigure(0, weight=1)

        ttk.Label(controls, text="Instruction").grid(row=0, column=0, sticky="w")
        self.task_instruction_text = self._make_text(
            controls, height=3, width=42
        )
        self.task_instruction_text.grid(row=1, column=0, sticky="ew", pady=(2, 4))
        self._set_text(
            self.task_instruction_text, "Merge PDFs from Folder A and Folder B"
        )

        ttk.Label(controls, text="Input folders (one per line)").grid(
            row=2, column=0, sticky="w"
        )
        self.task_folders_text = self._make_text(controls, height=2, width=42)
        self.task_folders_text.grid(row=3, column=0, sticky="ew", pady=(2, 2))
        self._set_text(self.task_folders_text, "Folder A\nFolder B")
        ttk.Button(
            controls,
            text="Add Folder...",
            command=lambda: self._browse_folder_into_text(self.task_folders_text),
        ).grid(row=4, column=0, sticky="w", pady=(0, 6))

        ttk.Label(controls, text="Input files (one per line)").grid(
            row=5, column=0, sticky="w"
        )
        self.task_files_text = self._make_text(controls, height=2, width=42)
        self.task_files_text.grid(row=6, column=0, sticky="ew", pady=(2, 2))
        ttk.Button(
            controls,
            text="Add File...",
            command=lambda: self._browse_file_into_text(self.task_files_text),
        ).grid(row=7, column=0, sticky="w", pady=(0, 6))

        self.task_output_var = tk.StringVar(value="gui-merged.pdf")
        self.task_target_var = tk.StringVar()
        self.task_expected_pages_var = tk.StringVar()
        self.task_recursive_var = tk.BooleanVar(value=False)
        self.task_use_ai_var = tk.BooleanVar(value=False)
        self.task_force_var = tk.BooleanVar(value=False)
        self.task_max_recovery_var = tk.StringVar(value="Default")

        self._labeled_entry(controls, "Output (workspace)", self.task_output_var, 8)
        self._labeled_entry(controls, "Target file/folder", self.task_target_var, 10)
        self._labeled_entry(
            controls, "Expected pages", self.task_expected_pages_var, 12
        )

        options = ttk.Frame(controls)
        options.grid(row=14, column=0, sticky="ew", pady=(4, 2))
        ttk.Checkbutton(options, text="Recursive", variable=self.task_recursive_var).pack(
            side="left"
        )
        ttk.Checkbutton(
            options, text="Use AI if needed", variable=self.task_use_ai_var
        ).pack(side="left", padx=(10, 0))
        ttk.Checkbutton(
            options, text="Force recovery", variable=self.task_force_var
        ).pack(side="left", padx=(10, 0))

        recovery = ttk.Frame(controls)
        recovery.grid(row=15, column=0, sticky="ew", pady=(2, 8))
        ttk.Label(recovery, text="Self-healing retries:").pack(side="left")
        ttk.Combobox(
            recovery,
            textvariable=self.task_max_recovery_var,
            values=("Default", "0", "1", "2", "3"),
            state="readonly",
            width=9,
        ).pack(side="left", padx=(6, 0))

        buttons = ttk.Frame(controls)
        buttons.grid(row=16, column=0, sticky="ew")
        ttk.Button(
            buttons,
            text="Run New Task",
            style="Accent.TButton",
            command=self._run_new_task,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=2)
        ttk.Button(buttons, text="Run Selected", command=self._run_selected_task).grid(
            row=0, column=1, sticky="ew", padx=(4, 0), pady=2
        )
        ttk.Button(buttons, text="Diagnose", command=self._diagnose_selected_task).grid(
            row=1, column=0, sticky="ew", padx=(0, 4), pady=2
        )
        ttk.Button(buttons, text="Recover", command=self._recover_selected_task).grid(
            row=1, column=1, sticky="ew", padx=(4, 0), pady=2
        )
        ttk.Button(buttons, text="Resume", command=self._resume_selected_task).grid(
            row=2, column=0, sticky="ew", padx=(0, 4), pady=2
        )
        ttk.Button(buttons, text="Cancel", command=self._cancel_selected_task).grid(
            row=2, column=1, sticky="ew", padx=(4, 0), pady=2
        )
        ttk.Button(
            buttons, text="Run Verified Workflow...", command=self._run_workflow
        ).grid(row=3, column=0, columnspan=2, sticky="ew", pady=2)
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)

        right = ttk.Frame(pane, padding=(8, 0, 0, 0))
        pane.add(controls_outer, weight=2)
        pane.add(right, weight=3)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        right.rowconfigure(2, weight=1)

        filter_frame = ttk.Frame(right)
        filter_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(filter_frame, text="Status:").pack(side="left")
        self.task_status_var = tk.StringVar(value="all")
        ttk.Combobox(
            filter_frame,
            textvariable=self.task_status_var,
            values=("all", *(status.value for status in TaskStatus)),
            state="readonly",
            width=20,
        ).pack(side="left", padx=(6, 8))
        ttk.Button(filter_frame, text="Refresh Tasks", command=self._refresh_tasks).pack(
            side="left"
        )

        tree_frame = ttk.Frame(right)
        tree_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.task_tree = ttk.Treeview(
            tree_frame,
            columns=("status", "instruction", "updated"),
            show="headings",
            selectmode="browse",
            height=10,
        )
        self.task_tree.heading("status", text="Status")
        self.task_tree.heading("instruction", text="Instruction")
        self.task_tree.heading("updated", text="Updated (UTC)")
        self.task_tree.column("status", width=110, stretch=False)
        self.task_tree.column("instruction", width=280)
        self.task_tree.column("updated", width=170, stretch=False)
        self.task_tree.grid(row=0, column=0, sticky="nsew")
        task_scroll = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.task_tree.yview
        )
        task_scroll.grid(row=0, column=1, sticky="ns")
        self.task_tree.configure(yscrollcommand=task_scroll.set)
        self.task_tree.bind("<<TreeviewSelect>>", self._on_task_selected)

        self.task_details_text = self._make_text(right, height=12, width=68)
        self.task_details_text.grid(row=2, column=0, sticky="nsew")

    def _build_documents_tab(self) -> None:
        tab = self.documents_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)

        pane = ttk.PanedWindow(tab, orient="horizontal")
        pane.grid(row=0, column=0, sticky="nsew")
        controls = ttk.LabelFrame(
            pane,
            text="Workspace document tools",
            style="Section.TLabelframe",
            padding=8,
        )
        controls.columnconfigure(0, weight=1)

        self.doc_input_var = tk.StringVar(value="Office Demo/sales.csv")
        self.doc_output_var = tk.StringVar(value="Office Demo/gui-output.json")
        self.doc_sheet_var = tk.StringVar(value="Sales")
        self.doc_sample_rows_var = tk.StringVar(value="5")
        self.doc_max_chars_var = tk.StringVar(value="20000")
        self.doc_title_var = tk.StringVar(value="GUI Document")
        self.pdf_expected_pages_var = tk.StringVar()
        self.pdf_recursive_var = tk.BooleanVar(value=False)
        self.pdf_pages_var = tk.StringVar(value="1-2")
        self.doc_find_var = tk.StringVar()
        self.doc_replace_var = tk.StringVar()
        self.doc_cell_ref_var = tk.StringVar(value="B2")
        self.doc_cell_value_var = tk.StringVar(value="=SUM(A1:A3)")
        self.files_new_name_var = tk.StringVar(value="renamed.txt")
        self.files_folder_var = tk.StringVar(value=".")
        self.files_by_var = tk.StringVar(value="category")
        self.files_query_var = tk.StringVar()
        self.files_content_var = tk.BooleanVar(value=False)

        self._labeled_entry(controls, "Input file (workspace)", self.doc_input_var, 0)
        browse = ttk.Frame(controls)
        browse.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(
            browse,
            text="Browse Input...",
            command=lambda: self._browse_workspace_file(self.doc_input_var),
        ).pack(side="left")
        ttk.Button(
            browse,
            text="Browse Output...",
            command=lambda: self._browse_workspace_output(self.doc_output_var),
        ).pack(side="left", padx=(6, 0))
        self._labeled_entry(controls, "Output file (new)", self.doc_output_var, 3)

        doc_notebook = ttk.Notebook(controls)
        doc_notebook.grid(row=5, column=0, sticky="nsew", pady=(4, 0))
        controls.rowconfigure(5, weight=1)
        office_panel = ttk.Frame(doc_notebook, padding=6)
        create_panel = ttk.Frame(doc_notebook, padding=6)
        pdf_panel = ttk.Frame(doc_notebook, padding=6)
        files_panel = ttk.Frame(doc_notebook, padding=6)
        for panel in (office_panel, create_panel, pdf_panel, files_panel):
            panel.columnconfigure(0, weight=1)
        doc_notebook.add(office_panel, text="Office")
        doc_notebook.add(create_panel, text="Create DOCX")
        doc_notebook.add(pdf_panel, text="PDF")
        doc_notebook.add(files_panel, text="Files")

        self._labeled_entry(office_panel, "XLSX sheet name", self.doc_sheet_var, 0)

        numbers = ttk.Frame(office_panel)
        numbers.grid(row=2, column=0, sticky="ew", pady=(2, 6))
        ttk.Label(numbers, text="CSV samples").grid(row=0, column=0, sticky="w")
        ttk.Entry(numbers, textvariable=self.doc_sample_rows_var, width=7).grid(
            row=1, column=0, sticky="w"
        )
        ttk.Label(numbers, text="Extract max chars").grid(
            row=0, column=1, sticky="w", padx=(12, 0)
        )
        ttk.Entry(numbers, textvariable=self.doc_max_chars_var, width=10).grid(
            row=1, column=1, sticky="w", padx=(12, 0)
        )

        office_buttons = ttk.Frame(office_panel)
        office_buttons.grid(row=3, column=0, sticky="ew")
        office_buttons.columnconfigure(0, weight=1)
        office_buttons.columnconfigure(1, weight=1)
        ttk.Button(office_buttons, text="Office Inspect", command=self._doc_inspect).grid(
            row=0, column=0, sticky="ew", padx=(0, 3), pady=2
        )
        ttk.Button(
            office_buttons, text="CSV Inspect", command=self._doc_csv_inspect
        ).grid(row=0, column=1, sticky="ew", padx=(3, 0), pady=2)
        ttk.Button(
            office_buttons, text="Extract Text", command=self._doc_extract
        ).grid(row=1, column=0, sticky="ew", padx=(0, 3), pady=2)
        ttk.Button(
            office_buttons, text="CSV to JSON", command=self._doc_csv_to_json
        ).grid(row=1, column=1, sticky="ew", padx=(3, 0), pady=2)
        ttk.Button(
            office_buttons, text="JSON to CSV", command=self._doc_json_to_csv
        ).grid(row=2, column=0, sticky="ew", padx=(0, 3), pady=2)
        ttk.Button(
            office_buttons, text="CSV to XLSX", command=self._doc_csv_to_xlsx
        ).grid(row=2, column=1, sticky="ew", padx=(3, 0), pady=2)
        self._labeled_entry(office_panel, "DOCX find text", self.doc_find_var, 4)
        self._labeled_entry(office_panel, "DOCX replace with", self.doc_replace_var, 6)
        ttk.Button(
            office_panel, text="DOCX Replace Text", command=self._doc_docx_replace
        ).grid(row=8, column=0, sticky="ew", pady=2)
        self._labeled_entry(office_panel, "XLSX cell (e.g. B2)", self.doc_cell_ref_var, 9)
        self._labeled_entry(
            office_panel, "XLSX value/formula", self.doc_cell_value_var, 11
        )
        xlsx_buttons = ttk.Frame(office_panel)
        xlsx_buttons.grid(row=13, column=0, sticky="ew")
        xlsx_buttons.columnconfigure(0, weight=1)
        xlsx_buttons.columnconfigure(1, weight=1)
        ttk.Button(xlsx_buttons, text="Set XLSX Cell", command=self._doc_xlsx_set_cell).grid(
            row=0, column=0, sticky="ew", padx=(0, 3), pady=2
        )
        ttk.Button(
            xlsx_buttons, text="Verify Formulas", command=self._doc_xlsx_verify_formulas
        ).grid(row=0, column=1, sticky="ew", padx=(3, 0), pady=2)

        self._labeled_entry(create_panel, "New DOCX title", self.doc_title_var, 0)
        ttk.Label(create_panel, text="Paragraphs (one per line)").grid(
            row=2, column=0, sticky="w"
        )
        self.doc_paragraphs_text = self._make_text(
            create_panel, height=6, width=44
        )
        self.doc_paragraphs_text.grid(row=3, column=0, sticky="ew", pady=(2, 4))
        self._set_text(
            self.doc_paragraphs_text,
            "Created from the Self-Healing PC Agent desktop GUI.",
        )
        ttk.Button(
            create_panel, text="Create DOCX", command=self._doc_create_docx
        ).grid(row=4, column=0, sticky="ew", pady=2)
        ttk.Button(
            create_panel,
            text="Append Paragraphs to Input DOCX",
            command=self._doc_docx_append,
        ).grid(row=5, column=0, sticky="ew", pady=2)

        ttk.Label(pdf_panel, text="PDF folders (one per line)").grid(
            row=0, column=0, sticky="w"
        )
        self.pdf_folders_text = self._make_text(pdf_panel, height=3, width=44)
        self.pdf_folders_text.grid(row=1, column=0, sticky="ew", pady=(2, 4))
        self._set_text(self.pdf_folders_text, "Folder A\nFolder B")
        self._labeled_entry(
            pdf_panel, "PDF expected pages", self.pdf_expected_pages_var, 2
        )
        ttk.Checkbutton(
            pdf_panel, text="Recursive PDF merge", variable=self.pdf_recursive_var
        ).grid(row=4, column=0, sticky="w", pady=(2, 4))
        pdf_buttons = ttk.Frame(pdf_panel)
        pdf_buttons.grid(row=5, column=0, sticky="ew")
        pdf_buttons.columnconfigure(0, weight=1)
        pdf_buttons.columnconfigure(1, weight=1)
        ttk.Button(pdf_buttons, text="PDF Inspect", command=self._pdf_inspect).grid(
            row=0, column=0, sticky="ew", padx=(0, 3), pady=2
        )
        ttk.Button(pdf_buttons, text="PDF Verify", command=self._pdf_verify).grid(
            row=0, column=1, sticky="ew", padx=(3, 0), pady=2
        )
        ttk.Button(
            pdf_buttons, text="Merge PDF Folders", command=self._pdf_merge_folders
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=2)
        self._labeled_entry(pdf_panel, "Pages to extract (e.g. 1-3,5)", self.pdf_pages_var, 6)
        pdf_edit_buttons = ttk.Frame(pdf_panel)
        pdf_edit_buttons.grid(row=8, column=0, sticky="ew")
        pdf_edit_buttons.columnconfigure(0, weight=1)
        pdf_edit_buttons.columnconfigure(1, weight=1)
        ttk.Button(
            pdf_edit_buttons, text="Extract Pages", command=self._pdf_extract_pages
        ).grid(row=0, column=0, sticky="ew", padx=(0, 3), pady=2)
        ttk.Button(
            pdf_edit_buttons, text="Split PDF", command=self._pdf_split
        ).grid(row=0, column=1, sticky="ew", padx=(3, 0), pady=2)
        ttk.Label(
            pdf_panel,
            text="Extract writes the Output file; Split treats Output as a folder name.",
            wraplength=380,
        ).grid(row=9, column=0, sticky="w")

        ttk.Label(
            files_panel,
            text="Copy/Move use Input file -> Output file above. Nothing is overwritten.",
            wraplength=380,
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self._labeled_entry(files_panel, "Rename to (plain file name)", self.files_new_name_var, 1)
        self._labeled_entry(files_panel, "Folder (organize/search, . = workspace)", self.files_folder_var, 3)
        ttk.Label(files_panel, text="Organize by").grid(row=5, column=0, sticky="w")
        ttk.Combobox(
            files_panel,
            textvariable=self.files_by_var,
            values=("category", "extension"),
            state="readonly",
        ).grid(row=6, column=0, sticky="ew", pady=(2, 4))
        self._labeled_entry(files_panel, "Search text (name or content)", self.files_query_var, 7)
        ttk.Checkbutton(
            files_panel, text="Search text content too", variable=self.files_content_var
        ).grid(row=9, column=0, sticky="w", pady=(2, 4))
        file_buttons = ttk.Frame(files_panel)
        file_buttons.grid(row=10, column=0, sticky="ew")
        file_buttons.columnconfigure(0, weight=1)
        file_buttons.columnconfigure(1, weight=1)
        ttk.Button(file_buttons, text="Copy File", command=self._files_copy).grid(
            row=0, column=0, sticky="ew", padx=(0, 3), pady=2
        )
        ttk.Button(file_buttons, text="Move File", command=self._files_move).grid(
            row=0, column=1, sticky="ew", padx=(3, 0), pady=2
        )
        ttk.Button(file_buttons, text="Rename File", command=self._files_rename).grid(
            row=1, column=0, sticky="ew", padx=(0, 3), pady=2
        )
        ttk.Button(file_buttons, text="Organize Folder", command=self._files_organize).grid(
            row=1, column=1, sticky="ew", padx=(3, 0), pady=2
        )
        ttk.Button(file_buttons, text="Search Files", command=self._files_search).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=2
        )

        output_frame = ttk.Frame(pane, padding=(8, 0, 0, 0))
        output_frame.columnconfigure(0, weight=1)
        output_frame.rowconfigure(0, weight=1)
        pane.add(controls, weight=2)
        pane.add(output_frame, weight=3)
        self.doc_output_text = self._make_text(
            output_frame, height=30, width=72
        )
        self.doc_output_text.grid(row=0, column=0, sticky="nsew")

    def _build_web_tab(self) -> None:
        tab = self.web_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        form = ttk.LabelFrame(
            tab,
            text="Fetch one exact page as text",
            style="Section.TLabelframe",
            padding=8,
        )
        form.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        form.columnconfigure(1, weight=1)

        self.web_url_var = tk.StringVar(value="https://example.com/")
        self.web_output_var = tk.StringVar(value="gui-example.txt")
        self.web_max_chars_var = tk.StringVar(value="20000")
        self.web_allow_private_var = tk.BooleanVar(value=False)

        ttk.Label(form, text="URL").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.web_url_var).grid(
            row=0, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=2
        )
        ttk.Label(form, text="Save as (optional)").grid(row=1, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.web_output_var).grid(
            row=1, column=1, sticky="ew", padx=(8, 8), pady=2
        )
        ttk.Label(form, text="Max chars").grid(row=1, column=2, sticky="w")
        ttk.Entry(form, textvariable=self.web_max_chars_var, width=10).grid(
            row=1, column=3, sticky="w", padx=(8, 0), pady=2
        )
        ttk.Checkbutton(
            form,
            text="Allow private/local address for this exact fetch",
            variable=self.web_allow_private_var,
        ).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(4, 2))
        ttk.Button(
            form,
            text="Fetch Page",
            style="Accent.TButton",
            command=self._fetch_web,
        ).grid(row=2, column=3, sticky="e", pady=(4, 2))

        ttk.Label(
            tab,
            text=(
                "Public pages are SSRF-guarded and HTTPS verification is never "
                "disabled. Scripts are not executed."
            ),
        ).grid(row=1, column=0, sticky="w", pady=(0, 6))
        self.web_output_text = self._make_text(tab, height=24, width=96)
        self.web_output_text.grid(row=2, column=0, sticky="nsew")

    def _build_desktop_tab(self) -> None:
        tab = self.desktop_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        form = ttk.LabelFrame(
            tab,
            text="Explicit desktop actions",
            style="Section.TLabelframe",
            padding=8,
        )
        form.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        form.columnconfigure(1, weight=1)

        self.desktop_path_var = tk.StringVar(value=".")
        self.desktop_url_var = tk.StringVar(value="https://example.com/")

        ttk.Button(form, text="System Info", command=self._desktop_info).grid(
            row=0, column=0, sticky="w", pady=2
        )
        ttk.Label(form, text="Workspace file/folder").grid(
            row=1, column=0, sticky="w", pady=2
        )
        ttk.Entry(form, textvariable=self.desktop_path_var).grid(
            row=1, column=1, sticky="ew", padx=(8, 8), pady=2
        )
        ttk.Button(
            form,
            text="Browse...",
            command=lambda: self._browse_workspace_file(
                self.desktop_path_var, directories=True
            ),
        ).grid(row=1, column=2, sticky="e", pady=2)
        ttk.Button(form, text="Open Path", command=self._open_desktop_path).grid(
            row=1, column=3, sticky="e", padx=(6, 0), pady=2
        )
        ttk.Label(form, text="HTTP(S) URL").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(form, textvariable=self.desktop_url_var).grid(
            row=2, column=1, sticky="ew", padx=(8, 8), pady=2
        )
        ttk.Button(form, text="Open URL", command=self._open_desktop_url).grid(
            row=2, column=3, sticky="e", pady=2
        )
        ttk.Label(
            form,
            text=(
                "Open actions use the default app/browser only. There is no "
                "shell, screenshot, clicking or typing automation."
            ),
        ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))

        self.desktop_output_text = self._make_text(tab, height=22, width=96)
        self.desktop_output_text.grid(row=1, column=0, sticky="nsew")

    def _build_tools_tab(self) -> None:
        tab = self.tools_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)

        pane = ttk.PanedWindow(tab, orient="horizontal")
        pane.grid(row=0, column=0, sticky="nsew")
        left = ttk.Frame(pane)
        left.rowconfigure(1, weight=1)
        ttk.Button(left, text="Refresh Tools", command=self._refresh_tools).grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        tree_frame = ttk.Frame(left)
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.tool_tree = ttk.Treeview(
            tree_frame,
            columns=("tool", "version", "state", "permissions"),
            show="headings",
            selectmode="browse",
            height=20,
        )
        self.tool_tree.heading("tool", text="Tool")
        self.tool_tree.heading("version", text="Version")
        self.tool_tree.heading("state", text="State")
        self.tool_tree.heading("permissions", text="Permissions")
        self.tool_tree.column("tool", width=220)
        self.tool_tree.column("version", width=65, stretch=False)
        self.tool_tree.column("state", width=80, stretch=False)
        self.tool_tree.column("permissions", width=175)
        self.tool_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.tool_tree.yview
        )
        scroll.grid(row=0, column=1, sticky="ns")
        self.tool_tree.configure(yscrollcommand=scroll.set)
        self.tool_tree.bind("<<TreeviewSelect>>", self._on_tool_selected)

        right = ttk.Frame(pane, padding=(8, 0, 0, 0))
        pane.add(left, weight=2)
        pane.add(right, weight=3)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        self.tool_details_text = self._make_text(right, height=15, width=66)
        self.tool_details_text.grid(row=0, column=0, sticky="nsew", pady=(0, 8))

        invoke_frame = ttk.LabelFrame(
            right,
            text="Invoke approved dynamic tool (custom.* only)",
            style="Section.TLabelframe",
            padding=8,
        )
        invoke_frame.grid(row=1, column=0, sticky="ew")
        invoke_frame.columnconfigure(0, weight=1)
        ttk.Label(invoke_frame, text="JSON arguments").grid(
            row=0, column=0, sticky="w"
        )
        self.tool_arguments_text = self._make_text(
            invoke_frame, height=4, width=66
        )
        self.tool_arguments_text.grid(row=1, column=0, sticky="ew", pady=(2, 6))
        self._set_text(self.tool_arguments_text, '{"text": "one two three"}')
        ttk.Button(
            invoke_frame, text="Invoke Dynamic Tool", command=self._invoke_dynamic_tool
        ).grid(row=2, column=0, sticky="w")
        self.tool_output_text = self._make_text(invoke_frame, height=5, width=66)
        self.tool_output_text.grid(row=3, column=0, sticky="ew", pady=(6, 0))

    def _build_providers_tab(self) -> None:
        tab = self.providers_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        tab.rowconfigure(3, weight=1)

        top = ttk.Frame(tab)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(
            top, text="Refresh Providers", command=self._refresh_providers
        ).pack(side="left")
        ttk.Label(
            top, text="Keys are never displayed or edited in the GUI."
        ).pack(side="left", padx=(10, 0))

        tree_frame = ttk.Frame(tab)
        tree_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.provider_tree = ttk.Treeview(
            tree_frame,
            columns=("provider", "model", "configured", "route", "detail"),
            show="headings",
            selectmode="browse",
            height=8,
        )
        self.provider_tree.heading("provider", text="Provider")
        self.provider_tree.heading("model", text="Model")
        self.provider_tree.heading("configured", text="Configured")
        self.provider_tree.heading("route", text="In route")
        self.provider_tree.heading("detail", text="Detail")
        self.provider_tree.column("provider", width=135)
        self.provider_tree.column("model", width=190)
        self.provider_tree.column("configured", width=100, stretch=False)
        self.provider_tree.column("route", width=85, stretch=False)
        self.provider_tree.column("detail", width=350)
        self.provider_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.provider_tree.yview
        )
        scroll.grid(row=0, column=1, sticky="ns")
        self.provider_tree.configure(yscrollcommand=scroll.set)

        ask_frame = ttk.LabelFrame(
            tab,
            text="Ask configured provider route",
            style="Section.TLabelframe",
            padding=8,
        )
        ask_frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ask_frame.columnconfigure(0, weight=1)
        self.provider_prompt_text = self._make_text(
            ask_frame, height=3, width=90
        )
        self.provider_prompt_text.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self._set_text(
            self.provider_prompt_text, "Reply briefly: provider test successful"
        )
        row = ttk.Frame(ask_frame)
        row.grid(row=1, column=0, sticky="ew")
        self.provider_fallback_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            row, text="Allow provider fallback", variable=self.provider_fallback_var
        ).pack(side="left")
        ttk.Button(row, text="Ask Provider", command=self._ask_provider).pack(
            side="right"
        )

        self.provider_output_text = self._make_text(tab, height=10, width=90)
        self.provider_output_text.grid(row=3, column=0, sticky="nsew")

    # ------------------------------------------------------------------
    # Generic widget and worker helpers
    # ------------------------------------------------------------------
    def _make_text(self, parent: Any, *, height: int, width: int = 80) -> Any:
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        widget = tk.Text(
            frame,
            height=height,
            width=width,
            wrap="word",
            font=("Segoe UI", 10),
            padx=6,
            pady=4,
        )
        widget.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=widget.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        widget.configure(yscrollcommand=scrollbar.set)
        # The surrounding frame participates in the caller's grid.  Remember it
        # on the widget so callers can grid the Text handle naturally.
        widget._pc_agent_frame = frame  # type: ignore[attr-defined]
        return _GridTextProxy(widget, frame)

    def _make_scrollable_labelframe(
        self, parent: Any, *, text: str
    ) -> tuple[Any, Any]:
        outer = ttk.LabelFrame(parent, text=text, style="Section.TLabelframe")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(
            outer, orient="vertical", command=canvas.yview
        )
        inner = ttk.Frame(canvas, padding=8)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        def sync_scrollregion(_event: Any = None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def sync_width(event: Any) -> None:
            canvas.itemconfigure(window, width=event.width)

        inner.bind("<Configure>", sync_scrollregion)
        canvas.bind("<Configure>", sync_width)
        return outer, inner

    def _labeled_entry(
        self, parent: Any, label: str, variable: Any, row: int
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w")
        ttk.Entry(parent, textvariable=variable).grid(
            row=row + 1, column=0, sticky="ew", pady=(1, 4)
        )

    @staticmethod
    def _set_text(widget: Any, value: str) -> None:
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.see("1.0")

    @staticmethod
    def _get_text(widget: Any) -> str:
        return str(widget.get("1.0", "end-1c"))

    def _show_tool_result(self, widget: Any, result: ToolResult) -> None:
        self._set_text(widget, format_tool_result(result))
        self.status_var.set(
            f"{result.tool_id}: {'success' if result.success else 'failed'}"
        )

    def _run_async(
        self,
        label: str,
        operation: Callable[[], Any],
        on_success: Callable[[Any], None] | None = None,
    ) -> bool:
        if self._busy:
            messagebox.showinfo(
                "Already running",
                "Wait for the current operation to finish before starting another one.",
                parent=self.root,
            )
            return False
        self._set_busy(True, label)

        def worker() -> None:
            try:
                value = operation()
            except Exception as exc:  # noqa: BLE001 - GUI boundary reports errors.
                self._events.put(
                    _WorkerEvent(
                        label=label,
                        error=f"{type(exc).__name__}: {exc}",
                        on_success=on_success,
                    )
                )
            else:
                self._events.put(
                    _WorkerEvent(label=label, value=value, on_success=on_success)
                )

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _set_busy(self, busy: bool, label: str) -> None:
        self._busy = busy
        if busy:
            self.status_var.set(f"Running: {label}...")
            self.progress.start(12)
            self.root.configure(cursor="watch")
        else:
            self.status_var.set(label)
            self.progress.stop()
            self.root.configure(cursor="")

    def _poll_events(self) -> None:
        try:
            self._handle_pending_approval()
            while True:
                try:
                    event = self._events.get_nowait()
                except queue.Empty:
                    break
                self._set_busy(
                    False,
                    f"Failed: {event.label}" if event.error else "Ready",
                )
                if event.error:
                    messagebox.showerror(event.label, event.error, parent=self.root)
                elif event.on_success is not None:
                    event.on_success(event.value)
        except tk.TclError:
            return
        self.root.after(100, self._poll_events)

    def _handle_pending_approval(self) -> None:
        if self._pending_approval is None:
            self._pending_approval = self.broker.poll()
        if self._pending_approval is None:
            return
        ticket = self._pending_approval
        approved = messagebox.askyesno(
            "Approval required",
            format_approval_request(ticket.request),
            icon="warning",
            parent=self.root,
        )
        self.broker.resolve(ticket, approved=bool(approved))
        self._pending_approval = None

    def _on_close(self) -> None:
        if self._busy and not messagebox.askokcancel(
            "Operation running",
            "An operation is still running. Close the GUI anyway? "
            "A task may remain in an active state for recovery scanning.",
            icon="warning",
            parent=self.root,
        ):
            return
        self.broker.close()
        self.root.destroy()

    # ------------------------------------------------------------------
    # Refresh actions
    # ------------------------------------------------------------------
    def _refresh_all(self) -> None:
        def load() -> tuple[Any, list[Any], list[Any], list[Any]]:
            return (
                self.controller.doctor_summary(),
                self.controller.list_tasks(limit=50),
                self.controller.list_tools(include_disabled=True),
                self.controller.provider_statuses(),
            )

        def done(data: tuple[Any, list[Any], list[Any], list[Any]]) -> None:
            doctor, tasks, tools, providers = data
            self._show_doctor(doctor)
            self._populate_tasks(tasks)
            self._populate_tools(tools)
            self._populate_providers(providers)

        self._run_async("Load dashboard", load, done)

    def _refresh_doctor(self) -> None:
        self._run_async(
            "Refresh doctor", self.controller.doctor_summary, self._show_doctor
        )

    def _show_doctor(self, summary: Any) -> None:
        self._set_text(self.doctor_text, summary.to_text())
        self.license_var.set(f"License: {summary.license_summary}")
        self.header_status_var.set(
            f"Status: {summary.status}  |  Route: {' -> '.join(summary.provider_route)}"
        )

    def _run_recovery_scan(self) -> None:
        def done(scan: Any) -> None:
            self._set_text(
                self.doctor_text,
                format_recovery_scan(
                    scan, workspace_dir=self.settings.workspace_dir
                ),
            )

        self._run_async("Recovery scan", self.controller.recovery_scan, done)

    def _create_pdf_demo(self) -> None:
        def done(demo: Any) -> None:
            lines = ["Demo PDF data is ready in the workspace:"]
            lines.extend(f"  {path}" for path in demo.created)
            lines.append(f"Expected merged page count: {demo.expected_pages}")
            self._set_text(self.doctor_text, "\n".join(lines))

        self._run_async("Create PDF demo", self.controller.create_pdf_demo, done)

    def _create_office_demo(self) -> None:
        def done(demo: Any) -> None:
            lines = ["Demo Office data is ready in the workspace:"]
            lines.extend(f"  {path}" for path in demo.created)
            self._set_text(self.doctor_text, "\n".join(lines))

        self._run_async(
            "Create Office demo", self.controller.create_office_demo, done
        )

    def _open_workspace(self) -> None:
        if not messagebox.askokcancel(
            "Open workspace",
            f"Open this workspace in the default file manager?\n\n"
            f"{self.settings.workspace_dir}",
            parent=self.root,
        ):
            return
        self._run_async(
            "Open workspace",
            lambda: self.controller.open_workspace_path("."),
            lambda result: self._show_tool_result(self.doctor_text, result),
        )

    def _show_license_status(self) -> None:
        def done(status: Any) -> None:
            self.license_var.set(f"License: {status.summary}")
            self._set_text(
                self.doctor_text,
                format_license_status(
                    status, machine_id=self.controller.machine_id()
                ),
            )

        self._run_async("License status", self.controller.license_status, done)

    def _activate_license(self) -> None:
        selected = filedialog.askopenfilename(
            title="Select a signed license file supplied by your vendor",
            parent=self.root,
            filetypes=[
                ("License files", "*.key *.lic *.license *.txt"),
                ("All files", "*.*"),
            ],
        )
        if not selected:
            return

        def done(status: Any) -> None:
            self.license_var.set(f"License: {status.summary}")
            self._set_text(
                self.doctor_text,
                "License activated.\n\n"
                + format_license_status(
                    status, machine_id=self.controller.machine_id()
                ),
            )
            messagebox.showinfo(
                "License activated",
                f"Plan: {status.plan}\nLicensee: {status.licensee}",
                parent=self.root,
            )

        self._run_async(
            "Activate license",
            lambda: self.controller.activate_license_file(selected),
            done,
        )

    def _deactivate_license(self) -> None:
        if not messagebox.askokcancel(
            "Deactivate license",
            "Remove the locally activated license file?\n\n"
            "You can activate the same license file again later. "
            "Your tasks and workspace files are not deleted.",
            parent=self.root,
        ):
            return

        def done(status: Any) -> None:
            self.license_var.set(f"License: {status.summary}")
            self._set_text(
                self.doctor_text,
                "License deactivated.\n\n"
                + format_license_status(
                    status, machine_id=self.controller.machine_id()
                ),
            )

        self._run_async(
            "Deactivate license", self.controller.deactivate_license, done
        )

    # ------------------------------------------------------------------
    # Task actions
    # ------------------------------------------------------------------
    def _task_options_from_form(self) -> TaskRunOptions:
        max_recovery_raw = self.task_max_recovery_var.get()
        return TaskRunOptions(
            input_folders=parse_line_list(self._get_text(self.task_folders_text)),
            input_files=parse_line_list(self._get_text(self.task_files_text)),
            output_path=self.task_output_var.get(),
            target_path=self.task_target_var.get(),
            expected_pages=parse_optional_int(
                self.task_expected_pages_var.get(), label="Expected pages"
            ),
            recursive=bool(self.task_recursive_var.get()),
            use_ai=bool(self.task_use_ai_var.get()),
            max_recovery_attempts=(
                None if max_recovery_raw == "Default" else int(max_recovery_raw)
            ),
        )

    def _refresh_tasks(self) -> None:
        raw_status = self.task_status_var.get()
        status = None if raw_status == "all" else TaskStatus(raw_status)
        self._run_async(
            "Refresh tasks",
            lambda: self.controller.list_tasks(limit=100, status=status),
            self._populate_tasks,
        )

    def _populate_tasks(self, tasks: list[Any]) -> None:
        selected = self._selected_task_id()
        for item in self.task_tree.get_children():
            self.task_tree.delete(item)
        for task in tasks:
            updated = task.updated_at.isoformat() if task.updated_at else ""
            self.task_tree.insert(
                "",
                "end",
                iid=task.id,
                values=(task.status.value, task.instruction, updated),
            )
        if selected and self.task_tree.exists(selected):
            self.task_tree.selection_set(selected)
            self.task_tree.see(selected)

    def _selected_task_id(self) -> str | None:
        selection = self.task_tree.selection()
        return str(selection[0]) if selection else None

    def _require_selected_task(self) -> str | None:
        task_id = self._selected_task_id()
        if task_id is None:
            messagebox.showinfo(
                "Select a task", "Select a task in the table first.", parent=self.root
            )
        return task_id

    def _on_task_selected(self, _event: Any = None) -> None:
        task_id = self._selected_task_id()
        if task_id is None:
            return
        try:
            details = self.controller.get_task_details(task_id)
        except PcAgentError as exc:
            self._set_text(self.task_details_text, f"Error: {exc}")
            return
        self._set_text(self.task_details_text, format_task_details(details))

    def _run_new_task(self) -> None:
        try:
            instruction = self._get_text(self.task_instruction_text)
            options = self._task_options_from_form()
        except PcAgentError as exc:
            messagebox.showerror("Invalid task options", str(exc), parent=self.root)
            return

        def done(report: Any) -> None:
            self._set_text(self.task_details_text, format_execution_report(report))
            self._populate_tasks(self.controller.list_tasks(limit=100))
            if self.task_tree.exists(report.task.id):
                self.task_tree.selection_set(report.task.id)
                self.task_tree.see(report.task.id)

        self._run_async(
            "Run new task",
            lambda: self.controller.run_new_task(instruction, options),
            done,
        )

    def _run_selected_task(self) -> None:
        task_id = self._require_selected_task()
        if task_id is None:
            return
        try:
            options = self._task_options_from_form()
        except PcAgentError as exc:
            messagebox.showerror("Invalid task options", str(exc), parent=self.root)
            return

        def done(report: Any) -> None:
            self._set_text(self.task_details_text, format_execution_report(report))
            self._populate_tasks(self.controller.list_tasks(limit=100))

        self._run_async(
            "Run selected task",
            lambda: self.controller.run_task(task_id, options),
            done,
        )

    def _diagnose_selected_task(self) -> None:
        task_id = self._require_selected_task()
        if task_id is None:
            return
        self._run_async(
            "Diagnose task",
            lambda: self.controller.diagnose_task(task_id),
            lambda diagnosis: self._set_text(
                self.task_details_text, format_diagnosis(diagnosis)
            ),
        )

    def _resume_selected_task(self) -> None:
        task_id = self._require_selected_task()
        if task_id is None:
            return
        try:
            options = self._task_options_from_form()
        except PcAgentError as exc:
            messagebox.showerror("Invalid task options", str(exc), parent=self.root)
            return
        force = bool(self.task_force_var.get())
        message = (
            f"Resume interrupted task {task_id} in place?\n\n"
            "A checkpointed completed tool step is reused and verified "
            "instead of being run again."
        )
        if force:
            message += (
                "\n\nForce is enabled. Only continue if no other pc-agent "
                "process is running this task."
            )
        if not messagebox.askokcancel("Resume task", message, parent=self.root):
            return

        def done(report: Any) -> None:
            self._set_text(self.task_details_text, format_execution_report(report))
            self._populate_tasks(self.controller.list_tasks(limit=100))

        self._run_async(
            "Resume task",
            lambda: self.controller.resume_task(task_id, options, force=force),
            done,
        )

    def _cancel_selected_task(self) -> None:
        # Deliberately not routed through _run_async: cancellation must also
        # work while the worker is busy running that very task.
        task_id = self._require_selected_task()
        if task_id is None:
            return
        if not messagebox.askokcancel(
            "Cancel task",
            f"Cancel task {task_id}?\n\n"
            "A running task stops safely at the next stage boundary; "
            "completed checkpoints and workspace files are kept.",
            icon="warning",
            parent=self.root,
        ):
            return
        try:
            message = self.controller.request_task_cancellation(task_id)
        except PcAgentError as exc:
            messagebox.showerror("Cancel task", str(exc), parent=self.root)
            return
        self.status_var.set(message)
        if not self._busy:
            self._populate_tasks(self.controller.list_tasks(limit=100))

    def _run_workflow(self) -> None:
        if self._busy:
            return
        try:
            records = self.controller.list_workflows()
        except PcAgentError as exc:
            messagebox.showerror("Run verified workflow", str(exc), parent=self.root)
            return
        if not records:
            messagebox.showinfo(
                "Run verified workflow",
                "No verified workflows are recorded yet.\n"
                "Run a task to completion first; its verified plan is then "
                "recorded automatically.",
                parent=self.root,
            )
            return
        known = "\n".join(
            f"{record.key}  {record.title}" for record in records[:10]
        )
        key = simpledialog.askstring(
            "Run verified workflow",
            "Workflow key (or 'latest'):\n\n" + known,
            initialvalue="latest",
            parent=self.root,
        )
        if not key:
            return
        try:
            options = self._task_options_from_form()
        except PcAgentError as exc:
            messagebox.showerror("Invalid task options", str(exc), parent=self.root)
            return
        if not messagebox.askokcancel(
            "Run verified workflow",
            f"Run verified workflow {key} as a new task?\n\n"
            "The stored plan is revalidated against live policy and "
            "verified again; memory itself grants no permission.",
            parent=self.root,
        ):
            return

        def done(report: Any) -> None:
            self._set_text(self.task_details_text, format_execution_report(report))
            self._populate_tasks(self.controller.list_tasks(limit=100))

        self._run_async(
            "Run verified workflow",
            lambda: self.controller.run_workflow(key, options),
            done,
        )

    def _recover_selected_task(self) -> None:
        task_id = self._require_selected_task()
        if task_id is None:
            return
        try:
            options = self._task_options_from_form()
        except PcAgentError as exc:
            messagebox.showerror("Invalid task options", str(exc), parent=self.root)
            return
        force = bool(self.task_force_var.get())
        message = f"Recover task {task_id}?"
        if force:
            message += (
                "\n\nForce is enabled. Only continue if no other pc-agent "
                "process is running this task."
            )
        if not messagebox.askokcancel(
            "Recover task", message, icon="warning", parent=self.root
        ):
            return

        def done(outcome: Any) -> None:
            self._set_text(
                self.task_details_text, format_recovery_outcome(outcome)
            )
            self._populate_tasks(self.controller.list_tasks(limit=100))

        self._run_async(
            "Recover task",
            lambda: self.controller.recover_task(task_id, options, force=force),
            done,
        )

    # ------------------------------------------------------------------
    # Document actions
    # ------------------------------------------------------------------
    def _doc_tool(self, label: str, operation: Callable[[], ToolResult]) -> None:
        self._run_async(
            label,
            operation,
            lambda result: self._show_tool_result(self.doc_output_text, result),
        )

    def _doc_inspect(self) -> None:
        path = self.doc_input_var.get()
        self._doc_tool("Office inspect", lambda: self.controller.office_inspect(path))

    def _doc_csv_inspect(self) -> None:
        path = self.doc_input_var.get()
        try:
            sample_rows = parse_optional_int(
                self.doc_sample_rows_var.get(), label="CSV sample rows"
            )
        except PcAgentError as exc:
            messagebox.showerror("Invalid number", str(exc), parent=self.root)
            return
        self._doc_tool(
            "CSV inspect",
            lambda: self.controller.office_csv_inspect(
                path, sample_rows=sample_rows or 0
            ),
        )

    def _doc_extract(self) -> None:
        path = self.doc_input_var.get()
        output = self.doc_output_var.get()
        try:
            max_chars = parse_optional_int(
                self.doc_max_chars_var.get(), label="Extract max chars"
            )
        except PcAgentError as exc:
            messagebox.showerror("Invalid number", str(exc), parent=self.root)
            return
        # Most GUI inspections should not accidentally write a file just because
        # an output path is present for conversions.  Extraction therefore
        # displays text in the GUI and writes only when its output ends in
        # .txt/.md, matching the workspace text-output use case.
        output_path = output if output.lower().endswith((".txt", ".md")) else None
        self._doc_tool(
            "Extract text",
            lambda: self.controller.office_extract_text(
                path, output_path=output_path, max_chars=max_chars or 20_000
            ),
        )

    def _doc_csv_to_json(self) -> None:
        input_path = self.doc_input_var.get()
        output_path = self.doc_output_var.get()
        self._doc_tool(
            "CSV to JSON",
            lambda: self.controller.office_csv_to_json(input_path, output_path),
        )

    def _doc_json_to_csv(self) -> None:
        input_path = self.doc_input_var.get()
        output_path = self.doc_output_var.get()
        self._doc_tool(
            "JSON to CSV",
            lambda: self.controller.office_json_to_csv(input_path, output_path),
        )

    def _doc_csv_to_xlsx(self) -> None:
        input_path = self.doc_input_var.get()
        output_path = self.doc_output_var.get()
        sheet_name = self.doc_sheet_var.get()
        self._doc_tool(
            "CSV to XLSX",
            lambda: self.controller.office_csv_to_xlsx(
                input_path, output_path, sheet_name=sheet_name
            ),
        )

    def _doc_create_docx(self) -> None:
        output_path = self.doc_output_var.get()
        title = self.doc_title_var.get()
        paragraphs = parse_line_list(self._get_text(self.doc_paragraphs_text))
        self._doc_tool(
            "Create DOCX",
            lambda: self.controller.office_create_docx(
                output_path, title, paragraphs
            ),
        )

    def _pdf_inspect(self) -> None:
        path = self.doc_input_var.get()
        self._doc_tool("PDF inspect", lambda: self.controller.pdf_inspect(path))

    def _pdf_verify(self) -> None:
        path = self.doc_input_var.get()
        try:
            expected = parse_optional_int(
                self.pdf_expected_pages_var.get(), label="PDF expected pages"
            )
        except PcAgentError as exc:
            messagebox.showerror("Invalid number", str(exc), parent=self.root)
            return
        self._doc_tool(
            "PDF verify",
            lambda: self.controller.pdf_verify(path, expected_pages=expected),
        )

    def _pdf_merge_folders(self) -> None:
        folders = parse_line_list(self._get_text(self.pdf_folders_text))
        output_path = self.doc_output_var.get()
        recursive = bool(self.pdf_recursive_var.get())
        self._doc_tool(
            "Merge PDF folders",
            lambda: self.controller.pdf_merge_folders(
                folders, output_path, recursive=recursive
            ),
        )

    def _pdf_extract_pages(self) -> None:
        path = self.doc_input_var.get()
        pages = self.pdf_pages_var.get()
        output_path = self.doc_output_var.get()
        self._doc_tool(
            "Extract PDF pages",
            lambda: self.controller.pdf_extract_pages(path, pages, output_path),
        )

    def _pdf_split(self) -> None:
        path = self.doc_input_var.get()
        output_folder = self.doc_output_var.get()
        self._doc_tool(
            "Split PDF", lambda: self.controller.pdf_split(path, output_folder)
        )

    def _doc_docx_replace(self) -> None:
        input_path = self.doc_input_var.get()
        output_path = self.doc_output_var.get()
        find = self.doc_find_var.get()
        replace = self.doc_replace_var.get()
        self._doc_tool(
            "DOCX replace text",
            lambda: self.controller.office_docx_replace(
                input_path, output_path, find, replace
            ),
        )

    def _doc_docx_append(self) -> None:
        input_path = self.doc_input_var.get()
        output_path = self.doc_output_var.get()
        paragraphs = parse_line_list(self._get_text(self.doc_paragraphs_text))
        self._doc_tool(
            "DOCX append paragraphs",
            lambda: self.controller.office_docx_append(
                input_path, output_path, paragraphs
            ),
        )

    def _doc_xlsx_set_cell(self) -> None:
        input_path = self.doc_input_var.get()
        output_path = self.doc_output_var.get()
        reference = self.doc_cell_ref_var.get()
        value = self.doc_cell_value_var.get()
        self._doc_tool(
            "Set XLSX cell",
            lambda: self.controller.office_xlsx_set_cell(
                input_path, output_path, reference, value
            ),
        )

    def _doc_xlsx_verify_formulas(self) -> None:
        path = self.doc_input_var.get()
        self._doc_tool(
            "Verify XLSX formulas",
            lambda: self.controller.office_xlsx_verify_formulas(path),
        )

    def _files_copy(self) -> None:
        source = self.doc_input_var.get()
        destination = self.doc_output_var.get()
        self._doc_tool(
            "Copy file", lambda: self.controller.files_copy(source, destination)
        )

    def _files_move(self) -> None:
        source = self.doc_input_var.get()
        destination = self.doc_output_var.get()
        if not messagebox.askokcancel(
            "Move file",
            f"Move workspace file?\n{source} -> {destination}\n\n"
            "The original path will no longer exist. Nothing is overwritten.",
            icon="warning",
            parent=self.root,
        ):
            return
        self._doc_tool(
            "Move file", lambda: self.controller.files_move(source, destination)
        )

    def _files_rename(self) -> None:
        source = self.doc_input_var.get()
        new_name = self.files_new_name_var.get()
        if not messagebox.askokcancel(
            "Rename file",
            f"Rename workspace file?\n{source} -> {new_name}",
            parent=self.root,
        ):
            return
        self._doc_tool(
            "Rename file", lambda: self.controller.files_rename(source, new_name)
        )

    def _files_organize(self) -> None:
        folder = self.files_folder_var.get()
        by = self.files_by_var.get()
        if not messagebox.askokcancel(
            "Organize folder",
            f"Organize top-level files in '{folder}' by {by}?\n\n"
            "Files move into subfolders; conflicts are skipped and reported. "
            "Nothing is overwritten or deleted.",
            icon="warning",
            parent=self.root,
        ):
            return
        self._doc_tool(
            "Organize folder",
            lambda: self.controller.files_organize(folder, by=by),
        )

    def _files_search(self) -> None:
        query = self.files_query_var.get()
        folder = self.files_folder_var.get()
        content = bool(self.files_content_var.get())
        self._doc_tool(
            "Search files",
            lambda: self.controller.files_search(
                query, folder=folder, search_content=content
            ),
        )

    # ------------------------------------------------------------------
    # Web and desktop actions
    # ------------------------------------------------------------------
    def _fetch_web(self) -> None:
        url = self.web_url_var.get()
        output = self.web_output_var.get()
        allow_private = bool(self.web_allow_private_var.get())
        try:
            max_chars = parse_optional_int(
                self.web_max_chars_var.get(), label="Web max chars"
            )
        except PcAgentError as exc:
            messagebox.showerror("Invalid number", str(exc), parent=self.root)
            return
        if allow_private and not messagebox.askokcancel(
            "Allow private/local address",
            "This one fetch may contact a private or loopback address and a "
            "non-standard port. Continue only for the exact server you intended.",
            icon="warning",
            parent=self.root,
        ):
            return

        def done(outcome: WebFetchOutcome) -> None:
            text = format_tool_result(outcome.result)
            if outcome.saved_path:
                text += f"\n\nSaved to workspace: {outcome.saved_path}"
            self._set_text(self.web_output_text, text)

        self._run_async(
            "Fetch web page",
            lambda: self.controller.fetch_web(
                url,
                max_chars=max_chars or 20_000,
                output_path=output,
                allow_private_network=allow_private,
            ),
            done,
        )

    def _desktop_info(self) -> None:
        self._run_async(
            "Desktop info",
            self.controller.desktop_info,
            lambda result: self._show_tool_result(self.desktop_output_text, result),
        )

    def _open_desktop_path(self) -> None:
        path = self.desktop_path_var.get()
        if not messagebox.askokcancel(
            "Open workspace path",
            f"Open this exact workspace target in its default app?\n\n{path}",
            parent=self.root,
        ):
            return
        self._run_async(
            "Open workspace path",
            lambda: self.controller.open_workspace_path(path),
            lambda result: self._show_tool_result(self.desktop_output_text, result),
        )

    def _open_desktop_url(self) -> None:
        url = self.desktop_url_var.get()
        if not messagebox.askokcancel(
            "Open URL",
            f"Open this exact URL in the default browser?\n\n{url}",
            parent=self.root,
        ):
            return
        self._run_async(
            "Open URL",
            lambda: self.controller.open_url(url),
            lambda result: self._show_tool_result(self.desktop_output_text, result),
        )

    # ------------------------------------------------------------------
    # Tool and provider actions
    # ------------------------------------------------------------------
    def _refresh_tools(self) -> None:
        self._run_async(
            "Refresh tools",
            lambda: self.controller.list_tools(include_disabled=True),
            self._populate_tools,
        )

    def _populate_tools(self, tools: list[Any]) -> None:
        self._tools_by_id = {tool.spec.id: tool for tool in tools}
        for item in self.tool_tree.get_children():
            self.tool_tree.delete(item)
        for tool in tools:
            state = "enabled" if tool.enabled else "disabled"
            permissions = (
                ",".join(action.value for action in tool.spec.permissions) or "none"
            )
            self.tool_tree.insert(
                "",
                "end",
                iid=tool.spec.id,
                values=(tool.spec.id, tool.spec.version, state, permissions),
            )

    def _selected_tool_id(self) -> str | None:
        selection = self.tool_tree.selection()
        return str(selection[0]) if selection else None

    def _on_tool_selected(self, _event: Any = None) -> None:
        tool_id = self._selected_tool_id()
        if tool_id is None:
            return
        tool = self._tools_by_id.get(tool_id)
        if tool is not None:
            self._set_text(self.tool_details_text, format_tool(tool))

    def _invoke_dynamic_tool(self) -> None:
        tool_id = self._selected_tool_id()
        if tool_id is None:
            messagebox.showinfo(
                "Select a tool", "Select an approved dynamic tool first.", parent=self.root
            )
            return
        try:
            arguments = parse_json_object(self._get_text(self.tool_arguments_text))
        except PcAgentError as exc:
            messagebox.showerror("Invalid JSON", str(exc), parent=self.root)
            return
        self._run_async(
            "Invoke dynamic tool",
            lambda: self.controller.invoke_dynamic_tool(tool_id, arguments),
            lambda result: self._show_tool_result(self.tool_output_text, result),
        )

    def _refresh_providers(self) -> None:
        self._run_async(
            "Refresh providers",
            self.controller.provider_statuses,
            self._populate_providers,
        )

    def _populate_providers(self, statuses: list[Any]) -> None:
        for item in self.provider_tree.get_children():
            self.provider_tree.delete(item)
        for index, status in enumerate(statuses):
            self.provider_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    status.name,
                    status.model,
                    str(status.configured),
                    str(status.in_route),
                    status.detail,
                ),
            )
        self._set_text(
            self.provider_output_text, format_provider_statuses(statuses)
        )

    def _ask_provider(self) -> None:
        prompt = self._get_text(self.provider_prompt_text)
        allow_fallback = bool(self.provider_fallback_var.get())

        def done(routed: Any) -> None:
            lines = []
            for attempt in routed.attempts:
                suffix = f" - {attempt.error}" if attempt.error else ""
                lines.append(
                    f"Provider: {attempt.provider}/{attempt.model} "
                    f"[{'success' if attempt.success else 'failed'}]{suffix}"
                )
            lines.extend(["", routed.response.content])
            self._set_text(self.provider_output_text, "\n".join(lines))

        self._run_async(
            "Ask provider",
            lambda: self.controller.ask_provider(
                prompt, allow_fallback=allow_fallback
            ),
            done,
        )

    # ------------------------------------------------------------------
    # File dialog helpers
    # ------------------------------------------------------------------
    def _relative_to_workspace(self, candidate: Path) -> str | None:
        workspace = self.settings.workspace_dir.resolve()
        try:
            return candidate.expanduser().resolve().relative_to(workspace).as_posix()
        except ValueError:
            return None

    def _browse_workspace_file(
        self, variable: Any, *, directories: bool = False
    ) -> None:
        workspace = str(self.settings.workspace_dir)
        selected = (
            filedialog.askdirectory(parent=self.root, initialdir=workspace)
            if directories
            else filedialog.askopenfilename(parent=self.root, initialdir=workspace)
        )
        if not selected:
            return
        relative = self._relative_to_workspace(Path(selected))
        if relative is None:
            messagebox.showwarning(
                "Outside workspace",
                "Document and desktop-open targets must stay inside the workspace.",
                parent=self.root,
            )
            return
        variable.set(relative)

    def _browse_workspace_output(self, variable: Any) -> None:
        selected = filedialog.asksaveasfilename(
            parent=self.root, initialdir=str(self.settings.workspace_dir)
        )
        if not selected:
            return
        relative = self._relative_to_workspace(Path(selected))
        if relative is None:
            messagebox.showwarning(
                "Outside workspace",
                "Output files must stay inside the workspace.",
                parent=self.root,
            )
            return
        variable.set(relative)

    def _browse_folder_into_text(self, widget: Any) -> None:
        selected = filedialog.askdirectory(parent=self.root)
        if selected:
            self._append_line(widget, str(Path(selected)))

    def _browse_file_into_text(self, widget: Any) -> None:
        selected = filedialog.askopenfilename(parent=self.root)
        if selected:
            self._append_line(widget, str(Path(selected)))

    def _append_line(self, widget: Any, value: str) -> None:
        current = self._get_text(widget).strip()
        lines = current.splitlines() if current else []
        if value not in lines:
            lines.append(value)
        self._set_text(widget, "\n".join(lines))


class _GridTextProxy:
    """Let a framed Text+scrollbar pair be gridded through the Text handle."""

    def __init__(self, widget: Any, frame: Any) -> None:
        self._widget = widget
        self._frame = frame

    def grid(self, **kwargs: Any) -> None:
        self._frame.grid(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._widget, name)


if __name__ == "__main__":
    raise SystemExit(main())
