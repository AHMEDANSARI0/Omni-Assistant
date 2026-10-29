"""Explicit online download of signed portable release updates.

Phase 24 adds a narrow, operator-initiated network front end to the Phase 21
offline-update machinery.  There is deliberately no background checker,
silent download, silent install, provider call or planner tool.  A configured
feed is fetched only when the CLI or GUI asks for it; the downloaded ZIP and
its detached Ed25519 signature are verified with the existing update code,
and application still requires the separate staged-update apply step.

The feed itself is untrusted metadata.  It may say which version and SHA-256
to expect, but the downloaded bundle must independently match that hash,
pass portable-bundle verification and satisfy the configured signature
policy before it can be staged.  Bundle and signature URLs must stay on the
feed origin, public URLs must use HTTPS, redirects cannot leave that origin,
and every response is size-bounded.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings
from .errors import PcAgentError, ToolError
from .http_tls import create_tls_context
from .updates import (
    StagedUpdate,
    UpdateCheck,
    check_update,
    compare_versions,
    parse_version,
    stage_update,
    updates_root,
)
from .web import ValidatedUrl, validate_url

APP_ID = "self-healing-pc-agent"
FEED_FORMAT = 1
MAX_FEED_BYTES = 65_536
MAX_SIGNATURE_DOWNLOAD_BYTES = 64_000
MAX_UPDATE_REDIRECTS = 3

_DOWNLOAD_RECEIPT = "download-receipt.json"
_CHANNEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FEED_REQUIRED_KEYS = {
    "format",
    "app",
    "channel",
    "version",
    "bundle_url",
    "bundle_sha256",
    "size_bytes",
}
_FEED_ALLOWED_KEYS = _FEED_REQUIRED_KEYS | {"signature_url", "published_at"}

UpdateTransport = Callable[[str, int, float], bytes]


def _utc_now() -> str:
    return (
        datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True, slots=True)
class UpdateFeedManifest:
    """Strictly parsed, still-untrusted online update feed metadata."""

    channel: str
    version: str
    bundle_url: str
    signature_url: str | None
    bundle_sha256: str
    size_bytes: int
    published_at: str | None = None

    def summary(self) -> str:
        return (
            f"channel={self.channel}; version={self.version}; "
            f"size={self.size_bytes}; sha256={self.bundle_sha256}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FEED_FORMAT,
            "app": APP_ID,
            "channel": self.channel,
            "version": self.version,
            "bundle_url": self.bundle_url,
            "signature_url": self.signature_url,
            "bundle_sha256": self.bundle_sha256,
            "size_bytes": self.size_bytes,
            "published_at": self.published_at,
        }


@dataclass(frozen=True, slots=True)
class OnlineUpdateCheck:
    current_version: str
    manifest: UpdateFeedManifest
    comparison: int
    feed_host: str
    feed_sha256: str

    @property
    def update_available(self) -> bool:
        return self.comparison > 0

    def summary(self) -> str:
        state = "update available" if self.update_available else "not newer"
        return (
            f"current={self.current_version}; feed={self.manifest.version}; "
            f"{state}; host={self.feed_host}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_version": self.current_version,
            "update_available": self.update_available,
            "comparison": self.comparison,
            "feed_host": self.feed_host,
            "feed_sha256": self.feed_sha256,
            "manifest": self.manifest.to_dict(),
            "bundle_signature_verified": False,
        }


@dataclass(frozen=True, slots=True)
class DownloadedUpdate:
    download_id: str
    status: str  # downloaded | staged | failed
    created_at: str
    directory: Path
    bundle_path: Path
    signature_path: Path | None
    feed_path: Path
    receipt_path: Path
    feed_host: str
    feed_sha256: str
    manifest: UpdateFeedManifest
    check: UpdateCheck

    def summary(self) -> str:
        return (
            f"{self.download_id}; status={self.status}; "
            f"version={self.manifest.version}; "
            f"signature={self.check.signature.status}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "download_id": self.download_id,
            "status": self.status,
            "created_at": self.created_at,
            "directory": str(self.directory),
            "bundle_path": str(self.bundle_path),
            "signature_path": (
                str(self.signature_path) if self.signature_path else None
            ),
            "feed_path": str(self.feed_path),
            "receipt_path": str(self.receipt_path),
            "feed_host": self.feed_host,
            "feed_sha256": self.feed_sha256,
            "manifest": self.manifest.to_dict(),
            "check": self.check.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class OnlineStageResult:
    downloaded: DownloadedUpdate
    staged: StagedUpdate

    def to_dict(self) -> dict[str, Any]:
        return {
            "download": self.downloaded.to_dict(),
            "staged": self.staged.to_dict(),
        }


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _origin(validated: ValidatedUrl) -> tuple[str, str, int]:
    return (validated.scheme, validated.host, validated.port)


def _validate_update_url(
    raw_url: str,
    *,
    label: str,
    expected_origin: tuple[str, str, int] | None = None,
    resolve: bool = True,
) -> ValidatedUrl:
    value = raw_url.strip()
    if not value:
        raise PcAgentError(f"{label} is required.")
    parsed = urllib.parse.urlsplit(value)
    if parsed.query or parsed.fragment:
        raise PcAgentError(f"{label} must not contain a query string or fragment.")
    host = parsed.hostname or ""
    loopback = _is_loopback_host(host)
    if parsed.scheme.lower() == "http" and not loopback:
        raise PcAgentError(f"{label} must use HTTPS unless it points to loopback.")
    try:
        validated = validate_url(
            value,
            allow_private_network=loopback,
            resolve=resolve,
        )
    except ToolError as exc:
        raise PcAgentError(f"{label} is not allowed: {exc}") from exc
    if expected_origin is not None and _origin(validated) != expected_origin:
        raise PcAgentError(
            f"{label} must stay on the update feed origin "
            f"{expected_origin[0]}://{expected_origin[1]}:{expected_origin[2]}."
        )
    return validated


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin: tuple[str, str, int]) -> None:
        super().__init__()
        self._origin = origin
        self._redirects = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        self._redirects += 1
        if self._redirects > MAX_UPDATE_REDIRECTS:
            raise PcAgentError("Online update download exceeded the redirect limit.")
        validated = _validate_update_url(
            newurl,
            label="Online update redirect URL",
            expected_origin=self._origin,
            resolve=True,
        )
        return super().redirect_request(req, fp, code, msg, headers, validated.url)


def _fetch_bytes(
    validated: ValidatedUrl,
    *,
    label: str,
    max_bytes: int,
    timeout_seconds: float,
) -> tuple[bytes, ValidatedUrl]:
    origin = _origin(validated)
    opener = urllib.request.build_opener(
        _SameOriginRedirectHandler(origin),
        urllib.request.HTTPSHandler(context=create_tls_context()),
    )
    request = urllib.request.Request(
        validated.url,
        headers={
            "Accept": "application/json,application/octet-stream,application/zip",
            "Accept-Encoding": "identity",
            "User-Agent": (
                f"Self-Healing-PC-Agent/{__version__} "
                "(user-requested signed update download)"
            ),
        },
        method="GET",
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", response.getcode()))
            final = _validate_update_url(
                str(response.geturl() or validated.url),
                label=f"Final {label} URL",
                expected_origin=origin,
                resolve=True,
            )
            if status < 200 or status >= 300:
                raise PcAgentError(
                    f"{label} download failed with HTTP {status} "
                    f"for host {validated.host}."
                )
            raw_length = response.headers.get("Content-Length")
            if raw_length:
                try:
                    declared_length = int(raw_length)
                except ValueError as exc:
                    raise PcAgentError(
                        f"{label} response has an invalid Content-Length."
                    ) from exc
                if declared_length < 0 or declared_length > max_bytes:
                    raise PcAgentError(
                        f"{label} response exceeds the {max_bytes}-byte limit."
                    )
            body = bytearray()
            while True:
                remaining = max_bytes + 1 - len(body)
                if remaining <= 0:
                    raise PcAgentError(
                        f"{label} response exceeds the {max_bytes}-byte limit."
                    )
                chunk = response.read(min(65_536, remaining))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise PcAgentError(
                        f"{label} response exceeds the {max_bytes}-byte limit."
                    )
            return bytes(body), final
    except PcAgentError:
        raise
    except urllib.error.HTTPError as exc:
        raise PcAgentError(
            f"{label} download failed with HTTP {exc.code} "
            f"for host {validated.host}."
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError) or (
            "certificate verify failed" in str(reason).lower()
        ):
            raise PcAgentError(
                f"TLS certificate verification failed for host {validated.host}. "
                "Certificate verification was not disabled."
            ) from exc
        raise PcAgentError(
            f"{label} download failed for host {validated.host}: {reason}"
        ) from exc


def _get_bytes(
    raw_url: str,
    *,
    label: str,
    max_bytes: int,
    timeout_seconds: float,
    expected_origin: tuple[str, str, int] | None = None,
    transport: UpdateTransport | None = None,
) -> tuple[bytes, ValidatedUrl]:
    validated = _validate_update_url(
        raw_url,
        label=label,
        expected_origin=expected_origin,
        resolve=transport is None,
    )
    if transport is None:
        return _fetch_bytes(
            validated,
            label=label,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
        )
    try:
        data = transport(validated.url, max_bytes, timeout_seconds)
    except PcAgentError:
        raise
    except Exception as exc:  # injected transports are test/host adapters
        raise PcAgentError(
            f"{label} download failed for host {validated.host}: {exc}"
        ) from exc
    if not isinstance(data, (bytes, bytearray)):
        raise PcAgentError(f"{label} transport did not return bytes.")
    body = bytes(data)
    if len(body) > max_bytes:
        raise PcAgentError(f"{label} response exceeds the {max_bytes}-byte limit.")
    return body, validated


def _require_online_enabled(settings: Settings) -> None:
    if not settings.online_updates_enabled:
        raise PcAgentError(
            "Online update downloads are disabled. Set PC_AGENT_ONLINE_UPDATES=1 "
            "and configure PC_AGENT_UPDATE_FEED_URL before using them."
        )


def _resolve_channel(settings: Settings, channel: str | None) -> str:
    value = (channel or settings.update_channel).strip().lower()
    if _CHANNEL_RE.fullmatch(value) is None:
        raise PcAgentError(
            "Online update channel must be 1-64 letters, digits, '.', '_' or '-'."
        )
    return value


def _resolve_feed_url(settings: Settings, feed_url: str | None) -> str:
    _require_online_enabled(settings)
    raw = (feed_url or settings.update_feed_url or "").strip()
    if not raw:
        raise PcAgentError(
            "No online update feed is configured. Set PC_AGENT_UPDATE_FEED_URL "
            "or pass --feed-url to the online update command."
        )
    validated = _validate_update_url(
        raw,
        label="Online update feed URL",
        resolve=False,
    )
    if urllib.parse.urlsplit(validated.url).path in {"", "/"}:
        raise PcAgentError("Online update feed URL must include a feed path.")
    return validated.url


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _feed_string(payload: dict[str, Any], key: str, *, maximum: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise PcAgentError(f"Update feed field {key!r} must be a string.")
    text = value.strip()
    if (
        not text
        or len(text) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in text)
    ):
        raise PcAgentError(f"Update feed field {key!r} is invalid.")
    return text


def _resolve_manifest_url(
    feed: ValidatedUrl,
    raw_url: str,
    *,
    label: str,
) -> ValidatedUrl:
    if "\\" in raw_url or any(char.isspace() for char in raw_url):
        raise PcAgentError(f"{label} in the update feed is invalid.")
    parsed = urllib.parse.urlsplit(raw_url)
    if parsed.query or parsed.fragment:
        raise PcAgentError(f"{label} must not contain a query string or fragment.")
    joined = urllib.parse.urljoin(feed.url, raw_url)
    return _validate_update_url(
        joined,
        label=label,
        expected_origin=_origin(feed),
        resolve=False,
    )


def _parse_feed_manifest(
    body: bytes,
    *,
    feed: ValidatedUrl,
    expected_channel: str,
    max_bundle_bytes: int,
) -> UpdateFeedManifest:
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PcAgentError(f"Online update feed is not valid strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PcAgentError("Online update feed must contain one JSON object.")
    keys = set(payload)
    missing = sorted(_FEED_REQUIRED_KEYS - keys)
    unknown = sorted(keys - _FEED_ALLOWED_KEYS)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise PcAgentError("Online update feed fields are invalid: " + "; ".join(details))
    if type(payload["format"]) is not int or payload["format"] != FEED_FORMAT:
        raise PcAgentError(f"Online update feed format must be {FEED_FORMAT}.")
    if _feed_string(payload, "app", maximum=128) != APP_ID:
        raise PcAgentError("Online update feed is for a different application.")

    channel = _feed_string(payload, "channel", maximum=64).lower()
    if _CHANNEL_RE.fullmatch(channel) is None:
        raise PcAgentError("Online update feed channel is invalid.")
    if channel != expected_channel:
        raise PcAgentError(
            f"Online update feed channel is {channel!r}, expected "
            f"{expected_channel!r}."
        )

    version = _feed_string(payload, "version", maximum=32)
    parse_version(version)

    bundle = _resolve_manifest_url(
        feed,
        _feed_string(payload, "bundle_url", maximum=4096),
        label="Online update bundle URL",
    )
    if not urllib.parse.urlsplit(bundle.url).path.lower().endswith(".zip"):
        raise PcAgentError("Online update bundle URL must end in .zip.")

    signature_url: str | None = None
    if payload.get("signature_url") is not None:
        signature = _resolve_manifest_url(
            feed,
            _feed_string(payload, "signature_url", maximum=4096),
            label="Online update signature URL",
        )
        signature_path = urllib.parse.urlsplit(signature.url).path.lower()
        if not signature_path.endswith((".sig.json", ".json")):
            raise PcAgentError("Online update signature URL must end in .json.")
        signature_url = signature.url

    bundle_sha256 = _feed_string(payload, "bundle_sha256", maximum=64).lower()
    if _SHA256_RE.fullmatch(bundle_sha256) is None:
        raise PcAgentError("Online update feed bundle_sha256 must be lowercase SHA-256.")
    size_bytes = payload["size_bytes"]
    if type(size_bytes) is not int or size_bytes < 1 or size_bytes > max_bundle_bytes:
        raise PcAgentError(
            "Online update feed size_bytes must be between 1 and "
            f"{max_bundle_bytes}."
        )

    published_at: str | None = None
    if payload.get("published_at") is not None:
        published_at = _feed_string(payload, "published_at", maximum=64)

    return UpdateFeedManifest(
        channel=channel,
        version=version,
        bundle_url=bundle.url,
        signature_url=signature_url,
        bundle_sha256=bundle_sha256,
        size_bytes=size_bytes,
        published_at=published_at,
    )


def _fetch_feed(
    settings: Settings,
    *,
    feed_url: str | None,
    channel: str | None,
    transport: UpdateTransport | None,
) -> tuple[UpdateFeedManifest, bytes, ValidatedUrl]:
    resolved_feed = _resolve_feed_url(settings, feed_url)
    expected_channel = _resolve_channel(settings, channel)
    body, final = _get_bytes(
        resolved_feed,
        label="Online update feed",
        max_bytes=MAX_FEED_BYTES,
        timeout_seconds=settings.online_update_timeout_seconds,
        transport=transport,
    )
    manifest = _parse_feed_manifest(
        body,
        feed=final,
        expected_channel=expected_channel,
        max_bundle_bytes=settings.online_update_max_bytes,
    )
    return manifest, body, final


def check_online_update(
    settings: Settings,
    *,
    feed_url: str | None = None,
    channel: str | None = None,
    transport: UpdateTransport | None = None,
) -> OnlineUpdateCheck:
    """Fetch only the feed and compare its advertised version.

    This does not download or authenticate the bundle.  Callers must present
    that limitation clearly; ``download_online_update`` performs the bundle
    hash, portable-bundle and signature verification.
    """

    manifest, body, final = _fetch_feed(
        settings,
        feed_url=feed_url,
        channel=channel,
        transport=transport,
    )
    return OnlineUpdateCheck(
        current_version=__version__,
        manifest=manifest,
        comparison=compare_versions(manifest.version, __version__),
        feed_host=final.host,
        feed_sha256=hashlib.sha256(body).hexdigest(),
    )


def _ensure_downloads_root(settings: Settings) -> Path:
    root = updates_root(settings)
    if root.is_symlink():
        raise PcAgentError("The updates folder must not be a symbolic link.")
    downloads = root / "downloads"
    if downloads.is_symlink():
        raise PcAgentError("The online update downloads folder must not be a link.")
    downloads.mkdir(parents=True, exist_ok=True)
    if not downloads.is_dir() or downloads.is_symlink():
        raise PcAgentError("The online update downloads folder is not safe.")
    resolved_root = root.resolve()
    resolved_downloads = downloads.resolve()
    if os.path.commonpath([str(resolved_root), str(resolved_downloads)]) != str(
        resolved_root
    ):
        raise PcAgentError("The online update downloads folder escapes data/updates.")
    return resolved_downloads


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    temporary = path.parent / f".{path.name}.tmp-{secrets.token_hex(4)}"
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise PcAgentError(f"Could not write online update file {path.name}: {exc}") from exc


def _write_download_receipt(
    receipt_path: Path,
    *,
    download_id: str,
    status: str,
    created_at: str,
    feed_host: str,
    feed_sha256: str,
    manifest: UpdateFeedManifest,
    bundle_path: Path,
    signature_path: Path | None,
    feed_path: Path,
    check: UpdateCheck | None,
    error: str | None = None,
    staged_update_id: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "format": 1,
        "app": APP_ID,
        "download_id": download_id,
        "status": status,
        "created_at": created_at,
        "current_version": __version__,
        "feed_host": feed_host,
        "feed_sha256": feed_sha256,
        "manifest": manifest.to_dict(),
        "bundle_path": str(bundle_path),
        "signature_path": str(signature_path) if signature_path else None,
        "feed_path": str(feed_path),
        "check": check.to_dict() if check is not None else None,
        "error": error,
        "staged_update_id": staged_update_id,
    }
    _write_bytes_atomic(
        receipt_path,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def download_online_update(
    settings: Settings,
    *,
    feed_url: str | None = None,
    channel: str | None = None,
    allow_downgrade: bool = False,
    transport: UpdateTransport | None = None,
) -> DownloadedUpdate:
    """Download and verify one feed-advertised bundle without staging it."""

    manifest, feed_body, final_feed = _fetch_feed(
        settings,
        feed_url=feed_url,
        channel=channel,
        transport=transport,
    )
    comparison = compare_versions(manifest.version, __version__)
    if comparison <= 0 and not allow_downgrade:
        relation = "the same as" if comparison == 0 else "older than"
        raise PcAgentError(
            f"Online feed version {manifest.version} is {relation} the running "
            f"version {__version__}. Pass --allow-downgrade only for an "
            "intentional reinstall or downgrade."
        )

    settings.ensure_directories()
    downloads_root = _ensure_downloads_root(settings)
    created_at = _utc_now()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    download_id = ""
    directory: Path | None = None
    for _ in range(20):
        candidate = f"download-{timestamp}-{secrets.token_hex(3)}-v{manifest.version}"
        candidate_dir = downloads_root / candidate
        try:
            candidate_dir.mkdir()
        except FileExistsError:
            continue
        download_id = candidate
        directory = candidate_dir
        break
    if directory is None:
        raise PcAgentError("Could not allocate a unique online update download folder.")

    feed_path = directory / "feed.json"
    bundle_path = directory / f"release-{manifest.version}.zip"
    signature_path = (
        directory / f"release-{manifest.version}.zip.sig.json"
        if manifest.signature_url
        else None
    )
    receipt_path = directory / _DOWNLOAD_RECEIPT
    feed_sha256 = hashlib.sha256(feed_body).hexdigest()
    check: UpdateCheck | None = None
    try:
        _write_bytes_atomic(feed_path, feed_body)
        bundle_body, _bundle_final = _get_bytes(
            manifest.bundle_url,
            label="Online update bundle",
            max_bytes=settings.online_update_max_bytes,
            timeout_seconds=settings.online_update_timeout_seconds,
            expected_origin=_origin(final_feed),
            transport=transport,
        )
        actual_sha256 = hashlib.sha256(bundle_body).hexdigest()
        if len(bundle_body) != manifest.size_bytes:
            raise PcAgentError(
                "Downloaded update bundle size does not match the feed: "
                f"expected {manifest.size_bytes}, got {len(bundle_body)}."
            )
        if actual_sha256 != manifest.bundle_sha256:
            raise PcAgentError(
                "Downloaded update bundle SHA-256 does not match the feed."
            )
        _write_bytes_atomic(bundle_path, bundle_body)

        if manifest.signature_url and signature_path is not None:
            signature_body, _signature_final = _get_bytes(
                manifest.signature_url,
                label="Online update signature",
                max_bytes=MAX_SIGNATURE_DOWNLOAD_BYTES,
                timeout_seconds=settings.online_update_timeout_seconds,
                expected_origin=_origin(final_feed),
                transport=transport,
            )
            _write_bytes_atomic(signature_path, signature_body)

        check = check_update(
            bundle_path,
            settings,
            signature_path=signature_path,
        )
        status = "downloaded"
        error: str | None = None
        if check.bundle.version != manifest.version:
            status = "failed"
            error = (
                "Downloaded bundle version does not match the feed: "
                f"feed={manifest.version}, bundle={check.bundle.version}."
            )
        elif check.bundle.bundle_sha256 != manifest.bundle_sha256:
            status = "failed"
            error = "Verified bundle SHA-256 does not match the feed."
        elif check.signature.status in {"invalid", "key-mismatch"}:
            status = "failed"
            error = f"Downloaded bundle signature is {check.signature.status}."

        downloaded = DownloadedUpdate(
            download_id=download_id,
            status=status,
            created_at=created_at,
            directory=directory,
            bundle_path=bundle_path,
            signature_path=signature_path,
            feed_path=feed_path,
            receipt_path=receipt_path,
            feed_host=final_feed.host,
            feed_sha256=feed_sha256,
            manifest=manifest,
            check=check,
        )
        _write_download_receipt(
            receipt_path,
            download_id=download_id,
            status=status,
            created_at=created_at,
            feed_host=final_feed.host,
            feed_sha256=feed_sha256,
            manifest=manifest,
            bundle_path=bundle_path,
            signature_path=signature_path,
            feed_path=feed_path,
            check=check,
            error=error,
        )
        return downloaded
    except PcAgentError as exc:
        _write_download_receipt(
            receipt_path,
            download_id=download_id,
            status="failed",
            created_at=created_at,
            feed_host=final_feed.host,
            feed_sha256=feed_sha256,
            manifest=manifest,
            bundle_path=bundle_path,
            signature_path=signature_path,
            feed_path=feed_path,
            check=check,
            error=str(exc),
        )
        raise PcAgentError(
            f"Online update download {download_id} failed: {exc} "
            f"Receipt: {receipt_path}"
        ) from exc


def stage_online_update(
    settings: Settings,
    *,
    feed_url: str | None = None,
    channel: str | None = None,
    allow_unsigned: bool = False,
    allow_downgrade: bool = False,
    transport: UpdateTransport | None = None,
) -> OnlineStageResult:
    """Download, verify and stage an online update; never apply it here."""

    downloaded = download_online_update(
        settings,
        feed_url=feed_url,
        channel=channel,
        allow_downgrade=allow_downgrade,
        transport=transport,
    )
    if downloaded.status != "downloaded":
        raise PcAgentError(
            f"Online update download {downloaded.download_id} cannot be staged "
            f"because its verified check failed: {downloaded.check.summary()}."
        )
    staged = stage_update(
        downloaded.bundle_path,
        settings,
        signature_path=downloaded.signature_path,
        allow_unsigned=allow_unsigned,
        allow_downgrade=allow_downgrade,
    )
    staged_download = replace(downloaded, status="staged")
    _write_download_receipt(
        downloaded.receipt_path,
        download_id=downloaded.download_id,
        status="staged",
        created_at=downloaded.created_at,
        feed_host=downloaded.feed_host,
        feed_sha256=downloaded.feed_sha256,
        manifest=downloaded.manifest,
        bundle_path=downloaded.bundle_path,
        signature_path=downloaded.signature_path,
        feed_path=downloaded.feed_path,
        check=downloaded.check,
        staged_update_id=staged.update_id,
    )
    return OnlineStageResult(downloaded=staged_download, staged=staged)
