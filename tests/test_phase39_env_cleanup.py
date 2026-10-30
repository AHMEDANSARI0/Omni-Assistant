"""Phase 39: verified .env provider-key cleanup."""

from __future__ import annotations

import codecs
import hashlib
import json
from pathlib import Path

import pytest

from pc_agent import __version__, cli
from pc_agent.cli import build_parser, main
from pc_agent.config import Settings
from pc_agent.credentials import (
    CredentialStore,
    InMemoryCredentialBackend,
    UnavailableCredentialBackend,
)
from pc_agent.env_cleanup import (
    PROVIDER_API_KEY_ENV_NAMES,
    apply_env_key_cleanup,
    plan_env_key_cleanup,
)
from pc_agent.errors import CredentialError
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[1]


def _store(
    values: dict[str, str] | None = None,
) -> tuple[CredentialStore, InMemoryCredentialBackend]:
    backend = InMemoryCredentialBackend()
    store = CredentialStore(backend, mode="windows")
    for provider, secret in (values or {}).items():
        store.set_api_key(provider, secret)
    return store, backend


def _write(path: Path, data: bytes | str) -> None:
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)


def test_phase39_plan_only_marks_exact_verified_matches(tmp_path: Path) -> None:
    store, _backend = _store(
        {
            "gemini": "gemini-current-secret",
            "openrouter": "vault-router-secret",
            "anthropic": "anthropic-current-secret",
        }
    )
    env_path = tmp_path / ".env"
    body = (
        "# keep this comment\r\n"
        "PC_AGENT_PROVIDER_ORDER=gemini,openrouter,anthropic\r\n"
        "GEMINI_API_KEY=stale-gemini-secret\n"
        'export GEMINI_API_KEY="gemini-current-secret"\n'
        "OPENROUTER_API_KEY=different-router-secret\n"
        "OPENAI_API_KEY=openai-only-in-env\n"
        "OLLAMA_API_KEY=\n"
        "# ANTHROPIC_API_KEY=comment-only\n"
        "ANTHROPIC_API_KEY='anthropic-current-secret'\n"
        "OTHER_SETTING=keep-me\n"
    )
    _write(env_path, codecs.BOM_UTF8 + body.encode("utf-8"))

    plan = plan_env_key_cleanup(
        env_path,
        store,
        environ={"OPENAI_API_KEY": "process-value"},
    )

    assert plan.removable_providers == ("gemini", "anthropic")
    assert dict(plan.statuses) == {
        "gemini": "verified_match",
        "openrouter": "value_mismatch",
        "openai": "not_stored",
        "openai-compatible": "not_in_dotenv",
        "ollama": "empty_in_dotenv",
        "anthropic": "verified_match",
    }
    assert plan.removed_line_numbers == (3, 4, 9)
    assert plan.duplicate_line_count == 1
    assert plan.process_environment_providers == ("openai",)
    expected_body = (
        "# keep this comment\r\n"
        "PC_AGENT_PROVIDER_ORDER=gemini,openrouter,anthropic\r\n"
        "OPENROUTER_API_KEY=different-router-secret\n"
        "OPENAI_API_KEY=openai-only-in-env\n"
        "OLLAMA_API_KEY=\n"
        "# ANTHROPIC_API_KEY=comment-only\n"
        "OTHER_SETTING=keep-me\n"
    )
    expected = codecs.BOM_UTF8 + expected_body.encode("utf-8")
    assert plan.proposed_size == len(expected)
    assert plan.proposed_sha256 == hashlib.sha256(expected).hexdigest()

    rendered = json.dumps(plan.to_dict()) + repr(plan) + plan.summary()
    for secret in (
        "gemini-current-secret",
        "stale-gemini-secret",
        "vault-router-secret",
        "different-router-secret",
        "openai-only-in-env",
        "anthropic-current-secret",
    ):
        assert secret not in rendered
    assert env_path.read_bytes().endswith(b"OTHER_SETTING=keep-me\n")


def test_phase39_apply_receipt_and_settings_fallback(tmp_path: Path) -> None:
    store, _backend = _store(
        {
            "gemini": "gemini-vault-secret",
            "openrouter": "router-vault-secret",
        }
    )
    env_path = tmp_path / ".env"
    _write(
        env_path,
        "PC_AGENT_PROVIDER_ORDER=gemini,openrouter\n"
        "GEMINI_API_KEY=gemini-vault-secret\n"
        "OPENROUTER_API_KEY=router-vault-secret\n"
        "PC_AGENT_LOG_LEVEL=INFO\n",
    )
    plan = plan_env_key_cleanup(env_path, store, environ={})
    result = apply_env_key_cleanup(
        plan,
        store,
        receipt_dir=tmp_path / "data" / "credential-cleanups",
        environ={},
    )

    cleaned = env_path.read_text(encoding="utf-8")
    assert result.removed_providers == ("gemini", "openrouter")
    assert result.verified is True
    assert "GEMINI_API_KEY" not in cleaned
    assert "OPENROUTER_API_KEY" not in cleaned
    assert "gemini-vault-secret" not in cleaned
    assert "router-vault-secret" not in cleaned
    assert "PC_AGENT_PROVIDER_ORDER=gemini,openrouter" in cleaned

    assert result.receipt_path is not None
    receipt_text = result.receipt_path.read_text(encoding="utf-8")
    receipt = json.loads(receipt_text)
    assert receipt["removed_providers"] == ["gemini", "openrouter"]
    assert receipt["plaintext_backup_created"] is False
    assert receipt["secret_values_included"] is False
    assert "gemini-vault-secret" not in receipt_text
    assert "router-vault-secret" not in receipt_text
    assert not list(tmp_path.glob(".env*tmp*"))

    settings = Settings.from_env(
        tmp_path,
        environ={},
        credential_store=store,
    )
    assert settings.gemini_api_key == "gemini-vault-secret"
    assert settings.openrouter_api_key == "router-vault-secret"
    assert settings.credential_source("gemini") == "windows-credential-manager"
    assert settings.credential_source("openrouter") == "windows-credential-manager"

    second_plan = plan_env_key_cleanup(env_path, store, environ={})
    assert second_plan.removable_providers == ()
    assert second_plan.status_for("gemini") == "not_in_dotenv"
    second = apply_env_key_cleanup(second_plan, store, environ={})
    assert second.removed_providers == ()
    assert second.result_sha256 == result.result_sha256


def test_phase39_cleanup_fails_closed_without_touching_env(
    tmp_path: Path,
) -> None:
    store, _backend = _store({"gemini": "vault-secret"})
    env_path = tmp_path / ".env"
    original = "GEMINI_API_KEY=vault-secret\nOTHER=keep\n"
    _write(env_path, original)

    unavailable = CredentialStore(
        UnavailableCredentialBackend(detail="disabled for test"),
        mode="env",
    )
    with pytest.raises(CredentialError, match="unavailable"):
        plan_env_key_cleanup(env_path, unavailable, environ={})
    assert env_path.read_text(encoding="utf-8") == original

    plan = plan_env_key_cleanup(env_path, store, environ={})
    _write(env_path, original + "CHANGED=1\n")
    with pytest.raises(CredentialError, match="changed after approval"):
        apply_env_key_cleanup(plan, store, environ={})
    assert env_path.read_text(encoding="utf-8") == original + "CHANGED=1\n"

    _write(env_path, original)
    plan = plan_env_key_cleanup(env_path, store, environ={})
    store.set_api_key("gemini", "replacement-vault-secret")
    with pytest.raises(CredentialError, match="changed after approval"):
        apply_env_key_cleanup(plan, store, environ={})
    assert env_path.read_text(encoding="utf-8") == original

    malformed = tmp_path / "malformed.env"
    _write(malformed, "GEMINI_API_KEY=vault-secret\nnot-an-assignment\n")
    with pytest.raises(CredentialError, match="Invalid .env entry"):
        plan_env_key_cleanup(malformed, store, environ={})

    symlink = tmp_path / "linked.env"
    symlink.symlink_to(env_path)
    with pytest.raises(CredentialError, match="not found"):
        plan_env_key_cleanup(symlink, store, environ={})


def test_phase39_direct_cli_dry_run_denial_apply_and_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _backend = _store(
        {
            "gemini": "cli-gemini-secret",
            "openrouter": "cli-vault-router-secret",
        }
    )
    monkeypatch.setattr(cli, "build_credential_store", lambda _mode: store)
    env_path = tmp_path / ".env"
    original = (
        "PC_AGENT_PROVIDER_ORDER=gemini,openrouter\n"
        "GEMINI_API_KEY=cli-gemini-secret\n"
        "OPENROUTER_API_KEY=cli-different-router-secret\n"
    )
    _write(env_path, original)
    root = ["--root", str(tmp_path)]

    parser = build_parser()
    args = parser.parse_args(
        ["credentials", "remove-env-keys", "gemini", "--dry-run", "--json"]
    )
    assert args.credentials_command == "remove-env-keys"
    assert args.provider == "gemini"
    assert args.dry_run is True

    assert (
        main(
            [
                *root,
                "credentials",
                "remove-env-keys",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    dry_output = capsys.readouterr().out
    dry = json.loads(dry_output)
    assert dry["action"] == "dry-run"
    assert dry["removable_providers"] == ["gemini"]
    assert dry["statuses"]["openrouter"] == "value_mismatch"
    assert "cli-gemini-secret" not in dry_output
    assert env_path.read_text(encoding="utf-8") == original

    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    assert main([*root, "credentials", "remove-env-keys", "gemini"]) == 4
    denied = capsys.readouterr()
    assert "cli-gemini-secret" not in denied.out + denied.err
    assert env_path.read_text(encoding="utf-8") == original

    assert (
        main(
            [
                *root,
                "credentials",
                "remove-env-keys",
                "gemini",
                "--yes",
                "--json",
            ]
        )
        == 0
    )
    applied_output = capsys.readouterr().out
    applied = json.loads(applied_output)
    assert applied["removed_providers"] == ["gemini"]
    assert applied["verified"] is True
    assert applied["secret_values_included"] is False
    assert "cli-gemini-secret" not in applied_output
    cleaned = env_path.read_text(encoding="utf-8")
    assert "GEMINI_API_KEY" not in cleaned
    assert "OPENROUTER_API_KEY=cli-different-router-secret" in cleaned
    receipts = list((tmp_path / "data" / "credential-cleanups").glob("*.json"))
    assert len(receipts) == 1
    assert "cli-gemini-secret" not in receipts[0].read_text(encoding="utf-8")


def test_phase39_scope_inventory_and_counts() -> None:
    assert __version__ == "0.41.0"
    assert len(build_default_registry().list_tools()) == 62
    assert LATEST_SCHEMA_VERSION == 6
    assert PROVIDER_API_KEY_ENV_NAMES == {
        "gemini": "GEMINI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "openai": "OPENAI_API_KEY",
        "openai-compatible": "OPENAI_COMPATIBLE_API_KEY",
        "ollama": "OLLAMA_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "src/pc_agent/env_cleanup.py",
        "docs/phase39.md",
        "tests/test_phase39_env_cleanup.py",
    ):
        assert expected in release_names

    phase38 = (ROOT / "docs" / "phase38.md").read_text(encoding="utf-8")
    phase39 = (ROOT / "docs" / "phase39.md").read_text(encoding="utf-8")
    assert "version | 0.40.0" in phase38
    assert "version | 0.41.0" in phase39
    assert "never created" in phase39
    assert "No planner" in phase39
