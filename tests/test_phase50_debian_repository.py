"""Phase 50: Debian APT repository metadata and optional signing."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from pc_agent import __version__
from pc_agent.cli import _run_release_command, build_parser
from pc_agent.config import Settings
from pc_agent.debian_package import build_debian_package
from pc_agent.debian_repository import (
    DEBIAN_REPOSITORY_KEYRING,
    DEBIAN_REPOSITORY_MANIFEST,
    build_debian_repository,
    verify_debian_repository,
)
from pc_agent.errors import PcAgentError
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[1]
_FAKE_FINGERPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"
_FAKE_SUBKEY_FINGERPRINT = "F" * 40
_FAKE_PUBLIC_KEY = b"FAKE-PHASE50-PUBLIC-KEY"


def _built_package(tmp_path: Path) -> Path:
    output = tmp_path / "self-healing-pc-agent.deb"
    info = build_debian_package(ROOT, output)
    assert info.path == output.resolve()
    return output


def _repository_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _fake_detached_signature(release: bytes) -> bytes:
    return b"FAKE-DETACHED:" + hashlib.sha256(release).digest()


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
                "uid:u::::::::Phase 50 Test <phase50@example.invalid>::",
            )
        )
        return (listing + "\n").encode()
    if "--export" in args:
        assert _FAKE_FINGERPRINT in args
        return _FAKE_PUBLIC_KEY
    if "--detach-sign" in args:
        assert input_data is not None
        return _fake_detached_signature(input_data)
    if "--clearsign" in args:
        assert input_data is not None
        return (
            b"-----BEGIN PGP SIGNED MESSAGE-----\n"
            b"Hash: SHA512\n\n"
            + input_data
            + b"-----BEGIN PGP SIGNATURE-----\nFAKE\n"
            b"-----END PGP SIGNATURE-----\n"
        )
    if "--import" in args:
        assert input_data == _FAKE_PUBLIC_KEY
        return b""
    if "--verify" in args:
        paths = [Path(item) for item in args[args.index("--verify") + 1 :]]
        if len(paths) == 2:
            signature, release = paths
            if signature.read_bytes() != _fake_detached_signature(
                release.read_bytes()
            ):
                raise PcAgentError("fake signature mismatch")
            return b""
        if len(paths) == 1:
            data = paths[0].read_bytes()
            if (
                not data.startswith(b"-----BEGIN PGP SIGNED MESSAGE-----")
                or b"Origin: Self-Healing PC Agent" not in data
                or b"FAKE" not in data
            ):
                raise PcAgentError("fake clear signature mismatch")
            return b""
    raise AssertionError(f"unexpected fake GPG arguments: {args!r}")


def _refresh_manifest_record(repository: Path, relative: str) -> None:
    manifest_path = repository / DEBIAN_REPOSITORY_MANIFEST
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


def _apt_oracle(repository: Path, package_sha256: str, tmp_path: Path) -> None:
    if os.name != "posix":
        return
    apt_get = shutil.which("apt-get")
    apt_cache = shutil.which("apt-cache")
    if apt_get is None or apt_cache is None:
        return

    state = tmp_path / "apt-state"
    cache = tmp_path / "apt-cache"
    download = tmp_path / "apt-download"
    (state / "lists" / "partial").mkdir(parents=True)
    (cache / "archives" / "partial").mkdir(parents=True)
    download.mkdir()
    sources = tmp_path / "sources.list"
    sources.write_text(
        f"deb [trusted=yes] file:{repository.as_posix()} stable main\n",
        encoding="utf-8",
    )
    options = [
        "-o",
        f"Dir::State={state}",
        "-o",
        f"Dir::State::lists={state / 'lists'}",
        "-o",
        f"Dir::Cache={cache}",
        "-o",
        f"Dir::Cache::archives={cache / 'archives'}",
        "-o",
        f"Dir::Etc::sourcelist={sources}",
        "-o",
        "Dir::Etc::sourceparts=-",
        "-o",
        "APT::Get::List-Cleanup=0",
    ]

    update = subprocess.run(
        [apt_get, *options, "update"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert update.returncode == 0, update.stderr
    assert "stable/main" in update.stdout

    policy = subprocess.run(
        [apt_cache, *options, "policy", "self-healing-pc-agent"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert policy.returncode == 0, policy.stderr
    assert f"Candidate: {__version__}-1" in policy.stdout

    downloaded = subprocess.run(
        [apt_get, *options, "download", "self-healing-pc-agent"],
        cwd=download,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert downloaded.returncode == 0, downloaded.stderr
    packages = list(download.glob("*.deb"))
    assert len(packages) == 1
    assert hashlib.sha256(packages[0].read_bytes()).hexdigest() == package_sha256


def test_phase50_builds_deterministic_unsigned_apt_repository(
    tmp_path: Path,
) -> None:
    package = _built_package(tmp_path)
    first = tmp_path / "repo-first"
    second = tmp_path / "repo-second"

    first_info = build_debian_repository(ROOT, package, first)
    second_info = build_debian_repository(ROOT, package, second)
    assert _repository_files(first) == _repository_files(second)
    assert first_info.package_sha256 == second_info.package_sha256
    assert first_info.release_sha256 == second_info.release_sha256
    assert first_info.signed is False
    assert first_info.signing_key_fingerprint is None
    assert first_info.version == __version__
    assert first_info.package_version == f"{__version__}-1"
    assert first_info.suite == "stable"
    assert first_info.component == "main"
    assert first_info.architectures == ("all", "amd64")
    assert first_info.file_count == 7
    assert first_info.to_dict()["repository_format"] == "apt"

    assert first_info.package_path.read_bytes() == package.read_bytes()
    release = (first / "dists/stable/Release").read_text(encoding="utf-8")
    for fragment in (
        "Origin: Self-Healing PC Agent",
        "Suite: stable",
        "Codename: stable",
        "Architectures: all amd64",
        "Components: main",
        "MD5Sum:",
        "SHA1:",
        "SHA256:",
        "SHA512:",
        "main/binary-all/Packages.gz",
        "main/binary-amd64/Packages",
    ):
        assert fragment in release

    packages = (
        first / "dists/stable/main/binary-all/Packages"
    ).read_text(encoding="utf-8")
    amd64_packages = (
        first / "dists/stable/main/binary-amd64/Packages"
    ).read_text(encoding="utf-8")
    assert packages == amd64_packages
    for fragment in (
        "Package: self-healing-pc-agent",
        f"Version: {__version__}-1",
        "Architecture: all",
        "Filename: pool/main/s/self-healing-pc-agent/",
        f"SHA256: {first_info.package_sha256}",
    ):
        assert fragment in packages

    _apt_oracle(first, first_info.package_sha256, tmp_path / "apt-oracle")


def test_phase50_signed_repository_uses_operator_gpg_key_and_fails_closed(
    tmp_path: Path,
) -> None:
    package = _built_package(tmp_path)
    repository = tmp_path / "repo-signed"
    info = build_debian_repository(
        ROOT,
        package,
        repository,
        signing_key="phase50@example.invalid",
        gpg_executable="fake-gpg",
        gpg_runner=_fake_gpg,
    )

    assert info.signed is True
    assert info.signing_key_fingerprint == _FAKE_FINGERPRINT
    assert info.signing_key_fingerprint != _FAKE_SUBKEY_FINGERPRINT
    assert info.file_count == 10
    assert (repository / DEBIAN_REPOSITORY_KEYRING).read_bytes() == _FAKE_PUBLIC_KEY
    release = (repository / "dists/stable/Release").read_bytes()
    assert (
        repository / "dists/stable/Release.gpg"
    ).read_bytes() == _fake_detached_signature(release)
    assert (repository / "dists/stable/InRelease").read_bytes().startswith(
        b"-----BEGIN PGP SIGNED MESSAGE-----"
    )
    verified = verify_debian_repository(
        repository,
        gpg_executable="fake-gpg",
        gpg_runner=_fake_gpg,
    )
    assert verified.manifest_sha256 == info.manifest_sha256

    tampered = tmp_path / "repo-signature-tampered"
    shutil.copytree(repository, tampered)
    signature_path = tampered / "dists/stable/Release.gpg"
    signature = bytearray(signature_path.read_bytes())
    signature[-1] ^= 0xFF
    signature_path.write_bytes(signature)
    _refresh_manifest_record(tampered, "dists/stable/Release.gpg")
    with pytest.raises(PcAgentError):
        verify_debian_repository(
            tampered,
            gpg_executable="fake-gpg",
            gpg_runner=_fake_gpg,
        )


def test_phase50_repository_verification_and_builder_fail_closed(
    tmp_path: Path,
) -> None:
    package = _built_package(tmp_path)
    repository = tmp_path / "repo"
    build_debian_repository(ROOT, package, repository)

    with pytest.raises(PcAgentError, match="already exists"):
        build_debian_repository(ROOT, package, repository)
    unsafe_output = ROOT / "src" / "phase50-must-not-be-created"
    with pytest.raises(PcAgentError, match="under workspace"):
        build_debian_repository(ROOT, package, unsafe_output)
    assert not unsafe_output.exists()
    with pytest.raises(PcAgentError, match="suite"):
        build_debian_repository(
            ROOT,
            package,
            tmp_path / "bad-suite",
            suite="../bad",
        )
    assert not (tmp_path / "bad-suite").exists()
    fake_home = tmp_path / "gnupg-home"
    fake_home.mkdir()
    with pytest.raises(PcAgentError, match="requires a Debian repository signing key"):
        build_debian_repository(
            ROOT,
            package,
            tmp_path / "home-without-key",
            gnupg_home=fake_home,
        )
    assert not (tmp_path / "home-without-key").exists()

    tampered = tmp_path / "repo-tampered"
    shutil.copytree(repository, tampered)
    packages_path = tampered / "dists/stable/main/binary-all/Packages"
    packages = bytearray(packages_path.read_bytes())
    packages[10] ^= 0xFF
    packages_path.write_bytes(packages)
    with pytest.raises(PcAgentError):
        verify_debian_repository(tampered)

    extra = tmp_path / "repo-extra-file"
    shutil.copytree(repository, extra)
    (extra / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
    with pytest.raises(PcAgentError, match="inventory"):
        verify_debian_repository(extra)


def test_phase50_direct_release_cli_build_verify_and_json(
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
            "debian-repository",
            str(package),
            "--output",
            str(repository),
            "--suite",
            "bookworm",
            "--json",
        ]
    )
    assert _run_release_command(settings, build_args) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["package"] == "self-healing-pc-agent"
    assert built["version"] == __version__
    assert built["suite"] == "bookworm"
    assert built["signed"] is False
    assert Path(built["path"]) == repository.resolve()

    verify_args = parser.parse_args(
        ["release", "debian-repository-verify", str(repository), "--json"]
    )
    assert _run_release_command(settings, verify_args) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["package_sha256"] == built["package_sha256"]
    assert verified["release_sha256"] == built["release_sha256"]

    text_args = parser.parse_args(
        ["release", "debian-repository-verify", str(repository)]
    )
    assert _run_release_command(settings, text_args) == 0
    text_output = capsys.readouterr().out
    assert "Repository signing: not performed (unsigned repository)" in text_output


def test_phase50_scope_inventory_and_documentation() -> None:
    assert __version__ == "0.82.12"
    assert LATEST_SCHEMA_VERSION == 18
    assert len(build_default_registry().list_tools()) == 69

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == __version__
    assert project["project"]["dependencies"] == [
        "certifi>=2024.7.4",
        "cryptography>=42,<51",
        "pypdf[crypto]>=5.0,<7",
        "pypdfium2>=5,<6",
        "Pillow>=10.4,<14",
        "comtypes>=1.4,<2; sys_platform == 'win32'",
    ]

    module = (ROOT / "src/pc_agent/debian_repository.py").read_text(
        encoding="utf-8"
    )
    cli = (ROOT / "src/pc_agent/cli.py").read_text(encoding="utf-8")
    phase = (ROOT / "docs/phase50.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tools_doc = (ROOT / "docs/tools.md").read_text(encoding="utf-8")
    phase47 = (ROOT / "docs/phase47.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "build_debian_repository" in module
    assert "verify_debian_repository" in module
    assert "Acquire-By-Hash" in module
    assert "Release.gpg" in module
    assert "InRelease" in module
    assert "debian-repository" in cli
    assert "debian-repository-verify" in cli

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "src/pc_agent/debian_repository.py",
        "docs/phase50.md",
        "tests/test_phase50_debian_repository.py",
    ):
        assert expected in release_names

    for text in (phase, readme, tools_doc, phase47):
        assert "Debian APT Repository" in text
    assert "does not generate, import, store, or accept a private key" in phase
    assert "compare the fingerprint through a separate trusted channel" in phase
    assert "does not host the repository" in phase
    assert "Live Debian/Ubuntu install/upgrade evidence remains future work" in phase
    assert (
        "## 0.52.0 - PHASE 50 Debian APT Repository"
        in changelog.splitlines()
    )
