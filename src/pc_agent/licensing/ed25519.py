"""Small pure-Python Ed25519 implementation (RFC 8032) for license tokens.

The product only *verifies* signatures at runtime, which uses public data
only. Signing exists for the vendor-side license admin tool. Pure-Python
arithmetic is not constant-time, so private signing keys must only ever be
used on a trusted, offline vendor machine; see ``docs/phase6.md``.

This module is intentionally self-contained (standard library only) and is
covered by the RFC 8032 test vectors in the test suite.
"""

from __future__ import annotations

import hashlib
import os

_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, -1, _Q)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)
_IDENTITY = (0, 1)


def _x_recover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, -1, _Q) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if x % 2 != 0:
        x = _Q - x
    return x


_BY = (4 * pow(5, -1, _Q)) % _Q
_B = (_x_recover(_BY), _BY)


def _edwards_add(point_p: tuple[int, int], point_q: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = point_p
    x2, y2 = point_q
    t = _D * x1 * x2 * y1 * y2 % _Q
    x3 = (x1 * y2 + x2 * y1) * pow(1 + t, -1, _Q) % _Q
    y3 = (y1 * y2 + x1 * x2) * pow(1 - t, -1, _Q) % _Q
    return x3, y3


def _scalarmult(point: tuple[int, int], scalar: int) -> tuple[int, int]:
    result = _IDENTITY
    addend = point
    remaining = scalar
    while remaining:
        if remaining & 1:
            result = _edwards_add(result, addend)
        addend = _edwards_add(addend, addend)
        remaining >>= 1
    return result


def _is_on_curve(point: tuple[int, int]) -> bool:
    x, y = point
    return (-x * x + y * y - 1 - _D * x * x * y * y) % _Q == 0


def _encode_int(value: int) -> bytes:
    return value.to_bytes(32, "little")


def _decode_int(raw: bytes) -> int:
    return int.from_bytes(raw, "little")


def _encode_point(point: tuple[int, int]) -> bytes:
    x, y = point
    bits = y | ((x & 1) << 255)
    return bits.to_bytes(32, "little")


def _decode_point(raw: bytes) -> tuple[int, int]:
    if len(raw) != 32:
        raise ValueError("encoded point must be 32 bytes")
    bits = int.from_bytes(raw, "little")
    y = bits & ((1 << 255) - 1)
    if y >= _Q:
        raise ValueError("non-canonical point encoding")
    x = _x_recover(y)
    if (x & 1) != (bits >> 255):
        x = _Q - x
    point = (x, y)
    if not _is_on_curve(point):
        raise ValueError("point is not on the curve")
    if _encode_point(point) != raw:
        raise ValueError("non-canonical point encoding")
    return point


def _hash_int(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


def _expand_seed(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be exactly 32 bytes")
    digest = hashlib.sha512(seed).digest()
    scalar_bytes = bytearray(digest[:32])
    scalar_bytes[0] &= 248
    scalar_bytes[31] &= 63
    scalar_bytes[31] |= 64
    return int.from_bytes(scalar_bytes, "little"), digest[32:]


def generate_seed() -> bytes:
    """Return a fresh random 32-byte Ed25519 seed (vendor key generation)."""

    return os.urandom(32)


def public_key_from_seed(seed: bytes) -> bytes:
    """Derive the 32-byte public key for a 32-byte seed."""

    scalar, _prefix = _expand_seed(seed)
    return _encode_point(_scalarmult(_B, scalar))


def sign(seed: bytes, message: bytes) -> bytes:
    """Sign ``message`` with the key derived from ``seed`` (64-byte signature)."""

    scalar, prefix = _expand_seed(seed)
    public_key = _encode_point(_scalarmult(_B, scalar))
    r_value = _hash_int(prefix + message) % _L
    r_point = _scalarmult(_B, r_value)
    r_encoded = _encode_point(r_point)
    challenge = _hash_int(r_encoded + public_key + message)
    s_value = (r_value + challenge * scalar) % _L
    return r_encoded + _encode_int(s_value)


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Return True only if ``signature`` is a valid strict Ed25519 signature."""

    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        point_a = _decode_point(public_key)
        point_r = _decode_point(signature[:32])
    except ValueError:
        return False
    if point_a == _IDENTITY or point_r == _IDENTITY:
        return False
    s_value = _decode_int(signature[32:])
    if s_value >= _L:
        return False
    challenge = _hash_int(signature[:32] + public_key + message) % _L
    left = _scalarmult(_B, s_value)
    right = _edwards_add(point_r, _scalarmult(point_a, challenge))
    return left == right
