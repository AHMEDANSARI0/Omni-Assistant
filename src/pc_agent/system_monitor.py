"""Live system monitoring with real sensors (Transformation Phase 6).

Everything here is stdlib-based and fail-soft: a sensor that cannot be
read on the current platform reports ``None``/``available: false``
with the method that was tried — never an invented number.

- CPU: Windows ``GetSystemTimes``, Linux ``/proc/stat``, otherwise a
  clearly-labelled load-average estimate.
- Memory: Windows ``GlobalMemoryStatusEx``, Linux ``/proc/meminfo``,
  POSIX ``sysconf`` totals.
- Disk: :func:`shutil.disk_usage` for the install/workspace volumes.
- Process (self): RSS via Windows ``GetProcessMemoryInfo`` /
  ``/proc/self/statm`` / ``getrusage`` fallback.
- Top processes: Linux ``/proc`` scan; Windows PSAPI/K32 via ctypes
  (best effort — protected processes are skipped).
- Network byte counters: Linux ``/proc/net/dev``; Windows
  ``GetIfTable`` (best effort). Rates are computed between samples.
- GPU: ``nvidia-smi`` when present (most Windows/NVIDIA machines);
  otherwise honestly unavailable in this phase.
- Battery: Windows ``GetSystemPowerStatus``; Linux
  ``/sys/class/power_supply``.

``SystemMonitor`` keeps the previous CPU/network readings to compute
rates, plus a bounded in-memory history for Command Center sparklines.
"""

from __future__ import annotations

import csv
import logging
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["SystemMonitor", "SystemSnapshot", "parse_nvidia_smi", "system_summary_line"]

_LOG = logging.getLogger("pc_agent.system_monitor")
_STARTED_MONOTONIC = time.monotonic()
_IS_WINDOWS = os.name == "nt"
_IS_LINUX = platform.system() == "Linux"


def _round(value: float | None, digits: int = 1) -> float | None:
    return round(value, digits) if value is not None else None


@dataclass(frozen=True, slots=True)
class CpuReading:
    busy: float
    total: float


def _read_cpu_reading() -> tuple[CpuReading | None, str]:
    if _IS_WINDOWS:
        try:
            import ctypes

            class FileTime(ctypes.Structure):
                _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

                def ticks(self) -> int:
                    return (self.high << 32) | self.low

            idle, kernel, user = FileTime(), FileTime(), FileTime()
            ok = ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            )
            if not ok:
                return None, "GetSystemTimes"
            total = float(kernel.ticks() + user.ticks())  # kernel includes idle
            busy = total - float(idle.ticks())
            return CpuReading(busy=busy, total=total), "GetSystemTimes"
        except Exception:  # noqa: BLE001 - sensor failure reports unavailable
            return None, "GetSystemTimes"
    if _IS_LINUX:
        try:
            first = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
            return parse_proc_stat_cpu(first), "/proc/stat"
        except Exception:  # noqa: BLE001
            return None, "/proc/stat"
    return None, "loadavg"


def parse_proc_stat_cpu(line: str) -> CpuReading | None:
    parts = line.split()
    if not parts or parts[0] != "cpu" or len(parts) < 5:
        return None
    try:
        values = [int(item) for item in parts[1:11]]
    except ValueError:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = float(sum(values))
    return CpuReading(busy=total - idle, total=total)


def parse_meminfo(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        chunks = rest.strip().split()
        if not chunks:
            continue
        try:
            value = int(chunks[0])
        except ValueError:
            continue
        if len(chunks) > 1 and chunks[1].lower() == "kb":
            value *= 1024
        result[key.strip()] = value
    return result


def _read_memory() -> dict[str, Any]:
    if _IS_WINDOWS:
        try:
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_uint32),
                    ("memory_load", ctypes.c_uint32),
                    ("total_phys", ctypes.c_uint64),
                    ("avail_phys", ctypes.c_uint64),
                    ("total_page_file", ctypes.c_uint64),
                    ("avail_page_file", ctypes.c_uint64),
                    ("total_virtual", ctypes.c_uint64),
                    ("avail_virtual", ctypes.c_uint64),
                    ("avail_extended_virtual", ctypes.c_uint64),
                ]

            status = MemoryStatusEx()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(  # type: ignore[attr-defined]
                ctypes.byref(status)
            ):
                total = int(status.total_phys)
                available = int(status.avail_phys)
                return _memory_payload(total, available, "GlobalMemoryStatusEx")
        except Exception:  # noqa: BLE001
            _LOG.debug("GlobalMemoryStatusEx failed", exc_info=True)
        return _memory_payload(None, None, "GlobalMemoryStatusEx")
    if _IS_LINUX:
        try:
            info = parse_meminfo(Path("/proc/meminfo").read_text(encoding="utf-8"))
            total = info.get("MemTotal")
            available = info.get("MemAvailable", info.get("MemFree"))
            return _memory_payload(total, available, "/proc/meminfo")
        except Exception:  # noqa: BLE001
            return _memory_payload(None, None, "/proc/meminfo")
    try:
        total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        available = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        return _memory_payload(int(total), int(available), "sysconf")
    except (ValueError, OSError, AttributeError):
        return _memory_payload(None, None, "sysconf")


def _memory_payload(total: int | None, available: int | None, method: str) -> dict[str, Any]:
    used = total - available if total is not None and available is not None else None
    percent = (100.0 * used / total) if used is not None and total else None
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": used,
        "percent": _round(percent),
        "method": method,
    }


def _read_disk(paths: dict[str, Path]) -> dict[str, Any]:
    volumes: dict[str, Any] = {}
    seen: set[str] = set()
    for label, path in paths.items():
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        key = str(Path(path).resolve().anchor or path)
        entry = {
            "path": str(path),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "percent": _round(100.0 * usage.used / usage.total) if usage.total else None,
        }
        volumes[label] = entry
        seen.add(key)
    return volumes


_WINDOWS_API_UNSET = object()
_WINDOWS_API: Any = _WINDOWS_API_UNSET


class WindowsProcessApi:
    """Resolved Win32 process-memory entry points (cached once per process).

    Windows exposes the same information under different names across
    versions and DLLs: ``kernel32!K32GetProcessMemoryInfo`` (Windows 7+),
    ``psapi!GetProcessMemoryInfo`` and, on some builds, a kernel32
    forwarder. Only one of them has to resolve for monitoring to work, so
    every combination is probed in order and the first that loads wins.

    Handles and buffers are declared with explicit ctypes signatures:
    without them 64-bit HANDLE values are truncated to 32-bit ``int``,
    which silently makes ``OpenProcess`` fail for many real processes.
    """

    __slots__ = (
        "_load",
        "close_handle",
        "counters_type",
        "current_process",
        "enum_processes",
        "get_memory",
        "get_memory_name",
        "image_name",
        "open_process",
        "process_entries",
        "process_entry_type",
        "toolhelp_snapshot",
    )

    def __init__(self, loader: Any = None) -> None:
        import ctypes

        self._load = loader or (lambda name: ctypes.WinDLL(name, use_last_error=True))

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_uint32),
                ("page_faults", ctypes.c_uint32),
                ("peak_working_set", ctypes.c_size_t),
                ("working_set", ctypes.c_size_t),
                ("quota_peak_paged", ctypes.c_size_t),
                ("quota_paged", ctypes.c_size_t),
                ("quota_peak_nonpaged", ctypes.c_size_t),
                ("quota_nonpaged", ctypes.c_size_t),
                ("page_file_usage", ctypes.c_size_t),
                ("peak_page_file_usage", ctypes.c_size_t),
            ]

        self.counters_type = Counters
        kernel32 = self._load("kernel32")
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = (
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        )
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetCurrentProcess.argtypes = ()
        self.open_process = kernel32.OpenProcess
        self.close_handle = kernel32.CloseHandle
        self.current_process = kernel32.GetCurrentProcess

        image_name = getattr(kernel32, "QueryFullProcessImageNameW", None)
        if image_name is not None:
            image_name.restype = ctypes.c_int
            image_name.argtypes = (
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_wchar),
                ctypes.POINTER(ctypes.c_uint32),
            )
        self.image_name = image_name

        enumeration = self._resolve(
            ctypes,
            (("kernel32", "K32EnumProcesses"), ("psapi", "EnumProcesses")),
            (
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
            ),
        )
        # _resolve returns (function, export name); storing the tuple here made
        # the Windows process scan silently return [] on real machines.
        self.enum_processes = enumeration[0] if enumeration else None

        entry = self._resolve(
            ctypes,
            (
                ("kernel32", "K32GetProcessMemoryInfo"),
                ("psapi", "GetProcessMemoryInfo"),
                ("kernel32", "GetProcessMemoryInfo"),
            ),
            (ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_uint32),
        )
        self.get_memory = entry[0] if entry else None
        self.get_memory_name = entry[1] if entry else "unavailable"

        # Second, independent way to list processes: a Toolhelp snapshot needs
        # no PSAPI export and no elevated rights, so it still works when
        # EnumProcesses is unavailable.
        class ProcessEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_uint32),
                ("cntUsage", ctypes.c_uint32),
                ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", ctypes.c_uint32),
                ("cntThreads", ctypes.c_uint32),
                ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase", ctypes.c_int32),
                ("dwFlags", ctypes.c_uint32),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        self.process_entry_type = ProcessEntry
        snapshot_processes = getattr(kernel32, "CreateToolhelp32Snapshot", None)
        if snapshot_processes is not None:
            snapshot_processes.restype = ctypes.c_void_p
            snapshot_processes.argtypes = (ctypes.c_uint32, ctypes.c_uint32)
        first = getattr(kernel32, "Process32FirstW", None)
        following = getattr(kernel32, "Process32NextW", None)
        for function in (first, following):
            if function is not None:
                function.restype = ctypes.c_int
                function.argtypes = (
                    ctypes.c_void_p,
                    ctypes.POINTER(ProcessEntry),
                )
        self.toolhelp_snapshot = snapshot_processes
        self.process_entries = (first, following) if first and following else None

    def iter_process_entries(self) -> list[tuple[int, str]]:
        """(pid, image name) pairs from a Toolhelp snapshot; ``[]`` if unusable."""

        import ctypes

        if self.toolhelp_snapshot is None or self.process_entries is None:
            return []
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        handle = self.toolhelp_snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if not handle or handle == INVALID_HANDLE_VALUE:
            return []
        first, following = self.process_entries
        entries: list[tuple[int, str]] = []
        try:
            entry = self.process_entry_type()
            entry.dwSize = ctypes.sizeof(entry)
            if not first(handle, ctypes.byref(entry)):
                return []
            while True:
                entries.append((int(entry.th32ProcessID), str(entry.szExeFile)))
                entry.dwSize = ctypes.sizeof(entry)
                if not following(handle, ctypes.byref(entry)):
                    break
                if len(entries) > 8192:  # hard stop: never loop forever
                    break
        finally:
            self.close_handle(handle)
        return entries

    def _resolve(
        self,
        ctypes_module: Any,
        candidates: tuple[tuple[str, str], ...],
        argtypes: tuple[Any, ...],
    ) -> tuple[Any, str] | None:
        for library, export in candidates:
            try:
                module = self._load(library)
                function = getattr(module, export)
            except (OSError, AttributeError):
                continue
            function.restype = ctypes_module.c_int
            function.argtypes = argtypes
            return function, export
        return None

    def working_set(self, handle: Any) -> int | None:
        """Working-set bytes for ``handle``, or ``None`` when unreadable."""
        import ctypes

        if self.get_memory is None:
            return None
        counters = self.counters_type()
        counters.cb = ctypes.sizeof(counters)
        if not self.get_memory(handle, ctypes.byref(counters), counters.cb):
            return None
        value = int(counters.working_set)
        return value if value > 0 else None


def _windows_api() -> WindowsProcessApi | None:
    """Return the cached Windows process API, or ``None`` if unavailable."""
    global _WINDOWS_API
    if _WINDOWS_API is not _WINDOWS_API_UNSET:
        return _WINDOWS_API
    try:
        _WINDOWS_API = WindowsProcessApi()
    except Exception:  # noqa: BLE001 - monitoring must never crash a snapshot
        _LOG.warning("Windows process-memory API unavailable", exc_info=True)
        _WINDOWS_API = None
    return _WINDOWS_API


def _process_rss() -> tuple[int | None, str]:
    if _IS_WINDOWS:
        api = _windows_api()
        if api is None:
            return None, "unavailable"
        try:
            rss = api.working_set(api.current_process())
        except Exception:  # noqa: BLE001
            _LOG.debug("GetProcessMemoryInfo failed", exc_info=True)
            rss = None
        return rss, api.get_memory_name
    if _IS_LINUX:
        try:
            fields = Path("/proc/self/statm").read_text(encoding="utf-8").split()
            pages = int(fields[1])
            return pages * os.sysconf("SC_PAGE_SIZE"), "/proc/self/statm"
        except Exception:  # noqa: BLE001
            _LOG.debug("/proc/self/statm failed", exc_info=True)
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KiB, macOS bytes.
        return int(peak * 1024) if _IS_LINUX else int(peak), "getrusage(peak)"
    except Exception:  # noqa: BLE001
        return None, "getrusage(peak)"


def _system_uptime() -> float | None:
    if _IS_WINDOWS:
        try:
            import ctypes

            return ctypes.windll.kernel32.GetTickCount64() / 1000.0  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return None
    if _IS_LINUX:
        try:
            return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        except Exception:  # noqa: BLE001
            return None
    return None


def parse_net_dev(text: str) -> dict[str, tuple[int, int]]:
    interfaces: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        fields = rest.split()
        if len(fields) < 16:
            continue
        try:
            interfaces[name.strip()] = (int(fields[0]), int(fields[8]))
        except ValueError:
            continue
    return interfaces


def _read_network_counters() -> tuple[dict[str, tuple[int, int]], str]:
    if _IS_LINUX:
        try:
            return parse_net_dev(Path("/proc/net/dev").read_text(encoding="utf-8")), "/proc/net/dev"
        except Exception:  # noqa: BLE001
            return {}, "/proc/net/dev"
    if _IS_WINDOWS:
        return _windows_net_counters(), "GetIfTable"
    return {}, "unavailable"


def _windows_net_counters() -> dict[str, tuple[int, int]]:
    try:
        import ctypes

        class IfRow(ctypes.Structure):
            _fields_ = [
                ("name", ctypes.c_wchar * 256),
                ("index", ctypes.c_uint32),
                ("type", ctypes.c_uint32),
                ("mtu", ctypes.c_uint32),
                ("speed", ctypes.c_uint32),
                ("phys_addr_len", ctypes.c_uint32),
                ("phys_addr", ctypes.c_ubyte * 8),
                ("admin_status", ctypes.c_uint32),
                ("oper_status", ctypes.c_uint32),
                ("last_change", ctypes.c_uint32),
                ("in_octets", ctypes.c_uint32),
                ("in_ucast", ctypes.c_uint32),
                ("in_nucast", ctypes.c_uint32),
                ("in_discards", ctypes.c_uint32),
                ("in_errors", ctypes.c_uint32),
                ("in_unknown", ctypes.c_uint32),
                ("out_octets", ctypes.c_uint32),
                ("out_ucast", ctypes.c_uint32),
                ("out_nucast", ctypes.c_uint32),
                ("out_discards", ctypes.c_uint32),
                ("out_errors", ctypes.c_uint32),
                ("out_qlen", ctypes.c_uint32),
                ("descr_len", ctypes.c_uint32),
                ("descr", ctypes.c_ubyte * 256),
            ]

        size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_uint32) + 64 * ctypes.sizeof(IfRow))
        buffer = ctypes.create_string_buffer(size.value)
        result = ctypes.windll.iphlpapi.GetIfTable(  # type: ignore[attr-defined]
            buffer, ctypes.byref(size), False
        )
        if result != 0:
            buffer = ctypes.create_string_buffer(size.value)
            result = ctypes.windll.iphlpapi.GetIfTable(  # type: ignore[attr-defined]
                buffer, ctypes.byref(size), False
            )
            if result != 0:
                return {}
        count = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint32)).contents.value
        rows = (IfRow * count).from_address(
            ctypes.addressof(buffer) + ctypes.sizeof(ctypes.c_uint32)
        )
        interfaces: dict[str, tuple[int, int]] = {}
        for row in rows:
            descr = bytes(row.descr[: row.descr_len]).decode("utf-8", errors="replace") or row.name
            interfaces[descr] = (int(row.in_octets), int(row.out_octets))
        return interfaces
    except Exception:  # noqa: BLE001
        return {}


def parse_nvidia_smi(output: str) -> dict[str, Any] | None:
    for line in output.splitlines():
        parts = [chunk.strip() for chunk in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            return {
                "available": True,
                "name": parts[0],
                "utilization_percent": float(parts[1]),
                "memory_used_mib": float(parts[2]),
                "memory_total_mib": float(parts[3]),
                "method": "nvidia-smi",
            }
        except ValueError:
            continue
    return None


def _read_gpu() -> dict[str, Any]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return {
            "available": False,
            "name": None,
            "utilization_percent": None,
            "memory_used_mib": None,
            "memory_total_mib": None,
            "method": "nvidia-smi not found (non-NVIDIA or driver tools absent)",
        }
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=4,
            check=False,
        )
        parsed = parse_nvidia_smi(result.stdout or "")
        if parsed:
            return parsed
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {
        "available": False,
        "name": None,
        "utilization_percent": None,
        "memory_used_mib": None,
        "memory_total_mib": None,
        "method": "nvidia-smi (query failed)",
    }


def _read_battery() -> dict[str, Any] | None:
    if _IS_WINDOWS:
        try:
            import ctypes

            class PowerStatus(ctypes.Structure):
                _fields_ = [
                    ("ac_line_status", ctypes.c_ubyte),
                    ("battery_flag", ctypes.c_ubyte),
                    ("battery_life_percent", ctypes.c_ubyte),
                    ("system_status_flag", ctypes.c_ubyte),
                    ("battery_life_time", ctypes.c_uint32),
                    ("battery_full_life_time", ctypes.c_uint32),
                ]

            status = PowerStatus()
            if ctypes.windll.kernel32.GetSystemPowerStatus(  # type: ignore[attr-defined]
                ctypes.byref(status)
            ):
                if status.battery_flag == 128:  # no system battery
                    return None
                percent = int(status.battery_life_percent)
                return {
                    "percent": percent if percent != 255 else None,
                    "plugged_in": bool(status.ac_line_status == 1),
                    "method": "GetSystemPowerStatus",
                }
        except Exception:  # noqa: BLE001
            return None
        return None
    if _IS_LINUX:
        try:
            base = Path("/sys/class/power_supply")
            for entry in sorted(base.iterdir()):
                capacity_file = entry / "capacity"
                if not capacity_file.is_file():
                    continue
                percent = int(capacity_file.read_text(encoding="utf-8").strip())
                status_text = ""
                status_file = entry / "status"
                if status_file.is_file():
                    status_text = status_file.read_text(encoding="utf-8").strip()
                return {
                    "percent": percent,
                    "plugged_in": status_text in {"Charging", "Full", "Not charging"},
                    "method": "/sys/class/power_supply",
                }
        except Exception:  # noqa: BLE001
            return None
    return None


@dataclass(frozen=True, slots=True)
class ProcessScan:
    """Top-process rows plus where they came from (never a silent empty list)."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    source: str = "unavailable"
    detail: str = ""


def _top_processes(limit: int = 8) -> ProcessScan:
    if _IS_LINUX:
        rows = _top_processes_linux(limit)
        return ProcessScan(rows, "/proc", "" if rows else "/proc unreadable")
    if _IS_WINDOWS:
        return _windows_scan(limit)
    return ProcessScan([], "unavailable", f"no process source for {platform.system() or 'this OS'}")


def _scan_with_fallbacks(
    layers: list[tuple[str, Callable[[], ProcessScan]]], limit: int
) -> ProcessScan:
    """First layer that produces rows wins; every failure is kept as a reason.

    Pure and injectable so the ordering (and the reason text) is testable on any
    platform without a Windows process table.
    """

    reasons: list[str] = []
    for name, layer in layers:
        try:
            scan = layer()
        except Exception as exc:  # noqa: BLE001 - a fallback may fail loudly
            reasons.append(f"{name}:{type(exc).__name__}")
            continue
        if scan.rows:
            scan_rows = scan.rows[:limit]
            detail = scan.detail or ("" if not reasons else "; ".join(reasons))
            return ProcessScan(scan_rows, scan.source or name, detail[:400])
        reasons.append(f"{name}:{scan.detail or 'empty'}")
    return ProcessScan([], "unavailable", "; ".join(reasons)[:400])


def _windows_scan(limit: int) -> ProcessScan:
    """Layered Windows process scan.

    Layer 1 enumerates PIDs with EnumProcesses and reads working sets with
    GetProcessMemoryInfo. Layer 2 uses a Toolhelp snapshot for PIDs/names and the
    same memory reader. Layer 3 asks the OS itself (``tasklist``) and layer 4
    falls back to PowerShell ``Get-Process``. Only the first two are silent; the
    text fallbacks spawn a fixed, argument-free helper, which is why they run
    last and only when the in-process API produced nothing.
    """

    layers: list[tuple[str, Callable[[], ProcessScan]]] = [
        ("win32-enum", lambda: _scan_via_enum(limit)),
        ("win32-toolhelp", lambda: _scan_via_toolhelp(limit)),
        ("tasklist", lambda: _scan_via_tasklist(limit)),
        ("powershell", lambda: _scan_via_powershell(limit)),
    ]
    return _scan_with_fallbacks(layers, limit)


def _scan_via_enum(limit: int) -> ProcessScan:
    api = _windows_api()
    if api is None:
        return ProcessScan([], "win32-enum", "process API unavailable")
    if api.enum_processes is None:
        return ProcessScan([], "win32-enum", "EnumProcesses export missing")
    if api.get_memory is None:
        return ProcessScan([], "win32-enum", "GetProcessMemoryInfo export missing")
    import ctypes

    capacity = 4096
    array = (ctypes.c_uint32 * capacity)()
    needed = ctypes.c_uint32()
    if not api.enum_processes(array, ctypes.sizeof(array), ctypes.byref(needed)):
        return ProcessScan([], "win32-enum", f"EnumProcesses failed ({ctypes.get_last_error()})")
    count = min(needed.value // ctypes.sizeof(ctypes.c_uint32), capacity)
    if not count:
        return ProcessScan([], "win32-enum", "EnumProcesses returned 0 pids")
    stats: dict[str, int] = {}
    rows = _windows_process_rows(
        api, [int(array[index]) for index in range(count)], limit, stats=stats
    )
    detail = (
        f"opened={stats.get('opened', 0)}/{len(range(count))} "
        f"unreadable={stats.get('unreadable', 0)}"
    )
    return ProcessScan(rows, "win32-memory", detail)


def _scan_via_toolhelp(limit: int) -> ProcessScan:
    api = _windows_api()
    if api is None or api.get_memory is None:
        return ProcessScan([], "win32-toolhelp", "memory reader unavailable")
    entries = api.iter_process_entries()
    if not entries:
        return ProcessScan([], "win32-toolhelp", "Toolhelp snapshot empty")
    stats: dict[str, int] = {}
    rows = _windows_process_rows(api, [pid for pid, _name in entries], limit, stats=stats)
    names = {pid: name for pid, name in entries if name}
    for row in rows:
        name = names.get(int(row["pid"]))
        if name:
            row["name"] = name
    detail = (
        f"snapshot={len(entries)} opened={stats.get('opened', 0)} "
        f"unreadable={stats.get('unreadable', 0)}"
    )
    return ProcessScan(rows, "win32-toolhelp", detail)


def _run_quiet(argv: list[str], timeout: float = 8.0) -> str:
    """Run a fixed, argument-free OS helper and return bounded stdout text."""

    creationflags = 0x08000000 if _IS_WINDOWS else 0  # CREATE_NO_WINDOW
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creationflags,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "")[:200_000]


_MEMORY_CELL = re.compile(
    r"^[\s\d.,\u00a0\u202f\u066c]*\d[\s\d.,\u00a0\u202f\u066c]*\s*[KkMm]?[Bb]?$"
)


def parse_tasklist(text: str, limit: int = 8) -> list[dict[str, Any]]:
    """Parse ``tasklist /FO CSV /NH`` output into rows (locale tolerant).

    Windows localises the header and the thousand separators, so the memory cell
    is found by shape (a number, optional separators, optional K/M suffix) and
    the image name is always the first column.
    """

    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith('"'):
            continue
        try:
            fields = next(csv.reader([line]))
        except (csv.Error, StopIteration):
            continue
        if len(fields) < 2:
            continue
        name = fields[0].strip()
        try:
            pid = int(fields[1].strip())
        except ValueError:
            continue
        if not name or pid <= 0:
            continue
        size = None
        for cell in reversed(fields[2:]):
            candidate = cell.strip()
            if not _MEMORY_CELL.match(candidate):
                continue
            digits = re.sub(r"[^0-9]", "", candidate)
            if not digits:
                continue
            multiplier = 1024 if candidate[-1:].upper() in {"K"} else 1
            if candidate[-2:].upper() == "KB":
                multiplier = 1024
            if candidate[-1:].upper() == "M" or candidate[-2:].upper() == "MB":
                multiplier = 1024 * 1024
            size = int(digits) * multiplier
            break
        if not size:
            continue
        rows.append({"pid": pid, "name": name, "rss_bytes": size})
    rows.sort(key=lambda row: row["rss_bytes"], reverse=True)
    return rows[:limit]


def parse_powershell_processes(text: str, limit: int = 8) -> list[dict[str, Any]]:
    """Parse ``Id|WorkingSet64|ProcessName`` lines from the PowerShell fallback."""

    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            rss = int(parts[1])
        except ValueError:
            continue
        name = parts[2]
        if pid <= 0 or rss <= 0 or not name:
            continue
        rows.append({"pid": pid, "name": name, "rss_bytes": rss})
    rows.sort(key=lambda row: row["rss_bytes"], reverse=True)
    return rows[:limit]


def _scan_via_tasklist(limit: int) -> ProcessScan:
    if not _IS_WINDOWS:
        return ProcessScan([], "tasklist", "not Windows")
    rows = parse_tasklist(_run_quiet(["tasklist", "/FO", "CSV", "/NH"]), limit)
    return ProcessScan(rows, "tasklist", "" if rows else "no parsable rows")


def _scan_via_powershell(limit: int) -> ProcessScan:
    if not _IS_WINDOWS:
        return ProcessScan([], "powershell", "not Windows")
    script = (
        "Get-Process | Sort-Object -Property WorkingSet64 -Descending | "
        "Select-Object -First 15 | ForEach-Object { "
        '"$($_.Id)|$($_.WorkingSet64)|$($_.ProcessName)" }'
    )
    for executable in ("powershell", "pwsh"):
        text = _run_quiet([executable, "-NoProfile", "-NonInteractive", "-Command", script])
        if text.strip():
            rows = parse_powershell_processes(text, limit)
            if rows:
                return ProcessScan(rows, "powershell", "")
    return ProcessScan([], "powershell", "Get-Process returned nothing")


def _top_processes_linux(limit: int) -> list[dict[str, Any]]:
    page_size = os.sysconf("SC_PAGE_SIZE")
    rows: list[dict[str, Any]] = []
    try:
        proc_entries = [entry for entry in Path("/proc").iterdir() if entry.name.isdigit()]
    except OSError:
        return []
    for entry in proc_entries:
        try:
            name = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
            rss_pages = int((entry / "statm").read_text(encoding="utf-8").split()[1])
        except (OSError, ValueError, IndexError):
            continue
        rows.append({"pid": int(entry.name), "name": name, "rss_bytes": rss_pages * page_size})
    rows.sort(key=lambda row: row["rss_bytes"], reverse=True)
    return rows[:limit]


def _top_processes_windows(limit: int) -> list[dict[str, Any]]:
    """Top processes by working set via EnumProcesses + GetProcessMemoryInfo."""
    api = _windows_api()
    if api is None or api.enum_processes is None:
        return []
    import ctypes

    capacity = 4096
    array = (ctypes.c_uint32 * capacity)()
    needed = ctypes.c_uint32()
    try:
        if not api.enum_processes(array, ctypes.sizeof(array), ctypes.byref(needed)):
            return []
        count = min(needed.value // ctypes.sizeof(ctypes.c_uint32), capacity)
        return _windows_process_rows(api, [int(array[index]) for index in range(count)], limit)
    except Exception:  # noqa: BLE001
        _LOG.debug("Windows top-process scan failed", exc_info=True)
        return []


def _windows_process_rows(
    api: WindowsProcessApi,
    pids: list[int],
    limit: int,
    *,
    stats: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Working-set rows for ``pids`` (highest first, unreadable ones skipped).

    Kept separate from the enumeration call so the mapping can be tested with an
    injected API object on any platform. ``stats`` (optional) receives the opened
    and unreadable counts so a failure can be explained instead of guessed.
    """
    rows: list[dict[str, Any]] = []
    if stats is not None:
        stats.setdefault("opened", 0)
        stats.setdefault("unreadable", 0)
    # Full query rights first, then the Vista+ limited-information right, which
    # many protected but readable processes (and modern Windows builds) accept.
    access_attempts = (0x1000 | 0x0410, 0x1000)
    for pid in pids:
        if pid <= 0:
            continue
        handle = 0
        for access in access_attempts:
            handle = api.open_process(access, 0, pid)
            if handle:
                break
        if not handle:
            continue
        if stats is not None:
            stats["opened"] = stats.get("opened", 0) + 1
        try:
            working_set = api.working_set(handle)
            if working_set is None:
                if stats is not None:
                    stats["unreadable"] = stats.get("unreadable", 0) + 1
                continue
            rows.append(
                {
                    "pid": pid,
                    "name": _windows_process_name(api, handle, pid),
                    "rss_bytes": working_set,
                }
            )
        finally:
            api.close_handle(handle)
    rows.sort(key=lambda row: row["rss_bytes"], reverse=True)
    return rows[:limit]


def _windows_process_name(api: WindowsProcessApi, handle: Any, pid: int) -> str:
    import ctypes

    fallback = f"pid-{pid}"
    if api.image_name is None:
        return fallback
    buffer = ctypes.create_unicode_buffer(1024)
    size = ctypes.c_uint32(len(buffer))
    try:
        ok = api.image_name(
            handle, 0, ctypes.cast(buffer, ctypes.POINTER(ctypes.c_wchar)), ctypes.byref(size)
        )
    except Exception:  # noqa: BLE001
        return fallback
    if not ok:
        return fallback
    return Path(buffer.value).name or fallback


@dataclass(frozen=True, slots=True)
class SystemSnapshot:
    captured_at: str
    platform_name: str
    platform_release: str
    machine: str
    python_version: str
    cpu_percent: float | None
    cpu_method: str
    logical_cores: int | None
    memory: dict[str, Any]
    disks: dict[str, Any]
    process: dict[str, Any]
    network: dict[str, Any]
    gpu: dict[str, Any]
    battery: dict[str, Any] | None
    system_uptime_seconds: float | None
    top_processes: list[dict[str, Any]] = field(default_factory=list)
    top_processes_source: str = "unavailable"
    top_processes_detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "captured_at": self.captured_at,
            "platform": {
                "name": self.platform_name,
                "release": self.platform_release,
                "machine": self.machine,
                "python": self.python_version,
            },
            "cpu": {
                "percent": self.cpu_percent,
                "method": self.cpu_method,
                "logical_cores": self.logical_cores,
            },
            "memory": self.memory,
            "disks": self.disks,
            "process": self.process,
            "network": self.network,
            "gpu": self.gpu,
            "battery": self.battery,
            "system_uptime_seconds": self.system_uptime_seconds,
            "top_processes": self.top_processes,
            "top_processes_source": self.top_processes_source,
            "top_processes_detail": self.top_processes_detail,
        }


class SystemMonitor:
    """Stateful sampler: rates need a previous reading."""

    def __init__(self, *, root_dir: Path, workspace_dir: Path) -> None:
        self._paths = {"install": Path(root_dir), "workspace": Path(workspace_dir)}
        self._lock = threading.Lock()
        self._last_cpu: CpuReading | None = None
        self._last_net: tuple[float, int, int] | None = None
        self._history: deque[dict[str, Any]] = deque(maxlen=120)

    def snapshot(self, *, include_top_processes: bool = True) -> SystemSnapshot:
        with self._lock:
            reading, method = _read_cpu_reading()
            cpu_percent: float | None = None
            if reading is not None and self._last_cpu is not None:
                busy_delta = reading.busy - self._last_cpu.busy
                total_delta = reading.total - self._last_cpu.total
                if total_delta > 0:
                    cpu_percent = _round(max(0.0, min(100.0, 100.0 * busy_delta / total_delta)))
            if reading is not None:
                self._last_cpu = reading
            if cpu_percent is None and method == "loadavg":
                cpu_percent = self._loadavg_percent()

            interfaces, net_method = _read_network_counters()
            total_recv = sum(pair[0] for name, pair in interfaces.items() if name != "lo")
            total_sent = sum(pair[1] for name, pair in interfaces.items() if name != "lo")
            now_mono = time.monotonic()
            recv_rate = sent_rate = None
            if self._last_net is not None and interfaces:
                last_time, last_recv, last_sent = self._last_net
                elapsed = now_mono - last_time
                if elapsed > 0:
                    recv_rate = _round(max(0.0, (total_recv - last_recv) / elapsed), 0)
                    sent_rate = _round(max(0.0, (total_sent - last_sent) / elapsed), 0)
            if interfaces:
                self._last_net = (now_mono, total_recv, total_sent)

            memory = _read_memory()
            rss, rss_method = _process_rss()
            scan = (
                _top_processes()
                if include_top_processes
                else ProcessScan([], "disabled", "top processes not requested")
            )
            snapshot = SystemSnapshot(
                captured_at=datetime.now(UTC).isoformat(),
                platform_name=platform.system(),
                platform_release=platform.release(),
                machine=platform.machine(),
                python_version=platform.python_version(),
                cpu_percent=cpu_percent,
                cpu_method=method,
                logical_cores=os.cpu_count(),
                memory=memory,
                disks=_read_disk(self._paths),
                process={
                    "pid": os.getpid(),
                    "rss_bytes": rss,
                    "rss_method": rss_method,
                    "python_threads": threading.active_count(),
                    "uptime_seconds": _round(time.monotonic() - _STARTED_MONOTONIC),
                },
                network={
                    "interfaces": sorted(interfaces),
                    "bytes_received": total_recv if interfaces else None,
                    "bytes_sent": total_sent if interfaces else None,
                    "receive_bytes_per_second": recv_rate,
                    "send_bytes_per_second": sent_rate,
                    "method": net_method,
                },
                gpu=_read_gpu(),
                battery=_read_battery(),
                system_uptime_seconds=_round(_system_uptime()),
                top_processes=scan.rows,
                top_processes_source=scan.source,
                top_processes_detail=scan.detail,
            )
            self._history.append(
                {
                    "captured_at": snapshot.captured_at,
                    "cpu_percent": snapshot.cpu_percent,
                    "memory_percent": snapshot.memory.get("percent"),
                    "receive_bytes_per_second": recv_rate,
                    "send_bytes_per_second": sent_rate,
                }
            )
            return snapshot

    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history)

    def _loadavg_percent(self) -> float | None:
        try:
            load = os.getloadavg()[0]
            cores = os.cpu_count() or 1
            return _round(max(0.0, min(100.0, 100.0 * load / cores)))
        except (OSError, AttributeError):
            return None


def system_summary_line(snapshot: SystemSnapshot) -> str:
    cpu = f"{snapshot.cpu_percent:.0f}%" if snapshot.cpu_percent is not None else "n/a"
    memory_percent = snapshot.memory.get("percent")
    memory_part = f"{memory_percent:.0f}%" if isinstance(memory_percent, (int, float)) else "n/a"
    install = snapshot.disks.get("install", {})
    disk_percent = install.get("percent")
    disk_part = f"{disk_percent:.0f}%" if isinstance(disk_percent, (int, float)) else "n/a"
    gpu = snapshot.gpu
    if gpu.get("available"):
        gpu_part = f"{gpu.get('name')} {gpu.get('utilization_percent')}%"
    else:
        gpu_part = "unavailable"
    return (
        f"cpu={cpu} ({snapshot.cpu_method}); memory={memory_part}; "
        f"disk={disk_part}; gpu={gpu_part}; process_rss="
        f"{snapshot.process.get('rss_bytes')}; processes={len(snapshot.top_processes)} "
        f"({snapshot.top_processes_source})"
    )
