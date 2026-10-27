"""Headless controller used by the desktop GUI (Phase 5-7).

The GUI does not shell out to the CLI and does not invent a second execution
path.  This controller calls the same orchestrator, recovery service, provider
router, registered tools and license gates as the command-line interface, so
policy checks, approvals, verification and audit records stay in one place.
Phase 7 adds checkpoint resume and cooperative cancellation for GUI tasks.
"""

from __future__ import annotations

import json
import platform
import queue
import ssl
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..config import Settings
from ..documents.office import DemoOfficeData, create_demo_office_files
from ..documents.pdf import DemoPdfData, create_demo_pdfs
from ..domain.task import Task, TaskStatus
from ..errors import ApprovalUnavailableError, PcAgentError, ToolError
from ..http_tls import tls_trust_info
from ..licensing import (
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
from ..orchestrator import (
    ApprovalCallback,
    ApprovalRequest,
    ExecutionReport,
    TaskOrchestrator,
)
from ..planning import DeterministicPlanner, PlannerOptions
from ..providers import (
    CompletionRequest,
    ProviderStatus,
    build_model_router,
    provider_statuses,
)
from ..providers.router import RoutedCompletion
from ..recovery import RecoveryOutcome, RecoveryScan, RecoveryService, TaskDiagnosis
from ..storage.db import Database
from ..storage.repositories import (
    ApprovalRecord,
    DynamicToolRecord,
    DynamicToolRepository,
    ExecutionRepository,
    MemoryRepository,
    PreferenceRepository,
    RecoveryAttemptRecord,
    RecoveryLinkRecord,
    RecoveryRepository,
    TaskCheckpointRecord,
    TaskEvent,
    TaskRepository,
    TaskVerificationRecord,
    ToolInvocationRecord,
)
from ..tooling.service import build_runtime_registry
from ..tools import RegisteredTool, ToolContext, ToolResult, build_default_registry


@dataclass(frozen=True, slots=True)
class DoctorSummary:
    version: str
    python_version: str
    environment: str
    root: Path
    workspace: Path
    database_path: Path
    schema_version: int
    provider_order: str
    providers_with_credentials: str
    provider_route: tuple[str, ...]
    approved_dynamic_tools: int
    enabled_dynamic_tools: int
    bundled_dynamic_example_present: bool
    recorded_recovery_attempts: int
    stale_tasks: int
    orphan_temp_pdfs: int
    builtin_tools: int
    office_tools: int
    web_timeout_seconds: float
    web_max_bytes: int
    tls_verified: bool
    tls_ca_certificates: int
    tls_sources: tuple[str, ...]
    tls_error: str | None
    status: str
    license_summary: str = "state=unconfigured, enforcement=off"
    memory_preferences: int = 0
    memory_notes: int = 0
    memory_workflows: int = 0
    task_checkpoints: int = 0
    verification_reports: int = 0
    file_tools: int = 0

    def to_text(self) -> str:
        if self.tls_verified:
            tls_line = (
                "TLS trust: verified; "
                f"CA certificates={self.tls_ca_certificates}; "
                f"sources={', '.join(self.tls_sources)}"
            )
        else:
            tls_line = (
                "TLS trust: unavailable "
                f"({self.tls_error or 'no CA certificates loaded'}); "
                "HTTPS will fail closed"
            )
        example = (
            "present"
            if self.bundled_dynamic_example_present
            else "missing (embedded fallback available; rerun the latest patcher)"
        )
        return "\n".join(
            [
                f"Self-Healing PC Agent {self.version} (PHASE 8 File Operations)",
                f"Python: {self.python_version}",
                f"Environment: {self.environment}",
                f"Root: {self.root}",
                f"Workspace: {self.workspace}",
                f"Database: {self.database_path} (schema v{self.schema_version})",
                f"Provider order: {self.provider_order}",
                f"Providers with credentials: {self.providers_with_credentials}",
                f"Usable provider route: {' -> '.join(self.provider_route)}",
                (
                    "Approved dynamic tools: "
                    f"{self.approved_dynamic_tools} "
                    f"({self.enabled_dynamic_tools} enabled)"
                ),
                f"Bundled dynamic-tool example arguments: {example}",
                (
                    "Recovery: "
                    f"recorded attempts={self.recorded_recovery_attempts}, "
                    f"stale tasks={self.stale_tasks}, "
                    f"orphan temp PDFs={self.orphan_temp_pdfs}"
                ),
                (
                    "Built-in tools: "
                    f"built-ins={self.builtin_tools}, office={self.office_tools}, "
                    f"files={self.file_tools}, "
                    "web.fetch=public-only by default, desktop=open/info only"
                ),
                (
                    "Web limits: "
                    f"timeout={self.web_timeout_seconds:.1f}s, "
                    f"max_bytes={self.web_max_bytes}"
                ),
                tls_line,
                f"License: {self.license_summary}",
                (
                    "Memory: "
                    f"preferences={self.memory_preferences}, "
                    f"notes={self.memory_notes}, "
                    f"workflows={self.memory_workflows}, "
                    f"checkpoints={self.task_checkpoints}, "
                    f"verifications={self.verification_reports}"
                ),
                "Note: deterministic supported tasks run without an AI provider call.",
                f"Status: {self.status}",
            ]
        )


@dataclass(frozen=True, slots=True)
class TaskRunOptions:
    input_folders: tuple[str, ...] = ()
    input_files: tuple[str, ...] = ()
    output_path: str | None = None
    target_path: str | None = None
    expected_pages: int | None = None
    recursive: bool = False
    use_ai: bool = False
    max_recovery_attempts: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_folders",
            tuple(value.strip() for value in self.input_folders if value.strip()),
        )
        object.__setattr__(
            self,
            "input_files",
            tuple(value.strip() for value in self.input_files if value.strip()),
        )
        object.__setattr__(
            self,
            "output_path",
            self.output_path.strip() if self.output_path and self.output_path.strip() else None,
        )
        object.__setattr__(
            self,
            "target_path",
            self.target_path.strip() if self.target_path and self.target_path.strip() else None,
        )
        if self.expected_pages is not None and self.expected_pages < 1:
            raise PcAgentError("Expected pages must be at least 1.")
        if self.max_recovery_attempts is not None and not (
            0 <= self.max_recovery_attempts <= 3
        ):
            raise PcAgentError("Max recovery attempts must be between 0 and 3.")

    def planner_options(self) -> PlannerOptions:
        return PlannerOptions(
            input_folders=self.input_folders,
            input_files=self.input_files,
            output_path=self.output_path,
            target_path=self.target_path,
            recursive=self.recursive,
            expected_pages=self.expected_pages,
        )


@dataclass(frozen=True, slots=True)
class TaskDetails:
    task: Task
    plan: Mapping[str, Any] | None
    events: tuple[TaskEvent, ...]
    invocations: tuple[ToolInvocationRecord, ...]
    approvals: tuple[ApprovalRecord, ...]
    recovery_attempts: tuple[RecoveryAttemptRecord, ...]
    recovery_links: tuple[RecoveryLinkRecord, ...]
    checkpoints: tuple[TaskCheckpointRecord, ...] = ()
    latest_verification: TaskVerificationRecord | None = None
    cancellation_requested: bool = False


@dataclass(frozen=True, slots=True)
class WebFetchOutcome:
    result: ToolResult
    saved_path: str | None = None


@dataclass(eq=False, slots=True)
class ApprovalTicket:
    request: ApprovalRequest
    event: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


class ApprovalBroker:
    """Bridge an orchestrator worker thread to decisions made in the GUI."""

    def __init__(self, *, timeout_seconds: float = 600.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Approval timeout must be positive.")
        self._timeout_seconds = timeout_seconds
        self._queue: queue.Queue[ApprovalTicket] = queue.Queue()
        self._pending: set[ApprovalTicket] = set()
        self._lock = threading.Lock()
        self._closed = False

    def decide(self, request: ApprovalRequest) -> bool:
        ticket = ApprovalTicket(request=request)
        with self._lock:
            if self._closed:
                raise ApprovalUnavailableError(
                    "The desktop GUI is closing, so approval is unavailable. "
                    "The task remains waiting_for_approval."
                )
            self._pending.add(ticket)
        self._queue.put(ticket)
        completed = ticket.event.wait(self._timeout_seconds)
        with self._lock:
            self._pending.discard(ticket)
        if not completed:
            raise ApprovalUnavailableError(
                "No GUI approval decision was made in time. "
                "The task remains waiting_for_approval."
            )
        return ticket.approved

    def poll(self) -> ApprovalTicket | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def resolve(self, ticket: ApprovalTicket, *, approved: bool) -> None:
        ticket.approved = approved
        ticket.event.set()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pending = tuple(self._pending)
        for ticket in pending:
            self.resolve(ticket, approved=False)


class GuiController:
    """All GUI operations, independent from Tkinter and worker threading."""

    def __init__(
        self,
        settings: Settings,
        *,
        database: Database | None = None,
        approval_callback: ApprovalCallback | None = None,
        desktop_opener: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        settings.ensure_directories()
        self.database = database or Database(settings.database_path)
        self.database.initialize()

        self._tasks = TaskRepository(self.database)
        self._execution = ExecutionRepository(self.database)
        self._recovery_repo = RecoveryRepository(self.database)
        self._dynamic_tools = DynamicToolRepository(self.database)
        self._memory = MemoryRepository(self.database)
        self._preferences = PreferenceRepository(self.database)
        self._cancel_event = threading.Event()
        self._active_task_id: str | None = None
        self._router = build_model_router(settings)
        self._licenses = LicenseService(settings, self.database)
        self._approval_callback = approval_callback or _approval_unavailable
        self._desktop_opener = desktop_opener

        self._builtin_registry = build_default_registry()
        self._orchestrator = TaskOrchestrator(
            settings=settings,
            task_repository=self._tasks,
            execution_repository=self._execution,
            tool_registry=self._builtin_registry,
            deterministic_planner=DeterministicPlanner(settings.workspace_dir),
            model_router=self._router,
            recovery_repository=self._recovery_repo,
            memory_repository=self._memory,
        )
        self._recovery = RecoveryService(
            settings=settings,
            task_repository=self._tasks,
            execution_repository=self._execution,
            recovery_repository=self._recovery_repo,
            orchestrator=self._orchestrator,
        )

    # ------------------------------------------------------------------
    # Status and discovery
    # ------------------------------------------------------------------
    def doctor_summary(self) -> DoctorSummary:
        public = self.settings.public_summary()
        approved_dynamic = self._dynamic_tools.approved_candidates()
        enabled_dynamic = sum(1 for record in approved_dynamic if record.enabled)
        scan = self._recovery.scan()
        builtin_tools = self._builtin_registry.list_tools()
        bundled_example = (
            self.settings.root_dir
            / "examples"
            / "dynamic-tools"
            / "word_count_arguments.json"
        )

        tls_verified = False
        tls_ca_certificates = 0
        tls_sources: tuple[str, ...] = ()
        tls_error: str | None = None
        try:
            tls_info = tls_trust_info()
            sources = ["system default"]
            if tls_info.certifi_loaded:
                sources.append("Certifi Mozilla bundle")
            if tls_info.windows_certificates_loaded:
                sources.append(
                    f"Windows store (+{tls_info.windows_certificates_loaded})"
                )
            tls_ca_certificates = tls_info.ca_certificates
            tls_sources = tuple(sources)
            tls_verified = tls_ca_certificates > 0
            if not tls_verified:
                tls_error = "no CA certificates loaded"
        except (OSError, ssl.SSLError, ValueError) as exc:
            tls_error = exc.__class__.__name__

        if scan.stale_tasks or scan.orphan_temp_pdfs:
            status = "ATTENTION - run a recovery scan"
        elif not tls_verified:
            status = "ATTENTION - no TLS CA certificates could be loaded"
        else:
            status = "OK"

        try:
            license_summary = self._licenses.status().summary
        except PcAgentError as exc:
            license_summary = f"unavailable ({exc})"
        memory_counts = self._memory.counts()

        return DoctorSummary(
            version=__version__,
            python_version=platform.python_version(),
            environment=public["environment"],
            root=self.settings.root_dir,
            workspace=self.settings.workspace_dir,
            database_path=self.settings.database_path,
            schema_version=self.database.schema_version(),
            provider_order=public["provider_order"],
            providers_with_credentials=public["providers_with_credentials"],
            provider_route=self._router.provider_names,
            approved_dynamic_tools=len(approved_dynamic),
            enabled_dynamic_tools=enabled_dynamic,
            bundled_dynamic_example_present=bundled_example.is_file(),
            recorded_recovery_attempts=self._recovery_repo.count_attempts(),
            stale_tasks=len(scan.stale_tasks),
            orphan_temp_pdfs=len(scan.orphan_temp_pdfs),
            builtin_tools=len(builtin_tools),
            office_tools=sum(
                1 for tool in builtin_tools if tool.spec.id.startswith("office.")
            ),
            file_tools=sum(
                1 for tool in builtin_tools if tool.spec.id.startswith("files.")
            ),
            web_timeout_seconds=self.settings.web_timeout_seconds,
            web_max_bytes=self.settings.web_max_bytes,
            tls_verified=tls_verified,
            tls_ca_certificates=tls_ca_certificates,
            tls_sources=tls_sources,
            tls_error=tls_error,
            status=status,
            license_summary=license_summary,
            memory_preferences=len(self._preferences.all()),
            memory_notes=memory_counts["note"],
            memory_workflows=memory_counts["workflow"],
            task_checkpoints=self._execution.count_checkpoints(),
            verification_reports=self._execution.count_verifications(),
        )

    def recovery_scan(self) -> RecoveryScan:
        return self._recovery.scan()

    # ------------------------------------------------------------------
    # Phase 6 licensing
    # ------------------------------------------------------------------
    def license_status(self) -> LicenseStatus:
        return self._licenses.status()

    def activate_license_file(self, path: str) -> LicenseStatus:
        return self._licenses.activate_file(Path(path).expanduser())

    def deactivate_license(self) -> LicenseStatus:
        return self._licenses.deactivate()

    def machine_id(self) -> str:
        return self._licenses.machine_id()

    def _require_license(self, feature: str) -> None:
        if self.settings.license_enforcement == "off":
            return
        self._licenses.require(feature)

    def create_pdf_demo(self) -> DemoPdfData:
        return create_demo_pdfs(self.settings.workspace_dir)

    def create_office_demo(self) -> DemoOfficeData:
        return create_demo_office_files(self.settings.workspace_dir)

    def provider_statuses(self) -> list[ProviderStatus]:
        return provider_statuses(self.settings)

    def ask_provider(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        allow_fallback: bool = True,
    ) -> RoutedCompletion:
        cleaned = prompt.strip()
        if not cleaned:
            raise PcAgentError("Enter a prompt before asking a provider.")
        self._require_license(FEATURE_PROVIDERS)
        request = CompletionRequest.from_prompt(
            cleaned,
            system_instruction=system_instruction.strip() if system_instruction else None,
        )
        return self._router.complete(request, allow_fallback=allow_fallback)

    # ------------------------------------------------------------------
    # Tasks and recovery
    # ------------------------------------------------------------------
    def list_tasks(
        self, *, limit: int = 50, status: TaskStatus | None = None
    ) -> list[Task]:
        return self._tasks.list(limit=limit, status=status)

    def get_task_details(self, task_id: str) -> TaskDetails:
        task = self._tasks.get(task_id)
        if task is None:
            raise PcAgentError(f"Task not found: {task_id}")
        return TaskDetails(
            task=task,
            plan=self._execution.get_plan(task.id),
            events=tuple(self._tasks.list_events(task.id)),
            invocations=tuple(self._execution.list_tool_invocations(task.id)),
            approvals=tuple(self._execution.list_approvals(task.id)),
            recovery_attempts=tuple(
                self._recovery_repo.list_attempts(task_id=task.id, limit=100)
            ),
            recovery_links=tuple(self._recovery_repo.list_links_for(task.id)),
            checkpoints=tuple(self._execution.list_checkpoints(task.id)),
            latest_verification=self._execution.get_latest_verification(task.id),
            cancellation_requested=self._execution.cancellation_requested(task.id),
        )

    def run_new_task(
        self, instruction: str, options: TaskRunOptions | None = None
    ) -> ExecutionReport:
        cleaned = instruction.strip()
        if not cleaned:
            raise PcAgentError("Enter a task instruction.")
        run_options = options or TaskRunOptions()
        self._require_license(FEATURE_CORE_TASKS)
        if run_options.use_ai:
            self._require_license(FEATURE_PROVIDERS)
        task = self._tasks.create(Task(instruction=cleaned))
        return self.run_task(task.id, run_options)

    def run_task(
        self, task_id: str, options: TaskRunOptions | None = None
    ) -> ExecutionReport:
        run_options = options or TaskRunOptions()
        self._require_license(FEATURE_CORE_TASKS)
        if run_options.use_ai:
            self._require_license(FEATURE_PROVIDERS)
        self._active_task_id = task_id
        self._cancel_event.clear()
        try:
            return self._orchestrator.execute(
                task_id,
                options=run_options.planner_options(),
                approval_callback=self._approval_callback,
                use_ai=run_options.use_ai,
                max_recovery_attempts=run_options.max_recovery_attempts,
                cancellation_requested=self._cancel_event.is_set,
            )
        finally:
            self._active_task_id = None

    def resume_task(
        self,
        task_id: str,
        options: TaskRunOptions | None = None,
        *,
        force: bool = False,
    ) -> ExecutionReport:
        run_options = options or TaskRunOptions()
        self._require_license(FEATURE_CORE_TASKS)
        if run_options.use_ai:
            self._require_license(FEATURE_PROVIDERS)
        self._active_task_id = task_id
        self._cancel_event.clear()
        try:
            return self._orchestrator.resume(
                task_id,
                options=run_options.planner_options(),
                approval_callback=self._approval_callback,
                use_ai=run_options.use_ai,
                max_recovery_attempts=run_options.max_recovery_attempts,
                force=force,
                cancellation_requested=self._cancel_event.is_set,
            )
        finally:
            self._active_task_id = None

    def request_task_cancellation(self, task_id: str) -> str:
        """Cancel immediately when safe, else flag the running worker + DB."""

        if self._active_task_id == task_id:
            self._cancel_event.set()
        task, action = self._orchestrator.request_cancellation(task_id)
        if action == "cancelled":
            return f"Task {task.id} cancelled."
        return (
            f"Cancellation requested for task {task.id}; it will stop at the "
            "next stage boundary."
        )

    def diagnose_task(self, task_id: str) -> TaskDiagnosis:
        return self._recovery.diagnose(task_id)

    def recover_task(
        self,
        task_id: str,
        options: TaskRunOptions | None = None,
        *,
        force: bool = False,
    ) -> RecoveryOutcome:
        run_options = options or TaskRunOptions()
        self._require_license(FEATURE_CORE_RECOVERY)
        if run_options.use_ai:
            self._require_license(FEATURE_PROVIDERS)
        return self._recovery.recover(
            task_id,
            approval_callback=self._approval_callback,
            options=run_options.planner_options(),
            use_ai=run_options.use_ai,
            force=force,
            max_recovery_attempts=run_options.max_recovery_attempts,
        )

    # ------------------------------------------------------------------
    # Direct document, web and desktop tools
    # ------------------------------------------------------------------
    def office_inspect(self, path: str) -> ToolResult:
        return self._invoke_builtin("office.inspect", {"path": _require(path, "file")})

    def office_extract_text(
        self, path: str, *, output_path: str | None = None, max_chars: int = 20_000
    ) -> ToolResult:
        arguments: dict[str, Any] = {
            "path": _require(path, "file"),
            "max_chars": max_chars,
        }
        if output_path and output_path.strip():
            arguments["output_path"] = output_path.strip()
        return self._invoke_builtin("office.extract_text", arguments)

    def office_csv_inspect(self, path: str, *, sample_rows: int = 5) -> ToolResult:
        return self._invoke_builtin(
            "office.csv_inspect",
            {"path": _require(path, "CSV file"), "sample_rows": sample_rows},
        )

    def office_csv_to_json(self, input_path: str, output_path: str) -> ToolResult:
        return self._invoke_builtin(
            "office.csv_to_json",
            {
                "input_path": _require(input_path, "input CSV"),
                "output_path": _require(output_path, "output JSON"),
            },
        )

    def office_json_to_csv(self, input_path: str, output_path: str) -> ToolResult:
        return self._invoke_builtin(
            "office.json_to_csv",
            {
                "input_path": _require(input_path, "input JSON"),
                "output_path": _require(output_path, "output CSV"),
            },
        )

    def office_csv_to_xlsx(
        self, input_path: str, output_path: str, *, sheet_name: str = "Sheet1"
    ) -> ToolResult:
        return self._invoke_builtin(
            "office.csv_to_xlsx",
            {
                "input_path": _require(input_path, "input CSV"),
                "output_path": _require(output_path, "output XLSX"),
                "sheet_name": _require(sheet_name, "sheet name"),
            },
        )

    def office_create_docx(
        self,
        output_path: str,
        title: str,
        paragraphs: Sequence[str],
    ) -> ToolResult:
        cleaned = [paragraph.strip() for paragraph in paragraphs if paragraph.strip()]
        return self._invoke_builtin(
            "office.create_docx",
            {
                "output_path": _require(output_path, "output DOCX"),
                "title": _require(title, "document title"),
                "paragraphs": cleaned,
            },
        )

    def pdf_inspect(self, path: str) -> ToolResult:
        return self._invoke_builtin("pdf.inspect", {"path": _require(path, "PDF file")})

    def pdf_verify(self, path: str, *, expected_pages: int | None = None) -> ToolResult:
        arguments: dict[str, Any] = {"path": _require(path, "PDF file")}
        if expected_pages is not None:
            arguments["expected_pages"] = expected_pages
        return self._invoke_builtin("pdf.verify", arguments)

    def pdf_merge_folders(
        self,
        input_folders: Sequence[str],
        output_path: str,
        *,
        recursive: bool = False,
    ) -> ToolResult:
        folders = [folder.strip() for folder in input_folders if folder.strip()]
        if not folders:
            raise PcAgentError("Add at least one PDF input folder.")
        return self._invoke_builtin(
            "pdf.merge_folders",
            {
                "input_folders": folders,
                "output_path": _require(output_path, "output PDF"),
                "recursive": recursive,
            },
        )

    def files_copy(self, source: str, destination: str) -> ToolResult:
        return self._invoke_builtin(
            "files.copy",
            {
                "path": _require(source, "source file"),
                "output_path": _require(destination, "destination file"),
            },
        )

    def files_move(self, source: str, destination: str) -> ToolResult:
        return self._invoke_builtin(
            "files.move",
            {
                "path": _require(source, "source file"),
                "output_path": _require(destination, "destination file"),
            },
        )

    def files_rename(self, source: str, new_name: str) -> ToolResult:
        return self._invoke_builtin(
            "files.rename",
            {
                "path": _require(source, "source file"),
                "new_name": _require(new_name, "new file name"),
            },
        )

    def files_organize(
        self, folder: str, *, by: str = "category", max_files: int = 500
    ) -> ToolResult:
        return self._invoke_builtin(
            "files.organize",
            {
                "directory": folder.strip() or ".",
                "by": by,
                "max_files": max_files,
            },
        )

    def files_search(
        self,
        query: str,
        *,
        folder: str = ".",
        search_content: bool = False,
        max_results: int = 100,
    ) -> ToolResult:
        return self._invoke_builtin(
            "files.search",
            {
                "query": _require(query, "search text"),
                "directory": folder.strip() or ".",
                "search_content": search_content,
                "max_results": max_results,
            },
        )

    def fetch_web(
        self,
        url: str,
        *,
        max_chars: int = 20_000,
        output_path: str | None = None,
        allow_private_network: bool = False,
    ) -> WebFetchOutcome:
        cleaned_url = _require(url, "URL")
        self._require_license(FEATURE_WEB)
        destination = (
            self._safe_web_output_path(output_path)
            if output_path and output_path.strip()
            else None
        )
        if destination is not None and destination.exists():
            raise PcAgentError(
                "Web output already exists. This action never overwrites; "
                "choose a new output path."
            )
        result = self._builtin_registry.invoke(
            "web.fetch",
            {"url": cleaned_url, "max_chars": max_chars},
            self._context(
                network_access_allowed=True,
                allow_private_network=allow_private_network,
            ),
        )
        if not result.success or result.output is None or destination is None:
            return WebFetchOutcome(result=result)

        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(str(result.output["text"]))
                handle.write("\n")
        except FileExistsError as exc:
            raise PcAgentError(
                "Web output already exists. This action never overwrites; "
                "choose a new output path."
            ) from exc
        except OSError as exc:
            raise PcAgentError(f"Could not save web output: {exc}") from exc
        saved = destination.relative_to(
            self.settings.workspace_dir.resolve()
        ).as_posix()
        return WebFetchOutcome(result=result, saved_path=saved)

    def desktop_info(self) -> ToolResult:
        return self._invoke_builtin("desktop.system_info", {})

    def open_workspace_path(self, path: str) -> ToolResult:
        self._require_license(FEATURE_DESKTOP)
        return self._builtin_registry.invoke(
            "desktop.open_path",
            {"path": _require(path, "workspace path")},
            self._context(desktop_open_allowed=True),
        )

    def open_url(self, url: str) -> ToolResult:
        self._require_license(FEATURE_DESKTOP)
        return self._builtin_registry.invoke(
            "desktop.open_url",
            {"url": _require(url, "URL")},
            self._context(desktop_open_allowed=True),
        )

    # ------------------------------------------------------------------
    # Tool discovery and approved dynamic tools
    # ------------------------------------------------------------------
    def list_tools(self, *, include_disabled: bool = True) -> list[RegisteredTool]:
        registry = build_runtime_registry(
            self.database, data_dir=self.settings.data_dir
        )
        return registry.list_tools(include_disabled=include_disabled)

    def get_tool(self, tool_id: str) -> RegisteredTool | None:
        registry = build_runtime_registry(
            self.database, data_dir=self.settings.data_dir
        )
        return registry.get(tool_id)

    def list_dynamic_candidates(self, *, limit: int = 100) -> list[DynamicToolRecord]:
        return self._dynamic_tools.list_candidates(limit=limit)

    def invoke_dynamic_tool(
        self, tool_id: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        registry = build_runtime_registry(
            self.database, data_dir=self.settings.data_dir
        )
        tool = registry.get(tool_id)
        if tool is None:
            raise ToolError(f"Tool not found: {tool_id}")
        if not tool.spec.provenance.startswith("dynamic:"):
            raise ToolError(
                "The GUI Tools tab invokes approved dynamic custom.* tools only. "
                "Use Tasks or the dedicated document/web/desktop tabs for built-ins."
            )
        self._require_license(FEATURE_TOOL_BUILDER)
        return registry.invoke(tool_id, dict(arguments), self._context())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _context(
        self,
        *,
        network_access_allowed: bool = False,
        allow_private_network: bool = False,
        desktop_open_allowed: bool = False,
    ) -> ToolContext:
        return ToolContext.for_workspace(
            self.settings.workspace_dir,
            network_access_allowed=network_access_allowed,
            allow_private_network=allow_private_network,
            desktop_open_allowed=desktop_open_allowed,
            web_timeout_seconds=self.settings.web_timeout_seconds,
            web_max_bytes=self.settings.web_max_bytes,
            desktop_opener=self._desktop_opener,
        )

    def _invoke_builtin(
        self, tool_id: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        if tool_id.startswith(("office.", "pdf.")):
            self._require_license(FEATURE_DOCUMENTS)
        elif tool_id.startswith("desktop."):
            self._require_license(FEATURE_DESKTOP)
        elif tool_id.startswith("web."):
            self._require_license(FEATURE_WEB)
        return self._builtin_registry.invoke(
            tool_id, dict(arguments), self._context()
        )

    def _safe_web_output_path(self, raw_output: str) -> Path:
        candidate = Path(raw_output.strip()).expanduser()
        if not candidate.is_absolute():
            candidate = self.settings.workspace_dir / candidate
        resolved = candidate.resolve()
        workspace = self.settings.workspace_dir.resolve()
        if not resolved.is_relative_to(workspace):
            raise PcAgentError("Web output must stay inside the workspace.")
        if resolved.suffix.lower() not in {".txt", ".md"}:
            raise PcAgentError("Web output must end with .txt or .md.")
        return resolved


def parse_line_list(text: str) -> tuple[str, ...]:
    """Parse GUI path fields that contain one exact path per line."""

    return tuple(line.strip() for line in text.splitlines() if line.strip())


def parse_optional_int(text: str, *, label: str) -> int | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError as exc:
        raise PcAgentError(f"{label} must be a whole number.") from exc


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if not cleaned:
        return {}
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise PcAgentError("Tool arguments must be one valid JSON object.") from exc
    if not isinstance(parsed, dict):
        raise PcAgentError("Tool arguments must be one valid JSON object.")
    return parsed


def format_approval_request(request: ApprovalRequest) -> str:
    lines = [
        "Approval is required for this exact, read-only external scope.",
        "",
        f"Task: {request.task_id}",
        f"Plan: {request.plan.summary}",
        "",
        "Allowed only for this run (list/read; no write or delete):",
    ]
    lines.extend(f"  {root}" for root in request.read_roots)
    output_path = request.plan.arguments.get("output_path")
    if output_path:
        lines.append(f"\nOutput stays inside the workspace: {output_path}")
    lines.append("\nApprove this exact scope?")
    return "\n".join(lines)


def format_tool_result(result: ToolResult) -> str:
    header = f"{result.tool_id} v{result.version} | success={result.success}"
    if result.success and result.output is not None:
        return header + "\n\n" + _json_text(dict(result.output))
    return header + f"\n\nError: {result.error or 'Tool failed without a message.'}"


def format_execution_report(report: ExecutionReport) -> str:
    lines = [
        f"Task: {report.task.id}",
        f"Status: {report.task.status.value}",
        f"Success: {report.success}",
    ]
    if report.source_task_id:
        lines.append(f"Source task: {report.source_task_id}")
    if report.plan is not None:
        lines.extend(
            [
                f"Plan: {report.plan.summary}",
                f"Planner: {report.plan.planner}",
                f"Tool: {report.plan.tool_id}",
            ]
        )
    for attempt in report.provider_attempts:
        suffix = f" - {attempt.error}" if attempt.error else ""
        lines.append(
            "Provider attempt: "
            f"{attempt.provider}/{attempt.model} "
            f"[{'success' if attempt.success else 'failed'}]{suffix}"
        )
    for attempt in report.recovery_attempts:
        lines.append(
            f"Recovery attempt {attempt.attempt_number}: "
            f"[{attempt.status}; {attempt.action}; "
            f"category={attempt.category or '-'}] {attempt.message or ''}".rstrip()
        )
    if report.recovered:
        lines.append("Recovered: True (a bounded self-healing retry succeeded)")
    if report.missing_information:
        lines.append("Needs input:")
        lines.extend(f"  - {item}" for item in report.missing_information)
    if report.tool_result is not None:
        lines.extend(["", "Tool result:", format_tool_result(report.tool_result)])
    if report.resumed_from_checkpoint:
        lines.append("Resumed from checkpoint: the completed tool step was not re-run.")
    if report.verification_message:
        lines.append(
            f"Verification: {report.verification_valid} - "
            f"{report.verification_message}"
        )
    if report.verification_report is not None:
        lines.append("Acceptance criteria:")
        for criterion_result in report.verification_report.results:
            mark = "PASS" if criterion_result.passed else "FAIL"
            lines.append(
                f"  [{mark}] {criterion_result.criterion.description}"
                f" - {criterion_result.evidence}"
            )
    if report.verification_result is not None:
        lines.extend(
            ["", "Verification result:", format_tool_result(report.verification_result)]
        )
    if report.error:
        lines.append(f"Error: {report.error}")
    if report.task.status is TaskStatus.FAILED:
        lines.append(
            "Next: select this task and use Diagnose, then Recover if appropriate."
        )
    return "\n".join(lines)


def format_task_details(details: TaskDetails) -> str:
    task = details.task
    lines = [
        f"Task: {task.id}",
        f"Status: {task.status.value}",
        f"Instruction: {task.instruction}",
        f"Created: {task.created_at.isoformat() if task.created_at else '-'}",
        f"Updated: {task.updated_at.isoformat() if task.updated_at else '-'}",
        f"Completed: {task.completed_at.isoformat() if task.completed_at else '-'}",
        f"Error: {task.error or '-'}",
    ]
    if details.plan is not None:
        lines.extend(["", "Stored plan:", _json_text(dict(details.plan))])
    if details.invocations:
        lines.append("\nTool invocations:")
        for invocation in details.invocations:
            suffix = f" - {invocation.error}" if invocation.error else ""
            lines.append(
                f"  #{invocation.id} {invocation.tool_id} "
                f"v{invocation.tool_version} "
                f"[{'success' if invocation.success else 'failed'}]{suffix}"
            )
    if details.approvals:
        lines.append("\nApprovals:")
        for approval in details.approvals:
            lines.append(
                f"  #{approval.id} {approval.action}: {approval.decision} "
                f"at {approval.created_at.isoformat()}"
            )
    if details.recovery_attempts:
        lines.append("\nRecovery checkpoints:")
        for attempt in details.recovery_attempts:
            lines.append(
                f"  #{attempt.id} attempt {attempt.attempt_number}: "
                f"[{attempt.status}; {attempt.action}; "
                f"category={attempt.category or '-'}] {attempt.message or ''}".rstrip()
            )
    if details.recovery_links:
        lines.append("\nRecovery links:")
        for link in details.recovery_links:
            lines.append(f"  {link.source_task_id} -> {link.recovery_task_id}")
    if details.checkpoints:
        lines.append("\nStep checkpoints:")
        for checkpoint in details.checkpoints:
            lines.append(
                f"  step {checkpoint.step_index} {checkpoint.step_key} "
                f"at {checkpoint.updated_at.isoformat()}"
            )
    if details.cancellation_requested:
        lines.append("\nCancellation: requested (takes effect at the next stage boundary)")
    if details.latest_verification is not None:
        verification = details.latest_verification
        outcome = "passed" if verification.passed else "failed"
        lines.append(f"\nAcceptance verification: {outcome}")
        criteria = verification.report.get("criteria") or []
        for criterion in criteria:
            if isinstance(criterion, dict):
                mark = "PASS" if criterion.get("passed") else "FAIL"
                lines.append(
                    f"  [{mark}] {criterion.get('description', '-')}"
                    f" - {criterion.get('evidence', '')}"
                )
    if details.events:
        lines.append("\nEvents:")
        for event in details.events:
            old = event.old_status.value if event.old_status else "-"
            new = event.new_status.value if event.new_status else "-"
            suffix = f" - {event.message}" if event.message else ""
            lines.append(
                f"  {event.created_at.isoformat()} {event.event_type}: "
                f"{old} -> {new}{suffix}"
            )
    return "\n".join(lines)


def format_diagnosis(diagnosis: TaskDiagnosis) -> str:
    return _json_text(diagnosis.to_dict())


def format_recovery_outcome(outcome: RecoveryOutcome) -> str:
    lines = [
        outcome.message,
        f"Source task: {outcome.source_task.id} [{outcome.source_task.status.value}]",
        (
            "Recovery task: "
            f"{outcome.recovery_task.id} [{outcome.recovery_task.status.value}]"
        ),
        f"Cloned: {outcome.cloned}",
    ]
    if outcome.report is not None:
        lines.extend(["", format_execution_report(outcome.report)])
    return "\n".join(lines)


def format_recovery_scan(scan: RecoveryScan, *, workspace_dir: Path) -> str:
    return _json_text(scan.to_dict(workspace_dir=workspace_dir))


def format_tool(tool: RegisteredTool) -> str:
    spec = tool.spec
    permissions = ", ".join(action.value for action in spec.permissions) or "none"
    return "\n".join(
        [
            f"Tool: {spec.id} v{spec.version}",
            f"Name: {spec.name}",
            f"Enabled: {tool.enabled}",
            f"Status: {spec.status.value}",
            f"Provenance: {spec.provenance}",
            f"Permissions: {permissions}",
            f"Network required: {spec.network_required}",
            f"Timeout seconds: {spec.timeout_seconds}",
            "",
            "Description:",
            " ".join(spec.description.split()),
            "",
            "Input schema:",
            _json_text(dict(spec.input_schema)),
            "",
            "Output schema:",
            _json_text(dict(spec.output_schema)),
        ]
    )


def format_provider_statuses(statuses: Sequence[ProviderStatus]) -> str:
    lines: list[str] = []
    for status in statuses:
        route = "in route" if status.in_route else "not in route"
        configured = "configured" if status.configured else "not configured"
        implemented = "implemented" if status.implemented else "reserved/not implemented"
        lines.append(
            f"{status.name}/{status.model}: {configured}; {implemented}; {route} - "
            f"{status.detail}"
        )
    return "\n".join(lines)


def format_license_status(status: LicenseStatus, *, machine_id: str) -> str:
    lines = [
        f"License state: {status.state} (enforcement={status.enforcement})",
        f"Detail: {status.detail}",
    ]
    if status.plan:
        lines.append(f"Plan: {status.plan}")
    if status.licensee:
        lines.append(f"Licensee: {status.licensee}")
    if status.license_id:
        lines.append(f"License ID: {status.license_id}")
    if status.expires_at:
        lines.append(f"Expires: {status.expires_at.isoformat()}")
    if status.days_remaining is not None:
        lines.append(f"Days remaining: {status.days_remaining}")
    lines.append(f"Licensed features: {', '.join(status.features) or 'none'}")
    lines.append(f"License file: {status.license_file}")
    lines.append(f"Machine ID: {machine_id}")
    if status.enforcement == "off":
        lines.append(
            "Enforcement is off (development default); nothing is blocked."
        )
    return "\n".join(lines)


def _approval_unavailable(request: ApprovalRequest) -> bool:
    _ = request
    raise ApprovalUnavailableError(
        "No GUI approval broker is attached. The task remains "
        "waiting_for_approval; open the desktop GUI or use the CLI approval flow."
    )


def _require(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise PcAgentError(f"Enter a {label} path or value.")
    return cleaned


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)
