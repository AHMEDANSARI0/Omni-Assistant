"""Headless controller used by the desktop GUI (Phase 5-7).

The GUI does not shell out to the CLI and does not invent a second execution
path.  This controller calls the same orchestrator, recovery service, provider
router, registered tools and license gates as the command-line interface, so
policy checks, approvals, verification and audit records stay in one place.
Phase 7 adds checkpoint resume and cooperative cancellation for GUI tasks.
"""

from __future__ import annotations

import json
import math
import platform
import queue
import shutil
import ssl
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .. import __version__
from ..auto_upgrade import (
    AutoUpgradeRunReport,
    AutoUpgradeSchedulerResult,
    AutoUpgradeStatus,
    get_auto_upgrade_schedule,
    get_auto_upgrade_status,
    install_auto_upgrade_schedule,
    run_auto_upgrade,
    uninstall_auto_upgrade_schedule,
)
from ..baseline import TRANSFORMATION_LABEL
from ..browser_cdp import probe_browser
from ..config import Settings
from ..credentials import CredentialStore, build_credential_store
from ..desktop_automation import probe_desktop_automation
from ..documents.office import DemoOfficeData, create_demo_office_files
from ..documents.pdf import DemoPdfData, create_demo_pdfs
from ..documents.pdf_document_timestamps import add_pdf_document_timestamp
from ..documents.pdf_ltv import add_pdf_ltv_material, inspect_pdf_dss
from ..documents.pdf_security import (
    DEFAULT_PDF_PERMISSIONS,
    inspect_pdf_security,
    protect_pdf,
    unprotect_pdf,
)
from ..documents.pdf_signatures import inspect_pdf_signatures
from ..documents.pdf_signing import sign_pdf
from ..documents.pdf_trust import validate_pdf_signature_trust
from ..domain.task import Task, TaskStatus
from ..errors import ApprovalUnavailableError, PcAgentError, ToolError
from ..http_tls import tls_trust_info
from ..knowledge import KnowledgeError
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
from ..maintenance import DatabaseMaintenanceService
from ..missions import MissionRunner, execution_levels
from ..online_updates import (
    DownloadedUpdate,
    OnlineStageResult,
    OnlineUpdateCheck,
    check_online_update,
    download_online_update,
    stage_online_update,
)
from ..orchestrator import (
    ApprovalCallback,
    ApprovalRequest,
    ExecutionReport,
    TaskOrchestrator,
)
from ..planning import DeterministicPlanner, PlannerOptions
from ..provider_platform import (
    capability_catalog,
    load_latest_evaluation,
    load_usage_records,
    provider_platform_line,
    run_evaluations,
    usage_stats,
)
from ..providers import (
    CompletionRequest,
    ProviderStatus,
    build_model_router,
    provider_statuses,
)
from ..providers.router import RoutedCompletion
from ..recovery import RecoveryOutcome, RecoveryScan, RecoveryService, TaskDiagnosis
from ..release import ReleaseAudit, audit_install
from ..safety import EmergencyStop
from ..self_repair import SelfRepairDiagnosis, SelfRepairService
from ..storage.db import Database
from ..storage.repositories import (
    ApprovalRecord,
    DynamicToolRecord,
    DynamicToolRepository,
    EventRepository,
    ExecutionRepository,
    MemoryRepository,
    MissionRepository,
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
from ..system_monitor import SystemMonitor, system_summary_line
from ..tooling.service import build_runtime_registry
from ..tools import RegisteredTool, ToolContext, ToolResult, build_default_registry
from ..updates import (
    StagedUpdate,
    UpdateCheck,
    apply_staged_update,
    check_update,
    list_updates,
    rollback_update,
    stage_update,
)


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
    subprocess_enabled: bool = False
    command_allowlist: str = ""
    git_available: bool = False
    browser_enabled: bool = False
    browser_port: int = 9222
    browser_connected: str = ""
    desktop_automation_enabled: bool = False
    desktop_automation_runtime: str = ""
    vision_enabled: bool = False
    vision_providers: str = ""
    credential_store_summary: str = ""
    provider_credential_sources: str = "none"
    self_repair_enabled: bool = False
    self_repair_summary: str = ""
    task_budgets_summary: str = ""
    safety_summary: str = ""
    service_summary: str = ""
    mission_summary: str = ""
    release_audit_summary: str = ""
    database_backup_summary: str = ""
    signed_updates_summary: str = ""

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
                f"Self-Healing PC Agent {self.version} ({TRANSFORMATION_LABEL})",
                f"Python: {self.python_version}",
                f"Environment: {self.environment}",
                f"Root: {self.root}",
                f"Workspace: {self.workspace}",
                f"Database: {self.database_path} (schema v{self.schema_version})",
                f"Provider order: {self.provider_order}",
                f"Providers with credentials: {self.providers_with_credentials}",
                f"Usable provider route: {' -> '.join(self.provider_route)}",
                (
                    "Credential store: "
                    f"{self.credential_store_summary}; "
                    f"sources={self.provider_credential_sources}"
                ),
                f"Task budgets: {self.task_budgets_summary}",
                f"Safety kernel: {self.safety_summary}",
                f"Local service: {self.service_summary}",
                f"Mission center: {self.mission_summary}",
                (
                    "Self-repair: "
                    f"{'enabled' if self.self_repair_enabled else 'disabled'}; "
                    f"{self.self_repair_summary}"
                ),
                (
                    "Production: "
                    f"release audit {self.release_audit_summary}; "
                    f"database backups: {self.database_backup_summary}"
                ),
                f"Signed updates: {self.signed_updates_summary}",
                (
                    "Approved dynamic tools: "
                    f"{self.approved_dynamic_tools} "
                    f"({self.enabled_dynamic_tools} enabled)"
                ),
                f"Bundled dynamic-tool example arguments: {example}",
                (
                    "Subprocess: "
                    + (
                        f"enabled; allowlist={self.command_allowlist or 'none'}; "
                        f"git={'found' if self.git_available else 'not found on PATH'}"
                        if self.subprocess_enabled
                        else "disabled (set PC_AGENT_ALLOW_SUBPROCESS=1 in .env)"
                    )
                ),
                (
                    "Browser: "
                    + (
                        f"enabled; debug port={self.browser_port}; "
                        f"{self.browser_connected or 'not reachable (start Edge/Chrome with --remote-debugging-port)'}"
                        if self.browser_enabled
                        else "disabled (set PC_AGENT_ALLOW_BROWSER=1 in .env)"
                    )
                ),
                (
                    "Desktop automation: "
                    + (
                        "enabled; runtime="
                        f"{self.desktop_automation_runtime or 'unavailable'}; "
                        "focus/click/type/keys require approval"
                        if self.desktop_automation_enabled
                        else "disabled (set PC_AGENT_ALLOW_DESKTOP_AUTOMATION=1 in .env)"
                    )
                ),
                (
                    "Vision: "
                    + (
                        "enabled; providers="
                        f"{self.vision_providers or 'none configured'}; "
                        "each exact image send requires approval"
                        if self.vision_enabled
                        else "disabled (set PC_AGENT_ALLOW_VISION=1 in .env; local image tools remain available)"
                    )
                ),
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
                    "web.fetch=public-only by default, desktop=open/info plus gated Windows UIA, images=local Pillow tools plus gated provider vision, provider keys=environment first, then secure OS storage"
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
    token_budget: int | None = None
    provider_call_budget: int | None = None
    tool_budget: int | None = None
    step_budget: int | None = None
    task_time_budget_seconds: float | None = None

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
        for name, value, maximum in (
            ("Token budget", self.token_budget, 10_000_000),
            ("Provider-call budget", self.provider_call_budget, 1000),
            ("Tool budget", self.tool_budget, 1000),
        ):
            if value is not None and not 0 <= value <= maximum:
                raise PcAgentError(f"{name} must be between 0 and {maximum}.")
        if self.step_budget is not None and not 1 <= self.step_budget <= 20:
            raise PcAgentError("Step budget must be between 1 and 20.")
        if self.task_time_budget_seconds is not None and not (
            0 <= self.task_time_budget_seconds <= 86_400
        ):
            raise PcAgentError("Task time budget must be between 0 and 86400 seconds.")

    def planner_options(self) -> PlannerOptions:
        return PlannerOptions(
            input_folders=self.input_folders,
            input_files=self.input_files,
            output_path=self.output_path,
            target_path=self.target_path,
            recursive=self.recursive,
            expected_pages=self.expected_pages,
            token_budget=self.token_budget,
            provider_call_budget=self.provider_call_budget,
            tool_budget=self.tool_budget,
            step_budget=self.step_budget,
            task_time_budget_seconds=self.task_time_budget_seconds,
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


@dataclass(frozen=True, slots=True)
class CommandCenterOverview:
    """Real live state for the Phase 5 Command Center tab."""

    tasks_total: int
    tasks_running: int
    tasks_succeeded: int
    tasks_failed: int
    missions_total: int
    missions_running: int
    events_total: int
    pending_approvals: int
    schedules_enabled: int
    emergency_stop: str
    api_line: str

    def to_text(self) -> str:
        return (
            f"Tasks: {self.tasks_total} (running {self.tasks_running}, "
            f"succeeded {self.tasks_succeeded}, failed {self.tasks_failed})   "
            f"Missions: {self.missions_total} (running {self.missions_running})   "
            f"Events: {self.events_total}   Pending approvals: {self.pending_approvals}   "
            f"Schedules on: {self.schedules_enabled}\n"
            f"Emergency stop: {self.emergency_stop}\n"
            f"Local API: {self.api_line}"
        )


@dataclass(frozen=True, slots=True)
class MissionRow:
    id: str
    name: str
    status: str
    total: int
    succeeded: int
    failed: int
    active: int


@dataclass(frozen=True, slots=True)
class MissionDetail:
    id: str
    name: str
    status: str
    text: str


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
        credential_store: CredentialStore | None = None,
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
        self._system_monitor = SystemMonitor(
            root_dir=settings.root_dir, workspace_dir=settings.workspace_dir
        )
        self._active_task_id: str | None = None
        self._router = build_model_router(settings)
        self._licenses = LicenseService(settings, self.database)
        self._approval_callback = approval_callback or _approval_unavailable
        self._desktop_opener = desktop_opener
        self._credential_store = credential_store

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
            subprocess_enabled=self.settings.allow_subprocess,
            command_allowlist=", ".join(self.settings.command_allowlist),
            git_available=shutil.which("git") is not None,
            browser_enabled=self.settings.allow_browser,
            browser_port=self.settings.browser_port,
            browser_connected=self._browser_connected_text(),
            desktop_automation_enabled=self.settings.allow_desktop_automation,
            desktop_automation_runtime=self._desktop_automation_runtime_text(),
            vision_enabled=self.settings.allow_vision,
            vision_providers=", ".join(
                provider.name
                for provider in self._router.providers
                if getattr(provider, "supports_vision", False) and provider.configured
            ),
            credential_store_summary=self.credential_store_summary(),
            provider_credential_sources=public["provider_credential_sources"],
            self_repair_enabled=self.settings.allow_self_repair,
            self_repair_summary=self.self_repair_summary(),
            task_budgets_summary=self.task_budgets_summary(),
            safety_summary=self.safety_summary(),
            service_summary=self.service_summary(),
            mission_summary=self.mission_summary(),
            release_audit_summary=self.release_audit().summary(),
            database_backup_summary=self.database_backup_summary(),
            signed_updates_summary=self.signed_updates_summary(),
        )

    def recovery_scan(self) -> RecoveryScan:
        return self._recovery.scan()

    def task_budgets_summary(self) -> str:
        settings = self.settings
        return (
            f"tokens={settings.task_token_budget}, "
            f"provider_calls={settings.task_provider_call_budget}, "
            f"tools={settings.task_tool_budget}, steps={settings.task_step_budget}, "
            f"time={settings.task_time_budget_seconds:.0f}s"
        )

    def safety_summary(self) -> str:
        settings = self.settings
        return (
            f"hard_timeouts={'on' if settings.safety_hard_timeouts else 'OFF'}, "
            f"worker_isolation={'on' if settings.safety_worker_isolation else 'off'}, "
            f"max_tool_seconds={settings.safety_max_tool_seconds:g}, "
            f"{EmergencyStop(settings.data_dir).status().summary()}, "
            f"pending_approvals={self._execution.pending_approval_center_count()}"
        )

    def service_summary(self) -> str:
        settings = self.settings
        return (
            f"api={'enabled' if settings.api_enabled else 'disabled'} "
            f"http://{settings.api_host}:{settings.api_port} (loopback), "
            f"log_format={settings.log_format}, "
            f"profile={settings.active_profile or 'none'}, "
            f"events={EventRepository(self.database).count()}"
        )

    def mission_summary(self) -> str:
        settings = self.settings
        repository = MissionRepository(self.database)
        return (
            f"missions={repository.count_missions()}, "
            f"max_parallel={settings.mission_max_parallel}, "
            f"schedules={repository.count_schedules()} "
            f"(enabled={repository.count_schedules(enabled_only=True)})"
        )

    # -- Command Center (Transformation Phase 5) ------------------------------
    def command_center_overview(self) -> CommandCenterOverview:
        missions = MissionRepository(self.database)
        running = sum(
            self._tasks.count(status=status)
            for status in (
                TaskStatus.RUNNING,
                TaskStatus.PLANNING,
                TaskStatus.VERIFYING,
                TaskStatus.RETRYING,
            )
        )
        mission_rows = missions.list_missions(limit=200)
        return CommandCenterOverview(
            tasks_total=self._tasks.count(),
            tasks_running=running,
            tasks_succeeded=self._tasks.count(status=TaskStatus.SUCCEEDED),
            tasks_failed=self._tasks.count(status=TaskStatus.FAILED),
            missions_total=len(mission_rows),
            missions_running=sum(1 for row in mission_rows if row.status == "running"),
            events_total=EventRepository(self.database).count(),
            pending_approvals=self._execution.pending_approval_center_count(),
            schedules_enabled=missions.count_schedules(enabled_only=True),
            emergency_stop=EmergencyStop(self.settings.data_dir).status().summary(),
            api_line=(
                f"{'enabled' if self.settings.api_enabled else 'disabled'} "
                f"http://{self.settings.api_host}:{self.settings.api_port}/ (loopback)"
            ),
        )

    def list_mission_rows(self, *, limit: int = 100) -> list[MissionRow]:
        missions = MissionRepository(self.database)
        rows: list[MissionRow] = []
        for mission in missions.list_missions(limit=limit):
            statuses = []
            for task_id in missions.list_task_ids(mission.id):
                task = self._tasks.get(task_id)
                if task is not None:
                    statuses.append(task.status)
            rows.append(
                MissionRow(
                    id=mission.id,
                    name=mission.name,
                    status=mission.status,
                    total=len(statuses),
                    succeeded=sum(1 for s in statuses if s is TaskStatus.SUCCEEDED),
                    failed=sum(
                        1
                        for s in statuses
                        if s in {TaskStatus.FAILED, TaskStatus.NEEDS_INPUT}
                    ),
                    active=sum(
                        1
                        for s in statuses
                        if s
                        in {
                            TaskStatus.RUNNING,
                            TaskStatus.PLANNING,
                            TaskStatus.VERIFYING,
                            TaskStatus.RETRYING,
                        }
                    ),
                )
            )
        return rows

    def mission_detail(self, mission_id: str) -> MissionDetail:
        missions = MissionRepository(self.database)
        mission = missions.get_mission(mission_id)
        if mission is None:
            raise PcAgentError(f"Mission not found: {mission_id}")
        task_ids = missions.list_task_ids(mission.id)
        dependencies = missions.list_dependencies(mission.id)
        lines = [f"Mission {mission.id} [{mission.status}] {mission.name}"]
        for task_id in task_ids:
            task = self._tasks.get(task_id)
            if task is None:
                continue
            deps = ", ".join(dep[:8] for dep in dependencies.get(task_id, [])) or "-"
            lines.append(
                f"- {task_id[:8]} [{task.status.value}] after={deps} {task.instruction}"
            )
        try:
            levels = execution_levels(task_ids, dependencies)
        except PcAgentError:
            levels = []
        if levels:
            lines.append("Execution graph levels:")
            for index, level in enumerate(levels):
                lines.append(
                    f"  level {index}: {', '.join(item[:8] for item in level)}"
                )
        return MissionDetail(
            id=mission.id, name=mission.name, status=mission.status, text="\n".join(lines)
        )

    def run_mission(self, mission_id: str, *, use_ai: bool = False) -> MissionDetail:
        """Blocking mission run for GUI worker threads (real Safety Kernel)."""
        cancel_event = getattr(self, "_mission_cancel_event", None)
        if cancel_event is None:
            cancel_event = threading.Event()
            self._mission_cancel_event = cancel_event
        cancel_event.clear()
        runner = MissionRunner(
            settings=self.settings,
            database=self.database,
            emit=lambda event_type, **kwargs: EventRepository(self.database).record(
                event_type,
                source=str(kwargs.get("source", "gui")),
                summary=str(kwargs.get("summary", "")),
                task_id=kwargs.get("task_id"),
                payload=kwargs.get("payload"),
            ),
        )
        runner.run(mission_id, use_ai=use_ai, cancel_event=cancel_event, source="gui")
        return self.mission_detail(mission_id)

    def cancel_mission(self, mission_id: str) -> MissionDetail:
        cancel_event = getattr(self, "_mission_cancel_event", None)
        if cancel_event is not None:
            cancel_event.set()
        missions = MissionRepository(self.database)
        for task_id in missions.list_task_ids(mission_id):
            task = self._tasks.get(task_id)
            if task is not None and not task.is_terminal:
                try:
                    self._orchestrator.request_cancellation(
                        task_id, reason="Mission cancelled from the GUI."
                    )
                except PcAgentError:
                    pass
        mission = missions.get_mission(mission_id)
        if mission is not None and mission.status == "draft":
            missions.update_mission_status(mission_id, "cancelled")
        return self.mission_detail(mission_id)

    def pending_approval_center_requests(self, *, limit: int = 50):
        return self._execution.list_approval_center_requests(
            status="pending", limit=limit
        )

    def decide_approval_center(self, request_key: str, *, approve: bool) -> str:
        decided = self._execution.decide_approval_center_request(
            request_key, approve=approve, decided_by="gui"
        )
        return f"Approval Center request {decided.request_key} {decided.status}."

    def system_status_text(self) -> str:
        return system_summary_line(self._system_monitor.snapshot())

    def emergency_stop_status_text(self) -> str:
        return EmergencyStop(self.settings.data_dir).status().summary()

    def set_emergency_stop(self, *, engaged: bool, reason: str = "") -> str:
        stop = EmergencyStop(self.settings.data_dir)
        events = EventRepository(self.database)
        if engaged:
            state = stop.engage(reason=reason or "Engaged from the GUI.", engaged_by="gui")
            events.record(
                "safety.emergency_stop_engaged",
                source="gui",
                summary=f"Emergency stop engaged from GUI: {reason[:140]}",
            )
        else:
            state = stop.clear(cleared_by="gui")
            events.record(
                "safety.emergency_stop_released",
                source="gui",
                summary="Emergency stop cleared from GUI.",
            )
        return state.summary()

    def events_since(self, cursor: int, *, limit: int = 100):
        events = EventRepository(self.database).list(since_id=cursor, limit=limit)
        return events, (events[-1].id if events else cursor)

    def _self_repair_service(self) -> SelfRepairService:
        return SelfRepairService(self.settings.root_dir, self.settings.data_dir)

    def self_repair_summary(self) -> str:
        service = self._self_repair_service()
        latest = next(iter(service.list_runs(limit=1)), None)
        suffix = (
            f"; latest run={latest.run_id} [{latest.status}]"
            if latest
            else "; latest run=none"
        )
        return service.baseline_status().summary() + suffix

    def self_repair_diagnosis(self) -> SelfRepairDiagnosis:
        """Read-only GUI diagnosis; capture/stage/apply stay CLI-only."""

        if not self.settings.allow_self_repair:
            raise PcAgentError(
                "Self-repair is disabled. Set PC_AGENT_ALLOW_SELF_REPAIR=1 "
                "in .env to enable read-only core diagnosis."
            )
        return self._self_repair_service().diagnose()

    def release_audit(self) -> ReleaseAudit:
        """Read-only production checks; bundle build/restore stay CLI-only."""

        return audit_install(self.settings)

    def signed_updates_summary(self) -> str:
        configured = "yes" if self.settings.release_public_key else "no"
        required = "yes" if self.settings.require_signed_updates else "no"
        try:
            updates = list_updates(self.settings)
            latest = updates[0].summary() if updates else "none"
        except PcAgentError as exc:
            latest = f"unavailable ({exc})"
        public = self.settings.public_summary()
        return (
            f"public_key={configured}; "
            f"key_id={self.settings.release_key_id or 'any'}; "
            f"required={required}; online={public['online_updates']}; "
            f"feed_host={public['update_feed_host']}; "
            f"channel={public['update_channel']}; "
            f"auto_upgrade={public['auto_upgrade']} "
            f"({public['auto_upgrade_mode']}); latest={latest}"
        )

    def check_offline_update(
        self, bundle_path: str, signature_path: str = ""
    ) -> UpdateCheck:
        bundle = bundle_path.strip()
        if not bundle:
            raise PcAgentError("Choose a portable release bundle first.")
        signature = signature_path.strip()
        return check_update(
            Path(bundle).expanduser(),
            self.settings,
            signature_path=Path(signature).expanduser() if signature else None,
        )

    def stage_offline_update(
        self,
        bundle_path: str,
        signature_path: str = "",
        *,
        allow_unsigned: bool = False,
        allow_downgrade: bool = False,
    ) -> StagedUpdate:
        bundle = bundle_path.strip()
        if not bundle:
            raise PcAgentError("Choose a portable release bundle first.")
        signature = signature_path.strip()
        self.settings.ensure_directories()
        return stage_update(
            Path(bundle).expanduser(),
            self.settings,
            signature_path=Path(signature).expanduser() if signature else None,
            allow_unsigned=allow_unsigned,
            allow_downgrade=allow_downgrade,
        )

    def check_online_update(
        self, feed_url: str = "", channel: str = ""
    ) -> OnlineUpdateCheck:
        return check_online_update(
            self.settings,
            feed_url=feed_url.strip() or None,
            channel=channel.strip() or None,
        )

    def download_online_update(
        self,
        feed_url: str = "",
        channel: str = "",
        *,
        allow_downgrade: bool = False,
    ) -> DownloadedUpdate:
        self.settings.ensure_directories()
        return download_online_update(
            self.settings,
            feed_url=feed_url.strip() or None,
            channel=channel.strip() or None,
            allow_downgrade=allow_downgrade,
        )

    def stage_online_update(
        self,
        feed_url: str = "",
        channel: str = "",
        *,
        allow_unsigned: bool = False,
        allow_downgrade: bool = False,
    ) -> OnlineStageResult:
        self.settings.ensure_directories()
        return stage_online_update(
            self.settings,
            feed_url=feed_url.strip() or None,
            channel=channel.strip() or None,
            allow_unsigned=allow_unsigned,
            allow_downgrade=allow_downgrade,
        )

    def apply_offline_update(self, update_id: str = "latest-staged") -> StagedUpdate:
        self.settings.ensure_directories()
        return apply_staged_update(
            self.settings, update_id.strip() or "latest-staged"
        )

    def rollback_offline_update(
        self, update_id: str = "latest-applied"
    ) -> StagedUpdate:
        self.settings.ensure_directories()
        return rollback_update(
            self.settings, update_id.strip() or "latest-applied"
        )

    def list_offline_updates(self, limit: int = 20) -> tuple[StagedUpdate, ...]:
        if limit < 1 or limit > 200:
            raise PcAgentError("Update list limit must be between 1 and 200.")
        return list_updates(self.settings)[:limit]

    def auto_upgrade_status(self) -> AutoUpgradeStatus:
        """Read background state; this never changes .env configuration."""

        return get_auto_upgrade_status(
            self.settings, include_scheduler=True
        )

    def auto_upgrade_run_now(self) -> AutoUpgradeRunReport:
        """Run one explicit GUI-triggered pass under the configured policy."""

        self.settings.ensure_directories()
        return run_auto_upgrade(self.settings, force=True)

    def auto_upgrade_schedule_status(self) -> AutoUpgradeSchedulerResult:
        return get_auto_upgrade_schedule()

    def install_auto_upgrade_schedule(self) -> AutoUpgradeSchedulerResult:
        return install_auto_upgrade_schedule(self.settings)

    def uninstall_auto_upgrade_schedule(self) -> AutoUpgradeSchedulerResult:
        return uninstall_auto_upgrade_schedule()

    def database_backup_summary(self) -> str:
        backups = DatabaseMaintenanceService(
            self.settings.database_path, self.settings.data_dir
        ).list_backups()
        if not backups:
            return "none"
        latest = backups[0]
        return f"count={len(backups)}; latest={latest.backup_id} (schema v{latest.schema_version})"

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

    def credential_store_summary(self) -> str:
        store = self._credential_store or build_credential_store(
            self.settings.credential_store
        )
        try:
            return store.status().summary()
        except PcAgentError as exc:
            return (
                f"mode={self.settings.credential_store}; "
                f"backend={self.settings.credential_store_backend}; "
                f"available=no; stored=unknown; {exc}"
            )

    def provider_statuses(self) -> list[ProviderStatus]:
        sources = dict(self.settings.credential_sources)
        return [
            replace(
                status,
                detail=(
                    f"{status.detail} Credential source: "
                    + (
                        "not-applicable"
                        if status.name == "mock"
                        else sources.get(status.name, "missing")
                    )
                    + "."
                ),
            )
            for status in provider_statuses(self.settings)
        ]

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

    def provider_platform_summary(self) -> str:
        lines = [
            "Provider Platform v2: " + provider_platform_line(self.settings, self._router)
        ]
        for row in capability_catalog(self.settings, self._router):
            flags = [
                label
                for label, active in (
                    ("local", row["local"]),
                    ("streaming", row["streaming"]),
                    ("vision", row["vision"]),
                    ("priced", row["priced"]),
                )
                if active
            ]
            lines.append(
                f"  {row['provider']}/{row['model']}: {', '.join(flags) or 'remote'}"
            )
        stats = usage_stats(load_usage_records(self.settings))
        for row in stats["by_model"]:
            lines.append(
                f"  usage {row['provider']}/{row['model']}: calls={row['calls']} "
                f"median={row['median_latency_ms']}ms"
            )
        evaluation = load_latest_evaluation(self.settings)
        if evaluation:
            lines.append(
                f"  latest evaluation: {evaluation.get('passed')}/"
                f"{evaluation.get('total')} passed"
            )
        return "\n".join(lines)

    def run_provider_evaluations(self) -> dict[str, Any]:
        self._require_license(FEATURE_PROVIDERS)
        return run_evaluations(self.settings, self._router)

    # -- Agent Studio & multi-agent runtime (Transformation Phase 9) ---------
    def _agent_studio(self) -> Any:
        from ..agents import AgentStudio

        return AgentStudio(self.settings, self.database, router=self._router)

    def agents_list(self) -> list[dict[str, Any]]:
        return list(self._agent_studio().list_agents()["agents"])

    def agent_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        from ..errors import AgentError

        try:
            return self._agent_studio().save_agent(payload)
        except AgentError as exc:
            raise PcAgentError(str(exc)) from exc

    def agent_set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        from ..errors import AgentError

        try:
            return self._agent_studio().set_agent_enabled(name, enabled)
        except AgentError as exc:
            raise PcAgentError(str(exc)) from exc

    def agent_delete(self, name: str) -> dict[str, Any]:
        from ..errors import AgentError

        try:
            return self._agent_studio().delete_agent(name)
        except AgentError as exc:
            raise PcAgentError(str(exc)) from exc

    def agent_run_text(self, name: str, instruction: str, *, use_ai: bool) -> str:
        from ..errors import AgentError

        if use_ai:
            self._require_license(FEATURE_PROVIDERS)
        try:
            result = self._agent_studio().run_agent(
                name,
                instruction,
                use_ai=use_ai,
                approval_callback=self._approval_callback,
            )
        except AgentError as exc:
            raise PcAgentError(str(exc)) from exc
        return (
            f"Agent {result['agent']} run #{result['run_id']}: {result['status']}"
            f" (task {result['task_id']})\n{result['summary']}"
        )

    def agent_team_text(self, goal: str, *, use_ai: bool) -> str:
        from ..errors import AgentError

        # Coordinator decomposition (and optional AI planning) uses providers.
        self._require_license(FEATURE_PROVIDERS)
        try:
            result = self._agent_studio().run_team(
                goal,
                use_ai=use_ai,
                approval_callback=self._approval_callback,
            )
        except AgentError as exc:
            raise PcAgentError(str(exc)) from exc
        lines = [
            (
                f"Team {result['team_run_id']}: "
                f"{result['succeeded']}/{result['total']} succeeded"
            )
        ]
        lines.extend(
            f"  {item['agent']}: {item['status']} - {item['summary'][:160]}"
            for item in result["results"]
        )
        return "\n".join(lines)

    def agent_runs_text(self, limit: int = 20) -> str:
        runs = self._agent_studio().list_runs(limit=limit)["runs"]
        if not runs:
            return "No agent runs yet."
        return "\n".join(
            f"#{run['id']} {run['agent']} [{run['status']}] "
            f"{run['instruction'][:70]}"
            + (f" (team {run['team_run_id']})" if run["team_run_id"] else "")
            for run in runs
        )

    def agents_stats_text(self) -> str:
        stats = self._agent_studio().stats()
        return (
            f"agents={stats['agents']} enabled={stats['agents_enabled']} "
            f"runs={stats['agent_runs']}"
        )

    # -- Visual Workflow Builder (Transformation Phase 12) -------------------
    def _flow_service(self) -> Any:
        from ..service import LocalService

        service = getattr(self, "_flows_service_cache", None)
        if service is None:
            service = LocalService(self.settings)
            self._flows_service_cache = service
        return service

    def flows_summary_text(self) -> str:
        from ..diagnostics import flows_summary_line

        return flows_summary_line(self.settings, self.database)

    def flows_list(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._flow_service().list_flows(limit=limit)["flows"]

    def flow_show_text(self, reference: str) -> str:
        payload = self._flow_service().get_flow(reference)
        edges = payload["flow"]["edges"]
        header = (
            f"{payload['name']} v{payload['version']} "
            f"({'enabled' if payload['enabled'] else 'DISABLED'}) "
            f"levels={len(payload['levels'])}"
        )
        lines = [header]
        for node in payload["flow"]["nodes"]:
            after = [edge["from"] for edge in edges if edge["to"] == node["id"]]
            lines.append(
                f"  {node['id']}: {node['title'] or '(no title)'} "
                f"after={after or 'none'} -> {node['instruction'][:70]}"
            )
        for trigger in payload["triggers"]:
            lines.append(
                f"  trigger {trigger['id'][:8]} {trigger['kind']} "
                f"enabled={trigger['enabled']} next={trigger['next_run_at'] or '-'}"
            )
        for run in payload["runs"][:5]:
            lines.append(
                f"  run {run['id'][:8]} {run['status']} via {run['trigger_kind']}"
            )
        return "\n".join(lines)

    def flow_validate(self, document: str) -> str:
        from ..errors import FlowError

        del FlowError
        payload = self._flow_document(document)
        result = self._flow_service().validate_flow(payload)
        return (
            f"valid: {result['name']} with {len(result['nodes'])} node(s); "
            f"levels={result['levels']}"
        )

    def flow_save(self, document: str, *, reference: str = "") -> str:
        payload = self._flow_document(document)
        service = self._flow_service()
        if reference.strip():
            saved = service.update_flow(reference.strip(), payload)
            return f"updated {saved['name']} to v{saved['version']}"
        saved = service.create_flow(payload)
        return f"created {saved['name']} v{saved['version']}"

    def flow_run(self, reference: str) -> str:
        result = self._flow_service().run_flow(reference.strip())
        return (
            f"started mission {result['mission_id']} for "
            f"{result['flow']['name']} v{result['flow']['version']} "
            f"(levels={result['levels']})"
        )

    # -- accessibility / i18n (Transformation Phase 17) ----------------------
    def production_report(self) -> dict[str, Any]:
        """The stored end-to-end release-chain report, or an empty state."""

        from ..production_check import stored_production_report

        stored = stored_production_report(self.settings)
        if stored is None:
            return {
                "ok": False,
                "failures": 0,
                "steps": [],
                "version": self.settings_identity_version(),
                "environment": {},
            }
        return stored

    def production_check(self) -> dict[str, Any]:
        """Run the real release chain check (build, verify, sign, apply, rollback)."""

        from ..production_check import run_production_check

        return run_production_check(self.settings).to_dict()

    def settings_identity_version(self) -> str:
        from .. import __version__

        return __version__

    def a11y_report(self) -> dict[str, Any]:
        from ..a11y import a11y_status

        return a11y_status(self.settings)

    def a11y_check_text(self) -> str:
        report = self.a11y_report()
        contrast = report["contrast"]
        checks = [
            ("language_supported", report["locale"] in report["supported_locales"]),
            ("coverage_complete", report["coverage"]["percent"] >= 100.0),
            ("contrast_aa", contrast["passing"] == contrast["total"]),
            ("text_scale_in_range", 0.8 <= report["scale"] <= 2.0),
            ("direction_declared", report["direction"] in {"ltr", "rtl"}),
            ("keyboard_shortcuts_documented", len(report["shortcuts"]) >= 4),
            ("limits_disclosed", len(report["limitations"]) >= 3),
        ]
        lines = ["Accessibility check (measured, not asserted):"]
        for name, passes in checks:
            lines.append(f"  [{'PASS' if passes else 'FAIL'}] {name}")
        failed = [name for name, passes in checks if not passes]
        lines.append("")
        lines.append(
            "result: " + ("all checks pass" if not failed else "failures: " + ", ".join(failed))
        )
        lines.append("")
        lines.append("Known limits:")
        for limitation in report["limitations"]:
            lines.append(f"  - {limitation}")
        return "\n".join(lines)

    def a11y_apply(
        self,
        *,
        locale: str,
        scale: float,
        high_contrast: bool,
        reduce_motion: bool,
    ) -> str:
        from ..service import LocalService

        result = LocalService(self.settings).i18n_set(
            locale=locale,
            scale=scale,
            high_contrast=high_contrast,
            reduce_motion=reduce_motion,
        )
        changed = ", ".join(f"{key}={value}" for key, value in sorted(result["saved"].items()))
        return f"saved {changed}"

    def i18n_status_text(self) -> str:
        from ..i18n import Translator

        translator = Translator(self.settings.ui_locale)
        return (
            f"language {translator.locale} ({translator.language_name}) "
            f"direction {translator.direction} "
            f"coverage {translator.coverage()['percent']}%"
        )

    # -- sync (Transformation Phase 16) -------------------------------------
    def _sync_service(self) -> Any:
        from ..service import LocalService

        service = getattr(self, "_sync_service_cache", None)
        if service is None:
            service = LocalService(self.settings)
            self._sync_service_cache = service
        return service

    def sync_status_text(self) -> str:
        from ..diagnostics import sync_summary_line

        return sync_summary_line(self.settings, self.database)

    def sync_status(self) -> dict[str, Any]:
        return self._sync_service().sync_status()

    def sync_identity_text(self) -> str:
        payload = self._sync_service().sync_identity()
        return (
            f"device id {payload['device_id']}\nfingerprint {payload['fingerprint']}\n"
            f"sign (Ed25519) {payload['sign_public']}\n"
            f"encrypt (X25519) {payload['encrypt_public']}"
        )

    def sync_overview_text(self) -> str:
        service = self._sync_service()
        status = service.sync_status()
        lines = [
            (
                f"device {status['device']['device_id'][:12]} ({status['device_name']}) "
                f"fp={status['device']['fingerprint']}"
            ),
            (
                f"transport {status['transport']} dir={status['sync_dir'] or 'none'} "
                f"({status['sync_dir_state']}) policy={status['conflict_policy']}"
            ),
        ]
        peers = service.sync_peers()["peers"]
        lines.append(f"peers: {len(peers)}")
        for peer in peers:
            lines.append(
                f"  {peer['device_id'][:12]} {peer['name']} [{peer['status']}] "
                f"bundles={peer['bundle_count']}"
            )
        bundles = service.sync_bundles(limit=6)["bundles"]
        lines.append(f"bundles: {len(bundles)} recent")
        for entry in bundles:
            lines.append(
                f"  {entry['created_at'][:19]} {entry['direction']} {entry['status']} "
                f"seq={entry['seq']} records={entry['record_count']}"
            )
        pending = service.sync_requests(status="pending", limit=10)["requests"]
        lines.append(f"pending coordination requests: {len(pending)}")
        for item in pending:
            body = item.get("payload") or {}
            lines.append(
                f"  {item['id'][:12]} {item['kind']} from {item['device'][:10]} "
                f"{str(body.get('title', ''))[:44]}"
            )
        return "\n".join(lines)

    def sync_pair(self, name: str, sign_public: str, encrypt_public: str) -> str:
        payload = self._sync_service().sync_pair(
            name=name.strip(),
            sign_public=sign_public.strip(),
            encrypt_public=encrypt_public.strip(),
            confirm="PAIR",
        )
        return f"paired {payload['name']} ({payload['device_id'][:12]})"

    def sync_export(self) -> str:
        payload = self._sync_service().sync_export()
        return (
            f"exported {payload['records']} record(s), {payload['size_bytes']} bytes "
            f"for {len(payload['recipients'])} device(s)"
        )

    def sync_import(self, path: str) -> str:
        payload = self._sync_service().sync_import(path.strip())
        return (
            f"imported seq {payload['seq']}: applied={payload['applied']} "
            f"skipped={payload['skipped']} conflicts={len(payload['conflicts'])}"
        )

    def sync_tick(self) -> str:
        payload = self._sync_service().sync_tick()
        return (
            f"exported 1 bundle; imported={len(payload['imported'])} "
            f"refused={len(payload['refused'])}"
        )

    def sync_decide(self, request_id: str, accept: bool) -> str:
        payload = self._sync_service().sync_decide(request_id.strip(), accept=accept)
        return f"{payload['request']['id'][:12]} {payload['status']}: {payload['detail']}"

    # -- plugins (Transformation Phase 15) ----------------------------------
    def _plugin_service(self) -> Any:
        from ..service import LocalService

        service = getattr(self, "_plugin_service_cache", None)
        if service is None:
            service = LocalService(self.settings)
            self._plugin_service_cache = service
        return service

    def plugins_status_text(self) -> str:
        from ..diagnostics import plugins_summary_line

        return plugins_summary_line(self.settings, self.database)

    def plugins_status(self) -> dict[str, Any]:
        return self._plugin_service().plugin_status()

    def plugins_list_text(self) -> str:
        payload = self._plugin_service().plugins()
        if not payload["plugins"]:
            return "No plugins installed. Verify and install a signed .zip bundle."
        return "\n".join(
            f"{entry['id']:<26} {entry['version']:<9} {entry['status']:<9} "
            f"tools={len(entry['tools'])} skills={len(entry['skills'])} "
            f"key={entry['signature_key'] or 'unsigned'}"
            for entry in payload["plugins"]
        )

    def plugin_verify(self, bundle: str) -> str:
        payload = self._plugin_service().plugin_verify(bundle.strip())
        manifest = payload["manifest"]
        return (
            f"{manifest['name']} {manifest['version']}: files={payload['file_count']} "
            f"signature={payload['signature_status']} "
            f"key={payload['signature_key'] or '-'}"
        )

    def plugin_install(self, bundle: str) -> str:
        payload = self._plugin_service().plugin_install(bundle.strip())
        return (
            f"installed {payload['name']} {payload['version']} "
            f"({payload['signature_status']}); enable it to run tools"
        )

    def plugin_set_enabled(self, reference: str, enabled: bool) -> str:
        payload = self._plugin_service().plugin_set_enabled(
            reference.strip(), enabled=enabled
        )
        return f"{payload['id']} is now {payload['status']}"

    def plugin_uninstall(self, reference: str) -> str:
        payload = self._plugin_service().plugin_uninstall(reference.strip())
        return f"removed {payload['name']}; {payload['total']} plugin(s) remain"

    def plugin_rollback(self, reference: str, version: str | None = None) -> str:
        payload = self._plugin_service().plugin_rollback(
            reference.strip(), version=version
        )
        return f"{payload['id']} rolled back to {payload['version']}"

    def plugin_run(self, reference: str, tool: str, arguments: str = "{}") -> str:
        from ..errors import PluginError

        try:
            values = json.loads(arguments or "{}")
        except ValueError as exc:
            raise PluginError("Tool arguments must be a JSON object.") from exc
        if not isinstance(values, dict):
            raise PluginError("Tool arguments must be a JSON object.")
        plugin_id = reference.strip()
        payload = self._plugin_service().plugin_run_tool(
            plugin_id, tool.strip(), values=values, confirm=plugin_id
        )
        lines = [
            "{} in {}ms ({})".format(
                payload["status"],
                payload["duration_ms"],
                payload["sandbox"]["mechanism"],
            )
        ]
        if payload["detail"]:
            lines.append(f"  {payload['detail']}")
        if payload["result"] is not None:
            lines.append(f"  {json.dumps(payload['result'], default=str)[:2000]}")
        return "\n".join(lines)

    def plugin_skills(self, reference: str) -> str:
        payload = self._plugin_service().plugin_skills(reference.strip())
        if not payload["skills"]:
            return "This plugin ships no skills."
        return "\n\n".join(
            f"--- {item['path']} ---\n{item['text']}" for item in payload["skills"]
        )

    def plugin_runs_text(self, limit: int = 20) -> str:
        payload = self._plugin_service().plugin_runs(limit=limit)
        if not payload["runs"]:
            return "No plugin runs recorded yet."
        return "\n".join(
            f"{item['created_at'][:19]} {item['plugin_id']:<24} {item['tool']:<18} "
            f"{item['status']:<8} {item['duration_ms']}ms"
            + (f" - {item['detail']}" if item["detail"] else "")
            for item in payload["runs"]
        )

    # -- proactive (Transformation Phase 14) --------------------------------
    def _proactive_service(self) -> Any:
        from ..service import LocalService

        service = getattr(self, "_proactive_service_cache", None)
        if service is None:
            service = LocalService(self.settings)
            self._proactive_service_cache = service
        return service

    def proactive_status_text(self) -> str:
        from ..diagnostics import proactive_summary_line

        return proactive_summary_line(self.settings, self.database)

    def proactive_status(self) -> dict[str, Any]:
        return self._proactive_service().proactive_status()

    def proactive_reminders_text(self, limit: int = 20) -> str:
        rows = self._proactive_service().reminders(limit=limit)["reminders"]
        if not rows:
            return "No reminders stored yet."
        return "\n".join(
            f"{item['id'][:8]} {item['status']:<9} {item['due_at'][:19]} "
            f"{item['severity']:<8} {item['title'][:46]}"
            + (f" (x{item['fired_count']})" if item["fired_count"] else "")
            for item in rows
        )

    def proactive_notifications_text(self, limit: int = 15) -> str:
        rows = self._proactive_service().notifications(limit=limit)["notifications"]
        if not rows:
            return "No notifications recorded yet."
        return "\n".join(
            f"{item['created_at'][:19]} {item['kind']:<9} {item['status']:<9} "
            f"{item['title'][:44]}" + (f" - {item['detail']}" if item["detail"] else "")
            for item in rows
        )

    def add_reminder(self, title: str, due: str, severity: str = "normal") -> str:
        payload = self._proactive_service().add_reminder(
            title=title.strip(), due=due.strip(), severity=severity, source="gui"
        )
        return (
            f"reminder {payload['id'][:8]} '{payload['title']}' due "
            f"{payload['due_at'][:19]} ({payload['severity']})"
        )

    def resolve_reminder(self, reference: str) -> str:
        payload = self._proactive_service().resolve_reminder(reference.strip(), source="gui")
        return f"{payload['id'][:8]} {payload['status']}"

    def snooze_reminder(self, reference: str, for_spec: str = "+15m") -> str:
        payload = self._proactive_service().snooze_reminder(
            reference.strip(), for_spec=for_spec.strip() or "+15m"
        )
        return f"{payload['id'][:8]} snoozed to {payload['due_at'][:19]}"

    def delete_reminder(self, reference: str) -> str:
        payload = self._proactive_service().delete_reminder(reference.strip())
        return f"deleted reminder {payload['reminder_id'][:8]}"

    def proactive_tick(self) -> str:
        payload = self._proactive_service().proactive_tick(source="gui")
        lines = [f"delivered {payload['total']}, deferred {len(payload['deferred'])}"]
        for item in payload["fired"]:
            lines.append(f"  fired: {item['reminder']['title']}")
        for item in payload["deferred"]:
            lines.append(f"  deferred: {item['title']} - {item['reason']}")
        briefing = payload.get("briefing")
        if isinstance(briefing, dict) and "briefing" in briefing:
            lines.append(f"  briefing: {briefing['briefing']['item_count']} item(s)")
        return "\n".join(lines)

    def run_briefing(self, force: bool = True) -> str:
        payload = self._proactive_service().run_briefing(force=force, source="gui")
        briefing = payload["briefing"]
        body = [f"briefing {briefing['local_date']} ({briefing['item_count']} item(s))"]
        body += [
            f"  {name}: {section.get('summary', '')}"
            for name, section in payload["sections"].items()
        ]
        return "\n".join(body)

    def notify_user(self, title: str, body: str, severity: str = "normal") -> str:
        payload = self._proactive_service().notify_user(
            title=title.strip(),
            body=body.strip(),
            kind="manual",
            severity=severity,
            source="gui",
        )
        return f"{payload['status']} [{payload['channels']}] {payload['title']}"

    # -- connectors (Transformation Phase 13) --------------------------------
    def _connector_service(self) -> Any:
        from ..service import LocalService

        service = getattr(self, "_connectors_service_cache", None)
        if service is None:
            service = LocalService(self.settings)
            self._connectors_service_cache = service
        return service

    def connectors_status_text(self) -> str:
        from ..diagnostics import connectors_summary_line

        return connectors_summary_line(self.settings, self.database)

    def connectors_status(self, *, probe: bool = False) -> dict[str, Any]:
        return self._connector_service().connector_status(probe=probe)

    def connectors_events_text(self, limit: int = 15) -> str:
        rows = self._connector_service().connector_events(limit=limit)["events"]
        if not rows:
            return "No connector events recorded yet."
        return "\n".join(
            f"{item['received_at'][:19]} {item['connector']:<9} {item['kind']:<12} "
            f"{item['summary'][:60]}"
            for item in rows
        )

    def connectors_deliveries_text(self, limit: int = 10) -> str:
        rows = self._connector_service().connector_deliveries(limit=limit)["deliveries"]
        if not rows:
            return "No connector deliveries recorded yet."
        return "\n".join(
            f"{item['created_at'][:19]} {item['connector']:<9} {item['status']:<7} "
            f"{item['payload_bytes']}B {item['target'][:40]}"
            for item in rows
        )

    def add_calendar_event(self, summary: str, start: str) -> str:
        payload = self._connector_service().connector_calendar_add(
            summary=summary.strip(), start=start.strip()
        )
        event = payload["event"]
        return f"added '{event['summary']}' at {event['start']} (uid {event['uid'][:16]})"

    def connector_drive_sync(self) -> str:
        payload = self._connector_service().connector_drive_sync()
        return (
            f"{payload['files']} file(s) indexed, {payload['total_bytes']} bytes; "
            f"added={payload['added']} modified={payload['modified']} "
            f"removed={payload['removed']} skipped={payload['skipped']}"
            + (" (truncated)" if payload["truncated"] else "")
        )

    def connector_mail_inbox(self, limit: int = 5) -> str:
        payload = self._connector_service().connector_mail_inbox(limit=limit)
        messages = payload["messages"]
        if not messages:
            return "The mailbox returned no message headers."
        return "\n".join(
            f"{message['date'][:28]:<28} {message['from'][:30]:<30} "
            f"{message['subject'][:50]}"
            for message in messages
        )

    def connector_chat_send(self, text: str) -> str:
        payload = self._connector_service().connector_chat_send(text)
        result = payload["result"]
        return (
            f"{'delivered' if result['ok'] else 'failed'} "
            f"HTTP {result['status_code']} {result['payload_bytes']}B "
            f"{result['duration_ms']}ms -> {result['target']}"
        )

    def connector_webhook_receive(self, body_file: Any, timestamp: str, signature: str) -> str:
        from pathlib import Path as _Path

        body = _Path(body_file).expanduser().read_bytes()
        payload = self._connector_service().connector_webhook_receive(
            body, timestamp=timestamp.strip(), signature=signature.strip()
        )
        receipt = payload["receipt"]
        if not receipt["accepted"]:
            return f"refused: {receipt['reason']}"
        return (
            f"accepted {receipt['kind']} event "
            f"{receipt['external_id'] or '(no id)'}: {receipt['summary'][:60]}"
        )

    def connectors_calendar_text(self, days: int = 7) -> str:
        payload = self._connector_service().connector_calendar_events(days_ahead=days)
        events = payload["events"]
        if not events:
            return f"No events in the next {payload['days_ahead']} day(s)."
        return "\n".join(
            f"{event['start'][:16]} -> {event['end'][11:16]} {event['summary'][:60]}"
            for event in events
        )

    def flow_tick(self) -> str:
        result = self._flow_service().flow_tick(limit=10)
        return (
            f"fired {result['total']} trigger(s); "
            f"skipped {len(result['skipped'])}"
        )

    def flow_export_text(self, reference: str) -> str:
        import json as _json

        return _json.dumps(
            self._flow_service().export_flow(reference.strip()), indent=2, sort_keys=True
        )

    def flow_import(self, document: str) -> str:
        import json as _json

        try:
            payload = _json.loads(document)
        except ValueError as exc:
            from ..errors import FlowError

            raise FlowError(f"That is not valid JSON: {exc}") from exc
        if isinstance(payload, dict) and "flow" in payload:
            result = self._flow_service().import_flow(payload, overwrite=True)
            return (
                f"imported {result['flow']['name']} v{result['flow']['version']} "
                f"with {len(result['triggers'])} trigger(s)"
            )
        return self.flow_save(document)

    def flow_runs(self, reference: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return self._flow_service().flow_runs(reference or None, limit=limit)["runs"]

    def flow_add_trigger(
        self, reference: str, *, kind: str, interval_seconds: int | None = None,
        event_type: str = "",
    ) -> str:
        payload = self._flow_service().flow_add_trigger(
            reference.strip(),
            {"kind": kind, "interval_seconds": interval_seconds, "event_type": event_type},
        )
        return f"trigger {payload['id'][:8]} {payload['kind']} added (next {payload['next_run_at'] or '-'})"

    def _flow_document(self, document: str) -> dict[str, Any]:
        import json as _json

        from ..errors import FlowError

        text = (document or "").strip()
        if not text:
            raise FlowError("Paste a flow JSON document first.")
        try:
            payload = _json.loads(text)
        except ValueError as exc:
            raise FlowError(f"That is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise FlowError("The flow document must be a JSON object.")
        return payload

    # -- automation ledger + local vision (Transformation Phase 11) ----------
    def _automation_recorder(self, payload: dict[str, Any]) -> None:
        try:
            self._automation_repository().record(**payload)
        except Exception:  # noqa: BLE001 - the ledger must never break an action
            return

    def _automation_repository(self) -> Any:
        from ..storage.repositories import AutomationRepository

        return AutomationRepository(self.database)

    def automation_capabilities(self) -> dict[str, Any]:
        from ..browser_cdp import probe_browser
        from ..desktop_automation import probe_desktop_automation
        from ..tools.builtins import build_default_registry

        version = probe_browser(port=self.settings.browser_port)
        registry = build_default_registry()
        return {
            "browser": {
                "enabled": bool(self.settings.allow_browser),
                "port": self.settings.browser_port,
                "connected": version is not None,
                "browser": str(version.get("Browser") or "") if version else "",
            },
            "desktop": {
                "enabled": bool(self.settings.allow_desktop_automation),
                **probe_desktop_automation(),
            },
            "vision": {
                "local_analysis": True,
                "provider_vision_enabled": bool(self.settings.allow_vision),
            },
            "tools": [
                tool.spec.id
                for tool in registry.list_tools()
                if tool.spec.id.startswith(("browser.", "desktop.", "vision."))
            ],
        }

    def automation_capabilities_text(self) -> str:
        caps = self.automation_capabilities()
        summary = self._automation_repository().summary()
        by_kind = ", ".join(
            f"{kind}={count}" for kind, count in sorted(summary["by_kind"].items())
        ) or "none"
        return (
            f"browser={'on' if caps['browser']['enabled'] else 'off'}"
            f" ({caps['browser']['browser'] or 'no debug browser'}) | "
            f"desktop={'on' if caps['desktop']['enabled'] else 'off'}"
            f" | provider vision={'on' if caps['vision']['provider_vision_enabled'] else 'off'}"
            f" | local vision=available | tools={len(caps['tools'])} | "
            f"ledger: total={summary['total']} ({by_kind}) refused={summary['refused']}"
        )

    def automation_runs(self, limit: int = 25) -> list[dict[str, Any]]:
        runs = self._automation_repository().list_runs(limit=limit)
        return [
            {
                "id": run.id,
                "kind": run.kind,
                "tool_id": run.tool_id,
                "action": run.action,
                "target": run.target,
                "status": run.status,
                "allowed": run.allowed,
                "detail": run.detail,
                "created_at": run.created_at.isoformat(),
            }
            for run in runs
        ]

    def automation_screen_text(self, path: str, grid: int = 4) -> str:

        arguments: dict[str, Any] = {"grid": max(1, min(int(grid), 12))}
        if path.strip():
            arguments["path"] = path.strip()
        result = self._invoke_builtin("vision.screen", arguments)
        if not result.success:
            raise PcAgentError(result.error or "Screen analysis failed.")
        output = result.output or {}
        return (
            f"{output.get('width')}x{output.get('height')} from {output.get('source')} "
            f"(brightness {output.get('brightness')}, contrast {output.get('contrast')}, "
            f"text_likely={output.get('text_likely')})\n{output.get('region_hint')}"
        )

    def automation_locate_text(
        self, screen_path: str, template_path: str, threshold: float = 0.9
    ) -> str:
        result = self._invoke_builtin(
            "vision.locate",
            {
                "screen_path": screen_path.strip(),
                "template_path": template_path.strip(),
                "threshold": float(threshold),
            },
        )
        if not result.success:
            raise PcAgentError(result.error or "Template search failed.")
        output = result.output or {}
        if not output.get("found"):
            return (
                f"not found (best score {output.get('score')} below "
                f"{output.get('threshold')})"
            )
        match = output["matches"][0]
        return (
            f"found at ({match['x']}, {match['y']}) "
            f"{match['width']}x{match['height']} score={output['score']}"
        )

    # -- voice pipeline (Transformation Phase 10) ----------------------------
    def _voice_service(self) -> Any:
        from ..voice import VoiceService

        return VoiceService(self.settings, self.database)

    def voice_status(self) -> dict[str, Any]:
        return self._voice_service().status().to_dict()

    def voice_status_text(self) -> str:
        return self._voice_service().status().summary_line()

    def voice_devices_text(self) -> str:
        devices = self._voice_service().devices()
        lines: list[str] = []
        for kind in ("input", "output"):
            block = devices[kind]
            lines.append(f"{kind} ({block['method']}): {block['count']}")
            if block["reason"]:
                lines.append(f"  {block['reason']}")
            lines.extend(
                f"  #{device['index']} {device['name']}"
                + (" (default)" if device["is_default"] else "")
                for device in block["devices"]
            )
        return "\n".join(lines)

    def voice_set_enabled(self, enabled: bool) -> str:
        service = self._voice_service()
        payload = service.enable() if enabled else service.disable()
        return f"microphone {payload['indicator']} (state={payload['state']})"

    def voice_set_wake_word(self, enabled: bool) -> str:
        payload = self._voice_service().set_wake_word(enabled)
        return (
            "wake word " + ("enabled" if payload["wake_word_enabled"] else "disabled")
            + f" (indicator={payload['indicator']})"
        )

    def voice_capture_start(self) -> str:
        payload = self._voice_service().start_capture()
        return (
            f"recording ({payload['mode']}; backend={payload['backend']}; "
            f"max {payload['max_seconds']:g}s)"
        )

    def voice_capture_stop(self) -> str:
        payload = self._voice_service().stop_capture()
        if payload["text"]:
            return (
                f"transcript ({payload['engine']}): {payload['text']}\n"
                f"audio {'retained' if payload['audio_retained'] else 'discarded'}"
            )
        return f"no transcript: {payload['error']}"

    def voice_capture_cancel(self) -> str:
        return "capture cancelled; microphone idle"

    def voice_speak(self, text: str) -> str:
        payload = self._voice_service().speak(text)
        if payload["ok"]:
            return f"spoke {payload['characters']} characters via {payload['engine']}"
        return f"speech failed: {payload['error']}"

    def voice_privacy_text(self) -> str:
        payload = self._voice_service().privacy()
        return (
            f"microphone {payload['indicator']}; retention={payload['retention']}; "
            f"transcripts={'stored' if payload['store_transcripts'] else 'not stored'}; "
            f"cloud_stt={'allowed' if payload['cloud_stt_allowed'] else 'off'}; "
            f"wake_word={'on' if payload['wake_word_enabled'] else 'off'}; "
            f"max={payload['max_seconds']:g}s"
        )

    def voice_purge_audio(self) -> str:
        payload = self._voice_service().purge_audio()
        return f"purged {payload['removed']} recording(s)"

    def voice_sessions_text(self, limit: int = 10) -> str:
        rows = self._voice_service().sessions(limit)
        if not rows:
            return "No voice sessions yet."
        return "\n".join(
            f"#{row['id']} {row['mode']} {row['audio_seconds']}s "
            f"{'kept' if row['audio_retained'] else 'discarded'} chars={row['transcript_chars']}"
            for row in rows
        )

    # -- knowledge base & Memory v2 (Transformation Phase 8) ------------------
    def _knowledge_service(self) -> Any:
        from ..knowledge import KnowledgeService

        return KnowledgeService(self.settings, self.database, router=self._router)

    def knowledge_documents(self) -> list[dict[str, Any]]:
        return list(self._knowledge_service().list_documents()["documents"])

    def knowledge_ingest(self, raw_path: str) -> dict[str, Any]:
        candidate = Path(raw_path.strip()).expanduser()
        if not candidate.is_absolute():
            candidate = self.settings.workspace_dir / candidate
        target = candidate.resolve()
        workspace = self.settings.workspace_dir.resolve()
        if not target.is_relative_to(workspace):
            raise PcAgentError(
                "Knowledge ingestion from the GUI is limited to files inside "
                "the workspace."
            )
        try:
            return self._knowledge_service().ingest_file(target)
        except KnowledgeError as exc:
            raise PcAgentError(str(exc)) from exc

    def knowledge_remove(self, document_id: int) -> dict[str, Any]:
        try:
            return self._knowledge_service().remove_document(int(document_id))
        except KnowledgeError as exc:
            raise PcAgentError(str(exc)) from exc

    def knowledge_search_text(self, query: str) -> str:
        try:
            payload = self._knowledge_service().search(query, limit=8)
        except KnowledgeError as exc:
            raise PcAgentError(str(exc)) from exc
        lines = [
            (
                f"Search ({payload['mode']} via {payload['embedding_model']}): "
                f"{payload['query']}"
            )
        ]
        if not payload["results"]:
            lines.append("No matches. Ingest a document or save a note first.")
        for index, item in enumerate(payload["results"], start=1):
            lines.append(
                f"[S{index}] {item['source']} · {item['title']} "
                f"(score {item['score']})\n{item['snippet']}"
            )
        return "\n\n".join(lines)

    def knowledge_ask_text(self, question: str) -> str:
        self._require_license(FEATURE_PROVIDERS)
        try:
            payload = self._knowledge_service().ask(question)
        except KnowledgeError as exc:
            raise PcAgentError(str(exc)) from exc
        if payload.get("answer") is None:
            return str(payload.get("note") or "No sources matched.")
        sources = ", ".join(
            f"[S{index}] {item['title']}"
            for index, item in enumerate(payload["sources"], start=1)
        )
        return (
            f"{payload['answer']}\n\nSources: {sources}\n"
            f"Answered by {payload.get('provider')}/{payload.get('model')} "
            f"grounded only in the sources above."
        )

    def knowledge_stats_text(self) -> str:
        stats = self._knowledge_service().stats()
        memories = stats["memories"]
        return (
            f"documents={stats['documents']} chunks={stats['chunks']} "
            f"embeddings={stats['active_embedding_model']} "
            f"notes={memories.get('note', 0)} embedded={stats['embedded_notes']}"
        )

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

    def git_status(self, repository: str = ".") -> ToolResult:
        return self._invoke_builtin("git.status", {"repository": repository})

    def git_log(self, repository: str = ".", max_count: int = 10) -> ToolResult:
        return self._invoke_builtin(
            "git.log", {"repository": repository, "max_count": max_count}
        )

    def _browser_connected_text(self) -> str:
        if not self.settings.allow_browser:
            return ""
        info = probe_browser(port=self.settings.browser_port)
        if not info:
            return ""
        return f"connected ({info.get('Browser') or 'Chromium'})"

    def _desktop_automation_runtime_text(self) -> str:
        if not self.settings.allow_desktop_automation:
            return ""
        probe = probe_desktop_automation()
        detail = str(probe.get("detail") or "")
        return detail if probe.get("available") else f"unavailable ({detail})"

    def browser_status(self) -> ToolResult:
        return self._invoke_builtin("browser.status", {})

    def browser_snapshot(self) -> ToolResult:
        return self._invoke_builtin("browser.snapshot", {})

    def desktop_windows(self, *, include_untitled: bool = False) -> ToolResult:
        return self._invoke_builtin(
            "desktop.windows",
            {"max_windows": 100, "include_untitled": include_untitled},
        )

    def desktop_window_inspect(
        self, title_contains: str, *, max_elements: int = 200
    ) -> ToolResult:
        return self._invoke_builtin(
            "desktop.window_inspect",
            {
                "window_title_contains": _require(title_contains, "window title"),
                "max_elements": max_elements,
            },
        )

    def desktop_screenshot(
        self, output_path: str, *, title_contains: str = ""
    ) -> ToolResult:
        arguments: dict[str, Any] = {
            "output_path": _require(output_path, "screenshot output path")
        }
        if title_contains.strip():
            arguments["window_title_contains"] = title_contains.strip()
        return self._invoke_builtin("desktop.screenshot", arguments)

    def image_inspect(self, path: str) -> ToolResult:
        return self._invoke_builtin(
            "image.inspect",
            {"path": _require(path, "image file"), "include_color_stats": True},
        )

    def image_convert(
        self, source: str, output: str, *, quality: int = 90
    ) -> ToolResult:
        return self._invoke_builtin(
            "image.convert",
            {
                "source": _require(source, "source image"),
                "output": _require(output, "image output"),
                "quality": quality,
            },
        )

    def image_transform(
        self,
        source: str,
        output: str,
        *,
        width: int | None = None,
        height: int | None = None,
        max_dimension: int | None = None,
        grayscale: bool = False,
        quality: int = 90,
    ) -> ToolResult:
        arguments: dict[str, Any] = {
            "source": _require(source, "source image"),
            "output": _require(output, "image output"),
            "quality": quality,
        }
        if width is not None:
            arguments["width"] = width
        if height is not None:
            arguments["height"] = height
        if max_dimension is not None:
            arguments["max_dimension"] = max_dimension
        if grayscale:
            arguments["grayscale"] = True
        return self._invoke_builtin("image.transform", arguments)

    def image_compare(self, first: str, second: str) -> ToolResult:
        return self._invoke_builtin(
            "image.compare",
            {
                "first": _require(first, "first image"),
                "second": _require(second, "second image"),
            },
        )

    def image_analyze(
        self, path: str, question: str, *, max_output_tokens: int = 1024
    ) -> ToolResult:
        return self._invoke_builtin(
            "image.analyze",
            {
                "path": _require(path, "image file"),
                "question": _require(question, "vision question"),
                "max_output_tokens": max_output_tokens,
            },
        )

    def list_workflows(self) -> tuple[Any, ...]:
        return self._orchestrator.workflow_records()

    def run_workflow(
        self, workflow_key: str, options: TaskRunOptions | None = None
    ) -> ExecutionReport:
        run_options = options or TaskRunOptions()
        self._require_license(FEATURE_CORE_TASKS)
        record = self._orchestrator.resolve_workflow(workflow_key)
        instruction = record.content.get("instruction")
        task = self._tasks.create(
            Task(
                instruction=instruction
                if isinstance(instruction, str) and instruction
                else f"Reuse verified workflow {record.key}"
            )
        )
        self._active_task_id = task.id
        self._cancel_event.clear()
        try:
            return self._orchestrator.run_workflow(
                task.id,
                record.key,
                approval_callback=self._approval_callback,
                max_recovery_attempts=run_options.max_recovery_attempts,
                cancellation_requested=self._cancel_event.is_set,
                budget_options=run_options.planner_options(),
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

    def pdf_extract_pages(
        self, path: str, pages: str, output_path: str
    ) -> ToolResult:
        return self._invoke_builtin(
            "pdf.extract_pages",
            {
                "path": _require(path, "PDF file"),
                "pages": _require(pages, "page selection"),
                "output_path": _require(output_path, "output PDF"),
            },
        )

    def pdf_split(self, path: str, output_folder: str) -> ToolResult:
        return self._invoke_builtin(
            "pdf.split",
            {
                "path": _require(path, "PDF file"),
                "output_folder": _require(output_folder, "output folder"),
            },
        )

    def pdf_extract_text(
        self,
        path: str,
        output_path: str,
        *,
        pages: str = "",
        output_format: str | None = None,
    ) -> ToolResult:
        arguments: dict[str, Any] = {
            "path": _require(path, "PDF file"),
            "output_path": _require(output_path, "output text file"),
        }
        if pages.strip():
            arguments["pages"] = pages.strip()
        if output_format:
            arguments["format"] = output_format
        return self._invoke_builtin("pdf.extract_text", arguments)



    def pdf_extract_tables(
        self,
        path: str,
        output_path: str,
        *,
        pages: str = "",
        min_rows: int = 2,
        min_columns: int = 2,
        output_format: str | None = None,
    ) -> ToolResult:
        arguments: dict[str, Any] = {
            "path": _require(path, "PDF file"),
            "output_path": _require(output_path, "table output file"),
            "min_rows": min_rows,
            "min_columns": min_columns,
        }
        if pages.strip():
            arguments["pages"] = pages.strip()
        if output_format:
            arguments["format"] = output_format
        return self._invoke_builtin("pdf.extract_tables", arguments)

    def pdf_inspect_form(self, path: str) -> ToolResult:
        return self._invoke_builtin(
            "pdf.inspect_form", {"path": _require(path, "PDF file")}
        )

    def pdf_fill_form(
        self,
        path: str,
        output_path: str,
        values: dict[str, Any],
    ) -> ToolResult:
        if not values:
            raise PcAgentError("Enter at least one PDF form Name=Value line.")
        return self._invoke_builtin(
            "pdf.fill_form",
            {
                "path": _require(path, "PDF file"),
                "output_path": _require(output_path, "filled PDF output file"),
                "values": [
                    {"name": name, "value": value}
                    for name, value in values.items()
                ],
            },
        )

    def pdf_security(
        self, path: str, *, password: str | None = None
    ) -> ToolResult:
        """Direct GUI-only encryption inspection; not a registered tool."""

        self._require_license(FEATURE_DOCUMENTS)
        source = self._workspace_pdf_path(
            _require(path, "PDF file"), must_exist=True
        )
        try:
            info = inspect_pdf_security(source, password=password)
        except PcAgentError as exc:
            return ToolResult(
                tool_id="direct.pdf_security",
                version="1.0.0",
                success=False,
                error=str(exc),
            )
        output = {
            "path": self._relative_workspace_path(source),
            **info.summary(),
        }
        success = not (
            info.encrypted
            and password is not None
            and info.password_accepted is not True
        )
        return ToolResult(
            tool_id="direct.pdf_security",
            version="1.0.0",
            success=success,
            output=output,
            error=None if success else "Incorrect PDF password.",
        )

    def pdf_protect(
        self,
        path: str,
        output_path: str,
        *,
        user_password: str,
        owner_password: str,
        current_password: str | None = None,
        permissions: Sequence[str] = DEFAULT_PDF_PERMISSIONS,
    ) -> ToolResult:
        """Direct GUI-only AES-256 protection; passwords stay out of results."""

        self._require_license(FEATURE_DOCUMENTS)
        source = self._workspace_pdf_path(
            _require(path, "PDF file"), must_exist=True
        )
        output = self._workspace_pdf_path(
            _require(output_path, "protected PDF output file"), must_exist=False
        )
        try:
            result = protect_pdf(
                source,
                output,
                user_password=user_password,
                owner_password=owner_password,
                current_password=current_password,
                permissions=permissions,
            )
        except PcAgentError as exc:
            return ToolResult(
                tool_id="direct.pdf_protect",
                version="1.0.0",
                success=False,
                error=str(exc),
            )
        return ToolResult(
            tool_id="direct.pdf_protect",
            version="1.0.0",
            success=True,
            output={
                "path": self._relative_workspace_path(source),
                "output_path": self._relative_workspace_path(output),
                **result.summary(),
            },
        )

    def pdf_unprotect(
        self, path: str, output_path: str, *, password: str
    ) -> ToolResult:
        """Direct GUI-only password removal into a new verified PDF."""

        self._require_license(FEATURE_DOCUMENTS)
        source = self._workspace_pdf_path(
            _require(path, "PDF file"), must_exist=True
        )
        output = self._workspace_pdf_path(
            _require(output_path, "unprotected PDF output file"),
            must_exist=False,
        )
        try:
            result = unprotect_pdf(source, output, password=password)
        except PcAgentError as exc:
            return ToolResult(
                tool_id="direct.pdf_unprotect",
                version="1.0.0",
                success=False,
                error=str(exc),
            )
        return ToolResult(
            tool_id="direct.pdf_unprotect",
            version="1.0.0",
            success=True,
            output={
                "path": self._relative_workspace_path(source),
                "output_path": self._relative_workspace_path(output),
                **result.summary(),
            },
        )

    def pdf_sign(
        self,
        path: str,
        output_path: str,
        pkcs12_path: str,
        password: str,
        *,
        field_name: str = "",
        create_field: bool = True,
        page: int = 1,
        rectangle: tuple[float, float, float, float] | None = (
            36.0,
            36.0,
            200.0,
            40.0,
        ),
        reason: str = "",
        location: str = "",
        contact_info: str = "",
        certify_permission: int | None = None,
        hash_algorithm: str = "sha256",
        signature_profile: str = "adbe",
        timestamp_url: str = "",
        timestamp_hash_algorithm: str = "sha256",
        timestamp_timeout_seconds: int = 15,
        timestamp_allow_private_network: bool = False,
        timestamp_opener: object | None = None,
    ) -> ToolResult:
        """Direct GUI-only signing; private key/password never enter a task."""

        self._require_license(FEATURE_DOCUMENTS)
        source = self._workspace_pdf_path(
            _require(path, "PDF file"), must_exist=True
        )
        output = self._workspace_pdf_path(
            _require(output_path, "signed PDF output file"),
            must_exist=False,
        )
        identity = self._workspace_identity_path(
            _require(pkcs12_path, "PKCS#12 identity file")
        )
        try:
            result = sign_pdf(
                source,
                output,
                pkcs12_path=identity,
                pkcs12_password=password,
                field_name=field_name.strip() or None,
                create_field=create_field,
                page=page,
                rectangle=rectangle,
                reason=reason.strip() or None,
                location=location.strip() or None,
                contact_info=contact_info.strip() or None,
                certify_permission=certify_permission,
                hash_algorithm=hash_algorithm,
                signature_profile=signature_profile,
                timestamp_url=timestamp_url.strip() or None,
                timestamp_hash_algorithm=timestamp_hash_algorithm,
                timestamp_timeout_seconds=timestamp_timeout_seconds,
                timestamp_allow_private_network=(
                    timestamp_allow_private_network
                ),
                timestamp_opener=timestamp_opener,
            )
        except PcAgentError as exc:
            return ToolResult(
                tool_id="direct.pdf_sign",
                version="1.0.0",
                success=False,
                error=str(exc),
            )
        return ToolResult(
            tool_id="direct.pdf_sign",
            version="1.0.0",
            success=True,
            output={
                "path": self._relative_workspace_path(source),
                "output_path": self._relative_workspace_path(output),
                **result.summary(),
            },
        )

    def pdf_document_timestamp(
        self,
        path: str,
        output_path: str,
        timestamp_url: str,
        *,
        field_name: str = "",
        create_field: bool = True,
        page: int = 1,
        hash_algorithm: str = "sha256",
        timeout_seconds: int = 15,
        allow_private_network: bool = False,
        timestamp_opener: object | None = None,
    ) -> ToolResult:
        """Direct GUI-only DocTimeStamp append; no signing identity is used."""

        self._require_license(FEATURE_DOCUMENTS)
        source = self._workspace_pdf_path(
            _require(path, "PDF file"), must_exist=True
        )
        output = self._workspace_pdf_path(
            _require(output_path, "timestamped PDF output file"),
            must_exist=False,
        )
        try:
            result = add_pdf_document_timestamp(
                source,
                output,
                timestamp_url=_require(
                    timestamp_url, "RFC 3161 TSA HTTPS URL"
                ),
                field_name=field_name.strip() or None,
                create_field=create_field,
                page=page,
                hash_algorithm=hash_algorithm,
                timeout_seconds=timeout_seconds,
                allow_private_network=allow_private_network,
                timestamp_opener=timestamp_opener,
            )
        except PcAgentError as exc:
            return ToolResult(
                tool_id="direct.pdf_document_timestamp",
                version="1.0.0",
                success=False,
                error=str(exc),
            )
        return ToolResult(
            tool_id="direct.pdf_document_timestamp",
            version="1.0.0",
            success=True,
            output={
                "path": self._relative_workspace_path(source),
                "output_path": self._relative_workspace_path(output),
                **result.summary(),
            },
        )

    def pdf_inspect_ltv(self, path: str) -> ToolResult:
        """Direct GUI-only DSS inspection; no trust decision is made."""

        self._require_license(FEATURE_DOCUMENTS)
        source = self._workspace_pdf_path(
            _require(path, "PDF file"), must_exist=True
        )
        try:
            inspection = inspect_pdf_dss(source)
        except PcAgentError as exc:
            return ToolResult(
                tool_id="direct.pdf_inspect_ltv",
                version="1.0.0",
                success=False,
                error=str(exc),
            )
        return ToolResult(
            tool_id="direct.pdf_inspect_ltv",
            version="1.0.0",
            success=True,
            output={
                "path": self._relative_workspace_path(source),
                **inspection.summary(),
            },
        )

    def pdf_add_ltv(
        self,
        path: str,
        output_path: str,
        certificate_paths: str = "",
        crl_paths: str = "",
        ocsp_response_paths: str = "",
        *,
        timestamp_url: str = "",
        timestamp_field_name: str = "",
        timestamp_create_field: bool = True,
        timestamp_page: int = 1,
        timestamp_hash_algorithm: str = "sha256",
        timestamp_timeout_seconds: int = 15,
        timestamp_allow_private_network: bool = False,
        timestamp_opener: object | None = None,
    ) -> ToolResult:
        """Direct GUI-only DSS embedding from explicit local material."""

        self._require_license(FEATURE_DOCUMENTS)
        source = self._workspace_pdf_path(
            _require(path, "PDF file"), must_exist=True
        )
        output = self._workspace_pdf_path(
            _require(output_path, "LTV PDF output file"),
            must_exist=False,
        )
        certificates = [
            self._workspace_validation_path(
                value,
                label="LTV certificate",
                suffixes={".cer", ".crt", ".der", ".pem"},
            )
            for value in parse_line_list(certificate_paths)
        ]
        crls = [
            self._workspace_validation_path(
                value,
                label="CRL",
                suffixes={".crl", ".der", ".pem"},
            )
            for value in parse_line_list(crl_paths)
        ]
        ocsps = [
            self._workspace_validation_path(
                value,
                label="OCSP response",
                suffixes={".der", ".ocsp", ".pem"},
            )
            for value in parse_line_list(ocsp_response_paths)
        ]
        try:
            result = add_pdf_ltv_material(
                source,
                output,
                certificate_paths=certificates,
                crl_paths=crls,
                ocsp_response_paths=ocsps,
                timestamp_url=timestamp_url.strip() or None,
                timestamp_field_name=timestamp_field_name.strip() or None,
                timestamp_create_field=timestamp_create_field,
                timestamp_page=timestamp_page,
                timestamp_hash_algorithm=timestamp_hash_algorithm,
                timestamp_timeout_seconds=timestamp_timeout_seconds,
                timestamp_allow_private_network=(
                    timestamp_allow_private_network
                ),
                timestamp_opener=timestamp_opener,
            )
        except PcAgentError as exc:
            return ToolResult(
                tool_id="direct.pdf_add_ltv",
                version="1.0.0",
                success=False,
                error=str(exc),
            )
        return ToolResult(
            tool_id="direct.pdf_add_ltv",
            version="1.0.0",
            success=True,
            output={
                "path": self._relative_workspace_path(source),
                "output_path": self._relative_workspace_path(output),
                **result.summary(),
            },
        )

    def pdf_validate_signatures(
        self,
        path: str,
        trust_anchor_paths: str = "",
        crl_paths: str = "",
        timestamp_trust_anchor_paths: str = "",
        *,
        accept_revocation_unknown: bool = False,
        online_revocation: bool = False,
        revocation_timeout_seconds: int = 10,
        revocation_allow_private_network: bool = False,
        required_policy_oids: str = "",
        timestamp_required_policy_oids: str = "",
        explicit_policy_required: bool = False,
        inhibit_any_policy: bool = False,
        inhibit_policy_mapping: bool = False,
        system_trust_anchors: bool = False,
        timestamp_system_trust_anchors: bool = False,
    ) -> ToolResult:
        """Direct GUI-only trust validation; offline unless explicitly enabled."""

        self._require_license(FEATURE_DOCUMENTS)
        source = self._workspace_pdf_path(
            _require(path, "PDF file"), must_exist=True
        )
        anchors = [
            self._workspace_validation_path(
                value,
                label="trust-anchor",
                suffixes={".cer", ".crt", ".der", ".pem"},
            )
            for value in parse_line_list(trust_anchor_paths)
        ]
        timestamp_anchors = [
            self._workspace_validation_path(
                value,
                label="timestamp-trust-anchor",
                suffixes={".cer", ".crt", ".der", ".pem"},
            )
            for value in parse_line_list(timestamp_trust_anchor_paths)
        ]
        crls = [
            self._workspace_validation_path(
                value,
                label="CRL",
                suffixes={".crl", ".der", ".pem"},
            )
            for value in parse_line_list(crl_paths)
        ]
        try:
            validation = validate_pdf_signature_trust(
                source,
                trust_anchor_paths=anchors,
                crl_paths=crls,
                timestamp_trust_anchor_paths=timestamp_anchors,
                online_revocation=online_revocation,
                revocation_timeout_seconds=revocation_timeout_seconds,
                revocation_allow_private_network=revocation_allow_private_network,
                required_policy_oids=required_policy_oids,
                timestamp_required_policy_oids=timestamp_required_policy_oids,
                explicit_policy_required=explicit_policy_required,
                inhibit_any_policy=inhibit_any_policy,
                inhibit_policy_mapping=inhibit_policy_mapping,
                system_trust_anchors=system_trust_anchors,
                timestamp_system_trust_anchors=timestamp_system_trust_anchors,
            )
        except PcAgentError as exc:
            return ToolResult(
                tool_id="direct.pdf_validate_signatures",
                version="1.0.0",
                success=False,
                error=str(exc),
            )
        signed = [
            item
            for item in validation.signatures
            if item.overall_status != "unsigned"
        ]
        accepted = {"trusted_valid"}
        if accept_revocation_unknown:
            accepted.add("trusted_chain_revocation_unknown")
        success = bool(signed) and all(
            item.overall_status in accepted for item in signed
        )
        return ToolResult(
            tool_id="direct.pdf_validate_signatures",
            version="1.0.0",
            success=success,
            output={
                "path": self._relative_workspace_path(source),
                **validation.summary(),
            },
            error=(
                None
                if success
                else "One or more signed signatures did not reach trusted_valid."
            ),
        )

    def pdf_inspect_signatures(self, path: str) -> ToolResult:
        """Direct GUI-only signature inspection; not a registered tool."""

        self._require_license(FEATURE_DOCUMENTS)
        source = self._workspace_pdf_path(
            _require(path, "PDF file"), must_exist=True
        )
        try:
            inspection = inspect_pdf_signatures(source)
        except PcAgentError as exc:
            return ToolResult(
                tool_id="direct.pdf_inspect_signatures",
                version="1.0.0",
                success=False,
                error=str(exc),
            )
        success = inspection.invalid_count == 0
        return ToolResult(
            tool_id="direct.pdf_inspect_signatures",
            version="1.0.0",
            success=success,
            output={
                "path": self._relative_workspace_path(source),
                **inspection.summary(),
            },
            error=(
                None
                if success
                else "One or more PDF signatures are invalid or malformed."
            ),
        )

    def pdf_ocr(
        self,
        path: str,
        output_path: str,
        *,
        pages: str = "",
        dpi: int = 200,
        language: str = "",
        skip_text_pages: bool = True,
    ) -> ToolResult:
        arguments: dict[str, Any] = {
            "path": _require(path, "PDF file"),
            "output_path": _require(output_path, "OCR output file"),
            "dpi": dpi,
            "skip_text_pages": skip_text_pages,
        }
        if pages.strip():
            arguments["pages"] = pages.strip()
        if language.strip():
            arguments["language"] = language.strip()
        return self._invoke_builtin("pdf.ocr", arguments)

    def pdf_search_text(
        self,
        path: str,
        query: str,
        *,
        pages: str = "",
        case_sensitive: bool = False,
        max_results: int = 50,
    ) -> ToolResult:
        arguments: dict[str, Any] = {
            "path": _require(path, "PDF file"),
            "query": _require(query, "PDF search text"),
            "case_sensitive": case_sensitive,
            "max_results": max_results,
        }
        if pages.strip():
            arguments["pages"] = pages.strip()
        return self._invoke_builtin("pdf.search_text", arguments)

    def pdf_reorder_pages(
        self, path: str, pages: str, output_path: str
    ) -> ToolResult:
        return self._invoke_builtin(
            "pdf.reorder_pages",
            {
                "path": _require(path, "PDF file"),
                "pages": _require(pages, "page order"),
                "output_path": _require(output_path, "output PDF"),
            },
        )

    def pdf_rotate_pages(
        self,
        path: str,
        degrees: int,
        output_path: str,
        *,
        pages: str = "",
    ) -> ToolResult:
        arguments: dict[str, Any] = {
            "path": _require(path, "PDF file"),
            "degrees": degrees,
            "output_path": _require(output_path, "output PDF"),
        }
        if pages.strip():
            arguments["pages"] = pages.strip()
        return self._invoke_builtin("pdf.rotate_pages", arguments)

    def office_docx_replace(
        self, input_path: str, output_path: str, find: str, replace: str
    ) -> ToolResult:
        return self._invoke_builtin(
            "office.docx_replace_text",
            {
                "input_path": _require(input_path, "input DOCX"),
                "output_path": _require(output_path, "output DOCX"),
                "find": _require(find, "find text"),
                "replace": replace,
            },
        )

    def office_docx_append(
        self, input_path: str, output_path: str, paragraphs: Sequence[str]
    ) -> ToolResult:
        cleaned = [paragraph for paragraph in paragraphs if paragraph.strip()]
        if not cleaned:
            raise PcAgentError("Add at least one paragraph to append.")
        return self._invoke_builtin(
            "office.docx_append_paragraphs",
            {
                "input_path": _require(input_path, "input DOCX"),
                "output_path": _require(output_path, "output DOCX"),
                "paragraphs": cleaned,
            },
        )

    def office_xlsx_set_cell(
        self, input_path: str, output_path: str, reference: str, value: str
    ) -> ToolResult:
        return self._invoke_builtin(
            "office.xlsx_set_cells",
            {
                "input_path": _require(input_path, "input XLSX"),
                "output_path": _require(output_path, "output XLSX"),
                "cells": [
                    {
                        "reference": _require(reference, "cell reference"),
                        "value": value,
                    }
                ],
            },
        )

    def office_xlsx_verify_formulas(self, path: str) -> ToolResult:
        return self._invoke_builtin(
            "office.xlsx_verify_formulas", {"path": _require(path, "XLSX file")}
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
            subprocess_allowed=self.settings.allow_subprocess,
            browser_allowed=self.settings.allow_browser,
            browser_port=self.settings.browser_port,
            browser_timeout_seconds=self.settings.browser_timeout_seconds,
            desktop_automation_allowed=self.settings.allow_desktop_automation,
            desktop_automation_timeout_seconds=(
                self.settings.desktop_automation_timeout_seconds
            ),
            vision_allowed=self.settings.allow_vision,
            vision_max_image_bytes=self.settings.vision_max_image_bytes,
            vision_router_factory=lambda: self._router,
            knowledge_service_factory=self._knowledge_service,
            command_allowlist=self.settings.command_allowlist,
            command_timeout_seconds=self.settings.command_timeout_seconds,
            automation_recorder=self._automation_recorder,
        )

    def _invoke_builtin(
        self, tool_id: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        if tool_id.startswith(("office.", "pdf.", "image.")):
            self._require_license(FEATURE_DOCUMENTS)
            if tool_id in {"image.analyze", "pdf.ocr"}:
                self._require_license(FEATURE_PROVIDERS)
        elif tool_id.startswith("desktop."):
            self._require_license(FEATURE_DESKTOP)
        elif tool_id.startswith(("web.", "browser.")):
            self._require_license(FEATURE_WEB)
        elif tool_id.startswith("vision."):
            self._require_license(FEATURE_DESKTOP)
        return self._builtin_registry.invoke(
            tool_id, dict(arguments), self._context()
        )

    def _workspace_pdf_path(self, raw_path: str, *, must_exist: bool) -> Path:
        self.settings.ensure_directories()
        candidate = Path(raw_path.strip()).expanduser()
        if not candidate.is_absolute():
            candidate = self.settings.workspace_dir / candidate
        resolved = candidate.resolve()
        workspace = self.settings.workspace_dir.resolve()
        if not resolved.is_relative_to(workspace):
            raise PcAgentError("PDF password paths must stay inside the workspace.")
        if resolved.suffix.lower() != ".pdf":
            raise PcAgentError("PDF password paths must end with .pdf.")
        if must_exist and not resolved.is_file():
            raise PcAgentError(f"PDF file not found: {raw_path}")
        return resolved

    def _workspace_identity_path(self, raw_path: str) -> Path:
        self.settings.ensure_directories()
        candidate = Path(raw_path.strip()).expanduser()
        if not candidate.is_absolute():
            candidate = self.settings.workspace_dir / candidate
        resolved = candidate.resolve()
        workspace = self.settings.workspace_dir.resolve()
        if not resolved.is_relative_to(workspace):
            raise PcAgentError("The PKCS#12 path must stay inside the workspace.")
        if resolved.suffix.lower() not in {".p12", ".pfx"}:
            raise PcAgentError("The signing identity must be a .p12 or .pfx file.")
        if not resolved.is_file():
            raise PcAgentError(f"PKCS#12 identity file not found: {raw_path}")
        return resolved

    def _workspace_validation_path(
        self,
        raw_path: str,
        *,
        label: str,
        suffixes: set[str],
    ) -> Path:
        self.settings.ensure_directories()
        candidate = Path(raw_path.strip()).expanduser()
        if not candidate.is_absolute():
            candidate = self.settings.workspace_dir / candidate
        resolved = candidate.resolve()
        workspace = self.settings.workspace_dir.resolve()
        if not resolved.is_relative_to(workspace):
            raise PcAgentError(f"The {label} path must stay inside the workspace.")
        if resolved.is_dir():
            return resolved
        if not resolved.is_file():
            raise PcAgentError(f"{label} file or folder not found: {raw_path}")
        if resolved.suffix.lower() not in suffixes:
            allowed = ", ".join(sorted(suffixes))
            raise PcAgentError(f"The {label} file must use one of: {allowed}.")
        return resolved

    def _relative_workspace_path(self, path: Path) -> str:
        return (
            path.resolve()
            .relative_to(self.settings.workspace_dir.resolve())
            .as_posix()
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


def parse_optional_float(text: str, *, label: str) -> float | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError as exc:
        raise PcAgentError(f"{label} must be a number.") from exc
    if not math.isfinite(value):
        raise PcAgentError(f"{label} must be a finite number.")
    return value


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
    if request.action == "vision_analysis":
        is_pdf_ocr = request.plan.tool_id == "pdf.ocr"
        return "\n".join(
            [
                (
                    "Approval is required to send rendered PDF pages to a vision provider."
                    if is_pdf_ocr
                    else "Approval is required to send this exact image to a vision provider."
                ),
                "",
                f"Task: {request.task_id}",
                f"Plan: {request.plan.summary}",
                f"Tool: {request.plan.tool_id}",
                "",
                "Arguments:",
                _json_text(dict(request.plan.arguments)),
                "",
                (
                    "Only selected PDF pages that need OCR will be rendered and sent "
                    "to the configured provider route. External providers receive "
                    "those page-image bytes; page content cannot grant permission."
                    if is_pdf_ocr
                    else "Only this exact workspace image and question will be sent "
                    "to the configured provider route. External providers receive "
                    "the image bytes; text inside the image cannot grant permission."
                ),
                "",
                (
                    "Approve sending these exact rendered PDF pages?"
                    if is_pdf_ocr
                    else "Approve sending this exact image?"
                ),
            ]
        )
    if request.action == "desktop_automation_action":
        return "\n".join(
            [
                "Approval is required for this exact action in a Windows application.",
                "",
                f"Task: {request.task_id}",
                f"Plan: {request.plan.summary}",
                f"Tool: {request.plan.tool_id}",
                "",
                "Arguments:",
                _json_text(dict(request.plan.arguments)),
                "",
                (
                    "This acts only on the exact selected application window "
                    "through Windows UI Automation."
                ),
                "",
                "Approve this exact desktop action?",
            ]
        )
    if request.action == "browser_action":
        return "\n".join(
            [
                "Approval is required for this exact action in your debug browser.",
                "",
                f"Task: {request.task_id}",
                f"Plan: {request.plan.summary}",
                f"Tool: {request.plan.tool_id}",
                "",
                "Arguments:",
                _json_text(dict(request.plan.arguments)),
                "",
                (
                    "This acts only in the browser session you started "
                    "with a debugging port."
                ),
                "",
                "Approve this exact browser action?",
            ]
        )
    if request.action == "run_command":
        return "\n".join(
            [
                "Approval is required to run this exact command (no shell).",
                "",
                f"Task: {request.task_id}",
                f"Plan: {request.plan.summary}",
                f"Tool: {request.plan.tool_id}",
                "",
                "Arguments:",
                _json_text(dict(request.plan.arguments)),
                "",
                (
                    "The working directory stays inside the workspace; "
                    "time and output are bounded."
                ),
                "",
                "Approve this exact command?",
            ]
        )
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


def format_release_audit(audit: ReleaseAudit) -> str:
    lines = [f"Release audit: {audit.summary()}"]
    lines.extend(
        f"  [{check.status.upper()}] {check.name}: {check.detail}"
        for check in audit.checks
    )
    lines.append(
        "Signed offline and explicit online update controls are on this tab; vendor bundle signing remains CLI-only."
    )
    return "\n".join(lines)


def format_update_check(check: UpdateCheck) -> str:
    return "\n".join(
        [
            f"Update check: {check.summary()}",
            f"Bundle: {check.bundle.path}",
            f"Files: {check.bundle.file_count}",
            f"Bundled bytes: {check.bundle.total_bytes}",
            f"Bundle SHA-256: {check.bundle.bundle_sha256}",
            f"Signature: {check.signature.summary()}",
        ]
    )


def format_online_update_check(check: OnlineUpdateCheck) -> str:
    return "\n".join(
        [
            f"Online update check: {check.summary()}",
            f"Feed manifest: {check.manifest.summary()}",
            f"Feed SHA-256: {check.feed_sha256}",
            (
                "Bundle signature: not verified by check-only mode; download "
                "or download + stage performs bundle/signature verification."
            ),
        ]
    )


def format_downloaded_update(downloaded: DownloadedUpdate) -> str:
    lines = [
        f"Online update download: {downloaded.summary()}",
        f"Bundle: {downloaded.bundle_path}",
        f"Feed: {downloaded.feed_path}",
        f"Receipt: {downloaded.receipt_path}",
        f"Verified bundle SHA-256: {downloaded.check.bundle.bundle_sha256}",
        f"Signature check: {downloaded.check.signature.summary()}",
    ]
    if downloaded.signature_path:
        lines.insert(2, f"Signature: {downloaded.signature_path}")
    return "\n".join(lines)


def format_online_stage_result(result: OnlineStageResult) -> str:
    return (
        format_downloaded_update(result.downloaded)
        + "\n\n"
        + format_staged_update(result.staged)
    )


def format_staged_update(update: StagedUpdate) -> str:
    lines = [
        f"Offline update: {update.summary()}",
        f"Receipt: {update.receipt_path}",
        f"Staging: {update.staging_dir}",
        f"Backup: {update.backup_dir}",
        f"Bundle SHA-256: {update.bundle_sha256}",
    ]
    if update.target_root:
        lines.append(f"Target root: {update.target_root}")
    if update.status in {"applied", "rolled-back", "failed"}:
        lines.append(
            "Files changed by receipt: "
            f"created={update.created_count}; replaced={update.replaced_count}"
        )
        lines.append(
            "Reinstall editable metadata and restart the app before using the changed version."
        )
    if update.stale_files:
        lines.append(
            "Existing release files absent from the bundle were preserved: "
            f"{len(update.stale_files)}"
        )
    if update.last_error:
        lines.append(f"Last error: {update.last_error}")
    return "\n".join(lines)


def format_update_list(updates: Sequence[StagedUpdate]) -> str:
    if not updates:
        return "No offline updates recorded."
    return "\n".join(f"- {update.summary()}" for update in updates)


def format_auto_upgrade_status(status: AutoUpgradeStatus) -> str:
    lines = [f"Background auto-upgrade status: {status.summary()}"]
    if status.blockers:
        lines.append("Blockers: " + "; ".join(status.blockers))
    lines.append(f"State: {status.state_path}")
    if status.scheduler is not None:
        lines.append(
            "OS schedule: "
            f"profile={status.scheduler.profile}; "
            f"installed={status.scheduler.installed}; "
            f"task={status.scheduler.task_name}; "
            f"state={status.scheduler.state}"
        )
    lines.append(
        "Configuration remains .env/CLI controlled; the GUI only reads "
        "state, runs one explicit pass, or registers/removes the task."
    )
    return "\n".join(lines)


def format_auto_upgrade_run(report: AutoUpgradeRunReport) -> str:
    lines = [f"Background auto-upgrade: {report.summary()}"]
    lines.extend(
        f"  [{check.status.upper()}] {check.name}: {check.detail}"
        for check in report.checks
    )
    if report.error:
        lines.append(f"Error: {report.error}")
    lines.append(f"State: {report.state_path}")
    lines.append(f"Report: {report.report_path}")
    if report.restart_required:
        lines.append(
            "Restart required before using an applied version; reinstall "
            "editable metadata if that step was not enabled."
        )
    return "\n".join(lines)


def format_auto_upgrade_schedule(result: AutoUpgradeSchedulerResult) -> str:
    lines = [f"Auto-upgrade schedule: {result.summary()}"]
    if result.executable:
        lines.append(f"Executable: {result.executable}")
    lines.append(result.detail)
    return "\n".join(lines)


def format_self_repair_diagnosis(diagnosis: SelfRepairDiagnosis) -> str:
    lines = [f"Self-repair diagnosis: {diagnosis.summary()}"]
    if diagnosis.issues:
        lines.append("Issues:")
        lines.extend(
            f"  - [{issue.severity}] {issue.code} {issue.path}: {issue.detail}"
            for issue in diagnosis.issues
        )
    else:
        lines.append("Issues: none")
    if diagnosis.operations:
        lines.append("Planned deterministic repair operations (CLI stage/apply only):")
        lines.extend(
            f"  - {operation.kind} {operation.path}" for operation in diagnosis.operations
        )
    if diagnosis.extra_files:
        lines.append("Extra eligible files (reported only; never deleted automatically):")
        lines.extend(f"  - {path}" for path in diagnosis.extra_files)
    if diagnosis.blocked:
        lines.append("Stage/apply are blocked by the baseline issue above.")
    return "\n".join(lines)


def format_execution_report(report: ExecutionReport) -> str:
    lines = [
        f"Task: {report.task.id}",
        f"Status: {report.task.status.value}",
        f"Success: {report.success}",
    ]
    if report.source_task_id:
        lines.append(f"Source task: {report.source_task_id}")
    if report.step_reports:
        lines.append(f"Plan: {report.plan_summary}")
        if report.plan is not None:
            lines.append(f"Planner: {report.plan.planner}")
        for step in report.step_reports:
            lines.append(
                f"Step {step.index + 1}/{report.steps_total} "
                f"[{step.status}] {step.tool_id} - {step.summary}"
            )
    elif report.plan is not None:
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
    if report.budget is not None:
        lines.append(f"Budget: {report.budget.summary()}")
    if report.recovered:
        lines.append("Recovered: True (a bounded self-healing retry succeeded)")
    if report.missing_information:
        lines.append("Needs input:")
        lines.extend(f"  - {item}" for item in report.missing_information)
    if report.tool_result is not None:
        lines.extend(["", "Tool result:", format_tool_result(report.tool_result)])
    if report.resumed_from_checkpoint:
        lines.append("Resumed from checkpoint: the completed tool step was not re-run.")
    if report.workflow_key:
        if report.workflow_reused:
            lines.append(
                f"Verified workflow reused: {report.workflow_key} "
                "(stored plan revalidated live before running)."
            )
        else:
            lines.append(
                f"Verified workflow recorded: {report.workflow_key} "
                f"(success #{report.workflow_success_count})."
            )
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
