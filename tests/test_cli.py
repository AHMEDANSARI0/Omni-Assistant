from __future__ import annotations

import re
from pathlib import Path

import pytest

from pc_agent.cli import main


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PC_AGENT_HOME",
        "PC_AGENT_ENVIRONMENT",
        "PC_AGENT_WORKSPACE_DIR",
        "PC_AGENT_DATA_DIR",
        "PC_AGENT_DATABASE_PATH",
        "PC_AGENT_PROVIDER_ORDER",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_help_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "pc-agent" in capsys.readouterr().out


def test_doctor_initializes_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--root", str(tmp_path), "doctor"]) == 0

    out = capsys.readouterr().out
    assert "Status: OK" in out
    assert "schema v2" in out
    assert "Usable provider route: mock" in out
    assert (tmp_path / "data" / "pc_agent.db").is_file()
    assert (tmp_path / "workspace").is_dir()


def test_task_create_and_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--root", str(tmp_path), "task", "create", "Merge", "the", "PDFs"]) == 0
    created_out = capsys.readouterr().out
    assert "queued" in created_out
    assert "only been queued" in created_out

    assert main(["--root", str(tmp_path), "task", "list"]) == 0
    list_out = capsys.readouterr().out
    assert "Merge the PDFs" in list_out

    task_id = re.search(r"Task recorded: (\S+)", created_out)
    assert task_id is not None
    assert main(["--root", str(tmp_path), "task", "show", task_id.group(1)]) == 0
    assert "Merge the PDFs" in capsys.readouterr().out


def test_show_missing_task_returns_one(tmp_path: Path) -> None:
    assert main(["--root", str(tmp_path), "task", "show", "missing"]) == 1


def test_invalid_config_returns_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PC_AGENT_MAX_RETRIES", "99")

    assert main(["--root", str(tmp_path), "doctor"]) == 2
    assert "Configuration error" in capsys.readouterr().err
