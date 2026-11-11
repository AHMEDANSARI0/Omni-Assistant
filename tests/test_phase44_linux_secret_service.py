"""Phase 44: Linux Secret Service provider-key storage."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from pc_agent import __version__
from pc_agent import credentials as credentials_module
from pc_agent.config import Settings
from pc_agent.credentials import (
    CredentialStore,
    LinuxSecretServiceBackend,
    build_credential_store,
)
from pc_agent.errors import CredentialError
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[1]


class FakeSecretTool:
    """In-memory stand-in with libsecret secret-tool exit/stderr semantics."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, ...], bytes] = {}
        self.calls: list[tuple[tuple[str, ...], bytes | None, float]] = []
        self.search_returncode = 0
        self.search_stderr = b""
        self.lookup_stderr = b""
        self.timeout_actions: set[str] = set()

    def __call__(
        self,
        argv: Sequence[str],
        input_data: bytes | None,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[bytes]:
        args = tuple(str(part) for part in argv)
        self.calls.append((args, input_data, timeout_seconds))
        action = args[1]
        if action in self.timeout_actions:
            raise subprocess.TimeoutExpired(args, timeout_seconds)

        attributes = list(args[2:])
        if action == "store":
            label = attributes.pop(0)
            assert label.startswith("--label=Self-Healing PC Agent")
        key = tuple(attributes)

        if action == "search":
            return self._completed(
                self.search_returncode, stderr=self.search_stderr
            )
        if action == "store":
            self.values[key] = input_data or b""
            return self._completed(0)
        if action == "lookup":
            if self.lookup_stderr:
                return self._completed(1, stderr=self.lookup_stderr)
            value = self.values.get(key)
            if value is None:
                return self._completed(1)
            return self._completed(0, stdout=value)
        if action == "clear":
            if self.values.pop(key, None) is None:
                return self._completed(1)
            return self._completed(0)
        raise AssertionError(f"unexpected secret-tool action: {action}")

    @staticmethod
    def _completed(
        returncode: int,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=(), returncode=returncode, stdout=stdout, stderr=stderr
        )


def _linux_store(
    fake: FakeSecretTool | None = None,
) -> tuple[CredentialStore, FakeSecretTool, LinuxSecretServiceBackend]:
    runner = fake or FakeSecretTool()
    backend = LinuxSecretServiceBackend(
        command_prefix=("/fake/secret-tool",),
        is_linux=True,
        runner=runner,
    )
    return CredentialStore(backend, mode="linux"), runner, backend


def test_phase44_linux_secret_service_roundtrip_and_secret_hygiene() -> None:
    store, fake, _backend = _linux_store()
    secret = "linux-secret-123"

    assert store.backend_name == "linux-secret-service"
    assert store.source_name == "linux-secret-service"
    assert store.get_api_key("gemini") is None

    store.set_api_key("gemini", secret)
    assert store.get_api_key("gemini") == secret
    assert store.configured_providers() == ("gemini",)
    status = store.status()
    assert status.available is True
    assert status.backend == "linux-secret-service"
    assert status.stored_providers == ("gemini",)
    assert secret not in status.summary()
    assert secret not in repr(status)

    store_calls = [call for call in fake.calls if call[0][1] == "store"]
    assert len(store_calls) == 1
    argv, input_data, timeout = store_calls[0]
    assert timeout == 10.0
    assert input_data == secret.encode("utf-8")
    assert not input_data.endswith(b"\n")
    assert secret not in " ".join(argv)
    assert argv[-6:] == (
        "application",
        "self-healing-pc-agent",
        "provider",
        "gemini",
        "purpose",
        "provider-api-key",
    )

    assert store.delete_api_key("gemini") is True
    assert store.get_api_key("gemini") is None
    assert store.delete_api_key("gemini") is False


def test_phase44_linux_secret_service_fails_closed() -> None:
    not_linux = LinuxSecretServiceBackend(
        command_prefix=("/fake/secret-tool",),
        is_linux=False,
        runner=FakeSecretTool(),
    )
    assert not_linux.probe() == (
        False,
        "Linux Secret Service is available only on Linux",
    )

    unavailable_fake = FakeSecretTool()
    unavailable_fake.search_returncode = 1
    unavailable_fake.search_stderr = b"cannot connect to session bus"
    unavailable_store, _fake, _backend = _linux_store(unavailable_fake)
    assert unavailable_store.available is False
    assert unavailable_store.get_api_key("gemini") is None
    with pytest.raises(CredentialError, match="unavailable"):
        unavailable_store.set_api_key("gemini", "linux-secret-123")

    store, fake, _backend = _linux_store()
    secret = "do-not-copy-this-secret"
    store.set_api_key("gemini", secret)
    fake.lookup_stderr = f"synthetic dbus failure {secret}".encode()
    with pytest.raises(CredentialError, match="could not read") as caught:
        store.get_api_key("gemini")
    assert secret not in str(caught.value)

    fake.lookup_stderr = b""
    fake.timeout_actions = {"lookup"}
    with pytest.raises(CredentialError, match="timed out"):
        store.get_api_key("gemini")


def test_phase44_linux_secret_service_default_subprocess_runner(
    tmp_path: Path,
) -> None:
    script = tmp_path / "fake_secret_tool.py"
    script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "state_path = Path(__file__).with_suffix('.json')\n"
        "state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}\n"
        "action = sys.argv[1]\n"
        "args = sys.argv[2:]\n"
        "if action == 'store':\n"
        "    args = args[1:]\n"
        "key = json.dumps(args)\n"
        "if action == 'search':\n"
        "    raise SystemExit(0)\n"
        "if action == 'store':\n"
        "    state[key] = sys.stdin.buffer.read().decode()\n"
        "    state_path.write_text(json.dumps(state), encoding='utf-8')\n"
        "    raise SystemExit(0)\n"
        "if action == 'lookup':\n"
        "    value = state.get(key)\n"
        "    if value is None:\n"
        "        raise SystemExit(1)\n"
        "    sys.stdout.write(value)\n"
        "    raise SystemExit(0)\n"
        "if action == 'clear':\n"
        "    existed = state.pop(key, None) is not None\n"
        "    state_path.write_text(json.dumps(state), encoding='utf-8')\n"
        "    raise SystemExit(0 if existed else 1)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    backend = LinuxSecretServiceBackend(
        command_prefix=(sys.executable, str(script)),
        is_linux=True,
    )
    store = CredentialStore(backend, mode="linux")

    store.set_api_key("openrouter", "process-secret-456")
    assert store.get_api_key("openrouter") == "process-secret-456"
    assert store.delete_api_key("openrouter") is True
    assert store.get_api_key("openrouter") is None


def test_phase44_settings_linux_mode_and_source_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _fake, backend = _linux_store()
    store.set_api_key("gemini", "settings-linux-secret")

    built = build_credential_store("linux", backend=backend)
    assert built.backend_name == "linux-secret-service"
    assert built.source_name == "linux-secret-service"

    monkeypatch.setattr(credentials_module.sys, "platform", "linux")
    assert build_credential_store("auto").backend_name == "linux-secret-service"
    monkeypatch.setattr(credentials_module.sys, "platform", "freebsd")
    assert build_credential_store("auto").backend_name == "unsupported-platform"
    monkeypatch.setattr(credentials_module.sys, "platform", sys.platform)

    settings = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_PROVIDER_ORDER": "gemini,openrouter",
            "PC_AGENT_CREDENTIAL_STORE": "linux",
            "OPENROUTER_API_KEY": "env-router-secret",
        },
        load_dotenv=False,
        credential_store=store,
    )
    assert settings.credential_store == "linux"
    assert settings.credential_store_backend == "linux-secret-service"
    assert settings.credential_store_available is True
    assert settings.gemini_api_key == "settings-linux-secret"
    assert settings.openrouter_api_key == "env-router-secret"
    assert settings.credential_source("gemini") == "linux-secret-service"
    assert settings.credential_source("openrouter") == "environment"
    assert "settings-linux-secret" not in repr(settings)


def test_phase44_scope_inventory_and_counts() -> None:
    assert __version__ == "0.82.17"
    assert len(build_default_registry().list_tools()) == 69
    assert LATEST_SCHEMA_VERSION == 18

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "src/pc_agent/credentials.py",
        "docs/phase44.md",
        "tests/test_phase44_linux_secret_service.py",
    ):
        assert expected in release_names

    phase43 = (ROOT / "docs" / "phase43.md").read_text(encoding="utf-8")
    phase44 = (ROOT / "docs" / "phase44.md").read_text(encoding="utf-8")
    assert "version | 0.45.0" in phase43
    assert "version | 0.46.0" in phase44
    assert "macOS Keychain is not implemented" in phase44
