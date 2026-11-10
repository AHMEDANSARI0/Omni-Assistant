"""0.82.3: the Command Center as desktop software, not just a browser tab.

The user asked the obvious question when the UI opened in a browser: *can this run
as a PC application?* It can, without a Rust toolchain, and these tests pin the
three honest ways it happens:

1. ``pywebview`` (optional ``desktop`` extra) - a real OS webview window;
2. an installed Chromium-family browser in ``--app`` mode - no address bar, no
   tabs, no extra install (Edge ships with Windows);
3. the default browser - always available, still one click.

What is asserted, and why:

* only a loopback origin is ever opened, so the "app window" cannot be pointed at
  a remote page and dressed up as the product;
* the launch is an argv list (never a shell string) and uses a dedicated
  ``--user-data-dir`` so it is a separate instance and not the user's own browser
  session;
* the fallback order is deterministic and the chosen plan says why;
* ``--explain`` reports the plan without binding a port or opening anything;
* ``pywebview`` stays optional: plain ``pip install -e ".[dev]"`` must keep
  working, which is why it lives in its own extra.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from pc_agent import cli
from pc_agent.desktop_window import (
    DesktopWindowPlan,
    chrome_style_argv,
    find_app_browser,
    is_loopback_url,
    launch,
    plan_desktop_window,
)
from pc_agent.errors import ConfigError

# ----------------------------------------------------------------- loopback

@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765/",
        "http://localhost:8765/",
        "http://[::1]:8765/",
        "https://127.0.0.1:9443/",
    ],
)
def test_loopback_urls_are_accepted(url: str) -> None:
    assert is_loopback_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "http://192.168.1.10:8765/",
        "file:///C:/Windows/System32/calc.exe",
        "javascript:alert(1)",
        "http://127.0.0.1.evil.example/",
        "",
    ],
)
def test_non_loopback_urls_are_refused(url: str) -> None:
    assert is_loopback_url(url) is False


def test_plan_refuses_a_remote_origin(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="loopback"):
        plan_desktop_window("https://example.com/", profile_dir=tmp_path / "profile")


# ------------------------------------------------------------ argv contract

def test_app_window_argv_has_no_chrome_and_its_own_profile(tmp_path: Path) -> None:
    argv = chrome_style_argv(
        Path("/opt/chrome"), "http://127.0.0.1:8765/", profile_dir=tmp_path / "ui-window"
    )
    assert isinstance(argv, list), "must be an argv list, never a shell string"
    # Platform-correct, not POSIX-literal: on Windows ``str(Path("/opt/chrome"))``
    # is ``\opt\chrome`` because a Path carries host path semantics. The
    # contract under test is that the executable is passed through unchanged,
    # which is what ``os.fspath`` states on both platforms. A *string* path is
    # asserted verbatim (including a real ``C:\...`` path) in
    # ``tests/test_windows_platform_regressions.py``.
    assert argv[0] == os.fspath(Path("/opt/chrome"))
    assert "--app=http://127.0.0.1:8765/" in argv
    assert any(item.startswith("--user-data-dir=") for item in argv)
    assert str(tmp_path / "ui-window") in " ".join(argv)
    assert any(item.startswith("--window-size=") for item in argv)
    assert "--no-first-run" in argv
    assert "&&" not in " ".join(argv) and ";" not in " ".join(argv)


# ------------------------------------------------------------ plan ordering

def _fake_browser(path: Path) -> Path:
    path.write_bytes(b"MZ")
    return path


def test_pywebview_wins_when_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pc_agent.desktop_window.pywebview_available", lambda: True)
    plan = plan_desktop_window("http://127.0.0.1:8765/", profile_dir=tmp_path)
    assert plan.kind == "pywebview"
    assert plan.opens_without_browser_chrome is True
    assert "webview" in plan.reason


def test_app_window_used_when_a_chromium_browser_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pc_agent.desktop_window.pywebview_available", lambda: False)
    browser = _fake_browser(tmp_path / "msedge.exe")
    monkeypatch.setattr("pc_agent.desktop_window.find_app_browser", lambda: browser)
    plan = plan_desktop_window("http://127.0.0.1:8765/", profile_dir=tmp_path / "p")
    assert plan.kind == "app-window"
    assert plan.executable == browser
    assert plan.opens_without_browser_chrome is True
    assert "--app=http://127.0.0.1:8765/" in plan.argv


def test_browser_is_the_honest_last_resort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pc_agent.desktop_window.pywebview_available", lambda: False)
    monkeypatch.setattr("pc_agent.desktop_window.find_app_browser", lambda: None)
    plan = plan_desktop_window("http://127.0.0.1:8765/", profile_dir=tmp_path)
    assert plan.kind == "browser"
    assert plan.opens_without_browser_chrome is False
    assert "default browser" in plan.reason


def test_prefer_browser_skips_the_app_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pc_agent.desktop_window.pywebview_available", lambda: True)
    plan = plan_desktop_window(
        "http://127.0.0.1:8765/", profile_dir=tmp_path, prefer_browser=True
    )
    assert plan.kind == "browser"


def test_find_app_browser_returns_an_existing_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_browser(tmp_path / "msedge.exe")
    monkeypatch.setattr(
        "pc_agent.desktop_window.BROWSER_CANDIDATES",
        {"win32": (str(fake),), "darwin": (), "linux": ()},
    )
    monkeypatch.setattr("pc_agent.desktop_window.sys.platform", "win32")
    assert find_app_browser() == fake


# ---------------------------------------------------------------- launching

def test_launch_returns_a_process_we_own(tmp_path: Path) -> None:
    """The window process is ours to close, and it starts without a shell."""

    plan = DesktopWindowPlan(
        kind="app-window",
        executable=Path(sys.executable),
        argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        reason="test double",
    )
    child = launch(plan, url="http://127.0.0.1:8765/")
    assert isinstance(child, subprocess.Popen)
    try:
        child.terminate()
        child.wait(timeout=10)
    finally:
        if child.poll() is None:  # pragma: no cover - defensive
            child.kill()
    assert child.poll() is not None


# ------------------------------------------------------------------ CLI

def test_ui_flags_are_wired() -> None:
    parser = cli.build_parser()
    default = parser.parse_args(["ui"])
    assert default.command == "ui"
    assert default.no_browser is False
    assert default.prefer_browser is False
    assert default.explain is False

    browser = parser.parse_args(["ui", "--browser"])
    assert browser.prefer_browser is True

    quiet = parser.parse_args(["ui", "--no-window", "--duration", "3"])
    assert quiet.no_browser is True
    assert quiet.duration == 3.0

    gui = parser.parse_args(["gui", "--explain"])
    assert gui.explain is True
    assert gui.tk is False, "plain `pc-agent gui` opens the Command Center window"
    assert parser.parse_args(["gui", "--tk"]).tk is True


def test_explain_opens_nothing_and_binds_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from pc_agent.config import Settings

    settings = Settings.from_env(tmp_path)
    settings.ensure_directories()

    guarded: list[str] = []

    def refuse(*_args, **_kwargs):
        guarded.append("constructed")
        raise AssertionError("--explain must not construct a server")

    monkeypatch.setattr("pc_agent.api_server.LocalApiServer", refuse)
    code = cli._open_command_center(settings, explain=True)
    assert code == 0
    assert guarded == []
    output = capsys.readouterr().out
    assert "Plan only" in output
    assert "nothing was opened" in output


def test_desktop_extra_is_optional() -> None:
    root = Path(__file__).resolve().parent.parent
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "desktop = [" in pyproject
    assert "pywebview" in pyproject
    # The dev extra must stay installable offline-and-simple.
    dev_block = pyproject.split("dev = [", 1)[1].split("]", 1)[0]
    assert "pywebview" not in dev_block
    module = (root / "src" / "pc_agent" / "desktop_window.py").read_text(encoding="utf-8")
    assert "def pywebview_available" in module
    assert "import webview" in module
    # Importing the module must never require the extra.
    assert "try:\n        import webview" in module
