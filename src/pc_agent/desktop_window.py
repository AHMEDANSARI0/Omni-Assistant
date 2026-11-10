"""Open the Command Center as a desktop window instead of a browser tab.

The Command Center is a normal web app served by the local API, which means the
only thing missing for "it behaves like PC software" is a window without browser
chrome. Three honest ways to get one, in order of preference:

1. **pywebview** - if the optional ``desktop`` extra is installed, the window is
   a real OS webview (WebView2 on Windows, WebKitGTK on Linux, WKWebView on
   macOS). No browser is involved at all.
2. **An installed Chromium-based browser in app mode** - Edge ships with every
   supported Windows install, Chrome and Brave are common; ``--app=<url>`` opens
   a borderless app window with no address bar and no tabs. This needs no extra
   install and no Rust toolchain.
3. **The default browser** - the fallback that always works, still one click.

The Tauri shell in ``frontend/src-tauri`` is the fourth option (a real installer
with its own embedded runtime); it needs a Rust toolchain to build, which is why
it is not the default path here.

Security notes that matter:

* the URL is validated to be loopback before anything is launched - this module
  will never open a remote origin;
* processes are started with an argv list and ``shell=False`` (default), never a
  shell string;
* the app window uses its own ``--user-data-dir`` inside the assistant's data
  directory, so it is a separate application instance (it does not merge into the
  user's normal browser session or inherit its extensions);
* nothing is written to a URL: the bearer token still reaches the page through
  the server-injected ``meta`` tag.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

__all__ = [
    "BROWSER_CANDIDATES",
    "DesktopWindowPlan",
    "chrome_style_argv",
    "find_app_browser",
    "is_loopback_url",
    "launch",
    "plan_desktop_window",
    "pywebview_available",
]

WINDOW_WIDTH = 1560
WINDOW_HEIGHT = 980
WINDOW_MIN_WIDTH = 1024

#: Chromium-family executables worth looking for, per platform. Relative entries
#: are resolved under the usual Program Files roots on Windows.
BROWSER_CANDIDATES: dict[str, tuple[str, ...]] = {
    "win32": (
        r"Microsoft\Edge\Application\msedge.exe",
        r"Google\Chrome\Application\chrome.exe",
        r"BraveSoftware\Brave-Browser\Application\brave.exe",
        r"Chromium\Application\chrome.exe",
    ),
    "darwin": (
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ),
    "linux": (
        "/usr/bin/microsoft-edge",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/brave-browser",
    ),
}

_NO_SHELL_NAMES = ("microsoft-edge", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "brave-browser")


def is_loopback_url(url: str) -> bool:
    """True only for http(s) loopback origins - the only thing we ever open."""

    lowered = url.strip().lower()
    if not lowered.startswith(("http://", "https://")):
        return False
    authority = lowered.split("://", 1)[1].split("/", 1)[0]
    host = authority.rsplit(":", 1)[0]
    return host in {"127.0.0.1", "localhost", "[::1]", "::1"}


def pywebview_available() -> bool:
    """True when the optional ``desktop`` extra is installed and importable."""

    try:
        import webview  # noqa: F401
    except Exception:  # noqa: BLE001 - a broken install is the same as "absent"
        return False
    return True


def _windows_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        value = os.environ.get(variable)
        if value:
            roots.append(Path(value))
    return tuple(roots)


def find_app_browser() -> Path | None:
    """Return a Chromium-family executable that can open an app window."""

    platform = "win32" if sys.platform.startswith("win") else "darwin" if sys.platform == "darwin" else "linux"
    for candidate in BROWSER_CANDIDATES.get(platform, ()):  # type: ignore[arg-type]
        if os.path.isabs(candidate):
            path = Path(candidate)
            if path.is_file():
                return path
            continue
        for root in _windows_roots():
            path = root / candidate
            if path.is_file():
                return path
    for name in _NO_SHELL_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def chrome_style_argv(executable: Path | str, url: str, *, profile_dir: Path | str) -> list[str]:
    """Build the app-window argv.

    ``--app`` gives a window with no address bar, no tabs and no browser menu;
    ``--user-data-dir`` keeps it a separate instance so the user's own browser
    session, extensions and history are untouched; the remaining flags suppress
    first-run prompts that would otherwise steal focus.
    """

    # A caller-supplied string is passed through **verbatim**: a round trip
    # through ``Path`` rewrites separators for the host platform, so on Windows
    # ``\opt\chrome`` would come back as ``\\opt\\chrome`` and a path the caller
    # already resolved would no longer be the one CreateProcess receives.
    executable_text = (
        os.fspath(executable) if isinstance(executable, os.PathLike) else str(executable)
    )
    return [
        executable_text,
        f"--app={url}",
        f"--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}",
        f"--user-data-dir={Path(profile_dir)}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
    ]


@dataclass(frozen=True, slots=True)
class DesktopWindowPlan:
    """What would be launched, and why - so ``--explain`` can be honest."""

    kind: str  # pywebview | app-window | browser
    executable: Path | None
    argv: tuple[str, ...] = ()
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def opens_without_browser_chrome(self) -> bool:
        return self.kind in {"pywebview", "app-window"}


def plan_desktop_window(
    url: str,
    *,
    profile_dir: Path | str,
    allow_pywebview: bool = True,
    prefer_browser: bool = False,
) -> DesktopWindowPlan:
    """Decide how to open ``url`` as a desktop window.

    Raises ``ConfigError`` for a non-loopback URL: this is a local-first product,
    and an "app window" pointing anywhere else would be a way to dress a remote
    page up as the product.
    """

    if not is_loopback_url(url):
        raise ConfigError(
            "The desktop window only opens the local Command Center "
            f"(loopback origin), refusing {url!r}."
        )
    profile_dir = Path(profile_dir)
    if allow_pywebview and not prefer_browser and pywebview_available():
        return DesktopWindowPlan(
            kind="pywebview",
            executable=None,
            reason="pywebview is installed; the window is an OS webview (WebView2/WebKit)",
            detail={"url": url, "width": WINDOW_WIDTH, "height": WINDOW_HEIGHT},
        )
    browser = None if prefer_browser else find_app_browser()
    if browser is not None:
        return DesktopWindowPlan(
            kind="app-window",
            executable=browser,
            argv=tuple(chrome_style_argv(browser, url, profile_dir=profile_dir)),
            reason=f"{browser.name} can run the UI as an app window (no address bar, no tabs)",
            detail={"url": url, "profile_dir": str(profile_dir)},
        )
    return DesktopWindowPlan(
        kind="browser",
        executable=None,
        reason="no Chromium-family browser found; opening the default browser instead",
        detail={"url": url},
    )


def launch(plan: DesktopWindowPlan, *, url: str) -> subprocess.Popen[bytes] | None:
    """Open the planned window. Returns the child process when we spawned one."""

    if plan.kind == "pywebview":
        import webview

        webview.create_window(
            "Omni-Assistant · Command Center",
            url,
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            min_size=(WINDOW_MIN_WIDTH, 700),
            text_select=True,
        )
        # Blocks until the user closes the window. The caller starts the API on a
        # worker thread first, so the webview can own the main thread.
        webview.start()
        return None
    if plan.kind == "app-window" and plan.argv:
        return subprocess.Popen(list(plan.argv))
    import webbrowser

    webbrowser.open(url, new=2)
    return None
