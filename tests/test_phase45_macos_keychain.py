from __future__ import annotations

import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest

from pc_agent import __version__
from pc_agent import credentials as credentials_module
from pc_agent.config import Settings
from pc_agent.credentials import (
    CredentialStore,
    MacOSKeychainBackend,
    build_credential_store,
)
from pc_agent.errors import CredentialError
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

ROOT = Path(__file__).parents[1]
_SECRET = "phase45-synthetic-key"
_NOT_FOUND = (
    b"security: SecKeychainSearchCopyNext: The specified item could not be "
    b"found in the keychain.\n"
)


class FakeMacOSSecurity:
    def __init__(self) -> None:
        self.available = True
        self.output_style = "raw"
        self.lookup_stderr: bytes | None = None
        self.set_stderr: bytes | None = None
        self.timeout_actions: set[str] = set()
        self.stored: dict[str, bytes] = {}
        self.calls: list[tuple[tuple[str, ...], bytes | None]] = []

    def __call__(
        self,
        argv: Sequence[str],
        input_data: bytes | None,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[bytes]:
        args = tuple(str(part) for part in argv)
        self.calls.append((args, input_data))
        action = self._action(args)
        if action in self.timeout_actions:
            raise subprocess.TimeoutExpired(list(args), timeout_seconds)
        if action == "list-keychains":
            if not self.available:
                return self._completed(args, 1, stderr=b"keychain unavailable")
            return self._completed(
                args,
                0,
                stdout=b'    "/Users/test/Library/Keychains/login.keychain-db"\n',
            )
        if action == "find-generic-password":
            if self.lookup_stderr is not None:
                return self._completed(args, 44, stderr=self.lookup_stderr)
            provider = args[args.index("-a") + 1]
            payload = self.stored.get(provider)
            if payload is None:
                return self._completed(args, 44, stderr=_NOT_FOUND)
            if self.output_style == "hex":
                stdout = payload.hex().upper().encode("ascii") + b"\n"
            elif self.output_style == "hex0x":
                stdout = b"0x" + payload.hex().encode("ascii") + b"\n"
            else:
                stdout = payload + b"\n"
            return self._completed(args, 0, stdout=stdout)
        if action == "interactive":
            if self.set_stderr is not None:
                return self._completed(
                    args,
                    0,
                    stdout=b"security> ",
                    stderr=self.set_stderr,
                )
            assert input_data is not None
            tokens = input_data.decode("ascii").strip().split()
            assert tokens[0] == "add-generic-password"
            provider = tokens[tokens.index("-a") + 1]
            service = tokens[tokens.index("-s") + 1]
            assert service == "self-healing-pc-agent-provider-api-key"
            self.stored[provider] = bytes.fromhex(tokens[tokens.index("-X") + 1])
            return self._completed(args, 0, stdout=b"security> ")
        if action == "delete-generic-password":
            provider = args[args.index("-a") + 1]
            self.stored.pop(provider, None)
            return self._completed(args, 0)
        raise AssertionError(f"Unexpected fake security action: {action}")

    @staticmethod
    def _action(args: tuple[str, ...]) -> str:
        if args[-2:] == ("-q", "-i"):
            return "interactive"
        return args[1]

    @staticmethod
    def _completed(
        args: tuple[str, ...],
        returncode: int,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)


def _store(fake: FakeMacOSSecurity) -> CredentialStore:
    backend = MacOSKeychainBackend(
        executable="/fake/security",
        is_macos=True,
        runner=fake,
    )
    return CredentialStore(backend, mode="macos")


def test_phase45_macos_keychain_round_trip_keeps_secret_out_of_argv() -> None:
    fake = FakeMacOSSecurity()
    store = _store(fake)

    assert store.backend_name == "macos-keychain"
    assert store.source_name == "macos-keychain"
    assert store.set_api_key("Gemini", _SECRET) == "gemini"
    assert store.get_api_key("gemini") == _SECRET
    assert store.contains("gemini")
    assert fake.stored["gemini"] == b"pc-agent-v1:" + _SECRET.encode()

    status = store.status()
    assert status.mode == "macos"
    assert status.backend == "macos-keychain"
    assert status.available
    assert status.stored_providers == ("gemini",)

    for argv, input_data in fake.calls:
        assert _SECRET not in " ".join(argv)
        assert _SECRET.encode() not in (input_data or b"")
    interactive_inputs = [
        input_data
        for argv, input_data in fake.calls
        if argv[-2:] == ("-q", "-i") and input_data is not None
    ]
    assert interactive_inputs
    assert b"add-generic-password -U" in interactive_inputs[0]
    assert b"-X" in interactive_inputs[0]

    assert store.delete_api_key("gemini")
    assert store.get_api_key("gemini") is None
    assert not store.delete_api_key("gemini")


def test_phase45_macos_keychain_decodes_hex_output_and_all_hex_secret() -> None:
    fake = FakeMacOSSecurity()
    fake.output_style = "hex"
    store = _store(fake)
    unicode_secret = "päss-wörd-123"

    store.set_api_key("openrouter", unicode_secret)
    assert store.get_api_key("openrouter") == unicode_secret

    fake.output_style = "hex0x"
    all_hex_secret = "deadbeefcafe"
    store.set_api_key("openai", all_hex_secret)
    assert store.get_api_key("openai") == all_hex_secret

    fake.output_style = "raw"
    assert store.get_api_key("openrouter") == unicode_secret
    assert store.get_api_key("openai") == all_hex_secret


def test_phase45_macos_keychain_fails_closed_without_leaking_output() -> None:
    fake = FakeMacOSSecurity()
    store = _store(fake)
    fake.stored["gemini"] = b"unprefixed-secret"
    with pytest.raises(CredentialError, match="unsupported format") as corrupt:
        store.get_api_key("gemini")
    assert "unprefixed-secret" not in str(corrupt.value)

    fake.lookup_stderr = b"User interaction is not allowed"
    with pytest.raises(CredentialError, match="could not read") as denied:
        store.get_api_key("openai")
    assert "User interaction" not in str(denied.value)
    fake.lookup_stderr = None

    fake.set_stderr = b"add-generic-password: returned -25308"
    with pytest.raises(CredentialError, match="could not store") as stored:
        store.set_api_key("anthropic", _SECRET)
    assert "-25308" not in str(stored.value)
    fake.set_stderr = None

    fake.timeout_actions.add("find-generic-password")
    with pytest.raises(CredentialError, match="timed out"):
        store.get_api_key("ollama")
    fake.timeout_actions.clear()

    call_count = len(fake.calls)
    with pytest.raises(CredentialError, match="too large"):
        store.set_api_key("openai-compatible", "é" * 1024)
    assert len(fake.calls) == call_count

    fake.available = False
    unavailable = _store(fake)
    assert not unavailable.available
    with pytest.raises(CredentialError, match="unavailable"):
        unavailable.set_api_key("gemini", _SECRET)

    wrong_platform = MacOSKeychainBackend(is_macos=False, runner=fake)
    assert wrong_platform.probe() == (
        False,
        "macOS Keychain is available only on macOS",
    )


def test_phase45_macos_keychain_default_subprocess_runner(
    tmp_path: Path,
) -> None:
    script = tmp_path / "fake_security.py"
    script.write_text(
        """
import json
import sys
from pathlib import Path

STATE = Path(__file__).with_suffix(".json")
state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
args = sys.argv[1:]

def save() -> None:
    STATE.write_text(json.dumps(state), encoding="utf-8")

if args == ["list-keychains"]:
    print('    "/tmp/login.keychain-db"')
elif args and args[0] == "find-generic-password":
    provider = args[args.index("-a") + 1]
    payload_hex = state.get(provider)
    if payload_hex is None:
        print(
            "security: SecKeychainSearchCopyNext: The specified item could "
            "not be found in the keychain.",
            file=sys.stderr,
        )
        raise SystemExit(44)
    sys.stdout.buffer.write(bytes.fromhex(payload_hex) + b"\\n")
elif args == ["-q", "-i"]:
    tokens = sys.stdin.buffer.read().decode("ascii").strip().split()
    provider = tokens[tokens.index("-a") + 1]
    state[provider] = tokens[tokens.index("-X") + 1]
    save()
    print("security> ", end="")
elif args and args[0] == "delete-generic-password":
    provider = args[args.index("-a") + 1]
    state.pop(provider, None)
    save()
else:
    raise SystemExit(3)
""".lstrip(),
        encoding="utf-8",
    )
    backend = MacOSKeychainBackend(
        command_prefix=(sys.executable, str(script)),
        is_macos=True,
    )
    store = CredentialStore(backend, mode="macos")

    store.set_api_key("gemini", _SECRET)
    assert store.get_api_key("gemini") == _SECRET
    assert store.configured_providers() == ("gemini",)
    assert store.delete_api_key("gemini")
    assert store.get_api_key("gemini") is None


@pytest.mark.parametrize("platform", ["darwin"])
def test_phase45_settings_and_auto_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    fake = FakeMacOSSecurity()
    fake.stored["gemini"] = b"pc-agent-v1:" + _SECRET.encode()
    store = _store(fake)
    settings = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_PROVIDER_ORDER": "gemini,openrouter",
            "PC_AGENT_CREDENTIAL_STORE": "macos",
            "OPENROUTER_API_KEY": "env-router-secret",
        },
        load_dotenv=False,
        credential_store=store,
    )
    assert settings.credential_store == "macos"
    assert settings.credential_store_backend == "macos-keychain"
    assert settings.credential_store_available
    assert settings.gemini_api_key == _SECRET
    assert settings.openrouter_api_key == "env-router-secret"
    assert settings.credential_source("gemini") == "macos-keychain"
    assert settings.credential_source("openrouter") == "environment"
    assert _SECRET not in repr(settings)

    built = build_credential_store("macos")
    assert built.backend_name == "macos-keychain"
    assert built.source_name == "macos-keychain"
    monkeypatch.setattr(credentials_module.sys, "platform", platform)
    assert build_credential_store("auto").backend_name == "macos-keychain"
    monkeypatch.setattr(credentials_module.sys, "platform", sys.platform)


def test_phase45_scope_and_documentation() -> None:
    credentials = (ROOT / "src" / "pc_agent" / "credentials.py").read_text(encoding="utf-8")
    config = (ROOT / "src" / "pc_agent" / "config.py").read_text(encoding="utf-8")
    phase = (ROOT / "docs" / "phase45.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tools_doc = (ROOT / "docs" / "tools.md").read_text(encoding="utf-8")
    providers = (ROOT / "docs" / "providers.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert __version__ == "0.82.17"
    assert LATEST_SCHEMA_VERSION == 18
    assert len(build_default_registry().list_tools()) == 69
    assert project["project"]["version"] == __version__
    assert project["project"]["dependencies"] == [
        "certifi>=2024.7.4",
        "cryptography>=42,<51",
        "pypdf[crypto]>=5.0,<7",
        "pypdfium2>=5,<6",
        "Pillow>=10.4,<14",
        "comtypes>=1.4,<2; sys_platform == 'win32'",
    ]

    assert "class MacOSKeychainBackend" in credentials
    assert '_MACOS_SECURITY_EXECUTABLE = "/usr/bin/security"' in credentials
    assert '"-q", "-i"' in credentials
    assert "-X {payload.hex()}" in credentials
    assert "find-generic-password" in credentials
    assert "delete-generic-password" in credentials
    assert '"macos"' in config

    for text in (phase, readme, tools_doc, providers):
        assert "macOS Keychain" in text
        assert "macos-keychain" in text
    assert "never puts a\nsecret in argv" in phase
    assert "does not provide a live macOS Keychain transcript" in phase
    assert "macOS desktop\nautomation parity remains future work" in phase
    assert "## 0.47.0 - PHASE 45 macOS Keychain Credentials" in changelog.splitlines()
