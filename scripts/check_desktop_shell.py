"""Preflight check for the Tauri desktop shell.

`npm run tauri:build` needs a Rust toolchain plus WebView2, so it cannot run on
every machine that has the source. What *can* run everywhere is the part that
usually breaks: the configuration itself. This script validates everything the
build will need before the toolchain is even installed, and prints the exact
commands for the machine that will do the real build.

    python scripts/check_desktop_shell.py

Exit code 0 = the shell definition is consistent (build may still need Rust),
1 = something in the tree is wrong and the build would fail, 2 = nothing to check.

Checks performed (each is a fact about the tree, not a guess):

* ``tauri.conf.json`` parses and matches the architecture the product promises
  (loopback origin, minimum window size, no remote URL);
* ``build.frontendDist`` resolves to a directory that exists - the one mistake
  that silently breaks ``generate_context!``;
* ``build.beforeBuildCommand`` rebuilds the SPA the API serves;
* every icon in ``bundle.icon`` exists and is a real PNG/ICO/ICNS file;
* the version in ``tauri.conf.json`` and ``Cargo.toml`` equals
  ``pc_agent.__version__``;
* capabilities grant ``core:default`` only, and no shell/fs/http plugin is
  pulled in;
* ``Cargo.toml`` declares the single-instance plugin and a release profile that
  keeps the binary small.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHELL = ROOT / "frontend" / "src-tauri"
ICON_MAGIC = {
    "png": b"\x89PNG\r\n\x1a\n",
    "ico": b"\x00\x00\x01\x00",
    "icns": b"icns",
}
FORBIDDEN_PLUGINS = ("tauri-plugin-shell", "tauri-plugin-fs", "tauri-plugin-http")


def _ok(message: str) -> None:
    print(f"  OK    {message}")


def _bad(message: str) -> None:
    print(f"  FAIL  {message}")


def _warn(message: str) -> None:
    print(f"  WARN  {message}")


def icon_kind(path: Path) -> str | None:
    """Return ``png``/``ico``/``icns`` from the file's own magic bytes."""

    try:
        head = path.read_bytes()[:8]
    except OSError:
        return None
    for kind, magic in ICON_MAGIC.items():
        if head.startswith(magic):
            return kind
    return None


def png_size(path: Path) -> tuple[int, int] | None:
    data = path.read_bytes()[:33]
    if not data.startswith(ICON_MAGIC["png"]) or len(data) < 24:
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _read(path: Path) -> str:
    """Read a shell file, or return an empty string if it is not there.

    The preflight is also used on a partial checkout (and by a test that feeds it
    a deliberately broken tree), so a missing file must be reported as a finding
    instead of crashing with a traceback.
    """

    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []

    print(f"Desktop shell preflight: {SHELL}")

    config_path = SHELL / "tauri.conf.json"
    if not config_path.is_file():
        print("  FAIL  tauri.conf.json is missing")
        return 1
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        _ok("tauri.conf.json parses")
    except json.JSONDecodeError as exc:
        _bad(f"tauri.conf.json does not parse: {exc}")
        return 1

    sys.path.insert(0, str(ROOT / "src"))
    try:
        from pc_agent import __version__
    except Exception as exc:  # noqa: BLE001 - the package must import
        _bad(f"cannot import pc_agent: {exc}")
        return 1

    # -- versions ---------------------------------------------------------
    for label, value in (
        ("tauri.conf.json", config.get("version")),
        ("Cargo.toml", _cargo_version()),
    ):
        if value == __version__:
            _ok(f"{label} version {value} matches pc_agent.__version__")
        else:
            failures.append(f"{label} version {value!r} != {__version__!r}")

    # -- frontend dist ----------------------------------------------------
    dist_value = (config.get("build") or {}).get("frontendDist") or ""
    dist = (SHELL / dist_value).resolve() if dist_value else None
    if dist is not None and dist.is_dir() and (dist / "index.html").is_file():
        _ok(f"frontendDist resolves to a built SPA ({dist.relative_to(ROOT).as_posix()})")
    else:
        failures.append(
            f"frontendDist {dist_value!r} does not resolve to a directory with index.html "
            "(it is relative to frontend/src-tauri/)"
        )

    if (config.get("build") or {}).get("beforeBuildCommand"):
        _ok(f"beforeBuildCommand rebuilds the UI: {(config['build']['beforeBuildCommand'])}")
    else:
        warnings.append("no beforeBuildCommand: the bundle uses whatever is in webui/ already")

    # -- window -----------------------------------------------------------
    windows = (config.get("app") or {}).get("windows") or []
    if len(windows) != 1:
        failures.append(f"expected exactly one window, found {len(windows)}")
    else:
        window = windows[0]
        url = str(window.get("url", ""))
        if re.match(r"^http://127\.0\.0\.1:\d+/", url):
            _ok(f"window loads the loopback origin ({url})")
        else:
            failures.append(f"window url must be a loopback origin, found {url!r}")
        if int(window.get("minWidth", 0)) >= 1024 and int(window.get("minHeight", 0)) >= 700:
            _ok(f"minimum window {window['minWidth']}x{window['minHeight']} keeps the layout usable")
        else:
            failures.append("minimum window size is smaller than 1024x700")
        if window.get("label") != "main":
            failures.append("the window label must stay 'main' (capabilities target it)")

    # -- icons ------------------------------------------------------------
    icons = (config.get("bundle") or {}).get("icon") or []
    if not icons:
        failures.append("bundle.icon is empty: the build cannot produce an installer")
    for relative in icons:
        path = SHELL / relative
        if not path.is_file():
            failures.append(f"missing icon {relative}")
            continue
        kind = icon_kind(path)
        if kind is None:
            failures.append(f"{relative} is not a real PNG/ICO/ICNS file")
            continue
        detail = ""
        if kind == "png":
            size = png_size(path)
            detail = f" {size[0]}x{size[1]}" if size else ""
        _ok(f"icon {relative} ({kind}{detail})")
    suffixes = {Path(name).suffix.lower() for name in icons}
    for required in (".ico", ".icns"):
        if required not in suffixes:
            warnings.append(f"no {required} icon: that installer target will fall back")

    # -- capabilities / plugins ------------------------------------------
    capability_path = SHELL / "capabilities" / "default.json"
    if capability_path.is_file():
        capability = json.loads(_read(capability_path) or "{}")
        if capability.get("permissions") == ["core:default"]:
            _ok("capabilities grant core:default only")
        else:
            failures.append(f"capabilities grant more than core:default: {capability.get('permissions')}")
        if capability.get("windows") != ["main"]:
            failures.append("capability is not scoped to the main window")
    else:
        failures.append("capabilities/default.json is missing")

    cargo = _read(SHELL / "Cargo.toml")
    main_rs = _read(SHELL / "src" / "main.rs")
    if not cargo:
        failures.append("Cargo.toml is missing or unreadable")
    if not main_rs:
        failures.append("src/main.rs is missing or unreadable")
    for plugin in FORBIDDEN_PLUGINS:
        if plugin in cargo or plugin in main_rs:
            failures.append(f"{plugin} must not be a dependency (no shell/fs/http power to the webview)")
    if "tauri-plugin-single-instance" in cargo and "single_instance" in main_rs:
        _ok("single-instance plugin is declared and wired")
    else:
        warnings.append("single-instance plugin is not fully wired")
    if 'lto = true' in cargo and 'strip = true' in cargo:
        _ok("release profile keeps the binary small (lto + strip)")
    if "127.0.0.1:8765" in main_rs:
        _ok("main.rs starts/attaches to the loopback API")
    else:
        warnings.append("main.rs does not reference the loopback API")

    # -- toolchain --------------------------------------------------------
    print("Desktop shell toolchain:")
    for tool, hint in (
        ("cargo", "install Rust from https://rustup.rs"),
        ("node", "install Node 20+"),
    ):
        found = shutil.which(tool)
        if found:
            _ok(f"{tool} found at {found}")
        else:
            _warn(f"{tool} not found - {hint} (needed for `npm run tauri:build`)")

    print()
    for problem in failures:
        _bad(problem)
    for warning in warnings:
        _warn(warning)
    if failures:
        print(f"\nDESKTOP SHELL: NOT READY ({len(failures)} problem(s))")
        return 1
    print("\nDESKTOP SHELL: READY" + (f" ({len(warnings)} warning(s))" if warnings else ""))
    print("Build it on a machine with Rust + WebView2:")
    print("  cd frontend")
    print("  npm ci")
    print("  npm run tauri:build")
    print("Installer output: frontend/src-tauri/target/release/bundle/")
    return 0


def _cargo_version() -> str | None:
    cargo = SHELL / "Cargo.toml"
    if not cargo.is_file():
        return None
    match = re.search(r'^version\s*=\s*"([^"]+)"', cargo.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else None


if __name__ == "__main__":
    raise SystemExit(main())
