"""0.82.6: one command that runs every gate on the user's own machine.

"Run the tests and send me the output" is not a verification story: the output of
a suite says nothing about *which* Python ran it, whether the installed package is
the tree that was patched, or what `ruff` and the release audit thought. This
script exists so the Windows confirmation is one command with a machine-readable
receipt.

The tests below are about the properties that make the receipt trustworthy:

* every command is an **argv list** run with the current interpreter - no shell,
  so what ran is what the report says ran;
* the import gate fails when the module comes from somewhere else (a stale
  install that shadows the editable tree - the trap that produced a phantom
  "version 0.82.3" failure);
* a gate that cannot run is reported as a *failure* it could not start, never as
  a quiet pass, and skips inside the suite are counted and shown;
* the exit code is 1 when any gate failed, and the JSON is written even then.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "verify_windows.py"


def _load_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("verify_windows", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve their module through sys.modules; registering the
    # freshly created module first is what makes that lookup succeed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------- script

def test_the_script_ships_and_is_documented() -> None:
    assert SCRIPT.is_file()
    source = SCRIPT.read_text(encoding="utf-8")
    for flag in ("--only", "--json", "--skip-suite", "--report-dir", "--no-report"):
        assert flag in source, flag
    assert "shell=True" not in source and "shell = True" not in source
    assert "subprocess.run(" in source

    guide = (ROOT / "INSTALLATION_GUIDE.md").read_text(encoding="utf-8")
    assert "verify_windows.py" in guide
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "verify_windows.py" in readme


def test_gate_order_puts_cheap_checks_first() -> None:
    module = _load_module()
    order = list(module.GATES)
    assert order[-2:] == ["suite", "production"], order
    assert order.index("ruff") < order.index("suite")
    assert order.index("import") < order.index("suite")


def test_every_gate_is_implemented() -> None:
    module = _load_module()
    source = SCRIPT.read_text(encoding="utf-8")
    for gate in module.GATES:
        assert f'if name == "{gate}"' in source, gate


# --------------------------------------------------------------- behaviour

def test_python_gate_reports_the_interpreter_it_used() -> None:
    module = _load_module()
    result = module._python_version_gate()
    assert result.status == "pass"
    assert sys.executable in result.detail


def test_import_gate_passes_for_this_tree() -> None:
    module = _load_module()
    result = module._import_gate()
    assert result.exit_code == 0
    assert result.status == "pass", result.detail
    assert "pc_agent" in result.detail


def test_import_gate_fails_when_the_module_lives_elsewhere(monkeypatch, tmp_path: Path) -> None:
    """A stale install outside this tree is a failure with an exact fix."""

    module = _load_module()

    def _fake_run(argv, *, timeout):
        return 0, f"0.82.12\n{tmp_path / 'site-packages' / 'pc_agent'}\n"

    monkeypatch.setattr(module, "_run", _fake_run)
    result = module._import_gate()
    assert result.status == "fail"
    assert "OUTSIDE this install" in result.detail
    assert 'pip install -e' in result.detail


def test_a_missing_command_is_not_a_pass(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module,
        "subprocess",
        type(
            "FakeSubprocess",
            (),
            {
                "run": staticmethod(
                    lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no such tool"))
                ),
                "TimeoutExpired": subprocess.TimeoutExpired,
            },
        ),
    )
    code, output = module._run(["definitely-not-a-real-tool"], timeout=5)
    assert code == 127
    assert "could not start" in output


def test_a_timeout_is_reported_as_a_failure(monkeypatch) -> None:
    module = _load_module()

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="slow", timeout=1, output="partial output")

    monkeypatch.setattr(module.subprocess, "run", _timeout)
    code, output = module._run(["slow-thing"], timeout=1)
    assert code == 124
    assert "timed out" in output
    assert "partial output" in output


def test_report_is_ok_only_when_no_gate_failed() -> None:
    module = _load_module()
    good = [module.GateResult(name="python", status="pass", detail="ok")]
    bad = [module.GateResult(name="suite", status="fail", detail="2 failed", exit_code=1)]
    not_run = [module.GateResult(name="signing", status="not-run", detail="no Windows")]
    assert module.build_report(good)["ok"] is True
    report = module.build_report(good + bad + not_run)
    assert report["ok"] is False
    assert report["failures"] == ["suite"]
    names = [gate["name"] for gate in report["gates"]]
    assert names == ["python", "suite", "signing"]


def test_main_writes_the_report_and_exits_non_zero_on_failure(
    monkeypatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module,
        "run_gates",
        lambda wanted=None, skip_suite=False: [
            module.GateResult(name="suite", status="fail", detail="1 failed", exit_code=1,
                              tail=["1 failed, 1200 passed"])
        ],
    )
    code = module.main(["--report-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 1
    assert "WINDOWS VERIFICATION: FAIL" in captured.out
    assert "1 failed, 1200 passed" in captured.out, "the tail must be shown, not summarised away"
    report = json.loads((tmp_path / module.REPORT_NAME).read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["failures"] == ["suite"]


def test_main_exit_zero_when_every_gate_passes(
    monkeypatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module,
        "run_gates",
        lambda wanted=None, skip_suite=False: [
            module.GateResult(name="python", status="pass", detail="3.11")
        ],
    )
    assert module.main(["--report-dir", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True


def test_an_unknown_gate_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    module = _load_module()
    assert module.main(["--only", "nope", "--no-report"]) == 2
    assert "unknown gate" in capsys.readouterr().err


def test_skip_suite_removes_only_the_suite(monkeypatch) -> None:
    module = _load_module()
    seen: list[str] = []

    def _record(name: str, *args, **kwargs):
        seen.append(name)
        return module.GateResult(name=name, status="pass", detail="ok")

    monkeypatch.setattr(module, "_simple_gate", lambda name, *a, **k: _record(name))
    module.run_gates(["ruff", "suite"], skip_suite=True)
    assert seen == ["ruff"]


def test_the_suite_gate_counts_skips_and_never_hides_them(monkeypatch) -> None:
    module = _load_module()

    def _fake_run(argv, timeout):
        output = (
            "SKIPPED [1] tests/test_phase5_gui.py:271: no display\n"
            "1216 passed, 2 skipped in 200.00s\n"
        )
        return 0, output

    monkeypatch.setattr(module, "_run", _fake_run)
    result = module._suite_gate()
    assert result.status == "pass"
    assert "2 skipped" in result.detail
    assert any("SKIPPED" in line for line in result.tail)


def test_the_real_script_runs_end_to_end(tmp_path: Path) -> None:
    """A genuine run of the two cheapest gates, through main()."""

    module = _load_module()
    code = module.main(["--only", "python,import", "--report-dir", str(tmp_path)])
    assert code == 0
    report = json.loads((tmp_path / module.REPORT_NAME).read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert {gate["name"] for gate in report["gates"]} == {"python", "import"}
    assert report["optional"], "signing status must be reported as optional, not omitted"
