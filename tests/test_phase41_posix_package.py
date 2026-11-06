"""Phase 41: Linux/macOS POSIX installer source packaging."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from pc_agent import __version__
from pc_agent.cli import _run_release_command, build_parser
from pc_agent.config import Settings
from pc_agent.errors import PcAgentError
from pc_agent.posix_package import (
    POSIX_PACKAGE_MANIFEST,
    build_posix_package,
    posix_package_payload_files,
    verify_posix_package,
)
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[1]


def _built(tmp_path: Path, name: str = "posix-package") -> Path:
    output = tmp_path / name
    info = build_posix_package(ROOT, output)
    assert info.path == output.resolve()
    return output


def _copy(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination)
    return destination


def _replace_and_rehash(source: Path, relative: str, old: bytes, new: bytes) -> None:
    target = source / relative
    data = target.read_bytes()
    assert old in data
    target.write_bytes(data.replace(old, new, 1))
    manifest_path = source / POSIX_PACKAGE_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    total = 0
    for record in manifest["files"]:
        record_data = (source / record["path"]).read_bytes()
        record["size"] = len(record_data)
        record["sha256"] = hashlib.sha256(record_data).hexdigest()
        total += len(record_data)
    manifest["total_bytes"] = total
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _run_sh(args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def test_phase41_payload_uses_release_runtime_subset() -> None:
    payload = posix_package_payload_files(ROOT)
    release_files = release_candidate_files(ROOT)
    payload_names = {path.as_posix() for path in payload}

    assert payload_names == {
        path.as_posix() for path in release_files if path.parts[0] != "tests"
    }
    for expected in (
        "scripts/install_posix.sh",
        "scripts/uninstall_posix.sh",
        "scripts/launch_gui.sh",
        "scripts/launch_cli.sh",
        "src/pc_agent/posix_package.py",
        "docs/phase41.md",
        "workspace/.gitkeep",
    ):
        assert expected in payload_names
    for name in payload_names:
        assert not name.startswith(("tests/", "data/"))
        assert name != ".env"
        assert not name.endswith((".db", ".sqlite", ".mjs", ".key", ".pem"))
        if name.startswith("workspace/"):
            assert name == "workspace/.gitkeep"


def test_phase41_builds_deterministic_executable_posix_package(
    tmp_path: Path,
) -> None:
    first = _built(tmp_path, "first")
    second = _built(tmp_path, "second")
    first_info = verify_posix_package(first)
    second_info = verify_posix_package(second)

    assert first_info.version == __version__ == "0.81.2"
    assert first_info.file_count == first_info.payload_file_count + 4
    assert (
        first_info.manifest_sha256,
        first_info.install_script_sha256,
        first_info.total_bytes,
    ) == (
        second_info.manifest_sha256,
        second_info.install_script_sha256,
        second_info.total_bytes,
    )
    assert (first / POSIX_PACKAGE_MANIFEST).read_bytes() == (
        second / POSIX_PACKAGE_MANIFEST
    ).read_bytes()
    assert first_info.to_dict()["signed"] is False
    assert first_info.to_dict()["notarized"] is False
    assert first_info.to_dict()["native_package_format"] is None

    payload_list = (first / "payload-files.txt").read_text(encoding="utf-8").splitlines()
    assert payload_list == sorted(payload_list)
    assert len(payload_list) == first_info.payload_file_count
    assert "scripts/install_posix.sh" in payload_list

    sh = shutil.which("sh") if os.name == "posix" else None
    for relative in (
        "install.sh",
        "uninstall.sh",
        "payload/scripts/install_posix.sh",
        "payload/scripts/uninstall_posix.sh",
        "payload/scripts/launch_gui.sh",
        "payload/scripts/launch_cli.sh",
    ):
        path = first / relative
        assert path.is_file()
        if os.name == "posix":
            # NTFS/Python on Windows does not expose POSIX execute bits; the
            # generated package is still run through ``sh`` on its target OS.
            assert stat.S_IMODE(path.stat().st_mode) & stat.S_IXUSR
        if sh is not None:
            result = subprocess.run(
                [sh, "-n", str(path)],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr

    install = (first / "install.sh").read_text(encoding="utf-8")
    uninstall = (first / "uninstall.sh").read_text(encoding="utf-8")
    assert "sys.version_info >= (3, 11)" in install
    assert "pc-agent gui --check" in install
    assert "sudo " not in install + uninstall
    assert "curl " not in install + uninstall
    assert 'rm -rf "$TARGET_DIR"' not in uninstall


def test_phase41_package_verification_and_builder_fail_closed(
    tmp_path: Path,
) -> None:
    original = _built(tmp_path)

    with pytest.raises(PcAgentError, match="already exists"):
        build_posix_package(ROOT, original)
    unsafe_output = ROOT / "src" / "phase41-must-not-be-created"
    with pytest.raises(PcAgentError, match="under workspace"):
        build_posix_package(ROOT, unsafe_output)
    assert not unsafe_output.exists()

    tampered = _copy(original, tmp_path / "tampered")
    readme = tampered / "payload" / "README.md"
    readme.write_bytes(readme.read_bytes() + b"\ntampered\n")
    with pytest.raises(PcAgentError, match="failed verification"):
        verify_posix_package(tampered)

    extra = _copy(original, tmp_path / "extra")
    (extra / "unexpected.txt").write_text("extra\n", encoding="utf-8")
    with pytest.raises(PcAgentError, match="inventory"):
        verify_posix_package(extra)

    unsafe_script = _copy(original, tmp_path / "unsafe-script")
    _replace_and_rehash(
        unsafe_script,
        "install.sh",
        b"set -eu",
        b"set -eu\nsudo true",
    )
    _replace_and_rehash(
        unsafe_script,
        "payload/scripts/install_posix.sh",
        b"set -eu",
        b"set -eu\nsudo true",
    )
    with pytest.raises(PcAgentError, match="forbidden"):
        verify_posix_package(unsafe_script)


def test_phase41_shell_install_and_uninstall_preserve_user_files(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("POSIX shell execution requires a POSIX host")
    if shutil.which("sh") is None:
        pytest.skip("POSIX shell execution requires sh")

    package = _built(tmp_path)
    home = tmp_path / "home"
    target = tmp_path / "installed app"
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_BIN_HOME": str(home / ".local" / "bin"),
    }

    install = _run_sh(
        [
            "sh",
            str(package / "install.sh"),
            "--prefix",
            str(target),
            "--skip-python-setup",
        ],
        env=env,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    assert (target / "pyproject.toml").is_file()
    assert (target / ".pc-agent-posix-install.txt").is_file()
    assert (target / ".pc-agent-posix-files.txt").is_file()
    cli_wrapper = home / ".local" / "bin" / "self-healing-pc-agent"
    gui_wrapper = home / ".local" / "bin" / "self-healing-pc-agent-gui"
    desktop = (
        home
        / ".local"
        / "share"
        / "applications"
        / "self-healing-pc-agent.desktop"
    )
    assert cli_wrapper.is_file()
    assert gui_wrapper.is_file()
    assert desktop.is_file()
    assert str(target) in cli_wrapper.read_text(encoding="utf-8")
    for wrapper in (cli_wrapper, gui_wrapper):
        syntax = _run_sh(["sh", "-n", str(wrapper)], env=env)
        assert syntax.returncode == 0, syntax.stderr

    (target / ".env").write_text("EXAMPLE=user-secret\n", encoding="utf-8")
    (target / "workspace" / "keep.txt").write_text("keep\n", encoding="utf-8")
    (target / "data" / "keep.txt").write_text("keep\n", encoding="utf-8")
    reinstall = _run_sh(
        [
            "sh",
            str(package / "install.sh"),
            "--prefix",
            str(target),
            "--skip-python-setup",
        ],
        env=env,
    )
    assert reinstall.returncode == 0, reinstall.stdout + reinstall.stderr
    assert (target / ".env").read_text(encoding="utf-8") == "EXAMPLE=user-secret\n"

    dummy_venv = target / ".venv" / "bin"
    dummy_venv.mkdir(parents=True)
    (dummy_venv / "python").write_text("dummy\n", encoding="utf-8")
    uninstall = _run_sh(
        ["sh", str(package / "uninstall.sh"), "--prefix", str(target)],
        env=env,
    )
    assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr
    assert not cli_wrapper.exists()
    assert not gui_wrapper.exists()
    assert not desktop.exists()
    assert not (target / ".venv").exists()
    assert (target / "pyproject.toml").is_file()
    assert (target / ".env").is_file()

    denied = _run_sh(
        [
            "sh",
            str(package / "uninstall.sh"),
            "--prefix",
            str(target),
            "--remove-app",
        ],
        env=env,
    )
    assert denied.returncode == 2
    assert (target / "pyproject.toml").is_file()

    remove_app = _run_sh(
        [
            "sh",
            str(package / "uninstall.sh"),
            "--prefix",
            str(target),
            "--remove-app",
            "--yes",
        ],
        env=env,
    )
    assert remove_app.returncode == 0, remove_app.stdout + remove_app.stderr
    assert not (target / "pyproject.toml").exists()
    assert not (target / "src" / "pc_agent" / "cli.py").exists()
    assert (target / ".env").is_file()
    assert (target / "workspace" / "keep.txt").is_file()
    assert (target / "data" / "keep.txt").is_file()


def test_phase41_direct_release_cli_build_verify_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert len(build_default_registry().list_tools()) == 69
    assert LATEST_SCHEMA_VERSION == 18
    parser = build_parser()
    settings = Settings(
        root_dir=ROOT,
        workspace_dir=tmp_path / "workspace",
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "agent.db",
    )
    output = tmp_path / "cli-posix-package"
    build_args = parser.parse_args(
        ["release", "posix-source", "--output", str(output), "--json"]
    )
    assert _run_release_command(settings, build_args) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["version"] == __version__
    assert built["platform"] == "posix"
    assert built["signed"] is False

    verify_args = parser.parse_args(
        ["release", "posix-verify", str(output), "--json"]
    )
    assert _run_release_command(settings, verify_args) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["manifest_sha256"] == built["manifest_sha256"]

    text_args = parser.parse_args(["release", "posix-verify", str(output)])
    assert _run_release_command(settings, text_args) == 0
    assert "signing and notarization: not performed" in capsys.readouterr().out


def test_phase41_checked_in_scripts_and_release_inventory() -> None:
    scripts = {
        name: (ROOT / "scripts" / name).read_text(encoding="utf-8")
        for name in (
            "install_posix.sh",
            "uninstall_posix.sh",
            "launch_gui.sh",
            "launch_cli.sh",
        )
    }
    combined = "\n".join(scripts.values())
    for forbidden in ("sudo ", "curl ", "wget ", "eval ", "chmod 777"):
        assert forbidden not in combined
    assert "administrator elevation" in scripts["install_posix.sh"]
    assert "--remove-app" in scripts["uninstall_posix.sh"]
    assert ".venv/bin/pc-agent-gui" in scripts["launch_gui.sh"]
    assert ".venv/bin/pc-agent" in scripts["launch_cli.sh"]

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "src/pc_agent/posix_package.py",
        "scripts/install_posix.sh",
        "scripts/uninstall_posix.sh",
        "scripts/launch_gui.sh",
        "scripts/launch_cli.sh",
        "docs/phase41.md",
        "tests/test_phase41_posix_package.py",
    ):
        assert expected in release_names

    phase40 = (ROOT / "docs" / "phase40.md").read_text(encoding="utf-8")
    phase41 = (ROOT / "docs" / "phase41.md").read_text(encoding="utf-8")
    assert "version | 0.42.0" in phase40
    assert "version | 0.43.0" in phase41
    assert "Apple notarization/Gatekeeper acceptance" in phase41
