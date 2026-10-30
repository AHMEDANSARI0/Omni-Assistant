"""Opt-in online CRL and OCSP fetching for PDF trust validation.

Phase 31 is used only when a direct CLI/GUI user explicitly enables online
revocation. Certificate-embedded CRL distribution points and OCSP responders
are contacted with bounded HTTP(S) requests. CRL/OCSP objects are never
trusted because of transport alone: callers and this module verify their
issuer signatures, freshness and certificate identity before using them.
"""

from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed448, ed25519, padding, rsa
from cryptography.x509 import ocsp
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtendedKeyUsageOID

from ..errors import ToolError
from ..http_tls import create_tls_context
from ..web import validate_url
from .pdf_signatures import _parse_der_root

MAX_ONLINE_CRL_BYTES = 5_000_000
MAX_OCSP_RESPONSE_BYTES = 262_144
MAX_REVOCATION_ENDPOINTS_PER_CERTIFICATE = 3
MAX_REVOCATION_REQUESTS = 50
_MAX_OCSP_AGE_WITHOUT_NEXT_UPDATE = timedelta(hours=24)
_CLOCK_SKEW = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class OnlineCrlRecord:
    crl: x509.CertificateRevocationList
    source_host: str


class _RevocationRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(
        self,
        *,
        allow_private_network: bool,
        resolver: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__()
        self._allow_private_network = allow_private_network
        self._resolver = resolver
        self._redirects = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if req.get_method() != "GET":
            return None
        self._redirects += 1
        if self._redirects > 3:
            raise ToolError("Revocation fetch exceeded the redirect limit.")
        validated = validate_url(
            newurl,
            allow_private_network=self._allow_private_network,
            resolver=self._resolver,
        )
        if validated.scheme not in {"http", "https"}:
            return None
        return super().redirect_request(req, fp, code, msg, headers, validated.url)


def _extension(certificate: x509.Certificate, extension_type: Any) -> Any | None:
    try:
        return certificate.extensions.get_extension_for_class(extension_type).value
    except (x509.DuplicateExtension, x509.ExtensionNotFound):
        return None


def _unique_limited(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if urlsplit(value).scheme.lower() not in {"http", "https"}:
            continue
        if value not in result:
            result.append(value)
        if len(result) >= MAX_REVOCATION_ENDPOINTS_PER_CERTIFICATE:
            break
    return tuple(result)


def crl_distribution_point_urls(certificate: x509.Certificate) -> tuple[str, ...]:
    extension = _extension(certificate, x509.CRLDistributionPoints)
    if extension is None:
        return ()
    urls: list[str] = []
    for point in extension:
        if not point.full_name:
            continue
        for name in point.full_name:
            if isinstance(name, x509.UniformResourceIdentifier):
                urls.append(name.value)
    return _unique_limited(urls)


def ocsp_responder_urls(certificate: x509.Certificate) -> tuple[str, ...]:
    extension = _extension(certificate, x509.AuthorityInformationAccess)
    if extension is None:
        return ()
    urls: list[str] = []
    for description in extension:
        if description.access_method != AuthorityInformationAccessOID.OCSP:
            continue
        location = description.access_location
        if isinstance(location, x509.UniformResourceIdentifier):
            urls.append(location.value)
    return _unique_limited(urls)


def _load_crl_bytes(data: bytes) -> x509.CertificateRevocationList:
    if b"-----BEGIN X509 CRL-----" in data:
        return x509.load_pem_x509_crl(data)
    return x509.load_der_x509_crl(data)


@dataclass(slots=True)
class OnlineRevocationClient:
    enabled: bool = False
    timeout_seconds: int = 10
    allow_private_network: bool = False
    opener: Any | None = None
    resolver: Callable[..., Any] | None = None
    online_crls: list[OnlineCrlRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    request_count: int = 0
    ocsp_response_count: int = 0
    _response_cache: dict[tuple[str, str, str], bytes | None] = field(
        default_factory=dict, repr=False
    )
    _crl_cache: dict[str, tuple[x509.CertificateRevocationList, ...]] = field(
        default_factory=dict, repr=False
    )
    _ocsp_cache: dict[tuple[str, int, str], tuple[str, str]] = field(
        default_factory=dict, repr=False
    )

    def __post_init__(self) -> None:
        if not 1 <= self.timeout_seconds <= 60:
            raise ToolError("Online revocation timeout must be between 1 and 60 seconds.")

    @property
    def network_used(self) -> bool:
        return self.request_count > 0

    def _record_error(self, url: str, exc: BaseException) -> None:
        host = urlsplit(url).hostname or "unknown-host"
        message = f"{host}: {type(exc).__name__}"
        if message not in self.errors and len(self.errors) < 50:
            self.errors.append(message)

    def _open(self, request: urllib.request.Request) -> Any:
        if self.opener is not None:
            if callable(self.opener):
                return self.opener(request, timeout=self.timeout_seconds)
            return self.opener.open(request, timeout=self.timeout_seconds)
        opener = urllib.request.build_opener(
            _RevocationRedirectHandler(
                allow_private_network=self.allow_private_network,
                resolver=self.resolver,
            ),
            urllib.request.HTTPSHandler(context=create_tls_context()),
        )
        return opener.open(request, timeout=self.timeout_seconds)

    def _fetch(
        self,
        url: str,
        *,
        maximum: int,
        data: bytes | None = None,
        content_type: str | None = None,
        accept: str | None = None,
    ) -> bytes | None:
        if not self.enabled:
            return None
        cache_key = (
            "POST" if data is not None else "GET",
            url,
            hashlib.sha256(data or b"").hexdigest(),
        )
        if cache_key in self._response_cache:
            return self._response_cache[cache_key]
        result: bytes | None = None
        try:
            if self.request_count >= MAX_REVOCATION_REQUESTS:
                raise ToolError("Online revocation request limit reached.")
            # DNS/URL validation itself is a network attempt and counts
            # against both the bound and the honest network-used report.
            self.request_count += 1
            validated = validate_url(
                url,
                allow_private_network=self.allow_private_network,
                resolver=self.resolver,
            )
            if validated.scheme not in {"http", "https"}:
                raise ToolError("Revocation endpoint is not HTTP(S).")
            headers = {"User-Agent": "Self-Healing-PC-Agent/0.41 (+PDF revocation)"}
            if content_type:
                headers["Content-Type"] = content_type
            if accept:
                headers["Accept"] = accept
            request = urllib.request.Request(
                validated.url,
                data=data,
                headers=headers,
                method="POST" if data is not None else "GET",
            )
            with self._open(request) as response:
                status_value = getattr(response, "status", None)
                status = int(
                    status_value if status_value is not None else response.getcode()
                )
                if status != 200:
                    raise ToolError(f"Revocation endpoint returned HTTP {status}.")
                body = response.read(maximum + 1)
                if len(body) > maximum:
                    raise ToolError("Revocation response exceeds the size limit.")
                result = bytes(body)
        except Exception as exc:  # noqa: BLE001 - failures mean unknown, not trusted.
            self._record_error(url, exc)
            result = None
        self._response_cache[cache_key] = result
        return result

    def crls_for_certificate(
        self, certificate: x509.Certificate
    ) -> tuple[x509.CertificateRevocationList, ...]:
        if not self.enabled:
            return ()
        fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
        if fingerprint in self._crl_cache:
            return self._crl_cache[fingerprint]
        loaded: list[x509.CertificateRevocationList] = []
        known = {
            record.crl.fingerprint(hashes.SHA256()).hex()
            for record in self.online_crls
        }
        for url in crl_distribution_point_urls(certificate):
            body = self._fetch(url, maximum=MAX_ONLINE_CRL_BYTES)
            if body is None:
                continue
            try:
                crl = _load_crl_bytes(body)
            except ValueError as exc:
                self._record_error(url, exc)
                continue
            loaded.append(crl)
            crl_fingerprint = crl.fingerprint(hashes.SHA256()).hex()
            if crl_fingerprint not in known:
                host = urlsplit(url).hostname or "online-crl"
                self.online_crls.append(OnlineCrlRecord(crl=crl, source_host=host))
                known.add(crl_fingerprint)
        result = tuple(loaded)
        self._crl_cache[fingerprint] = result
        return result


    def ocsp_status(
        self,
        certificate: x509.Certificate,
        issuer: x509.Certificate,
        *,
        verification_time: datetime,
        now: datetime,
    ) -> tuple[str, str]:
        if not self.enabled:
            return "unknown", "online OCSP was not enabled"
        cache_key = (
            certificate.fingerprint(hashes.SHA256()).hex(),
            certificate.serial_number,
            issuer.fingerprint(hashes.SHA256()).hex(),
        )
        if cache_key in self._ocsp_cache:
            return self._ocsp_cache[cache_key]

        result: tuple[str, str] = (
            "unknown",
            "no usable OCSP response was obtained",
        )
        urls = ocsp_responder_urls(certificate)
        if not urls:
            result = ("unknown", "certificate advertises no OCSP responder")
            self._ocsp_cache[cache_key] = result
            return result

        try:
            request_der = (
                ocsp.OCSPRequestBuilder()
                .add_certificate(certificate, issuer, hashes.SHA1())
                .build()
                .public_bytes(serialization.Encoding.DER)
            )
        except (TypeError, UnsupportedAlgorithm, ValueError) as exc:
            result = ("unknown", f"OCSP request could not be built: {type(exc).__name__}")
            self._ocsp_cache[cache_key] = result
            return result

        for url in urls:
            body = self._fetch(
                url,
                maximum=MAX_OCSP_RESPONSE_BYTES,
                data=request_der,
                content_type="application/ocsp-request",
                accept="application/ocsp-response",
            )
            if body is None:
                continue
            try:
                response = ocsp.load_der_ocsp_response(body)
            except ValueError as exc:
                self._record_error(url, exc)
                continue
            self.ocsp_response_count += 1
            candidate = _validate_ocsp_response(
                response,
                certificate=certificate,
                issuer=issuer,
                verification_time=verification_time,
                now=now,
            )
            if candidate[0] != "unknown":
                result = candidate
                break
            result = candidate
        self._ocsp_cache[cache_key] = result
        return result


def _public_key_hash(certificate: x509.Certificate, algorithm: Any) -> bytes | None:
    try:
        spki = certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        root = _parse_der_root(spki)
        if len(root.children) != 2:
            return None
        bit_string = root.children[1].value
        if not bit_string:
            return None
        digest = hashes.Hash(algorithm)
        digest.update(bit_string[1:])
        return digest.finalize()
    except Exception:  # noqa: BLE001 - malformed key encodings cannot authorize.
        return None


def _verify_ocsp_signature(
    response: ocsp.OCSPResponse, responder: x509.Certificate
) -> bool:
    public_key = responder.public_key()
    hash_algorithm = response.signature_hash_algorithm
    try:
        if isinstance(public_key, rsa.RSAPublicKey):
            if response.signature_algorithm_oid.dotted_string == "1.2.840.113549.1.1.10":
                return False  # PSS parameters are not exposed on OCSPResponse.
            if hash_algorithm is None:
                return False
            public_key.verify(
                response.signature,
                response.tbs_response_bytes,
                padding.PKCS1v15(),
                hash_algorithm,
            )
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            if hash_algorithm is None:
                return False
            public_key.verify(
                response.signature,
                response.tbs_response_bytes,
                ec.ECDSA(hash_algorithm),
            )
        elif isinstance(public_key, dsa.DSAPublicKey):
            if hash_algorithm is None:
                return False
            public_key.verify(
                response.signature,
                response.tbs_response_bytes,
                hash_algorithm,
            )
        elif isinstance(
            public_key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)
        ):
            public_key.verify(response.signature, response.tbs_response_bytes)
        else:
            return False
    except (InvalidSignature, TypeError, UnsupportedAlgorithm, ValueError):
        return False
    return True


def _responder_is_authorized(
    response: ocsp.OCSPResponse,
    responder: x509.Certificate,
    issuer: x509.Certificate,
    *,
    now: datetime,
) -> bool:
    if response.responder_name is not None and responder.subject != response.responder_name:
        return False
    if (
        response.responder_key_hash is not None
        and _public_key_hash(responder, hashes.SHA1())
        != response.responder_key_hash
    ):
        return False
    if not _verify_ocsp_signature(response, responder):
        return False
    if responder.fingerprint(hashes.SHA256()) == issuer.fingerprint(hashes.SHA256()):
        return True
    try:
        responder.verify_directly_issued_by(issuer)
    except (InvalidSignature, TypeError, UnsupportedAlgorithm, ValueError):
        return False
    if not (
        responder.not_valid_before_utc <= now <= responder.not_valid_after_utc
    ):
        return False
    usage = _extension(responder, x509.KeyUsage)
    if usage is not None and not usage.digital_signature:
        return False
    eku = _extension(responder, x509.ExtendedKeyUsage)
    return eku is not None and ExtendedKeyUsageOID.OCSP_SIGNING in list(eku)


def _validate_ocsp_response(
    response: ocsp.OCSPResponse,
    *,
    certificate: x509.Certificate,
    issuer: x509.Certificate,
    verification_time: datetime,
    now: datetime,
) -> tuple[str, str]:
    if response.response_status != ocsp.OCSPResponseStatus.SUCCESSFUL:
        return "unknown", "OCSP responder returned a non-success status"
    if response.serial_number != certificate.serial_number:
        return "unknown", "OCSP response serial does not match the certificate"

    hash_algorithm = response.hash_algorithm
    try:
        name_digest = hashes.Hash(hash_algorithm)
        name_digest.update(issuer.subject.public_bytes())
        expected_name_hash = name_digest.finalize()
    except (TypeError, UnsupportedAlgorithm, ValueError):
        return "unknown", "OCSP issuer hash algorithm is unsupported"
    if response.issuer_name_hash != expected_name_hash:
        return "unknown", "OCSP issuer-name hash does not match"
    if _public_key_hash(issuer, hash_algorithm) != response.issuer_key_hash:
        return "unknown", "OCSP issuer-key hash does not match"

    this_update = response.this_update_utc
    next_update = response.next_update_utc
    produced_at = response.produced_at_utc
    if this_update > now + _CLOCK_SKEW or produced_at > now + _CLOCK_SKEW:
        return "unknown", "OCSP response is dated in the future"
    if next_update is not None:
        if next_update <= now:
            return "unknown", "OCSP response has expired"
    elif this_update < now - _MAX_OCSP_AGE_WITHOUT_NEXT_UPDATE:
        return "unknown", "OCSP response without nextUpdate is too old"

    candidates = {cert.fingerprint(hashes.SHA256()).hex(): cert for cert in response.certificates}
    candidates[issuer.fingerprint(hashes.SHA256()).hex()] = issuer
    if not any(
        _responder_is_authorized(response, candidate, issuer, now=produced_at)
        for candidate in candidates.values()
    ):
        return "unknown", "OCSP response signer is not an authorized responder"

    if response.certificate_status == ocsp.OCSPCertStatus.GOOD:
        return "not_revoked", "a verified OCSP response reports the certificate as good"
    if response.certificate_status == ocsp.OCSPCertStatus.REVOKED:
        revoked_at = response.revocation_time_utc
        if revoked_at is None or revoked_at <= verification_time:
            return "revoked", "a verified OCSP response reports revocation at signing time"
        return (
            "revoked_after_signing",
            "a verified OCSP response reports a revocation after signing time",
        )
    return "unknown", "OCSP responder reported unknown certificate status"

