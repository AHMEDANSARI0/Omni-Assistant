"""Phase 40: vendor-side signed update feed publishing."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from pc_agent import __version__, release_admin
from pc_agent import release as release_module
from pc_agent.config import Settings
from pc_agent.errors import PcAgentError
from pc_agent.online_updates import download_online_update
from pc_agent.release import build_release_bundle, release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry
from pc_agent.update_feed_publisher import (
    UPDATE_FEED_PUBLISHER_MANIFEST,
    build_update_feed_package,
    normalize_update_base_url,
    verify_update_feed_package,
)
from pc_agent.updates import b64url_encode

ROOT = Path(__file__).resolve().parents[1]
_FUTURE = "0.83.0"
_BASE_URL = "https://updates.example.com/self-healing-pc-agent"
_PUBLISHED_AT = "2026-10-30T12:00:00Z"
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
        "VALUE = 40\n", encoding="utf-8"
    )
    (root / "README.md").write_text("Feed publisher test.\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("Changes.\n", encoding="utf-8")
    (root / ".env.example").write_text("EXAMPLE=1\n", encoding="utf-8")
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")
    (root / "docs" / "phase40.md").write_text("Phase 40.\n", encoding="utf-8")
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


def _future_bundle_and_keys(
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
    public_key = _key_value(
        keys / "release-public-key.txt", "PC_AGENT_RELEASE_PUBLIC_KEY"
    )
    return bundle, keys / "release-private-key.txt", keys / "release-public-key.txt", public_key


def _publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[Path, Path, Path, str]:
    bundle, private_path, public_path, public_key = _future_bundle_and_keys(
        tmp_path, monkeypatch, capsys
    )
    output = tmp_path / "feed-package"
    info = build_update_feed_package(
        bundle,
        output,
        seed=release_admin._read_private_seed(private_path),
        key_id=_KEY_ID,
        base_url=_BASE_URL,
        published_at=_PUBLISHED_AT,
    )
    assert info.path == output
    return output, private_path, public_path, public_key


def test_phase40_builds_and_verifies_static_signed_feed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output, private_path, _public_path, public_key = _publish(
        tmp_path, monkeypatch, capsys
    )
    info = verify_update_feed_package(
        output,
        public_key=public_key,
        key_id=_KEY_ID,
        expected_base_url=_BASE_URL,
        expected_channel="stable",
    )

    assert info.version == _FUTURE
    assert info.bundle_name == f"release-{_FUTURE}.zip"
    assert info.signature_name == f"release-{_FUTURE}.zip.sig.json"
    assert info.feed_url == f"{_BASE_URL}/stable.json"
    assert info.file_count == 6
    assert info.to_dict()["private_key_included"] is False
    assert info.to_dict()["uploaded"] is False

    inventory = {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file()
    }
    assert inventory == {
        "HOSTING.txt",
        "customer-config.env.example",
        "publisher-manifest.json",
        f"releases/release-{_FUTURE}.zip",
        f"releases/release-{_FUTURE}.zip.sig.json",
        "stable.json",
    }

    feed = json.loads((output / "stable.json").read_text(encoding="utf-8"))
    assert feed == {
        "app": "self-healing-pc-agent",
        "bundle_sha256": info.bundle_sha256,
        "bundle_url": f"releases/release-{_FUTURE}.zip",
        "channel": "stable",
        "format": 1,
        "published_at": _PUBLISHED_AT,
        "signature_url": f"releases/release-{_FUTURE}.zip.sig.json",
        "size_bytes": info.bundle_size,
        "version": _FUTURE,
    }
    signature = json.loads(
        (output / "releases" / info.signature_name).read_text(encoding="utf-8")
    )
    assert signature["bundle_file"] == info.bundle_name
    assert signature["bundle_sha256"] == info.bundle_sha256
    assert signature["key_id"] == _KEY_ID

    config = (output / "customer-config.env.example").read_text(encoding="utf-8")
    assert f"PC_AGENT_UPDATE_FEED_URL={_BASE_URL}/stable.json" in config
    assert f"PC_AGENT_RELEASE_PUBLIC_KEY={public_key}" in config
    assert "PC_AGENT_REQUIRE_SIGNED_UPDATES=1" in config

    private_value = _key_value(private_path, "PC_AGENT_RELEASE_PRIVATE_KEY")
    seed = release_admin._read_private_seed(private_path)
    for path in output.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert private_value.encode("ascii") not in data
            assert b64url_encode(seed).encode("ascii") not in data
            assert seed not in data

    with pytest.raises(PcAgentError, match="already exists"):
        build_update_feed_package(
            tmp_path / "input-bundle.zip",
            output,
            seed=seed,
            key_id=_KEY_ID,
            base_url=_BASE_URL,
            published_at=_PUBLISHED_AT,
        )


def test_phase40_feed_verification_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output, _private_path, _public_path, public_key = _publish(
        tmp_path, monkeypatch, capsys
    )

    for bad_url in (
        "http://updates.example.com/feed",
        "https://user:pass@updates.example.com/feed",
        "https://updates.example.com/stable.json",
        "https://updates.example.com/feed?token=secret",
    ):
        with pytest.raises(PcAgentError):
            normalize_update_base_url(bad_url)
    assert normalize_update_base_url("http://127.0.0.1:8765/feed/") == (
        "http://127.0.0.1:8765/feed"
    )

    with pytest.raises(PcAgentError, match="signing key"):
        verify_update_feed_package(
            output,
            public_key=b"\x01" * 32,
            key_id=_KEY_ID,
        )

    tampered = tmp_path / "tampered"
    shutil.copytree(output, tampered)
    bundle = tampered / "releases" / f"release-{_FUTURE}.zip"
    bundle.write_bytes(bundle.read_bytes() + b"tamper")
    with pytest.raises(PcAgentError, match="failed verification"):
        verify_update_feed_package(
            tampered,
            public_key=public_key,
            key_id=_KEY_ID,
        )

    extra = tmp_path / "extra"
    shutil.copytree(output, extra)
    (extra / "unexpected.txt").write_text("extra\n", encoding="utf-8")
    with pytest.raises(PcAgentError, match="inventory"):
        verify_update_feed_package(
            extra,
            public_key=public_key,
            key_id=_KEY_ID,
        )

    rehashed = tmp_path / "rehashed"
    shutil.copytree(output, rehashed)
    feed_path = rehashed / "stable.json"
    feed = json.loads(feed_path.read_text(encoding="utf-8"))
    feed["version"] = "9.9.9"  # deliberately mismatched with _FUTURE
    feed_path.write_text(json.dumps(feed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_path = rehashed / UPDATE_FEED_PUBLISHER_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for record in manifest["files"]:
        data = (rehashed / record["path"]).read_bytes()
        record["size"] = len(data)
        record["sha256"] = hashlib.sha256(data).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(PcAgentError, match="does not match the publisher manifest"):
        verify_update_feed_package(
            rehashed,
            public_key=public_key,
            key_id=_KEY_ID,
        )


def test_phase40_release_admin_and_customer_consumer_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle, private_path, public_path, public_key = _future_bundle_and_keys(
        tmp_path, monkeypatch, capsys
    )
    output = tmp_path / "cli-feed"
    assert (
        release_admin.main(
            [
                "publish-feed",
                str(bundle),
                "--private-key",
                str(private_path),
                "--key-id",
                _KEY_ID,
                "--base-url",
                _BASE_URL,
                "--output",
                str(output),
                "--published-at",
                _PUBLISHED_AT,
            ]
        )
        == 0
    )
    publish_output = capsys.readouterr().out
    private_value = _key_value(private_path, "PC_AGENT_RELEASE_PRIVATE_KEY")
    assert "Built and verified signed update feed package" in publish_output
    assert private_value not in publish_output

    assert (
        release_admin.main(
            [
                "verify-feed",
                str(output),
                "--public-key-file",
                str(public_path),
                "--key-id",
                _KEY_ID,
                "--base-url",
                _BASE_URL,
                "--channel",
                "stable",
            ]
        )
        == 0
    )
    verify_output = capsys.readouterr().out
    assert "Verified signed update feed package" in verify_output
    assert private_value not in verify_output

    customer_root = tmp_path / "customer"
    settings = Settings.from_env(
        customer_root,
        environ={
            "PC_AGENT_PROVIDER_ORDER": "mock",
            "PC_AGENT_CREDENTIAL_STORE": "env",
            "PC_AGENT_ONLINE_UPDATES": "1",
            "PC_AGENT_UPDATE_FEED_URL": f"{_BASE_URL}/stable.json",
            "PC_AGENT_UPDATE_CHANNEL": "stable",
            "PC_AGENT_RELEASE_PUBLIC_KEY": public_key,
            "PC_AGENT_RELEASE_KEY_ID": _KEY_ID,
            "PC_AGENT_REQUIRE_SIGNED_UPDATES": "1",
        },
        load_dotenv=False,
    )
    settings.ensure_directories()

    def transport(url: str, _max_bytes: int, _timeout: float) -> bytes:
        if not url.startswith(_BASE_URL + "/"):
            raise RuntimeError(f"unexpected URL: {url}")
        relative = url.removeprefix(_BASE_URL + "/")
        return (output / relative).read_bytes()

    downloaded = download_online_update(settings, transport=transport)
    assert downloaded.status == "downloaded"
    assert downloaded.manifest.version == _FUTURE
    assert downloaded.check.signature.status == "verified"
    assert downloaded.check.bundle.bundle_sha256 == downloaded.manifest.bundle_sha256


def test_phase40_scope_inventory_and_counts() -> None:
    assert __version__ == "0.82.12"
    assert len(build_default_registry().list_tools()) == 69
    assert LATEST_SCHEMA_VERSION == 18

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "src/pc_agent/update_feed_publisher.py",
        "docs/phase40.md",
        "tests/test_phase40_update_feed_publisher.py",
    ):
        assert expected in release_names

    phase39 = (ROOT / "docs" / "phase39.md").read_text(encoding="utf-8")
    phase40 = (ROOT / "docs" / "phase40.md").read_text(encoding="utf-8")
    assert "version | 0.41.0" in phase39
    assert "version | 0.42.0" in phase40
    assert "does **not** create a hosting account" in phase40
    assert "does not add background polling" in phase40
