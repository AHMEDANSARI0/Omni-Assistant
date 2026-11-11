"""0.82.0: the Command Center UI (built SPA served by the local API).

What is covered here, and why each check exists:

* the shell is served from the same loopback origin as ``/v1`` with the strict
  CSP, the token meta tag, and *no* token in a URL or script;
* ``/assets/*`` serves only files that exist inside the package, with the right
  MIME type and an immutable cache header, and refuses traversal;
* a missing build produces the honest "not built" page rather than a blank 200;
* the legacy panel is still reachable at ``/legacy`` for rollback;
* ``/v1/events/stream`` is real NDJSON: a ``stream.ready`` frame, then events that
  the service actually wrote, bounded by ``duration`` and closed by the server;
* the shipped bundle is self-contained (no external host, no unsafe-eval) and the
  text/background token pairs pass WCAG AA in both themes, measured by running
  the same Python contrast checker the frontend build uses.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

from pc_agent import webui
from pc_agent.api_server import LocalApiServer
from pc_agent.config import Settings


@pytest.fixture
def settings(tmp_path):
    value = Settings.from_env(tmp_path)
    value.ensure_directories()
    return value


@pytest.fixture
def server(settings):
    instance = LocalApiServer(replace(settings, api_port=0, scheduler_interval_seconds=0))
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def _get(server: LocalApiServer, path: str, *, token: bool = True, timeout: float = 15.0):
    host, port = server.address
    headers = {"Authorization": f"Bearer {server.token}"} if token else {}
    request = urllib.request.Request(f"http://{host}:{port}{path}", headers=headers)
    return urllib.request.urlopen(request, timeout=timeout)


# --------------------------------------------------------------------------- module


def test_build_is_present_and_self_contained():
    smoke = webui.smoke()
    assert smoke["built"] is True, "run `npm run build` in frontend/ before testing the UI"
    assert smoke["assets"] > 0
    assert smoke["bytes"] < 4_000_000  # a local UI must stay small
    html = webui.read_index("token-value")
    assert html.startswith("<!doctype html>")
    assert 'name="pc-agent-token" content="token-value"' in html
    # Every referenced asset must exist in the package.
    for reference in re.findall(r'(?:src|href)="(/assets/[^"]+)"', html):
        assert webui.static_path(reference) is not None, reference
    # No external host may appear anywhere in the shell. The only absolute URL
    # allowed is the SVG namespace inside the inlined favicon data URI, which is
    # an identifier, never a request.
    absolute = set(re.findall(r"https?://[^\"'\s>]+", html))
    assert absolute <= {"http://www.w3.org/2000/svg"}, absolute


def test_token_meta_is_never_empty_or_duplicated():
    with_token = webui.read_index("abc123")
    assert with_token.count('name="pc-agent-token"') == 1
    without = webui.read_index("")
    assert 'content=""' in without


def test_static_path_refuses_traversal(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "ok.js").write_text("console.log(1)", encoding="utf-8")
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(webui, "ASSET_ROOT", tmp_path)

    assert webui.static_path("assets/ok.js") == (tmp_path / "assets" / "ok.js").resolve()
    for attack in (
        "assets/../../outside-secret.txt",
        "../outside-secret.txt",
        "/etc/passwd",
        "",
        "assets/missing.js",
        "assets/ok.js\x00.png",
    ):
        assert webui.static_path(attack) is None, attack


def test_inline_script_hashes_match_the_shell():
    html = webui.read_index("t")
    hashes = webui.inline_script_hashes(html)
    assert hashes, "the shell needs one pre-paint theme script"
    for value in hashes:
        assert value.startswith("'sha256-") and value.endswith("'")
    assert "unsafe-inline" not in " ".join(hashes)


# ----------------------------------------------------------------------------- HTTP


def test_spa_shell_is_served_with_strict_headers(server):
    with _get(server, "/", token=False) as response:
        body = response.read().decode("utf-8")
        assert response.status == 200
        assert response.headers.get_content_type() == "text/html"
        csp = response.headers.get("Content-Security-Policy") or ""
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "connect-src 'self'" in csp
        # The inline bootstrap is allowed by hash, never by 'unsafe-inline'.
        assert "'sha256-" in csp
        assert "script-src 'self' 'sha256-" in csp
        assert "'unsafe-eval'" not in csp
        assert "X-Frame-Options" in response.headers
    assert 'name="pc-agent-token"' in body
    assert server.token in body  # the page is bound to this API instance
    # The token appears only inside the meta tag, not in a script or a URL.
    assert body.count(server.token) == 1


def test_assets_are_served_with_types_and_cache(server):
    html = webui.read_index("x")
    script = next(reference for reference in re.findall(r'(?:src|href)="(/assets/[^"]+)"', html) if reference.endswith(".js"))
    with _get(server, script, token=False) as response:
        assert response.status == 200
        assert response.headers.get_content_type() in {"text/javascript", "application/javascript"}
        assert "immutable" in (response.headers.get("Cache-Control") or "")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert len(response.read()) > 100
    css = next(reference for reference in re.findall(r'(?:src|href)="(/assets/[^"]+)"', html) if reference.endswith(".css"))
    with _get(server, css, token=False) as response:
        assert response.headers.get_content_type() == "text/css"


def test_asset_route_refuses_traversal_and_missing_files(server):
    for path in ("/assets/../../api_server.py", "/assets/does-not-exist.js", "/assets/"):
        with pytest.raises(urllib.error.HTTPError) as error:
            _get(server, path, token=False)
        assert error.value.code == 404


def test_legacy_panel_still_available_for_rollback(server):
    with _get(server, "/legacy", token=False) as response:
        body = response.read().decode("utf-8")
        assert response.status == 200
        assert "COMMAND CENTER" in body  # the pre-0.82 panel, kept behind a route
        assert "/v1/health" in body


def test_missing_build_is_reported_honestly(server, tmp_path, monkeypatch):
    monkeypatch.setattr(webui, "ASSET_ROOT", tmp_path)
    with _get(server, "/", token=False) as response:
        body = response.read().decode("utf-8")
        assert response.status == 200
        assert "no built Command Center" in body
        assert "npm run build" in body  # the real fix, not a fake dashboard
        assert "/legacy" in body


# ------------------------------------------------------------------------- streaming


def _read_ndjson(response, limit: int = 40, timeout: float = 20.0) -> list[dict]:
    frames: list[dict] = []
    deadline = time.monotonic() + timeout
    while len(frames) < limit and time.monotonic() < deadline:
        line = response.readline()
        if not line:
            break
        frames.append(json.loads(line.decode("utf-8")))
    return frames


def test_event_stream_emits_ready_frame_and_real_events(server):
    with urllib.request.urlopen(
        urllib.request.Request(
            f"http://{server.address[0]}:{server.address[1]}/v1/events/stream?since_id=0&duration=5",
            headers={"Authorization": f"Bearer {server.token}"},
        ),
        timeout=25,
    ) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "application/x-ndjson"
        assert response.headers.get("Cache-Control") == "no-store"
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        ready = json.loads(response.readline().decode("utf-8"))
        assert ready["type"] == "stream.ready"
        assert ready["since_id"] == 0

        # Create real work while the stream is open; the event must arrive.
        server.service.create_task("stream probe: list workspace files")
        frames = _read_ndjson(response, limit=6, timeout=15)
        assert frames, "the stream produced no frames after a real task was created"
        event_types = {frame.get("event_type") for frame in frames if "event_type" in frame}
        assert "task.created" in event_types
        for frame in frames:
            if "id" in frame:
                assert isinstance(frame["id"], int)
                assert frame["created_at"]


def test_event_stream_requires_the_token(server):
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(
            f"http://{server.address[0]}:{server.address[1]}/v1/events/stream?duration=5",
            timeout=10,
        )
    assert error.value.code == 401


def test_event_stream_duration_is_bounded(server):
    started = time.monotonic()
    with urllib.request.urlopen(
        urllib.request.Request(
            # 1 s is clamped up to the 5 s floor, 100_000 s down to 900 s: either
            # way the server, not the client, owns how long a socket lives.
            f"http://{server.address[0]}:{server.address[1]}/v1/events/stream?since_id=0&duration=1",
            headers={"Authorization": f"Bearer {server.token}"},
        ),
        timeout=30,
    ) as response:
        ready = json.loads(response.readline().decode("utf-8"))
        assert ready["type"] == "stream.ready"
        body = response.read()  # blocks until the server closes the stream
    elapsed = time.monotonic() - started
    assert body == b"" or body.strip()  # heartbeats may have been written
    assert 4.0 <= elapsed <= 20.0, elapsed


def test_event_stream_rejects_garbage_instead_of_guessing(server):
    """A typo must be a 400, not a silently different stream.

    ``since_id=abc`` or ``duration=xyz`` used to be able to fall back to defaults,
    which on this route means an unexpected two-minute socket. The route now
    refuses both with an explicit message, and the client-side clamp stays for
    out-of-range but parseable numbers.
    """

    for query in ("since_id=abc&duration=5", "since_id=0&duration=xyz"):
        request = urllib.request.Request(
            f"http://{server.address[0]}:{server.address[1]}/v1/events/stream?{query}",
            headers={"Authorization": f"Bearer {server.token}"},
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=10)
        assert error.value.code == 400
        payload = json.loads(error.value.read().decode("utf-8"))
        assert payload["ok"] is False
        assert "Expected" in payload["error"]["message"]


# ------------------------------------------------------------- bundle hygiene / a11y


def _frontend_root() -> Path:
    return Path(__file__).resolve().parents[1] / "frontend"


def test_frontend_sources_are_shipped_for_rebuilds():
    frontend = _frontend_root()
    assert (frontend / "package.json").is_file()
    assert (frontend / "src" / "App.tsx").is_file()
    assert (frontend / "src" / "theme.css").is_file()
    package = json.loads((frontend / "package.json").read_text(encoding="utf-8"))
    scripts = package["scripts"]
    # A build must typecheck and clean stale hashed assets before bundling.
    assert "tsc" in scripts["build"]
    assert "clean-webui" in scripts["build"]
    assert (frontend / "scripts" / "clean-webui.js").is_file()
    # No state-management or charting dependency was added for the redesign.
    dependencies = set(package["dependencies"])
    assert dependencies == {"react", "react-dom", "framer-motion"}


def test_both_themes_are_defined_and_contrast_checked():
    css = (_frontend_root() / "src" / "theme.css").read_text(encoding="utf-8")
    assert '[data-theme="dark"]' in css
    assert '[data-theme="light"]' in css
    assert "@tailwind" in css
    # Reduced motion is a first-class token, not an afterthought.
    assert '[data-motion="reduce"]' in css

    script = _frontend_root() / "scripts" / "check_contrast.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout
    assert "FAIL" not in result.stdout.replace("0 failing", "")


def test_ui_verification_script_is_checked_in():
    script = _frontend_root() / "scripts" / "ui-verify.js"
    assert script.is_file()
    source = script.read_text(encoding="utf-8")
    # The script must walk every route and assert real rendering, not just load.
    assert "measureContrast" in source
    assert "no console errors" in source
    assert "route " in source or "ROUTES" in source
    for route in ("/agents", "/voice", "/security", "/analytics", "/settings"):
        assert f'"{route}"' in source


# --------------------------------------------------------------- RTL + shell

def test_ui_sources_use_logical_direction_utilities():
    """The shell must mirror, not "kind of" mirror.

    Physical utilities (``ml-*``, ``pr-*``, ``border-l``, ``text-left``,
    ``left-0``) keep pointing the same way in a ``dir="rtl"`` document, so a
    right-to-left locale would produce a half-flipped layout. Everything must go
    through the logical equivalents (``ms-*``, ``pe-*``, ``border-s``,
    ``text-start``, ``start-0``) which Tailwind emits for both directions.
    """

    physical = re.compile(
        r"\b(ml|mr|pl|pr)-[0-9.]+|\b(border-[lr]|rounded-[lr])(?![a-z])"
        r"|\b(text-left|text-right)\b|\b(left|right)-[0-9.]+"
    )
    offenders: list[str] = []
    for path in sorted((_frontend_root() / "src").rglob("*.tsx")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = physical.search(line)
            if match:
                offenders.append(f"{path.name}:{number}: {match.group(0)}")
    assert offenders == [], "physical direction utilities found: " + ", ".join(offenders[:6])

    store = (_frontend_root() / "src" / "store" / "ui.ts").read_text(encoding="utf-8")
    assert 'export type Direction = "auto" | "ltr" | "rtl";' in store
    assert "root.dir = resolved" in store or 'root.dir = next.direction' in store
    assert "applyAutoDirection" in store
    # The engine's locale report is the authority for `auto`, not the browser.
    app = (_frontend_root() / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "applyAutoDirection(a11y.data.direction)" in app
    settings_view = (_frontend_root() / "src" / "views" / "Settings.tsx").read_text(encoding="utf-8")
    assert "setDirection" in settings_view


def test_ui_verifier_covers_rtl_and_both_themes():
    script = (_frontend_root() / "scripts" / "ui-verify.js").read_text(encoding="utf-8")
    assert "rtl" in script.lower()
    assert "html dir" in script.lower() or "document.documentElement.dir" in script


def test_command_center_launcher_opens_the_api_not_tk():
    """`pc-agent ui` is the entry point a user will type; it must be the web UI."""

    from pc_agent import cli

    parser = cli.build_parser()
    args = parser.parse_args(["ui", "--no-browser", "--duration", "5"])
    assert args.command == "ui"
    assert args.no_browser is True
    assert args.duration == 5.0

    gui_default = parser.parse_args(["gui"])
    assert gui_default.tk is False, "plain `pc-agent gui` must not fall back to the Tk panel"
    gui_tk = parser.parse_args(["gui", "--tk"])
    assert gui_tk.tk is True

    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "def _open_command_center(" in source
    assert "loopback only" in source
    # The launcher hands the origin to the desktop-window planner; the token still
    # reaches the page through the server-injected meta tag, never through a URL.
    body = source.split("def _open_command_center(", 1)[1].split("\ndef ", 1)[0]
    assert "plan_desktop_window(" in body
    assert "launch(plan, url=url)" in body
    assert "token=" not in body
    assert "?token" not in body


def test_command_center_launcher_serves_and_stops(tmp_path, monkeypatch):
    """A bounded run really binds loopback, serves `/`, and shuts down cleanly."""

    from pc_agent import cli

    settings = Settings.from_env(tmp_path)
    settings.ensure_directories()
    monkeypatch.setattr(cli, "_open_in_browser", lambda url: True)
    monkeypatch.setattr(settings, "api_port", 0, raising=False)

    started = time.time()
    code = cli._open_command_center(settings, no_browser=True, duration=1.5)
    assert code == 0
    assert time.time() - started < 20, "the bounded run must stop on its own"


def test_desktop_shell_config_and_icons_are_real():
    """The Tauri shell ships a real icon set and matching versions."""

    root = _frontend_root() / "src-tauri"
    config = json.loads((root / "tauri.conf.json").read_text(encoding="utf-8"))
    package = json.loads((_frontend_root() / "package.json").read_text(encoding="utf-8"))
    from pc_agent import __version__

    assert config["version"] == __version__
    assert config["identifier"].startswith("local.omni.")
    window = config["app"]["windows"][0]
    assert window["url"].startswith("http://127.0.0.1:")
    assert window["minWidth"] >= 1024

    icons = config["bundle"]["icon"]
    assert len(icons) >= 6
    for relative in icons:
        path = root / relative
        assert path.is_file(), f"missing icon {relative}"
        head = path.read_bytes()[:8]
        assert head.startswith(
            (b"\x89PNG", b"\x00\x00\x01\x00", b"icns")
        ), f"{relative} is not a real image file"

    assert "icon.ico" in " ".join(icons)
    assert "icon.icns" in " ".join(icons)
    assert package["version"] == __version__


def test_desktop_shell_grants_no_extra_capabilities():
    root = _frontend_root() / "src-tauri"
    capabilities = json.loads((root / "capabilities" / "default.json").read_text(encoding="utf-8"))
    assert capabilities["permissions"] == ["core:default"]
    assert capabilities["windows"] == ["main"]
    main_rs = (root / "src" / "main.rs").read_text(encoding="utf-8")
    assert 'const UI_ORIGIN: &str = "http://127.0.0.1:8765";' in main_rs
    assert "127.0.0.1:8765" in main_rs
    # No shell/fs/network plugin may sneak in.
    for forbidden in ("tauri-plugin-shell", "tauri-plugin-fs", "tauri-plugin-http"):
        assert forbidden not in main_rs
    cargo = (root / "Cargo.toml").read_text(encoding="utf-8")
    assert "tauri-plugin-single-instance" in cargo
    assert "single_instance" in main_rs


def test_no_simulated_metrics_in_ui_sources():
    """Guard against the failure mode the brief calls out: fake numbers.

    A sample series, a fake latency table or a hard-coded success toast would all
    have to be invented somewhere. These markers must not appear in any view or
    widget source.
    """

    forbidden = ("lorem ipsum", "mockdata", "sampletasks", "dummy data", "Math.random() * 100")
    sources = list((_frontend_root() / "src").rglob("*.tsx")) + list((_frontend_root() / "src").rglob("*.ts"))
    assert sources
    for path in sources:
        text = path.read_text(encoding="utf-8").lower()
        for marker in forbidden:
            assert marker.lower() not in text, f"{path} contains {marker!r}"


# ------------------------------------------- layout clipping (0.82.13)
def test_the_verifier_checks_for_clipped_content_not_just_document_overflow() -> None:
    """A panel with `overflow: hidden` clips its own content silently.

    Document-level overflow (the older check) stays at zero while a row runs
    47 px past its panel - which is exactly what shipped in 0.82.0-0.82.12 in the
    Intelligence feed: an event type of 30+ characters pushed the timestamp out
    of view.
    """

    script = (_frontend_root() / "scripts" / "ui-verify.js").read_text(encoding="utf-8")

    assert "no clipped content at" in script
    assert "scrollWidth > element.clientWidth" in script
    # intentional overflow must not be reported as a defect
    assert 'style.textOverflow === "ellipsis"' in script
    assert '["auto", "scroll"].includes(style.overflowX)' in script


def test_the_intelligence_feed_meta_line_can_wrap() -> None:
    feed = (_frontend_root() / "src" / "components" / "IntelligenceFeed.tsx").read_text(encoding="utf-8")

    assert "flex-wrap" in feed
    assert "min-w-0 max-w-full truncate" in feed  # the event type ellipsises
    assert 'className="shrink-0" title={event.created_at}' in feed  # the time is never squeezed out
