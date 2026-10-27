from __future__ import annotations

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


def test_tools_list_includes_pdf_tools(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["tools", "list"]) == 0

    out = capsys.readouterr().out
    assert "pdf.merge_folders v1.0.0" in out
    assert "workspace.list_files" in out
    assert "  provenance=builtin" in out
    assert max(len(line) for line in out.splitlines()) <= 90


def test_demo_pdf_then_merge_folders_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--root", str(tmp_path), "demo", "pdf"]) == 0
    demo_out = capsys.readouterr().out
    assert "Expected merged page count: 6" in demo_out

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "pdf",
                "merge-folders",
                "Folder A",
                "Folder B",
                "--output",
                "merged.pdf",
            ]
        )
        == 0
    )
    merge_out = capsys.readouterr().out
    assert "Verified: True" in merge_out
    assert "Pages: 6" in merge_out
    assert "Folder B/broken.pdf" in merge_out
    assert (tmp_path / "workspace" / "merged.pdf").is_file()

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "pdf",
                "verify",
                "merged.pdf",
                "--expected-pages",
                "6",
            ]
        )
        == 0
    )
    assert "Valid: True" in capsys.readouterr().out
