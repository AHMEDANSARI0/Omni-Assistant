"""Approved subprocess execution engine (Phase 12).

One narrow, auditable doorway for running external programs:

- argv arrays only - a shell is never invoked (``shell=False`` everywhere);
- the executable must be a bare program name on the configured allow-list,
  resolved through PATH by the OS;
- the working directory is confined to the workspace by policy at the tool
  layer (``Action.RUN_SHELL``);
- the child gets a minimal scrubbed environment (no agent API keys), no
  stdin, a hard timeout and bounded captured output.

Git tools and the generic approved-command runner build on this module;
nothing else should spawn subprocesses for user work.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import ToolError
from .policies.policy import Action, ActionRequest, PolicyEngine

MAX_OUTPUT_BYTES = 64 * 1024
MAX_ARGV_ITEMS = 64
MAX_ARGV_ITEM_CHARS = 2000

_SAFE_ENV_KEYS = (
    "PATH",
    "PATHEXT",
    "SystemRoot",
    "windir",
    "COMSPEC",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)
# Characters that cmd.exe would interpret even inside the quoted command line
# used for .cmd/.bat shims on Windows. Arguments containing them are refused
# for batch shims instead of being escaped into a shell.
_BATCH_UNSAFE = re.compile(r'[%^&|<>"\r\n]')


@dataclass(frozen=True, slots=True)
class CommandResult:
    executable: str
    argv: tuple[str, ...]
    cwd: str
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    elapsed_ms: float


def require_command_policy(policy_engine: PolicyEngine, cwd: Path) -> None:
    """Fail closed unless approved command execution may run in ``cwd``."""

    if not policy_engine.subprocess_allowed:
        raise ToolError(
            "Subprocess execution is not allowed: it is disabled by policy "
            "(set PC_AGENT_ALLOW_SUBPROCESS=1 in .env to enable approved "
            "command execution)."
        )
    decision = policy_engine.evaluate(ActionRequest(Action.RUN_SHELL, cwd))
    if not decision.allowed:
        raise ToolError(f"Policy {decision.outcome.value}: {decision.reason}")


def scrubbed_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Minimal child environment: system plumbing only, never secrets."""

    environ = os.environ if source is None else source
    wanted = {name.upper() for name in _SAFE_ENV_KEYS}
    env = {key: value for key, value in environ.items() if key.upper() in wanted}
    if not any(key.upper() == "PATH" for key in env):
        env["PATH"] = os.defpath
    # A child must never block on an interactive credential prompt.
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def validate_windows_batch_argv(argv: Sequence[str]) -> None:
    for item in argv:
        if _BATCH_UNSAFE.search(item):
            raise ToolError(
                "This program is a Windows batch shim (.cmd/.bat); arguments "
                "containing % ^ & | < > \" or newlines are refused rather "
                "than escaped into cmd.exe."
            )


def _allowlist_key(name: str) -> str:
    key = name.strip().lower()
    return key.removesuffix(".exe")


def _decode_capped(data: bytes, cap: int) -> tuple[str, bool]:
    return data[:cap].decode("utf-8", errors="replace"), len(data) > cap


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass


def run_command(
    *,
    executable: str,
    argv: Sequence[str],
    cwd: Path,
    timeout_seconds: float,
    allowlist: Sequence[str],
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> CommandResult:
    """Run one allow-listed program with an argv array and no shell."""

    name = executable.strip()
    if not name or "/" in name or "\\" in name or name in {".", ".."} or len(name) > 100:
        raise ToolError(
            "Executable must be a bare program name like 'git' or 'python', "
            "not a path."
        )
    allowed = {_allowlist_key(item) for item in allowlist}
    if _allowlist_key(name) not in allowed:
        listing = ", ".join(sorted(allowed)) or "none"
        raise ToolError(
            f"Command not allowed: '{name}' is not on the approved program "
            f"list ({listing}). Add it via PC_AGENT_COMMAND_ALLOWLIST only "
            "if you trust it."
        )
    clean_argv: list[str] = []
    for item in argv:
        if not isinstance(item, str):
            raise ToolError("Command arguments must be strings.")
        if "\x00" in item or len(item) > MAX_ARGV_ITEM_CHARS:
            raise ToolError("A command argument is empty of meaning or too long.")
        clean_argv.append(item)
    if len(clean_argv) > MAX_ARGV_ITEMS:
        raise ToolError(f"Too many command arguments (max {MAX_ARGV_ITEMS}).")
    if timeout_seconds <= 0:
        raise ToolError("Command timeout must be positive.")

    resolved = shutil.which(name)
    if resolved is None:
        raise ToolError(f"Program not found on PATH: {name}")

    command: list[str]
    if os.name == "nt" and Path(resolved).suffix.lower() in {".cmd", ".bat"}:
        # Batch shims (npm & friends) cannot be CreateProcess'd directly.
        # Invoke cmd.exe explicitly (still shell=False) with a single
        # list2cmdline-quoted string, after refusing metacharacters.
        validate_windows_batch_argv(clean_argv)
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        command = [
            comspec,
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline([resolved, *clean_argv]),
        ]
    else:
        command = [resolved, *clean_argv]

    started = time.perf_counter()
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=scrubbed_environment(),
            shell=False,
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        raise ToolError(f"Could not start {name}: {exc}") from exc
    timed_out = False
    try:
        out_bytes, err_bytes = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(process)
        out_bytes, err_bytes = process.communicate()
    elapsed_ms = (time.perf_counter() - started) * 1000
    stdout, out_truncated = _decode_capped(out_bytes, max_output_bytes)
    stderr, err_truncated = _decode_capped(err_bytes, max_output_bytes)
    return CommandResult(
        executable=name,
        argv=tuple(clean_argv),
        cwd=str(cwd),
        exit_code=None if timed_out else process.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        truncated=out_truncated or err_truncated,
        elapsed_ms=elapsed_ms,
    )
