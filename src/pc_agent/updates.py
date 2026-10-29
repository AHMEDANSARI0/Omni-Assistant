"""Signed offline updates from verified portable release bundles.

Phase 21 deliberately keeps updating local and inspectable.  There is no
network download, background updater, silent install or planner tool here:
an operator supplies a Phase 18 portable ZIP, optionally verifies a detached
Ed25519 signature against a configured public key, stages the exact hashed
files under ``data/updates``, and only then explicitly applies them.

Application is an overlay with a complete backup of replaced files.  Files
created by an update are preserved under the update receipt folder during a
rollback instead of being permanently deleted.  Applying an update does not
run ``pip`` or restart the current process; the CLI tells the operator to
reinstall editable metadata and restart after a successful apply.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import shutil
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings
from .errors import PcAgentError
from .licensing.ed25519 import verify as ed25519_verify
from .release import (
    ReleaseBundleInfo,
    ReleaseFileRecord,
    release_candidate_files,
    verify_release_bundle,
)

UPDATE_FORMAT = 1
SIGNATURE_FORMAT = 1
UPDATE_RECEIPT = "receipt.json"
_MAX_SIGNATURE_BYTES = 64_000
_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_UPDATE_ID_RE = re.compile(r"^update-[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_UPDATE_STATUSES = {"staged", "applied", "rolled-back", "failed"}
_SIGNATURE_PAYLOAD_KEYS = {
    "format",
    "app",
    "bundle_file",
    "bundle_sha256",
    "version",
    "key_id",
    "algorithm",
    "signed_at",
}


def _utc_now() -> str:
    return (
        datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str, *, expected_length: int, label: str) -> bytes:
    text = value.strip()
    if not text or re.fullmatch(r"[A-Za-z0-9_-]+", text) is None:
        raise PcAgentError(f"{label} must be unpadded base64url data.")
    padded = text + "=" * (-len(text) % 4)
    try:
        decoded = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True
        )
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise PcAgentError(f"{label} must be unpadded base64url data.") from exc
    if len(decoded) != expected_length:
        raise PcAgentError(
            f"{label} must decode to exactly {expected_length} bytes."
        )
    return decoded


def validate_key_id(value: str, *, label: str = "Release key ID") -> str:
    key_id = value.strip()
    if _KEY_ID_RE.fullmatch(key_id) is None:
        raise PcAgentError(
            f"{label} must be 1-64 letters, digits, '.', '_' or '-'."
        )
    return key_id


def parse_version(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(value.strip())
    if match is None:
        raise PcAgentError(
            f"Unsupported release version {value!r}; expected MAJOR.MINOR.PATCH."
        )
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def compare_versions(left: str, right: str) -> int:
    a = parse_version(left)
    b = parse_version(right)
    return (a > b) - (a < b)


@dataclass(frozen=True, slots=True)
class ReleaseSignatureInfo:
    status: str  # verified | unsigned | unverified | invalid | key-mismatch
    detail: str
    path: Path | None = None
    key_id: str | None = None
    bundle_sha256: str | None = None
    version: str | None = None

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    def summary(self) -> str:
        key = f"; key_id={self.key_id}" if self.key_id else ""
        return f"{self.status}{key}; {self.detail}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "path": str(self.path) if self.path else None,
            "key_id": self.key_id,
            "bundle_sha256": self.bundle_sha256,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class UpdateCheck:
    current_version: str
    bundle: ReleaseBundleInfo
    signature: ReleaseSignatureInfo
    comparison: int

    @property
    def update_available(self) -> bool:
        return self.comparison > 0

    def summary(self) -> str:
        state = "update available" if self.update_available else "not newer"
        return (
            f"current={self.current_version}; bundle={self.bundle.version}; "
            f"{state}; signature={self.signature.status}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_version": self.current_version,
            "bundle_version": self.bundle.version,
            "update_available": self.update_available,
            "comparison": self.comparison,
            "bundle": {
                "path": str(self.bundle.path),
                "version": self.bundle.version,
                "file_count": self.bundle.file_count,
                "total_bytes": self.bundle.total_bytes,
                "bundle_sha256": self.bundle.bundle_sha256,
            },
            "signature": self.signature.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class UpdateFileRecord:
    path: str
    size: int
    sha256: str
    existed_before: bool | None = None
    previous_sha256: str | None = None

    @classmethod
    def from_release_record(cls, record: ReleaseFileRecord) -> UpdateFileRecord:
        return cls(record.path, record.size, record.sha256)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "existed_before": self.existed_before,
            "previous_sha256": self.previous_sha256,
        }


@dataclass(frozen=True, slots=True)
class StagedUpdate:
    update_id: str
    status: str
    update_dir: Path
    staging_dir: Path
    backup_dir: Path
    receipt_path: Path
    created_at: str
    current_version: str
    bundle_version: str
    bundle_path: Path
    bundle_sha256: str
    signature_status: str
    signature_key_id: str | None
    files: tuple[UpdateFileRecord, ...]
    total_bytes: int
    target_root: Path | None = None
    applied_at: str | None = None
    rolled_back_at: str | None = None
    stale_files: tuple[str, ...] = ()
    last_error: str | None = None

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def created_count(self) -> int:
        return sum(1 for record in self.files if record.existed_before is False)

    @property
    def replaced_count(self) -> int:
        return sum(1 for record in self.files if record.existed_before is True)

    def summary(self) -> str:
        return (
            f"{self.update_id} [{self.status}]; "
            f"{self.current_version} -> {self.bundle_version}; "
            f"files={self.file_count}; signature={self.signature_status}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "update_id": self.update_id,
            "status": self.status,
            "update_dir": str(self.update_dir),
            "staging_dir": str(self.staging_dir),
            "backup_dir": str(self.backup_dir),
            "receipt_path": str(self.receipt_path),
            "created_at": self.created_at,
            "current_version": self.current_version,
            "bundle_version": self.bundle_version,
            "bundle_path": str(self.bundle_path),
            "bundle_sha256": self.bundle_sha256,
            "signature_status": self.signature_status,
            "signature_key_id": self.signature_key_id,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "created_count": self.created_count,
            "replaced_count": self.replaced_count,
            "target_root": str(self.target_root) if self.target_root else None,
            "applied_at": self.applied_at,
            "rolled_back_at": self.rolled_back_at,
            "stale_files": list(self.stale_files),
            "last_error": self.last_error,
            "files": [record.to_dict() for record in self.files],
        }


def default_signature_path(bundle_path: Path) -> Path:
    bundle = bundle_path.expanduser()
    return Path(f"{bundle}.sig.json")


def build_signature_payload(
    bundle: ReleaseBundleInfo,
    *,
    key_id: str,
    signed_at: str | None = None,
) -> dict[str, Any]:
    parse_version(bundle.version)
    return {
        "format": SIGNATURE_FORMAT,
        "app": "Self-Healing PC Agent",
        "bundle_file": bundle.path.name,
        "bundle_sha256": bundle.bundle_sha256,
        "version": bundle.version,
        "key_id": validate_key_id(key_id),
        "algorithm": "Ed25519",
        "signed_at": signed_at or _utc_now(),
    }


def signature_payload_bytes(payload: dict[str, Any]) -> bytes:
    signed = {key: payload[key] for key in sorted(_SIGNATURE_PAYLOAD_KEYS)}
    return json.dumps(
        signed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _signed_at_is_valid(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 40:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def inspect_release_signature(
    bundle: ReleaseBundleInfo,
    settings: Settings,
    signature_path: Path | None = None,
) -> ReleaseSignatureInfo:
    """Inspect a detached signature without trusting any key inside it."""

    explicit = signature_path is not None
    path = (
        signature_path.expanduser().resolve()
        if signature_path is not None
        else default_signature_path(bundle.path).resolve()
    )
    if not path.is_file():
        if explicit:
            raise PcAgentError(f"Release signature was not found: {path}.")
        return ReleaseSignatureInfo(
            status="unsigned",
            detail="No detached .sig.json file was found beside the bundle.",
            bundle_sha256=bundle.bundle_sha256,
            version=bundle.version,
        )
    if path.is_symlink():
        return ReleaseSignatureInfo(
            status="invalid",
            detail="Release signature path is a symbolic link.",
            path=path,
            bundle_sha256=bundle.bundle_sha256,
            version=bundle.version,
        )
    if path.stat().st_size > _MAX_SIGNATURE_BYTES:
        return ReleaseSignatureInfo(
            status="invalid",
            detail="Release signature file is too large.",
            path=path,
            bundle_sha256=bundle.bundle_sha256,
            version=bundle.version,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ReleaseSignatureInfo(
            status="invalid",
            detail="Release signature file is not readable JSON.",
            path=path,
            bundle_sha256=bundle.bundle_sha256,
            version=bundle.version,
        )
    if not isinstance(payload, dict):
        return ReleaseSignatureInfo(
            status="invalid",
            detail="Release signature file must contain one JSON object.",
            path=path,
            bundle_sha256=bundle.bundle_sha256,
            version=bundle.version,
        )

    key_id_raw = payload.get("key_id")
    key_id = key_id_raw if isinstance(key_id_raw, str) else None
    expected_keys = _SIGNATURE_PAYLOAD_KEYS | {"signature"}
    valid_shape = (
        set(payload) == expected_keys
        and type(payload.get("format")) is int
        and payload.get("format") == SIGNATURE_FORMAT
        and payload.get("app") == "Self-Healing PC Agent"
        and payload.get("algorithm") == "Ed25519"
        and payload.get("bundle_file") == bundle.path.name
        and payload.get("bundle_sha256") == bundle.bundle_sha256
        and payload.get("version") == bundle.version
        and isinstance(key_id_raw, str)
        and _KEY_ID_RE.fullmatch(key_id_raw) is not None
        and _signed_at_is_valid(payload.get("signed_at"))
        and isinstance(payload.get("signature"), str)
    )
    if not valid_shape:
        return ReleaseSignatureInfo(
            status="invalid",
            detail="Release signature does not match this exact bundle or format.",
            path=path,
            key_id=key_id,
            bundle_sha256=bundle.bundle_sha256,
            version=bundle.version,
        )

    configured_key_id = getattr(settings, "release_key_id", None)
    if configured_key_id and key_id != configured_key_id:
        return ReleaseSignatureInfo(
            status="key-mismatch",
            detail=(
                f"Signature key_id={key_id} does not match configured "
                f"key_id={configured_key_id}."
            ),
            path=path,
            key_id=key_id,
            bundle_sha256=bundle.bundle_sha256,
            version=bundle.version,
        )

    try:
        signature = b64url_decode(
            str(payload["signature"]),
            expected_length=64,
            label="Release signature",
        )
    except PcAgentError:
        return ReleaseSignatureInfo(
            status="invalid",
            detail="Release signature value is not a 64-byte Ed25519 signature.",
            path=path,
            key_id=key_id,
            bundle_sha256=bundle.bundle_sha256,
            version=bundle.version,
        )

    public_key_value = getattr(settings, "release_public_key", None)
    if not public_key_value:
        return ReleaseSignatureInfo(
            status="unverified",
            detail=(
                "A signature is present, but PC_AGENT_RELEASE_PUBLIC_KEY is "
                "not configured, so it cannot be trusted yet."
            ),
            path=path,
            key_id=key_id,
            bundle_sha256=bundle.bundle_sha256,
            version=bundle.version,
        )
    try:
        public_key = b64url_decode(
            str(public_key_value),
            expected_length=32,
            label="PC_AGENT_RELEASE_PUBLIC_KEY",
        )
    except PcAgentError:
        return ReleaseSignatureInfo(
            status="invalid",
            detail="Configured PC_AGENT_RELEASE_PUBLIC_KEY is invalid.",
            path=path,
            key_id=key_id,
            bundle_sha256=bundle.bundle_sha256,
            version=bundle.version,
        )
    verified = ed25519_verify(
        public_key, signature_payload_bytes(payload), signature
    )
    return ReleaseSignatureInfo(
        status="verified" if verified else "invalid",
        detail=(
            "Ed25519 signature verified against the configured public key."
            if verified
            else "Ed25519 signature verification failed."
        ),
        path=path,
        key_id=key_id,
        bundle_sha256=bundle.bundle_sha256,
        version=bundle.version,
    )


def check_update(
    bundle_path: Path,
    settings: Settings,
    *,
    signature_path: Path | None = None,
) -> UpdateCheck:
    bundle = verify_release_bundle(bundle_path.expanduser())
    comparison = compare_versions(bundle.version, __version__)
    signature = inspect_release_signature(bundle, settings, signature_path)
    return UpdateCheck(
        current_version=__version__,
        bundle=bundle,
        signature=signature,
        comparison=comparison,
    )


def _enforce_signature_policy(
    signature: ReleaseSignatureInfo,
    settings: Settings,
    *,
    allow_unsigned: bool,
) -> None:
    if signature.status == "verified":
        return
    if signature.status in {"invalid", "key-mismatch"}:
        raise PcAgentError(
            f"Release signature rejected ({signature.status}): {signature.detail}"
        )
    if bool(getattr(settings, "require_signed_updates", False)):
        raise PcAgentError(
            "Signed updates are required by PC_AGENT_REQUIRE_SIGNED_UPDATES=1, "
            f"but this bundle is {signature.status}."
        )
    if getattr(settings, "release_public_key", None):
        raise PcAgentError(
            "A release public key is configured, so only a verified signed "
            f"bundle may be staged; this bundle is {signature.status}."
        )
    if not allow_unsigned:
        raise PcAgentError(
            "This bundle has no verified signature. Configure "
            "PC_AGENT_RELEASE_PUBLIC_KEY and use its .sig.json file, or pass "
            "--allow-unsigned only for a bundle you already trust."
        )


def updates_root(settings: Settings) -> Path:
    return settings.data_dir.expanduser().resolve() / "updates"


def _ensure_updates_root(settings: Settings) -> Path:
    root = updates_root(settings)
    if root.is_symlink():
        raise PcAgentError("The updates folder must not be a symbolic link.")
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir() or root.is_symlink():
        raise PcAgentError("The updates folder is not a safe directory.")
    return root


def _safe_join(base: Path, relative_text: str) -> Path:
    relative = Path(relative_text)
    if (
        not relative_text
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in relative_text
    ):
        raise PcAgentError(f"Unsafe update file path: {relative_text!r}.")
    target = (base / relative).resolve()
    root = base.resolve()
    if os.path.commonpath([str(root), str(target)]) != str(root):
        raise PcAgentError(f"Unsafe update file path: {relative_text!r}.")
    return target


def _write_receipt(receipt_path: Path, receipt: dict[str, Any]) -> None:
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = receipt_path.parent / (
        f".{receipt_path.name}.tmp-{secrets.token_hex(4)}"
    )
    temporary.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, receipt_path)


def _receipt_dict(
    *,
    update_id: str,
    status: str,
    created_at: str,
    check: UpdateCheck,
    files: tuple[UpdateFileRecord, ...],
) -> dict[str, Any]:
    return {
        "format": UPDATE_FORMAT,
        "update_id": update_id,
        "status": status,
        "created_at": created_at,
        "applied_at": None,
        "rolled_back_at": None,
        "last_error": None,
        "current_version": check.current_version,
        "bundle_version": check.bundle.version,
        "bundle_path": str(check.bundle.path),
        "bundle_sha256": check.bundle.bundle_sha256,
        "signature_status": check.signature.status,
        "signature_key_id": check.signature.key_id,
        "signature_path": (
            str(check.signature.path) if check.signature.path else None
        ),
        "file_count": len(files),
        "total_bytes": check.bundle.total_bytes,
        "target_root": None,
        "stale_files": [],
        "files": [record.to_dict() for record in files],
    }


def _parse_receipt(update_dir: Path) -> dict[str, Any]:
    receipt_path = update_dir / UPDATE_RECEIPT
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise PcAgentError(f"Update receipt was not found: {receipt_path}.")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PcAgentError("Update receipt is unreadable.") from exc
    if not isinstance(receipt, dict) or receipt.get("format") != UPDATE_FORMAT:
        raise PcAgentError("Update receipt has an unsupported format.")
    update_id = receipt.get("update_id")
    if (
        not isinstance(update_id, str)
        or _UPDATE_ID_RE.fullmatch(update_id) is None
        or update_id != update_dir.name
    ):
        raise PcAgentError("Update receipt ID does not match its folder.")
    if receipt.get("status") not in _UPDATE_STATUSES:
        raise PcAgentError("Update receipt has an invalid status.")
    for field in ("created_at", "current_version", "bundle_version"):
        if not isinstance(receipt.get(field), str) or not receipt[field]:
            raise PcAgentError(f"Update receipt field is invalid: {field}.")
    parse_version(str(receipt["bundle_version"]))
    if not isinstance(receipt.get("bundle_path"), str):
        raise PcAgentError("Update receipt bundle path is invalid.")
    bundle_hash = receipt.get("bundle_sha256")
    if not isinstance(bundle_hash, str) or re.fullmatch(
        r"[0-9a-f]{64}", bundle_hash
    ) is None:
        raise PcAgentError("Update receipt bundle hash is invalid.")
    if receipt.get("signature_status") not in {
        "verified",
        "unsigned",
        "unverified",
        "invalid",
        "key-mismatch",
    }:
        raise PcAgentError("Update receipt signature status is invalid.")
    raw_files = receipt.get("files")
    if not isinstance(raw_files, list) or not raw_files or len(raw_files) > 5_000:
        raise PcAgentError("Update receipt file list is invalid.")
    if receipt.get("file_count") != len(raw_files):
        raise PcAgentError("Update receipt file count is invalid.")
    total = 0
    for item in raw_files:
        if not isinstance(item, dict):
            raise PcAgentError("Update receipt has an invalid file record.")
        path = item.get("path")
        size = item.get("size")
        digest = item.get("sha256")
        existed = item.get("existed_before")
        previous = item.get("previous_sha256")
        if not isinstance(path, str):
            raise PcAgentError("Update receipt has an invalid file record.")
        _safe_join(update_dir, path)
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise PcAgentError("Update receipt has an invalid file record.")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise PcAgentError("Update receipt has an invalid file record.")
        if existed is not None and not isinstance(existed, bool):
            raise PcAgentError("Update receipt has an invalid file record.")
        if previous is not None and (
            not isinstance(previous, str)
            or re.fullmatch(r"[0-9a-f]{64}", previous) is None
        ):
            raise PcAgentError("Update receipt has an invalid file record.")
        if existed is True and previous is None:
            raise PcAgentError("Update receipt is missing a previous file hash.")
        if existed is False and previous is not None:
            raise PcAgentError("Update receipt has an inconsistent previous hash.")
        total += size
    if receipt.get("total_bytes") != total:
        raise PcAgentError("Update receipt total size is invalid.")
    stale = receipt.get("stale_files", [])
    if not isinstance(stale, list) or any(not isinstance(item, str) for item in stale):
        raise PcAgentError("Update receipt stale-file list is invalid.")
    return receipt


def _staged_from_receipt(update_dir: Path, receipt: dict[str, Any]) -> StagedUpdate:
    files = tuple(
        UpdateFileRecord(
            path=str(item["path"]),
            size=int(item["size"]),
            sha256=str(item["sha256"]),
            existed_before=item.get("existed_before"),
            previous_sha256=item.get("previous_sha256"),
        )
        for item in receipt["files"]
    )
    target_root = receipt.get("target_root")
    return StagedUpdate(
        update_id=str(receipt["update_id"]),
        status=str(receipt["status"]),
        update_dir=update_dir,
        staging_dir=update_dir / "staging",
        backup_dir=update_dir / "backup",
        receipt_path=update_dir / UPDATE_RECEIPT,
        created_at=str(receipt["created_at"]),
        current_version=str(receipt["current_version"]),
        bundle_version=str(receipt["bundle_version"]),
        bundle_path=Path(str(receipt["bundle_path"])),
        bundle_sha256=str(receipt["bundle_sha256"]),
        signature_status=str(receipt["signature_status"]),
        signature_key_id=receipt.get("signature_key_id"),
        files=files,
        total_bytes=int(receipt["total_bytes"]),
        target_root=Path(str(target_root)) if target_root else None,
        applied_at=receipt.get("applied_at"),
        rolled_back_at=receipt.get("rolled_back_at"),
        stale_files=tuple(receipt.get("stale_files") or ()),
        last_error=receipt.get("last_error"),
    )


def list_updates(settings: Settings) -> tuple[StagedUpdate, ...]:
    root = updates_root(settings)
    if not root.exists():
        return ()
    if root.is_symlink() or not root.is_dir():
        raise PcAgentError("The updates folder is not a safe directory.")
    updates: list[StagedUpdate] = []
    for update_dir in root.iterdir():
        if not update_dir.is_dir() or update_dir.is_symlink():
            continue
        if _UPDATE_ID_RE.fullmatch(update_dir.name) is None:
            continue
        try:
            updates.append(
                _staged_from_receipt(update_dir, _parse_receipt(update_dir))
            )
        except PcAgentError:
            continue
    updates.sort(key=lambda item: (item.created_at, item.update_id), reverse=True)
    return tuple(updates)


def get_update(
    settings: Settings,
    update_id: str,
    *,
    status: str | None = None,
) -> StagedUpdate:
    requested = update_id.strip()
    aliases = {"latest", "latest-staged", "latest-applied", "latest-failed"}
    if requested in aliases:
        wanted = status
        if requested == "latest-staged":
            wanted = "staged"
        elif requested == "latest-applied":
            wanted = "applied"
        elif requested == "latest-failed":
            wanted = "failed"
        for update in list_updates(settings):
            if wanted is None or update.status == wanted:
                return update
        label = wanted or "recorded"
        raise PcAgentError(f"No {label} update has been recorded.")
    if _UPDATE_ID_RE.fullmatch(requested) is None:
        raise PcAgentError(f"Invalid update ID: {update_id!r}.")
    update_dir = _ensure_updates_root(settings) / requested
    if update_dir.is_symlink() or not update_dir.is_dir():
        raise PcAgentError(f"Update was not found: {requested}.")
    update = _staged_from_receipt(update_dir, _parse_receipt(update_dir))
    if status is not None and update.status != status:
        raise PcAgentError(
            f"Update {requested} is {update.status}, not {status}."
        )
    return update


def resolve_update_id(
    settings: Settings,
    update_id: str,
    *,
    status: str | None = None,
) -> str:
    return get_update(settings, update_id, status=status).update_id


def stage_update(
    bundle_path: Path,
    settings: Settings,
    *,
    signature_path: Path | None = None,
    allow_unsigned: bool = False,
    allow_downgrade: bool = False,
) -> StagedUpdate:
    """Verify and extract a bundle into data/updates without applying it."""

    check = check_update(bundle_path, settings, signature_path=signature_path)
    _enforce_signature_policy(
        check.signature, settings, allow_unsigned=allow_unsigned
    )
    if check.comparison <= 0 and not allow_downgrade:
        relation = "the same as" if check.comparison == 0 else "older than"
        raise PcAgentError(
            f"Bundle version {check.bundle.version} is {relation} the running "
            f"version {check.current_version}. Pass --allow-downgrade only "
            "for an intentional reinstall or downgrade."
        )

    root = _ensure_updates_root(settings)
    created_at = _utc_now()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    update_id = ""
    update_dir: Path | None = None
    for _ in range(20):
        candidate = (
            f"update-{timestamp}-{secrets.token_hex(3)}-v{check.bundle.version}"
        )
        candidate_dir = root / candidate
        try:
            candidate_dir.mkdir()
        except FileExistsError:
            continue
        update_id = candidate
        update_dir = candidate_dir
        break
    if update_dir is None:
        raise PcAgentError("Could not allocate a unique update staging folder.")

    staging_dir = update_dir / "staging"
    staging_dir.mkdir()
    files = tuple(
        UpdateFileRecord.from_release_record(record) for record in check.bundle.files
    )
    try:
        with zipfile.ZipFile(check.bundle.path, "r") as archive:
            for record in files:
                try:
                    data = archive.read(record.path)
                except (KeyError, RuntimeError, zipfile.BadZipFile) as exc:
                    raise PcAgentError(
                        f"Release bundle entry could not be staged: {record.path}."
                    ) from exc
                if len(data) != record.size or hashlib.sha256(data).hexdigest() != record.sha256:
                    raise PcAgentError(
                        f"Staged update file failed verification: {record.path}."
                    )
                target = _safe_join(staging_dir, record.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
    except OSError as exc:
        raise PcAgentError(f"Could not stage the release bundle: {exc}.") from exc

    if check.signature.path is not None:
        shutil.copy2(check.signature.path, update_dir / "signature.json")
    receipt = _receipt_dict(
        update_id=update_id,
        status="staged",
        created_at=created_at,
        check=check,
        files=files,
    )
    _write_receipt(update_dir / UPDATE_RECEIPT, receipt)
    staged = _staged_from_receipt(update_dir, receipt)
    _verify_staged_files(staged)
    return staged


def _verify_staged_files(staged: StagedUpdate) -> None:
    if staged.staging_dir.is_symlink() or not staged.staging_dir.is_dir():
        raise PcAgentError("The staged update folder is missing or unsafe.")
    for record in staged.files:
        source = _safe_join(staged.staging_dir, record.path)
        if source.is_symlink() or not source.is_file():
            raise PcAgentError(f"Staged update file is missing: {record.path}.")
        if source.stat().st_size != record.size or _sha256_file(source) != record.sha256:
            raise PcAgentError(
                f"Staged update file failed verification: {record.path}."
            )


def _is_symlink_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _assert_no_symlink_components(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise PcAgentError("Update target escapes the application root.") from exc
    current = root
    parts = relative.parts
    for index, part in enumerate(parts):
        current /= part
        if not os.path.lexists(current):
            continue
        if _is_symlink_or_junction(current):
            raise PcAgentError(
                f"Update target path contains a link or junction: {relative.as_posix()}."
            )
        if index < len(parts) - 1 and not current.is_dir():
            raise PcAgentError(
                f"Update target parent is not a directory: {relative.as_posix()}."
            )


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / (
        f".{target.name}.pc-agent-update-{secrets.token_hex(4)}.tmp"
    )
    with source.open("rb") as reader, temporary.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=64 * 1024)
        writer.flush()
        try:
            os.fsync(writer.fileno())
        except OSError:
            pass
    if target.suffix.lower() == ".sh" and os.name != "nt":
        temporary.chmod(0o755)
    os.replace(temporary, target)


def _move_preserving(source: Path, destination: Path) -> None:
    if os.path.lexists(destination):
        raise PcAgentError(
            f"Preserved update file destination already exists: {destination}."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))


def _restore_records(
    staged: StagedUpdate,
    records: tuple[UpdateFileRecord, ...],
    *,
    archive_name: str,
) -> list[str]:
    """Best-effort restore used if apply fails after some files changed."""

    assert staged.target_root is not None
    errors: list[str] = []
    for record in reversed(records):
        target = _safe_join(staged.target_root, record.path)
        try:
            if record.existed_before:
                backup = _safe_join(staged.backup_dir, record.path)
                _atomic_copy(backup, target)
                if _sha256_file(target) != record.previous_sha256:
                    errors.append(record.path)
            elif os.path.lexists(target):
                if target.is_file() and _sha256_file(target) == record.sha256:
                    archive = _safe_join(
                        staged.update_dir / archive_name, record.path
                    )
                    _move_preserving(target, archive)
                else:
                    errors.append(record.path)
        except (OSError, PcAgentError):
            errors.append(record.path)
    return errors


def _update_receipt(
    staged: StagedUpdate,
    *,
    status: str,
    files: tuple[UpdateFileRecord, ...] | None = None,
    stale_files: tuple[str, ...] | None = None,
    target_root: Path | None = None,
    applied_at: str | None = None,
    rolled_back_at: str | None = None,
    last_error: str | None = None,
) -> StagedUpdate:
    receipt = _parse_receipt(staged.update_dir)
    receipt["status"] = status
    if files is not None:
        receipt["files"] = [record.to_dict() for record in files]
        receipt["file_count"] = len(files)
        receipt["total_bytes"] = sum(record.size for record in files)
    if stale_files is not None:
        receipt["stale_files"] = list(stale_files)
    if target_root is not None:
        receipt["target_root"] = str(target_root)
    if applied_at is not None:
        receipt["applied_at"] = applied_at
    if rolled_back_at is not None:
        receipt["rolled_back_at"] = rolled_back_at
    receipt["last_error"] = last_error
    _write_receipt(staged.receipt_path, receipt)
    return _staged_from_receipt(staged.update_dir, receipt)


def apply_staged_update(
    settings: Settings,
    update_id: str = "latest-staged",
) -> StagedUpdate:
    """Apply one staged overlay after backing up every replaced file."""

    staged = get_update(settings, update_id, status="staged")
    _verify_staged_files(staged)
    root_input = settings.root_dir.expanduser()
    if _is_symlink_or_junction(root_input) or not root_input.is_dir():
        raise PcAgentError("The application root is missing or unsafe.")
    root = root_input.resolve()
    if staged.staging_dir.resolve() == root:
        raise PcAgentError("The staged update folder cannot be the application root.")
    if staged.backup_dir.exists() and any(staged.backup_dir.rglob("*")):
        raise PcAgentError(
            "This staged update already has backup files; refusing to mix "
            "two apply attempts. Stage the bundle again for a clean receipt."
        )

    prepared: list[UpdateFileRecord] = []
    for record in staged.files:
        target = _safe_join(root, record.path)
        _assert_no_symlink_components(root, target)
        if os.path.lexists(target):
            if not target.is_file():
                raise PcAgentError(
                    f"Update target is not a regular file: {record.path}."
                )
            prepared.append(
                UpdateFileRecord(
                    path=record.path,
                    size=record.size,
                    sha256=record.sha256,
                    existed_before=True,
                    previous_sha256=_sha256_file(target),
                )
            )
        else:
            prepared.append(
                UpdateFileRecord(
                    path=record.path,
                    size=record.size,
                    sha256=record.sha256,
                    existed_before=False,
                    previous_sha256=None,
                )
            )
    prepared_files = tuple(prepared)
    current_release_files = {
        path.as_posix() for path in release_candidate_files(root)
    }
    incoming_files = {record.path for record in prepared_files}
    stale_files = tuple(sorted(current_release_files - incoming_files))

    staged = _update_receipt(
        staged,
        status="staged",
        files=prepared_files,
        stale_files=stale_files,
        target_root=root,
    )

    # Back up and verify everything before the first target is replaced.
    staged.backup_dir.mkdir(parents=True, exist_ok=True)
    for record in prepared_files:
        if not record.existed_before:
            continue
        target = _safe_join(root, record.path)
        backup = _safe_join(staged.backup_dir, record.path)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
        if _sha256_file(backup) != record.previous_sha256:
            raise PcAgentError(
                f"Update backup failed verification: {record.path}."
            )

    completed: list[UpdateFileRecord] = []
    try:
        for record in prepared_files:
            source = _safe_join(staged.staging_dir, record.path)
            target = _safe_join(root, record.path)
            _atomic_copy(source, target)
            completed.append(record)
        for record in prepared_files:
            target = _safe_join(root, record.path)
            if (
                not target.is_file()
                or target.stat().st_size != record.size
                or _sha256_file(target) != record.sha256
            ):
                raise PcAgentError(
                    f"Applied update file failed verification: {record.path}."
                )
    except (OSError, PcAgentError) as exc:
        restore_errors = _restore_records(
            staged,
            tuple(completed),
            archive_name="failed-created",
        )
        message = f"Update apply failed and was rolled back: {exc}"
        if restore_errors:
            message += (
                "; automatic restore could not verify: "
                + ", ".join(restore_errors)
            )
        _update_receipt(staged, status="failed", last_error=message)
        raise PcAgentError(message) from exc

    return _update_receipt(
        staged,
        status="applied",
        applied_at=_utc_now(),
        last_error=None,
    )


def rollback_update(
    settings: Settings,
    update_id: str = "latest-applied",
) -> StagedUpdate:
    """Roll back an applied update without overwriting later user edits."""

    staged = get_update(settings, update_id, status="applied")
    if staged.target_root is None:
        raise PcAgentError("The applied update receipt has no target root.")
    root_input = settings.root_dir.expanduser()
    if _is_symlink_or_junction(root_input) or not root_input.is_dir():
        raise PcAgentError("The application root is missing or unsafe.")
    root = root_input.resolve()
    if staged.target_root.resolve() != root:
        raise PcAgentError(
            "This update was applied to a different application root; "
            "run rollback from that root."
        )
    _verify_staged_files(staged)

    archive_root = staged.update_dir / "rolled-back-created"
    for record in staged.files:
        if record.existed_before is None:
            raise PcAgentError("The applied update receipt is incomplete.")
        target = _safe_join(root, record.path)
        _assert_no_symlink_components(root, target)
        if (
            not target.is_file()
            or target.stat().st_size != record.size
            or _sha256_file(target) != record.sha256
        ):
            raise PcAgentError(
                "Rollback refused because an updated file changed after "
                f"apply: {record.path}."
            )
        if record.existed_before:
            backup = _safe_join(staged.backup_dir, record.path)
            if (
                backup.is_symlink()
                or not backup.is_file()
                or _sha256_file(backup) != record.previous_sha256
            ):
                raise PcAgentError(
                    f"Rollback backup failed verification: {record.path}."
                )
        else:
            archive = _safe_join(archive_root, record.path)
            if os.path.lexists(archive):
                raise PcAgentError(
                    f"Rollback archive already exists for: {record.path}."
                )

    completed: list[UpdateFileRecord] = []
    try:
        for record in staged.files:
            target = _safe_join(root, record.path)
            if record.existed_before:
                backup = _safe_join(staged.backup_dir, record.path)
                _atomic_copy(backup, target)
            else:
                archive = _safe_join(archive_root, record.path)
                _move_preserving(target, archive)
            completed.append(record)
        for record in staged.files:
            target = _safe_join(root, record.path)
            if record.existed_before:
                if _sha256_file(target) != record.previous_sha256:
                    raise PcAgentError(
                        f"Rolled-back file failed verification: {record.path}."
                    )
            else:
                archive = _safe_join(archive_root, record.path)
                if os.path.lexists(target) or _sha256_file(archive) != record.sha256:
                    raise PcAgentError(
                        f"Rolled-back created file was not preserved: {record.path}."
                    )
    except (OSError, PcAgentError) as exc:
        # Best-effort forward restore for files already rolled back.
        for record in reversed(completed):
            target = _safe_join(root, record.path)
            try:
                if record.existed_before:
                    source = _safe_join(staged.staging_dir, record.path)
                    _atomic_copy(source, target)
                else:
                    archive = _safe_join(archive_root, record.path)
                    if archive.is_file() and not os.path.lexists(target):
                        _move_preserving(archive, target)
            except (OSError, PcAgentError):
                pass
        message = f"Update rollback failed: {exc}"
        _update_receipt(staged, status="applied", last_error=message)
        raise PcAgentError(message) from exc

    return _update_receipt(
        staged,
        status="rolled-back",
        rolled_back_at=_utc_now(),
        last_error=None,
    )
