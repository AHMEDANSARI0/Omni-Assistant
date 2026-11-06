"""Phase 48: deterministic native RPM binary packaging."""

from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import struct
import subprocess
import tomllib
from pathlib import Path

import pytest

from pc_agent import __version__
from pc_agent.cli import _run_release_command, build_parser
from pc_agent.config import Settings
from pc_agent.errors import PcAgentError
from pc_agent.release import release_candidate_files
from pc_agent.rpm_package import (
    RPM_PACKAGE_NAME,
    _parse_cpio,
    _parse_header,
    build_rpm_package,
    verify_rpm_package,
)
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[1]


def _built(tmp_path: Path, name: str = "self-healing-pc-agent.rpm") -> Path:
    output = tmp_path / name
    info = build_rpm_package(ROOT, output)
    assert info.path == output.resolve()
    return output


def _payload_files(package: Path) -> dict[str, bytes]:
    data = package.read_bytes()
    _sig, sig_end, _blob = _parse_header(data, 96, region_tag=62, label="signature")
    padded = sig_end + ((-sig_end) % 8)
    _main, main_end, _blob2 = _parse_header(
        data, padded, region_tag=63, label="main"
    )
    with gzip.GzipFile(fileobj=io.BytesIO(data[main_end:])) as stream:
        cpio_data = stream.read()
    return {
        item.path: item.data for item in _parse_cpio(cpio_data) if not item.is_dir
    }


def test_phase48_builds_deterministic_native_rpm_package(tmp_path: Path) -> None:
    first = _built(tmp_path, "first.rpm")
    second = _built(tmp_path, "second.rpm")
    first_info = verify_rpm_package(first)
    second_info = verify_rpm_package(second)

    assert first.read_bytes() == second.read_bytes()
    assert first_info.package_sha256 == second_info.package_sha256
    assert first_info.version == __version__
    assert first_info.package_version == f"{__version__}-1"
    assert first_info.architecture == "noarch"
    assert first_info.file_count == len(first_info.files) + 1
    assert first_info.payload_file_count > 100
    assert first_info.to_dict()["native_package_format"] == "rpm"
    assert first_info.to_dict()["signed"] is False

    # RPM lead magic and v4 signature/main header regions.
    data = first.read_bytes()
    assert data[:4] == b"\xed\xab\xee\xdb"
    assert data[96 : 96 + 4] == b"\x8e\xad\xe8\x01"
    sig_tags = struct.unpack_from("!I", data, 96 + 8)[0]
    assert sig_tags == 6

    paths = {record["path"] for record in first_info.files}
    for expected in (
        "/usr/bin/self-healing-pc-agent",
        "/usr/bin/self-healing-pc-agent-gui",
        "/usr/share/applications/self-healing-pc-agent.desktop",
        "/usr/share/self-healing-pc-agent/posix-package/install.sh",
        "/usr/share/self-healing-pc-agent/posix-package/payload/pyproject.toml",
        "/usr/share/self-healing-pc-agent/posix-package/payload/src/pc_agent/rpm_package.py",
        "/usr/share/self-healing-pc-agent/posix-package/payload/docs/phase48.md",
    ):
        assert expected in paths
    assert not any("/tests/" in path for path in paths)
    assert "/usr/share/self-healing-pc-agent/posix-package/payload/.env" not in paths

    # Cross-check with a real rpm tool when the host provides one.
    rpm_tool = shutil.which("rpmquery") or shutil.which("rpm")
    if rpm_tool is not None:
        result = subprocess.run(
            [
                rpm_tool,
                "--query",
                "--package",
                "--queryformat",
                "%{NAME} %{VERSION}-%{RELEASE} %{ARCH}\n",
                str(first),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            assert result.stdout.strip() == (
                f"{RPM_PACKAGE_NAME} {__version__}-1 noarch"
            )


def test_phase48_rpm_verification_and_builder_fail_closed(tmp_path: Path) -> None:
    original = _built(tmp_path)

    with pytest.raises(PcAgentError, match="already exists"):
        build_rpm_package(ROOT, original)
    unsafe_output = ROOT / "src" / "phase48-must-not-be-created.rpm"
    with pytest.raises(PcAgentError, match="under workspace"):
        build_rpm_package(ROOT, unsafe_output)
    assert not unsafe_output.exists()
    with pytest.raises(PcAgentError, match=".rpm suffix"):
        build_rpm_package(ROOT, tmp_path / "wrong.zip")

    tampered = tmp_path / "tampered.rpm"
    data = bytearray(original.read_bytes())
    data[-10] ^= 0xFF
    tampered.write_bytes(data)
    with pytest.raises(PcAgentError):
        verify_rpm_package(tampered)

    header_tampered = tmp_path / "header-tampered.rpm"
    data = bytearray(original.read_bytes())
    data[200] ^= 0xFF
    header_tampered.write_bytes(data)
    with pytest.raises(PcAgentError):
        verify_rpm_package(header_tampered)


def test_phase48_system_launcher_bootstraps_per_user_posix_install(
    tmp_path: Path,
) -> None:
    if os.name != "posix" or shutil.which("sh") is None:
        pytest.skip("RPM launcher execution requires POSIX sh")

    package = _built(tmp_path)
    files = _payload_files(package)
    launcher = tmp_path / "system-launcher"
    launcher.write_bytes(files["/usr/bin/self-healing-pc-agent"])
    launcher.chmod(0o755)
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
        "PC_AGENT_RPM_PACKAGE_ROOT": str(fake_root),
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


def test_phase48_direct_release_cli_build_verify_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(
        root_dir=ROOT,
        workspace_dir=tmp_path / "workspace",
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "agent.db",
    )
    parser = build_parser()
    output = tmp_path / "cli-package.rpm"
    build_args = parser.parse_args(
        [
            "release",
            "rpm-package",
            "--output",
            str(output),
            "--packager",
            "Phase 48 Test <phase48@example.invalid>",
            "--json",
        ]
    )
    assert _run_release_command(settings, build_args) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["package"] == RPM_PACKAGE_NAME
    assert built["version"] == __version__
    assert built["package_version"] == f"{__version__}-1"
    assert built["packager"] == "Phase 48 Test <phase48@example.invalid>"
    assert Path(built["path"]).is_file()

    verify_args = parser.parse_args(["release", "rpm-verify", str(output), "--json"])
    assert _run_release_command(settings, verify_args) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["package_sha256"] == built["package_sha256"]

    text_args = parser.parse_args(["release", "rpm-verify", str(output)])
    assert _run_release_command(settings, text_args) == 0
    assert "Repository/package signing: not performed" in capsys.readouterr().out


def test_phase48_scope_inventory_and_documentation() -> None:
    assert __version__ == "0.81.2"
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

    module = (ROOT / "src/pc_agent/rpm_package.py").read_text(encoding="utf-8")
    cli = (ROOT / "src/pc_agent/cli.py").read_text(encoding="utf-8")
    phase = (ROOT / "docs/phase48.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tools_doc = (ROOT / "docs/tools.md").read_text(encoding="utf-8")
    phase47 = (ROOT / "docs/phase47.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "build_rpm_package" in module
    assert "verify_rpm_package" in module
    assert "_TAG_HEADERIMMUTABLE" in module
    assert "070701" in module
    assert "rpm-package" in cli
    assert "rpm-verify" in cli

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "src/pc_agent/rpm_package.py",
        "docs/phase48.md",
        "tests/test_phase48_rpm_package.py",
    ):
        assert expected in release_names

    for text in (phase, readme, tools_doc, phase47):
        assert "Native RPM Package" in text
    assert "does not bundle Python wheels" in phase
    assert "PKG and DMG remain future work" in phase
    assert "repository signing" in phase
    assert "Header SHA256 digest: OK" in phase
    assert "## 0.50.0 - PHASE 48 Native RPM Package" in (
        changelog.splitlines()
    )
