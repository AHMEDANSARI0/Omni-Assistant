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
from .config import Settings
from .domain.task import TaskStatus
from .errors import PcAgentError
from .licensing import LicenseService
from .maintenance import DatabaseMaintenanceService
from .providers import provider_statuses
from .release import audit_install
from .self_repair import SelfRepairService
from .storage.db import Database
from .storage.repositories import (
    DynamicToolRepository,
    ExecutionRepository,
    MemoryRepository,
    PreferenceRepository,
    RecoveryRepository,
    TaskRepository,
)
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
        counts["recovery_attempts"] = recovery.count_attempts()
        counts["dynamic_tools_enabled"] = sum(
            1 for record in approved_dynamic if record.enabled
        )
        counts["dynamic_tools_approved"] = len(approved_dynamic)
        if settings.license_enforcement != "off":
            license_summary = LicenseService(settings, database).status().to_dict()

    repair = SelfRepairService(settings.root_dir, settings.data_dir)
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
        "release_audit": audit.to_dict(),
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
