"""OS-level process sandbox for plugin execution.

Two independent layers, both applied to every plugin tool call:

1. **Process limits** - POSIX ``setrlimit`` (address space, CPU seconds, file
   size, open files, child processes) or a Windows Job Object (process memory,
   active process count, kill-on-close). A hard timeout terminates the whole
   process group / job.
2. **Interpreter-level denial** - the worker runs in isolated mode (``-I -B``)
   with a scrubbed environment and installs a ``sys.addaudithook`` hook that
   refuses sockets, subprocesses, ``os.system``/``os.exec*`` and file access
   outside the paths the plugin's declared capabilities allow.

Layer 1 is enforced by the operating system. Layer 2 is enforced by CPython and
is the reason a plugin cannot quietly open a socket; it is not a defence against
hand-written native code, which is documented as a known limitation rather than
claimed as impossibility.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

__all__ = [
    "MAX_OUTPUT_BYTES",
    "SandboxLimits",
    "SandboxResult",
    "_base_interpreter_paths",
    "describe_sandbox",
    "run_sandboxed",
]

MAX_OUTPUT_BYTES = 200_000
_IS_WINDOWS = sys.platform.startswith("win")
#: ``(interpreter, active-process limit) -> "" if it starts, otherwise why not``.
#: A result is only reusable for the *same* job limits: an interpreter that needs a
#: launcher slot must never be trusted with the strict limit (that was the Windows
#: plugin bug fixed here) and vice versa. Probing is per process.
_PROBE_CACHE: dict[tuple[str, int], str] = {}


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """Everything the sandbox enforces, in one immutable value."""

    timeout_seconds: float = 30.0
    memory_limit_mb: int = 512
    cpu_seconds: int = 30
    max_output_bytes: int = MAX_OUTPUT_BYTES
    allow_network: bool = False
    allow_subprocess: bool = False
    max_processes: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "memory_limit_mb": self.memory_limit_mb,
            "cpu_seconds": self.cpu_seconds,
            "max_output_bytes": self.max_output_bytes,
            "allow_network": self.allow_network,
            "allow_subprocess": self.allow_subprocess,
            "max_processes": self.max_processes,
        }


@dataclass(frozen=True, slots=True)
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int
    mechanism: str
    truncated: bool = False
    #: Set when the child process could not be created at all (missing or
    #: unusable interpreter). ``returncode`` is -1 in that case, and the text
    #: carries the real OS error so a host problem is never reported as a
    #: plugin failure.
    spawn_error: str = ""
    #: The interpreter that actually ran, which is not necessarily ``argv[0]``:
    #: the sandbox substitutes a working interpreter when the requested one
    #: cannot start, and always says so instead of switching silently.
    interpreter: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.spawn_error


def describe_sandbox() -> dict[str, Any]:
    """What this host can actually enforce - reported, never assumed."""

    if _IS_WINDOWS:
        return {
            "platform": "windows",
            "mechanism": "job-object + isolated-interpreter + audit-hook",
            "os_limits": ["process_memory", "active_processes", "kill_on_close"],
            "timeout": "hard kill of the job",
        }
    if os.name == "posix":
        return {
            "platform": "posix",
            "mechanism": "setrlimit + isolated-interpreter + audit-hook",
            "os_limits": [
                "address_space",
                "cpu_seconds",
                "file_size",
                "open_files",
                "child_processes",
            ],
            "timeout": "hard kill of the process group",
        }
    return {
        "platform": os.name,
        "mechanism": "isolated-interpreter + audit-hook",
        "os_limits": [],
        "timeout": "hard kill of the child process",
    }


def _posix_preexec(limits: SandboxLimits) -> Any:  # pragma: no cover - POSIX only
    def _apply() -> None:
        import resource

        resource.setrlimit(
            resource.RLIMIT_AS, (limits.memory_limit_mb * 1024 * 1024,) * 2
        )
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_NOFILE, (64,) * 2)
        if not limits.allow_subprocess:
            resource.setrlimit(
                resource.RLIMIT_NPROC, (max(1, limits.max_processes),) * 2
            )

    return _apply


def _windows_job(process: subprocess.Popen[bytes], limits: SandboxLimits) -> Any:
    """Assign a child to a Job Object with memory and process caps (best effort)."""

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limit_flags = 0x00000100 | 0x00002000  # KILL_ON_JOB_CLOSE | PROCESS_MEMORY
        info.BasicLimitInformation.LimitFlags = limit_flags
        info.ProcessMemoryLimit = limits.memory_limit_mb * 1024 * 1024
        if not limits.allow_subprocess:
            info.BasicLimitInformation.LimitFlags |= 0x00000008  # ACTIVE_PROCESS
            info.BasicLimitInformation.ActiveProcessLimit = max(1, limits.max_processes)
        # Limits are set *before* the process joins the job. Setting them afterwards
        # (as this code used to) was a race: a launcher shim could create its real
        # interpreter child in the window before ``ACTIVE_PROCESS`` was applied, and
        # the parent usually lost that race, which is why every plugin call on that
        # host died with "Unable to create process using ..." and exit code 101.
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            kernel32.CloseHandle(job)
            return None
        process_handle = int(process._handle)  # type: ignore[attr-defined]
        if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process_handle)):
            kernel32.CloseHandle(job)
            return None
        return (kernel32, job)
    except Exception:  # noqa: BLE001 - a missing Win32 path must not crash the app
        return None


def _close_job(handle: Any) -> None:
    if not handle:
        return
    try:
        kernel32, job = handle
        kernel32.CloseHandle(job)
    except Exception:  # noqa: BLE001
        return


#: Non-secret variables a Windows child needs in order to be created at all.
#: ``CreateProcess`` and the C runtime's loader consult these; without them a
#: scrubbed environment makes interpreter start-up fail on some hosts. None of
#: them can carry a credential, and the allow-list is fixed (never inherited
#: wholesale), so the sandbox stays credential-free.
_WINDOWS_ESSENTIALS = (
    "SystemRoot",
    "SystemDrive",
    "windir",
    "ComSpec",
    "PATH",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
    "OS",
)


def _windows_essential(name: str) -> str:
    value = os.environ.get(name, "")
    if value:
        return value
    try:  # pragma: no cover - Windows only
        import ctypes
        import tempfile

        if name.lower() in {"systemroot", "windir"}:
            buffer = ctypes.create_unicode_buffer(260)
            if ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, 260):
                return buffer.value
        if name.lower() == "systemdrive":
            root = os.environ.get("SystemRoot") or os.environ.get("windir") or ""
            return root[:2] or "C:"
        if name.lower() in {"temp", "tmp"}:
            return tempfile.gettempdir()
    except Exception:  # noqa: BLE001 - best effort only
        return ""
    return ""


def _scrubbed_env(*, allow_network: bool) -> dict[str, str]:
    """A minimal environment: no provider keys, no secrets, no proxies."""

    env = {
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "LANG": "C.UTF-8",
    }
    if _IS_WINDOWS:  # pragma: no cover - Windows only
        for name in _WINDOWS_ESSENTIALS:
            value = _windows_essential(name)
            if value:
                env[name] = value
    else:
        env["PATH"] = "/usr/bin:/bin"
        env["TMPDIR"] = "/tmp"
    if not allow_network:
        # Belt-and-braces for libraries that consult proxy variables.
        env["http_proxy"] = env["https_proxy"] = "http://127.0.0.1:9"
        env["no_proxy"] = ""
    return env


def interpreter_candidates() -> list[str]:
    """Every interpreter we are willing to try, most specific first.

    ``sys.executable`` is the normal answer, but on Windows it can point at a
    launcher shim or at an interpreter the caller may not be allowed to start.
    The base interpreter, an unactivated virtual environment and the ones on
    ``PATH`` are tried in turn; a host that cannot run any of them is reported
    as a host problem instead of being blamed on the plugin.
    """

    seen: list[str] = []

    def _add(value: str | None) -> None:
        if not value:
            return
        candidate = str(value)
        if candidate and candidate not in seen:
            seen.append(candidate)

    _add(sys.executable)
    _add(getattr(sys, "_base_executable", None))
    for base in _base_interpreter_paths():
        _add(base)
    for name in ("python3", "python", "python.exe"):
        _add(shutil.which(name))
    return seen


def _base_interpreter_paths() -> list[str]:
    """The interpreter a virtual environment redirects to, derived three ways.

    ``sys._base_executable`` is not available on every Python build, so the base
    prefix and ``pyvenv.cfg`` (``home``) are consulted as well. This is what lets
    a launcher-shim environment find the *real* interpreter and run the worker as
    a single process, with the strict job limit intact.
    """

    found: list[str] = []
    base_prefix = getattr(sys, "base_prefix", "") or ""
    if base_prefix and base_prefix != sys.prefix:
        for name in ("python.exe", "python3", "python"):
            candidate = Path(base_prefix) / name
            if candidate.is_file():
                found.append(str(candidate))
        for name in ("python3", "python"):
            candidate = Path(base_prefix) / "bin" / name
            if candidate.is_file():
                found.append(str(candidate))
        config = Path(sys.prefix) / "pyvenv.cfg"
        if config.is_file():
            try:
                for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
                    key, _, value = line.partition("=")
                    if key.strip().lower() != "home":
                        continue
                    home = Path(value.strip())
                    for name in ("python.exe", "python3", "python"):
                        candidate = home / name
                        if candidate.is_file() and str(candidate) not in found:
                            found.append(str(candidate))
                    for name in ("python3", "python"):
                        candidate = home / "bin" / name
                        if candidate.is_file() and str(candidate) not in found:
                            found.append(str(candidate))
            except OSError:  # pragma: no cover - unreadable config is not fatal
                pass
    return found


def _effective_limits(limits: SandboxLimits, slack: int) -> SandboxLimits:
    """Add launcher slots to the process cap (Windows launcher shims only)."""

    if not slack:
        return limits
    return replace(limits, max_processes=max(1, limits.max_processes) + slack)


def _probe_interpreter(
    target: str, env: dict[str, str], limits: SandboxLimits, cwd: Path
) -> str:
    """Return ``""`` when ``target`` can start *under these exact limits*.

    The probe deliberately runs inside the same job object / rlimits as the real
    worker. An out-of-band ``subprocess.run`` (what this used to do) says nothing
    about whether the sandboxed spawn will work: on Windows a launcher shim starts
    fine unaided and fails the moment the job caps it at one process, which turned
    a host limitation into a misleading plugin error.
    """

    key = (target, max(1, limits.max_processes))
    cached = _PROBE_CACHE.get(key)
    if cached is not None:
        return cached
    reason = ""
    process: subprocess.Popen[bytes] | None = None
    job: Any = None
    if not cwd.is_dir():  # a caller with a stale directory must still get an answer
        cwd = Path.cwd()
    try:
        process = subprocess.Popen(
            [target, "-I", "-B", "-c", "import sys; sys.stdout.write(sys.executable)"],
            **_popen_kwargs(cwd=cwd, env=env, limits=limits),
        )
        job = _windows_job(process, limits) if os.name != "posix" else None
        _stdout, stderr = process.communicate(b"", timeout=30)
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()[-200:]
            reason = f"exit code {process.returncode}: {detail}" if detail else (
                f"exit code {process.returncode}"
            )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        reason = f"{exc.__class__.__name__}: {exc}"
    finally:
        _close_job(job)
        if process is not None and process.poll() is None:  # pragma: no cover
            _terminate(process, None)
    _PROBE_CACHE[key] = reason
    return reason


def _popen_kwargs(*, cwd: Path, env: dict[str, str], limits: SandboxLimits) -> dict[str, Any]:
    """Everything that is identical for every spawn attempt."""

    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": env,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "posix":
        # ``start_new_session`` gives us the process group we later kill, and the
        # preexec hook only applies rlimits (it must never call setsid itself).
        kwargs["start_new_session"] = True
        kwargs["preexec_fn"] = _posix_preexec(limits)
    else:  # pragma: no cover - Windows
        kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        ) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return kwargs


def _spawn(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    limits: SandboxLimits,
) -> tuple[subprocess.Popen[bytes] | None, str, str, SandboxLimits]:
    """Create the worker process, or explain why the host could not.

    Candidates are probed **under the sandbox**, so a candidate is only chosen if
    it can really run inside the job object. On Windows a second pass with one
    extra process slot covers virtual environments whose ``python.exe`` is a
    launcher shim: the shim and the interpreter it starts are then the only two
    processes the job ever allows, and the plugin code still cannot start a third
    (the audit hook refuses subprocess creation inside the worker as well).
    """

    tail = list(argv[1:])
    reasons: list[str] = []
    targets = [argv[0], *interpreter_candidates()] if argv else interpreter_candidates()
    unique: list[str] = []
    for target in targets:
        if target and target not in unique:
            unique.append(target)
    passes = (0, 1) if _IS_WINDOWS and not limits.allow_subprocess else (0,)
    for slack in passes:
        effective = _effective_limits(limits, slack)
        for target in unique:
            if not (Path(target).exists() or shutil.which(target)):
                _note(reasons, f"{target}: not found")
                continue
            reason = _probe_interpreter(target, env, effective, cwd)
            if reason:
                _note(reasons, f"{target}: {reason}")
                continue
            try:
                process = subprocess.Popen(
                    [target, *tail],
                    **_popen_kwargs(cwd=cwd, env=env, limits=effective),
                )
            except (OSError, ValueError) as exc:
                _note(reasons, f"{target}: {exc.__class__.__name__}: {exc}")
                continue
            return process, target, "", effective
    return (
        None,
        "",
        "; ".join(reasons) or "no interpreter candidate is available",
        limits,
    )


def _note(reasons: list[str], message: str) -> None:
    if message not in reasons:
        reasons.append(message)


def run_sandboxed(
    argv: list[str],
    *,
    cwd: Path,
    payload: bytes,
    limits: SandboxLimits,
) -> SandboxResult:
    """Run ``argv`` with the payload on stdin and return a bounded result."""

    started = time.perf_counter()
    mechanism = describe_sandbox()["mechanism"]
    env = _scrubbed_env(allow_network=limits.allow_network)
    process, interpreter, spawn_reason, effective = _spawn(
        argv, cwd=cwd, env=env, limits=limits
    )
    if process is None:
        return SandboxResult(
            returncode=-1,
            stdout="",
            stderr="",
            timed_out=False,
            duration_ms=int((time.perf_counter() - started) * 1000),
            mechanism=mechanism,
            spawn_error=(
                "the sandbox could not start a Python worker on this host "
                f"({spawn_reason}); check that the interpreter runs and that "
                "the sandbox environment is allowed to launch it"
            ),
        )
    if effective.max_processes != limits.max_processes:
        mechanism += f" +launcher-slot(active-process-limit={effective.max_processes})"
    job = _windows_job(process, effective) if os.name != "posix" else None
    timed_out = False
    try:
        stdout, stderr = process.communicate(payload, timeout=limits.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(process, job)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - worst case
            stdout, stderr = b"", b""
    finally:
        _close_job(job)

    truncated = False
    if len(stdout) > limits.max_output_bytes:
        stdout = stdout[: limits.max_output_bytes]
        truncated = True
    if len(stderr) > limits.max_output_bytes:
        stderr = stderr[: limits.max_output_bytes]
        truncated = True
    return SandboxResult(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
        timed_out=timed_out,
        duration_ms=int((time.perf_counter() - started) * 1000),
        mechanism=mechanism,
        truncated=truncated,
        interpreter=interpreter,
    )


def _terminate(process: subprocess.Popen[bytes], job: Any) -> None:
    """Kill the child and everything it may have started."""

    try:
        if job:
            kernel32, job_handle = job
            kernel32.TerminateJobObject(job_handle, 1)
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
        else:  # pragma: no cover - Windows
            process.kill()
    except Exception:  # noqa: BLE001 - killing must never raise into the caller
        return
