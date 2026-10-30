"""Phase 38: unsigned Windows installer source packaging."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

from pc_agent import __version__
from pc_agent.cli import _run_release_command, build_parser
from pc_agent.config import Settings
from pc_agent.errors import PcAgentError
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry
from pc_agent.windows_installer import (
    WINDOWS_INSTALLER_APP_ID,
    WINDOWS_INSTALLER_MANIFEST,
    WINDOWS_INSTALLER_SCRIPT,
    build_windows_installer_source,
    verify_windows_installer_source,
    windows_installer_payload_files,
)

ROOT = Path(__file__).resolve().parents[1]


def _built(tmp_path: Path, name: str = "installer-source") -> Path:
    output = tmp_path / name
    info = build_windows_installer_source(ROOT, output)
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

    manifest_path = source / WINDOWS_INSTALLER_MANIFEST
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


def test_phase38_payload_uses_release_runtime_subset() -> None:
    payload = windows_installer_payload_files(ROOT)
    release_files = release_candidate_files(ROOT)
    payload_names = {path.as_posix() for path in payload}

    assert payload_names == {
        path.as_posix() for path in release_files if path.parts[0] != "tests"
    }
    assert "scripts/windows_installer.iss" in payload_names
    assert "scripts/build_windows_installer.ps1" in payload_names
    assert "src/pc_agent/windows_installer.py" in payload_names
    assert "docs/phase38.md" in payload_names
    assert "workspace/.gitkeep" in payload_names

    for name in payload_names:
        assert not name.startswith("tests/")
        assert not name.startswith("data/")
        assert name != ".env"
        assert not name.endswith((".db", ".sqlite", ".mjs", ".key", ".pem"))
        if name.startswith("workspace/"):
            assert name == "workspace/.gitkeep"


def test_phase38_builds_deterministic_hash_verified_installer_source(
    tmp_path: Path,
) -> None:
    first = _built(tmp_path, "first")
    second = _built(tmp_path, "second")
    first_info = verify_windows_installer_source(first)
    second_info = verify_windows_installer_source(second)

    assert first_info.version == __version__ == "0.41.0"
    assert first_info.app_id == WINDOWS_INSTALLER_APP_ID
    assert first_info.file_count == first_info.payload_file_count + 1
    assert (
        first_info.script_sha256,
        first_info.manifest_sha256,
        first_info.total_bytes,
        first_info.file_count,
    ) == (
        second_info.script_sha256,
        second_info.manifest_sha256,
        second_info.total_bytes,
        second_info.file_count,
    )
    assert (first / WINDOWS_INSTALLER_MANIFEST).read_bytes() == (
        second / WINDOWS_INSTALLER_MANIFEST
    ).read_bytes()
    assert first_info.to_dict()["signed"] is False
    assert first_info.to_dict()["authenticode_status"] == "not_performed"

    manifest = json.loads(
        (first / WINDOWS_INSTALLER_MANIFEST).read_text(encoding="utf-8")
    )
    assert manifest["app_id"] == WINDOWS_INSTALLER_APP_ID
    assert manifest["version"] == __version__
    assert manifest["file_count"] == len(manifest["files"])
    assert manifest["total_bytes"] == sum(
        record["size"] for record in manifest["files"]
    )

    script = (first / WINDOWS_INSTALLER_SCRIPT).read_text(encoding="utf-8")
    assert "@@PC_AGENT_VERSION@@" not in script
    assert "@@PC_AGENT_FILES@@" not in script
    assert f"AppId={{{{{WINDOWS_INSTALLER_APP_ID}}}" in script
    assert f'#define MyAppVersion "{__version__}"' in script
    assert "PrivilegesRequired=lowest" in script
    assert r"DefaultDirName={localappdata}\Programs\Self-Healing PC Agent" in script
    assert r"scripts\install_windows.ps1" in script
    assert r"scripts\uninstall_windows.ps1" in script
    assert "-RemoveVirtualEnvironment" in script
    assert r"scripts\launch_gui.cmd" in script
    assert r"scripts\launch_cli.cmd" in script

    source_entries = re.findall(r'^Source: "payload\\[^"]+"', script, flags=re.MULTILINE)
    assert len(source_entries) == first_info.payload_file_count
    assert len(set(source_entries)) == first_info.payload_file_count

    output = first / "output"
    output.mkdir()
    (output / "SelfHealingPCAgent-Setup-0.41.0.exe").write_bytes(b"not-real")
    with_output = verify_windows_installer_source(first)
    assert with_output.compiled_output_count == 1


def test_phase38_verification_and_builder_fail_closed(tmp_path: Path) -> None:
    original = _built(tmp_path)

    with pytest.raises(PcAgentError, match="already exists"):
        build_windows_installer_source(ROOT, original)
    unsafe_output = ROOT / "src" / "phase38-must-not-be-created"
    with pytest.raises(PcAgentError, match="under workspace"):
        build_windows_installer_source(ROOT, unsafe_output)
    assert not unsafe_output.exists()

    tampered = _copy(original, tmp_path / "tampered")
    readme = tampered / "payload" / "README.md"
    readme.write_bytes(readme.read_bytes() + b"\ntampered\n")
    with pytest.raises(PcAgentError, match="failed verification"):
        verify_windows_installer_source(tampered)

    extra = _copy(original, tmp_path / "extra")
    (extra / "payload" / "unexpected.txt").write_text("extra\n", encoding="utf-8")
    with pytest.raises(PcAgentError, match="inventory"):
        verify_windows_installer_source(extra)

    admin = _copy(original, tmp_path / "admin")
    _replace_and_rehash(
        admin,
        WINDOWS_INSTALLER_SCRIPT,
        b"PrivilegesRequired=lowest",
        b"PrivilegesRequired=admin",
    )
    with pytest.raises(PcAgentError, match="safety checks"):
        verify_windows_installer_source(admin)

    unsafe = _copy(original, tmp_path / "unsafe")
    manifest_path = unsafe / WINDOWS_INSTALLER_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../escape.txt"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(PcAgentError):
        verify_windows_installer_source(unsafe)


def test_phase38_direct_release_cli_build_verify_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert len(build_default_registry().list_tools()) == 62
    assert LATEST_SCHEMA_VERSION == 6

    parser = build_parser()
    help_text = parser.format_help()
    assert "release" in help_text

    settings = Settings(
        root_dir=ROOT,
        workspace_dir=tmp_path / "workspace",
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "agent.db",
    )
    output = tmp_path / "cli-installer-source"
    build_args = parser.parse_args(
        [
            "release",
            "installer-source",
            "--output",
            str(output),
            "--json",
        ]
    )
    assert _run_release_command(settings, build_args) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["version"] == __version__
    assert built["app_id"] == WINDOWS_INSTALLER_APP_ID
    assert built["signed"] is False
    assert built["authenticode_status"] == "not_performed"
    assert Path(built["path"]).is_dir()

    verify_args = parser.parse_args(
        ["release", "installer-verify", str(output), "--json"]
    )
    assert _run_release_command(settings, verify_args) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["file_count"] == built["file_count"]
    assert verified["script_sha256"] == built["script_sha256"]

    text_args = parser.parse_args(["release", "installer-verify", str(output)])
    assert _run_release_command(settings, text_args) == 0
    assert "Authenticode signing: not performed" in capsys.readouterr().out


def test_phase38_checked_in_scripts_and_release_inventory() -> None:
    template = (ROOT / "scripts" / "windows_installer.iss").read_text(
        encoding="utf-8"
    )
    build_script = (ROOT / "scripts" / "build_windows_installer.ps1").read_text(
        encoding="utf-8"
    )
    uninstall = (ROOT / "scripts" / "uninstall_windows.ps1").read_text(
        encoding="utf-8"
    )
    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}

    assert template.count("@@PC_AGENT_VERSION@@") == 1
    assert template.count("@@PC_AGENT_FILES@@") == 1
    assert f"AppId={{{{{WINDOWS_INSTALLER_APP_ID}}}" in template
    assert "PrivilegesRequired=lowest" in template
    assert "Type: dirifempty" in template
    assert "SignTool=" not in template
    assert "SignedUninstaller=" not in template

    for fragment in (
        "INNO_SETUP_COMPILER",
        "ISCC.exe",
        "installer-source",
        "installer-verify",
        "will not be overwritten",
        "Authenticode signing was not performed",
        "Get-FileHash",
    ):
        assert fragment in build_script
    lowered_build = build_script.lower()
    for forbidden in (
        "invoke-webrequest",
        "downloadstring",
        "winget",
        "choco ",
        "signtool",
    ):
        assert forbidden not in lowered_build

    assert "RemoveVariable".lower() not in uninstall.lower()
    assert "RemoveVirtualEnvironment" in uninstall
    assert "RemoveUserData" in uninstall
    assert "explicit -Yes switch" in uninstall
    assert "Windows Credential Manager entries are not removed automatically" in uninstall

    for expected in (
        "src/pc_agent/windows_installer.py",
        "scripts/windows_installer.iss",
        "scripts/build_windows_installer.ps1",
        "docs/phase38.md",
        "tests/test_phase38_windows_installer.py",
    ):
        assert expected in release_names
