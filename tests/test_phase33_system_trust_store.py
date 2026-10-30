"""Phase 33: opt-in system trust-store anchors for PDF validation."""

from __future__ import annotations

import json
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

import pc_agent.documents.pdf_system_trust as system_trust_module
import pc_agent.documents.pdf_trust as pdf_trust_module
from pc_agent import __version__
from pc_agent.cli import build_parser, main
from pc_agent.config import Settings
from pc_agent.documents.pdf import write_blank_pdf
from pc_agent.documents.pdf_signing import sign_pdf
from pc_agent.documents.pdf_system_trust import (
    SystemTrustStore,
    load_system_trust_store,
)
from pc_agent.documents.pdf_trust import validate_pdf_signature_trust
from pc_agent.errors import ToolError
from pc_agent.gui.controller import GuiController
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

PFX_PASSWORD = "pfx-secret-phase33"
SOURCE = "synthetic-system-store"


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
            x509.BasicConstraints(ca=ca, path_length=0 if ca else None),
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


def _hierarchy(folder: Path) -> dict[str, Any]:
    folder.mkdir(parents=True, exist_ok=True)
    root_key = ec.generate_private_key(ec.SECP256R1())
    signer_key = ec.generate_private_key(ec.SECP256R1())
    root_name = _name("Phase 33 Synthetic System Root")
    signer_name = _name("Phase 33 Signer")
    root_cert = _certificate(
        subject=root_name,
        issuer=root_name,
        public_key=root_key.public_key(),
        issuer_key=root_key,
        serial=3301,
        ca=True,
    )
    signer_cert = _certificate(
        subject=signer_name,
        issuer=root_name,
        public_key=signer_key.public_key(),
        issuer_key=root_key,
        serial=3302,
        ca=False,
    )
    (folder / "root-ca.pem").write_bytes(
        root_cert.public_bytes(serialization.Encoding.PEM)
    )
    (folder / "identity.pfx").write_bytes(
        pkcs12.serialize_key_and_certificates(
            b"Phase 33 Signer",
            signer_key,
            signer_cert,
            [root_cert],
            serialization.BestAvailableEncryption(PFX_PASSWORD.encode()),
        )
    )
    now = datetime.now(UTC)
    crl = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(root_name)
        .last_update(now - timedelta(hours=1))
        .next_update(now + timedelta(days=1))
        .sign(private_key=root_key, algorithm=hashes.SHA256())
    )
    (folder / "root.crl").write_bytes(
        crl.public_bytes(serialization.Encoding.PEM)
    )
    return {"root_cert": root_cert, "signer_cert": signer_cert}


def _signed(folder: Path) -> Path:
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


def _loader(hierarchy: dict[str, Any]):
    return lambda: SystemTrustStore(
        certificates=(
            hierarchy["root_cert"],
            hierarchy["signer_cert"],
        ),
        source=SOURCE,
        skipped_count=2,
    )


def test_system_trust_loader_filters_dedupes_and_platform_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hierarchy = _hierarchy(tmp_path)
    root = hierarchy["root_cert"]
    signer = hierarchy["signer_cert"]
    root_der = root.public_bytes(serialization.Encoding.DER)

    loaded = load_system_trust_store(
        lambda: (
            [
                (root_der, "x509_asn", True),
                root_der,
                signer,
                b"not a certificate",
            ],
            "synthetic-windows-root",
        )
    )
    assert loaded.source == "synthetic-windows-root"
    assert loaded.certificates == (root,)
    assert loaded.skipped_count == 3

    normalised = load_system_trust_store(_loader(hierarchy))
    assert normalised.source == SOURCE
    assert normalised.certificates == (root,)
    assert normalised.skipped_count == 3

    with pytest.raises(ToolError, match="did not provide any usable"):
        load_system_trust_store(list)
    with pytest.raises(ToolError, match="Could not load.*RuntimeError") as raised:
        load_system_trust_store(lambda: (_ for _ in ()).throw(RuntimeError("secret-path")))
    assert "secret-path" not in str(raised.value)

    monkeypatch.setattr(system_trust_module.sys, "platform", "win32")
    monkeypatch.setattr(
        ssl,
        "enum_certificates",
        lambda store: [(root_der, "x509_asn", True)]
        if store == "ROOT"
        else [],
        raising=False,
    )
    windows = load_system_trust_store()
    assert windows.source == "windows-root-certificate-store"
    assert windows.certificates == (root,)

    monkeypatch.setattr(system_trust_module.sys, "platform", "linux")

    class FakeContext:
        def get_ca_certs(self, binary_form: bool = False):
            assert binary_form is True
            return [root_der]

    monkeypatch.setattr(
        ssl, "create_default_context", lambda purpose: FakeContext()
    )
    openssl_store = load_system_trust_store()
    assert openssl_store.source == "openssl-default-certificate-store"
    assert openssl_store.certificates == (root,)


def test_system_anchors_are_opt_in_and_signer_tsa_separate(
    tmp_path: Path,
) -> None:
    hierarchy = _hierarchy(tmp_path)
    signed = _signed(tmp_path)
    crl = tmp_path / "root.crl"

    disabled = validate_pdf_signature_trust(
        signed,
        crl_paths=[crl],
        system_trust_loader=lambda: (_ for _ in ()).throw(
            AssertionError("loader must not run while disabled")
        ),
    )
    assert disabled.trusted_valid_count == 0
    assert disabled.signatures[0].chain_status == "untrusted"
    disabled_summary = disabled.summary()
    assert disabled_summary["system_trust_enabled"] is False
    assert disabled_summary["effective_trust_anchor_count"] == 0
    assert disabled_summary["system_trust_source"] is None

    signer_system = validate_pdf_signature_trust(
        signed,
        crl_paths=[crl],
        system_trust_anchors=True,
        system_trust_loader=_loader(hierarchy),
    )
    assert signer_system.trusted_valid_count == 1
    summary = signer_system.summary()
    assert summary["system_trust_enabled"] is True
    assert summary["system_trust_anchor_count"] == 1
    assert summary["local_trust_anchor_count"] == 0
    assert summary["effective_trust_anchor_count"] == 1
    assert summary["timestamp_system_trust_enabled"] is False
    assert summary["timestamp_system_trust_anchor_count"] == 0
    assert summary["effective_timestamp_trust_anchor_count"] == 0
    assert summary["system_trust_source"] == SOURCE
    assert summary["system_trust_skipped_count"] == 3
    assert summary["adobe_trust_status"] == "not_evaluated"
    assert summary["network_used"] is False
    assert "BEGIN CERTIFICATE" not in json.dumps(summary)
    assert PFX_PASSWORD not in json.dumps(summary)

    tsa_only = validate_pdf_signature_trust(
        signed,
        crl_paths=[crl],
        timestamp_system_trust_anchors=True,
        system_trust_loader=_loader(hierarchy),
    )
    assert tsa_only.trusted_valid_count == 0
    tsa_summary = tsa_only.summary()
    assert tsa_summary["effective_trust_anchor_count"] == 0
    assert tsa_summary["timestamp_system_trust_anchor_count"] == 1
    assert tsa_summary["effective_timestamp_trust_anchor_count"] == 1

    with pytest.raises(ToolError, match="synthetic store unavailable"):
        validate_pdf_signature_trust(
            signed,
            trust_anchor_paths=[tmp_path / "root-ca.pem"],
            system_trust_anchors=True,
            system_trust_loader=lambda: (_ for _ in ()).throw(
                ToolError("synthetic store unavailable")
            ),
        )


def test_system_trust_direct_cli_and_gui(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    workspace = settings.workspace_dir
    hierarchy = _hierarchy(workspace)
    _signed(workspace)
    monkeypatch.setattr(
        pdf_trust_module,
        "load_system_trust_store",
        lambda loader=None: SystemTrustStore(
            certificates=(hierarchy["root_cert"],),
            source=SOURCE,
        ),
    )
    root_args = ["--root", str(tmp_path)]

    assert main(
        [
            *root_args,
            "pdf",
            "validate-signatures",
            "signed.pdf",
            "--crl",
            "root.crl",
            "--system-trust-anchors",
            "--json",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["output"]["trusted_valid_count"] == 1
    assert payload["output"]["system_trust_anchor_count"] == 1
    assert payload["output"]["trust_anchors"] == []

    assert main(
        [
            *root_args,
            "pdf",
            "validate-signatures",
            "signed.pdf",
            "--crl",
            "root.crl",
            "--system-trust-anchors",
        ]
    ) == 0
    text = capsys.readouterr().out
    assert "Trust anchors: 1 (0 local, 1 system)" in text
    assert SOURCE in text
    assert PFX_PASSWORD not in text

    controller = GuiController(settings)
    result = controller.pdf_validate_signatures(
        "signed.pdf",
        "",
        "root.crl",
        system_trust_anchors=True,
    )
    assert result.success, result.error
    assert result.tool_id == "direct.pdf_validate_signatures"
    assert result.output is not None
    assert result.output["system_trust_anchor_count"] == 1

    def unavailable(loader=None):
        raise ToolError("synthetic store unavailable")

    monkeypatch.setattr(pdf_trust_module, "load_system_trust_store", unavailable)
    failed = controller.pdf_validate_signatures(
        "signed.pdf",
        system_trust_anchors=True,
    )
    assert failed.success is False
    assert "synthetic store unavailable" in (failed.error or "")

    summary = controller.doctor_summary()
    assert summary.version == __version__ == "0.41.0"
    assert summary.builtin_tools == 62
    assert "PHASE 39 Secure .env Key Cleanup" in summary.to_text()


def test_phase33_scope_defaults_docs_and_no_registered_tool() -> None:
    project_root = Path(__file__).resolve().parents[1]
    candidates = {path.as_posix() for path in release_candidate_files(project_root)}
    ids = {item.spec.id for item in build_default_registry().list_tools()}
    args = build_parser().parse_args(
        ["pdf", "validate-signatures", "signed.pdf"]
    )

    assert __version__ == "0.41.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(ids) == 62
    assert args.system_trust_anchors is False
    assert args.timestamp_system_trust_anchors is False
    assert "pdf.validate_signatures" not in ids
    assert "direct.pdf_validate_signatures" not in ids
    assert "src/pc_agent/documents/pdf_system_trust.py" in candidates
    assert "docs/phase33.md" in candidates
    assert "tests/test_phase33_system_trust_store.py" in candidates

    app_source = (project_root / "src/pc_agent/gui/app.py").read_text(
        encoding="utf-8"
    )
    assert "Also use system CA store for signer chains" in app_source
    assert "Also use system CA store for TSA chains" in app_source

    phase_doc = (project_root / "docs/phase33.md").read_text(encoding="utf-8")
    lowered = phase_doc.lower()
    assert "off by default" in lowered
    assert "windows root" in lowered
    assert "openssl" in lowered
    assert "not adobe" in lowered
    assert "no network" in lowered
    assert "no registered tool" in lowered
