"""Verified TLS contexts for the agent's small HTTP clients.

Python installations do not all expose the same CA store to ``urllib``.  In
particular, a Windows Python install can report ``unable to get local issuer
certificate`` for a valid public site even though the browser trusts it.  The
fix here never disables verification: start with Python/OpenSSL defaults, add
the Mozilla bundle shipped by ``certifi`` when available, and on Windows also
add server-auth certificates from the operating-system ROOT/CA stores.
"""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

_SERVER_AUTH_OID = "1.3.6.1.5.5.7.3.1"


@dataclass(frozen=True, slots=True)
class TlsTrustInfo:
    ca_certificates: int
    certifi_loaded: bool
    windows_certificates_loaded: int


@lru_cache(maxsize=1)
def _build_tls_context() -> tuple[ssl.SSLContext, TlsTrustInfo]:
    context = ssl.create_default_context()
    certifi_loaded = _load_certifi_certificates(context)
    windows_loaded = _load_windows_certificates(context)

    # Re-state the secure defaults explicitly.  Loading extra roots must never
    # turn this into an unverified context.
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED

    try:
        ca_certificates = int(context.cert_store_stats().get("x509_ca", 0))
    except (AttributeError, TypeError, ValueError):
        ca_certificates = 0
    return context, TlsTrustInfo(
        ca_certificates=ca_certificates,
        certifi_loaded=certifi_loaded,
        windows_certificates_loaded=windows_loaded,
    )


def create_tls_context() -> ssl.SSLContext:
    """Return the process-wide verified TLS context."""

    return _build_tls_context()[0]


def tls_trust_info() -> TlsTrustInfo:
    """Summarize the verified context without exposing certificate contents."""

    return _build_tls_context()[1]


def _load_certifi_certificates(
    context: ssl.SSLContext, cafile: str | None = None
) -> bool:
    try:
        if cafile is None:
            import certifi

            cafile = certifi.where()
        context.load_verify_locations(cafile=cafile)
    except (ImportError, OSError, ssl.SSLError, ValueError):
        return False
    return True


def _load_windows_certificates(context: ssl.SSLContext) -> int:
    if os.name != "nt":
        return 0
    enum_certificates = getattr(ssl, "enum_certificates", None)
    der_to_pem = getattr(ssl, "DER_cert_to_PEM_cert", None)
    if not callable(enum_certificates) or not callable(der_to_pem):
        return 0

    loaded = 0
    for store_name in ("ROOT", "CA"):
        try:
            entries = list(enum_certificates(store_name))
        except (AttributeError, OSError, TypeError, ValueError):
            continue
        for entry in entries:
            certificate = _windows_certificate_pem(entry, der_to_pem)
            if certificate is None:
                continue
            try:
                context.load_verify_locations(cadata=certificate)
            except (OSError, ssl.SSLError, ValueError):
                # Duplicates already loaded by OpenSSL defaults are expected.
                continue
            loaded += 1
    return loaded


def _windows_certificate_pem(entry: Any, der_to_pem: Any) -> str | None:
    try:
        certificate, encoding, trust = entry
    except (TypeError, ValueError):
        return None
    if encoding != "x509_asn" or not isinstance(certificate, bytes):
        return None
    if trust is not True:
        try:
            trusted = _SERVER_AUTH_OID in trust
        except TypeError:
            trusted = False
        if not trusted:
            return None
    try:
        converted = der_to_pem(certificate)
    except (TypeError, ValueError):
        return None
    return converted if isinstance(converted, str) else None
