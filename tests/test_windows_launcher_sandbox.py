"""Windows launcher-shim sandbox fix (0.81.2).

The 0.81.2 Windows acceptance run failed ten tests with the same message:

    Unable to create process using '"…\\Python311\\python.exe" -I -B …\\worker.py'
    … exit code 101

Root cause: `D:\\omni-assistant\\.venv\\Scripts\\python.exe` is a **launcher
shim** (a virtualenv-style redirector), so running it is not one process - the
shim creates the real interpreter as a *child*. The sandbox assigned the worker
to a Windows Job Object with ``ACTIVE_PROCESS`` capped at 1, which forbids
exactly that child, and the shim reported the refusal as its own failure. The
pre-flight probe could not catch it because it ran *outside* the job.

These tests pin both halves of the fix: the probe now runs under the real
policy, and a launcher shim gets exactly one extra slot (shim + worker, never a
third process) instead of being silently rejected or silently trusted.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from pc_agent.plugins import sandbox as sandbox_module
from pc_agent.plugins.sandbox import (
    SandboxLimits,
    _effective_limits,
    _scrubbed_env,
    describe_sandbox,
    interpreter_candidates,
    run_sandboxed,
)

SANDBOX_SOURCE = Path(sandbox_module.__file__).read_text(encoding="utf-8")


def test_effective_limits_adds_exactly_one_launcher_slot() -> None:
    strict = SandboxLimits(max_processes=1)
    relaxed = _effective_limits(strict, 1)
    assert strict.max_processes == 1
    assert relaxed.max_processes == 2  # shim + worker, and no room for a third
    assert relaxed.memory_limit_mb == strict.memory_limit_mb
    assert relaxed.timeout_seconds == strict.timeout_seconds


def test_effective_limits_is_unchanged_without_slack() -> None:
    strict = SandboxLimits(max_processes=1)
    assert _effective_limits(strict, 0) is strict


def test_job_limits_are_applied_before_the_process_joins_the_job() -> None:
    """The ordering is the bug: setting limits afterwards is a race."""

    set_info = SANDBOX_SOURCE.index("SetInformationJobObject")
    assign = SANDBOX_SOURCE.index("AssignProcessToJobObject")
    assert set_info < assign, "limits must be in force before the child joins the job"


def test_probe_cache_is_keyed_by_the_process_limit(tmp_path: Path) -> None:
    """A green probe at one limit says nothing about another - that was the bug.

    On a host whose ``sys.executable`` is a launcher shim the *strict* probe is
    expected to fail (that is the real property being fixed), while the relaxed
    probe - the one the ladder then uses - must succeed. The cache must keep both
    answers apart, which is exactly what the old single-key cache got wrong.
    """

    sandbox_module._PROBE_CACHE.clear()
    env = _scrubbed_env(allow_network=False)
    strict = SandboxLimits(timeout_seconds=60, max_processes=1)
    relaxed = _effective_limits(strict, 1)
    strict_reason = sandbox_module._probe_interpreter(sys.executable, env, strict, tmp_path)
    relaxed_reason = sandbox_module._probe_interpreter(sys.executable, env, relaxed, tmp_path)
    assert relaxed_reason == "", relaxed_reason
    if strict_reason:
        # A shim interpreter: the reason must name the failure, not be blank.
        assert "exit code" in strict_reason or "FileNotFound" in strict_reason
    keys = set(sandbox_module._PROBE_CACHE)
    assert (sys.executable, 1) in keys
    assert (sys.executable, 2) in keys
    assert sandbox_module._PROBE_CACHE[(sys.executable, 2)] == ""


def test_the_probe_reports_why_a_candidate_cannot_start(tmp_path: Path) -> None:
    sandbox_module._PROBE_CACHE.clear()
    reason = sandbox_module._probe_interpreter(
        "definitely-not-a-python-interpreter",
        _scrubbed_env(allow_network=False),
        SandboxLimits(timeout_seconds=30),
        tmp_path,
    )
    assert reason  # a real explanation, not an empty string
    assert "FileNotFoundError" in reason or "exit code" in reason


def test_a_launcher_slot_is_used_only_after_the_strict_pass_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def fake_probe(target: str, env: dict, limits: SandboxLimits, cwd: Path) -> str:
        calls.append(limits.max_processes)
        if limits.max_processes <= 1:
            return 'exit code 101: Unable to create process using "python.exe"'
        return ""

    monkeypatch.setattr(sandbox_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(sandbox_module, "_probe_interpreter", fake_probe)
    limits = SandboxLimits(timeout_seconds=60, max_processes=1)
    process, interpreter, reason, effective = sandbox_module._spawn(
        [sys.executable, "-c", "print(1)"], cwd=tmp_path, env=_scrubbed_env(allow_network=False), limits=limits
    )
    try:
        assert process is not None, reason
        assert reason == ""
        assert interpreter == sys.executable
        assert effective.max_processes == 2
        assert calls[0] == 1 and 2 in calls  # strict first, then the launcher slot
    finally:
        if process is not None:
            process.communicate(timeout=30)


def test_used_launcher_slot_is_reported_in_the_mechanism(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_probe(target: str, env: dict, limits: SandboxLimits, cwd: Path) -> str:
        return "exit code 101: launcher cannot start its interpreter" if limits.max_processes <= 1 else ""

    monkeypatch.setattr(sandbox_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(sandbox_module, "_probe_interpreter", fake_probe)
    result = run_sandboxed(
        [sys.executable, "-I", "-B", "-c", "import sys; sys.stdout.write('alive')"],
        cwd=tmp_path,
        payload=b"",
        limits=SandboxLimits(timeout_seconds=60),
    )
    assert result.spawn_error == ""
    assert result.ok is True, result.stderr
    assert "+launcher-slot(active-process-limit=2)" in result.mechanism
    assert describe_sandbox()["mechanism"] in result.mechanism


def test_no_launcher_slot_when_subprocesses_are_allowed(tmp_path: Path) -> None:
    """An explicit multi-process sandbox never needs the Windows second pass."""

    monkeypatch_free = SandboxLimits(max_processes=4, allow_subprocess=True)
    assert _effective_limits(monkeypatch_free, 1).max_processes == 5
    # The ladder itself only adds a pass when the sandbox forbids children.
    source = SANDBOX_SOURCE
    assert "passes = (0, 1) if _IS_WINDOWS and not limits.allow_subprocess else (0,)" in source


def test_the_chosen_interpreter_really_runs_the_worker(tmp_path: Path) -> None:
    """End to end: whatever the sandbox picked must produce a real answer."""

    result = run_sandboxed(
        [sys.executable, "-I", "-B", "-c", "import sys; sys.stdout.write(sys.executable)"],
        cwd=tmp_path,
        payload=b"",
        limits=SandboxLimits(timeout_seconds=60),
    )
    assert result.spawn_error == ""
    assert result.ok is True, result.stderr
    assert result.stdout.strip() == result.interpreter
    assert result.interpreter in interpreter_candidates()


def test_candidate_order_still_prefers_the_running_interpreter() -> None:
    candidates = interpreter_candidates()
    assert candidates[0] == sys.executable
    assert len(candidates) == len(set(candidates))


def test_a_launcher_shim_host_still_has_a_working_candidate(tmp_path: Path) -> None:
    """The ladder must find *some* interpreter that runs under the strict limit.

    This is the Windows acceptance property: on a host where the venv's
    ``python.exe`` is a shim, one of the candidates (the base interpreter) has to
    start with a single process slot, otherwise the last-resort launcher slot is
    the only thing keeping plugins alive.
    """

    sandbox_module._PROBE_CACHE.clear()
    env = _scrubbed_env(allow_network=False)
    strict = SandboxLimits(timeout_seconds=60, max_processes=1)
    reasons = {}
    for candidate in interpreter_candidates():
        if not (Path(candidate).is_file() or shutil.which(candidate)):
            continue
        reasons[candidate] = sandbox_module._probe_interpreter(candidate, env, strict, tmp_path)
    working = [candidate for candidate, reason in reasons.items() if reason == ""]
    assert working, reasons


def test_base_interpreter_paths_are_real_files() -> None:
    """Whatever is derived from base_prefix/pyvenv.cfg must actually exist."""

    from pc_agent.plugins.sandbox import _base_interpreter_paths

    for candidate in _base_interpreter_paths():
        assert Path(candidate).is_file(), candidate


def test_candidates_include_the_base_interpreter_of_a_virtual_environment() -> None:
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if not in_venv:
        pytest.skip("this interpreter is not inside a virtual environment")
    candidates = interpreter_candidates()
    base_candidates = [c for c in candidates if str(sys.prefix) not in c]
    assert base_candidates, "a launcher-shim environment must offer the base interpreter"


def test_the_probe_survives_a_stale_working_directory(tmp_path: Path) -> None:
    sandbox_module._PROBE_CACHE.clear()
    missing = tmp_path / "gone"
    # The relaxed limit is the one that must work everywhere; a strict probe may
    # legitimately refuse a launcher shim (see the cache test above).
    reason = sandbox_module._probe_interpreter(
        sys.executable,
        _scrubbed_env(allow_network=False),
        _effective_limits(SandboxLimits(timeout_seconds=60), 1),
        missing,
    )
    assert reason == "", reason
