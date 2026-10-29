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
from ..browser_cdp import probe_browser
from ..config import Settings
from ..credentials import CredentialStore, build_credential_store
from ..desktop_automation import probe_desktop_automation
from ..documents.office import DemoOfficeData, create_demo_office_files
from ..documents.pdf import DemoPdfData, create_demo_pdfs
from ..documents.pdf_security import (
    DEFAULT_PDF_PERMISSIONS,
    inspect_pdf_security,
    protect_pdf,
    unprotect_pdf,
)
from ..documents.pdf_signatures import inspect_pdf_signatures
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
from ..maintenance import DatabaseMaintenanceService
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
from ..providers import (
    CompletionRequest,
    ProviderStatus,
    build_model_router,
    provider_statuses,
)
from ..providers.router import RoutedCompletion
from ..recovery import RecoveryOutcome, RecoveryScan, RecoveryService, TaskDiagnosis
from ..release import ReleaseAudit, audit_install
from ..self_repair import SelfRepairDiagnosis, SelfRepairService
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
                (
                    f"Self-Healing PC Agent {self.version} "
                    "(PHASE 27 PDF Signature Inspection)"
                ),
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
                    "web.fetch=public-only by default, desktop=open/info plus gated Windows UIA, images=local Pillow tools plus gated provider vision, provider keys=environment first, then Windows Credential Manager"
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
            f"channel={public['update_channel']}; latest={latest}"
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
            command_allowlist=self.settings.command_allowlist,
            command_timeout_seconds=self.settings.command_timeout_seconds,
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
        elif tool_id.startswith("web."):
            self._require_license(FEATURE_WEB)
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
