"""Safety Kernel for orchestrated tool execution (Transformation Phase 2).

Every tool call made by the autonomous orchestrator passes through
:class:`SafetyKernel`, which provides three guarantees that did not exist
before:

* **Hard timeouts.** A tool can no longer block its caller past its
  declared ``timeout_seconds`` (clamped by the user's
  ``PC_AGENT_SAFETY_MAX_TOOL_SECONDS`` ceiling). Worker-eligible tools run
  in a separate process that is killed at the deadline; the remaining
  factory-bound tools (browser, desktop UI Automation, vision) run in a
  daemon thread whose late result is abandoned and discarded.
* **Isolated worker.** Tools whose context is pure data (files, PDF,
  Office, local images, Git, web fetch, workspace, commands and approved
  dynamic tools) execute in a persistent ``pc_agent.safety_worker``
  subprocess speaking JSON lines. A hung or crashed tool therefore cannot
  hang or crash the CLI/GUI process. Browser/desktop/vision tools stay
  in-process because they need live factories (CDP client, UIA backend,
  budgeted provider router) that cannot cross a process boundary; they
  still get the hard thread timeout, and dynamic tools keep their own
  subprocess sandbox inside their handler.
* **Emergency stop.** A file-backed stop flag under the data directory
  (or ``PC_AGENT_EMERGENCY_STOP=1``) blocks all orchestrated tool
  execution until a human explicitly clears it.

Direct, operator-invoked CLI/GUI commands (for example ``pc-agent pdf
inspect``) are explicit user actions and keep their existing synchronous
behaviour; the kernel guards the autonomous execution path.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .logging_config import redact_text
from .tools.registry import ToolContext, ToolRegistry, ToolResult

EMERGENCY_STOP_FILENAME = "emergency_stop.json"
EMERGENCY_STOP_MESSAGE = (
    "Emergency stop is engaged; tool execution is blocked. "
    "Clear it with 'pc-agent safety resume' when it is safe to continue."
)

_WORKER_EXCLUDED_PREFIXES = ("browser.", "desktop.", "knowledge.")
_WORKER_EXCLUDED_TOOL_IDS = frozenset({"image.analyze", "pdf.ocr"})
_WORKER_GRACE_SECONDS = 5.0
_THREAD_GRACE_SECONDS = 2.0
_WORKER_START_TIMEOUT_SECONDS = 30.0
_MAX_WORKER_LINE_BYTES = 64 * 1024 * 1024


def arguments_digest(arguments: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON encoding of tool arguments.

    Approval records store this digest instead of raw arguments so typed
    text, command lines and other sensitive values are never persisted in
    the approval audit trail while exact-scope matching stays possible.
    """

    canonical = json.dumps(
        dict(arguments),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def worker_eligible(tool_id: str) -> bool:
    """Whether a tool may run in the isolated worker process."""

    if tool_id in _WORKER_EXCLUDED_TOOL_IDS:
        return False
    return not tool_id.startswith(_WORKER_EXCLUDED_PREFIXES)


@dataclass(frozen=True, slots=True)
class EmergencyStopState:
    engaged: bool
    source: str = "none"  # "none" | "file" | "environment"
    reason: str = ""
    engaged_at: str | None = None
    engaged_by: str | None = None

    def summary(self) -> str:
        if not self.engaged:
            return "emergency_stop=off"
        detail = f"; source={self.source}"
        if self.engaged_at:
            detail += f"; since={self.engaged_at}"
        if self.reason:
            detail += f"; reason={self.reason}"
        return f"emergency_stop=ENGAGED{detail}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "engaged": self.engaged,
            "source": self.source,
            "reason": self.reason,
            "engaged_at": self.engaged_at,
            "engaged_by": self.engaged_by,
        }


class EmergencyStop:
    """File-backed emergency stop with an environment override.

    The flag lives in the data directory so it survives process restarts
    and is honoured by every pc-agent process using the same install.
    """

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir.expanduser() / EMERGENCY_STOP_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def status(self) -> EmergencyStopState:
        env_value = os.environ.get("PC_AGENT_EMERGENCY_STOP", "").strip().lower()
        if env_value in {"1", "true", "yes", "on"}:
            return EmergencyStopState(
                engaged=True,
                source="environment",
                reason="PC_AGENT_EMERGENCY_STOP is set",
            )
        try:
            parsed = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return EmergencyStopState(engaged=False)
        if not isinstance(parsed, dict) or parsed.get("engaged") is not True:
            return EmergencyStopState(engaged=False)
        return EmergencyStopState(
            engaged=True,
            source="file",
            reason=str(parsed.get("reason") or "")[:200],
            engaged_at=(
                str(parsed["engaged_at"]) if parsed.get("engaged_at") else None
            ),
            engaged_by=(
                str(parsed["engaged_by"]) if parsed.get("engaged_by") else None
            ),
        )

    def engaged(self) -> bool:
        return self.status().engaged

    def engage(self, *, reason: str = "", engaged_by: str = "cli") -> EmergencyStopState:
        self._write(
            {
                "engaged": True,
                "reason": redact_text(reason.strip())[:200],
                "engaged_by": engaged_by[:40],
                "engaged_at": datetime.now(UTC).isoformat(),
            }
        )
        return self.status()

    def clear(self, *, cleared_by: str = "cli") -> EmergencyStopState:
        self._write(
            {
                "engaged": False,
                "cleared_by": cleared_by[:40],
                "cleared_at": datetime.now(UTC).isoformat(),
            }
        )
        return self.status()

    def _write(self, payload: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(
            f".{self._path.name}.tmp-{secrets.token_hex(3)}"
        )
        try:
            temporary.write_text(
                json.dumps(dict(payload), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self._path)
        finally:
            if temporary.exists():
                temporary.unlink()


@dataclass(slots=True)
class SafetyKernelStats:
    invocations: int = 0
    worker_invocations: int = 0
    in_process_invocations: int = 0
    hard_timeouts: int = 0
    worker_restarts: int = 0
    worker_crashes: int = 0
    abandoned_threads: int = 0
    emergency_blocks: int = 0
    fallbacks: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "invocations": self.invocations,
            "worker_invocations": self.worker_invocations,
            "in_process_invocations": self.in_process_invocations,
            "hard_timeouts": self.hard_timeouts,
            "worker_restarts": self.worker_restarts,
            "worker_crashes": self.worker_crashes,
            "abandoned_threads": self.abandoned_threads,
            "emergency_blocks": self.emergency_blocks,
            "fallbacks": self.fallbacks,
        }


class _WorkerUnavailable(Exception):
    """The worker process could not be started at all."""


class _WorkerCrashed(Exception):
    """The worker process died after a request was sent."""


class _WorkerTimedOut(Exception):
    """The worker missed its hard deadline and was killed."""


class _WorkerClient:
    """Persistent JSON-lines client for ``pc_agent.safety_worker``."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[str | None] | None = None
        self._stderr_lines: deque[str] = deque(maxlen=50)
        self._next_id = 0

    def invoke(
        self,
        *,
        tool_id: str,
        arguments: Mapping[str, Any],
        context_snapshot: Mapping[str, Any],
        version: str | None,
        deadline_seconds: float,
    ) -> ToolResult:
        process = self._ensure_started()
        assert self._responses is not None
        self._next_id += 1
        request_id = self._next_id
        payload = {
            "id": request_id,
            "tool_id": tool_id,
            "arguments": dict(arguments),
            "version": version,
            "context": dict(context_snapshot),
            "Database": {
                "path": str(self._settings.database_path),
                "data_dir": str(self._settings.data_dir),
            },
        }
        started = time.perf_counter()
        try:
            assert process.stdin is not None
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._kill()
            raise _WorkerCrashed(f"could not write to the worker process: {exc}") from exc

        deadline = started + deadline_seconds
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                self._kill()
                raise _WorkerTimedOut(
                    f"worker did not answer within {deadline_seconds:.1f}s"
                )
            try:
                line = self._responses.get(timeout=remaining)
            except queue.Empty:
                self._kill()
                raise _WorkerTimedOut(
                    f"worker did not answer within {deadline_seconds:.1f}s"
                ) from None
            if line is None:
                detail = self._stderr_tail()
                self._kill()
                raise _WorkerCrashed(
                    "worker process exited while running the tool" + detail
                )
            if len(line.encode("utf-8", errors="ignore")) > _MAX_WORKER_LINE_BYTES:
                self._kill()
                raise _WorkerCrashed("worker returned an oversized response")
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                # A tool printing to stdout must not corrupt the protocol;
                # ignore the noise line and keep waiting for the response.
                self._stderr_lines.append(f"worker-stdout: {line.strip()[:200]}")
                continue
            if not isinstance(parsed, dict) or parsed.get("id") != request_id:
                self._stderr_lines.append("worker protocol: unmatched response")
                continue
            if "fatal" in parsed:
                return ToolResult(
                    tool_id=tool_id,
                    version=version or "unknown",
                    success=False,
                    error=redact_text(
                        f"Isolated worker could not run the tool: {parsed['fatal']}"
                    ),
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                )
            raw = parsed.get("result")
            if not isinstance(raw, dict):
                self._kill()
                raise _WorkerCrashed("worker returned a malformed result")
            output = raw.get("output")
            return ToolResult(
                tool_id=str(raw.get("tool_id") or tool_id),
                version=str(raw.get("version") or version or "unknown"),
                success=bool(raw.get("success")),
                output=output if isinstance(output, Mapping) else None,
                error=(str(raw["error"]) if raw.get("error") else None),
                elapsed_ms=float(raw.get("elapsed_ms") or 0.0),
            )

    def close(self) -> None:
        self._kill()

    def _ensure_started(self) -> subprocess.Popen[str]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        self._kill()
        command = [sys.executable, "-m", "pc_agent.safety_worker"]
        env = dict(os.environ)
        env["PC_AGENT_SAFETY_WORKER"] = "1"
        env.setdefault("PYTHONIOENCODING", "utf-8")
        creationflags = 0
        if os.name == "nt":
            creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        cwd = self._settings.root_dir
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd) if cwd.is_dir() else None,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise _WorkerUnavailable(f"could not start the worker: {exc}") from exc
        self._process = process
        self._responses = queue.Queue()
        self._stderr_lines.clear()
        threading.Thread(
            target=self._pump_stdout, args=(process,), daemon=True,
            name="pc-agent-safety-worker-stdout",
        ).start()
        threading.Thread(
            target=self._pump_stderr, args=(process,), daemon=True,
            name="pc-agent-safety-worker-stderr",
        ).start()
        try:
            ready_line = self._responses.get(timeout=_WORKER_START_TIMEOUT_SECONDS)
        except queue.Empty:
            self._kill()
            raise _WorkerUnavailable("worker did not become ready in time") from None
        if ready_line is None:
            detail = self._stderr_tail()
            self._kill()
            raise _WorkerUnavailable("worker exited during startup" + detail)
        try:
            ready = json.loads(ready_line)
        except json.JSONDecodeError:
            ready = None
        if not isinstance(ready, dict) or ready.get("ready") is not True:
            self._kill()
            raise _WorkerUnavailable("worker sent an invalid readiness line")
        return process

    def _pump_stdout(self, process: subprocess.Popen[str]) -> None:
        responses = self._responses
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if responses is not None:
                    responses.put(line)
        except (OSError, ValueError):
            pass
        if responses is not None:
            responses.put(None)

    def _pump_stderr(self, process: subprocess.Popen[str]) -> None:
        try:
            assert process.stderr is not None
            for line in process.stderr:
                self._stderr_lines.append(line.rstrip()[:300])
        except (OSError, ValueError):
            pass

    def _stderr_tail(self) -> str:
        if not self._stderr_lines:
            return ""
        return "; stderr tail: " + redact_text(" | ".join(list(self._stderr_lines)[-3:]))[:400]

    def _kill(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


def context_snapshot(context: ToolContext) -> dict[str, Any]:
    """JSON-safe snapshot of the primitive parts of a tool context."""

    policy = context.policy_engine
    return {
        "workspace_dir": str(context.workspace_dir),
        "approved_read_roots": [str(path) for path in context.approved_read_roots],
        "network_access_allowed": bool(context.network_access_allowed),
        "allow_private_network": bool(context.allow_private_network),
        "desktop_open_allowed": bool(context.desktop_open_allowed),
        "web_timeout_seconds": float(context.web_timeout_seconds),
        "web_max_bytes": int(context.web_max_bytes),
        "desktop_automation_allowed": bool(context.desktop_automation_allowed),
        "desktop_automation_timeout_seconds": float(
            context.desktop_automation_timeout_seconds
        ),
        "vision_allowed": bool(context.vision_allowed),
        "vision_max_image_bytes": int(context.vision_max_image_bytes),
        "command_allowlist": [str(item) for item in context.command_allowlist],
        "command_timeout_seconds": float(context.command_timeout_seconds),
        "browser_allowed": bool(context.browser_allowed),
        "browser_port": int(context.browser_port),
        "browser_timeout_seconds": float(context.browser_timeout_seconds),
        "subprocess_allowed": bool(policy.subprocess_allowed),
    }


class SafetyKernel:
    """Single guarded entry point for orchestrated tool execution."""

    def __init__(self, *, settings: Settings, registry: ToolRegistry) -> None:
        self._settings = settings
        self._registry = registry
        self._emergency_stop = EmergencyStop(settings.data_dir)
        self._worker = _WorkerClient(settings)
        self._stats = SafetyKernelStats()
        self._lock = threading.Lock()

    @property
    def stats(self) -> SafetyKernelStats:
        return self._stats

    @property
    def emergency_stop(self) -> EmergencyStop:
        return self._emergency_stop

    def summary(self) -> str:
        settings = self._settings
        return (
            f"hard_timeouts={'on' if settings.safety_hard_timeouts else 'OFF'}; "
            f"worker_isolation={'on' if settings.safety_worker_isolation else 'off'}; "
            f"max_tool_seconds={settings.safety_max_tool_seconds:g}; "
            f"{self._emergency_stop.status().summary()}"
        )

    def close(self) -> None:
        self._worker.close()

    def invoke(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        context: ToolContext,
        *,
        version: str | None = None,
    ) -> ToolResult:
        with self._lock:
            return self._invoke_locked(tool_id, arguments, context, version=version)

    def _registry_invoke(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        context: ToolContext,
        *,
        version: str | None,
    ) -> ToolResult:
        # Call positionally when no version is pinned so registries with
        # the historical 3-argument invoke signature keep working.
        if version is None:
            return self._registry.invoke(tool_id, arguments, context)
        return self._registry.invoke(tool_id, arguments, context, version=version)

    def _invoke_locked(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        context: ToolContext,
        *,
        version: str | None,
    ) -> ToolResult:
        started = time.perf_counter()
        self._stats.invocations += 1

        def failure(message: str, tool_version: str) -> ToolResult:
            return ToolResult(
                tool_id=tool_id,
                version=tool_version,
                success=False,
                error=redact_text(message),
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )

        if self._emergency_stop.engaged():
            self._stats.emergency_blocks += 1
            selected = self._registry.get(tool_id, version)
            return failure(
                EMERGENCY_STOP_MESSAGE,
                selected.spec.version if selected is not None else (version or "unknown"),
            )

        selected = self._registry.get(tool_id, version)
        if selected is None:
            self._stats.in_process_invocations += 1
            return self._registry_invoke(tool_id, arguments, context, version=version)

        effective_timeout = min(
            float(selected.spec.timeout_seconds),
            float(self._settings.safety_max_tool_seconds),
        )

        # The worker rebuilds the standard runtime registry, so it may
        # only serve a plain ToolRegistry. A subclass overriding invoke()
        # - or an instance whose invoke was replaced - keeps authority
        # over its own execution and stays on the in-process path.
        standard_registry = (
            getattr(self._registry.invoke, "__func__", None) is ToolRegistry.invoke
        )
        if (
            self._settings.safety_worker_isolation
            and standard_registry
            and worker_eligible(tool_id)
        ):
            snapshot = context_snapshot(context)
            try:
                json.dumps({"arguments": dict(arguments), "context": snapshot})
                serializable = True
            except (TypeError, ValueError):
                serializable = False
            if serializable:
                try:
                    result = self._worker.invoke(
                        tool_id=tool_id,
                        arguments=arguments,
                        context_snapshot=snapshot,
                        version=version,
                        deadline_seconds=effective_timeout + _WORKER_GRACE_SECONDS,
                    )
                    self._stats.worker_invocations += 1
                    if (
                        not result.success
                        and result.error is not None
                        and result.error.startswith("Tool not found")
                    ):
                        # The parent registry carries an ad-hoc tool the
                        # worker cannot rebuild; run it in-process instead.
                        self._stats.fallbacks += 1
                    else:
                        return result
                except _WorkerTimedOut:
                    self._stats.worker_invocations += 1
                    self._stats.hard_timeouts += 1
                    self._stats.worker_restarts += 1
                    return failure(
                        f"Tool exceeded its hard timeout of {effective_timeout:g} "
                        "seconds; the isolated worker process was killed and its "
                        "result discarded.",
                        selected.spec.version,
                    )
                except _WorkerCrashed as exc:
                    self._stats.worker_invocations += 1
                    self._stats.worker_crashes += 1
                    self._stats.worker_restarts += 1
                    return failure(
                        f"The isolated worker process crashed while running the tool ({exc}). "
                        "The tool was not re-run in-process.",
                        selected.spec.version,
                    )
                except _WorkerUnavailable:
                    # The worker never started (for example a frozen build
                    # without a usable interpreter). Dynamic tools still run
                    # their own subprocess sandbox from the in-process path;
                    # built-ins keep the hard thread timeout below.
                    self._stats.fallbacks += 1

        self._stats.in_process_invocations += 1
        if not self._settings.safety_hard_timeouts:
            return self._registry_invoke(tool_id, arguments, context, version=version)
        return self._invoke_in_thread(
            tool_id,
            arguments,
            context,
            version=version,
            effective_timeout=effective_timeout,
            tool_version=selected.spec.version,
            started=started,
        )

    def _invoke_in_thread(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        context: ToolContext,
        *,
        version: str | None,
        effective_timeout: float,
        tool_version: str,
        started: float,
    ) -> ToolResult:
        outcome: dict[str, Any] = {}

        def runner() -> None:
            try:
                outcome["result"] = self._registry_invoke(
                    tool_id, arguments, context, version=version
                )
            except BaseException as exc:  # noqa: BLE001 - re-raised in the caller below.
                outcome["error"] = exc

        # Budget tracking (and any future context-local state) lives in
        # ContextVars, which a fresh thread would otherwise not inherit;
        # the copied context still references the same tracker object.
        caller_context = contextvars.copy_context()
        thread = threading.Thread(
            target=caller_context.run,
            args=(runner,),
            daemon=True,
            name=f"pc-agent-tool-{tool_id}",
        )
        thread.start()
        thread.join(timeout=effective_timeout + _THREAD_GRACE_SECONDS)
        if thread.is_alive():
            self._stats.hard_timeouts += 1
            self._stats.abandoned_threads += 1
            return ToolResult(
                tool_id=tool_id,
                version=tool_version,
                success=False,
                error=redact_text(
                    f"Tool exceeded its hard timeout of {effective_timeout:g} "
                    "seconds; its result was discarded. This tool runs "
                    "in-process, so the abandoned worker thread may finish "
                    "in the background without checkpointing anything."
                ),
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
        if "error" in outcome:
            # A registry-level failure (not a tool failure - the registry
            # converts those to ToolResult) used to propagate straight to
            # the orchestrator; preserve that crash semantics.
            raise outcome["error"]
        return outcome["result"]


__all__ = [
    "EMERGENCY_STOP_FILENAME",
    "EMERGENCY_STOP_MESSAGE",
    "EmergencyStop",
    "EmergencyStopState",
    "SafetyKernel",
    "SafetyKernelStats",
    "arguments_digest",
    "context_snapshot",
    "worker_eligible",
]
