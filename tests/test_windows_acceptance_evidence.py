"""Windows acceptance evidence gate (0.81.2 hotfix on top of Phase 18).

The programme's only remaining item was machine evidence. These tests pin the
honest behaviour of the harness: it must never claim a Windows pass from a
non-Windows host, it must refuse edited reports, and only a verified passed
Windows report may change the baseline status.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from pc_agent import __version__
from pc_agent.baseline import (
    WINDOWS_ACCEPTANCE_EVIDENCE,
    WINDOWS_ACCEPTANCE_STATUS,
    acceptance_body_sha256,
    baseline_status,
    windows_acceptance_evidence,
)
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.errors import PcAgentError
from pc_agent.windows_acceptance import (
    REQUIRED_GATES,
    record_acceptance_report,
    run_windows_acceptance,
    verify_acceptance_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
IS_WINDOWS = sys.platform.startswith("win")


def _settings(tmp_path: Path) -> Settings:
    root = tmp_path / "install"
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    return Settings.from_env(
        root,
        environ={
            "PC_AGENT_DATA_DIR": str(data),
            "PC_AGENT_WORKSPACE_DIR": str(tmp_path / "workspace"),
            "PC_AGENT_DB_PATH": str(data / "pc_agent.db"),
        },
        load_dotenv=False,
    )


def _passed_windows_report(settings: Settings) -> dict:
    """A syntactically valid, correctly sealed *passed Windows* report.

    This is what the user's machine writes; here it stands in for that run. It
    proves the reader accepts well-formed evidence - it is not a claim that a
    Windows run happened in this test.
    """

    body = {
        "status": "passed",
        "platform": "windows",
        "generated_at": "2026-11-06T12:00:00+00:00",
        "version": __version__,
        "machine": {"system": "Windows", "release": "11", "python": "3.11.9"},
        "gates": [
            {"name": name, "status": "pass", "detail": "recorded", "duration_ms": 1}
            for name in REQUIRED_GATES
        ],
    }
    body["body_sha256"] = acceptance_body_sha256(body)
    return body


def _write(tmp_path: Path, payload: dict, name: str = "report.json") -> Path:
    target = tmp_path / name
    target.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    return target


# --------------------------------------------------------------- the reader ---


def test_no_evidence_is_reported_as_pending(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state = windows_acceptance_evidence(Path(settings.root_dir))
    assert state["status"] == WINDOWS_ACCEPTANCE_STATUS == "pending_user_windows_evidence"
    assert state["recorded"] is False
    assert baseline_status(Path(settings.root_dir)).windows_acceptance_status == (
        WINDOWS_ACCEPTANCE_STATUS
    )


def test_a_verified_passed_windows_report_changes_the_baseline(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    target = Path(settings.root_dir) / "data" / WINDOWS_ACCEPTANCE_EVIDENCE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(_passed_windows_report(settings), sort_keys=True), encoding="utf-8"
    )
    state = windows_acceptance_evidence(Path(settings.root_dir))
    assert state["recorded"] is True
    assert state["status"] == "verified:2026-11-06"
    assert state["gates"] == len(REQUIRED_GATES)
    assert baseline_status(Path(settings.root_dir)).windows_acceptance_status == (
        "verified:2026-11-06"
    )


def test_an_edited_report_is_refused_by_the_reader(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    payload = _passed_windows_report(settings)
    payload["gates"][0]["detail"] = "edited after sealing"
    target = Path(settings.root_dir) / "data" / WINDOWS_ACCEPTANCE_EVIDENCE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    state = windows_acceptance_evidence(Path(settings.root_dir))
    assert state["recorded"] is False
    assert "hash" in state["detail"]


def test_a_report_that_is_not_a_passed_windows_run_is_refused(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    target = Path(settings.root_dir) / "data" / WINDOWS_ACCEPTANCE_EVIDENCE
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _passed_windows_report(settings)
    payload["platform"] = "posix"
    payload["body_sha256"] = acceptance_body_sha256(payload)
    target.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    assert windows_acceptance_evidence(Path(settings.root_dir))["recorded"] is False

    payload = _passed_windows_report(settings)
    payload["status"] = "self-test"
    payload["body_sha256"] = acceptance_body_sha256(payload)
    target.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    assert windows_acceptance_evidence(Path(settings.root_dir))["recorded"] is False


def test_verify_accepts_a_well_formed_report(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    verification = verify_acceptance_report(_write(tmp_path, _passed_windows_report(settings)))
    assert verification.ok, verification.problems
    assert verification.status == "passed"


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (lambda body: body.update(status="shipped"), "unknown report status"),
        (lambda body: body.update(platform="posix"), "must come from a Windows host"),
        (lambda body: body["gates"].pop(), "missing gates"),
        (lambda body: body["gates"][0].update(status="fail"), "unpassed gates"),
        (lambda body: body.update(version="0.1.0"), "this build is"),
        (lambda body: body.update(detail="sk-abcdef0123456789abcdef"), "key material"),
    ],
)
def test_verify_rejects_broken_or_dishonest_reports(
    tmp_path: Path, mutation, expected: str
) -> None:
    settings = _settings(tmp_path)
    body = _passed_windows_report(settings)
    mutation(body)
    body["body_sha256"] = acceptance_body_sha256(body)
    verification = verify_acceptance_report(_write(tmp_path, body))
    assert verification.ok is False
    assert any(expected in problem for problem in verification.problems), verification.problems


def test_verify_reports_a_missing_file_instead_of_crashing(tmp_path: Path) -> None:
    verification = verify_acceptance_report(tmp_path / "nope.json")
    assert verification.ok is False
    assert verification.status == "missing"


# --------------------------------------------------------------- the runner ---


def test_the_default_run_refuses_to_claim_a_pass_off_windows(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    out = tmp_path / "attempt.json"
    report = run_windows_acceptance(settings, run_suite=False, out_path=out)
    if IS_WINDOWS:
        pytest.skip("this assertion is about non-Windows hosts")
    assert report.status == "skipped"
    assert report.ok is False
    assert report.platform != "windows"
    assert report.failures == 1  # the platform gate
    other = [gate for gate in report.gates if gate.name != "platform"]
    assert all(gate.status == "not-run" for gate in other)
    verification = verify_acceptance_report(out)
    assert verification.ok is True
    assert verification.status == "skipped"


def test_a_self_test_exercises_real_gates_without_claiming_windows(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    out = tmp_path / "self-test.json"
    report = run_windows_acceptance(
        settings, run_suite=False, allow_non_windows=True, out_path=out
    )
    assert report.status in {"self-test", "failed"}
    assert [gate.name for gate in report.gates] == list(REQUIRED_GATES)
    # The gates really ran: they carry evidence text, not placeholders.
    sandbox = report.gate("plugin_sandbox")
    assert sandbox is not None and "worker" in sandbox.detail
    assert report.gate("release_audit").status in {"pass", "fail"}
    assert report.gate("release_chain").status in {"pass", "fail"}
    assert verify_acceptance_report(out).ok is True


def test_a_self_test_report_can_never_be_recorded(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    out = tmp_path / "self-test.json"
    run_windows_acceptance(settings, run_suite=False, allow_non_windows=True, out_path=out)
    with pytest.raises(PcAgentError):
        record_acceptance_report(settings, out)
    assert windows_acceptance_evidence(Path(settings.root_dir))["recorded"] is False


def test_recording_a_verified_passed_windows_report(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    target = _write(tmp_path, _passed_windows_report(settings))
    result = record_acceptance_report(settings, target)
    assert result["recorded"] is True
    assert result["status"] == "verified:2026-11-06"
    stored = Path(settings.root_dir) / "data" / WINDOWS_ACCEPTANCE_EVIDENCE
    assert stored.is_file()
    assert windows_acceptance_evidence(Path(settings.root_dir))["recorded"] is True
    with pytest.raises(PcAgentError):
        record_acceptance_report(settings, tmp_path / "missing.json")


def test_recorded_evidence_carries_no_secrets(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    out = tmp_path / "report.json"
    run_windows_acceptance(
        settings, run_suite=False, allow_non_windows=True, out_path=out
    )
    text = out.read_text(encoding="utf-8")
    for marker in ("sk-", "BEGIN", "api_key", "token", "password", "secret"):
        assert marker.lower() not in text.lower()
    payload = json.loads(text)
    assert payload["body_sha256"] == acceptance_body_sha256(payload)


# ------------------------------------------------------------------ the CLI ---


def _cli_env(monkeypatch, settings: Settings) -> list[str]:
    """Point the real CLI at this test's install, the way a user's shell does."""

    monkeypatch.setenv("PC_AGENT_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("PC_AGENT_WORKSPACE_DIR", str(settings.workspace_dir))
    monkeypatch.setenv("PC_AGENT_DB_PATH", str(settings.database_path))
    monkeypatch.delenv("PC_AGENT_ROOT", raising=False)
    return ["--root", str(settings.root_dir)]


def test_cli_status_reports_pending_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    settings = _settings(tmp_path)
    base = _cli_env(monkeypatch, settings)
    # `status` reports and runs nothing on any platform. (Bare `release evidence`
    # runs the gates on Windows by design, so the status verb is what this test
    # pins; the run path is covered by the self-test case below.)
    code = main([*base, "release", "evidence", "status"])
    out = capsys.readouterr().out
    assert code == 0
    assert "pending_user_windows_evidence" in out
    assert "release evidence record" in out


def test_cli_self_test_run_and_verify_and_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    settings = _settings(tmp_path)
    base = _cli_env(monkeypatch, settings)
    out = tmp_path / "self-test.json"
    code = main(
        [*base, "release", "evidence", "--self-test", "--no-suite", "--out", str(out)]
    )
    printed = capsys.readouterr().out
    assert "Windows acceptance:" in printed
    assert "[skip]" in printed or "[not-applicable]" in printed or "[FAIL]" in printed
    assert out.is_file()
    report = json.loads(out.read_text(encoding="utf-8"))
    # The exit status must agree with the report it just wrote: this synthetic
    # install root has no source tree, so the audit and chain gates really fail
    # there and the command must say so instead of returning success.
    assert code == (0 if report["status"] in {"passed", "self-test"} else 1)
    if report["status"] == "failed":
        assert any(gate["status"] == "fail" for gate in report["gates"])

    code = main([*base, "release", "evidence", "verify", str(out)])
    assert code == 0
    assert "VERIFIED" in capsys.readouterr().out

    code = main([*base, "release", "evidence", "record", str(out)])
    captured = capsys.readouterr()
    assert code == 1
    refused = (captured.out + captured.err).lower()
    assert "only a passed windows run" in refused or "rejected" in refused
    assert not (Path(settings.root_dir) / "data" / WINDOWS_ACCEPTANCE_EVIDENCE).exists()

    report = _write(tmp_path, _passed_windows_report(settings), "windows.json")
    code = main([*base, "release", "evidence", "record", str(report)])
    recorded_out = capsys.readouterr().out
    assert code == 0
    assert "verified:2026-11-06" in recorded_out

    code = main([*base, "release", "evidence", "status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["recorded"]["recorded"] is True

    # --json must be exactly one JSON document on every path, including the one
    # that runs the gates (the 0.80.1 Windows run printed a report followed by a
    # status line, which no parser can read).
    out_path = tmp_path / "second-self-test.json"
    code = main(
        [
            *base,
            "release",
            "evidence",
            "--self-test",
            "--no-suite",
            "--out",
            str(out_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"report", "recorded"}
    assert payload["report"]["gates"]
    assert code == (0 if payload["report"]["status"] in {"passed", "self-test"} else 1)
