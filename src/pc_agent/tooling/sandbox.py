"""Isolated-subprocess execution for approved/tested dynamic tools."""

from __future__ import annotations

import importlib.resources
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ToolBuilderError, ToolError
from .models import ToolProposal, ToolTestCase

MAX_IO_BYTES = 1_000_000
_BASE_ENV_KEYS = (
    "PATH",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)


@dataclass(frozen=True, slots=True)
class CandidateTestResult:
    name: str
    passed: bool
    expected: Mapping[str, Any]
    actual: Mapping[str, Any] | None
    error: str | None
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "expected": dict(self.expected),
            "actual": dict(self.actual) if self.actual is not None else None,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class CandidateTestReport:
    passed: bool
    cases: tuple[CandidateTestResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "cases": [case.to_dict() for case in self.cases]}


def run_candidate_tests(proposal: ToolProposal, *, data_dir: Path) -> CandidateTestReport:
    results: list[CandidateTestResult] = []
    for test_case in proposal.test_cases:
        results.append(_run_test_case(proposal, test_case, data_dir=data_dir))
    return CandidateTestReport(
        passed=all(result.passed for result in results), cases=tuple(results)
    )


def invoke_candidate(
    *,
    tool_id: str,
    source: str,
    arguments: Mapping[str, Any],
    timeout_seconds: float,
    data_dir: Path,
) -> dict[str, Any]:
    result, error, _duration = _execute(
        source=source,
        arguments=arguments,
        timeout_seconds=timeout_seconds,
        data_dir=data_dir,
    )
    if error is not None:
        raise ToolError(f"Dynamic tool '{tool_id}' failed: {error}")
    if result is None:
        raise ToolError(f"Dynamic tool '{tool_id}' returned no result")
    return result


def _run_test_case(
    proposal: ToolProposal, test_case: ToolTestCase, *, data_dir: Path
) -> CandidateTestResult:
    actual, error, duration_ms = _execute(
        source=proposal.normalized_source,
        arguments=test_case.arguments,
        timeout_seconds=proposal.timeout_seconds,
        data_dir=data_dir,
    )
    passed = error is None and actual == dict(test_case.expected_output)
    if error is None and not passed:
        error = "Output did not match expected_output"
    return CandidateTestResult(
        name=test_case.name,
        passed=passed,
        expected=test_case.expected_output,
        actual=actual,
        error=error,
        duration_ms=duration_ms,
    )


def _execute(
    *,
    source: str,
    arguments: Mapping[str, Any],
    timeout_seconds: float,
    data_dir: Path,
) -> tuple[dict[str, Any] | None, str | None, int]:
    try:
        payload = json.dumps(dict(arguments), allow_nan=False)
    except (TypeError, ValueError):
        return None, "Dynamic tool input must be strict JSON (NaN/Infinity are not allowed)", 0
    if len(payload.encode("utf-8")) > MAX_IO_BYTES:
        return None, "Dynamic tool input exceeds the 1 MB limit", 0

    runner_source = _runner_source()
    sandbox_parent = (data_dir / "dynamic-tool-sandboxes").resolve()
    sandbox_parent.mkdir(parents=True, exist_ok=True)

    temp_dir = Path(tempfile.mkdtemp(prefix="candidate-", dir=sandbox_parent))
    started = time.monotonic()
    try:
        (temp_dir / "candidate.py").write_text(source, encoding="utf-8")
        (temp_dir / "runner.py").write_text(runner_source, encoding="utf-8")
        command = [sys.executable, "-I", "-B", "-s", "runner.py"]
        try:
            completed = subprocess.run(
                command,
                cwd=temp_dir,
                env=_minimal_env(temp_dir),
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            duration_ms = int((time.monotonic() - started) * 1000)
            return None, f"Timed out after {timeout_seconds:g} seconds", duration_ms
        except OSError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            return None, f"Could not start sandbox process: {exc}", duration_ms

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = completed.stdout or ""
        if len(stdout.encode("utf-8")) > MAX_IO_BYTES + 10_000:
            return None, "Dynamic tool output exceeded the size limit", duration_ms

        lines = [line for line in stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            detail = _stderr_detail(completed.stderr)
            return (
                None,
                f"Sandbox returned malformed output (exit {completed.returncode}){detail}",
                duration_ms,
            )
        try:
            envelope = json.loads(lines[0])
        except (TypeError, ValueError):
            return None, "Sandbox returned non-JSON output", duration_ms
        if not isinstance(envelope, dict):
            return None, "Sandbox returned a non-object response", duration_ms
        if envelope.get("ok") is not True:
            message = envelope.get("error")
            return None, str(message or "Sandbox execution failed")[:1000], duration_ms
        result = envelope.get("result")
        if not isinstance(result, dict):
            return None, "Sandbox result was not a JSON object", duration_ms
        return result, None, duration_ms
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _runner_source() -> str:
    try:
        resource = (
            importlib.resources.files("pc_agent.tooling")
            .joinpath("sandbox_runner.py")
        )
        return resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise ToolBuilderError("Dynamic-tool sandbox runner resource is missing") from exc


def _minimal_env(temp_dir: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in _BASE_ENV_KEYS if key in os.environ}
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "TEMP": str(temp_dir),
            "TMP": str(temp_dir),
        }
    )
    return env


def _stderr_detail(stderr: str | None) -> str:
    if not stderr:
        return ""
    detail = " ".join(stderr.strip().splitlines()[-3:])[:500]
    return f": {detail}" if detail else ""
