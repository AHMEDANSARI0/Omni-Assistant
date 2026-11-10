"""Transformation Phase 13: calendar, drive, mail, chat and webhook connectors.

Everything runs against the real modules: a real SQLite database, a real ``.ics``
file on disk, a real folder with real hashes, real HMAC signatures and real IMAP
and SMTP conversations driven through injected protocol factories. Where a
connector must refuse - gate closed, wrong signature, stale timestamp, replay,
symlink escape, unconfirmed send, oversized calendar - the test asserts the
refusal and the message.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

from pc_agent import __version__
from pc_agent.baseline import TRANSFORMATION_LABEL, TRANSFORMATION_PHASE
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.connectors import (
    CalendarConnector,
    ChatConnector,
    ConnectorService,
    ConnectorStore,
    DriveConnector,
    EmailConnector,
    WebhookConnector,
    redact_endpoint,
    sign_payload,
)
from pc_agent.diagnostics import build_diagnostics, connectors_summary_line
from pc_agent.errors import ConnectorError
from pc_agent.release import audit_install
from pc_agent.service import LocalService
from pc_agent.storage.db import LATEST_SCHEMA_VERSION, Database

# ---------------------------------------------------------------- fixtures ---


def _settings(tmp_path: Path, **overrides: str) -> Settings:
    environ = {
        "PC_AGENT_ALLOW_CONNECTORS": "1",
        "PC_AGENT_CONNECTOR_CALENDAR": str(tmp_path / "calendar.ics"),
        "PC_AGENT_CONNECTOR_DRIVE_ROOT": str(tmp_path / "drive"),
        "PC_AGENT_CONNECTOR_WEBHOOK_SECRET": "webhook-secret",
        "PC_AGENT_CONNECTOR_CHAT_URL": "https://chat.example.com/hooks/abc?token=leak",
        "PC_AGENT_CONNECTOR_CHAT_SECRET": "chat-secret",
    }
    environ.update(overrides)
    settings = Settings.from_env(tmp_path, environ=environ, load_dotenv=False)
    settings.ensure_directories()
    return settings


def _database(settings: Settings) -> Database:
    database = Database(settings.database_path)
    database.initialize()
    return database


def _service(tmp_path: Path, **overrides: str) -> tuple[ConnectorService, Settings]:
    settings = _settings(tmp_path, **overrides)
    return ConnectorService(settings, _database(settings)), settings


def _public_resolver(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
    """Offline stand-in for ``socket.getaddrinfo`` (public address)."""

    import socket

    return [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))
    ]


def _signed(body: dict[str, Any], *, secret: str = "webhook-secret") -> tuple[bytes, str, str]:
    raw = json.dumps(body).encode("utf-8")
    stamp = str(int(time.time()))
    return raw, stamp, sign_payload(secret, stamp, raw)


# ------------------------------------------------------------ gates/limits ---


def test_phase13_identity_is_wired_through_the_product() -> None:
    assert __version__ == "0.82.12"
    assert TRANSFORMATION_PHASE == 18
    assert "Production" in TRANSFORMATION_LABEL
    assert LATEST_SCHEMA_VERSION == 18


def test_connectors_are_off_until_the_user_opts_in(tmp_path: Path) -> None:
    settings = _settings(tmp_path, PC_AGENT_ALLOW_CONNECTORS="0")
    service = ConnectorService(settings, _database(settings))
    status = service.status()
    assert status["enabled"] == []
    assert all(not item["enabled"] for item in status["connectors"])
    assert all("PC_AGENT_ALLOW_CONNECTORS" in item["reason"] for item in status["connectors"])
    with pytest.raises(ConnectorError) as excinfo:
        service.list_calendar_events()
    assert "calendar connector is disabled" in str(excinfo.value)


def test_master_gate_alone_is_not_enough(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        PC_AGENT_CONNECTOR_CALENDAR="",
        PC_AGENT_CONNECTOR_CHAT_SECRET="",
    )
    service = ConnectorService(settings, _database(settings))
    status = service.status()
    by_name = {item["connector"]: item for item in status["connectors"]}
    assert by_name["calendar"]["enabled"] is False
    assert "PC_AGENT_CONNECTOR_CALENDAR" in by_name["calendar"]["reason"]
    assert by_name["chat"]["enabled"] is False
    assert "PC_AGENT_CONNECTOR_CHAT_SECRET" in by_name["chat"]["reason"]
    assert by_name["webhook"]["enabled"] is True


def test_email_needs_its_own_gate(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        PC_AGENT_ALLOW_EMAIL_CONNECTOR="0",
        PC_AGENT_CONNECTOR_EMAIL_HOST="imap.example.com",
        PC_AGENT_CONNECTOR_EMAIL_USER="me@example.com",
        PC_AGENT_CONNECTOR_EMAIL_PASSWORD="app-password",
    )
    service = ConnectorService(settings, _database(settings))
    by_name = {item["connector"]: item for item in service.status()["connectors"]}
    assert by_name["email"]["enabled"] is False
    assert "PC_AGENT_ALLOW_EMAIL_CONNECTOR" in by_name["email"]["reason"]
    with pytest.raises(ConnectorError):
        service.mail_inbox(limit=1)


def test_secrets_never_reach_the_public_summary_or_status(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        PC_AGENT_CONNECTOR_EMAIL_HOST="imap.example.com",
        PC_AGENT_CONNECTOR_EMAIL_USER="me@example.com",
        PC_AGENT_CONNECTOR_EMAIL_PASSWORD="imap-secret-value",
    )
    service = ConnectorService(settings, _database(settings))
    blob = json.dumps(settings.public_summary()) + json.dumps(service.status(probe=True))
    assert "imap-secret-value" not in blob
    assert "chat-secret" not in blob
    assert "webhook-secret" not in blob
    # query strings are stripped from any endpoint we echo back
    assert "token=leak" not in blob
    assert redact_endpoint("https://host/hook?token=leak").endswith("/hook")


def test_declared_limits_are_bounded_not_guessed(tmp_path: Path) -> None:
    from pc_agent.errors import ConfigError

    defaults = _settings(tmp_path)
    assert defaults.connector_max_items == 25
    assert defaults.connector_timeout_seconds == 15.0
    assert defaults.connector_webhook_skew_seconds == 300
    for name, value, expected in (
        ("PC_AGENT_CONNECTOR_MAX_ITEMS", "5000", "between 1 and 100"),
        ("PC_AGENT_CONNECTOR_TIMEOUT_SECONDS", "9000", "between 1.0 and 120.0"),
        ("PC_AGENT_CONNECTOR_WEBHOOK_SKEW_SECONDS", "1", "between 30 and 86400"),
    ):
        with pytest.raises(ConfigError) as excinfo:
            _settings(tmp_path, **{name: value})
        assert expected in str(excinfo.value)


# ---------------------------------------------------------------- calendar ---


def test_calendar_round_trip_keeps_folding_and_order(tmp_path: Path) -> None:
    connector = CalendarConnector(tmp_path / "calendar.ics")
    soon = (datetime.now(UTC) + timedelta(hours=4)).replace(microsecond=0, tzinfo=None)
    long_title = "Weekly review: metrics, risks, and a deliberately long summary line"
    first = connector.add_event(summary=long_title, start=soon.isoformat(), duration_minutes=45)
    connector.add_event(summary="Later", start=(soon + timedelta(days=30)).isoformat())
    assert first.uid.startswith("pcagent-")
    events = connector.list_events(days_ahead=7)
    assert [event.summary for event in events] == [long_title]
    assert events[0].end > events[0].start
    lines = (tmp_path / "calendar.ics").read_text(encoding="utf-8").splitlines()
    assert all(len(line.encode("utf-8")) <= 76 for line in lines)
    assert lines[0] == "BEGIN:VCALENDAR"
    assert lines[-1] == "END:VCALENDAR"


def test_calendar_rejects_bad_input_and_oversized_files(tmp_path: Path) -> None:
    connector = CalendarConnector(tmp_path / "calendar.ics")
    with pytest.raises(ConnectorError):
        connector.add_event(summary="No start", start="not-a-date")
    with pytest.raises(ConnectorError):
        connector.add_event(summary="   ", start="2026-11-06T09:00:00")
    big = tmp_path / "big.ics"
    big.write_text("BEGIN:VCALENDAR\n" + "X" * 2_100_000, encoding="utf-8")
    with pytest.raises(ConnectorError) as excinfo:
        CalendarConnector(big).list_events()
    assert "larger than" in str(excinfo.value)


def test_calendar_service_records_a_ledger_event(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    start = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0).isoformat()
    payload = service.add_calendar_event(summary="Plan the day", start=start)
    assert payload["event"]["summary"] == "Plan the day"
    ledger = service.events()["events"]
    assert ledger[0]["connector"] == "calendar"
    assert ledger[0]["kind"] == "event_added"
    assert ledger[0]["external_id"] == payload["event"]["uid"]
    listing = service.list_calendar_events(days_ahead=7)
    assert listing["total"] == 1
    assert len(listing["digest"]) == 64


# ------------------------------------------------------------------- drive ---


def test_drive_scan_reports_adds_modifications_and_removals(tmp_path: Path) -> None:
    root = tmp_path / "drive"
    root.mkdir()
    (root / "notes.txt").write_text("first", encoding="utf-8")
    (root / "archive").mkdir()
    (root / "archive" / "old.md").write_text("old", encoding="utf-8")
    connector = DriveConnector(root, max_files=10)
    first = connector.scan(previous={})
    assert sorted(first.added) == ["archive/old.md", "notes.txt"]
    (root / "notes.txt").write_text("second", encoding="utf-8")
    (root / "archive" / "old.md").unlink()
    (root / "new.txt").write_text("new", encoding="utf-8")
    second = connector.scan(previous=first.manifest())
    assert second.modified == ["notes.txt"]
    assert second.removed == ["archive/old.md"]
    assert second.added == ["new.txt"]
    assert second.unchanged == 0


def test_drive_skips_secrets_symlinks_and_symlink_escapes(tmp_path: Path) -> None:
    root = tmp_path / "drive"
    root.mkdir()
    (root / "keep.txt").write_text("keep", encoding="utf-8")
    (root / "id_rsa.key").write_text("private", encoding="utf-8")
    (root / ".ssh").mkdir()
    (root / ".ssh" / "config").write_text("hidden", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = root / "escape.txt"
    try:
        os.symlink(outside, link)
        has_symlink = True
    except (OSError, NotImplementedError):
        has_symlink = False
    report = DriveConnector(root, max_files=10).scan(previous={})
    assert report.added == ["keep.txt"]
    assert "id_rsa.key" in report.skipped
    if has_symlink:
        assert "escape.txt" in report.skipped


def test_drive_truncates_instead_of_scanning_forever(tmp_path: Path) -> None:
    root = tmp_path / "drive"
    root.mkdir()
    for index in range(6):
        (root / f"file-{index}.txt").write_text(str(index), encoding="utf-8")
    report = DriveConnector(root, max_files=3).scan(previous={})
    assert len(report.indexed) == 3
    assert report.truncated is True


def test_drive_service_stores_the_manifest_between_runs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = tmp_path / "drive"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    service = ConnectorService(settings, _database(settings))
    first = service.drive_sync()
    assert first["added"] == 1 and first["unchanged"] == 0
    second = service.drive_sync()
    assert second["added"] == 0 and second["unchanged"] == 1
    assert ConnectorStore(Database(settings.database_path)).get_state("drive.manifest")


# -------------------------------------------------------------------- mail ---


class _FakeImap:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.selected: str | None = None
        self.logged_out = False

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        assert password == "app-password"
        return "OK", [b""]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        assert readonly is True
        self.selected = folder
        return "OK", [b"3"]

    def search(self, charset: str, criteria: str) -> tuple[str, list[bytes]]:
        return "OK", [b"1 2 3"]

    def fetch(self, identifier: bytes, spec: str) -> tuple[str, list[Any]]:
        assert "BODY.PEEK[HEADER.FIELDS" in spec
        header = (
            b"From: Sender <sender@example.com>\r\n"
            b"Subject: =?utf-8?q?Report_ready?=\r\n"
            b"Date: Wed, 05 Nov 2026 10:00:00 +0000\r\n"
            b"Message-ID: <" + identifier + b"@example.com>\r\n\r\n"
        )
        return "OK", [(b"1 (BODY[] {10}", header)]

    def logout(self) -> None:
        self.logged_out = True


class _FakeSmtp:
    instances: ClassVar[list[_FakeSmtp]] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.messages: list[Any] = []
        self.started = False
        _FakeSmtp.instances.append(self)

    def ehlo(self) -> None:
        return None

    def starttls(self, context: Any = None) -> None:
        self.started = True

    def login(self, user: str, password: str) -> None:
        assert password == "app-password"

    def send_message(self, message: Any) -> None:
        self.messages.append(message)

    def quit(self) -> None:
        return None


def _email_connector() -> EmailConnector:
    _FakeSmtp.instances.clear()
    return EmailConnector(
        imap_host="imap.example.com",
        username="me@example.com",
        password="app-password",
        folder="INBOX",
        smtp_host="smtp.example.com",
        imap_factory=_FakeImap,
        smtp_factory=_FakeSmtp,
    )


def test_mail_inbox_reads_headers_only_and_decodes_subjects() -> None:
    connector = _email_connector()
    messages = connector.inbox(limit=3)
    assert len(messages) == 3
    assert messages[0].sender.startswith("Sender")
    assert messages[0].subject == "Report ready"
    status = connector.status()
    assert status["can_read"] is True and status["can_send"] is True


def test_mail_send_needs_a_confirm_token_and_blocks_injection() -> None:
    connector = _email_connector()
    with pytest.raises(ConnectorError) as excinfo:
        connector.send(to="someone@example.com", subject="Hi", body="Body", confirm="")
    assert "confirmation" in str(excinfo.value)
    with pytest.raises(ConnectorError):
        connector.send(
            to="someone@example.com",
            subject="Hi",
            body="Body",
            confirm="SEND\r\nBcc: attacker@example.com",
        )
    with pytest.raises(ConnectorError):
        connector.send(to="not-an-address", subject="Hi", body="Body", confirm="SEND")
    with pytest.raises(ConnectorError):
        connector.send(
            to=[f"user{index}@example.com" for index in range(6)],
            subject="Hi",
            body="Body",
            confirm="SEND",
        )


def test_mail_send_delivers_one_message_and_records_it(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        PC_AGENT_ALLOW_EMAIL_CONNECTOR="1",
        PC_AGENT_CONNECTOR_EMAIL_HOST="imap.example.com",
        PC_AGENT_CONNECTOR_EMAIL_USER="me@example.com",
        PC_AGENT_CONNECTOR_EMAIL_PASSWORD="app-password",
        PC_AGENT_CONNECTOR_SMTP_HOST="smtp.example.com",
    )
    service = ConnectorService(settings, _database(settings))
    fake = _email_connector()
    monkeypatch_target = service.email_connector
    service.email_connector = lambda: fake  # type: ignore[method-assign]
    payload = service.mail_send(
        to=["someone@example.com"],
        subject="Daily summary",
        body="Everything is fine.",
        confirm="SEND",
    )
    assert payload["delivery"]["status"] == "sent"
    assert payload["delivery"]["payload_bytes"] > 0
    assert _FakeSmtp.instances and _FakeSmtp.instances[0].messages
    assert _FakeSmtp.instances[0].started is True
    assert service.deliveries()["deliveries"][0]["connector"] == "email"
    assert monkeypatch_target is not None


# -------------------------------------------------------------------- chat ---


def test_chat_send_signs_the_payload_and_records_delivery(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def transport(request: Any, timeout: float) -> tuple[int, bytes]:
        captured["url"] = request.full_url
        captured["body"] = request.data
        captured["signature"] = request.get_header("X-pcagent-signature")
        captured["timestamp"] = request.get_header("X-pcagent-timestamp")
        captured["timeout"] = timeout
        return 204, b""

    settings = _settings(tmp_path)
    service = ConnectorService(settings, _database(settings))
    service.chat_connector = lambda: ChatConnector(  # type: ignore[method-assign]
        settings.connector_chat_url,
        secret=settings.connector_chat_secret,
        timeout=settings.connector_timeout_seconds,
        transport=transport,
        resolver=_public_resolver,
    )
    payload = service.chat_send("Build finished", target="ops")
    assert payload["result"]["ok"] is True
    assert payload["result"]["status_code"] == 204
    assert captured["url"].startswith("https://chat.example.com/hooks/abc")
    assert "token=leak" not in payload["result"]["target"]
    assert json.loads(captured["body"])["text"] == "Build finished"
    expected = sign_payload("chat-secret", captured["timestamp"], captured["body"])
    assert captured["signature"] == expected
    assert service.deliveries()["deliveries"][0]["status"] == "sent"


def test_chat_failure_is_recorded_not_hidden(tmp_path: Path) -> None:
    def transport(request: Any, timeout: float) -> tuple[int, bytes]:
        return 500, b"upstream exploded"

    settings = _settings(tmp_path)
    service = ConnectorService(settings, _database(settings))
    service.chat_connector = lambda: ChatConnector(  # type: ignore[method-assign]
        settings.connector_chat_url,
        secret=settings.connector_chat_secret,
        transport=transport,
        resolver=_public_resolver,
    )
    payload = service.chat_send("Try anyway")
    assert payload["result"]["ok"] is False
    rows = service.deliveries()["deliveries"]
    assert rows[0]["status"] == "failed"
    assert "500" in rows[0]["detail"]
    assert service.summary()["deliveries_failed"] == 1


def test_chat_refuses_private_endpoints_by_default(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        PC_AGENT_CONNECTOR_CHAT_URL="http://127.0.0.1:8080/hook",
    )
    connector = ChatConnector(
        settings.connector_chat_url,
        secret="chat-secret",
        allow_private_network=False,
    )
    with pytest.raises(ConnectorError) as excinfo:
        connector.send("hello")
    assert "refused" in str(excinfo.value)


# ----------------------------------------------------------------- webhook ---


def test_webhook_accepts_a_fresh_signed_event(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    body, stamp, signature = _signed({"kind": "build", "id": "b-1", "summary": "CI green"})
    payload = service.receive_webhook(body, timestamp=stamp, signature=signature)
    assert payload["receipt"]["accepted"] is True
    assert payload["receipt"]["kind"] == "build"
    assert payload["ledger_event"]["external_id"] == "b-1"
    assert service.events()["events"][0]["summary"] == "CI green"


def test_webhook_refuses_replay_stale_and_forged_events(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    body, stamp, signature = _signed({"kind": "build", "id": "b-2"})
    assert service.receive_webhook(body, timestamp=stamp, signature=signature)["receipt"][
        "accepted"
    ]
    replay = service.receive_webhook(body, timestamp=stamp, signature=signature)
    assert replay["receipt"]["accepted"] is False
    assert "replay" in replay["receipt"]["reason"]

    forged = service.receive_webhook(body, timestamp=stamp, signature="sha256=deadbeef")
    assert forged["receipt"]["accepted"] is False
    assert "signature" in forged["receipt"]["reason"]

    stale = str(int(time.time()) - 10_000)
    stale_signature = sign_payload("webhook-secret", stale, body)
    aged = service.receive_webhook(body, timestamp=stale, signature=stale_signature)
    assert aged["receipt"]["accepted"] is False
    assert "window" in aged["receipt"]["reason"]
    assert service.summary()["events"] == 4


def test_webhook_rejects_oversized_and_non_json_bodies(tmp_path: Path) -> None:
    connector = WebhookConnector(secret="webhook-secret")
    stamp = str(int(time.time()))
    huge = b"x" * 70_000
    with pytest.raises(ConnectorError):
        connector.verify(huge, timestamp=stamp, signature=sign_payload("webhook-secret", stamp, huge))
    not_json = b"not json at all"
    receipt = connector.verify(
        not_json, timestamp=stamp, signature=sign_payload("webhook-secret", stamp, not_json)
    )
    assert receipt.accepted is False
    assert receipt.reason == "body is not JSON"


# ------------------------------------------------------ service and surfaces ---


def test_local_service_exposes_the_connector_surface(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (tmp_path / "drive").mkdir(exist_ok=True)
    with LocalService(settings) as service:
        status = service.connector_status()
        assert "calendar" in status["enabled"]
        summary = service.connector_summary()
        assert summary["allow_connectors"] is True
        start = (datetime.now(UTC) + timedelta(days=2)).replace(microsecond=0).isoformat()
        created = service.connector_calendar_add(summary="Call with team", start=start)
        assert created["event"]["summary"] == "Call with team"
        assert service.connector_calendar_events(days_ahead=7)["total"] == 1
        assert service.connector_drive_sync()["files"] >= 0
        assert service.connector_events(limit=5)["total"] >= 2
        assert service.connector_deliveries(limit=5)["total"] == 0


def test_cli_connector_commands_report_real_state(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    environ = {
        "PC_AGENT_ROOT": str(tmp_path),
        "PC_AGENT_ALLOW_CONNECTORS": "1",
        "PC_AGENT_CONNECTOR_CALENDAR": str(tmp_path / "calendar.ics"),
        "PC_AGENT_CONNECTOR_DRIVE_ROOT": str(tmp_path / "drive"),
    }
    (tmp_path / "drive").mkdir(exist_ok=True)
    monkey_monkey = dict(os.environ)
    os.environ.update(environ)
    try:
        assert main(["connectors", "status"]) == 0
        out = capsys.readouterr().out
        assert "connectors=on" in out
        assert "calendar  ready" in out

        start = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0).isoformat()
        assert main(["connectors", "calendar", "add", "--summary", "CLI event", "--start", start]) == 0
        capsys.readouterr()
        assert main(["connectors", "calendar", "list", "--days", "5"]) == 0
        assert "CLI event" in capsys.readouterr().out
        assert main(["connectors", "drive", "sync"]) == 0
        assert "file(s) indexed" in capsys.readouterr().out

        assert main(["connectors", "mail", "inbox"]) == 1
        assert "email connector is disabled" in capsys.readouterr().out
    finally:
        os.environ.clear()
        os.environ.update(monkey_monkey)


def test_diagnostics_and_release_report_connectors(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    section = build_diagnostics(settings, database)["connectors"]
    assert section["available"] is True
    assert section["allow_connectors"] is True
    line = connectors_summary_line(settings, database)
    assert line.startswith("enabled=")
    audit = audit_install(settings)
    checks = {check.name: check for check in audit.checks}
    assert "connectors" in checks
    assert len(audit.checks) == 34
    # With connectors switched on the audit warns (honestly) instead of passing.
    assert checks["connectors"].status == "warn"
    assert "PC_AGENT_ALLOW_CONNECTORS" in checks["connectors"].detail
    off_root = tmp_path / "off"
    off_settings = Settings.from_env(
        off_root, environ={"PC_AGENT_ALLOW_CONNECTORS": "0"}, load_dotenv=False
    )
    off_checks = {check.name: check for check in audit_install(off_settings).checks}
    assert off_checks["connectors"].status == "pass"
    assert "connector_events" in off_checks["connectors"].detail
    assert off_checks["connectors"].detail.count("schema v18") == 1


def test_ledgers_stay_bounded(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = ConnectorStore(_database(settings))
    for index in range(30):
        store.record_event("webhook", "ping", summary=f"event {index}", limit=10)
    assert len(store.events(limit=100)) == 10
    for index in range(25):
        store.record_delivery("chat", "https://host/hook", "sent", limit=5)
    assert len(store.deliveries(limit=100)) == 5
    assert store.counts()["deliveries"] == 5
