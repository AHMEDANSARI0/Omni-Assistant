"""Portable release bundles and production-readiness audits.

Phase 18 keeps packaging deliberately boring and inspectable: a release is
an ordinary ZIP with a SHA-256 manifest, built from an allow-list of project
areas. Real ``.env`` files, databases, user workspaces, virtual environments
and caches can never enter a bundle. Verification reads the archive without
extracting it, so a hostile or accidental traversal entry cannot write a
file merely by being inspected.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import sqlite3
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings
from .errors import PcAgentError
from .http_tls import tls_trust_info
from .providers.router import build_model_router
from .self_repair import SelfRepairService
from .storage.db import LATEST_SCHEMA_VERSION

RELEASE_MANIFEST = "release-manifest.json"
_BUNDLE_FORMAT = 1
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_MAX_BUNDLE_FILES = 5_000
_MAX_BUNDLE_TOTAL_BYTES = 250_000_000
_MAX_BUNDLE_FILE_BYTES = 25_000_000
_ROOT_FILES = (
    ".env.example",
    ".gitignore",
    "CHANGELOG.md",
    "README.md",
    "pyproject.toml",
)
_INCLUDE_DIRS = ("src", "tests", "docs", "examples", "scripts")
_EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".patcher-backups",
    "data",
    "workspace",
}
_FORBIDDEN_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".key",
    ".pem",
    ".p12",
    ".pfx",
}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_forbidden_release_path(relative: Path) -> bool:
    posix = relative.as_posix()
    if posix == "workspace/.gitkeep":
        return False
    if relative.name == ".env":
        return True
    lowered_name = relative.name.lower()
    if any(
        token in lowered_name
        for token in ("private-key", "private_key", "privatekey")
    ):
        return True
    if relative.suffix.lower() in _FORBIDDEN_SUFFIXES:
        return True
    if len(relative.parts) == 1 and relative.suffix.lower() == ".mjs":
        return True
    return any(
        part in _EXCLUDED_PARTS or part.endswith(".egg-info")
        for part in relative.parts
    )


@dataclass(frozen=True, slots=True)
class ReleaseFileRecord:
    path: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class ReleaseBundleInfo:
    path: Path
    version: str
    file_count: int
    total_bytes: int
    bundle_sha256: str
    files: tuple[ReleaseFileRecord, ...] = ()

    def summary(self) -> str:
        return (
            f"version={self.version}; files={self.file_count}; "
            f"bytes={self.total_bytes}; sha256={self.bundle_sha256[:16]}..."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "version": self.version,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "bundle_sha256": self.bundle_sha256,
            "files": [record.to_dict() for record in self.files],
        }


def release_candidate_files(root_dir: Path) -> tuple[Path, ...]:
    """Return the sorted project-relative files allowed into a bundle."""

    root = root_dir.expanduser().resolve()
    candidates: list[Path] = []
    for name in _ROOT_FILES:
        path = root / name
        if path.is_file() and not path.is_symlink():
            candidates.append(Path(name))
    gitkeep = root / "workspace" / ".gitkeep"
    if gitkeep.is_file() and not gitkeep.is_symlink():
        candidates.append(Path("workspace/.gitkeep"))
    for dirname in _INCLUDE_DIRS:
        base = root / dirname
        if not base.is_dir() or base.is_symlink():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root)
            if _is_forbidden_release_path(relative):
                continue
            candidates.append(relative)
    unique = {relative.as_posix(): relative for relative in candidates}
    return tuple(unique[key] for key in sorted(unique))


def build_release_bundle(root_dir: Path, output_path: Path) -> ReleaseBundleInfo:
    """Build a reproducible portable ZIP without overwriting an output."""

    root = root_dir.expanduser().resolve()
    destination = output_path.expanduser().resolve()
    if destination.suffix.lower() != ".zip":
        raise PcAgentError("Release bundle output must end with .zip.")
    if destination.exists():
        raise PcAgentError(f"Release bundle already exists: {destination.name}.")
    relatives = release_candidate_files(root)
    if not relatives:
        raise PcAgentError("No release files were found in the project root.")
    if len(relatives) > _MAX_BUNDLE_FILES:
        raise PcAgentError("Release bundle would contain too many files.")

    records: list[ReleaseFileRecord] = []
    total = 0
    contents: list[tuple[str, bytes]] = []
    for relative in relatives:
        source = root / relative
        size = source.stat().st_size
        if size > _MAX_BUNDLE_FILE_BYTES:
            raise PcAgentError(f"Release file is too large: {relative.as_posix()}.")
        total += size
        if total > _MAX_BUNDLE_TOTAL_BYTES:
            raise PcAgentError("Release bundle would exceed the total size limit.")
        data = source.read_bytes()
        if len(data) != size:
            raise PcAgentError(f"Release file changed while reading: {relative.as_posix()}.")
        records.append(
            ReleaseFileRecord(relative.as_posix(), size, _sha256_bytes(data))
        )
        contents.append((relative.as_posix(), data))

    manifest = {
        "format": _BUNDLE_FORMAT,
        "app": "Self-Healing PC Agent",
        "version": __version__,
        "file_count": len(records),
        "total_bytes": total,
        "files": [record.to_dict() for record in records],
    }
    manifest_data = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(
        f".{destination.name}.tmp-{secrets.token_hex(4)}"
    )
    try:
        with zipfile.ZipFile(
            temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            _write_entry(archive, RELEASE_MANIFEST, manifest_data)
            for name, data in contents:
                _write_entry(archive, name, data)
        info = verify_release_bundle(temp)
        if destination.exists():
            raise PcAgentError(f"Release bundle already exists: {destination.name}.")
        temp.replace(destination)
    finally:
        if temp.exists():
            temp.unlink()
    return ReleaseBundleInfo(
        path=destination,
        version=info.version,
        file_count=info.file_count,
        total_bytes=info.total_bytes,
        bundle_sha256=_sha256_file(destination),
        files=info.files,
    )


def _write_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, data)


def _validate_entry_name(name: str) -> Path:
    if not name or "\\" in name or name.startswith("/") or ":" in name.split("/", 1)[0]:
        raise PcAgentError(f"Release bundle contains an unsafe entry: {name!r}.")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise PcAgentError(f"Release bundle contains an unsafe entry: {name!r}.")
    if path.as_posix() != name:
        raise PcAgentError(f"Release bundle contains a non-canonical entry: {name!r}.")
    return path


def verify_release_bundle(bundle_path: Path) -> ReleaseBundleInfo:
    """Verify one bundle's manifest and hashes without extracting it."""

    bundle = bundle_path.expanduser().resolve()
    if not bundle.is_file():
        raise PcAgentError(f"Release bundle was not found: {bundle}.")
    try:
        archive = zipfile.ZipFile(bundle, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise PcAgentError("Release bundle is not a readable ZIP file.") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > _MAX_BUNDLE_FILES + 1:
            raise PcAgentError("Release bundle contains too many files.")
        names: list[str] = []
        for info in infos:
            if info.is_dir():
                continue
            relative = _validate_entry_name(info.filename)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise PcAgentError("Release bundle contains a symbolic link entry.")
            if info.filename != RELEASE_MANIFEST and _is_forbidden_release_path(
                relative
            ):
                raise PcAgentError(
                    f"Release bundle contains a forbidden private/generated file: {info.filename}."
                )
            names.append(info.filename)
        if len(names) != len(set(names)):
            raise PcAgentError("Release bundle contains duplicate entries.")
        if RELEASE_MANIFEST not in names:
            raise PcAgentError("Release bundle manifest is missing.")
        try:
            manifest = json.loads(archive.read(RELEASE_MANIFEST).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
            raise PcAgentError("Release bundle manifest is unreadable.") from exc
        records = _parse_manifest(manifest)
        expected_names = [RELEASE_MANIFEST] + [record.path for record in records]
        if sorted(names) != sorted(expected_names):
            raise PcAgentError("Release bundle entries do not match its manifest.")

        total = 0
        version = str(manifest.get("version") or "")
        pyproject_version = ""
        package_version = ""
        for record in records:
            info = archive.getinfo(record.path)
            if info.file_size != record.size or record.size > _MAX_BUNDLE_FILE_BYTES:
                raise PcAgentError(f"Release bundle size mismatch: {record.path}.")
            total += record.size
            if total > _MAX_BUNDLE_TOTAL_BYTES:
                raise PcAgentError("Release bundle exceeds the total size limit.")
            digest = hashlib.sha256()
            with archive.open(info, "r") as handle:
                for chunk in iter(lambda: handle.read(64 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != record.sha256:
                raise PcAgentError(f"Release bundle hash mismatch: {record.path}.")
            if record.path == "pyproject.toml":
                pyproject_version = _version_from_pyproject(
                    archive.read(record.path).decode("utf-8", errors="replace")
                )
            if record.path == "src/pc_agent/__init__.py":
                package_version = _version_from_package(
                    archive.read(record.path).decode("utf-8", errors="replace")
                )
        if total != int(manifest.get("total_bytes") or -1):
            raise PcAgentError("Release bundle total size does not match its manifest.")
        if pyproject_version and pyproject_version != version:
            raise PcAgentError("Release bundle pyproject version does not match its manifest.")
        if package_version and package_version != version:
            raise PcAgentError("Release bundle package version does not match its manifest.")

    return ReleaseBundleInfo(
        path=bundle,
        version=version,
        file_count=len(records),
        total_bytes=total,
        bundle_sha256=_sha256_file(bundle),
        files=tuple(records),
    )


def _parse_manifest(manifest: Any) -> list[ReleaseFileRecord]:
    if not isinstance(manifest, dict) or manifest.get("format") != _BUNDLE_FORMAT:
        raise PcAgentError("Release bundle manifest has an unsupported format.")
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise PcAgentError("Release bundle manifest version is invalid.")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise PcAgentError("Release bundle manifest has no file records.")
    if int(manifest.get("file_count") or -1) != len(raw_files):
        raise PcAgentError("Release bundle manifest file count is invalid.")
    records: list[ReleaseFileRecord] = []
    for item in raw_files:
        if not isinstance(item, dict):
            raise PcAgentError("Release bundle manifest has an invalid file record.")
        raw_path = item.get("path")
        size = item.get("size")
        digest = item.get("sha256")
        if not isinstance(raw_path, str):
            raise PcAgentError("Release bundle manifest has an invalid file record.")
        relative = _validate_entry_name(raw_path)
        if _is_forbidden_release_path(relative):
            raise PcAgentError("Release bundle manifest contains a forbidden file.")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise PcAgentError("Release bundle manifest has an invalid file record.")
        records.append(ReleaseFileRecord(raw_path, size, digest))
    if [record.path for record in records] != sorted(record.path for record in records):
        raise PcAgentError("Release bundle manifest records are not sorted.")
    return records


def _version_from_pyproject(text: str) -> str:
    try:
        import tomllib

        data = tomllib.loads(text)
        project = data.get("project") if isinstance(data, dict) else None
        return str(project.get("version") or "") if isinstance(project, dict) else ""
    except (tomllib.TOMLDecodeError, ValueError):
        match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
        return match.group(1) if match else ""


def _version_from_package(text: str) -> str:
    match = re.search(r'(?m)^__version__\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else ""


# ----------------------------------------------------------------------
# Production readiness audit
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditCheck:
    name: str
    status: str  # pass | warn | fail | info
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class ReleaseAudit:
    checks: tuple[AuditCheck, ...]

    @property
    def failures(self) -> int:
        return sum(1 for check in self.checks if check.status == "fail")

    @property
    def warnings(self) -> int:
        return sum(1 for check in self.checks if check.status == "warn")

    @property
    def ok(self) -> bool:
        return self.failures == 0

    def summary(self) -> str:
        state = "READY" if self.ok else "ATTENTION"
        return f"{state}; failures={self.failures}; warnings={self.warnings}; checks={len(self.checks)}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "failures": self.failures,
            "warnings": self.warnings,
            "checks": [check.to_dict() for check in self.checks],
        }


def _database_state(path: Path) -> tuple[int, str, int]:
    if not path.is_file():
        return 0, "missing", 0
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return 0, "unreadable", path.stat().st_size
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
        integrity = str(row[0]) if row else "unknown"
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if table is None:
            return 0, integrity, path.stat().st_size
        version_row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
        return int(version_row[0]), integrity, path.stat().st_size
    except sqlite3.Error:
        return 0, "unreadable", path.stat().st_size
    finally:
        connection.close()


def audit_install(settings: Settings) -> ReleaseAudit:
    """Run secret-free, read-only production-readiness checks."""

    checks: list[AuditCheck] = []
    python_ok = sys.version_info >= (3, 11)
    checks.append(
        AuditCheck(
            "python",
            "pass" if python_ok else "fail",
            f"Python {platform.python_version()} (requires 3.11+)",
        )
    )

    pyproject = settings.root_dir / "pyproject.toml"
    package_init = settings.root_dir / "src" / "pc_agent" / "__init__.py"
    pyproject_version = (
        _version_from_pyproject(pyproject.read_text(encoding="utf-8", errors="replace"))
        if pyproject.is_file()
        else ""
    )
    package_version = (
        _version_from_package(package_init.read_text(encoding="utf-8", errors="replace"))
        if package_init.is_file()
        else ""
    )
    versions = {__version__, pyproject_version, package_version}
    checks.append(
        AuditCheck(
            "versions",
            "pass" if versions == {__version__} else "fail",
            (
                f"app={__version__}, pyproject={pyproject_version or 'missing'}, "
                f"package={package_version or 'missing'}"
            ),
        )
    )

    schema, integrity, _size = _database_state(settings.database_path)
    if integrity == "missing":
        checks.append(
            AuditCheck("database", "warn", "Database does not exist yet; it will be created on first use.")
        )
    elif integrity != "ok":
        checks.append(
            AuditCheck("database", "fail", f"SQLite integrity check returned {integrity!r}.")
        )
    elif schema > LATEST_SCHEMA_VERSION:
        checks.append(
            AuditCheck(
                "database",
                "fail",
                f"Database schema v{schema} is newer than this build's v{LATEST_SCHEMA_VERSION}.",
            )
        )
    elif schema < LATEST_SCHEMA_VERSION:
        checks.append(
            AuditCheck(
                "database",
                "warn",
                f"Database schema v{schema}; migration to v{LATEST_SCHEMA_VERSION} is pending.",
            )
        )
    else:
        checks.append(
            AuditCheck("database", "pass", f"SQLite integrity ok; schema v{schema}.")
        )

    folder_problems = []
    for label, path in (
        ("root", settings.root_dir),
        ("workspace", settings.workspace_dir),
        ("data", settings.data_dir),
    ):
        if not path.is_dir():
            folder_problems.append(f"{label} missing")
        elif not os.access(path, os.W_OK):
            folder_problems.append(f"{label} not writable")
    checks.append(
        AuditCheck(
            "folders",
            "fail" if folder_problems else "pass",
            "; ".join(folder_problems) or "Root, workspace and data folders are writable.",
        )
    )

    try:
        tls = tls_trust_info()
        checks.append(
            AuditCheck(
                "tls",
                "pass" if tls.ca_certificates > 0 else "fail",
                f"CA certificates loaded: {tls.ca_certificates}.",
            )
        )
    except Exception as exc:  # noqa: BLE001 - audit reports instead of crashing
        checks.append(
            AuditCheck("tls", "fail", f"TLS trust unavailable ({exc.__class__.__name__}).")
        )

    route = build_model_router(settings).provider_names
    external = tuple(name for name in route if name != "mock")
    checks.append(
        AuditCheck(
            "providers",
            "pass" if external else "warn",
            (
                f"Usable route: {' -> '.join(route)}"
                if external
                else "Only the offline mock provider is usable; deterministic local tasks still work."
            ),
        )
    )

    environment_providers = [
        name
        for name, source in settings.credential_sources
        if source == "environment"
    ]
    if settings.credential_store == "windows" and not settings.credential_store_available:
        checks.append(
            AuditCheck(
                "credentials",
                "fail",
                "Windows Credential Manager was explicitly selected but is unavailable.",
            )
        )
    elif environment_providers and sys.platform == "win32":
        checks.append(
            AuditCheck(
                "credentials",
                "warn",
                "Provider keys still come from environment/.env for: "
                + ", ".join(environment_providers)
                + ". If copies are already stored, remove those .env lines to use Credential Manager.",
            )
        )
    else:
        checks.append(
            AuditCheck(
                "credentials",
                "pass",
                "No credential values are shown; effective sources are recorded by provider name only.",
            )
        )

    enabled_gates = [
        name
        for name, enabled in (
            ("subprocess", settings.allow_subprocess),
            ("browser", settings.allow_browser),
            ("desktop_automation", settings.allow_desktop_automation),
            ("vision", settings.allow_vision),
            ("self_repair", settings.allow_self_repair),
        )
        if enabled
    ]
    checks.append(
        AuditCheck(
            "optional_gates",
            "warn" if enabled_gates else "pass",
            (
                "Explicitly enabled: " + ", ".join(enabled_gates)
                if enabled_gates
                else "All optional high-impact gates are off."
            ),
        )
    )

    unlimited = [
        name
        for name, value in (
            ("tokens", settings.task_token_budget),
            ("provider_calls", settings.task_provider_call_budget),
            ("tools", settings.task_tool_budget),
            ("time", settings.task_time_budget_seconds),
        )
        if value == 0
    ]
    checks.append(
        AuditCheck(
            "task_budgets",
            "warn" if unlimited else "pass",
            (
                "Unlimited budgets: " + ", ".join(unlimited)
                if unlimited
                else "Token, provider-call, tool, step and time budgets are finite."
            ),
        )
    )

    if not settings.allow_self_repair:
        checks.append(
            AuditCheck(
                "self_repair",
                "info",
                "Core self-repair is disabled; baseline status is not required.",
            )
        )
    else:
        status = SelfRepairService(
            settings.root_dir, settings.data_dir
        ).baseline_status()
        if not status.valid:
            checks.append(
                AuditCheck("self_repair", "fail", f"Baseline invalid: {status.detail}")
            )
        elif not status.exists:
            checks.append(
                AuditCheck(
                    "self_repair",
                    "warn",
                    "Self-repair is enabled but no known-good baseline has been captured.",
                )
            )
        elif status.app_version and status.app_version != __version__:
            checks.append(
                AuditCheck(
                    "self_repair",
                    "fail",
                    f"Baseline is for v{status.app_version}, not v{__version__}.",
                )
            )
        else:
            checks.append(
                AuditCheck(
                    "self_repair",
                    "pass",
                    f"Known-good baseline present ({status.file_count} files).",
                )
            )

    if settings.license_enforcement == "off":
        checks.append(
            AuditCheck(
                "licensing",
                "warn" if settings.environment == "production" else "info",
                "License enforcement is off (personal/local default).",
            )
        )
    elif not settings.license_public_key:
        checks.append(
            AuditCheck(
                "licensing",
                "warn",
                "License enforcement is enabled but no vendor public key is configured.",
            )
        )
    else:
        checks.append(
            AuditCheck(
                "licensing",
                "pass",
                f"License enforcement={settings.license_enforcement} with a configured public key.",
            )
        )

    if settings.require_signed_updates and not settings.release_public_key:
        checks.append(
            AuditCheck(
                "signed_updates",
                "fail",
                "Signed updates are required but PC_AGENT_RELEASE_PUBLIC_KEY is not configured.",
            )
        )
    elif settings.release_public_key:
        checks.append(
            AuditCheck(
                "signed_updates",
                "pass",
                (
                    "Offline update signatures can be verified; "
                    f"key_id={settings.release_key_id or 'any'}; "
                    f"required={'yes' if settings.require_signed_updates else 'no'}."
                ),
            )
        )
    else:
        checks.append(
            AuditCheck(
                "signed_updates",
                "warn" if settings.environment == "production" else "info",
                "No release public key is configured; signed offline updates cannot be verified yet.",
            )
        )

    if not settings.online_updates_enabled:
        checks.append(
            AuditCheck(
                "online_updates",
                "info",
                "Online update downloads are disabled; signed offline updates remain available.",
            )
        )
    elif not settings.update_feed_url:
        checks.append(
            AuditCheck(
                "online_updates",
                "warn",
                "Online updates are enabled but PC_AGENT_UPDATE_FEED_URL is not configured.",
            )
        )
    elif settings.release_public_key and settings.require_signed_updates:
        checks.append(
            AuditCheck(
                "online_updates",
                "pass",
                (
                    "Online signed update downloads are explicitly enabled for "
                    f"channel {settings.update_channel}; downloads still require "
                    "separate staging and apply steps."
                ),
            )
        )
    else:
        checks.append(
            AuditCheck(
                "online_updates",
                "warn",
                (
                    "Online updates are enabled, but verified signatures are not "
                    "fully required; configure PC_AGENT_RELEASE_PUBLIC_KEY and "
                    "PC_AGENT_REQUIRE_SIGNED_UPDATES=1 for the strongest policy."
                ),
            )
        )

    candidates = release_candidate_files(settings.root_dir)
    checks.append(
        AuditCheck(
            "release_bundle",
            "pass" if candidates else "fail",
            f"{len(candidates)} allow-listed files can be bundled; .env/data/workspace contents are excluded.",
        )
    )
    return ReleaseAudit(tuple(checks))
