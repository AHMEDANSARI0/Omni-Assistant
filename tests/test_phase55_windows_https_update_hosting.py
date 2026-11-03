"""Phase 55: Windows-first operational HTTPS update-feed hosting."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import shutil
import ssl
import threading
import tomllib
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from pc_agent import __version__, release_admin
from pc_agent import cli as cli_module
from pc_agent import release as release_module
from pc_agent.cli import build_parser, main
from pc_agent.config import Settings
from pc_agent.errors import PcAgentError
from pc_agent.release import build_release_bundle, release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry
from pc_agent.update_feed_hosting import (
    UPDATE_FEED_HOSTING_MANIFEST,
    HostingHttpResponse,
    build_update_feed_hosting_package,
    create_update_feed_hosting_server,
    list_hosting_profiles,
    run_update_feed_hosting_live_verification,
    verify_update_feed_hosting_package,
    verify_update_feed_hosting_report,
)
from pc_agent.update_feed_publisher import (
    UPDATE_FEED_PUBLISHER_MANIFEST,
    build_update_feed_package,
)
from pc_agent.updates import b64url_encode

ROOT = Path(__file__).resolve().parents[1]
_FUTURE = "0.61.0"
_BASE_URL = "https://updates.example.com/self-healing-pc-agent"
_PUBLISHED_AT = "2026-11-02T12:00:00Z"
_KEY_ID = "release-test"


def _write_project(root: Path, version: str) -> None:
    (root / "src" / "pc_agent").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "scripts").mkdir()
    (root / "tests").mkdir()
    (root / "workspace").mkdir()
    (root / "data").mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "self-healing-pc-agent"\n'
        f'version = "{version}"\n',
        encoding="utf-8",
    )
    (root / "src" / "pc_agent" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    (root / "src" / "pc_agent" / "module.py").write_text(
        "VALUE = 55\n", encoding="utf-8"
    )
    (root / "README.md").write_text("Hosting test.\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("Changes.\n", encoding="utf-8")
    (root / ".env.example").write_text("EXAMPLE=1\n", encoding="utf-8")
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")
    (root / "docs" / "phase55.md").write_text("Phase 55.\n", encoding="utf-8")
    (root / "scripts" / "install_windows.ps1").write_text(
        "Write-Host ok\n", encoding="utf-8"
    )
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (root / "workspace" / ".gitkeep").write_text("", encoding="utf-8")


def _key_value(path: Path, name: str) -> str:
    return next(
        line.split("=", 1)[1]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"{name}=")
    )


def _feed_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[Path, Path, Path, str]:
    source = tmp_path / "future-source"
    _write_project(source, _FUTURE)
    bundle = tmp_path / "input-bundle.zip"
    monkeypatch.setattr(release_module, "__version__", _FUTURE)
    build_release_bundle(source, bundle)

    keys = tmp_path / "vendor-keys"
    assert (
        release_admin.main(
            ["keygen", "--out-dir", str(keys), "--key-id", _KEY_ID]
        )
        == 0
    )
    capsys.readouterr()
    private_path = keys / "release-private-key.txt"
    public_path = keys / "release-public-key.txt"
    public_key = _key_value(public_path, "PC_AGENT_RELEASE_PUBLIC_KEY")
    output = tmp_path / "feed-package"
    build_update_feed_package(
        bundle,
        output,
        seed=release_admin._read_private_seed(private_path),
        key_id=_KEY_ID,
        base_url=_BASE_URL,
        published_at=_PUBLISHED_AT,
    )
    return output, private_path, public_path, public_key


def _hosting_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    profile: str = "windows-https-task",
    name: str = "hosting-package",
) -> tuple[Path, Path, Path, str]:
    feed, private_path, public_path, public_key = _feed_package(
        tmp_path, monkeypatch, capsys
    )
    output = tmp_path / name
    build_update_feed_hosting_package(
        feed,
        output,
        public_key=public_key,
        key_id=_KEY_ID,
        profile_id=profile,
        deployment_root=r"D:\UpdateFeedHost",
    )
    return output, private_path, public_path, public_key


def _settings(tmp_path: Path, public_key: str) -> Settings:
    settings = Settings.from_env(
        tmp_path / "customer",
        environ={
            "PC_AGENT_PROVIDER_ORDER": "mock",
            "PC_AGENT_CREDENTIAL_STORE": "env",
            "PC_AGENT_ONLINE_UPDATES": "0",
            "PC_AGENT_UPDATE_FEED_URL": f"{_BASE_URL}/stable.json",
            "PC_AGENT_UPDATE_CHANNEL": "stable",
            "PC_AGENT_RELEASE_PUBLIC_KEY": public_key,
            "PC_AGENT_RELEASE_KEY_ID": _KEY_ID,
            "PC_AGENT_REQUIRE_SIGNED_UPDATES": "1",
        },
        load_dotenv=False,
    )
    settings.ensure_directories()
    return settings


def _transport(
    feed: Path,
    *,
    feed_cache: str = "public, max-age=60, must-revalidate",
    bundle_cache: str = "public, max-age=31536000, immutable",
):
    def fetch(url: str, _max_bytes: int, _timeout: float) -> HostingHttpResponse:
        assert url.startswith(_BASE_URL + "/")
        relative = url.removeprefix(_BASE_URL + "/")
        body = (feed / relative).read_bytes()
        if relative == "stable.json":
            content_type = "application/json"
            cache = feed_cache
        elif relative.endswith(".zip"):
            content_type = "application/zip"
            cache = bundle_cache
        else:
            content_type = "application/json"
            cache = bundle_cache
        return HostingHttpResponse(
            status=200,
            headers={
                "Cache-Control": cache,
                "Content-Type": content_type,
                "ETag": f'"{hashlib.sha256(body).hexdigest()}"',
            },
            body=body,
            final_url=url,
            tls_verified=True,
        )

    return fetch


def test_phase55_builds_windows_task_and_iis_hosting_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    feed, private_path, _public_path, public_key = _feed_package(
        tmp_path, monkeypatch, capsys
    )

    task_output = tmp_path / "task-hosting"
    task = build_update_feed_hosting_package(
        feed,
        task_output,
        public_key=public_key,
        key_id=_KEY_ID,
        deployment_root=r"D:\UpdateFeedHost",
    )
    assert task.profile == "windows-https-task"
    assert task.platform == "windows"
    assert task.host_name == "updates.example.com"
    assert task.port == 443
    assert task.url_prefix == "/self-healing-pc-agent"
    assert task.file_count == 12
    assert task.to_dict()["certificate_included"] is False
    assert task.to_dict()["live_verified"] is False
    inventory = {
        path.relative_to(task_output).as_posix()
        for path in task_output.rglob("*")
        if path.is_file()
    }
    assert inventory == {
        "README-WINDOWS.txt",
        "hosting-manifest.json",
        "site/HOSTING.txt",
        "site/customer-config.env.example",
        "site/publisher-manifest.json",
        f"site/releases/release-{_FUTURE}.zip",
        f"site/releases/release-{_FUTURE}.zip.sig.json",
        "site/stable.json",
        "windows/Register-UpdateFeedHost.ps1",
        "windows/Start-UpdateFeedHost.ps1",
        "windows/Unregister-UpdateFeedHost.ps1",
        "windows/Verify-LiveUpdateFeed.ps1",
    }
    register = (
        task_output / "windows" / "Register-UpdateFeedHost.ps1"
    ).read_text(encoding="utf-8")
    assert "New-ScheduledTaskAction" in register
    assert f"Feed URL: {_BASE_URL}" in register
    assert "__FEED_URL__" not in register

    iis_output = tmp_path / "iis-hosting"
    iis = build_update_feed_hosting_package(
        feed,
        iis_output,
        public_key=public_key,
        key_id=_KEY_ID,
        profile_id="windows-iis",
        deployment_name="UpdatesSite",
        deployment_root=r"D:\inetpub\Updates",
    )
    assert iis.profile == "windows-iis"
    assert iis.file_count == 13
    root_config = (iis_output / "windows" / "iis" / "web.config").read_text(
        encoding="utf-8"
    )
    deploy = (
        iis_output / "windows" / "Deploy-UpdateFeedIis.ps1"
    ).read_text(encoding="utf-8")
    rollback = (
        iis_output / "windows" / "Rollback-UpdateFeedIis.ps1"
    ).read_text(encoding="utf-8")
    assert 'directoryBrowse enabled="false"' in root_config
    assert "Strict-Transport-Security" in root_config
    assert "SslBindings" in deploy
    assert "hosting-state.json" in deploy
    assert "previous_release" in rollback

    verified = verify_update_feed_hosting_package(
        task_output,
        public_key=public_key,
        key_id=_KEY_ID,
        expected_profile="windows-https-task",
    )
    assert verified.manifest_sha256 == task.manifest_sha256

    private_value = _key_value(private_path, "PC_AGENT_RELEASE_PRIVATE_KEY")
    seed = release_admin._read_private_seed(private_path)
    for output in (task_output, iis_output):
        for path in output.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                assert private_value.encode("ascii") not in data
                assert b64url_encode(seed).encode("ascii") not in data
                assert seed not in data

    profiles = {profile.profile_id: profile for profile in list_hosting_profiles()}
    assert profiles["windows-https-task"].status == "implemented"
    assert profiles["windows-iis"].status == "implemented"
    assert profiles["linux-nginx"].status == "planned"
    assert profiles["macos-caddy"].status == "planned"
    with pytest.raises(PcAgentError, match="Windows-first"):
        build_update_feed_hosting_package(
            feed,
            tmp_path / "linux-hosting",
            public_key=public_key,
            key_id=_KEY_ID,
            profile_id="linux-nginx",
        )


def test_phase55_hosting_package_verification_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output, _private, _public_path, public_key = _hosting_package(
        tmp_path, monkeypatch, capsys
    )

    tampered = tmp_path / "tampered"
    shutil.copytree(output, tampered)
    script = tampered / "windows" / "Start-UpdateFeedHost.ps1"
    script.write_bytes(script.read_bytes() + b"\n# tamper\n")
    with pytest.raises(PcAgentError, match="failed verification"):
        verify_update_feed_hosting_package(
            tampered, public_key=public_key, key_id=_KEY_ID
        )

    rehashed = tmp_path / "rehashed"
    shutil.copytree(output, rehashed)
    script = rehashed / "windows" / "Start-UpdateFeedHost.ps1"
    script.write_bytes(script.read_bytes() + b"\n# rehashed tamper\n")
    manifest_path = rehashed / UPDATE_FEED_HOSTING_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for record in manifest["files"]:
        data = (rehashed / record["path"]).read_bytes()
        record["size"] = len(data)
        record["sha256"] = hashlib.sha256(data).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(PcAgentError, match="generated Windows files"):
        verify_update_feed_hosting_package(
            rehashed, public_key=public_key, key_id=_KEY_ID
        )

    extra = tmp_path / "extra"
    shutil.copytree(output, extra)
    (extra / "unexpected.txt").write_text("extra\n", encoding="utf-8")
    with pytest.raises(PcAgentError, match="inventory"):
        verify_update_feed_hosting_package(
            extra, public_key=public_key, key_id=_KEY_ID
        )

    with pytest.raises(PcAgentError, match="already exists"):
        build_update_feed_hosting_package(
            output / "site",
            output,
            public_key=public_key,
            key_id=_KEY_ID,
        )


def test_phase55_builtin_static_host_serves_only_verified_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output, _private, _public_path, public_key = _hosting_package(
        tmp_path, monkeypatch, capsys
    )
    server = create_update_feed_hosting_server(
        output,
        public_key=public_key,
        key_id=_KEY_ID,
        bind_host="127.0.0.1",
        port=0,
        allow_http_for_tests=True,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}/self-healing-pc-agent"
    try:
        with urllib.request.urlopen(f"{base}/healthz", timeout=5) as response:
            assert response.status == 200
            assert json.loads(response.read())["version"] == _FUTURE
            assert response.headers["Cache-Control"] == "no-store"

        with urllib.request.urlopen(f"{base}/stable.json", timeout=5) as response:
            body = response.read()
            assert body == (output / "site" / "stable.json").read_bytes()
            assert response.headers["Content-Type"] == "application/json"
            assert response.headers["Cache-Control"] == (
                "public, max-age=60, must-revalidate"
            )
            etag = response.headers["ETag"]

        request = urllib.request.Request(
            f"{base}/stable.json", headers={"If-None-Match": etag}
        )
        with pytest.raises(urllib.error.HTTPError) as not_modified:
            urllib.request.urlopen(request, timeout=5)
        assert not_modified.value.code == 304

        bundle_path = output / "site" / "releases" / f"release-{_FUTURE}.zip"
        request = urllib.request.Request(
            f"{base}/releases/release-{_FUTURE}.zip",
            headers={"Range": "bytes=0-3"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 206
            assert response.read() == bundle_path.read_bytes()[:4]
            assert response.headers["Cache-Control"] == (
                "public, max-age=31536000, immutable"
            )

        for bad_path in ("/stable.json", f"{base}/../hosting-manifest.json"):
            url = bad_path if bad_path.startswith("http") else (
                f"http://127.0.0.1:{port}{bad_path}"
            )
            with pytest.raises(urllib.error.HTTPError) as failure:
                urllib.request.urlopen(url, timeout=5)
            assert failure.value.code in {400, 404}

        post = urllib.request.Request(
            f"{base}/stable.json", data=b"nope", method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as failure:
            urllib.request.urlopen(post, timeout=5)
        assert failure.value.code == 405
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    tls_key = ec.generate_private_key(ec.SECP256R1())
    tls_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]
    )
    now = datetime.now(UTC)
    tls_cert = (
        x509.CertificateBuilder()
        .subject_name(tls_name)
        .issuer_name(tls_name)
        .public_key(tls_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        )
        .sign(tls_key, hashes.SHA256())
    )
    cert_path = tmp_path / "localhost-cert.pem"
    key_path = tmp_path / "localhost-key.pem"
    cert_path.write_bytes(tls_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        tls_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
    tls_context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    tls_server = create_update_feed_hosting_server(
        output,
        public_key=public_key,
        key_id=_KEY_ID,
        bind_host="127.0.0.1",
        port=0,
        tls_context=tls_context,
    )
    tls_thread = threading.Thread(target=tls_server.serve_forever, daemon=True)
    tls_thread.start()
    try:
        client_context = ssl.create_default_context(cafile=str(cert_path))
        tls_port = tls_server.server_address[1]
        with urllib.request.urlopen(
            f"https://127.0.0.1:{tls_port}/self-healing-pc-agent/healthz",
            timeout=5,
            context=client_context,
        ) as response:
            assert response.status == 200
            assert json.loads(response.read())["status"] == "ok"
            assert response.headers["Strict-Transport-Security"] == (
                "max-age=31536000"
            )
    finally:
        tls_server.shutdown()
        tls_server.server_close()
        tls_thread.join(timeout=5)


def test_phase55_live_hosting_verification_and_offline_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    feed, private_path, _public_path, public_key = _feed_package(
        tmp_path, monkeypatch, capsys
    )
    settings = _settings(tmp_path, public_key)
    report_path = tmp_path / "live-hosting.json"

    report = run_update_feed_hosting_live_verification(
        settings,
        report_path,
        expected_version=_FUTURE,
        transport=_transport(feed),
        client_platform="windows",
    )

    assert settings.online_updates_enabled is False
    assert report.status == "passed"
    assert all(check.status == "passed" for check in report.checks)
    assert report.feed_manifest is not None
    assert report.feed_manifest["version"] == _FUTURE
    assert report.resources["bundle"] is not None
    assert report.resources["bundle"]["sha256"] == report.feed_manifest[
        "bundle_sha256"
    ]

    raw = report_path.read_text(encoding="utf-8")
    private_value = _key_value(private_path, "PC_AGENT_RELEASE_PRIVATE_KEY")
    assert private_value not in raw
    assert public_key not in raw
    assert "phase55" not in raw.lower()
    payload = json.loads(raw)
    assert payload["tls"] == {
        "certificate_verified": True,
        "scheme": "https",
    }

    verification = verify_update_feed_hosting_report(report_path)
    assert verification.feed_version == _FUTURE
    assert verification.profile == "windows-https-task"
    assert verification.report_sha256 == payload["report_sha256"]

    payload["hosting"]["profile"] = "windows-iis"
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PcAgentError, match="hash does not match"):
        verify_update_feed_hosting_report(report_path)


def test_phase55_live_hosting_failures_are_reported_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    feed, _private, _public_path, public_key = _feed_package(
        tmp_path, monkeypatch, capsys
    )
    settings = _settings(tmp_path, public_key)

    stale_cache = run_update_feed_hosting_live_verification(
        settings,
        tmp_path / "stale-cache.json",
        transport=_transport(feed, feed_cache="public, max-age=86400"),
        client_platform="windows",
    )
    assert stale_cache.status == "failed"
    assert stale_cache.check("feed_cache_policy").status == "failed"
    with pytest.raises(PcAgentError, match="does not record a passed"):
        verify_update_feed_hosting_report(tmp_path / "stale-cache.json")

    wrong_key = run_update_feed_hosting_live_verification(
        settings,
        tmp_path / "wrong-key.json",
        public_key=b"\x02" * 32,
        transport=_transport(feed),
        client_platform="windows",
    )
    assert wrong_key.status == "failed"
    assert wrong_key.check("bundle_signature_verified").status == "failed"

    http_report = run_update_feed_hosting_live_verification(
        settings,
        tmp_path / "http.json",
        feed_url="http://127.0.0.1:8765/stable.json",
        transport=_transport(feed),
        client_platform="windows",
    )
    assert http_report.status == "failed"
    assert http_report.check("https_feed_url").status == "failed"
    assert http_report.check("feed_fetch").status == "skipped"


def test_phase55_release_admin_and_main_cli_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    feed, private_path, public_path, public_key = _feed_package(
        tmp_path, monkeypatch, capsys
    )
    hosting = tmp_path / "admin-hosting"

    assert (
        release_admin.main(
            [
                "build-hosting",
                str(feed),
                "--public-key-file",
                str(public_path),
                "--key-id",
                _KEY_ID,
                "--output",
                str(hosting),
                "--json",
            ]
        )
        == 0
    )
    build_output = capsys.readouterr().out
    assert json.loads(build_output)["profile"] == "windows-https-task"
    assert _key_value(private_path, "PC_AGENT_RELEASE_PRIVATE_KEY") not in build_output

    assert (
        release_admin.main(
            [
                "verify-hosting",
                str(hosting),
                "--public-key-file",
                str(public_path),
                "--key-id",
                _KEY_ID,
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["version"] == _FUTURE

    assert release_admin.main(["hosting-profiles", "--json"]) == 0
    profiles = json.loads(capsys.readouterr().out)
    assert {item["id"]: item["status"] for item in profiles}["linux-caddy"] == "planned"

    parser = build_parser()
    serve_help = next(
        action
        for action in parser._subparsers._group_actions[0].choices["update"]._actions
        if getattr(action, "dest", None) == "update_command"
    )
    assert "hosting-check" in serve_help.choices
    assert "hosting-check-verify" in serve_help.choices

    real_run = cli_module.run_update_feed_hosting_live_verification

    def fake_cli_run(settings: Settings, report_path: Path | None, **kwargs: Any):
        kwargs.pop("public_key", None)
        return real_run(
            settings,
            report_path,
            public_key=public_key,
            transport=_transport(feed),
            client_platform="windows",
            **kwargs,
        )

    monkeypatch.setattr(
        cli_module,
        "run_update_feed_hosting_live_verification",
        fake_cli_run,
    )
    customer_root = tmp_path / "cli-customer"
    customer_root.mkdir()
    (customer_root / ".env").write_text(
        "PC_AGENT_CREDENTIAL_STORE=env\n"
        f"PC_AGENT_UPDATE_FEED_URL={_BASE_URL}/stable.json\n"
        f"PC_AGENT_RELEASE_PUBLIC_KEY={public_key}\n"
        f"PC_AGENT_RELEASE_KEY_ID={_KEY_ID}\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "cli-hosting-report.json"
    assert (
        main(
            [
                "--root",
                str(customer_root),
                "update",
                "hosting-check",
                "--public-key-file",
                str(public_path),
                "--expected-version",
                _FUTURE,
                "--report",
                str(report_path),
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "passed"

    assert (
        main(
            [
                "--root",
                str(customer_root),
                "update",
                "hosting-check-verify",
                str(report_path),
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["feed_version"] == _FUTURE

    called: dict[str, Any] = {}

    def fake_serve(package: Path, **kwargs: Any) -> int:
        called["package"] = package
        called.update(kwargs)
        return 0

    monkeypatch.setattr(release_admin, "_is_windows", lambda: True)
    monkeypatch.setattr(
        release_admin, "serve_update_feed_hosting_package", fake_serve
    )
    assert (
        release_admin.main(
            [
                "serve-hosting",
                str(hosting),
                "--public-key-file",
                str(public_path),
                "--key-id",
                _KEY_ID,
                "--certificate",
                str(tmp_path / "cert.pem"),
                "--private-key",
                str(tmp_path / "key.pem"),
                "--bind",
                "127.0.0.1",
                "--port",
                "8443",
            ]
        )
        == 0
    )
    assert called["package"] == hosting
    assert called["port"] == 8443

    monkeypatch.setattr(release_admin, "_is_windows", lambda: False)
    assert (
        release_admin.main(
            [
                "serve-hosting",
                str(hosting),
                "--public-key-file",
                str(public_path),
                "--key-id",
                _KEY_ID,
                "--certificate",
                str(tmp_path / "cert.pem"),
                "--private-key",
                str(tmp_path / "key.pem"),
            ]
        )
        == 1
    )
    assert "Windows only" in capsys.readouterr().err


def test_phase55_scope_inventory_and_documentation() -> None:
    assert __version__ == "0.60.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(build_default_registry().list_tools()) == 62

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == __version__
    assert project["project"]["dependencies"] == [
        "certifi>=2024.7.4",
        "pypdf[crypto]>=5.0,<7",
        "pypdfium2>=5,<6",
        "Pillow>=10.4,<14",
        "comtypes>=1.4,<2; sys_platform == 'win32'",
    ]

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "src/pc_agent/update_feed_hosting.py",
        "docs/phase55.md",
        "tests/test_phase55_windows_https_update_hosting.py",
    ):
        assert expected in release_names

    module = (ROOT / "src/pc_agent/update_feed_hosting.py").read_text(
        encoding="utf-8"
    )
    phase = (ROOT / "docs/phase55.md").read_text(encoding="utf-8")
    phase24 = (ROOT / "docs/phase24.md").read_text(encoding="utf-8")
    phase40 = (ROOT / "docs/phase40.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tools = (ROOT / "docs/tools.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "windows-https-task" in module
    assert "windows-iis" in module
    assert "linux-nginx" in module
    assert "macos-caddy" in module
    assert "ThreadingHTTPServer" in module
    assert "build_model_router" not in module
    for text in (phase, phase24, phase40, readme, tools):
        assert "Windows HTTPS Update-Feed Hosting" in text
    assert "publicly trusted TLS certificate" in phase
    assert "Linux/macOS" in phase
    assert (
        "## 0.57.0 - PHASE 55 Windows HTTPS Update-Feed Hosting"
        in changelog.splitlines()
    )
    assert UPDATE_FEED_PUBLISHER_MANIFEST == "publisher-manifest.json"
