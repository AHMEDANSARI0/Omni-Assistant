"""Phase 21: signed offline updates from portable release bundles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pc_agent import __version__, release_admin
from pc_agent import release as release_module
from pc_agent import updates as updates_module
from pc_agent.cli import main
from pc_agent.config import ConfigError, Settings
from pc_agent.errors import PcAgentError
from pc_agent.gui.controller import (
    GuiController,
    format_staged_update,
    format_update_check,
    format_update_list,
)
from pc_agent.release import (
    audit_install,
    build_release_bundle,
    release_candidate_files,
)
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry
from pc_agent.updates import (
    apply_staged_update,
    check_update,
    compare_versions,
    default_signature_path,
    get_update,
    list_updates,
    parse_version,
    rollback_update,
    stage_update,
)

_FUTURE = "0.30.0"
_PAST = "0.22.0"
_SECRET = "[SECURITY_DATA]"


def _settings(root: Path, **environ: str) -> Settings:
    values = {
        "PC_AGENT_PROVIDER_ORDER": "mock",
        "PC_AGENT_CREDENTIAL_STORE": "env",
        **environ,
    }
    settings = Settings.from_env(root, environ=values, load_dotenv=False)
    settings.ensure_directories()
    return settings


def _write_project(
    root: Path,
    version: str,
    *,
    value: int,
    include_new: bool = False,
    include_old: bool = False,
) -> None:
    (root / "src" / "pc_agent").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(exist_ok=True)
    (root / "scripts").mkdir(exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "examples").mkdir(exist_ok=True)
    (root / "workspace").mkdir(exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "self-healing-pc-agent"\n'
        f'version = "{version}"\n',
        encoding="utf-8",
    )
    (root / "src" / "pc_agent" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    (root / "src" / "pc_agent" / "module.py").write_text(
        f"VALUE = {value}\n", encoding="utf-8"
    )
    if include_new:
        (root / "src" / "pc_agent" / "new_module.py").write_text(
            "NEW_VALUE = 21\n", encoding="utf-8"
        )
    if include_old:
        (root / "src" / "pc_agent" / "old_only.py").write_text(
            "OLD_VALUE = 9\n", encoding="utf-8"
        )
    (root / "README.md").write_text(f"Portable test project {version}.\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("Changes.\n", encoding="utf-8")
    (root / ".env.example").write_text("EXAMPLE=1\n", encoding="utf-8")
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")
    (root / "docs" / "phase21.md").write_text("Phase 21.\n", encoding="utf-8")
    (root / "scripts" / "install_windows.ps1").write_text(
        "Write-Host ok\n", encoding="utf-8"
    )
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (root / "workspace" / ".gitkeep").write_text("", encoding="utf-8")


def _build_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    version: str,
    value: int,
    include_new: bool = False,
) -> Path:
    source = tmp_path / f"source-{name}"
    _write_project(source, version, value=value, include_new=include_new)
    output = tmp_path / f"bundle-{name}.zip"
    monkeypatch.setattr(release_module, "__version__", version)
    info = build_release_bundle(source, output)
    assert info.version == version
    return output


def _make_keys(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[Path, str]:
    keys = tmp_path / "vendor-keys"
    assert (
        release_admin.main(
            [
                "keygen",
                "--out-dir",
                str(keys),
                "--key-id",
                "release-test",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    public_line = next(
        line
        for line in (keys / "release-public-key.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.startswith("PC_AGENT_RELEASE_PUBLIC_KEY=")
    )
    public_key = public_line.split("=", 1)[1]
    assert public_key in output
    assert _SECRET not in output
    return keys, public_key


def _sign_bundle(
    bundle: Path,
    keys: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    key_id: str = "release-test",
) -> Path:
    assert (
        release_admin.main(
            [
                "sign",
                str(bundle),
                "--private-key",
                str(keys / "release-private-key.txt"),
                "--key-id",
                key_id,
            ]
        )
        == 0
    )
    capsys.readouterr()
    signature = default_signature_path(bundle)
    assert signature.is_file()
    return signature


def _signed_settings(root: Path, public_key: str, **overrides: str) -> Settings:
    values = {
        "PC_AGENT_RELEASE_PUBLIC_KEY": public_key,
        "PC_AGENT_RELEASE_KEY_ID": "release-test",
        "PC_AGENT_REQUIRE_SIGNED_UPDATES": "1",
        **overrides,
    }
    return _settings(root, **values)


def test_version_parsing_and_comparison_are_strict() -> None:
    assert parse_version("0.23.0") == (0, 23, 0)
    assert compare_versions("0.24.0", "0.23.9") == 1
    assert compare_versions("0.23.0", "0.23.0") == 0
    assert compare_versions("0.22.9", "0.23.0") == -1
    for invalid in ("", "1.2", "01.2.3", "1.2.3-beta", "v1.2.3"):
        with pytest.raises(PcAgentError, match="MAJOR.MINOR.PATCH"):
            parse_version(invalid)


def test_release_trust_configuration_is_validated_and_secret_free(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    summary = settings.public_summary()
    assert settings.release_public_key is None
    assert settings.release_key_id is None
    assert settings.require_signed_updates is False
    assert summary["release_public_key_configured"] == "no"
    assert summary["release_key_id"] == "any"

    with pytest.raises(ConfigError, match="PC_AGENT_RELEASE_PUBLIC_KEY"):
        _settings(tmp_path, PC_AGENT_RELEASE_PUBLIC_KEY="not-a-real-key")
    with pytest.raises(ConfigError, match="PC_AGENT_RELEASE_KEY_ID"):
        _settings(tmp_path, PC_AGENT_RELEASE_KEY_ID="bad key")
    with pytest.raises(ConfigError, match="PC_AGENT_REQUIRE_SIGNED_UPDATES"):
        _settings(tmp_path, PC_AGENT_REQUIRE_SIGNED_UPDATES="sometimes")


def test_release_candidates_exclude_private_key_material(tmp_path: Path) -> None:
    _write_project(tmp_path, __version__, value=23)
    (tmp_path / "docs" / "release-private-key.txt").write_text(
        _SECRET, encoding="utf-8"
    )
    (tmp_path / "scripts" / "signing.p12").write_bytes(b"private")
    (tmp_path / "docs" / "certificate.pem").write_text(_SECRET, encoding="utf-8")

    candidates = {path.as_posix() for path in release_candidate_files(tmp_path)}
    assert "docs/release-private-key.txt" not in candidates
    assert "scripts/signing.p12" not in candidates
    assert "docs/certificate.pem" not in candidates


def test_release_audit_reports_signed_update_trust_states(tmp_path: Path) -> None:
    _write_project(tmp_path, __version__, value=23)

    local = audit_install(_settings(tmp_path))
    local_by_name = {check.name: check for check in local.checks}
    assert local_by_name["signed_updates"].status == "info"

    required_without_key = audit_install(
        _settings(tmp_path, PC_AGENT_REQUIRE_SIGNED_UPDATES="1")
    )
    assert {
        check.name: check for check in required_without_key.checks
    }["signed_updates"].status == "fail"

    keys_public = "A" * 43  # base64url form of 32 zero bytes; format-only here.
    configured = audit_install(
        _settings(
            tmp_path,
            PC_AGENT_RELEASE_PUBLIC_KEY=keys_public,
            PC_AGENT_RELEASE_KEY_ID="release-test",
            PC_AGENT_REQUIRE_SIGNED_UPDATES="1",
        )
    )
    assert {
        check.name: check for check in configured.checks
    }["signed_updates"].status == "pass"


def test_release_admin_keygen_sign_and_no_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = _build_bundle(
        tmp_path, monkeypatch, name="admin", version=_FUTURE, value=24
    )
    keys, public_key = _make_keys(tmp_path, capsys)

    assert (
        release_admin.main(
            ["keygen", "--out-dir", str(keys), "--key-id", "release-test"]
        )
        == 1
    )
    capsys.readouterr()

    signature = _sign_bundle(bundle, keys, capsys)
    payload = json.loads(signature.read_text(encoding="utf-8"))
    assert payload["version"] == _FUTURE
    assert payload["key_id"] == "release-test"
    assert public_key not in signature.read_text(encoding="utf-8")
    private_text = (keys / "release-private-key.txt").read_text(encoding="utf-8")
    assert "PC_AGENT_RELEASE_PRIVATE_KEY=" in private_text
    assert private_text not in signature.read_text(encoding="utf-8")

    assert (
        release_admin.main(
            [
                "sign",
                str(bundle),
                "--private-key",
                str(keys / "release-private-key.txt"),
                "--key-id",
                "release-test",
            ]
        )
        == 1
    )
    capsys.readouterr()


def test_signed_check_stage_apply_and_rollback_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "target"
    _write_project(target, __version__, value=23, include_old=True)
    (target / ".env").write_text(f"GEMINI_API_KEY={_SECRET}\n", encoding="utf-8")
    (target / "data" / "private.txt").write_text(_SECRET, encoding="utf-8")
    bundle = _build_bundle(
        tmp_path,
        monkeypatch,
        name="future",
        version=_FUTURE,
        value=24,
        include_new=True,
    )
    keys, public_key = _make_keys(tmp_path, capsys)
    signature = _sign_bundle(bundle, keys, capsys)
    settings = _signed_settings(target, public_key)

    check = check_update(bundle, settings)
    assert check.current_version == __version__
    assert check.bundle.version == _FUTURE
    assert check.update_available is True
    assert check.signature.status == "verified"
    assert check.signature.key_id == "release-test"

    staged = stage_update(bundle, settings, signature_path=signature)
    assert staged.status == "staged"
    assert staged.signature_status == "verified"
    assert (staged.update_dir / "signature.json").is_file()
    assert (
        staged.staging_dir / "src" / "pc_agent" / "module.py"
    ).read_text(encoding="utf-8") == "VALUE = 24\n"
    assert list_updates(settings)[0].update_id == staged.update_id
    assert get_update(settings, "latest-staged").update_id == staged.update_id

    applied = apply_staged_update(settings, staged.update_id)
    module = target / "src" / "pc_agent" / "module.py"
    new_module = target / "src" / "pc_agent" / "new_module.py"
    old_only = target / "src" / "pc_agent" / "old_only.py"
    assert applied.status == "applied"
    assert applied.created_count >= 1
    assert applied.replaced_count >= 1
    assert "src/pc_agent/old_only.py" in applied.stale_files
    assert module.read_text(encoding="utf-8") == "VALUE = 24\n"
    assert new_module.read_text(encoding="utf-8") == "NEW_VALUE = 21\n"
    assert old_only.read_text(encoding="utf-8") == "OLD_VALUE = 9\n"
    assert (target / ".env").read_text(encoding="utf-8") == (
        f"GEMINI_API_KEY={_SECRET}\n"
    )
    assert (target / "data" / "private.txt").read_text(encoding="utf-8") == _SECRET
    assert (
        applied.backup_dir / "src" / "pc_agent" / "module.py"
    ).read_text(encoding="utf-8") == "VALUE = 23\n"

    rolled_back = rollback_update(settings, "latest-applied")
    archived = (
        rolled_back.update_dir
        / "rolled-back-created"
        / "src"
        / "pc_agent"
        / "new_module.py"
    )
    assert rolled_back.status == "rolled-back"
    assert module.read_text(encoding="utf-8") == "VALUE = 23\n"
    assert not new_module.exists()
    assert archived.read_text(encoding="utf-8") == "NEW_VALUE = 21\n"
    assert old_only.read_text(encoding="utf-8") == "OLD_VALUE = 9\n"


def test_signature_tamper_key_mismatch_and_unverified_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "target"
    _write_project(target, __version__, value=23)
    bundle = _build_bundle(
        tmp_path, monkeypatch, name="tamper", version=_FUTURE, value=24
    )
    keys, public_key = _make_keys(tmp_path, capsys)
    signature = _sign_bundle(bundle, keys, capsys)

    unverified = check_update(bundle, _settings(target))
    assert unverified.signature.status == "unverified"
    staged_unverified = stage_update(
        bundle, _settings(target), allow_unsigned=True
    )
    assert staged_unverified.signature_status == "unverified"

    mismatch_settings = _signed_settings(
        target, public_key, PC_AGENT_RELEASE_KEY_ID="other-key"
    )
    mismatch = check_update(bundle, mismatch_settings)
    assert mismatch.signature.status == "key-mismatch"
    with pytest.raises(PcAgentError, match="key-mismatch"):
        stage_update(bundle, mismatch_settings)

    payload = json.loads(signature.read_text(encoding="utf-8"))
    first = payload["signature"][0]
    payload["signature"] = ("A" if first != "A" else "B") + payload["signature"][1:]
    tampered = tmp_path / "tampered.sig.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")
    settings = _signed_settings(target, public_key)
    invalid = check_update(bundle, settings, signature_path=tampered)
    assert invalid.signature.status == "invalid"
    with pytest.raises(PcAgentError, match="signature rejected"):
        stage_update(bundle, settings, signature_path=tampered)


def test_unsigned_and_version_gates_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    _write_project(target, __version__, value=23)
    future = _build_bundle(
        tmp_path, monkeypatch, name="unsigned", version=_FUTURE, value=24
    )
    settings = _settings(target)
    check = check_update(future, settings)
    assert check.signature.status == "unsigned"
    assert check.update_available is True
    with pytest.raises(PcAgentError, match="no verified signature"):
        stage_update(future, settings)

    required = _settings(target, PC_AGENT_REQUIRE_SIGNED_UPDATES="1")
    with pytest.raises(PcAgentError, match="Signed updates are required"):
        stage_update(future, required, allow_unsigned=True)

    staged_unsigned = stage_update(future, settings, allow_unsigned=True)
    assert staged_unsigned.signature_status == "unsigned"

    past = _build_bundle(
        tmp_path, monkeypatch, name="past", version=_PAST, value=22
    )
    with pytest.raises(PcAgentError, match="older than"):
        stage_update(past, settings, allow_unsigned=True)
    downgrade = stage_update(
        past, settings, allow_unsigned=True, allow_downgrade=True
    )
    assert downgrade.bundle_version == _PAST

    same = _build_bundle(
        tmp_path, monkeypatch, name="same", version=__version__, value=23
    )
    with pytest.raises(PcAgentError, match="same as"):
        stage_update(same, settings, allow_unsigned=True)


def test_apply_failure_restores_files_and_preserves_created_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    _write_project(target, __version__, value=23)
    bundle = _build_bundle(
        tmp_path,
        monkeypatch,
        name="apply-failure",
        version=_FUTURE,
        value=24,
        include_new=True,
    )
    settings = _settings(target)
    staged = stage_update(bundle, settings, allow_unsigned=True)

    original_copy = updates_module._atomic_copy

    def flaky_copy(source: Path, destination: Path) -> None:
        if destination.name == "test_ok.py":
            raise OSError("simulated update copy failure")
        original_copy(source, destination)

    monkeypatch.setattr(updates_module, "_atomic_copy", flaky_copy)
    with pytest.raises(PcAgentError, match="rolled back"):
        apply_staged_update(settings, staged.update_id)

    failed = get_update(settings, staged.update_id, status="failed")
    archived = (
        failed.update_dir
        / "failed-created"
        / "src"
        / "pc_agent"
        / "new_module.py"
    )
    assert failed.last_error and "simulated" in failed.last_error
    assert (
        target / "src" / "pc_agent" / "module.py"
    ).read_text(encoding="utf-8") == "VALUE = 23\n"
    assert not (target / "src" / "pc_agent" / "new_module.py").exists()
    assert archived.read_text(encoding="utf-8") == "NEW_VALUE = 21\n"


def test_staged_tamper_receipt_tamper_and_modified_rollback_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    _write_project(target, __version__, value=23)
    bundle = _build_bundle(
        tmp_path, monkeypatch, name="refusals", version=_FUTURE, value=24
    )
    settings = _settings(target)
    staged = stage_update(bundle, settings, allow_unsigned=True)
    staged_module = staged.staging_dir / "src" / "pc_agent" / "module.py"
    staged_module.write_text("VALUE = 999\n", encoding="utf-8")
    with pytest.raises(PcAgentError, match="failed verification"):
        apply_staged_update(settings, staged.update_id)
    staged_module.write_text("VALUE = 24\n", encoding="utf-8")

    applied = apply_staged_update(settings, staged.update_id)
    target_module = target / "src" / "pc_agent" / "module.py"
    target_module.write_text("VALUE = 777\n", encoding="utf-8")
    with pytest.raises(PcAgentError, match="changed after"):
        rollback_update(settings, applied.update_id)
    assert target_module.read_text(encoding="utf-8") == "VALUE = 777\n"

    receipt_path = staged.receipt_path
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["files"][0]["path"] = "../evil.py"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(PcAgentError, match="Unsafe update file path"):
        get_update(settings, staged.update_id)


def test_cli_signed_update_flow_and_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "target"
    _write_project(target, __version__, value=23)
    bundle = _build_bundle(
        tmp_path,
        monkeypatch,
        name="cli",
        version=_FUTURE,
        value=24,
        include_new=True,
    )
    keys, public_key = _make_keys(tmp_path, capsys)
    _sign_bundle(bundle, keys, capsys)
    monkeypatch.setenv("PC_AGENT_PROVIDER_ORDER", "mock")
    monkeypatch.setenv("PC_AGENT_CREDENTIAL_STORE", "env")
    monkeypatch.setenv("PC_AGENT_RELEASE_PUBLIC_KEY", public_key)
    monkeypatch.setenv("PC_AGENT_RELEASE_KEY_ID", "release-test")
    monkeypatch.setenv("PC_AGENT_REQUIRE_SIGNED_UPDATES", "1")
    root = ["--root", str(target)]

    assert main([*root, "update", "check", str(bundle), "--json"]) == 0
    check_payload = json.loads(capsys.readouterr().out)
    assert check_payload["update_available"] is True
    assert check_payload["signature"]["status"] == "verified"

    assert main([*root, "update", "stage", str(bundle), "--json"]) == 0
    staged_payload = json.loads(capsys.readouterr().out)
    update_id = staged_payload["update_id"]
    assert staged_payload["status"] == "staged"

    monkeypatch.setattr(
        "pc_agent.cli._confirm_direct_write", lambda *args, **kwargs: False
    )
    assert main([*root, "update", "apply", update_id]) == 3
    assert "cancelled" in capsys.readouterr().out
    assert (
        target / "src" / "pc_agent" / "module.py"
    ).read_text(encoding="utf-8") == "VALUE = 23\n"
    monkeypatch.undo()

    # Re-apply the environment after undoing only the confirmation patch.
    monkeypatch.setenv("PC_AGENT_PROVIDER_ORDER", "mock")
    monkeypatch.setenv("PC_AGENT_CREDENTIAL_STORE", "env")
    monkeypatch.setenv("PC_AGENT_RELEASE_PUBLIC_KEY", public_key)
    monkeypatch.setenv("PC_AGENT_RELEASE_KEY_ID", "release-test")
    monkeypatch.setenv("PC_AGENT_REQUIRE_SIGNED_UPDATES", "1")
    assert main([*root, "update", "apply", update_id, "--yes", "--json"]) == 0
    applied_payload = json.loads(capsys.readouterr().out)
    assert applied_payload["status"] == "applied"
    assert (
        target / "src" / "pc_agent" / "module.py"
    ).read_text(encoding="utf-8") == "VALUE = 24\n"

    assert main([*root, "update", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["update_id"] == update_id
    assert main([*root, "update", "show", "latest"]) == 0
    assert update_id in capsys.readouterr().out

    assert main([*root, "update", "rollback", update_id, "--yes", "--json"]) == 0
    rolled_payload = json.loads(capsys.readouterr().out)
    assert rolled_payload["status"] == "rolled-back"
    assert (
        target / "src" / "pc_agent" / "module.py"
    ).read_text(encoding="utf-8") == "VALUE = 23\n"


def test_gui_controller_signed_update_flow_and_doctor_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "target"
    _write_project(target, __version__, value=23)
    bundle = _build_bundle(
        tmp_path,
        monkeypatch,
        name="gui",
        version=_FUTURE,
        value=24,
        include_new=True,
    )
    keys, public_key = _make_keys(tmp_path, capsys)
    signature = _sign_bundle(bundle, keys, capsys)
    settings = _signed_settings(target, public_key)
    controller = GuiController(settings)

    check = controller.check_offline_update(str(bundle), str(signature))
    assert "signature=verified" in format_update_check(check)
    staged = controller.stage_offline_update(str(bundle), str(signature))
    assert staged.update_id in format_staged_update(staged)
    applied = controller.apply_offline_update(staged.update_id)
    assert applied.status == "applied"
    updates = controller.list_offline_updates()
    assert staged.update_id in format_update_list(updates)
    rolled_back = controller.rollback_offline_update(applied.update_id)
    assert rolled_back.status == "rolled-back"
    assert (
        target / "src" / "pc_agent" / "module.py"
    ).read_text(encoding="utf-8") == "VALUE = 23\n"

    summary = controller.doctor_summary()
    text = summary.to_text()
    assert summary.version == __version__ == "0.29.0"
    assert "PHASE 27 PDF Signature Inspection" in text
    assert "Signed updates: public_key=yes" in text
    assert "latest=" in summary.signed_updates_summary


def test_phase21_scope_schema_tools_and_release_candidates() -> None:
    project_root = Path(__file__).resolve().parents[1]
    tool_ids = {tool.spec.id for tool in build_default_registry().list_tools()}
    candidates = {
        path.as_posix() for path in release_candidate_files(project_root)
    }
    pyproject = (project_root / "pyproject.toml").read_text(encoding="utf-8")

    assert __version__ == "0.29.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(tool_ids) == 62
    assert not any(tool_id.startswith("update.") for tool_id in tool_ids)
    assert "pc-agent-release-admin" in pyproject
    assert "src/pc_agent/updates.py" in candidates
    assert "src/pc_agent/release_admin.py" in candidates
    assert "tests/test_phase21_signed_updates.py" in candidates
    assert "docs/phase21.md" in candidates
