"""Redacted diagnostics export for user-controlled support sharing.

The export includes versions, counts, provider names and local paths. It
never includes API-key values, task instructions, memory values, browser form
values, provider prompts, tool arguments or document contents. Users can
inspect the JSON before deciding whether to share it.
"""

from __future__ import annotations

import hashlib
import json
import platform
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .baseline import baseline_status
from .config import Settings
from .domain.task import TaskStatus
from .errors import PcAgentError
from .licensing import LicenseService
from .maintenance import DatabaseMaintenanceService
from .profiles import list_profiles
from .provider_platform import (
    capability_catalog,
    load_latest_evaluation,
    load_usage_records,
    route_explanation,
    usage_stats,
)
from .providers import provider_statuses
from .providers.router import build_model_router
from .release import audit_install
from .safety import EmergencyStop
from .self_repair import SelfRepairService
from .storage.db import Database
from .storage.repositories import (
    DynamicToolRepository,
    EventRepository,
    ExecutionRepository,
    MemoryRepository,
    MissionRepository,
    PreferenceRepository,
    RecoveryRepository,
    TaskRepository,
)
from .system_monitor import SystemMonitor
from .tools import build_default_registry


@dataclass(frozen=True, slots=True)
class DiagnosticsExportInfo:
    path: Path
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "size": self.size, "sha256": self.sha256}


def default_diagnostics_path(settings: Settings) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return settings.workspace_dir / f"diagnostics-{stamp}.json"


def _provider_rows(settings: Settings) -> list[dict[str, Any]]:
    sources = dict(settings.credential_sources)
    return [
        {
            "name": status.name,
            "model": status.model,
            "implemented": status.implemented,
            "configured": status.configured,
            "in_route": status.in_route,
            "credential_source": sources.get(status.name, "none"),
            "detail": status.detail,
        }
        for status in provider_statuses(settings)
    ]


def build_diagnostics(
    settings: Settings, database: Database | None = None
) -> dict[str, Any]:
    """Build a JSON-serializable, secret-free diagnostics document."""

    counts: dict[str, Any] = {
        "tasks_by_status": {},
        "memory_by_kind": {},
        "preferences": 0,
        "checkpoints": 0,
        "verifications": 0,
        "recovery_attempts": 0,
        "dynamic_tools_enabled": 0,
        "dynamic_tools_approved": 0,
    }
    license_summary: dict[str, Any] = {
        "enforcement": settings.license_enforcement,
        "state": "not_evaluated",
    }
    schema_version = 0
    pending_approvals: int | None = None
    events_total: int | None = None
    mission_counts: dict[str, int] = {
        "missions": 0,
        "schedules": 0,
        "schedules_enabled": 0,
    }
    if database is not None:
        schema_version = database.schema_version()
        tasks = TaskRepository(database)
        memory = MemoryRepository(database)
        execution = ExecutionRepository(database)
        recovery = RecoveryRepository(database)
        dynamic_tools = DynamicToolRepository(database)
        approved_dynamic = dynamic_tools.approved_candidates()
        counts["tasks_by_status"] = {
            status.value: tasks.count(status=status) for status in TaskStatus
        }
        counts["memory_by_kind"] = memory.counts()
        counts["preferences"] = len(PreferenceRepository(database).all())
        counts["checkpoints"] = execution.count_checkpoints()
        counts["verifications"] = execution.count_verifications()
        if schema_version >= 7:
            pending_approvals = execution.pending_approval_center_count()
        if schema_version >= 8:
            events_total = EventRepository(database).count()
        if schema_version >= 9:
            mission_repository = MissionRepository(database)
            mission_counts = {
                "missions": mission_repository.count_missions(),
                "schedules": mission_repository.count_schedules(),
                "schedules_enabled": mission_repository.count_schedules(enabled_only=True),
            }
        counts["recovery_attempts"] = recovery.count_attempts()
        counts["dynamic_tools_enabled"] = sum(
            1 for record in approved_dynamic if record.enabled
        )
        counts["dynamic_tools_approved"] = len(approved_dynamic)
        if settings.license_enforcement != "off":
            license_summary = LicenseService(settings, database).status().to_dict()

    repair = SelfRepairService(settings.root_dir, settings.data_dir)
    transformation_baseline = baseline_status(settings.root_dir)
    emergency_state = EmergencyStop(settings.data_dir).status()
    baseline = repair.baseline_status()
    latest_run = next(iter(repair.list_runs(limit=1)), None)
    backups = DatabaseMaintenanceService(
        settings.database_path, settings.data_dir
    ).list_backups()
    builtin_tools = build_default_registry().list_tools()
    audit = audit_install(settings)

    return {
        "format": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "privacy": {
            "includes": [
                "versions",
                "local paths",
                "provider and tool names",
                "counts and status summaries",
            ],
            "excludes": [
                "API keys and license keys",
                "task instructions",
                "memory values",
                "tool arguments and outputs",
                "provider prompts",
                "documents and workspace file contents",
            ],
        },
        "app": {
            "name": "Self-Healing PC Agent",
            "version": __version__,
            "environment": settings.environment,
            "transformation_baseline": transformation_baseline.to_dict(),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "paths": {
            "root": str(settings.root_dir),
            "workspace": str(settings.workspace_dir),
            "data": str(settings.data_dir),
            "database": str(settings.database_path),
        },
        "database": {
            "exists": settings.database_path.is_file(),
            "schema_version": schema_version,
            "size": (
                settings.database_path.stat().st_size
                if settings.database_path.is_file()
                else 0
            ),
            "backup_count": len(backups),
            "latest_backup": backups[0].backup_id if backups else "",
        },
        "settings": settings.public_summary(),
        "providers": _provider_rows(settings),
        "tools": {
            "builtin_count": len(builtin_tools),
            "builtin_ids": [tool.spec.id for tool in builtin_tools],
        },
        "counts": counts,
        "licensing": license_summary,
        "self_repair": {
            "enabled": settings.allow_self_repair,
            "baseline": baseline.to_dict(),
            "latest_run": latest_run.to_dict() if latest_run else None,
        },
        "safety": {
            "hard_timeouts": settings.safety_hard_timeouts,
            "worker_isolation": settings.safety_worker_isolation,
            "max_tool_seconds": settings.safety_max_tool_seconds,
            "approval_expiry_minutes": settings.safety_approval_expiry_minutes,
            "emergency_stop": {
                "engaged": emergency_state.engaged,
                "source": emergency_state.source,
                "engaged_at": emergency_state.engaged_at,
            },
            "pending_approvals": pending_approvals,
            "encrypted_backups": sum(1 for backup in backups if backup.encrypted),
        },
        "service": {
            "api_enabled": settings.api_enabled,
            "api_host": settings.api_host,
            "api_port": settings.api_port,
            "api_token_source": "environment" if settings.api_token else "file (auto)",
            "log_format": settings.log_format,
            "active_profile": settings.active_profile,
            "profiles": [info.name for info in list_profiles(settings.data_dir)],
            "events_total": events_total,
        },
        "system": _system_section(settings),
        "provider_platform": _provider_platform_section(settings),
        "knowledge": _knowledge_section(settings, database),
        "agents": _agents_section(settings, database),
        "voice": _voice_section(settings, database),
        "automation": _automation_section(settings, database),
        "flows": _flows_section(settings, database),
        "connectors": _connectors_section(settings, database),
        "proactive": _proactive_section(settings, database),
        "plugins": _plugins_section(settings, database),
        "sync": _sync_section(settings, database),
        "a11y": _a11y_section(settings),
        "missions": {
            "max_parallel": settings.mission_max_parallel,
            "scheduler_interval_seconds": settings.scheduler_interval_seconds,
            **mission_counts,
        },
        "release_audit": audit.to_dict(),
    }


def _system_section(settings: Settings) -> dict[str, Any]:
    import time as _time

    monitor = SystemMonitor(
        root_dir=settings.root_dir, workspace_dir=settings.workspace_dir
    )
    monitor.snapshot()
    _time.sleep(0.2)
    snapshot = monitor.snapshot().to_dict()
    return snapshot


def _knowledge_section(
    settings: Settings, database: Database | None
) -> dict[str, Any]:
    if database is None:
        return {"available": False, "reason": "database unavailable"}
    from .knowledge import KnowledgeService

    try:
        service = KnowledgeService(settings, database)
        stats = service.stats()
        documents = service.list_documents(limit=20)["documents"]
    except Exception as exc:  # noqa: BLE001 - diagnostics must never fail wholesale.
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "available": True,
        "stats": stats,
        "recent_documents": [
            {
                "id": item["id"],
                "title": item["title"],
                "kind": item["kind"],
                "chunk_count": item["chunk_count"],
            }
            for item in documents
        ],
    }


def automation_summary_line(settings: Settings, database: Database | None) -> str:
    """One-line automation posture for ``pc-agent doctor``."""
    section = _automation_section(settings, database)
    ledger = section["ledger"]
    browser = section["browser"]
    desktop = section["desktop_automation"]
    return (
        f"browser={'on' if browser['enabled'] else 'off'}"
        f" ({browser['browser'] or 'no debug browser'}); "
        f"desktop={'on' if desktop.get('enabled') else 'off'}"
        f" ({desktop.get('backend') or 'unavailable'}); "
        f"vision=local; "
        f"ledger={ledger.get('total', 0)}"
        f" (refused={ledger.get('refused', 0)})"
    )


def _flows_section(settings: Settings, database: Database | None) -> dict[str, Any]:
    """Workflow-builder posture: counts, triggers and recent runs (no instructions)."""
    del settings
    if database is None:
        return {"available": False, "reason": "database unavailable"}
    from .flows import FlowStore

    try:
        store = FlowStore(database)
        summary = store.summary()
        recent = [
            {
                "id": run.id,
                "flow_name": run.flow_name,
                "status": run.status,
                "trigger_kind": run.trigger_kind,
                "mission_id": run.mission_id,
            }
            for run in store.list_runs(limit=5)
        ]
    except Exception as exc:  # noqa: BLE001 - diagnostics must never fail wholesale
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {"available": True, **summary, "recent_runs": recent}


def flows_summary_line(settings: Settings, database: Database | None) -> str:
    """One-line workflow posture for ``pc-agent doctor`` and the dashboard."""
    section = _flows_section(settings, database)
    if not section.get("available"):
        return f"unavailable ({section.get('reason', 'unknown')})"
    by_status = ", ".join(
        f"{key}={value}" for key, value in sorted(section["by_status"].items())
    ) or "none"
    return (
        f"flows={section['flows']}; triggers_enabled={section['triggers_enabled']}; "
        f"runs={section['runs']} ({by_status}); last={section['last_run_at'] or 'never'}"
    )


def _automation_section(
    settings: Settings, database: Database | None
) -> dict[str, Any]:
    from .browser_cdp import probe_browser
    from .desktop_automation import probe_desktop_automation

    version = probe_browser(port=settings.browser_port)
    ledger: dict[str, Any] = {"available": False, "reason": "database unavailable"}
    recent: list[dict[str, Any]] = []
    if database is not None:
        from .storage.repositories import AutomationRepository

        try:
            repository = AutomationRepository(database)
            ledger = {"available": True, **repository.summary()}
            recent = [
                {
                    "id": run.id,
                    "kind": run.kind,
                    "tool_id": run.tool_id,
                    "action": run.action,
                    "status": run.status,
                    "allowed": run.allowed,
                }
                for run in repository.list_runs(limit=10)
            ]
        except Exception as exc:  # noqa: BLE001 - diagnostics must never fail wholesale.
            ledger = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "browser": {
            "enabled": bool(settings.allow_browser),
            "port": settings.browser_port,
            "connected": version is not None,
            "browser": str(version.get("Browser") or "") if version else "",
        },
        "desktop_automation": {
            "enabled": bool(settings.allow_desktop_automation),
            **probe_desktop_automation(),
        },
        "vision": {
            "local_analysis": True,
            "provider_vision_enabled": bool(settings.allow_vision),
            "max_image_bytes": settings.vision_max_image_bytes,
        },
        "ledger": ledger,
        "recent_runs": recent,
    }


def _voice_section(
    settings: Settings, database: Database | None
) -> dict[str, Any]:
    if database is None:
        return {"available": False, "reason": "database unavailable"}
    from .voice import VoiceService

    try:
        service = VoiceService(settings, database)
        status = service.status()
    except Exception as exc:  # noqa: BLE001 - diagnostics must never fail wholesale.
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "available": status.available,
        "summary": status.summary_line(),
        "microphone": status.microphone,
        "stt_engine": status.stt["engine"],
        "tts_engine": status.tts["engine"],
        "stt_engines": status.stt["engines"],
        "tts_engines": status.tts["engines"],
        "input_devices": status.input_devices["count"],
        "output_devices": status.output_devices["count"],
        "sessions": status.sessions,
    }


def _connectors_section(
    settings: Settings, database: Database | None
) -> dict[str, Any]:
    if database is None:
        return {"available": False, "reason": "database unavailable"}
    from .connectors import ConnectorService

    try:
        service = ConnectorService(settings, database)
        status = service.status()
    except Exception as exc:  # noqa: BLE001 - diagnostics must never fail wholesale.
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "available": True,
        "allow_connectors": status["allow_connectors"],
        "allow_email_connector": status["allow_email_connector"],
        "enabled": status["enabled"],
        "connectors": status["connectors"],
        "ledger": status["ledger"],
    }


def connectors_summary_line(settings: Settings, database: Database | None) -> str:
    """One-line connector posture for ``pc-agent doctor``."""

    section = _connectors_section(settings, database)
    if not section.get("available"):
        return f"unavailable ({section.get('reason', 'unknown')})"
    ready = ", ".join(section["enabled"]) or "none"
    ledger = section["ledger"]
    return (
        f"enabled={ready}; events={ledger['events']}; "
        f"deliveries={ledger['deliveries']} (failed={ledger['deliveries_failed']}); "
        f"last={ledger['last_event_at'] or 'never'}"
    )


def _proactive_section(
    settings: Settings, database: Database | None
) -> dict[str, Any]:
    if database is None:
        return {"available": False, "reason": "database unavailable"}
    from .proactive import ProactiveEngine

    try:
        status = ProactiveEngine(settings, database).status()
    except Exception as exc:  # noqa: BLE001 - diagnostics must never fail wholesale.
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "available": bool(status["enabled"]),
        "outbound_enabled": status["outbound_enabled"],
        "quiet_hours": status["quiet_hours"],
        "quiet_now": status["quiet_now"],
        "briefing_enabled": status["briefing_enabled"],
        "briefing_time": status["briefing_time"],
        "reminders_open": status["reminders_open"],
        "next_due_at": status["next_due_at"],
        "notifications": status["notifications"],
        "notifications_last_hour": status["notifications_last_hour"],
        "notification_max_per_hour": status["notification_max_per_hour"],
        "briefings": status["briefings"],
        "last_briefing_at": status["last_briefing_at"],
    }


def _a11y_section(settings: Settings) -> dict[str, Any]:
    from .a11y import a11y_status

    try:
        report = a11y_status(settings)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never fail wholesale
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "available": True,
        "locale": report["locale"],
        "direction": report["direction"],
        "rtl": report["rtl"],
        "scale": report["scale"],
        "theme": report["theme"],
        "reduce_motion": report["reduce_motion"],
        "coverage_percent": report["coverage"]["percent"],
        "contrast_passing": report["contrast"]["passing"],
        "contrast_total": report["contrast"]["total"],
        "wcag_level": report["contrast"]["wcag_level"],
        "shortcut_count": len(report["shortcuts"]),
        "known_limitations": len(report["limitations"]),
    }


def _sync_section(settings: Settings, database: Database | None) -> dict[str, Any]:
    if database is None:
        return {"available": False, "reason": "database unavailable"}
    from .sync import SyncService

    try:
        status = SyncService(settings, database).status()
    except Exception as exc:  # noqa: BLE001 - diagnostics must never fail wholesale.
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "available": bool(status["enabled"]),
        "enabled": bool(status["enabled"]),
        "device_id": status["device"]["device_id"],
        "transport": status["transport"],
        "sync_dir_state": status["sync_dir_state"],
        "conflict_policy": status["conflict_policy"],
        "limits": status["limits"],
        "peers": status["counts"]["peers"],
        "bundles": status["counts"]["bundles"],
        "pending_requests": status["counts"]["pending_requests"],
        "last_export_at": status["last_export_at"],
        "last_import_at": status["last_import_at"],
    }


def sync_summary_line(settings: Settings, database: Database | None) -> str:
    """One-line device-sync posture for ``pc-agent doctor``."""

    section = _sync_section(settings, database)
    if not section.get("available"):
        reason = section.get("reason")
        return f"off ({reason})" if reason else "off (PC_AGENT_ALLOW_SYNC is not set)"
    return (
        f"device={section['device_id'][:12]} peers={section['peers']} "
        f"bundles={section['bundles']} pending={section['pending_requests']}; "
        f"transport={section['transport']} dir={section['sync_dir_state']} "
        f"policy={section['conflict_policy']}; "
        f"last_export={section['last_export_at'] or 'never'} "
        f"last_import={section['last_import_at'] or 'never'}"
    )


def _plugins_section(settings: Settings, database: Database | None) -> dict[str, Any]:
    if database is None:
        return {"available": False, "reason": "database unavailable"}
    from .plugins import PluginService

    try:
        status = PluginService(settings, database).status()
    except Exception as exc:  # noqa: BLE001 - diagnostics must never fail wholesale.
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    plugins = status["plugins"]
    return {
        "available": bool(status["enabled"]),
        "enabled": bool(status["enabled"]),
        "signature_required": status["signature_required"],
        "trusted_keys": status["trusted_keys"],
        "sandbox": status["sandbox"],
        "limits": status["limits"],
        "installed": status["counts"]["plugins"],
        "enabled_plugins": status["counts"]["enabled"],
        "runs": status["counts"]["runs"],
        "names": [entry["id"] for entry in plugins],
        "denied_capabilities": status["denied_capabilities"],
    }


def plugins_summary_line(settings: Settings, database: Database | None) -> str:
    """One-line plugin posture for ``pc-agent doctor``."""

    section = _plugins_section(settings, database)
    if not section.get("available"):
        reason = section.get("reason", "disabled")
        return f"off ({reason if reason != 'disabled' else 'PC_AGENT_ALLOW_PLUGINS is not set'})"
    return (
        f"installed={section['installed']} enabled={section['enabled_plugins']} "
        f"runs={section['runs']}; "
        f"signatures={'required' if section['signature_required'] else 'optional'} "
        f"keys={len(section['trusted_keys'])}; "
        f"sandbox={section['sandbox']['mechanism']} "
        f"timeout={section['limits']['timeout_seconds']}s"
    )


def proactive_summary_line(settings: Settings, database: Database | None) -> str:
    """One-line proactive posture for ``pc-agent doctor``."""

    section = _proactive_section(settings, database)
    if not section.get("available"):
        return f"off ({section.get('reason', 'disabled')})"
    quiet = "quiet" if section["quiet_now"] else "open"
    briefing = section["briefing_time"] if section["briefing_enabled"] else "off"
    return (
        f"reminders={section['reminders_open']} next={section['next_due_at'] or 'none'}; "
        f"notifications={section['notifications']} "
        f"(last hour {section['notifications_last_hour']}/{section['notification_max_per_hour']}); "
        f"quiet={section['quiet_hours']}({quiet}); briefing={briefing}; "
        f"outbound={'on' if section['outbound_enabled'] else 'off'}"
    )


def _agents_section(
    settings: Settings, database: Database | None
) -> dict[str, Any]:
    if database is None:
        return {"available": False, "reason": "database unavailable"}
    from .agents import AgentStudio

    try:
        studio = AgentStudio(settings, database)
        stats = studio.stats()
        agents = studio.list_agents()["agents"]
        recent = studio.list_runs(limit=10)["runs"]
    except Exception as exc:  # noqa: BLE001 - diagnostics must never fail wholesale.
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "available": True,
        "stats": stats,
        "agents": [
            {
                "name": item["name"],
                "role": item["role"],
                "tools": len(item["allowed_tools"]),
                "builtin": item["builtin"],
                "enabled": item["enabled"],
            }
            for item in agents
        ],
        "recent_runs": [
            {
                "id": run["id"],
                "agent": run["agent"],
                "status": run["status"],
                "team_run_id": run["team_run_id"],
            }
            for run in recent
        ],
    }


def _provider_platform_section(settings: Settings) -> dict[str, Any]:
    router = build_model_router(settings)
    evaluation = load_latest_evaluation(settings)
    return {
        "route": route_explanation(settings, router),
        "capabilities": capability_catalog(settings, router),
        "usage": usage_stats(load_usage_records(settings)),
        "latest_evaluation": (
            {
                "generated_at": evaluation.get("generated_at"),
                "passed": evaluation.get("passed"),
                "total": evaluation.get("total"),
                "score": evaluation.get("score"),
            }
            if evaluation
            else None
        ),
    }


def export_diagnostics(
    settings: Settings,
    output_path: Path | None = None,
    database: Database | None = None,
) -> DiagnosticsExportInfo:
    """Write diagnostics JSON without overwriting an existing file."""

    destination = (output_path or default_diagnostics_path(settings)).expanduser()
    if destination.exists() and destination.is_dir():
        destination = destination / default_diagnostics_path(settings).name
    destination = destination.resolve()
    if destination.suffix.lower() != ".json":
        raise PcAgentError("Diagnostics output must end with .json.")
    if destination.exists():
        raise PcAgentError(f"Diagnostics file already exists: {destination.name}.")
    payload = build_diagnostics(settings, database)
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f".{destination.name}.tmp-{secrets.token_hex(4)}")
    try:
        temp.write_bytes(data)
        if destination.exists():
            raise PcAgentError(f"Diagnostics file already exists: {destination.name}.")
        temp.replace(destination)
    finally:
        if temp.exists():
            temp.unlink()
    return DiagnosticsExportInfo(
        path=destination,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def production_summary_line(settings: Settings) -> str:
    """One honest line about CI coverage and the last end-to-end release run."""

    from .production_check import stored_production_report

    workflow = Path(settings.root_dir) / ".github" / "workflows" / "ci.yml"
    if not workflow.is_file():
        ci = "none"
    else:
        try:
            text = workflow.read_text(encoding="utf-8", errors="replace")
        except OSError:
            ci = "unreadable"
        else:
            systems = [
                name
                for name, token in (("ubuntu", "ubuntu-latest"), ("windows", "windows-latest"))
                if token in text
            ]
            ci = "+".join(systems) or "present"
    report = stored_production_report(settings)
    if report is None:
        evidence = "not run yet"
    elif report.get("ok") is True:
        evidence = f"passed {report.get('generated_at', '?')}"
    elif report.get("failures") == -1:
        evidence = "unreadable report"
    else:
        evidence = f"FAILED ({report.get('failures')} step(s)) {report.get('generated_at', '?')}"
    unsigned = "yes" if not getattr(settings, "require_signed_updates", False) else "no"
    from .baseline import windows_acceptance_evidence

    acceptance = windows_acceptance_evidence(Path(settings.root_dir))["status"]
    from .release import WINDOWS_SIGNING_REPORT

    signing_path = Path(settings.data_dir) / WINDOWS_SIGNING_REPORT
    authenticode = "not-run"
    if signing_path.is_file():
        try:
            signed = json.loads(signing_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            authenticode = "unreadable"
        else:
            authenticode = str(signed.get("status") or "unknown")
    return (
        f"ci={ci}; last-release-chain={evidence}; "
        f"unsigned-updates-allowed={unsigned}; "
        f"feed={'configured' if settings.update_feed_url else 'none'}; "
        f"windows-acceptance={acceptance}; authenticode={authenticode}"
    )
