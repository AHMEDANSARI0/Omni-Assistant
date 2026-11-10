"""0.82.4: the desktop installer build is preflighted, not promised.

`npm run tauri:build` cannot run in this sandbox (no Rust toolchain, no
WebView2), so the honest thing to do is make everything *around* that build
verifiable everywhere:

* `scripts/check_desktop_shell.py` validates the shell definition and fails on
  the mistakes that actually break a Tauri build;
* a Windows workflow (`.github/workflows/desktop-windows.yml`) runs the real
  build on a runner that has Rust, and uploads the NSIS/MSI installers.

These tests pin the preflight's behaviour, including the negative case: a broken
tree must fail loudly, not print "READY".
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHELL = ROOT / "frontend" / "src-tauri"
SCRIPT = ROOT / "scripts" / "check_desktop_shell.py"
WORKFLOW = ROOT / ".github" / "workflows" / "desktop-windows.yml"


def run_preflight(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(cwd / "scripts" / "check_desktop_shell.py")],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_preflight_script_exists_and_passes():
    assert SCRIPT.is_file()
    result = run_preflight(ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DESKTOP SHELL: READY" in result.stdout
    # It must report the toolchain honestly rather than assert a build happened.
    assert "cargo" in result.stdout
    assert "frontend/src-tauri/target/release/bundle/" in result.stdout


def test_frontend_dist_resolves_from_the_shell_directory():
    """Regression: `../src/pc_agent/webui` pointed at frontend/src/... , not the tree."""

    config = json.loads((SHELL / "tauri.conf.json").read_text(encoding="utf-8"))
    dist = (SHELL / config["build"]["frontendDist"]).resolve()
    assert dist.is_dir(), config["build"]["frontendDist"]
    assert (dist / "index.html").is_file()
    assert dist == (ROOT / "src" / "pc_agent" / "webui").resolve()
    assert config["build"]["beforeBuildCommand"], "the bundle should rebuild the SPA"


def test_preflight_fails_a_broken_tree(tmp_path: Path):
    """A wrong frontendDist must be a hard failure with a useful message."""

    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    (tree / "src" / "pc_agent").mkdir(parents=True)
    (tree / "frontend" / "src-tauri").mkdir(parents=True)
    shutil.copy2(SCRIPT, tree / "scripts" / "check_desktop_shell.py")
    (tree / "src" / "pc_agent" / "__init__.py").write_text(
        '__version__ = "0.0.0"\n', encoding="utf-8"
    )
    config = json.loads((SHELL / "tauri.conf.json").read_text(encoding="utf-8"))
    config["version"] = "0.0.0"
    config["build"]["frontendDist"] = "../src/pc_agent/webui"  # the old, wrong value
    (tree / "frontend" / "src-tauri" / "tauri.conf.json").write_text(
        json.dumps(config, indent=2), encoding="utf-8"
    )
    (tree / "frontend" / "src-tauri" / "Cargo.toml").write_text(
        '[package]\nname = "x"\nversion = "0.0.0"\n', encoding="utf-8"
    )

    result = run_preflight(tree)
    assert result.returncode == 1
    assert "NOT READY" in result.stdout
    assert "frontendDist" in result.stdout


def test_preflight_rejects_a_remote_window_url(tmp_path: Path):
    tree = tmp_path / "tree2"
    shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns("node_modules", ".git", ".venv", "__pycache__", "target", ".patcher-backups"))
    config_path = tree / "frontend" / "src-tauri" / "tauri.conf.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["app"]["windows"][0]["url"] = "https://example.com/"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    result = run_preflight(tree)
    assert result.returncode == 1
    assert "loopback origin" in result.stdout


def test_windows_workflow_builds_and_uploads_without_signing():
    # `.github/` is part of the release allow-list since 0.82.4, so this file must
    # exist in every tree the suite runs in - a patched install included.
    assert WORKFLOW.is_file(), "desktop workflow must ship with the release tree"
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "runs-on: windows-latest" in text
    assert "npm ci" in text
    assert "@tauri-apps/cli" in text
    assert "actions/upload-artifact" in text
    assert "check_desktop_shell.py" in text

    # No secrets, no certificate material, no silent signing in CI.
    for forbidden in ("PC_AGENT_SIGN_THUMBPRINT", "signtool", "PC_AGENT_SIGN_PFX", "secrets."):
        assert forbidden not in text, f"CI must not do code signing: {forbidden}"


def test_docs_state_that_the_installer_is_user_built():
    guide = (ROOT / "INSTALLATION_GUIDE.md").read_text(encoding="utf-8")
    assert "tauri:build" in guide
    assert "check_desktop_shell.py" in guide
    assert "not built" in guide.lower() or "not verified" in guide.lower()


@pytest.mark.parametrize(
    "relative", ["icons/32x32.png", "icons/icon.ico", "icons/icon.icns"]
)
def test_icons_are_real_files(relative: str):
    path = SHELL / relative
    assert path.is_file()
    head = path.read_bytes()[:8]
    assert head.startswith((b"\x89PNG", b"\x00\x00\x01\x00", b"icns"))
