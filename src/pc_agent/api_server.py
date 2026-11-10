"""Loopback-only local HTTP API (Transformation Phase 3).

A deliberately small standard-library HTTP server fronting
:class:`~pc_agent.service.LocalService`:

* binds loopback addresses only (validated in configuration);
* every route except ``GET /v1/health`` requires
  ``Authorization: Bearer <token>``; the token comes from
  ``PC_AGENT_API_TOKEN`` or is generated once into
  ``<data-dir>/api-token`` with owner-only permissions where the
  platform supports them;
* JSON in / JSON out, bounded request bodies, structured error
  envelopes, no CORS headers (browsers are not an API client yet).

The API never executes tools directly: task runs still pass through
the orchestrator, the Safety Kernel, and the Approval Center.
"""

from __future__ import annotations

import errno
import hmac
import json
import logging
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Self
from urllib.parse import parse_qs, urlparse

from . import webui
from .config import Settings
from .errors import ConfigError, PcAgentError, StorageError
from .profiles import (
    active_profile_name,
    delete_profile,
    list_profiles,
    save_profile,
    set_active_profile,
    snapshot_from_settings,
)
from .service import LocalService
from .web_ui import INDEX_HTML

__all__ = ["LocalApiServer", "load_or_create_api_token"]

_LOG = logging.getLogger("pc_agent.api")

#: Served at ``/`` when no Command Center build is present. It says what is
#: missing and how to fix it - it never pretends to be the product UI.
_MISSING_BUILD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8" /><title>Omni-Assistant - UI not built</title>
<style>body{margin:0;background:#050b16;color:#dbeafe;font:15px/1.6 system-ui,sans-serif;padding:48px}
code{background:#0d1b2e;padding:2px 6px;border-radius:4px;color:#7dd3fc}a{color:#67e8f9}</style></head>
<body><h1 style="font-size:20px;letter-spacing:.18em">OMNI-ASSISTANT</h1>
<p>The API is running, but this install has no built Command Center.</p>
<p>Build it once with Node 20+:</p>
<pre><code>cd frontend
npm ci
npm run build</code></pre>
<p>Then reload this page. The API itself is available at <code>/v1/health</code>,
and the legacy panel is at <a href="/legacy">/legacy</a>.</p></body></html>"""
_MAX_BODY_BYTES = 256 * 1024
_TOKEN_FILENAME = "api-token"


def load_or_create_api_token(settings: Settings) -> str:
    """Token from settings, or a persisted random token in the data dir."""
    if settings.api_token:
        return settings.api_token
    data_dir = Path(settings.data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    token_path = data_dir / _TOKEN_FILENAME
    if token_path.is_file():
        existing = token_path.read_text(encoding="utf-8", errors="strict").strip()
        if len(existing) >= 16:
            return existing
    token = secrets.token_urlsafe(32)
    temp_path = token_path.with_suffix(".tmp")
    temp_path.write_text(token + "\n", encoding="utf-8")
    try:
        temp_path.chmod(0o600)
    except OSError:  # Best effort on platforms without POSIX modes.
        pass
    temp_path.replace(token_path)
    return token


# A browser closing a stream is normal, and the *same* event is reported
# differently per platform: POSIX raises BrokenPipeError/ConnectionResetError,
# Windows raises ConnectionAbortedError (WinError 10053). 0.82.9 and earlier
# only caught the POSIX pair, so every Command Center stream end on Windows
# logged an ERROR with a full traceback and then tried to answer with a 500 on
# a socket that no longer existed - the second traceback in the report.
_CLIENT_GONE: tuple[type[BaseException], ...] = (
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
)
# Some platforms raise a bare OSError carrying the Winsock/POSIX code instead of
# the specialised subclass, so match on the code as well.
_DISCONNECT_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, "EPIPE", None),
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "ECONNABORTED", None),
        getattr(errno, "WSAECONNABORTED", None),
        getattr(errno, "WSAECONNRESET", None),
        getattr(errno, "ENOTCONN", None),
        getattr(errno, "ESHUTDOWN", None),
    )
    if code is not None
)


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """A threaded local server that does not shout when a client hangs up."""

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, _CLIENT_GONE) or (
            isinstance(exc, OSError) and exc.errno in _DISCONNECT_ERRNOS
        ):
            _LOG.debug("client %s disconnected mid-response", client_address)
            return
        super().handle_error(request, client_address)


class LocalApiServer:
    """Owns the HTTP server + LocalService lifecycle."""

    def __init__(self, settings: Settings, *, service: LocalService | None = None) -> None:
        if not settings.api_enabled:
            raise ConfigError("The local API is disabled (PC_AGENT_API_ENABLED=false).")
        self._settings = settings
        self._service = service or LocalService(settings)
        self._token = load_or_create_api_token(settings)
        handler = _make_handler(self._service, self._token)
        self._httpd = _QuietThreadingHTTPServer(
            (settings.api_host, settings.api_port), handler
        )
        self._thread: threading.Thread | None = None
        self._scheduler_thread: threading.Thread | None = None
        self._scheduler_stop = threading.Event()
        # True while *some* thread is inside ``serve_forever`` - including a
        # caller-owned one (``pc-agent api serve`` runs it on the main thread).
        # Closing the socket without shutting that loop down first is a Windows
        # error (10038 on a closed selector), so stop() has to know about it.
        self._serving = threading.Event()

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    @property
    def token(self) -> str:
        return self._token

    @property
    def service(self) -> LocalService:
        return self._service

    def start(self) -> None:
        if self._thread is not None:
            return
        self._start_scheduler()
        self._service._emit(
            "service.api_started",
            source="api",
            summary=f"Local API listening on {self.address[0]}:{self.address[1]}.",
        )
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="local-api",
            daemon=True,
        )
        self._thread.start()
        _LOG.info("Local API listening on %s:%s", *self.address)

    def serve_forever(self) -> None:
        """Blocking variant for ``pc-agent api serve``."""
        self._start_scheduler()
        self._service._emit(
            "service.api_started",
            source="api",
            summary=f"Local API listening on {self.address[0]}:{self.address[1]}.",
        )
        self._serving.set()
        try:
            self._httpd.serve_forever(poll_interval=0.25)
        except OSError as exc:  # pragma: no cover - Windows shutdown race
            # A real socket error must end the loop, not kill the caller's
            # thread with an unhandled exception (that was WinError 10038).
            _LOG.debug("Local API loop ended with a socket error: %s", exc)
        finally:
            self._serving.clear()

    def _start_scheduler(self) -> None:
        interval = self._settings.scheduler_interval_seconds
        if interval <= 0 or self._scheduler_thread is not None:
            return
        self._scheduler_stop.clear()

        def _loop() -> None:
            while not self._scheduler_stop.wait(interval):
                try:
                    self._service.run_due_schedules(source="scheduler")
                except Exception:  # Loop must survive; details in the log.  # noqa: BLE001
                    _LOG.exception("Scheduler tick failed")
                if self._settings.allow_proactive:
                    try:
                        self._service.proactive_tick(source="scheduler")
                    except Exception:  # noqa: BLE001 - reminders must not kill the loop
                        _LOG.exception("Proactive tick failed")

        self._scheduler_thread = threading.Thread(
            target=_loop, name="task-scheduler", daemon=True
        )
        self._scheduler_thread.start()
        _LOG.info("Task scheduler loop started (every %s s)", interval)

    def stop(self) -> None:
        self._scheduler_stop.set()
        if self._scheduler_thread is not None:
            self._scheduler_thread.join(timeout=2)
            self._scheduler_thread = None
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        elif self._serving.is_set():
            # ``serve_forever`` is running on a thread we do not own: ask the
            # loop to end *before* the socket is closed, otherwise the loop
            # wakes up selecting on a closed socket (WinError 10038).
            self._httpd.shutdown()
        self._httpd.server_close()
        self._service.close()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def _profiles_payload(service: LocalService) -> dict[str, Any]:
    data_dir = service.settings.data_dir
    active, source = active_profile_name(data_dir)
    return {
        "profiles": [info.to_dict() for info in list_profiles(data_dir)],
        "active": service.settings.active_profile or active,
        "active_source": (
            "settings" if service.settings.active_profile else source
        ),
        "restart_required_to_apply": True,
    }


def _make_handler(service: LocalService, token: str):
    settings = service.settings

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "OmniAssistantLocalAPI"

        # -- plumbing ---------------------------------------------------------
        def log_message(self, format: str, *args: Any) -> None:
            _LOG.debug(format, *args)

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; img-src data:; connect-src 'self'; "
                "frame-ancestors 'none'",
            )
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_asset(self, target: Any) -> None:
            """Serve one built Command Center file with a fixed MIME map.

            No directory listing, no range requests, no CORS: the SPA is served
            from the same loopback origin as ``/v1``, and anything outside the
            asset root is refused by :func:`pc_agent.webui.static_path`.
            """

            data = target.read_bytes()
            suffix = target.suffix.lower()
            mime = {
                ".html": "text/html; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".mjs": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".svg": "image/svg+xml",
                ".png": "image/png",
                ".webp": "image/webp",
                ".woff2": "font/woff2",
                ".ico": "image/x-icon",
                ".map": "application/json; charset=utf-8",
            }.get(suffix, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            # Assets are content-hashed by Vite; the shell must never be cached.
            cache = "no-store" if suffix == ".html" else "public, max-age=31536000, immutable"
            self.send_header("Cache-Control", cache)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _serve_command_center(self) -> bool:
            """Serve the built SPA shell, or report honestly that it is absent."""

            html = webui.read_index(token)
            if not html:
                self._send_html(_MISSING_BUILD_HTML)
                return False
            body = html.encode("utf-8")
            # Only the shell's own inline bootstrap is allowed, by exact hash.
            script_src = " ".join(["'self'", *webui.inline_script_hashes(html)])
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'self'; script-src {script_src}; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; font-src 'self' data:; worker-src 'self' blob:; "
                "connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'none'",
            )
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return True

        def _stream_events(self, query: dict[str, str]) -> None:
            """Live application events as NDJSON, with heartbeats (0.82.0).

            The Command Center subscribes here instead of polling. Bounded on
            purpose: the stream ends after ``duration`` seconds (default 120) or
            when the client goes away, and the browser reconnects - a local
            server must not accumulate idle sockets.
            """

            since = _int(query.get("since_id"), 0)
            # Strict parse on purpose: a typo must fail loudly instead of opening
            # a two-minute socket on the default duration.
            duration = min(max(_strict_float(query.get("duration"), 120.0), 5.0), 900.0)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            deadline = time.monotonic() + duration
            last_beat = 0.0
            try:
                self.wfile.write(
                    json.dumps({"type": "stream.ready", "since_id": since}).encode("utf-8")
                    + b"\n"
                )
                self.wfile.flush()
                while time.monotonic() < deadline:
                    payload = service.list_events(since_id=since, limit=100)
                    events = list(payload.get("events") or [])
                    if events:
                        for event in events:
                            self.wfile.write(
                                json.dumps(event, ensure_ascii=True).encode("utf-8") + b"\n"
                            )
                            since = max(since, int(event.get("id") or 0))
                        self.wfile.flush()
                        last_beat = time.monotonic()
                    elif time.monotonic() - last_beat >= 10.0:
                        self.wfile.write(b'{"type":"stream.heartbeat"}\n')
                        self.wfile.flush()
                        last_beat = time.monotonic()
                    time.sleep(0.75)
            except OSError as exc:
                # Every OSError here means the reader is gone (Windows 10053,
                # POSIX EPIPE/ECONNRESET, or a closed socket). The stream is
                # bounded anyway, so ending it quietly is the whole handling.
                _LOG.debug("event stream client disconnected: %s", exc)

        def _stream_ndjson(self, events: Any) -> None:
            """Write JSON-lines events until the iterable ends (Phase 7).

            No Content-Length: the body ends when the connection closes,
            which is how fetch/urllib readers detect completion.
            """
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                for event in events:
                    self.wfile.write(
                        json.dumps(event, ensure_ascii=True).encode("utf-8") + b"\n"
                    )
                    self.wfile.flush()
            except OSError as exc:
                _LOG.debug("NDJSON stream client disconnected: %s", exc)

        def _error(self, status: int, code: str, message: str) -> None:
            self._send_json(status, {"ok": False, "error": {"code": code, "message": message}})

        def _ok(self, payload: dict[str, Any], *, status: int = 200) -> None:
            self._send_json(status, {"ok": True, "data": payload})

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return False
            return hmac.compare_digest(header[7:].strip(), token)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > _MAX_BODY_BYTES:
                raise PcAgentError("Request body exceeds the local API limit.")
            raw = self.rfile.read(length)
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise PcAgentError("Request body must be a JSON object.")
            return parsed

        # -- routing -----------------------------------------------------------
        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            try:
                if method == "GET" and path in {"/", "/index.html"}:
                    self._serve_command_center()
                    return
                if method == "GET" and path in {"/legacy", "/legacy/"}:
                    self._send_html(INDEX_HTML)
                    return
                if method == "GET" and path.startswith("/assets/"):
                    asset = webui.static_path(path)
                    if asset is None:
                        self._error(404, "not_found", "No such asset.")
                        return
                    self._send_asset(asset)
                    return
                if method == "GET" and path == "/favicon.ico":
                    icon = webui.static_path("favicon.svg")
                    if icon is not None:
                        self._send_asset(icon)
                    else:
                        self._error(404, "not_found", "No icon.")
                    return
                if method == "GET" and path == "/v1/health":
                    self._ok(service.health())
                    return
                if not self._authorized():
                    self._error(401, "unauthorized", "A valid bearer token is required.")
                    return
                body = self._read_json() if method in {"POST", "PUT", "PATCH"} else {}
                self._route(method, path, query, body)
            except PcAgentError as exc:
                self._error(_status_for_error(exc), type(exc).__name__, str(exc))
            except (ValueError, KeyError) as exc:
                self._error(400, "bad_request", str(exc))
            except _CLIENT_GONE as exc:
                # The browser closed the tab/stream mid-request. Nothing to
                # report and nothing left to answer *to*.
                _LOG.debug("client went away during %s %s: %s", method, path, exc)
            except Exception:  # Never leak stack traces to the client.  # noqa: BLE001
                _LOG.exception("Local API %s %s failed", method, path)
                try:
                    self._error(500, "internal_error", "The local API hit an unexpected error.")
                except OSError as exc:  # the client left while we were failing
                    _LOG.debug("could not report the 500: %s", exc)

        def _route(
            self, method: str, path: str, query: dict[str, str], body: dict[str, Any]
        ) -> None:
            if method == "GET" and path == "/v1/system":
                self._ok(service.system_status())
            elif method == "GET" and path == "/v1/overview":
                self._ok(service.overview())
            elif method == "GET" and path == "/v1/settings":
                self._ok({"settings": service.public_settings()})
            elif method == "GET" and path == "/v1/tools":
                self._ok(service.list_tools())
            elif method == "GET" and path == "/v1/providers":
                self._ok(service.providers())
            elif method == "GET" and path == "/v1/providers/capabilities":
                self._ok(service.provider_capabilities())
            elif method == "GET" and path == "/v1/providers/usage":
                self._ok(service.provider_usage())
            elif method == "GET" and path == "/v1/providers/evaluations":
                self._ok(service.provider_evaluations())
            elif method == "POST" and path == "/v1/providers/evaluations/run":
                self._ok(service.run_provider_evaluations())
            elif method == "POST" and path == "/v1/providers/stream":
                prompt = str(body.get("prompt") or "")
                system = body.get("system") or None
                raw_tokens = body.get("max_output_tokens") or 512
                try:
                    max_tokens = int(raw_tokens)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    max_tokens = 512
                self._stream_ndjson(
                    service.stream_chat(
                        prompt,
                        system_instruction=str(system) if system else None,
                        max_output_tokens=max_tokens,
                    )
                )
            elif method == "GET" and path == "/v1/knowledge/documents":
                self._ok(service.knowledge_documents())
            elif method == "POST" and path == "/v1/knowledge/ingest":
                self._ok(service.knowledge_ingest(body), status=201)
            elif method == "DELETE" and path.startswith("/v1/knowledge/documents/"):
                raw_id = path[len("/v1/knowledge/documents/") :]
                try:
                    document_id = int(raw_id)
                except ValueError:
                    self._error(404, "not_found", "Unknown knowledge document.")
                    return
                self._ok(service.knowledge_remove(document_id))
            elif method == "GET" and path == "/v1/knowledge/search":
                limit_raw = query.get("limit", "5")
                try:
                    limit_value = int(limit_raw)
                except ValueError:
                    limit_value = 5
                self._ok(service.knowledge_search(query.get("q", ""), limit=limit_value))
            elif method == "POST" and path == "/v1/knowledge/ask":
                self._ok(service.knowledge_ask(str(body.get("question") or "")))
            elif method == "GET" and path == "/v1/knowledge/stats":
                self._ok(service.knowledge_stats())
            elif method == "GET" and path == "/v1/memory/search":
                limit_raw = query.get("limit", "5")
                try:
                    limit_value = int(limit_raw)
                except ValueError:
                    limit_value = 5
                self._ok(service.memory_search(query.get("q", ""), limit=limit_value))
            elif method == "GET" and path == "/v1/agents":
                self._ok(service.agents_list())
            elif method == "POST" and path == "/v1/agents":
                self._ok(service.agent_save(body), status=201)
            elif method == "GET" and path == "/v1/agents/stats":
                self._ok(service.agent_stats())
            elif method == "GET" and path == "/v1/agents/runs":
                self._ok(
                    service.agent_runs_list(
                        agent_name=query.get("agent", ""),
                        team_run_id=query.get("team_run_id", ""),
                        limit=_int(query.get("limit"), 50),
                    )
                )
            elif method == "POST" and path == "/v1/agents/team":
                self._ok(service.agent_team_start(body), status=202)
            elif method == "GET" and path.startswith("/v1/agents/team/"):
                self._ok(service.agent_team_result(path[len("/v1/agents/team/"):]))
            elif method == "GET" and path.startswith("/v1/agents/"):
                self._ok(service.agent_get(path[len("/v1/agents/"):]))
            elif method == "DELETE" and path.startswith("/v1/agents/"):
                self._ok(service.agent_delete(path[len("/v1/agents/"):]))
            elif method == "POST" and path.startswith("/v1/agents/") and path.endswith("/run"):
                name = path[len("/v1/agents/") : -len("/run")]
                self._ok(service.agent_run_start(name, body), status=202)
            elif method == "GET" and path == "/v1/automation/capabilities":
                self._ok(service.automation_capabilities())
            elif method == "GET" and path == "/v1/automation/runs":
                self._ok(
                    service.automation_runs(
                        kind=query.get("kind", ""),
                        status=query.get("status", ""),
                        limit=_int(query.get("limit"), 50),
                    )
                )
            elif method == "GET" and path == "/v1/automation/stats":
                self._ok(service.automation_stats())
            elif method == "POST" and path == "/v1/vision/screen":
                self._ok(service.vision_screen(body))
            elif method == "POST" and path == "/v1/vision/locate":
                self._ok(service.vision_locate(body))
            elif method == "POST" and path == "/v1/browser/wait":
                self._ok(service.browser_wait(body))
            elif method == "POST" and path == "/v1/browser/tabs":
                self._ok(service.browser_tabs(body))
            elif method == "POST" and path == "/v1/desktop/window-state":
                self._ok(service.desktop_window_state(body))
            elif method == "GET" and path == "/v1/voice/status":
                self._ok(service.voice_status())
            elif method == "GET" and path == "/v1/voice/devices":
                self._ok(service.voice_devices())
            elif method == "GET" and path == "/v1/voice/privacy":
                self._ok(service.voice_privacy())
            elif method == "POST" and path == "/v1/voice/enable":
                self._ok(service.voice_enable(bool(body.get("enabled", True))))
            elif method == "POST" and path == "/v1/voice/wake-word":
                self._ok(service.voice_wake_word(bool(body.get("enabled"))))
            elif method == "POST" and path == "/v1/voice/capture/start":
                self._ok(
                    service.voice_capture_start(str(body.get("mode") or "push_to_talk")),
                    status=202,
                )
            elif method == "POST" and path == "/v1/voice/capture/stop":
                self._ok(service.voice_capture_stop(body))
            elif method == "POST" and path == "/v1/voice/capture/cancel":
                self._ok(service.voice_capture_cancel())
            elif method == "POST" and path == "/v1/voice/intent":
                self._ok(service.voice_intent(body))
            elif method == "GET" and path == "/v1/voice/acceptance":
                self._ok(service.voice_acceptance())
            elif method == "POST" and path == "/v1/voice/speak":
                self._ok(service.voice_speak(body))
            elif method == "POST" and path == "/v1/voice/transcribe":
                self._ok(service.voice_transcribe(body))
            elif method == "GET" and path == "/v1/voice/sessions":
                self._ok(service.voice_sessions(limit=_int(query.get("limit"), 20)))
            elif method == "POST" and path == "/v1/voice/purge":
                self._ok(service.voice_purge_audio())
            elif method == "POST" and path == "/v1/voice/clear":
                self._ok(service.voice_clear_history())
            elif method == "GET" and path == "/v1/safety":
                self._ok(service.safety_status())
            elif method == "POST" and path == "/v1/safety/stop":
                reason = str(body.get("reason") or "Requested through the local API.")
                self._ok(service.safety_stop(reason=reason, engaged_by="api"))
            elif method == "POST" and path == "/v1/safety/resume":
                self._ok(service.safety_resume(released_by="api"))
            elif method == "GET" and path == "/v1/events/stream":
                self._stream_events(query)
            elif method == "GET" and path == "/v1/events":
                self._ok(
                    service.list_events(
                        since_id=_int(query.get("since_id"), 0),
                        event_type=query.get("type"),
                        task_id=query.get("task_id"),
                        limit=_int(query.get("limit"), 100),
                    )
                )
            elif method == "GET" and path == "/v1/tasks":
                self._ok(
                    service.list_tasks(
                        limit=_int(query.get("limit"), 50),
                        offset=_int(query.get("offset"), 0),
                        status=query.get("status"),
                    )
                )
            elif method == "POST" and path == "/v1/tasks":
                instruction = str(body.get("instruction") or "").strip()
                if not instruction:
                    self._error(400, "bad_request", "Field 'instruction' is required.")
                    return
                self._ok(service.create_task(instruction), status=201)
            elif method == "POST" and path == "/v1/run":
                instruction = str(body.get("instruction") or "").strip()
                if not instruction:
                    self._error(400, "bad_request", "Field 'instruction' is required.")
                    return
                self._ok(
                    service.run_instruction(instruction, use_ai=bool(body.get("use_ai"))),
                    status=202,
                )
            elif method == "GET" and path == "/v1/approvals":
                self._ok(
                    service.list_approvals(
                        status=query.get("status", "pending"),
                        limit=_int(query.get("limit"), 100),
                    )
                )
            elif method == "POST" and path.startswith("/v1/approvals/") and path.endswith("/decide"):
                key = path[len("/v1/approvals/") : -len("/decide")]
                self._ok(
                    service.decide_approval(
                        key,
                        approve=bool(body.get("approve")),
                        note=str(body["note"]) if body.get("note") else None,
                        decided_by="api",
                    )
                )
            elif method == "GET" and path == "/v1/profiles":
                self._ok(_profiles_payload(service))
            elif method == "POST" and path == "/v1/profiles/save-current":
                name = str(body.get("name") or "").strip()
                info = save_profile(settings.data_dir, name, snapshot_from_settings(settings))
                self._ok({"saved": info.to_dict(), **_profiles_payload(service)}, status=201)
            elif method == "POST" and path == "/v1/profiles/activate":
                name = body.get("name")
                set_active_profile(settings.data_dir, None if name in (None, "") else str(name))
                self._ok(_profiles_payload(service))
            elif method == "DELETE" and path.startswith("/v1/profiles/"):
                name = path[len("/v1/profiles/") :]
                deleted = delete_profile(settings.data_dir, name)
                if not deleted:
                    self._error(404, "not_found", f"Profile '{name}' does not exist.")
                    return
                self._ok({"deleted": name, **_profiles_payload(service)})
            elif method == "GET" and path == "/v1/missions":
                self._ok(service.list_missions(limit=_int(query.get("limit"), 100)))
            elif method == "POST" and path == "/v1/missions":
                name = str(body.get("name") or "").strip()
                if not name:
                    self._error(400, "bad_request", "Field 'name' is required.")
                    return
                specs = body.get("tasks") if isinstance(body.get("tasks"), list) else None
                self._ok(
                    service.create_mission(
                        name,
                        description=str(body.get("description") or ""),
                        task_specs=specs,
                    ),
                    status=201,
                )
            elif method == "GET" and path == "/v1/schedules":
                self._ok(service.list_schedules())
            elif method == "POST" and path == "/v1/schedules":
                self._ok(_create_schedule(service, body), status=201)
            elif method == "POST" and path == "/v1/schedules/run-due":
                self._ok(service.run_due_schedules(source="api"))
            elif method == "POST" and path.startswith("/v1/schedules/") and path.endswith("/enabled"):
                schedule_id = path[len("/v1/schedules/") : -len("/enabled")]
                self._ok(
                    service.set_schedule_enabled(schedule_id, enabled=bool(body.get("enabled")))
                )
            elif method == "DELETE" and path.startswith("/v1/schedules/"):
                self._ok(service.delete_schedule(path[len("/v1/schedules/") :]))
            elif method == "GET" and path == "/v1/proactive/status":
                self._ok(service.proactive_status())
            elif method == "POST" and path == "/v1/proactive/tick":
                self._ok(service.proactive_tick(limit=_int(body.get("limit"), 0) or None))
            elif method == "GET" and path == "/v1/proactive/reminders":
                self._ok(
                    service.reminders(
                        status=query.get("status") or None,
                        limit=_int(query.get("limit"), 50),
                    )
                )
            elif method == "POST" and path == "/v1/proactive/reminders":
                self._ok(
                    service.add_reminder(
                        title=str(body.get("title") or ""),
                        due=str(body.get("due") or ""),
                        note=str(body.get("note") or ""),
                        repeat_seconds=(
                            _int(str(body.get("repeat_seconds")), 0)
                            if body.get("repeat_seconds") not in (None, "")
                            else None
                        ),
                        severity=str(body.get("severity") or "normal"),
                    ),
                    status=201,
                )
            elif (
                method == "POST"
                and path.startswith("/v1/proactive/reminders/")
                and path.endswith("/done")
            ):
                reference = path[len("/v1/proactive/reminders/") : -len("/done")]
                self._ok(service.resolve_reminder(reference))
            elif (
                method == "POST"
                and path.startswith("/v1/proactive/reminders/")
                and path.endswith("/dismiss")
            ):
                self._ok(
                    service.dismiss_reminder(
                        path[len("/v1/proactive/reminders/") : -len("/dismiss")]
                    )
                )
            elif (
                method == "POST"
                and path.startswith("/v1/proactive/reminders/")
                and path.endswith("/snooze")
            ):
                self._ok(
                    service.snooze_reminder(
                        path[len("/v1/proactive/reminders/") : -len("/snooze")],
                        for_spec=str(body.get("for") or "+15m"),
                    )
                )
            elif method == "DELETE" and path.startswith("/v1/proactive/reminders/"):
                self._ok(service.delete_reminder(path[len("/v1/proactive/reminders/") :]))
            elif method == "GET" and path == "/v1/proactive/notifications":
                self._ok(
                    service.notifications(
                        limit=_int(query.get("limit"), 25), kind=query.get("kind") or None
                    )
                )
            elif method == "POST" and path == "/v1/proactive/notifications/clear":
                self._ok(service.clear_notifications())
            elif method == "POST" and path == "/v1/proactive/notify":
                self._ok(
                    service.notify_user(
                        title=str(body.get("title") or ""),
                        body=str(body.get("body") or ""),
                        kind=str(body.get("kind") or "general"),
                        severity=str(body.get("severity") or "normal"),
                    ),
                    status=201,
                )
            elif method == "GET" and path == "/v1/proactive/briefings":
                self._ok(service.briefings(limit=_int(query.get("limit"), 10)))
            elif method == "POST" and path == "/v1/proactive/briefing":
                self._ok(
                    service.run_briefing(force=bool(body.get("force", False))), status=201
                )
            elif method == "GET" and path == "/v1/i18n/status":
                self._ok(service.i18n_status())
            elif method == "GET" and path == "/v1/i18n/messages":
                self._ok(service.i18n_messages(query.get("locale") or None))
            elif method == "POST" and path == "/v1/i18n/locale":
                self._ok(
                    service.i18n_set(
                        locale=str(body["locale"]) if body.get("locale") else None,
                        scale=body.get("scale"),
                        high_contrast=body.get("high_contrast"),
                        reduce_motion=body.get("reduce_motion"),
                        cli_color=body.get("cli_color"),
                    )
                )
            elif method == "GET" and path == "/v1/a11y/status":
                self._ok(service.a11y_status())
            elif method == "GET" and path == "/v1/perf/bench":
                self._ok(service.perf_bench(iterations=_int(query.get("iterations"), 5)))
            elif method == "GET" and path == "/v1/sync/status":
                self._ok(service.sync_status())
            elif method == "GET" and path == "/v1/sync/identity":
                self._ok(service.sync_identity())
            elif method == "GET" and path == "/v1/sync/peers":
                self._ok(service.sync_peers())
            elif method == "POST" and path == "/v1/sync/pair":
                self._ok(
                    service.sync_pair(
                        name=str(body.get("name") or ""),
                        sign_public=str(body.get("sign_public") or ""),
                        encrypt_public=str(body.get("encrypt_public") or ""),
                        confirm=str(body["confirm"]) if body.get("confirm") else None,
                    ),
                    status=201,
                )
            elif method == "POST" and path == "/v1/sync/peers/enabled":
                self._ok(
                    service.sync_set_peer_enabled(
                        str(body.get("device") or ""), enabled=bool(body.get("enabled"))
                    )
                )
            elif method == "DELETE" and path.startswith("/v1/sync/peers/"):
                self._ok(service.sync_unpair(path[len("/v1/sync/peers/") :]))
            elif method == "POST" and path == "/v1/sync/export":
                self._ok(
                    service.sync_export(path=str(body["path"]) if body.get("path") else None),
                    status=201,
                )
            elif method == "POST" and path == "/v1/sync/import":
                target = str(body.get("path") or "").strip()
                if not target:
                    self._error(400, "bad_request", "Field 'path' is required.")
                    return
                self._ok(service.sync_import(target))
            elif method == "POST" and path == "/v1/sync/tick":
                self._ok(service.sync_tick())
            elif method == "GET" and path == "/v1/sync/bundles":
                self._ok(
                    service.sync_bundles(
                        direction=query.get("direction") or None,
                        limit=_int(query.get("limit"), 25),
                    )
                )
            elif method == "GET" and path == "/v1/sync/requests":
                self._ok(
                    service.sync_requests(
                        status=query.get("status") or None,
                        limit=_int(query.get("limit"), 25),
                    )
                )
            elif method == "POST" and path == "/v1/sync/request":
                self._ok(
                    service.sync_request(
                        str(body.get("kind") or "remind"),
                        title=str(body.get("title") or ""),
                        body=str(body.get("body") or ""),
                        due=str(body.get("due") or ""),
                        severity=str(body.get("severity") or "normal"),
                    ),
                    status=201,
                )
            elif method == "POST" and path.startswith("/v1/sync/requests/") and path.endswith("/decide"):
                request_id = path[len("/v1/sync/requests/") : -len("/decide")]
                self._ok(
                    service.sync_decide(
                        request_id,
                        accept=bool(body.get("accept")),
                        note=str(body.get("note") or ""),
                    )
                )
            elif method == "GET" and path == "/v1/plugins/status":
                self._ok(service.plugin_status())
            elif method == "GET" and path == "/v1/plugins":
                self._ok(service.plugins())
            elif method == "POST" and path == "/v1/plugins/verify":
                bundle = str(body.get("bundle") or "").strip()
                if not bundle:
                    self._error(400, "bad_request", "Field 'bundle' is required.")
                    return
                self._ok(service.plugin_verify(bundle))
            elif method == "POST" and path == "/v1/plugins/install":
                bundle = str(body.get("bundle") or "").strip()
                if not bundle:
                    self._error(400, "bad_request", "Field 'bundle' is required.")
                    return
                self._ok(service.plugin_install(bundle), status=201)
            elif method == "GET" and path == "/v1/plugins/runs":
                self._ok(
                    service.plugin_runs(
                        plugin_id=query.get("plugin") or None,
                        limit=_int(query.get("limit"), 25),
                    )
                )
            elif method == "GET" and path.startswith("/v1/plugins/") and path.endswith("/skills"):
                plugin_id = path[len("/v1/plugins/") : -len("/skills")]
                self._ok(service.plugin_skills(plugin_id))
            elif (
                method == "POST"
                and path.startswith("/v1/plugins/")
                and path.endswith("/run")
            ):
                plugin_id = path[len("/v1/plugins/") : -len("/run")]
                tool = str(body.get("tool") or "").strip()
                if not tool:
                    self._error(400, "bad_request", "Field 'tool' is required.")
                    return
                values = body.get("values") if isinstance(body.get("values"), dict) else {}
                self._ok(
                    service.plugin_run_tool(
                        plugin_id,
                        tool,
                        values=values,
                        confirm=str(body["confirm"]) if body.get("confirm") else None,
                    )
                )
            elif (
                method == "POST"
                and path.startswith("/v1/plugins/")
                and path.endswith("/enabled")
            ):
                plugin_id = path[len("/v1/plugins/") : -len("/enabled")]
                self._ok(
                    service.plugin_set_enabled(plugin_id, enabled=bool(body.get("enabled")))
                )
            elif (
                method == "POST"
                and path.startswith("/v1/plugins/")
                and path.endswith("/rollback")
            ):
                plugin_id = path[len("/v1/plugins/") : -len("/rollback")]
                version = str(body["version"]) if body.get("version") else None
                self._ok(service.plugin_rollback(plugin_id, version=version))
            elif method == "DELETE" and path.startswith("/v1/plugins/"):
                self._ok(service.plugin_uninstall(path[len("/v1/plugins/") :]))
            elif method == "GET" and path.startswith("/v1/plugins/"):
                self._ok(service.plugin_show(path[len("/v1/plugins/") :]))
            elif method == "GET" and path == "/v1/connectors/status":
                self._ok(
                    service.connector_status(
                        probe=str(query.get("probe") or "").lower() in {"1", "true", "yes"}
                    )
                )
            elif method == "GET" and path == "/v1/connectors/events":
                self._ok(
                    service.connector_events(
                        connector=query.get("connector") or None,
                        limit=_int(query.get("limit"), 50),
                    )
                )
            elif method == "GET" and path == "/v1/connectors/deliveries":
                self._ok(
                    service.connector_deliveries(
                        connector=query.get("connector") or None,
                        limit=_int(query.get("limit"), 20),
                    )
                )
            elif method == "GET" and path == "/v1/connectors/calendar":
                self._ok(
                    service.connector_calendar_events(
                        days_ahead=_int(query.get("days"), 7),
                        limit=_int(query.get("limit"), 0) or None,
                    )
                )
            elif method == "POST" and path == "/v1/connectors/calendar":
                self._ok(
                    service.connector_calendar_add(
                        summary=str(body.get("summary") or ""),
                        start=str(body.get("start") or ""),
                        duration_minutes=_int(body.get("duration_minutes"), 30),
                        location=str(body.get("location") or ""),
                        description=str(body.get("description") or ""),
                    ),
                    status=201,
                )
            elif method == "POST" and path == "/v1/connectors/drive/sync":
                self._ok(service.connector_drive_sync())
            elif method == "GET" and path == "/v1/connectors/mail/inbox":
                self._ok(service.connector_mail_inbox(limit=_int(query.get("limit"), 10)))
            elif method == "POST" and path == "/v1/connectors/mail/send":
                self._ok(
                    service.connector_mail_send(
                        to=body.get("to") or [],
                        subject=str(body.get("subject") or ""),
                        body=str(body.get("body") or ""),
                        confirm=str(body.get("confirm") or ""),
                    ),
                    status=202,
                )
            elif method == "POST" and path == "/v1/connectors/chat/send":
                self._ok(
                    service.connector_chat_send(
                        str(body.get("text") or ""), target=str(body.get("target") or "")
                    )
                )
            elif method == "POST" and path == "/v1/connectors/webhook":
                self._ok(
                    service.connector_webhook_receive(
                        str(body.get("body") or "").encode("utf-8"),
                        timestamp=str(body.get("timestamp") or ""),
                        signature=str(body.get("signature") or ""),
                    )
                )
            elif method == "GET" and path == "/v1/flows":
                self._ok(service.list_flows(limit=_int(query.get("limit"), 100)))
            elif method == "POST" and path == "/v1/flows":
                self._ok(service.create_flow(body), status=201)
            elif method == "POST" and path == "/v1/flows/validate":
                self._ok(service.validate_flow(body))
            elif method == "POST" and path == "/v1/flows/import":
                self._ok(
                    service.import_flow(
                        body.get("document") if isinstance(body.get("document"), dict) else body,
                        overwrite=bool(body.get("overwrite", False)),
                    ),
                    status=201,
                )
            elif method == "POST" and path == "/v1/flows/tick":
                self._ok(service.flow_tick(limit=_int(body.get("limit"), 10)))
            elif method == "GET" and path == "/v1/flows/stats":
                self._ok(service.flow_stats())
            elif method == "GET" and path == "/v1/flow-runs":
                self._ok(
                    service.flow_runs(
                        query.get("flow_id") or None, limit=_int(query.get("limit"), 50)
                    )
                )
            elif method == "GET" and path.startswith("/v1/flows/"):
                remainder = path[len("/v1/flows/") :]
                if remainder.endswith("/export"):
                    self._ok(service.export_flow(remainder[: -len("/export")]))
                elif remainder.endswith("/runs"):
                    self._ok(service.flow_runs(remainder[: -len("/runs")]))
                elif remainder.endswith("/run"):
                    self._error(405, "method_not_allowed", "Use POST to run a flow.")
                elif remainder.endswith("/triggers"):
                    self._ok(service.flow_triggers(remainder[: -len("/triggers")]))
                else:
                    self._ok(service.get_flow(remainder))
            elif (
                method == "POST"
                and path.startswith("/v1/flows/")
                and path.endswith("/definition")
            ):
                self._ok(
                    service.update_flow(
                        path[len("/v1/flows/") : -len("/definition")], body
                    )
                )
            elif method == "DELETE" and path.startswith("/v1/flows/"):
                self._ok(service.delete_flow(path[len("/v1/flows/") :]))
            elif method == "POST" and path.startswith("/v1/flows/") and path.endswith("/run"):
                self._ok(
                    service.run_flow(path[len("/v1/flows/") : -len("/run")]), status=202
                )
            elif method == "POST" and path.startswith("/v1/flows/") and path.endswith("/triggers"):
                self._ok(
                    service.flow_add_trigger(
                        path[len("/v1/flows/") : -len("/triggers")], body
                    ),
                    status=201,
                )
            elif method == "POST" and path.startswith("/v1/flows/") and path.endswith("/enabled"):
                reference = path[len("/v1/flows/") : -len("/enabled")]
                self._ok(
                    service.set_flow_enabled(reference, enabled=bool(body.get("enabled", True)))
                )
            elif method == "POST" and path.startswith("/v1/flow-triggers/"):
                trigger_id = path[len("/v1/flow-triggers/") :]
                if trigger_id.endswith("/enabled"):
                    self._ok(
                        service.flow_set_trigger_enabled(
                            trigger_id[: -len("/enabled")],
                            enabled=bool(body.get("enabled", True)),
                        )
                    )
                else:
                    self._error(404, "not_found", f"No such endpoint: {method} {path}")
            elif method == "DELETE" and path.startswith("/v1/flow-triggers/"):
                self._ok(service.flow_delete_trigger(path[len("/v1/flow-triggers/") :]))
            elif path.startswith("/v1/missions/"):
                self._mission_route(method, path[len("/v1/missions/") :], body)
            elif path.startswith("/v1/tasks/"):
                self._task_route(method, path[len("/v1/tasks/") :], body)
            else:
                self._error(404, "not_found", f"No such endpoint: {method} {path}")

        def _mission_route(self, method: str, remainder: str, body: dict[str, Any]) -> None:
            if "/" not in remainder and method == "GET":
                self._ok(service.get_mission(remainder))
            elif "/" not in remainder and method == "DELETE":
                self._ok(service.delete_mission(remainder))
            elif remainder.endswith("/run") and method == "POST":
                self._ok(
                    service.start_mission(
                        remainder[: -len("/run")], use_ai=bool(body.get("use_ai"))
                    ),
                    status=202,
                )
            elif remainder.endswith("/cancel") and method == "POST":
                reason = str(body.get("reason") or "Cancelled through the local API.")
                self._ok(service.cancel_mission(remainder[: -len("/cancel")], reason=reason))
            elif remainder.endswith("/tasks") and method == "POST":
                mission_id = remainder[: -len("/tasks")]
                depends_on = body.get("depends_on")
                self._ok(
                    service.add_mission_task(
                        mission_id,
                        instruction=str(body["instruction"])
                        if body.get("instruction")
                        else None,
                        task_reference=str(body["task_id"]) if body.get("task_id") else None,
                        depends_on=[str(item) for item in depends_on]
                        if isinstance(depends_on, list)
                        else None,
                    ),
                    status=201,
                )
            elif remainder.endswith("/graph") and method == "GET":
                mission = service.get_mission(remainder[: -len("/graph")])
                self._ok({"mission_id": mission["id"], "graph": mission["graph"]})
            else:
                self._error(404, "not_found", f"No such mission endpoint: {method}")

        def _task_route(self, method: str, remainder: str, body: dict[str, Any]) -> None:
            if "/" not in remainder and method == "GET":
                self._ok(service.get_task(remainder))
            elif "/" not in remainder and method == "DELETE":
                self._ok(service.delete_task(remainder))
            elif remainder.endswith("/run") and method == "POST":
                reference = remainder[: -len("/run")]
                self._ok(
                    service.start_task(reference, use_ai=bool(body.get("use_ai"))), status=202
                )
            elif remainder.endswith("/cancel") and method == "POST":
                reference = remainder[: -len("/cancel")]
                reason = str(body.get("reason") or "Cancelled through the local API.")
                self._ok(service.cancel_task(reference, reason=reason))
            else:
                self._error(404, "not_found", f"No such task endpoint: {method}")

        # -- verb entry points ---------------------------------------------------
        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def do_HEAD(self) -> None:
            self._dispatch("GET")

    return Handler


def _create_schedule(service: LocalService, body: dict[str, Any]) -> dict[str, Any]:
    from .domain.task import parse_iso_datetime

    name = str(body.get("name") or "").strip()
    instruction = str(body.get("instruction") or "").strip()
    kind = str(body.get("kind") or "").strip()
    if not name or not instruction or kind not in {"once", "interval"}:
        raise PcAgentError(
            "Schedules need name, instruction, and kind ('once' or 'interval')."
        )
    run_at = parse_iso_datetime(str(body["run_at"])) if body.get("run_at") else None
    interval = body.get("interval_seconds")
    return service.create_schedule(
        name,
        instruction=instruction,
        kind=kind,
        run_at=run_at,
        interval_seconds=int(interval) if interval is not None else None,
        use_ai=bool(body.get("use_ai")),
    )


def _float(raw: str | None, default: float) -> float:
    try:
        return float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _strict_float(raw: str | None, default: float) -> float:
    """Float query parameter that refuses garbage instead of silently defaulting."""

    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise PcAgentError(f"Expected a number, got '{raw}'.") from exc


def _int(raw: str | None, default: int) -> int:
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise PcAgentError(f"Expected an integer, got '{raw}'.") from exc


def _status_for_error(exc: PcAgentError) -> int:
    message = str(exc).lower()
    if isinstance(exc, ConfigError):
        return 400
    if "not found" in message or "does not exist" in message:
        return 404
    if isinstance(exc, StorageError):
        return 400
    return 400
