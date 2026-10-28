"""Bounded Chromium DevTools Protocol (CDP) client (Phase 13).

The agent never launches or profiles a browser itself in this phase: the
user starts Edge/Chrome with ``--remote-debugging-port`` and a throwaway
profile, and these tools connect to that exact local endpoint. The wire
client is standard-library only: a minimal RFC 6455 WebSocket client plus
a small CDP request/event layer. Every JavaScript expression evaluated in
the page is a fixed template; user values are embedded exclusively via
``json.dumps`` so page content and arguments can never become code.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from .errors import ToolError

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

START_HINT = (
    "Start Edge or Chrome with a debugging port and a throwaway profile, "
    "e.g. msedge.exe --remote-debugging-port=9222 "
    "--user-data-dir=browser-profile (see docs/phase13.md)."
)


# ---------------------------------------------------------------- WebSocket


class _SocketReader:
    """Buffered exact reads over a socket (handshake leftovers survive)."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buffer = bytearray()

    def read_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._sock.recv(64 * 1024)
            if not chunk:
                raise ToolError("Browser connection closed unexpectedly.")
            self._buffer.extend(chunk)
        result = bytes(self._buffer[:count])
        del self._buffer[:count]
        return result

    def read_until(self, separator: bytes, *, limit: int = 64 * 1024) -> bytes:
        while separator not in self._buffer:
            if len(self._buffer) > limit:
                raise ToolError("Browser handshake response was too large.")
            chunk = self._sock.recv(64 * 1024)
            if not chunk:
                raise ToolError("Browser connection closed during handshake.")
            self._buffer.extend(chunk)
        index = self._buffer.index(separator) + len(separator)
        result = bytes(self._buffer[:index])
        del self._buffer[:index]
        return result


class WebSocketConnection:
    """Minimal client-side RFC 6455 text connection for local CDP."""

    def __init__(self, sock: socket.socket, reader: _SocketReader) -> None:
        self._sock = sock
        self._reader = reader

    def send_text(self, message: str) -> None:
        payload = message.encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])  # FIN + text opcode
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(length.to_bytes(2, "big"))
        else:
            header.append(0x80 | 127)
            header.extend(length.to_bytes(8, "big"))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        try:
            self._sock.sendall(bytes(header) + mask + masked)
        except OSError as exc:
            raise ToolError(f"Could not send to the browser: {exc}") from exc

    def recv_text(self) -> str:
        fragments = bytearray()
        while True:
            opcode, payload, fin = self._read_frame()
            if opcode == 0x8:  # close
                raise ToolError("Browser closed the DevTools connection.")
            if opcode == 0x9:  # ping -> pong
                self._send_control(0x8A, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x1, 0x2, 0x0):
                fragments.extend(payload)
                if len(fragments) > _MAX_MESSAGE_BYTES:
                    raise ToolError("Browser message exceeded the size limit.")
                if fin:
                    return fragments.decode("utf-8", errors="strict")
                continue
            raise ToolError(f"Unsupported WebSocket opcode: {opcode}")

    def _read_frame(self) -> tuple[int, bytes, bool]:
        try:
            first, second = self._reader.read_exact(2)
        except TimeoutError as exc:
            raise ToolError("Timed out waiting for the browser.") from exc
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(self._reader.read_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(self._reader.read_exact(8), "big")
        if length > _MAX_MESSAGE_BYTES:
            raise ToolError("Browser message exceeded the size limit.")
        mask = self._reader.read_exact(4) if masked else b""
        payload = self._reader.read_exact(length) if length else b""
        if masked:
            payload = bytes(
                byte ^ mask[index % 4] for index, byte in enumerate(payload)
            )
        return opcode, payload, fin

    def _send_control(self, first_byte: int, payload: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        frame = bytes([first_byte, 0x80 | len(payload)]) + mask + masked
        try:
            self._sock.sendall(frame)
        except OSError:
            pass  # best effort for pong/close

    def close(self) -> None:
        try:
            self._send_control(0x88, b"")
        finally:
            try:
                self._sock.close()
            except OSError:
                pass


def ws_connect(url: str, *, timeout: float) -> WebSocketConnection:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "ws":
        raise ToolError("Only local ws:// DevTools endpoints are supported.")
    host = parsed.hostname or ""
    if host not in _LOCAL_HOSTS:
        raise ToolError("DevTools endpoint must be on this machine (localhost).")
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise ToolError(
            f"Could not connect to the browser DevTools port {port}. {START_HINT}"
        ) from exc
    sock.settimeout(timeout)
    reader = _SocketReader(sock)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    try:
        sock.sendall(request.encode("ascii"))
        response = reader.read_until(b"\r\n\r\n").decode("iso-8859-1")
    except (OSError, ToolError) as exc:
        sock.close()
        if isinstance(exc, ToolError):
            raise
        raise ToolError(f"Browser DevTools handshake failed: {exc}") from exc
    status_line = response.splitlines()[0] if response else ""
    expected_accept = base64.b64encode(
        hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    ).decode("ascii")
    if " 101 " not in f" {status_line} " or expected_accept not in response:
        sock.close()
        raise ToolError(
            f"Browser DevTools handshake was rejected ({status_line or 'no response'})."
        )
    return WebSocketConnection(sock, reader)


# ------------------------------------------------------------------ CDP core


class WebSocketCdpConnection:
    """CDP request/response routing over one WebSocket connection."""

    def __init__(self, websocket: WebSocketConnection, *, timeout: float) -> None:
        self._ws = websocket
        self._timeout = timeout
        self._next_id = 1
        self._events: list[tuple[str | None, str, dict[str, Any]]] = []
        self._pending_results: dict[int, dict[str, Any]] = {}

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {
            "id": request_id,
            "method": method,
            "params": dict(params or {}),
        }
        if session_id is not None:
            message["sessionId"] = session_id
        self._ws.send_text(json.dumps(message))
        deadline = time.monotonic() + self._timeout
        while True:
            if request_id in self._pending_results:
                return self._unwrap(method, self._pending_results.pop(request_id))
            if time.monotonic() > deadline:
                raise ToolError(f"Browser call {method} timed out.")
            self._pump_one()

    def wait_event(
        self,
        method: str,
        *,
        session_id: str | None = None,
        timeout: float,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while True:
            for index, (event_session, event_method, params) in enumerate(
                self._events
            ):
                if event_method == method and (
                    session_id is None or event_session == session_id
                ):
                    return self._events.pop(index)[2]
            if time.monotonic() > deadline:
                return None
            try:
                self._pump_one()
            except ToolError:
                return None  # a missed load event is not fatal

    def close(self) -> None:
        self._ws.close()

    def _pump_one(self) -> None:
        raw = self._ws.recv_text()
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ToolError("Browser sent an invalid DevTools message.") from exc
        if not isinstance(message, dict):
            return
        if "id" in message:
            self._pending_results[int(message["id"])] = message
        elif "method" in message:
            params = message.get("params")
            self._events.append(
                (
                    message.get("sessionId"),
                    str(message["method"]),
                    params if isinstance(params, dict) else {},
                )
            )

    @staticmethod
    def _unwrap(method: str, message: Mapping[str, Any]) -> dict[str, Any]:
        error = message.get("error")
        if isinstance(error, Mapping):
            detail = str(error.get("message") or "unknown error")[:300]
            raise ToolError(f"Browser call {method} failed: {detail}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}


CdpFactory = Callable[[str, float], WebSocketCdpConnection]
HttpGetJson = Callable[[str, float], Any]


def _default_http_get_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4 * 1024 * 1024)
    except (OSError, urllib.error.URLError) as exc:
        raise ToolError(
            f"No browser answered on the DevTools endpoint ({url}). {START_HINT}"
        ) from exc
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError("Browser DevTools endpoint returned invalid JSON.") from exc


def _default_cdp_factory(url: str, timeout: float) -> WebSocketCdpConnection:
    return WebSocketCdpConnection(ws_connect(url, timeout=timeout), timeout=timeout)


class BrowserClient:
    """High-level access to one local Chromium DevTools endpoint."""

    def __init__(
        self,
        *,
        port: int = 9222,
        timeout: float = 20.0,
        connection_factory: CdpFactory | None = None,
        http_get_json: HttpGetJson | None = None,
    ) -> None:
        self._port = int(port)
        self._timeout = float(timeout)
        self._factory = connection_factory or _default_cdp_factory
        self._http_get_json = http_get_json or _default_http_get_json

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def version(self) -> dict[str, Any]:
        data = self._http_get_json(f"{self.endpoint}/json/version", self._timeout)
        if not isinstance(data, dict):
            raise ToolError("Browser DevTools version response was not an object.")
        return data

    def targets(self) -> list[dict[str, Any]]:
        data = self._http_get_json(f"{self.endpoint}/json/list", self._timeout)
        if not isinstance(data, list):
            raise ToolError("Browser DevTools target list was not an array.")
        return [item for item in data if isinstance(item, dict)]

    def connect(self) -> WebSocketCdpConnection:
        ws_url = str(self.version().get("webSocketDebuggerUrl") or "")
        local = self._local_ws_url(ws_url)
        return self._factory(local, self._timeout)

    def _local_ws_url(self, raw: str) -> str:
        parsed = urllib.parse.urlsplit(raw)
        host = parsed.hostname or ""
        if parsed.scheme != "ws" or host not in _LOCAL_HOSTS or not parsed.path:
            raise ToolError("Browser returned an unusable DevTools WebSocket URL.")
        return f"ws://127.0.0.1:{self._port}{parsed.path}"

    def open_new_page(self) -> PageSession:
        """Create a fresh about:blank tab and attach to it."""

        connection = self.connect()
        try:
            created = connection.call(
                "Target.createTarget", {"url": "about:blank"}
            )
            target_id = str(created.get("targetId") or "")
            if not target_id:
                raise ToolError("Browser did not create a new tab.")
            return self._attach(connection, target_id)
        except Exception:
            connection.close()
            raise

    def attach_page(self, target_id: str | None = None) -> PageSession:
        """Attach to an existing tab (default: the last listed page tab)."""

        chosen = target_id or self._last_page_target_id()
        if not chosen:
            raise ToolError(
                "The debug browser has no page tab yet. Open one first "
                "(browser.open or any page in that browser window)."
            )
        connection = self.connect()
        try:
            return self._attach(connection, chosen)
        except Exception:
            connection.close()
            raise

    def _last_page_target_id(self) -> str:
        pages = [
            item
            for item in self.targets()
            if item.get("type") == "page"
            and not str(item.get("url") or "").startswith("devtools://")
        ]
        if not pages:
            return ""
        # Chromium lists the most recent tab first; prefer the first real
        # page over leftover about:blank tabs from browser startup.
        for item in pages:
            if str(item.get("url") or "") not in ("", "about:blank"):
                return str(item.get("id") or "")
        return str(pages[0].get("id") or "")

    def _attach(
        self, connection: WebSocketCdpConnection, target_id: str
    ) -> PageSession:
        attached = connection.call(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )
        session_id = str(attached.get("sessionId") or "")
        if not session_id:
            raise ToolError("Could not attach to the browser tab.")
        session = PageSession(connection, target_id, session_id, self._timeout)
        session.call("Page.enable")
        session.call("Runtime.enable")
        return session


def probe_browser(
    *, port: int = 9222, timeout: float = 1.5
) -> dict[str, Any] | None:
    """Return DevTools version info when a debug browser answers, else None."""

    try:
        return BrowserClient(port=port, timeout=timeout).version()
    except Exception:  # noqa: BLE001 - doctor probe must never fail the report
        return None


# --------------------------------------------------------------- page session


class PageSession:
    """One attached browser tab; all JavaScript here is fixed templates."""

    def __init__(
        self,
        connection: WebSocketCdpConnection,
        target_id: str,
        session_id: str,
        timeout: float,
    ) -> None:
        self._connection = connection
        self.target_id = target_id
        self._session_id = session_id
        self._timeout = timeout

    def call(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._connection.call(method, params, session_id=self._session_id)

    def close(self) -> None:
        self._connection.close()

    def evaluate(self, expression: str) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": False,
            },
        )
        exception = result.get("exceptionDetails")
        if isinstance(exception, Mapping):
            detail = exception.get("exception")
            description = ""
            if isinstance(detail, Mapping):
                description = str(
                    detail.get("description") or detail.get("value") or ""
                )
            raise ToolError(
                f"Page script failed: {(description or 'unknown error')[:300]}"
            )
        remote = result.get("result")
        if isinstance(remote, Mapping):
            return remote.get("value")
        return None

    # ------------------------------------------------------------ navigation

    def navigate(self, url: str, *, wait_seconds: float | None = None) -> dict[str, Any]:
        result = self.call("Page.navigate", {"url": url})
        error_text = str(result.get("errorText") or "")
        if error_text:
            raise ToolError(f"Browser could not load the page: {error_text[:300]}")
        wait = self._timeout if wait_seconds is None else wait_seconds
        self._connection.wait_event(
            "Page.loadEventFired", session_id=self._session_id, timeout=wait
        )
        return self.page_identity()

    def page_identity(self) -> dict[str, Any]:
        value = self.evaluate(
            "JSON.stringify({url: location.href, title: document.title, "
            "ready: document.readyState})"
        )
        return _parse_json_object(value, "page identity")

    def snapshot(self, *, max_chars: int, max_links: int) -> dict[str, Any]:
        expression = (
            "(()=>{"
            "const body=document.body;"
            "const text=body?body.innerText||'':'';"
            "const links=Array.from(document.querySelectorAll('a[href]'))"
            f".slice(0,{int(max_links)})"
            ".map(a=>({text:(a.innerText||'').trim().slice(0,120),href:a.href}));"
            "return JSON.stringify({url:location.href,title:document.title,"
            f"text:text.slice(0,{int(max_chars)}),"
            f"truncated:text.length>{int(max_chars)},links}});"
            "})()"
        )
        return _parse_json_object(self.evaluate(expression), "page snapshot")

    # -------------------------------------------------------------- actions

    def click(
        self, *, selector: str | None = None, text: str | None = None
    ) -> dict[str, Any]:
        before = self.page_identity()
        probe = _parse_json_object(
            self.evaluate(_find_clickable_js(selector, text)), "click search"
        )
        if not probe.get("found"):
            raise ToolError(
                "Nothing clickable matched "
                + (f"selector {selector!r}" if selector else f"text {text!r}")
                + f" on this page (matches: {probe.get('matches', 0)})."
            )
        if int(probe.get("matches") or 0) > 1:
            raise ToolError(
                f"{probe['matches']} elements matched; refusing to guess. "
                "Use a more specific selector."
            )
        clicked = _parse_json_object(
            self.evaluate(_click_found_js(selector, text)), "click"
        )
        self._connection.wait_event(
            "Page.loadEventFired",
            session_id=self._session_id,
            timeout=min(5.0, self._timeout),
        )
        after = self.page_identity()
        return {
            "clicked": True,
            "tag": clicked.get("tag"),
            "text": clicked.get("text"),
            "url_before": before.get("url"),
            "url": after.get("url"),
            "title": after.get("title"),
            "target_id": self.target_id,
        }

    def type_text(self, *, selector: str, text: str) -> dict[str, Any]:
        probe = _parse_json_object(
            self.evaluate(_probe_field_js(selector)), "field search"
        )
        if not probe.get("found"):
            raise ToolError(f"No element matched selector {selector!r}.")
        field_type = str(probe.get("type") or "").lower()
        tag = str(probe.get("tag") or "").lower()
        if tag == "input" and field_type in {
            "password",
            "file",
            "hidden",
            "checkbox",
            "radio",
            "submit",
            "button",
        }:
            raise ToolError(
                f"Refusing to type into an input of type {field_type!r}."
            )
        if tag not in {"input", "textarea"}:
            raise ToolError(
                f"Selector matched a <{tag}> element, not a text field."
            )
        # First try the framework-friendly fill: native value setter plus
        # input/change events (what React/Vue listen to). If the field
        # still does not hold the text, fall back to real keystrokes.
        value = self.evaluate(_set_value_js(selector, text))
        if value != text:
            self.evaluate(_focus_select_js(selector))
            self.call("Input.insertText", {"text": text})
            value = self.evaluate(
                f"(()=>{{const el=document.querySelector({json.dumps(selector)});"
                "return el?String(el.value):null;})()"
            )
        identity = self.page_identity()
        return {
            "selector": selector,
            "value": value,
            "url": identity.get("url"),
            "target_id": self.target_id,
        }

    def screenshot_png(self) -> bytes:
        result = self.call(
            "Page.captureScreenshot", {"format": "png", "fromSurface": True}
        )
        encoded = result.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise ToolError("Browser returned an empty screenshot.")
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ToolError("Browser returned an invalid screenshot.") from exc


def _parse_json_object(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ToolError(f"Browser returned an unexpected {label} response.")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ToolError(f"Browser returned invalid {label} JSON.") from exc
    if not isinstance(parsed, dict):
        raise ToolError(f"Browser returned an unexpected {label} response.")
    return parsed


def _find_clickable_js(selector: str | None, text: str | None) -> str:
    if selector:
        return (
            f"(()=>{{const els=Array.from(document.querySelectorAll({json.dumps(selector)}));"
            "const el=els[0];"
            "return JSON.stringify({found:!!el,matches:els.length,"
            "tag:el?el.tagName:null,"
            "text:el?(el.innerText||el.value||'').trim().slice(0,120):null});})()"
        )
    wanted = json.dumps(text or "")
    return (
        "(()=>{"
        "const wanted="
        + wanted
        + ".trim().toLowerCase();"
        "const els=Array.from(document.querySelectorAll("
        "'a[href],button,[role=\"button\"],input[type=\"submit\"],input[type=\"button\"]'))"
        ".filter(el=>((el.innerText||el.value||'').trim().toLowerCase()===wanted));"
        "const el=els[0];"
        "return JSON.stringify({found:!!el,matches:els.length,"
        "tag:el?el.tagName:null,"
        "text:el?(el.innerText||el.value||'').trim().slice(0,120):null});})()"
    )


def _click_found_js(selector: str | None, text: str | None) -> str:
    finder = _find_clickable_js(selector, text)
    # Re-run the same search, then scroll + click the unique element. The
    # search template already guarantees uniqueness was checked by caller.
    if selector:
        locate = f"document.querySelector({json.dumps(selector)})"
    else:
        locate = (
            "Array.from(document.querySelectorAll("
            "'a[href],button,[role=\"button\"],input[type=\"submit\"],input[type=\"button\"]'))"
            f".find(el=>((el.innerText||el.value||'').trim().toLowerCase()==={json.dumps((text or '').strip().lower())}))"
        )
    del finder  # search expression already evaluated by the caller
    return (
        f"(()=>{{const el={locate};"
        "if(!el){return JSON.stringify({clicked:false});}"
        "el.scrollIntoView({block:'center'});"
        "const info={clicked:true,tag:el.tagName,"
        "text:(el.innerText||el.value||'').trim().slice(0,120)};"
        "el.click();"
        "return JSON.stringify(info);})()"
    )


def _probe_field_js(selector: str) -> str:
    return (
        f"(()=>{{const el=document.querySelector({json.dumps(selector)});"
        "return JSON.stringify({found:!!el,"
        "tag:el?el.tagName.toLowerCase():null,"
        "type:el?(el.getAttribute('type')||''):null});})()"
    )


def _set_value_js(selector: str, text: str) -> str:
    return (
        f"(()=>{{const el=document.querySelector({json.dumps(selector)});"
        "if(!el){return null;}"
        "el.focus();"
        "const proto=el.tagName==='TEXTAREA'"
        "?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;"
        "const setter=Object.getOwnPropertyDescriptor(proto,'value');"
        f"if(setter&&setter.set){{setter.set.call(el,{json.dumps(text)});}}"
        f"else{{el.value={json.dumps(text)};}}"
        "el.dispatchEvent(new Event('input',{bubbles:true}));"
        "el.dispatchEvent(new Event('change',{bubbles:true}));"
        "return String(el.value);})()"
    )


def _focus_select_js(selector: str) -> str:
    return (
        f"(()=>{{const el=document.querySelector({json.dumps(selector)});"
        "if(!el){return false;}"
        "el.focus();"
        "if(typeof el.select==='function'){el.select();}"
        "return true;})()"
    )
