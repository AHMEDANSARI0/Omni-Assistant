"""Mission execution engine (Transformation Phase 4).

A mission is a DAG of existing tasks. This module computes the
*execution graph* (topological levels for display and API consumers)
and runs it with bounded parallelism: a task starts as soon as every
dependency has SUCCEEDED, up to ``mission_max_parallel`` at once.
Every branch executes through its own ``TaskOrchestrator`` instance
(own Safety Kernel + worker), so parallel tasks get the same hard
timeouts, approvals, budgets, verification, and recovery as a
single CLI run — the kernel's per-instance lock never serializes
independent branches.

If a dependency ends FAILED/CANCELLED/NEEDS_INPUT, its dependents are
blocked (left QUEUED with a task event) and the mission ends FAILED;
nothing runs out of order and nothing is silently re-planned.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from .config import Settings
from .domain.task import TaskStatus
from .errors import PlanningError
from .logging_config import redact_text
from .orchestrator import ApprovalRequest, TaskOrchestrator
from .planning import DeterministicPlanner
from .providers.router import build_model_router
from .storage.db import Database
from .storage.repositories import (
    ExecutionRepository,
    MemoryRepository,
    MissionRepository,
    RecoveryRepository,
    TaskRepository,
)
from .tools import build_default_registry

__all__ = ["MissionRunner", "execution_levels"]

_LOG = logging.getLogger("pc_agent.missions")
_NON_RUNNABLE = frozenset(
    {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.NEEDS_INPUT}
)


def execution_levels(
    task_ids: list[str], dependencies: dict[str, list[str]]
) -> list[list[str]]:
    """Topological levels: level N runs only after levels < N succeed."""
    remaining = {task_id: set(dependencies.get(task_id, [])) for task_id in task_ids}
    placed: set[str] = set()
    levels: list[list[str]] = []
    while remaining:
        ready = sorted(
            task_id for task_id, deps in remaining.items() if deps <= placed
        )
        if not ready:
            raise PlanningError("The mission graph contains a cycle or unknown task ids.")
        levels.append(ready)
        placed.update(ready)
        for task_id in ready:
            remaining.pop(task_id)
    return levels


class MissionRunner:
    """Runs one mission graph with bounded ready-set parallelism."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        orchestrator_factory: Callable[[], TaskOrchestrator] | None = None,
        emit: Callable[..., None] | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._tasks = TaskRepository(database)
        self._missions = MissionRepository(database)
        self._factory = orchestrator_factory or self._default_factory
        self._emit = emit or (lambda *args, **kwargs: None)

    def _default_factory(self) -> TaskOrchestrator:
        settings = self._settings
        database = self._database
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

    def run(
        self,
        mission_id: str,
        *,
        use_ai: bool = False,
        cancel_event: threading.Event | None = None,
        source: str = "mission",
    ) -> dict[str, Any]:
        mission = self._missions.get_mission(mission_id)
        if mission is None:
            raise PlanningError(f"Mission not found: {mission_id}")
        task_ids = self._missions.list_task_ids(mission_id)
        dependencies = self._missions.list_dependencies(mission_id)
        levels = execution_levels(task_ids, dependencies)  # validates acyclic
        self._missions.update_mission_status(mission_id, "running")
        self._emit(
            "mission.run_started",
            source=source,
            summary=f"Mission '{mission.name}' started with {len(task_ids)} task(s).",
            payload={"mission_id": mission_id, "levels": len(levels)},
        )
        cancel_event = cancel_event or threading.Event()
        outcomes: dict[str, TaskStatus] = {}
        pending = {task_id for task_id in task_ids}
        # Seed outcomes for tasks that are already terminal (resume semantics).
        for task_id in list(pending):
            task = self._tasks.get(task_id)
            if task is not None and task.is_terminal:
                outcomes[task_id] = task.status
                pending.discard(task_id)
        max_workers = max(1, self._settings.mission_max_parallel)
        blocked: set[str] = set()
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mission") as pool:
            running: dict[str, Future[TaskStatus]] = {}
            while pending or running:
                if cancel_event.is_set():
                    break
                made_progress = False
                for task_id in sorted(pending):
                    deps = dependencies.get(task_id, [])
                    dep_statuses = [outcomes.get(dep) for dep in deps]
                    if any(status in _NON_RUNNABLE for status in dep_statuses):
                        blocked.add(task_id)
                        pending.discard(task_id)
                        outcomes[task_id] = TaskStatus.FAILED  # marker: never ran
                        self._tasks.record_event(
                            task_id,
                            "mission_blocked",
                            "Skipped: a dependency did not succeed.",
                        )
                        made_progress = True
                        continue
                    if all(status == TaskStatus.SUCCEEDED for status in dep_statuses):
                        running[task_id] = pool.submit(
                            self._execute_one, task_id, use_ai, cancel_event
                        )
                        pending.discard(task_id)
                        made_progress = True
                finished = [task_id for task_id, future in running.items() if future.done()]
                for task_id in finished:
                    future = running.pop(task_id)
                    try:
                        outcomes[task_id] = future.result()
                    except Exception:  # _execute_one already logs; treat as failure.  # noqa: BLE001
                        _LOG.exception("Mission task %s raised", task_id)
                        outcomes[task_id] = TaskStatus.FAILED
                    made_progress = True
                if not made_progress:
                    time.sleep(0.05)
            if cancel_event.is_set():
                for future in running.values():
                    future.cancel()
        final_status = self._final_status(task_ids, outcomes, blocked, cancel_event)
        updated = self._missions.update_mission_status(mission_id, final_status)
        succeeded = sum(1 for status in outcomes.values() if status == TaskStatus.SUCCEEDED)
        self._emit(
            "mission.run_finished",
            source=source,
            summary=(
                f"Mission '{mission.name}' finished {final_status}: "
                f"{succeeded}/{len(task_ids)} task(s) succeeded."
            ),
            payload={
                "mission_id": mission_id,
                "status": final_status,
                "succeeded": succeeded,
                "blocked": sorted(blocked),
            },
        )
        return {
            "mission": updated.to_dict(),
            "levels": levels,
            "outcomes": {task_id: status.value for task_id, status in outcomes.items()},
            "blocked_task_ids": sorted(blocked),
        }

    def _final_status(
        self,
        task_ids: list[str],
        outcomes: dict[str, TaskStatus],
        blocked: set[str],
        cancel_event: threading.Event,
    ) -> str:
        if cancel_event.is_set():
            return "cancelled"
        if not task_ids:
            return "failed"
        if blocked:
            return "failed"
        statuses = [outcomes.get(task_id) for task_id in task_ids]
        if all(status == TaskStatus.SUCCEEDED for status in statuses):
            return "succeeded"
        if all(status == TaskStatus.CANCELLED for status in statuses if status is not None) and any(
            status == TaskStatus.CANCELLED for status in statuses
        ):
            return "cancelled"
        return "failed"

    def _execute_one(
        self, task_id: str, use_ai: bool, cancel_event: threading.Event
    ) -> TaskStatus:
        def deny(_request: ApprovalRequest) -> bool:
            return False

        orchestrator = self._factory()
        try:
            orchestrator.execute(
                task_id,
                approval_callback=deny,
                use_ai=use_ai,
                cancellation_requested=cancel_event.is_set,
            )
        except Exception as exc:  # Branch failure must not kill the pool.  # noqa: BLE001
            _LOG.exception("Mission branch %s crashed: %s", task_id, redact_text(str(exc)))
            return TaskStatus.FAILED
        finally:
            orchestrator._safety.close()  # Worker belongs to this branch.
        task = self._tasks.get(task_id)
        return task.status if task is not None else TaskStatus.FAILED
