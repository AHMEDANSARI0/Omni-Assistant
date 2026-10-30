"""Phase 32: certificate policies and name constraints for PDF trust."""

from __future__ import annotations

import ipaddress
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtensionOID, NameOID

from pc_agent import __version__
from pc_agent.cli import build_parser
from pc_agent.documents.pdf import write_blank_pdf
from pc_agent.documents.pdf_policy import (
    ANY_POLICY_OID,
    CertificatePolicyOptions,
    evaluate_chain_constraints,
    parse_policy_oids,
)
from pc_agent.documents.pdf_signing import sign_pdf
from pc_agent.documents.pdf_timestamps import _oid, _sequence
from pc_agent.documents.pdf_trust import validate_pdf_signature_trust
from pc_agent.errors import ToolError
from pc_agent.gui.controller import GuiController
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

PFX_PASSWORD = "pfx-secret-phase32"
POLICY_A = "1.2.3.4.1"
POLICY_B = "1.2.3.4.2"


def _name(common_name: str, *, organization: str = "Phase32") -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def _policy_mapping_der(mappings: tuple[tuple[str, str], ...]) -> bytes:
    return _sequence(
        *(_sequence(_oid(issuer), _oid(subject)) for issuer, subject in mappings)
    )


def _certificate(
    *,
    subject: x509.Name,
    issuer: x509.Name,
    public_key: Any,
    issuer_key: Any,
    serial: int,
    ca: bool,
    path_length: int | None,
    policies: tuple[str, ...] | None = None,
    policy_mappings: tuple[tuple[str, str], ...] = (),
    policy_constraints: tuple[int | None, int | None] | None = None,
    inhibit_any_policy: int | None = None,
    name_constraints: tuple[
        tuple[Any, ...], tuple[Any, ...]
    ]
    | None = None,
    san: tuple[Any, ...] = (),
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
            x509.BasicConstraints(ca=ca, path_length=path_length if ca else None),
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
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(public_key),
            critical=False,
        )
    )
    if policies is not None:
        builder = builder.add_extension(
            x509.CertificatePolicies(
                [
                    x509.PolicyInformation(
                        x509.ObjectIdentifier(policy),
                        None,
                    )
                    for policy in policies
                ]
            ),
            critical=False,
        )
    if policy_mappings:
        builder = builder.add_extension(
            x509.UnrecognizedExtension(
                ExtensionOID.POLICY_MAPPINGS,
                _policy_mapping_der(policy_mappings),
            ),
            critical=True,
        )
    if policy_constraints is not None:
        builder = builder.add_extension(
            x509.PolicyConstraints(
                require_explicit_policy=policy_constraints[0],
                inhibit_policy_mapping=policy_constraints[1],
            ),
            critical=True,
        )
    if inhibit_any_policy is not None:
        builder = builder.add_extension(
            x509.InhibitAnyPolicy(inhibit_any_policy),
            critical=True,
        )
    if name_constraints is not None:
        permitted, excluded = name_constraints
        builder = builder.add_extension(
            x509.NameConstraints(
                permitted_subtrees=list(permitted) or None,
                excluded_subtrees=list(excluded) or None,
            ),
            critical=True,
        )
    if san:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(list(san)),
            critical=False,
        )
    return builder.sign(issuer_key, hashes.SHA256())


def _hierarchy(
    folder: Path | None = None,
    *,
    root_policies: tuple[str, ...] | None = None,
    intermediate_policies: tuple[str, ...] | None = (POLICY_A,),
    signer_policies: tuple[str, ...] | None = (POLICY_A,),
    intermediate_mappings: tuple[tuple[str, str], ...] = (),
    intermediate_policy_constraints: tuple[int | None, int | None] | None = None,
    root_name_constraints: tuple[tuple[Any, ...], tuple[Any, ...]] | None = None,
    signer_san: tuple[Any, ...] = (),
    signer_organization: str = "Phase32",
) -> dict[str, Any]:
    root_key = ec.generate_private_key(ec.SECP256R1())
    intermediate_key = ec.generate_private_key(ec.SECP256R1())
    signer_key = ec.generate_private_key(ec.SECP256R1())
    root_name = _name("Phase 32 Root CA")
    intermediate_name = _name("Phase 32 Intermediate CA")
    root_cert = _certificate(
        subject=root_name,
        issuer=root_name,
        public_key=root_key.public_key(),
        issuer_key=root_key,
        serial=3201,
        ca=True,
        path_length=1,
        policies=root_policies,
        name_constraints=root_name_constraints,
    )
    intermediate_cert = _certificate(
        subject=intermediate_name,
        issuer=root_name,
        public_key=intermediate_key.public_key(),
        issuer_key=root_key,
        serial=3202,
        ca=True,
        path_length=0,
        policies=intermediate_policies,
        policy_mappings=intermediate_mappings,
        policy_constraints=intermediate_policy_constraints,
    )
    signer_cert = _certificate(
        subject=_name(
            "Phase 32 Signer", organization=signer_organization
        ),
        issuer=intermediate_name,
        public_key=signer_key.public_key(),
        issuer_key=intermediate_key,
        serial=3203,
        ca=False,
        path_length=None,
        policies=signer_policies,
        san=signer_san,
    )
    result: dict[str, Any] = {
        "root_key": root_key,
        "intermediate_key": intermediate_key,
        "signer_key": signer_key,
        "root_cert": root_cert,
        "intermediate_cert": intermediate_cert,
        "signer_cert": signer_cert,
    }
    if folder is not None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "root-ca.pem").write_bytes(
            root_cert.public_bytes(serialization.Encoding.PEM)
        )
        (folder / "identity.pfx").write_bytes(
            pkcs12.serialize_key_and_certificates(
                b"Phase 32 Signer",
                signer_key,
                signer_cert,
                [intermediate_cert, root_cert],
                serialization.BestAvailableEncryption(PFX_PASSWORD.encode()),
            )
        )
    return result


def _evaluate(
    hierarchy: dict[str, Any],
    options: CertificatePolicyOptions | None = None,
):
    root = hierarchy["root_cert"]
    return evaluate_chain_constraints(
        (
            hierarchy["signer_cert"],
            hierarchy["intermediate_cert"],
            root,
        ),
        anchor_fingerprints={root.fingerprint(hashes.SHA256()).hex()},
        options=options,
    )


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
        .last_update(now - timedelta(minutes=5))
        .next_update(now + timedelta(days=1))
        .sign(private_key=issuer_key, algorithm=hashes.SHA256())
    )
    path.write_bytes(crl.public_bytes(serialization.Encoding.PEM))


def _signed_pdf_and_crls(folder: Path, hierarchy: dict[str, Any]) -> tuple[Path, tuple[Path, Path]]:
    source = folder / "plain.pdf"
    signed = folder / "signed.pdf"
    write_blank_pdf(source, pages=1)
    sign_pdf(
        source,
        signed,
        pkcs12_path=folder / "identity.pfx",
        pkcs12_password=PFX_PASSWORD,
    )
    signer_crl = folder / "intermediate.crl"
    root_crl = folder / "root.crl"
    _write_crl(
        signer_crl,
        issuer_cert=hierarchy["intermediate_cert"],
        issuer_key=hierarchy["intermediate_key"],
    )
    _write_crl(
        root_crl,
        issuer_cert=hierarchy["root_cert"],
        issuer_key=hierarchy["root_key"],
    )
    return signed, (signer_crl, root_crl)


def test_policy_oid_parsing_is_bounded_and_canonical() -> None:
    assert parse_policy_oids(["1.2.3, 2.5.29.32.0", "1.2.3"]) == (
        "1.2.3",
        ANY_POLICY_OID,
    )
    assert parse_policy_oids("  \n") == ()
    with pytest.raises(ToolError, match="Invalid certificate policy OID"):
        parse_policy_oids(["3.2.1"])
    with pytest.raises(ToolError, match="Invalid certificate policy OID"):
        parse_policy_oids(["1.02.3"])


def test_certificate_policy_graph_required_any_and_explicit() -> None:
    hierarchy = _hierarchy(root_policies=(POLICY_B,))
    result = _evaluate(
        hierarchy,
        CertificatePolicyOptions(required_policy_oids=(POLICY_A,)),
    )
    assert result.policy_status == "valid"
    assert result.policy_oids == (POLICY_A,)
    assert result.name_constraints_status == "valid"

    wrong = _hierarchy(signer_policies=(POLICY_B,))
    result = _evaluate(
        wrong,
        CertificatePolicyOptions(required_policy_oids=(POLICY_A,)),
    )
    assert result.policy_status == "invalid"

    via_any = _hierarchy(intermediate_policies=(ANY_POLICY_OID,))
    result = _evaluate(
        via_any,
        CertificatePolicyOptions(required_policy_oids=(POLICY_A,)),
    )
    assert result.policy_status == "valid"
    result = _evaluate(
        via_any,
        CertificatePolicyOptions(
            required_policy_oids=(POLICY_A,), inhibit_any_policy=True
        ),
    )
    assert result.policy_status == "invalid"

    no_policies = _hierarchy(
        intermediate_policies=None, signer_policies=None
    )
    assert _evaluate(no_policies).policy_status == "valid"
    assert (
        _evaluate(
            no_policies,
            CertificatePolicyOptions(explicit_policy_required=True),
        ).policy_status
        == "invalid"
    )


def test_policy_mappings_constraints_and_malformed_any_mapping() -> None:
    mapped = _hierarchy(
        intermediate_policies=(POLICY_B,),
        intermediate_mappings=((POLICY_B, POLICY_A),),
        signer_policies=(POLICY_A,),
    )
    result = _evaluate(
        mapped,
        CertificatePolicyOptions(required_policy_oids=(POLICY_A,)),
    )
    assert result.policy_status == "valid"
    assert result.policy_oids == (POLICY_A,)
    result = _evaluate(
        mapped,
        CertificatePolicyOptions(
            required_policy_oids=(POLICY_A,), inhibit_policy_mapping=True
        ),
    )
    assert result.policy_status == "invalid"

    require_explicit = _hierarchy(
        intermediate_policies=(POLICY_A,),
        intermediate_policy_constraints=(0, None),
        signer_policies=None,
    )
    assert _evaluate(require_explicit).policy_status == "invalid"

    malformed = _hierarchy(
        intermediate_policies=(POLICY_B,),
        intermediate_mappings=((POLICY_B, ANY_POLICY_OID),),
    )
    result = _evaluate(malformed)
    assert result.policy_status == "invalid"
    assert "must not map" in result.policy_detail


def test_name_constraints_dns_ip_directory_email_uri_and_oid() -> None:
    allowed = _hierarchy(
        root_name_constraints=(
            (
                x509.DNSName("example.com"),
                x509.DirectoryName(
                    x509.Name(
                        [
                            x509.NameAttribute(
                                NameOID.ORGANIZATION_NAME, "Phase32"
                            )
                        ]
                    )
                ),
            ),
            (),
        ),
        signer_san=(x509.DNSName("signer.example.com"),),
    )
    assert _evaluate(allowed).name_constraints_status == "valid"

    denied_dns = _hierarchy(
        root_name_constraints=((x509.DNSName("example.com"),), ()),
        signer_san=(x509.DNSName("signer.example.org"),),
    )
    result = _evaluate(denied_dns)
    assert result.name_constraints_status == "invalid"
    assert "outside the permitted subtree" in result.name_constraints_detail

    combined_good = _hierarchy(
        root_name_constraints=(
            (
                x509.RFC822Name(".example.com"),
                x509.UniformResourceIdentifier(".example.com"),
                x509.RegisteredID(x509.ObjectIdentifier("1.2.3")),
            ),
            (
                x509.IPAddress(ipaddress.ip_network("203.0.113.0/24")),
            ),
        ),
        signer_san=(
            x509.RFC822Name("Signer@Mail.Example.com"),
            x509.UniformResourceIdentifier("https://Docs.Example.com/a"),
            x509.RegisteredID(x509.ObjectIdentifier("1.2.3.4")),
            x509.IPAddress(ipaddress.ip_address("192.0.2.10")),
        ),
    )
    assert _evaluate(combined_good).name_constraints_status == "valid"

    excluded_ip = _hierarchy(
        root_name_constraints=(
            (),
            (x509.IPAddress(ipaddress.ip_network("203.0.113.0/24")),),
        ),
        signer_san=(
            x509.IPAddress(ipaddress.ip_address("203.0.113.9")),
        ),
    )
    result = _evaluate(excluded_ip)
    assert result.name_constraints_status == "invalid"
    assert "excluded subtree" in result.name_constraints_detail

    wrong_directory = _hierarchy(
        root_name_constraints=(
            (
                x509.DirectoryName(
                    x509.Name(
                        [
                            x509.NameAttribute(
                                NameOID.ORGANIZATION_NAME, "Phase32"
                            )
                        ]
                    )
                ),
            ),
            (),
        ),
        signer_organization="Other",
    )
    assert _evaluate(wrong_directory).name_constraints_status == "invalid"


def test_pdf_policy_and_name_constraint_statuses_gate_trusted_valid(
    tmp_path: Path,
) -> None:
    hierarchy = _hierarchy(tmp_path / "valid")
    signed, crls = _signed_pdf_and_crls(tmp_path / "valid", hierarchy)
    anchor = tmp_path / "valid" / "root-ca.pem"

    validation = validate_pdf_signature_trust(
        signed,
        trust_anchor_paths=[anchor],
        crl_paths=list(crls),
        required_policy_oids=[POLICY_A],
    )
    signature = validation.signatures[0]
    assert signature.overall_status == "trusted_valid"
    assert signature.policy_status == "valid"
    assert signature.policy_oids == (POLICY_A,)
    assert signature.name_constraints_status == "valid"
    assert signature.signers[0].policy_detail
    summary = validation.summary()
    assert summary["policy_status"] == (
        "bounded_rfc5280_subset_policies_and_name_constraints"
    )
    assert summary["required_policy_oids"] == [POLICY_A]
    assert summary["policy_invalid_count"] == 0
    assert PFX_PASSWORD not in json.dumps(summary)

    wrong_policy = validate_pdf_signature_trust(
        signed,
        trust_anchor_paths=[anchor],
        crl_paths=list(crls),
        required_policy_oids=[POLICY_B],
    )
    assert wrong_policy.signatures[0].policy_status == "invalid"
    assert wrong_policy.signatures[0].overall_status == (
        "trusted_chain_policy_invalid"
    )
    assert wrong_policy.trusted_valid_count == 0
    assert wrong_policy.policy_invalid_count == 1

    nc_folder = tmp_path / "name-constraints"
    nc_hierarchy = _hierarchy(
        nc_folder,
        root_name_constraints=((x509.DNSName("example.com"),), ()),
        signer_san=(x509.DNSName("signer.example.org"),),
    )
    nc_signed, nc_crls = _signed_pdf_and_crls(nc_folder, nc_hierarchy)
    nc_validation = validate_pdf_signature_trust(
        nc_signed,
        trust_anchor_paths=[nc_folder / "root-ca.pem"],
        crl_paths=list(nc_crls),
    )
    nc_signature = nc_validation.signatures[0]
    assert nc_signature.chain_status == "trusted"
    assert nc_signature.revocation_status == "not_revoked"
    assert nc_signature.name_constraints_status == "invalid"
    assert nc_signature.overall_status == (
        "trusted_chain_name_constraints_invalid"
    )
    assert nc_validation.name_constraints_invalid_count == 1

    with pytest.raises(ToolError, match="Invalid certificate policy OID"):
        validate_pdf_signature_trust(
            signed,
            trust_anchor_paths=[anchor],
            required_policy_oids=["not-an-oid"],
        )


def test_phase32_direct_cli_gui_scope_docs_and_registry() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "pdf",
            "validate-signatures",
            "signed.pdf",
            "--required-policy",
            POLICY_A,
            "--timestamp-required-policy",
            POLICY_B,
            "--explicit-policy",
            "--inhibit-any-policy",
            "--inhibit-policy-mapping",
        ]
    )
    assert args.required_policy == [POLICY_A]
    assert args.timestamp_required_policy == [POLICY_B]
    assert args.explicit_policy is True
    assert args.inhibit_any_policy is True
    assert args.inhibit_policy_mapping is True
    defaults = parser.parse_args(["pdf", "validate-signatures", "signed.pdf"])
    assert defaults.required_policy == []
    assert defaults.timestamp_required_policy == []
    assert defaults.explicit_policy is False
    assert hasattr(GuiController, "pdf_validate_signatures")

    root = Path(__file__).resolve().parents[1]
    app_source = (root / "src/pc_agent/gui/app.py").read_text(
        encoding="utf-8"
    )
    assert "Required signer policy OIDs" in app_source
    assert "Required TSA policy OIDs" in app_source
    assert "Name constraints are always checked" in app_source
    controller_source = (root / "src/pc_agent/gui/controller.py").read_text(
        encoding="utf-8"
    )
    assert "required_policy_oids=required_policy_oids" in controller_source
    assert "inhibit_policy_mapping=inhibit_policy_mapping" in controller_source

    assert __version__ == "0.41.0"
    assert LATEST_SCHEMA_VERSION == 6
    registry = build_default_registry()
    tools = registry.list_tools()
    assert len(tools) == 62
    assert not any(
        "policy" in tool.spec.id or "name_constraint" in tool.spec.id
        for tool in tools
    )
    release_paths = {path.as_posix() for path in release_candidate_files(root)}
    assert "src/pc_agent/documents/pdf_policy.py" in release_paths
    assert "tests/test_phase32_policy_name_constraints.py" in release_paths
    assert "docs/phase32.md" in release_paths
