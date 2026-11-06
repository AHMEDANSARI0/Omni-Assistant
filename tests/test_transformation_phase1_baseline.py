"""Transformation baseline identity and audit checks (Phase 1 contract, Phase 2 identity)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pc_agent import __version__
from pc_agent.baseline import (
    GITHUB_AUDIT_COMMIT,
    REQUIRED_BASELINE_DOCUMENTS,
    TRANSFORMATION_LABEL,
    TRANSFORMATION_PHASE,
    baseline_identity,
    baseline_status,
)
from pc_agent.config import Settings
from pc_agent.diagnostics import build_diagnostics
from pc_agent.release import audit_install, release_candidate_files
from pc_agent.storage.db import Database

_ROOT = Path(__file__).resolve().parents[1]


def test_transformation_phase1_identity_is_single_sourced() -> None:
    identity = baseline_identity()

    assert __version__ == "0.81.2"
    assert TRANSFORMATION_PHASE == 18
    assert identity["label"] == TRANSFORMATION_LABEL
    assert identity["github_audit_commit"] == GITHUB_AUDIT_COMMIT
    assert identity["github_audit_version"] == "0.60.0"
    assert identity["pre_transformation_version"] == "0.62.0"
    assert identity["previous_transformation_version"] == "0.81.1"
    assert identity["windows_acceptance_status"] == "pending_user_windows_evidence"


def test_real_project_satisfies_baseline_document_and_dependency_contract() -> None:
    status = baseline_status(_ROOT)
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["version"] == __version__
    assert status.documents_missing == ()
    assert status.documents_present == REQUIRED_BASELINE_DOCUMENTS
    assert status.env_example_ignored is False
    assert status.direct_cryptography_dependency is True
    assert status.ok is True
    assert any(
        str(dependency).startswith("cryptography")
        for dependency in project["project"]["dependencies"]
    )


def test_baseline_status_rejects_ignored_example_and_missing_documents(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text(".env\n.env.example\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.63.0"\ndependencies = []\n',
        encoding="utf-8",
    )

    status = baseline_status(tmp_path)

    assert status.ok is False
    assert status.env_example_ignored is True
    assert status.direct_cryptography_dependency is False
    assert status.documents_missing == REQUIRED_BASELINE_DOCUMENTS


def test_release_audit_and_diagnostics_expose_baseline_without_secrets(
    tmp_path: Path,
) -> None:
    settings = Settings.from_env(
        tmp_path,
        environ={"PC_AGENT_PROVIDER_ORDER": "mock"},
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()

    audit = {check.name: check for check in audit_install(settings).checks}
    payload = build_diagnostics(settings, database)
    baseline_payload = payload["app"]["transformation_baseline"]

    assert audit["transformation_baseline"].status == "warn"
    assert baseline_payload["transformation_phase"] == 18
    assert baseline_payload["github_audit_commit"] == GITHUB_AUDIT_COMMIT
    assert baseline_payload["windows_acceptance_status"] == (
        "pending_user_windows_evidence"
    )
    assert "api_key" not in str(baseline_payload).lower()


def test_root_transformation_documents_are_release_candidates() -> None:
    candidates = {path.as_posix() for path in release_candidate_files(_ROOT)}

    for relative in REQUIRED_BASELINE_DOCUMENTS:
        assert relative in candidates
