"""Deterministic native RPM binary packages.

Phase 48 wraps the already verified Phase 41 POSIX package in a real RPM
v4 binary package, mirroring the Phase 47 Debian package for RPM-based
distributions.  The package carries no scriptlets: the payload installs the
POSIX package under ``/usr/share/self-healing-pc-agent`` plus `/usr/bin`
launchers and a desktop entry.  On first launch (or when the packaged
version changes), the launcher invokes the existing per-user POSIX
installer, which preserves `.env`, `data/`, workspace files and OS
credentials while creating the user's virtual environment.

The builder and verifier use only Python's standard library and follow the
documented RPM v4 on-disk format: a 96-byte Lead, a signature header with
an immutable region (tag 62) padded to eight bytes, a main header with an
immutable region (tag 63), and a gzip-compressed SVr4 "newc" cpio payload
whose entry order matches the header file list.  Nothing here needs
administrator privileges or the `rpm`/`rpmbuild` tools, downloads Python
packages, or creates OpenPGP signatures or repository metadata; the first
launch per-user setup may still need network access because Python wheels
are not bundled in the payload.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import secrets
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .errors import PcAgentError
from .posix_package import build_posix_package, verify_posix_package
from .release import _sha256_bytes, _sha256_file

RPM_PACKAGE_NAME = "self-healing-pc-agent"
RPM_PACKAGE_APP_NAME = "Self-Healing PC Agent"
RPM_PACKAGE_ARCHITECTURE = "noarch"
RPM_PACKAGE_RELEASE = "1"
RPM_PACKAGE_MANIFEST = "usr/share/self-healing-pc-agent/rpm-package-manifest.json"
RPM_PACKAGE_SYSTEM_ROOT = "usr/share/self-healing-pc-agent"
RPM_PACKAGE_POSIX_ROOT = f"{RPM_PACKAGE_SYSTEM_ROOT}/posix-package"
RPM_PACKAGE_CLI_LAUNCHER = "usr/bin/self-healing-pc-agent"
RPM_PACKAGE_GUI_LAUNCHER = "usr/bin/self-healing-pc-agent-gui"
RPM_PACKAGE_DESKTOP_ENTRY = "usr/share/applications/self-healing-pc-agent.desktop"
RPM_PACKAGE_README = "usr/share/doc/self-healing-pc-agent/README"
RPM_PACKAGE_CHANGELOG = "usr/share/doc/self-healing-pc-agent/CHANGELOG.md"
DEFAULT_RPM_PACKAGER = (
    "Self-Healing PC Agent project <self-healing-pc-agent@localhost>"
)

_MANIFEST_FORMAT = 1
_MAX_RPM_FILES = 5_000
_MAX_RPM_TOTAL_BYTES = 250_000_000
_MAX_RPM_FILE_BYTES = 25_000_000
_MAX_RPM_PACKAGE_BYTES = 250_000_000
_MAX_RPM_HEADER_BYTES = 32_000_000
_MAX_RPM_MANIFEST_BYTES = 5_000_000
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

_HEADER_MAGIC = b"\x8e\xad\xe8\x01\x00\x00\x00\x00"
_LEAD_MAGIC = b"\xed\xab\xee\xdb"
_LEAD_STRUCT = struct.Struct("!4sBBhh66shh16s")
_REGION_STRUCT = struct.Struct("!IIiI")
_ENTRY_STRUCT = struct.Struct("!IIiI")
_OWNED_ROOTS = ("/usr/share/self-healing-pc-agent", "/usr/share/doc/self-healing-pc-agent")

# RPM header data types.
_TYPE_INT16 = 3
_TYPE_INT32 = 4
_TYPE_INT64 = 5
_TYPE_STRING = 6
_TYPE_BIN = 7
_TYPE_STRING_ARRAY = 8
_TYPE_I18NSTRING = 9
_TYPE_ALIGN = {0: 1, 1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 1, 9: 1}
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1}

# Signature header tags.
_SIG_HEADERSIGNATURES = 62
_SIG_SHA1 = 269
_SIG_SHA256 = 273
_SIG_SIZE = 1000
_SIG_MD5 = 1004
_SIG_PAYLOADSIZE = 1007

# Main header tags.
_TAG_HEADERIMMUTABLE = 63
_TAG_HEADERI18NTABLE = 100
_TAG_NAME = 1000
_TAG_VERSION = 1001
_TAG_RELEASE = 1002
_TAG_SUMMARY = 1004
_TAG_DESCRIPTION = 1005
_TAG_BUILDTIME = 1006
_TAG_BUILDHOST = 1007
_TAG_SIZE = 1009
_TAG_VENDOR = 1011
_TAG_LICENSE = 1014
_TAG_PACKAGER = 1015
_TAG_GROUP = 1016
_TAG_OS = 1021
_TAG_ARCH = 1022
_TAG_FILESIZES = 1028
_TAG_FILEMODES = 1030
_TAG_FILERDEVS = 1033
_TAG_FILEMTIMES = 1034
_TAG_FILEDIGESTS = 1035
_TAG_FILELINKTOS = 1036
_TAG_FILEFLAGS = 1037
_TAG_FILEUSERNAME = 1039
_TAG_FILEGROUPNAME = 1040
_TAG_FILEVERIFYFLAGS = 1045
_TAG_ARCHIVESIZE = 1046
_TAG_PROVIDENAME = 1047
_TAG_REQUIREFLAGS = 1048
_TAG_REQUIRENAME = 1049
_TAG_REQUIREVERSION = 1050
_TAG_FILEDEVICES = 1095
_TAG_FILEINODES = 1096
_TAG_FILELANGS = 1097
_TAG_PROVIDEFLAGS = 1112
_TAG_PROVIDEVERSION = 1113
_TAG_DIRINDEXES = 1116
_TAG_BASENAMES = 1117
_TAG_DIRNAMES = 1118
_TAG_PAYLOADFORMAT = 1124
_TAG_PAYLOADCOMPRESSOR = 1125
_TAG_PAYLOADFLAGS = 1126
_TAG_FILEDIGESTALGO = 5011
_TAG_ENCODING = 5062

_SCRIPTLET_TAGS = frozenset(
    {
        1023,  # PREIN
        1024,  # POSTIN
        1025,  # PREUN
        1026,  # POSTUN
        1085,  # PREINPROG
        1086,  # POSTINPROG
        1087,  # PREUNPROG
        1088,  # POSTUNPROG
        1151,  # PRETRANS
        1152,  # POSTTRANS
        1153,  # PRETRANSPROG
        1154,  # POSTTRANSPROG
        5020,
        5021,
        5022,
        5023,
        5024,
        5025,
        5103,
        5104,
    }
)

# Dependency sense flags.
_DEP_LESS = 2
_DEP_GREATER = 4
_DEP_EQUAL = 8

_SUMMARY = "Local approval-gated personal PC assistant"
_DESCRIPTION = (
    "Self-Healing PC Agent is a local personal PC assistant with\n"
    "deterministic document, file, provider and recovery workflows.\n"
    "\n"
    "This architecture-independent RPM package installs the verified POSIX\n"
    "package and launchers without any scriptlets. First launch prepares\n"
    "the existing per-user application environment; it may require network\n"
    "access for Python dependencies because wheels are not bundled."
)
_GROUP = "Applications/Productivity"
_VENDOR = "Self-Healing PC Agent project"
_LICENSE = "Proprietary"


def _package_version() -> str:
    return f"{__version__}-{RPM_PACKAGE_RELEASE}"


def rpm_package_filename() -> str:
    return (
        f"{RPM_PACKAGE_NAME}-{__version__}-{RPM_PACKAGE_RELEASE}"
        f".{RPM_PACKAGE_ARCHITECTURE}.rpm"
    )


def _requires() -> list[tuple[str, int, str]]:
    return [
        ("/bin/sh", 0, ""),
        ("python3", _DEP_GREATER | _DEP_EQUAL, "3.11"),
        ("python3-tkinter", 0, ""),
        ("rpmlib(CompressedFileNames)", _DEP_LESS | _DEP_EQUAL, "3.0.4-1"),
        ("rpmlib(FileDigests)", _DEP_LESS | _DEP_EQUAL, "4.6.0-1"),
        ("rpmlib(PayloadFilesHavePrefix)", _DEP_LESS | _DEP_EQUAL, "4.0-1"),
    ]


@dataclass(frozen=True, slots=True)
class RpmPackageInfo:
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
    header_sha256: str
    packager: str
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
            "app": RPM_PACKAGE_APP_NAME,
            "package": RPM_PACKAGE_NAME,
            "version": self.version,
            "package_version": self.package_version,
            "architecture": self.architecture,
            "file_count": self.file_count,
            "payload_file_count": self.payload_file_count,
            "total_bytes": self.total_bytes,
            "package_bytes": self.package_bytes,
            "package_sha256": self.package_sha256,
            "manifest_sha256": self.manifest_sha256,
            "header_sha256": self.header_sha256,
            "packager": self.packager,
            "native_package_format": "rpm",
            "signed": False,
            "repository_signed": False,
            "files": list(self.files),
        }


def _validate_output_path(root: Path, output: Path) -> Path:
    destination = output.expanduser().resolve()
    if destination.suffix != ".rpm":
        raise PcAgentError("RPM package output must use the .rpm suffix.")
    if destination.exists() or destination.is_symlink():
        raise PcAgentError(f"RPM package output already exists: {destination}.")
    if destination == root or destination in root.parents:
        raise PcAgentError("RPM package output must not be the project root.")
    try:
        relative = destination.relative_to(root)
    except ValueError:
        return destination
    if not relative.parts or relative.parts[0] != "workspace":
        raise PcAgentError(
            "RPM package output inside the project must be under workspace/."
        )
    return destination


def _validate_packager(value: str) -> str:
    packager = value.strip()
    if (
        not packager
        or len(packager) > 200
        or "\n" in packager
        or "\r" in packager
        or not packager.endswith(">")
        or " <" not in packager
        or packager.index(" <") < 1
    ):
        raise PcAgentError(
            "RPM packager must use the form 'Name <email-or-local-address>'."
        )
    return packager


def _launcher_bytes(*, gui: bool) -> bytes:
    target_script = "launch_gui.sh" if gui else "launch_cli.sh"
    label = "GUI" if gui else "CLI"
    script = f'''#!/bin/sh
# Self-Healing PC Agent RPM {label} launcher.
#
# The .rpm installs only the verified POSIX package under /usr/share. This
# launcher runs that package's existing per-user installer on first use or
# after a version change, preserving .env, data, workspace files and OS
# credentials, then delegates to the per-user application launcher.
set -eu

PACKAGE_ROOT=${{PC_AGENT_RPM_PACKAGE_ROOT:-/usr/share/self-healing-pc-agent}}
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

if [ "$INSTALLED_VERSION" != "$EXPECTED_VERSION" ] || \\
   [ ! -x "$TARGET_DIR/.venv/bin/pc-agent" ] || \\
   [ ! -x "$TARGET_DIR/.venv/bin/pc-agent-gui" ] || \\
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


def _readme_rpm_bytes() -> bytes:
    return (
        f"Self-Healing PC Agent {__version__} for RPM-based systems\n"
        "=====================================================\n\n"
        "This architecture-independent (noarch) package installs the verified\n"
        "POSIX package under /usr/share/self-healing-pc-agent, command\n"
        "launchers under /usr/bin, and a desktop entry under\n"
        "/usr/share/applications. It contains no scriptlets, so rpm never\n"
        "runs project code as root during installation or removal.\n\n"
        "The application keeps its existing per-user root model. On first\n"
        "launch, or after the packaged version changes, the system launcher\n"
        "runs the bundled POSIX installer for the current user. That creates\n"
        "or refreshes ${XDG_DATA_HOME:-~/.local/share}/self-healing-pc-agent\n"
        "(or $PC_AGENT_HOME), creates .venv, installs the bundled local\n"
        "project and its Python dependencies, and runs doctor plus the GUI\n"
        "environment check. Python dependency installation may require\n"
        "network access on first launch; this .rpm does not bundle wheels.\n\n"
        "Removing the RPM package removes only the system payload, launchers\n"
        "and desktop entry. The per-user application copy, .env, data,\n"
        "workspace files and OS credential entries are preserved. Use the\n"
        "per-user POSIX uninstaller separately if those generated launchers\n"
        "or .venv should also be removed.\n"
    ).encode()


def _changelog_bytes(root: Path) -> bytes:
    changelog = root / "CHANGELOG.md"
    if not changelog.is_file() or changelog.is_symlink():
        raise PcAgentError("Project CHANGELOG.md was not found for the RPM package.")
    data = changelog.read_bytes()
    if len(data) > _MAX_RPM_FILE_BYTES:
        raise PcAgentError("Project changelog exceeds the RPM package file limit.")
    return data


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


def _gzip_bytes(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=9, mtime=0)


@dataclass(frozen=True, slots=True)
class _PayloadItem:
    path: str  # absolute install path, e.g. /usr/bin/self-healing-pc-agent
    is_dir: bool
    data: bytes
    mode: int  # permission bits only; type bits are derived from is_dir

    @property
    def full_mode(self) -> int:
        return (0o40755 if self.is_dir else 0o100000 | self.mode)


def _payload_items(data_files: dict[str, tuple[bytes, int]]) -> list[_PayloadItem]:
    directories: set[str] = set()
    for path in data_files:
        absolute = "/" + path
        parent = absolute.rsplit("/", 1)[0]
        while parent and parent != "/":
            if any(parent == root or parent.startswith(root + "/") for root in _OWNED_ROOTS):
                directories.add(parent)
            parent = parent.rsplit("/", 1)[0]
    items = [
        _PayloadItem(path=directory, is_dir=True, data=b"", mode=0o755)
        for directory in directories
    ]
    items.extend(
        _PayloadItem(path="/" + path, is_dir=False, data=data, mode=mode)
        for path, (data, mode) in data_files.items()
    )
    items.sort(key=lambda item: item.path)
    return items


def _encode_value(entry_type: int, value: Any) -> tuple[bytes, int]:
    if entry_type in (_TYPE_STRING, _TYPE_I18NSTRING):
        if not isinstance(value, str):
            raise PcAgentError("RPM header string value has the wrong type.")
        return value.encode() + b"\x00", 1
    if entry_type == _TYPE_STRING_ARRAY:
        values = list(value)
        return b"".join(item.encode() + b"\x00" for item in values), len(values)
    if entry_type == _TYPE_BIN:
        raw = bytes(value)
        return raw, len(raw)
    values = [value] if isinstance(value, int) else list(value)
    if entry_type == _TYPE_INT16:
        return b"".join(struct.pack("!H", item & 0xFFFF) for item in values), len(values)
    if entry_type == _TYPE_INT32:
        return b"".join(struct.pack("!I", item & 0xFFFFFFFF) for item in values), len(values)
    if entry_type == _TYPE_INT64:
        return b"".join(struct.pack("!Q", item & 0xFFFFFFFFFFFFFFFF) for item in values), len(values)
    raise PcAgentError(f"Unsupported RPM header entry type: {entry_type}.")


def _build_header(
    region_tag: int, entries: list[tuple[int, int, Any]]
) -> bytes:
    """Build one RPM header blob with a sealed immutable region."""

    ordered = sorted(entries, key=lambda item: item[0])
    if len({tag for tag, _type, _value in ordered}) != len(ordered):
        raise PcAgentError("RPM header entries contain a duplicate tag.")
    if ordered and ordered[0][0] <= region_tag:
        raise PcAgentError("RPM header region tag must sort first.")
    store = bytearray()
    index: list[tuple[int, int, int, int]] = []
    for tag, entry_type, value in ordered:
        alignment = _TYPE_ALIGN[entry_type]
        while len(store) % alignment:
            store.append(0)
        encoded, count = _encode_value(entry_type, value)
        index.append((tag, entry_type, len(store), count))
        store.extend(encoded)
    trailer_offset = len(store)
    index_count = len(index) + 1
    store.extend(
        _REGION_STRUCT.pack(region_tag, _TYPE_BIN, -(index_count * 16), 16)
    )
    index.insert(0, (region_tag, _TYPE_BIN, trailer_offset, 16))
    intro = _HEADER_MAGIC + struct.pack("!II", index_count, len(store))
    index_bytes = b"".join(_ENTRY_STRUCT.pack(*entry) for entry in index)
    return intro + index_bytes + bytes(store)


def _lead_bytes() -> bytes:
    name = f"{RPM_PACKAGE_NAME}-{__version__}-{RPM_PACKAGE_RELEASE}".encode()
    if len(name) > 65:
        raise PcAgentError("RPM lead package name is too long.")
    return _LEAD_STRUCT.pack(
        _LEAD_MAGIC, 3, 0, 0, 1, name.ljust(66, b"\x00"), 1, 5, b"\x00" * 16
    )


def _cpio_bytes(items: list[_PayloadItem]) -> bytes:
    """Build a deterministic SVr4 newc cpio archive (magic 070701)."""

    archive = bytearray()

    def add_entry(name: str, mode: int, data: bytes, ino: int, nlink: int) -> None:
        encoded_name = name.encode()
        fields = (
            ino,
            mode,
            0,  # uid
            0,  # gid
            nlink,
            0,  # mtime
            len(data),
            0,  # devMajor
            0,  # devMinor
            0,  # rdevMajor
            0,  # rdevMinor
            len(encoded_name) + 1,
            0,  # check (unused by 070701)
        )
        archive.extend(b"070701" + b"".join(f"{field:08x}".encode() for field in fields))
        archive.extend(encoded_name + b"\x00")
        while len(archive) % 4:
            archive.append(0)
        archive.extend(data)
        while len(archive) % 4:
            archive.append(0)

    for position, item in enumerate(items, start=1):
        add_entry(
            "." + item.path,
            item.full_mode,
            b"" if item.is_dir else item.data,
            position,
            2 if item.is_dir else 1,
        )
    add_entry("TRAILER!!!", 0, b"", 0, 1)
    while len(archive) % 512:
        archive.append(0)
    return bytes(archive)


def _signature_header(main_header: bytes, payload_gz: bytes, cpio_size: int) -> bytes:
    header_and_payload = main_header + payload_gz
    entries: list[tuple[int, int, Any]] = [
        (_SIG_SHA1, _TYPE_STRING, hashlib.sha1(main_header, usedforsecurity=False).hexdigest()),
        (_SIG_SHA256, _TYPE_STRING, hashlib.sha256(main_header).hexdigest()),
        (_SIG_SIZE, _TYPE_INT32, len(header_and_payload)),
        (_SIG_MD5, _TYPE_BIN, hashlib.md5(header_and_payload, usedforsecurity=False).digest()),
        (_SIG_PAYLOADSIZE, _TYPE_INT32, cpio_size),
    ]
    blob = _build_header(_SIG_HEADERSIGNATURES, entries)
    padding = (-len(blob)) % 8
    return blob + b"\x00" * padding


def _main_header(items: list[_PayloadItem], *, packager: str) -> bytes:
    dirnames: list[str] = []
    basenames: list[str] = []
    dirindexes: list[int] = []
    for item in items:
        directory, _, base = item.path.rpartition("/")
        dirname = directory + "/"
        if dirname not in dirnames:
            dirnames.append(dirname)
        dirindexes.append(dirnames.index(dirname))
        basenames.append(base)
    count = len(items)
    digests = [
        "" if item.is_dir else _sha256_bytes(item.data) for item in items
    ]
    fileflags = [
        1 if (not item.is_dir and item.path.startswith("/usr/share/doc/")) else 0
        for item in items
    ]
    requires = _requires()
    entries: list[tuple[int, int, Any]] = [
        (_TAG_HEADERI18NTABLE, _TYPE_STRING_ARRAY, ["C"]),
        (_TAG_NAME, _TYPE_STRING, RPM_PACKAGE_NAME),
        (_TAG_VERSION, _TYPE_STRING, __version__),
        (_TAG_RELEASE, _TYPE_STRING, RPM_PACKAGE_RELEASE),
        (_TAG_SUMMARY, _TYPE_I18NSTRING, _SUMMARY),
        (_TAG_DESCRIPTION, _TYPE_I18NSTRING, _DESCRIPTION),
        (_TAG_BUILDTIME, _TYPE_INT32, 0),
        (_TAG_BUILDHOST, _TYPE_STRING, "localhost"),
        (
            _TAG_SIZE,
            _TYPE_INT32,
            sum(len(item.data) for item in items if not item.is_dir),
        ),
        (_TAG_VENDOR, _TYPE_STRING, _VENDOR),
        (_TAG_LICENSE, _TYPE_STRING, _LICENSE),
        (_TAG_PACKAGER, _TYPE_STRING, packager),
        (_TAG_GROUP, _TYPE_I18NSTRING, _GROUP),
        (_TAG_OS, _TYPE_STRING, "linux"),
        (_TAG_ARCH, _TYPE_STRING, RPM_PACKAGE_ARCHITECTURE),
        (
            _TAG_FILESIZES,
            _TYPE_INT32,
            [0 if item.is_dir else len(item.data) for item in items],
        ),
        (_TAG_FILEMODES, _TYPE_INT16, [item.full_mode for item in items]),
        (_TAG_FILERDEVS, _TYPE_INT16, [0] * count),
        (_TAG_FILEMTIMES, _TYPE_INT32, [0] * count),
        (_TAG_FILEDIGESTS, _TYPE_STRING_ARRAY, digests),
        (_TAG_FILELINKTOS, _TYPE_STRING_ARRAY, [""] * count),
        (_TAG_FILEFLAGS, _TYPE_INT32, fileflags),
        (_TAG_FILEUSERNAME, _TYPE_STRING_ARRAY, ["root"] * count),
        (_TAG_FILEGROUPNAME, _TYPE_STRING_ARRAY, ["root"] * count),
        (_TAG_FILEVERIFYFLAGS, _TYPE_INT32, [0xFFFFFFFF] * count),
        (_TAG_PROVIDENAME, _TYPE_STRING_ARRAY, [RPM_PACKAGE_NAME]),
        (_TAG_REQUIREFLAGS, _TYPE_INT32, [flags for _n, flags, _v in requires]),
        (_TAG_REQUIRENAME, _TYPE_STRING_ARRAY, [name for name, _f, _v in requires]),
        (
            _TAG_REQUIREVERSION,
            _TYPE_STRING_ARRAY,
            [version for _n, _f, version in requires],
        ),
        (_TAG_FILEDEVICES, _TYPE_INT32, [1] * count),
        (_TAG_FILEINODES, _TYPE_INT32, list(range(1, count + 1))),
        (_TAG_FILELANGS, _TYPE_STRING_ARRAY, [""] * count),
        (_TAG_PROVIDEFLAGS, _TYPE_INT32, [_DEP_EQUAL]),
        (_TAG_PROVIDEVERSION, _TYPE_STRING_ARRAY, [_package_version()]),
        (_TAG_DIRINDEXES, _TYPE_INT32, dirindexes),
        (_TAG_BASENAMES, _TYPE_STRING_ARRAY, basenames),
        (_TAG_DIRNAMES, _TYPE_STRING_ARRAY, dirnames),
        (_TAG_PAYLOADFORMAT, _TYPE_STRING, "cpio"),
        (_TAG_PAYLOADCOMPRESSOR, _TYPE_STRING, "gzip"),
        (_TAG_PAYLOADFLAGS, _TYPE_STRING, "9"),
        (_TAG_FILEDIGESTALGO, _TYPE_INT32, 8),  # SHA-256
        (_TAG_ENCODING, _TYPE_STRING, "utf-8"),
    ]
    return _build_header(_TAG_HEADERIMMUTABLE, entries)


def build_rpm_package(
    root_dir: Path,
    output_path: Path,
    *,
    packager: str = DEFAULT_RPM_PACKAGER,
) -> RpmPackageInfo:
    """Build and verify a deterministic native RPM binary package."""

    root = root_dir.expanduser().resolve()
    destination = _validate_output_path(root, output_path)
    packager = _validate_packager(packager)
    destination.parent.mkdir(parents=True, exist_ok=True)

    data_files: dict[str, tuple[bytes, int]] = {}
    with tempfile.TemporaryDirectory(
        prefix=".pc-agent-rpm-", dir=destination.parent
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
            if len(data) > _MAX_RPM_FILE_BYTES:
                raise PcAgentError("RPM package payload file exceeds the size limit.")
            data_files[f"{RPM_PACKAGE_POSIX_ROOT}/{relative}"] = (
                data,
                _posix_package_mode(relative),
            )

    generated = {
        RPM_PACKAGE_CLI_LAUNCHER: (_launcher_bytes(gui=False), 0o755),
        RPM_PACKAGE_GUI_LAUNCHER: (_launcher_bytes(gui=True), 0o755),
        RPM_PACKAGE_DESKTOP_ENTRY: (_desktop_entry_bytes(), 0o644),
        RPM_PACKAGE_README: (_readme_rpm_bytes(), 0o644),
        RPM_PACKAGE_CHANGELOG: (_changelog_bytes(root), 0o644),
    }
    data_files.update(generated)
    if len(data_files) + 1 > _MAX_RPM_FILES:
        raise PcAgentError("RPM package would contain too many files.")
    total = sum(len(data) for data, _mode in data_files.values())
    if total > _MAX_RPM_TOTAL_BYTES:
        raise PcAgentError("RPM package exceeds the total size limit.")

    records = [
        {
            "path": "/" + path,
            "size": len(data),
            "sha256": _sha256_bytes(data),
            "mode": mode,
        }
        for path, (data, mode) in sorted(data_files.items())
    ]
    manifest = {
        "format": _MANIFEST_FORMAT,
        "app": RPM_PACKAGE_APP_NAME,
        "package": RPM_PACKAGE_NAME,
        "version": __version__,
        "package_version": _package_version(),
        "architecture": RPM_PACKAGE_ARCHITECTURE,
        "file_count": len(records),
        "payload_file_count": posix_info.payload_file_count,
        "total_bytes": total,
        "posix_manifest_sha256": posix_info.manifest_sha256,
        "files": records,
    }
    manifest_data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    if len(manifest_data) > _MAX_RPM_MANIFEST_BYTES:
        raise PcAgentError("RPM package manifest exceeds the size limit.")
    data_files[RPM_PACKAGE_MANIFEST] = (manifest_data, 0o644)

    items = _payload_items(data_files)
    cpio_data = _cpio_bytes(items)
    payload_gz = _gzip_bytes(cpio_data)
    main_header = _main_header(items, packager=packager)
    signature_header = _signature_header(main_header, payload_gz, len(cpio_data))
    package_data = _lead_bytes() + signature_header + main_header + payload_gz
    if len(package_data) > _MAX_RPM_PACKAGE_BYTES:
        raise PcAgentError("RPM package exceeds the package size limit.")

    temporary = destination.with_name(
        f".{destination.stem}.tmp-{secrets.token_hex(4)}.rpm"
    )
    try:
        temporary.write_bytes(package_data)
        verify_rpm_package(temporary)
        if destination.exists():
            raise PcAgentError(f"RPM package output already exists: {destination}.")
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return verify_rpm_package(destination)


def _read_package(path: Path) -> bytes:
    candidate = path.expanduser().resolve()
    if candidate.suffix != ".rpm" or not candidate.is_file() or candidate.is_symlink():
        raise PcAgentError(f"RPM package was not found: {candidate}.")
    size = candidate.stat().st_size
    if size > _MAX_RPM_PACKAGE_BYTES:
        raise PcAgentError("RPM package exceeds the package size limit.")
    data = candidate.read_bytes()
    if len(data) != size:
        raise PcAgentError(f"RPM package changed while reading: {candidate}.")
    return data


def _parse_lead(data: bytes) -> None:
    if len(data) < _LEAD_STRUCT.size:
        raise PcAgentError("RPM package is shorter than the lead structure.")
    magic, major, minor, lead_type, archnum, raw_name, osnum, sig_type, reserved = (
        _LEAD_STRUCT.unpack_from(data)
    )
    if magic != _LEAD_MAGIC:
        raise PcAgentError("RPM package lead magic is invalid.")
    if (major, minor, lead_type, archnum, osnum, sig_type) != (3, 0, 0, 1, 1, 5):
        raise PcAgentError("RPM package lead fields are unexpected.")
    expected_name = (
        f"{RPM_PACKAGE_NAME}-{__version__}-{RPM_PACKAGE_RELEASE}".encode()
    )
    if raw_name != expected_name.ljust(66, b"\x00"):
        raise PcAgentError("RPM package lead name is unexpected.")
    if reserved != b"\x00" * 16:
        raise PcAgentError("RPM package lead reserved bytes are not zero.")


def _decoded_length(
    entry_type: int, store: bytes, offset: int, count: int, *, label: str
) -> int:
    if entry_type in (_TYPE_STRING, _TYPE_I18NSTRING, _TYPE_STRING_ARRAY):
        if entry_type == _TYPE_STRING and count != 1:
            raise PcAgentError(f"RPM {label} string entry has an invalid count.")
        cursor = offset
        for _ in range(count):
            end = store.find(b"\x00", cursor)
            if end == -1:
                raise PcAgentError(f"RPM {label} contains an unterminated string.")
            cursor = end + 1
        return cursor - offset
    size = _TYPE_SIZE.get(entry_type)
    if size is None:
        raise PcAgentError(f"RPM {label} contains an unsupported entry type.")
    return size * count


def _decode_value(
    entry_type: int, store: bytes, offset: int, count: int, length: int
) -> Any:
    raw = store[offset : offset + length]
    if entry_type in (_TYPE_STRING, _TYPE_I18NSTRING):
        return raw[:-1].decode()
    if entry_type == _TYPE_STRING_ARRAY:
        return [part.decode() for part in raw.split(b"\x00")[:-1]]
    if entry_type == _TYPE_BIN:
        return raw
    if entry_type == _TYPE_INT16:
        return list(struct.unpack(f"!{count}H", raw))
    if entry_type == _TYPE_INT32:
        return list(struct.unpack(f"!{count}I", raw))
    if entry_type == _TYPE_INT64:
        return list(struct.unpack(f"!{count}Q", raw))
    if entry_type in (1, 2):
        return list(raw)
    raise PcAgentError("RPM header contains an unsupported entry type.")


def _parse_header(
    data: bytes, start: int, *, region_tag: int, label: str
) -> tuple[dict[int, tuple[int, Any]], int, bytes]:
    """Parse and strictly validate one RPM header blob."""

    if start + 16 > len(data) or data[start : start + 8] != _HEADER_MAGIC:
        raise PcAgentError(f"RPM {label} header magic is invalid.")
    index_count, data_length = struct.unpack_from("!II", data, start + 8)
    max_tags = 32 if region_tag == _SIG_HEADERSIGNATURES else 0xFFFF
    if not 1 <= index_count <= max_tags or data_length > _MAX_RPM_HEADER_BYTES:
        raise PcAgentError(f"RPM {label} header size fields are out of range.")
    index_start = start + 16
    store_start = index_start + index_count * 16
    store_end = store_start + data_length
    if store_end > len(data):
        raise PcAgentError(f"RPM {label} header is truncated.")
    store = data[store_start:store_end]
    raw_entries = [
        _ENTRY_STRUCT.unpack_from(data, index_start + i * 16)
        for i in range(index_count)
    ]
    tags = [entry[0] for entry in raw_entries]
    if tags != sorted(tags) or len(set(tags)) != len(tags):
        raise PcAgentError(f"RPM {label} header tags are not strictly sorted.")
    first_tag, first_type, first_offset, first_count = raw_entries[0]
    if (first_tag, first_type, first_count) != (region_tag, _TYPE_BIN, 16):
        raise PcAgentError(f"RPM {label} header region entry is invalid.")
    if first_offset != data_length - 16:
        raise PcAgentError(f"RPM {label} header region trailer is misplaced.")
    trailer = _REGION_STRUCT.unpack_from(store, first_offset)
    if trailer != (region_tag, _TYPE_BIN, -(index_count * 16), 16):
        raise PcAgentError(f"RPM {label} header region trailer is invalid.")

    values: dict[int, tuple[int, Any]] = {}
    cursor = 0
    for tag, entry_type, offset, count in raw_entries[1:]:
        if entry_type < 1 or entry_type > 9 or count < 1:
            raise PcAgentError(f"RPM {label} header entry is malformed.")
        alignment = _TYPE_ALIGN[entry_type]
        while cursor % alignment:
            cursor += 1
        if offset != cursor:
            raise PcAgentError(
                f"RPM {label} header data layout is not the canonical tight layout."
            )
        length = _decoded_length(
            entry_type, store, offset, count, label=label
        )
        if offset + length > data_length - 16:
            raise PcAgentError(f"RPM {label} header entry overruns the data area.")
        try:
            values[tag] = (
                entry_type,
                _decode_value(entry_type, store, offset, count, length),
            )
        except (UnicodeDecodeError, struct.error) as exc:
            raise PcAgentError(f"RPM {label} header entry cannot be decoded.") from exc
        cursor = offset + length
    if cursor != data_length - 16:
        raise PcAgentError(f"RPM {label} header data area has trailing slack.")
    return values, store_end, data[start:store_end]


def _gunzip_bounded(data: bytes, *, limit: int) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            output = stream.read(limit + 1)
    except (OSError, EOFError) as exc:
        raise PcAgentError("RPM payload is not valid gzip data.") from exc
    if len(output) > limit:
        raise PcAgentError("RPM payload exceeds the size limit.")
    return output


def _parse_cpio(data: bytes) -> list[_PayloadItem]:
    items: list[_PayloadItem] = []
    offset = 0
    inode = 0
    while True:
        if offset + 110 > len(data):
            raise PcAgentError("RPM cpio payload is truncated.")
        header = data[offset : offset + 110]
        if header[:6] != b"070701":
            raise PcAgentError("RPM cpio payload uses an unsupported format.")
        try:
            fields = [
                int(header[6 + i * 8 : 14 + i * 8], 16) for i in range(13)
            ]
        except ValueError as exc:
            raise PcAgentError("RPM cpio payload has a malformed header.") from exc
        (
            ino,
            mode,
            uid,
            gid,
            nlink,
            mtime,
            filesize,
            dev_major,
            dev_minor,
            rdev_major,
            rdev_minor,
            namesize,
            check,
        ) = fields
        offset += 110
        if namesize < 1 or offset + namesize > len(data):
            raise PcAgentError("RPM cpio payload has an invalid name size.")
        raw_name = data[offset : offset + namesize]
        if not raw_name.endswith(b"\x00"):
            raise PcAgentError("RPM cpio payload name is not NUL-terminated.")
        try:
            name = raw_name[:-1].decode()
        except UnicodeDecodeError as exc:
            raise PcAgentError("RPM cpio payload name is not valid UTF-8.") from exc
        offset += namesize
        offset += (-offset) % 4
        if offset + filesize > len(data):
            raise PcAgentError("RPM cpio payload file data is truncated.")
        content = data[offset : offset + filesize]
        offset += filesize
        offset += (-offset) % 4
        fixed = (uid, gid, mtime, dev_major, dev_minor, rdev_major, rdev_minor, check)
        if name == "TRAILER!!!":
            if (ino, mode, filesize) != (0, 0, 0) or any(fixed):
                raise PcAgentError("RPM cpio trailer entry is invalid.")
            break
        inode += 1
        if ino != inode or any(fixed):
            raise PcAgentError("RPM cpio payload metadata is unexpected.")
        is_dir = mode & 0o170000 == 0o040000
        is_file = mode & 0o170000 == 0o100000
        if not (is_dir or is_file):
            raise PcAgentError("RPM cpio payload contains a non-file entry.")
        expected_mode = 0o40755 if is_dir else 0o100755 if mode & 0o111 else 0o100644
        if mode != expected_mode:
            raise PcAgentError("RPM cpio payload contains an unsafe file mode.")
        if nlink != (2 if is_dir else 1):
            raise PcAgentError("RPM cpio payload link count is unexpected.")
        if not name.startswith("./usr/") or ".." in Path(name).parts or "\\" in name:
            raise PcAgentError(f"RPM cpio payload contains an unsafe path: {name!r}.")
        if filesize > _MAX_RPM_FILE_BYTES:
            raise PcAgentError("RPM cpio payload contains an oversized file.")
        items.append(
            _PayloadItem(
                path=name[1:],
                is_dir=is_dir,
                data=b"" if is_dir else content,
                mode=mode & 0o777,
            )
        )
        if len(items) > _MAX_RPM_FILES + 1_000:
            raise PcAgentError("RPM cpio payload contains too many entries.")
    if any(data[offset:]):
        raise PcAgentError("RPM cpio payload has non-zero trailing padding.")
    paths = [item.path for item in items]
    if paths != sorted(paths) or len(set(paths)) != len(paths):
        raise PcAgentError("RPM cpio payload entries are not strictly sorted.")
    return items


def _parse_manifest(data: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if len(data) > _MAX_RPM_MANIFEST_BYTES:
        raise PcAgentError("RPM package manifest exceeds the size limit.")
    try:
        manifest = json.loads(data.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PcAgentError("RPM package manifest is not valid JSON.") from exc
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
        raise PcAgentError("RPM package manifest has an unexpected shape.")
    if manifest["format"] != _MANIFEST_FORMAT:
        raise PcAgentError("RPM package manifest format is unsupported.")
    if manifest["app"] != RPM_PACKAGE_APP_NAME:
        raise PcAgentError("RPM package manifest app is unexpected.")
    if manifest["package"] != RPM_PACKAGE_NAME:
        raise PcAgentError("RPM package manifest package name is unexpected.")
    if manifest["version"] != __version__:
        raise PcAgentError("RPM package version does not match this application.")
    if manifest["package_version"] != _package_version():
        raise PcAgentError("RPM package release identifier is unexpected.")
    if manifest["architecture"] != RPM_PACKAGE_ARCHITECTURE:
        raise PcAgentError("RPM package architecture is unexpected.")
    if not isinstance(manifest["files"], list):
        raise PcAgentError("RPM package manifest files are invalid.")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    for raw in manifest["files"]:
        if not isinstance(raw, dict) or set(raw) != {"path", "size", "sha256", "mode"}:
            raise PcAgentError("RPM package manifest contains a malformed record.")
        path = raw["path"]
        size = raw["size"]
        digest = raw["sha256"]
        mode = raw["mode"]
        if (
            not isinstance(path, str)
            or path in seen
            or not path.startswith("/usr/")
            or ".." in Path(path).parts
            or not isinstance(size, int)
            or size < 0
            or size > _MAX_RPM_FILE_BYTES
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            or mode not in (0o644, 0o755)
        ):
            raise PcAgentError("RPM package manifest contains an invalid record.")
        seen.add(path)
        total += size
        records.append(raw)
    if manifest["file_count"] != len(records) or manifest["total_bytes"] != total:
        raise PcAgentError("RPM package manifest totals do not match its files.")
    if (
        not isinstance(manifest["payload_file_count"], int)
        or manifest["payload_file_count"] < 1
        or not isinstance(manifest["posix_manifest_sha256"], str)
        or _SHA256_RE.fullmatch(manifest["posix_manifest_sha256"]) is None
    ):
        raise PcAgentError("RPM package manifest POSIX summary is invalid.")
    return manifest, records


def _verify_launcher(data: bytes, *, gui: bool) -> None:
    try:
        text = data.decode()
    except UnicodeDecodeError as exc:
        raise PcAgentError("RPM launcher is not valid UTF-8.") from exc
    target_script = "launch_gui.sh" if gui else "launch_cli.sh"
    required = (
        "#!/bin/sh",
        "set -eu",
        "PC_AGENT_RPM_PACKAGE_ROOT:-/usr/share/self-healing-pc-agent",
        f'EXPECTED_VERSION="{__version__}"',
        'sh "$POSIX_PACKAGE/install.sh" --prefix "$TARGET_DIR"',
        f'exec "$TARGET_DIR/scripts/{target_script}" "$@"',
    )
    if any(fragment not in text for fragment in required):
        raise PcAgentError("RPM launcher wiring is incomplete.")
    forbidden = ("sudo ", "curl ", "wget ", "eval ", "rm -rf", "chmod 777")
    if any(fragment in text for fragment in forbidden):
        raise PcAgentError("RPM launcher contains a forbidden command.")


def _verify_embedded_posix_package(
    files: dict[str, tuple[bytes, int]], manifest: dict[str, Any]
) -> None:
    prefix = f"/{RPM_PACKAGE_POSIX_ROOT}/"
    with tempfile.TemporaryDirectory(prefix="pc-agent-rpm-verify-") as name:
        package_root = Path(name) / "posix-package"
        for path, (data, mode) in sorted(files.items()):
            if not path.startswith(prefix):
                continue
            relative = path.removeprefix(prefix)
            target = package_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(mode)
        info = verify_posix_package(package_root)
    if info.payload_file_count != manifest["payload_file_count"]:
        raise PcAgentError("RPM embedded POSIX payload count does not match.")
    if info.manifest_sha256 != manifest["posix_manifest_sha256"]:
        raise PcAgentError("RPM embedded POSIX manifest hash does not match.")


def _header_value(
    header: dict[int, tuple[int, Any]], tag: int, entry_type: int, *, label: str
) -> Any:
    if tag not in header:
        raise PcAgentError(f"RPM main header is missing tag {tag} ({label}).")
    actual_type, value = header[tag]
    if actual_type != entry_type:
        raise PcAgentError(f"RPM main header tag {tag} ({label}) has the wrong type.")
    return value


def verify_rpm_package(package_path: Path) -> RpmPackageInfo:
    """Verify an RPM binary package without invoking rpm or shell code."""

    candidate = package_path.expanduser().resolve()
    package_data = _read_package(candidate)
    _parse_lead(package_data)

    signature, signature_end, _signature_blob = _parse_header(
        package_data,
        _LEAD_STRUCT.size,
        region_tag=_SIG_HEADERSIGNATURES,
        label="signature",
    )
    if set(signature) != {
        _SIG_SHA1,
        _SIG_SHA256,
        _SIG_SIZE,
        _SIG_MD5,
        _SIG_PAYLOADSIZE,
    }:
        raise PcAgentError("RPM signature header tags are unexpected.")
    padded_end = signature_end + ((-signature_end) % 8)
    if any(package_data[signature_end:padded_end]):
        raise PcAgentError("RPM signature header padding is not zero.")
    header, header_end, main_header_blob = _parse_header(
        package_data, padded_end, region_tag=_TAG_HEADERIMMUTABLE, label="main"
    )
    payload_gz = package_data[header_end:]
    if not payload_gz:
        raise PcAgentError("RPM package payload is missing.")

    # Signature header digests and sizes cover the main header and payload.
    if signature[_SIG_SHA1][1] != hashlib.sha1(
        main_header_blob, usedforsecurity=False
    ).hexdigest():
        raise PcAgentError("RPM signature SHA1 header digest does not match.")
    if signature[_SIG_SHA256][1] != hashlib.sha256(main_header_blob).hexdigest():
        raise PcAgentError("RPM signature SHA256 header digest does not match.")
    if signature[_SIG_SIZE][1] != [len(main_header_blob) + len(payload_gz)]:
        raise PcAgentError("RPM signature SIZE does not match the package.")
    if signature[_SIG_MD5][1] != hashlib.md5(
        main_header_blob + payload_gz, usedforsecurity=False
    ).digest():
        raise PcAgentError("RPM signature MD5 digest does not match.")

    if _SCRIPTLET_TAGS & set(header):
        raise PcAgentError("RPM package contains forbidden scriptlet tags.")
    expected_strings = {
        _TAG_NAME: (_TYPE_STRING, RPM_PACKAGE_NAME),
        _TAG_VERSION: (_TYPE_STRING, __version__),
        _TAG_RELEASE: (_TYPE_STRING, RPM_PACKAGE_RELEASE),
        _TAG_SUMMARY: (_TYPE_I18NSTRING, _SUMMARY),
        _TAG_DESCRIPTION: (_TYPE_I18NSTRING, _DESCRIPTION),
        _TAG_BUILDHOST: (_TYPE_STRING, "localhost"),
        _TAG_VENDOR: (_TYPE_STRING, _VENDOR),
        _TAG_LICENSE: (_TYPE_STRING, _LICENSE),
        _TAG_GROUP: (_TYPE_I18NSTRING, _GROUP),
        _TAG_OS: (_TYPE_STRING, "linux"),
        _TAG_ARCH: (_TYPE_STRING, RPM_PACKAGE_ARCHITECTURE),
        _TAG_PAYLOADFORMAT: (_TYPE_STRING, "cpio"),
        _TAG_PAYLOADCOMPRESSOR: (_TYPE_STRING, "gzip"),
        _TAG_PAYLOADFLAGS: (_TYPE_STRING, "9"),
        _TAG_ENCODING: (_TYPE_STRING, "utf-8"),
    }
    for tag, (entry_type, expected) in expected_strings.items():
        if _header_value(header, tag, entry_type, label="metadata") != expected:
            raise PcAgentError(f"RPM main header tag {tag} is unexpected.")
    packager = _validate_packager(
        str(_header_value(header, _TAG_PACKAGER, _TYPE_STRING, label="packager"))
    )
    if _header_value(
        header, _TAG_HEADERI18NTABLE, _TYPE_STRING_ARRAY, label="i18n"
    ) != ["C"]:
        raise PcAgentError("RPM main header i18n table is unexpected.")
    if _header_value(header, _TAG_BUILDTIME, _TYPE_INT32, label="buildtime") != [0]:
        raise PcAgentError("RPM main header build time is not deterministic zero.")
    if _header_value(
        header, _TAG_FILEDIGESTALGO, _TYPE_INT32, label="digestalgo"
    ) != [8]:
        raise PcAgentError("RPM file digest algorithm is not SHA-256.")
    if _TAG_ARCHIVESIZE in header:
        # ARCHIVESIZE is merged from the signature PAYLOADSIZE tag by rpm;
        # carrying it in the main header as well makes real rpm reject the
        # package, so it must never be baked into the main header.
        raise PcAgentError("RPM main header must not carry ARCHIVESIZE.")
    if _header_value(header, _TAG_PROVIDENAME, _TYPE_STRING_ARRAY, label="provides") != [
        RPM_PACKAGE_NAME
    ] or _header_value(
        header, _TAG_PROVIDEFLAGS, _TYPE_INT32, label="provides"
    ) != [_DEP_EQUAL] or _header_value(
        header, _TAG_PROVIDEVERSION, _TYPE_STRING_ARRAY, label="provides"
    ) != [_package_version()]:
        raise PcAgentError("RPM provides metadata is unexpected.")
    requires = _requires()
    if (
        _header_value(header, _TAG_REQUIRENAME, _TYPE_STRING_ARRAY, label="requires")
        != [name for name, _f, _v in requires]
        or _header_value(header, _TAG_REQUIREFLAGS, _TYPE_INT32, label="requires")
        != [flags for _n, flags, _v in requires]
        or _header_value(
            header, _TAG_REQUIREVERSION, _TYPE_STRING_ARRAY, label="requires"
        )
        != [version for _n, _f, version in requires]
    ):
        raise PcAgentError("RPM requires metadata is unexpected.")

    cpio_data = _gunzip_bounded(payload_gz, limit=_MAX_RPM_TOTAL_BYTES)
    if signature[_SIG_PAYLOADSIZE][1] != [len(cpio_data)]:
        raise PcAgentError("RPM signature payload size does not match.")
    items = _parse_cpio(cpio_data)

    array_tags = {
        _TAG_FILESIZES: _TYPE_INT32,
        _TAG_FILEMODES: _TYPE_INT16,
        _TAG_FILERDEVS: _TYPE_INT16,
        _TAG_FILEMTIMES: _TYPE_INT32,
        _TAG_FILEDIGESTS: _TYPE_STRING_ARRAY,
        _TAG_FILELINKTOS: _TYPE_STRING_ARRAY,
        _TAG_FILEFLAGS: _TYPE_INT32,
        _TAG_FILEUSERNAME: _TYPE_STRING_ARRAY,
        _TAG_FILEGROUPNAME: _TYPE_STRING_ARRAY,
        _TAG_FILEVERIFYFLAGS: _TYPE_INT32,
        _TAG_FILEDEVICES: _TYPE_INT32,
        _TAG_FILEINODES: _TYPE_INT32,
        _TAG_FILELANGS: _TYPE_STRING_ARRAY,
        _TAG_DIRINDEXES: _TYPE_INT32,
        _TAG_BASENAMES: _TYPE_STRING_ARRAY,
    }
    arrays = {
        tag: _header_value(header, tag, entry_type, label="file list")
        for tag, entry_type in array_tags.items()
    }
    dirnames = _header_value(
        header, _TAG_DIRNAMES, _TYPE_STRING_ARRAY, label="file list"
    )
    count = len(items)
    if any(len(value) != count for value in arrays.values()):
        raise PcAgentError("RPM header file arrays have inconsistent lengths.")
    if _header_value(header, _TAG_SIZE, _TYPE_INT32, label="size") != [
        sum(len(item.data) for item in items if not item.is_dir)
    ]:
        raise PcAgentError("RPM installed size does not match its files.")

    expected_dirnames: list[str] = []
    files: dict[str, tuple[bytes, int]] = {}
    for position, item in enumerate(items):
        directory, _, base = item.path.rpartition("/")
        dirname = directory + "/"
        if dirname not in expected_dirnames:
            expected_dirnames.append(dirname)
        checks = (
            arrays[_TAG_BASENAMES][position] == base,
            arrays[_TAG_DIRINDEXES][position] == expected_dirnames.index(dirname),
            arrays[_TAG_FILESIZES][position]
            == (0 if item.is_dir else len(item.data)),
            arrays[_TAG_FILEMODES][position] == item.full_mode,
            arrays[_TAG_FILERDEVS][position] == 0,
            arrays[_TAG_FILEMTIMES][position] == 0,
            arrays[_TAG_FILEDIGESTS][position]
            == ("" if item.is_dir else _sha256_bytes(item.data)),
            arrays[_TAG_FILELINKTOS][position] == "",
            arrays[_TAG_FILEFLAGS][position]
            == (
                1
                if (not item.is_dir and item.path.startswith("/usr/share/doc/"))
                else 0
            ),
            arrays[_TAG_FILEUSERNAME][position] == "root",
            arrays[_TAG_FILEGROUPNAME][position] == "root",
            arrays[_TAG_FILEVERIFYFLAGS][position] == 0xFFFFFFFF,
            arrays[_TAG_FILEDEVICES][position] == 1,
            arrays[_TAG_FILEINODES][position] == position + 1,
            arrays[_TAG_FILELANGS][position] == "",
        )
        if not all(checks):
            raise PcAgentError(
                f"RPM header file metadata does not match payload: {item.path}."
            )
        if not item.is_dir:
            files[item.path] = (item.data, item.mode)
    if dirnames != expected_dirnames:
        raise PcAgentError("RPM header directory names do not match the payload.")

    manifest_path = "/" + RPM_PACKAGE_MANIFEST
    if manifest_path not in files:
        raise PcAgentError("RPM package manifest is missing.")
    manifest_data, manifest_mode = files[manifest_path]
    if manifest_mode != 0o644:
        raise PcAgentError("RPM package manifest mode is unexpected.")
    manifest, records = _parse_manifest(manifest_data)
    recorded_paths = {str(record["path"]) for record in records}
    if recorded_paths != set(files) - {manifest_path}:
        raise PcAgentError("RPM package file inventory does not match its manifest.")
    for record in records:
        data, mode = files[str(record["path"])]
        if (
            len(data) != record["size"]
            or _sha256_bytes(data) != record["sha256"]
            or mode != record["mode"]
        ):
            raise PcAgentError(
                f"RPM package file failed verification: {record['path']}."
            )

    for path in (
        "/" + RPM_PACKAGE_CLI_LAUNCHER,
        "/" + RPM_PACKAGE_GUI_LAUNCHER,
        "/" + RPM_PACKAGE_DESKTOP_ENTRY,
        "/" + RPM_PACKAGE_README,
        "/" + RPM_PACKAGE_CHANGELOG,
    ):
        if path not in files:
            raise PcAgentError(f"RPM package is missing required file: {path}.")
    _verify_launcher(files["/" + RPM_PACKAGE_CLI_LAUNCHER][0], gui=False)
    _verify_launcher(files["/" + RPM_PACKAGE_GUI_LAUNCHER][0], gui=True)
    desktop = files["/" + RPM_PACKAGE_DESKTOP_ENTRY][0].decode()
    for fragment in (
        "[Desktop Entry]",
        "Type=Application",
        "Name=Self-Healing PC Agent",
        "Exec=/usr/bin/self-healing-pc-agent-gui",
        "TryExec=/usr/bin/self-healing-pc-agent-gui",
        "Terminal=false",
    ):
        if fragment not in desktop:
            raise PcAgentError("RPM desktop entry is incomplete.")
    _verify_embedded_posix_package(files, manifest)

    return RpmPackageInfo(
        path=candidate,
        version=__version__,
        package_version=_package_version(),
        architecture=RPM_PACKAGE_ARCHITECTURE,
        file_count=len(files),
        payload_file_count=int(manifest["payload_file_count"]),
        total_bytes=sum(len(data) for data, _mode in files.values()),
        package_bytes=len(package_data),
        package_sha256=_sha256_file(candidate),
        manifest_sha256=_sha256_bytes(manifest_data),
        header_sha256=_sha256_bytes(main_header_blob),
        packager=packager,
        files=tuple(records),
    )


__all__ = [
    "DEFAULT_RPM_PACKAGER",
    "RPM_PACKAGE_APP_NAME",
    "RPM_PACKAGE_ARCHITECTURE",
    "RPM_PACKAGE_CLI_LAUNCHER",
    "RPM_PACKAGE_DESKTOP_ENTRY",
    "RPM_PACKAGE_GUI_LAUNCHER",
    "RPM_PACKAGE_MANIFEST",
    "RPM_PACKAGE_NAME",
    "RPM_PACKAGE_POSIX_ROOT",
    "RpmPackageInfo",
    "build_rpm_package",
    "rpm_package_filename",
    "verify_rpm_package",
]
