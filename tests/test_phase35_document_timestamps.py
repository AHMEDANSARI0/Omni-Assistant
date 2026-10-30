"""Phase 35: direct-only incremental PDF DocTimeStamp revisions."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from test_phase30_rfc3161 import PFX_PASSWORD as TSA_IDENTITY_PASSWORD
from test_phase30_rfc3161 import TSA_URL, _FakeTsa, _hierarchy
from test_phase34_incremental_signing import PFX_PASSWORD as SIGNER_PASSWORD
from test_phase34_incremental_signing import (
    _write_blank_signature_fields,
    _write_identity,
)

import pc_agent.cli as cli_module
from pc_agent import __version__
from pc_agent.cli import build_parser, main
from pc_agent.config import Settings
from pc_agent.documents.pdf import write_blank_pdf
from pc_agent.documents.pdf_document_timestamps import add_pdf_document_timestamp
from pc_agent.documents.pdf_signatures import inspect_pdf_signatures
from pc_agent.documents.pdf_signing import sign_pdf
from pc_agent.errors import PcAgentError, ToolError
from pc_agent.gui.controller import GuiController
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _signature(path: Path, field_name: str):
    return next(
        item
        for item in inspect_pdf_signatures(path).signatures
        if item.field_name == field_name
    )


def _byterange_digest(path: Path, field_name: str) -> bytes:
    signature = _signature(path, field_name)
    assert signature.byte_range is not None
    first_start, first_length, second_start, second_length = signature.byte_range
    data = path.read_bytes()
    covered = (
        data[first_start : first_start + first_length]
        + data[second_start : second_start + second_length]
    )
    return hashlib.sha256(covered).digest()


def test_document_timestamp_on_unsigned_pdf_is_incremental(
    tmp_path: Path,
) -> None:
    hierarchy = _hierarchy(tmp_path, prefix="Phase 35")
    source = tmp_path / "plain.pdf"
    output = tmp_path / "timestamped.pdf"
    write_blank_pdf(source, pages=2)
    source_bytes = source.read_bytes()
    source_hash = _hash(source)
    fake = _FakeTsa(hierarchy)

    result = add_pdf_document_timestamp(
        source,
        output,
        timestamp_url=TSA_URL,
        timestamp_opener=fake,
    )

    assert _hash(source) == source_hash
    assert output.read_bytes().startswith(source_bytes)
    assert result.field_name == "DocTimeStamp"
    assert result.created_field is True
    assert result.incremental_update is True
    assert result.existing_signature_count == 0
    assert result.existing_document_timestamp_count == 0
    assert result.preserved_valid_count == 0
    assert result.total_signed_field_count == 1
    assert result.source_revision_bytes == len(source_bytes)
    assert result.status == "cryptographically_valid_untrusted"
    assert result.timestamp_status == "verified_untrusted"
    assert result.summary()["operation"] == "document_timestamp"
    assert result.summary()["signature_created"] is False
    assert result.summary()["tsa_trust_status"] == "not_evaluated"

    inspection = inspect_pdf_signatures(output)
    assert inspection.signed_count == 1
    assert inspection.valid_count == 1
    timestamp = inspection.signatures[0]
    assert timestamp.field_name == "DocTimeStamp"
    assert timestamp.sub_filter == "ETSI.RFC3161"
    assert timestamp.covers_whole_file is True
    assert timestamp.signers == ()
    assert timestamp.document_timestamp is not None
    assert timestamp.document_timestamp.status == "verified_untrusted"
    assert timestamp.document_timestamp.message_imprint_matches is True
    assert timestamp.document_timestamp.hash_algorithm == "sha256"
    assert re.search(rb"/Type\s*/DocTimeStamp", output.read_bytes())
    assert re.search(rb"/SubFilter\s*/ETSI\.RFC3161", output.read_bytes())

    assert len(fake.calls) == 1
    assert fake.calls[0]["url"] == TSA_URL
    assert fake.calls[0]["imprint"] == _byterange_digest(output, "DocTimeStamp")
    assert len(fake.calls[0]["imprint"]) == 32


def test_document_timestamp_preserves_signature_and_supports_renewal(
    tmp_path: Path,
) -> None:
    hierarchy = _hierarchy(tmp_path, prefix="Phase 35 Renewal")
    source = tmp_path / "plain.pdf"
    signed = tmp_path / "signed.pdf"
    first_timestamped = tmp_path / "first-timestamped.pdf"
    renewed = tmp_path / "renewed.pdf"
    write_blank_pdf(source, pages=1)
    sign_pdf(
        source,
        signed,
        pkcs12_path=tmp_path / "identity.pfx",
        pkcs12_password=TSA_IDENTITY_PASSWORD,
        field_name="Approval",
        create_field=True,
    )
    signed_bytes = signed.read_bytes()
    approval_before = _signature(signed, "Approval")

    first = add_pdf_document_timestamp(
        signed,
        first_timestamped,
        timestamp_url=TSA_URL,
        timestamp_opener=_FakeTsa(hierarchy),
    )
    first_bytes = first_timestamped.read_bytes()
    first_doc_timestamp = _signature(first_timestamped, "DocTimeStamp")

    second = add_pdf_document_timestamp(
        first_timestamped,
        renewed,
        timestamp_url=TSA_URL,
        timestamp_opener=_FakeTsa(hierarchy),
    )

    assert first_bytes.startswith(signed_bytes)
    assert renewed.read_bytes().startswith(first_bytes)
    assert first.existing_signature_count == 1
    assert first.existing_document_timestamp_count == 0
    assert first.preserved_valid_count == 1
    assert first.total_signed_field_count == 2
    assert second.field_name == "DocTimeStamp2"
    assert second.created_field is True
    assert second.existing_signature_count == 1
    assert second.existing_document_timestamp_count == 1
    assert second.preserved_valid_count == 2
    assert second.total_signed_field_count == 3

    inspection = inspect_pdf_signatures(renewed)
    assert inspection.signed_count == 3
    assert inspection.valid_count == 3
    by_name = {item.field_name: item for item in inspection.signatures}
    assert by_name["Approval"].byte_range == approval_before.byte_range
    assert by_name["Approval"].covers_whole_file is False
    assert by_name["DocTimeStamp"].byte_range == first_doc_timestamp.byte_range
    assert by_name["DocTimeStamp"].covers_whole_file is False
    assert by_name["DocTimeStamp2"].covers_whole_file is True
    assert by_name["DocTimeStamp2"].sub_filter == "ETSI.RFC3161"


def test_document_timestamp_docmdp_permissions(tmp_path: Path) -> None:
    hierarchy = _hierarchy(tmp_path, prefix="Phase 35 DocMDP")
    identity = tmp_path / "signer.p12"
    _write_identity(identity, "Phase 35 Certifier", 3511)

    form_p2 = tmp_path / "form-p2.pdf"
    certified_p2 = tmp_path / "certified-p2.pdf"
    timestamped_p2 = tmp_path / "timestamped-p2.pdf"
    _write_blank_signature_fields(
        form_p2, ("Signer1", "PreparedTimestamp")
    )
    sign_pdf(
        form_p2,
        certified_p2,
        pkcs12_path=identity,
        pkcs12_password=SIGNER_PASSWORD,
        field_name="Signer1",
        certify_permission=2,
    )
    refused_p2 = tmp_path / "refused-p2.pdf"
    with pytest.raises(ToolError, match=r"P=2 permits timestamping an existing"):
        add_pdf_document_timestamp(
            certified_p2,
            refused_p2,
            timestamp_url=TSA_URL,
            timestamp_opener=_FakeTsa(hierarchy),
        )
    assert not refused_p2.exists()

    p2_result = add_pdf_document_timestamp(
        certified_p2,
        timestamped_p2,
        timestamp_url=TSA_URL,
        field_name="PreparedTimestamp",
        timestamp_opener=_FakeTsa(hierarchy),
    )
    assert p2_result.created_field is False
    assert p2_result.field_name == "PreparedTimestamp"
    assert inspect_pdf_signatures(timestamped_p2).valid_count == 2
    assert timestamped_p2.read_bytes().startswith(certified_p2.read_bytes())

    form_p1 = tmp_path / "form-p1.pdf"
    certified_p1 = tmp_path / "certified-p1.pdf"
    _write_blank_signature_fields(form_p1, ("Signer1", "DocTimeStamp"))
    sign_pdf(
        form_p1,
        certified_p1,
        pkcs12_path=identity,
        pkcs12_password=SIGNER_PASSWORD,
        field_name="Signer1",
        certify_permission=1,
    )
    refused_p1 = tmp_path / "refused-p1.pdf"
    with pytest.raises(ToolError, match=r"P=1 prohibits"):
        add_pdf_document_timestamp(
            certified_p1,
            refused_p1,
            timestamp_url=TSA_URL,
            timestamp_opener=_FakeTsa(hierarchy),
        )
    assert not refused_p1.exists()

    plain_p3 = tmp_path / "plain-p3.pdf"
    certified_p3 = tmp_path / "certified-p3.pdf"
    timestamped_p3 = tmp_path / "timestamped-p3.pdf"
    write_blank_pdf(plain_p3, pages=1)
    sign_pdf(
        plain_p3,
        certified_p3,
        pkcs12_path=identity,
        pkcs12_password=SIGNER_PASSWORD,
        field_name="Certification",
        create_field=True,
        certify_permission=3,
    )
    p3_result = add_pdf_document_timestamp(
        certified_p3,
        timestamped_p3,
        timestamp_url=TSA_URL,
        timestamp_opener=_FakeTsa(hierarchy),
    )
    assert p3_result.created_field is True
    assert inspect_pdf_signatures(timestamped_p3).valid_count == 2


def test_document_timestamp_refusals_do_not_publish(tmp_path: Path) -> None:
    hierarchy = _hierarchy(tmp_path, prefix="Phase 35 Refusals")
    source = tmp_path / "plain.pdf"
    write_blank_pdf(source, pages=1)

    wrong_output = tmp_path / "wrong-imprint.pdf"
    with pytest.raises(ToolError, match="does not match"):
        add_pdf_document_timestamp(
            source,
            wrong_output,
            timestamp_url=TSA_URL,
            timestamp_opener=_FakeTsa(hierarchy, wrong_imprint=True),
        )
    assert not wrong_output.exists()

    rejected_output = tmp_path / "rejected.pdf"
    with pytest.raises(ToolError, match="did not grant|status"):
        add_pdf_document_timestamp(
            source,
            rejected_output,
            timestamp_url=TSA_URL,
            timestamp_opener=_FakeTsa(hierarchy, reject=True),
        )
    assert not rejected_output.exists()

    existing_output = tmp_path / "existing.pdf"
    existing_output.write_bytes(b"keep-me")
    with pytest.raises(ToolError, match="already exists"):
        add_pdf_document_timestamp(
            source,
            existing_output,
            timestamp_url=TSA_URL,
            timestamp_opener=_FakeTsa(hierarchy),
        )
    assert existing_output.read_bytes() == b"keep-me"

    signed = tmp_path / "signed.pdf"
    sign_pdf(
        source,
        signed,
        pkcs12_path=tmp_path / "identity.pfx",
        pkcs12_password=TSA_IDENTITY_PASSWORD,
        field_name="Approval",
        create_field=True,
    )
    tampered = tmp_path / "tampered.pdf"
    data = bytearray(signed.read_bytes())
    data[10] ^= 1
    tampered.write_bytes(data)
    assert inspect_pdf_signatures(tampered).invalid_count == 1
    fake = _FakeTsa(hierarchy)
    refused_tampered = tmp_path / "tampered-timestamped.pdf"
    with pytest.raises(ToolError, match="requires every existing"):
        add_pdf_document_timestamp(
            tampered,
            refused_tampered,
            timestamp_url=TSA_URL,
            timestamp_opener=fake,
        )
    assert not refused_tampered.exists()
    assert fake.calls == []

    timestamped = tmp_path / "timestamped.pdf"
    add_pdf_document_timestamp(
        signed,
        timestamped,
        timestamp_url=TSA_URL,
        timestamp_opener=_FakeTsa(hierarchy),
    )
    reused_field = tmp_path / "reused-field.pdf"
    with pytest.raises(ToolError, match="already has a value"):
        add_pdf_document_timestamp(
            timestamped,
            reused_field,
            timestamp_url=TSA_URL,
            field_name="DocTimeStamp",
            timestamp_opener=_FakeTsa(hierarchy),
        )
    assert not reused_field.exists()

    signed_after_timestamp = tmp_path / "signed-after-timestamp.pdf"
    with pytest.raises(ToolError, match="existing document timestamps"):
        sign_pdf(
            timestamped,
            signed_after_timestamp,
            pkcs12_path=tmp_path / "identity.pfx",
            pkcs12_password=TSA_IDENTITY_PASSWORD,
            field_name="LaterApproval",
            create_field=True,
        )
    assert not signed_after_timestamp.exists()


def test_document_timestamp_direct_cli_and_gui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    workspace = settings.workspace_dir
    hierarchy = _hierarchy(workspace, prefix="Phase 35 Direct")
    write_blank_pdf(workspace / "plain.pdf", pages=1)

    parser = build_parser()
    args = parser.parse_args(
        [
            "--root",
            str(tmp_path),
            "pdf",
            "timestamp",
            "plain.pdf",
            "--output",
            "cli-timestamped.pdf",
            "--timestamp-url",
            TSA_URL,
            "--json",
        ]
    )
    assert args.pdf_command == "timestamp"
    assert args.timestamp_url == TSA_URL
    assert args.timestamp_hash_algorithm == "sha256"

    fake = _FakeTsa(hierarchy)
    actual = cli_module.add_pdf_document_timestamp
    monkeypatch.setattr(
        cli_module,
        "add_pdf_document_timestamp",
        lambda source, output, **kwargs: actual(
            source, output, **{**kwargs, "timestamp_opener": fake}
        ),
    )
    monkeypatch.setattr(
        cli_module.getpass,
        "getpass",
        lambda _prompt="": pytest.fail("DocTimeStamp must not ask a password"),
    )

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "pdf",
                "timestamp",
                "plain.pdf",
                "--output",
                "cli-timestamped.pdf",
                "--timestamp-url",
                TSA_URL,
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["output"]["operation"] == "document_timestamp"
    assert payload["output"]["field_name"] == "DocTimeStamp"
    assert payload["output"]["signature_created"] is False
    assert payload["output"]["incremental_update"] is True
    assert payload["output"]["timestamp_status"] == "verified_untrusted"
    assert payload["output"]["tsa_trust_status"] == "not_evaluated"
    assert len(fake.calls) == 1

    controller = GuiController(settings)
    gui_result = controller.pdf_document_timestamp(
        "plain.pdf",
        "gui-timestamped.pdf",
        TSA_URL,
        timestamp_opener=_FakeTsa(hierarchy),
    )
    assert gui_result.success, gui_result.error
    assert gui_result.tool_id == "direct.pdf_document_timestamp"
    assert gui_result.output is not None
    assert gui_result.output["field_name"] == "DocTimeStamp"
    assert gui_result.output["incremental_update"] is True

    with pytest.raises(PcAgentError, match="must stay inside the workspace"):
        controller.pdf_document_timestamp(
            "plain.pdf",
            "../outside.pdf",
            TSA_URL,
            timestamp_opener=_FakeTsa(hierarchy),
        )

    summary = controller.doctor_summary()
    assert summary.version == __version__ == "0.41.0"
    assert summary.builtin_tools == 62
    assert "PHASE 39 Secure .env Key Cleanup" in summary.to_text()


def test_phase35_scope_docs_schema_and_no_registered_tool() -> None:
    project_root = Path(__file__).resolve().parents[1]
    candidates = {path.as_posix() for path in release_candidate_files(project_root)}
    ids = {item.spec.id for item in build_default_registry().list_tools()}

    assert __version__ == "0.41.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(ids) == 62
    assert "pdf.timestamp" not in ids
    assert "direct.pdf_document_timestamp" not in ids
    assert "src/pc_agent/documents/pdf_document_timestamps.py" in candidates
    assert "docs/phase35.md" in candidates
    assert "tests/test_phase35_document_timestamps.py" in candidates

    phase_doc = (project_root / "docs" / "phase35.md").read_text(encoding="utf-8")
    lowered = phase_doc.lower()
    assert "version | 0.37.0" in lowered
    assert "schema | v6" in lowered
    assert "tools | 62" in lowered
    assert "exact byte prefix" in lowered
    assert "never sends the pdf" in lowered
    assert "p=1" in lowered and "p=2" in lowered
    assert "not_evaluated" in lowered
    assert "pades/ltv" in lowered and "not" in lowered
    assert "signer workflow still refuses" in lowered

    gui_source = (
        project_root / "src" / "pc_agent" / "gui" / "app.py"
    ).read_text(encoding="utf-8")
    assert "Add DocTimeStamp" in gui_source
    assert "pdf_document_timestamp" in gui_source
