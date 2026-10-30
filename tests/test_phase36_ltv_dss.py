"""Phase 36: direct-only LTV DSS embedding and inspection."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509 import ocsp
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)
from test_phase30_rfc3161 import TSA_URL, _FakeTsa
from test_phase30_rfc3161 import _hierarchy as _tsa_hierarchy
from test_phase31_online_revocation import _crl_bytes, _ocsp_response, _signed_pdf
from test_phase31_online_revocation import _hierarchy as _revocation_hierarchy
from test_phase34_incremental_signing import PFX_PASSWORD as DOCMDP_PASSWORD
from test_phase34_incremental_signing import (
    _write_blank_signature_fields,
    _write_identity,
)

from pc_agent import __version__
from pc_agent.cli import build_parser, main
from pc_agent.config import Settings
from pc_agent.documents.pdf import _load_pypdf, write_blank_pdf
from pc_agent.documents.pdf_forms import _get, _object, _raw_fields
from pc_agent.documents.pdf_ltv import add_pdf_ltv_material, inspect_pdf_dss
from pc_agent.documents.pdf_signatures import (
    _contents_bytes,
    inspect_pdf_signatures,
)
from pc_agent.documents.pdf_signing import sign_pdf
from pc_agent.errors import PcAgentError, ToolError
from pc_agent.gui.controller import GuiController
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cert_fingerprint(certificate: x509.Certificate) -> str:
    return certificate.fingerprint(hashes.SHA256()).hex()


def _vri_identifier(path: Path, field_name: str) -> str:
    PdfReader, _PdfWriter = _load_pypdf()
    reader = PdfReader(str(path))
    raw = next(item for item in _raw_fields(reader) if item.name == field_name)
    signature = _object(raw.values.get("/V"))
    contents = _contents_bytes(_get(signature, "/Contents"))
    assert contents is not None
    return hashlib.sha1(contents.hex().encode("ascii")).hexdigest().upper()


def _write_materials(folder: Path, hierarchy: dict[str, Any]) -> tuple[Path, Path]:
    crl_path = folder / "root.crl"
    ocsp_path = folder / "signer.ocsp"
    crl_path.write_bytes(_crl_bytes(hierarchy))
    ocsp_path.write_bytes(_ocsp_response(hierarchy))
    return crl_path, ocsp_path


def test_ltv_dss_embeds_relevant_certificates_crl_and_ocsp(
    tmp_path: Path,
) -> None:
    hierarchy = _revocation_hierarchy(tmp_path)
    signed = _signed_pdf(tmp_path)
    crl_path, ocsp_path = _write_materials(tmp_path, hierarchy)
    signed_bytes = signed.read_bytes()
    signed_hash = _file_hash(signed)
    output = tmp_path / "ltv.pdf"

    before = inspect_pdf_dss(signed)
    assert before.present is False
    assert before.certificate_count == 0

    result = add_pdf_ltv_material(
        signed,
        output,
        certificate_paths=[tmp_path / "root-ca.pem"],
        crl_paths=[crl_path],
        ocsp_response_paths=[ocsp_path],
    )

    assert _file_hash(signed) == signed_hash
    assert output.read_bytes().startswith(signed_bytes)
    assert result.status == "dss_embedded"
    assert result.incremental_update is True
    assert result.dss_updated is True
    assert result.existing_dss_present is False
    assert result.added_certificate_count == 2
    assert result.certificate_count == 2
    assert result.added_crl_count == 1
    assert result.added_ocsp_count == 1
    assert result.vri_count == 1
    assert result.vri_field_names == ("Signature1",)
    assert result.preserved_valid_count == 1
    assert result.total_signed_field_count == 1
    assert result.summary()["material_status"] == "verified_relevant"
    assert result.summary()["trust_status"] == "not_evaluated"
    assert result.summary()["pades_status"] == "not_evaluated"

    signatures = inspect_pdf_signatures(output)
    assert signatures.valid_count == 1
    assert signatures.signatures[0].covers_whole_file is False

    dss = inspect_pdf_dss(output)
    assert dss.present is True
    assert dss.extensions_esic is True
    assert {item.sha256_fingerprint for item in dss.certificates} == {
        _cert_fingerprint(hierarchy["signer_cert"]),
        _cert_fingerprint(hierarchy["root_cert"]),
    }
    assert dss.crls[0].issuer == "CN=Phase 31 Root CA"
    assert dss.ocsps[0].certificate_status == "good"
    assert dss.ocsps[0].serial_number == f"{hierarchy['signer_cert'].serial_number:x}"
    assert len(dss.vri) == 1
    vri = dss.vri[0]
    assert vri.identifier == _vri_identifier(output, "Signature1")
    assert vri.field_names == ("Signature1",)
    assert set(vri.certificate_fingerprints) == {
        _cert_fingerprint(hierarchy["signer_cert"]),
        _cert_fingerprint(hierarchy["root_cert"]),
    }
    assert vri.crl_fingerprints == (dss.crls[0].sha256_fingerprint,)
    assert vri.ocsp_fingerprints == (dss.ocsps[0].sha256_fingerprint,)
    assert vri.tu is not None and vri.tu.startswith("D:")
    assert vri.ts_present is False


def test_ltv_dss_noop_refusal_and_timestamp_seal_merge(
    tmp_path: Path,
) -> None:
    hierarchy = _revocation_hierarchy(tmp_path)
    signed = _signed_pdf(tmp_path)
    crl_path, ocsp_path = _write_materials(tmp_path, hierarchy)
    first = tmp_path / "ltv.pdf"
    add_pdf_ltv_material(
        signed,
        first,
        certificate_paths=[tmp_path / "root-ca.pem"],
        crl_paths=[crl_path],
        ocsp_response_paths=[ocsp_path],
    )
    first_bytes = first.read_bytes()

    refused = tmp_path / "ltv-again.pdf"
    with pytest.raises(ToolError, match="already contains the same DSS"):
        add_pdf_ltv_material(first, refused)
    assert not refused.exists()

    tsa_hierarchy = _tsa_hierarchy(tmp_path / "tsa", prefix="Phase 36 TSA")
    sealed = tmp_path / "ltv-sealed.pdf"
    result = add_pdf_ltv_material(
        first,
        sealed,
        timestamp_url=TSA_URL,
        timestamp_create_field=True,
        timestamp_opener=_FakeTsa(tsa_hierarchy),
    )

    assert sealed.read_bytes().startswith(first_bytes)
    assert sealed.read_bytes().startswith(signed.read_bytes())
    assert result.status == "dss_embedded_timestamped"
    assert result.dss_updated is False
    assert result.existing_dss_present is True
    assert result.added_certificate_count == 0
    assert result.timestamp_field_name == "DocTimeStamp"
    assert result.timestamp_status == "verified_untrusted"
    assert result.total_signed_field_count == 2
    assert inspect_pdf_signatures(sealed).valid_count == 2
    sealed_dss = inspect_pdf_dss(sealed)
    assert sealed_dss.certificate_count == 2
    assert sealed_dss.crl_count == 1
    assert sealed_dss.ocsp_count == 1
    assert sealed_dss.vri[0].field_names == ("Signature1",)


def test_ltv_dss_docmdp_p1_allowed_but_timestamp_seal_still_refused(
    tmp_path: Path,
) -> None:
    form = tmp_path / "form-p1.pdf"
    certified = tmp_path / "certified-p1.pdf"
    identity = tmp_path / "certifier.p12"
    _write_blank_signature_fields(form, ("Signer1", "Signer2"))
    _write_identity(identity, "Phase 36 Certifier", 3611)
    sign_pdf(
        form,
        certified,
        pkcs12_path=identity,
        pkcs12_password=DOCMDP_PASSWORD,
        field_name="Signer1",
        certify_permission=1,
    )

    output = tmp_path / "certified-p1-ltv.pdf"
    result = add_pdf_ltv_material(certified, output)
    assert result.certificate_count == 1
    assert result.preserved_valid_count == 1
    assert output.read_bytes().startswith(certified.read_bytes())
    inspection = inspect_pdf_signatures(output)
    assert inspection.valid_count == 1
    assert inspection.signatures[0].docmdp_permission == 1
    assert inspect_pdf_dss(output).present is True

    tsa_hierarchy = _tsa_hierarchy(tmp_path / "tsa", prefix="Phase 36 P1")
    sealed = tmp_path / "certified-p1-sealed.pdf"
    with pytest.raises(ToolError, match=r"P=1 prohibits"):
        add_pdf_ltv_material(
            certified,
            sealed,
            timestamp_url=TSA_URL,
            timestamp_create_field=True,
            timestamp_opener=_FakeTsa(tsa_hierarchy),
        )
    assert not sealed.exists()


def _stale_crl_bytes(hierarchy: dict[str, Any]) -> bytes:
    now = datetime.now(UTC)
    return (
        x509.CertificateRevocationListBuilder()
        .issuer_name(hierarchy["root_cert"].subject)
        .last_update(now - timedelta(days=2))
        .next_update(now - timedelta(days=1))
        .sign(private_key=hierarchy["root_key"], algorithm=hashes.SHA256())
        .public_bytes(serialization.Encoding.DER)
    )


def _write_malformed_dss(source: Path, output: Path) -> None:
    _PdfReader, PdfWriter = _load_pypdf()
    writer = PdfWriter(str(source), incremental=True)
    try:
        stream = DecodedStreamObject()
        stream.set_data(b"this is not a DER certificate")
        reference = writer._add_object(stream)
        dss = DictionaryObject()
        dss.update(
            {
                NameObject("/Type"): NameObject("/DSS"),
                NameObject("/Certs"): ArrayObject([reference]),
            }
        )
        writer._root_object[NameObject("/DSS")] = dss
        with output.open("wb") as handle:
            writer.write(handle)
    finally:
        writer.close()


def test_ltv_dss_refusals_do_not_publish(tmp_path: Path) -> None:
    hierarchy = _revocation_hierarchy(tmp_path)
    other = _revocation_hierarchy(tmp_path / "other")
    signed = _signed_pdf(tmp_path)

    unsigned = tmp_path / "unsigned.pdf"
    write_blank_pdf(unsigned, pages=1)
    refused_unsigned = tmp_path / "unsigned-ltv.pdf"
    with pytest.raises(ToolError, match="at least one signed field"):
        add_pdf_ltv_material(unsigned, refused_unsigned)
    assert not refused_unsigned.exists()

    refused_cert = tmp_path / "unrelated-cert.pdf"
    with pytest.raises(ToolError, match="certificates were unrelated"):
        add_pdf_ltv_material(
            signed,
            refused_cert,
            certificate_paths=[tmp_path / "other" / "root-ca.pem"],
        )
    assert not refused_cert.exists()

    stale_crl = tmp_path / "stale.crl"
    stale_crl.write_bytes(_stale_crl_bytes(hierarchy))
    refused_crl = tmp_path / "stale-crl.pdf"
    with pytest.raises(ToolError, match="Every supplied CRL"):
        add_pdf_ltv_material(signed, refused_crl, crl_paths=[stale_crl])
    assert not refused_crl.exists()

    revoked_crl = tmp_path / "revoked.crl"
    revoked_crl.write_bytes(_crl_bytes(hierarchy, revoked=True))
    refused_revoked_crl = tmp_path / "revoked-crl.pdf"
    with pytest.raises(ToolError, match="supplied CRL proves"):
        add_pdf_ltv_material(
            signed, refused_revoked_crl, crl_paths=[revoked_crl]
        )
    assert not refused_revoked_crl.exists()

    revoked_ocsp = tmp_path / "revoked.ocsp"
    revoked_ocsp.write_bytes(
        _ocsp_response(hierarchy, status=ocsp.OCSPCertStatus.REVOKED)
    )
    refused_revoked_ocsp = tmp_path / "revoked-ocsp.pdf"
    with pytest.raises(ToolError, match="supplied OCSP response proves"):
        add_pdf_ltv_material(
            signed, refused_revoked_ocsp, ocsp_response_paths=[revoked_ocsp]
        )
    assert not refused_revoked_ocsp.exists()

    unrelated_ocsp = tmp_path / "unrelated.ocsp"
    unrelated_ocsp.write_bytes(_ocsp_response(other))
    refused_ocsp = tmp_path / "unrelated-ocsp.pdf"
    with pytest.raises(ToolError, match="Every supplied OCSP response"):
        add_pdf_ltv_material(
            signed, refused_ocsp, ocsp_response_paths=[unrelated_ocsp]
        )
    assert not refused_ocsp.exists()

    tampered = tmp_path / "tampered.pdf"
    data = bytearray(signed.read_bytes())
    data[10] ^= 1
    tampered.write_bytes(data)
    assert inspect_pdf_signatures(tampered).invalid_count == 1
    refused_tampered = tmp_path / "tampered-ltv.pdf"
    with pytest.raises(ToolError, match="requires every existing"):
        add_pdf_ltv_material(tampered, refused_tampered)
    assert not refused_tampered.exists()

    malformed = tmp_path / "malformed-dss.pdf"
    _write_malformed_dss(signed, malformed)
    with pytest.raises(ToolError, match="not a valid DER"):
        inspect_pdf_dss(malformed)
    refused_malformed = tmp_path / "malformed-ltv.pdf"
    with pytest.raises(ToolError, match="not a valid DER"):
        add_pdf_ltv_material(malformed, refused_malformed)
    assert not refused_malformed.exists()

    existing_output = tmp_path / "existing.pdf"
    existing_output.write_bytes(b"keep-me")
    with pytest.raises(ToolError, match="already exists"):
        add_pdf_ltv_material(signed, existing_output)
    assert existing_output.read_bytes() == b"keep-me"


def test_ltv_dss_direct_cli_and_gui(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    workspace = settings.workspace_dir
    hierarchy = _revocation_hierarchy(workspace)
    _signed_pdf(workspace)
    _write_materials(workspace, hierarchy)

    parser = build_parser()
    inspect_args = parser.parse_args(
        ["--root", str(tmp_path), "pdf", "inspect-ltv", "signed.pdf", "--json"]
    )
    assert inspect_args.pdf_command == "inspect-ltv"
    add_args = parser.parse_args(
        [
            "--root",
            str(tmp_path),
            "pdf",
            "add-ltv",
            "signed.pdf",
            "--output",
            "cli-ltv.pdf",
            "--certificate",
            "root-ca.pem",
            "--crl",
            "root.crl",
            "--ocsp-response",
            "signer.ocsp",
            "--json",
        ]
    )
    assert add_args.pdf_command == "add-ltv"
    assert add_args.certificate_paths == ["root-ca.pem"]
    assert add_args.ocsp_response_paths == ["signer.ocsp"]

    root = ["--root", str(tmp_path)]
    assert main([*root, "pdf", "inspect-ltv", "signed.pdf", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["output"]["dss_present"] is False

    assert (
        main(
            [
                *root,
                "pdf",
                "add-ltv",
                "signed.pdf",
                "--output",
                "cli-ltv.pdf",
                "--certificate",
                "root-ca.pem",
                "--crl",
                "root.crl",
                "--ocsp-response",
                "signer.ocsp",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["output"]["operation"] == "add_ltv_dss"
    assert payload["output"]["certificate_count"] == 2
    assert payload["output"]["ocsp_count"] == 1
    assert payload["output"]["trust_status"] == "not_evaluated"

    assert main([*root, "pdf", "inspect-ltv", "cli-ltv.pdf", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["output"]["dss_present"] is True
    assert payload["output"]["vri"][0]["field_names"] == ["Signature1"]

    controller = GuiController(settings)
    inspected = controller.pdf_inspect_ltv("cli-ltv.pdf")
    assert inspected.success, inspected.error
    assert inspected.tool_id == "direct.pdf_inspect_ltv"
    assert inspected.output is not None
    assert inspected.output["crl_count"] == 1

    tsa_hierarchy = _tsa_hierarchy(tmp_path / "tsa", prefix="Phase 36 GUI")
    gui_result = controller.pdf_add_ltv(
        "signed.pdf",
        "gui-ltv.pdf",
        "root-ca.pem",
        "root.crl",
        "signer.ocsp",
        timestamp_url=TSA_URL,
        timestamp_create_field=True,
        timestamp_opener=_FakeTsa(tsa_hierarchy),
    )
    assert gui_result.success, gui_result.error
    assert gui_result.tool_id == "direct.pdf_add_ltv"
    assert gui_result.output is not None
    assert gui_result.output["status"] == "dss_embedded_timestamped"
    assert gui_result.output["timestamp_status"] == "verified_untrusted"
    assert gui_result.output["total_signed_field_count"] == 2

    with pytest.raises(PcAgentError, match="must stay inside the workspace"):
        controller.pdf_add_ltv(
            "signed.pdf",
            "outside-ltv.pdf",
            "root-ca.pem",
            "root.crl",
            "../outside.ocsp",
        )

    summary = controller.doctor_summary()
    assert summary.version == __version__ == "0.41.0"
    assert summary.builtin_tools == 62
    assert "PHASE 39 Secure .env Key Cleanup" in summary.to_text()


def test_phase36_scope_docs_schema_and_no_registered_tool() -> None:
    project_root = Path(__file__).resolve().parents[1]
    candidates = {path.as_posix() for path in release_candidate_files(project_root)}
    ids = {item.spec.id for item in build_default_registry().list_tools()}

    assert __version__ == "0.41.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(ids) == 62
    assert "pdf.add_ltv" not in ids
    assert "direct.pdf_add_ltv" not in ids
    assert "direct.pdf_inspect_ltv" not in ids
    assert "src/pc_agent/documents/pdf_ltv.py" in candidates
    assert "docs/phase36.md" in candidates
    assert "tests/test_phase36_ltv_dss.py" in candidates

    phase_doc = (project_root / "docs" / "phase36.md").read_text(encoding="utf-8")
    lowered = phase_doc.lower()
    assert "version | 0.38.0" in lowered
    assert "schema | v6" in lowered
    assert "tools | 62" in lowered
    assert "exact byte prefix" in lowered
    assert "complete hexadecimal representation" in lowered
    assert "verified_relevant" in lowered
    assert "not_evaluated" in lowered
    assert "does not fetch" in lowered
    assert "never sends the pdf" in lowered
    assert "p=1, p=2 and p=3" in lowered
    assert "not a claim of pades" in lowered

    gui_source = (
        project_root / "src" / "pc_agent" / "gui" / "app.py"
    ).read_text(encoding="utf-8")
    assert "Inspect LTV DSS" in gui_source
    assert "Add LTV DSS" in gui_source
    assert "pdf_ocsp_responses_text" in gui_source
    assert "pdf_ltv_seal_timestamp_var" in gui_source
