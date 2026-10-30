"""Phase 30: RFC 3161 PDF timestamp creation and validation."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Self

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    ByteStringObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    RectangleObject,
    TextStringObject,
)

import pc_agent.cli as cli_module
import pc_agent.documents.pdf_signing as signing_module
from pc_agent import __version__
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.documents.pdf import write_blank_pdf
from pc_agent.documents.pdf_signatures import (
    _integer,
    _oid_string,
    _parse_der_root,
    inspect_pdf_signatures,
)
from pc_agent.documents.pdf_signing import sign_pdf
from pc_agent.documents.pdf_timestamps import (
    _context,
    _integer_der,
    _octet_string,
    _oid,
    _sequence,
    _set,
    _tlv,
    request_timestamp_for_cms,
)
from pc_agent.documents.pdf_trust import validate_pdf_signature_trust
from pc_agent.errors import PcAgentError, ToolError
from pc_agent.gui.controller import GuiController
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

PFX_PASSWORD = "pfx-secret-phase30"
TSA_URL = "https://8.8.8.8/timestamp"
_OID_SHA256 = "2.16.840.1.101.3.4.2.1"
_OID_SIGNED_DATA = "1.2.840.113549.1.7.2"
_OID_CONTENT_TYPE = "1.2.840.113549.1.9.3"
_OID_MESSAGE_DIGEST = "1.2.840.113549.1.9.4"
_OID_SIGNING_TIME = "1.2.840.113549.1.9.5"
_OID_TST_INFO = "1.2.840.113549.1.9.16.1.4"
_OID_ECDSA_SHA256 = "1.2.840.10045.4.3.2"


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _certificate(
    *,
    subject: x509.Name,
    issuer: x509.Name,
    public_key: Any,
    issuer_key: Any,
    serial: int,
    ca: bool,
    path_length: int | None,
    tsa: bool = False,
) -> x509.Certificate:
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(serial)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.BasicConstraints(ca=ca, path_length=path_length),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=not ca,
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
    )
    if tsa:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]),
            critical=True,
        )
    return builder.sign(issuer_key, hashes.SHA256())


def _hierarchy(folder: Path, *, prefix: str = "Phase 30") -> dict[str, Any]:
    folder.mkdir(parents=True, exist_ok=True)
    keys = {
        name: ec.generate_private_key(ec.SECP256R1())
        for name in ("root", "intermediate", "signer", "tsa")
    }
    root_name = _name(f"{prefix} Root CA")
    intermediate_name = _name(f"{prefix} Intermediate CA")
    root = _certificate(
        subject=root_name,
        issuer=root_name,
        public_key=keys["root"].public_key(),
        issuer_key=keys["root"],
        serial=3001,
        ca=True,
        path_length=1,
    )
    intermediate = _certificate(
        subject=intermediate_name,
        issuer=root_name,
        public_key=keys["intermediate"].public_key(),
        issuer_key=keys["root"],
        serial=3002,
        ca=True,
        path_length=0,
    )
    signer = _certificate(
        subject=_name(f"{prefix} Signer"),
        issuer=intermediate_name,
        public_key=keys["signer"].public_key(),
        issuer_key=keys["intermediate"],
        serial=3003,
        ca=False,
        path_length=None,
    )
    tsa = _certificate(
        subject=_name(f"{prefix} TSA"),
        issuer=intermediate_name,
        public_key=keys["tsa"].public_key(),
        issuer_key=keys["intermediate"],
        serial=3004,
        ca=False,
        path_length=None,
        tsa=True,
    )
    (folder / "root-ca.pem").write_bytes(
        root.public_bytes(serialization.Encoding.PEM)
    )
    (folder / "identity.pfx").write_bytes(
        pkcs12.serialize_key_and_certificates(
            b"Phase 30 Signer",
            keys["signer"],
            signer,
            [intermediate, root],
            serialization.BestAvailableEncryption(PFX_PASSWORD.encode()),
        )
    )
    return {
        **{f"{name}_key": key for name, key in keys.items()},
        "root_cert": root,
        "intermediate_cert": intermediate,
        "signer_cert": signer,
        "tsa_cert": tsa,
    }


def _write_crl(
    path: Path,
    *,
    issuer_cert: x509.Certificate,
    issuer_key: Any,
) -> None:
    now = datetime.now(UTC)
    crl = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(issuer_cert.subject)
        .last_update(now - timedelta(days=1))
        .next_update(now + timedelta(days=1))
        .sign(private_key=issuer_key, algorithm=hashes.SHA256())
    )
    path.write_bytes(crl.public_bytes(serialization.Encoding.PEM))


def _write_crls(folder: Path, hierarchy: dict[str, Any]) -> tuple[Path, Path]:
    leaf_issuer = folder / "leaf-issuer.crl"
    intermediate_issuer = folder / "intermediate-issuer.crl"
    _write_crl(
        leaf_issuer,
        issuer_cert=hierarchy["intermediate_cert"],
        issuer_key=hierarchy["intermediate_key"],
    )
    _write_crl(
        intermediate_issuer,
        issuer_cert=hierarchy["root_cert"],
        issuer_key=hierarchy["root_key"],
    )
    return leaf_issuer, intermediate_issuer


def _timestamp_token(
    message: bytes,
    hierarchy: dict[str, Any],
    *,
    generation_time: datetime,
    nonce: int | None = None,
    wrong_imprint: bool = False,
) -> bytes:
    digest = hashlib.sha256(b"wrong" if wrong_imprint else message).digest()
    algorithm = _sequence(_oid(_OID_SHA256), _tlv(0x05, b""))
    tst_parts = [
        _integer_der(1),
        _oid("1.2.3.4.30"),
        _sequence(algorithm, _octet_string(digest)),
        _integer_der(0x3030),
        _tlv(
            0x18,
            generation_time.astimezone(UTC).strftime("%Y%m%d%H%M%SZ").encode(),
        ),
    ]
    if nonce is not None:
        tst_parts.append(_integer_der(nonce))
    tst = _sequence(*tst_parts)

    signing_time = _tlv(
        0x17,
        generation_time.astimezone(UTC).strftime("%y%m%d%H%M%SZ").encode(),
    )
    attributes = sorted(
        [
            _sequence(_oid(_OID_CONTENT_TYPE), _set(_oid(_OID_TST_INFO))),
            _sequence(_oid(_OID_SIGNING_TIME), _set(signing_time)),
            _sequence(
                _oid(_OID_MESSAGE_DIGEST),
                _set(_octet_string(hashlib.sha256(tst).digest())),
            ),
        ]
    )
    signed_attrs = b"".join(attributes)
    signing_payload = _set(*attributes)
    tsa_cert: x509.Certificate = hierarchy["tsa_cert"]
    signature = hierarchy["tsa_key"].sign(
        signing_payload, ec.ECDSA(hashes.SHA256())
    )
    signer = _sequence(
        _integer_der(1),
        _sequence(
            tsa_cert.issuer.public_bytes(),
            _integer_der(tsa_cert.serial_number),
        ),
        algorithm,
        _context(0, signed_attrs),
        _sequence(_oid(_OID_ECDSA_SHA256)),
        _octet_string(signature),
    )
    certs = b"".join(
        cert.public_bytes(serialization.Encoding.DER)
        for cert in (
            tsa_cert,
            hierarchy["intermediate_cert"],
            hierarchy["root_cert"],
        )
    )
    signed_data = _sequence(
        _integer_der(3),
        _set(algorithm),
        _sequence(_oid(_OID_TST_INFO), _context(0, _octet_string(tst))),
        _context(0, certs),
        _set(signer),
    )
    return _sequence(_oid(_OID_SIGNED_DATA), _context(0, signed_data))


class _FakeResponse:
    status = 200
    headers: ClassVar[dict[str, str]] = {
        "Content-Type": "application/timestamp-reply"
    }

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]


class _FakeTsa:
    def __init__(
        self,
        hierarchy: dict[str, Any],
        *,
        wrong_imprint: bool = False,
        reject: bool = False,
    ) -> None:
        self.hierarchy = hierarchy
        self.wrong_imprint = wrong_imprint
        self.reject = reject
        self.calls: list[dict[str, Any]] = []
        self.generation_time = datetime.now(UTC).replace(microsecond=0)

    def __call__(self, request: Any, timeout: int) -> _FakeResponse:
        body = bytes(request.data)
        root = _parse_der_root(body)
        imprint = root.children[1]
        requested_digest = imprint.children[1].value
        digest_oid = _oid_string(imprint.children[0].children[0])
        nonce = _integer(root.children[2])
        self.calls.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "content_type": request.get_header("Content-type"),
                "digest_oid": digest_oid,
                "imprint": requested_digest,
                "nonce": nonce,
            }
        )
        if self.reject:
            return _FakeResponse(_sequence(_sequence(_integer_der(2))))
        # The token helper hashes `message`; feed it a marker whose SHA-256
        # cannot be the requested imprint unless wrong_imprint is handled
        # below by building TSTInfo via the correct digest path instead.
        token = _timestamp_token_from_digest(
            requested_digest,
            self.hierarchy,
            generation_time=self.generation_time,
            nonce=nonce,
            wrong_imprint=self.wrong_imprint,
        )
        return _FakeResponse(
            _sequence(_sequence(_integer_der(0)), token)
        )


def _timestamp_token_from_digest(
    requested_digest: bytes,
    hierarchy: dict[str, Any],
    *,
    generation_time: datetime,
    nonce: int,
    wrong_imprint: bool = False,
) -> bytes:
    # Reuse the full builder by temporarily treating a synthetic message only
    # when its digest matches is impossible; construct via the same code path
    # by monkey-level substitution at the DER level is brittle. Instead,
    # duplicate the small TSTInfo head here and delegate signer assembly by
    # calling _timestamp_token with a message preimage only for non-fake use.
    # For the fake TSA we need exact requested digest, so build TST here.
    digest = hashlib.sha256(b"wrong").digest() if wrong_imprint else requested_digest
    algorithm = _sequence(_oid(_OID_SHA256), _tlv(0x05, b""))
    tst = _sequence(
        _integer_der(1),
        _oid("1.2.3.4.30"),
        _sequence(algorithm, _octet_string(digest)),
        _integer_der(0x3030),
        _tlv(0x18, generation_time.strftime("%Y%m%d%H%M%SZ").encode()),
        _integer_der(nonce),
    )
    signing_time = _tlv(
        0x17, generation_time.strftime("%y%m%d%H%M%SZ").encode()
    )
    attributes = sorted(
        [
            _sequence(_oid(_OID_CONTENT_TYPE), _set(_oid(_OID_TST_INFO))),
            _sequence(_oid(_OID_SIGNING_TIME), _set(signing_time)),
            _sequence(
                _oid(_OID_MESSAGE_DIGEST),
                _set(_octet_string(hashlib.sha256(tst).digest())),
            ),
        ]
    )
    signed_attrs = b"".join(attributes)
    signing_payload = _set(*attributes)
    tsa_cert: x509.Certificate = hierarchy["tsa_cert"]
    signature = hierarchy["tsa_key"].sign(
        signing_payload, ec.ECDSA(hashes.SHA256())
    )
    signer = _sequence(
        _integer_der(1),
        _sequence(tsa_cert.issuer.public_bytes(), _integer_der(tsa_cert.serial_number)),
        algorithm,
        _context(0, signed_attrs),
        _sequence(_oid(_OID_ECDSA_SHA256)),
        _octet_string(signature),
    )
    certs = b"".join(
        cert.public_bytes(serialization.Encoding.DER)
        for cert in (
            tsa_cert,
            hierarchy["intermediate_cert"],
            hierarchy["root_cert"],
        )
    )
    signed_data = _sequence(
        _integer_der(3),
        _set(algorithm),
        _sequence(_oid(_OID_TST_INFO), _context(0, _octet_string(tst))),
        _context(0, certs),
        _set(signer),
    )
    return _sequence(_oid(_OID_SIGNED_DATA), _context(0, signed_data))



def _sign_plain(folder: Path, output_name: str = "signed.pdf") -> Path:
    source = folder / "plain.pdf"
    write_blank_pdf(source, pages=1)
    return source


def test_rfc3161_signing_inspection_and_trusted_time(tmp_path: Path) -> None:
    hierarchy = _hierarchy(tmp_path)
    crls = _write_crls(tmp_path, hierarchy)
    source = _sign_plain(tmp_path)
    fake = _FakeTsa(hierarchy)
    signed = tmp_path / "signed.pdf"

    result = sign_pdf(
        source,
        signed,
        pkcs12_path=tmp_path / "identity.pfx",
        pkcs12_password=PFX_PASSWORD,
        timestamp_url=TSA_URL,
        timestamp_opener=fake,
    )

    assert fake.calls
    assert fake.calls[0]["url"] == TSA_URL
    assert fake.calls[0]["content_type"] == "application/timestamp-query"
    assert fake.calls[0]["digest_oid"] == _OID_SHA256
    assert result.timestamp_status == "verified_untrusted"
    assert result.timestamp_time == fake.generation_time.isoformat()
    assert result.tsa_certificate_sha256 == hierarchy["tsa_cert"].fingerprint(
        hashes.SHA256()
    ).hex()
    assert PFX_PASSWORD not in json.dumps(result.summary())

    inspection = inspect_pdf_signatures(signed)
    signer = inspection.signatures[0].signers[0]
    assert signer.timestamp is not None
    assert signer.timestamp.status == "verified_untrusted"
    assert signer.timestamp.message_imprint_matches is True
    assert signer.timestamp.policy_oid == "1.2.3.4.30"
    assert signer.timestamp.signer_certificate is not None
    assert (
        signer.timestamp.signer_certificate.sha256_fingerprint
        == result.tsa_certificate_sha256
    )

    validation = validate_pdf_signature_trust(
        signed,
        trust_anchor_paths=[tmp_path / "root-ca.pem"],
        crl_paths=list(crls),
    )
    trusted_signer = validation.signatures[0].signers[0]
    assert validation.trusted_valid_count == 1
    assert trusted_signer.timestamp_trusted is True
    assert trusted_signer.timestamp_chain_status == "trusted"
    assert trusted_signer.timestamp_revocation_status == "not_revoked"
    assert trusted_signer.verification_time_source == "rfc3161_timestamp"
    assert trusted_signer.verification_time == fake.generation_time.isoformat()
    assert validation.summary()["trusted_timestamp_count"] == 1

    no_crl = validate_pdf_signature_trust(
        signed, trust_anchor_paths=[tmp_path / "root-ca.pem"]
    ).signatures[0].signers[0]
    assert no_crl.timestamp_trusted is False
    assert no_crl.verification_time_source == "cms_signing_time"

    other = _hierarchy(tmp_path / "other", prefix="Other")
    del other
    wrong_tsa_anchor = validate_pdf_signature_trust(
        signed,
        trust_anchor_paths=[tmp_path / "root-ca.pem"],
        crl_paths=list(crls),
        timestamp_trust_anchor_paths=[tmp_path / "other" / "root-ca.pem"],
    ).signatures[0].signers[0]
    assert wrong_tsa_anchor.timestamp_trusted is False
    assert wrong_tsa_anchor.timestamp_chain_status == "untrusted"
    assert wrong_tsa_anchor.verification_time_source == "cms_signing_time"


def test_rfc3161_bad_tsa_responses_never_publish(tmp_path: Path) -> None:
    hierarchy = _hierarchy(tmp_path)
    source = _sign_plain(tmp_path)

    wrong_output = tmp_path / "wrong-imprint.pdf"
    with pytest.raises(ToolError, match="did not verify"):
        sign_pdf(
            source,
            wrong_output,
            pkcs12_path=tmp_path / "identity.pfx",
            pkcs12_password=PFX_PASSWORD,
            timestamp_url=TSA_URL,
            timestamp_opener=_FakeTsa(hierarchy, wrong_imprint=True),
        )
    assert not wrong_output.exists()

    rejected_output = tmp_path / "rejected.pdf"
    with pytest.raises(ToolError, match="rejected"):
        sign_pdf(
            source,
            rejected_output,
            pkcs12_path=tmp_path / "identity.pfx",
            pkcs12_password=PFX_PASSWORD,
            timestamp_url=TSA_URL,
            timestamp_opener=_FakeTsa(hierarchy, reject=True),
        )
    assert not rejected_output.exists()

    # Exercise the transport function directly for the HTTPS-only rule with a
    # one-signer base CMS produced by an untimestamped output.
    base = tmp_path / "base.pdf"
    sign_pdf(
        source,
        base,
        pkcs12_path=tmp_path / "identity.pfx",
        pkcs12_password=PFX_PASSWORD,
    )
    contents = inspect_pdf_signatures(base)
    assert contents.valid_count == 1
    http_output = tmp_path / "http.pdf"
    with pytest.raises(ToolError, match="must use HTTPS"):
        sign_pdf(
            source,
            http_output,
            pkcs12_path=tmp_path / "identity.pfx",
            pkcs12_password=PFX_PASSWORD,
            timestamp_url="http://8.8.8.8/timestamp",
            timestamp_opener=_FakeTsa(hierarchy),
        )
    assert not http_output.exists()


def test_rfc3161_document_timestamp_inspection_and_tamper(tmp_path: Path) -> None:
    hierarchy = _hierarchy(tmp_path)
    path = tmp_path / "doc-timestamp.pdf"
    writer = PdfWriter()
    try:
        page = writer.add_blank_page(width=612, height=792)
        placeholder = NumberObject(9999999999)
        signature = DictionaryObject()
        signature.update(
            {
                NameObject("/Type"): NameObject("/DocTimeStamp"),
                NameObject("/Filter"): NameObject("/Adobe.PPKLite"),
                NameObject("/SubFilter"): NameObject("/ETSI.RFC3161"),
                NameObject("/ByteRange"): ArrayObject(
                    [NumberObject(0), placeholder, placeholder, placeholder]
                ),
                NameObject("/Contents"): ByteStringObject(b"\x00" * 8192),
            }
        )
        signature_ref = writer._add_object(signature)
        field = DictionaryObject()
        field.update(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Widget"),
                NameObject("/Rect"): RectangleObject([0, 0, 0, 0]),
                NameObject("/F"): NumberObject(4),
                NameObject("/FT"): NameObject("/Sig"),
                NameObject("/T"): TextStringObject("DocTimestamp"),
                NameObject("/V"): signature_ref,
            }
        )
        field_ref = writer._add_object(field)
        page[NameObject("/Annots")] = ArrayObject([field_ref])
        acro_form = DictionaryObject()
        acro_form.update(
            {
                NameObject("/Fields"): ArrayObject([field_ref]),
                NameObject("/SigFlags"): NumberObject(3),
            }
        )
        writer._root_object[NameObject("/AcroForm")] = writer._add_object(acro_form)
        with path.open("wb") as handle:
            writer.write(handle)
    finally:
        writer.close()

    data = bytearray(path.read_bytes())
    contents_token = data.find(b"/Contents")
    hex_start = data.find(b"<", contents_token)
    hex_end = data.find(b">", hex_start)
    second_start = hex_end + 1
    match = re.search(
        rb"/ByteRange\s*\[\s*0\s+(\d{10})\s+(\d{10})\s+(\d{10})\s*\]",
        bytes(data),
    )
    assert match is not None
    for group, value in zip(
        (1, 2, 3),
        (hex_start, second_start, len(data) - second_start),
        strict=True,
    ):
        start, end = match.span(group)
        data[start:end] = f"{value:010d}".encode()
    signed_bytes = bytes(data[:hex_start]) + bytes(data[second_start:])
    token = _timestamp_token(
        signed_bytes,
        hierarchy,
        generation_time=datetime.now(UTC).replace(microsecond=0),
    )
    hexed = token.hex().encode()
    capacity = hex_end - hex_start - 1
    assert len(hexed) <= capacity
    data[hex_start + 1 : hex_end] = hexed + b"0" * (capacity - len(hexed))
    path.write_bytes(data)

    inspection = inspect_pdf_signatures(path)
    result = inspection.signatures[0]
    assert result.status == "cryptographically_valid_untrusted"
    assert result.signers == ()
    assert result.document_timestamp is not None
    assert result.document_timestamp.status == "verified_untrusted"
    assert result.document_timestamp.message_imprint_matches is True

    tampered = tmp_path / "doc-timestamp-tampered.pdf"
    changed = bytearray(path.read_bytes())
    changed[10] ^= 1
    tampered.write_bytes(changed)
    invalid = inspect_pdf_signatures(tampered).signatures[0]
    assert invalid.status == "invalid"
    assert invalid.document_timestamp is not None
    assert invalid.document_timestamp.message_imprint_matches is False



def _fake_getpass(monkeypatch: pytest.MonkeyPatch, values: list[str]) -> None:
    iterator = iter(values)
    monkeypatch.setattr(
        cli_module.getpass, "getpass", lambda _prompt="": next(iterator)
    )


def test_rfc3161_direct_cli_and_gui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    workspace = settings.workspace_dir
    hierarchy = _hierarchy(workspace)
    _write_crls(workspace, hierarchy)
    _sign_plain(workspace)
    fake = _FakeTsa(hierarchy)
    actual_request = request_timestamp_for_cms
    monkeypatch.setattr(
        signing_module,
        "request_timestamp_for_cms",
        lambda cms, **kwargs: actual_request(
            cms, **{**kwargs, "opener": fake}
        ),
    )
    _fake_getpass(monkeypatch, [PFX_PASSWORD])
    root = ["--root", str(tmp_path)]

    assert (
        main(
            [
                *root,
                "pdf",
                "sign",
                "plain.pdf",
                "--output",
                "cli-signed.pdf",
                "--pkcs12",
                "identity.pfx",
                "--timestamp-url",
                TSA_URL,
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["output"]["timestamp_status"] == "verified_untrusted"
    assert payload["output"]["tsa_trust_status"] == "not_evaluated"
    assert PFX_PASSWORD not in json.dumps(payload)

    assert (
        main(
            [
                *root,
                "pdf",
                "validate-signatures",
                "cli-signed.pdf",
                "--trust-anchor",
                "root-ca.pem",
                "--timestamp-trust-anchor",
                "root-ca.pem",
                "--crl",
                "leaf-issuer.crl",
                "--crl",
                "intermediate-issuer.crl",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    signer = payload["output"]["signatures"][0]["signers"][0]
    assert signer["timestamp_trusted"] is True
    assert signer["verification_time_source"] == "rfc3161_timestamp"
    assert payload["output"]["trusted_timestamp_count"] == 1

    controller = GuiController(settings)
    gui_fake = _FakeTsa(hierarchy)
    signed = controller.pdf_sign(
        "plain.pdf",
        "gui-signed.pdf",
        "identity.pfx",
        password=PFX_PASSWORD,
        timestamp_url=TSA_URL,
        timestamp_opener=gui_fake,
    )
    assert signed.success, signed.error
    assert signed.output is not None
    assert signed.output["timestamp_status"] == "verified_untrusted"

    validated = controller.pdf_validate_signatures(
        "gui-signed.pdf",
        "root-ca.pem",
        "leaf-issuer.crl\nintermediate-issuer.crl",
        "root-ca.pem",
    )
    assert validated.success, validated.error
    assert validated.output is not None
    assert validated.output["trusted_timestamp_count"] == 1

    with pytest.raises(PcAgentError, match="must stay inside the workspace"):
        controller.pdf_validate_signatures(
            "gui-signed.pdf",
            "root-ca.pem",
            "leaf-issuer.crl\nintermediate-issuer.crl",
            "../outside.pem",
        )

    summary = controller.doctor_summary()
    assert summary.version == __version__ == "0.41.0"
    assert summary.builtin_tools == 62
    assert "PHASE 39 Secure .env Key Cleanup" in summary.to_text()


def test_phase30_scope_docs_schema_and_no_registered_timestamp_tool() -> None:
    project_root = Path(__file__).resolve().parents[1]
    candidates = {path.as_posix() for path in release_candidate_files(project_root)}
    ids = {item.spec.id for item in build_default_registry().list_tools()}

    assert __version__ == "0.41.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(ids) == 62
    assert "pdf.timestamp" not in ids
    assert "direct.pdf_sign" not in ids
    assert "src/pc_agent/documents/pdf_timestamps.py" in candidates
    assert "docs/phase30.md" in candidates
    assert "tests/test_phase30_rfc3161.py" in candidates

    phase_doc = (project_root / "docs" / "phase30.md").read_text(encoding="utf-8")
    lowered = phase_doc.lower()
    assert "no registered-tool" in lowered or "registered tool" in lowered
    assert "does **not** receive the pdf" in lowered
    assert "rfc3161_timestamp" in lowered
    assert "legal acceptance" in lowered and "not evaluated" in lowered
    assert "no default tsa" in lowered


