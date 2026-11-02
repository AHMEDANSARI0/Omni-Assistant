"""Deterministic RPM repository metadata and optional signing.

Phase 51 turns the already verified Phase 48 ``.rpm`` into a small static
DNF/YUM repository tree.  It writes the package under ``Packages/``, the
standard ``primary``, ``filelists`` and ``other`` XML metadata as
deterministic gzip files, and a ``repomd.xml`` index with SHA-256 hashes for
both compressed and uncompressed metadata.

Repository signing is optional and delegated to the operator's existing
system GnuPG key.  The signed form carries an armored detached signature for
``repomd.xml`` plus the exported public key.  No private key or passphrase is
accepted or stored.  The RPM package itself is not OpenPGP-signed; tools that
enable repository metadata GPG checking can authenticate the signed metadata
and the package checksum within it.

Building or verifying this tree does not host it, run dnf/yum/rpm, install
the package, or prove live Fedora/RHEL installation or upgrade evidence.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from . import __version__
from .debian_repository import (
    GpgRunner,
    _gpg_fingerprint,
    _run_gpg,
    _set_epoch_mtime,
    _validate_gnupg_home,
    _validate_gpg_executable,
    _validate_signing_key,
)
from .errors import PcAgentError
from .release import _sha256_bytes
from .rpm_package import (
    _LEAD_STRUCT,
    _MAX_RPM_TOTAL_BYTES,
    _SIG_HEADERSIGNATURES,
    _TAG_ARCH,
    _TAG_BUILDHOST,
    _TAG_DESCRIPTION,
    _TAG_GROUP,
    _TAG_HEADERIMMUTABLE,
    _TAG_LICENSE,
    _TAG_NAME,
    _TAG_PACKAGER,
    _TAG_PROVIDEFLAGS,
    _TAG_PROVIDENAME,
    _TAG_PROVIDEVERSION,
    _TAG_RELEASE,
    _TAG_REQUIREFLAGS,
    _TAG_REQUIRENAME,
    _TAG_REQUIREVERSION,
    _TAG_SIZE,
    _TAG_SUMMARY,
    _TAG_VENDOR,
    _TAG_VERSION,
    _TYPE_I18NSTRING,
    _TYPE_INT32,
    _TYPE_STRING,
    _TYPE_STRING_ARRAY,
    RPM_PACKAGE_APP_NAME,
    RPM_PACKAGE_ARCHITECTURE,
    RPM_PACKAGE_NAME,
    _gunzip_bounded,
    _header_value,
    _package_version,
    _parse_cpio,
    _parse_header,
    _parse_lead,
    _read_package,
    rpm_package_filename,
    verify_rpm_package,
)

RPM_REPOSITORY_MANIFEST = "pc-agent-rpm-repository-manifest.json"
RPM_REPOSITORY_KEYRING = "RPM-GPG-KEY-self-healing-pc-agent"

_REPOSITORY_FORMAT = 1
_MAX_REPOSITORY_FILES = 100
_MAX_REPOSITORY_FILE_BYTES = 250_000_000
_MAX_REPOSITORY_TOTAL_BYTES = 300_000_000
_MAX_REPOSITORY_METADATA_BYTES = 5_000_000
_MAX_REPOSITORY_MANIFEST_BYTES = 1_000_000
_MAX_PUBLIC_KEY_BYTES = 1_000_000
_MAX_SIGNATURE_BYTES = 1_000_000
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_FINGERPRINT_RE = re.compile(r"[0-9A-F]{40}")
_PRIMARY_FILE_PREFIXES = ("/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/", "/etc/")
_FLAG_NAMES = {2: "LT", 4: "GT", 8: "EQ", 10: "LE", 12: "GE"}


@dataclass(frozen=True, slots=True)
class RpmRepositoryInfo:
    path: Path
    version: str
    package_version: str
    architecture: str
    package_path: Path
    package_sha256: str
    repomd_sha256: str
    manifest_sha256: str
    file_count: int
    total_bytes: int
    signed: bool
    signing_key_fingerprint: str | None
    files: tuple[dict[str, Any], ...] = ()

    def summary(self) -> str:
        return (
            f"version={self.version}; package={self.package_version}; "
            f"architecture={self.architecture}; files={self.file_count}; "
            f"bytes={self.total_bytes}; signed={str(self.signed).lower()}; "
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
            "package_path": str(self.package_path),
            "package_sha256": self.package_sha256,
            "repomd_sha256": self.repomd_sha256,
            "manifest_sha256": self.manifest_sha256,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "repository_format": "rpm-repodata",
            "signed": self.signed,
            "package_signed": False,
            "signing_key_fingerprint": self.signing_key_fingerprint,
            "files": list(self.files),
        }


@dataclass(frozen=True, slots=True)
class _RpmMetadata:
    name: str
    version: str
    release: str
    architecture: str
    summary: str
    description: str
    packager: str
    license_name: str
    vendor: str
    group: str
    buildhost: str
    installed_size: int
    archive_size: int
    header_start: int
    header_end: int
    provides: tuple[tuple[str, int, str], ...]
    requires: tuple[tuple[str, int, str], ...]
    files: tuple[tuple[str, bool], ...]


def _validate_output_path(root: Path, output: Path) -> Path:
    raw = output.expanduser()
    if raw.is_symlink():
        raise PcAgentError("RPM repository output must not be a symbolic link.")
    destination = raw.resolve()
    if destination.exists():
        raise PcAgentError(f"RPM repository output already exists: {destination}.")
    if destination == root or destination in root.parents:
        raise PcAgentError("RPM repository output must not be the project root.")
    try:
        relative = destination.relative_to(root)
    except ValueError:
        return destination
    if not relative.parts or relative.parts[0] != "workspace":
        raise PcAgentError(
            "RPM repository output inside the project must be under workspace/."
        )
    return destination


def _xml_text(value: Any) -> str:
    return escape(str(value), {"\r": "&#13;"})


def _xml_attr(value: Any) -> str:
    return escape(
        str(value),
        {'"': "&quot;", "\r": "&#13;", "\n": "&#10;", "\t": "&#9;"},
    )


def _package_relative_path() -> str:
    return f"Packages/{rpm_package_filename()}"


def _zip_dependencies(
    names: Any, flags: Any, versions: Any, *, label: str
) -> tuple[tuple[str, int, str], ...]:
    if (
        not isinstance(names, list)
        or not isinstance(flags, list)
        or not isinstance(versions, list)
        or len(names) != len(flags)
        or len(names) != len(versions)
    ):
        raise PcAgentError(f"RPM {label} metadata arrays are inconsistent.")
    result: list[tuple[str, int, str]] = []
    for name, flag, version in zip(names, flags, versions, strict=True):
        if not isinstance(name, str) or not isinstance(flag, int) or not isinstance(
            version, str
        ):
            raise PcAgentError(f"RPM {label} metadata contains an invalid entry.")
        if flag != 0 and flag not in _FLAG_NAMES:
            raise PcAgentError(f"RPM {label} metadata uses unsupported flags.")
        result.append((name, flag, version))
    return tuple(result)


def _read_rpm_metadata(package_path: Path) -> _RpmMetadata:
    package_data = _read_package(package_path)
    _parse_lead(package_data)
    _signature, signature_end, _blob = _parse_header(
        package_data,
        _LEAD_STRUCT.size,
        region_tag=_SIG_HEADERSIGNATURES,
        label="signature",
    )
    header_start = signature_end + ((-signature_end) % 8)
    header, header_end, _header_blob = _parse_header(
        package_data,
        header_start,
        region_tag=_TAG_HEADERIMMUTABLE,
        label="main",
    )
    payload = _gunzip_bounded(
        package_data[header_end:], limit=_MAX_RPM_TOTAL_BYTES
    )
    items = _parse_cpio(payload)

    def string(tag: int, entry_type: int, label: str) -> str:
        value = _header_value(header, tag, entry_type, label=label)
        if not isinstance(value, str):
            raise PcAgentError(f"RPM header {label} metadata is invalid.")
        return value

    installed_sizes = _header_value(
        header, _TAG_SIZE, _TYPE_INT32, label="installed size"
    )
    if (
        not isinstance(installed_sizes, list)
        or len(installed_sizes) != 1
        or not isinstance(installed_sizes[0], int)
        or installed_sizes[0] < 0
    ):
        raise PcAgentError("RPM header installed-size metadata is invalid.")

    provides = _zip_dependencies(
        _header_value(
            header, _TAG_PROVIDENAME, _TYPE_STRING_ARRAY, label="provides"
        ),
        _header_value(header, _TAG_PROVIDEFLAGS, _TYPE_INT32, label="provides"),
        _header_value(
            header, _TAG_PROVIDEVERSION, _TYPE_STRING_ARRAY, label="provides"
        ),
        label="provides",
    )
    requires = tuple(
        entry
        for entry in _zip_dependencies(
            _header_value(
                header, _TAG_REQUIRENAME, _TYPE_STRING_ARRAY, label="requires"
            ),
            _header_value(
                header, _TAG_REQUIREFLAGS, _TYPE_INT32, label="requires"
            ),
            _header_value(
                header, _TAG_REQUIREVERSION, _TYPE_STRING_ARRAY, label="requires"
            ),
            label="requires",
        )
        if not entry[0].startswith("rpmlib(")
    )
    return _RpmMetadata(
        name=string(_TAG_NAME, _TYPE_STRING, "name"),
        version=string(_TAG_VERSION, _TYPE_STRING, "version"),
        release=string(_TAG_RELEASE, _TYPE_STRING, "release"),
        architecture=string(_TAG_ARCH, _TYPE_STRING, "architecture"),
        summary=string(_TAG_SUMMARY, _TYPE_I18NSTRING, "summary"),
        description=string(_TAG_DESCRIPTION, _TYPE_I18NSTRING, "description"),
        packager=string(_TAG_PACKAGER, _TYPE_STRING, "packager"),
        license_name=string(_TAG_LICENSE, _TYPE_STRING, "license"),
        vendor=string(_TAG_VENDOR, _TYPE_STRING, "vendor"),
        group=string(_TAG_GROUP, _TYPE_I18NSTRING, "group"),
        buildhost=string(_TAG_BUILDHOST, _TYPE_STRING, "build host"),
        installed_size=installed_sizes[0],
        archive_size=len(payload),
        header_start=header_start,
        header_end=header_end,
        provides=provides,
        requires=requires,
        files=tuple((item.path, item.is_dir) for item in items),
    )


def _dependency_entry(entry: tuple[str, int, str]) -> str:
    name, flags, version = entry
    attrs = f'name="{_xml_attr(name)}"'
    if flags == 0:
        return f"<rpm:entry {attrs}/>"
    flag_name = _FLAG_NAMES[flags]
    release: str | None = None
    version_text = version
    if "-" in version:
        version_text, release = version.rsplit("-", 1)
        if not version_text or not release:
            raise PcAgentError("RPM dependency version metadata is invalid.")
    attrs += (
        f' flags="{flag_name}" epoch="0" ver="{_xml_attr(version_text)}"'
    )
    if release is not None:
        attrs += f' rel="{_xml_attr(release)}"'
    return f"<rpm:entry {attrs}/>"


def _primary_xml(metadata: _RpmMetadata, package_sha256: str, package_size: int) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        ('<metadata xmlns="http://linux.duke.edu/metadata/common" '
        'xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="1">'),
        '<package type="rpm">',
        f"  <name>{_xml_text(metadata.name)}</name>",
        f"  <arch>{_xml_text(metadata.architecture)}</arch>",
        (
            f'  <version epoch="0" ver="{_xml_attr(metadata.version)}" '
            f'rel="{_xml_attr(metadata.release)}"/>'
        ),
        (
            '  <checksum type="sha256" pkgid="YES">'
            f"{package_sha256}</checksum>"
        ),
        f"  <summary>{_xml_text(metadata.summary)}</summary>",
        f"  <description>{_xml_text(metadata.description)}</description>",
        f"  <packager>{_xml_text(metadata.packager)}</packager>",
        "  <url></url>",
        '  <time file="0" build="0"/>',
        (
            f'  <size package="{package_size}" '
            f'installed="{metadata.installed_size}" archive="{metadata.archive_size}"/>'
        ),
        f'  <location href="{_xml_attr(_package_relative_path())}"/>',
        "  <format>",
        f"    <rpm:license>{_xml_text(metadata.license_name)}</rpm:license>",
        f"    <rpm:vendor>{_xml_text(metadata.vendor)}</rpm:vendor>",
        f"    <rpm:group>{_xml_text(metadata.group)}</rpm:group>",
        f"    <rpm:buildhost>{_xml_text(metadata.buildhost)}</rpm:buildhost>",
        "    <rpm:sourcerpm>(none)</rpm:sourcerpm>",
        (
            f'    <rpm:header-range start="{metadata.header_start}" '
            f'end="{metadata.header_end}"/>'
        ),
        "    <rpm:provides>",
    ]
    lines.extend(
        f"      {_dependency_entry(entry)}" for entry in metadata.provides
    )
    lines.extend(("    </rpm:provides>", "    <rpm:requires>"))
    lines.extend(
        f"      {_dependency_entry(entry)}" for entry in metadata.requires
    )
    lines.append("    </rpm:requires>")
    for path, is_dir in metadata.files:
        if not is_dir and path.startswith(_PRIMARY_FILE_PREFIXES):
            lines.append(f"    <file>{_xml_text(path)}</file>")
    lines.extend(("  </format>", "</package>", "</metadata>"))
    return "\n".join(lines).encode()


def _package_opening(metadata: _RpmMetadata, package_sha256: str) -> str:
    return (
        f'<package pkgid="{package_sha256}" name="{_xml_attr(metadata.name)}" '
        f'arch="{_xml_attr(metadata.architecture)}">'
    )


def _version_line(metadata: _RpmMetadata) -> str:
    return (
        f'  <version epoch="0" ver="{_xml_attr(metadata.version)}" '
        f'rel="{_xml_attr(metadata.release)}"/>'
    )


def _filelists_xml(metadata: _RpmMetadata, package_sha256: str) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<filelists xmlns="http://linux.duke.edu/metadata/filelists" packages="1">',
        _package_opening(metadata, package_sha256),
        _version_line(metadata),
    ]
    for path, is_dir in metadata.files:
        if is_dir:
            lines.append(f'  <file type="dir">{_xml_text(path)}</file>')
        else:
            lines.append(f"  <file>{_xml_text(path)}</file>")
    lines.extend(("</package>", "</filelists>"))
    return "\n".join(lines).encode()


def _other_xml(metadata: _RpmMetadata, package_sha256: str) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<otherdata xmlns="http://linux.duke.edu/metadata/other" packages="1">',
        _package_opening(metadata, package_sha256),
        _version_line(metadata),
        "</package>",
        "</otherdata>",
    ]
    return "\n".join(lines).encode()


def _gzip_deterministic(data: bytes) -> bytes:
    output = gzip.compress(data, compresslevel=9, mtime=0)
    if len(output) > _MAX_REPOSITORY_METADATA_BYTES:
        raise PcAgentError("RPM repository compressed metadata exceeds the size limit.")
    return output


def _metadata_files(
    metadata: _RpmMetadata, package_sha256: str, package_size: int
) -> tuple[dict[str, tuple[bytes, bytes, str]], bytes]:
    raw_by_type = {
        "primary": _primary_xml(metadata, package_sha256, package_size),
        "filelists": _filelists_xml(metadata, package_sha256),
        "other": _other_xml(metadata, package_sha256),
    }
    files: dict[str, tuple[bytes, bytes, str]] = {}
    for kind in ("primary", "filelists", "other"):
        raw = raw_by_type[kind]
        if len(raw) > _MAX_REPOSITORY_METADATA_BYTES:
            raise PcAgentError("RPM repository metadata exceeds the size limit.")
        compressed = _gzip_deterministic(raw)
        checksum = _sha256_bytes(compressed)
        relative = f"repodata/{checksum}-{kind}.xml.gz"
        files[kind] = (raw, compressed, relative)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        ('<repomd xmlns="http://linux.duke.edu/metadata/repo" '
        'xmlns:rpm="http://linux.duke.edu/metadata/rpm">'),
        "  <revision>0</revision>",
    ]
    for kind in ("primary", "filelists", "other"):
        raw, compressed, relative = files[kind]
        lines.extend(
            (
                f'  <data type="{kind}">',
                (
                    '    <checksum type="sha256">'
                    f"{_sha256_bytes(compressed)}</checksum>"
                ),
                (
                    '    <open-checksum type="sha256">'
                    f"{_sha256_bytes(raw)}</open-checksum>"
                ),
                f'    <location href="{_xml_attr(relative)}"/>',
                "    <timestamp>0</timestamp>",
                f"    <size>{len(compressed)}</size>",
                f"    <open-size>{len(raw)}</open-size>",
                "  </data>",
            )
        )
    lines.append("</repomd>")
    return files, "\n".join(lines).encode()


def _write_file(root: Path, relative: str, data: bytes) -> None:
    target = root / Path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(0o644)


def _scan_repository(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise PcAgentError("RPM repository contains a symbolic link.")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PcAgentError("RPM repository contains a non-file entry.")
        relative = path.relative_to(root).as_posix()
        if (
            not relative
            or relative.startswith("../")
            or "/../" in relative
            or "\\" in relative
        ):
            raise PcAgentError("RPM repository contains an unsafe path.")
        size = path.stat().st_size
        if size > _MAX_REPOSITORY_FILE_BYTES:
            raise PcAgentError("RPM repository contains an oversized file.")
        total += size
        if total > _MAX_REPOSITORY_TOTAL_BYTES:
            raise PcAgentError("RPM repository exceeds the total size limit.")
        data = path.read_bytes()
        if len(data) != size:
            raise PcAgentError("RPM repository changed while reading.")
        files[relative] = data
    if not files or len(files) > _MAX_REPOSITORY_FILES:
        raise PcAgentError("RPM repository has an invalid file count.")
    return files


def _manifest_records(files: dict[str, bytes]) -> list[dict[str, Any]]:
    return [
        {"path": path, "size": len(data), "sha256": _sha256_bytes(data)}
        for path, data in sorted(files.items())
        if path != RPM_REPOSITORY_MANIFEST
    ]


def _parse_manifest(data: bytes) -> dict[str, Any]:
    if len(data) > _MAX_REPOSITORY_MANIFEST_BYTES:
        raise PcAgentError("RPM repository manifest exceeds the size limit.")
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PcAgentError("RPM repository manifest is not valid JSON.") from exc
    expected = {
        "format",
        "app",
        "package",
        "version",
        "package_version",
        "architecture",
        "package_filename",
        "package_size",
        "package_sha256",
        "signed",
        "signing_key_fingerprint",
        "files",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected:
        raise PcAgentError("RPM repository manifest has an unexpected shape.")
    if manifest["format"] != _REPOSITORY_FORMAT:
        raise PcAgentError("RPM repository manifest format is unsupported.")
    if manifest["app"] != RPM_PACKAGE_APP_NAME:
        raise PcAgentError("RPM repository manifest app is unexpected.")
    if manifest["package"] != RPM_PACKAGE_NAME:
        raise PcAgentError("RPM repository manifest package is unexpected.")
    if manifest["version"] != __version__:
        raise PcAgentError("RPM repository version does not match this application.")
    if manifest["package_version"] != _package_version():
        raise PcAgentError("RPM repository package version is unexpected.")
    if manifest["architecture"] != RPM_PACKAGE_ARCHITECTURE:
        raise PcAgentError("RPM repository architecture is unexpected.")
    if manifest["package_filename"] != _package_relative_path():
        raise PcAgentError("RPM repository package filename is unexpected.")
    if (
        not isinstance(manifest["package_size"], int)
        or isinstance(manifest["package_size"], bool)
        or manifest["package_size"] <= 0
        or not isinstance(manifest["package_sha256"], str)
        or _SHA256_RE.fullmatch(manifest["package_sha256"]) is None
    ):
        raise PcAgentError("RPM repository package summary is invalid.")
    if not isinstance(manifest["signed"], bool):
        raise PcAgentError("RPM repository signing state is invalid.")
    fingerprint = manifest["signing_key_fingerprint"]
    if manifest["signed"]:
        if not isinstance(fingerprint, str) or _FINGERPRINT_RE.fullmatch(fingerprint) is None:
            raise PcAgentError("RPM repository signing fingerprint is invalid.")
    elif fingerprint is not None:
        raise PcAgentError("Unsigned RPM repository has a signing fingerprint.")

    records = manifest["files"]
    if not isinstance(records, list) or not records:
        raise PcAgentError("RPM repository manifest files are invalid.")
    paths: list[str] = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
            raise PcAgentError("RPM repository manifest contains a malformed record.")
        path = record["path"]
        size = record["size"]
        digest = record["sha256"]
        if (
            not isinstance(path, str)
            or path in paths
            or path == RPM_REPOSITORY_MANIFEST
            or path.startswith("/")
            or "\\" in path
            or ".." in Path(path).parts
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > _MAX_REPOSITORY_FILE_BYTES
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
        ):
            raise PcAgentError("RPM repository manifest contains an invalid record.")
        paths.append(path)
    if paths != sorted(paths):
        raise PcAgentError("RPM repository manifest records are not sorted.")
    return manifest


def _signing_material(
    repomd_data: bytes,
    *,
    signing_key: str,
    gnupg_home: Path | None,
    executable: str,
    runner: GpgRunner | None,
) -> tuple[str, bytes, bytes]:
    key = _validate_signing_key(signing_key)
    fingerprint = _gpg_fingerprint(
        executable,
        selector=key,
        secret=True,
        gnupg_home=gnupg_home,
        runner=runner,
    )
    public_key = _run_gpg(
        (executable, "--batch", "--armor", "--export", fingerprint),
        input_data=None,
        gnupg_home=gnupg_home,
        runner=runner,
    )
    if (
        not public_key
        or len(public_key) > _MAX_PUBLIC_KEY_BYTES
        or not public_key.startswith(b"-----BEGIN PGP PUBLIC KEY BLOCK-----")
    ):
        raise PcAgentError("GnuPG returned an invalid repository public key.")
    signature = _run_gpg(
        (
            executable,
            "--batch",
            "--yes",
            "--pinentry-mode",
            "loopback",
            "--digest-algo",
            "SHA512",
            "--local-user",
            fingerprint,
            "--armor",
            "--detach-sign",
        ),
        input_data=repomd_data,
        gnupg_home=gnupg_home,
        runner=runner,
    )
    if (
        not signature
        or len(signature) > _MAX_SIGNATURE_BYTES
        or not signature.startswith(b"-----BEGIN PGP SIGNATURE-----")
    ):
        raise PcAgentError("GnuPG returned an invalid repository signature.")
    return fingerprint, public_key, signature


def _verify_signature(
    root: Path,
    *,
    keyring_data: bytes,
    expected_fingerprint: str,
    executable: str,
    runner: GpgRunner | None,
) -> None:
    repomd = root / "repodata" / "repomd.xml"
    signature = root / "repodata" / "repomd.xml.asc"
    with tempfile.TemporaryDirectory(prefix="pc-agent-gnupg-") as name:
        home = Path(name)
        if os.name == "posix":
            home.chmod(0o700)
        _run_gpg(
            (executable, "--batch", "--import"),
            input_data=keyring_data,
            gnupg_home=home,
            runner=runner,
        )
        actual_fingerprint = _gpg_fingerprint(
            executable,
            selector=None,
            secret=False,
            gnupg_home=home,
            runner=runner,
        )
        if actual_fingerprint != expected_fingerprint:
            raise PcAgentError("RPM repository keyring fingerprint does not match.")
        _run_gpg(
            (executable, "--batch", "--verify", str(signature), str(repomd)),
            input_data=None,
            gnupg_home=home,
            runner=runner,
        )


def build_rpm_repository(
    root_dir: Path,
    package_path: Path,
    output_path: Path,
    *,
    signing_key: str | None = None,
    gnupg_home: Path | None = None,
    gpg_executable: str = "gpg",
    gpg_runner: GpgRunner | None = None,
) -> RpmRepositoryInfo:
    """Build and verify a static RPM repository around a verified ``.rpm``."""

    root = root_dir.expanduser().resolve()
    destination = _validate_output_path(root, output_path)
    executable = _validate_gpg_executable(gpg_executable)
    validated_gnupg_home = _validate_gnupg_home(gnupg_home)
    if signing_key is None and validated_gnupg_home is not None:
        raise PcAgentError("A GnuPG home requires an RPM repository signing key.")

    package_info = verify_rpm_package(package_path)
    package_data = _read_package(package_info.path)
    if len(package_data) != package_info.package_bytes or _sha256_bytes(
        package_data
    ) != package_info.package_sha256:
        raise PcAgentError("RPM package changed while building the repository.")
    metadata = _read_rpm_metadata(package_info.path)
    metadata_files, repomd_data = _metadata_files(
        metadata, package_info.package_sha256, len(package_data)
    )

    signing_fingerprint: str | None = None
    public_key: bytes | None = None
    signature: bytes | None = None
    if signing_key is not None:
        signing_fingerprint, public_key, signature = _signing_material(
            repomd_data,
            signing_key=signing_key,
            gnupg_home=validated_gnupg_home,
            executable=executable,
            runner=gpg_runner,
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent)
    )
    try:
        package_relative = _package_relative_path()
        _write_file(temporary, package_relative, package_data)
        for (_raw, compressed, relative) in metadata_files.values():
            _write_file(temporary, relative, compressed)
        _write_file(temporary, "repodata/repomd.xml", repomd_data)
        if signing_key is not None:
            assert public_key is not None
            assert signature is not None
            _write_file(temporary, RPM_REPOSITORY_KEYRING, public_key)
            _write_file(temporary, "repodata/repomd.xml.asc", signature)

        staged_files = _scan_repository(temporary)
        manifest = {
            "format": _REPOSITORY_FORMAT,
            "app": RPM_PACKAGE_APP_NAME,
            "package": RPM_PACKAGE_NAME,
            "version": __version__,
            "package_version": _package_version(),
            "architecture": RPM_PACKAGE_ARCHITECTURE,
            "package_filename": package_relative,
            "package_size": len(package_data),
            "package_sha256": package_info.package_sha256,
            "signed": signing_key is not None,
            "signing_key_fingerprint": signing_fingerprint,
            "files": _manifest_records(staged_files),
        }
        manifest_data = (
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        ).encode()
        if len(manifest_data) > _MAX_REPOSITORY_MANIFEST_BYTES:
            raise PcAgentError("RPM repository manifest exceeds the size limit.")
        _write_file(temporary, RPM_REPOSITORY_MANIFEST, manifest_data)

        for path in sorted(temporary.rglob("*"), reverse=True):
            _set_epoch_mtime(path)
        _set_epoch_mtime(temporary)

        verify_rpm_repository(
            temporary,
            gpg_executable=executable,
            gpg_runner=gpg_runner,
        )
        if destination.exists() or destination.is_symlink():
            raise PcAgentError(f"RPM repository output already exists: {destination}.")
        temporary.replace(destination)
    finally:
        if temporary.exists() or temporary.is_symlink():
            shutil.rmtree(temporary, ignore_errors=True)

    return verify_rpm_repository(
        destination,
        gpg_executable=executable,
        gpg_runner=gpg_runner,
    )


def verify_rpm_repository(
    repository_path: Path,
    *,
    gpg_executable: str = "gpg",
    gpg_runner: GpgRunner | None = None,
) -> RpmRepositoryInfo:
    """Verify a Phase 51 RPM repository without invoking dnf/yum/rpm."""

    raw = repository_path.expanduser()
    if raw.is_symlink() or not raw.is_dir():
        raise PcAgentError(f"RPM repository was not found: {raw}.")
    root = raw.resolve()
    executable = _validate_gpg_executable(gpg_executable)
    files = _scan_repository(root)
    if RPM_REPOSITORY_MANIFEST not in files:
        raise PcAgentError("RPM repository manifest is missing.")
    manifest = _parse_manifest(files[RPM_REPOSITORY_MANIFEST])
    signed = bool(manifest["signed"])
    fingerprint = manifest["signing_key_fingerprint"]

    records = _manifest_records(files)
    if manifest["files"] != records:
        raise PcAgentError("RPM repository manifest does not match its files.")

    package_relative = _package_relative_path()
    if package_relative not in files:
        raise PcAgentError("RPM repository package is missing.")
    package_data = files[package_relative]
    if (
        len(package_data) != manifest["package_size"]
        or _sha256_bytes(package_data) != manifest["package_sha256"]
    ):
        raise PcAgentError("RPM repository package does not match its manifest.")
    package_path = root / Path(package_relative)
    package_info = verify_rpm_package(package_path)
    if package_info.package_sha256 != manifest["package_sha256"]:
        raise PcAgentError("RPM repository package verification does not match.")

    metadata = _read_rpm_metadata(package_path)
    expected_metadata, expected_repomd = _metadata_files(
        metadata, package_info.package_sha256, len(package_data)
    )
    expected_paths = {
        RPM_REPOSITORY_MANIFEST,
        package_relative,
        "repodata/repomd.xml",
    }
    expected_paths.update(
        relative for _raw, _compressed, relative in expected_metadata.values()
    )
    if signed:
        expected_paths.update(
            {RPM_REPOSITORY_KEYRING, "repodata/repomd.xml.asc"}
        )
    if set(files) != expected_paths:
        raise PcAgentError("RPM repository file inventory is unexpected.")
    if files["repodata/repomd.xml"] != expected_repomd:
        raise PcAgentError("RPM repository repomd.xml does not match its package.")

    for (raw_xml, compressed, relative) in expected_metadata.values():
        if files[relative] != compressed:
            raise PcAgentError(f"RPM repository metadata is unexpected: {relative}.")
        decompressed = _gunzip_bounded(
            files[relative], limit=_MAX_REPOSITORY_METADATA_BYTES
        )
        if decompressed != raw_xml:
            raise PcAgentError(f"RPM repository metadata is invalid: {relative}.")
        try:
            ElementTree.fromstring(decompressed)
        except ElementTree.ParseError as exc:
            raise PcAgentError(
                f"RPM repository metadata is not valid XML: {relative}."
            ) from exc
    try:
        ElementTree.fromstring(files["repodata/repomd.xml"])
    except ElementTree.ParseError as exc:
        raise PcAgentError("RPM repository repomd.xml is not valid XML.") from exc

    if signed:
        if not isinstance(fingerprint, str):
            raise PcAgentError("RPM repository signing fingerprint is invalid.")
        keyring_data = files[RPM_REPOSITORY_KEYRING]
        signature = files["repodata/repomd.xml.asc"]
        if (
            not keyring_data
            or len(keyring_data) > _MAX_PUBLIC_KEY_BYTES
            or not keyring_data.startswith(b"-----BEGIN PGP PUBLIC KEY BLOCK-----")
        ):
            raise PcAgentError("RPM repository keyring is invalid.")
        if (
            not signature
            or len(signature) > _MAX_SIGNATURE_BYTES
            or not signature.startswith(b"-----BEGIN PGP SIGNATURE-----")
        ):
            raise PcAgentError("RPM repository repomd signature is invalid.")
        _verify_signature(
            root,
            keyring_data=keyring_data,
            expected_fingerprint=fingerprint,
            executable=executable,
            runner=gpg_runner,
        )

    return RpmRepositoryInfo(
        path=root,
        version=__version__,
        package_version=_package_version(),
        architecture=RPM_PACKAGE_ARCHITECTURE,
        package_path=package_path,
        package_sha256=package_info.package_sha256,
        repomd_sha256=_sha256_bytes(files["repodata/repomd.xml"]),
        manifest_sha256=_sha256_bytes(files[RPM_REPOSITORY_MANIFEST]),
        file_count=len(files),
        total_bytes=sum(len(data) for data in files.values()),
        signed=signed,
        signing_key_fingerprint=fingerprint if signed else None,
        files=tuple(records),
    )


__all__ = [
    "RPM_REPOSITORY_KEYRING",
    "RPM_REPOSITORY_MANIFEST",
    "RpmRepositoryInfo",
    "build_rpm_repository",
    "verify_rpm_repository",
]
