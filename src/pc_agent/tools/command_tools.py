"""Generic approved-command runner tool (Phase 12).

`commands.run` executes exactly one allow-listed program with an explicit
argv array - no shell, scrubbed environment, workspace-confined working
directory, bounded time and output. The orchestrator always requires a
fresh user approval of the exact arguments before this tool may run, and
subprocess steps are never auto-retried, so an approval covers precisely
one execution.
"""

from __future__ import annotations

from typing import Any

from ..commands import require_command_policy, run_command
from ..errors import ToolError
from ..policies.policy import Action
from .registry import ToolContext, ToolHandler, ToolRegistry, ToolSpec
from .workspace_tools import _resolve_allowed, relative_posix


def _run_approved(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    executable = str(arguments.get("executable") or "")
    argv = [str(item) for item in (arguments.get("argv") or [])]
    cwd = _resolve_allowed(context, str(arguments.get("cwd") or "."), Action.LIST_FILES)
    if not cwd.is_dir():
        raise ToolError(f"Working directory was not found: {arguments.get('cwd') or '.'}")
    require_command_policy(context.policy_engine, cwd)
    requested = arguments.get("timeout_seconds")
    timeout = min(
        float(requested) if isinstance(requested, (int, float)) else 30.0,
        context.command_timeout_seconds,
    )
    expected = arguments.get("expected_exit_code")
    expected_exit = int(expected) if isinstance(expected, int) else 0
    result = run_command(
        executable=executable,
        argv=argv,
        cwd=cwd,
        timeout_seconds=timeout,
        allowlist=context.command_allowlist,
    )
    if result.timed_out:
        partial = result.stdout.strip()[:500]
        raise ToolError(
            f"Command timed out after {timeout:.0f}s and was killed."
            + (f" Partial output: {partial}" if partial else "")
        )
    return {
        "executable": result.executable,
        "argv": list(result.argv),
        "cwd": relative_posix(context, cwd),
        "exit_code": result.exit_code,
        "expected_exit_code": expected_exit,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "truncated": result.truncated,
        "elapsed_ms": round(result.elapsed_ms, 3),
    }


def _specs() -> list[tuple[ToolSpec, ToolHandler]]:
    return [
        (
            ToolSpec(
                id="commands.run",
                name="Run approved command",
                version="1.0.0",
                description="Run one allow-listed program with an explicit argv array (no shell; approval required; bounded time/output).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "executable": {"type": "string", "minLength": 1, "maxLength": 100},
                        "argv": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 2000},
                            "maxItems": 64,
                        },
                        "cwd": {"type": "string", "minLength": 1, "maxLength": 500},
                        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
                        "expected_exit_code": {"type": "integer", "minimum": 0, "maximum": 255},
                    },
                    "required": ["executable"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "executable": {"type": "string"},
                        "argv": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 2000},
                            "maxItems": 64,
                        },
                        "cwd": {"type": "string"},
                        "exit_code": {"type": ["integer", "null"]},
                        "expected_exit_code": {"type": "integer"},
                        "stdout": {"type": "string", "maxLength": 70000},
                        "stderr": {"type": "string", "maxLength": 70000},
                        "truncated": {"type": "boolean"},
                        "elapsed_ms": {"type": "number"},
                    },
                    "required": [
                        "executable", "argv", "cwd", "exit_code",
                        "expected_exit_code", "stdout", "stderr",
                        "truncated", "elapsed_ms",
                    ],
                },
                permissions=(Action.RUN_SHELL,),
                timeout_seconds=360.0,
            ),
            _run_approved,
        )
    ]


def register_command_tools(registry: ToolRegistry) -> None:
    for spec, handler in _specs():
        registry.register(spec, handler)
