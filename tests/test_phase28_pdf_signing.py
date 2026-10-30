"""Phase 28: direct-only PDF digital-signature creation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
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
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.documents.pdf import write_blank_pdf
from pc_agent.documents.pdf_security import protect_pdf
from pc_agent.documents.pdf_signatures import inspect_pdf_signatures
from pc_agent.documents.pdf_signing import sign_pdf
from pc_agent.errors import PcAgentError, ToolError
from pc_agent.gui.controller import GuiController
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

PFX_PASSWORD = "pfx-secret-phase28"


def _write_identity(
    path: Path,
    *,
    key_type: str = "rsa",
    not_before_offset: int = -1,
    not_after_offset: int = 30,
    digital_signature_usage: bool = True,
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if key_type == "ec":
        key = ec.generate_private_key(ec.SECP256R1())
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "Phase 28 Signer"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Local Test"),
        ]
    )
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(0x28)
        .not_valid_before(now + timedelta(days=not_before_offset))
        .not_valid_after(now + timedelta(days=not_after_offset))
        .add_extension(
            x509.KeyUsage(
                digital_signature=digital_signature_usage,
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
            b"Phase 28 Signer",
            key,
            certificate,
            None,
            serialization.BestAvailableEncryption(PFX_PASSWORD.encode()),
        )
    )
    return certificate.fingerprint(hashes.SHA256()).hex()


def _write_blank_signature_fields(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    try:
        page = writer.add_blank_page(width=612, height=792)
        fields: list[object] = []
        annotations: list[object] = []
        for index in (1, 2):
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
                    NameObject("/T"): TextStringObject(f"Signer{index}"),
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


def test_pdf_signing_creates_visible_certified_verified_signature(
    tmp_path: Path,
) -> None:
    source = tmp_path / "plain.pdf"
    output = tmp_path / "signed.pdf"
    identity = tmp_path / "identity.pfx"
    write_blank_pdf(source, pages=2)
    fingerprint = _write_identity(identity)
    before = _hash(source)

    result = sign_pdf(
        source,
        output,
        pkcs12_path=identity,
        pkcs12_password=PFX_PASSWORD,
        field_name="ApprovalSignature",
        create_field=True,
        page=2,
        rectangle=(36, 36, 220, 44),
        reason="Approved for release",
        location="Karachi",
        contact_info="signer@example.invalid",
        certify_permission=2,
    )

    assert _hash(source) == before
    assert result.field_name == "ApprovalSignature"
    assert result.created_field is True
    assert result.page == 2
    assert result.certificate_sha256 == fingerprint
    assert result.status == "cryptographically_valid_untrusted"
    assert result.summary()["trust_status"] == "not_evaluated"
    assert PFX_PASSWORD not in json.dumps(result.summary())

    inspection = inspect_pdf_signatures(output)
    assert inspection.valid_count == 1
    signature = inspection.signatures[0]
    assert signature.field_name == "ApprovalSignature"
    assert signature.reason == "Approved for release"
    assert signature.location == "Karachi"
    assert signature.contact_info == "signer@example.invalid"
    assert signature.docmdp_permission == 2
    assert signature.covers_whole_file is True
    assert signature.signers[0].digest_matches is True
    assert signature.signers[0].signature_valid is True
    assert signature.signers[0].certificate is not None
    assert (
        signature.signers[0].certificate.sha256_fingerprint
        == fingerprint
    )


def test_pdf_signing_uses_existing_field_and_supports_ec_sha384(
    tmp_path: Path,
) -> None:
    source = tmp_path / "form.pdf"
    output = tmp_path / "form-signed.pdf"
    identity = tmp_path / "identity-ec.p12"
    _write_blank_signature_fields(source)
    fingerprint = _write_identity(identity, key_type="ec")

    result = sign_pdf(
        source,
        output,
        pkcs12_path=identity,
        pkcs12_password=PFX_PASSWORD,
        field_name="Signer2",
        hash_algorithm="sha384",
    )

    assert result.created_field is False
    assert result.field_name == "Signer2"
    assert result.certificate_sha256 == fingerprint
    inspection = inspect_pdf_signatures(output)
    by_name = {item.field_name: item for item in inspection.signatures}
    assert by_name["Signer1"].status == "unsigned"
    assert by_name["Signer2"].status == "cryptographically_valid_untrusted"
    assert by_name["Signer2"].signers[0].digest_algorithm == "sha384"
    assert by_name["Signer2"].signers[0].certificate is not None
    assert by_name["Signer2"].signers[0].certificate.key_type == "EC"



def test_pdf_signing_refusals_never_publish_output(tmp_path: Path) -> None:
    source = tmp_path / "plain.pdf"
    identity = tmp_path / "identity.pfx"
    write_blank_pdf(source, pages=1)
    _write_identity(identity)
    signed = tmp_path / "already-signed.pdf"
    sign_pdf(
        source,
        signed,
        pkcs12_path=identity,
        pkcs12_password=PFX_PASSWORD,
    )

    refused = tmp_path / "refused.pdf"
    with pytest.raises(ToolError, match="already signed"):
        sign_pdf(
            signed,
            refused,
            pkcs12_path=identity,
            pkcs12_password=PFX_PASSWORD,
            field_name="Signature1",
        )
    assert not refused.exists()

    encrypted = tmp_path / "encrypted.pdf"
    protect_pdf(
        source,
        encrypted,
        user_password="user-pass-phase28",
        owner_password="owner-pass-phase28",
    )
    with pytest.raises(ToolError, match="unprotected"):
        sign_pdf(
            encrypted,
            tmp_path / "encrypted-signed.pdf",
            pkcs12_path=identity,
            pkcs12_password=PFX_PASSWORD,
        )

    with pytest.raises(ToolError, match="Could not unlock"):
        sign_pdf(
            source,
            tmp_path / "wrong-password.pdf",
            pkcs12_path=identity,
            pkcs12_password="wrong-pfx-password",
        )
    assert not (tmp_path / "wrong-password.pdf").exists()

    expired_identity = tmp_path / "expired.pfx"
    _write_identity(
        expired_identity,
        not_before_offset=-10,
        not_after_offset=-1,
    )
    with pytest.raises(ToolError, match="expired or not yet valid"):
        sign_pdf(
            source,
            tmp_path / "expired-signed.pdf",
            pkcs12_path=expired_identity,
            pkcs12_password=PFX_PASSWORD,
        )

    usage_identity = tmp_path / "wrong-usage.pfx"
    _write_identity(usage_identity, digital_signature_usage=False)
    with pytest.raises(ToolError, match="does not allow digital signatures"):
        sign_pdf(
            source,
            tmp_path / "usage-signed.pdf",
            pkcs12_path=usage_identity,
            pkcs12_password=PFX_PASSWORD,
        )

    existing_output = tmp_path / "existing.pdf"
    existing_output.write_bytes(b"keep me")
    with pytest.raises(ToolError, match="never overwrite"):
        sign_pdf(
            source,
            existing_output,
            pkcs12_path=identity,
            pkcs12_password=PFX_PASSWORD,
        )
    assert existing_output.read_bytes() == b"keep me"


def _fake_getpass(monkeypatch: pytest.MonkeyPatch, values: list[str]) -> list[str]:
    prompts: list[str] = []
    iterator = iter(values)

    def fake(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(iterator)

    monkeypatch.setattr(cli_module.getpass, "getpass", fake)
    return prompts


def test_pdf_signing_direct_cli_and_gui_without_password_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    write_blank_pdf(settings.workspace_dir / "plain.pdf", pages=1)
    _write_identity(settings.workspace_dir / "identity.pfx")
    root = ["--root", str(tmp_path)]

    prompts = _fake_getpass(monkeypatch, [PFX_PASSWORD])
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
                "--field",
                "CliSignature",
                "--create-field",
                "--reason",
                "CLI approval",
                "--json",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert prompts == ["PKCS#12 password: "]
    assert payload["success"] is True
    assert payload["output"]["status"] == "cryptographically_valid_untrusted"
    assert payload["output"]["trust_status"] == "not_evaluated"
    assert PFX_PASSWORD not in captured.out
    assert PFX_PASSWORD not in captured.err

    _fake_getpass(monkeypatch, ["wrong-pfx-password"])
    assert (
        main(
            [
                *root,
                "pdf",
                "sign",
                "plain.pdf",
                "--output",
                "cli-wrong.pdf",
                "--pkcs12",
                "identity.pfx",
                "--json",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["success"] is False
    assert "Could not unlock" in payload["error"]
    assert "wrong-pfx-password" not in captured.out
    assert not (settings.workspace_dir / "cli-wrong.pdf").exists()

    controller = GuiController(settings)
    result = controller.pdf_sign(
        "plain.pdf",
        "gui-signed.pdf",
        "identity.pfx",
        password=PFX_PASSWORD,
        field_name="GuiSignature",
        create_field=True,
        rectangle=None,
    )
    assert result.success, result.error
    assert result.tool_id == "direct.pdf_sign"
    assert result.output is not None
    assert result.output["status"] == "cryptographically_valid_untrusted"
    assert PFX_PASSWORD not in json.dumps(result.output)

    wrong = controller.pdf_sign(
        "plain.pdf",
        "gui-wrong.pdf",
        "identity.pfx",
        password="wrong-pfx-password",
    )
    assert wrong.success is False
    assert "wrong-pfx-password" not in (wrong.error or "")
    assert not (settings.workspace_dir / "gui-wrong.pdf").exists()

    with pytest.raises(PcAgentError, match="must stay inside the workspace"):
        controller.pdf_sign(
            "plain.pdf",
            "outside-identity.pdf",
            "../identity.pfx",
            password=PFX_PASSWORD,
        )

    summary = controller.doctor_summary()
    assert summary.version == __version__ == "0.41.0"
    assert summary.builtin_tools == 62
    assert "PHASE 39 Secure .env Key Cleanup" in summary.to_text()


def test_phase28_scope_docs_schema_and_no_registered_signing_tool() -> None:
    project_root = Path(__file__).resolve().parents[1]
    candidates = {path.as_posix() for path in release_candidate_files(project_root)}
    ids = {item.spec.id for item in build_default_registry().list_tools()}

    assert __version__ == "0.41.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(ids) == 62
    assert "pdf.sign" not in ids
    assert "direct.pdf_sign" not in ids
    assert "src/pc_agent/documents/pdf_signing.py" in candidates
    assert "docs/phase28.md" in candidates
    assert "tests/test_phase28_pdf_signing.py" in candidates

    phase_doc = (project_root / "docs" / "phase28.md").read_text(encoding="utf-8")
    lowered = phase_doc.lower()
    assert "no registered tool" in lowered
    assert "pkcs#12" in lowered
    assert "trust was not evaluated" in lowered
    assert "already signed" in lowered
    assert "never overwrites" in lowered

