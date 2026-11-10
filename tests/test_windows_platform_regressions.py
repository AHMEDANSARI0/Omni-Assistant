"""0.82.6: the three things a Windows run of the suite actually caught.

The suite is green on Linux and was not on Windows, for reasons that had nothing
to do with the features being tested. Each one is fixed here, and each fix is
pinned by a test that fails without it:

1. **A POSIX path was asserted instead of a platform-correct one.**
   ``chrome_style_argv(Path("/opt/chrome"))`` was expected to start with
   ``/opt/chrome``. On Windows ``str(Path(...))`` is ``\\opt\\chrome`` - the code
   was right, the assertion was not. The real contract is "the executable reaches
   CreateProcess unchanged"; a caller-supplied **string** is now passed through
   verbatim (no ``Path`` round trip) and the test asserts that contract on both
   platforms instead of one literal.

2. **``LocalApiServer.stop()`` closed the socket under a running loop.**
   ``serve_forever`` (the ``pc-agent api serve`` path) runs on a caller-owned
   thread, so ``self._thread`` is ``None`` and ``stop()`` skipped ``shutdown()``
   and went straight to ``server_close()``. On Windows the loop then wakes up
   selecting on a closed socket: ``OSError [WinError 10038]``. ``stop()`` now
   shuts the loop down first, and the loop itself survives a socket error
   instead of killing its thread.

3. **A failing release-chain check was silent in ``--json`` mode.**
   ``pc-agent release production-check --json`` printed a report and exited 1
   with the reason buried inside the JSON, so a test (and a person) saw only
   ``assert 1 == 0``. Every failing step is now named on stderr while stdout
   stays valid JSON, and the two Windows-relevant file operations in the chain
   are retried when the OS reports a transient sharing violation.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from pc_agent import cli
from pc_agent.api_server import LocalApiServer
from pc_agent.desktop_window import chrome_style_argv
from pc_agent.production_check import (
    _TEMP_PREFIX,
    ProductionReport,
    ProductionStep,
    _retry_file_op,
)

# ------------------------------------------------ 1. argv is platform-correct

def test_argv_keeps_the_executable_the_caller_gave(tmp_path: Path) -> None:
    """The executable reaches the process unchanged, on every platform."""

    given = Path("/opt/chrome")
    argv = chrome_style_argv(given, "http://127.0.0.1:8765/", profile_dir=tmp_path / "p")
    assert argv[0] == os.fspath(given), (
        "the executable must be the host's representation of the path, not a literal"
    )
    # The intent of the original assertion: no shell string, no chrome.
    assert isinstance(argv, list)
    assert "--app=http://127.0.0.1:8765/" in argv
    assert not any(flag in " ".join(argv) for flag in ("&&", "||", ";"))


def test_a_windows_path_string_is_passed_through_verbatim(tmp_path: Path) -> None:
    """A string path must not be re-normalised - ``C:\\...`` is not a URL."""

    windows_exe = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    argv = chrome_style_argv(windows_exe, "http://127.0.0.1:8765/", profile_dir=tmp_path / "p")
    assert argv[0] == windows_exe
    assert "\\" in argv[0]
    assert any(item.startswith("--user-data-dir=") for item in argv)


def test_pywebview_and_app_window_still_claim_no_chrome(tmp_path: Path, monkeypatch) -> None:
    from pc_agent.desktop_window import plan_desktop_window

    monkeypatch.setattr("pc_agent.desktop_window.pywebview_available", lambda: False)
    exe = tmp_path / "msedge.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr("pc_agent.desktop_window.find_app_browser", lambda: exe)
    plan = plan_desktop_window("http://127.0.0.1:8765/", profile_dir=tmp_path / "p")
    assert plan.kind == "app-window"
    assert plan.opens_without_browser_chrome is True
    assert plan.argv[0] == os.fspath(exe)


# ------------------------------------------- 2. the server can always be stopped

def _server(tmp_path: Path) -> LocalApiServer:
    """A server on an OS-assigned port.

    ``api_port=0`` matters: a developer (or the user) may well have
    ``pc-agent api serve`` running on the default port, and a test that binds
    8765 would then fail with "address already in use" for no product reason.
    """

    from dataclasses import replace

    from pc_agent.config import Settings

    settings = replace(Settings.from_env(tmp_path), api_port=0)
    settings.ensure_directories()
    return LocalApiServer(settings)


def test_stop_while_serve_forever_runs_does_not_raise(tmp_path: Path) -> None:
    """The ``api serve`` path: a loop we do not own must be shut down first."""

    server = _server(tmp_path)
    started = threading.Event()

    def _serve() -> None:
        started.set()
        server.serve_forever()

    thread = threading.Thread(target=_serve, name="omni-ui", daemon=True)
    thread.start()
    assert started.wait(5)
    time.sleep(0.2)

    begin = time.monotonic()
    server.stop()  # must not raise OSError/WinError 10038
    elapsed = time.monotonic() - begin
    thread.join(timeout=5)
    assert not thread.is_alive(), "the serve loop must end when stop() is called"
    assert elapsed < 5, "stop() must return, not block on a loop that is not running"


def test_stop_is_immediate_when_the_blocking_loop_never_started(tmp_path: Path) -> None:
    """``shutdown()`` on a server that never served would hang forever."""

    server = _server(tmp_path)
    begin = time.monotonic()
    server.stop()
    assert time.monotonic() - begin < 2
    server.stop()  # and it is idempotent


def test_a_socket_error_does_not_kill_the_serving_thread(tmp_path: Path, monkeypatch) -> None:
    server = _server(tmp_path)

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError(10038, "An operation was attempted on something that is not a socket")

    monkeypatch.setattr(server._httpd, "serve_forever", _boom)
    errors: list[BaseException] = []

    def _serve() -> None:
        try:
            server.serve_forever()
        except BaseException as exc:  # noqa: BLE001 - the point of the test
            errors.append(exc)

    thread = threading.Thread(target=_serve, daemon=True, name="omni-ui")
    thread.start()
    thread.join(timeout=5)
    assert errors == [], f"the loop leaked {errors!r} out of its thread"
    server.stop()


# --------------------------------------- 3. a failing chain is never silent

def _failing_report() -> ProductionReport:
    return ProductionReport(
        version="0.0.0",
        generated_at="2026-01-01T00:00:00+00:00",
        steps=(
            ProductionStep(name="release_catalog", status="pass", detail="ok", duration_ms=1),
            ProductionStep(
                name="apply",
                status="fail",
                detail="Staged update could not be applied: access denied (WinError 5)",
                duration_ms=2,
            ),
        ),
    )


def test_json_report_stays_parseable_and_the_reason_goes_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli._print_production_report(_failing_report(), as_json=True, out=None)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)  # stdout is still machine-readable
    assert payload["ok"] is False
    assert "apply" in captured.err
    assert "WinError 5" in captured.err
    assert "apply" not in captured.out.replace('"apply"', "")


def test_text_report_names_the_failing_step_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli._print_production_report(_failing_report(), as_json=False, out=None)
    captured = capsys.readouterr()
    assert "Release chain step failed -> apply:" in captured.err
    assert "ATTENTION" in captured.out


def test_report_exposes_failures_without_parsing_text() -> None:
    report = _failing_report()
    assert report.first_failure is not None
    assert report.first_failure.name == "apply"
    assert report.failure_lines() == [
        "apply: Staged update could not be applied: access denied (WinError 5)"
    ]
    assert report.ok is False


def test_retry_survives_a_transient_windows_sharing_violation() -> None:
    calls: list[int] = []

    def _op() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(13, "The process cannot access the file")
        return "done"

    assert _retry_file_op(_op, attempts=5, delay=0.001) == "done"
    assert len(calls) == 3


def test_retry_does_not_hide_a_real_problem() -> None:
    def _op() -> None:
        raise FileNotFoundError(2, "missing")

    with pytest.raises(FileNotFoundError):
        _retry_file_op(_op, attempts=3, delay=0.001)


def test_a_broken_operation_is_raised_after_the_last_attempt() -> None:
    attempts: list[int] = []

    def _op() -> None:
        attempts.append(1)
        raise PermissionError(13, "still locked")

    with pytest.raises(PermissionError):
        _retry_file_op(_op, attempts=4, delay=0.001)
    assert len(attempts) == 4


# -------------------------------------------------- temp path stays short

def test_the_work_directory_prefix_is_short() -> None:
    """Windows MAX_PATH: ``<temp>/<prefix>xxxx/install/<58-char path>`` must fit."""

    assert len(_TEMP_PREFIX) <= 12
    assert _TEMP_PREFIX.endswith("-")

    from pc_agent import production_check

    captured: dict[str, str] = {}
    real = production_check.tempfile.mkdtemp

    def _spy(*args: object, **kwargs: object) -> str:
        captured["prefix"] = str(kwargs.get("prefix", args[0] if args else ""))
        return real()

    production_check.tempfile.mkdtemp = _spy  # type: ignore[assignment]
    try:
        from pc_agent.config import Settings

        tmp = Path(real())  # unrelated dir, cleaned by the OS
        settings = Settings.from_env(tmp / "root")
        settings.ensure_directories()
        # The chain's own result is not the point of this test; only the prefix
        # it chose for its work directory is. A failure here is still recorded.
        try:
            production_check.run_production_check(settings, write_report=False)
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            captured["error"] = str(exc)
    finally:
        production_check.tempfile.mkdtemp = real  # type: ignore[assignment]
    assert captured.get("prefix") == _TEMP_PREFIX


def test_windows_max_path_budget_holds_for_the_longest_shipped_file() -> None:
    """A guard on the whole idea: no release path may exceed the budget."""

    from pc_agent.release import release_candidate_files

    root = Path(__file__).resolve().parent.parent
    longest = max(len(path.as_posix()) for path in release_candidate_files(root))
    # 260 is the classic Windows MAX_PATH; leave room for the work directory.
    assert longest < 120, f"a release path is {longest} characters long"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only long-path check")
def test_windows_paths_stay_under_the_classic_limit(tmp_path: Path) -> None:  # pragma: no cover
    from pc_agent.release import release_candidate_files

    root = Path(__file__).resolve().parent.parent
    base = str(tmp_path)
    longest = max(len(str(Path(base) / path)) for path in release_candidate_files(root))
    assert longest < 260
