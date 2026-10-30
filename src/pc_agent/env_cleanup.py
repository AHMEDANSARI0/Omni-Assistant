"""Verified removal of duplicated provider API keys from ``.env``.

Phase 39 closes the manual last step of the Phase 16 credential migration.
A provider line is removable only when the exact effective ``.env`` value is
already present in the configured OS credential store.  Planning is
read-only; applying a plan revalidates both the file and credential store,
writes through a same-directory temporary file, and never creates a backup
that would retain the plaintext secrets being removed.
"""

from __future__ import annotations

import codecs
import hashlib
import hmac
import json
import os
import secrets
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .credentials import (
    SUPPORTED_CREDENTIAL_PROVIDERS,
    CredentialStore,
    canonical_credential_provider,
)
from .errors import CredentialError

PROVIDER_API_KEY_ENV_NAMES = {
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-compatible": "OPENAI_COMPATIBLE_API_KEY",
    "ollama": "OLLAMA_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

_MAX_ENV_BYTES = 1_000_000
_MAX_ENV_LINES = 10_000


@dataclass(frozen=True, slots=True)
class _Assignment:
    line_index: int
    line_number: int
    key: str
    value: str


@dataclass(frozen=True, slots=True)
class EnvKeyCleanupPlan:
    """A secret-free cleanup plan; private byte fields are never rendered."""

    path: Path
    backend: str
    selected_providers: tuple[str, ...]
    removable_providers: tuple[str, ...]
    statuses: tuple[tuple[str, str], ...]
    removed_line_numbers: tuple[int, ...]
    duplicate_line_count: int
    original_size: int
    original_sha256: str
    proposed_size: int
    proposed_sha256: str
    process_environment_providers: tuple[str, ...] = ()
    _original_bytes: bytes = field(default=b"", repr=False, compare=False)
    _proposed_bytes: bytes = field(default=b"", repr=False, compare=False)

    @property
    def skipped_providers(self) -> tuple[str, ...]:
        removable = set(self.removable_providers)
        return tuple(
            provider
            for provider in self.selected_providers
            if provider not in removable
        )

    def status_for(self, provider: str) -> str:
        canonical = canonical_credential_provider(provider)
        return dict(self.statuses)[canonical]

    def summary(self) -> str:
        removable = ", ".join(self.removable_providers) or "none"
        skipped = ", ".join(
            f"{provider}={status}"
            for provider, status in self.statuses
            if provider not in self.removable_providers
        ) or "none"
        return (
            f"removable={removable}; skipped={skipped}; "
            f"lines={len(self.removed_line_numbers)}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "backend": self.backend,
            "selected_providers": list(self.selected_providers),
            "removable_providers": list(self.removable_providers),
            "skipped_providers": list(self.skipped_providers),
            "statuses": dict(self.statuses),
            "environment_variables": {
                provider: PROVIDER_API_KEY_ENV_NAMES[provider]
                for provider in self.selected_providers
            },
            "removed_line_numbers": list(self.removed_line_numbers),
            "duplicate_line_count": self.duplicate_line_count,
            "original_size": self.original_size,
            "original_sha256": self.original_sha256,
            "proposed_size": self.proposed_size,
            "proposed_sha256": self.proposed_sha256,
            "process_environment_providers": list(
                self.process_environment_providers
            ),
            "secret_values_included": False,
        }


@dataclass(frozen=True, slots=True)
class EnvKeyCleanupResult:
    path: Path
    backend: str
    removed_providers: tuple[str, ...]
    statuses: tuple[tuple[str, str], ...]
    removed_line_numbers: tuple[int, ...]
    duplicate_line_count: int
    original_sha256: str
    result_sha256: str
    result_size: int
    process_environment_providers: tuple[str, ...] = ()
    receipt_path: Path | None = None
    receipt_error: str | None = None
    verified: bool = True

    def summary(self) -> str:
        removed = ", ".join(self.removed_providers) or "none"
        return (
            f"removed={removed}; lines={len(self.removed_line_numbers)}; "
            f"verified={'yes' if self.verified else 'no'}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "backend": self.backend,
            "removed_providers": list(self.removed_providers),
            "statuses": dict(self.statuses),
            "removed_line_numbers": list(self.removed_line_numbers),
            "duplicate_line_count": self.duplicate_line_count,
            "original_sha256": self.original_sha256,
            "result_sha256": self.result_sha256,
            "result_size": self.result_size,
            "process_environment_providers": list(
                self.process_environment_providers
            ),
            "receipt_path": str(self.receipt_path) if self.receipt_path else None,
            "receipt_error": self.receipt_error,
            "verified": self.verified,
            "secret_values_included": False,
        }


def _selected_providers(
    providers: Sequence[str] | None,
) -> tuple[str, ...]:
    if providers is None:
        return SUPPORTED_CREDENTIAL_PROVIDERS
    requested = {
        canonical_credential_provider(provider) for provider in providers
    }
    if not requested:
        raise CredentialError("Select at least one credential provider.")
    return tuple(
        provider
        for provider in SUPPORTED_CREDENTIAL_PROVIDERS
        if provider in requested
    )


def _read_env_bytes(path: Path) -> bytes:
    candidate = path.expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise CredentialError(f"The .env file was not found: {candidate}.")
    size_before = candidate.stat().st_size
    if size_before > _MAX_ENV_BYTES:
        raise CredentialError("The .env file exceeds the 1000000-byte limit.")
    data = candidate.read_bytes()
    if len(data) != size_before or candidate.stat().st_size != size_before:
        raise CredentialError("The .env file changed while it was being read.")
    return data


def _decode_env(data: bytes) -> tuple[str, bool]:
    has_bom = data.startswith(codecs.BOM_UTF8)
    try:
        text = data.decode("utf-8-sig" if has_bom else "utf-8")
    except UnicodeDecodeError as exc:
        raise CredentialError("The .env file is not valid UTF-8.") from exc
    if text.count("\n") + 1 > _MAX_ENV_LINES:
        raise CredentialError("The .env file contains too many lines.")
    return text, has_bom


def _encode_env(text: str, *, has_bom: bool) -> bytes:
    data = text.encode("utf-8")
    return codecs.BOM_UTF8 + data if has_bom else data


def _parse_assignments(lines: list[str]) -> list[_Assignment]:
    assignments: list[_Assignment] = []
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            raise CredentialError(
                f"Invalid .env entry at line {index + 1} (expected KEY=VALUE)."
            )
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        assignments.append(_Assignment(index, index + 1, key, value))
    return assignments


def _assert_removed(data: bytes, providers: tuple[str, ...]) -> None:
    text, _has_bom = _decode_env(data)
    assignments = _parse_assignments(text.splitlines(True))
    names = {PROVIDER_API_KEY_ENV_NAMES[provider] for provider in providers}
    remaining = [
        assignment.key for assignment in assignments if assignment.key in names
    ]
    if remaining:
        raise CredentialError(
            "The cleaned .env still contains a selected provider API-key line."
        )


def plan_env_key_cleanup(
    env_path: Path,
    store: CredentialStore,
    *,
    providers: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> EnvKeyCleanupPlan:
    """Plan removals without changing either storage location."""

    selected = _selected_providers(providers)
    status = store.status(include_stored=False)
    if not status.available:
        raise CredentialError(
            "Cannot remove .env provider keys because credential storage "
            f"is unavailable: {status.detail}."
        )

    path = env_path.expanduser().resolve()
    original = _read_env_bytes(env_path)
    text, has_bom = _decode_env(original)
    lines = text.splitlines(True)
    assignments = _parse_assignments(lines)

    by_name: dict[str, list[_Assignment]] = {}
    for assignment in assignments:
        by_name.setdefault(assignment.key, []).append(assignment)

    removable: list[str] = []
    statuses: list[tuple[str, str]] = []
    remove_indexes: set[int] = set()
    removed_line_numbers: list[int] = []
    duplicate_line_count = 0

    for provider in selected:
        env_name = PROVIDER_API_KEY_ENV_NAMES[provider]
        occurrences = by_name.get(env_name, [])
        if not occurrences:
            statuses.append((provider, "not_in_dotenv"))
            continue
        effective = occurrences[-1].value
        if not effective:
            statuses.append((provider, "empty_in_dotenv"))
            continue
        stored = store.get_api_key(provider)
        if stored is None:
            statuses.append((provider, "not_stored"))
            continue
        if not hmac.compare_digest(effective, stored):
            statuses.append((provider, "value_mismatch"))
            continue
        statuses.append((provider, "verified_match"))
        removable.append(provider)
        duplicate_line_count += max(0, len(occurrences) - 1)
        for occurrence in occurrences:
            remove_indexes.add(occurrence.line_index)
            removed_line_numbers.append(occurrence.line_number)

    proposed_text = "".join(
        line for index, line in enumerate(lines) if index not in remove_indexes
    )
    proposed = _encode_env(proposed_text, has_bom=has_bom)
    _assert_removed(proposed, tuple(removable))

    process_values = os.environ if environ is None else environ
    process_providers = tuple(
        provider
        for provider in selected
        if bool(process_values.get(PROVIDER_API_KEY_ENV_NAMES[provider]))
    )

    return EnvKeyCleanupPlan(
        path=path,
        backend=status.backend,
        selected_providers=selected,
        removable_providers=tuple(removable),
        statuses=tuple(statuses),
        removed_line_numbers=tuple(sorted(removed_line_numbers)),
        duplicate_line_count=duplicate_line_count,
        original_size=len(original),
        original_sha256=hashlib.sha256(original).hexdigest(),
        proposed_size=len(proposed),
        proposed_sha256=hashlib.sha256(proposed).hexdigest(),
        process_environment_providers=process_providers,
        _original_bytes=original,
        _proposed_bytes=proposed,
    )


def _write_receipt(
    receipt_dir: Path,
    plan: EnvKeyCleanupPlan,
) -> Path:
    directory = receipt_dir.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    receipt = directory / f"env-key-cleanup-{stamp}-{secrets.token_hex(3)}.json"
    payload = {
        "format": 1,
        "app_version": __version__,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "applied_and_verified",
        "path": str(plan.path),
        "backend": plan.backend,
        "removed_providers": list(plan.removable_providers),
        "statuses": dict(plan.statuses),
        "removed_line_numbers": list(plan.removed_line_numbers),
        "duplicate_line_count": plan.duplicate_line_count,
        "original_sha256": plan.original_sha256,
        "result_sha256": plan.proposed_sha256,
        "result_size": plan.proposed_size,
        "process_environment_providers": list(
            plan.process_environment_providers
        ),
        "secret_values_included": False,
        "plaintext_backup_created": False,
    }
    temporary = receipt.with_name(f".{receipt.name}.tmp-{secrets.token_hex(3)}")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(receipt)
    finally:
        temporary.unlink(missing_ok=True)
    return receipt


def apply_env_key_cleanup(
    plan: EnvKeyCleanupPlan,
    store: CredentialStore,
    *,
    receipt_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> EnvKeyCleanupResult:
    """Apply an approved plan only if nothing relevant has changed."""

    if not plan.removable_providers:
        return EnvKeyCleanupResult(
            path=plan.path,
            backend=plan.backend,
            removed_providers=(),
            statuses=plan.statuses,
            removed_line_numbers=(),
            duplicate_line_count=0,
            original_sha256=plan.original_sha256,
            result_sha256=plan.original_sha256,
            result_size=plan.original_size,
            process_environment_providers=plan.process_environment_providers,
        )

    fresh = plan_env_key_cleanup(
        plan.path,
        store,
        providers=plan.selected_providers,
        environ=environ,
    )
    if (
        fresh.backend != plan.backend
        or fresh.removable_providers != plan.removable_providers
        or fresh.statuses != plan.statuses
        or fresh.original_sha256 != plan.original_sha256
        or fresh.proposed_sha256 != plan.proposed_sha256
    ):
        raise CredentialError(
            "The .env file or credential store changed after approval; "
            "nothing was removed. Review a fresh cleanup plan."
        )

    original_mode = stat.S_IMODE(plan.path.stat().st_mode)
    temporary = plan.path.with_name(
        f".{plan.path.name}.pc-agent-cleanup-{secrets.token_hex(4)}.tmp"
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(fresh._proposed_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, original_mode)
        temporary_data = _read_env_bytes(temporary)
        if hashlib.sha256(temporary_data).hexdigest() != fresh.proposed_sha256:
            raise CredentialError(
                "The prepared .env replacement failed verification; "
                "the original file was preserved."
            )
        _assert_removed(temporary_data, fresh.removable_providers)
        os.replace(temporary, plan.path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)

    result_data = _read_env_bytes(plan.path)
    result_hash = hashlib.sha256(result_data).hexdigest()
    if result_hash != fresh.proposed_sha256:
        raise CredentialError(
            "The .env file changed during cleanup verification; inspect it "
            "before running provider commands."
        )
    _assert_removed(result_data, fresh.removable_providers)

    receipt_path: Path | None = None
    receipt_error: str | None = None
    if receipt_dir is not None:
        try:
            receipt_path = _write_receipt(receipt_dir, fresh)
        except OSError as exc:
            receipt_error = (
                "Cleanup succeeded and was verified, but the secret-free "
                f"receipt could not be written ({exc.__class__.__name__})."
            )

    return EnvKeyCleanupResult(
        path=fresh.path,
        backend=fresh.backend,
        removed_providers=fresh.removable_providers,
        statuses=fresh.statuses,
        removed_line_numbers=fresh.removed_line_numbers,
        duplicate_line_count=fresh.duplicate_line_count,
        original_sha256=fresh.original_sha256,
        result_sha256=result_hash,
        result_size=len(result_data),
        process_environment_providers=fresh.process_environment_providers,
        receipt_path=receipt_path,
        receipt_error=receipt_error,
        verified=True,
    )
