"""Local Service boundary for Omni-Assistant (Transformation Phase 3).

``LocalService`` is the single transport-independent application service
the local HTTP API (and future UIs) talk to. It owns one database and
one orchestrator, runs submitted tasks on a background thread (never
blocking an API caller), and records durable application events. It
reuses the exact orchestrator construction the CLI has always used, so
safety, approvals, budgets, verification and recovery behave
identically; nothing here can bypass the Safety Kernel or the Approval
Center. Approvals through the service are non-interactive: a tool that
needs consent files an Approval Center request and simply has no
in-band callback to grant it.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Self

from .agents import AgentStudio
from .config import Settings
from .domain.task import Task, TaskStatus
from .errors import (
    AgentError,
    PcAgentError,
    PlanningError,
    ProviderError,
    VoiceError,
)
from .knowledge import KnowledgeError, KnowledgeService
from .licensing import FEATURE_CORE_TASKS, LicenseService
from .logging_config import redact_text
from .missions import MissionRunner, execution_levels
from .orchestrator import ApprovalRequest, ExecutionReport, TaskOrchestrator
from .planning import DeterministicPlanner
from .provider_platform import (
    capability_catalog,
    load_latest_evaluation,
    load_usage_records,
    route_explanation,
    run_evaluations,
    usage_stats,
)
from .providers.base import CompletionRequest
from .providers.router import build_model_router
from .safety import EmergencyStop
from .storage.db import LATEST_SCHEMA_VERSION, Database
from .storage.repositories import (
    EventRepository,
    ExecutionRepository,
    MemoryRepository,
    MissionRepository,
    RecoveryRepository,
    TaskRepository,
)
from .system_monitor import SystemMonitor
from .tooling.service import build_runtime_registry
from .tools import build_default_registry
from .tools.registry import ToolRegistry

__all__ = ["LocalService", "task_to_dict"]

_LOG = logging.getLogger("pc_agent.service")
TERMINAL_STATUSES = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.NEEDS_INPUT}
)


def task_to_dict(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "instruction": task.instruction,
        "status": task.status.value,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "error": task.error,
    }


class LocalService:
    """Thread-safe facade over tasks, approvals, safety, events, config."""

    def __init__(
        self,
        settings: Settings,
        *,
        database: Database | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        settings.ensure_directories()
        self._settings = settings
        self._database = database or Database(settings.database_path)
        self._database.initialize()
        self._tasks = TaskRepository(self._database)
        self._executions = ExecutionRepository(self._database)
        self._events = EventRepository(self._database)
        self._missions = MissionRepository(self._database)
        self._system = SystemMonitor(
            root_dir=settings.root_dir, workspace_dir=settings.workspace_dir
        )
        self._registry = registry or build_default_registry()
        self._orchestrator = TaskOrchestrator(
            settings=settings,
            task_repository=self._tasks,
            execution_repository=self._executions,
            tool_registry=self._registry,
            deterministic_planner=DeterministicPlanner(settings.workspace_dir),
            model_router=build_model_router(settings),
            recovery_repository=RecoveryRepository(self._database),
            memory_repository=MemoryRepository(self._database),
        )
        self._license = LicenseService(settings, self._database)
        self._runtime_registry = build_runtime_registry(
            self._database, data_dir=settings.data_dir
        )
        self._emergency_stop = EmergencyStop(settings.data_dir)
        self._cancel_flags: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._mission_threads: dict[str, threading.Thread] = {}
        self._agent_threads: dict[str, threading.Thread] = {}
        self._agent_results: dict[str, dict[str, Any]] = {}
        self._mission_cancels: dict[str, threading.Event] = {}
        self._mission_results: dict[str, dict[str, Any]] = {}
        self._reports: dict[str, dict[str, Any]] = {}
        self._run_lock = threading.Lock()
        self._closed = False

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def database(self) -> Database:
        return self._database

    # -- health / overview --------------------------------------------------
    def health(self) -> dict[str, Any]:
        emergency = self._emergency_stop.status()
        return {
            "status": "ok",
            "service": "omni-assistant-local",
            "version": _package_version(),
            "schema_version": self._database.schema_version(),
            "schema_latest": LATEST_SCHEMA_VERSION,
            "emergency_stop_engaged": bool(emergency.engaged),
            "active_profile": self._settings.active_profile,
            "running_tasks": sorted(tid for tid, t in self._threads.items() if t.is_alive()),
            "running_missions": sorted(
                mid for mid, thread in self._mission_threads.items() if thread.is_alive()
            ),
        }

    def overview(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for status in TaskStatus:
            total = self._tasks.count(status=status)
            if total:
                counts[status.value] = total
        return {
            "tasks": {"total": self._tasks.count(), "by_status": counts},
            "events_total": self._events.count(),
            "pending_approvals": self._executions.pending_approval_center_count(),
            "tools": len(self._runtime_registry.list_tools()),
            "missions": self._missions.count_missions(),
            "schedules": self._missions.count_schedules(),
            "schedules_enabled": self._missions.count_schedules(enabled_only=True),
            "health": self.health(),
        }

    # -- tasks ---------------------------------------------------------------
    def create_task(self, instruction: str, *, source: str = "api") -> dict[str, Any]:
        self._require_core_tasks()
        task = self._tasks.create(Task(instruction=instruction))
        self._emit(
            "task.created",
            source=source,
            task_id=task.id,
            summary=f"Task created: {task.instruction[:120]}",
        )
        return task_to_dict(task)

    def list_tasks(
        self, *, limit: int = 50, offset: int = 0, status: str | None = None
    ) -> dict[str, Any]:
        parsed = TaskStatus(status) if status else None
        tasks = self._tasks.list(limit=limit, offset=offset, status=parsed)
        return {
            "tasks": [task_to_dict(task) for task in tasks],
            "total": self._tasks.count(status=parsed),
            "limit": limit,
            "offset": offset,
        }

    def resolve_task_id(self, reference: str) -> str:
        if reference == "latest":
            tasks = self._tasks.list(limit=1)
            if not tasks:
                raise PlanningError("No tasks exist yet.")
            return tasks[0].id
        if reference == "latest-failed":
            tasks = self._tasks.list(limit=1, status=TaskStatus.FAILED)
            if not tasks:
                raise PlanningError("No failed tasks exist yet.")
            return tasks[0].id
        exact = self._tasks.get(reference)
        if exact is not None:
            return exact.id
        matches = [task for task in self._tasks.list(limit=500) if task.id.startswith(reference)]
        if len(matches) == 1:
            return matches[0].id
        if len(matches) > 1:
            raise PlanningError(
                f"Task reference '{reference}' matches {len(matches)} tasks; use a full id."
            )
        raise PlanningError(f"Task not found: {reference}")

    def get_task(self, reference: str) -> dict[str, Any]:
        task_id = self.resolve_task_id(reference)
        task = self._tasks.get(task_id)
        assert task is not None
        events = [
            {
                "id": event.id,
                "event_type": event.event_type,
                "old_status": event.old_status.value if event.old_status else None,
                "new_status": event.new_status.value if event.new_status else None,
                "message": event.message,
                "created_at": event.created_at.isoformat(),
            }
            for event in self._tasks.list_events(task_id)
        ]
        payload = task_to_dict(task)
        payload["task_events"] = events
        payload["report"] = self._reports.get(task_id)
        payload["running"] = bool(
            self._threads.get(task_id) and self._threads[task_id].is_alive()
        )
        return payload

    def start_task(
        self, reference: str, *, use_ai: bool = False, source: str = "api"
    ) -> dict[str, Any]:
        """Start a task on a background thread; returns immediately (202-style)."""
        self._require_core_tasks()
        task_id = self.resolve_task_id(reference)
        task = self._tasks.get(task_id)
        assert task is not None
        if task.status in TERMINAL_STATUSES:
            raise PlanningError(
                f"Task {task_id} is already {task.status.value} and cannot be run again."
            )
        with self._run_lock:
            existing = self._threads.get(task_id)
            if existing is not None and existing.is_alive():
                raise PlanningError(f"Task {task_id} is already running.")
            cancel_flag = threading.Event()
            self._cancel_flags[task_id] = cancel_flag
            self._reports.pop(task_id, None)
            thread = threading.Thread(
                target=self._execute_on_thread,
                args=(task_id, use_ai, source, cancel_flag),
                name=f"local-service-task-{task_id[:8]}",
                daemon=True,
            )
            self._threads[task_id] = thread
            thread.start()
        self._emit(
            "task.run_started",
            source=source,
            task_id=task_id,
            summary=f"Task run started (use_ai={use_ai}).",
        )
        return {"task_id": task_id, "accepted": True, "task": task_to_dict(task)}

    def run_instruction(
        self, instruction: str, *, use_ai: bool = False, source: str = "api"
    ) -> dict[str, Any]:
        created = self.create_task(instruction, source=source)
        return self.start_task(created["id"], use_ai=use_ai, source=source)

    def wait_for_task(self, reference: str, *, timeout_seconds: float = 900.0) -> dict[str, Any]:
        task_id = self.resolve_task_id(reference)
        deadline = time.monotonic() + timeout_seconds
        while True:
            thread = self._threads.get(task_id)
            task = self._tasks.get(task_id)
            if task is not None and task.status in TERMINAL_STATUSES:
                if thread is not None:
                    thread.join(timeout=5)
                return self.get_task(task_id)
            if thread is None or not thread.is_alive():
                return self.get_task(task_id)
            if time.monotonic() > deadline:
                raise PlanningError(f"Timed out waiting for task {task_id}.")
            time.sleep(0.05)

    def cancel_task(self, reference: str, *, reason: str, source: str = "api") -> dict[str, Any]:
        task_id = self.resolve_task_id(reference)
        flag = self._cancel_flags.get(task_id)
        if flag is not None:
            flag.set()
        task, action = self._orchestrator.request_cancellation(task_id, reason=reason)
        self._emit(
            "task.cancel_requested",
            source=source,
            task_id=task_id,
            summary=f"Cancellation requested ({action}): {reason[:120]}",
            payload={"action": action},
        )
        return {"task_id": task_id, "action": action, "task": task_to_dict(task)}

    def delete_task(self, reference: str, *, source: str = "api") -> dict[str, Any]:
        task_id = self.resolve_task_id(reference)
        task = self._tasks.get(task_id)
        assert task is not None
        if not task.is_terminal:
            raise PlanningError(
                f"Task {task_id} is {task.status.value}, not terminal; cancel it first."
            )
        self._tasks.delete(task_id)
        self._emit(
            "task.deleted",
            source=source,
            task_id=None,
            summary=f"Task record deleted: {task_id} (workspace files untouched).",
            payload={"task_id": task_id},
        )
        return {"task_id": task_id, "deleted": True}

    # -- missions (Transformation Phase 4) -------------------------------------
    def create_mission(
        self,
        name: str,
        *,
        description: str = "",
        task_specs: list[dict[str, Any]] | None = None,
        source: str = "api",
    ) -> dict[str, Any]:
        self._require_core_tasks()
        mission = self._missions.create_mission(name, description=description)
        self._emit(
            "mission.created",
            source=source,
            summary=f"Mission created: {mission.name}",
            payload={"mission_id": mission.id},
        )
        keys: dict[str, str] = {}
        created_ids: list[str] = []
        for index, spec in enumerate(task_specs or []):
            instruction = str(spec.get("instruction") or "").strip()
            if not instruction:
                raise PlanningError("Every mission task needs an instruction.")
            task = self._tasks.create(Task(instruction=instruction))
            self._missions.add_task(mission.id, task.id)
            keys[str(spec.get("key") or index)] = task.id
            created_ids.append(task.id)
        for index, spec in enumerate(task_specs or []):
            task_id = created_ids[index]
            for dependency_key in spec.get("depends_on") or []:
                dependency_id = keys.get(str(dependency_key))
                if dependency_id is None:
                    raise PlanningError(
                        f"Unknown dependency key '{dependency_key}' in mission task {index}."
                    )
                self._missions.add_dependency(mission.id, task_id, dependency_id)
        return self.get_mission(mission.id)

    def add_mission_task(
        self,
        mission_id: str,
        *,
        instruction: str | None = None,
        task_reference: str | None = None,
        depends_on: list[str] | None = None,
        source: str = "api",
    ) -> dict[str, Any]:
        mission = self._require_mission(mission_id)
        if mission.status == "running":
            raise PlanningError("Cannot edit a mission while it is running.")
        if instruction:
            task = self._tasks.create(Task(instruction=instruction))
            task_id = task.id
        elif task_reference:
            task_id = self.resolve_task_id(task_reference)
        else:
            raise PlanningError("Provide an instruction or an existing task reference.")
        self._missions.add_task(mission.id, task_id)
        for dependency in depends_on or []:
            self._missions.add_dependency(mission.id, task_id, self.resolve_task_id(dependency))
        self._emit(
            "mission.task_added",
            source=source,
            task_id=task_id,
            summary=f"Task added to mission '{mission.name}'.",
            payload={"mission_id": mission.id},
        )
        return self.get_mission(mission.id)

    def add_mission_dependency(
        self, mission_id: str, *, task_reference: str, depends_on_reference: str
    ) -> dict[str, Any]:
        mission = self._require_mission(mission_id)
        if mission.status == "running":
            raise PlanningError("Cannot edit a mission while it is running.")
        self._missions.add_dependency(
            mission.id,
            self.resolve_task_id(task_reference),
            self.resolve_task_id(depends_on_reference),
        )
        return self.get_mission(mission.id)

    def list_missions(self, *, limit: int = 100) -> dict[str, Any]:
        missions = []
        for mission in self._missions.list_missions(limit=limit):
            payload = mission.to_dict()
            payload["progress"] = self._mission_progress(mission.id)
            missions.append(payload)
        return {"missions": missions, "total": self._missions.count_missions()}

    def get_mission(self, mission_reference: str) -> dict[str, Any]:
        mission = self._require_mission(self.resolve_mission_id(mission_reference))
        task_ids = self._missions.list_task_ids(mission.id)
        dependencies = self._missions.list_dependencies(mission.id)
        tasks = []
        for task_id in task_ids:
            task = self._tasks.get(task_id)
            if task is None:
                continue
            entry = task_to_dict(task)
            entry["depends_on"] = dependencies.get(task_id, [])
            tasks.append(entry)
        try:
            levels = execution_levels(task_ids, dependencies)
        except PlanningError:
            levels = []
        payload = mission.to_dict()
        payload["tasks"] = tasks
        payload["graph"] = {
            "nodes": task_ids,
            "edges": [
                {"from": dependency, "to": task_id}
                for task_id, deps in sorted(dependencies.items())
                for dependency in deps
            ],
            "levels": levels,
            "max_parallel": self._settings.mission_max_parallel,
        }
        payload["progress"] = self._mission_progress(mission.id)
        payload["last_run"] = self._mission_results.get(mission.id)
        payload["running"] = bool(
            self._mission_threads.get(mission.id)
            and self._mission_threads[mission.id].is_alive()
        )
        return payload

    def resolve_mission_id(self, reference: str) -> str:
        if reference == "latest":
            missions = self._missions.list_missions(limit=1)
            if not missions:
                raise PlanningError("No missions exist yet.")
            return missions[0].id
        exact = self._missions.get_mission(reference)
        if exact is not None:
            return exact.id
        matches = [
            mission
            for mission in self._missions.list_missions(limit=500)
            if mission.id.startswith(reference)
        ]
        if len(matches) == 1:
            return matches[0].id
        if len(matches) > 1:
            raise PlanningError(
                f"Mission reference '{reference}' matches {len(matches)} missions; use a full id."
            )
        raise PlanningError(f"Mission not found: {reference}")

    def start_mission(
        self, mission_reference: str, *, use_ai: bool = False, source: str = "api"
    ) -> dict[str, Any]:
        self._require_core_tasks()
        mission = self._require_mission(self.resolve_mission_id(mission_reference))
        task_ids = self._missions.list_task_ids(mission.id)
        if not task_ids:
            raise PlanningError(f"Mission '{mission.name}' has no tasks yet.")
        with self._run_lock:
            existing = self._mission_threads.get(mission.id)
            if existing is not None and existing.is_alive():
                raise PlanningError(f"Mission {mission.id} is already running.")
            cancel_event = threading.Event()
            self._mission_cancels[mission.id] = cancel_event
            self._mission_results.pop(mission.id, None)
            thread = threading.Thread(
                target=self._run_mission_on_thread,
                args=(mission.id, use_ai, source, cancel_event),
                name=f"mission-{mission.id[:8]}",
                daemon=True,
            )
            self._mission_threads[mission.id] = thread
            thread.start()
        return {"mission_id": mission.id, "accepted": True, "mission": mission.to_dict()}

    def wait_for_mission(
        self, mission_reference: str, *, timeout_seconds: float = 1800.0
    ) -> dict[str, Any]:
        mission_id = self.resolve_mission_id(mission_reference)
        deadline = time.monotonic() + timeout_seconds
        while True:
            thread = self._mission_threads.get(mission_id)
            mission = self._missions.get_mission(mission_id)
            if mission is not None and mission.status in {"succeeded", "failed", "cancelled"}:
                if thread is not None:
                    thread.join(timeout=5)
                return self.get_mission(mission_id)
            if thread is None or not thread.is_alive():
                return self.get_mission(mission_id)
            if time.monotonic() > deadline:
                raise PlanningError(f"Timed out waiting for mission {mission_id}.")
            time.sleep(0.05)

    def cancel_mission(
        self, mission_reference: str, *, reason: str, source: str = "api"
    ) -> dict[str, Any]:
        mission = self._require_mission(self.resolve_mission_id(mission_reference))
        cancel_event = self._mission_cancels.get(mission.id)
        if cancel_event is not None:
            cancel_event.set()
        cancelled: list[str] = []
        for task_id in self._missions.list_task_ids(mission.id):
            task = self._tasks.get(task_id)
            if task is None or task.is_terminal:
                continue
            try:
                self._orchestrator.request_cancellation(task_id, reason=reason)
                cancelled.append(task_id)
            except PcAgentError:
                _LOG.exception("Mission cancel could not cancel task %s", task_id)
        thread = self._mission_threads.get(mission.id)
        thread_active = thread is not None and thread.is_alive()
        if not thread_active and mission.status in {"draft", "running"}:
            self._missions.update_mission_status(mission.id, "cancelled")
        self._emit(
            "mission.cancel_requested",
            source=source,
            summary=f"Mission '{mission.name}' cancel requested: {reason[:120]}",
            payload={"mission_id": mission.id, "tasks": cancelled},
        )
        return {"mission_id": mission.id, "cancelled_tasks": cancelled,
                "mission": self._require_mission(mission.id).to_dict()}

    def delete_mission(self, mission_reference: str, *, source: str = "api") -> dict[str, Any]:
        mission = self._require_mission(self.resolve_mission_id(mission_reference))
        thread = self._mission_threads.get(mission.id)
        if thread is not None and thread.is_alive():
            raise PlanningError("Mission is running; cancel it before deleting.")
        deleted = self._missions.delete_mission(mission.id)
        self._emit(
            "mission.deleted",
            source=source,
            summary=f"Mission deleted: {mission.name} (its tasks were kept).",
            payload={"mission_id": mission.id},
        )
        return {"mission_id": mission.id, "deleted": deleted}

    def _run_mission_on_thread(
        self, mission_id: str, use_ai: bool, source: str, cancel_event: threading.Event
    ) -> None:
        runner = MissionRunner(
            settings=self._settings, database=self._database, emit=self._emit
        )
        try:
            result = runner.run(
                mission_id, use_ai=use_ai, cancel_event=cancel_event, source=source
            )
            self._mission_results[mission_id] = result
        except Exception as exc:  # Mission thread must never die silently.  # noqa: BLE001
            _LOG.exception("Mission %s crashed", mission_id)
            self._mission_results[mission_id] = {"error": redact_text(str(exc))[:400]}
            try:
                self._missions.update_mission_status(mission_id, "failed")
            except PcAgentError:
                _LOG.exception("Could not mark crashed mission %s failed", mission_id)

    def _require_mission(self, mission_id: str):
        mission = self._missions.get_mission(mission_id)
        if mission is None:
            raise PlanningError(f"Mission not found: {mission_id}")
        return mission

    def _mission_progress(self, mission_id: str) -> dict[str, int]:
        progress: dict[str, int] = {"total": 0}
        for task_id in self._missions.list_task_ids(mission_id):
            task = self._tasks.get(task_id)
            if task is None:
                continue
            progress["total"] += 1
            progress[task.status.value] = progress.get(task.status.value, 0) + 1
        return progress

    # -- schedules ---------------------------------------------------------------
    def create_schedule(
        self,
        name: str,
        *,
        instruction: str,
        kind: str,
        run_at: Any = None,
        interval_seconds: int | None = None,
        use_ai: bool = False,
        source: str = "api",
    ) -> dict[str, Any]:
        self._require_core_tasks()
        schedule = self._missions.create_schedule(
            name,
            instruction=instruction,
            kind=kind,
            run_at=run_at,
            interval_seconds=interval_seconds,
            use_ai=use_ai,
        )
        self._emit(
            "schedule.created",
            source=source,
            summary=f"Schedule '{schedule.name}' created ({schedule.kind}).",
            payload={"schedule_id": schedule.id},
        )
        return schedule.to_dict()

    def list_schedules(self) -> dict[str, Any]:
        schedules = self._missions.list_schedules()
        return {
            "schedules": [schedule.to_dict() for schedule in schedules],
            "total": len(schedules),
            "scheduler_interval_seconds": self._settings.scheduler_interval_seconds,
        }

    def set_schedule_enabled(
        self, schedule_id: str, *, enabled: bool, source: str = "api"
    ) -> dict[str, Any]:
        schedule = self._missions.set_schedule_enabled(schedule_id, enabled)
        self._emit(
            "schedule.updated",
            source=source,
            summary=f"Schedule '{schedule.name}' {'enabled' if enabled else 'disabled'}.",
            payload={"schedule_id": schedule.id},
        )
        return schedule.to_dict()

    def delete_schedule(self, schedule_id: str, *, source: str = "api") -> dict[str, Any]:
        deleted = self._missions.delete_schedule(schedule_id)
        if not deleted:
            raise PlanningError(f"Schedule not found: {schedule_id}")
        self._emit(
            "schedule.deleted",
            source=source,
            summary=f"Schedule deleted: {schedule_id}",
            payload={"schedule_id": schedule_id},
        )
        return {"schedule_id": schedule_id, "deleted": True}

    def run_due_schedules(self, *, source: str = "scheduler") -> dict[str, Any]:
        triggered: list[dict[str, Any]] = []
        for schedule in self._missions.due_schedules():
            created = self.create_task(schedule.instruction, source=source)
            updated = self._missions.mark_schedule_triggered(
                schedule.id, task_id=created["id"]
            )
            self.start_task(created["id"], use_ai=schedule.use_ai, source=source)
            self._emit(
                "schedule.triggered",
                source=source,
                task_id=created["id"],
                summary=(
                    f"Schedule '{schedule.name}' triggered"
                    f" (run #{updated.run_count}, next {updated.next_run_at.isoformat()})."
                ),
                payload={"schedule_id": schedule.id},
            )
            triggered.append(
                {"schedule_id": schedule.id, "task_id": created["id"], "name": schedule.name}
            )
        return {"triggered": triggered, "total": len(triggered)}

    def _execute_on_thread(
        self, task_id: str, use_ai: bool, source: str, cancel_flag: threading.Event
    ) -> None:
        def deny(_request: ApprovalRequest) -> bool:
            return False

        try:
            report = self._orchestrator.execute(
                task_id,
                approval_callback=deny,
                use_ai=use_ai,
                cancellation_requested=cancel_flag.is_set,
            )
            self._reports[task_id] = _report_summary(report)
            final = self._tasks.get(task_id)
            status_value = final.status.value if final else "unknown"
            self._emit(
                "task.run_finished",
                source=source,
                task_id=task_id,
                summary=f"Task finished with status {status_value}.",
                payload={"status": status_value, "success": bool(report.success)},
            )
        except Exception as exc:  # Thread must never die silently.  # noqa: BLE001
            _LOG.exception("Local service task %s crashed", task_id)
            self._reports[task_id] = {"error": redact_text(str(exc))[:400]}
            try:
                self._emit(
                    "task.run_crashed",
                    source=source,
                    task_id=task_id,
                    summary=f"Task run crashed: {redact_text(str(exc))[:180]}",
                )
            except Exception:  # Event failure must not mask the crash.  # noqa: BLE001
                _LOG.exception("Failed recording crash event for task %s", task_id)

    # -- system monitoring (Transformation Phase 6) ---------------------------
    def system_status(self) -> dict[str, Any]:
        snapshot = self._system.snapshot()
        return {"snapshot": snapshot.to_dict(), "history": self._system.history()}

    # -- approvals -----------------------------------------------------------
    def list_approvals(self, *, status: str = "pending", limit: int = 100) -> dict[str, Any]:
        requests = self._executions.list_approval_center_requests(status=status, limit=limit)
        return {
            "requests": [_approval_dict(request) for request in requests],
            "pending_count": self._executions.pending_approval_center_count(),
        }

    def decide_approval(
        self,
        request_key: str,
        *,
        approve: bool,
        note: str | None = None,
        decided_by: str = "api",
    ) -> dict[str, Any]:
        decided = self._executions.decide_approval_center_request(
            request_key, approve=approve, note=note, decided_by=decided_by
        )
        return _approval_dict(decided)

    # -- safety --------------------------------------------------------------
    def safety_status(self) -> dict[str, Any]:
        state = self._emergency_stop.status()
        return {
            "hard_timeouts": self._settings.safety_hard_timeouts,
            "worker_isolation": self._settings.safety_worker_isolation,
            "max_tool_seconds": self._settings.safety_max_tool_seconds,
            "approval_expiry_minutes": self._settings.safety_approval_expiry_minutes,
            "emergency_stop": state.to_dict(),
            "emergency_stop_file": str(self._emergency_stop.path),
            "pending_approvals": self._executions.pending_approval_center_count(),
        }

    def safety_stop(self, *, reason: str, engaged_by: str = "api") -> dict[str, Any]:
        state = self._emergency_stop.engage(reason=reason, engaged_by=engaged_by)
        self._emit(
            "safety.emergency_stop_engaged",
            source=engaged_by,
            summary=f"Emergency stop engaged: {reason[:160]}",
        )
        return state.to_dict()

    def safety_resume(self, *, released_by: str = "api") -> dict[str, Any]:
        state = self._emergency_stop.clear(cleared_by=released_by)
        self._emit(
            "safety.emergency_stop_released",
            source=released_by,
            summary="Emergency stop released.",
        )
        return state.to_dict()

    # -- events / config / catalog --------------------------------------------
    def list_events(
        self,
        *,
        since_id: int = 0,
        event_type: str | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        events = self._events.list(
            since_id=since_id, event_type=event_type, task_id=task_id, limit=limit
        )
        return {
            "events": [event.to_dict() for event in events],
            "cursor": events[-1].id if events else since_id,
            "total": self._events.count(),
        }

    def public_settings(self) -> dict[str, str]:
        return self._settings.public_summary()

    def list_tools(self) -> dict[str, Any]:
        tools = []
        for registered in self._runtime_registry.list_tools():
            spec = registered.spec
            tools.append(
                {
                    "id": spec.id,
                    "name": spec.name,
                    "version": spec.version,
                    "description": spec.description,
                    "permissions": sorted(action.value for action in spec.permissions),
                    "timeout_seconds": spec.timeout_seconds,
                    "network_required": spec.network_required,
                    "provenance": spec.provenance,
                }
            )
        tools.sort(key=lambda item: item["id"])
        return {"tools": tools, "total": len(tools)}

    def providers(self) -> dict[str, Any]:
        settings = self._settings
        configured = {name: True for name in settings.providers_with_credentials}
        router = build_model_router(settings)
        capabilities = {
            row["provider"]: row for row in capability_catalog(settings, router)
        }
        providers: list[dict[str, Any]] = []
        for name in settings.provider_order:
            capability = capabilities.get(name, {})
            providers.append(
                {
                    "name": name,
                    "credential_configured": bool(configured.get(name)),
                    "model": capability.get("model"),
                    "in_route": capability.get("in_route", False),
                    "local": capability.get("local", False),
                    "streaming": capability.get("streaming", False),
                    "vision": capability.get("vision", False),
                    "context_tokens": capability.get("context_tokens"),
                    "priced": capability.get("priced", False),
                }
            )
        return {
            "order": list(settings.provider_order),
            "strategy": settings.provider_strategy,
            "route": list(router.provider_names),
            "providers": providers,
        }

    # -- provider platform (Transformation Phase 7) ----------------------------
    def provider_capabilities(self) -> dict[str, Any]:
        router = build_model_router(self._settings)
        return {
            "capabilities": capability_catalog(self._settings, router),
            "route": route_explanation(self._settings, router),
        }

    def provider_usage(self) -> dict[str, Any]:
        stats = usage_stats(load_usage_records(self._settings))
        return {
            "analytics_enabled": self._settings.provider_analytics_enabled,
            "usage": stats,
        }

    def provider_evaluations(self) -> dict[str, Any]:
        return {"evaluation": load_latest_evaluation(self._settings)}

    def run_provider_evaluations(self) -> dict[str, Any]:
        router = build_model_router(self._settings)
        return run_evaluations(self._settings, router)

    def stream_chat(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        max_output_tokens: int = 512,
    ) -> Iterator[dict[str, Any]]:
        """Stream one chat completion as event dicts (delta/done/error).

        This is a direct model call (like ``pc-agent ask``): it cannot
        run tools, touch files, or approve anything. Usage is metered
        by the router observer like every other completion.
        """
        text = (prompt or "").strip()
        if not text:
            raise PlanningError("A non-empty prompt is required.")
        if len(text) > 8000:
            raise PlanningError("Prompt exceeds the 8000-character streaming limit.")
        bounded_tokens = max(16, min(int(max_output_tokens), 2048))
        router = build_model_router(self._settings)
        request = CompletionRequest.from_prompt(
            text,
            system_instruction=system_instruction,
            max_output_tokens=bounded_tokens,
        )
        return self._stream_chat_events(router, request)

    @staticmethod
    def _stream_chat_events(
        router: Any, request: CompletionRequest
    ) -> Iterator[dict[str, Any]]:
        yield {"type": "start", "route": list(router.provider_names)}
        chars = 0
        try:
            for delta in router.stream_complete(request):
                chars += len(delta)
                yield {"type": "delta", "text": delta}
        except ProviderError as exc:
            yield {"type": "error", "message": redact_text(str(exc))[:300]}
            return
        yield {"type": "done", "chars": chars}

    # -- knowledge base & Memory v2 (Transformation Phase 8) -------------------
    def _knowledge(self) -> KnowledgeService:
        return KnowledgeService(self._settings, self._database)

    def knowledge_documents(self) -> dict[str, Any]:
        return self._knowledge().list_documents()

    def knowledge_ingest(self, body: dict[str, Any]) -> dict[str, Any]:
        service = self._knowledge()
        try:
            raw_path = str(body.get("path") or "").strip()
            if raw_path:
                candidate = Path(raw_path).expanduser()
                if not candidate.is_absolute():
                    candidate = self._settings.workspace_dir / candidate
                target = candidate.resolve()
                workspace = self._settings.workspace_dir.resolve()
                if not target.is_relative_to(workspace):
                    raise PlanningError(
                        "API ingestion is limited to files inside the workspace."
                    )
                return service.ingest_file(target)
            title = str(body.get("title") or "Untitled")
            text = str(body.get("text") or "")
            return service.ingest_text(title=title, text=text)
        except KnowledgeError as exc:
            raise PlanningError(str(exc)) from exc

    def knowledge_remove(self, document_id: int) -> dict[str, Any]:
        try:
            return self._knowledge().remove_document(document_id)
        except KnowledgeError as exc:
            raise PlanningError(str(exc)) from exc

    def knowledge_search(self, query: str, *, limit: int = 5) -> dict[str, Any]:
        try:
            return self._knowledge().search(query, limit=limit)
        except KnowledgeError as exc:
            raise PlanningError(str(exc)) from exc

    def knowledge_ask(self, question: str) -> dict[str, Any]:
        try:
            return self._knowledge().ask(question)
        except KnowledgeError as exc:
            raise PlanningError(str(exc)) from exc

    def knowledge_stats(self) -> dict[str, Any]:
        return {"knowledge": self._knowledge().stats()}

    def memory_search(self, query: str, *, limit: int = 5) -> dict[str, Any]:
        try:
            return self._knowledge().search_memories(query, limit=limit)
        except KnowledgeError as exc:
            raise PlanningError(str(exc)) from exc

    # -- Agent Studio & multi-agent runtime (Transformation Phase 9) ---------
    # -- Automation ledger + local vision (Transformation Phase 11) ----------
    def _automation(self) -> Any:
        from .storage.repositories import AutomationRepository

        return AutomationRepository(self._database)

    def _automation_context(self) -> Any:
        from .tools.registry import ToolContext

        def recorder(payload: dict[str, Any]) -> None:
            try:
                self._automation().record(**payload)
            except Exception:  # noqa: BLE001 - auditing must not gate the action
                return

        return ToolContext.for_workspace(
            self._settings.workspace_dir,
            network_access_allowed=False,
            allow_private_network=False,
            web_timeout_seconds=self._settings.web_timeout_seconds,
            web_max_bytes=self._settings.web_max_bytes,
            browser_allowed=self._settings.allow_browser,
            browser_port=self._settings.browser_port,
            browser_timeout_seconds=self._settings.browser_timeout_seconds,
            desktop_automation_allowed=self._settings.allow_desktop_automation,
            desktop_automation_timeout_seconds=(
                self._settings.desktop_automation_timeout_seconds
            ),
            vision_allowed=self._settings.allow_vision,
            vision_max_image_bytes=self._settings.vision_max_image_bytes,
            automation_recorder=recorder,
        )

    def _invoke_automation(
        self, tool_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        from .errors import ToolError

        result = self._registry.invoke(tool_id, arguments, self._automation_context())
        if not result.success:
            raise PlanningError(result.error or f"{tool_id} failed.")
        try:
            return dict(result.output or {})
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            raise ToolError(f"{tool_id} returned an unusable result.") from exc

    def automation_capabilities(self) -> dict[str, Any]:
        from .browser_cdp import probe_browser
        from .desktop_automation import probe_desktop_automation

        version = probe_browser(port=self._settings.browser_port)
        return {
            "browser": {
                "enabled": bool(self._settings.allow_browser),
                "port": self._settings.browser_port,
                "connected": version is not None,
                "browser": str(version.get("Browser") or "") if version else "",
            },
            "desktop": {
                "enabled": bool(self._settings.allow_desktop_automation),
                **probe_desktop_automation(),
            },
            "vision": {
                "local_analysis": True,
                "provider_vision_enabled": bool(self._settings.allow_vision),
                "max_image_bytes": self._settings.vision_max_image_bytes,
            },
            "tools": [
                tool.spec.id
                for tool in self._registry.list_tools()
                if tool.spec.id.startswith(("browser.", "desktop.", "vision."))
            ],
            "ledger": self._automation().summary(),
        }

    def automation_runs(
        self, *, kind: str = "", status: str = "", limit: int = 50
    ) -> dict[str, Any]:
        runs = self._automation().list_runs(kind=kind, status=status, limit=limit)
        return {
            "runs": [
                {
                    "id": run.id,
                    "kind": run.kind,
                    "tool_id": run.tool_id,
                    "action": run.action,
                    "target": run.target,
                    "status": run.status,
                    "detail": run.detail,
                    "artifact_path": run.artifact_path,
                    "duration_ms": run.duration_ms,
                    "allowed": run.allowed,
                    "created_at": run.created_at.isoformat(),
                }
                for run in runs
            ],
            "count": len(runs),
        }

    def automation_stats(self) -> dict[str, Any]:
        return {"automation": self._automation().summary()}

    def vision_screen(self, body: dict[str, Any]) -> dict[str, Any]:
        arguments: dict[str, Any] = {"grid": int(body.get("grid") or 4)}
        raw = str(body.get("path") or "").strip()
        if raw:
            arguments["path"] = raw
        return self._invoke_automation("vision.screen", arguments)

    def vision_locate(self, body: dict[str, Any]) -> dict[str, Any]:
        screen = str(body.get("screen_path") or "").strip()
        template = str(body.get("template_path") or "").strip()
        if not screen or not template:
            raise PlanningError("screen_path and template_path are both required.")
        arguments: dict[str, Any] = {
            "screen_path": screen,
            "template_path": template,
        }
        if body.get("threshold") is not None:
            arguments["threshold"] = float(body["threshold"])
        return self._invoke_automation("vision.locate", arguments)

    def browser_wait(self, body: dict[str, Any]) -> dict[str, Any]:
        arguments = {
            key: body[key]
            for key in ("selector", "text", "url_contains", "timeout_seconds")
            if body.get(key) not in (None, "")
        }
        if not arguments:
            raise PlanningError("browser wait needs selector, text or url_contains.")
        return self._invoke_automation("browser.wait", arguments)

    def browser_tabs(self, body: dict[str, Any]) -> dict[str, Any]:
        arguments = {
            key: body[key] for key in ("action", "target_id") if body.get(key) not in (None, "")
        }
        return self._invoke_automation("browser.tabs", arguments)

    def desktop_window_state(self, body: dict[str, Any]) -> dict[str, Any]:
        arguments = {
            key: body[key]
            for key in (
                "state",
                "bounds",
                "window_handle",
                "window_title",
                "window_title_contains",
                "window_class_name",
            )
            if body.get(key) not in (None, "")
        }
        if not arguments.get("state"):
            raise PlanningError("A 'state' is required (minimize/maximize/restore/move/resize).")
        return self._invoke_automation("desktop.window_state", arguments)

    # -- Visual Workflow Builder (Transformation Phase 12) -------------------
    def _flow_store(self) -> Any:
        from .flows import FlowStore

        return FlowStore(self._database)

    def _start_flow_mission(
        self,
        name: str,
        *,
        description: str = "",
        task_specs: list[dict[str, Any]] | None = None,
        source: str = "flow",
    ) -> dict[str, Any]:
        mission = self.create_mission(
            name, description=description, task_specs=task_specs, source=source
        )
        self.start_mission(mission["id"], source=source)
        return mission

    def _flow_runner(self) -> Any:
        from .flows import FlowRunner

        return FlowRunner(
            store=self._flow_store(),
            start_mission=self._start_flow_mission,
            emit=self._emit,
            mission_state=self._flow_mission_state,
        )

    def _flow_mission_state(self, mission_id: str) -> str | None:
        missions = getattr(self, "_missions", None)
        if missions is None:
            return None
        mission = missions.get(mission_id)
        return None if mission is None else str(mission.status)

    def resolve_flow_id(self, reference: str) -> str:
        store = self._flow_store()
        text = str(reference or "").strip()
        if not text:
            raise PlanningError("A flow id or name is required.")
        flow = store.get(text)
        if flow is not None:
            return flow.id
        by_name = store.find_by_name(text)
        if by_name is not None:
            return by_name.id
        matches = [item.id for item in store.list_flows(limit=500) if item.id.startswith(text)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise PlanningError(f"Flow reference '{text}' is ambiguous.")
        raise PlanningError(f"Flow not found: {text}")

    def list_flows(self, *, limit: int = 100) -> dict[str, Any]:
        store = self._flow_store()
        flows = store.list_flows(limit=limit)
        return {
            "flows": [flow.to_dict() for flow in flows],
            "total": len(flows),
            "summary": store.summary(),
        }

    def get_flow(self, reference: str, *, include_runs: bool = True) -> dict[str, Any]:
        store = self._flow_store()
        flow = store.get(self.resolve_flow_id(reference))
        assert flow is not None
        payload = flow.to_dict()
        payload["versions"] = store.versions(flow.id)
        payload["triggers"] = [item.to_dict() for item in store.triggers(flow.id)]
        if include_runs:
            payload["runs"] = [run.to_dict() for run in store.list_runs(flow_id=flow.id, limit=20)]
        return payload

    def validate_flow(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .flows import validate_definition

        definition = validate_definition(payload)
        return {
            "valid": True,
            "name": definition.name,
            "levels": definition.topological_levels(),
            "nodes": [node.to_dict() for node in definition.nodes],
            "task_specs": definition.mission_specs(),
        }

    def create_flow(self, payload: dict[str, Any], *, source: str = "api") -> dict[str, Any]:
        from .flows import validate_definition

        store = self._flow_store()
        definition = validate_definition(payload)
        if store.find_by_name(definition.name) is not None:
            raise PlanningError(f"A flow named '{definition.name}' already exists.")
        flow = store.create(definition)
        self._emit(
            "flow.created",
            source=source,
            summary=f"Flow '{flow.name}' created ({len(flow.definition.nodes)} nodes).",
            payload={"flow_id": flow.id},
        )
        return flow.to_dict()

    def update_flow(
        self, reference: str, payload: dict[str, Any], *, source: str = "api"
    ) -> dict[str, Any]:
        from .flows import validate_definition

        store = self._flow_store()
        flow_id = self.resolve_flow_id(reference)
        clash = store.find_by_name(str(payload.get("name") or ""))
        if clash is not None and clash.id != flow_id:
            raise PlanningError(f"A flow named '{clash.name}' already exists.")
        flow = store.update(flow_id, validate_definition(payload))
        self._emit(
            "flow.updated",
            source=source,
            summary=f"Flow '{flow.name}' updated to v{flow.version}.",
            payload={"flow_id": flow.id, "version": flow.version},
        )
        return flow.to_dict()

    def set_flow_enabled(
        self, reference: str, *, enabled: bool, source: str = "api"
    ) -> dict[str, Any]:
        store = self._flow_store()
        flow = store.set_enabled(self.resolve_flow_id(reference), enabled)
        self._emit(
            "flow.enabled" if enabled else "flow.disabled",
            source=source,
            summary=f"Flow '{flow.name}' {'enabled' if enabled else 'disabled'}.",
            payload={"flow_id": flow.id},
        )
        return flow.to_dict()

    def delete_flow(self, reference: str, *, source: str = "api") -> dict[str, Any]:
        store = self._flow_store()
        flow_id = self.resolve_flow_id(reference)
        deleted = store.delete(flow_id)
        if not deleted:
            raise PlanningError(f"Flow not found: {reference}")
        self._emit(
            "flow.deleted",
            source=source,
            summary=f"Flow deleted: {reference}",
            payload={"flow_id": flow_id},
        )
        return {"flow_id": flow_id, "deleted": True}

    def export_flow(self, reference: str) -> dict[str, Any]:
        from . import __version__
        from .flows import export_payload

        store = self._flow_store()
        flow = store.get(self.resolve_flow_id(reference))
        assert flow is not None
        triggers = [
            item
            for item in (record.to_dict() for record in store.triggers(flow.id))
            if item["kind"] != "manual"
        ]
        reduced = [
            {
                key: item[key]
                for key in ("kind", "interval_seconds", "run_at", "event_type", "enabled")
                if item.get(key) is not None
            }
            for item in triggers
        ]
        return export_payload(flow.definition, triggers=reduced, version=__version__)

    def import_flow(
        self,
        payload: dict[str, Any],
        *,
        overwrite: bool = False,
        source: str = "import",
    ) -> dict[str, Any]:
        from datetime import datetime as _datetime

        from .flows import load_export_payload

        store = self._flow_store()
        definition, triggers = load_export_payload(payload)
        existing = store.find_by_name(definition.name)
        if existing is not None and not overwrite:
            raise PlanningError(
                f"A flow named '{definition.name}' already exists "
                "(pass --overwrite to replace it)."
            )
        flow = store.update(existing.id, definition) if existing else store.create(definition)
        added_triggers: list[dict[str, Any]] = []
        for spec in triggers:
            kind = str(spec.get("kind") or "").strip()
            if kind == "manual":
                continue
            run_at = None
            raw_run_at = spec.get("run_at")
            if raw_run_at:
                try:
                    run_at = _datetime.fromisoformat(str(raw_run_at))
                except ValueError as exc:
                    raise PlanningError(f"Invalid run_at in the imported trigger: {raw_run_at}") from exc
            added_triggers.append(
                store.add_trigger(
                    flow.id,
                    kind=kind,
                    interval_seconds=spec.get("interval_seconds"),
                    run_at=run_at,
                    event_type=str(spec.get("event_type") or ""),
                    enabled=bool(spec.get("enabled", True)),
                ).to_dict()
            )
        self._emit(
            "flow.imported",
            source=source,
            summary=f"Flow '{flow.name}' imported as v{flow.version}.",
            payload={"flow_id": flow.id, "triggers": len(added_triggers)},
        )
        return {
            "flow": flow.to_dict(),
            "id": flow.id,
            "name": flow.name,
            "version": flow.version,
            "triggers": added_triggers,
            "trigger_count": len(added_triggers),
        }

    def run_flow(self, reference: str, *, source: str = "api") -> dict[str, Any]:
        result = self._flow_runner().run_flow(
            self.resolve_flow_id(reference), source=source
        )
        return result

    def flow_runs(self, reference: str | None = None, *, limit: int = 50) -> dict[str, Any]:
        store = self._flow_store()
        flow_id = self.resolve_flow_id(reference) if reference else None
        runs = store.list_runs(flow_id=flow_id, limit=limit)
        return {
            "runs": [run.to_dict() for run in runs],
            "total": len(runs),
            "count": len(runs),
        }

    def flow_add_trigger(
        self, reference: str, spec: dict[str, Any], *, source: str = "api"
    ) -> dict[str, Any]:
        from datetime import datetime as _datetime

        store = self._flow_store()
        flow_id = self.resolve_flow_id(reference)
        run_at = None
        if spec.get("run_at"):
            try:
                run_at = _datetime.fromisoformat(str(spec["run_at"]))
            except ValueError as exc:
                raise PlanningError(f"Invalid run_at: {spec['run_at']}") from exc
        trigger = store.add_trigger(
            flow_id,
            kind=str(spec.get("kind") or "schedule"),
            interval_seconds=spec.get("interval_seconds"),
            run_at=run_at,
            event_type=str(spec.get("event_type") or ""),
            enabled=bool(spec.get("enabled", True)),
        )
        self._emit(
            "flow.trigger_added",
            source=source,
            summary=f"Trigger {trigger.kind} added to flow {flow_id[:8]}.",
            payload={"flow_id": flow_id, "trigger_id": trigger.id},
        )
        return trigger.to_dict()

    def flow_triggers(self, reference: str | None = None) -> dict[str, Any]:
        store = self._flow_store()
        flow_id = self.resolve_flow_id(reference) if reference else None
        triggers = store.triggers(flow_id)
        return {
            "triggers": [item.to_dict() for item in triggers],
            "total": len(triggers),
            "count": len(triggers),
        }

    def flow_set_trigger_enabled(
        self, trigger_id: str, *, enabled: bool, source: str = "api"
    ) -> dict[str, Any]:
        trigger = self._flow_store().set_trigger_enabled(trigger_id, enabled)
        self._emit(
            "flow.trigger_updated",
            source=source,
            summary=f"Trigger {trigger.id[:8]} {'enabled' if enabled else 'disabled'}.",
            payload={"trigger_id": trigger.id},
        )
        return trigger.to_dict()

    def flow_delete_trigger(self, trigger_id: str, *, source: str = "api") -> dict[str, Any]:
        deleted = self._flow_store().delete_trigger(trigger_id)
        if not deleted:
            raise PlanningError(f"Trigger not found: {trigger_id}")
        self._emit(
            "flow.trigger_deleted",
            source=source,
            summary=f"Trigger deleted: {trigger_id}",
            payload={"trigger_id": trigger_id},
        )
        return {"trigger_id": trigger_id, "deleted": True}

    def flow_tick(self, *, limit: int = 10, source: str = "scheduler") -> dict[str, Any]:
        result = self._flow_runner().tick(limit=limit)
        if result["total"]:
            self._emit(
                "flow.tick",
                source=source,
                summary=f"{result['total']} flow trigger(s) fired.",
                payload={"fired": [item.get("run_id") for item in result["fired"]]},
            )
        return result

    def flow_stats(self) -> dict[str, Any]:
        return {"flows": self._flow_store().summary()}

    # -- Proactive intelligence (Transformation Phase 14) -------------------
    def _proactive_outbound(self, title: str, body: str, severity: str) -> dict[str, Any]:
        """Send a proactive notification through the chat connector, if allowed.

        The gate order is deliberate: the proactive outbound flag, then the
        connector master gate, then the configured chat endpoint. Any refusal is
        returned as data so the in-app copy is never lost.
        """

        from .errors import ConnectorError

        try:
            payload = self._connectors().chat_send(
                f"[{severity}] {title}" + (f"\n{body}" if body else ""), target="proactive"
            )
        except ConnectorError as exc:
            return {"ok": False, "detail": str(exc)}
        return {"ok": bool(payload["result"]["ok"]), "detail": payload["result"]["detail"]}

    def _proactive(self) -> Any:
        from .proactive import ProactiveEngine

        return ProactiveEngine(
            self._settings,
            self._database,
            events=self._events,
            outbound=self._proactive_outbound,
        )

    def proactive_status(self) -> dict[str, Any]:
        return self._proactive().status()

    def proactive_summary(self) -> dict[str, Any]:
        return self._proactive().summary()

    def proactive_tick(
        self, *, limit: int | None = None, source: str = "api"
    ) -> dict[str, Any]:
        result = self._proactive().tick(limit=limit)
        if result["total"]:
            self._emit(
                "proactive.tick",
                source=source,
                summary=f"{result['total']} proactive notification(s) delivered.",
                payload={"fired": result["total"], "deferred": len(result["deferred"])},
            )
        return result

    def add_reminder(
        self,
        *,
        title: str,
        due: str,
        note: str = "",
        repeat_seconds: int | None = None,
        severity: str = "normal",
        source: str = "api",
    ) -> dict[str, Any]:
        payload = self._proactive().add_reminder(
            title=title,
            due=due,
            note=note,
            repeat_seconds=repeat_seconds,
            severity=severity,
            source=source,
        )
        self._emit(
            "reminder.created",
            source=source,
            summary=f"Reminder '{payload['title']}' scheduled for {payload['due_at']}.",
            payload={"reminder_id": payload["id"]},
        )
        return payload

    def reminders(self, *, status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return self._proactive().reminders(status=status, limit=limit)

    def resolve_reminder(self, reference: str, *, source: str = "api") -> dict[str, Any]:
        return self._proactive().resolve_reminder(reference)

    def dismiss_reminder(self, reference: str) -> dict[str, Any]:
        return self._proactive().dismiss_reminder(reference)

    def snooze_reminder(self, reference: str, *, for_spec: str = "+15m") -> dict[str, Any]:
        return self._proactive().snooze_reminder(reference, for_spec=for_spec)

    def enable_reminder(self, reference: str, *, enabled: bool) -> dict[str, Any]:
        return self._proactive().enable_reminder(reference, enabled=enabled)

    def delete_reminder(self, reference: str) -> dict[str, Any]:
        return self._proactive().delete_reminder(reference)

    def notifications(self, *, limit: int = 25, kind: str | None = None) -> dict[str, Any]:
        return self._proactive().notifications(limit=limit, kind=kind)

    def clear_notifications(self) -> dict[str, Any]:
        return self._proactive().clear_notifications()

    def run_briefing(self, *, force: bool = False, source: str = "api") -> dict[str, Any]:
        payload = self._proactive().run_briefing(force=force)
        self._emit(
            "briefing.generated",
            source=source,
            summary=f"Daily briefing generated ({payload['briefing']['item_count']} item(s)).",
            payload={"briefing_id": payload["briefing"]["id"]},
        )
        return payload

    def briefings(self, *, limit: int = 10) -> dict[str, Any]:
        return self._proactive().briefings(limit=limit)

    def notify_user(
        self,
        *,
        title: str,
        body: str = "",
        kind: str = "general",
        severity: str = "normal",
        source: str = "api",
    ) -> dict[str, Any]:
        return self._proactive().notify(
            title=title, body=body, kind=kind, severity=severity
        )

    # -- Connectors (Transformation Phase 13) --------------------------------
    # -- interface / accessibility (Transformation Phase 17) ----------------
    def i18n_status(self) -> dict[str, Any]:
        from .i18n import Translator, locale_rows

        translator = Translator(self._settings.ui_locale)
        return {
            "locale": translator.locale,
            "name": translator.language_name,
            "direction": translator.direction,
            "rtl": translator.is_rtl,
            "coverage": translator.coverage(),
            "notice": translator.notice(),
            "locales": locale_rows(),
        }

    def i18n_messages(self, locale: str | None = None) -> dict[str, Any]:
        from .i18n import MESSAGES, Translator

        translator = Translator(locale or self._settings.ui_locale)
        return {
            "locale": translator.locale,
            "direction": translator.direction,
            "messages": dict(MESSAGES[translator.locale]),
        }

    def i18n_set(
        self,
        *,
        locale: str | None = None,
        scale: float | None = None,
        high_contrast: bool | None = None,
        reduce_motion: bool | None = None,
        cli_color: str | None = None,
    ) -> dict[str, Any]:
        """Persist an interface choice to ``data/ui.json`` (env still wins)."""

        from .a11y import resolve_ui
        from .errors import ConfigError
        from .i18n import normalize_locale
        from .ui_prefs import save_prefs

        current = resolve_ui(self._settings)
        values: dict[str, Any] = {}
        if locale is not None:
            values["locale"] = normalize_locale(locale)
        if scale is not None:
            value = float(scale)
            if not 0.8 <= value <= 2.0:
                raise ConfigError("Text size must be between 0.8 and 2.0 (80%-200%).")
            values["scale"] = round(value, 2)
        if high_contrast is not None:
            values["high_contrast"] = bool(high_contrast)
        if reduce_motion is not None:
            values["reduce_motion"] = bool(reduce_motion)
        if cli_color is not None:
            color = str(cli_color).strip().lower()
            if color not in {"auto", "always", "never"}:
                raise ConfigError("CLI colour must be auto, always or never.")
            values["cli_color"] = color
        if not values:
            raise ConfigError(
                "Nothing to change; pass a language, text size, contrast, motion "
                "or colour choice."
            )
        path = save_prefs(self._settings.data_dir, values)
        self._emit(
            "ui.preferences_saved",
            source="service",
            summary="Interface preferences saved: "
            + ", ".join(f"{key}={value}" for key, value in sorted(values.items())),
            payload={"changed": sorted(values), "file": path.name},
        )
        overridden = sorted(
            key
            for key in values
            if key == "locale" and os.environ.get("PC_AGENT_UI_LOCALE")
        )
        return {
            "saved": values,
            "previous": {
                "locale": current["locale"],
                "scale": current["scale"],
                "high_contrast": current["high_contrast"],
                "reduce_motion": current["reduce_motion"],
            },
            "path": str(path),
            "applies": "restart the desktop app to apply",
            "env_overrides": overridden,
        }

    def a11y_status(self) -> dict[str, Any]:
        from .a11y import a11y_status

        return a11y_status(self._settings)

    def perf_bench(self, *, iterations: int = 5) -> dict[str, Any]:
        """Measure real local costs - no invented numbers, no provider calls."""

        import platform as platform_module
        import sys
        import time

        from .a11y import a11y_status
        from .storage.db import LATEST_SCHEMA_VERSION

        runs = max(1, min(int(iterations), 20))

        def measure(action: Any) -> float:
            best = float("inf")
            for _ in range(runs):
                started = time.perf_counter()
                action()
                best = min(best, (time.perf_counter() - started) * 1000.0)
            return round(best, 3)

        started = time.perf_counter()
        self.health()
        first_health = round((time.perf_counter() - started) * 1000.0, 3)
        return {
            "iterations": runs,
            "python": sys.version.split()[0],
            "platform": platform_module.platform(),
            "measurements_ms": {
                "health_first_call": first_health,
                "health_warm": measure(self.health),
                "events_list": measure(lambda: self.list_events(limit=20)),
                "a11y_status": measure(lambda: a11y_status(self._settings)),
                "system_status": measure(self.system_status),
            },
            "schema": LATEST_SCHEMA_VERSION,
            "notes": (
                "single-process wall-clock timings on this machine; provider calls, "
                "GUI paint time and disk cold-start are not included"
            ),
        }

    def _sync(self) -> Any:
        from .sync import SyncService

        return SyncService(self._settings, self._database, events=self._events)

    def sync_status(self) -> dict[str, Any]:
        return self._sync().status()

    def sync_summary(self) -> dict[str, Any]:
        return self._sync().summary()

    def sync_identity(self) -> dict[str, Any]:
        return self._sync().identity().public_document()

    def sync_peers(self) -> dict[str, Any]:
        return self._sync().peers()

    def sync_pair(
        self,
        *,
        name: str,
        sign_public: str,
        encrypt_public: str,
        confirm: str | None = None,
    ) -> dict[str, Any]:
        return self._sync().pair(
            name=name,
            sign_public=sign_public,
            encrypt_public=encrypt_public,
            confirm=confirm,
        )

    def sync_unpair(self, device: str) -> dict[str, Any]:
        return self._sync().unpair(device)

    def sync_set_peer_enabled(self, device: str, *, enabled: bool) -> dict[str, Any]:
        return self._sync().set_peer_enabled(device, enabled=enabled)

    def sync_export(self, *, path: str | None = None) -> dict[str, Any]:
        result = self._sync().export(path=path)
        self._emit(
            "sync.export",
            source="service",
            summary=f"Exported {result['records']} record(s) for {len(result['recipients'])} peer(s).",
            payload={"bundle": result["bundle_id"], "records": result["records"]},
        )
        return result

    def sync_import(self, path: str) -> dict[str, Any]:
        result = self._sync().import_file(path)
        self._emit(
            "sync.import",
            source="service",
            summary=(
                f"Imported sync bundle: applied {result['applied']}, "
                f"skipped {result['skipped']}."
            ),
            payload={"bundle": result["bundle_id"], "from": result["from"]},
        )
        return result

    def sync_tick(self) -> dict[str, Any]:
        return self._sync().tick()

    def sync_bundles(self, *, direction: str | None = None, limit: int = 25) -> dict[str, Any]:
        return self._sync().bundles(direction=direction, limit=limit)

    def sync_requests(self, *, status: str | None = None, limit: int = 25) -> dict[str, Any]:
        return self._sync().requests(status=status, limit=limit)

    def sync_request(
        self,
        kind: str,
        *,
        title: str,
        body: str = "",
        due: str = "",
        severity: str = "normal",
        path: str | None = None,
    ) -> dict[str, Any]:
        return self._sync().request(
            kind, title=title, body=body, due=due, severity=severity, path=path
        )

    def sync_decide(self, request_id: str, *, accept: bool, note: str = "") -> dict[str, Any]:
        return self._sync().decide(request_id, accept=accept, note=note)

    def _plugins(self) -> Any:
        from .plugins import PluginService

        return PluginService(self._settings, self._database, events=self._events)

    def plugin_status(self) -> dict[str, Any]:
        return self._plugins().status()

    def plugin_summary(self) -> dict[str, Any]:
        return self._plugins().summary()

    def plugins(self) -> dict[str, Any]:
        return self._plugins().list_plugins()

    def plugin_show(self, plugin_id: str) -> dict[str, Any]:
        return self._plugins().show(plugin_id)

    def plugin_verify(self, bundle: str) -> dict[str, Any]:
        return self._plugins().verify_bundle(bundle)

    def plugin_install(self, bundle: str) -> dict[str, Any]:
        installed = self._plugins().install(bundle)
        self._emit(
            "plugin.installed",
            source="service",
            summary=f"Plugin '{installed.get('name', installed.get('id'))}' installed.",
            payload={"plugin": installed.get("id"), "version": installed.get("version")},
        )
        return installed

    def plugin_set_enabled(self, plugin_id: str, *, enabled: bool) -> dict[str, Any]:
        return self._plugins().set_enabled(plugin_id, enabled=enabled)

    def plugin_uninstall(self, plugin_id: str) -> dict[str, Any]:
        return self._plugins().uninstall(plugin_id)

    def plugin_rollback(self, plugin_id: str, *, version: str | None = None) -> dict[str, Any]:
        return self._plugins().rollback(plugin_id, version=version)

    def plugin_run_tool(
        self,
        plugin_id: str,
        tool: str,
        *,
        values: dict[str, Any] | None = None,
        confirm: str | None = None,
    ) -> dict[str, Any]:
        result = self._plugins().run_tool(
            plugin_id, tool, values=values, confirm=confirm
        )
        self._emit(
            "plugin.run",
            source="service",
            summary=f"Plugin tool '{tool}' {result['status']}.",
            payload={
                "plugin": plugin_id,
                "tool": tool,
                "status": result["status"],
                "duration_ms": result["duration_ms"],
            },
        )
        return result

    def plugin_skills(self, plugin_id: str) -> dict[str, Any]:
        return self._plugins().skills(plugin_id)

    def plugin_runs(self, *, plugin_id: str | None = None, limit: int = 25) -> dict[str, Any]:
        return self._plugins().runs(plugin_id=plugin_id, limit=limit)

    def _connectors(self) -> Any:
        from .connectors.service import ConnectorService

        return ConnectorService(self._settings, self._database, events=self._events)

    def connector_status(self, *, probe: bool = False) -> dict[str, Any]:
        return self._connectors().status(probe=probe)

    def connector_summary(self) -> dict[str, Any]:
        return self._connectors().summary()

    def connector_events(
        self, *, connector: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        return self._connectors().events(connector=connector, limit=limit)

    def connector_deliveries(
        self, *, connector: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        return self._connectors().deliveries(connector=connector, limit=limit)

    def connector_calendar_events(
        self, *, days_ahead: int = 7, limit: int | None = None
    ) -> dict[str, Any]:
        return self._connectors().list_calendar_events(days_ahead=days_ahead, limit=limit)

    def connector_calendar_add(
        self,
        *,
        summary: str,
        start: str,
        duration_minutes: int = 30,
        location: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        return self._connectors().add_calendar_event(
            summary=summary,
            start=start,
            duration_minutes=duration_minutes,
            location=location,
            description=description,
        )

    def connector_drive_sync(self) -> dict[str, Any]:
        return self._connectors().drive_sync()

    def connector_mail_inbox(self, *, limit: int = 10) -> dict[str, Any]:
        return self._connectors().mail_inbox(limit=limit)

    def connector_mail_send(
        self,
        *,
        to: str | list[str],
        subject: str,
        body: str,
        confirm: str = "",
    ) -> dict[str, Any]:
        return self._connectors().mail_send(to=to, subject=subject, body=body, confirm=confirm)

    def connector_chat_send(self, text: str, *, target: str = "") -> dict[str, Any]:
        return self._connectors().chat_send(text, target=target)

    def connector_webhook_receive(
        self, body: bytes, *, timestamp: str, signature: str
    ) -> dict[str, Any]:
        return self._connectors().receive_webhook(
            body, timestamp=timestamp, signature=signature
        )

    # -- Voice (Transformation Phase 10) -------------------------------------
    def _voice(self) -> Any:
        from .voice import VoiceService

        return VoiceService(
            self._settings,
            self._database,
            events=self._events,
            emergency_stop=self._emergency_stop,
        )

    def voice_status(self) -> dict[str, Any]:
        return self._voice().status().to_dict()

    def voice_devices(self) -> dict[str, Any]:
        return self._voice().devices()

    def voice_privacy(self) -> dict[str, Any]:
        return self._voice().privacy()

    def voice_enable(self, enabled: bool) -> dict[str, Any]:
        service = self._voice()
        return service.enable() if enabled else service.disable()

    def voice_wake_word(self, enabled: bool) -> dict[str, Any]:
        try:
            return self._voice().set_wake_word(enabled)
        except VoiceError as exc:
            raise PlanningError(str(exc)) from exc

    def voice_capture_start(self, mode: str = "push_to_talk") -> dict[str, Any]:
        try:
            return self._voice().start_capture(mode)
        except VoiceError as exc:
            raise PlanningError(str(exc)) from exc

    def voice_capture_stop(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = body or {}
        try:
            return self._voice().stop_capture(
                cloud_consent=bool(payload.get("cloud_consent"))
            )
        except VoiceError as exc:
            raise PlanningError(str(exc)) from exc

    def voice_capture_cancel(self) -> dict[str, Any]:
        return self._voice().cancel_capture()

    def voice_intent(self, body: dict[str, Any]) -> dict[str, Any]:
        """Parse a sentence into an intent and optionally perform it."""

        from .voice.intents import VoiceIntentExecutor, parse_intent

        text = str(body.get("text") or "").strip()
        if not text:
            raise PlanningError("A 'text' field is required.")
        wake_word = str(body.get("wake_word") or self._settings.voice_wake_word or "")
        intent = parse_intent(text, wake_word=wake_word)
        payload: dict[str, Any] = {"intent": intent.to_dict()}
        if bool(body.get("execute")):
            executor = VoiceIntentExecutor(self._settings, self._database)
            payload["outcome"] = executor.execute(intent).to_dict()
        return payload

    def voice_acceptance(self) -> dict[str, Any]:
        from . import __version__
        from .voice.acceptance import run_voice_acceptance

        return run_voice_acceptance(self._voice(), version=__version__).to_dict()

    def voice_speak(self, body: dict[str, Any]) -> dict[str, Any]:
        text = str(body.get("text") or "").strip()
        if not text:
            raise PlanningError("A 'text' field is required to speak.")
        out = str(body.get("output_path") or "").strip()
        target = Path(out).expanduser() if out else None
        if target is not None and not target.is_absolute():
            target = self._settings.workspace_dir / target
        try:
            return self._voice().speak(text, output_path=target)
        except VoiceError as exc:
            raise PlanningError(str(exc)) from exc

    def voice_transcribe(self, body: dict[str, Any]) -> dict[str, Any]:
        raw = str(body.get("path") or "").strip()
        if not raw:
            raise PlanningError("A 'path' field is required to transcribe.")
        try:
            return self._voice().transcribe_file(
                Path(raw), cloud_consent=bool(body.get("cloud_consent"))
            )
        except VoiceError as exc:
            raise PlanningError(str(exc)) from exc

    def voice_sessions(self, limit: int = 20) -> dict[str, Any]:
        service = self._voice()
        return {
            "sessions": service.sessions(limit),
            "summary": service.status().sessions,
        }

    def voice_purge_audio(self) -> dict[str, Any]:
        return self._voice().purge_audio()

    def voice_clear_history(self) -> dict[str, Any]:
        return self._voice().clear_history()

    def _studio(self) -> AgentStudio:
        return AgentStudio(
            self._settings,
            self._database,
            emit=lambda event_type, *, source, summary, payload=None: self._emit(
                event_type, source=source, summary=summary, payload=payload
            ),
        )

    def agents_list(self) -> dict[str, Any]:
        return self._studio().list_agents()

    def agent_get(self, name: str) -> dict[str, Any]:
        try:
            return self._studio().get_agent(name)
        except AgentError as exc:
            raise PlanningError(str(exc)) from exc

    def agent_save(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._studio().save_agent(body)
        except AgentError as exc:
            raise PlanningError(str(exc)) from exc

    def agent_delete(self, name: str) -> dict[str, Any]:
        try:
            return self._studio().delete_agent(name)
        except AgentError as exc:
            raise PlanningError(str(exc)) from exc

    def agent_stats(self) -> dict[str, Any]:
        return {"agents": self._studio().stats()}

    def agent_runs_list(
        self, *, agent_name: str = "", team_run_id: str = "", limit: int = 50
    ) -> dict[str, Any]:
        return self._studio().list_runs(
            agent_name=agent_name or None,
            team_run_id=team_run_id or None,
            limit=limit,
        )

    def agent_run_start(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        """Start one specialist run on a background thread (202-style)."""
        self._require_core_tasks()
        studio = self._studio()
        try:
            studio.get_agent(name)  # validate before accepting
        except AgentError as exc:
            raise PlanningError(str(exc)) from exc
        instruction = str(body.get("instruction") or "").strip()
        if not instruction:
            raise PlanningError("Field 'instruction' is required.")
        key = f"agent:{name}"
        with self._run_lock:
            existing = self._agent_threads.get(key)
            if existing is not None and existing.is_alive():
                raise PlanningError(f"Agent '{name}' already has a run in progress.")
            thread = threading.Thread(
                target=self._agent_run_thread,
                args=(key, name, instruction, bool(body.get("use_ai"))),
                name=f"local-service-agent-{name}",
                daemon=True,
            )
            self._agent_threads[key] = thread
            thread.start()
        return {"accepted": True, "agent": name, "instruction": instruction}

    def _agent_run_thread(
        self, key: str, name: str, instruction: str, use_ai: bool
    ) -> None:
        try:
            result = self._studio().run_agent(name, instruction, use_ai=use_ai)
            self._agent_results[key] = result
        except Exception:  # noqa: BLE001 - thread boundary; ledger/event carry it
            _LOG.exception("Agent run thread failed for %s", name)

    def agent_team_start(self, body: dict[str, Any]) -> dict[str, Any]:
        """Start a team run (explicit assignments or coordinator-proposed)."""
        self._require_core_tasks()
        goal = str(body.get("goal") or "").strip()
        if not goal:
            raise PlanningError("Field 'goal' is required.")
        assignments = body.get("assignments")
        if assignments is not None and not isinstance(assignments, list):
            raise PlanningError("Field 'assignments' must be a list when given.")
        team_run_id = uuid.uuid4().hex[:12]
        key = f"team:{team_run_id}"
        thread = threading.Thread(
            target=self._agent_team_thread,
            args=(key, goal, assignments, bool(body.get("use_ai")), team_run_id),
            name=f"local-service-team-{team_run_id}",
            daemon=True,
        )
        with self._run_lock:
            self._agent_threads[key] = thread
        thread.start()
        return {"accepted": True, "team_run_id": team_run_id, "goal": goal}

    def _agent_team_thread(
        self,
        key: str,
        goal: str,
        assignments: list[dict[str, Any]] | None,
        use_ai: bool,
        team_run_id: str,
    ) -> None:
        try:
            result = self._studio().run_team(
                goal, assignments, use_ai=use_ai, team_run_id=team_run_id
            )
            self._agent_results[key] = result
        except Exception:  # noqa: BLE001 - thread boundary; ledger/event carry it
            _LOG.exception("Team run thread %s failed", team_run_id)

    def agent_team_result(self, team_run_id: str) -> dict[str, Any]:
        key = f"team:{team_run_id}"
        thread = self._agent_threads.get(key)
        result = self._agent_results.get(key)
        return {
            "team_run_id": team_run_id,
            "running": bool(thread is not None and thread.is_alive()),
            "result": result,
            "runs": self.agent_runs_list(team_run_id=team_run_id)["runs"],
        }

    # -- lifecycle -------------------------------------------------------------
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for flag in self._cancel_flags.values():
            flag.set()
        for flag in self._mission_cancels.values():
            flag.set()
        for thread in list(self._threads.values()):
            thread.join(timeout=2)
        for thread in list(self._mission_threads.values()):
            thread.join(timeout=2)
        self._orchestrator._safety.close()  # Worker is ours to stop.

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- internals -------------------------------------------------------------
    def _emit(
        self,
        event_type: str,
        *,
        source: str,
        summary: str,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        try:
            self._events.record(
                event_type, source=source, summary=summary, task_id=task_id, payload=payload
            )
        except PcAgentError:
            _LOG.exception("Failed to record event %s", event_type)

    def _require_core_tasks(self) -> None:
        if self._settings.license_enforcement == "off":
            return
        self._license.require(FEATURE_CORE_TASKS, warn=lambda _message: None)


def _approval_dict(request: Any) -> dict[str, Any]:
    return {
        "request_key": request.request_key,
        "task_id": request.task_id,
        "tool_id": request.tool_id,
        "action": request.action,
        "arguments": dict(request.arguments),
        "message": request.message,
        "requested_by": request.requested_by,
        "status": request.status,
        "note": request.note,
        "decided_by": request.decided_by,
        "created_at": request.created_at.isoformat(),
        "expires_at": request.expires_at.isoformat(),
        "decided_at": request.decided_at.isoformat() if request.decided_at else None,
        "consumed_at": request.consumed_at.isoformat() if request.consumed_at else None,
    }


def _report_summary(report: ExecutionReport) -> dict[str, Any]:
    steps = []
    for step in report.step_reports:
        result = step.tool_result
        steps.append(
            {
                "tool_id": step.tool_id,
                "success": bool(result.success) if result else None,
                "message": redact_text((result.message or "")[:300]) if result else None,
            }
        )
    tool_result = report.tool_result
    return {
        "success": bool(report.success),
        "cancelled": bool(report.cancelled),
        "status": report.task.status.value,
        "error": redact_text(report.error[:400]) if report.error else None,
        "plan_summary": report.plan_summary,
        "steps_total": report.steps_total,
        "steps": steps,
        "tool_result": (
            {
                "success": bool(tool_result.success),
                "message": redact_text((tool_result.message or "")[:300]),
            }
            if tool_result is not None
            else None
        ),
        "verification_message": report.verification_message,
        "recovered": bool(report.recovered),
        "missing_information": list(report.missing_information),
    }


def _package_version() -> str:
    from . import __version__

    return __version__
