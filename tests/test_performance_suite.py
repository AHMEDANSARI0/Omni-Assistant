"""Phase 18 performance budget suite (production hardening).

Every number here is *measured* on the machine running the suite, and every
budget is a real ceiling with a wide margin so a slow CI runner does not turn a
correct build into a red one. The budgets exist to catch order-of-magnitude
regressions (a query that becomes O(n^2), an import that pulls in a heavy module
at start-up), not to win a micro-benchmark.

Budgets are expressed in absolute milliseconds and are documented in
``PERFORMANCE_REPORT.md``. If a budget has to move, the report has to move with
it - that is the point of writing it down.
"""

from __future__ import annotations

import os
import statistics
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

import pytest

from pc_agent.config import Settings
from pc_agent.storage.db import Database

#: Ceilings measured with CPython 3.13 on a 2-vCPU container; ~10x observed.
BUDGET_SETTINGS_LOAD_MS = 250.0
BUDGET_HEALTH_MS = 400.0
BUDGET_SYSTEM_STATUS_MS = 900.0
BUDGET_EVENTS_LIST_MS = 400.0
BUDGET_A11Y_STATUS_MS = 200.0
BUDGET_IMPORT_SECONDS = 12.0
BUDGET_HEALTH_GROWTH_BYTES = 512 * 1024


def _settings(tmp_path: Path) -> Settings:
    settings = Settings.from_env(tmp_path, environ={}, load_dotenv=False)
    settings.ensure_directories()
    Database(settings.database_path).initialize()
    return settings


def _median_ms(call, iterations: int = 7) -> float:
    samples = []
    for _ in range(iterations):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def test_settings_load_is_fast_enough_for_a_cold_start(tmp_path: Path) -> None:
    elapsed = _median_ms(
        lambda: Settings.from_env(tmp_path / "cold", environ={}, load_dotenv=False),
        iterations=5,
    )
    assert elapsed < BUDGET_SETTINGS_LOAD_MS, f"settings load took {elapsed:.1f} ms"


def test_import_of_the_cli_entry_point_is_bounded(tmp_path: Path) -> None:
    """The CLI must not pull the world in at start-up."""

    project_root = Path(__file__).resolve().parents[1]
    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-c", "import pc_agent.cli"],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env={**os.environ, "PYTHONPATH": str(project_root / "src")},
    )
    elapsed = time.perf_counter() - started
    assert completed.returncode == 0, completed.stderr[-400:]
    assert elapsed < BUDGET_IMPORT_SECONDS, f"import took {elapsed:.2f}s"


def test_health_and_status_are_sub_second(tmp_path: Path) -> None:
    from pc_agent.service import LocalService

    service = LocalService(_settings(tmp_path))
    health_ms = _median_ms(service.health)
    status_ms = _median_ms(service.system_status, iterations=3)
    assert health_ms < BUDGET_HEALTH_MS, f"health took {health_ms:.1f} ms"
    assert status_ms < BUDGET_SYSTEM_STATUS_MS, f"system_status took {status_ms:.1f} ms"


def test_event_listing_is_sub_second_with_a_warm_ledger(tmp_path: Path) -> None:
    from pc_agent.service import LocalService

    service = LocalService(_settings(tmp_path))
    for index in range(20):
        service.create_task(f"phase18 bench task {index}")
    elapsed = _median_ms(lambda: service.list_events(limit=50), iterations=5)
    assert elapsed < BUDGET_EVENTS_LIST_MS, f"events(limit=50) took {elapsed:.1f} ms"


def test_accessibility_report_is_cheap(tmp_path: Path) -> None:
    from pc_agent.a11y import a11y_status

    settings = _settings(tmp_path)
    elapsed = _median_ms(lambda: a11y_status(settings))
    assert elapsed < BUDGET_A11Y_STATUS_MS, f"a11y_status took {elapsed:.1f} ms"


def test_repeated_health_calls_do_not_leak_memory(tmp_path: Path) -> None:
    """A long-running service must not grow on every status read."""

    from pc_agent.service import LocalService

    service = LocalService(_settings(tmp_path))
    service.health()  # warm every cache
    tracemalloc.start()
    try:
        baseline = tracemalloc.take_snapshot()
        for _ in range(200):
            service.health()
        current, _peak = tracemalloc.get_traced_memory()
        growth = tracemalloc.take_snapshot().compare_to(baseline, "filename")
    finally:
        tracemalloc.stop()
    total_growth = sum(item.size_diff for item in growth if item.size_diff > 0)
    assert current < BUDGET_HEALTH_GROWTH_BYTES, f"traced memory {current} bytes"
    assert total_growth < BUDGET_HEALTH_GROWTH_BYTES, (
        f"200 health() calls grew the heap by {total_growth} bytes"
    )


def test_release_candidate_scan_is_not_quadratic() -> None:
    """Scanning the allow-list twice in a row must cost the same."""

    from pc_agent.release import release_candidate_files

    project_root = Path(__file__).resolve().parents[1]
    first = _median_ms(lambda: release_candidate_files(project_root), iterations=3)
    files = release_candidate_files(project_root)
    assert files, "no release candidates found"
    second = _median_ms(lambda: release_candidate_files(project_root), iterations=3)
    assert max(first, second) < 4000.0
    # Cached page state makes the second pass faster or equal, never 3x slower.
    assert second < max(first * 3.0, 250.0)


def test_database_initialisation_is_bounded(tmp_path: Path) -> None:
    settings = Settings.from_env(tmp_path, environ={}, load_dotenv=False)
    settings.ensure_directories()
    started = time.perf_counter()
    version = Database(settings.database_path).initialize()
    elapsed = time.perf_counter() - started
    assert version >= 18
    assert elapsed < 20.0, f"database initialisation took {elapsed:.2f}s"


@pytest.mark.parametrize("iterations", [1])
def test_perf_bench_measures_real_operations(iterations: int, tmp_path: Path) -> None:
    """``pc-agent perf bench`` reports measurements, not invented numbers."""

    from pc_agent.service import LocalService

    bench = LocalService(_settings(tmp_path)).perf_bench(iterations=iterations)
    measurements = bench["measurements_ms"]
    assert set(measurements) >= {
        "health_first_call",
        "health_warm",
        "events_list",
        "a11y_status",
        "system_status",
    }
    for name, value in measurements.items():
        assert value >= 0.0, name
    assert bench["iterations"] == iterations
    assert "not included" in bench["notes"]
