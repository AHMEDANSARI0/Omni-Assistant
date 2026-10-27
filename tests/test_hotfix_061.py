"""Phase 4.1 TLS and duplicate-recovery hotfix tests."""

from __future__ import annotations

import ssl
import urllib.error
from typing import Any, Self

import pytest

import pc_agent.providers.base as provider_base
from pc_agent import http_tls
from pc_agent.errors import ProviderError, ToolError
from pc_agent.http_tls import (
    _load_certifi_certificates,
    _load_windows_certificates,
    create_tls_context,
    tls_trust_info,
)
from pc_agent.providers.base import HttpJsonRequest, UrllibJsonTransport
from pc_agent.web import fetch_url


class _RecordingContext:
    def __init__(self) -> None:
        self.loaded: list[str] = []

    def load_verify_locations(
        self, *, cafile: str | None = None, cadata: str | None = None
    ) -> None:
        self.loaded.append(cadata or cafile or "")


def test_tls_context_remains_verified_and_loads_certifi() -> None:
    context = create_tls_context()
    info = tls_trust_info()

    assert context.check_hostname
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert info.certifi_loaded
    assert info.ca_certificates > 50


def test_certifi_loader_uses_the_packaged_ca_bundle() -> None:
    import certifi

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    assert _load_certifi_certificates(context, cafile=certifi.where())
    assert int(context.cert_store_stats()["x509_ca"]) > 50


def test_windows_certificate_loader_filters_server_auth_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [
        (b"root", "x509_asn", True),
        (b"intermediate", "x509_asn", {"1.3.6.1.5.5.7.3.1"}),
        (b"not-server-auth", "x509_asn", {"1.3.6.1.5.5.7.3.2"}),
        (b"not-x509", "pkcs7", True),
    ]
    monkeypatch.setattr(http_tls.os, "name", "nt")
    monkeypatch.setattr(
        http_tls.ssl,
        "enum_certificates",
        lambda _store: entries,
        raising=False,
    )
    monkeypatch.setattr(
        http_tls.ssl,
        "DER_cert_to_PEM_cert",
        lambda certificate: f"PEM:{certificate.decode()}",
        raising=False,
    )
    context = _RecordingContext()

    loaded = _load_windows_certificates(context)  # type: ignore[arg-type]

    # ROOT and CA are both consulted; only server-auth X.509 entries load.
    assert loaded == 4
    assert context.loaded == [
        "PEM:root",
        "PEM:intermediate",
        "PEM:root",
        "PEM:intermediate",
    ]


def _public_resolver(_host: str, port: int, **_kwargs: Any) -> list[Any]:
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def test_web_fetch_reports_tls_failure_without_disabling_verification() -> None:
    def fail_with_certificate(_request: Any, timeout: float) -> Any:
        assert timeout > 0
        raise urllib.error.URLError(
            ssl.SSLCertVerificationError("certificate verify failed")
        )

    with pytest.raises(ToolError, match="TLS certificate verification failed"):
        fetch_url(
            "https://example.com/",
            resolver=_public_resolver,
            opener=fail_with_certificate,
        )


class _FakeProviderResponse:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return b'{"ok": true}'


def _provider_request() -> HttpJsonRequest:
    return HttpJsonRequest(
        url="https://provider.example/v1/test",
        payload={"prompt": "hello"},
        headers={},
        timeout_seconds=5,
    )


def test_provider_transport_passes_verified_tls_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(_request: Any, *, timeout: float, context: Any) -> Any:
        captured["timeout"] = timeout
        captured["context"] = context
        return _FakeProviderResponse()

    monkeypatch.setattr(provider_base, "urlopen", fake_urlopen)

    assert UrllibJsonTransport()(_provider_request()) == {"ok": True}
    assert captured["context"].check_hostname
    assert captured["context"].verify_mode is ssl.CERT_REQUIRED


def test_provider_transport_maps_tls_failure_to_nonretryable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(_request: Any, *, timeout: float, context: Any) -> Any:
        assert timeout > 0
        assert context.verify_mode is ssl.CERT_REQUIRED
        raise urllib.error.URLError(
            ssl.SSLCertVerificationError("certificate verify failed")
        )

    monkeypatch.setattr(provider_base, "urlopen", fake_urlopen)

    with pytest.raises(ProviderError, match="TLS certificate verification") as captured:
        UrllibJsonTransport()(_provider_request())
    assert not captured.value.retryable
