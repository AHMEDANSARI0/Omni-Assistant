"""Phase 52: Windows repository timestamp compatibility hotfix."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from pc_agent import __version__
from pc_agent.debian_package import build_debian_package
from pc_agent.debian_repository import (
    _set_epoch_mtime,
    build_debian_repository,
    verify_debian_repository,
)
from pc_agent.errors import PcAgentError
from pc_agent.release import release_candidate_files
from pc_agent.rpm_package import build_rpm_package
from pc_agent.rpm_repository import (
    _set_epoch_mtime as rpm_set_epoch_mtime,
)
from pc_agent.rpm_repository import (
    build_rpm_repository,
    verify_rpm_repository,
)
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[1]


def test_phase52_epoch_mtime_works_without_follow_symlinks_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert rpm_set_epoch_mtime is _set_epoch_mtime

    target = tmp_path / "file.txt"
    directory = tmp_path / "folder"
    target.write_text("phase52\n", encoding="utf-8")
    directory.mkdir()

    # Windows CPython does not include os.utime in this capability set, and
    # passing follow_symlinks=False there raises NotImplementedError.
    monkeypatch.setattr(os, "supports_follow_symlinks", frozenset())
    _set_epoch_mtime(target)
    _set_epoch_mtime(directory)
    assert target.stat().st_mtime == 0
    assert directory.stat().st_mtime == 0

    os.utime(target, (123, 123))
    link = tmp_path / "linked.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Cannot create a symlink on this machine")
    with pytest.raises(PcAgentError, match="symbolic link"):
        _set_epoch_mtime(link)
    assert target.stat().st_mtime == 123


def test_phase52_repository_builders_use_portable_timestamp_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    debian_package = tmp_path / "package.deb"
    rpm_package = tmp_path / "package.rpm"
    build_debian_package(ROOT, debian_package)
    build_rpm_package(ROOT, rpm_package)

    monkeypatch.setattr(os, "supports_follow_symlinks", frozenset())
    debian_repository = tmp_path / "apt-repository"
    rpm_repository = tmp_path / "rpm-repository"
    debian_info = build_debian_repository(ROOT, debian_package, debian_repository)
    rpm_info = build_rpm_repository(ROOT, rpm_package, rpm_repository)

    assert verify_debian_repository(debian_repository).manifest_sha256 == (
        debian_info.manifest_sha256
    )
    assert verify_rpm_repository(rpm_repository).manifest_sha256 == (
        rpm_info.manifest_sha256
    )
    assert debian_repository.stat().st_mtime == 0
    assert rpm_repository.stat().st_mtime == 0


def test_phase52_scope_inventory_and_documentation() -> None:
    assert __version__ == "0.82.12"
    assert LATEST_SCHEMA_VERSION == 18
    assert len(build_default_registry().list_tools()) == 69

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == __version__
    assert project["project"]["dependencies"] == [
        "certifi>=2024.7.4",
        "cryptography>=42,<51",
        "pypdf[crypto]>=5.0,<7",
        "pypdfium2>=5,<6",
        "Pillow>=10.4,<14",
        "comtypes>=1.4,<2; sys_platform == 'win32'",
    ]

    debian_module = (ROOT / "src/pc_agent/debian_repository.py").read_text(
        encoding="utf-8"
    )
    rpm_module = (ROOT / "src/pc_agent/rpm_repository.py").read_text(
        encoding="utf-8"
    )
    phase = (ROOT / "docs/phase52.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tools_doc = (ROOT / "docs/tools.md").read_text(encoding="utf-8")
    phase50 = (ROOT / "docs/phase50.md").read_text(encoding="utf-8")
    phase51 = (ROOT / "docs/phase51.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert debian_module.count("follow_symlinks=False") == 1
    assert "if os.utime in os.supports_follow_symlinks" in debian_module
    assert "_set_epoch_mtime" in debian_module
    assert "follow_symlinks=False" not in rpm_module
    assert "_set_epoch_mtime" in rpm_module

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "docs/phase52.md",
        "tests/test_phase52_windows_repository_timestamp_hotfix.py",
    ):
        assert expected in release_names

    for text in (phase, readme, tools_doc, phase50, phase51):
        assert "Windows Repository Timestamp Hotfix" in text
    assert "NotImplementedError" in phase
    assert "follow_symlinks unavailable" in phase
    assert "No repository format" in phase
    assert "## 0.54.0 - PHASE 52 Windows Repository Timestamp Hotfix" in changelog
