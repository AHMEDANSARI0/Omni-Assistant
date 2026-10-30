"""Deterministic unsigned Windows installer source packages.

Phase 38 prepares an inspectable per-user Inno Setup source package from
the same allow-listed release files used for portable bundles. It does not
compile an executable, download Inno Setup, modify Windows, or perform
Authenticode signing. A Windows build machine can subsequently run
``scripts/build_windows_installer.ps1`` with a locally installed Inno Setup
6 compiler.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
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

WINDOWS_INSTALLER_APP_ID = "C80BA7A3-9651-46C2-9D45-D1DADF2AD063"
WINDOWS_INSTALLER_APP_NAME = "Self-Healing PC Agent"
WINDOWS_INSTALLER_MANIFEST = "windows-installer-manifest.json"
WINDOWS_INSTALLER_SCRIPT = "SelfHealingPCAgent.iss"
WINDOWS_INSTALLER_TEMPLATE = Path("scripts/windows_installer.iss")
WINDOWS_INSTALLER_BUILD_SCRIPT = Path("scripts/build_windows_installer.ps1")

_MANIFEST_FORMAT = 1
_MAX_INSTALLER_FILES = 5_000
_MAX_INSTALLER_TOTAL_BYTES = 250_000_000
_MAX_INSTALLER_FILE_BYTES = 25_000_000
_MAX_INSTALLER_MANIFEST_BYTES = 5_000_000
_MAX_INSTALLER_SCRIPT_BYTES = 2_000_000
_VERSION_TOKEN = "@@PC_AGENT_VERSION@@"
_FILES_TOKEN = "@@PC_AGENT_FILES@@"
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
_REQUIRED_PAYLOAD_FILES = {
    ".env.example",
    "CHANGELOG.md",
    "README.md",
    "pyproject.toml",
    "scripts/build_windows_installer.ps1",
    "scripts/install_windows.ps1",
    "scripts/launch_cli.cmd",
    "scripts/launch_gui.cmd",
    "scripts/uninstall_windows.ps1",
    "scripts/windows_installer.iss",
    "src/pc_agent/__init__.py",
    "src/pc_agent/cli.py",
    "src/pc_agent/gui/app.py",
    "workspace/.gitkeep",
}


@dataclass(frozen=True, slots=True)
class WindowsInstallerSourceInfo:
    path: Path
    version: str
    app_id: str
    file_count: int
    payload_file_count: int
    total_bytes: int
    script_sha256: str
    manifest_sha256: str
    compiled_output_count: int = 0
    files: tuple[ReleaseFileRecord, ...] = ()

    def summary(self) -> str:
        return (
            f"version={self.version}; files={self.file_count}; "
            f"payload={self.payload_file_count}; bytes={self.total_bytes}; "
            f"script_sha256={self.script_sha256[:16]}..."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "app": WINDOWS_INSTALLER_APP_NAME,
            "app_id": self.app_id,
            "version": self.version,
            "file_count": self.file_count,
            "payload_file_count": self.payload_file_count,
            "total_bytes": self.total_bytes,
            "script_sha256": self.script_sha256,
            "manifest_sha256": self.manifest_sha256,
            "compiled_output_count": self.compiled_output_count,
            "signed": False,
            "authenticode_status": "not_performed",
            "files": [record.to_dict() for record in self.files],
        }


def windows_installer_payload_files(root_dir: Path) -> tuple[Path, ...]:
    """Return sorted runtime files that an installer may copy."""

    candidates = tuple(
        relative
        for relative in release_candidate_files(root_dir)
        if relative.parts[0] != "tests"
    )
    present = {relative.as_posix() for relative in candidates}
    missing = sorted(_REQUIRED_PAYLOAD_FILES - present)
    if missing:
        raise PcAgentError(
            "Windows installer source is missing required files: "
            + ", ".join(missing)
            + "."
        )
    return candidates


def _read_bounded(path: Path, *, limit: int, label: str) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise PcAgentError(f"{label} was not found: {path}.")
    size = path.stat().st_size
    if size > limit:
        raise PcAgentError(f"{label} exceeds the {limit}-byte limit.")
    data = path.read_bytes()
    if len(data) != size:
        raise PcAgentError(f"{label} changed while reading: {path}.")
    return data


def _check_template(template: str) -> None:
    required_fragments = (
        f"AppId={{{{{WINDOWS_INSTALLER_APP_ID}}}",
        'AppName={#MyAppName}',
        "PrivilegesRequired=lowest",
        r"DefaultDirName={localappdata}\Programs\Self-Healing PC Agent",
        r"scripts\install_windows.ps1",
        r"scripts\uninstall_windows.ps1",
        "-RemoveVirtualEnvironment",
        r"scripts\launch_gui.cmd",
        r"scripts\launch_cli.cmd",
    )
    missing = [fragment for fragment in required_fragments if fragment not in template]
    if template.count(_VERSION_TOKEN) != 1 or template.count(_FILES_TOKEN) != 1:
        missing.append("exactly one version/files build token")
    if "PrivilegesRequired=admin" in template:
        missing.append("non-administrative install mode")
    if missing:
        raise PcAgentError(
            "Windows installer template is incomplete: " + "; ".join(missing) + "."
        )


def _render_installer_script(
    template: str,
    *,
    version: str,
    payload_files: tuple[Path, ...],
) -> str:
    if _VERSION_RE.fullmatch(version) is None:
        raise PcAgentError(f"Invalid Windows installer version: {version!r}.")
    _check_template(template)
    lines: list[str] = []
    for relative in payload_files:
        posix = relative.as_posix()
        if '"' in posix or "\n" in posix or "\r" in posix:
            raise PcAgentError(f"Installer payload path is unsafe: {posix!r}.")
        source = "payload\\" + posix.replace("/", "\\")
        parent = relative.parent.as_posix()
        destination = "{app}" if parent == "." else "{app}\\" + parent.replace("/", "\\")
        lines.append(
            f'Source: "{source}"; DestDir: "{destination}"; Flags: ignoreversion'
        )
    rendered = template.replace(_VERSION_TOKEN, version).replace(
        _FILES_TOKEN, "\n".join(lines)
    )
    if _VERSION_TOKEN in rendered or _FILES_TOKEN in rendered:
        raise PcAgentError("Windows installer template tokens were not replaced.")
    return rendered


def _validate_output_path(root: Path, output: Path) -> Path:
    destination = output.expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise PcAgentError(
            f"Windows installer source output already exists: {destination}."
        )
    if destination == root or destination in root.parents:
        raise PcAgentError("Windows installer source output must not contain the project root.")
    try:
        relative = destination.relative_to(root)
    except ValueError:
        return destination
    if not relative.parts or relative.parts[0] != "workspace":
        raise PcAgentError(
            "Windows installer source output inside the project must be under workspace/."
        )
    return destination


def build_windows_installer_source(
    root_dir: Path,
    output_dir: Path,
) -> WindowsInstallerSourceInfo:
    """Build and verify a hash-manifested Inno Setup source directory."""

    root = root_dir.expanduser().resolve()
    destination = _validate_output_path(root, output_dir)
    payload_relatives = windows_installer_payload_files(root)
    if len(payload_relatives) + 1 > _MAX_INSTALLER_FILES:
        raise PcAgentError("Windows installer source would contain too many files.")

    contents: list[tuple[str, bytes]] = []
    records: list[ReleaseFileRecord] = []
    total = 0
    for relative in payload_relatives:
        data = _read_bounded(
            root / relative,
            limit=_MAX_INSTALLER_FILE_BYTES,
            label="Windows installer payload file",
        )
        total += len(data)
        if total > _MAX_INSTALLER_TOTAL_BYTES:
            raise PcAgentError("Windows installer source exceeds the total size limit.")
        package_path = f"payload/{relative.as_posix()}"
        contents.append((package_path, data))
        records.append(
            ReleaseFileRecord(package_path, len(data), _sha256_bytes(data))
        )

    template_data = _read_bounded(
        root / WINDOWS_INSTALLER_TEMPLATE,
        limit=_MAX_INSTALLER_SCRIPT_BYTES,
        label="Windows installer template",
    )
    try:
        template = template_data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PcAgentError("Windows installer template is not valid UTF-8.") from exc
    script = _render_installer_script(
        template,
        version=__version__,
        payload_files=payload_relatives,
    )
    script_data = script.encode("utf-8")
    if len(script_data) > _MAX_INSTALLER_SCRIPT_BYTES:
        raise PcAgentError("Generated Windows installer script exceeds the size limit.")
    total += len(script_data)
    if total > _MAX_INSTALLER_TOTAL_BYTES:
        raise PcAgentError("Windows installer source exceeds the total size limit.")
    records.append(
        ReleaseFileRecord(
            WINDOWS_INSTALLER_SCRIPT,
            len(script_data),
            _sha256_bytes(script_data),
        )
    )
    records_tuple = tuple(sorted(records, key=lambda record: record.path))
    contents.append((WINDOWS_INSTALLER_SCRIPT, script_data))

    manifest = {
        "format": _MANIFEST_FORMAT,
        "app": WINDOWS_INSTALLER_APP_NAME,
        "app_id": WINDOWS_INSTALLER_APP_ID,
        "version": __version__,
        "file_count": len(records_tuple),
        "total_bytes": total,
        "files": [record.to_dict() for record in records_tuple],
    }
    manifest_data = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(manifest_data) > _MAX_INSTALLER_MANIFEST_BYTES:
        raise PcAgentError("Windows installer manifest exceeds the size limit.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.tmp-{secrets.token_hex(4)}"
    )
    try:
        for package_path, data in contents:
            target = temporary / package_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (temporary / WINDOWS_INSTALLER_MANIFEST).write_bytes(manifest_data)
        verify_windows_installer_source(temporary)
        if destination.exists():
            raise PcAgentError(
                f"Windows installer source output already exists: {destination}."
            )
        temporary.replace(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return verify_windows_installer_source(destination)


def _parse_manifest(data: bytes) -> dict[str, Any]:
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PcAgentError("Windows installer manifest is not valid JSON.") from exc
    expected_keys = {
        "format",
        "app",
        "app_id",
        "version",
        "file_count",
        "total_bytes",
        "files",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected_keys:
        raise PcAgentError("Windows installer manifest has an unexpected shape.")
    if manifest["format"] != _MANIFEST_FORMAT:
        raise PcAgentError("Windows installer manifest format is unsupported.")
    if manifest["app"] != WINDOWS_INSTALLER_APP_NAME:
        raise PcAgentError("Windows installer manifest app name is unexpected.")
    if manifest["app_id"] != WINDOWS_INSTALLER_APP_ID:
        raise PcAgentError("Windows installer manifest AppId is unexpected.")
    if manifest["version"] != __version__:
        raise PcAgentError(
            "Windows installer manifest version does not match this application."
        )
    if not isinstance(manifest["file_count"], int) or not isinstance(
        manifest["total_bytes"], int
    ):
        raise PcAgentError("Windows installer manifest counts are invalid.")
    if not isinstance(manifest["files"], list):
        raise PcAgentError("Windows installer manifest files are invalid.")
    return manifest


def _manifest_records(manifest: dict[str, Any]) -> tuple[ReleaseFileRecord, ...]:
    raw_files = manifest["files"]
    if not raw_files or len(raw_files) > _MAX_INSTALLER_FILES:
        raise PcAgentError("Windows installer manifest file count is invalid.")
    records: list[ReleaseFileRecord] = []
    seen: set[str] = set()
    total = 0
    for raw in raw_files:
        if not isinstance(raw, dict) or set(raw) != {"path", "size", "sha256"}:
            raise PcAgentError("Windows installer manifest contains a malformed record.")
        raw_path = raw["path"]
        size = raw["size"]
        digest = raw["sha256"]
        if not isinstance(raw_path, str) or raw_path in seen:
            raise PcAgentError("Windows installer manifest contains a duplicate path.")
        relative = _validate_entry_name(raw_path)
        if raw_path != WINDOWS_INSTALLER_SCRIPT and relative.parts[0] != "payload":
            raise PcAgentError(
                f"Windows installer manifest contains an unsafe entry: {raw_path!r}."
            )
        if (
            not isinstance(size, int)
            or size < 0
            or size > _MAX_INSTALLER_FILE_BYTES
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise PcAgentError("Windows installer manifest contains an invalid record.")
        seen.add(raw_path)
        total += size
        records.append(ReleaseFileRecord(raw_path, size, digest))
    if manifest["file_count"] != len(records) or manifest["total_bytes"] != total:
        raise PcAgentError("Windows installer manifest totals do not match its files.")
    if total > _MAX_INSTALLER_TOTAL_BYTES:
        raise PcAgentError("Windows installer source exceeds the total size limit.")
    return tuple(sorted(records, key=lambda record: record.path))


def _payload_relatives(
    records: tuple[ReleaseFileRecord, ...],
) -> tuple[Path, ...]:
    relatives: list[Path] = []
    for record in records:
        if record.path == WINDOWS_INSTALLER_SCRIPT:
            continue
        relative = Path(record.path.removeprefix("payload/"))
        if relative.parts[0] == "tests" or _is_forbidden_release_path(relative):
            raise PcAgentError(
                f"Windows installer payload contains a forbidden file: {record.path}."
            )
        relatives.append(relative)
    present = {relative.as_posix() for relative in relatives}
    missing = sorted(_REQUIRED_PAYLOAD_FILES - present)
    if missing:
        raise PcAgentError(
            "Windows installer payload is missing required files: "
            + ", ".join(missing)
            + "."
        )
    return tuple(sorted(relatives, key=lambda relative: relative.as_posix()))


def _verify_installer_script(
    script_data: bytes,
    *,
    payload_files: tuple[Path, ...],
) -> None:
    try:
        script = script_data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PcAgentError("Windows installer script is not valid UTF-8.") from exc
    if _VERSION_TOKEN in script or _FILES_TOKEN in script:
        raise PcAgentError("Windows installer script still contains build tokens.")
    required = (
        f"AppId={{{{{WINDOWS_INSTALLER_APP_ID}}}",
        f'#define MyAppVersion "{__version__}"',
        "PrivilegesRequired=lowest",
        r"DefaultDirName={localappdata}\Programs\Self-Healing PC Agent",
        "UninstallDisplayName={#MyAppName}",
        r"scripts\install_windows.ps1",
        r"scripts\uninstall_windows.ps1",
        "-RemoveVirtualEnvironment",
        r"scripts\launch_gui.cmd",
        r"scripts\launch_cli.cmd",
    )
    missing = [fragment for fragment in required if fragment not in script]
    forbidden = (
        "PrivilegesRequired=admin",
        "Flags: external",
        "download",
        "SignTool=",
        "SignedUninstaller=",
    )
    present_forbidden = [fragment for fragment in forbidden if fragment in script]
    if missing or present_forbidden:
        raise PcAgentError(
            "Windows installer script failed safety checks; missing="
            f"{missing!r}, forbidden={present_forbidden!r}."
        )

    in_files = False
    entries: dict[str, str] = {}
    for raw_line in script.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_files = line.lower() == "[files]"
            continue
        if not in_files or not line or line.startswith(";"):
            continue
        match = re.fullmatch(
            r'Source: "([^"]+)"; DestDir: "([^"]+)"; Flags: ignoreversion',
            line,
        )
        if match is None:
            raise PcAgentError("Windows installer [Files] section is malformed.")
        source = match.group(1).replace("\\", "/")
        if source in entries:
            raise PcAgentError("Windows installer [Files] section has a duplicate.")
        entries[source] = match.group(2)

    expected: dict[str, str] = {}
    for relative in payload_files:
        source = f"payload/{relative.as_posix()}"
        parent = relative.parent.as_posix()
        destination = "{app}" if parent == "." else "{app}\\" + parent.replace("/", "\\")
        expected[source] = destination
    if entries != expected:
        raise PcAgentError(
            "Windows installer [Files] section does not match the payload manifest."
        )


def verify_windows_installer_source(
    source_dir: Path,
) -> WindowsInstallerSourceInfo:
    """Verify a Windows installer source directory without compiling it."""

    root = source_dir.expanduser().resolve()
    if not root.is_dir() or source_dir.is_symlink():
        raise PcAgentError(f"Windows installer source directory not found: {root}.")
    manifest_path = root / WINDOWS_INSTALLER_MANIFEST
    manifest_data = _read_bounded(
        manifest_path,
        limit=_MAX_INSTALLER_MANIFEST_BYTES,
        label="Windows installer manifest",
    )
    manifest = _parse_manifest(manifest_data)
    records = _manifest_records(manifest)
    payload_files = _payload_relatives(records)

    record_by_path = {record.path: record for record in records}
    script_record = record_by_path.get(WINDOWS_INSTALLER_SCRIPT)
    if script_record is None:
        raise PcAgentError("Windows installer script is missing from the manifest.")

    actual: set[str] = set()
    compiled_output_count = 0
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise PcAgentError(
                f"Windows installer source contains a symbolic link: {relative}."
            )
        if not path.is_file():
            continue
        posix = relative.as_posix()
        if posix.startswith("output/"):
            compiled_output_count += 1
            continue
        actual.add(posix)
    expected = set(record_by_path) | {WINDOWS_INSTALLER_MANIFEST}
    if actual != expected:
        raise PcAgentError(
            "Windows installer source inventory does not match its manifest."
        )

    script_data = b""
    for record in records:
        data = _read_bounded(
            root / record.path,
            limit=_MAX_INSTALLER_FILE_BYTES,
            label="Windows installer source file",
        )
        if len(data) != record.size or _sha256_bytes(data) != record.sha256:
            raise PcAgentError(
                f"Windows installer source file failed verification: {record.path}."
            )
        if record.path == WINDOWS_INSTALLER_SCRIPT:
            if len(data) > _MAX_INSTALLER_SCRIPT_BYTES:
                raise PcAgentError("Windows installer script exceeds the size limit.")
            script_data = data
    _verify_installer_script(script_data, payload_files=payload_files)

    return WindowsInstallerSourceInfo(
        path=root,
        version=str(manifest["version"]),
        app_id=str(manifest["app_id"]),
        file_count=len(records),
        payload_file_count=len(payload_files),
        total_bytes=int(manifest["total_bytes"]),
        script_sha256=script_record.sha256,
        manifest_sha256=_sha256_file(manifest_path),
        compiled_output_count=compiled_output_count,
        files=records,
    )
