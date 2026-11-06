"""Cloud-drive connector over a plain folder (Transformation Phase 13).

No OAuth, no vendor SDK, no credential to leak: the "drive" is a directory that
the user already syncs with OneDrive / Google Drive / iCloud / a NAS. This
connector indexes it with a content hash so the agent can tell what is new,
what changed and what disappeared, and it refuses anything that escapes the
configured root.

Hard rules:

* the root must exist and be a directory;
* symlinks and junctions that resolve outside the root are skipped, never
  followed;
* well-known noise/secret directories are skipped by name;
* files above the size limit are listed as skipped, not hashed;
* the number of files is bounded, and the report says when it was truncated.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ConnectorError

__all__ = ["DriveConnector", "DriveEntry", "SyncReport"]

MAX_FILES = 5_000
MAX_HASH_BYTES = 32 * 1024 * 1024
SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        "$RECYCLE.BIN",
        "System Volume Information",
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".password-store",
    }
)
SKIP_SUFFIXES = frozenset(
    {
        ".tmp",
        ".temp",
        ".part",
        ".crdownload",
        ".lock",
        ".pem",
        ".key",
        ".pfx",
        ".p12",
        ".kdbx",
        ".env",
    }
)


@dataclass(frozen=True, slots=True)
class DriveEntry:
    relative_path: str
    size: int
    modified: str
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "size": self.size,
            "modified": self.modified,
            "sha256": self.sha256,
        }


@dataclass(slots=True)
class SyncReport:
    root_name: str
    indexed: list[DriveEntry] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0
    skipped: list[str] = field(default_factory=list)
    truncated: bool = False
    scanned_at: str = ""

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.indexed)

    def manifest(self) -> dict[str, str]:
        return {entry.relative_path: entry.sha256 for entry in self.indexed}

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root_name,
            "scanned_at": self.scanned_at,
            "files": len(self.indexed),
            "total_bytes": self.total_bytes,
            "added": len(self.added),
            "modified": len(self.modified),
            "removed": len(self.removed),
            "unchanged": self.unchanged,
            "skipped": len(self.skipped),
            "truncated": self.truncated,
            "changes": {
                "added": self.added[:50],
                "modified": self.modified[:50],
                "removed": self.removed[:50],
            },
            "skipped_sample": self.skipped[:20],
        }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DriveConnector:
    """Content-hashed index of one local folder."""

    def __init__(
        self,
        root: Path | str,
        *,
        max_files: int = 1_000,
        max_hash_bytes: int = MAX_HASH_BYTES,
        follow_symlinks: bool = False,
    ) -> None:
        self._root = Path(root).expanduser()
        self._max_files = max(1, min(int(max_files), MAX_FILES))
        self._max_hash_bytes = max(1, int(max_hash_bytes))
        self._follow_symlinks = bool(follow_symlinks)

    @property
    def root(self) -> Path:
        return self._root

    def _resolved_root(self) -> Path:
        try:
            resolved = self._root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ConnectorError(
                f"The drive root is not available: {self._root.name or 'unset'} ({exc.__class__.__name__})."
            ) from exc
        if not resolved.is_dir():
            raise ConnectorError("The drive root is not a directory.")
        return resolved

    def status(self) -> dict[str, Any]:
        exists = False
        try:
            exists = self._root.is_dir()
        except OSError:
            exists = False
        return {
            "connector": "drive",
            "kind": "local_folder",
            "configured": bool(str(self._root)) and str(self._root) not in {"", "."},
            "exists": exists,
            "root_name": self._root.name,
            "max_files": self._max_files,
        }

    def _walk(self, root: Path) -> tuple[list[Path], list[str], bool]:
        found: list[Path] = []
        skipped: list[str] = []
        truncated = False
        for current, dirnames, filenames in os.walk(root, followlinks=self._follow_symlinks):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in SKIP_DIRECTORIES and not name.startswith(".")
            )
            current_path = Path(current)
            for name in sorted(filenames):
                if len(found) >= self._max_files:
                    truncated = True
                    break
                path = current_path / name
                relative = path.relative_to(root).as_posix()
                if path.suffix.lower() in SKIP_SUFFIXES or name.startswith("~$"):
                    skipped.append(relative)
                    continue
                if path.is_symlink() and not self._follow_symlinks:
                    skipped.append(relative)
                    continue
                try:
                    resolved = path.resolve(strict=True)
                except (OSError, RuntimeError):
                    skipped.append(relative)
                    continue
                try:
                    resolved.relative_to(root)
                except ValueError:
                    skipped.append(relative)
                    continue
                found.append(path)
            if truncated:
                break
        return found, skipped, truncated

    def scan(
        self,
        *,
        previous: dict[str, str] | None = None,
        hash_files: bool = True,
    ) -> SyncReport:
        root = self._resolved_root()
        from .base import utc_stamp

        report = SyncReport(root_name=root.name, scanned_at=utc_stamp())
        previous = previous or {}
        paths, skipped, truncated = self._walk(root)
        report.skipped = skipped
        report.truncated = truncated
        seen: set[str] = set()
        for path in paths:
            relative = path.relative_to(root).as_posix()
            try:
                stat = path.stat()
            except OSError:
                report.skipped.append(relative)
                continue
            if stat.st_size > self._max_hash_bytes:
                report.skipped.append(relative)
                continue
            digest = ""
            if hash_files:
                try:
                    digest = _hash_file(path)
                except OSError:
                    report.skipped.append(relative)
                    continue
            entry = DriveEntry(
                relative_path=relative,
                size=int(stat.st_size),
                modified=_iso_mtime(stat.st_mtime),
                sha256=digest,
            )
            report.indexed.append(entry)
            seen.add(relative)
            before = previous.get(relative)
            if before is None:
                report.added.append(relative)
            elif digest and before != digest:
                report.modified.append(relative)
            else:
                report.unchanged += 1
        for relative in sorted(set(previous) - seen):
            report.removed.append(relative)
        report.indexed.sort(key=lambda item: item.relative_path)
        return report


def _iso_mtime(value: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(value, UTC).replace(microsecond=0).isoformat()
