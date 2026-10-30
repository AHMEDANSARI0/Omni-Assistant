"""Phase 29: offline PDF trust-chain and CRL validation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from pc_agent import __version__
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.documents.pdf import write_blank_pdf
from pc_agent.documents.pdf_signing import sign_pdf
from pc_agent.documents.pdf_trust import validate_pdf_signature_trust
from pc_agent.errors import PcAgentError, ToolError
from pc_agent.gui.controller import GuiController
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

PFX_PASSWORD = "pfx-secret-phase29"


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
) -> x509.Certificate:
    now = datetime.now(UTC)
    return (
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
        .sign(issuer_key, hashes.SHA256())
    )


def _make_hierarchy(root: Path, *, prefix: str = "Phase 29") -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    root_key = ec.generate_private_key(ec.SECP256R1())
    intermediate_key = ec.generate_private_key(ec.SECP256R1())
    signer_key = ec.generate_private_key(ec.SECP256R1())
    root_name = _name(f"{prefix} Root CA")
    intermediate_name = _name(f"{prefix} Intermediate CA")
    signer_name = _name(f"{prefix} Signer")
    root_cert = _certificate(
        subject=root_name,
        issuer=root_name,
        public_key=root_key.public_key(),
        issuer_key=root_key,
        serial=2901,
        ca=True,
        path_length=1,
    )
    intermediate_cert = _certificate(
        subject=intermediate_name,
        issuer=root_name,
        public_key=intermediate_key.public_key(),
        issuer_key=root_key,
        serial=2902,
        ca=True,
        path_length=0,
    )
    signer_cert = _certificate(
        subject=signer_name,
        issuer=intermediate_name,
        public_key=signer_key.public_key(),
        issuer_key=intermediate_key,
        serial=2903,
        ca=False,
        path_length=None,
    )

    (root / "root-ca.pem").write_bytes(
        root_cert.public_bytes(serialization.Encoding.PEM)
    )
    (root / "identity.pfx").write_bytes(
        pkcs12.serialize_key_and_certificates(
            b"Phase 29 Signer",
            signer_key,
            signer_cert,
            [intermediate_cert, root_cert],
            serialization.BestAvailableEncryption(PFX_PASSWORD.encode()),
        )
    )
    return {
        "root_key": root_key,
        "intermediate_key": intermediate_key,
        "signer_key": signer_key,
        "root_cert": root_cert,
        "intermediate_cert": intermediate_cert,
        "signer_cert": signer_cert,
    }


def _signed_pdf(folder: Path, hierarchy: dict[str, Any]) -> Path:
    del hierarchy
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


def _write_crl(
    path: Path,
    *,
    issuer_cert: x509.Certificate,
    issuer_key: Any,
    revoked_cert: x509.Certificate | None = None,
    stale: bool = False,
) -> None:
    now = datetime.now(UTC)
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(issuer_cert.subject)
        .last_update(now - timedelta(days=2 if stale else 1))
        .next_update(now - timedelta(days=1) if stale else now + timedelta(days=1))
    )
    if revoked_cert is not None:
        builder = builder.add_revoked_certificate(
            x509.RevokedCertificateBuilder()
            .serial_number(revoked_cert.serial_number)
            .revocation_date(now - timedelta(hours=1))
            .build()
        )
    crl = builder.sign(private_key=issuer_key, algorithm=hashes.SHA256())
    path.write_bytes(crl.public_bytes(serialization.Encoding.PEM))


def _write_good_crls(folder: Path, hierarchy: dict[str, Any]) -> tuple[Path, Path]:
    signer_crl = folder / "signer-issuer.crl"
    intermediate_crl = folder / "intermediate-issuer.crl"
    _write_crl(
        signer_crl,
        issuer_cert=hierarchy["intermediate_cert"],
        issuer_key=hierarchy["intermediate_key"],
    )
    _write_crl(
        intermediate_crl,
        issuer_cert=hierarchy["root_cert"],
        issuer_key=hierarchy["root_key"],
    )
    return signer_crl, intermediate_crl


def test_pdf_trust_trusted_chain_with_complete_fresh_crls(tmp_path: Path) -> None:
    hierarchy = _make_hierarchy(tmp_path)
    signed = _signed_pdf(tmp_path, hierarchy)
    crls = _write_good_crls(tmp_path, hierarchy)

    validation = validate_pdf_signature_trust(
        signed,
        trust_anchor_paths=[tmp_path / "root-ca.pem"],
        crl_paths=list(crls),
    )

    assert validation.trusted_valid_count == 1
    assert validation.revoked_count == 0
    signature = validation.signatures[0]
    assert signature.cryptographic_status == "cryptographically_valid_untrusted"
    assert signature.chain_status == "trusted"
    assert signature.revocation_status == "not_revoked"
    assert signature.overall_status == "trusted_valid"
    signer = signature.signers[0]
    assert signer.verification_time_source == "cms_signing_time"
    assert [item.role for item in signer.chain] == [
        "signer",
        "intermediate_or_root",
        "trust_anchor",
    ]
    assert signer.signer_sha256_fingerprint == hierarchy[
        "signer_cert"
        ].fingerprint(hashes.SHA256()).hex()
    dumped = json.dumps(validation.summary())
    assert validation.summary()["network_used"] is False
    assert validation.summary()["ocsp_status"] == "not_evaluated"
    assert "BEGIN CERTIFICATE" not in dumped
    assert "der_bytes" not in dumped
    assert PFX_PASSWORD not in dumped



def test_pdf_trust_untrusted_unknown_revoked_stale_and_invalid(
    tmp_path: Path,
) -> None:
    hierarchy = _make_hierarchy(tmp_path)
    signed = _signed_pdf(tmp_path, hierarchy)
    root_anchor = tmp_path / "root-ca.pem"
    good_crls = _write_good_crls(tmp_path, hierarchy)

    no_crl = validate_pdf_signature_trust(
        signed, trust_anchor_paths=[root_anchor]
    ).signatures[0]
    assert no_crl.chain_status == "trusted"
    assert no_crl.revocation_status == "unknown"
    assert no_crl.overall_status == "trusted_chain_revocation_unknown"

    no_anchor = validate_pdf_signature_trust(
        signed, crl_paths=list(good_crls)
    ).signatures[0]
    assert no_anchor.chain_status == "untrusted"
    assert no_anchor.overall_status == "cryptographically_valid_untrusted"

    other = _make_hierarchy(tmp_path / "other", prefix="Other")
    del other
    wrong_anchor = validate_pdf_signature_trust(
        signed,
        trust_anchor_paths=[tmp_path / "other" / "root-ca.pem"],
        crl_paths=list(good_crls),
    ).signatures[0]
    assert wrong_anchor.chain_status == "untrusted"

    revoked_crl = tmp_path / "signer-revoked.crl"
    _write_crl(
        revoked_crl,
        issuer_cert=hierarchy["intermediate_cert"],
        issuer_key=hierarchy["intermediate_key"],
        revoked_cert=hierarchy["signer_cert"],
    )
    revoked = validate_pdf_signature_trust(
        signed,
        trust_anchor_paths=[root_anchor],
        crl_paths=[revoked_crl, good_crls[1]],
    ).signatures[0]
    assert revoked.chain_status == "trusted"
    assert revoked.revocation_status == "revoked"
    assert revoked.overall_status == "trusted_chain_revoked"

    stale_crl = tmp_path / "signer-stale.crl"
    _write_crl(
        stale_crl,
        issuer_cert=hierarchy["intermediate_cert"],
        issuer_key=hierarchy["intermediate_key"],
        stale=True,
    )
    stale = validate_pdf_signature_trust(
        signed,
        trust_anchor_paths=[root_anchor],
        crl_paths=[stale_crl, good_crls[1]],
    ).signatures[0]
    assert stale.revocation_status == "unknown"
    assert "stale" in stale.status_detail

    expired = validate_pdf_signature_trust(
        signed,
        trust_anchor_paths=[root_anchor],
        crl_paths=list(good_crls),
        verification_time=datetime.now(UTC) + timedelta(days=40),
    ).signatures[0]
    assert expired.chain_status == "invalid"
    assert "expired" in expired.status_detail
    assert expired.overall_status == "cryptographically_valid_untrusted"

    tampered = tmp_path / "tampered.pdf"
    data = bytearray(signed.read_bytes())
    data[10] ^= 1
    tampered.write_bytes(data)
    invalid = validate_pdf_signature_trust(
        tampered,
        trust_anchor_paths=[root_anchor],
        crl_paths=list(good_crls),
    ).signatures[0]
    assert invalid.cryptographic_status == "invalid"
    assert invalid.chain_status == "not_evaluated"
    assert invalid.overall_status == "invalid"

    bad_anchor = tmp_path / "bad.pem"
    bad_anchor.write_text("not a certificate", encoding="utf-8")
    with pytest.raises(ToolError, match="Could not parse trust-anchor"):
        validate_pdf_signature_trust(signed, trust_anchor_paths=[bad_anchor])


def test_pdf_trust_direct_cli_gui_and_workspace_bounds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    workspace = settings.workspace_dir
    hierarchy = _make_hierarchy(workspace)
    _signed_pdf(workspace, hierarchy)
    _write_good_crls(workspace, hierarchy)
    root = ["--root", str(tmp_path)]

    assert (
        main(
            [
                *root,
                "pdf",
                "validate-signatures",
                "signed.pdf",
                "--trust-anchor",
                "root-ca.pem",
                "--crl",
                "signer-issuer.crl",
                "--crl",
                "intermediate-issuer.crl",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["output"]["trusted_valid_count"] == 1
    assert payload["output"]["signatures"][0]["overall_status"] == "trusted_valid"
    assert "BEGIN CERTIFICATE" not in json.dumps(payload)

    assert (
        main(
            [
                *root,
                "pdf",
                "validate-signatures",
                "signed.pdf",
                "--trust-anchor",
                "root-ca.pem",
                "--json",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert payload["output"]["signatures"][0]["overall_status"] == (
        "trusted_chain_revocation_unknown"
    )

    assert (
        main(
            [
                *root,
                "pdf",
                "validate-signatures",
                "signed.pdf",
                "--trust-anchor",
                "root-ca.pem",
                "--accept-revocation-unknown",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True

    assert (
        main(
            [
                *root,
                "pdf",
                "validate-signatures",
                "signed.pdf",
                "--trust-anchor",
                "../outside.pem",
                "--json",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert "must stay inside the workspace" in payload["error"]

    controller = GuiController(settings)
    result = controller.pdf_validate_signatures(
        "signed.pdf",
        "root-ca.pem",
        "signer-issuer.crl\nintermediate-issuer.crl",
    )
    assert result.success, result.error
    assert result.tool_id == "direct.pdf_validate_signatures"
    assert result.output is not None
    assert result.output["trusted_valid_count"] == 1

    unknown = controller.pdf_validate_signatures("signed.pdf", "root-ca.pem")
    assert unknown.success is False
    assert unknown.output is not None
    assert unknown.output["trusted_chain_revocation_unknown_count"] == 1

    accepted = controller.pdf_validate_signatures(
        "signed.pdf",
        "root-ca.pem",
        accept_revocation_unknown=True,
    )
    assert accepted.success, accepted.error

    with pytest.raises(PcAgentError, match="must stay inside the workspace"):
        controller.pdf_validate_signatures("signed.pdf", "../outside.pem")

    summary = controller.doctor_summary()
    assert summary.version == __version__ == "0.41.0"
    assert summary.builtin_tools == 62
    assert "PHASE 39 Secure .env Key Cleanup" in summary.to_text()


def test_phase29_scope_docs_schema_and_no_registered_trust_tool() -> None:
    project_root = Path(__file__).resolve().parents[1]
    candidates = {path.as_posix() for path in release_candidate_files(project_root)}
    ids = {item.spec.id for item in build_default_registry().list_tools()}

    assert __version__ == "0.41.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(ids) == 62
    assert "pdf.validate_signatures" not in ids
    assert "direct.pdf_validate_signatures" not in ids
    assert "src/pc_agent/documents/pdf_trust.py" in candidates
    assert "docs/phase29.md" in candidates
    assert "tests/test_phase29_pdf_trust.py" in candidates

    phase_doc = (project_root / "docs" / "phase29.md").read_text(encoding="utf-8")
    lowered = phase_doc.lower()
    assert "no registered tool" in lowered
    assert "no network call is made" in lowered
    assert "local basic chain and crl validation only" in lowered
    assert "ocsp" in lowered and "not evaluated" in lowered

