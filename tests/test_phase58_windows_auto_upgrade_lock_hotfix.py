"""Phase 58: Windows background auto-upgrade lock-release hotfix."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from pc_agent import __version__
from pc_agent import auto_upgrade as auto_upgrade_module
from pc_agent.auto_upgrade import _AutoUpgradeLock
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[1]


class _FakeMsvcrt:
    """Model the current-position byte-range semantics of msvcrt.locking."""

    LK_UNLCK = 0
    LK_NBLCK = 2

    def __init__(self) -> None:
        self.locked: dict[tuple[int, int, int, int], int] = {}
        self.calls: list[tuple[int, int, int]] = []

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        stat = os.fstat(fd)
        offset = os.lseek(fd, 0, os.SEEK_CUR)
        key = (stat.st_dev, stat.st_ino, offset, nbytes)
        self.calls.append((mode, offset, nbytes))
        if mode == self.LK_NBLCK:
            owner = self.locked.get(key)
            if owner is not None and owner != fd:
                raise PermissionError(13, "Permission denied")
            self.locked[key] = fd
            return
        if mode == self.LK_UNLCK:
            if self.locked.get(key) != fd:
                raise PermissionError(13, "Permission denied")
            del self.locked[key]
            return
        raise AssertionError(f"unexpected msvcrt locking mode: {mode}")


def test_phase58_windows_lock_reuses_existing_file_at_byte_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "auto-upgrade.lock"
    lock_path.write_bytes(b"pid=1234; started=2026-01-01T00:00:00Z\n")
    fake_msvcrt = _FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(auto_upgrade_module.sys, "platform", "win32")

    # The reported Windows failures happened on the second run: an existing
    # lock file opened in a+b mode starts at EOF.  Both lock and unlock must
    # therefore be normalized to byte zero on every run.
    for _ in range(2):
        with _AutoUpgradeLock(lock_path) as lock:
            assert lock.acquired is True
        assert fake_msvcrt.locked == {}

    with _AutoUpgradeLock(lock_path) as first:
        assert first.acquired is True
        with _AutoUpgradeLock(lock_path) as second:
            assert second.acquired is False
        assert first.acquired is True
    assert fake_msvcrt.locked == {}
    assert fake_msvcrt.calls
    assert all(offset == 0 and nbytes == 1 for _mode, offset, nbytes in fake_msvcrt.calls)


def test_phase58_scope_inventory_and_documentation() -> None:
    assert __version__ == "0.81.2"
    assert LATEST_SCHEMA_VERSION == 18
    assert len(build_default_registry().list_tools()) == 69

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "docs/phase58.md",
        "tests/test_phase58_windows_auto_upgrade_lock_hotfix.py",
    ):
        assert expected in release_names

    module = (ROOT / "src/pc_agent/auto_upgrade.py").read_text(encoding="utf-8")
    phase = (ROOT / "docs/phase58.md").read_text(encoding="utf-8")
    phase56 = (ROOT / "docs/phase56.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tools = (ROOT / "docs/tools.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    lock_at = module.index("msvcrt.LK_NBLCK")
    seek_at = module.rindex("self._handle.seek(0)", 0, lock_at)
    assert seek_at < lock_at
    assert "self._lock_offset = 0" in module
    assert "self._handle.seek(self._lock_offset or 0)" in module
    for text in (phase, phase56, readme, tools):
        assert "Windows Auto-Upgrade Lock Hotfix" in text
    assert "msvcrt.locking" in phase
    assert "byte 0" in phase
    assert "## 0.60.0 - PHASE 58 Windows Auto-Upgrade Lock Hotfix" in (
        changelog.splitlines()
    )
