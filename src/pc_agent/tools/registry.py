"""Versioned in-code registry for approved built-in tools.

Phase 1B intentionally keeps built-in tool definitions in reviewed source
code. Dynamic, persisted tool registration belongs to Phase 2, where generated
candidates must pass validation and approval before they reach this registry.
"""

from __future__ import annotations

import enum
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import PcAgentError, ToolError
from ..logging_config import redact_text
from ..policies.policy import Action, PolicyEngine
from .schema import validate_object

ToolHandler = Callable[[dict[str, Any], "ToolContext"], dict[str, Any]]
_TOOL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")


class ToolStatus(str, enum.Enum):
    APPROVED = "approved"
    EXPERIMENTAL = "experimental"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class ToolContext:
    workspace_dir: Path
    policy_engine: PolicyEngine
    approved_read_roots: tuple[Path, ...] = ()

    @classmethod
    def for_workspace(
        cls,
        workspace_dir: Path,
        approved_read_roots: tuple[Path, ...] = (),
    ) -> ToolContext:
        resolved = workspace_dir.expanduser().resolve()
        roots = tuple(
            sorted(
                {
                    root.expanduser().resolve()
                    for root in approved_read_roots
                    if root.expanduser().resolve() != resolved
                },
                key=str,
            )
        )
        return cls(
            workspace_dir=resolved,
            policy_engine=PolicyEngine(resolved, approved_read_roots=roots),
            approved_read_roots=roots,
        )


@dataclass(frozen=True, slots=True)
class ToolSpec:
    id: str
    name: str
    version: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    permissions: tuple[Action, ...]
    timeout_seconds: float = 30.0
    network_required: bool = False
    provenance: str = "builtin"
    status: ToolStatus = ToolStatus.APPROVED

    def __post_init__(self) -> None:
        if not _TOOL_ID_PATTERN.fullmatch(self.id):
            raise ValueError(f"Invalid tool id: {self.id}")
        _version_tuple(self.version)
        if self.timeout_seconds <= 0 or self.timeout_seconds > 600:
            raise ValueError("Tool timeout must be greater than 0 and at most 600 seconds.")


@dataclass(slots=True)
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_id: str
    version: str
    success: bool
    output: Mapping[str, Any] | None = None
    error: str | None = None
    elapsed_ms: float = 0.0


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], RegisteredTool] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> RegisteredTool:
        key = (spec.id, spec.version)
        if key in self._tools:
            raise ToolError(f"Tool {spec.id} version {spec.version} is already registered.")
        registered = RegisteredTool(
            spec=spec,
            handler=handler,
            enabled=spec.status is not ToolStatus.DISABLED,
        )
        self._tools[key] = registered
        return registered

    def list_tools(self, *, include_disabled: bool = True) -> list[RegisteredTool]:
        tools = [
            tool for tool in self._tools.values() if include_disabled or tool.enabled
        ]
        return sorted(tools, key=lambda tool: (tool.spec.id, _version_tuple(tool.spec.version)))

    def get(self, tool_id: str, version: str | None = None) -> RegisteredTool | None:
        candidates = [tool for tool in self._tools.values() if tool.spec.id == tool_id]
        if not candidates:
            return None
        if version is not None:
            return self._tools.get((tool_id, version))
        return max(candidates, key=lambda tool: _version_tuple(tool.spec.version))

    def set_enabled(self, tool_id: str, enabled: bool, version: str | None = None) -> RegisteredTool:
        tool = self.get(tool_id, version)
        if tool is None:
            raise ToolError(f"Tool not found: {tool_id}")
        tool.enabled = enabled
        return tool

    def invoke(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        context: ToolContext,
        *,
        version: str | None = None,
    ) -> ToolResult:
        started = time.perf_counter()
        selected: RegisteredTool | None = None

        def failure(message: str) -> ToolResult:
            return ToolResult(
                tool_id=tool_id,
                version=selected.spec.version if selected is not None else (version or "unknown"),
                success=False,
                error=redact_text(message),
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )

        try:
            selected = self._select(tool_id, version)
            validated = validate_object(selected.spec.input_schema, arguments, label="arguments")
            output = selected.handler(validated, context)
            if not isinstance(output, Mapping):
                raise ToolError("Tool returned a non-object result.")
            validated_output = validate_object(
                selected.spec.output_schema, output, label="output"
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            if elapsed_ms > selected.spec.timeout_seconds * 1000:
                return failure(
                    "Tool exceeded its declared timeout; its result was discarded. "
                    "Hard timeout enforcement requires the isolated worker planned later."
                )
            return ToolResult(
                tool_id=tool_id,
                version=selected.spec.version,
                success=True,
                output=validated_output,
                elapsed_ms=elapsed_ms,
            )
        except PcAgentError as exc:
            return failure(str(exc))
        except Exception as exc:  # noqa: BLE001 - registry boundary: report, never crash callers.
            return failure(f"Unexpected tool error ({type(exc).__name__}): {exc}")

    def _select(self, tool_id: str, version: str | None) -> RegisteredTool:
        candidates = [tool for tool in self._tools.values() if tool.spec.id == tool_id]
        if not candidates:
            raise ToolError(f"Tool not found: {tool_id}")
        if version is not None:
            selected = self._tools.get((tool_id, version))
            if selected is None:
                raise ToolError(f"Tool {tool_id} version {version} was not found.")
            candidates = [selected]
        else:
            candidates.sort(key=lambda tool: _version_tuple(tool.spec.version), reverse=True)

        for candidate in candidates:
            if candidate.enabled and candidate.spec.status is ToolStatus.APPROVED:
                return candidate
        raise ToolError(f"Tool {tool_id} has no enabled, approved version.")


def _version_tuple(version: str) -> tuple[int, ...]:
    try:
        parts = tuple(int(part) for part in version.split("."))
    except ValueError as exc:
        raise ValueError(f"Tool version must be numeric and dot-separated: {version}") from exc
    if not parts or any(part < 0 for part in parts):
        raise ValueError(f"Invalid tool version: {version}")
    return parts
