"""RFC 3161 timestamp requests for PDF CMS and document timestamps.

This module is used only by direct PDF workflows when the user explicitly
supplies an HTTPS Time-Stamp Authority URL. For a CMS signature it timestamps
the generated signature value and inserts the token as an unsigned
attribute. For a Phase 35 DocTimeStamp it timestamps the exact ByteRange
bytes of the new document revision. In both cases only a digest is sent to
the TSA. It never claims that the TSA itself is trusted; Phase 29 performs
that separate local evaluation for signer timestamps.
"""

from __future__ import annotations

import hashlib
import secrets
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..errors import ToolError
from ..http_tls import create_tls_context
from ..web import validate_url
from .pdf_signatures import (
    _DIGEST_OIDS,
    _OID_TIMESTAMP_TOKEN,
    _DerNode,
    _integer,
    _is,
    _oid_string,
    _parse_der_root,
    _parse_timestamp_token,
    _require,
)

MAX_TIMESTAMP_RESPONSE_BYTES = 262_144
MAX_TIMESTAMP_TOKEN_BYTES = 262_144
SUPPORTED_TIMESTAMP_HASHES = ("sha256", "sha384", "sha512")

_OID_BY_DIGEST = {name: oid for oid, name in _DIGEST_OIDS.items()}


@dataclass(frozen=True, slots=True)
class TimestampAttachment:
    cms_der: bytes
    token_der: bytes
    status: str
    generation_time: str | None
    policy_oid: str | None
    hash_algorithm: str
    tsa_subject: str | None
    tsa_certificate_sha256: str | None


@dataclass(frozen=True, slots=True)
class TimestampToken:
    token_der: bytes
    status: str
    generation_time: str | None
    policy_oid: str | None
    serial_number: str | None
    hash_algorithm: str
    tsa_subject: str | None
    tsa_certificate_sha256: str | None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _der_length(length: int) -> bytes:
    if length < 0:
        raise ValueError("Negative DER length")
    if length < 128:
        return bytes([length])
    encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(encoded)]) + encoded


def _tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _der_length(len(content)) + content


def _sequence(*parts: bytes) -> bytes:
    return _tlv(0x30, b"".join(parts))


def _set(*parts: bytes) -> bytes:
    return _tlv(0x31, b"".join(sorted(parts)))


def _oid(value: str) -> bytes:
    numbers = [int(part) for part in value.split(".")]
    if len(numbers) < 2 or numbers[0] not in {0, 1, 2} or numbers[1] < 0:
        raise ValueError("Invalid OID")
    encoded = bytearray([40 * numbers[0] + numbers[1]])
    for number in numbers[2:]:
        if number < 0:
            raise ValueError("Invalid OID")
        chunks = [number & 0x7F]
        number >>= 7
        while number:
            chunks.append(number & 0x7F)
            number >>= 7
        for chunk in reversed(chunks[1:]):
            encoded.append(chunk | 0x80)
        encoded.append(chunks[0])
    return _tlv(0x06, bytes(encoded))


def _integer_der(value: int) -> bytes:
    if value == 0:
        content = b"\x00"
    elif value > 0:
        content = value.to_bytes((value.bit_length() + 8) // 8, "big")
    else:
        length = 1
        while value < -(1 << (8 * length - 1)):
            length += 1
        content = value.to_bytes(length, "big", signed=True)
    return _tlv(0x02, content)


def _octet_string(value: bytes) -> bytes:
    return _tlv(0x04, value)


def _context(tag: int, content: bytes) -> bytes:
    return _tlv(0xA0 | tag, content)


def _build_timestamp_request(
    message_bytes: bytes, *, hash_algorithm: str, nonce: int
) -> bytes:
    normalized = hash_algorithm.strip().lower().replace("-", "")
    if normalized not in SUPPORTED_TIMESTAMP_HASHES:
        raise ToolError("Timestamp hash must be sha256, sha384 or sha512.")
    digest = hashlib.new(normalized, message_bytes).digest()
    algorithm = _sequence(_oid(_OID_BY_DIGEST[normalized]), _tlv(0x05, b""))
    imprint = _sequence(algorithm, _octet_string(digest))
    return _sequence(
        _integer_der(1),
        imprint,
        _integer_der(nonce),
        _tlv(0x01, b"\xff"),
    )


def _encode_node(node: _DerNode, content: bytes) -> bytes:
    if node.tag >= 31:
        raise ToolError("CMS DER uses an unsupported multi-byte tag.")
    tag = (node.tag_class << 6) | (int(node.constructed) << 5) | node.tag
    return _tlv(tag, content)


def _rebuild_node(node: _DerNode, parts: list[bytes]) -> bytes:
    return _encode_node(node, b"".join(parts))


def _signed_data(cms_der: bytes) -> tuple[_DerNode, _DerNode, _DerNode, _DerNode]:
    root = _parse_der_root(cms_der)
    _require(root, 0, 16, "CMS content info")
    if len(root.children) != 2:
        raise ToolError("Malformed CMS content info.")
    wrapper = _require(root.children[1], 2, 0, "CMS signed data")
    if len(wrapper.children) != 1:
        raise ToolError("Malformed CMS signed-data wrapper.")
    signed_data = _require(wrapper.children[0], 0, 16, "CMS signed data")
    signer_infos = next(
        (
            child
            for child in signed_data.children[3:]
            if _is(child, 0, 17, constructed=True)
        ),
        None,
    )
    if signer_infos is None or len(signer_infos.children) != 1:
        raise ToolError("Timestamping requires a CMS with exactly one signer.")
    return root, wrapper, signed_data, signer_infos


def _signer_signature_value(signer: _DerNode) -> tuple[bytes, int]:
    _require(signer, 0, 16, "CMS signer info")
    index = 3
    if index < len(signer.children) and _is(
        signer.children[index], 2, 0, constructed=True
    ):
        index += 1
    if index + 1 >= len(signer.children):
        raise ToolError("Malformed CMS signer info.")
    signature = _require(signer.children[index + 1], 0, 4, "CMS signature")
    return signature.value, index


def cms_signature_value(cms_der: bytes) -> bytes:
    _root, _wrapper, _signed, signer_infos = _signed_data(cms_der)
    value, _index = _signer_signature_value(signer_infos.children[0])
    return value


def attach_timestamp_token(cms_der: bytes, token_der: bytes) -> bytes:
    """Insert a validated token as the sole signer's unsigned attribute."""

    if len(token_der) > MAX_TIMESTAMP_TOKEN_BYTES:
        raise ToolError("Timestamp token exceeds the size limit.")
    # Parse once here so malformed, non-CMS input cannot be attached.
    _parse_der_root(token_der)
    root, wrapper, signed_data, signer_infos = _signed_data(cms_der)
    signer = signer_infos.children[0]
    _signature, signature_index = _signer_signature_value(signer)

    attribute = _sequence(_oid(_OID_TIMESTAMP_TOKEN), _set(token_der))
    signer_parts = [child.raw for child in signer.children]
    unsigned_index = signature_index + 2
    if unsigned_index < len(signer.children) and _is(
        signer.children[unsigned_index], 2, 1, constructed=True
    ):
        existing = signer.children[unsigned_index]
        for child in existing.children:
            if child.children and _oid_string(child.children[0]) == _OID_TIMESTAMP_TOKEN:
                raise ToolError("CMS already contains a signature timestamp token.")
        merged = sorted([*(item.raw for item in existing.children), attribute])
        signer_parts[unsigned_index] = _context(1, b"".join(merged))
    else:
        signer_parts.append(_context(1, attribute))
    new_signer = _rebuild_node(signer, signer_parts)
    new_signer_infos = _rebuild_node(signer_infos, [new_signer])

    signed_parts = []
    for child in signed_data.children:
        signed_parts.append(new_signer_infos if child is signer_infos else child.raw)
    new_signed_data = _rebuild_node(signed_data, signed_parts)
    new_wrapper = _rebuild_node(wrapper, [new_signed_data])
    return _rebuild_node(root, [root.children[0].raw, new_wrapper])



def _parse_timestamp_response(
    data: bytes,
    *,
    message_bytes: bytes,
    expected_nonce: int,
    requested_hash: str,
) -> tuple[bytes, Any]:
    if len(data) > MAX_TIMESTAMP_RESPONSE_BYTES:
        raise ToolError("Timestamp response exceeds the size limit.")
    try:
        root = _parse_der_root(data)
        _require(root, 0, 16, "timestamp response")
        if len(root.children) not in {1, 2}:
            raise ToolError("Malformed timestamp response.")
        status_info = _require(root.children[0], 0, 16, "timestamp status")
        if not status_info.children:
            raise ToolError("Timestamp response status is missing.")
        status = _integer(status_info.children[0])
        if status not in {0, 1}:
            raise ToolError(f"Timestamp authority rejected the request (status {status}).")
        if len(root.children) != 2:
            raise ToolError("Timestamp response contains no token.")
        token_der = root.children[1].raw
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(
            f"Malformed timestamp response: {type(exc).__name__}"
        ) from exc

    summary, _certificates = _parse_timestamp_token(
        token_der, message_bytes=message_bytes
    )
    if summary.status != "verified_untrusted":
        raise ToolError(f"Timestamp token did not verify: {summary.status_detail}")
    if summary.hash_algorithm != requested_hash:
        raise ToolError("Timestamp token hash does not match the request.")
    if summary.nonce is None:
        raise ToolError("Timestamp token does not echo the request nonce.")
    try:
        response_nonce = int(summary.nonce, 16)
    except ValueError as exc:
        raise ToolError("Timestamp token nonce is malformed.") from exc
    if response_nonce != expected_nonce:
        raise ToolError("Timestamp token nonce does not match the request.")
    return token_der, summary


def request_timestamp_token(
    message_bytes: bytes,
    *,
    timestamp_url: str,
    hash_algorithm: str = "sha256",
    timeout_seconds: int = 15,
    allow_private_network: bool = False,
    opener: Callable[..., Any] | None = None,
) -> TimestampToken:
    """Request and cryptographically validate one RFC 3161 token."""

    normalized_hash = hash_algorithm.strip().lower().replace("-", "")
    if normalized_hash not in SUPPORTED_TIMESTAMP_HASHES:
        raise ToolError("Timestamp hash must be sha256, sha384 or sha512.")
    if not 1 <= timeout_seconds <= 60:
        raise ToolError("Timestamp timeout must be between 1 and 60 seconds.")
    validated = validate_url(
        timestamp_url,
        allow_private_network=allow_private_network,
    )
    if validated.scheme != "https":
        raise ToolError("The timestamp authority URL must use HTTPS.")

    nonce = secrets.randbits(63) | 1
    request_der = _build_timestamp_request(
        message_bytes,
        hash_algorithm=normalized_hash,
        nonce=nonce,
    )
    request = urllib.request.Request(
        validated.url,
        data=request_der,
        headers={
            "Accept": "application/timestamp-reply",
            "Content-Type": "application/timestamp-query",
            "User-Agent": "Self-Healing-PC-Agent/0.41 (+local PDF timestamping)",
        },
        method="POST",
    )
    open_url = opener
    if open_url is None:
        open_url = urllib.request.build_opener(
            _NoRedirectHandler(),
            urllib.request.HTTPSHandler(context=create_tls_context()),
        ).open

    try:
        with open_url(request, timeout=timeout_seconds) as response:
            status_value = getattr(response, "status", None)
            status = int(status_value if status_value is not None else response.getcode())
            if status != 200:
                raise ToolError(f"Timestamp authority returned HTTP {status}.")
            headers = getattr(response, "headers", None)
            content_type = ""
            if headers is not None:
                content_type = str(headers.get("Content-Type", ""))
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type and media_type not in {
                "application/timestamp-reply",
                "application/timestamp-response",
                "application/octet-stream",
            }:
                raise ToolError("Timestamp authority returned an unexpected content type.")
            response_der = response.read(MAX_TIMESTAMP_RESPONSE_BYTES + 1)
    except ToolError:
        raise
    except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
        raise ToolError(
            f"Timestamp request failed: {type(exc).__name__}"
        ) from exc

    token_der, summary = _parse_timestamp_response(
        response_der,
        message_bytes=message_bytes,
        expected_nonce=nonce,
        requested_hash=normalized_hash,
    )
    certificate = summary.signer_certificate
    return TimestampToken(
        token_der=token_der,
        status=summary.status,
        generation_time=summary.generation_time,
        policy_oid=summary.policy_oid,
        serial_number=summary.serial_number,
        hash_algorithm=normalized_hash,
        tsa_subject=certificate.subject if certificate is not None else None,
        tsa_certificate_sha256=(
            certificate.sha256_fingerprint if certificate is not None else None
        ),
    )


def request_timestamp_for_cms(
    cms_der: bytes,
    *,
    timestamp_url: str,
    hash_algorithm: str = "sha256",
    timeout_seconds: int = 15,
    allow_private_network: bool = False,
    opener: Callable[..., Any] | None = None,
) -> TimestampAttachment:
    """Request one RFC 3161 token and attach it to a one-signer CMS."""

    token = request_timestamp_token(
        cms_signature_value(cms_der),
        timestamp_url=timestamp_url,
        hash_algorithm=hash_algorithm,
        timeout_seconds=timeout_seconds,
        allow_private_network=allow_private_network,
        opener=opener,
    )
    attached = attach_timestamp_token(cms_der, token.token_der)
    return TimestampAttachment(
        cms_der=attached,
        token_der=token.token_der,
        status=token.status,
        generation_time=token.generation_time,
        policy_oid=token.policy_oid,
        hash_algorithm=token.hash_algorithm,
        tsa_subject=token.tsa_subject,
        tsa_certificate_sha256=token.tsa_certificate_sha256,
    )

