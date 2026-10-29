"""Verified SQLite database backups and approval-gated restores.

Backups copy the complete application database, so they may contain task
history and user-approved memory. They are therefore private data files:
they stay under ``data/backups`` and are never candidates for a release
bundle. Restore is intentionally separate from backup creation and the CLI
requires explicit confirmation before it can replace the live database.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .errors import PcAgentError

_BACKUP_FORMAT = 1
_MAX_BACKUP_BYTES = 500_000_000
_BACKUP_NAME = re.compile(
    r"pc-agent-db-\d{8}T\d{6}Z-[0-9a-f]{6}-[A-Za-z0-9][A-Za-z0-9_.-]{0,39}\.db"
)
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")


def _now_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_state(path: Path) -> tuple[int, str]:
    if not path.is_file():
        raise PcAgentError(f"Database file was not found: {path}.")
    if path.stat().st_size > _MAX_BACKUP_BYTES:
        raise PcAgentError("Database file exceeds the backup safety limit.")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise PcAgentError("Database file could not be opened as SQLite.") from exc
    try:
        integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
        integrity = str(integrity_row[0]) if integrity_row else "unknown"
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "tasks" not in tables or "schema_migrations" not in tables:
            raise PcAgentError("Database file is missing required application tables.")
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
        return int(row[0]), integrity
    except sqlite3.Error as exc:
        raise PcAgentError("Database file could not be verified as SQLite.") from exc
    finally:
        connection.close()


@dataclass(frozen=True, slots=True)
class DatabaseBackupInfo:
    backup_id: str
    filename: str
    path: Path
    manifest_path: Path
    created_at: str
    label: str
    app_version: str
    schema_version: int
    size: int
    sha256: str

    def summary(self) -> str:
        return (
            f"id={self.backup_id}; schema=v{self.schema_version}; "
            f"bytes={self.size}; sha256={self.sha256[:16]}..."
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "backup_id": self.backup_id,
            "filename": self.filename,
            "path": str(self.path),
            "manifest_path": str(self.manifest_path),
            "created_at": self.created_at,
            "label": self.label,
            "app_version": self.app_version,
            "schema_version": self.schema_version,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class DatabaseRestoreResult:
    restored: DatabaseBackupInfo
    pre_restore_backup_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "restored": self.restored.to_dict(),
            "pre_restore_backup_id": self.pre_restore_backup_id,
        }


class DatabaseMaintenanceService:
    """Create, list, verify and restore local database backups."""

    def __init__(self, database_path: Path, data_dir: Path):
        self.database_path = database_path.expanduser().resolve()
        self.data_dir = data_dir.expanduser().resolve()
        self.backups_dir = self.data_dir / "backups"

    def _ensure_layout(self) -> None:
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def create_backup(self, label: str = "manual") -> DatabaseBackupInfo:
        clean_label = label.strip()
        if _LABEL.fullmatch(clean_label) is None:
            raise PcAgentError(
                "Backup label may contain letters, numbers, dots, dashes and underscores (max 40)."
            )
        self._ensure_layout()
        if not self.database_path.is_file():
            raise PcAgentError("No live database exists yet, so there is nothing to back up.")
        source_schema, integrity = _sqlite_state(self.database_path)
        if integrity != "ok":
            raise PcAgentError(
                f"Refusing to back up a database whose integrity check returned {integrity!r}."
            )

        filename = (
            f"pc-agent-db-{_now_id()}-{secrets.token_hex(3)}-{clean_label}.db"
        )
        destination = self.backups_dir / filename
        manifest_path = destination.with_suffix(".json")
        if destination.exists() or manifest_path.exists():
            raise PcAgentError("A database backup with that generated name already exists.")
        temp = destination.with_name(f".{filename}.tmp-{secrets.token_hex(3)}")
        try:
            source = sqlite3.connect(
                f"file:{self.database_path.as_posix()}?mode=ro", uri=True
            )
            try:
                target = sqlite3.connect(temp)
                try:
                    source.backup(target)
                finally:
                    target.close()
            except sqlite3.Error as exc:
                raise PcAgentError("SQLite backup failed; the live database was not changed.") from exc
            finally:
                source.close()

            schema, temp_integrity = _sqlite_state(temp)
            if temp_integrity != "ok" or schema != source_schema:
                raise PcAgentError("The new database backup did not verify; it was discarded.")
            temp.replace(destination)
            manifest = {
                "format": _BACKUP_FORMAT,
                "app_version": __version__,
                "backup_id": destination.stem,
                "filename": filename,
                "created_at": _now_iso(),
                "label": clean_label,
                "schema_version": schema,
                "size": destination.stat().st_size,
                "sha256": _sha256(destination),
            }
            with manifest_path.open("x", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=2, sort_keys=True)
                handle.write("\n")
        finally:
            if temp.exists():
                temp.unlink()
        return self.verify_backup(destination.stem)

    def list_backups(self) -> tuple[DatabaseBackupInfo, ...]:
        if not self.backups_dir.is_dir():
            return ()
        backups: list[DatabaseBackupInfo] = []
        for manifest_path in self.backups_dir.glob("pc-agent-db-*.json"):
            try:
                backups.append(self._load_manifest(manifest_path))
            except PcAgentError:
                continue
        return tuple(
            sorted(backups, key=lambda item: (item.created_at, item.backup_id), reverse=True)
        )

    def resolve_backup(self, reference: str) -> DatabaseBackupInfo:
        wanted = reference.strip()
        backups = self.list_backups()
        if wanted == "latest":
            if not backups:
                raise PcAgentError("No database backups are available.")
            return backups[0]
        if "/" in wanted or "\\" in wanted or wanted in {"", ".", ".."}:
            raise PcAgentError("Use a backup ID, filename, or 'latest'.")
        for backup in backups:
            if wanted in {backup.backup_id, backup.filename}:
                return backup
        raise PcAgentError(f"Database backup was not found: {wanted}.")

    def verify_backup(self, reference: str) -> DatabaseBackupInfo:
        info = self.resolve_backup(reference)
        if not info.path.is_file():
            raise PcAgentError("The backup database file is missing.")
        if info.path.stat().st_size != info.size:
            raise PcAgentError("The backup database size does not match its manifest.")
        if _sha256(info.path) != info.sha256:
            raise PcAgentError("The backup database hash does not match its manifest.")
        schema, integrity = _sqlite_state(info.path)
        if integrity != "ok":
            raise PcAgentError(
                f"The backup database integrity check returned {integrity!r}."
            )
        if schema != info.schema_version:
            raise PcAgentError("The backup database schema does not match its manifest.")
        return info

    def restore_backup(self, reference: str) -> DatabaseRestoreResult:
        self._ensure_layout()
        verified = self.verify_backup(reference)
        pre_restore_id = ""
        if self.database_path.is_file():
            pre_restore_id = self.create_backup("pre-restore").backup_id

        temp = self.database_path.with_name(
            f".{self.database_path.name}.restore-{secrets.token_hex(3)}"
        )
        replaced = False
        try:
            shutil.copyfile(verified.path, temp)
            if _sha256(temp) != verified.sha256:
                raise PcAgentError("The staged database restore copy did not verify.")
            schema, integrity = _sqlite_state(temp)
            if integrity != "ok" or schema != verified.schema_version:
                raise PcAgentError("The staged database restore copy did not verify.")
            self._checkpoint_live_database()
            for suffix in ("-wal", "-shm"):
                sidecar = self.database_path.with_name(
                    self.database_path.name + suffix
                )
                if sidecar.exists():
                    sidecar.unlink()
            temp.replace(self.database_path)
            replaced = True
            schema, integrity = _sqlite_state(self.database_path)
            if integrity != "ok" or schema != verified.schema_version:
                raise PcAgentError("The restored live database did not verify.")
        except Exception:
            if temp.exists():
                temp.unlink()
            if pre_restore_id and (replaced or not self.database_path.is_file()):
                backup = self.verify_backup(pre_restore_id)
                rollback = self.database_path.with_name(
                    f".{self.database_path.name}.rollback-{secrets.token_hex(3)}"
                )
                try:
                    shutil.copyfile(backup.path, rollback)
                    rollback.replace(self.database_path)
                finally:
                    if rollback.exists():
                        rollback.unlink()
            raise
        return DatabaseRestoreResult(verified, pre_restore_id)

    def _checkpoint_live_database(self) -> None:
        if not self.database_path.is_file():
            return
        try:
            connection = sqlite3.connect(self.database_path)
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.commit()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise PcAgentError(
                "The live database could not be checkpointed before restore."
            ) from exc

    def _load_manifest(self, manifest_path: Path) -> DatabaseBackupInfo:
        try:
            parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PcAgentError("Database backup manifest is unreadable.") from exc
        if not isinstance(parsed, dict) or parsed.get("format") != _BACKUP_FORMAT:
            raise PcAgentError("Database backup manifest has an unsupported format.")
        filename = parsed.get("filename")
        backup_id = parsed.get("backup_id")
        created_at = parsed.get("created_at")
        label = parsed.get("label")
        app_version = parsed.get("app_version")
        schema_version = parsed.get("schema_version")
        size = parsed.get("size")
        digest = parsed.get("sha256")
        if (
            not isinstance(filename, str)
            or _BACKUP_NAME.fullmatch(filename) is None
            or backup_id != Path(filename).stem
            or manifest_path.name != f"{backup_id}.json"
            or not isinstance(created_at, str)
            or not created_at
            or not isinstance(label, str)
            or _LABEL.fullmatch(label) is None
            or not isinstance(app_version, str)
            or not app_version
            or isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version < 0
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > _MAX_BACKUP_BYTES
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise PcAgentError("Database backup manifest is invalid.")
        return DatabaseBackupInfo(
            backup_id=backup_id,
            filename=filename,
            path=self.backups_dir / filename,
            manifest_path=manifest_path,
            created_at=created_at,
            label=label,
            app_version=app_version,
            schema_version=schema_version,
            size=size,
            sha256=digest,
        )
