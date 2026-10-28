from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from pc_agent.cli import main
from pc_agent.documents.pdf import write_blank_pdf


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_names = {
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "OLLAMA_API_KEY",
        "ANTHROPIC_API_KEY",
    }
    for name in list(os.environ):
        if name.startswith("PC_AGENT_") or name in secret_names:
            monkeypatch.delenv(name, raising=False)


def test_providers_list_and_ask_use_offline_mock_without_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Rooted at tmp_path so a developer's real .env (with provider keys)
    # in the working directory can never leak into this offline test.
    assert main(["--root", str(tmp_path), "providers", "list"]) == 0
    providers_out = capsys.readouterr().out
    assert "Fallback route: mock" in providers_out
    assert "No provider network call" in providers_out

    assert main(["--root", str(tmp_path), "ask", "Hello"]) == 0
    captured = capsys.readouterr()
    assert "Offline mock response" in captured.out
    assert "mock/offline-mock" in captured.err


def test_run_natural_language_demo_merge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--root", str(tmp_path), "demo", "pdf"]) == 0
    capsys.readouterr()

    result = main(
        [
            "--root",
            str(tmp_path),
            "run",
            "Merge PDFs from Folder A and Folder B",
        ]
    )
    out = capsys.readouterr().out

    assert result == 0
    assert "Status: succeeded" in out
    assert "Output PDF (workspace): merged.pdf" in out
    assert (tmp_path / "workspace" / "merged.pdf").is_file()

    assert main(["--root", str(tmp_path), "task", "list"]) == 0
    assert "succeeded" in capsys.readouterr().out


def test_task_create_then_task_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--root", str(tmp_path), "demo", "pdf"]) == 0
    capsys.readouterr()
    assert main(
        [
            "--root",
            str(tmp_path),
            "task",
            "create",
            "Merge PDFs from Folder A and Folder B into queued.pdf",
        ]
    ) == 0
    created = capsys.readouterr().out
    match = re.search(r"Task recorded: (\S+)", created)
    assert match is not None

    assert main(["--root", str(tmp_path), "task", "run", match.group(1)]) == 0
    out = capsys.readouterr().out
    assert "Status: succeeded" in out
    assert (tmp_path / "workspace" / "queued.pdf").is_file()


def test_run_external_folder_with_explicit_yes_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path / "outside"
    write_blank_pdf(outside / "a.pdf", 1)
    write_blank_pdf(outside / "b.pdf", 2)
    root = tmp_path / "app"
    root.mkdir()

    result = main(
        [
            "--root",
            str(root),
            "run",
            "Merge PDFs",
            "--input-folder",
            str(outside),
            "--output",
            "external.pdf",
            "--yes",
        ]
    )
    out = capsys.readouterr().out

    assert result == 0
    assert "APPROVED by --yes" in out
    assert (root / "workspace" / "external.pdf").is_file()


def test_run_unsupported_returns_needs_input_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = main(["--root", str(tmp_path), "run", "Build a mobile game"])
    out = capsys.readouterr().out

    assert result == 3
    assert "Status: needs_input" in out


def test_run_json_report_is_machine_readable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--root", str(tmp_path), "demo", "pdf"]) == 0
    capsys.readouterr()

    result = main(
        [
            "--root",
            str(tmp_path),
            "run",
            "Merge PDFs from Folder A and Folder B",
            "--output",
            "json-merged.pdf",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["status"] == "succeeded"
    assert payload["tool_result"]["output"]["page_count"] == 6
