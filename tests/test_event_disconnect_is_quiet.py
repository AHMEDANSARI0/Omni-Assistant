"""0.82.10: a client hanging up is not an application error.

The Command Center subscribes to ``GET /v1/events/stream`` and reconnects when
the two-minute stream ends. Closing the window, reloading, or the browser
dropping a socket mid-stream used to surface as::

    ERROR | pc_agent.api | Local API GET /v1/events/stream failed
    ConnectionAbortedError: [WinError 10053] ...

because only ``BrokenPipeError`` and ``ConnectionResetError`` were caught - the
two errors POSIX raises. Windows raises ``ConnectionAbortedError`` (10053), so
the fault was invisible on Linux and only ever appeared on the user's machine.
That ERROR also triggered a 500 reply on a dead socket, which is the second
traceback ("Exception occurred during processing of request ...").

These tests pin both halves: the write path ends quietly, and nothing tries to
answer a client that has gone.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

import pytest

from pc_agent import api_server
from pc_agent.api_server import _CLIENT_GONE, LocalApiServer, _QuietThreadingHTTPServer
from pc_agent.config import Settings

LOGGER = "pc_agent.api"


class _DeadSocket:
    """A write target that fails the way a closed client socket fails."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.writes = 0

    def write(self, _data: bytes) -> int:
        self.writes += 1
        raise self.error

    def flush(self) -> None:
        raise self.error


class _FakeHandler:
    """Enough of ``BaseHTTPRequestHandler`` for the stream/dispatch paths."""

    def __init__(self, error: BaseException, path: str = "/v1/events/stream") -> None:
        self.wfile = _DeadSocket(error)
        self.path = path
        self.close_connection = False
        self.command = "GET"
        self.headers: dict[str, str] = {}
        self.errors: list[tuple[int, str, str]] = []

    # response plumbing the stream calls before it writes
    def send_response(self, *_args: object, **_kwargs: object) -> None:
        pass

    def send_header(self, *_args: object, **_kwargs: object) -> None:
        pass

    def end_headers(self) -> None:
        pass

    def _error(self, status: int, code: str, message: str) -> None:
        self.errors.append((status, code, message))

    def _authorized(self) -> bool:
        return True

    def _route(self, *_args: object, **_kwargs: object) -> None:
        raise self.wfile.error


@pytest.fixture()
def handler_class(tmp_path: Path):
    """The real handler class, bound to a throwaway service."""

    settings = replace(Settings.from_env(tmp_path), api_port=0)
    service = api_server.LocalService(settings)
    try:
        yield api_server._make_handler(service, "token")
    finally:
        service.close()


@pytest.mark.parametrize(
    "error",
    [
        ConnectionAbortedError(10053, "An established connection was aborted"),
        ConnectionResetError(10054, "An existing connection was forcibly closed"),
        BrokenPipeError(32, "Broken pipe"),
        OSError(10053, "Winsock abort"),
    ],
    ids=["windows-10053", "windows-10054", "posix-epipe", "bare-oserror"],
)
def test_the_stream_ends_quietly_whatever_the_platform_calls_it(
    handler_class: type, error: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    handler = handler_class
    fake = _FakeHandler(error)

    # No exception may escape: an escaping error is what produced the traceback.
    handler._stream_events.__get__(fake, _FakeHandler)({"duration": "5"})

    assert fake.wfile.writes >= 1
    noisy = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert noisy == [], [r.getMessage() for r in noisy]


def test_dispatch_does_not_answer_a_client_that_is_gone(
    handler_class: type, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    handler = handler_class
    fake = _FakeHandler(ConnectionAbortedError(10053, "aborted"))

    handler._dispatch.__get__(fake, _FakeHandler)("GET")

    # The old code logged at ERROR and then tried to send a 500 on the same
    # dead socket - the source of the second traceback in the report.
    assert fake.errors == []
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_a_real_application_error_still_reports_a_500(
    handler_class: type, caplog: pytest.LogCaptureFixture
) -> None:
    """The quiet path must not swallow bugs: a genuine fault still says so."""

    caplog.set_level(logging.DEBUG, logger=LOGGER)
    handler = handler_class

    class _Broken(_FakeHandler):
        def _route(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("boom")

    fake = _Broken(ConnectionAbortedError(10053, "unused"))
    handler._dispatch.__get__(fake, _FakeHandler)("GET")

    assert fake.errors and fake.errors[0][0] == 500
    assert [r for r in caplog.records if r.levelno == logging.ERROR]


def test_connection_aborted_is_part_of_the_handled_set() -> None:
    assert ConnectionAbortedError in _CLIENT_GONE
    assert BrokenPipeError in _CLIENT_GONE
    assert ConnectionResetError in _CLIENT_GONE


def test_the_server_swallows_a_hangup_instead_of_printing_a_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    server = _QuietThreadingHTTPServer.__new__(_QuietThreadingHTTPServer)
    assert server.daemon_threads is True
    try:
        raise ConnectionAbortedError(10053, "An established connection was aborted")
    except ConnectionAbortedError:
        server.handle_error(None, ("127.0.0.1", 50000))  # must not raise
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_the_server_still_surfaces_unexpected_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Quiet about hang-ups, not about real bugs: the base class must still run."""

    caplog.set_level(logging.DEBUG, logger=LOGGER)
    calls: list[str] = []
    monkeypatch.setattr(
        api_server.ThreadingHTTPServer,
        "handle_error",
        lambda self, request, client_address: calls.append("base"),
    )
    settings = replace(Settings.from_env(tmp_path), api_port=0)
    server = LocalApiServer(settings)
    try:
        try:
            raise RuntimeError("not a disconnect")
        except RuntimeError:
            server._httpd.handle_error(None, ("127.0.0.1", 50001))
        assert calls == ["base"]
    finally:
        server.stop()
        server._service.close()
