"""Phase 43: Windows-host POSIX package test compatibility hotfix."""

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


def test_phase43_windows_host_guards_are_narrowly_scoped() -> None:
    source = (ROOT / "tests" / "test_phase41_posix_package.py").read_text(
        encoding="utf-8"
    )
    deterministic = _function_source(
        source, "test_phase41_builds_deterministic_executable_posix_package"
    )
    shell_execution = _function_source(
        source, "test_phase41_shell_install_and_uninstall_preserve_user_files"
    )

    assert 'sh = shutil.which("sh") if os.name == "posix" else None' in deterministic
    assert 'if os.name == "posix":' in deterministic
    assert "stat.S_IMODE(path.stat().st_mode) & stat.S_IXUSR" in deterministic
    assert "\n        assert stat.S_IMODE" not in deterministic
    assert "if sh is not None:" in deterministic
    assert '[sh, "-n", str(path)]' in deterministic

    posix_guard = shell_execution.index('if os.name != "posix":')
    sh_guard = shell_execution.index('if shutil.which("sh") is None:')
    package_build = shell_execution.index("package = _built(tmp_path)")
    assert posix_guard < package_build
    assert sh_guard < package_build
    assert "POSIX shell execution requires a POSIX host" in shell_execution
    assert "POSIX shell execution requires sh" in shell_execution


def test_phase43_scope_inventory_and_counts() -> None:
    assert __version__ == "0.55.0"
    assert len(build_default_registry().list_tools()) == 62
    assert LATEST_SCHEMA_VERSION == 6

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "docs/phase43.md",
        "tests/test_phase41_posix_package.py",
        "tests/test_phase43_windows_posix_test_hotfix.py",
    ):
        assert expected in release_names

    phase42 = (ROOT / "docs" / "phase42.md").read_text(encoding="utf-8")
    phase43 = (ROOT / "docs" / "phase43.md").read_text(encoding="utf-8")
    assert "version | 0.44.0" in phase42
    assert "version | 0.45.0" in phase43
    assert "WinError 2" in phase43
    assert "runtime behavior | unchanged" in phase43
