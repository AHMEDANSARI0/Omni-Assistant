"""Phase 42: Windows symlink-privilege test compatibility hotfix."""

from __future__ import annotations

from pathlib import Path

from pc_agent import __version__
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[1]


def _function_source(source: str, function_name: str) -> str:
    marker = f"def {function_name}("
    start = source.index(marker)
    next_def = source.find("\ndef ", start + len(marker))
    return source[start:] if next_def == -1 else source[start:next_def]


def test_phase42_symlink_creation_is_privilege_guarded_and_isolated() -> None:
    source = (ROOT / "tests" / "test_phase39_env_cleanup.py").read_text(
        encoding="utf-8"
    )
    combined = _function_source(
        source, "test_phase39_cleanup_fails_closed_without_touching_env"
    )
    dedicated = _function_source(
        source, "test_phase39_cleanup_rejects_symlink_env_file"
    )

    assert "symlink_to" not in combined
    assert "try:" in dedicated
    assert "symlink.symlink_to(env_path)" in dedicated
    assert "except OSError as exc:" in dedicated
    assert "Windows may require Developer Mode/privilege" in dedicated
    assert "Cannot create a symlink on this machine" in dedicated
    assert "plan_env_key_cleanup(symlink, store, environ={})" in dedicated


def test_phase42_scope_inventory_and_counts() -> None:
    assert __version__ == "0.82.17"
    assert len(build_default_registry().list_tools()) == 69
    assert LATEST_SCHEMA_VERSION == 18

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "docs/phase42.md",
        "tests/test_phase39_env_cleanup.py",
        "tests/test_phase42_windows_symlink_hotfix.py",
    ):
        assert expected in release_names

    phase41 = (ROOT / "docs" / "phase41.md").read_text(encoding="utf-8")
    phase42 = (ROOT / "docs" / "phase42.md").read_text(encoding="utf-8")
    assert "version | 0.43.0" in phase41
    assert "version | 0.44.0" in phase42
    assert "WinError 1314" in phase42
    assert "runtime behavior | unchanged" in phase42
