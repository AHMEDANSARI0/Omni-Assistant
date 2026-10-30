"""Direct-only LTV DSS embedding for already signed PDFs.

Phase 36 appends a Document Security Store (DSS) and signature VRI entries
through a true incremental update.  It embeds only DER validation material
that is already present in the document, already present in a prior DSS, or
explicitly supplied from local files and proven relevant to a valid signed
field.  Certificate trust and PAdES/legal conformance are not evaluated.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.x509 import ocsp

from ..errors import ToolError
from .pdf import _load_pypdf, inspect_pdf, sha256_file
from .pdf_document_timestamps import (
    _classify_existing_fields,
    _verify_preserved_fields,
    add_pdf_document_timestamp,
)
from .pdf_forms import (
    MAX_PDF_FORM_PAGES,
    _as_name,
    _as_text,
    _get,
    _object,
    _raw_fields,
    inspect_pdf_form,
)
from .pdf_revocation import MAX_OCSP_RESPONSE_BYTES, _validate_ocsp_response
from .pdf_signatures import (
    MAX_PDF_SIGNATURE_CONTENTS_BYTES,
    MAX_PDF_SIGNATURE_FILE_BYTES,
    PdfCertificateSummary,
    PdfSignatureInspection,
    _certificate_summary,
    _contents_bytes,
    _parse_der_root,
    inspect_pdf_signatures,
)
from .pdf_signing import MAX_PDF_SIGNING_FILE_BYTES, MAX_PDF_SIGNING_PAGES
from .pdf_timestamps import MAX_TIMESTAMP_TOKEN_BYTES
from .pdf_trust import (
    _PEM_CERTIFICATE,
    MAX_CRL_FILE_BYTES,
    MAX_CRL_FILES,
    MAX_TRUST_ANCHOR_CERTIFICATES,
    MAX_TRUST_ANCHOR_FILE_BYTES,
    MAX_TRUST_ANCHOR_FILES,
    PdfCrlSummary,
    _crl_status_for_certificate,
    _fingerprint,
    _iter_input_files,
    _load_crls,
    _read_bounded,
)

MAX_DSS_CERTIFICATES = MAX_TRUST_ANCHOR_CERTIFICATES
MAX_DSS_CRLS = MAX_CRL_FILES
MAX_DSS_OCSP_RESPONSES = 50
MAX_DSS_VRI_ENTRIES = 100
MAX_DSS_CERTIFICATE_BYTES = 262_144
MAX_DSS_CRL_BYTES = MAX_CRL_FILE_BYTES
MAX_DSS_OCSP_BYTES = MAX_OCSP_RESPONSE_BYTES
MAX_DSS_TOTAL_MATERIAL_BYTES = 25_000_000

_CERTIFICATE_SUFFIXES = {".cer", ".crt", ".der", ".pem"}
_OCSP_SUFFIXES = {".der", ".ocsp", ".pem"}
_VRI_IDENTIFIER = re.compile(r"^[0-9A-F]{40}$")
_MATERIAL_LIMITS = {
    "certificate": (MAX_DSS_CERTIFICATES, MAX_DSS_CERTIFICATE_BYTES),
    "crl": (MAX_DSS_CRLS, MAX_DSS_CRL_BYTES),
    "ocsp": (MAX_DSS_OCSP_RESPONSES, MAX_DSS_OCSP_BYTES),
}


@dataclass(frozen=True, slots=True)
class PdfOcspSummary:
    sha256_fingerprint: str
    response_status: str
    certificate_status: str | None
    serial_number: str | None
    responder_name: str | None
    responder_certificate_fingerprints: tuple[str, ...]
    this_update: str | None
    next_update: str | None
    produced_at: str | None

    def summary(self) -> dict[str, Any]:
        return {
            "sha256_fingerprint": self.sha256_fingerprint,
            "response_status": self.response_status,
            "certificate_status": self.certificate_status,
            "serial_number": self.serial_number,
            "responder_name": self.responder_name,
            "responder_certificate_fingerprints": list(
                self.responder_certificate_fingerprints
            ),
            "this_update": self.this_update,
            "next_update": self.next_update,
            "produced_at": self.produced_at,
        }


@dataclass(frozen=True, slots=True)
class _DssMaterial:
    kind: str
    der: bytes = field(repr=False, compare=False)
    sha256: str = ""
    certificate: x509.Certificate | None = field(
        default=None, repr=False, compare=False
    )
    crl: x509.CertificateRevocationList | None = field(
        default=None, repr=False, compare=False
    )
    ocsp_response: ocsp.OCSPResponse | None = field(
        default=None, repr=False, compare=False
    )


@dataclass(frozen=True, slots=True)
class PdfDssVriSummary:
    identifier: str
    field_names: tuple[str, ...]
    certificate_fingerprints: tuple[str, ...]
    crl_fingerprints: tuple[str, ...]
    ocsp_fingerprints: tuple[str, ...]
    tu: str | None
    ts_present: bool
    ts_sha256: str | None
    ts_der: bytes = field(default=b"", repr=False, compare=False)

    def summary(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "field_names": list(self.field_names),
            "certificate_fingerprints": list(self.certificate_fingerprints),
            "crl_fingerprints": list(self.crl_fingerprints),
            "ocsp_fingerprints": list(self.ocsp_fingerprints),
            "tu": self.tu,
            "ts_present": self.ts_present,
            "ts_sha256": self.ts_sha256,
        }


@dataclass(frozen=True, slots=True)
class PdfDssInspection:
    path: Path
    pages: int
    size_bytes: int
    sha256: str
    present: bool
    certificates: tuple[PdfCertificateSummary, ...]
    crls: tuple[PdfCrlSummary, ...]
    ocsps: tuple[PdfOcspSummary, ...]
    vri: tuple[PdfDssVriSummary, ...]
    extensions_esic: bool
    materials: tuple[_DssMaterial, ...] = field(
        default=(), repr=False, compare=False
    )

    @property
    def certificate_count(self) -> int:
        return len(self.certificates)

    @property
    def crl_count(self) -> int:
        return len(self.crls)

    @property
    def ocsp_count(self) -> int:
        return len(self.ocsps)

    @property
    def vri_count(self) -> int:
        return len(self.vri)

    def summary(self) -> dict[str, Any]:
        return {
            "operation": "inspect_ltv_dss",
            "pages": self.pages,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "dss_present": self.present,
            "extensions_esic": self.extensions_esic,
            "certificate_count": self.certificate_count,
            "crl_count": self.crl_count,
            "ocsp_count": self.ocsp_count,
            "vri_count": self.vri_count,
            "certificates": [item.summary() for item in self.certificates],
            "crls": [item.summary() for item in self.crls],
            "ocsps": [item.summary() for item in self.ocsps],
            "vri": [item.summary() for item in self.vri],
            "trust_status": "not_evaluated",
            "pades_status": "not_evaluated",
        }


@dataclass(frozen=True, slots=True)
class PdfLtvResult:
    source_path: Path
    output_path: Path
    status: str
    pages: int
    size_bytes: int
    sha256: str
    signed_field_count: int
    approval_signature_count: int
    existing_document_timestamp_count: int
    preserved_valid_count: int
    total_signed_field_count: int
    source_revision_bytes: int
    existing_dss_present: bool
    dss_updated: bool
    existing_certificate_count: int
    added_certificate_count: int
    certificate_count: int
    existing_crl_count: int
    added_crl_count: int
    crl_count: int
    existing_ocsp_count: int
    added_ocsp_count: int
    ocsp_count: int
    vri_count: int
    vri_field_names: tuple[str, ...]
    supplied_certificate_count: int
    supplied_crl_count: int
    supplied_ocsp_count: int
    timestamp_requested: bool
    timestamp_field_name: str | None = None
    timestamp_status: str | None = None
    timestamp_time: str | None = None
    tsa_subject: str | None = None
    incremental_update: bool = True
    verified: bool = True

    def summary(self) -> dict[str, Any]:
        return {
            "operation": "add_ltv_dss",
            "status": self.status,
            "verified": self.verified,
            "material_status": "verified_relevant",
            "trust_status": "not_evaluated",
            "pades_status": "not_evaluated",
            "pages": self.pages,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "signed_field_count": self.signed_field_count,
            "approval_signature_count": self.approval_signature_count,
            "existing_document_timestamp_count": (
                self.existing_document_timestamp_count
            ),
            "preserved_valid_count": self.preserved_valid_count,
            "total_signed_field_count": self.total_signed_field_count,
            "source_revision_bytes": self.source_revision_bytes,
            "incremental_update": self.incremental_update,
            "existing_dss_present": self.existing_dss_present,
            "dss_updated": self.dss_updated,
            "existing_certificate_count": self.existing_certificate_count,
            "added_certificate_count": self.added_certificate_count,
            "certificate_count": self.certificate_count,
            "existing_crl_count": self.existing_crl_count,
            "added_crl_count": self.added_crl_count,
            "crl_count": self.crl_count,
            "existing_ocsp_count": self.existing_ocsp_count,
            "added_ocsp_count": self.added_ocsp_count,
            "ocsp_count": self.ocsp_count,
            "vri_count": self.vri_count,
            "vri_field_names": list(self.vri_field_names),
            "supplied_certificate_count": self.supplied_certificate_count,
            "supplied_crl_count": self.supplied_crl_count,
            "supplied_ocsp_count": self.supplied_ocsp_count,
            "timestamp_requested": self.timestamp_requested,
            "timestamp_field_name": self.timestamp_field_name,
            "timestamp_status": self.timestamp_status,
            "timestamp_time": self.timestamp_time,
            "tsa_subject": self.tsa_subject,
            "tsa_trust_status": (
                "not_evaluated" if self.timestamp_requested else None
            ),
        }


@dataclass(slots=True)
class _VriPlan:
    identifier: str
    certificates: set[str]
    crls: set[str]
    ocsps: set[str]
    tu: str | None = None
    ts_der: bytes = b""


@dataclass(slots=True)
class _DssPlan:
    materials: dict[str, dict[str, _DssMaterial]]
    vri: dict[str, _VriPlan]
    field_identifiers: dict[str, str]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _certificate_material(certificate: x509.Certificate) -> _DssMaterial:
    der = certificate.public_bytes(serialization.Encoding.DER)
    if len(der) > MAX_DSS_CERTIFICATE_BYTES:
        raise ToolError("A DSS certificate exceeds the size limit.")
    return _DssMaterial(
        kind="certificate",
        der=der,
        sha256=_fingerprint(certificate),
        certificate=certificate,
    )


def _crl_material(crl: x509.CertificateRevocationList) -> _DssMaterial:
    der = crl.public_bytes(serialization.Encoding.DER)
    if len(der) > MAX_DSS_CRL_BYTES:
        raise ToolError("A DSS CRL exceeds the size limit.")
    return _DssMaterial(
        kind="crl", der=der, sha256=_sha256(der), crl=crl
    )


def _ocsp_material(response: ocsp.OCSPResponse) -> _DssMaterial:
    der = response.public_bytes(serialization.Encoding.DER)
    if len(der) > MAX_DSS_OCSP_BYTES:
        raise ToolError("A DSS OCSP response exceeds the size limit.")
    return _DssMaterial(
        kind="ocsp", der=der, sha256=_sha256(der), ocsp_response=response
    )


def _parse_material(kind: str, data: bytes, label: str) -> _DssMaterial:
    _count_limit, byte_limit = _MATERIAL_LIMITS[kind]
    if not data or len(data) > byte_limit:
        raise ToolError(f"{label} is empty or exceeds the DSS size limit.")
    try:
        if kind == "certificate":
            certificate = x509.load_der_x509_certificate(data)
            if certificate.public_bytes(serialization.Encoding.DER) != data:
                raise ValueError("not DER")
            return _certificate_material(certificate)
        if kind == "crl":
            crl = x509.load_der_x509_crl(data)
            if crl.public_bytes(serialization.Encoding.DER) != data:
                raise ValueError("not DER")
            return _crl_material(crl)
        response = ocsp.load_der_ocsp_response(data)
        if response.public_bytes(serialization.Encoding.DER) != data:
            raise ValueError("not DER")
        return _ocsp_material(response)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{label} is not a valid DER object.") from exc


def _safe_attribute(value: Any, name: str) -> Any:
    try:
        return getattr(value, name)
    except (AttributeError, TypeError, ValueError):
        return None


def _ocsp_summary(material: _DssMaterial) -> PdfOcspSummary:
    response = material.ocsp_response
    if response is None:  # Defensive; materials are constructed locally.
        raise ToolError("DSS OCSP material has no parsed response.")
    status = _safe_attribute(response, "response_status")
    response_status = (
        str(getattr(status, "name", status)).lower()
        if status is not None
        else "unknown"
    )
    successful = response_status == "successful"
    certificate_status = None
    serial_number = None
    this_update = None
    next_update = None
    produced_at = None
    if successful:
        cert_status = _safe_attribute(response, "certificate_status")
        certificate_status = (
            str(getattr(cert_status, "name", cert_status)).lower()
            if cert_status is not None
            else None
        )
        serial = _safe_attribute(response, "serial_number")
        serial_number = f"{serial:x}" if isinstance(serial, int) else None
        for target, attribute in (
            ("this", "this_update_utc"),
            ("next", "next_update_utc"),
            ("produced", "produced_at_utc"),
        ):
            moment = _safe_attribute(response, attribute)
            text = moment.isoformat() if isinstance(moment, datetime) else None
            if target == "this":
                this_update = text
            elif target == "next":
                next_update = text
            else:
                produced_at = text
    responder_name = _safe_attribute(response, "responder_name")
    responder_certificates = _safe_attribute(response, "certificates") or ()
    fingerprints = tuple(
        sorted(
            _fingerprint(certificate)
            for certificate in responder_certificates
            if isinstance(certificate, x509.Certificate)
        )
    )
    return PdfOcspSummary(
        sha256_fingerprint=material.sha256,
        response_status=response_status,
        certificate_status=certificate_status,
        serial_number=serial_number,
        responder_name=(
            responder_name.rfc4514_string()[:1000]
            if responder_name is not None
            else None
        ),
        responder_certificate_fingerprints=fingerprints,
        this_update=this_update,
        next_update=next_update,
        produced_at=produced_at,
    )


def _crl_summary(material: _DssMaterial) -> PdfCrlSummary:
    crl = material.crl
    if crl is None:
        raise ToolError("DSS CRL material has no parsed CRL.")
    last_update = getattr(crl, "last_update_utc", None)
    next_update = getattr(crl, "next_update_utc", None)
    return PdfCrlSummary(
        source_name="DSS",
        issuer=crl.issuer.rfc4514_string()[:1000],
        sha256_fingerprint=material.sha256,
        last_update=last_update.isoformat() if last_update else None,
        next_update=next_update.isoformat() if next_update else None,
    )


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _mapping_keys(value: Any) -> set[str]:
    if not _is_mapping(value):
        return set()
    return {str(key).lstrip("/") for key in value}


def _is_indirect(value: Any) -> bool:
    return (
        hasattr(value, "idnum")
        and hasattr(value, "generation")
        and hasattr(value, "get_object")
    )


def _stream_data(value: Any, label: str) -> bytes:
    stream = _object(value)
    get_data = getattr(stream, "get_data", None)
    if not callable(get_data):
        raise ToolError(f"{label} is not a PDF stream.")
    try:
        data = get_data()
    except Exception as exc:
        raise ToolError(f"{label} could not be decoded.") from exc
    if not isinstance(data, bytes):
        raise ToolError(f"{label} did not decode to bytes.")
    return data


def _parse_dss_array(
    dss: Any, key: str, kind: str
) -> dict[str, _DssMaterial]:
    raw = _get(dss, key)
    if raw is None:
        return {}
    array = _object(raw)
    if isinstance(array, (str, bytes, Mapping)) or not isinstance(
        array, (list, tuple)
    ):
        raise ToolError(f"Existing DSS {key} entry is not an array.")
    count_limit, _byte_limit = _MATERIAL_LIMITS[kind]
    if len(array) > count_limit:
        raise ToolError(f"Existing DSS contains too many {kind} objects.")
    materials: dict[str, _DssMaterial] = {}
    for index, item in enumerate(array, start=1):
        if not _is_indirect(item):
            raise ToolError(
                f"Existing DSS {key}[{index}] is not an indirect stream."
            )
        material = _parse_material(
            kind,
            _stream_data(item, f"Existing DSS {key}[{index}]"),
            f"Existing DSS {key}[{index}]",
        )
        materials[material.sha256] = material
    return materials


def _parse_vri_array(
    entry: Any,
    key: str,
    kind: str,
    global_materials: dict[str, _DssMaterial],
    identifier: str,
) -> set[str]:
    raw = _get(entry, key)
    if raw is None:
        return set()
    array = _object(raw)
    if isinstance(array, (str, bytes, Mapping)) or not isinstance(
        array, (list, tuple)
    ):
        raise ToolError(f"Existing VRI {identifier} {key} is not an array.")
    if not array:
        raise ToolError(f"Existing VRI {identifier} {key} array is empty.")
    if len(array) > _MATERIAL_LIMITS[kind][0]:
        raise ToolError(f"Existing VRI {identifier} has too many {kind} objects.")
    fingerprints: set[str] = set()
    for index, item in enumerate(array, start=1):
        if not _is_indirect(item):
            raise ToolError(
                f"Existing VRI {identifier} {key}[{index}] is not indirect."
            )
        material = _parse_material(
            kind,
            _stream_data(item, f"Existing VRI {identifier} {key}[{index}]"),
            f"Existing VRI {identifier} {key}[{index}]",
        )
        if material.sha256 not in global_materials:
            raise ToolError(
                f"Existing VRI {identifier} references material outside DSS."
            )
        fingerprints.add(material.sha256)
    return fingerprints


def _signature_identifiers(
    path: Path,
    inspection: PdfSignatureInspection,
    *,
    strict: bool,
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    PdfReader, _PdfWriter = _load_pypdf()
    try:
        reader = PdfReader(str(path))
        raw_fields = _raw_fields(reader)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError("Could not read PDF signature contents for DSS.") from exc

    signed_names = {
        item.field_name for item in inspection.signatures if item.signed
    }
    field_to_identifier: dict[str, str] = {}
    identifier_to_fields: dict[str, list[str]] = {}
    for raw in raw_fields:
        if _as_name(raw.values.get("/FT")) != "Sig":
            continue
        signature = _object(raw.values.get("/V"))
        if signature is None:
            continue
        contents = _contents_bytes(_get(signature, "/Contents"))
        if contents is None or len(contents) > MAX_PDF_SIGNATURE_CONTENTS_BYTES:
            if strict and raw.name in signed_names:
                raise ToolError(
                    f"Signature field {raw.name!r} has unusable Contents."
                )
            continue
        # ETSI hashes the complete hexadecimal representation of Contents,
        # including zero padding, rather than only the decoded DER object.
        identifier = hashlib.sha1(
            contents.hex().encode("ascii")
        ).hexdigest().upper()
        field_to_identifier[raw.name] = identifier
        identifier_to_fields.setdefault(identifier, []).append(raw.name)
    if strict:
        missing = signed_names - set(field_to_identifier)
        if missing:
            names = ", ".join(sorted(missing))
            raise ToolError(f"Could not identify signed field(s) for VRI: {names}.")
    return field_to_identifier, {
        key: tuple(sorted(values))
        for key, values in identifier_to_fields.items()
    }


def inspect_pdf_dss(path: Path) -> PdfDssInspection:
    """Inspect and strictly parse an existing PDF Document Security Store."""

    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise ToolError(f"PDF file not found or not a PDF: {path.name}")
    if path.stat().st_size > MAX_PDF_SIGNATURE_FILE_BYTES:
        raise ToolError("PDF DSS inspection is limited to 100 MB files.")
    info = inspect_pdf(path)
    if info.encrypted or info.pages is None:
        raise ToolError(
            "Encrypted PDFs must be unprotected through the direct Phase 26 "
            "workflow before DSS inspection."
        )
    if info.pages > MAX_PDF_FORM_PAGES:
        raise ToolError(
            f"PDF DSS inspection is limited to {MAX_PDF_FORM_PAGES} pages."
        )
    signature_inspection = inspect_pdf_signatures(path)
    _field_ids, identifier_fields = _signature_identifiers(
        path, signature_inspection, strict=False
    )

    PdfReader, _PdfWriter = _load_pypdf()
    try:
        reader = PdfReader(str(path))
        root = _object(getattr(reader, "root_object", None))
        if root is None:
            root = _object(_get(getattr(reader, "trailer", {}), "/Root"))
    except Exception as exc:
        raise ToolError("Could not read the PDF catalog for DSS inspection.") from exc
    if not _is_mapping(root):
        raise ToolError("PDF catalog is missing or malformed.")

    extensions = _object(_get(root, "/Extensions"))
    extensions_esic = False
    if extensions is not None:
        if not _is_mapping(extensions):
            raise ToolError("PDF Extensions dictionary is malformed.")
        extensions_esic = _get(extensions, "/ESIC") is not None

    raw_dss = _get(root, "/DSS")
    if raw_dss is None:
        return PdfDssInspection(
            path=path,
            pages=info.pages,
            size_bytes=signature_inspection.size_bytes,
            sha256=signature_inspection.sha256,
            present=False,
            certificates=(),
            crls=(),
            ocsps=(),
            vri=(),
            extensions_esic=extensions_esic,
        )
    dss = _object(raw_dss)
    if not _is_mapping(dss):
        raise ToolError("Existing DSS entry is not a dictionary.")
    unknown = _mapping_keys(dss) - {"Type", "Certs", "CRLs", "OCSPs", "VRI"}
    if unknown:
        raise ToolError(
            "Existing DSS contains unsupported entries: "
            + ", ".join(sorted(unknown))
            + "."
        )
    dss_type = _as_name(_get(dss, "/Type"))
    if dss_type and dss_type != "DSS":
        raise ToolError("Existing DSS Type is not DSS.")

    material_maps = {
        "certificate": _parse_dss_array(dss, "/Certs", "certificate"),
        "crl": _parse_dss_array(dss, "/CRLs", "crl"),
        "ocsp": _parse_dss_array(dss, "/OCSPs", "ocsp"),
    }
    total_material = sum(
        len(material.der)
        for materials in material_maps.values()
        for material in materials.values()
    )
    if total_material > MAX_DSS_TOTAL_MATERIAL_BYTES:
        raise ToolError("Existing DSS validation material is too large.")

    vri_summaries: list[PdfDssVriSummary] = []
    raw_vri = _get(dss, "/VRI")
    if raw_vri is not None:
        vri = _object(raw_vri)
        if not _is_mapping(vri):
            raise ToolError("Existing DSS VRI entry is not a dictionary.")
        if len(vri) > MAX_DSS_VRI_ENTRIES:
            raise ToolError("Existing DSS contains too many VRI entries.")
        for raw_identifier, raw_entry in vri.items():
            identifier = str(raw_identifier).lstrip("/").upper()
            if not _VRI_IDENTIFIER.fullmatch(identifier):
                raise ToolError("Existing DSS contains an invalid VRI identifier.")
            entry = _object(raw_entry)
            if not _is_mapping(entry):
                raise ToolError(f"Existing VRI {identifier} is not a dictionary.")
            unknown_entry = _mapping_keys(entry) - {
                "Type",
                "Cert",
                "CRL",
                "OCSP",
                "TU",
                "TS",
            }
            if unknown_entry:
                raise ToolError(
                    f"Existing VRI {identifier} contains unsupported entries."
                )
            entry_type = _as_name(_get(entry, "/Type"))
            if entry_type and entry_type != "VRI":
                raise ToolError(f"Existing VRI {identifier} Type is not VRI.")
            certificates = _parse_vri_array(
                entry, "/Cert", "certificate", material_maps["certificate"], identifier
            )
            crls = _parse_vri_array(
                entry, "/CRL", "crl", material_maps["crl"], identifier
            )
            ocsps = _parse_vri_array(
                entry, "/OCSP", "ocsp", material_maps["ocsp"], identifier
            )
            tu = _as_text(_get(entry, "/TU")).strip() or None
            ts_raw = _get(entry, "/TS")
            ts_der = b""
            if ts_raw is not None:
                ts_der = _stream_data(ts_raw, f"Existing VRI {identifier} TS")
                if not ts_der or len(ts_der) > MAX_TIMESTAMP_TOKEN_BYTES:
                    raise ToolError(f"Existing VRI {identifier} TS is too large.")
                try:
                    token_root = _parse_der_root(ts_der)
                except Exception as exc:
                    raise ToolError(
                        f"Existing VRI {identifier} TS is not valid DER."
                    ) from exc
                if token_root.end != len(ts_der):
                    raise ToolError(f"Existing VRI {identifier} TS has trailing data.")
            if tu and ts_der:
                raise ToolError(f"Existing VRI {identifier} has both TU and TS.")
            vri_summaries.append(
                PdfDssVriSummary(
                    identifier=identifier,
                    field_names=identifier_fields.get(identifier, ()),
                    certificate_fingerprints=tuple(sorted(certificates)),
                    crl_fingerprints=tuple(sorted(crls)),
                    ocsp_fingerprints=tuple(sorted(ocsps)),
                    tu=tu,
                    ts_present=bool(ts_der),
                    ts_sha256=_sha256(ts_der) if ts_der else None,
                    ts_der=ts_der,
                )
            )
    vri_summaries.sort(key=lambda item: item.identifier)

    certificates = tuple(
        _certificate_summary(material.certificate)  # type: ignore[arg-type]
        for _fp, material in sorted(material_maps["certificate"].items())
    )
    crls = tuple(
        _crl_summary(material)
        for _fp, material in sorted(material_maps["crl"].items())
    )
    ocsps = tuple(
        _ocsp_summary(material)
        for _fp, material in sorted(material_maps["ocsp"].items())
    )
    materials = tuple(
        material
        for kind in ("certificate", "crl", "ocsp")
        for _fp, material in sorted(material_maps[kind].items())
    )
    return PdfDssInspection(
        path=path,
        pages=info.pages,
        size_bytes=signature_inspection.size_bytes,
        sha256=signature_inspection.sha256,
        present=True,
        certificates=certificates,
        crls=crls,
        ocsps=ocsps,
        vri=tuple(vri_summaries),
        extensions_esic=extensions_esic,
        materials=materials,
    )


def _load_supplied_certificates(
    paths: Sequence[Path],
) -> dict[str, _DssMaterial]:
    files = _iter_input_files(
        paths,
        suffixes=_CERTIFICATE_SUFFIXES,
        label="LTV certificate",
        maximum=MAX_TRUST_ANCHOR_FILES,
    )
    materials: dict[str, _DssMaterial] = {}
    for path in files:
        data = _read_bounded(
            path, maximum=MAX_TRUST_ANCHOR_FILE_BYTES, label="LTV certificate"
        )
        blocks = _PEM_CERTIFICATE.findall(data)
        try:
            loaded = (
                [x509.load_pem_x509_certificate(block) for block in blocks]
                if blocks
                else [x509.load_der_x509_certificate(data)]
            )
        except ValueError as exc:
            raise ToolError(f"Could not parse LTV certificate: {path.name}") from exc
        for certificate in loaded:
            material = _certificate_material(certificate)
            materials[material.sha256] = material
            if len(materials) > MAX_DSS_CERTIFICATES:
                raise ToolError("Too many supplied LTV certificates.")
    return materials


def _load_supplied_crls(paths: Sequence[Path]) -> dict[str, _DssMaterial]:
    crls, _summaries = _load_crls(paths)
    materials: dict[str, _DssMaterial] = {}
    for crl in crls:
        material = _crl_material(crl)
        materials[material.sha256] = material
    return materials


def _load_supplied_ocsps(paths: Sequence[Path]) -> dict[str, _DssMaterial]:
    files = _iter_input_files(
        paths,
        suffixes=_OCSP_SUFFIXES,
        label="OCSP response",
        maximum=MAX_DSS_OCSP_RESPONSES,
    )
    materials: dict[str, _DssMaterial] = {}
    for path in files:
        data = _read_bounded(
            path, maximum=MAX_DSS_OCSP_BYTES, label="OCSP response"
        )
        try:
            response = (
                ocsp.load_pem_ocsp_response(data)
                if b"-----BEGIN OCSP RESPONSE-----" in data
                else ocsp.load_der_ocsp_response(data)
            )
        except (TypeError, ValueError) as exc:
            raise ToolError(f"Could not parse OCSP response: {path.name}") from exc
        material = _ocsp_material(response)
        materials[material.sha256] = material
    return materials


def _summary_certificate(
    summary: PdfCertificateSummary | None,
) -> x509.Certificate | None:
    if summary is None or not summary.der_bytes:
        return None
    try:
        certificate = x509.load_der_x509_certificate(summary.der_bytes)
    except ValueError:
        return None
    if _fingerprint(certificate) != summary.sha256_fingerprint:
        return None
    return certificate


def _directly_issues(
    issuer: x509.Certificate, certificate: x509.Certificate
) -> bool:
    try:
        certificate.verify_directly_issued_by(issuer)
    except (InvalidSignature, TypeError, UnsupportedAlgorithm, ValueError):
        return False
    return True


def _upward_closure(
    seed_fingerprints: set[str],
    pool: dict[str, x509.Certificate],
) -> set[str]:
    relevant = {fp for fp in seed_fingerprints if fp in pool}
    changed = True
    while changed:
        changed = False
        current = [pool[fp] for fp in relevant]
        for fingerprint, candidate in pool.items():
            if fingerprint in relevant:
                continue
            if any(_directly_issues(candidate, item) for item in current):
                relevant.add(fingerprint)
                changed = True
    return relevant


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _field_times(signature: Any) -> tuple[datetime, ...]:
    values: set[datetime] = set()
    for signer in signature.signers:
        for candidate in (
            signer.signing_time,
            signer.timestamp.generation_time if signer.timestamp else None,
        ):
            parsed = _parse_time(candidate)
            if parsed is not None:
                values.add(parsed)
    if signature.document_timestamp is not None:
        parsed = _parse_time(signature.document_timestamp.generation_time)
        if parsed is not None:
            values.add(parsed)
    parsed = _parse_time(signature.signature_date)
    if parsed is not None:
        values.add(parsed)
    return tuple(sorted(values))


def _field_certificate_data(
    signature: Any,
) -> tuple[dict[str, x509.Certificate], set[str]]:
    certificates: dict[str, x509.Certificate] = {}
    seeds: set[str] = set()

    def add(summary: PdfCertificateSummary | None, *, seed: bool = False) -> None:
        certificate = _summary_certificate(summary)
        if certificate is None:
            return
        fingerprint = _fingerprint(certificate)
        certificates[fingerprint] = certificate
        if seed:
            seeds.add(fingerprint)

    for summary in signature.certificates:
        add(summary)
    for signer in signature.signers:
        add(signer.certificate, seed=True)
        if signer.timestamp is not None:
            for summary in signer.timestamp.certificates:
                add(summary)
            add(signer.timestamp.signer_certificate, seed=True)
    if signature.document_timestamp is not None:
        for summary in signature.document_timestamp.certificates:
            add(summary)
        add(signature.document_timestamp.signer_certificate, seed=True)
    if not seeds:
        seeds = set(certificates)
    return certificates, seeds


def _canonical_from_inspection(
    inspection: PdfDssInspection,
) -> tuple[Any, ...]:
    by_kind: dict[str, set[str]] = {
        "certificate": set(),
        "crl": set(),
        "ocsp": set(),
    }
    for material in inspection.materials:
        by_kind[material.kind].add(material.sha256)
    vri = {
        item.identifier: (
            frozenset(item.certificate_fingerprints),
            frozenset(item.crl_fingerprints),
            frozenset(item.ocsp_fingerprints),
            bool(item.tu),
            item.ts_sha256,
        )
        for item in inspection.vri
    }
    return (
        inspection.present,
        frozenset(by_kind["certificate"]),
        frozenset(by_kind["crl"]),
        frozenset(by_kind["ocsp"]),
        vri,
        inspection.extensions_esic,
    )


def _canonical_from_plan(plan: _DssPlan) -> tuple[Any, ...]:
    vri = {
        identifier: (
            frozenset(entry.certificates),
            frozenset(entry.crls),
            frozenset(entry.ocsps),
            bool(entry.tu),
            _sha256(entry.ts_der) if entry.ts_der else None,
        )
        for identifier, entry in plan.vri.items()
    }
    return (
        True,
        frozenset(plan.materials["certificate"]),
        frozenset(plan.materials["crl"]),
        frozenset(plan.materials["ocsp"]),
        vri,
        True,
    )


def _pdf_date(moment: datetime) -> str:
    return "D:" + moment.astimezone(UTC).strftime("%Y%m%d%H%M%S") + "Z"


def _collect_plan(
    *,
    before: PdfSignatureInspection,
    existing: PdfDssInspection,
    field_identifiers: dict[str, str],
    supplied_certificates: dict[str, _DssMaterial],
    supplied_crls: dict[str, _DssMaterial],
    supplied_ocsps: dict[str, _DssMaterial],
    operation_time: datetime,
    seal_with_timestamp: bool,
) -> _DssPlan:
    existing_materials: dict[str, dict[str, _DssMaterial]] = {
        "certificate": {},
        "crl": {},
        "ocsp": {},
    }
    for material in existing.materials:
        existing_materials[material.kind][material.sha256] = material

    existing_certificates = {
        fingerprint: material.certificate
        for fingerprint, material in existing_materials["certificate"].items()
        if material.certificate is not None
    }
    supplied_certificate_objects = {
        fingerprint: material.certificate
        for fingerprint, material in supplied_certificates.items()
        if material.certificate is not None
    }

    signed_fields = [item for item in before.signatures if item.signed]
    field_certificates: dict[str, dict[str, x509.Certificate]] = {}
    field_certificate_fingerprints: dict[str, set[str]] = {}
    supplied_relevant: set[str] = set()
    final_certificate_materials = dict(existing_materials["certificate"])

    for signature in signed_fields:
        embedded, seeds = _field_certificate_data(signature)
        if not embedded:
            raise ToolError(
                f"Signed field {signature.field_name!r} has no embedded "
                "certificate available for LTV material collection."
            )
        pool = {
            **existing_certificates,
            **embedded,
            **supplied_certificate_objects,
        }
        closure = _upward_closure(seeds, pool)
        field_certificates[signature.field_name] = {
            fp: pool[fp] for fp in closure
        }
        field_certificate_fingerprints[signature.field_name] = set(closure)
        supplied_relevant.update(closure & set(supplied_certificates))
        for fingerprint in closure:
            if fingerprint in final_certificate_materials:
                continue
            if fingerprint in supplied_certificates:
                final_certificate_materials[fingerprint] = supplied_certificates[
                    fingerprint
                ]
            else:
                final_certificate_materials[fingerprint] = _certificate_material(
                    pool[fingerprint]
                )

    field_crls: dict[str, set[str]] = {
        item.field_name: set() for item in signed_fields
    }
    associated_crls: set[str] = set()
    revoked_evidence: list[str] = []
    for signature in signed_fields:
        certificates = field_certificates[signature.field_name]
        issuer_subjects = {cert.subject.public_bytes() for cert in certificates.values()}
        times = _field_times(signature) or (operation_time,)
        for fingerprint, material in supplied_crls.items():
            crl = material.crl
            if crl is None or crl.issuer.public_bytes() not in issuer_subjects:
                continue
            for target in certificates.values():
                for moment in times:
                    status, _detail = _crl_status_for_certificate(
                        target,
                        tuple(certificates.values()),
                        (crl,),
                        verification_time=moment,
                        now=operation_time,
                    )
                    if status == "revoked":
                        revoked_evidence.append(
                            f"{signature.field_name}:{target.subject.rfc4514_string()[:200]}"
                        )
                    elif status in {"not_revoked", "revoked_after_signing"}:
                        field_crls[signature.field_name].add(fingerprint)
                        associated_crls.add(fingerprint)
    if revoked_evidence:
        raise ToolError(
            "A supplied CRL proves a certificate was revoked at or before "
            "the relevant signing time; LTV output was not published."
        )
    unassociated_crls = set(supplied_crls) - associated_crls
    if unassociated_crls:
        raise ToolError(
            "Every supplied CRL must be fresh, correctly signed by a "
            "certificate in the collected chain, and relevant to a signed "
            "field; one or more CRLs did not qualify."
        )

    field_ocsps: dict[str, set[str]] = {
        item.field_name: set() for item in signed_fields
    }
    associated_ocsps: set[str] = set()
    for signature in signed_fields:
        certificates = field_certificates[signature.field_name]
        times = _field_times(signature) or (operation_time,)
        for fingerprint, material in supplied_ocsps.items():
            response = material.ocsp_response
            if response is None:
                continue
            for target in certificates.values():
                for issuer in certificates.values():
                    if not _directly_issues(issuer, target):
                        continue
                    for moment in times:
                        status, _detail = _validate_ocsp_response(
                            response,
                            certificate=target,
                            issuer=issuer,
                            verification_time=moment,
                            now=operation_time,
                        )
                        if status == "revoked":
                            revoked_evidence.append(
                                f"{signature.field_name}:{target.subject.rfc4514_string()[:200]}"
                            )
                        elif status in {"not_revoked", "revoked_after_signing"}:
                            field_ocsps[signature.field_name].add(fingerprint)
                            associated_ocsps.add(fingerprint)
    if revoked_evidence:
        raise ToolError(
            "A supplied OCSP response proves a certificate was revoked at "
            "or before the relevant signing time; LTV output was not published."
        )
    if set(supplied_ocsps) - associated_ocsps:
        raise ToolError(
            "Every supplied OCSP response must be fresh, signed by an "
            "authorized responder, and relevant to a collected certificate; "
            "one or more responses did not qualify."
        )

    for fingerprint in associated_ocsps:
        response = supplied_ocsps[fingerprint].ocsp_response
        if response is None:
            continue
        for certificate in response.certificates:
            responder_material = _certificate_material(certificate)
            responder_fp = responder_material.sha256
            final_certificate_materials.setdefault(
                responder_fp, responder_material
            )
            supplied_relevant.add(responder_fp)
            for signature in signed_fields:
                if fingerprint in field_ocsps[signature.field_name]:
                    field_certificate_fingerprints[signature.field_name].add(
                        responder_fp
                    )
                    field_certificates[signature.field_name][responder_fp] = (
                        certificate
                    )

    irrelevant_supplied = set(supplied_certificates) - supplied_relevant
    if irrelevant_supplied:
        raise ToolError(
            "Every supplied LTV certificate must be in, or cryptographically "
            "issue, a collected signer/TSA certificate chain; one or more "
            "certificates were unrelated."
        )

    final_materials: dict[str, dict[str, _DssMaterial]] = {
        "certificate": final_certificate_materials,
        "crl": {**existing_materials["crl"], **supplied_crls},
        "ocsp": {**existing_materials["ocsp"], **supplied_ocsps},
    }
    for kind, materials in final_materials.items():
        if len(materials) > _MATERIAL_LIMITS[kind][0]:
            raise ToolError(f"The final DSS would contain too many {kind} objects.")
    total_bytes = sum(
        len(material.der)
        for materials in final_materials.values()
        for material in materials.values()
    )
    if total_bytes > MAX_DSS_TOTAL_MATERIAL_BYTES:
        raise ToolError("The final DSS validation material is too large.")

    vri: dict[str, _VriPlan] = {}
    existing_vri = {item.identifier: item for item in existing.vri}
    for identifier, item in existing_vri.items():
        vri[identifier] = _VriPlan(
            identifier=identifier,
            certificates=set(item.certificate_fingerprints),
            crls=set(item.crl_fingerprints),
            ocsps=set(item.ocsp_fingerprints),
            tu=item.tu,
            ts_der=item.ts_der,
        )
    for signature in signed_fields:
        identifier = field_identifiers[signature.field_name]
        entry = vri.setdefault(
            identifier,
            _VriPlan(
                identifier=identifier,
                certificates=set(),
                crls=set(),
                ocsps=set(),
            ),
        )
        entry.certificates.update(
            field_certificate_fingerprints[signature.field_name]
        )
        entry.crls.update(field_crls[signature.field_name])
        entry.ocsps.update(field_ocsps[signature.field_name])

    for identifier, entry in vri.items():
        previous = existing_vri.get(identifier)
        changed = (
            previous is None
            or set(previous.certificate_fingerprints) != entry.certificates
            or set(previous.crl_fingerprints) != entry.crls
            or set(previous.ocsp_fingerprints) != entry.ocsps
        )
        if changed:
            entry.tu = None if seal_with_timestamp else _pdf_date(operation_time)
            entry.ts_der = b""
    if len(vri) > MAX_DSS_VRI_ENTRIES:
        raise ToolError("The final DSS would contain too many VRI entries.")

    return _DssPlan(
        materials=final_materials,
        vri=vri,
        field_identifiers=dict(field_identifiers),
    )


def _build_dss_pdf(source: Path, plan: _DssPlan) -> bytes:
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
        NumberObject,
        TextStringObject,
    )

    _PdfReader, PdfWriter = _load_pypdf()
    writer = PdfWriter(str(source), incremental=True)
    try:
        reserved_objects = int(writer._reader.trailer.get("/Size")) - 1
    except (AttributeError, TypeError, ValueError):
        reserved_objects = len(writer._objects)
    while len(writer._objects) < reserved_objects:
        writer._objects.append(None)

    try:
        references: dict[str, dict[str, Any]] = {
            "certificate": {},
            "crl": {},
            "ocsp": {},
        }
        for kind in ("certificate", "crl", "ocsp"):
            for fingerprint in sorted(plan.materials[kind]):
                stream = DecodedStreamObject()
                stream.set_data(plan.materials[kind][fingerprint].der)
                references[kind][fingerprint] = writer._add_object(stream)

        vri = DictionaryObject()
        for identifier in sorted(plan.vri):
            entry = plan.vri[identifier]
            value = DictionaryObject()
            value[NameObject("/Type")] = NameObject("/VRI")
            if entry.certificates:
                value[NameObject("/Cert")] = ArrayObject(
                    [references["certificate"][fp] for fp in sorted(entry.certificates)]
                )
            if entry.crls:
                value[NameObject("/CRL")] = ArrayObject(
                    [references["crl"][fp] for fp in sorted(entry.crls)]
                )
            if entry.ocsps:
                value[NameObject("/OCSP")] = ArrayObject(
                    [references["ocsp"][fp] for fp in sorted(entry.ocsps)]
                )
            if entry.tu:
                value[NameObject("/TU")] = TextStringObject(entry.tu)
            if entry.ts_der:
                stream = DecodedStreamObject()
                stream.set_data(entry.ts_der)
                value[NameObject("/TS")] = writer._add_object(stream)
            vri[NameObject(f"/{identifier}")] = value

        dss = DictionaryObject()
        dss[NameObject("/Type")] = NameObject("/DSS")
        if references["certificate"]:
            dss[NameObject("/Certs")] = ArrayObject(
                [references["certificate"][fp] for fp in sorted(references["certificate"])]
            )
        if references["crl"]:
            dss[NameObject("/CRLs")] = ArrayObject(
                [references["crl"][fp] for fp in sorted(references["crl"])]
            )
        if references["ocsp"]:
            dss[NameObject("/OCSPs")] = ArrayObject(
                [references["ocsp"][fp] for fp in sorted(references["ocsp"])]
            )
        if vri:
            dss[NameObject("/VRI")] = vri

        root = writer._root_object
        root[NameObject("/DSS")] = dss
        extensions = _object(_get(root, "/Extensions"))
        if extensions is None:
            extensions = DictionaryObject()
            root[NameObject("/Extensions")] = extensions
        if _is_mapping(extensions) and _get(extensions, "/ESIC") is None:
            esic = DictionaryObject()
            esic[NameObject("/BaseVersion")] = NameObject("/1.7")
            esic[NameObject("/ExtensionLevel")] = NumberObject(1)
            extensions[NameObject("/ESIC")] = esic

        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()
    finally:
        writer.close()


def _verify_candidate(
    candidate: Path,
    *,
    before: PdfSignatureInspection,
    before_form: Any,
    plan: _DssPlan,
    expected_signed_count: int,
    expected_form_fields: set[str] | None,
) -> tuple[PdfSignatureInspection, PdfDssInspection, int]:
    after = inspect_pdf_signatures(candidate)
    if after.pages != before.pages:
        raise ToolError("LTV PDF failed verification: page count changed.")
    if after.signed_count != expected_signed_count:
        raise ToolError("LTV PDF failed verification: signed-field count changed.")
    preserved = _verify_preserved_fields(before, after)
    if expected_form_fields is not None:
        after_form = inspect_pdf_form(candidate)
        if set(after_form.field_names) != expected_form_fields:
            raise ToolError("LTV PDF failed verification: form fields changed.")
    del before_form
    dss = inspect_pdf_dss(candidate)
    if _canonical_from_inspection(dss) != _canonical_from_plan(plan):
        raise ToolError("LTV PDF failed verification: DSS material does not match.")
    return after, dss, preserved


def add_pdf_ltv_material(
    source: Path,
    output: Path,
    *,
    certificate_paths: Sequence[Path] = (),
    crl_paths: Sequence[Path] = (),
    ocsp_response_paths: Sequence[Path] = (),
    timestamp_url: str | None = None,
    timestamp_field_name: str | None = None,
    timestamp_create_field: bool = False,
    timestamp_page: int = 1,
    timestamp_hash_algorithm: str = "sha256",
    timestamp_timeout_seconds: int = 15,
    timestamp_allow_private_network: bool = False,
    timestamp_opener: Any | None = None,
) -> PdfLtvResult:
    """Append verified LTV DSS material to a valid signed PDF."""

    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise ToolError(f"Source PDF file not found or not a PDF: {source.name}")
    if source.stat().st_size > MAX_PDF_SIGNING_FILE_BYTES:
        raise ToolError("PDF LTV embedding is limited to 100 MB files.")
    if output.suffix.lower() != ".pdf":
        raise ToolError("The LTV PDF output must be a .pdf file.")
    if output.exists():
        raise ToolError(
            "Output file already exists. PDF tools never overwrite; "
            "choose a new output path."
        )
    if source.expanduser().resolve() == output.expanduser().resolve():
        raise ToolError("The LTV PDF output must not be the source PDF.")
    clean_timestamp_url = timestamp_url.strip() if timestamp_url else None
    if timestamp_url is not None and not clean_timestamp_url:
        raise ToolError("A timestamp authority HTTPS URL is required when sealing.")

    before = inspect_pdf_signatures(source)
    if before.pages > MAX_PDF_SIGNING_PAGES:
        raise ToolError(
            f"PDF LTV embedding is limited to {MAX_PDF_SIGNING_PAGES} pages."
        )
    _permission, approval_count, timestamp_count = _classify_existing_fields(before)
    if before.signed_count == 0:
        raise ToolError("LTV DSS embedding requires at least one signed field.")
    before_form = inspect_pdf_form(source)
    existing_dss = inspect_pdf_dss(source)
    field_identifiers, _identifier_fields = _signature_identifiers(
        source, before, strict=True
    )
    source_bytes = source.read_bytes()
    source_hash = sha256_file(source)
    if hashlib.sha256(source_bytes).hexdigest() != source_hash or (
        source_hash != before.sha256
    ):
        raise ToolError("PDF changed during LTV preparation; retry.")

    supplied_certificates = _load_supplied_certificates(certificate_paths)
    supplied_crls = _load_supplied_crls(crl_paths)
    supplied_ocsps = _load_supplied_ocsps(ocsp_response_paths)
    operation_time = datetime.now(UTC).replace(microsecond=0)
    plan = _collect_plan(
        before=before,
        existing=existing_dss,
        field_identifiers=field_identifiers,
        supplied_certificates=supplied_certificates,
        supplied_crls=supplied_crls,
        supplied_ocsps=supplied_ocsps,
        operation_time=operation_time,
        seal_with_timestamp=clean_timestamp_url is not None,
    )
    dss_changed = _canonical_from_inspection(existing_dss) != _canonical_from_plan(plan)
    if not dss_changed and clean_timestamp_url is None:
        raise ToolError(
            "The PDF already contains the same DSS validation material and "
            "VRI associations; no output was published."
        )
    projected_size = len(source_bytes) + sum(
        len(material.der)
        for materials in plan.materials.values()
        for material in materials.values()
    )
    if dss_changed and projected_size > MAX_PDF_SIGNING_FILE_BYTES:
        raise ToolError("The LTV output would exceed the 100 MB PDF limit.")

    output.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(4)
    dss_temp = output.with_name(f".{output.stem}.dss-tmp-{token}.pdf")
    final_temp = output.with_name(f".{output.stem}.ltv-tmp-{token}.pdf")
    timestamp_result = None
    try:
        staging = source
        if dss_changed:
            built = _build_dss_pdf(source, plan)
            if not built.startswith(source_bytes):
                raise ToolError(
                    "LTV DSS building failed to preserve the exact source "
                    "revision prefix."
                )
            dss_temp.write_bytes(built)
            staging = dss_temp
            _verify_candidate(
                staging,
                before=before,
                before_form=before_form,
                plan=plan,
                expected_signed_count=before.signed_count,
                expected_form_fields=set(before_form.field_names),
            )

        if clean_timestamp_url is None:
            if sha256_file(source) != source_hash:
                raise ToolError("LTV embedding changed the source; output rejected.")
            if output.exists():
                raise ToolError(
                    "Output file appeared during LTV embedding; it was not "
                    "overwritten."
                )
            os.replace(staging, output)
        else:
            timestamp_result = add_pdf_document_timestamp(
                staging,
                final_temp,
                timestamp_url=clean_timestamp_url,
                field_name=timestamp_field_name,
                create_field=timestamp_create_field,
                page=timestamp_page,
                hash_algorithm=timestamp_hash_algorithm,
                timeout_seconds=timestamp_timeout_seconds,
                allow_private_network=timestamp_allow_private_network,
                timestamp_opener=timestamp_opener,
            )
            expected_fields = set(before_form.field_names)
            if timestamp_result.created_field:
                expected_fields.add(timestamp_result.field_name)
            if not final_temp.read_bytes().startswith(source_bytes):
                raise ToolError(
                    "Timestamped LTV output does not preserve the source prefix."
                )
            _verify_candidate(
                final_temp,
                before=before,
                before_form=before_form,
                plan=plan,
                expected_signed_count=before.signed_count + 1,
                expected_form_fields=expected_fields,
            )
            if sha256_file(source) != source_hash:
                raise ToolError("LTV embedding changed the source; output rejected.")
            if output.exists():
                raise ToolError(
                    "Output file appeared during LTV embedding; it was not "
                    "overwritten."
                )
            os.replace(final_temp, output)
    except BaseException:
        for temporary in (dss_temp, final_temp):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        for temporary in (dss_temp, final_temp):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    final = inspect_pdf_signatures(output)
    preserved = _verify_preserved_fields(before, final)
    final_dss = inspect_pdf_dss(output)
    if _canonical_from_inspection(final_dss) != _canonical_from_plan(plan):
        raise ToolError("Published LTV PDF failed final DSS verification.")

    existing_counts = {
        kind: {
            material.sha256
            for material in existing_dss.materials
            if material.kind == kind
        }
        for kind in ("certificate", "crl", "ocsp")
    }
    vri_fields = tuple(
        sorted(
            {
                field_name
                for item in final_dss.vri
                for field_name in item.field_names
                if field_name in field_identifiers
            }
        )
    )
    return PdfLtvResult(
        source_path=source,
        output_path=output,
        status=(
            "dss_embedded_timestamped"
            if clean_timestamp_url is not None
            else "dss_embedded"
        ),
        pages=final.pages,
        size_bytes=final.size_bytes,
        sha256=final.sha256,
        signed_field_count=before.signed_count,
        approval_signature_count=approval_count,
        existing_document_timestamp_count=timestamp_count,
        preserved_valid_count=preserved,
        total_signed_field_count=final.signed_count,
        source_revision_bytes=len(source_bytes),
        existing_dss_present=existing_dss.present,
        dss_updated=dss_changed,
        existing_certificate_count=len(existing_counts["certificate"]),
        added_certificate_count=len(
            set(plan.materials["certificate"]) - existing_counts["certificate"]
        ),
        certificate_count=final_dss.certificate_count,
        existing_crl_count=len(existing_counts["crl"]),
        added_crl_count=len(set(plan.materials["crl"]) - existing_counts["crl"]),
        crl_count=final_dss.crl_count,
        existing_ocsp_count=len(existing_counts["ocsp"]),
        added_ocsp_count=len(
            set(plan.materials["ocsp"]) - existing_counts["ocsp"]
        ),
        ocsp_count=final_dss.ocsp_count,
        vri_count=final_dss.vri_count,
        vri_field_names=vri_fields,
        supplied_certificate_count=len(supplied_certificates),
        supplied_crl_count=len(supplied_crls),
        supplied_ocsp_count=len(supplied_ocsps),
        timestamp_requested=clean_timestamp_url is not None,
        timestamp_field_name=(
            timestamp_result.field_name if timestamp_result else None
        ),
        timestamp_status=(
            timestamp_result.timestamp_status if timestamp_result else None
        ),
        timestamp_time=(
            timestamp_result.timestamp_time if timestamp_result else None
        ),
        tsa_subject=timestamp_result.tsa_subject if timestamp_result else None,
    )
