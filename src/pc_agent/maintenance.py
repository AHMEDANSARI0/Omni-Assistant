"""Verified SQLite database backups and approval-gated restores.

Backups copy the complete application database, so they may contain task
history and user-approved memory. They are therefore private data files:
they stay under ``data/backups`` and are never candidates for a release
bundle. Restore is intentionally separate from backup creation and the CLI
requires explicit confirmation before it can replace the live database.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import __version__
from .errors import PcAgentError

_BACKUP_FORMAT = 1
_MAX_BACKUP_BYTES = 500_000_000
_BACKUP_NAME = re.compile(
    r"pc-agent-db-\d{8}T\d{6}Z-[0-9a-f]{6}-[A-Za-z0-9][A-Za-z0-9_.-]{0,39}\.db(?:\.enc)?"
)
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
# Transformation Phase 2 data protection: optional passphrase-encrypted
# backups. The manifest stores only the KDF salt/iterations and hashes of
# the ciphertext and plaintext - never the passphrase or the derived key.
_ENCRYPTION_SCHEME = "pbkdf2-sha256+fernet"
_PBKDF2_ITERATIONS = 480_000
_MIN_PASSPHRASE_LENGTH = 8


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


def _backup_id_from_filename(filename: str) -> str:
    if filename.endswith(".db.enc"):
        return filename[: -len(".db.enc")]
    if filename.endswith(".db"):
        return filename[: -len(".db")]
    return filename


def _derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _encrypt_bytes(plaintext: bytes, passphrase: str) -> tuple[bytes, str, int]:
    salt = secrets.token_bytes(16)
    token = Fernet(_derive_key(passphrase, salt, _PBKDF2_ITERATIONS)).encrypt(plaintext)
    return token, base64.b64encode(salt).decode("ascii"), _PBKDF2_ITERATIONS


def _decrypt_bytes(
    token: bytes, passphrase: str, *, salt_b64: str, iterations: int
) -> bytes:
    try:
        salt = base64.b64decode(salt_b64.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise PcAgentError("The encrypted backup manifest is invalid.") from exc
    try:
        return Fernet(_derive_key(passphrase, salt, iterations)).decrypt(token)
    except InvalidToken as exc:
        raise PcAgentError(
            "The encrypted backup could not be decrypted; the passphrase "
            "is wrong or the backup file is corrupted."
        ) from exc


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
    encrypted: bool = False
    plaintext_size: int | None = None
    plaintext_sha256: str | None = None
    encryption_salt: str | None = None
    encryption_iterations: int | None = None

    def summary(self) -> str:
        return (
            f"id={self.backup_id}; schema=v{self.schema_version}; "
            f"bytes={self.size}; sha256={self.sha256[:16]}...; "
            f"encrypted={'yes' if self.encrypted else 'no'}"
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
            "encrypted": self.encrypted,
            "plaintext_size": self.plaintext_size,
            "plaintext_sha256": self.plaintext_sha256,
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

    def create_backup(
        self, label: str = "manual", *, passphrase: str | None = None
    ) -> DatabaseBackupInfo:
        clean_label = label.strip()
        if _LABEL.fullmatch(clean_label) is None:
            raise PcAgentError(
                "Backup label may contain letters, numbers, dots, dashes and underscores (max 40)."
            )
        if passphrase is not None and len(passphrase) < _MIN_PASSPHRASE_LENGTH:
            raise PcAgentError(
                f"An encryption passphrase must be at least {_MIN_PASSPHRASE_LENGTH} characters."
            )
        self._ensure_layout()
        if not self.database_path.is_file():
            raise PcAgentError("No live database exists yet, so there is nothing to back up.")
        source_schema, integrity = _sqlite_state(self.database_path)
        if integrity != "ok":
            raise PcAgentError(
                f"Refusing to back up a database whose integrity check returned {integrity!r}."
            )

        backup_id = f"pc-agent-db-{_now_id()}-{secrets.token_hex(3)}-{clean_label}"
        filename = backup_id + (".db.enc" if passphrase else ".db")
        destination = self.backups_dir / filename
        manifest_path = self.backups_dir / f"{backup_id}.json"
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
            manifest: dict[str, object] = {
                "format": _BACKUP_FORMAT,
                "app_version": __version__,
                "backup_id": backup_id,
                "filename": filename,
                "created_at": _now_iso(),
                "label": clean_label,
                "schema_version": schema,
            }
            if passphrase is None:
                temp.replace(destination)
            else:
                plaintext = temp.read_bytes()
                token, salt_b64, iterations = _encrypt_bytes(plaintext, passphrase)
                manifest["encryption"] = {
                    "scheme": _ENCRYPTION_SCHEME,
                    "salt": salt_b64,
                    "iterations": iterations,
                    "plaintext_size": len(plaintext),
                    "plaintext_sha256": hashlib.sha256(plaintext).hexdigest(),
                }
                encrypted_temp = destination.with_name(
                    f".{filename}.enc-tmp-{secrets.token_hex(3)}"
                )
                try:
                    encrypted_temp.write_bytes(token)
                    encrypted_temp.replace(destination)
                finally:
                    if encrypted_temp.exists():
                        encrypted_temp.unlink()
            manifest["size"] = destination.stat().st_size
            manifest["sha256"] = _sha256(destination)
            with manifest_path.open("x", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=2, sort_keys=True)
                handle.write("\n")
        finally:
            if temp.exists():
                temp.unlink()
        return self.verify_backup(backup_id, passphrase=passphrase)

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

    def verify_backup(
        self, reference: str, *, passphrase: str | None = None
    ) -> DatabaseBackupInfo:
        info = self.resolve_backup(reference)
        if not info.path.is_file():
            raise PcAgentError("The backup database file is missing.")
        if info.path.stat().st_size != info.size:
            raise PcAgentError("The backup database size does not match its manifest.")
        if _sha256(info.path) != info.sha256:
            raise PcAgentError("The backup database hash does not match its manifest.")
        if not info.encrypted:
            schema, integrity = _sqlite_state(info.path)
            if integrity != "ok":
                raise PcAgentError(
                    f"The backup database integrity check returned {integrity!r}."
                )
            if schema != info.schema_version:
                raise PcAgentError("The backup database schema does not match its manifest.")
            return info
        if passphrase is None:
            raise PcAgentError(
                "The backup is passphrase-encrypted; its stored size and hash "
                "match the manifest, but a passphrase is required to verify "
                "the database contents."
            )
        plaintext = self._decrypt_backup_bytes(info, passphrase)
        if len(plaintext) != info.plaintext_size or (
            hashlib.sha256(plaintext).hexdigest() != info.plaintext_sha256
        ):
            raise PcAgentError("The decrypted backup does not match its manifest.")
        temp = info.path.with_name(f".{info.filename}.verify-{secrets.token_hex(3)}")
        try:
            temp.write_bytes(plaintext)
            schema, integrity = _sqlite_state(temp)
        finally:
            if temp.exists():
                temp.unlink()
        if integrity != "ok":
            raise PcAgentError(
                f"The backup database integrity check returned {integrity!r}."
            )
        if schema != info.schema_version:
            raise PcAgentError("The backup database schema does not match its manifest.")
        return info

    def _decrypt_backup_bytes(
        self, info: DatabaseBackupInfo, passphrase: str
    ) -> bytes:
        if (
            not info.encrypted
            or info.encryption_salt is None
            or info.encryption_iterations is None
        ):
            raise PcAgentError("The backup manifest has no usable encryption block.")
        return _decrypt_bytes(
            info.path.read_bytes(),
            passphrase,
            salt_b64=info.encryption_salt,
            iterations=info.encryption_iterations,
        )

    def restore_backup(
        self, reference: str, *, passphrase: str | None = None
    ) -> DatabaseRestoreResult:
        self._ensure_layout()
        verified = self.verify_backup(reference, passphrase=passphrase)
        pre_restore_id = ""
        if self.database_path.is_file():
            pre_restore_id = self.create_backup("pre-restore").backup_id

        temp = self.database_path.with_name(
            f".{self.database_path.name}.restore-{secrets.token_hex(3)}"
        )
        plain_source: Path | None = None
        replaced = False
        try:
            if verified.encrypted:
                assert passphrase is not None  # enforced by verify_backup above
                plain_source = self.database_path.with_name(
                    f".{self.database_path.name}.decrypted-{secrets.token_hex(3)}"
                )
                plain_source.write_bytes(
                    self._decrypt_backup_bytes(verified, passphrase)
                )
                source_path = plain_source
                expected_sha256 = verified.plaintext_sha256
            else:
                source_path = verified.path
                expected_sha256 = verified.sha256
            shutil.copyfile(source_path, temp)
            if _sha256(temp) != expected_sha256:
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
            if plain_source is not None and plain_source.exists():
                plain_source.unlink()
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
        if plain_source is not None and plain_source.exists():
            plain_source.unlink()
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
            or backup_id != _backup_id_from_filename(filename)
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
        encryption = parsed.get("encryption")
        encrypted = filename.endswith(".db.enc")
        plaintext_size: int | None = None
        plaintext_sha256: str | None = None
        encryption_salt: str | None = None
        encryption_iterations: int | None = None
        if encrypted or encryption is not None:
            if not encrypted or not isinstance(encryption, dict):
                raise PcAgentError("Database backup manifest is invalid.")
            salt = encryption.get("salt")
            iterations = encryption.get("iterations")
            plain_size = encryption.get("plaintext_size")
            plain_digest = encryption.get("plaintext_sha256")
            if (
                encryption.get("scheme") != _ENCRYPTION_SCHEME
                or not isinstance(salt, str)
                or not salt
                or isinstance(iterations, bool)
                or not isinstance(iterations, int)
                or iterations < 100_000
                or iterations > 5_000_000
                or isinstance(plain_size, bool)
                or not isinstance(plain_size, int)
                or plain_size < 0
                or plain_size > _MAX_BACKUP_BYTES
                or not isinstance(plain_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", plain_digest) is None
            ):
                raise PcAgentError("Database backup manifest is invalid.")
            plaintext_size = plain_size
            plaintext_sha256 = plain_digest
            encryption_salt = salt
            encryption_iterations = iterations
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
            encrypted=encrypted,
            plaintext_size=plaintext_size,
            plaintext_sha256=plaintext_sha256,
            encryption_salt=encryption_salt,
            encryption_iterations=encryption_iterations,
        )
