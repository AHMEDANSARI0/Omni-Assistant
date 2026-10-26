from __future__ import annotations

from pathlib import Path

import pytest

from pc_agent.tools import ToolContext, build_default_registry


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture()
def context(workspace: Path) -> ToolContext:
    return ToolContext.for_workspace(workspace)


@pytest.fixture()
def registry():
    return build_default_registry()


def test_write_read_and_list_round_trip(workspace, context, registry) -> None:
    written = registry.invoke(
        "workspace.write_text",
        {"path": "notes/hello.txt", "content": "Assalam-o-Alaikum"},
        context,
    )
    assert written.success, written.error
    assert written.output is not None and written.output["path"] == "notes/hello.txt"

    read = registry.invoke("workspace.read_text", {"path": "notes/hello.txt"}, context)
    assert read.success, read.error
    assert read.output is not None and read.output["content"] == "Assalam-o-Alaikum"

    listed = registry.invoke("workspace.list_files", {"directory": "notes"}, context)
    assert listed.success, listed.error
    assert listed.output is not None and listed.output["files"] == ["notes/hello.txt"]


def test_write_never_overwrites(workspace, context, registry) -> None:
    (workspace / "existing.txt").write_text("original", encoding="utf-8")

    result = registry.invoke(
        "workspace.write_text",
        {"path": "existing.txt", "content": "replacement"},
        context,
    )

    assert not result.success
    assert (workspace / "existing.txt").read_text(encoding="utf-8") == "original"


def test_workspace_boundary_and_sensitive_files_are_enforced(workspace, context, registry) -> None:
    (workspace / ".env").write_text("SECRET=value", encoding="utf-8")

    traversal = registry.invoke(
        "workspace.read_text", {"path": "../outside.txt"}, context
    )
    sensitive = registry.invoke("workspace.read_text", {"path": ".env"}, context)

    assert not traversal.success
    assert not sensitive.success


def test_list_rejects_pattern_with_path_separator(workspace, context, registry) -> None:
    result = registry.invoke(
        "workspace.list_files", {"pattern": "../*.txt"}, context
    )

    assert not result.success
