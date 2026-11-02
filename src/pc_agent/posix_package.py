"""Deterministic per-user POSIX installer source packages.

Phase 41 provides the Linux/macOS counterpart to the Phase 38 Windows
installer source package.  It stages the same allow-listed runtime release
subset, adds auditable POSIX shell install/uninstall/launch scripts, and
hash-manifests every file.  It does not create a native DEB/RPM/PKG/DMG,
sign or notarize anything, use administrator privileges, download Python,
or claim live macOS verification from a Linux build host.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .errors import PcAgentError
from .release import (
    ReleaseFileRecord,
    _is_forbidden_release_path,
    _sha256_bytes,
    _sha256_file,
    _validate_entry_name,
    release_candidate_files,
)

POSIX_PACKAGE_APP_NAME = "Self-Healing PC Agent"
POSIX_PACKAGE_APP_ID = "self-healing-pc-agent"
POSIX_PACKAGE_MANIFEST = "posix-package-manifest.json"
POSIX_PACKAGE_README = "README-POSIX.txt"
POSIX_PACKAGE_FILE_LIST = "payload-files.txt"
POSIX_INSTALL_SCRIPT = Path("scripts/install_posix.sh")
POSIX_UNINSTALL_SCRIPT = Path("scripts/uninstall_posix.sh")
POSIX_GUI_LAUNCHER = Path("scripts/launch_gui.sh")
POSIX_CLI_LAUNCHER = Path("scripts/launch_cli.sh")

_MANIFEST_FORMAT = 1
_MAX_POSIX_FILES = 5_000
_MAX_POSIX_TOTAL_BYTES = 250_000_000
_MAX_POSIX_FILE_BYTES = 25_000_000
_MAX_POSIX_MANIFEST_BYTES = 5_000_000
_MAX_POSIX_SCRIPT_BYTES = 1_000_000
_REQUIRED_PAYLOAD_FILES = {
    ".env.example",
    "CHANGELOG.md",
    "README.md",
    "docs/phase41.md",
    "pyproject.toml",
    "scripts/install_posix.sh",
    "scripts/launch_cli.sh",
    "scripts/launch_gui.sh",
    "scripts/uninstall_posix.sh",
    "src/pc_agent/__init__.py",
    "src/pc_agent/cli.py",
    "src/pc_agent/gui/app.py",
    "workspace/.gitkeep",
}


@dataclass(frozen=True, slots=True)
class PosixPackageInfo:
    path: Path
    version: str
    file_count: int
    payload_file_count: int
    total_bytes: int
    manifest_sha256: str
    install_script_sha256: str
    files: tuple[ReleaseFileRecord, ...] = ()

    def summary(self) -> str:
        return (
            f"version={self.version}; files={self.file_count}; "
            f"payload={self.payload_file_count}; bytes={self.total_bytes}; "
            f"install_sha256={self.install_script_sha256[:16]}..."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "app": POSIX_PACKAGE_APP_NAME,
            "app_id": POSIX_PACKAGE_APP_ID,
            "platform": "posix",
            "version": self.version,
            "file_count": self.file_count,
            "payload_file_count": self.payload_file_count,
            "total_bytes": self.total_bytes,
            "manifest_sha256": self.manifest_sha256,
            "install_script_sha256": self.install_script_sha256,
            "native_package_format": None,
            "signed": False,
            "notarized": False,
            "files": [record.to_dict() for record in self.files],
        }


def posix_package_payload_files(root_dir: Path) -> tuple[Path, ...]:
    """Return sorted runtime files allowed into a POSIX package."""

    candidates = tuple(
        relative
        for relative in release_candidate_files(root_dir)
        if relative.parts[0] != "tests"
    )
    present = {relative.as_posix() for relative in candidates}
    missing = sorted(_REQUIRED_PAYLOAD_FILES - present)
    if missing:
        raise PcAgentError(
            "POSIX installer source is missing required files: "
            + ", ".join(missing)
            + "."
        )
    return candidates


def _read_bounded(path: Path, *, limit: int, label: str) -> bytes:
    candidate = path.expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise PcAgentError(f"{label} was not found: {candidate}.")
    size = candidate.stat().st_size
    if size > limit:
        raise PcAgentError(f"{label} exceeds the {limit}-byte limit.")
    data = candidate.read_bytes()
    if len(data) != size:
        raise PcAgentError(f"{label} changed while reading: {candidate}.")
    return data


def _validate_output_path(root: Path, output: Path) -> Path:
    destination = output.expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise PcAgentError(f"POSIX package output already exists: {destination}.")
    if destination == root or destination in root.parents:
        raise PcAgentError("POSIX package output must not contain the project root.")
    try:
        relative = destination.relative_to(root)
    except ValueError:
        return destination
    if not relative.parts or relative.parts[0] != "workspace":
        raise PcAgentError(
            "POSIX package output inside the project must be under workspace/."
        )
    return destination


def _readme_text(version: str) -> bytes:
    return (
        f"Self-Healing PC Agent {version} - POSIX installer source package\n"
        "================================================================\n\n"
        "Prerequisites:\n"
        "- Linux or macOS with a POSIX sh\n"
        "- Python 3.11 or newer available as python3 (or set PYTHON_BIN)\n"
        "- Tkinter if the desktop GUI will be used\n\n"
        "Install for the current user:\n\n"
        "  sh ./install.sh\n\n"
        "Choose another per-user directory:\n\n"
        "  sh ./install.sh --prefix /path/to/self-healing-pc-agent\n\n"
        "The installer copies payload/, creates .venv, installs the local\n"
        "package into that environment, runs doctor and the GUI environment\n"
        "check, and creates CLI/GUI launchers. It does not use administrator\n"
        "privileges, download Python, edit a real .env, or create a native\n"
        "DEB/RPM/PKG/DMG package.\n\n"
        "Remove launchers and .venv while preserving application/user files:\n\n"
        "  sh ./uninstall.sh\n\n"
        "Also remove the exact published payload files (still preserving\n"
        ".env, data, workspace files, and OS credentials):\n\n"
        "  sh ./uninstall.sh --remove-app --yes\n\n"
        "Verify this package from an installed Self-Healing PC Agent with:\n\n"
        "  pc-agent release posix-verify <this-directory>\n\n"
        "This source package is unsigned and not Apple-notarized. Linux is\n"
        "exercised by automated tests; macOS uses the same POSIX scripts but\n"
        "requires a real Mac for live desktop evidence.\n"
    ).encode()


def _payload_list_bytes(payload_files: tuple[Path, ...]) -> bytes:
    return (
        "\n".join(relative.as_posix() for relative in payload_files) + "\n"
    ).encode()


def build_posix_package(
    root_dir: Path,
    output_dir: Path,
) -> PosixPackageInfo:
    """Build and verify a hash-manifested POSIX installer source directory."""

    root = root_dir.expanduser().resolve()
    destination = _validate_output_path(root, output_dir)
    payload_relatives = posix_package_payload_files(root)
    if len(payload_relatives) + 4 > _MAX_POSIX_FILES:
        raise PcAgentError("POSIX package would contain too many files.")

    contents: dict[str, bytes] = {}
    total = 0
    for relative in payload_relatives:
        data = _read_bounded(
            root / relative,
            limit=_MAX_POSIX_FILE_BYTES,
            label="POSIX package payload file",
        )
        total += len(data)
        if total > _MAX_POSIX_TOTAL_BYTES:
            raise PcAgentError("POSIX package exceeds the total size limit.")
        contents[f"payload/{relative.as_posix()}"] = data

    generated = {
        "install.sh": contents[f"payload/{POSIX_INSTALL_SCRIPT.as_posix()}"],
        "uninstall.sh": contents[f"payload/{POSIX_UNINSTALL_SCRIPT.as_posix()}"],
        POSIX_PACKAGE_FILE_LIST: _payload_list_bytes(payload_relatives),
        POSIX_PACKAGE_README: _readme_text(__version__),
    }
    for path, data in generated.items():
        if len(data) > _MAX_POSIX_SCRIPT_BYTES:
            raise PcAgentError(f"Generated POSIX package file is too large: {path}.")
        total += len(data)
        contents[path] = data
    if total > _MAX_POSIX_TOTAL_BYTES:
        raise PcAgentError("POSIX package exceeds the total size limit.")

    records = tuple(
        ReleaseFileRecord(path, len(data), _sha256_bytes(data))
        for path, data in sorted(contents.items())
    )
    manifest = {
        "format": _MANIFEST_FORMAT,
        "app": POSIX_PACKAGE_APP_NAME,
        "app_id": POSIX_PACKAGE_APP_ID,
        "platform": "posix",
        "version": __version__,
        "file_count": len(records),
        "total_bytes": total,
        "files": [record.to_dict() for record in records],
    }
    manifest_data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    if len(manifest_data) > _MAX_POSIX_MANIFEST_BYTES:
        raise PcAgentError("POSIX package manifest exceeds the size limit.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.tmp-{secrets.token_hex(4)}"
    )
    executable_paths = {
        "install.sh",
        "uninstall.sh",
        f"payload/{POSIX_INSTALL_SCRIPT.as_posix()}",
        f"payload/{POSIX_UNINSTALL_SCRIPT.as_posix()}",
        f"payload/{POSIX_GUI_LAUNCHER.as_posix()}",
        f"payload/{POSIX_CLI_LAUNCHER.as_posix()}",
    }
    try:
        for relative, data in contents.items():
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if relative in executable_paths:
                target.chmod(0o755)
        (temporary / POSIX_PACKAGE_MANIFEST).write_bytes(manifest_data)
        verify_posix_package(temporary)
        if destination.exists():
            raise PcAgentError(f"POSIX package output already exists: {destination}.")
        temporary.replace(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return verify_posix_package(destination)


def _parse_manifest(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PcAgentError("POSIX package manifest is not valid JSON.") from exc
    expected = {
        "format",
        "app",
        "app_id",
        "platform",
        "version",
        "file_count",
        "total_bytes",
        "files",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise PcAgentError("POSIX package manifest has an unexpected shape.")
    if payload["format"] != _MANIFEST_FORMAT:
        raise PcAgentError("POSIX package manifest format is unsupported.")
    if payload["app"] != POSIX_PACKAGE_APP_NAME or payload["app_id"] != POSIX_PACKAGE_APP_ID:
        raise PcAgentError("POSIX package manifest application is unexpected.")
    if payload["platform"] != "posix" or payload["version"] != __version__:
        raise PcAgentError("POSIX package platform/version does not match this application.")
    if not isinstance(payload["file_count"], int) or not isinstance(
        payload["total_bytes"], int
    ):
        raise PcAgentError("POSIX package manifest counts are invalid.")
    if not isinstance(payload["files"], list):
        raise PcAgentError("POSIX package manifest files are invalid.")
    return payload


def _manifest_records(payload: dict[str, Any]) -> tuple[ReleaseFileRecord, ...]:
    raw_files = payload["files"]
    if not raw_files or len(raw_files) > _MAX_POSIX_FILES:
        raise PcAgentError("POSIX package manifest file count is invalid.")
    records: list[ReleaseFileRecord] = []
    seen: set[str] = set()
    total = 0
    for raw in raw_files:
        if not isinstance(raw, dict) or set(raw) != {"path", "size", "sha256"}:
            raise PcAgentError("POSIX package manifest contains a malformed record.")
        raw_path = raw["path"]
        size = raw["size"]
        digest = raw["sha256"]
        if not isinstance(raw_path, str) or raw_path in seen:
            raise PcAgentError("POSIX package manifest contains a duplicate path.")
        relative = _validate_entry_name(raw_path)
        allowed_root_files = {
            "install.sh",
            "uninstall.sh",
            POSIX_PACKAGE_FILE_LIST,
            POSIX_PACKAGE_README,
        }
        if raw_path not in allowed_root_files and relative.parts[0] != "payload":
            raise PcAgentError(f"POSIX package contains an unsafe entry: {raw_path!r}.")
        if (
            not isinstance(size, int)
            or size < 0
            or size > _MAX_POSIX_FILE_BYTES
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise PcAgentError("POSIX package manifest contains an invalid record.")
        seen.add(raw_path)
        total += size
        records.append(ReleaseFileRecord(raw_path, size, digest))
    if payload["file_count"] != len(records) or payload["total_bytes"] != total:
        raise PcAgentError("POSIX package manifest totals do not match its files.")
    if total > _MAX_POSIX_TOTAL_BYTES:
        raise PcAgentError("POSIX package exceeds the total size limit.")
    return tuple(sorted(records, key=lambda record: record.path))


def _payload_relatives(
    records: tuple[ReleaseFileRecord, ...],
) -> tuple[Path, ...]:
    relatives: list[Path] = []
    for record in records:
        if not record.path.startswith("payload/"):
            continue
        relative = Path(record.path.removeprefix("payload/"))
        if relative.parts[0] == "tests" or _is_forbidden_release_path(relative):
            raise PcAgentError(
                f"POSIX package payload contains a forbidden file: {record.path}."
            )
        relatives.append(relative)
    present = {relative.as_posix() for relative in relatives}
    missing = sorted(_REQUIRED_PAYLOAD_FILES - present)
    if missing:
        raise PcAgentError(
            "POSIX package payload is missing required files: "
            + ", ".join(missing)
            + "."
        )
    return tuple(sorted(relatives, key=lambda relative: relative.as_posix()))


def _script_text(data: bytes, *, label: str) -> str:
    if len(data) > _MAX_POSIX_SCRIPT_BYTES:
        raise PcAgentError(f"{label} exceeds the size limit.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PcAgentError(f"{label} is not valid UTF-8.") from exc


def _verify_scripts(file_data: dict[str, bytes]) -> None:
    install = _script_text(file_data["install.sh"], label="POSIX install script")
    uninstall = _script_text(
        file_data["uninstall.sh"], label="POSIX uninstall script"
    )
    payload_install = file_data[f"payload/{POSIX_INSTALL_SCRIPT.as_posix()}"]
    payload_uninstall = file_data[f"payload/{POSIX_UNINSTALL_SCRIPT.as_posix()}"]
    if file_data["install.sh"] != payload_install:
        raise PcAgentError("POSIX root install script does not match its payload copy.")
    if file_data["uninstall.sh"] != payload_uninstall:
        raise PcAgentError("POSIX root uninstall script does not match its payload copy.")

    gui = _script_text(
        file_data[f"payload/{POSIX_GUI_LAUNCHER.as_posix()}"],
        label="POSIX GUI launcher",
    )
    cli = _script_text(
        file_data[f"payload/{POSIX_CLI_LAUNCHER.as_posix()}"],
        label="POSIX CLI launcher",
    )
    scripts = {
        "install": install,
        "uninstall": uninstall,
        "gui": gui,
        "cli": cli,
    }
    for name, script in scripts.items():
        if not script.startswith("#!/bin/sh\n") or "set -eu" not in script:
            raise PcAgentError(f"POSIX {name} script is missing its safe shell header.")
        lowered = script.lower()
        for forbidden in (
            "sudo ",
            "curl ",
            "wget ",
            "eval ",
            "--break-system-packages",
            "chmod 777",
            "rm -rf /",
            'rm -rf "$home"',
        ):
            if forbidden in lowered:
                raise PcAgentError(f"POSIX {name} script contains forbidden wiring.")

    required_install = (
        "payload-files.txt",
        ".pc-agent-posix-install.txt",
        "sys.version_info >= (3, 11)",
        "-m venv",
        "-m pip install -e",
        "pc-agent doctor",
        "pc-agent gui --check",
        "self-healing-pc-agent-gui",
        "self-healing-pc-agent.desktop",
        "Self-Healing PC Agent.command",
        "--skip-python-setup",
    )
    missing = [fragment for fragment in required_install if fragment not in install]
    if missing:
        raise PcAgentError(
            "POSIX install script is incomplete: " + ", ".join(missing) + "."
        )

    required_uninstall = (
        ".pc-agent-posix-install.txt",
        ".pc-agent-posix-files.txt",
        "--remove-app",
        "--yes",
        "Unsafe path in installed payload list",
        "Preserved user .env, data, workspace files, and OS credentials.",
    )
    missing = [
        fragment for fragment in required_uninstall if fragment not in uninstall
    ]
    if 'rm -rf "$TARGET_DIR"' in uninstall:
        missing.append("exact-target virtual-environment removal")
    if missing:
        raise PcAgentError(
            "POSIX uninstall script is incomplete or unsafe: "
            + ", ".join(missing)
            + "."
        )
    if ".venv/bin/pc-agent-gui" not in gui or ".venv/bin/pc-agent" not in cli:
        raise PcAgentError("POSIX launch scripts do not point at the local virtual environment.")


def verify_posix_package(package_dir: Path) -> PosixPackageInfo:
    """Verify a POSIX installer source directory without executing it."""

    root_input = package_dir.expanduser()
    if root_input.is_symlink() or not root_input.is_dir():
        raise PcAgentError(f"POSIX package directory was not found: {root_input}.")
    root = root_input.resolve()
    manifest_path = root / POSIX_PACKAGE_MANIFEST
    manifest_data = _read_bounded(
        manifest_path,
        limit=_MAX_POSIX_MANIFEST_BYTES,
        label="POSIX package manifest",
    )
    manifest = _parse_manifest(manifest_data)
    records = _manifest_records(manifest)
    payload_files = _payload_relatives(records)
    record_by_path = {record.path: record for record in records}

    actual: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise PcAgentError(f"POSIX package contains a symbolic link: {relative}.")
        if path.is_file():
            actual.add(relative.as_posix())
    expected = set(record_by_path) | {POSIX_PACKAGE_MANIFEST}
    if actual != expected:
        raise PcAgentError("POSIX package inventory does not match its manifest.")

    file_data: dict[str, bytes] = {}
    for record in records:
        data = _read_bounded(
            root / record.path,
            limit=_MAX_POSIX_FILE_BYTES,
            label="POSIX package file",
        )
        if len(data) != record.size or _sha256_bytes(data) != record.sha256:
            raise PcAgentError(f"POSIX package file failed verification: {record.path}.")
        file_data[record.path] = data

    if file_data[POSIX_PACKAGE_FILE_LIST] != _payload_list_bytes(payload_files):
        raise PcAgentError("POSIX payload file list does not match the manifest.")
    if file_data[POSIX_PACKAGE_README] != _readme_text(__version__):
        raise PcAgentError("POSIX package README is inconsistent.")
    _verify_scripts(file_data)

    if os.name == "posix":
        executable_paths = [
            "install.sh",
            "uninstall.sh",
            f"payload/{POSIX_INSTALL_SCRIPT.as_posix()}",
            f"payload/{POSIX_UNINSTALL_SCRIPT.as_posix()}",
            f"payload/{POSIX_GUI_LAUNCHER.as_posix()}",
            f"payload/{POSIX_CLI_LAUNCHER.as_posix()}",
        ]
        for relative in executable_paths:
            mode = stat.S_IMODE((root / relative).stat().st_mode)
            if not (mode & stat.S_IXUSR):
                raise PcAgentError(f"POSIX package script is not executable: {relative}.")

    return PosixPackageInfo(
        path=root,
        version=str(manifest["version"]),
        file_count=len(records),
        payload_file_count=len(payload_files),
        total_bytes=int(manifest["total_bytes"]),
        manifest_sha256=_sha256_file(manifest_path),
        install_script_sha256=record_by_path["install.sh"].sha256,
        files=records,
    )
