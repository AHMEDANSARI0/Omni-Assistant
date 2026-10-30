"""Phase 31: opt-in online CRL distribution-point and OCSP revocation."""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self
from urllib.request import Request

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509 import ocsp
from cryptography.x509.oid import (
    AuthorityInformationAccessOID,
    ExtendedKeyUsageOID,
    NameOID,
)

from pc_agent import __version__
from pc_agent.cli import build_parser
from pc_agent.documents.pdf import write_blank_pdf
from pc_agent.documents.pdf_revocation import (
    OnlineRevocationClient,
    crl_distribution_point_urls,
    ocsp_responder_urls,
)
from pc_agent.documents.pdf_signing import sign_pdf
from pc_agent.documents.pdf_trust import validate_pdf_signature_trust
from pc_agent.gui.controller import GuiController
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

PFX_PASSWORD = "pfx-secret-phase31"
CRL_URL = "http://crl.example.test/root.crl"
OCSP_URL = "http://ocsp.example.test/status"


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _certificate(
    *,
    subject: x509.Name,
    issuer: x509.Name,
    public_key: Any,
    issuer_key: Any,
    serial: int,
    ca: bool = False,
    crl_urls: tuple[str, ...] = (),
    ocsp_url: str | None = None,
    ocsp_signing: bool = False,
) -> x509.Certificate:
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(serial)
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.BasicConstraints(ca=ca, path_length=0 if ca else None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca,
                crl_sign=ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(public_key),
            critical=False,
        )
    )
    if crl_urls:
        builder = builder.add_extension(
            x509.CRLDistributionPoints(
                [
                    x509.DistributionPoint(
                        full_name=[x509.UniformResourceIdentifier(url)],
                        relative_name=None,
                        reasons=None,
                        crl_issuer=None,
                    )
                    for url in crl_urls
                ]
            ),
            critical=False,
        )
    if ocsp_url:
        builder = builder.add_extension(
            x509.AuthorityInformationAccess(
                [
                    x509.AccessDescription(
                        AuthorityInformationAccessOID.OCSP,
                        x509.UniformResourceIdentifier(ocsp_url),
                    )
                ]
            ),
            critical=False,
        )
    if ocsp_signing:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.OCSP_SIGNING]),
            critical=False,
        )
    return builder.sign(issuer_key, hashes.SHA256())


def _hierarchy(
    folder: Path,
    *,
    crl_urls: tuple[str, ...] = (),
    ocsp_url: str | None = None,
) -> dict[str, Any]:
    folder.mkdir(parents=True, exist_ok=True)
    root_key = ec.generate_private_key(ec.SECP256R1())
    signer_key = ec.generate_private_key(ec.SECP256R1())
    root_name = _name("Phase 31 Root CA")
    root_cert = _certificate(
        subject=root_name,
        issuer=root_name,
        public_key=root_key.public_key(),
        issuer_key=root_key,
        serial=3101,
        ca=True,
    )
    signer_cert = _certificate(
        subject=_name("Phase 31 Signer"),
        issuer=root_name,
        public_key=signer_key.public_key(),
        issuer_key=root_key,
        serial=3102,
        crl_urls=crl_urls,
        ocsp_url=ocsp_url,
    )
    (folder / "root-ca.pem").write_bytes(
        root_cert.public_bytes(serialization.Encoding.PEM)
    )
    (folder / "identity.pfx").write_bytes(
        pkcs12.serialize_key_and_certificates(
            b"Phase 31 Signer",
            signer_key,
            signer_cert,
            [root_cert],
            serialization.BestAvailableEncryption(PFX_PASSWORD.encode()),
        )
    )
    return {
        "root_key": root_key,
        "root_cert": root_cert,
        "signer_key": signer_key,
        "signer_cert": signer_cert,
    }


def _signed_pdf(folder: Path) -> Path:
    source = folder / "plain.pdf"
    signed = folder / "signed.pdf"
    write_blank_pdf(source, pages=1)
    sign_pdf(
        source,
        signed,
        pkcs12_path=folder / "identity.pfx",
        pkcs12_password=PFX_PASSWORD,
    )
    return signed


def _crl_bytes(
    hierarchy: dict[str, Any], *, revoked: bool = False
) -> bytes:
    now = datetime.now(UTC)
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(hierarchy["root_cert"].subject)
        .last_update(now - timedelta(minutes=5))
        .next_update(now + timedelta(days=1))
    )
    if revoked:
        builder = builder.add_revoked_certificate(
            x509.RevokedCertificateBuilder()
            .serial_number(hierarchy["signer_cert"].serial_number)
            .revocation_date(now - timedelta(hours=1))
            .build()
        )
    return builder.sign(
        private_key=hierarchy["root_key"], algorithm=hashes.SHA256()
    ).public_bytes(serialization.Encoding.DER)


def _ocsp_response(
    hierarchy: dict[str, Any],
    *,
    status: ocsp.OCSPCertStatus = ocsp.OCSPCertStatus.GOOD,
    responder_cert: x509.Certificate | None = None,
    responder_key: Any = None,
) -> bytes:
    now = datetime.now(UTC)
    responder = responder_cert or hierarchy["root_cert"]
    key = responder_key or hierarchy["root_key"]
    revoked = status == ocsp.OCSPCertStatus.REVOKED
    builder = ocsp.OCSPResponseBuilder().add_response(
        cert=hierarchy["signer_cert"],
        issuer=hierarchy["root_cert"],
        algorithm=hashes.SHA1(),  # RFC 6960 CertID issuer hashing.
        cert_status=status,
        this_update=now - timedelta(minutes=1),
        next_update=now + timedelta(days=1),
        revocation_time=now - timedelta(hours=1) if revoked else None,
        revocation_reason=None,
    )
    builder = builder.responder_id(ocsp.OCSPResponderEncoding.NAME, responder)
    if responder != hierarchy["root_cert"]:
        builder = builder.certificates([responder])
    return builder.sign(key, hashes.SHA256()).public_bytes(
        serialization.Encoding.DER
    )


def _fake_resolver(host: str, port: int, type: int = 0) -> list[Any]:
    del host, type
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class _Response:
    def __init__(self, body: bytes, *, status: int = 200, url: str = "") -> None:
        self._body = body
        self._offset = 0
        self.status = status
        self._url = url

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._body[self._offset :]
            self._offset = len(self._body)
            return chunk
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        return None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        del args
        self.close()


class _FakeOpener:
    def __init__(self, routes: dict[str, bytes]) -> None:
        self.routes = routes
        self.requests: list[dict[str, Any]] = []

    def open(self, request: Any, timeout: float | None = None) -> _Response:
        del timeout
        if isinstance(request, Request):
            url = request.full_url
            method = request.get_method()
            data = request.data
            headers = dict(request.header_items())
        else:
            url = str(request)
            method = "GET"
            data = None
            headers = {}
        self.requests.append(
            {"method": method, "url": url, "data": data, "headers": headers}
        )
        body = self.routes.get(url)
        if body is None:
            raise OSError("fake route unavailable")
        return _Response(body, url=url)


def test_online_revocation_endpoint_extraction_is_bounded(
    tmp_path: Path,
) -> None:
    hierarchy = _hierarchy(
        tmp_path,
        crl_urls=(
            "http://crl.example.test/one.crl",
            "https://crl.example.test/two.crl",
            "http://crl.example.test/one.crl",
            "ldap://directory.example.test/root.crl",
            "http://crl.example.test/three.crl",
            "http://crl.example.test/four.crl",
        ),
        ocsp_url=OCSP_URL,
    )
    certificate = hierarchy["signer_cert"]
    assert crl_distribution_point_urls(certificate) == (
        "http://crl.example.test/one.crl",
        "https://crl.example.test/two.crl",
        "http://crl.example.test/three.crl",
    )
    assert ocsp_responder_urls(certificate) == (OCSP_URL,)


def test_online_crl_distribution_point_completes_trusted_validation(
    tmp_path: Path,
) -> None:
    hierarchy = _hierarchy(tmp_path, crl_urls=(CRL_URL,), ocsp_url=OCSP_URL)
    signed = _signed_pdf(tmp_path)
    anchor = tmp_path / "root-ca.pem"

    offline = validate_pdf_signature_trust(
        signed, trust_anchor_paths=[anchor]
    )
    assert offline.signatures[0].overall_status == (
        "trusted_chain_revocation_unknown"
    )
    assert offline.online_revocation_enabled is False
    assert offline.revocation_network_used is False
    assert offline.summary()["network_used"] is False

    opener = _FakeOpener({CRL_URL: _crl_bytes(hierarchy)})
    online = validate_pdf_signature_trust(
        signed,
        trust_anchor_paths=[anchor],
        online_revocation=True,
        revocation_opener=opener,
        revocation_resolver=_fake_resolver,
    )
    assert online.signatures[0].chain_status == "trusted"
    assert online.signatures[0].revocation_status == "not_revoked"
    assert online.signatures[0].overall_status == "trusted_valid"
    assert online.online_revocation_enabled is True
    assert online.revocation_network_used is True
    assert online.online_crl_count == 1
    assert online.ocsp_response_count == 0
    assert online.summary()["network_used"] is True
    assert online.summary()["online_revocation_enabled"] is True
    assert online.summary()["revocation_network_used"] is True
    assert online.summary()["online_crl_count"] == 1
    assert online.crls[0].source_name == "online:crl.example.test"
    assert opener.requests[0]["method"] == "GET"
    assert opener.requests[0]["url"] == CRL_URL
    dumped = json.dumps(online.summary())
    assert PFX_PASSWORD not in dumped
    assert CRL_URL not in dumped


def test_online_ocsp_good_and_revoked_are_signature_verified(
    tmp_path: Path,
) -> None:
    hierarchy = _hierarchy(tmp_path, ocsp_url=OCSP_URL)
    signed = _signed_pdf(tmp_path)
    anchor = tmp_path / "root-ca.pem"

    good_opener = _FakeOpener({OCSP_URL: _ocsp_response(hierarchy)})
    good = validate_pdf_signature_trust(
        signed,
        trust_anchor_paths=[anchor],
        online_revocation=True,
        revocation_opener=good_opener,
        revocation_resolver=_fake_resolver,
    )
    assert good.signatures[0].overall_status == "trusted_valid"
    assert good.signatures[0].revocation_status == "not_revoked"
    assert good.ocsp_response_count == 1
    assert good.online_crl_count == 0
    assert good.summary()["ocsp_status"] == "evaluated"
    request = good_opener.requests[0]
    assert request["method"] == "POST"
    assert request["headers"]["Content-type"] == "application/ocsp-request"
    parsed = ocsp.load_der_ocsp_request(request["data"])
    assert parsed.serial_number == hierarchy["signer_cert"].serial_number

    revoked_opener = _FakeOpener(
        {
            OCSP_URL: _ocsp_response(
                hierarchy, status=ocsp.OCSPCertStatus.REVOKED
            )
        }
    )
    revoked = validate_pdf_signature_trust(
        signed,
        trust_anchor_paths=[anchor],
        online_revocation=True,
        revocation_opener=revoked_opener,
        revocation_resolver=_fake_resolver,
    )
    assert revoked.signatures[0].revocation_status == "revoked"
    assert revoked.signatures[0].overall_status == "trusted_chain_revoked"


def test_ocsp_delegated_responder_requires_ocsp_signing_eku(
    tmp_path: Path,
) -> None:
    hierarchy = _hierarchy(tmp_path, ocsp_url=OCSP_URL)
    responder_key = ec.generate_private_key(ec.SECP256R1())
    authorized = _certificate(
        subject=_name("Phase 31 OCSP Responder"),
        issuer=hierarchy["root_cert"].subject,
        public_key=responder_key.public_key(),
        issuer_key=hierarchy["root_key"],
        serial=3103,
        ocsp_signing=True,
    )
    now = datetime.now(UTC)
    good_opener = _FakeOpener(
        {
            OCSP_URL: _ocsp_response(
                hierarchy,
                responder_cert=authorized,
                responder_key=responder_key,
            )
        }
    )
    client = OnlineRevocationClient(
        enabled=True, opener=good_opener, resolver=_fake_resolver
    )
    status, detail = client.ocsp_status(
        hierarchy["signer_cert"],
        hierarchy["root_cert"],
        verification_time=now,
        now=now,
    )
    assert status == "not_revoked"
    assert "verified OCSP response" in detail
    assert client.ocsp_response_count == 1

    unauthorized_key = ec.generate_private_key(ec.SECP256R1())
    unauthorized = _certificate(
        subject=_name("Phase 31 Plain Responder"),
        issuer=hierarchy["root_cert"].subject,
        public_key=unauthorized_key.public_key(),
        issuer_key=hierarchy["root_key"],
        serial=3104,
        ocsp_signing=False,
    )
    bad_opener = _FakeOpener(
        {
            OCSP_URL: _ocsp_response(
                hierarchy,
                responder_cert=unauthorized,
                responder_key=unauthorized_key,
            )
        }
    )
    bad_client = OnlineRevocationClient(
        enabled=True, opener=bad_opener, resolver=_fake_resolver
    )
    status, detail = bad_client.ocsp_status(
        hierarchy["signer_cert"],
        hierarchy["root_cert"],
        verification_time=now,
        now=now,
    )
    assert status == "unknown"
    assert "not an authorized responder" in detail


def test_online_revocation_network_failure_remains_unknown_not_trusted(
    tmp_path: Path,
) -> None:
    _hierarchy(tmp_path, crl_urls=(CRL_URL,), ocsp_url=OCSP_URL)
    signed = _signed_pdf(tmp_path)
    opener = _FakeOpener({})
    validation = validate_pdf_signature_trust(
        signed,
        trust_anchor_paths=[tmp_path / "root-ca.pem"],
        online_revocation=True,
        revocation_opener=opener,
        revocation_resolver=_fake_resolver,
    )
    assert validation.signatures[0].chain_status == "trusted"
    assert validation.signatures[0].revocation_status == "unknown"
    assert validation.signatures[0].overall_status == (
        "trusted_chain_revocation_unknown"
    )
    assert validation.revocation_network_used is True
    assert validation.online_crl_count == 0
    assert validation.ocsp_response_count == 0
    assert validation.revocation_network_errors
    assert all("?" not in item for item in validation.revocation_network_errors)


def test_phase31_direct_cli_gui_scope_docs_and_registry() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "pdf",
            "validate-signatures",
            "signed.pdf",
            "--online-revocation",
            "--revocation-timeout",
            "12",
            "--allow-private-revocation-network",
        ]
    )
    assert args.online_revocation is True
    assert args.revocation_timeout == 12
    assert args.allow_private_revocation_network is True
    defaults = parser.parse_args(
        ["pdf", "validate-signatures", "signed.pdf"]
    )
    assert defaults.online_revocation is False
    assert defaults.revocation_timeout == 10
    assert defaults.allow_private_revocation_network is False
    assert hasattr(GuiController, "pdf_validate_signatures")

    root = Path(__file__).resolve().parents[1]
    app_source = (root / "src/pc_agent/gui/app.py").read_text(
        encoding="utf-8"
    )
    assert "Enable online CRL/OCSP revocation fallback (network)" in app_source
    assert "Allow online revocation checks?" in app_source
    controller_source = (root / "src/pc_agent/gui/controller.py").read_text(
        encoding="utf-8"
    )
    assert "online_revocation=online_revocation" in controller_source

    assert __version__ == "0.41.0"
    assert LATEST_SCHEMA_VERSION == 6
    registry = build_default_registry()
    assert len(registry.list_tools()) == 62
    assert not any(
        "online_revocation" in tool.spec.id or "ocsp" in tool.spec.id
        for tool in registry.list_tools()
    )
    release_paths = {path.as_posix() for path in release_candidate_files(root)}
    assert "src/pc_agent/documents/pdf_revocation.py" in release_paths
    assert "tests/test_phase31_online_revocation.py" in release_paths
    assert "docs/phase31.md" in release_paths
