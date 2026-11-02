"""Phase 51: RPM repository metadata and optional signing."""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import tomllib
from pathlib import Path
from xml.etree import ElementTree

import pytest

from pc_agent import __version__
from pc_agent.cli import _run_release_command, build_parser
from pc_agent.config import Settings
from pc_agent.errors import PcAgentError
from pc_agent.release import release_candidate_files
from pc_agent.rpm_package import build_rpm_package
from pc_agent.rpm_repository import (
    RPM_REPOSITORY_KEYRING,
    RPM_REPOSITORY_MANIFEST,
    build_rpm_repository,
    verify_rpm_repository,
)
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[1]
_FAKE_FINGERPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"
_FAKE_SUBKEY_FINGERPRINT = "F" * 40
_FAKE_PUBLIC_KEY = (
    b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nFAKE-PHASE51\n"
    b"-----END PGP PUBLIC KEY BLOCK-----\n"
)


def _built_package(tmp_path: Path) -> Path:
    output = tmp_path / "self-healing-pc-agent.rpm"
    info = build_rpm_package(ROOT, output)
    assert info.path == output.resolve()
    return output


def _repository_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _fake_signature(repomd: bytes) -> bytes:
    return (
        b"-----BEGIN PGP SIGNATURE-----\n"
        + hashlib.sha256(repomd).hexdigest().encode()
        + b"\n-----END PGP SIGNATURE-----\n"
    )


def _fake_gpg(
    args: tuple[str, ...], input_data: bytes | None, gnupg_home: Path | None
) -> bytes:
    assert args[0] == "fake-gpg"
    assert gnupg_home is None or isinstance(gnupg_home, Path)
    if "--list-secret-keys" in args or "--list-keys" in args:
        primary_record = "sec" if "--list-secret-keys" in args else "pub"
        subkey_record = "ssb" if "--list-secret-keys" in args else "sub"
        listing = "\n".join(
            (
                f"{primary_record}:u:255:22:0123456789ABCDEF::::::scESC:::",
                "fpr" + ":" * 9 + _FAKE_FINGERPRINT + ":",
                f"{subkey_record}:u:255:22:FEDCBA9876543210::::::e:::",
                "fpr" + ":" * 9 + _FAKE_SUBKEY_FINGERPRINT + ":",
                "uid:u::::::::Phase 51 Test <phase51@example.invalid>::",
            )
        )
        return (listing + "\n").encode()
    if "--export" in args:
        assert "--armor" in args
        assert _FAKE_FINGERPRINT in args
        return _FAKE_PUBLIC_KEY
    if "--detach-sign" in args:
        assert "--armor" in args
        assert input_data is not None
        return _fake_signature(input_data)
    if "--import" in args:
        assert input_data == _FAKE_PUBLIC_KEY
        return b""
    if "--verify" in args:
        paths = [Path(item) for item in args[args.index("--verify") + 1 :]]
        assert len(paths) == 2
        signature, repomd = paths
        if signature.read_bytes() != _fake_signature(repomd.read_bytes()):
            raise PcAgentError("fake signature mismatch")
        return b""
    raise AssertionError(f"unexpected fake GPG arguments: {args!r}")


def _refresh_manifest_record(repository: Path, relative: str) -> None:
    manifest_path = repository / RPM_REPOSITORY_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    data = (repository / Path(relative)).read_bytes()
    for record in manifest["files"]:
        if record["path"] == relative:
            record["size"] = len(data)
            record["sha256"] = hashlib.sha256(data).hexdigest()
            break
    else:  # pragma: no cover - protects the test helper itself.
        raise AssertionError(f"manifest record missing: {relative}")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _metadata_xml(repository: Path, kind: str) -> ElementTree.Element:
    matches = list(repository.glob(f"repodata/*-{kind}.xml.gz"))
    assert len(matches) == 1
    return ElementTree.fromstring(gzip.decompress(matches[0].read_bytes()))


def test_phase51_builds_deterministic_unsigned_rpm_repository(
    tmp_path: Path,
) -> None:
    package = _built_package(tmp_path)
    first = tmp_path / "repo-first"
    second = tmp_path / "repo-second"

    first_info = build_rpm_repository(ROOT, package, first)
    second_info = build_rpm_repository(ROOT, package, second)
    assert _repository_files(first) == _repository_files(second)
    assert first_info.package_sha256 == second_info.package_sha256
    assert first_info.repomd_sha256 == second_info.repomd_sha256
    assert first_info.signed is False
    assert first_info.signing_key_fingerprint is None
    assert first_info.version == __version__
    assert first_info.package_version == f"{__version__}-1"
    assert first_info.architecture == "noarch"
    assert first_info.file_count == 6
    assert first_info.to_dict()["repository_format"] == "rpm-repodata"
    assert first_info.to_dict()["package_signed"] is False
    assert first_info.package_path.read_bytes() == package.read_bytes()

    repo_ns = {"repo": "http://linux.duke.edu/metadata/repo"}
    repomd = ElementTree.fromstring((first / "repodata/repomd.xml").read_bytes())
    data_types = [
        item.attrib["type"] for item in repomd.findall("repo:data", repo_ns)
    ]
    assert data_types == ["primary", "filelists", "other"]
    assert repomd.findtext("repo:revision", namespaces=repo_ns) == "0"

    common_ns = {
        "common": "http://linux.duke.edu/metadata/common",
        "rpm": "http://linux.duke.edu/metadata/rpm",
    }
    primary = _metadata_xml(first, "primary")
    package_element = primary.find("common:package", common_ns)
    assert package_element is not None
    assert (
        package_element.findtext("common:name", namespaces=common_ns)
        == "self-healing-pc-agent"
    )
    assert (
        package_element.findtext("common:checksum", namespaces=common_ns)
        == first_info.package_sha256
    )
    location = package_element.find("common:location", common_ns)
    assert location is not None
    assert location.attrib["href"] == (
        f"Packages/self-healing-pc-agent-{__version__}-1.noarch.rpm"
    )
    format_element = package_element.find("common:format", common_ns)
    assert format_element is not None
    header_range = format_element.find("rpm:header-range", common_ns)
    assert header_range is not None
    assert int(header_range.attrib["end"]) > int(header_range.attrib["start"]) > 0
    requires = format_element.findall("rpm:requires/rpm:entry", common_ns)
    require_names = [entry.attrib["name"] for entry in requires]
    assert "/bin/sh" in require_names
    assert "python3" in require_names
    assert not any(name.startswith("rpmlib(") for name in require_names)
    primary_files = [
        item.text for item in format_element.findall("common:file", common_ns)
    ]
    assert "/usr/bin/self-healing-pc-agent" in primary_files
    assert "/usr/bin/self-healing-pc-agent-gui" in primary_files

    filelists_ns = {"filelists": "http://linux.duke.edu/metadata/filelists"}
    filelists = _metadata_xml(first, "filelists")
    listed_package = filelists.find("filelists:package", filelists_ns)
    assert listed_package is not None
    listed_files = {
        (item.text, item.attrib.get("type", "file"))
        for item in listed_package.findall("filelists:file", filelists_ns)
    }
    assert (
        "/usr/share/self-healing-pc-agent/rpm-package-manifest.json",
        "file",
    ) in listed_files
    assert ("/usr/share/self-healing-pc-agent", "dir") in listed_files

    other_ns = {"other": "http://linux.duke.edu/metadata/other"}
    other = _metadata_xml(first, "other")
    assert other.find("other:package", other_ns) is not None


def test_phase51_signed_repository_uses_operator_gpg_key_and_fails_closed(
    tmp_path: Path,
) -> None:
    package = _built_package(tmp_path)
    repository = tmp_path / "repo-signed"
    info = build_rpm_repository(
        ROOT,
        package,
        repository,
        signing_key="phase51@example.invalid",
        gpg_executable="fake-gpg",
        gpg_runner=_fake_gpg,
    )

    assert info.signed is True
    assert info.signing_key_fingerprint == _FAKE_FINGERPRINT
    assert info.signing_key_fingerprint != _FAKE_SUBKEY_FINGERPRINT
    assert info.file_count == 8
    assert (repository / RPM_REPOSITORY_KEYRING).read_bytes() == _FAKE_PUBLIC_KEY
    repomd = (repository / "repodata/repomd.xml").read_bytes()
    assert (
        repository / "repodata/repomd.xml.asc"
    ).read_bytes() == _fake_signature(repomd)
    verified = verify_rpm_repository(
        repository,
        gpg_executable="fake-gpg",
        gpg_runner=_fake_gpg,
    )
    assert verified.manifest_sha256 == info.manifest_sha256

    tampered = tmp_path / "repo-signature-tampered"
    shutil.copytree(repository, tampered)
    signature_path = tampered / "repodata/repomd.xml.asc"
    signature = bytearray(signature_path.read_bytes())
    signature[-10] ^= 0xFF
    signature_path.write_bytes(signature)
    _refresh_manifest_record(tampered, "repodata/repomd.xml.asc")
    with pytest.raises(PcAgentError):
        verify_rpm_repository(
            tampered,
            gpg_executable="fake-gpg",
            gpg_runner=_fake_gpg,
        )


def test_phase51_repository_verification_and_builder_fail_closed(
    tmp_path: Path,
) -> None:
    package = _built_package(tmp_path)
    repository = tmp_path / "repo"
    build_rpm_repository(ROOT, package, repository)

    with pytest.raises(PcAgentError, match="already exists"):
        build_rpm_repository(ROOT, package, repository)
    unsafe_output = ROOT / "src" / "phase51-must-not-be-created"
    with pytest.raises(PcAgentError, match="under workspace"):
        build_rpm_repository(ROOT, package, unsafe_output)
    assert not unsafe_output.exists()

    fake_home = tmp_path / "gnupg-home"
    fake_home.mkdir()
    with pytest.raises(PcAgentError, match="requires an RPM repository signing key"):
        build_rpm_repository(
            ROOT,
            package,
            tmp_path / "home-without-key",
            gnupg_home=fake_home,
        )
    assert not (tmp_path / "home-without-key").exists()

    tampered = tmp_path / "repo-tampered"
    shutil.copytree(repository, tampered)
    primary_path = next(iter(tampered.glob("repodata/*-primary.xml.gz")))
    primary = bytearray(primary_path.read_bytes())
    primary[12] ^= 0xFF
    primary_path.write_bytes(primary)
    with pytest.raises(PcAgentError):
        verify_rpm_repository(tampered)

    extra = tmp_path / "repo-extra-file"
    shutil.copytree(repository, extra)
    (extra / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
    with pytest.raises(PcAgentError, match="manifest does not match"):
        verify_rpm_repository(extra)


def test_phase51_direct_release_cli_build_verify_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = _built_package(tmp_path)
    settings = Settings(
        root_dir=ROOT,
        workspace_dir=tmp_path / "workspace",
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "agent.db",
    )
    parser = build_parser()
    repository = tmp_path / "cli-repository"
    build_args = parser.parse_args(
        [
            "release",
            "rpm-repository",
            str(package),
            "--output",
            str(repository),
            "--json",
        ]
    )
    assert _run_release_command(settings, build_args) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["package"] == "self-healing-pc-agent"
    assert built["version"] == __version__
    assert built["signed"] is False
    assert built["package_signed"] is False
    assert Path(built["path"]) == repository.resolve()

    verify_args = parser.parse_args(
        ["release", "rpm-repository-verify", str(repository), "--json"]
    )
    assert _run_release_command(settings, verify_args) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["package_sha256"] == built["package_sha256"]
    assert verified["repomd_sha256"] == built["repomd_sha256"]

    text_args = parser.parse_args(
        ["release", "rpm-repository-verify", str(repository)]
    )
    assert _run_release_command(settings, text_args) == 0
    text_output = capsys.readouterr().out
    assert "Repository metadata signing: not performed" in text_output
    assert "RPM package OpenPGP signing: not performed" in text_output


def test_phase51_scope_inventory_and_documentation() -> None:
    assert __version__ == "0.55.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(build_default_registry().list_tools()) == 62

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == __version__
    assert project["project"]["dependencies"] == [
        "certifi>=2024.7.4",
        "pypdf[crypto]>=5.0,<7",
        "pypdfium2>=5,<6",
        "Pillow>=10.4,<14",
        "comtypes>=1.4,<2; sys_platform == 'win32'",
    ]

    module = (ROOT / "src/pc_agent/rpm_repository.py").read_text(encoding="utf-8")
    cli = (ROOT / "src/pc_agent/cli.py").read_text(encoding="utf-8")
    phase = (ROOT / "docs/phase51.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tools_doc = (ROOT / "docs/tools.md").read_text(encoding="utf-8")
    phase48 = (ROOT / "docs/phase48.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "build_rpm_repository" in module
    assert "verify_rpm_repository" in module
    assert "repomd.xml" in module
    assert "repomd.xml.asc" in module
    assert "filelists" in module
    assert "rpm-repository" in cli
    assert "rpm-repository-verify" in cli

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "src/pc_agent/rpm_repository.py",
        "docs/phase51.md",
        "tests/test_phase51_rpm_repository.py",
    ):
        assert expected in release_names

    for text in (phase, readme, tools_doc, phase48):
        assert "RPM Repository" in text
    assert "does not generate, import, store, or accept a private key" in phase
    assert "compare the fingerprint through a separate trusted channel" in phase
    assert "RPM package itself is not OpenPGP-signed" in phase
    assert "does not host the repository" in phase
    assert "Live Fedora/RHEL install/upgrade evidence remains future work" in phase
    assert "## 0.53.0 - PHASE 51 RPM Repository" in changelog.splitlines()
