"""Console-script bootstrap (0.81.2).

The Windows acceptance machine ran the **global** ``pc-agent`` shim and got a raw
``ModuleNotFoundError: No module named 'cryptography'`` with no hint that the
command was coming from the wrong interpreter. These tests pin the replacement:
the entry point either runs the CLI or explains, in one screen, what is missing,
which interpreter is running, and how to fix it.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

from pc_agent import entry

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_main_delegates_to_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class FakeCli:
        @staticmethod
        def main(argv=None) -> int:
            calls.append(list(argv or []))
            return 7

    monkeypatch.setitem(sys.modules, "pc_agent.cli", FakeCli)
    assert entry.main(["doctor"]) == 7
    assert calls == [["doctor"]]


def test_a_missing_package_is_explained_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenCli:
        @staticmethod
        def main(argv=None) -> int:  # pragma: no cover - never reached
            raise AssertionError("the CLI must not be reached")

    monkeypatch.setitem(sys.modules, "pc_agent.cli", BrokenCli)
    message = entry.explain_missing_dependency(
        ModuleNotFoundError("No module named 'cryptography'", name="cryptography"),
        prefix=r"C:\Python311\Scripts\pc-agent.exe",
        project_root=PROJECT_ROOT,
    )
    assert "missing package : cryptography" in message
    assert r"C:\Python311\Scripts\pc-agent.exe" in message
    assert 'pip install -e ".[dev]"' in message


def test_the_message_points_at_the_project_interpreter_when_one_exists(
    tmp_path: Path,
) -> None:
    project = tmp_path / "omni-assistant"
    (project / ".venv" / "Scripts").mkdir(parents=True)
    interpreter = project / ".venv" / "Scripts" / "python.exe"
    interpreter.write_bytes(b"stub")
    assert entry.venv_hint(project) == str(interpreter)

    posix_project = tmp_path / "posix-install"
    (posix_project / ".venv" / "bin").mkdir(parents=True)
    posix_interpreter = posix_project / ".venv" / "bin" / "python"
    posix_interpreter.write_bytes(b"stub")
    assert entry.venv_hint(posix_project) == str(posix_interpreter)

    assert entry.venv_hint(tmp_path / "nothing-here") == ""


def test_missing_package_without_a_name_field_is_still_named() -> None:
    message = entry.explain_missing_dependency(
        ImportError("No module named 'PIL'"), prefix="python.exe"
    )
    assert "missing package : PIL" in message


def test_unknown_import_failure_is_still_actionable() -> None:
    message = entry.explain_missing_dependency(ImportError("boom"), prefix="python.exe")
    assert "missing package : unknown" in message
    assert 'pip install -e ".[dev]"' in message


def test_the_console_script_uses_the_bootstrap() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = project["project"]["scripts"]
    assert scripts["pc-agent"] == "pc_agent.entry:main"
    # The repair bootstrap must stay separate: it exists for a tree whose CLI
    # cannot import at all.
    assert scripts["pc-agent-self-repair"] == "pc_agent.self_repair_cli:main"


def test_the_bootstrap_imports_nothing_heavy() -> None:
    source = (PROJECT_ROOT / "src" / "pc_agent" / "entry.py").read_text(encoding="utf-8")
    top = source.split("def venv_hint")[0]
    for heavy in ("cryptography", "pypdf", "PIL", "import argparse"):
        assert f"import {heavy}" not in top
    assert "from .cli import" in source  # imported inside main(), not at module level
