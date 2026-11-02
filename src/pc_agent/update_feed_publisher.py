"""Vendor-side static publishing for signed online update feeds.

Phase 24 implemented the customer-side HTTPS downloader, but a vendor still
had to assemble its static directory by hand.  Phase 40 adds the missing
publisher: it signs one already-verified portable bundle, emits the strict
format-1 feed and customer configuration, inventories every published file,
and verifies the exact tree before it is made visible.  The resulting
directory can be uploaded unchanged to any static HTTPS host; this module
does not upload files, contact a network, or copy the private signing key.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import secrets
import shutil
import urllib.parse
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .errors import PcAgentError
from .licensing.ed25519 import public_key_from_seed, sign
from .online_updates import (
    APP_ID,
    FEED_FORMAT,
    _parse_feed_manifest,
    _validate_update_url,
)
from .release import ReleaseFileRecord, verify_release_bundle
from .updates import (
    b64url_decode,
    b64url_encode,
    build_signature_payload,
    inspect_release_signature,
    parse_version,
    signature_payload_bytes,
    validate_key_id,
)

UPDATE_FEED_PACKAGE_FORMAT = 1
UPDATE_FEED_PUBLISHER_MANIFEST = "publisher-manifest.json"
UPDATE_FEED_CUSTOMER_CONFIG = "customer-config.env.example"
UPDATE_FEED_HOSTING_NOTES = "HOSTING.txt"
UPDATE_FEED_FILE_NAME = "stable.json"
MAX_UPDATE_FEED_BUNDLE_BYTES = 25_000_000

_MAX_PUBLISHER_MANIFEST_BYTES = 1_000_000
_MAX_FEED_BYTES = 65_536
_MAX_TEXT_BYTES = 65_536
_CHANNEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class UpdateFeedPackageInfo:
    path: Path
    base_url: str
    feed_url: str
    channel: str
    version: str
    key_id: str
    public_key: str
    bundle_name: str
    signature_name: str
    bundle_sha256: str
    bundle_size: int
    feed_sha256: str
    manifest_sha256: str
    published_at: str
    files: tuple[ReleaseFileRecord, ...] = ()

    @property
    def file_count(self) -> int:
        return len(self.files) + 1  # publisher manifest inventories the rest

    def summary(self) -> str:
        return (
            f"channel={self.channel}; version={self.version}; "
            f"files={self.file_count}; bundle_sha256={self.bundle_sha256[:16]}..."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "base_url": self.base_url,
            "feed_url": self.feed_url,
            "channel": self.channel,
            "version": self.version,
            "key_id": self.key_id,
            "public_key": self.public_key,
            "bundle_name": self.bundle_name,
            "signature_name": self.signature_name,
            "bundle_sha256": self.bundle_sha256,
            "bundle_size": self.bundle_size,
            "feed_sha256": self.feed_sha256,
            "manifest_sha256": self.manifest_sha256,
            "published_at": self.published_at,
            "file_count": self.file_count,
            "files": [record.to_dict() for record in self.files],
            "private_key_included": False,
            "uploaded": False,
        }


def validate_update_channel(raw: str) -> str:
    channel = raw.strip().lower()
    if _CHANNEL_RE.fullmatch(channel) is None:
        raise PcAgentError(
            "Update channel must be 1-64 letters, digits, '.', '_' or '-'."
        )
    return channel


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def normalize_update_base_url(raw: str) -> str:
    """Return a safe static-directory base URL without a trailing slash."""

    value = raw.strip()
    if (
        not value
        or len(value) > 4096
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in value)
    ):
        raise PcAgentError("Update feed base URL must be one safe URL.")
    parsed = urllib.parse.urlsplit(value)
    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""
    if scheme not in {"https", "http"} or not parsed.netloc or not host:
        raise PcAgentError("Update feed base URL must be a valid http(s) URL.")
    if scheme == "http" and not _is_loopback_host(host):
        raise PcAgentError(
            "Update feed base URL must use HTTPS unless it points to loopback."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PcAgentError(
            "Update feed base URL must not contain credentials, a query "
            "string, or a fragment."
        )
    try:
        _ = parsed.port
    except ValueError as exc:
        raise PcAgentError("Update feed base URL has an invalid port.") from exc
    path = parsed.path.rstrip("/")
    if path.lower().endswith((".json", ".zip")):
        raise PcAgentError(
            "Update feed base URL must identify a directory, not a feed or "
            "bundle file."
        )
    return urllib.parse.urlunsplit((scheme, parsed.netloc, path, "", ""))


def _canonical_timestamp(raw: str | None) -> str:
    if raw is None:
        moment = datetime.now(UTC).replace(microsecond=0)
    else:
        value = raw.strip()
        if not value or len(value) > 64:
            raise PcAgentError("Update publication timestamp is invalid.")
        try:
            moment = datetime.fromisoformat(value)
        except ValueError as exc:
            raise PcAgentError("Update publication timestamp is invalid.") from exc
        if moment.tzinfo is None:
            raise PcAgentError(
                "Update publication timestamp must include a timezone."
            )
        moment = moment.astimezone(UTC).replace(microsecond=0)
    return moment.isoformat().replace("+00:00", "Z")


def _signature_timestamp(published_at: str) -> str:
    return published_at.replace("Z", "+00:00")


def _bundle_name(version: str) -> str:
    # Phase 24 downloads to this exact filename before signature inspection,
    # and the detached signature binds that filename.
    return f"release-{version}.zip"


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _customer_config(
    *,
    feed_url: str,
    channel: str,
    public_key: str,
    key_id: str,
) -> bytes:
    return (
        "# Self-Healing PC Agent signed online-update settings.\n"
        "# Copy these public values into the customer .env; no secret is included.\n"
        "PC_AGENT_ONLINE_UPDATES=1\n"
        f"PC_AGENT_UPDATE_FEED_URL={feed_url}\n"
        f"PC_AGENT_UPDATE_CHANNEL={channel}\n"
        f"PC_AGENT_RELEASE_PUBLIC_KEY={public_key}\n"
        f"PC_AGENT_RELEASE_KEY_ID={key_id}\n"
        "PC_AGENT_REQUIRE_SIGNED_UPDATES=1\n"
    ).encode()


def _hosting_text(
    *,
    base_url: str,
    feed_url: str,
    channel: str,
    version: str,
) -> bytes:
    return (
        "Self-Healing PC Agent signed update feed\n"
        "=======================================\n\n"
        f"Channel: {channel}\n"
        f"Version: {version}\n"
        f"Base URL: {base_url}\n"
        f"Feed URL: {feed_url}\n\n"
        "Upload the complete contents of this package, preserving paths, to\n"
        "the static HTTPS directory represented by the base URL. The host only\n"
        "needs to serve static .json and .zip files; no script, database,\n"
        "redirect service, or dynamic endpoint is required.\n\n"
        "Keep the release ZIP and its .sig.json immutable after publication.\n"
        "Publish a new package for a new version instead of replacing files in\n"
        "place. Serve stable.json with a short cache lifetime so customers do\n"
        "not keep stale metadata longer than intended.\n\n"
        "The vendor release private key is never part of this package and must\n"
        "never be uploaded. This program prepared and verified the static\n"
        "files only; it did not upload them or prove that a public host is\n"
        "currently reachable.\n"
    ).encode()


def _read_bounded(path: Path, *, limit: int, label: str) -> bytes:
    candidate = path.expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise PcAgentError(f"{label} was not found: {candidate}.")
    size = candidate.stat().st_size
    if size > limit:
        raise PcAgentError(f"{label} exceeds the {limit}-byte limit.")
    data = candidate.read_bytes()
    if len(data) != size:
        raise PcAgentError(f"{label} changed while reading: {candidate}.")
    return data


def _records(
    files: list[tuple[str, bytes]],
) -> tuple[ReleaseFileRecord, ...]:
    return tuple(
        ReleaseFileRecord(
            path,
            len(data),
            hashlib.sha256(data).hexdigest(),
        )
        for path, data in sorted(files, key=lambda item: item[0])
    )


def _normalize_public_key(public_key: str | bytes) -> tuple[bytes, str]:
    if isinstance(public_key, bytes):
        if len(public_key) != 32:
            raise PcAgentError("Release public key must decode to exactly 32 bytes.")
        raw = public_key
    else:
        raw = b64url_decode(
            public_key,
            expected_length=32,
            label="Release public key",
        )
    return raw, b64url_encode(raw)


def build_update_feed_package(
    bundle_path: Path,
    output_dir: Path,
    *,
    seed: bytes,
    key_id: str,
    base_url: str,
    channel: str = "stable",
    published_at: str | None = None,
) -> UpdateFeedPackageInfo:
    """Build and verify one static signed update-feed directory."""

    if len(seed) != 32:
        raise PcAgentError("Release private key seed must be exactly 32 bytes.")
    clean_key_id = validate_key_id(key_id)
    clean_channel = validate_update_channel(channel)
    clean_base = normalize_update_base_url(base_url)
    timestamp = _canonical_timestamp(published_at)

    source = bundle_path.expanduser()
    if source.is_symlink() or not source.is_file():
        raise PcAgentError(f"Release bundle was not found: {source}.")
    bundle_data = _read_bounded(
        source,
        limit=MAX_UPDATE_FEED_BUNDLE_BYTES,
        label="Release bundle",
    )
    bundle = verify_release_bundle(source)
    if hashlib.sha256(bundle_data).hexdigest() != bundle.bundle_sha256:
        raise PcAgentError("Release bundle changed during feed publication.")

    destination = output_dir.expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise PcAgentError(
            f"Update feed package output already exists: {destination}."
        )

    public_raw = public_key_from_seed(seed)
    public_key = b64url_encode(public_raw)
    bundle_name = _bundle_name(bundle.version)
    signature_name = f"{bundle_name}.sig.json"
    bundle_relative = f"releases/{bundle_name}"
    signature_relative = f"releases/{signature_name}"
    feed_url = f"{clean_base}/{UPDATE_FEED_FILE_NAME}"

    signature_payload = build_signature_payload(
        replace(bundle, path=Path(bundle_name)),
        key_id=clean_key_id,
        signed_at=_signature_timestamp(timestamp),
    )
    signature_document = dict(signature_payload)
    signature_document["signature"] = b64url_encode(
        sign(seed, signature_payload_bytes(signature_payload))
    )
    signature_data = _json_bytes(signature_document)

    feed_payload = {
        "format": FEED_FORMAT,
        "app": APP_ID,
        "channel": clean_channel,
        "version": bundle.version,
        "bundle_url": bundle_relative,
        "signature_url": signature_relative,
        "bundle_sha256": bundle.bundle_sha256,
        "size_bytes": len(bundle_data),
        "published_at": timestamp,
    }
    feed_data = _json_bytes(feed_payload)
    config_data = _customer_config(
        feed_url=feed_url,
        channel=clean_channel,
        public_key=public_key,
        key_id=clean_key_id,
    )
    hosting_data = _hosting_text(
        base_url=clean_base,
        feed_url=feed_url,
        channel=clean_channel,
        version=bundle.version,
    )
    published_files = [
        (UPDATE_FEED_FILE_NAME, feed_data),
        (bundle_relative, bundle_data),
        (signature_relative, signature_data),
        (UPDATE_FEED_CUSTOMER_CONFIG, config_data),
        (UPDATE_FEED_HOSTING_NOTES, hosting_data),
    ]
    records = _records(published_files)
    package_manifest = {
        "format": UPDATE_FEED_PACKAGE_FORMAT,
        "app": APP_ID,
        "version": bundle.version,
        "channel": clean_channel,
        "base_url": clean_base,
        "feed_url": feed_url,
        "key_id": clean_key_id,
        "public_key": public_key,
        "published_at": timestamp,
        "files": [record.to_dict() for record in records],
    }
    manifest_data = _json_bytes(package_manifest)

    encoded_seed = b64url_encode(seed).encode("ascii")
    for _path, data in [*published_files, (UPDATE_FEED_PUBLISHER_MANIFEST, manifest_data)]:
        if seed in data or encoded_seed in data:
            raise PcAgentError(
                "The release bundle or generated metadata contains the "
                "private signing key; refusing to publish it."
            )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.tmp-{secrets.token_hex(4)}"
    )
    try:
        for relative, data in published_files:
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (temporary / UPDATE_FEED_PUBLISHER_MANIFEST).write_bytes(manifest_data)
        verify_update_feed_package(
            temporary,
            public_key=public_raw,
            key_id=clean_key_id,
            expected_base_url=clean_base,
            expected_channel=clean_channel,
        )
        if destination.exists():
            raise PcAgentError(
                f"Update feed package output already exists: {destination}."
            )
        temporary.replace(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)

    return verify_update_feed_package(
        destination,
        public_key=public_raw,
        key_id=clean_key_id,
        expected_base_url=clean_base,
        expected_channel=clean_channel,
    )


def _parse_publisher_manifest(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PcAgentError("Update feed publisher manifest is not valid JSON.") from exc
    expected = {
        "format",
        "app",
        "version",
        "channel",
        "base_url",
        "feed_url",
        "key_id",
        "public_key",
        "published_at",
        "files",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise PcAgentError("Update feed publisher manifest has an unexpected shape.")
    if payload["format"] != UPDATE_FEED_PACKAGE_FORMAT or payload["app"] != APP_ID:
        raise PcAgentError("Update feed publisher manifest format is unsupported.")
    if not isinstance(payload["files"], list):
        raise PcAgentError("Update feed publisher manifest files are invalid.")
    return payload


def _manifest_records(payload: dict[str, Any]) -> tuple[ReleaseFileRecord, ...]:
    records: list[ReleaseFileRecord] = []
    seen: set[str] = set()
    for raw in payload["files"]:
        if not isinstance(raw, dict) or set(raw) != {"path", "size", "sha256"}:
            raise PcAgentError("Update feed publisher manifest has a malformed record.")
        path = raw["path"]
        size = raw["size"]
        digest = raw["sha256"]
        if (
            not isinstance(path, str)
            or not path
            or path in seen
            or path.startswith(("/", "\\"))
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or not isinstance(size, int)
            or size < 0
            or size > MAX_UPDATE_FEED_BUNDLE_BYTES
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
        ):
            raise PcAgentError("Update feed publisher manifest has an invalid record.")
        seen.add(path)
        records.append(ReleaseFileRecord(path, size, digest))
    if not records:
        raise PcAgentError("Update feed publisher manifest contains no files.")
    return tuple(sorted(records, key=lambda record: record.path))


def verify_update_feed_package(
    package_dir: Path,
    *,
    public_key: str | bytes,
    key_id: str,
    expected_base_url: str | None = None,
    expected_channel: str | None = None,
) -> UpdateFeedPackageInfo:
    """Verify a static update-feed tree without contacting its host."""

    clean_key_id = validate_key_id(key_id)
    _public_raw, public_text = _normalize_public_key(public_key)
    root_input = package_dir.expanduser()
    if root_input.is_symlink() or not root_input.is_dir():
        raise PcAgentError(f"Update feed package was not found: {root_input}.")
    root = root_input.resolve()

    manifest_path = root / UPDATE_FEED_PUBLISHER_MANIFEST
    manifest_data = _read_bounded(
        manifest_path,
        limit=_MAX_PUBLISHER_MANIFEST_BYTES,
        label="Update feed publisher manifest",
    )
    payload = _parse_publisher_manifest(manifest_data)
    records = _manifest_records(payload)
    record_by_path = {record.path: record for record in records}

    actual: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise PcAgentError(
                f"Update feed package contains a symbolic link: {relative}."
            )
        if path.is_file():
            actual.add(relative.as_posix())
    expected_files = set(record_by_path) | {UPDATE_FEED_PUBLISHER_MANIFEST}
    if actual != expected_files:
        raise PcAgentError(
            "Update feed package inventory does not match its publisher manifest."
        )

    file_data: dict[str, bytes] = {}
    for record in records:
        limit = (
            MAX_UPDATE_FEED_BUNDLE_BYTES
            if record.path.lower().endswith(".zip")
            else _MAX_TEXT_BYTES
        )
        data = _read_bounded(root / record.path, limit=limit, label="Update feed file")
        if len(data) != record.size or hashlib.sha256(data).hexdigest() != record.sha256:
            raise PcAgentError(
                f"Update feed file failed verification: {record.path}."
            )
        file_data[record.path] = data

    version = payload["version"]
    if not isinstance(version, str):
        raise PcAgentError("Update feed publisher manifest version is invalid.")
    parse_version(version)
    channel = validate_update_channel(str(payload["channel"]))
    if expected_channel is not None and channel != validate_update_channel(
        expected_channel
    ):
        raise PcAgentError("Update feed package channel does not match the expected channel.")
    base_url = normalize_update_base_url(str(payload["base_url"]))
    if payload["base_url"] != base_url:
        raise PcAgentError("Update feed package base URL is not normalized.")
    if expected_base_url is not None and base_url != normalize_update_base_url(
        expected_base_url
    ):
        raise PcAgentError("Update feed package base URL does not match the expected URL.")
    feed_url = f"{base_url}/{UPDATE_FEED_FILE_NAME}"
    if payload["feed_url"] != feed_url:
        raise PcAgentError("Update feed package feed URL is inconsistent.")
    if payload["key_id"] != clean_key_id or payload["public_key"] != public_text:
        raise PcAgentError(
            "Update feed package signing key does not match the expected public key."
        )
    published_at = _canonical_timestamp(str(payload["published_at"]))
    if payload["published_at"] != published_at:
        raise PcAgentError("Update feed package timestamp is not canonical.")

    bundle_name = _bundle_name(version)
    signature_name = f"{bundle_name}.sig.json"
    bundle_relative = f"releases/{bundle_name}"
    signature_relative = f"releases/{signature_name}"
    expected_paths = {
        UPDATE_FEED_FILE_NAME,
        bundle_relative,
        signature_relative,
        UPDATE_FEED_CUSTOMER_CONFIG,
        UPDATE_FEED_HOSTING_NOTES,
    }
    if set(record_by_path) != expected_paths:
        raise PcAgentError("Update feed package published paths are unexpected.")

    feed_data = file_data[UPDATE_FEED_FILE_NAME]
    if len(feed_data) > _MAX_FEED_BYTES:
        raise PcAgentError("Update feed file exceeds the customer size limit.")
    feed_validated = _validate_update_url(
        feed_url,
        label="Online update feed URL",
        resolve=False,
    )
    feed = _parse_feed_manifest(
        feed_data,
        feed=feed_validated,
        expected_channel=channel,
        max_bundle_bytes=MAX_UPDATE_FEED_BUNDLE_BYTES,
    )
    if (
        feed.version != version
        or feed.bundle_url != f"{base_url}/{bundle_relative}"
        or feed.signature_url != f"{base_url}/{signature_relative}"
        or feed.published_at != published_at
    ):
        raise PcAgentError("Update feed does not match the publisher manifest.")

    bundle_path = root / bundle_relative
    bundle = verify_release_bundle(bundle_path)
    bundle_data = file_data[bundle_relative]
    if (
        bundle.version != version
        or bundle.bundle_sha256 != feed.bundle_sha256
        or len(bundle_data) != feed.size_bytes
        or hashlib.sha256(bundle_data).hexdigest() != feed.bundle_sha256
    ):
        raise PcAgentError("Published update bundle does not match its feed.")

    settings = SimpleNamespace(
        release_public_key=public_text,
        release_key_id=clean_key_id,
    )
    signature = inspect_release_signature(
        bundle,
        settings,  # type: ignore[arg-type]
        root / signature_relative,
    )
    if not signature.verified:
        raise PcAgentError(
            f"Published update bundle signature is not verified: {signature.summary()}"
        )

    expected_config = _customer_config(
        feed_url=feed_url,
        channel=channel,
        public_key=public_text,
        key_id=clean_key_id,
    )
    if file_data[UPDATE_FEED_CUSTOMER_CONFIG] != expected_config:
        raise PcAgentError("Published customer update configuration is inconsistent.")
    expected_hosting = _hosting_text(
        base_url=base_url,
        feed_url=feed_url,
        channel=channel,
        version=version,
    )
    if file_data[UPDATE_FEED_HOSTING_NOTES] != expected_hosting:
        raise PcAgentError("Published update hosting notes are inconsistent.")

    return UpdateFeedPackageInfo(
        path=root,
        base_url=base_url,
        feed_url=feed_url,
        channel=channel,
        version=version,
        key_id=clean_key_id,
        public_key=public_text,
        bundle_name=bundle_name,
        signature_name=signature_name,
        bundle_sha256=bundle.bundle_sha256,
        bundle_size=len(bundle_data),
        feed_sha256=hashlib.sha256(feed_data).hexdigest(),
        manifest_sha256=hashlib.sha256(manifest_data).hexdigest(),
        published_at=published_at,
        files=records,
    )
