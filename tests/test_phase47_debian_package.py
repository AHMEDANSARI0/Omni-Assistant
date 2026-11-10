"""Phase 47: deterministic native Debian binary packaging."""

from __future__ import annotations

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
from pc_agent.debian_package import (
    DEBIAN_PACKAGE_NAME,
    build_debian_package,
    verify_debian_package,
)
from pc_agent.errors import PcAgentError
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[1]


def _built(tmp_path: Path, name: str = "self-healing-pc-agent.deb") -> Path:
    output = tmp_path / name
    info = build_debian_package(ROOT, output)
    assert info.path == output.resolve()
    return output


def test_phase47_builds_deterministic_native_debian_package(
    tmp_path: Path,
) -> None:
    first = _built(tmp_path, "first.deb")
    second = _built(tmp_path, "second.deb")
    first_info = verify_debian_package(first)
    second_info = verify_debian_package(second)

    assert first.read_bytes() == second.read_bytes()
    assert first_info.package_sha256 == second_info.package_sha256
    assert first_info.version == __version__
    assert first_info.package_version == f"{__version__}-1"
    assert first_info.architecture == "all"
    assert first_info.file_count == len(first_info.files) + 1
    assert first_info.payload_file_count > 100
    assert first_info.to_dict()["native_package_format"] == "deb"
    assert first_info.to_dict()["signed"] is False

    paths = {record["path"] for record in first_info.files}
    for expected in (
        "usr/bin/self-healing-pc-agent",
        "usr/bin/self-healing-pc-agent-gui",
        "usr/share/applications/self-healing-pc-agent.desktop",
        "usr/share/self-healing-pc-agent/posix-package/install.sh",
        "usr/share/self-healing-pc-agent/posix-package/payload/pyproject.toml",
        "usr/share/self-healing-pc-agent/posix-package/payload/src/pc_agent/debian_package.py",
        "usr/share/self-healing-pc-agent/posix-package/payload/docs/phase47.md",
    ):
        assert expected in paths
    assert not any("/tests/" in path for path in paths)
    assert "usr/share/self-healing-pc-agent/posix-package/payload/.env" not in paths

    dpkg_deb = shutil.which("dpkg-deb")
    if dpkg_deb is not None:
        info = subprocess.run(
            [dpkg_deb, "--info", str(first)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert info.returncode == 0, info.stderr
        assert f"Package: {DEBIAN_PACKAGE_NAME}" in info.stdout
        assert f"Version: {__version__}-1" in info.stdout
        contents = subprocess.run(
            [dpkg_deb, "--contents", str(first)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert contents.returncode == 0, contents.stderr
        assert "./usr/bin/self-healing-pc-agent" in contents.stdout
        assert "./usr/share/applications/self-healing-pc-agent.desktop" in contents.stdout


def test_phase47_debian_verification_and_builder_fail_closed(
    tmp_path: Path,
) -> None:
    original = _built(tmp_path)

    with pytest.raises(PcAgentError, match="already exists"):
        build_debian_package(ROOT, original)
    unsafe_output = ROOT / "src" / "phase47-must-not-be-created.deb"
    with pytest.raises(PcAgentError, match="under workspace"):
        build_debian_package(ROOT, unsafe_output)
    assert not unsafe_output.exists()
    with pytest.raises(PcAgentError, match=".deb suffix"):
        build_debian_package(ROOT, tmp_path / "wrong.zip")

    tampered = tmp_path / "tampered.deb"
    data = bytearray(original.read_bytes())
    data[-10] ^= 0xFF
    tampered.write_bytes(data)
    with pytest.raises(PcAgentError):
        verify_debian_package(tampered)


def test_phase47_system_launcher_bootstraps_per_user_posix_install(
    tmp_path: Path,
) -> None:
    if os.name != "posix" or shutil.which("sh") is None:
        pytest.skip("Debian launcher execution requires POSIX sh")
    dpkg_deb = shutil.which("dpkg-deb")
    if dpkg_deb is None:
        pytest.skip("Debian launcher extraction requires dpkg-deb")

    package = _built(tmp_path)
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    result = subprocess.run(
        [dpkg_deb, "-x", str(package), str(extracted)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    launcher = extracted / "usr/bin/self-healing-pc-agent"
    syntax = subprocess.run(
        ["sh", "-n", str(launcher)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert syntax.returncode == 0, syntax.stderr

    fake_root = tmp_path / "fake-package-root"
    fake_install = fake_root / "posix-package" / "install.sh"
    fake_install.parent.mkdir(parents=True)
    fake_install.write_text(
        """#!/bin/sh
set -eu
TARGET=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix) TARGET=$2; shift 2 ;;
    *) shift ;;
  esac
done
mkdir -p "$TARGET/scripts" "$TARGET/.venv/bin" "$TARGET/data" "$TARGET/workspace"
printf '%s\n' "format=1" "app=self-healing-pc-agent" "version=__VERSION__" > "$TARGET/.pc-agent-posix-install.txt"
printf '%s\n' '#!/bin/sh' 'printf "fake cli %s\n" "$*"' > "$TARGET/scripts/launch_cli.sh"
printf '%s\n' '#!/bin/sh' 'printf "fake gui\n"' > "$TARGET/scripts/launch_gui.sh"
printf '%s\n' '#!/bin/sh' 'exit 0' > "$TARGET/.venv/bin/pc-agent"
printf '%s\n' '#!/bin/sh' 'exit 0' > "$TARGET/.venv/bin/pc-agent-gui"
chmod +x "$TARGET/scripts/launch_cli.sh" "$TARGET/scripts/launch_gui.sh" "$TARGET/.venv/bin/pc-agent" "$TARGET/.venv/bin/pc-agent-gui"
""".replace("__VERSION__", __version__),
        encoding="utf-8",
    )
    fake_install.chmod(0o755)

    target = tmp_path / "user app"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PC_AGENT_DEB_PACKAGE_ROOT": str(fake_root),
        "PC_AGENT_HOME": str(target),
    }
    launched = subprocess.run(
        ["sh", str(launcher), "doctor"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert launched.returncode == 0, launched.stderr
    assert launched.stdout.strip() == "fake cli doctor"
    assert (target / ".pc-agent-posix-install.txt").is_file()


def test_phase47_direct_release_cli_build_verify_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(
        root_dir=ROOT,
        workspace_dir=tmp_path / "workspace",
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "agent.db",
    )
    parser = build_parser()
    output = tmp_path / "cli-package.deb"
    build_args = parser.parse_args(
        [
            "release",
            "debian-package",
            "--output",
            str(output),
            "--maintainer",
            "Phase 47 Test <phase47@example.invalid>",
            "--json",
        ]
    )
    assert _run_release_command(settings, build_args) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["package"] == DEBIAN_PACKAGE_NAME
    assert built["version"] == __version__
    assert built["package_version"] == f"{__version__}-1"
    assert built["maintainer"] == "Phase 47 Test <phase47@example.invalid>"
    assert Path(built["path"]).is_file()

    verify_args = parser.parse_args(
        ["release", "debian-verify", str(output), "--json"]
    )
    assert _run_release_command(settings, verify_args) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["package_sha256"] == built["package_sha256"]

    text_args = parser.parse_args(["release", "debian-verify", str(output)])
    assert _run_release_command(settings, text_args) == 0
    assert "Repository/package signing: not performed" in capsys.readouterr().out


def test_phase47_scope_inventory_and_documentation() -> None:
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

    module = (ROOT / "src/pc_agent/debian_package.py").read_text(encoding="utf-8")
    cli = (ROOT / "src/pc_agent/cli.py").read_text(encoding="utf-8")
    phase = (ROOT / "docs/phase47.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tools_doc = (ROOT / "docs/tools.md").read_text(encoding="utf-8")
    phase41 = (ROOT / "docs/phase41.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "build_debian_package" in module
    assert "verify_debian_package" in module
    assert "debian-binary" in module
    assert "control.tar.gz" in module
    assert "data.tar.gz" in module
    assert "debian-package" in cli
    assert "debian-verify" in cli

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "src/pc_agent/debian_package.py",
        "docs/phase47.md",
        "tests/test_phase47_debian_package.py",
    ):
        assert expected in release_names

    for text in (phase, readme, tools_doc, phase41):
        assert "Native Debian Package" in text
    assert "does not bundle Python wheels" in phase
    assert "RPM, PKG and DMG remain future work" in phase
    assert "repository signing" in phase
    assert "## 0.49.0 - PHASE 47 Native Debian Package" in (
        changelog.splitlines()
    )
