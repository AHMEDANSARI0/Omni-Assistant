"""Phase 49: Windows locale/UTF-8 test hotfix."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from pc_agent import __version__
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[1]


def test_phase49_project_text_reads_and_writes_are_locale_independent() -> None:
    """Every Path text call in checked Python must name UTF-8 explicitly."""

    offenders: list[str] = []
    for base in (ROOT / "src", ROOT / "tests"):
        for path in sorted(base.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in {"read_text", "write_text"}:
                    continue
                encodings = [
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg == "encoding"
                ]
                if len(encodings) != 1:
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}:{node.func.attr}"
                    )
                    continue
                encoding = encodings[0]
                if not isinstance(encoding, ast.Constant) or encoding.value not in {
                    "utf-8",
                    "utf-8-sig",
                }:
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}:{node.func.attr}"
                    )

    assert offenders == []


def test_phase49_scope_documents_decode_as_utf8() -> None:
    providers = (ROOT / "docs/providers.md").read_bytes().decode("utf-8")
    phase45 = (ROOT / "docs/phase45.md").read_bytes().decode("utf-8")
    readme = (ROOT / "README.md").read_bytes().decode("utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_bytes().decode("utf-8")

    # These UTF-8 characters were enough to crash a cp1252 locale read on
    # Windows when the Phase 45 scope test omitted ``encoding=``.
    assert "“model not found”" in providers
    assert "# Phase 45 —" in phase45
    assert "—" in readme
    assert "—" in changelog

    for relative in (
        "tests/test_phase45_macos_keychain.py",
        "tests/test_phase46_windows_installer_acceptance.py",
        "tests/test_phase47_debian_package.py",
        "tests/test_phase48_rpm_package.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert ".read_text()" not in source


def test_phase49_scope_inventory_and_documentation() -> None:
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

    phase = (ROOT / "docs/phase49.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tools_doc = (ROOT / "docs/tools.md").read_text(encoding="utf-8")
    phase45 = (ROOT / "docs/phase45.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "docs/phase49.md",
        "tests/test_phase49_windows_utf8_hotfix.py",
    ):
        assert expected in release_names

    for text in (phase, readme, tools_doc, phase45):
        assert "Windows UTF-8 Test Hotfix" in text
    assert "cp1252" in phase
    assert "PYTHONUTF8=0" in phase
    assert "no registered tool" in phase
    assert "Administrator" in phase
    assert "## 0.51.0 - PHASE 49 Windows UTF-8 Test Hotfix" in changelog.splitlines()
