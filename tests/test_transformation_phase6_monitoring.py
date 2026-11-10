"""Transformation Phase 6: live system monitoring and diagnostics."""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import replace

from pc_agent.api_server import LocalApiServer
from pc_agent.baseline import TRANSFORMATION_PHASE
from pc_agent.config import Settings
from pc_agent.gui.controller import GuiController
from pc_agent.storage.db import Database
from pc_agent.system_monitor import (
    ProcessScan,
    SystemMonitor,
    WindowsProcessApi,
    _scan_with_fallbacks,
    _windows_process_rows,
    parse_meminfo,
    parse_net_dev,
    parse_nvidia_smi,
    parse_powershell_processes,
    parse_proc_stat_cpu,
    parse_tasklist,
    system_summary_line,
)


def test_phase6_identity():
    assert TRANSFORMATION_PHASE == 18


def test_pure_parsers():
    reading = parse_proc_stat_cpu("cpu  100 0 50 800 10 0 2 0 0 0")
    assert reading is not None
    assert reading.total == 962
    assert reading.busy == 962 - 810
    assert parse_proc_stat_cpu("nonsense") is None

    info = parse_meminfo("MemTotal: 1000 kB\nMemAvailable: 400 kB\nHugePages_Total: 0\n")
    assert info["MemTotal"] == 1000 * 1024
    assert info["MemAvailable"] == 400 * 1024

    interfaces = parse_net_dev(
        "Inter-| Receive | Transmit\neth0: 1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0\n"
    )
    assert interfaces["eth0"] == (1000, 2000)

    gpu = parse_nvidia_smi("NVIDIA RTX 4060, 42, 2048, 8192\n")
    assert gpu is not None and gpu["available"] is True
    assert gpu["utilization_percent"] == 42.0
    assert parse_nvidia_smi("") is None


def test_live_snapshot_on_this_machine(tmp_path):
    settings = Settings.from_env(tmp_path)
    settings.ensure_directories()
    monitor = SystemMonitor(root_dir=settings.root_dir, workspace_dir=settings.workspace_dir)
    first = monitor.snapshot()
    assert first.cpu_percent is None  # rates need a baseline, never invented
    time.sleep(0.25)
    second = monitor.snapshot()
    assert second.cpu_percent is not None
    assert 0.0 <= second.cpu_percent <= 100.0
    assert second.memory["total_bytes"] > 0
    assert 0.0 <= second.memory["percent"] <= 100.0
    assert second.process["rss_bytes"] > 0
    assert second.disks["install"]["total_bytes"] > 0
    assert second.network["bytes_received"] is not None
    # The scan must produce rows AND say where they came from: an empty list
    # alone is what let a real Windows defect hide behind a silent fallback.
    assert second.top_processes
    assert second.top_processes_source in {
        "/proc",
        "win32-memory",
        "win32-toolhelp",
        "tasklist",
        "powershell",
    }
    assert 0 <= len(second.top_processes) <= 8
    assert all(row["rss_bytes"] > 0 for row in second.top_processes)
    history = monitor.history()
    assert len(history) == 2
    assert "cpu=" in system_summary_line(second)


def _fake_windows_module(*, fail_processes: bool = False):
    """A stand-in for kernel32/psapi so the Windows scan is testable anywhere."""

    class FakeFunction:
        def __init__(self, handler) -> None:
            self._handler = handler
            self.restype = None
            self.argtypes = ()

        def __call__(self, *args):
            return self._handler(*args)

    def open_process(access, inherit, pid):
        return 0 if fail_processes else 1000 + pid

    return type(
        "FakeModule",
        (),
        {
            "OpenProcess": FakeFunction(open_process),
            "CloseHandle": FakeFunction(lambda handle: 1),
            "GetCurrentProcess": FakeFunction(lambda: 4242),
            "QueryFullProcessImageNameW": FakeFunction(lambda *args: 0),
            "K32EnumProcesses": FakeFunction(lambda *args: 1),
            "K32GetProcessMemoryInfo": FakeFunction(lambda *args: 1),
        },
    )()


def test_windows_process_api_resolves_callables_not_tuples(tmp_path):
    """Regression: storing the raw ``_resolve`` tuple made the scan return [].

    The monitor resolved ``(function, export_name)`` pairs for every entry point
    but only unwrapped them for the memory calls, so on a real Windows machine
    ``enum_processes`` was a tuple, the call raised, and ``top_processes`` came
    back empty for every snapshot.
    """

    api = WindowsProcessApi(loader=lambda name: _fake_windows_module())
    assert callable(api.enum_processes)
    assert callable(api.get_memory)
    assert api.get_memory_name == "K32GetProcessMemoryInfo"
    assert callable(api.open_process)
    assert callable(api.image_name)


def test_windows_process_rows_sort_by_working_set(tmp_path):
    working_sets = {10: 5_000, 20: 90_000, 30: 40_000}

    class FakeApi:
        image_name = None

        @staticmethod
        def open_process(access, inherit, pid):
            return 0 if pid not in working_sets else pid + 100

        @staticmethod
        def close_handle(handle):
            return 1

        @staticmethod
        def working_set(handle):
            return working_sets.get(handle - 100)

    rows = _windows_process_rows(FakeApi(), [10, 20, 30, 0, 99], limit=2)
    assert [row["pid"] for row in rows] == [20, 30]
    assert rows[0]["rss_bytes"] == 90_000
    assert rows[0]["name"] == "pid-20"


def test_windows_process_rows_skip_unopenable_processes(tmp_path):
    class DeniedApi:
        image_name = None

        @staticmethod
        def open_process(access, inherit, pid):
            return 0

        @staticmethod
        def close_handle(handle):
            return 1

        @staticmethod
        def working_set(handle):
            return 1024

    assert _windows_process_rows(DeniedApi(), [4, 8], limit=5) == []


def test_system_api_and_service(tmp_path):
    settings = replace(Settings.from_env(tmp_path), api_port=0, scheduler_interval_seconds=0)
    server = LocalApiServer(settings)
    server.start()
    try:
        host, port = server.address
        token = server.token
        request = urllib.request.Request(f"http://{host}:{port}/v1/system")
        request.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read())
        data = payload["data"]
        assert data["snapshot"]["memory"]["total_bytes"] > 0
        assert data["snapshot"]["cpu"]["logical_cores"] >= 1
        assert isinstance(data["history"], list)
    finally:
        server.stop()


def test_system_cli(tmp_path, capsys):
    from pc_agent.cli import main

    assert main(["--root", str(tmp_path), "system", "status"]) == 0
    out = capsys.readouterr().out
    assert "CPU:" in out
    assert "Memory:" in out
    assert "GPU:" in out

    assert main(["--root", str(tmp_path), "system", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["snapshot"]["process"]["pid"] > 0

    assert (
        main(["--root", str(tmp_path), "system", "sample", "--count", "2", "--interval", "0.2"])
        == 0
    )
    sampled = capsys.readouterr().out
    assert sampled.count("sample ") == 2

    assert main(["--root", str(tmp_path), "doctor"]) == 0
    assert "System: cpu=" in capsys.readouterr().out


def test_diagnostics_release_gui_system_surfaces(tmp_path):
    settings = Settings.from_env(tmp_path)
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()

    from pc_agent.diagnostics import build_diagnostics
    from pc_agent.release import audit_install

    report = build_diagnostics(settings, database)
    assert report["system"]["memory"]["total_bytes"] > 0
    assert "cpu" in report["system"]

    audit = audit_install(settings)
    checks = {check.name: check for check in audit.checks}
    assert len(audit.checks) == 34
    assert checks["system_monitor"].status == "pass"

    controller = GuiController(settings, database=database)
    try:
        line = controller.system_status_text()
        assert line.startswith("cpu=")
    finally:
        orchestrator = getattr(controller, "_orchestrator", None)
        if orchestrator is not None:
            orchestrator._safety.close()


def test_web_ui_system_panel():
    from pc_agent.web_ui import INDEX_HTML

    assert "/v1/system" in INDEX_HTML
    assert "System (live)" in INDEX_HTML
    assert "fmtBytes" in INDEX_HTML


# ----------------------------------------- layered Windows process fallbacks ---


def test_fallback_chain_uses_the_first_layer_that_reports_rows() -> None:
    calls: list[str] = []

    def empty() -> ProcessScan:
        calls.append("empty")
        return ProcessScan([], "win32-enum", "opened=0/300")

    def broken() -> ProcessScan:
        calls.append("broken")
        raise RuntimeError("toolhelp exploded")

    def working() -> ProcessScan:
        calls.append("working")
        return ProcessScan([{"pid": 4, "name": "System", "rss_bytes": 1024}], "tasklist")

    scan = _scan_with_fallbacks(
        [("win32-enum", empty), ("win32-toolhelp", broken), ("tasklist", working)], limit=2
    )
    assert calls == ["empty", "broken", "working"]
    assert scan.source == "tasklist"
    assert scan.rows == [{"pid": 4, "name": "System", "rss_bytes": 1024}]
    # every earlier failure is preserved so a report can explain the gap
    assert "win32-enum:opened=0/300" in scan.detail
    assert "win32-toolhelp:RuntimeError" in scan.detail


def test_fallback_chain_reports_a_reason_instead_of_an_empty_list() -> None:
    scan = _scan_with_fallbacks(
        [
            (
                "win32-enum",
                lambda: ProcessScan([], "win32-enum", "GetProcessMemoryInfo export missing"),
            ),
            ("tasklist", lambda: ProcessScan([], "tasklist", "no parsable rows")),
        ],
        limit=5,
    )
    assert scan.rows == []
    assert scan.source == "unavailable"
    assert "GetProcessMemoryInfo export missing" in scan.detail


def test_tasklist_csv_parsing_is_locale_tolerant() -> None:
    english = (
        '"System Idle Process","0","Services","0","8 K"\r\n'
        '"chrome.exe","1234","Console","1","1,234,567 K"\r\n'
        '"My App.exe","4321","Console","1","12 345 K"\r\n'
        '"tiny.exe","7","Console","1","512 K"\r\n'
        "not a csv line\r\n"
        '"no-pid","Console","1","10 K"\r\n'
    )
    rows = parse_tasklist(english, limit=4)
    assert [row["pid"] for row in rows] == [1234, 4321, 7]
    assert rows[0]["rss_bytes"] == 1_234_567 * 1024
    assert rows[0]["name"] == "chrome.exe"
    assert rows[1]["rss_bytes"] == 12_345 * 1024  # space separator (fr/de style)

    german = '"chrome.exe","1234","Konsole","1","1.234.567 K"\r\n'
    assert parse_tasklist(german)[0]["rss_bytes"] == 1_234_567 * 1024

    megabytes = '"big.exe","99","Console","1","640,000 MB"\r\n'
    assert parse_tasklist(megabytes)[0]["rss_bytes"] == 640_000 * 1024 * 1024

    assert parse_tasklist("") == []
    assert parse_tasklist('"nameless.exe","abc","Console","1","10 K"') == []


def test_powershell_fallback_parsing() -> None:
    text = "\r\n4321|734003200|chrome\r\n99|1024|svchost\r\nbroken line\r\n"
    rows = parse_powershell_processes(text, limit=2)
    assert [row["pid"] for row in rows] == [4321, 99]
    assert rows[0]["name"] == "chrome"
    assert rows[0]["rss_bytes"] == 734003200


def test_enum_layer_records_opened_and_unreadable_counts(monkeypatch) -> None:
    from pc_agent import system_monitor

    class FakeApi:
        image_name = None
        enum_processes = staticmethod(lambda array, size, needed: True)
        get_memory = staticmethod(lambda handle, counters, size: True)
        open_process = staticmethod(lambda access, inherit, pid: 0 if pid == 4 else pid)
        close_handle = staticmethod(lambda handle: None)

        @staticmethod
        def working_set(handle):
            return None if handle == 8 else 4096 * int(handle)

    class CountingEnum:
        def __call__(self, array, size, needed):
            for index, pid in enumerate((4, 8, 12)):
                array[index] = pid
            needed._obj.value = 3 * 4
            return True

    FakeApi.enum_processes = CountingEnum()
    monkeypatch.setattr(system_monitor, "_windows_api", lambda: FakeApi())
    scan = system_monitor._scan_via_enum(limit=5)
    assert scan.source == "win32-memory"
    assert "opened=2/3" in scan.detail
    assert "unreadable=1" in scan.detail
    assert [row["pid"] for row in scan.rows] == [12]


def test_enum_layer_explains_a_missing_export(monkeypatch) -> None:
    from pc_agent import system_monitor

    class NoMemory:
        enum_processes = staticmethod(lambda array, size, needed: True)
        get_memory = None

    monkeypatch.setattr(system_monitor, "_windows_api", lambda: NoMemory())
    scan = system_monitor._scan_via_enum(limit=3)
    assert scan.rows == []
    assert scan.source == "win32-enum"
    assert "GetProcessMemoryInfo export missing" in scan.detail


def test_toolhelp_layer_uses_snapshot_names(monkeypatch) -> None:
    from pc_agent import system_monitor

    class FakeApi:
        image_name = None
        get_memory = staticmethod(lambda handle, counters, size: True)

        @staticmethod
        def iter_process_entries():
            return [(4, "System"), (20, "chrome.exe"), (30, "code.exe")]

        open_process = staticmethod(lambda access, inherit, pid: pid)
        close_handle = staticmethod(lambda handle: None)

        @staticmethod
        def working_set(handle):
            return {4: 1024, 20: 900_000, 30: 500_000}[int(handle)]

    monkeypatch.setattr(system_monitor, "_windows_api", lambda: FakeApi())
    scan = system_monitor._scan_via_toolhelp(limit=2)
    assert scan.source == "win32-toolhelp"
    assert [row["name"] for row in scan.rows] == ["chrome.exe", "code.exe"]
    assert "snapshot=3" in scan.detail


def test_snapshot_never_hides_an_empty_process_scan(tmp_path, monkeypatch) -> None:
    from pc_agent import system_monitor

    settings = Settings.from_env(tmp_path)
    settings.ensure_directories()
    monkeypatch.setattr(
        system_monitor,
        "_top_processes",
        lambda limit=8: ProcessScan([], "unavailable", "every layer failed: enum,toolhelp"),
    )
    monitor = system_monitor.SystemMonitor(
        root_dir=settings.root_dir, workspace_dir=settings.workspace_dir
    )
    snapshot = monitor.snapshot()
    assert snapshot.top_processes == []
    assert snapshot.top_processes_source == "unavailable"
    assert "every layer failed" in snapshot.top_processes_detail
    assert snapshot.to_dict()["top_processes_detail"].startswith("every layer failed")
    assert "processes=0 (unavailable)" in system_summary_line(snapshot)
