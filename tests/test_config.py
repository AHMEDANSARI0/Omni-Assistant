from __future__ import annotations

from pathlib import Path

import pytest

from pc_agent.config import Settings
from pc_agent.errors import ConfigError


def test_defaults_without_env_file(tmp_path: Path) -> None:
    settings = Settings.from_env(tmp_path, environ={})

    assert settings.environment == "development"
    assert settings.workspace_dir == (tmp_path / "workspace").resolve()
    assert settings.database_path == (tmp_path / "data" / "pc_agent.db").resolve()
    assert settings.provider_order == ()
    assert settings.max_retries == 2


def test_dotenv_is_read_and_real_environment_wins(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "PC_AGENT_LOG_LEVEL=DEBUG\nGEMINI_API_KEY=from-file\n", encoding="utf-8"
    )

    settings = Settings.from_env(tmp_path, environ={"PC_AGENT_LOG_LEVEL": "WARNING"})

    assert settings.log_level == "WARNING"
    assert settings.gemini_api_key == "from-file"


def test_provider_order_is_normalised_and_deduplicated(tmp_path: Path) -> None:
    settings = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_PROVIDER_ORDER": " Gemini ,OpenRouter,Gemini ",
            "GEMINI_API_KEY": "key-1",
            "OPENROUTER_API_KEY": "key-2",
        },
    )

    assert settings.provider_order == ("gemini", "openrouter")
    assert settings.providers_with_credentials == ("gemini", "openrouter")


def test_api_keys_never_appear_in_repr_or_summary(tmp_path: Path) -> None:
    settings = Settings.from_env(
        tmp_path, environ={"GEMINI_API_KEY": "super-secret-value"}
    )

    assert "super-secret-value" not in repr(settings)
    assert "super-secret-value" not in str(settings.public_summary())


def test_relative_paths_are_resolved_from_root(tmp_path: Path) -> None:
    settings = Settings.from_env(
        tmp_path,
        environ={"PC_AGENT_WORKSPACE_DIR": "my-work", "PC_AGENT_DATA_DIR": "my-data"},
    )

    assert settings.workspace_dir == (tmp_path / "my-work").resolve()
    assert settings.database_path == (tmp_path / "my-data" / "pc_agent.db").resolve()


def test_unknown_provider_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(tmp_path, environ={"PC_AGENT_PROVIDER_ORDER": "not-a-provider"})


def test_unbounded_retries_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(tmp_path, environ={"PC_AGENT_MAX_RETRIES": "99"})


def test_invalid_log_level_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(tmp_path, environ={"PC_AGENT_LOG_LEVEL": "LOUD"})
