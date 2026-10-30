"""Phase 18: production hardening, backups and portable releases."""

from __future__ import annotations

import json
import sqlite3
import warnings
import zipfile
from pathlib import Path

import pytest

from pc_agent import __version__
from pc_agent import maintenance as maintenance_module
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.diagnostics import build_diagnostics, export_diagnostics
from pc_agent.domain.task import Task
from pc_agent.errors import PcAgentError
from pc_agent.gui.controller import GuiController, format_release_audit
from pc_agent.maintenance import DatabaseMaintenanceService
from pc_agent.release import (
    audit_install,
    build_release_bundle,
    release_candidate_files,
    verify_release_bundle,
)
from pc_agent.storage.db import LATEST_SCHEMA_VERSION, Database
from pc_agent.storage.repositories import (
    MemoryRepository,
    PreferenceRepository,
    TaskRepository,
)
from pc_agent.tools import build_default_registry

_SECRET = "phase18-secret-marker-do-not-export"


def _settings(tmp_path: Path, **environ: str) -> Settings:
    values = {"PC_AGENT_PROVIDER_ORDER": "mock", **environ}
    settings = Settings.from_env(tmp_path, environ=values)
    settings.ensure_directories()
    return settings


def _write_project(root: Path) -> None:
    (root / "src" / "pc_agent").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(exist_ok=True)
    (root / "scripts").mkdir(exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "examples").mkdir(exist_ok=True)
    (root / "workspace").mkdir(exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "self-healing-pc-agent"\n'
        f'version = "{__version__}"\n',
        encoding="utf-8",
    )
    (root / "src" / "pc_agent" / "__init__.py").write_text(
        f'__version__ = "{__version__}"\n', encoding="utf-8"
    )
    (root / "src" / "pc_agent" / "module.py").write_text(
        "VALUE = 18\n", encoding="utf-8"
    )
    (root / "README.md").write_text("Portable test project.\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("Changes.\n", encoding="utf-8")
    (root / ".env.example").write_text("EXAMPLE=1\n", encoding="utf-8")
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")
    (root / "docs" / "phase18.md").write_text("Phase 18.\n", encoding="utf-8")
    (root / "scripts" / "install_windows.ps1").write_text(
        "Write-Host ok\n", encoding="utf-8"
    )
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (root / "workspace" / ".gitkeep").write_text("", encoding="utf-8")
    (root / ".env").write_text(f"GEMINI_API_KEY={_SECRET}\n", encoding="utf-8")
    (root / "workspace" / "private.txt").write_text(_SECRET, encoding="utf-8")
    (root / "data" / "private.db").write_bytes(b"not bundled")
    (root / "PATCHER.mjs").write_text("not bundled\n", encoding="utf-8")
    cache = root / "src" / "pc_agent" / "__pycache__"
    cache.mkdir()
    (cache / "module.pyc").write_bytes(b"cache")


def _rewrite_zip(source: Path, destination: Path, replacements: dict[str, bytes]) -> None:
    with zipfile.ZipFile(source) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    entries.update(replacements)
    with zipfile.ZipFile(destination, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


def test_release_candidates_are_allow_listed_and_private_free(tmp_path: Path) -> None:
    _write_project(tmp_path)

    candidates = {
        path.as_posix() for path in release_candidate_files(tmp_path)
    }

    assert "pyproject.toml" in candidates
    assert "scripts/install_windows.ps1" in candidates
    assert "workspace/.gitkeep" in candidates
    assert ".env" not in candidates
    assert "workspace/private.txt" not in candidates
    assert "data/private.db" not in candidates
    assert "PATCHER.mjs" not in candidates
    assert not any(path.endswith(".pyc") for path in candidates)


def test_release_bundle_is_deterministic_and_verifiable(tmp_path: Path) -> None:
    _write_project(tmp_path)
    first = tmp_path / "outside-first.zip"
    second = tmp_path / "outside-second.zip"

    first_info = build_release_bundle(tmp_path, first)
    second_info = build_release_bundle(tmp_path, second)

    assert first_info.bundle_sha256 == second_info.bundle_sha256
    assert first.read_bytes() == second.read_bytes()
    verified = verify_release_bundle(first)
    assert verified.version == __version__
    assert verified.file_count == len(verified.files)
    assert {record.path for record in verified.files} == {
        path.as_posix() for path in release_candidate_files(tmp_path)
    }
    with zipfile.ZipFile(first) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("release-manifest.json"))
    assert manifest["version"] == __version__
    assert ".env" not in names
    assert "workspace/private.txt" not in names


def test_release_bundle_refuses_overwrite_and_wrong_suffix(tmp_path: Path) -> None:
    _write_project(tmp_path)
    output = tmp_path / "bundle.zip"
    build_release_bundle(tmp_path, output)

    with pytest.raises(PcAgentError, match="already exists"):
        build_release_bundle(tmp_path, output)
    with pytest.raises(PcAgentError, match="\\.zip"):
        build_release_bundle(tmp_path, tmp_path / "bundle.bin")


def test_release_verification_rejects_tamper_traversal_forbidden_and_duplicates(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    bundle = tmp_path / "bundle.zip"
    build_release_bundle(tmp_path, bundle)

    tampered = tmp_path / "tampered.zip"
    _rewrite_zip(bundle, tampered, {"README.md": b"tampered\n"})
    with pytest.raises(PcAgentError, match="hash mismatch|size mismatch"):
        verify_release_bundle(tampered)

    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("release-manifest.json", "{}")
        archive.writestr("../evil.txt", "evil")
    with pytest.raises(PcAgentError, match="unsafe entry"):
        verify_release_bundle(traversal)

    forbidden = tmp_path / "forbidden.zip"
    with zipfile.ZipFile(forbidden, "w") as archive:
        archive.writestr("release-manifest.json", "{}")
        archive.writestr(".env", _SECRET)
    with pytest.raises(PcAgentError, match="forbidden"):
        verify_release_bundle(forbidden)

    duplicate = tmp_path / "duplicate.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("README.md", "one")
            archive.writestr("README.md", "two")
    with pytest.raises(PcAgentError, match="duplicate"):
        verify_release_bundle(duplicate)


def test_release_audit_ready_and_failure_states(tmp_path: Path) -> None:
    _write_project(tmp_path)
    settings = _settings(tmp_path)
    Database(settings.database_path).initialize()

    audit = audit_install(settings)
    by_name = {check.name: check for check in audit.checks}
    assert audit.ok
    assert by_name["database"].status == "pass"
    assert by_name["versions"].status == "pass"
    assert by_name["providers"].status == "warn"

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.0.1"\n', encoding="utf-8"
    )
    risky = _settings(
        tmp_path,
        PC_AGENT_TASK_TOKEN_BUDGET="0",
        PC_AGENT_ALLOW_SUBPROCESS="1",
    )
    risky_audit = audit_install(risky)
    risky_by_name = {check.name: check for check in risky_audit.checks}
    assert risky_by_name["versions"].status == "fail"
    assert risky_by_name["task_budgets"].status == "warn"
    assert risky_by_name["optional_gates"].status == "warn"

    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (99, 'test')"
        )
    assert {
        check.name: check for check in audit_install(settings).checks
    }["database"].status == "fail"


def test_database_backup_verify_and_restore_with_prebackup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    tasks = TaskRepository(database)
    first = tasks.create(Task("first private task"))
    service = DatabaseMaintenanceService(settings.database_path, settings.data_dir)

    backup = service.create_backup("first")
    assert backup.schema_version == LATEST_SCHEMA_VERSION
    assert service.verify_backup(backup.backup_id).backup_id == backup.backup_id
    assert service.list_backups()[0].backup_id == backup.backup_id

    second = tasks.create(Task("second private task"))
    result = service.restore_backup(backup.backup_id)

    restored = Database(settings.database_path)
    restored.initialize()
    restored_tasks = TaskRepository(restored)
    assert restored_tasks.get(first.id) is not None
    assert restored_tasks.get(second.id) is None
    assert result.pre_restore_backup_id
    assert len(service.list_backups()) >= 2
    assert service.verify_backup(result.pre_restore_backup_id).label == "pre-restore"


def test_database_restore_rolls_back_if_post_replace_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    tasks = TaskRepository(database)
    first = tasks.create(Task("rollback first task"))
    service = DatabaseMaintenanceService(settings.database_path, settings.data_dir)
    backup = service.create_backup("rollback-source")
    second = tasks.create(Task("rollback second task"))
    original_state = maintenance_module._sqlite_state

    def flaky_state(path: Path):
        if (
            path == settings.database_path
            and path.is_file()
            and maintenance_module._sha256(path) == backup.sha256
        ):
            return backup.schema_version, "simulated-corruption"
        return original_state(path)

    monkeypatch.setattr(maintenance_module, "_sqlite_state", flaky_state)
    with pytest.raises(PcAgentError, match="restored live database"):
        service.restore_backup(backup.backup_id)
    monkeypatch.undo()

    restored = Database(settings.database_path)
    restored.initialize()
    restored_tasks = TaskRepository(restored)
    assert restored_tasks.get(first.id) is not None
    assert restored_tasks.get(second.id) is not None


def test_database_backup_rejects_bad_label_reference_and_tamper(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    Database(settings.database_path).initialize()
    service = DatabaseMaintenanceService(settings.database_path, settings.data_dir)

    with pytest.raises(PcAgentError, match="Backup label"):
        service.create_backup("../bad")
    backup = service.create_backup("safe")
    with pytest.raises(PcAgentError, match="backup ID"):
        service.resolve_backup("../" + backup.backup_id)

    with backup.path.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(PcAgentError, match="size|hash"):
        service.verify_backup(backup.backup_id)


def test_diagnostics_are_counts_only_and_redacted(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        PC_AGENT_PROVIDER_ORDER="gemini,mock",
        GEMINI_API_KEY=_SECRET,
    )
    database = Database(settings.database_path)
    database.initialize()
    TaskRepository(database).create(Task(f"private instruction {_SECRET}"))
    PreferenceRepository(database).set("favorite", _SECRET)
    MemoryRepository(database).upsert(
        kind="note",
        key="private-note",
        title="Private",
        content={"text": _SECRET},
    )

    payload = build_diagnostics(settings, database)
    rendered = json.dumps(payload, sort_keys=True)

    assert _SECRET not in rendered
    assert "private instruction" not in rendered
    assert payload["app"]["version"] == __version__
    assert payload["counts"]["tasks_by_status"]["queued"] == 1
    assert payload["counts"]["preferences"] == 1
    assert payload["counts"]["memory_by_kind"]["note"] == 1
    assert payload["tools"]["builtin_count"] == 62
    gemini = next(row for row in payload["providers"] if row["name"] == "gemini")
    assert gemini["credential_source"] == "environment"

    exported = export_diagnostics(
        settings, tmp_path / "workspace" / "diagnostics.json", database
    )
    assert exported.path.is_file()
    assert _SECRET not in exported.path.read_text(encoding="utf-8")
    with pytest.raises(PcAgentError, match="already exists"):
        export_diagnostics(settings, exported.path, database)


def test_cli_release_maintenance_diagnostics_and_doctor_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_project(tmp_path)
    root = ["--root", str(tmp_path)]

    assert main([*root, "release", "audit", "--json"]) == 0
    audit_payload = json.loads(capsys.readouterr().out)
    assert audit_payload["ok"] is True
    assert _SECRET not in json.dumps(audit_payload)

    assert main(
        [*root, "release", "build", "--output", "workspace/release.zip", "--json"]
    ) == 0
    bundle_payload = json.loads(capsys.readouterr().out)
    assert bundle_payload["version"] == __version__
    assert main([*root, "release", "verify", "workspace/release.zip"]) == 0
    assert "Release bundle verified" in capsys.readouterr().out
    assert main(
        [*root, "release", "build", "--output", "workspace/release.zip"]
    ) == 1
    capsys.readouterr()

    assert main([*root, "maintenance", "backup", "--label", "cli", "--json"]) == 0
    backup_payload = json.loads(capsys.readouterr().out)
    assert backup_payload["schema_version"] == LATEST_SCHEMA_VERSION
    assert main([*root, "maintenance", "list", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1
    assert main([*root, "maintenance", "verify", "latest"]) == 0
    capsys.readouterr()
    assert main([*root, "maintenance", "restore", "latest", "--yes", "--json"]) == 0
    restore_payload = json.loads(capsys.readouterr().out)
    assert restore_payload["pre_restore_backup_id"]
    assert restore_payload["live_schema_version"] == LATEST_SCHEMA_VERSION

    assert main(
        [
            *root,
            "diagnostics",
            "export",
            "--output",
            "workspace/diagnostics.json",
            "--json",
        ]
    ) == 0
    diagnostics_info = json.loads(capsys.readouterr().out)
    diagnostics_text = Path(diagnostics_info["path"]).read_text(encoding="utf-8")
    assert _SECRET not in diagnostics_text

    assert main([*root, "doctor", "--json"]) == 0
    doctor_payload = json.loads(capsys.readouterr().out)
    assert doctor_payload["app"]["version"] == __version__
    assert _SECRET not in json.dumps(doctor_payload)


def test_cli_restore_cancellation_leaves_database_and_backup_count_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(tmp_path)
    Database(settings.database_path).initialize()
    service = DatabaseMaintenanceService(settings.database_path, settings.data_dir)
    backup = service.create_backup("keep")
    monkeypatch.setattr(
        "pc_agent.cli._confirm_direct_write", lambda *args, **kwargs: False
    )

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "maintenance",
                "restore",
                backup.backup_id,
            ]
        )
        == 3
    )
    assert "unchanged" in capsys.readouterr().out
    assert [item.backup_id for item in service.list_backups()] == [backup.backup_id]


def test_gui_controller_release_audit_and_doctor_text(tmp_path: Path) -> None:
    _write_project(tmp_path)
    settings = _settings(tmp_path)
    controller = GuiController(settings)
    DatabaseMaintenanceService(
        settings.database_path, settings.data_dir
    ).create_backup("gui")

    audit = controller.release_audit()
    rendered_audit = format_release_audit(audit)
    summary = controller.doctor_summary()

    assert audit.ok
    assert "Release audit:" in rendered_audit
    assert "CLI-only" in rendered_audit
    assert "count=1" in summary.database_backup_summary
    text = summary.to_text()
    assert "PHASE 39 Secure .env Key Cleanup" in text
    assert "Production: release audit" in text
    assert _SECRET not in text


def test_windows_scripts_are_conservative_and_secret_free() -> None:
    project_root = Path(__file__).resolve().parents[1]
    install = (project_root / "scripts" / "install_windows.ps1").read_text(
        encoding="utf-8"
    )
    uninstall = (project_root / "scripts" / "uninstall_windows.ps1").read_text(
        encoding="utf-8"
    )
    build = (project_root / "scripts" / "build_release.ps1").read_text(
        encoding="utf-8"
    )
    launch_gui = (project_root / "scripts" / "launch_gui.cmd").read_text(
        encoding="utf-8"
    )
    launch_cli = (project_root / "scripts" / "launch_cli.cmd").read_text(
        encoding="utf-8"
    )

    assert "py -3 -m venv" in install
    assert 'pip install -e ".[dev]"' in install
    assert "pc-agent.exe\" doctor" in install or "& $PcAgent doctor" in install
    assert "& $PcAgentGui --check" in install
    assert "Invoke-Expression" not in install + uninstall + build
    assert "DownloadString" not in install + uninstall + build
    assert "Remove-Item" not in install
    assert "Assert-ExactChild" in uninstall
    assert "RemoveUserData" in uninstall and "-not $Yes" in uninstall
    assert "Self-Healing PC Agent.lnk" in uninstall
    assert "sys.version_info >= (3, 11)" in install
    assert "& $PcAgent --version" in build
    assert "& $PcAgent version" not in build
    assert "pytest -q" in build and "ruff check" in build
    assert "release build" in build and "release verify" in build
    assert "pc-agent-gui.exe" in launch_gui
    assert "activate.bat" in launch_cli
    assert _SECRET not in install + uninstall + build + launch_gui + launch_cli


def test_current_project_candidates_include_phase18_but_no_private_areas() -> None:
    project_root = Path(__file__).resolve().parents[1]
    candidates = {
        path.as_posix() for path in release_candidate_files(project_root)
    }

    assert __version__ == "0.41.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(build_default_registry().list_tools()) == 62
    assert "src/pc_agent/release.py" in candidates
    assert "src/pc_agent/maintenance.py" in candidates
    assert "src/pc_agent/diagnostics.py" in candidates
    assert "scripts/install_windows.ps1" in candidates
    assert "scripts/uninstall_windows.ps1" in candidates
    assert "scripts/build_release.ps1" in candidates
    assert ".env" not in candidates
    assert not any(path.startswith("data/") for path in candidates)
    assert not any(path.endswith(".mjs") for path in candidates)
