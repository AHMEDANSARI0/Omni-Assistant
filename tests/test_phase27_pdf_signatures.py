"""Phase 27: PDF digital-signature inspection and CMS verification."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID
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

from pc_agent import __version__
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.documents.pdf import write_blank_pdf
from pc_agent.documents.pdf_signatures import inspect_pdf_signatures
from pc_agent.errors import PcAgentError, ToolError
from pc_agent.gui.controller import GuiController
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry


def _write_signed_pdf(
    path: Path,
    *,
    signed: bool = True,
    sub_filter: str = "adbe.pkcs7.detached",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "Phase 27 Signer"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Local Test"),
        ]
    )
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(0x1234)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )

    writer = PdfWriter()
    try:
        page = writer.add_blank_page(width=612, height=792)
        annotations: list[object] = []
        fields: list[object] = []

        signature_ref = None
        if signed:
            placeholder = NumberObject(9999999999)
            signature = DictionaryObject()
            signature.update(
                {
                    NameObject("/Type"): NameObject("/Sig"),
                    NameObject("/Filter"): NameObject("/Adobe.PPKLite"),
                    NameObject("/SubFilter"): NameObject(f"/{sub_filter}"),
                    NameObject("/ByteRange"): ArrayObject(
                        [NumberObject(0), placeholder, placeholder, placeholder]
                    ),
                    NameObject("/Contents"): ByteStringObject(b"\x00" * 8192),
                    NameObject("/Name"): TextStringObject("Phase 27 Signer"),
                    NameObject("/Reason"): TextStringObject("Fixture approval"),
                    NameObject("/Location"): TextStringObject("Karachi"),
                    NameObject("/M"): TextStringObject("D:20261029130000Z"),
                }
            )
            params = DictionaryObject()
            params.update(
                {
                    NameObject("/Type"): NameObject("/TransformParams"),
                    NameObject("/P"): NumberObject(2),
                    NameObject("/V"): TextStringObject("1.2"),
                }
            )
            reference = DictionaryObject()
            reference.update(
                {
                    NameObject("/Type"): NameObject("/SigRef"),
                    NameObject("/TransformMethod"): NameObject("/DocMDP"),
                    NameObject("/DigestMethod"): NameObject("/SHA256"),
                    NameObject("/TransformParams"): params,
                }
            )
            signature[NameObject("/Reference")] = ArrayObject([reference])
            signature_ref = writer._add_object(signature)

        for index, include_value in enumerate((signed, False), start=1):
            field = DictionaryObject()
            field.update(
                {
                    NameObject("/Type"): NameObject("/Annot"),
                    NameObject("/Subtype"): NameObject("/Widget"),
                    NameObject("/Rect"): RectangleObject(
                        [60, 60 + index * 50, 300, 100 + index * 50]
                    ),
                    NameObject("/F"): NumberObject(4),
                    NameObject("/FT"): NameObject("/Sig"),
                    NameObject("/T"): TextStringObject(f"Signature{index}"),
                }
            )
            if include_value and signature_ref is not None:
                field[NameObject("/V")] = signature_ref
            reference = writer._add_object(field)
            annotations.append(reference)
            fields.append(reference)

        page[NameObject("/Annots")] = ArrayObject(annotations)
        acro_form = DictionaryObject()
        acro_form.update(
            {
                NameObject("/Fields"): ArrayObject(fields),
                NameObject("/SigFlags"): NumberObject(3 if signed else 0),
            }
        )
        roots: dict[NameObject, object] = {
            NameObject("/AcroForm"): writer._add_object(acro_form)
        }
        if signature_ref is not None:
            perms = DictionaryObject()
            perms[NameObject("/DocMDP")] = signature_ref
            roots[NameObject("/Perms")] = writer._add_object(perms)
        writer._root_object.update(roots)
        with path.open("wb") as handle:
            writer.write(handle)
    finally:
        writer.close()

    if not signed:
        return

    data = bytearray(path.read_bytes())
    contents_token = data.find(b"/Contents")
    hex_start = data.find(b"<", contents_token)
    hex_end = data.find(b">", hex_start)
    first_length = hex_start
    second_start = hex_end + 1
    second_length = len(data) - second_start
    match = re.search(
        rb"/ByteRange\s*\[\s*0\s+(\d{10})\s+(\d{10})\s+(\d{10})\s*\]",
        bytes(data),
    )
    assert match is not None
    for group, value in zip(
        (1, 2, 3), (first_length, second_start, second_length), strict=True
    ):
        start, end = match.span(group)
        data[start:end] = f"{value:010d}".encode("ascii")

    signed_bytes = bytes(data[:first_length]) + bytes(data[second_start:])
    der = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(signed_bytes)
        .add_signer(certificate, key, hashes.SHA256())
        .sign(
            serialization.Encoding.DER,
            [
                pkcs7.PKCS7Options.Binary,
                pkcs7.PKCS7Options.DetachedSignature,
            ],
        )
    )
    hexed = der.hex().encode("ascii")
    capacity = hex_end - hex_start - 1
    assert len(hexed) <= capacity
    data[hex_start + 1 : hex_end] = hexed + b"0" * (capacity - len(hexed))
    path.write_bytes(data)


def _tamper(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[10] ^= 1  # The PDF binary comment is inside the signed first range.
    path.write_bytes(data)


def _corrupt_cms(path: Path) -> None:
    data = bytearray(path.read_bytes())
    contents_token = data.find(b"/Contents")
    hex_start = data.find(b"<", contents_token)
    data[hex_start + 1 : hex_start + 3] = b"FF"
    path.write_bytes(data)


def test_pdf_signature_core_valid_tamper_malformed_and_unsigned(
    tmp_path: Path,
) -> None:
    signed = tmp_path / "signed.pdf"
    _write_signed_pdf(signed)

    inspection = inspect_pdf_signatures(signed)
    assert inspection.pages == 1
    assert inspection.signature_count == 2
    assert inspection.signed_count == 1
    assert inspection.valid_count == 1
    assert inspection.invalid_count == 0
    assert inspection.unsigned_count == 1
    assert inspection.signature_flags == 3

    fields = {item.field_name: item for item in inspection.signatures}
    valid = fields["Signature1"]
    assert valid.status == "cryptographically_valid_untrusted"
    assert valid.signed and valid.page == 1 and valid.widget_count == 1
    assert valid.signer_name == "Phase 27 Signer"
    assert valid.reason == "Fixture approval"
    assert valid.location == "Karachi"
    assert valid.signature_date == "2026-10-29T13:00:00+00:00"
    assert valid.sub_filter == "adbe.pkcs7.detached"
    assert valid.byte_range is not None and valid.byte_range[0] == 0
    assert valid.covers_whole_file is True
    assert valid.unsigned_trailing_bytes == 0
    assert valid.docmdp_permission == 2
    assert len(valid.signers) == 1
    signer = valid.signers[0]
    assert signer.digest_algorithm == "sha256"
    assert signer.digest_matches is True
    assert signer.signature_valid is True
    assert signer.certificate_valid_at_signing is True
    assert signer.certificate is not None
    assert signer.certificate.subject.endswith("CN=Phase 27 Signer")
    assert signer.certificate.serial_number == "1234"
    assert signer.certificate.self_signed is True
    assert signer.certificate.key_type == "RSA"
    assert valid.certificates[0].sha256_fingerprint == (
        signer.certificate.sha256_fingerprint
    )
    assert 'trust_status": "not_evaluated"' in json.dumps(inspection.summary())

    assert fields["Signature2"].status == "unsigned"

    tampered = tmp_path / "tampered.pdf"
    _write_signed_pdf(tampered)
    _tamper(tampered)
    tampered_result = inspect_pdf_signatures(tampered).signatures[0]
    assert tampered_result.status == "invalid"
    assert tampered_result.signers[0].digest_matches is False

    malformed = tmp_path / "malformed.pdf"
    _write_signed_pdf(malformed)
    _corrupt_cms(malformed)
    assert inspect_pdf_signatures(malformed).signatures[0].status == "malformed"

    unsupported = tmp_path / "unsupported.pdf"
    _write_signed_pdf(unsupported, sub_filter="adbe.pkcs7.sha1")
    assert inspect_pdf_signatures(unsupported).signatures[0].status == "unsupported"

    unsigned = tmp_path / "unsigned.pdf"
    _write_signed_pdf(unsigned, signed=False)
    unsigned_result = inspect_pdf_signatures(unsigned)
    assert unsigned_result.signature_count == 2
    assert unsigned_result.unsigned_count == 2

    plain = tmp_path / "plain.pdf"
    write_blank_pdf(plain, pages=1)
    assert inspect_pdf_signatures(plain).signature_count == 0



def test_pdf_signature_encrypted_and_no_registered_tool_surface(
    tmp_path: Path,
) -> None:
    source = tmp_path / "plain.pdf"
    write_blank_pdf(source, pages=1)
    encrypted = tmp_path / "encrypted.pdf"
    writer = PdfWriter(clone_from=str(source))
    try:
        writer.encrypt("user-pass-123", "owner-pass-456", algorithm="AES-256")
        with encrypted.open("wb") as handle:
            writer.write(handle)
    finally:
        writer.close()
    with pytest.raises(ToolError, match="must be unprotected"):
        inspect_pdf_signatures(encrypted)

    ids = {tool.spec.id for tool in build_default_registry().list_tools()}
    assert len(ids) == 62
    assert "pdf.inspect_signatures" not in ids
    assert "direct.pdf_inspect_signatures" not in ids


def test_pdf_signature_direct_cli_text_json_and_invalid_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    _write_signed_pdf(workspace / "signed.pdf")
    _write_signed_pdf(workspace / "tampered.pdf")
    _tamper(workspace / "tampered.pdf")
    root = ["--root", str(tmp_path)]

    assert main([*root, "pdf", "inspect-signatures", "signed.pdf"]) == 0
    text = capsys.readouterr().out
    assert "cryptographically_valid_untrusted" in text
    assert "Trust/revocation: not evaluated" in text
    assert "Phase 27 Signer" in text

    assert main([*root, "pdf", "inspect-signatures", "signed.pdf", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["output"]["valid_count"] == 1
    assert payload["output"]["trust_status"] == "not_evaluated"

    assert main([*root, "pdf", "inspect-signatures", "tampered.pdf", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert payload["output"]["invalid_count"] == 1

    assert main([*root, "pdf", "inspect-signatures", "../outside.pdf", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert "must stay inside the workspace" in payload["error"]


def test_pdf_signature_gui_controller_direct_method(tmp_path: Path) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    _write_signed_pdf(settings.workspace_dir / "signed.pdf")
    _write_signed_pdf(settings.workspace_dir / "tampered.pdf")
    _tamper(settings.workspace_dir / "tampered.pdf")
    controller = GuiController(settings)

    result = controller.pdf_inspect_signatures("signed.pdf")
    assert result.success, result.error
    assert result.tool_id == "direct.pdf_inspect_signatures"
    assert result.output is not None
    assert result.output["valid_count"] == 1
    assert result.output["trust_status"] == "not_evaluated"

    invalid = controller.pdf_inspect_signatures("tampered.pdf")
    assert invalid.success is False
    assert invalid.output is not None
    assert invalid.output["invalid_count"] == 1

    with pytest.raises(PcAgentError, match="must stay inside the workspace"):
        controller.pdf_inspect_signatures("../outside.pdf")

    summary = controller.doctor_summary()
    assert summary.version == __version__ == "0.29.0"
    assert summary.builtin_tools == 62
    assert "PHASE 27 PDF Signature Inspection" in summary.to_text()


def test_phase27_scope_docs_schema_and_release_candidates() -> None:
    project_root = Path(__file__).resolve().parents[1]
    candidates = {path.as_posix() for path in release_candidate_files(project_root)}
    ids = {item.spec.id for item in build_default_registry().list_tools()}

    assert __version__ == "0.29.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(ids) == 62
    assert "src/pc_agent/documents/pdf_signatures.py" in candidates
    assert "docs/phase27.md" in candidates
    assert "tests/test_phase27_pdf_signatures.py" in candidates

    phase_doc = (project_root / "docs" / "phase27.md").read_text(encoding="utf-8")
    lowered = phase_doc.lower()
    assert "no registered tool" in lowered
    assert "trust was not evaluated" in lowered
    assert "does not create" in lowered
    assert "byterange" in lowered

