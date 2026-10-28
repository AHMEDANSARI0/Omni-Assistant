"""Phase 16: Windows Credential Manager provider-key storage."""

from __future__ import annotations

import ctypes
import json
import struct
from pathlib import Path
from typing import Any

import pytest

from pc_agent import cli
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.credentials import (
    CredentialStore,
    InMemoryCredentialBackend,
    WindowsCredentialBackend,
    _CredentialW,
    canonical_credential_provider,
    credential_target_name,
)
from pc_agent.errors import ConfigError, CredentialError
from pc_agent.gui.controller import GuiController
from pc_agent.providers.router import build_model_router, provider_statuses


class FakeAdvapi32:
    """In-memory CredReadW/CredWriteW/CredDeleteW/CredFree surface."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, Any]] = {}
        self.freed = 0
        self.last_error = 0
        self._allocations: list[tuple[Any, ...]] = []

    def CredWriteW(self, credential_ref: Any, _flags: int) -> bool:
        credential = ctypes.cast(
            credential_ref, ctypes.POINTER(_CredentialW)
        ).contents
        target = credential.TargetName
        assert isinstance(target, str)
        blob = ctypes.string_at(
            credential.CredentialBlob, credential.CredentialBlobSize
        )
        self.blobs[target] = blob
        self.metadata[target] = {
            "type": credential.Type,
            "persist": credential.Persist,
            "username": credential.UserName,
            "comment": credential.Comment,
        }
        return True

    def CredReadW(
        self, target: str, _type: int, _flags: int, output_ref: Any
    ) -> bool:
        blob = self.blobs.get(target)
        if blob is None:
            self.last_error = 1168  # ERROR_NOT_FOUND
            return False
        credential = _CredentialW()
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential.Type = 1
        credential.TargetName = target
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.c_void_p)
        credential.Persist = 2
        credential.UserName = self.metadata.get(target, {}).get("username")
        pointer = ctypes.pointer(credential)
        output = ctypes.cast(
            output_ref, ctypes.POINTER(ctypes.POINTER(_CredentialW))
        )
        output[0] = pointer
        self._allocations.append((credential, buffer, pointer))
        return True

    def CredDeleteW(self, target: str, _type: int, _flags: int) -> bool:
        if target not in self.blobs:
            self.last_error = 1168
            return False
        del self.blobs[target]
        return True

    def CredFree(self, _pointer: Any) -> None:
        self.freed += 1


def _store() -> tuple[CredentialStore, InMemoryCredentialBackend]:
    backend = InMemoryCredentialBackend()
    return CredentialStore(backend, mode="auto"), backend


def test_credential_store_canonical_validation_and_status() -> None:
    store, backend = _store()
    store.set_api_key("openai_compatible", "secret-value-123")

    assert canonical_credential_provider("OPENAI_COMPATIBLE") == "openai-compatible"
    assert store.get_api_key("openai-compatible") == "secret-value-123"
    assert store.configured_providers() == ("openai-compatible",)
    status = store.status()
    assert status.available is True
    assert status.stored_providers == ("openai-compatible",)
    assert "secret-value-123" not in status.summary()
    assert "secret-value-123" not in repr(status)
    assert backend.get("openai-compatible") == "secret-value-123"

    assert store.delete_api_key("openai-compatible") is True
    assert store.delete_api_key("openai-compatible") is False

    for invalid in ("", "has space", "line\nbreak", "x" * 1025):
        with pytest.raises(CredentialError):
            store.set_api_key("gemini", invalid)
    with pytest.raises(CredentialError, match="Unsupported credential provider"):
        store.set_api_key("mock", "not-a-key-slot")


@pytest.mark.skipif(struct.calcsize("P") != 8, reason="Win64 layout check")
def test_windows_credential_structure_layout() -> None:
    assert ctypes.sizeof(_CredentialW) == 80
    assert _CredentialW.TargetName.offset == 8
    assert _CredentialW.CredentialBlobSize.offset == 32
    assert _CredentialW.CredentialBlob.offset == 40
    assert _CredentialW.Persist.offset == 48
    assert _CredentialW.UserName.offset == 72


def test_windows_backend_roundtrip_with_simulated_advapi32() -> None:
    fake = FakeAdvapi32()
    store = CredentialStore(
        WindowsCredentialBackend(library=fake, is_windows=False), mode="windows"
    )

    store.set_api_key("gemini", "windows-secret-123")
    target = credential_target_name("gemini")
    assert fake.blobs[target] == "windows-secret-123".encode("utf-16-le")
    assert fake.metadata[target]["type"] == 1
    assert fake.metadata[target]["persist"] == 2
    assert fake.metadata[target]["username"] == "gemini"

    assert store.get_api_key("gemini") == "windows-secret-123"
    assert fake.freed == 1
    assert store.configured_providers() == ("gemini",)
    assert store.delete_api_key("gemini") is True
    assert store.get_api_key("gemini") is None
    assert store.delete_api_key("gemini") is False


def test_windows_backend_injected_last_error_wins_over_ctypes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pc_agent import credentials as credentials_module

    fake = FakeAdvapi32()
    store = CredentialStore(
        WindowsCredentialBackend(library=fake, is_windows=True), mode="windows"
    )
    monkeypatch.setattr(
        credentials_module.ctypes,
        "get_last_error",
        lambda: 0,
        raising=False,
    )
    monkeypatch.setattr(
        credentials_module.ctypes,
        "set_last_error",
        lambda _code: None,
        raising=False,
    )
    assert store.get_api_key("openrouter") is None
    assert fake.last_error == 1168


def test_settings_env_precedence_os_fallback_and_router(tmp_path: Path) -> None:
    store, backend = _store()
    backend.set("gemini", "os-gemini-secret")
    backend.set("openrouter", "os-router-secret")

    settings = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_PROVIDER_ORDER": "gemini,openrouter",
            "OPENROUTER_API_KEY": "env-router-secret",
        },
        load_dotenv=False,
        credential_store=store,
    )

    assert settings.gemini_api_key == "os-gemini-secret"
    assert settings.openrouter_api_key == "env-router-secret"
    assert settings.credential_source("gemini") == "windows-credential-manager"
    assert settings.credential_source("openrouter") == "environment"
    assert settings.providers_with_credentials == ("gemini", "openrouter")
    assert settings.public_summary()["provider_credential_sources"] == (
        "gemini=windows-credential-manager, openrouter=environment"
    )
    assert "os-gemini-secret" not in json.dumps(settings.public_summary())
    assert "os-gemini-secret" not in repr(settings)
    assert build_model_router(settings).provider_names == (
        "gemini",
        "openrouter",
    )
    statuses = {status.name: status for status in provider_statuses(settings)}
    assert statuses["gemini"].configured is True


def test_settings_credential_modes(tmp_path: Path) -> None:
    store, backend = _store()
    backend.set("gemini", "os-secret")

    env_only = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_PROVIDER_ORDER": "gemini",
            "PC_AGENT_CREDENTIAL_STORE": "env",
        },
        load_dotenv=False,
        credential_store=store,
    )
    assert env_only.gemini_api_key is None
    assert env_only.credential_sources == ()

    explicit_mapping = Settings.from_env(
        tmp_path,
        environ={"PC_AGENT_PROVIDER_ORDER": "gemini"},
        load_dotenv=False,
    )
    # An explicit mapping is a hermetic test seam: it must not consult a
    # developer's real OS vault unless a store object is injected.
    assert explicit_mapping.gemini_api_key is None

    with pytest.raises(ConfigError, match="PC_AGENT_CREDENTIAL_STORE"):
        Settings.from_env(
            tmp_path,
            environ={"PC_AGENT_CREDENTIAL_STORE": "vault"},
            load_dotenv=False,
        )


def _install_fake_store(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[CredentialStore, InMemoryCredentialBackend]:
    store, backend = _store()
    monkeypatch.setattr(cli, "build_credential_store", lambda _mode: store)
    return store, backend


def test_cli_credentials_set_status_migrate_delete(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store_obj, backend = _install_fake_store(monkeypatch)
    (tmp_path / ".env").write_text(
        "PC_AGENT_PROVIDER_ORDER=gemini,openrouter\n"
        "OPENROUTER_API_KEY=env-router-secret\n",
        encoding="utf-8",
    )
    root = ["--root", str(tmp_path)]

    answers = iter(["stored-gemini-secret", "stored-gemini-secret"])
    monkeypatch.setattr(
        "pc_agent.cli.getpass.getpass", lambda _prompt: next(answers)
    )
    assert main([*root, "credentials", "set", "gemini"]) == 0
    set_output = capsys.readouterr().out
    assert "Stored gemini API key" in set_output
    assert "stored-gemini-secret" not in set_output
    assert backend.get("gemini") == "stored-gemini-secret"

    assert main([*root, "credentials", "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["available"] is True
    assert status["stored_providers"] == ["gemini"]
    assert status["effective_sources"]["openrouter"] == "environment"
    assert "secret" not in json.dumps(status)

    assert main([*root, "providers", "list"]) == 0
    providers_output = capsys.readouterr().out
    assert "openrouter" in providers_output
    assert "source=environment" in providers_output
    assert "env-router-secret" not in providers_output

    assert main([*root, "credentials", "migrate", "openrouter", "--yes"]) == 0
    migrate_output = capsys.readouterr().out
    assert "Migrated openrouter API key" in migrate_output
    assert "env-router-secret" not in migrate_output
    assert backend.get("openrouter") == "env-router-secret"
    assert "OPENROUTER_API_KEY=env-router-secret" in (
        tmp_path / ".env"
    ).read_text(encoding="utf-8")

    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    assert main([*root, "credentials", "delete", "gemini"]) == 4
    capsys.readouterr()
    assert backend.get("gemini") == "stored-gemini-secret"

    assert main([*root, "credentials", "delete", "gemini", "--yes"]) == 0
    assert "Deleted stored gemini API key" in capsys.readouterr().out
    assert backend.get("gemini") is None


def test_cli_credential_set_mismatch_stores_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store_obj, backend = _install_fake_store(monkeypatch)
    answers = iter(["first-secret", "second-secret"])
    monkeypatch.setattr(
        "pc_agent.cli.getpass.getpass", lambda _prompt: next(answers)
    )

    assert (
        main(["--root", str(tmp_path), "credentials", "set", "openai"])
        == 1
    )
    captured = capsys.readouterr()
    assert "do not match" in captured.err
    assert "first-secret" not in captured.out + captured.err
    assert backend.get("openai") is None


def test_cli_doctor_credential_store_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store_obj, backend = _install_fake_store(monkeypatch)
    backend.set("gemini", "doctor-secret")
    (tmp_path / ".env").write_text(
        "PC_AGENT_PROVIDER_ORDER=openrouter\nOPENROUTER_API_KEY=doctor-env-secret\n",
        encoding="utf-8",
    )

    assert main(["--root", str(tmp_path), "doctor"]) == 0
    doctor = capsys.readouterr().out
    assert "PHASE 16.1 Credential Manager Hotfix" in doctor
    assert "Credential store:" in doctor
    assert "stored=gemini" in doctor
    assert "openrouter=environment" in doctor
    assert "built-ins=54" in doctor
    assert "doctor-secret" not in doctor
    assert "doctor-env-secret" not in doctor


def test_gui_controller_credential_summary(tmp_path: Path) -> None:
    store, backend = _store()
    backend.set("gemini", "gui-os-secret")
    settings = Settings.from_env(
        tmp_path,
        environ={"PC_AGENT_PROVIDER_ORDER": "gemini"},
        load_dotenv=False,
        credential_store=store,
    )
    controller = GuiController(settings, credential_store=store)

    summary = controller.doctor_summary()
    text = summary.to_text()
    assert "PHASE 16.1 Credential Manager Hotfix" in text
    assert "Credential store:" in text
    assert "stored=gemini" in text
    assert "gemini=windows-credential-manager" in text
    assert "gui-os-secret" not in text

    statuses = {status.name: status for status in controller.provider_statuses()}
    assert statuses["gemini"].configured is True
    assert "windows-credential-manager" in statuses["gemini"].detail
    assert "gui-os-secret" not in statuses["gemini"].detail
