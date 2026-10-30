"""Phase 37: PAdES-oriented Baseline B-B signature profile."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from test_phase27_pdf_signatures import _write_signed_pdf as _write_generic_signed_pdf
from test_phase28_pdf_signing import PFX_PASSWORD as RSA_PASSWORD
from test_phase28_pdf_signing import _fake_getpass
from test_phase28_pdf_signing import _write_identity as _write_rsa_identity
from test_phase30_rfc3161 import TSA_URL, _FakeTsa
from test_phase30_rfc3161 import _hierarchy as _tsa_hierarchy
from test_phase31_online_revocation import PFX_PASSWORD as EC_PASSWORD
from test_phase31_online_revocation import _crl_bytes
from test_phase31_online_revocation import _hierarchy as _ec_hierarchy
from test_phase34_incremental_signing import PFX_PASSWORD as INC_PASSWORD
from test_phase34_incremental_signing import _write_blank_signature_fields
from test_phase34_incremental_signing import (
    _write_identity as _write_incremental_identity,
)

import pc_agent.cli as cli_module
from pc_agent import __version__
from pc_agent.cli import build_parser, main
from pc_agent.config import Settings
from pc_agent.documents.pdf import _load_pypdf, write_blank_pdf
from pc_agent.documents.pdf_forms import _get, _object, _raw_fields
from pc_agent.documents.pdf_signatures import (
    _OID_CONTENT_TYPE,
    _OID_MESSAGE_DIGEST,
    _OID_SIGNING_CERTIFICATE_V2,
    _OID_SIGNING_TIME,
    _parse_der_root,
    _validate_signing_certificate_v2,
    inspect_pdf_signatures,
)
from pc_agent.documents.pdf_signing import (
    _signing_certificate_v2,
    sign_pdf,
)
from pc_agent.documents.pdf_trust import validate_pdf_signature_trust
from pc_agent.errors import PcAgentError, ToolError
from pc_agent.gui.controller import GuiController
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

_ED_PASSWORD = "pfx-secret-phase37-ed"


def _signature_dictionary(path: Path, field_name: str) -> object:
    PdfReader, _PdfWriter = _load_pypdf()
    reader = PdfReader(str(path))
    raw = next(item for item in _raw_fields(reader) if item.name == field_name)
    return _object(raw.values.get("/V"))


def _write_ed25519_identity(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Phase 37 Ed25519 Signer")]
    )
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(3701)
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
        .sign(key, None)  # type: ignore[arg-type]
    )
    path.write_bytes(
        pkcs12.serialize_key_and_certificates(
            b"Phase 37 Ed25519 Signer",
            key,
            certificate,
            None,
            serialization.BestAvailableEncryption(_ED_PASSWORD.encode()),
        )
    )


def test_pades_bb_ec_signature_structure_and_trust_time_source(
    tmp_path: Path,
) -> None:
    hierarchy = _ec_hierarchy(tmp_path)
    source = tmp_path / "plain.pdf"
    output = tmp_path / "pades-ec.pdf"
    write_blank_pdf(source, pages=1)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    result = sign_pdf(
        source,
        output,
        pkcs12_path=tmp_path / "identity.pfx",
        pkcs12_password=EC_PASSWORD,
        hash_algorithm="sha384",
        signature_profile="pades-bb",
        reason="Phase 37 approval",
    )

    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    assert result.signature_profile == "pades-bb"
    assert result.signature_sub_filter == "ETSI.CAdES.detached"
    assert result.pades_profile_status == "bb_structure_verified"
    assert result.summary()["pades_conformance_status"] == (
        "not_externally_evaluated"
    )
    assert EC_PASSWORD not in json.dumps(result.summary())

    raw = output.read_bytes()
    assert re.search(rb"/SubFilter\s*/ETSI\.CAdES\.detached", raw)
    assert re.search(rb"/ESIC\s*<<.*?/ExtensionLevel\s*2", raw, flags=re.DOTALL)
    signature_dict = _signature_dictionary(output, result.field_name)
    assert _get(signature_dict, "/Cert") is None

    inspection = inspect_pdf_signatures(output)
    assert inspection.valid_count == 1
    signature = inspection.signatures[0]
    signer = signature.signers[0]
    assert signature.pades_profile_status == "bb_structure_verified"
    assert signature.covers_whole_file is True
    assert signer.cms_content_type == "1.2.840.113549.1.7.1"
    assert signer.signed_attribute_oids == (
        _OID_CONTENT_TYPE,
        _OID_MESSAGE_DIGEST,
        _OID_SIGNING_CERTIFICATE_V2,
    )
    assert _OID_SIGNING_TIME not in signer.signed_attribute_oids
    assert signer.signing_time is None
    assert signer.claimed_signing_time == signature.signature_date
    assert signer.claimed_signing_time_source == "pdf_signature_dictionary_m"
    assert signer.signing_certificate_v2_present is True
    assert signer.signing_certificate_v2_valid is True
    assert signer.signing_certificate_v2_hash_algorithm == "sha384"
    assert signer.signature_algorithm == "ECDSA/SHA384"

    crl_path = tmp_path / "root.crl"
    crl_path.write_bytes(_crl_bytes(hierarchy))
    validation = validate_pdf_signature_trust(
        output,
        trust_anchor_paths=[tmp_path / "root-ca.pem"],
        crl_paths=[crl_path],
    )
    assert validation.trusted_valid_count == 1
    trusted_signer = validation.signatures[0].signers[0]
    assert trusted_signer.overall_status == "trusted_valid"
    assert trusted_signer.verification_time_source == (
        "pdf_signature_dictionary_m"
    )


def test_pades_bb_rsa_timestamp_and_incremental_second_signature(
    tmp_path: Path,
) -> None:
    form = tmp_path / "form.pdf"
    first_output = tmp_path / "first.pdf"
    second_output = tmp_path / "second-pades.pdf"
    first_identity = tmp_path / "first.p12"
    second_identity = tmp_path / "second-rsa.pfx"
    _write_blank_signature_fields(form, ("Signer1", "Signer2"))
    _write_incremental_identity(first_identity, "Phase 37 First", 3711)
    _write_rsa_identity(second_identity, key_type="rsa")
    sign_pdf(
        form,
        first_output,
        pkcs12_path=first_identity,
        pkcs12_password=INC_PASSWORD,
        field_name="Signer1",
    )
    first_bytes = first_output.read_bytes()

    tsa_hierarchy = _tsa_hierarchy(tmp_path / "tsa", prefix="Phase 37 TSA")
    result = sign_pdf(
        first_output,
        second_output,
        pkcs12_path=second_identity,
        pkcs12_password=RSA_PASSWORD,
        field_name="Signer2",
        hash_algorithm="sha512",
        signature_profile="pades-bb",
        timestamp_url=TSA_URL,
        timestamp_opener=_FakeTsa(tsa_hierarchy),
    )

    assert second_output.read_bytes().startswith(first_bytes)
    assert result.incremental_update is True
    assert result.preserved_valid_signature_count == 1
    assert result.total_signature_count == 2
    assert result.timestamp_status == "verified_untrusted"
    assert result.pades_profile_status == "bb_structure_verified"

    inspection = inspect_pdf_signatures(second_output)
    assert inspection.valid_count == 2
    by_name = {item.field_name: item for item in inspection.signatures}
    assert by_name["Signer1"].sub_filter == "adbe.pkcs7.detached"
    assert by_name["Signer1"].pades_profile_status == "not_pades_subfilter"
    pades = by_name["Signer2"]
    assert pades.sub_filter == "ETSI.CAdES.detached"
    assert pades.pades_profile_status == "bb_structure_verified"
    assert pades.covers_whole_file is True
    assert pades.signers[0].signature_algorithm == "RSA/SHA512"
    assert pades.signers[0].signing_certificate_v2_hash_algorithm == "sha512"
    assert pades.signers[0].timestamp is not None
    assert pades.signers[0].timestamp.status == "verified_untrusted"


def test_pades_inspector_incomplete_ess_mismatch_and_key_refusal(
    tmp_path: Path,
) -> None:
    generic = tmp_path / "generic-etsi.pdf"
    _write_generic_signed_pdf(generic, sub_filter="ETSI.CAdES.detached")
    generic_signature = inspect_pdf_signatures(generic).signatures[0]
    assert generic_signature.status == "cryptographically_valid_untrusted"
    assert generic_signature.pades_profile_status == "bb_structure_incomplete"
    assert generic_signature.signers[0].signing_certificate_v2_present is False
    assert _OID_SIGNING_TIME in generic_signature.signers[0].signed_attribute_oids

    hierarchy = _ec_hierarchy(tmp_path / "ess")
    signer_cert = hierarchy["signer_cert"]
    root_cert = hierarchy["root_cert"]
    matching = _parse_der_root(_signing_certificate_v2(signer_cert, "sha256"))
    valid, algorithm, _detail = _validate_signing_certificate_v2(
        matching, signer_cert
    )
    assert valid is True
    assert algorithm == "sha256"
    mismatched = _parse_der_root(_signing_certificate_v2(root_cert, "sha512"))
    valid, algorithm, detail = _validate_signing_certificate_v2(
        mismatched, signer_cert
    )
    assert valid is False
    assert algorithm == "sha512"
    assert "does not match" in detail

    source = tmp_path / "plain.pdf"
    write_blank_pdf(source, pages=1)
    ed_identity = tmp_path / "ed25519.p12"
    _write_ed25519_identity(ed_identity)
    refused = tmp_path / "ed-refused.pdf"
    with pytest.raises(ToolError, match="supports RSA and ECDSA"):
        sign_pdf(
            source,
            refused,
            pkcs12_path=ed_identity,
            pkcs12_password=_ED_PASSWORD,
            signature_profile="pades-bb",
        )
    assert not refused.exists()

    ec_identity = tmp_path / "ess" / "identity.pfx"
    invalid_profile = tmp_path / "invalid-profile.pdf"
    with pytest.raises(ToolError, match="signature profile must be one of"):
        sign_pdf(
            source,
            invalid_profile,
            pkcs12_path=ec_identity,
            pkcs12_password=EC_PASSWORD,
            signature_profile="qualified",
        )
    assert not invalid_profile.exists()


def test_pades_bb_direct_cli_and_gui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    workspace = settings.workspace_dir
    write_blank_pdf(workspace / "plain.pdf", pages=1)
    _write_rsa_identity(workspace / "identity.pfx", key_type="rsa")

    parser = build_parser()
    args = parser.parse_args(
        [
            "--root",
            str(tmp_path),
            "pdf",
            "sign",
            "plain.pdf",
            "--output",
            "cli-pades.pdf",
            "--pkcs12",
            "identity.pfx",
            "--profile",
            "pades-bb",
        ]
    )
    assert args.signature_profile == "pades-bb"

    prompts = _fake_getpass(monkeypatch, [RSA_PASSWORD])
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "pdf",
                "sign",
                "plain.pdf",
                "--output",
                "cli-pades.pdf",
                "--pkcs12",
                "identity.pfx",
                "--field",
                "CliPades",
                "--create-field",
                "--hash",
                "sha384",
                "--profile",
                "pades-bb",
                "--json",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert prompts == ["PKCS#12 password: "]
    assert payload["success"] is True
    assert payload["output"]["signature_profile"] == "pades-bb"
    assert payload["output"]["signature_sub_filter"] == "ETSI.CAdES.detached"
    assert payload["output"]["pades_profile_status"] == "bb_structure_verified"
    assert RSA_PASSWORD not in captured.out
    assert RSA_PASSWORD not in captured.err
    assert cli_module.__name__ == "pc_agent.cli"

    controller = GuiController(settings)
    result = controller.pdf_sign(
        "plain.pdf",
        "gui-pades.pdf",
        "identity.pfx",
        password=RSA_PASSWORD,
        field_name="GuiPades",
        create_field=True,
        rectangle=None,
        signature_profile="pades-bb",
    )
    assert result.success, result.error
    assert result.output is not None
    assert result.output["signature_profile"] == "pades-bb"
    assert result.output["pades_profile_status"] == "bb_structure_verified"
    assert RSA_PASSWORD not in json.dumps(result.output)

    with pytest.raises(PcAgentError, match="must stay inside the workspace"):
        controller.pdf_sign(
            "plain.pdf",
            "outside-pades.pdf",
            "../identity.pfx",
            password=RSA_PASSWORD,
            signature_profile="pades-bb",
        )

    summary = controller.doctor_summary()
    assert summary.version == __version__ == "0.41.0"
    assert summary.builtin_tools == 62
    assert "PHASE 39 Secure .env Key Cleanup" in summary.to_text()


def test_phase37_scope_docs_schema_and_no_new_tool() -> None:
    project_root = Path(__file__).resolve().parents[1]
    candidates = {path.as_posix() for path in release_candidate_files(project_root)}
    ids = {item.spec.id for item in build_default_registry().list_tools()}

    assert __version__ == "0.41.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(ids) == 62
    assert "pdf.sign_pades" not in ids
    assert "direct.pdf_sign" not in ids
    assert "docs/phase37.md" in candidates
    assert "tests/test_phase37_pades_baseline.py" in candidates

    phase_doc = (project_root / "docs" / "phase37.md").read_text(encoding="utf-8")
    lowered = phase_doc.lower()
    assert "version | 0.39.0" in lowered
    assert "schema | v6" in lowered
    assert "tools | 62" in lowered
    assert "etsi.cades.detached" in lowered
    assert "signing-certificate-v2" in lowered
    assert "no `signing-time` attribute" in lowered
    assert "bb_structure_verified" in lowered
    assert "not_externally_evaluated" in lowered
    assert "does not claim" in lowered
    assert "adobe aatl trust" in lowered

    gui_source = (
        project_root / "src" / "pc_agent" / "gui" / "app.py"
    ).read_text(encoding="utf-8")
    assert "pdf_signature_profile_var" in gui_source
    assert 'values=("adbe", "pades-bb")' in gui_source
