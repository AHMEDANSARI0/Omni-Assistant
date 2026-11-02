"""Deterministic native Debian binary packages.

Phase 47 wraps the already verified Phase 41 POSIX package in a real
``.deb``.  The system part is intentionally small and has no maintainer
scripts: dpkg installs the POSIX package under
``/usr/share/self-healing-pc-agent`` plus `/usr/bin` launchers and a desktop
entry.  On first launch (or when the packaged version changes), the launcher
invokes the existing per-user POSIX installer, which preserves `.env`,
`data/`, workspace files and OS credentials while creating the user's
virtual environment.

The builder/verifier use only Python's standard library.  They do not use
administrator privileges, run `dpkg`, download Python packages, sign a
repository, or claim that the first-launch Python dependencies are bundled
offline; the existing POSIX installer performs that per-user setup when a
user actually launches the application.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import secrets
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .errors import PcAgentError
from .posix_package import build_posix_package, verify_posix_package
from .release import _sha256_bytes, _sha256_file

DEBIAN_PACKAGE_NAME = "self-healing-pc-agent"
DEBIAN_PACKAGE_APP_NAME = "Self-Healing PC Agent"
DEBIAN_PACKAGE_ARCHITECTURE = "all"
DEBIAN_PACKAGE_REVISION = "1"
DEBIAN_PACKAGE_MANIFEST = "usr/share/self-healing-pc-agent/debian-package-manifest.json"
DEBIAN_PACKAGE_SYSTEM_ROOT = "usr/share/self-healing-pc-agent"
DEBIAN_PACKAGE_POSIX_ROOT = f"{DEBIAN_PACKAGE_SYSTEM_ROOT}/posix-package"
DEBIAN_PACKAGE_CLI_LAUNCHER = "usr/bin/self-healing-pc-agent"
DEBIAN_PACKAGE_GUI_LAUNCHER = "usr/bin/self-healing-pc-agent-gui"
DEBIAN_PACKAGE_DESKTOP_ENTRY = (
    "usr/share/applications/self-healing-pc-agent.desktop"
)
DEBIAN_PACKAGE_README = "usr/share/doc/self-healing-pc-agent/README.Debian"
DEBIAN_PACKAGE_CHANGELOG = (
    "usr/share/doc/self-healing-pc-agent/changelog.gz"
)
DEFAULT_DEBIAN_MAINTAINER = (
    "Self-Healing PC Agent project <self-healing-pc-agent@localhost>"
)

_MANIFEST_FORMAT = 1
_MAX_DEBIAN_FILES = 5_000
_MAX_DEBIAN_TOTAL_BYTES = 250_000_000
_MAX_DEBIAN_FILE_BYTES = 25_000_000
_MAX_DEBIAN_PACKAGE_BYTES = 250_000_000
_MAX_DEBIAN_CONTROL_BYTES = 1_000_000
_MAX_DEBIAN_MANIFEST_BYTES = 5_000_000
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MD5_RE = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True, slots=True)
class DebianPackageInfo:
    path: Path
    version: str
    package_version: str
    architecture: str
    file_count: int
    payload_file_count: int
    total_bytes: int
    package_bytes: int
    package_sha256: str
    manifest_sha256: str
    control_sha256: str
    maintainer: str
    files: tuple[dict[str, Any], ...] = ()

    def summary(self) -> str:
        return (
            f"version={self.version}; package={self.package_version}; "
            f"architecture={self.architecture}; files={self.file_count}; "
            f"payload={self.payload_file_count}; bytes={self.total_bytes}; "
            f"package_sha256={self.package_sha256[:16]}..."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "app": DEBIAN_PACKAGE_APP_NAME,
            "package": DEBIAN_PACKAGE_NAME,
            "version": self.version,
            "package_version": self.package_version,
            "architecture": self.architecture,
            "file_count": self.file_count,
            "payload_file_count": self.payload_file_count,
            "total_bytes": self.total_bytes,
            "package_bytes": self.package_bytes,
            "package_sha256": self.package_sha256,
            "manifest_sha256": self.manifest_sha256,
            "control_sha256": self.control_sha256,
            "maintainer": self.maintainer,
            "native_package_format": "deb",
            "signed": False,
            "repository_signed": False,
            "files": list(self.files),
        }


def debian_package_version(version: str = __version__) -> str:
    return f"{version}-{DEBIAN_PACKAGE_REVISION}"


def _validate_output_path(root: Path, output: Path) -> Path:
    destination = output.expanduser().resolve()
    if destination.suffix != ".deb":
        raise PcAgentError("Debian package output must use the .deb suffix.")
    if destination.exists() or destination.is_symlink():
        raise PcAgentError(f"Debian package output already exists: {destination}.")
    if destination == root or destination in root.parents:
        raise PcAgentError("Debian package output must not be the project root.")
    try:
        relative = destination.relative_to(root)
    except ValueError:
        return destination
    if not relative.parts or relative.parts[0] != "workspace":
        raise PcAgentError(
            "Debian package output inside the project must be under workspace/."
        )
    return destination


def _validate_maintainer(value: str) -> str:
    maintainer = value.strip()
    if (
        not maintainer
        or "\n" in maintainer
        or "\r" in maintainer
        or not maintainer.endswith(">")
        or " <" not in maintainer
        or maintainer.index(" <") < 1
    ):
        raise PcAgentError(
            "Debian maintainer must use the form 'Name <email-or-local-address>'."
        )
    return maintainer


def _launcher_bytes(*, gui: bool) -> bytes:
    target_script = "launch_gui.sh" if gui else "launch_cli.sh"
    label = "GUI" if gui else "CLI"
    script = f'''#!/bin/sh
# Self-Healing PC Agent Debian {label} launcher.
#
# The .deb installs only the verified POSIX package under /usr/share. This
# launcher runs that package's existing per-user installer on first use or
# after a version change, preserving .env, data, workspace files and OS
# credentials, then delegates to the per-user application launcher.
set -eu

PACKAGE_ROOT=${{PC_AGENT_DEB_PACKAGE_ROOT:-/usr/share/self-healing-pc-agent}}
POSIX_PACKAGE="$PACKAGE_ROOT/posix-package"
EXPECTED_VERSION="{__version__}"

if [ -n "${{PC_AGENT_HOME:-}}" ]; then
  TARGET_DIR=$PC_AGENT_HOME
else
  if [ -z "${{HOME:-}}" ]; then
    printf '%s\n' "HOME is not set; cannot choose a per-user install location." >&2
    exit 1
  fi
  TARGET_DIR="${{XDG_DATA_HOME:-$HOME/.local/share}}/self-healing-pc-agent"
fi

INSTALLED_VERSION=""
if [ -f "$TARGET_DIR/.pc-agent-posix-install.txt" ]; then
  INSTALLED_VERSION=$(sed -n 's/^version=//p' "$TARGET_DIR/.pc-agent-posix-install.txt" | head -n 1)
fi

if [ "$INSTALLED_VERSION" != "$EXPECTED_VERSION" ] || \
   [ ! -x "$TARGET_DIR/.venv/bin/pc-agent" ] || \
   [ ! -x "$TARGET_DIR/.venv/bin/pc-agent-gui" ] || \
   [ ! -x "$TARGET_DIR/scripts/{target_script}" ]; then
  printf '%s\n' "Preparing Self-Healing PC Agent $EXPECTED_VERSION for this user..." >&2
  sh "$POSIX_PACKAGE/install.sh" --prefix "$TARGET_DIR"
fi

exec "$TARGET_DIR/scripts/{target_script}" "$@"
'''
    return script.encode()


def _desktop_entry_bytes() -> bytes:
    return (
        b"[Desktop Entry]\n"
        b"Type=Application\n"
        b"Version=1.0\n"
        b"Name=Self-Healing PC Agent\n"
        b"GenericName=Local PC Assistant\n"
        b"Comment=Local, approval-gated personal PC assistant\n"
        b"Exec=/usr/bin/self-healing-pc-agent-gui\n"
        b"TryExec=/usr/bin/self-healing-pc-agent-gui\n"
        b"Icon=utilities-terminal\n"
        b"Terminal=false\n"
        b"Categories=Office;Utility;\n"
        b"Keywords=assistant;automation;documents;pdf;\n"
    )


def _readme_debian_bytes() -> bytes:
    return (
        f"Self-Healing PC Agent {__version__} for Debian\n"
        "==========================================\n\n"
        "This architecture-independent package installs the verified POSIX\n"
        "package under /usr/share/self-healing-pc-agent, command launchers\n"
        "under /usr/bin, and a desktop entry under /usr/share/applications.\n"
        "It contains no maintainer scripts.\n\n"
        "The application keeps its existing per-user root model. On first\n"
        "launch, or after the packaged version changes, the system launcher\n"
        "runs the bundled POSIX installer for the current user. That creates\n"
        "or refreshes ${XDG_DATA_HOME:-~/.local/share}/self-healing-pc-agent\n"
        "(or $PC_AGENT_HOME), creates .venv, installs the bundled local\n"
        "project and its Python dependencies, and runs doctor plus the GUI\n"
        "environment check. Python dependency installation may require\n"
        "network access on first launch; this .deb does not bundle wheels.\n\n"
        "Removing the Debian package removes only the system payload,\n"
        "launchers and desktop entry. The per-user application copy, .env,\n"
        "data, workspace files and OS credential entries are preserved.\n"
        "Use the per-user POSIX uninstaller separately if those generated\n"
        "launchers or .venv should also be removed.\n"
    ).encode()


def _changelog_gz_bytes(root: Path) -> bytes:
    changelog = root / "CHANGELOG.md"
    if not changelog.is_file() or changelog.is_symlink():
        raise PcAgentError("Project CHANGELOG.md was not found for the Debian package.")
    data = changelog.read_bytes()
    if len(data) > _MAX_DEBIAN_FILE_BYTES:
        raise PcAgentError("Project changelog exceeds the Debian package file limit.")
    return gzip.compress(data, compresslevel=9, mtime=0)


def _installed_size_kib(files: dict[str, tuple[bytes, int]]) -> int:
    directories: set[str] = set()
    total = 0
    for path, (data, _mode) in files.items():
        total += max(1, (len(data) + 1023) // 1024)
        parts = Path(path).parts[:-1]
        for index in range(1, len(parts) + 1):
            directories.add("/".join(parts[:index]))
    return total + len(directories)


def _control_bytes(*, maintainer: str, installed_size: int) -> bytes:
    fields = [
        f"Package: {DEBIAN_PACKAGE_NAME}",
        f"Version: {debian_package_version()}",
        "Section: utils",
        "Priority: optional",
        f"Architecture: {DEBIAN_PACKAGE_ARCHITECTURE}",
        "Depends: python3 (>= 3.11), python3-venv, python3-tk",
        "Recommends: ca-certificates, secret-tool",
        f"Installed-Size: {installed_size}",
        f"Maintainer: {maintainer}",
        "Description: Local approval-gated personal PC assistant",
        " Self-Healing PC Agent is a local personal PC assistant with",
        " deterministic document, file, provider and recovery workflows.",
        " .",
        " This architecture-independent Debian package installs the verified",
        " POSIX package and launchers. First launch prepares the existing",
        " per-user application environment; it may require network access",
        " for Python dependencies because wheels are not bundled in the .deb.",
    ]
    return ("\n".join(fields) + "\n").encode()


def _tar_bytes(entries: dict[str, tuple[bytes, int]]) -> bytes:
    """Create a deterministic ustar archive from relative package paths."""

    directories: set[str] = set()
    for path in entries:
        parts = Path(path).parts[:-1]
        for index in range(1, len(parts) + 1):
            directories.add("/".join(parts[:index]))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for directory in sorted(directories):
            info = tarfile.TarInfo(f"./{directory}/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            archive.addfile(info)
        for path in sorted(entries):
            data, mode = entries[path]
            info = tarfile.TarInfo(f"./{path}")
            info.size = len(data)
            info.mode = mode
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _gzip_bytes(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=9, mtime=0)


def _ar_member(name: str, data: bytes) -> bytes:
    encoded_name = f"{name}/"
    if len(encoded_name) > 16:
        raise PcAgentError(f"Debian ar member name is too long: {name}.")
    header = (
        f"{encoded_name:<16}"
        f"{0:<12}"
        f"{0:<6}"
        f"{0:<6}"
        f"{0o100644:<8}"
        f"{len(data):<10}"
        "`\n"
    ).encode("ascii")
    if len(header) != 60:
        raise PcAgentError("Debian ar member header has an invalid length.")
    padding = b"\n" if len(data) % 2 else b""
    return header + data + padding


def _ar_bytes(control_tgz: bytes, data_tgz: bytes) -> bytes:
    return b"".join(
        (
            b"!<arch>\n",
            _ar_member("debian-binary", b"2.0\n"),
            _ar_member("control.tar.gz", control_tgz),
            _ar_member("data.tar.gz", data_tgz),
        )
    )


def _posix_package_mode(relative: str) -> int:
    executable = {
        "install.sh",
        "uninstall.sh",
        "payload/scripts/install_posix.sh",
        "payload/scripts/uninstall_posix.sh",
        "payload/scripts/launch_gui.sh",
        "payload/scripts/launch_cli.sh",
    }
    return 0o755 if relative in executable else 0o644


def build_debian_package(
    root_dir: Path,
    output_path: Path,
    *,
    maintainer: str = DEFAULT_DEBIAN_MAINTAINER,
) -> DebianPackageInfo:
    """Build and verify a deterministic native Debian binary package."""

    root = root_dir.expanduser().resolve()
    destination = _validate_output_path(root, output_path)
    maintainer = _validate_maintainer(maintainer)
    destination.parent.mkdir(parents=True, exist_ok=True)

    data_files: dict[str, tuple[bytes, int]] = {}
    with tempfile.TemporaryDirectory(
        prefix=".pc-agent-debian-", dir=destination.parent
    ) as temporary_name:
        temporary_root = Path(temporary_name)
        posix_output = temporary_root / "posix-package"
        posix_info = build_posix_package(root, posix_output)
        for path in sorted(posix_output.rglob("*")):
            if path.is_symlink():
                raise PcAgentError("POSIX package contains a symbolic link.")
            if not path.is_file():
                continue
            relative = path.relative_to(posix_output).as_posix()
            data = path.read_bytes()
            if len(data) > _MAX_DEBIAN_FILE_BYTES:
                raise PcAgentError("Debian package payload file exceeds the size limit.")
            data_files[f"{DEBIAN_PACKAGE_POSIX_ROOT}/{relative}"] = (
                data,
                _posix_package_mode(relative),
            )

    generated = {
        DEBIAN_PACKAGE_CLI_LAUNCHER: (_launcher_bytes(gui=False), 0o755),
        DEBIAN_PACKAGE_GUI_LAUNCHER: (_launcher_bytes(gui=True), 0o755),
        DEBIAN_PACKAGE_DESKTOP_ENTRY: (_desktop_entry_bytes(), 0o644),
        DEBIAN_PACKAGE_README: (_readme_debian_bytes(), 0o644),
        DEBIAN_PACKAGE_CHANGELOG: (_changelog_gz_bytes(root), 0o644),
    }
    data_files.update(generated)
    if len(data_files) + 1 > _MAX_DEBIAN_FILES:
        raise PcAgentError("Debian package would contain too many files.")
    total = sum(len(data) for data, _mode in data_files.values())
    if total > _MAX_DEBIAN_TOTAL_BYTES:
        raise PcAgentError("Debian package exceeds the total size limit.")

    records = [
        {
            "path": path,
            "size": len(data),
            "sha256": _sha256_bytes(data),
            "mode": mode,
        }
        for path, (data, mode) in sorted(data_files.items())
    ]
    manifest = {
        "format": _MANIFEST_FORMAT,
        "app": DEBIAN_PACKAGE_APP_NAME,
        "package": DEBIAN_PACKAGE_NAME,
        "version": __version__,
        "package_version": debian_package_version(),
        "architecture": DEBIAN_PACKAGE_ARCHITECTURE,
        "file_count": len(records),
        "payload_file_count": posix_info.payload_file_count,
        "total_bytes": total,
        "posix_manifest_sha256": posix_info.manifest_sha256,
        "files": records,
    }
    manifest_data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    if len(manifest_data) > _MAX_DEBIAN_MANIFEST_BYTES:
        raise PcAgentError("Debian package manifest exceeds the size limit.")
    data_files[DEBIAN_PACKAGE_MANIFEST] = (manifest_data, 0o644)

    control_data = _control_bytes(
        maintainer=maintainer,
        installed_size=_installed_size_kib(data_files),
    )
    md5sums_data = "".join(
        f"{hashlib.md5(data, usedforsecurity=False).hexdigest()}  {path}\n"
        for path, (data, _mode) in sorted(data_files.items())
    ).encode()
    control_tgz = _gzip_bytes(
        _tar_bytes(
            {
                "control": (control_data, 0o644),
                "md5sums": (md5sums_data, 0o644),
            }
        )
    )
    data_tgz = _gzip_bytes(_tar_bytes(data_files))
    package_data = _ar_bytes(control_tgz, data_tgz)
    if len(package_data) > _MAX_DEBIAN_PACKAGE_BYTES:
        raise PcAgentError("Debian package exceeds the package size limit.")

    temporary = destination.with_name(
        f".{destination.stem}.tmp-{secrets.token_hex(4)}.deb"
    )
    try:
        temporary.write_bytes(package_data)
        verify_debian_package(temporary)
        if destination.exists():
            raise PcAgentError(f"Debian package output already exists: {destination}.")
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return verify_debian_package(destination)


def _read_package(path: Path) -> bytes:
    candidate = path.expanduser().resolve()
    if candidate.suffix != ".deb" or not candidate.is_file() or candidate.is_symlink():
        raise PcAgentError(f"Debian package was not found: {candidate}.")
    size = candidate.stat().st_size
    if size > _MAX_DEBIAN_PACKAGE_BYTES:
        raise PcAgentError("Debian package exceeds the package size limit.")
    data = candidate.read_bytes()
    if len(data) != size:
        raise PcAgentError(f"Debian package changed while reading: {candidate}.")
    return data


def _parse_ar(data: bytes) -> dict[str, bytes]:
    if not data.startswith(b"!<arch>\n"):
        raise PcAgentError("Debian package is not an ar archive.")
    members: dict[str, bytes] = {}
    offset = 8
    while offset < len(data):
        if offset + 60 > len(data):
            raise PcAgentError("Debian package has a truncated ar header.")
        header = data[offset : offset + 60]
        if header[58:60] != b"`\n":
            raise PcAgentError("Debian package has an invalid ar header.")
        try:
            raw_name = header[:16].decode("ascii")
            size = int(header[48:58].decode("ascii").strip())
        except (UnicodeDecodeError, ValueError) as exc:
            raise PcAgentError("Debian package has a malformed ar header.") from exc
        name = raw_name.strip().removesuffix("/")
        if not name or name in members or size < 0:
            raise PcAgentError("Debian package has a duplicate or invalid ar member.")
        start = offset + 60
        end = start + size
        if end > len(data):
            raise PcAgentError("Debian package has a truncated ar member.")
        members[name] = data[start:end]
        offset = end + (size % 2)
    if list(members) != ["debian-binary", "control.tar.gz", "data.tar.gz"]:
        raise PcAgentError("Debian package ar members are incomplete or out of order.")
    if members["debian-binary"] != b"2.0\n":
        raise PcAgentError("Debian package format version is unsupported.")
    return members


def _gunzip_bounded(data: bytes, *, limit: int, label: str) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            output = stream.read(limit + 1)
    except (OSError, EOFError) as exc:
        raise PcAgentError(f"Debian package {label} is not valid gzip data.") from exc
    if len(output) > limit:
        raise PcAgentError(f"Debian package {label} exceeds the size limit.")
    return output


def _parse_tar(
    data: bytes,
    *,
    label: str,
) -> dict[str, tuple[bytes, int]]:
    files: dict[str, tuple[bytes, int]] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive.getmembers():
                raw_name = member.name
                name = raw_name.removeprefix("./")
                name = name.rstrip("/")
                if (
                    not name
                    or name.startswith("/")
                    or ".." in Path(name).parts
                    or "\\" in name
                ):
                    raise PcAgentError(
                        f"Debian package {label} contains an unsafe path: {raw_name!r}."
                    )
                if member.isdir():
                    continue
                if not member.isfile() or member.issym() or member.islnk():
                    raise PcAgentError(
                        f"Debian package {label} contains a non-file entry: {raw_name!r}."
                    )
                if name in files:
                    raise PcAgentError(
                        f"Debian package {label} contains a duplicate file: {name}."
                    )
                if member.size < 0 or member.size > _MAX_DEBIAN_FILE_BYTES:
                    raise PcAgentError(
                        f"Debian package {label} contains an oversized file: {name}."
                    )
                if member.mode & 0o7777 not in (0o644, 0o755):
                    raise PcAgentError(
                        f"Debian package {label} contains an unsafe file mode: {name}."
                    )
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise PcAgentError(
                        f"Debian package {label} file could not be read: {name}."
                    )
                content = extracted.read(member.size + 1)
                if len(content) != member.size:
                    raise PcAgentError(
                        f"Debian package {label} file changed while reading: {name}."
                    )
                total += len(content)
                if total > _MAX_DEBIAN_TOTAL_BYTES:
                    raise PcAgentError("Debian package exceeds the total size limit.")
                files[name] = (content, member.mode & 0o7777)
    except tarfile.TarError as exc:
        raise PcAgentError(f"Debian package {label} is not a valid tar archive.") from exc
    if len(files) > _MAX_DEBIAN_FILES:
        raise PcAgentError("Debian package contains too many files.")
    return files


def _parse_control(data: bytes) -> dict[str, str]:
    if len(data) > _MAX_DEBIAN_CONTROL_BYTES:
        raise PcAgentError("Debian control file exceeds the size limit.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PcAgentError("Debian control file is not valid UTF-8.") from exc
    fields: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith(" "):
            if current is None:
                raise PcAgentError("Debian control file has an orphan continuation line.")
            fields[current] += "\n" + line[1:]
            continue
        if ":" not in line:
            raise PcAgentError("Debian control file contains a malformed line.")
        name, value = line.split(":", 1)
        if name in fields:
            raise PcAgentError(f"Debian control file duplicates field {name}.")
        fields[name] = value.strip()
        current = name
    return fields


def _parse_manifest(data: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if len(data) > _MAX_DEBIAN_MANIFEST_BYTES:
        raise PcAgentError("Debian package manifest exceeds the size limit.")
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PcAgentError("Debian package manifest is not valid JSON.") from exc
    expected = {
        "format",
        "app",
        "package",
        "version",
        "package_version",
        "architecture",
        "file_count",
        "payload_file_count",
        "total_bytes",
        "posix_manifest_sha256",
        "files",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected:
        raise PcAgentError("Debian package manifest has an unexpected shape.")
    if manifest["format"] != _MANIFEST_FORMAT:
        raise PcAgentError("Debian package manifest format is unsupported.")
    if manifest["app"] != DEBIAN_PACKAGE_APP_NAME:
        raise PcAgentError("Debian package manifest app is unexpected.")
    if manifest["package"] != DEBIAN_PACKAGE_NAME:
        raise PcAgentError("Debian package manifest package name is unexpected.")
    if manifest["version"] != __version__:
        raise PcAgentError("Debian package version does not match this application.")
    if manifest["package_version"] != debian_package_version():
        raise PcAgentError("Debian package control version is unexpected.")
    if manifest["architecture"] != DEBIAN_PACKAGE_ARCHITECTURE:
        raise PcAgentError("Debian package architecture is unexpected.")
    if not isinstance(manifest["files"], list):
        raise PcAgentError("Debian package manifest files are invalid.")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    for raw in manifest["files"]:
        if not isinstance(raw, dict) or set(raw) != {"path", "size", "sha256", "mode"}:
            raise PcAgentError("Debian package manifest contains a malformed record.")
        path = raw["path"]
        size = raw["size"]
        digest = raw["sha256"]
        mode = raw["mode"]
        if (
            not isinstance(path, str)
            or path in seen
            or not path.startswith("usr/")
            or ".." in Path(path).parts
            or not isinstance(size, int)
            or size < 0
            or size > _MAX_DEBIAN_FILE_BYTES
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            or mode not in (0o644, 0o755)
        ):
            raise PcAgentError("Debian package manifest contains an invalid record.")
        seen.add(path)
        total += size
        records.append(raw)
    if manifest["file_count"] != len(records) or manifest["total_bytes"] != total:
        raise PcAgentError("Debian package manifest totals do not match its files.")
    if (
        not isinstance(manifest["payload_file_count"], int)
        or manifest["payload_file_count"] < 1
        or not isinstance(manifest["posix_manifest_sha256"], str)
        or _SHA256_RE.fullmatch(manifest["posix_manifest_sha256"]) is None
    ):
        raise PcAgentError("Debian package manifest POSIX summary is invalid.")
    return manifest, records


def _verify_launcher(data: bytes, *, gui: bool) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PcAgentError("Debian launcher is not valid UTF-8.") from exc
    target_script = "launch_gui.sh" if gui else "launch_cli.sh"
    required = (
        "#!/bin/sh",
        "set -eu",
        "PC_AGENT_DEB_PACKAGE_ROOT:-/usr/share/self-healing-pc-agent",
        f'EXPECTED_VERSION="{__version__}"',
        'sh "$POSIX_PACKAGE/install.sh" --prefix "$TARGET_DIR"',
        f'exec "$TARGET_DIR/scripts/{target_script}" "$@"',
    )
    if any(fragment not in text for fragment in required):
        raise PcAgentError("Debian launcher wiring is incomplete.")
    forbidden = ("sudo ", "curl ", "wget ", "eval ", "rm -rf", "chmod 777")
    if any(fragment in text for fragment in forbidden):
        raise PcAgentError("Debian launcher contains a forbidden command.")


def _verify_embedded_posix_package(
    data_files: dict[str, tuple[bytes, int]],
    manifest: dict[str, Any],
) -> None:
    prefix = f"{DEBIAN_PACKAGE_POSIX_ROOT}/"
    with tempfile.TemporaryDirectory(prefix="pc-agent-debian-verify-") as name:
        package_root = Path(name) / "posix-package"
        for path, (data, mode) in sorted(data_files.items()):
            if not path.startswith(prefix):
                continue
            relative = path.removeprefix(prefix)
            target = package_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(mode)
        info = verify_posix_package(package_root)
    if info.payload_file_count != manifest["payload_file_count"]:
        raise PcAgentError("Debian embedded POSIX payload count does not match.")
    if info.manifest_sha256 != manifest["posix_manifest_sha256"]:
        raise PcAgentError("Debian embedded POSIX manifest hash does not match.")


def verify_debian_package(package_path: Path) -> DebianPackageInfo:
    """Verify a Debian binary package without invoking dpkg or shell code."""

    candidate = package_path.expanduser().resolve()
    package_data = _read_package(candidate)
    members = _parse_ar(package_data)
    control_files = _parse_tar(
        _gunzip_bounded(
            members["control.tar.gz"],
            limit=_MAX_DEBIAN_CONTROL_BYTES * 4,
            label="control archive",
        ),
        label="control archive",
    )
    if set(control_files) != {"control", "md5sums"}:
        raise PcAgentError(
            "Debian control archive must contain exactly control and md5sums; "
            "maintainer scripts are not permitted."
        )
    control_data, control_mode = control_files["control"]
    if control_mode != 0o644:
        raise PcAgentError("Debian control file mode is unexpected.")
    control = _parse_control(control_data)
    expected_control = {
        "Package": DEBIAN_PACKAGE_NAME,
        "Version": debian_package_version(),
        "Section": "utils",
        "Priority": "optional",
        "Architecture": DEBIAN_PACKAGE_ARCHITECTURE,
    }
    for field, expected in expected_control.items():
        if control.get(field) != expected:
            raise PcAgentError(f"Debian control field {field} is unexpected.")
    maintainer = _validate_maintainer(str(control.get("Maintainer", "")))
    depends = str(control.get("Depends", ""))
    for fragment in ("python3 (>= 3.11)", "python3-venv", "python3-tk"):
        if fragment not in depends:
            raise PcAgentError("Debian control Depends field is incomplete.")
    if not str(control.get("Description", "")).startswith(
        "Local approval-gated personal PC assistant"
    ):
        raise PcAgentError("Debian control Description is unexpected.")
    try:
        installed_size = int(str(control.get("Installed-Size", "")))
    except ValueError as exc:
        raise PcAgentError("Debian control Installed-Size is invalid.") from exc
    if installed_size <= 0:
        raise PcAgentError("Debian control Installed-Size is invalid.")

    data_files = _parse_tar(
        _gunzip_bounded(
            members["data.tar.gz"],
            limit=_MAX_DEBIAN_TOTAL_BYTES,
            label="data archive",
        ),
        label="data archive",
    )
    for path in data_files:
        if not path.startswith("usr/"):
            raise PcAgentError(f"Debian data archive contains an unsafe root: {path}.")
    if DEBIAN_PACKAGE_MANIFEST not in data_files:
        raise PcAgentError("Debian package manifest is missing.")
    manifest_data, manifest_mode = data_files[DEBIAN_PACKAGE_MANIFEST]
    if manifest_mode != 0o644:
        raise PcAgentError("Debian package manifest mode is unexpected.")
    manifest, records = _parse_manifest(manifest_data)
    recorded_paths = {str(record["path"]) for record in records}
    if recorded_paths != set(data_files) - {DEBIAN_PACKAGE_MANIFEST}:
        raise PcAgentError("Debian package data inventory does not match its manifest.")
    for record in records:
        data, mode = data_files[str(record["path"])]
        if (
            len(data) != record["size"]
            or _sha256_bytes(data) != record["sha256"]
            or mode != record["mode"]
        ):
            raise PcAgentError(
                f"Debian package file failed verification: {record['path']}."
            )

    expected_md5 = {
        path: hashlib.md5(data, usedforsecurity=False).hexdigest()
        for path, (data, _mode) in data_files.items()
    }
    md5_text = control_files["md5sums"][0].decode("ascii", errors="strict")
    actual_md5: dict[str, str] = {}
    for line in md5_text.splitlines():
        digest, separator, path = line.partition("  ")
        if not separator or _MD5_RE.fullmatch(digest) is None or path in actual_md5:
            raise PcAgentError("Debian md5sums file is malformed.")
        actual_md5[path] = digest
    if actual_md5 != expected_md5:
        raise PcAgentError("Debian md5sums do not match the data archive.")
    if installed_size != _installed_size_kib(data_files):
        raise PcAgentError("Debian control Installed-Size does not match its files.")

    for path in (
        DEBIAN_PACKAGE_CLI_LAUNCHER,
        DEBIAN_PACKAGE_GUI_LAUNCHER,
        DEBIAN_PACKAGE_DESKTOP_ENTRY,
        DEBIAN_PACKAGE_README,
        DEBIAN_PACKAGE_CHANGELOG,
    ):
        if path not in data_files:
            raise PcAgentError(f"Debian package is missing required file: {path}.")
    _verify_launcher(data_files[DEBIAN_PACKAGE_CLI_LAUNCHER][0], gui=False)
    _verify_launcher(data_files[DEBIAN_PACKAGE_GUI_LAUNCHER][0], gui=True)
    desktop = data_files[DEBIAN_PACKAGE_DESKTOP_ENTRY][0].decode("utf-8")
    for fragment in (
        "[Desktop Entry]",
        "Type=Application",
        "Name=Self-Healing PC Agent",
        "Exec=/usr/bin/self-healing-pc-agent-gui",
        "TryExec=/usr/bin/self-healing-pc-agent-gui",
        "Terminal=false",
    ):
        if fragment not in desktop:
            raise PcAgentError("Debian desktop entry is incomplete.")
    _verify_embedded_posix_package(data_files, manifest)

    return DebianPackageInfo(
        path=candidate,
        version=__version__,
        package_version=debian_package_version(),
        architecture=DEBIAN_PACKAGE_ARCHITECTURE,
        file_count=len(data_files),
        payload_file_count=int(manifest["payload_file_count"]),
        total_bytes=sum(len(data) for data, _mode in data_files.values()),
        package_bytes=len(package_data),
        package_sha256=_sha256_file(candidate),
        manifest_sha256=_sha256_bytes(manifest_data),
        control_sha256=_sha256_bytes(control_data),
        maintainer=maintainer,
        files=tuple(records),
    )


__all__ = [
    "DEBIAN_PACKAGE_APP_NAME",
    "DEBIAN_PACKAGE_ARCHITECTURE",
    "DEBIAN_PACKAGE_CLI_LAUNCHER",
    "DEBIAN_PACKAGE_DESKTOP_ENTRY",
    "DEBIAN_PACKAGE_GUI_LAUNCHER",
    "DEBIAN_PACKAGE_MANIFEST",
    "DEBIAN_PACKAGE_NAME",
    "DEBIAN_PACKAGE_POSIX_ROOT",
    "DEFAULT_DEBIAN_MAINTAINER",
    "DebianPackageInfo",
    "build_debian_package",
    "debian_package_version",
    "verify_debian_package",
]
