"""Phase 24: explicit online download of signed portable updates."""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from pc_agent import __version__, release_admin
from pc_agent import online_updates as online_module
from pc_agent import release as release_module
from pc_agent.cli import main
from pc_agent.config import ConfigError, Settings
from pc_agent.errors import PcAgentError
from pc_agent.gui.controller import (
    GuiController,
    format_downloaded_update,
    format_online_stage_result,
    format_online_update_check,
)
from pc_agent.online_updates import (
    check_online_update,
    download_online_update,
    stage_online_update,
)
from pc_agent.release import (
    audit_install,
    build_release_bundle,
    release_candidate_files,
)
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry
from pc_agent.updates import default_signature_path, list_updates

_FUTURE = "0.42.0"
_FEED_URL = "https://updates.example.com/self-healing-pc-agent/stable.json"
_BUNDLE_NAME = f"release-{_FUTURE}.zip"
_SIGNATURE_NAME = f"{_BUNDLE_NAME}.sig.json"
_BUNDLE_URL = f"https://updates.example.com/self-healing-pc-agent/{_BUNDLE_NAME}"
_SIGNATURE_URL = (
    f"https://updates.example.com/self-healing-pc-agent/{_SIGNATURE_NAME}"
)


def _settings(root: Path, **overrides: str) -> Settings:
    values = {
        "PC_AGENT_PROVIDER_ORDER": "mock",
        "PC_AGENT_CREDENTIAL_STORE": "env",
        "PC_AGENT_ONLINE_UPDATES": "1",
        "PC_AGENT_UPDATE_FEED_URL": _FEED_URL,
        "PC_AGENT_UPDATE_CHANNEL": "stable",
        **overrides,
    }
    settings = Settings.from_env(root, environ=values, load_dotenv=False)
    settings.ensure_directories()
    return settings


def _write_project(root: Path, version: str, *, value: int) -> None:
    (root / "src" / "pc_agent").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(exist_ok=True)
    (root / "scripts").mkdir(exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "workspace").mkdir(exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "self-healing-pc-agent"\n'
        f'version = "{version}"\n',
        encoding="utf-8",
    )
    (root / "src" / "pc_agent" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    (root / "src" / "pc_agent" / "module.py").write_text(
        f"VALUE = {value}\n", encoding="utf-8"
    )
    (root / "README.md").write_text("Portable online-update test.\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("Changes.\n", encoding="utf-8")
    (root / ".env.example").write_text("EXAMPLE=1\n", encoding="utf-8")
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")
    (root / "docs" / "phase24.md").write_text("Phase 24.\n", encoding="utf-8")
    (root / "scripts" / "install_windows.ps1").write_text(
        "Write-Host ok\n", encoding="utf-8"
    )
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (root / "workspace" / ".gitkeep").write_text("", encoding="utf-8")


def _signed_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[bytes, bytes, str]:
    source = tmp_path / "future-source"
    _write_project(source, _FUTURE, value=27)
    bundle = tmp_path / _BUNDLE_NAME
    monkeypatch.setattr(release_module, "__version__", _FUTURE)
    build_release_bundle(source, bundle)

    keys = tmp_path / "vendor-keys"
    assert (
        release_admin.main(
            ["keygen", "--out-dir", str(keys), "--key-id", "release-test"]
        )
        == 0
    )
    capsys.readouterr()
    public_key = next(
        line.split("=", 1)[1]
        for line in (keys / "release-public-key.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.startswith("PC_AGENT_RELEASE_PUBLIC_KEY=")
    )
    assert (
        release_admin.main(
            [
                "sign",
                str(bundle),
                "--private-key",
                str(keys / "release-private-key.txt"),
                "--key-id",
                "release-test",
            ]
        )
        == 0
    )
    capsys.readouterr()
    signature = default_signature_path(bundle)
    return bundle.read_bytes(), signature.read_bytes(), public_key


def _feed_bytes(
    bundle: bytes,
    *,
    version: str = _FUTURE,
    channel: str = "stable",
    bundle_url: str = _BUNDLE_NAME,
    signature_url: str | None = _SIGNATURE_NAME,
    bundle_sha256: str | None = None,
    size_bytes: int | None = None,
    extra: dict[str, Any] | None = None,
) -> bytes:
    payload: dict[str, Any] = {
        "format": 1,
        "app": "self-healing-pc-agent",
        "channel": channel,
        "version": version,
        "bundle_url": bundle_url,
        "bundle_sha256": bundle_sha256 or hashlib.sha256(bundle).hexdigest(),
        "size_bytes": len(bundle) if size_bytes is None else size_bytes,
        "published_at": "2026-10-29T00:00:00Z",
    }
    if signature_url is not None:
        payload["signature_url"] = signature_url
    if extra:
        payload.update(extra)
    return json.dumps(payload, indent=2).encode("utf-8")


class _FakeTransport:
    def __init__(self, mapping: dict[str, bytes]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, url: str, max_bytes: int, timeout: float) -> bytes:
        self.calls.append(url)
        if url not in self.mapping:
            raise RuntimeError(f"unexpected URL {url}")
        return self.mapping[url]


def _transport(bundle: bytes, signature: bytes, feed: bytes) -> _FakeTransport:
    return _FakeTransport(
        {_FEED_URL: feed, _BUNDLE_URL: bundle, _SIGNATURE_URL: signature}
    )


def _signed_settings(root: Path, public_key: str, **overrides: str) -> Settings:
    values = {
        "PC_AGENT_RELEASE_PUBLIC_KEY": public_key,
        "PC_AGENT_RELEASE_KEY_ID": "release-test",
        "PC_AGENT_REQUIRE_SIGNED_UPDATES": "1",
        **overrides,
    }
    return _settings(root, **values)


def test_online_update_configuration_is_default_off_and_validated(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    _write_project(target, __version__, value=26)

    default = _settings(target, PC_AGENT_ONLINE_UPDATES="0")
    assert default.online_updates_enabled is False
    assert default.update_feed_url == _FEED_URL
    audit = {check.name: check for check in audit_install(default).checks}
    assert audit["online_updates"].status == "info"

    enabled = _settings(target)
    summary = enabled.public_summary()
    assert enabled.online_updates_enabled is True
    assert summary["online_updates"] == "yes"
    assert summary["update_feed_configured"] == "yes"
    assert summary["update_feed_host"] == "updates.example.com"
    assert summary["update_channel"] == "stable"
    assert _FEED_URL not in str(summary)
    audit = {check.name: check for check in audit_install(enabled).checks}
    assert audit["online_updates"].status == "warn"

    signed = _signed_settings(target, "A" * 43)
    audit = {check.name: check for check in audit_install(signed).checks}
    assert audit["online_updates"].status == "pass"

    invalid_cases = [
        ({"PC_AGENT_ONLINE_UPDATES": "sometimes"}, "PC_AGENT_ONLINE_UPDATES"),
        (
            {"PC_AGENT_UPDATE_FEED_URL": "http://updates.example.com/stable.json"},
            "must use HTTPS",
        ),
        (
            {"PC_AGENT_UPDATE_FEED_URL": "https://updates.example.com/stable.json?x=1"},
            "query",
        ),
        ({"PC_AGENT_UPDATE_CHANNEL": "bad channel"}, "PC_AGENT_UPDATE_CHANNEL"),
        (
            {"PC_AGENT_ONLINE_UPDATE_TIMEOUT_SECONDS": "121"},
            "PC_AGENT_ONLINE_UPDATE_TIMEOUT_SECONDS",
        ),
        (
            {"PC_AGENT_ONLINE_UPDATE_MAX_BYTES": "999999"},
            "PC_AGENT_ONLINE_UPDATE_MAX_BYTES",
        ),
    ]
    for overrides, message in invalid_cases:
        with pytest.raises(ConfigError, match=message):
            _settings(target, **overrides)

    with pytest.raises(PcAgentError, match="disabled"):
        check_online_update(default, transport=_FakeTransport({}))
    with pytest.raises(PcAgentError, match="No online update feed"):
        check_online_update(
            _settings(target, PC_AGENT_UPDATE_FEED_URL=""),
            transport=_FakeTransport({}),
        )


def test_online_check_is_strict_same_origin_and_feed_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "target"
    _write_project(target, __version__, value=26)
    bundle, signature, _public = _signed_bundle(tmp_path, monkeypatch, capsys)
    settings = _settings(target)
    transport = _transport(bundle, signature, _feed_bytes(bundle))

    check = check_online_update(settings, transport=transport)
    assert check.current_version == __version__
    assert check.manifest.version == _FUTURE
    assert check.update_available is True
    assert check.feed_host == "updates.example.com"
    assert check.to_dict()["bundle_signature_verified"] is False
    assert transport.calls == [_FEED_URL]

    with pytest.raises(PcAgentError, match="must use HTTPS"):
        check_online_update(
            settings,
            feed_url="http://updates.example.com/stable.json",
            transport=transport,
        )

    bad_feeds = [
        (_feed_bytes(bundle, channel="beta"), "channel"),
        (
            _feed_bytes(
                bundle,
                bundle_url="https://evil.example.com/release-0.27.0.zip",
            ),
            "stay on the update feed origin",
        ),
        (_feed_bytes(bundle, extra={"notes": "untrusted"}), "unknown notes"),
        (b" " * (online_module.MAX_FEED_BYTES + 1), "exceeds"),
        (
            b'{"format":1,"format":1,"app":"self-healing-pc-agent"}',
            "strict JSON",
        ),
    ]
    for feed, message in bad_feeds:
        fake = _FakeTransport({_FEED_URL: feed})
        with pytest.raises(PcAgentError, match=message):
            check_online_update(settings, transport=fake)


def test_online_download_and_stage_verify_signed_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "target"
    _write_project(target, __version__, value=26)
    bundle, signature, public_key = _signed_bundle(tmp_path, monkeypatch, capsys)
    settings = _signed_settings(target, public_key)
    transport = _transport(bundle, signature, _feed_bytes(bundle))

    downloaded = download_online_update(settings, transport=transport)
    assert downloaded.status == "downloaded"
    assert downloaded.check.signature.status == "verified"
    assert downloaded.bundle_path.read_bytes() == bundle
    assert downloaded.signature_path is not None
    assert downloaded.signature_path.read_bytes() == signature
    receipt = json.loads(downloaded.receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "downloaded"
    assert receipt["check"]["signature"]["status"] == "verified"
    assert list_updates(settings) == ()
    assert (
        target / "src" / "pc_agent" / "module.py"
    ).read_text(encoding="utf-8") == "VALUE = 26\n"

    result = stage_online_update(settings, transport=transport)
    assert result.downloaded.status == "staged"
    assert result.staged.status == "staged"
    assert result.staged.signature_status == "verified"
    staged_receipt = json.loads(
        result.downloaded.receipt_path.read_text(encoding="utf-8")
    )
    assert staged_receipt["status"] == "staged"
    assert staged_receipt["staged_update_id"] == result.staged.update_id
    assert list_updates(settings)[0].update_id == result.staged.update_id
    assert (
        result.staged.staging_dir / "src" / "pc_agent" / "module.py"
    ).read_text(encoding="utf-8") == "VALUE = 27\n"
    assert (
        target / "src" / "pc_agent" / "module.py"
    ).read_text(encoding="utf-8") == "VALUE = 26\n"


def test_online_download_rejects_hash_mismatch_and_bad_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "target"
    _write_project(target, __version__, value=26)
    bundle, signature, public_key = _signed_bundle(tmp_path, monkeypatch, capsys)
    settings = _signed_settings(target, public_key)

    bad_hash = _transport(
        bundle,
        signature,
        _feed_bytes(bundle, bundle_sha256="0" * 64),
    )
    with pytest.raises(PcAgentError, match="SHA-256 does not match"):
        download_online_update(settings, transport=bad_hash)
    receipts = list(
        (settings.data_dir / "updates" / "downloads").glob(
            "*/download-receipt.json"
        )
    )
    assert len(receipts) == 1
    failed = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert not Path(failed["bundle_path"]).exists()

    tampered_payload = json.loads(signature.decode("utf-8"))
    tampered_payload["signed_at"] = "2026-01-01T00:00:00Z"
    tampered_signature = json.dumps(tampered_payload).encode("utf-8")
    tampered = _transport(bundle, tampered_signature, _feed_bytes(bundle))
    downloaded = download_online_update(settings, transport=tampered)
    assert downloaded.status == "failed"
    assert downloaded.check.signature.status == "invalid"
    with pytest.raises(PcAgentError, match="cannot be staged"):
        stage_online_update(settings, transport=tampered)
    assert list_updates(settings) == ()

    current_feed = _feed_bytes(bundle, version=__version__)
    not_newer = _FakeTransport({_FEED_URL: current_feed})
    with pytest.raises(PcAgentError, match="same as the running version"):
        download_online_update(settings, transport=not_newer)
    assert not_newer.calls == [_FEED_URL]


def test_online_unsigned_bundle_requires_explicit_stage_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "target"
    _write_project(target, __version__, value=26)
    bundle, _signature, _public = _signed_bundle(tmp_path, monkeypatch, capsys)
    settings = _settings(target)
    feed = _feed_bytes(bundle, signature_url=None)
    transport = _FakeTransport({_FEED_URL: feed, _BUNDLE_URL: bundle})

    downloaded = download_online_update(settings, transport=transport)
    assert downloaded.status == "downloaded"
    assert downloaded.check.signature.status == "unsigned"
    with pytest.raises(PcAgentError, match="no verified signature"):
        stage_online_update(settings, transport=transport)

    result = stage_online_update(
        settings, transport=transport, allow_unsigned=True
    )
    assert result.staged.signature_status == "unsigned"
    assert result.staged.status == "staged"


def _install_fake_fetch(
    monkeypatch: pytest.MonkeyPatch, mapping: dict[str, bytes]
) -> list[str]:
    calls: list[str] = []

    def fake_fetch(validated, *, label, max_bytes, timeout_seconds):  # type: ignore[no-untyped-def]
        calls.append(validated.url)
        return mapping[validated.url], validated

    original_validate = online_module._validate_update_url

    def validate_without_dns(raw_url, *, label, expected_origin=None, resolve=True):  # type: ignore[no-untyped-def]
        return original_validate(
            raw_url,
            label=label,
            expected_origin=expected_origin,
            resolve=False,
        )

    monkeypatch.setattr(online_module, "_fetch_bytes", fake_fetch)
    monkeypatch.setattr(online_module, "_validate_update_url", validate_without_dns)
    return calls


def test_online_cli_and_gui_controller_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "target"
    _write_project(target, __version__, value=26)
    bundle, signature, public_key = _signed_bundle(tmp_path, monkeypatch, capsys)
    mapping = {
        _FEED_URL: _feed_bytes(bundle),
        _BUNDLE_URL: bundle,
        _SIGNATURE_URL: signature,
    }
    _install_fake_fetch(monkeypatch, mapping)

    monkeypatch.setenv("PC_AGENT_PROVIDER_ORDER", "mock")
    monkeypatch.setenv("PC_AGENT_CREDENTIAL_STORE", "env")
    monkeypatch.setenv("PC_AGENT_ONLINE_UPDATES", "1")
    monkeypatch.setenv("PC_AGENT_UPDATE_FEED_URL", _FEED_URL)
    monkeypatch.setenv("PC_AGENT_RELEASE_PUBLIC_KEY", public_key)
    monkeypatch.setenv("PC_AGENT_RELEASE_KEY_ID", "release-test")
    monkeypatch.setenv("PC_AGENT_REQUIRE_SIGNED_UPDATES", "1")
    root = ["--root", str(target)]

    assert main([*root, "update", "online-check", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["update_available"] is True
    assert payload["manifest"]["version"] == _FUTURE

    assert main([*root, "update", "online-download", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "downloaded"
    assert payload["check"]["signature"]["status"] == "verified"

    assert main([*root, "update", "online-stage", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["download"]["status"] == "staged"
    assert payload["staged"]["status"] == "staged"

    settings = _signed_settings(target, public_key)
    controller = GuiController(settings)
    check = controller.check_online_update()
    assert "not verified by check-only" in format_online_update_check(check)
    downloaded = controller.download_online_update()
    assert "verified" in format_downloaded_update(downloaded)
    staged = controller.stage_online_update()
    assert staged.staged.update_id in format_online_stage_result(staged)

    summary = controller.doctor_summary()
    text = summary.to_text()
    assert summary.version == __version__ == "0.41.0"
    assert "PHASE 39 Secure .env Key Cleanup" in text
    assert "online=yes" in text
    assert "feed_host=updates.example.com" in text
    assert "channel=stable" in text


class _LoopbackHandler(BaseHTTPRequestHandler):
    feed_body = b""
    bundle_body = b""
    signature_body = b""

    def do_GET(self) -> None:
        if self.path == "/stable.json":
            self.send_response(302)
            self.send_header("Location", "/feed/stable.json")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        mapping = {
            "/feed/stable.json": self.feed_body,
            f"/feed/{_BUNDLE_NAME}": self.bundle_body,
            f"/feed/{_SIGNATURE_NAME}": self.signature_body,
        }
        body = mapping.get(self.path)
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def test_online_loopback_http_redirect_and_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "target"
    _write_project(target, __version__, value=26)
    bundle, signature, public_key = _signed_bundle(tmp_path, monkeypatch, capsys)
    _LoopbackHandler.feed_body = _feed_bytes(bundle)
    _LoopbackHandler.bundle_body = bundle
    _LoopbackHandler.signature_body = signature

    server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        settings = _signed_settings(
            target,
            public_key,
            PC_AGENT_UPDATE_FEED_URL=f"http://127.0.0.1:{port}/stable.json",
        )
        check = check_online_update(settings)
        assert check.update_available is True
        result = stage_online_update(settings)
        assert result.staged.signature_status == "verified"
        assert (
            target / "src" / "pc_agent" / "module.py"
        ).read_text(encoding="utf-8") == "VALUE = 26\n"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_phase24_scope_docs_schema_and_release_candidates() -> None:
    project_root = Path(__file__).resolve().parents[1]
    candidates = {path.as_posix() for path in release_candidate_files(project_root)}
    tool_ids = {item.spec.id for item in build_default_registry().list_tools()}

    assert __version__ == "0.41.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(tool_ids) == 62
    assert not any(tool_id.startswith("update.") for tool_id in tool_ids)
    assert "src/pc_agent/online_updates.py" in candidates
    assert "docs/phase24.md" in candidates
    assert "tests/test_phase24_online_updates.py" in candidates

    phase_doc = (project_root / "docs" / "phase24.md").read_text(encoding="utf-8")
    assert "no background" in phase_doc.lower()
    assert "never auto-apply" in phase_doc.lower()
    assert "same origin" in phase_doc.lower()
    env_example = (project_root / ".env.example").read_text(encoding="utf-8")
    assert "PC_AGENT_ONLINE_UPDATES=0" in env_example
    assert "PC_AGENT_UPDATE_FEED_URL=" in env_example
