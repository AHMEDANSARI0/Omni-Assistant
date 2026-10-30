"""Phase 34: incremental multi-signature PDF signing."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    RectangleObject,
    TextStringObject,
)

import pc_agent.cli as cli_module
from pc_agent import __version__
from pc_agent.cli import build_parser, main
from pc_agent.config import Settings
from pc_agent.documents.pdf import write_blank_pdf
from pc_agent.documents.pdf_signatures import inspect_pdf_signatures
from pc_agent.documents.pdf_signing import sign_pdf
from pc_agent.errors import ToolError
from pc_agent.gui.controller import GuiController
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

PFX_PASSWORD = "pfx-secret-phase34"


def _write_identity(path: Path, common_name: str, serial: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Phase 34 Test"),
        ]
    )
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(
        pkcs12.serialize_key_and_certificates(
            common_name.encode(),
            key,
            certificate,
            None,
            serialization.BestAvailableEncryption(PFX_PASSWORD.encode()),
        )
    )
    return certificate.fingerprint(hashes.SHA256()).hex()


def _write_blank_signature_fields(path: Path, names: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    try:
        page = writer.add_blank_page(width=612, height=792)
        fields: list[object] = []
        annotations: list[object] = []
        for index, field_name in enumerate(names, start=1):
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
                    NameObject("/T"): TextStringObject(field_name),
                }
            )
            reference = writer._add_object(field)
            fields.append(reference)
            annotations.append(reference)
        page[NameObject("/Annots")] = ArrayObject(annotations)
        acro_form = DictionaryObject()
        acro_form.update(
            {
                NameObject("/Fields"): ArrayObject(fields),
                NameObject("/SigFlags"): NumberObject(0),
            }
        )
        writer._root_object[NameObject("/AcroForm")] = writer._add_object(
            acro_form
        )
        with path.open("wb") as handle:
            writer.write(handle)
    finally:
        writer.close()


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _signature(path: Path, field_name: str):
    return next(
        item
        for item in inspect_pdf_signatures(path).signatures
        if item.field_name == field_name
    )


def test_incremental_signing_preserves_two_previous_revisions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "plain.pdf"
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    third = tmp_path / "third.pdf"
    write_blank_pdf(source, pages=1)
    fingerprints = [
        _write_identity(tmp_path / f"signer-{index}.p12", f"Signer {index}", 3400 + index)
        for index in (1, 2, 3)
    ]

    first_result = sign_pdf(
        source,
        first,
        pkcs12_path=tmp_path / "signer-1.p12",
        pkcs12_password=PFX_PASSWORD,
        field_name="FirstApproval",
        create_field=True,
        rectangle=(36, 36, 200, 40),
    )
    first_bytes = first.read_bytes()
    first_hash = _hash(first)
    first_signature = _signature(first, "FirstApproval")

    second_result = sign_pdf(
        first,
        second,
        pkcs12_path=tmp_path / "signer-2.p12",
        pkcs12_password=PFX_PASSWORD,
        field_name="SecondApproval",
        create_field=True,
        rectangle=(36, 100, 200, 40),
    )
    second_bytes = second.read_bytes()
    second_hash = _hash(second)

    third_result = sign_pdf(
        second,
        third,
        pkcs12_path=tmp_path / "signer-3.p12",
        pkcs12_password=PFX_PASSWORD,
        field_name="ThirdApproval",
        create_field=True,
        rectangle=(36, 164, 200, 40),
    )
    third_bytes = third.read_bytes()

    assert first_result.incremental_update is False
    assert second_result.summary()["incremental_update"] is True
    assert second_result.existing_signature_count == 1
    assert second_result.preserved_valid_signature_count == 1
    assert second_result.total_signature_count == 2
    assert second_result.source_revision_bytes == len(first_bytes)
    assert third_result.existing_signature_count == 2
    assert third_result.preserved_valid_signature_count == 2
    assert third_result.total_signature_count == 3
    assert third_result.source_revision_bytes == len(second_bytes)
    assert _hash(first) == first_hash
    assert _hash(second) == second_hash
    assert second_bytes.startswith(first_bytes)
    assert third_bytes.startswith(second_bytes)

    inspection = inspect_pdf_signatures(third)
    assert inspection.signed_count == 3
    assert inspection.valid_count == 3
    by_name = {item.field_name: item for item in inspection.signatures}
    assert by_name["FirstApproval"].byte_range == first_signature.byte_range
    assert by_name["FirstApproval"].covers_whole_file is False
    assert by_name["FirstApproval"].unsigned_trailing_bytes == (
        len(third_bytes) - len(first_bytes)
    )
    assert by_name["SecondApproval"].covers_whole_file is False
    assert by_name["ThirdApproval"].covers_whole_file is True
    for field_name, fingerprint in zip(
        ("FirstApproval", "SecondApproval", "ThirdApproval"),
        fingerprints,
        strict=True,
    ):
        certificate = by_name[field_name].signers[0].certificate
        assert certificate is not None
        assert certificate.sha256_fingerprint == fingerprint
    assert PFX_PASSWORD not in json.dumps(third_result.summary())


def test_incremental_signing_docmdp_permissions(tmp_path: Path) -> None:
    identity_a = tmp_path / "a.p12"
    identity_b = tmp_path / "b.p12"
    _write_identity(identity_a, "DocMDP A", 3411)
    _write_identity(identity_b, "DocMDP B", 3412)

    form_p2 = tmp_path / "form-p2.pdf"
    certified_p2 = tmp_path / "certified-p2.pdf"
    approved_p2 = tmp_path / "approved-p2.pdf"
    _write_blank_signature_fields(form_p2, ("Signer1", "Signer2"))
    sign_pdf(
        form_p2,
        certified_p2,
        pkcs12_path=identity_a,
        pkcs12_password=PFX_PASSWORD,
        field_name="Signer1",
        certify_permission=2,
    )
    p2_result = sign_pdf(
        certified_p2,
        approved_p2,
        pkcs12_path=identity_b,
        pkcs12_password=PFX_PASSWORD,
        field_name="Signer2",
    )
    assert p2_result.incremental_update is True
    assert p2_result.created_field is False
    assert inspect_pdf_signatures(approved_p2).valid_count == 2
    assert approved_p2.read_bytes().startswith(certified_p2.read_bytes())

    refused_p2 = tmp_path / "refused-p2.pdf"
    with pytest.raises(ToolError, match="P=2 permits signing an existing"):
        sign_pdf(
            certified_p2,
            refused_p2,
            pkcs12_path=identity_b,
            pkcs12_password=PFX_PASSWORD,
            field_name="NewSignature",
            create_field=True,
        )
    assert not refused_p2.exists()

    form_p1 = tmp_path / "form-p1.pdf"
    certified_p1 = tmp_path / "certified-p1.pdf"
    _write_blank_signature_fields(form_p1, ("Signer1", "Signer2"))
    sign_pdf(
        form_p1,
        certified_p1,
        pkcs12_path=identity_a,
        pkcs12_password=PFX_PASSWORD,
        field_name="Signer1",
        certify_permission=1,
    )
    refused_p1 = tmp_path / "refused-p1.pdf"
    with pytest.raises(ToolError, match="P=1 prohibits"):
        sign_pdf(
            certified_p1,
            refused_p1,
            pkcs12_path=identity_b,
            pkcs12_password=PFX_PASSWORD,
            field_name="Signer2",
        )
    assert not refused_p1.exists()

    plain = tmp_path / "plain-p3.pdf"
    certified_p3 = tmp_path / "certified-p3.pdf"
    approved_p3 = tmp_path / "approved-p3.pdf"
    write_blank_pdf(plain, pages=1)
    sign_pdf(
        plain,
        certified_p3,
        pkcs12_path=identity_a,
        pkcs12_password=PFX_PASSWORD,
        field_name="Certification",
        create_field=True,
        certify_permission=3,
    )
    p3_result = sign_pdf(
        certified_p3,
        approved_p3,
        pkcs12_path=identity_b,
        pkcs12_password=PFX_PASSWORD,
        field_name="Approval",
        create_field=True,
        rectangle=(36, 100, 200, 40),
    )
    assert p3_result.created_field is True
    assert inspect_pdf_signatures(approved_p3).valid_count == 2


def test_incremental_signing_refusals_do_not_publish(tmp_path: Path) -> None:
    source = tmp_path / "plain.pdf"
    signed = tmp_path / "signed.pdf"
    identity = tmp_path / "identity.p12"
    write_blank_pdf(source, pages=1)
    _write_identity(identity, "Refusal Signer", 3421)
    sign_pdf(
        source,
        signed,
        pkcs12_path=identity,
        pkcs12_password=PFX_PASSWORD,
        field_name="Approval",
        create_field=True,
    )

    same_field = tmp_path / "same-field.pdf"
    with pytest.raises(ToolError, match="already signed"):
        sign_pdf(
            signed,
            same_field,
            pkcs12_path=identity,
            pkcs12_password=PFX_PASSWORD,
            field_name="Approval",
            create_field=True,
        )
    assert not same_field.exists()

    certify_late = tmp_path / "certify-late.pdf"
    with pytest.raises(ToolError, match="must be the first signature"):
        sign_pdf(
            signed,
            certify_late,
            pkcs12_path=identity,
            pkcs12_password=PFX_PASSWORD,
            field_name="Second",
            create_field=True,
            certify_permission=2,
        )
    assert not certify_late.exists()

    tampered = tmp_path / "tampered.pdf"
    data = bytearray(signed.read_bytes())
    data[10] ^= 1  # PDF binary-comment byte inside the first ByteRange.
    tampered.write_bytes(data)
    assert inspect_pdf_signatures(tampered).invalid_count == 1
    refused = tmp_path / "tampered-second.pdf"
    with pytest.raises(ToolError, match="requires every existing"):
        sign_pdf(
            tampered,
            refused,
            pkcs12_path=identity,
            pkcs12_password=PFX_PASSWORD,
            field_name="Second",
            create_field=True,
        )
    assert not refused.exists()


def _fake_getpass(monkeypatch: pytest.MonkeyPatch, values: list[str]) -> list[str]:
    prompts: list[str] = []
    iterator = iter(values)

    def fake(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(iterator)

    monkeypatch.setattr(cli_module.getpass, "getpass", fake)
    return prompts


def test_incremental_signing_direct_cli_and_gui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    workspace = settings.workspace_dir
    write_blank_pdf(workspace / "plain.pdf", pages=1)
    _write_identity(workspace / "first.p12", "CLI First", 3431)
    _write_identity(workspace / "second.p12", "GUI Second", 3432)
    root = ["--root", str(tmp_path)]

    prompts = _fake_getpass(monkeypatch, [PFX_PASSWORD, PFX_PASSWORD])
    assert main(
        [
            *root,
            "pdf",
            "sign",
            "plain.pdf",
            "--output",
            "first.pdf",
            "--pkcs12",
            "first.p12",
            "--field",
            "First",
            "--create-field",
            "--json",
        ]
    ) == 0
    first_payload = json.loads(capsys.readouterr().out)
    assert first_payload["output"]["incremental_update"] is False

    assert main(
        [
            *root,
            "pdf",
            "sign",
            "first.pdf",
            "--output",
            "second.pdf",
            "--pkcs12",
            "second.p12",
            "--field",
            "Second",
            "--create-field",
            "--rect",
            "36,100,200,40",
            "--json",
        ]
    ) == 0
    captured = capsys.readouterr()
    second_payload = json.loads(captured.out)
    assert prompts == ["PKCS#12 password: ", "PKCS#12 password: "]
    assert second_payload["output"]["incremental_update"] is True
    assert second_payload["output"]["preserved_valid_signature_count"] == 1
    assert second_payload["output"]["total_signature_count"] == 2
    assert PFX_PASSWORD not in captured.out
    assert PFX_PASSWORD not in captured.err

    controller = GuiController(settings)
    result = controller.pdf_sign(
        "second.pdf",
        "third.pdf",
        "first.p12",
        password=PFX_PASSWORD,
        field_name="Third",
        create_field=True,
        rectangle=(36, 164, 200, 40),
    )
    assert result.success, result.error
    assert result.tool_id == "direct.pdf_sign"
    assert result.output is not None
    assert result.output["incremental_update"] is True
    assert result.output["preserved_valid_signature_count"] == 2
    assert result.output["total_signature_count"] == 3
    assert PFX_PASSWORD not in json.dumps(result.output)

    summary = controller.doctor_summary()
    assert summary.version == __version__ == "0.41.0"
    assert summary.builtin_tools == 62
    assert "PHASE 39 Secure .env Key Cleanup" in summary.to_text()


def test_phase34_scope_defaults_docs_and_no_registered_tool() -> None:
    project_root = Path(__file__).resolve().parents[1]
    candidates = {path.as_posix() for path in release_candidate_files(project_root)}
    ids = {item.spec.id for item in build_default_registry().list_tools()}
    args = build_parser().parse_args(
        [
            "pdf",
            "sign",
            "signed.pdf",
            "--output",
            "next.pdf",
            "--pkcs12",
            "identity.pfx",
        ]
    )

    assert __version__ == "0.41.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(ids) == 62
    assert args.certify is None
    assert "pdf.sign" not in ids
    assert "direct.pdf_sign" not in ids
    assert "docs/phase34.md" in candidates
    assert "tests/test_phase34_incremental_signing.py" in candidates

    app_source = (project_root / "src/pc_agent/gui/app.py").read_text(
        encoding="utf-8"
    )
    assert "incremental update" in app_source
    assert "preserved byte-for-byte" in app_source

    phase_doc = (project_root / "docs/phase34.md").read_text(encoding="utf-8")
    lowered = phase_doc.lower()
    assert "exact byte prefix" in lowered
    assert "docmdp" in lowered
    assert "no registered tool" in lowered
    assert "doctimestamp-only" in lowered
    assert "not** create" in lowered
