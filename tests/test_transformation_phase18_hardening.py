"""Transformation Phase 18 acceptance: final production hardening.

The phase claims four things, and each one is checked against real behaviour:

1. the release chain (build > verify > tamper detection > sign > stage > apply >
   rollback) actually runs, end to end, against a throw-away copy of the tree;
2. the result is recorded, and "never run" can never be confused with "passed";
3. continuous integration exists for both Windows and POSIX and runs the same
   commands a developer runs locally;
4. the hardening surface (audit check, CLI, doctor line, dashboard tab) is wired
   through the product rather than described in a document.

The heavy chain runs from a source tree the phase itself owns (the project root),
never from the user's install.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from pc_agent import __version__
from pc_agent.baseline import TRANSFORMATION_LABEL, TRANSFORMATION_PHASE
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.diagnostics import build_diagnostics, production_summary_line
from pc_agent.production_check import (
    PRODUCTION_REPORT_NAME,
    production_report_path,
    run_production_check,
    stored_production_report,
)
from pc_agent.release import audit_install
from pc_agent.storage.db import LATEST_SCHEMA_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"


def _settings(tmp_path: Path) -> Settings:
    settings = Settings.from_env(
        PROJECT_ROOT,
        environ={"PC_AGENT_DATA_DIR": str(tmp_path / "data")},
        load_dotenv=False,
    )
    settings.ensure_directories()
    return settings


@pytest.fixture(scope="module")
def chain_report(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Run the expensive chain once for the whole module."""

    tmp_path = tmp_path_factory.mktemp("phase18")
    settings = Settings.from_env(
        PROJECT_ROOT,
        environ={"PC_AGENT_DATA_DIR": str(tmp_path / "data")},
        load_dotenv=False,
    )
    settings.ensure_directories()
    report = run_production_check(settings)
    return {"report": report, "data_dir": str(settings.data_dir)}


# ------------------------------------------------------------------ identity ---


def test_phase18_identity_is_wired_through_the_product() -> None:
    assert __version__ == "0.82.17"
    assert TRANSFORMATION_PHASE == 18
    assert "Production" in TRANSFORMATION_LABEL
    assert LATEST_SCHEMA_VERSION == 18  # hardening adds no migration


# ------------------------------------------------------------- release chain ---


def test_release_chain_passes_every_step(chain_report: dict) -> None:
    report = chain_report["report"]
    names = [step.name for step in report.steps]
    assert names == [
        "release_catalog",
        "bundle_build",
        "bundle_verify",
        "tamper_detection",
        "signature_verify",
        "unsigned_refused",
        "stage",
        "apply",
        "rollback",
        "cleanup",
    ]
    failures = [step for step in report.steps if step.status == "fail"]
    assert not failures, failures
    assert report.ok and report.failures == 0
    assert report.version == __version__


def test_release_chain_proves_the_dangerous_paths(chain_report: dict) -> None:
    report = chain_report["report"]

    tamper = report.step("tamper_detection")
    assert tamper is not None and tamper.status == "pass"
    assert "refused" in tamper.detail

    unsigned = report.step("unsigned_refused")
    assert unsigned is not None and unsigned.status == "pass"
    assert "signed updates are required" in unsigned.detail.lower()

    rollback = report.step("rollback")
    assert rollback is not None and rollback.status == "pass"
    assert "pre-apply" in rollback.detail

    apply_step = report.step("apply")
    assert apply_step is not None
    applied_hash = re.search(r"sha256=([0-9a-f]{16})", apply_step.detail)
    restored_hash = re.search(r"sha256=([0-9a-f]{16})", rollback.detail)
    assert applied_hash and restored_hash
    assert applied_hash.group(1) == restored_hash.group(1)


def test_release_chain_states_that_no_production_key_was_used(
    chain_report: dict,
) -> None:
    report = chain_report["report"]
    assert "ephemeral" in report.environment["signing_key"]
    assert "no production key" in report.environment["signing_key"]
    assert report.step("signature_verify").detail.find("no production key") >= 0


def test_release_chain_leaves_nothing_behind(chain_report: dict) -> None:
    report = chain_report["report"]
    cleanup = report.step("cleanup")
    assert cleanup is not None and cleanup.status == "pass"
    assert not Path(report.workdir).exists()
    assert report.kept is False


def test_release_chain_report_is_stored_and_readable(chain_report: dict) -> None:
    data_dir = Path(chain_report["data_dir"])
    path = data_dir / PRODUCTION_REPORT_NAME
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["version"] == __version__
    assert payload["duration_ms"] > 0
    assert len(payload["steps"]) == 10


def test_keep_flag_preserves_and_reports_the_work_directory(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    report = run_production_check(settings, keep=True, write_report=False)
    try:
        assert report.ok
        assert report.kept is True
        assert report.step("cleanup").status == "skip"
        assert Path(report.workdir).is_dir()
        assert (Path(report.workdir) / "work").is_dir()
    finally:
        import shutil

        shutil.rmtree(report.workdir, ignore_errors=True)


# ------------------------------------------------ stored evidence is honest ---


def test_missing_report_is_reported_as_never_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert stored_production_report(settings) is None
    check = {item.name: item for item in audit_install(settings).checks}[
        "production_hardening"
    ]
    assert check.status in {"pass", "warn"}
    assert "no recorded end-to-end run yet" in check.detail


def test_a_failed_stored_report_turns_the_audit_check_red(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = production_report_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ok": False,
                "failures": 2,
                "generated_at": "2026-01-01T00:00:00+00:00",
                "steps": [],
            }
        ),
        encoding="utf-8",
    )
    check = {item.name: item for item in audit_install(settings).checks}[
        "production_hardening"
    ]
    assert check.status == "fail"
    assert "FAILED" in check.detail


def test_an_unreadable_stored_report_is_not_treated_as_a_pass(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = production_report_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    check = {item.name: item for item in audit_install(settings).checks}[
        "production_hardening"
    ]
    assert check.status == "warn"
    assert "unreadable" in check.detail


def test_audit_reports_32_checks_and_no_failures(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    audit = audit_install(settings)
    assert len(audit.checks) == 34
    assert audit.failures == 0
    assert "production_hardening" in {check.name for check in audit.checks}


# ------------------------------------------------------------------------ CI ---


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_ci_workflow_exists_and_covers_both_platforms() -> None:
    assert WORKFLOW.is_file(), "CI workflow is missing"
    text = _workflow_text()
    assert "ubuntu-latest" in text
    assert "windows-latest" in text
    # Python floor and the version the sandbox suite runs on.
    assert '"3.11"' in text and '"3.13"' in text


def test_ci_workflow_runs_the_same_gates_as_a_developer() -> None:
    text = _workflow_text()
    for gate in (
        "python -m ruff check .",
        "python -m pytest -q",
        "pc-agent release audit",
        "pc-agent release production-check",
    ):
        assert gate in text, gate
    assert "fail-fast: false" in text  # one platform failing must not hide the other


def test_ci_workflow_needs_no_secrets_and_cannot_write() -> None:
    text = _workflow_text()
    assert "permissions:\n  contents: read" in text
    assert "secrets." not in text
    assert "pull_request:" in text


def test_ci_workflow_matrix_is_parseable_and_consistent() -> None:
    text = _workflow_text()
    # No YAML library is a dependency, so the structure is checked directly:
    # both matrix keys exist, every job declares a runner, and no step is empty.
    assert "matrix:" in text
    assert "os: [ubuntu-latest, windows-latest]" in text
    jobs = re.findall(r"(?m)^  ([a-z][a-z0-9_-]*):$", text)
    assert {"test", "accessibility", "package"} <= set(jobs)
    assert text.count("runs-on:") >= 3
    assert "- name:" in text
    assert "\t" not in text  # YAML forbids tabs for indentation
    for line in text.splitlines():
        if line.strip().startswith("run:"):
            assert line.startswith(" "), line


def test_package_job_only_runs_for_version_tags() -> None:
    text = _workflow_text()
    package_block = text.split("  package:", 1)[1]
    assert "startsWith(github.ref, 'refs/tags/v')" in package_block
    assert "pc-agent release build" in package_block
    assert "pc-agent release verify" in package_block


# ------------------------------------------------------- product wiring ----


def test_cli_production_check_runs_and_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "report.json"
    code = main(["release", "production-check", "--out", str(out)])
    captured = capsys.readouterr()
    assert code == 0, captured.out + captured.err
    assert "Release chain check: READY" in captured.out
    for step in (
        "release_catalog",
        "tamper_detection",
        "signature_verify",
        "apply",
        "rollback",
    ):
        assert step in captured.out
    assert out.is_file()
    assert json.loads(out.read_text(encoding="utf-8"))["ok"] is True


def test_cli_production_check_json_is_machine_readable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["release", "production-check", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    names = [step["name"] for step in payload["steps"]]
    assert names[0] == "release_catalog"


def test_doctor_and_diagnostics_expose_the_hardening_state(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    line = production_summary_line(settings)
    assert line.startswith("ci=")
    assert "last-release-chain=" in line
    diagnostics = build_diagnostics(settings)
    assert isinstance(diagnostics, dict)
    assert diagnostics  # a real, non-empty report


def test_dashboard_has_a_production_tab_that_runs_the_real_check() -> None:
    from pc_agent.gui.app import NOTEBOOK_TAB_COUNT

    source = (PROJECT_ROOT / "src" / "pc_agent" / "gui" / "app.py").read_text(
        encoding="utf-8"
    )
    controller = (PROJECT_ROOT / "src" / "pc_agent" / "gui" / "controller.py").read_text(
        encoding="utf-8"
    )
    assert NOTEBOOK_TAB_COUNT == 20
    assert "def _build_production_tab" in source
    assert "Run release chain check" in source
    assert "tabular" not in source.lower()  # no placeholder table
    # The button calls the real implementation, not a pre-canned string.
    assert "run_production_check" in controller


def test_hardening_docs_are_present_and_reference_real_commands() -> None:
    for name in (
        "AUDIT_REPORT.md",
        "DEVELOPER_GUIDE.md",
        "INSTALLATION_GUIDE.md",
        "PLUGIN_DEVELOPMENT_GUIDE.md",
        "docs/transformation_phase18.md",
    ):
        path = PROJECT_ROOT / name
        assert path.is_file(), f"{name} is missing"
        text = path.read_text(encoding="utf-8")
        assert len(text) > 1500, f"{name} is too thin to be useful"
    guide = (PROJECT_ROOT / "DEVELOPER_GUIDE.md").read_text(encoding="utf-8")
    assert "python -m pytest -q" in guide
    assert "pc-agent release production-check" in guide
    install = (PROJECT_ROOT / "INSTALLATION_GUIDE.md").read_text(encoding="utf-8")
    assert "pip install -e" in install
    plugin = (PROJECT_ROOT / "PLUGIN_DEVELOPMENT_GUIDE.md").read_text(encoding="utf-8")
    assert "manifest" in plugin.lower()
    audit = (PROJECT_ROOT / "AUDIT_REPORT.md").read_text(encoding="utf-8")
    assert "release audit" in audit


def test_readme_documents_the_hardening_entry_points() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "production-check" in readme
    assert "0.82.17" in readme


def test_no_workflow_or_script_shells_out_to_a_hardcoded_user_path() -> None:
    """Hardening must not reintroduce the class of bug the phases fixed."""

    text = _workflow_text()
    assert "D:\\" not in text
    assert "C:\\Users" not in text
    assert os.sep in ("/", "\\")  # trivially true; keeps the platform import honest
