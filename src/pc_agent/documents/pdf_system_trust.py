"""Opt-in operating-system/OpenSSL trust anchors for PDF validation.

Phase 33 lets a direct CLI/GUI user add CA certificates from the platform
trust store to the PDF validator's explicit workspace anchors. On Windows the
ROOT certificate store is enumerated through :func:`ssl.enum_certificates`;
elsewhere the CA certificates loaded into Python/OpenSSL's default context
are used. This is not Adobe's trust program and it grants no authority by
itself: callers still run the normal chain, policy/name-constraint and
revocation checks.
"""

from __future__ import annotations

import ssl
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes

from ..errors import ToolError

MAX_SYSTEM_TRUST_CERTIFICATES = 2_000
_PEM_HEADER = b"-----BEGIN CERTIFICATE-----"


@dataclass(frozen=True, slots=True)
class SystemTrustStore:
    certificates: tuple[x509.Certificate, ...]
    source: str
    skipped_count: int = 0


SystemTrustLoader = Callable[[], Any]


def _parse_certificate(value: Any) -> x509.Certificate | None:
    if isinstance(value, x509.Certificate):
        return value
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
        try:
            if _PEM_HEADER in data:
                return x509.load_pem_x509_certificate(data)
            return x509.load_der_x509_certificate(data)
        except ValueError:
            return None
    # Windows ssl.enum_certificates rows are (cert_bytes, encoding, trust).
    if isinstance(value, tuple) and value:
        return _parse_certificate(value[0])
    return None


def _is_ca_anchor_candidate(certificate: x509.Certificate) -> bool:
    try:
        constraints = certificate.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value
    except x509.ExtensionNotFound:
        constraints = None
    except (x509.DuplicateExtension, ValueError):
        return False
    if constraints is not None and not constraints.ca:
        return False
    try:
        usage = certificate.extensions.get_extension_for_class(
            x509.KeyUsage
        ).value
    except x509.ExtensionNotFound:
        usage = None
    except (x509.DuplicateExtension, ValueError):
        return False
    return usage is None or usage.key_cert_sign


def _normalise_certificates(
    values: Iterable[Any],
    *,
    source: str,
    already_skipped: int = 0,
) -> SystemTrustStore:
    certificates: list[x509.Certificate] = []
    seen: set[str] = set()
    skipped = already_skipped
    exceeded = False
    for value in values:
        if len(certificates) >= MAX_SYSTEM_TRUST_CERTIFICATES:
            exceeded = True
            break
        certificate = _parse_certificate(value)
        if certificate is None or not _is_ca_anchor_candidate(certificate):
            skipped += 1
            continue
        fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
        if fingerprint in seen:
            skipped += 1
            continue
        seen.add(fingerprint)
        certificates.append(certificate)
    if exceeded:
        raise ToolError(
            "System trust store contains too many CA certificates; the limit "
            f"is {MAX_SYSTEM_TRUST_CERTIFICATES}."
        )
    if not certificates:
        raise ToolError(
            "System trust store did not provide any usable CA certificates."
        )
    return SystemTrustStore(
        certificates=tuple(certificates),
        source=source,
        skipped_count=skipped,
    )


def _platform_certificates() -> tuple[Iterable[Any], str]:
    enum_certificates = getattr(ssl, "enum_certificates", None)
    if sys.platform.startswith("win") and callable(enum_certificates):
        return enum_certificates("ROOT"), "windows-root-certificate-store"
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    return (
        context.get_ca_certs(binary_form=True),
        "openssl-default-certificate-store",
    )


def load_system_trust_store(
    loader: SystemTrustLoader | None = None,
) -> SystemTrustStore:
    """Load bounded CA anchors from the platform or an injected loader.

    A custom loader is an internal test/host seam. It may return a
    :class:`SystemTrustStore`, a ``(certificates, source)`` pair, or an
    iterable of certificates/DER/PEM bytes/Windows enum rows.
    """

    try:
        if loader is None:
            values, source = _platform_certificates()
            return _normalise_certificates(values, source=source)

        loaded = loader()
        if isinstance(loaded, SystemTrustStore):
            return _normalise_certificates(
                loaded.certificates,
                source=loaded.source,
                already_skipped=loaded.skipped_count,
            )
        if (
            isinstance(loaded, tuple)
            and len(loaded) == 2
            and isinstance(loaded[1], str)
        ):
            values, source = loaded
            return _normalise_certificates(values, source=source)
        return _normalise_certificates(
            loaded,
            source="injected-system-certificate-store",
        )
    except ToolError:
        raise
    except Exception as exc:  # Host-store failures fail closed.
        raise ToolError(
            f"Could not load the system trust store: {type(exc).__name__}"
        ) from exc
