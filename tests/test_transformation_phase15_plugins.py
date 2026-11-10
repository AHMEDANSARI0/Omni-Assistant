"""Transformation Phase 15 acceptance: signed plugin/skill ecosystem.

These tests build real Ed25519-signed bundles, install them, and run plugin code
through the actual sandbox - no mocked verifier, no mocked subprocess.
"""

from __future__ import annotations

import base64
import json
import tempfile
import urllib.request
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from pc_agent import __version__
from pc_agent.api_server import LocalApiServer
from pc_agent.baseline import TRANSFORMATION_LABEL, TRANSFORMATION_PHASE
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.diagnostics import build_diagnostics, plugins_summary_line
from pc_agent.errors import PluginError
from pc_agent.plugins import CAPABILITIES, PluginService
from pc_agent.plugins.manifest import (
    bundle_checksum,
    manifest_bytes,
    sha256_hex,
    validate_manifest,
)
from pc_agent.plugins.sandbox import describe_sandbox
from pc_agent.release import audit_install
from pc_agent.storage.db import LATEST_SCHEMA_VERSION, Database

KEY_ID = "test-publisher"

WORDCOUNT_PLUGIN = """
def count_words(text):
    return {"words": len(text.split()), "chars": len(text)}
"""

SOCKET_PLUGIN = """
import socket

def probe(text):
    socket.create_connection(("127.0.0.1", 9), timeout=1)
    return "connected"
"""

# The escape target is resolved at run time so the plugin behaves the same on a
# POSIX host (``/tmp``) and on Windows (``%TEMP%``). It is always outside the
# declared capability roots, which is the property under test.
WRITE_PLUGIN = """
import tempfile
from pathlib import Path

def probe(text):
    target = Path(tempfile.gettempdir()) / "pc-agent-plugin-escape.txt"
    target.write_text("escaped", encoding="utf-8")
    return "wrote"
"""

SLEEP_PLUGIN = """
import time

def probe(text):
    time.sleep(30)
    return "done"
"""


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, base64.b64encode(public).decode("ascii")


#: Test-only registry so a bundle helper can sign with the key a settings object
#: trusts. ``Settings`` uses ``slots``, so it cannot carry the key itself.
_KEYS: dict[int, Ed25519PrivateKey] = {}


def _settings(tmp_path: Path, **overrides: str) -> Settings:
    private, public = _keypair()
    environ = {
        "PC_AGENT_ALLOW_PLUGINS": "1",
        "PC_AGENT_PLUGIN_TRUSTED_KEYS": json.dumps({KEY_ID: public}),
        "PC_AGENT_PLUGIN_TIMEOUT_SECONDS": "20",
    }
    environ.update(overrides)
    settings = Settings.from_env(tmp_path, environ=environ, load_dotenv=False)
    settings.ensure_directories()
    _KEYS[id(settings)] = private
    return settings


def _db(settings: Settings) -> Database:
    database = Database(settings.database_path)
    database.initialize()
    return database


def _bundle(
    tmp_path: Path,
    *,
    settings: Settings | None = None,
    plugin_id: str = "com.test.wordcount",
    version: str = "1.0.0",
    capabilities: tuple[str, ...] = ("read_workspace",),
    body: str = WORDCOUNT_PLUGIN,
    tools: list[dict] | None = None,
    skills: dict[str, str] | None = None,
    sign: bool = True,
    key_id: str = KEY_ID,
    private_key: Ed25519PrivateKey | None = None,
    manifest_overrides: dict | None = None,
    extra_files: dict[str, bytes] | None = None,
    omit_manifest: bool = False,
    declared_files: list[str] | None = None,
) -> Path:
    files: dict[str, bytes] = {"plugin.py": body.encode("utf-8")}
    for path, text in (skills or {}).items():
        files[path] = text.encode("utf-8")
    files.update(extra_files or {})
    digests = {path: sha256_hex(data) for path, data in files.items()}
    checksum = bundle_checksum(digests)
    manifest = {
        "schema": 1,
        "id": plugin_id,
        "name": "Word Count",
        "version": version,
        "description": "Counts words in a string.",
        "publisher": "Test Publisher",
        "license": "MIT",
        "entry": "plugin.py",
        "capabilities": list(capabilities),
        "tools": tools
        if tools is not None
        else [
            {
                "name": "count_words",
                "description": "Count the words in a string.",
                "arguments": {"text": "string"},
            }
        ],
        "skills": sorted((skills or {}).keys()),
        "files": sorted(declared_files if declared_files is not None else files),
        "checksum": checksum,
    }
    manifest.update(manifest_overrides or {})
    raw = manifest_bytes(manifest)
    key = private_key or _KEYS.get(id(settings))
    if key is None:
        raise AssertionError("no test signing key registered for these settings")
    signature = {
        "algorithm": "Ed25519",
        "key_id": key_id,
        "signature": base64.b64encode(key.sign(raw)).decode("ascii"),
        "payload_sha256": sha256_hex(raw),
        "checksum": checksum,
    }
    path = tmp_path / f"{plugin_id}-{version}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        if not omit_manifest:
            archive.writestr("manifest.json", raw)
        for name, data in files.items():
            archive.writestr(name, data)
        if sign:
            archive.writestr("manifest.sig.json", json.dumps(signature, sort_keys=True))
    return path


def _service(tmp_path: Path, **overrides: str) -> tuple[PluginService, Settings]:
    settings = _settings(tmp_path, **overrides)
    return PluginService(settings, _db(settings)), settings


def _installed(tmp_path: Path, **overrides: str) -> tuple[PluginService, Settings, str]:
    service, settings = _service(tmp_path, **overrides)
    bundle = _bundle(tmp_path, settings=settings)
    service.install(bundle)
    return service, settings, "com.test.wordcount"


# -- identity ---------------------------------------------------------------
def test_phase15_identity_is_wired_through_the_product(tmp_path: Path) -> None:
    from pc_agent.gui.app import NOTEBOOK_TAB_COUNT

    assert __version__ == "0.82.12"
    assert TRANSFORMATION_PHASE == 18
    assert TRANSFORMATION_LABEL == "TRANSFORMATION PHASE 18 Production Hardening"
    assert LATEST_SCHEMA_VERSION == 18
    assert NOTEBOOK_TAB_COUNT == 20
    settings = Settings.from_env(tmp_path, environ={}, load_dotenv=False)
    settings.ensure_directories()
    assert len(audit_install(settings).checks) == 34


def test_schema_v17_creates_the_plugin_tables(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = _db(settings)
    assert database.schema_version() == 18
    with database.connect() as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {"plugins", "plugin_versions", "plugin_runs", "plugin_state"} <= names


# -- manifest validation ----------------------------------------------------
@pytest.mark.parametrize(
    "override",
    [
        {"id": "Bad Id"},
        {"version": "1.0"},
        {"schema": 99},
        {"unexpected": True},
        {"capabilities": ["root_everything"]},
        {"entry": "../escape.py"},
        {"skills": ["notes.txt"]},
        {"tools": [{"name": "Bad Tool", "description": "x"}]},
    ],
)
def test_manifest_validation_refuses_bad_documents(override: dict) -> None:
    base = {
        "schema": 1,
        "id": "com.test.good",
        "name": "Good",
        "version": "1.0.0",
        "description": "fine",
        "publisher": "Pub",
        "license": "MIT",
        "entry": "plugin.py",
        "capabilities": [],
        "tools": [],
        "skills": [],
        "files": ["plugin.py"],
        "checksum": "",
    }
    base.update(override)
    with pytest.raises(PluginError):
        validate_manifest(base)


def test_manifest_capability_list_is_closed_and_small() -> None:
    good = {
        "id": "com.test.caps",
        "name": "Caps",
        "version": "2.0.1",
        "description": "caps",
        "publisher": "Pub",
        "entry": "plugin.py",
        "capabilities": list(CAPABILITIES),
        "tools": [],
        "skills": [],
        "files": ["plugin.py"],
    }
    manifest = validate_manifest(good)
    assert manifest.capabilities == CAPABILITIES
    assert set(manifest.denied_capabilities) == {"network", "subprocess"}


# -- gating -----------------------------------------------------------------
def test_plugin_service_is_off_by_default(tmp_path: Path) -> None:
    settings = Settings.from_env(tmp_path, environ={}, load_dotenv=False)
    settings.ensure_directories()
    service = PluginService(settings, _db(settings))
    status = service.status()
    assert status["enabled"] is False
    assert "PC_AGENT_ALLOW_PLUGINS" in status["detail"]
    with pytest.raises(PluginError) as excinfo:
        service.install(tmp_path / "anything.zip")
    assert "PC_AGENT_ALLOW_PLUGINS" in str(excinfo.value)


# -- install ----------------------------------------------------------------
def test_signed_bundle_installs_verifies_and_enables(tmp_path: Path) -> None:
    service, _settings_obj, plugin_id = _installed(tmp_path)
    entry = service.show(plugin_id)
    assert entry["status"] == "installed"
    assert entry["signature_status"] == "verified"
    assert entry["signature_key"] == KEY_ID
    assert entry["capabilities"] == ["read_workspace"]
    assert Path(entry["install_dir"]).is_dir()
    # Nothing runs until the user enables it explicitly.
    with pytest.raises(PluginError) as excinfo:
        service.run_tool(plugin_id, "count_words", values={"text": "a b"}, confirm=plugin_id)
    assert "enable it" in str(excinfo.value)
    assert service.set_enabled(plugin_id, enabled=True)["status"] == "enabled"


def test_unsigned_bundle_is_refused_unless_explicitly_allowed(tmp_path: Path) -> None:
    service, settings = _service(tmp_path)
    unsigned = _bundle(tmp_path, settings=settings, sign=False)
    with pytest.raises(PluginError) as excinfo:
        service.install(unsigned)
    assert "unsigned" in str(excinfo.value)

    permissive, permissive_settings = _service(
        tmp_path / "permissive", PC_AGENT_PLUGIN_ALLOW_UNSIGNED="1"
    )
    bundle = _bundle(tmp_path / "permissive", settings=permissive_settings, sign=False)
    entry = permissive.install(bundle)
    assert entry["signature_status"] == "unsigned"


def test_tampered_content_is_refused(tmp_path: Path) -> None:
    service, settings = _service(tmp_path)
    bundle = _bundle(tmp_path, settings=settings)
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(bundle) as source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "plugin.py":
                data = data + b"\n# injected\n"
            target.writestr(info, data)
    with pytest.raises(PluginError) as excinfo:
        service.install(tampered)
    assert "checksum" in str(excinfo.value)


def test_signature_from_an_untrusted_key_is_refused(tmp_path: Path) -> None:
    service, settings = _service(tmp_path)
    rogue, _public = _keypair()
    bundle = _bundle(
        tmp_path, settings=settings, private_key=rogue, key_id="rogue-publisher"
    )
    with pytest.raises(PluginError) as excinfo:
        service.install(bundle)
    assert "not in the trusted key set" in str(excinfo.value)


def test_zip_traversal_and_symlink_entries_are_refused(tmp_path: Path) -> None:
    service, settings = _service(tmp_path)
    good = _bundle(tmp_path, settings=settings)
    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(good) as source, zipfile.ZipFile(traversal, "w") as target:
        for info in source.infolist():
            target.writestr(info, source.read(info.filename))
        target.writestr("../evil.py", b"print('pwned')")
    with pytest.raises(PluginError) as excinfo:
        service.install(traversal)
    assert "relative path" in str(excinfo.value)

    link = tmp_path / "link.zip"
    with zipfile.ZipFile(good) as source, zipfile.ZipFile(link, "w") as target:
        for info in source.infolist():
            target.writestr(info, source.read(info.filename))
        info = zipfile.ZipInfo("link.py")
        info.external_attr = (0o120777 << 16)
        target.writestr(info, b"target")
    with pytest.raises(PluginError) as excinfo:
        service.install(link)
    assert "symlink" in str(excinfo.value)


def test_bundle_limits_are_enforced(tmp_path: Path) -> None:
    service, settings = _service(tmp_path, PC_AGENT_PLUGIN_MAX_FILES="2")
    bundle = _bundle(
        tmp_path,
        settings=settings,
        extra_files={"notes.md": b"hello"},
        skills={"skills/howto.md": "# how\n"},
    )
    with pytest.raises(PluginError) as excinfo:
        service.install(bundle)
    assert "more than the limit" in str(excinfo.value)


def test_network_capability_is_refused_at_install(tmp_path: Path) -> None:
    service, settings = _service(tmp_path)
    bundle = _bundle(tmp_path, settings=settings, capabilities=("network",))
    with pytest.raises(PluginError) as excinfo:
        service.install(bundle)
    assert "cannot grant" in str(excinfo.value)


def test_missing_manifest_is_refused(tmp_path: Path) -> None:
    service, settings = _service(tmp_path)
    bundle = _bundle(tmp_path, settings=settings, omit_manifest=True)
    with pytest.raises(PluginError) as excinfo:
        service.install(bundle)
    assert "manifest.json" in str(excinfo.value)


# -- lifecycle --------------------------------------------------------------
def test_lifecycle_disable_rollback_and_uninstall(tmp_path: Path) -> None:
    service, settings = _service(tmp_path)
    first = _bundle(tmp_path, settings=settings, version="1.0.0")
    service.install(first)
    service.set_enabled("com.test.wordcount", enabled=True)
    second = _bundle(
        tmp_path, settings=settings, version="1.2.0", body=WORDCOUNT_PLUGIN + "\nVERSION = 2\n"
    )
    service.install(second)
    entry = service.show("com.test.wordcount")
    assert entry["version"] == "1.2.0"
    assert [item["version"] for item in entry["versions"]][:2] == ["1.2.0", "1.0.0"]

    service.set_enabled("com.test.wordcount", enabled=False)
    assert service.show("com.test.wordcount")["status"] == "disabled"

    rolled = service.rollback("com.test.wordcount")
    assert rolled["version"] == "1.0.0"
    assert Path(rolled["install_dir"]).is_dir()

    removed = service.uninstall("com.test.wordcount")
    assert removed["total"] == 0
    assert not Path(entry["install_dir"]).exists()


def test_rollback_without_history_is_refused(tmp_path: Path) -> None:
    service, _settings_obj, plugin_id = _installed(tmp_path)
    with pytest.raises(PluginError) as excinfo:
        service.rollback(plugin_id)
    assert "No earlier version" in str(excinfo.value)


# -- execution --------------------------------------------------------------
def test_tool_runs_in_the_sandbox_and_returns_a_real_result(tmp_path: Path) -> None:
    service, _settings_obj, plugin_id = _installed(tmp_path)
    service.set_enabled(plugin_id, enabled=True)
    payload = service.run_tool(
        plugin_id, "count_words", values={"text": "one two three"}, confirm=plugin_id
    )
    assert payload["status"] == "ok"
    assert payload["result"] == {"words": 3, "chars": 13}
    assert payload["sandbox"]["mechanism"] == describe_sandbox()["mechanism"]
    assert payload["sandbox"]["limits"]["memory_limit_mb"] == 512
    assert service.runs()["total"] == 1
    assert payload["ledger"]["status"] == "ok"


def test_run_requires_confirm_token_and_declared_tool(tmp_path: Path) -> None:
    service, _settings_obj, plugin_id = _installed(tmp_path)
    service.set_enabled(plugin_id, enabled=True)
    with pytest.raises(PluginError) as excinfo:
        service.run_tool(plugin_id, "count_words", values={"text": "a"}, confirm=None)
    assert "confirm" in str(excinfo.value)
    with pytest.raises(PluginError) as excinfo:
        service.run_tool(plugin_id, "delete_everything", values={}, confirm=plugin_id)
    assert "does not declare" in str(excinfo.value)


def test_argument_types_are_enforced_inside_the_worker(tmp_path: Path) -> None:
    service, _settings_obj, plugin_id = _installed(tmp_path)
    service.set_enabled(plugin_id, enabled=True)
    payload = service.run_tool(
        plugin_id, "count_words", values={"text": 42}, confirm=plugin_id
    )
    assert payload["status"] == "error"
    assert "must be a string" in payload["detail"]


def test_sandbox_times_out_on_a_slow_plugin(tmp_path: Path) -> None:
    service, settings = _service(tmp_path, PC_AGENT_PLUGIN_TIMEOUT_SECONDS="1")
    bundle = _bundle(
        tmp_path,
        settings=settings,
        body=SLEEP_PLUGIN,
        tools=[
            {"name": "probe", "description": "sleep", "arguments": {"text": "string"}}
        ],
    )
    service.install(bundle)
    service.set_enabled("com.test.wordcount", enabled=True)
    payload = service.run_tool(
        "com.test.wordcount", "probe", values={"text": "x"}, confirm="com.test.wordcount"
    )
    assert payload["status"] == "timeout"
    assert "timeout" in payload["detail"]
    assert payload["duration_ms"] < 20_000


def test_plugin_cannot_open_a_socket(tmp_path: Path) -> None:
    service, settings = _service(tmp_path)
    bundle = _bundle(
        tmp_path,
        settings=settings,
        body=SOCKET_PLUGIN,
        tools=[
            {"name": "probe", "description": "connect", "arguments": {"text": "string"}}
        ],
    )
    service.install(bundle)
    service.set_enabled("com.test.wordcount", enabled=True)
    payload = service.run_tool(
        "com.test.wordcount", "probe", values={"text": "x"}, confirm="com.test.wordcount"
    )
    assert payload["status"] == "error"
    assert "network" in payload["detail"]


def test_plugin_write_outside_its_roots_is_refused(tmp_path: Path) -> None:
    service, settings = _service(tmp_path)
    escape = Path(tempfile.gettempdir()) / "pc-agent-plugin-escape.txt"
    escape.unlink(missing_ok=True)
    bundle = _bundle(
        tmp_path,
        settings=settings,
        body=WRITE_PLUGIN,
        tools=[
            {"name": "probe", "description": "write", "arguments": {"text": "string"}}
        ],
    )
    service.install(bundle)
    service.set_enabled("com.test.wordcount", enabled=True)
    payload = service.run_tool(
        "com.test.wordcount", "probe", values={"text": "x"}, confirm="com.test.wordcount"
    )
    assert payload["status"] == "error"
    assert "outside the declared capability roots" in payload["detail"]
    assert not escape.exists()


def test_skills_are_text_only_and_bounded(tmp_path: Path) -> None:
    service, settings = _service(tmp_path)
    bundle = _bundle(
        tmp_path, settings=settings, skills={"skills/howto.md": "# How to count\nBe careful."}
    )
    entry = service.install(bundle)
    assert entry["skills"] == ["skills/howto.md"]
    skills = service.skills("com.test.wordcount")
    assert skills["total"] == 1
    assert "How to count" in skills["skills"][0]["text"]


# -- surfaces ---------------------------------------------------------------
def test_cli_plugin_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    bundle = _bundle(tmp_path, settings=settings)
    monkeypatch.setenv("PC_AGENT_ROOT", str(tmp_path))
    monkeypatch.setenv("PC_AGENT_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("PC_AGENT_DATABASE_PATH", str(settings.database_path))
    monkeypatch.setenv("PC_AGENT_PLUGIN_TRUSTED_KEYS", settings.plugin_trusted_keys)
    monkeypatch.setenv("PC_AGENT_ALLOW_PLUGINS", "1")

    assert main(["plugins", "status"]) == 0
    assert "plugins=on" in capsys.readouterr().out
    assert main(["plugins", "verify", str(bundle)]) == 0
    assert "signature=verified" in capsys.readouterr().out
    assert main(["plugins", "install", str(bundle)]) == 0
    assert "installed Word Count 1.0.0" in capsys.readouterr().out
    assert main(["plugins", "enable", "com.test.wordcount"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "plugins",
                "run",
                "com.test.wordcount",
                "count_words",
                "--arg",
                'text="one two"',
                "--confirm",
                "com.test.wordcount",
            ]
        )
        == 0
    )
    assert "words" in capsys.readouterr().out
    assert main(["plugins", "list"]) == 0
    assert "com.test.wordcount" in capsys.readouterr().out
    assert main(["plugins", "runs"]) == 0
    assert "count_words" in capsys.readouterr().out
    assert main(["plugins", "remove", "com.test.wordcount"]) == 0
    assert "removed" in capsys.readouterr().out


def test_cli_refuses_a_bad_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "nope.zip"
    # Gate first: with plugins disabled nothing on disk is even inspected.
    assert main(["--root", str(tmp_path), "plugins", "install", str(missing)]) == 1
    assert "PC_AGENT_ALLOW_PLUGINS" in capsys.readouterr().err

    monkeypatch.setenv("PC_AGENT_ALLOW_PLUGINS", "1")
    assert main(["--root", str(tmp_path), "plugins", "install", str(missing)]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_api_routes_for_plugins(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    bundle = _bundle(tmp_path, settings=settings)
    server = LocalApiServer(replace(settings, api_port=0, scheduler_interval_seconds=0))
    server.start()
    try:
        _host, port = server.address
        headers = {"Authorization": f"Bearer {server.token}"}
        base = f"http://127.0.0.1:{port}"

        def call(method: str, path: str, payload: dict | None = None) -> dict:
            data = json.dumps(payload).encode("utf-8") if payload is not None else None
            request = urllib.request.Request(
                base + path, data=data, method=method, headers={**headers, "Content-Type": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))

        status = call("GET", "/v1/plugins/status")["data"]
        assert status["signature_required"] is True
        installed = call("POST", "/v1/plugins/install", {"bundle": str(bundle)})["data"]
        assert installed["signature_status"] == "verified"
        assert call("POST", "/v1/plugins/com.test.wordcount/enabled", {"enabled": True})["data"][
            "status"
        ] == "enabled"
        run = call(
            "POST",
            "/v1/plugins/com.test.wordcount/run",
            {"tool": "count_words", "values": {"text": "a b"}, "confirm": "com.test.wordcount"},
        )["data"]
        assert run["status"] == "ok" and run["result"]["words"] == 2
        assert call("GET", "/v1/plugins")["data"]["total"] == 1
        assert call("GET", "/v1/plugins/runs")["data"]["total"] == 1
        assert call("DELETE", "/v1/plugins/com.test.wordcount")["data"]["total"] == 0
    finally:
        server.stop()


def test_gui_controller_and_web_panel_are_wired() -> None:
    from pc_agent.gui.controller import GuiController
    from pc_agent.web_ui import INDEX_HTML

    for method in (
        "plugins_status_text",
        "plugins_list_text",
        "plugin_verify",
        "plugin_install",
        "plugin_run",
        "plugin_set_enabled",
        "plugin_uninstall",
        "plugin_rollback",
        "plugin_skills",
        "plugin_runs_text",
    ):
        assert hasattr(GuiController, method), method
    html = INDEX_HTML
    for marker in ("Plugins", "plugin-verify", "plugin-install", "plugin-run", "plugin-out"):
        assert marker in html, marker


def test_diagnostics_and_release_cover_plugins(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    report = build_diagnostics(settings, _db(settings))
    assert report["plugins"]["enabled"] is True
    assert report["plugins"]["sandbox"]["mechanism"]
    line = plugins_summary_line(settings, _db(settings))
    assert "sandbox=" in line
    audit = audit_install(settings)
    assert len(audit.checks) == 34
    check = next(item for item in audit.checks if item.name == "plugin_ecosystem")
    assert check.status in {"pass", "warn"}
    assert "schema v18" in check.detail
    assert "sandbox" in check.detail

    disabled = Settings.from_env(tmp_path / "off", environ={}, load_dotenv=False)
    disabled.ensure_directories()
    off_check = next(
        item for item in audit_install(disabled).checks if item.name == "plugin_ecosystem"
    )
    assert off_check.status == "pass"
    assert "PC_AGENT_ALLOW_PLUGINS" in off_check.detail


def test_ledgers_stay_bounded(tmp_path: Path) -> None:
    service, settings, plugin_id = _installed(tmp_path)
    service.set_enabled(plugin_id, enabled=True)
    store = service.store()
    for index in range(510):
        store.record_run(
            plugin_id=plugin_id, tool="count_words", status="ok", detail=str(index)
        )
    assert store.count()["runs"] == 500
    for minor in range(2, 9):
        bundle = _bundle(tmp_path, settings=settings, version=f"1.0.{minor}")
        service.install(bundle)
    assert len(store.versions(plugin_id, limit=50)) == 5


def test_doctor_reports_plugin_posture(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = _settings(tmp_path)
    assert main(["--root", str(tmp_path), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "Plugins:" in out
    assert settings.allow_plugins is True
