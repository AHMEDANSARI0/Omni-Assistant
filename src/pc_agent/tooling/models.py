"""Data contracts for dynamic pure-compute tool candidates."""

from __future__ import annotations

import copy
import enum
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..errors import ToolBuilderError
from ..tools.registry import ToolSpec, ToolStatus


class DynamicToolStatus(str, enum.Enum):
    PROPOSED = "proposed"
    TESTED = "tested"
    TEST_FAILED = "test_failed"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class ToolTestCase:
    name: str
    arguments: Mapping[str, Any]
    expected_output: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arguments": copy.deepcopy(dict(self.arguments)),
            "expected_output": copy.deepcopy(dict(self.expected_output)),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ToolTestCase:
        allowed = {"name", "arguments", "expected_output"}
        unknown = set(data) - allowed
        if unknown:
            raise ToolBuilderError(
                f"Tool test case contains unknown field(s): {', '.join(sorted(unknown))}."
            )
        name = data.get("name")
        arguments = data.get("arguments")
        expected = data.get("expected_output")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ToolBuilderError("Each tool test case needs a short name.")
        if not isinstance(arguments, Mapping) or not isinstance(expected, Mapping):
            raise ToolBuilderError("Tool test arguments and expected_output must be objects.")
        return cls(
            name=name.strip(),
            arguments=copy.deepcopy(dict(arguments)),
            expected_output=copy.deepcopy(dict(expected)),
        )


@dataclass(frozen=True, slots=True)
class ToolProposal:
    tool_id: str
    name: str
    version: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    source_code: str
    test_cases: tuple[ToolTestCase, ...]
    dependencies: tuple[str, ...] = ()
    timeout_seconds: float = 10.0

    @property
    def normalized_source(self) -> str:
        source = self.source_code.replace("\r\n", "\n").replace("\r", "\n")
        return source if source.endswith("\n") else source + "\n"

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.normalized_source.encode("utf-8")).hexdigest()

    def with_dependencies(self, dependencies: tuple[str, ...]) -> ToolProposal:
        return ToolProposal(
            tool_id=self.tool_id,
            name=self.name,
            version=self.version,
            description=self.description,
            input_schema=copy.deepcopy(dict(self.input_schema)),
            output_schema=copy.deepcopy(dict(self.output_schema)),
            source_code=self.normalized_source,
            test_cases=self.test_cases,
            dependencies=dependencies,
            timeout_seconds=self.timeout_seconds,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "input_schema": copy.deepcopy(dict(self.input_schema)),
            "output_schema": copy.deepcopy(dict(self.output_schema)),
            "source_code": self.normalized_source,
            "test_cases": [test.to_dict() for test in self.test_cases],
            "dependencies": list(self.dependencies),
            "timeout_seconds": self.timeout_seconds,
            "permissions": [],
            "network_required": False,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ToolProposal:
        allowed = {
            "tool_id",
            "name",
            "version",
            "description",
            "input_schema",
            "output_schema",
            "source_code",
            "test_cases",
            "dependencies",
            "timeout_seconds",
            "permissions",
            "network_required",
        }
        unknown = set(data) - allowed
        if unknown:
            raise ToolBuilderError(
                f"Tool proposal contains unknown field(s): {', '.join(sorted(unknown))}."
            )

        permissions = data.get("permissions", [])
        if permissions not in ([], ()):
            raise ToolBuilderError(
                "Phase 2 dynamic tools are pure-compute only and cannot request "
                "filesystem, network, shell or other tool permissions."
            )
        if data.get("network_required", False) is not False:
            raise ToolBuilderError("Phase 2 dynamic tools cannot require network access.")

        strings: dict[str, str] = {}
        for field in ("tool_id", "name", "version", "description", "source_code"):
            value = data.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ToolBuilderError(f"Tool proposal field '{field}' must be a non-empty string.")
            strings[field] = value

        input_schema = data.get("input_schema")
        output_schema = data.get("output_schema")
        if not isinstance(input_schema, Mapping) or not isinstance(output_schema, Mapping):
            raise ToolBuilderError("Tool proposal input_schema/output_schema must be objects.")

        raw_tests = data.get("test_cases")
        if not isinstance(raw_tests, list) or not raw_tests:
            raise ToolBuilderError("A tool proposal needs at least one test case.")
        if len(raw_tests) > 50:
            raise ToolBuilderError("A tool proposal may contain at most 50 test cases.")
        tests: list[ToolTestCase] = []
        for item in raw_tests:
            if not isinstance(item, Mapping):
                raise ToolBuilderError("Each tool test case must be an object.")
            tests.append(ToolTestCase.from_dict(item))

        raw_dependencies = data.get("dependencies") or []
        if not isinstance(raw_dependencies, list) or not all(
            isinstance(item, str) for item in raw_dependencies
        ):
            raise ToolBuilderError("Tool proposal dependencies must be an array of module names.")

        timeout = data.get("timeout_seconds", 10.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ToolBuilderError("Tool proposal timeout_seconds must be a number.")

        proposal = cls(
            tool_id=strings["tool_id"].strip(),
            name=strings["name"].strip(),
            version=strings["version"].strip(),
            description=strings["description"].strip(),
            input_schema=copy.deepcopy(dict(input_schema)),
            output_schema=copy.deepcopy(dict(output_schema)),
            source_code=strings["source_code"],
            test_cases=tuple(tests),
            dependencies=tuple(str(item).strip() for item in raw_dependencies),
            timeout_seconds=float(timeout),
        )
        # Eager JSON check: candidate arguments/results cross a JSON process boundary.
        try:
            json.dumps(proposal.to_dict(), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ToolBuilderError("Tool proposal must be JSON-serializable.") from exc
        return proposal

    def to_tool_spec(self, *, provenance: str) -> ToolSpec:
        return ToolSpec(
            id=self.tool_id,
            name=self.name,
            version=self.version,
            description=self.description,
            input_schema=copy.deepcopy(dict(self.input_schema)),
            output_schema=copy.deepcopy(dict(self.output_schema)),
            permissions=(),
            timeout_seconds=self.timeout_seconds,
            network_required=False,
            provenance=provenance,
            status=ToolStatus.APPROVED,
        )
