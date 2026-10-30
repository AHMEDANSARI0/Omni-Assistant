"""Direct-only PDF digital-signature creation from a PKCS#12 identity.

Phase 28 signs a new copy of an unencrypted PDF with a detached CMS
signature. Phase 34 extends that workflow to already signed PDFs by writing
one true incremental update: the complete source revision remains an exact
byte prefix, and publication requires every previously valid signature to
verify again over its original ByteRange. The PKCS#12 password and private
key are supplied only through a direct CLI/GUI operation in memory; there is
no registered tool or planner surface. The source is never modified, the
output is never overwritten, and the result is published only after the
Phase 27 inspector verifies the new ByteRange digest and CMS signature
against the intended certificate.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import (
    dsa,
    ec,
    ed448,
    ed25519,
    padding,
    rsa,
)
from cryptography.hazmat.primitives.serialization import pkcs7, pkcs12
from cryptography.x509.oid import NameOID

from ..errors import ToolError
from .pdf import _load_pypdf, sha256_file
from .pdf_forms import _as_int, _as_name, _as_text, _get, _has, _items, _object
from .pdf_signatures import inspect_pdf_signatures
from .pdf_timestamps import (
    TimestampAttachment,
    _context,
    _integer_der,
    _octet_string,
    _oid,
    _sequence,
    _set,
    _tlv,
    request_timestamp_for_cms,
)

MAX_PDF_SIGNING_FILE_BYTES = 100_000_000
MAX_PDF_SIGNING_PAGES = 500
MAX_PKCS12_BYTES = 5_000_000
MAX_PKCS12_PASSWORD_CHARS = 4_096
MAX_SIGNATURE_TEXT_CHARS = 500
MAX_SIGNATURE_FIELD_NAME_CHARS = 100
MAX_SIGNATURE_CONTENTS_BYTES = 1_000_000
MIN_SIGNATURE_CONTENTS_BYTES = 16_384

SUPPORTED_SIGNATURE_HASHES = ("sha256", "sha384", "sha512")
SIGNATURE_PROFILE_ADBE = "adbe"
SIGNATURE_PROFILE_PADES_BB = "pades-bb"
SUPPORTED_SIGNATURE_PROFILES = (
    SIGNATURE_PROFILE_ADBE,
    SIGNATURE_PROFILE_PADES_BB,
)

_OID_DATA = "1.2.840.113549.1.7.1"
_OID_SIGNED_DATA = "1.2.840.113549.1.7.2"
_OID_CONTENT_TYPE = "1.2.840.113549.1.9.3"
_OID_MESSAGE_DIGEST = "1.2.840.113549.1.9.4"
_OID_SIGNING_CERTIFICATE_V2 = "1.2.840.113549.1.9.16.2.47"
_DIGEST_OIDS = {
    "sha256": "2.16.840.1.101.3.4.2.1",
    "sha384": "2.16.840.1.101.3.4.2.2",
    "sha512": "2.16.840.1.101.3.4.2.3",
}
_RSA_SIGNATURE_OIDS = {
    "sha256": "1.2.840.113549.1.1.11",
    "sha384": "1.2.840.113549.1.1.12",
    "sha512": "1.2.840.113549.1.1.13",
}
_ECDSA_SIGNATURE_OIDS = {
    "sha256": "1.2.840.10045.4.3.2",
    "sha384": "1.2.840.10045.4.3.3",
    "sha512": "1.2.840.10045.4.3.4",
}


@dataclass(frozen=True, slots=True)
class PdfSigningResult:
    source_path: Path
    output_path: Path
    field_name: str
    created_field: bool
    page: int | None
    signer_subject: str
    certificate_sha256: str
    hash_algorithm: str
    certify_permission: int | None
    cms_size: int
    reserved_contents_size: int
    pages: int
    size_bytes: int
    sha256: str
    status: str
    verified: bool = True
    timestamp_status: str | None = None
    timestamp_time: str | None = None
    timestamp_policy_oid: str | None = None
    timestamp_hash_algorithm: str | None = None
    tsa_subject: str | None = None
    tsa_certificate_sha256: str | None = None
    incremental_update: bool = False
    existing_signature_count: int = 0
    preserved_valid_signature_count: int = 0
    total_signature_count: int = 1
    source_revision_bytes: int = 0
    signature_profile: str = SIGNATURE_PROFILE_ADBE
    signature_sub_filter: str = "adbe.pkcs7.detached"
    pades_profile_status: str = "not_pades_subfilter"

    def summary(self) -> dict[str, Any]:
        return {
            "field_name": self.field_name,
            "created_field": self.created_field,
            "page": self.page,
            "signer_subject": self.signer_subject,
            "certificate_sha256": self.certificate_sha256,
            "hash_algorithm": self.hash_algorithm,
            "signature_profile": self.signature_profile,
            "signature_sub_filter": self.signature_sub_filter,
            "pades_profile_status": self.pades_profile_status,
            "pades_conformance_status": "not_externally_evaluated",
            "certify_permission": self.certify_permission,
            "cms_size": self.cms_size,
            "reserved_contents_size": self.reserved_contents_size,
            "pages": self.pages,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "status": self.status,
            "trust_status": "not_evaluated",
            "verified": self.verified,
            "timestamp_status": self.timestamp_status,
            "timestamp_time": self.timestamp_time,
            "timestamp_policy_oid": self.timestamp_policy_oid,
            "timestamp_hash_algorithm": self.timestamp_hash_algorithm,
            "tsa_subject": self.tsa_subject,
            "tsa_certificate_sha256": self.tsa_certificate_sha256,
            "tsa_trust_status": (
                "not_evaluated" if self.timestamp_status is not None else None
            ),
            "incremental_update": self.incremental_update,
            "existing_signature_count": self.existing_signature_count,
            "preserved_valid_signature_count": (
                self.preserved_valid_signature_count
            ),
            "total_signature_count": self.total_signature_count,
            "source_revision_bytes": self.source_revision_bytes,
        }


@dataclass(frozen=True, slots=True)
class _SignerIdentity:
    private_key: Any
    certificate: x509.Certificate
    additional_certificates: tuple[x509.Certificate, ...]

    @property
    def fingerprint(self) -> str:
        return self.certificate.fingerprint(hashes.SHA256()).hex()

    @property
    def subject(self) -> str:
        return self.certificate.subject.rfc4514_string()[:1000]

    @property
    def display_name(self) -> str:
        for attribute_id in (NameOID.COMMON_NAME, NameOID.ORGANIZATION_NAME):
            values = self.certificate.subject.get_attributes_for_oid(attribute_id)
            if values and values[0].value:
                return str(values[0].value)[:200]
        return self.subject[:200]


def _check_pkcs12_password(password: str) -> bytes:
    if not isinstance(password, str):
        raise ToolError("PKCS#12 password must be text.")
    if len(password) > MAX_PKCS12_PASSWORD_CHARS or "\x00" in password:
        raise ToolError("PKCS#12 password is too long or contains a NUL character.")
    return password.encode("utf-8")


def _load_identity(path: Path, password: str) -> _SignerIdentity:
    if not path.is_file():
        raise ToolError(f"PKCS#12 file not found: {path.name}")
    if path.suffix.lower() not in {".p12", ".pfx"}:
        raise ToolError("The signing identity must be a .p12 or .pfx file.")
    if path.stat().st_size > MAX_PKCS12_BYTES:
        raise ToolError("The PKCS#12 file exceeds the 5 MB inspection limit.")

    try:
        private_key, certificate, additional = pkcs12.load_key_and_certificates(
            path.read_bytes(), _check_pkcs12_password(password)
        )
    except (TypeError, UnsupportedAlgorithm, ValueError) as exc:
        raise ToolError(
            "Could not unlock the PKCS#12 identity; check the password and "
            "file format."
        ) from exc

    if private_key is None or certificate is None:
        raise ToolError("The PKCS#12 file does not contain a private key and certificate.")
    if not isinstance(
        private_key,
        (
            rsa.RSAPrivateKey,
            ec.EllipticCurvePrivateKey,
            dsa.DSAPrivateKey,
            ed25519.Ed25519PrivateKey,
            ed448.Ed448PrivateKey,
        ),
    ):
        raise ToolError("The PKCS#12 private-key type is not supported for PDF signing.")

    now = datetime.now(UTC)
    if not (certificate.not_valid_before_utc <= now <= certificate.not_valid_after_utc):
        raise ToolError("The signing certificate is expired or not yet valid.")
    try:
        usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        usage = None
    if usage is not None and not usage.digital_signature:
        raise ToolError("The signing certificate does not allow digital signatures.")

    signer_fingerprint = certificate.fingerprint(hashes.SHA256())
    unique_additional: list[x509.Certificate] = []
    seen = {signer_fingerprint}
    for item in additional or ():
        fingerprint = item.fingerprint(hashes.SHA256())
        if fingerprint not in seen:
            seen.add(fingerprint)
            unique_additional.append(item)
    return _SignerIdentity(
        private_key=private_key,
        certificate=certificate,
        additional_certificates=tuple(unique_additional),
    )


def _hash_algorithm(name: str) -> hashes.HashAlgorithm:
    normalized = name.strip().lower().replace("-", "")
    if normalized not in SUPPORTED_SIGNATURE_HASHES:
        allowed = ", ".join(SUPPORTED_SIGNATURE_HASHES)
        raise ToolError(f"PDF signing hash must be one of: {allowed}.")
    return {
        "sha256": hashes.SHA256(),
        "sha384": hashes.SHA384(),
        "sha512": hashes.SHA512(),
    }[normalized]


def _normalise_signature_profile(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    aliases = {
        "adbe": SIGNATURE_PROFILE_ADBE,
        "adobe": SIGNATURE_PROFILE_ADBE,
        "pkcs7": SIGNATURE_PROFILE_ADBE,
        "pades": SIGNATURE_PROFILE_PADES_BB,
        "pades-bb": SIGNATURE_PROFILE_PADES_BB,
        "pades-b-b": SIGNATURE_PROFILE_PADES_BB,
    }
    profile = aliases.get(normalized)
    if profile is None:
        allowed = ", ".join(SUPPORTED_SIGNATURE_PROFILES)
        raise ToolError(f"PDF signature profile must be one of: {allowed}.")
    return profile


def _signature_subfilter(profile: str) -> str:
    return (
        "ETSI.CAdES.detached"
        if profile == SIGNATURE_PROFILE_PADES_BB
        else "adbe.pkcs7.detached"
    )


def _clean_text(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_SIGNATURE_TEXT_CHARS or "\x00" in cleaned:
        raise ToolError(f"PDF signature {label} is too long or contains NUL.")
    return cleaned


def _pdf_date(value: datetime) -> str:
    return value.astimezone(UTC).strftime("D:%Y%m%d%H%M%SZ")


def _initial_contents_capacity(identity: _SignerIdentity) -> int:
    certificate_bytes = sum(
        len(certificate.public_bytes(serialization.Encoding.DER))
        for certificate in (
            identity.certificate,
            *identity.additional_certificates,
        )
    )
    estimated = certificate_bytes + 8192
    rounded = ((estimated + 4095) // 4096) * 4096
    return max(MIN_SIGNATURE_CONTENTS_BYTES, min(rounded, MAX_SIGNATURE_CONTENTS_BYTES))


def _select_field(
    inspection: Any,
    *,
    field_name: str | None,
    create_field: bool,
    all_field_names: set[str],
) -> tuple[str, bool]:
    signature_names = [item.field_name for item in inspection.signatures]
    signed_names = {
        item.field_name for item in inspection.signatures if item.signed
    }
    blank_names = [
        item.field_name
        for item in inspection.signatures
        if item.status == "unsigned"
    ]
    if field_name is not None:
        cleaned = field_name.strip()
        if not cleaned or len(cleaned) > MAX_SIGNATURE_FIELD_NAME_CHARS:
            raise ToolError("The PDF signature field name is empty or too long.")
        if "\x00" in cleaned:
            raise ToolError("The PDF signature field name contains a NUL character.")
        if cleaned in signed_names:
            raise ToolError(
                f"PDF signature field {cleaned!r} is already signed; choose a "
                "blank signature field or create a new one."
            )
        if cleaned in blank_names:
            return cleaned, False
        if cleaned in signature_names or cleaned in all_field_names:
            raise ToolError(f"PDF field {cleaned!r} is not a blank signature field.")
        if not create_field:
            raise ToolError(f"PDF signature field was not found: {cleaned!r}.")
        if "." in cleaned or "/" in cleaned:
            raise ToolError("A new signature field name must not contain '.' or '/'.")
        return cleaned, True

    if len(blank_names) == 1:
        return blank_names[0], False
    if len(blank_names) > 1:
        raise ToolError(
            "Choose one existing blank signature field with --field; this PDF "
            f"has {len(blank_names)} blank signature fields."
        )
    index = 1
    while f"Signature{index}" in all_field_names:
        index += 1
    return f"Signature{index}", True


def _existing_docmdp_permission(inspection: Any) -> int | None:
    """Validate signatures that an incremental update must preserve."""

    signed = [item for item in inspection.signatures if item.signed]
    if not signed:
        return None
    document_timestamps = [
        item.field_name
        for item in signed
        if item.document_timestamp is not None
        or item.sub_filter == "ETSI.RFC3161"
    ]
    if document_timestamps:
        names = ", ".join(sorted(document_timestamps))
        raise ToolError(
            "Incremental signing does not support PDFs with existing "
            "document timestamps; collect signer signatures before adding "
            f"a DocTimeStamp. Existing document timestamp field(s): {names}."
        )
    unsupported = [
        item.field_name
        for item in signed
        if item.status != "cryptographically_valid_untrusted"
        or item.byte_range is None
        or item.document_timestamp is not None
        or item.sub_filter == "ETSI.RFC3161"
    ]
    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise ToolError(
            "Incremental signing requires every existing document signature "
            "to be cryptographically valid and supported; unsupported or "
            f"invalid field(s): {names}."
        )
    permissions = [
        item.docmdp_permission
        for item in signed
        if item.docmdp_permission is not None
    ]
    return min(permissions) if permissions else None


def _signer_fingerprints(signature: Any) -> tuple[str, ...]:
    return tuple(
        sorted(
            signer.certificate.sha256_fingerprint
            for signer in signature.signers
            if signer.certificate is not None
        )
    )


def _verify_preserved_signatures(before: Any, after: Any) -> int:
    before_signed = {
        item.field_name: item for item in before.signatures if item.signed
    }
    after_by_name = {item.field_name: item for item in after.signatures}
    for field_name, previous in before_signed.items():
        current = after_by_name.get(field_name)
        if (
            current is None
            or current.status != "cryptographically_valid_untrusted"
            or current.byte_range != previous.byte_range
            or current.contents_size != previous.contents_size
            or current.cms_size != previous.cms_size
            or current.docmdp_permission != previous.docmdp_permission
            or _signer_fingerprints(current) != _signer_fingerprints(previous)
        ):
            raise ToolError(
                "Signed PDF failed verification: existing signature "
                f"{field_name!r} was not preserved."
            )
    return len(before_signed)


def _find_writer_signature_field(writer: Any, field_name: str) -> Any | None:
    root = _object(getattr(writer, "_root_object", None))
    fields = _items(_get(_get(root, "/AcroForm"), "/Fields"))
    seen: set[int] = set()

    def walk(node: Any, prefix: str, inherited_ft: str) -> Any | None:
        obj = _object(node)
        if obj is None or id(obj) in seen:
            return None
        seen.add(id(obj))
        field_type = _as_name(_get(obj, "/FT", inherited_ft) or inherited_ft)
        local = _as_text(_get(obj, "/T")).strip()
        qualified = f"{prefix}.{local}" if prefix and local else local or prefix
        kids = _items(_get(obj, "/Kids"))
        field_kids = [
            kid
            for kid in kids
            if _has(_object(kid), "/T")
            or _has(_object(kid), "/Kids")
            or _has(_object(kid), "/FT")
        ]
        if field_kids:
            for kid in field_kids:
                found = walk(kid, qualified, field_type)
                if found is not None:
                    return found
            return None
        if qualified == field_name and field_type == "Sig":
            return obj
        return None

    for field in fields:
        found = walk(field, "", "")
        if found is not None:
            return found
    return None


def _escape_pdf_text(text: str) -> str:
    safe = text.encode("latin-1", errors="replace").decode("latin-1")
    safe = "".join(character if 32 <= ord(character) <= 126 else "?" for character in safe)
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _appearance_stream(
    writer: Any,
    *,
    width: float,
    height: float,
    signer_name: str,
    signing_time: datetime,
) -> Any:
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    font = DictionaryObject()
    font.update(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )
    fonts = DictionaryObject()
    fonts[NameObject("/Helv")] = writer._add_object(font)
    resources = DictionaryObject()
    resources[NameObject("/Font")] = fonts

    first = _escape_pdf_text(f"Digitally signed by {signer_name}")[:80]
    second = _escape_pdf_text(
        "Date: " + signing_time.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    )
    content = (
        f"q 0.96 g 0 0 {width:g} {height:g} re f "
        f"0.25 g 1 w 0.5 0.5 {width - 1:g} {height - 1:g} re S "
        f"BT /Helv 7 Tf 4 {max(height - 12, 4):g} Td ({first}) Tj "
        f"0 -9 Td ({second}) Tj ET Q\n"
    )
    stream = DecodedStreamObject()
    stream.set_data(content.encode("ascii"))
    stream.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/Resources"): resources,
        }
    )
    return stream


def _build_placeholder_pdf(
    *,
    source: Path,
    identity: _SignerIdentity,
    field_name: str,
    create_field: bool,
    contents_capacity: int,
    page_number: int,
    rectangle: tuple[float, float, float, float] | None,
    reason: str | None,
    location: str | None,
    contact_info: str | None,
    certify_permission: int | None,
    signing_time: datetime,
    incremental: bool = False,
    signature_profile: str = SIGNATURE_PROFILE_ADBE,
) -> bytes:
    from pypdf.generic import (
        ArrayObject,
        ByteStringObject,
        DictionaryObject,
        NameObject,
        NumberObject,
        RectangleObject,
        TextStringObject,
    )

    _PdfReader, PdfWriter = _load_pypdf()
    writer = (
        PdfWriter(str(source), incremental=True)
        if incremental
        else PdfWriter(clone_from=str(source))
    )
    if incremental:
        # pypdf's incremental writer does not represent the previous
        # cross-reference stream as a cloned regular object. Reserve its
        # object number (trailer /Size - 1) so a new signature cannot reuse
        # that number and collide with readers' cached XRef stream object.
        try:
            reserved_objects = int(writer._reader.trailer.get("/Size")) - 1
        except (AttributeError, TypeError, ValueError):
            reserved_objects = len(writer._objects)
        while len(writer._objects) < reserved_objects:
            writer._objects.append(None)
    try:
        if not (1 <= page_number <= len(writer.pages)):
            raise ToolError(
                f"Signature page {page_number} is outside this PDF "
                f"(1 to {len(writer.pages)})."
            )

        placeholder = NumberObject(9999999999)
        signature = DictionaryObject()
        signature.update(
            {
                NameObject("/Type"): NameObject("/Sig"),
                NameObject("/Filter"): NameObject("/Adobe.PPKLite"),
                NameObject("/SubFilter"): NameObject(
                    f"/{_signature_subfilter(signature_profile)}"
                ),
                NameObject("/ByteRange"): ArrayObject(
                    [NumberObject(0), placeholder, placeholder, placeholder]
                ),
                NameObject("/Contents"): ByteStringObject(
                    b"\x00" * contents_capacity
                ),
                NameObject("/Name"): TextStringObject(identity.display_name),
                NameObject("/M"): TextStringObject(_pdf_date(signing_time)),
            }
        )
        if reason:
            signature[NameObject("/Reason")] = TextStringObject(reason)
        if location:
            signature[NameObject("/Location")] = TextStringObject(location)
        if contact_info:
            signature[NameObject("/ContactInfo")] = TextStringObject(contact_info)

        app = DictionaryObject()
        app[NameObject("/Name")] = NameObject("/SelfHealingPCAgent")
        sig_build = DictionaryObject()
        sig_build[NameObject("/App")] = app
        prop_build = DictionaryObject()
        prop_build[NameObject("/Sig")] = sig_build
        signature[NameObject("/Prop_Build")] = prop_build

        if certify_permission is not None:
            params = DictionaryObject()
            params.update(
                {
                    NameObject("/Type"): NameObject("/TransformParams"),
                    NameObject("/P"): NumberObject(certify_permission),
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

        root = writer._root_object
        if signature_profile == SIGNATURE_PROFILE_PADES_BB:
            extensions = _object(_get(root, "/Extensions"))
            if extensions is None:
                extensions = DictionaryObject()
                root[NameObject("/Extensions")] = extensions
            if hasattr(extensions, "keys"):
                esic = _object(_get(extensions, "/ESIC"))
                if esic is None:
                    esic = DictionaryObject()
                    esic[NameObject("/BaseVersion")] = NameObject("/1.7")
                    esic[NameObject("/ExtensionLevel")] = NumberObject(2)
                    extensions[NameObject("/ESIC")] = esic
                elif hasattr(esic, "keys"):
                    if _get(esic, "/BaseVersion") is None:
                        esic[NameObject("/BaseVersion")] = NameObject("/1.7")
                    level = _as_int(_get(esic, "/ExtensionLevel"))
                    if level is None or level < 2:
                        esic[NameObject("/ExtensionLevel")] = NumberObject(2)
        acro_form = _object(_get(root, "/AcroForm"))
        if acro_form is None:
            acro_form = DictionaryObject()
            acro_form[NameObject("/Fields")] = ArrayObject()
            root[NameObject("/AcroForm")] = writer._add_object(acro_form)
            acro_form = _object(root[NameObject("/AcroForm")])
        fields = _object(_get(acro_form, "/Fields"))
        if fields is None:
            fields = ArrayObject()
            acro_form[NameObject("/Fields")] = fields
        flags = _as_int(_get(acro_form, "/SigFlags")) or 0
        acro_form[NameObject("/SigFlags")] = NumberObject(flags | 3)

        if create_field:
            page = writer.pages[page_number - 1]
            mediabox = page.mediabox
            page_left = float(mediabox.left)
            page_bottom = float(mediabox.bottom)
            page_width = float(mediabox.width)
            page_height = float(mediabox.height)
            if rectangle is None:
                rect = RectangleObject([0, 0, 0, 0])
            else:
                x_value, y_value, width, height = rectangle
                if not all(math.isfinite(value) for value in rectangle):
                    raise ToolError("The signature rectangle must contain finite numbers.")
                if width <= 0 or height <= 0:
                    raise ToolError("The signature rectangle width/height must be positive.")
                if (
                    x_value < 0
                    or y_value < 0
                    or x_value + width > page_width
                    or y_value + height > page_height
                ):
                    raise ToolError("The signature rectangle is outside the selected page.")
                rect = RectangleObject(
                    [
                        page_left + x_value,
                        page_bottom + y_value,
                        page_left + x_value + width,
                        page_bottom + y_value + height,
                    ]
                )

            field = DictionaryObject()
            field.update(
                {
                    NameObject("/Type"): NameObject("/Annot"),
                    NameObject("/Subtype"): NameObject("/Widget"),
                    NameObject("/Rect"): rect,
                    NameObject("/F"): NumberObject(4),
                    NameObject("/FT"): NameObject("/Sig"),
                    NameObject("/T"): TextStringObject(field_name),
                    NameObject("/V"): signature_ref,
                }
            )
            if rectangle is not None:
                appearance = _appearance_stream(
                    writer,
                    width=float(rect.width),
                    height=float(rect.height),
                    signer_name=identity.display_name,
                    signing_time=signing_time,
                )
                # `_appearance_stream` deliberately leaves BBox assignment to
                # this call site so it can use RectangleObject directly.
                appearance[NameObject("/BBox")] = RectangleObject(
                    [0, 0, float(rect.width), float(rect.height)]
                )
                normal = writer._add_object(appearance)
                appearance_dict = DictionaryObject()
                appearance_dict[NameObject("/N")] = normal
                field[NameObject("/AP")] = appearance_dict
            field_ref = writer._add_object(field)
            fields.append(field_ref)
            annotations = _object(_get(page, "/Annots"))
            if annotations is None:
                annotations = ArrayObject()
                page[NameObject("/Annots")] = annotations
            annotations.append(field_ref)
        else:
            target = _find_writer_signature_field(writer, field_name)
            if target is None:
                raise ToolError(f"PDF signature field was not found: {field_name!r}.")
            target[NameObject("/V")] = signature_ref

        if certify_permission is not None:
            perms = _object(_get(root, "/Perms"))
            if perms is None:
                perms = DictionaryObject()
                root[NameObject("/Perms")] = writer._add_object(perms)
                perms = _object(root[NameObject("/Perms")])
            if _get(perms, "/DocMDP") is not None:
                raise ToolError("This PDF already has a DocMDP certification signature.")
            perms[NameObject("/DocMDP")] = signature_ref

        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()
    finally:
        writer.close()



def _placeholder_layout(
    data: bytes, capacity: int
) -> tuple[re.Match[bytes], int, int]:
    needle = b"<" + b"0" * (capacity * 2) + b">"
    first = data.find(needle)
    if first < 0 or data.find(needle, first + 1) >= 0:
        raise ToolError("Could not locate the reserved PDF signature Contents.")
    match = re.search(
        rb"/ByteRange\s*\[\s*0\s+(9999999999)\s+(9999999999)\s+(9999999999)\s*\]",
        data,
    )
    if match is None:
        raise ToolError("Could not locate the reserved PDF signature ByteRange.")
    return match, first, first + len(needle) - 1


def _digest_algorithm_identifier(hash_name: str) -> bytes:
    return _sequence(_oid(_DIGEST_OIDS[hash_name]), _tlv(0x05, b""))


def _signing_certificate_v2(
    certificate: x509.Certificate,
    hash_name: str,
) -> bytes:
    certificate_der = certificate.public_bytes(serialization.Encoding.DER)
    issuer_serial = _sequence(
        _sequence(_context(4, certificate.issuer.public_bytes())),
        _integer_der(certificate.serial_number),
    )
    parts: list[bytes] = []
    if hash_name != "sha256":
        # SHA-256 is the RFC 5035 default and is therefore omitted.
        parts.append(_digest_algorithm_identifier(hash_name))
    parts.extend(
        [
            _octet_string(hashlib.new(hash_name, certificate_der).digest()),
            issuer_serial,
        ]
    )
    return _sequence(_sequence(_sequence(*parts)))


def _generate_pades_cms(
    identity: _SignerIdentity,
    signed_bytes: bytes,
    hash_name: str,
) -> bytes:
    """Build a minimal CAdES SignedData for the local PAdES B-B profile."""

    private_key = identity.private_key
    if not isinstance(
        private_key,
        (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey),
    ):
        raise ToolError(
            "The PAdES B-B profile supports RSA and ECDSA identities only; "
            "use the legacy adbe profile for this key type."
        )
    hash_algorithm = _hash_algorithm(hash_name)
    digest_algorithm = _digest_algorithm_identifier(hash_name)
    message_digest = hashlib.new(hash_name, signed_bytes).digest()
    attributes = [
        _sequence(_oid(_OID_CONTENT_TYPE), _set(_oid(_OID_DATA))),
        _sequence(
            _oid(_OID_MESSAGE_DIGEST),
            _set(_octet_string(message_digest)),
        ),
        _sequence(
            _oid(_OID_SIGNING_CERTIFICATE_V2),
            _set(_signing_certificate_v2(identity.certificate, hash_name)),
        ),
    ]
    signed_attributes = _set(*attributes)
    try:
        if isinstance(private_key, rsa.RSAPrivateKey):
            signature_algorithm = _sequence(
                _oid(_RSA_SIGNATURE_OIDS[hash_name]),
                _tlv(0x05, b""),
            )
            signature = private_key.sign(
                signed_attributes,
                padding.PKCS1v15(),
                hash_algorithm,
            )
        else:
            signature_algorithm = _sequence(
                _oid(_ECDSA_SIGNATURE_OIDS[hash_name])
            )
            signature = private_key.sign(
                signed_attributes,
                ec.ECDSA(hash_algorithm),
            )
    except (TypeError, UnsupportedAlgorithm, ValueError) as exc:
        raise ToolError(
            "PAdES CMS signature creation is unsupported for this identity: "
            f"{type(exc).__name__}"
        ) from exc

    signer_info = _sequence(
        _integer_der(1),
        _sequence(
            identity.certificate.issuer.public_bytes(),
            _integer_der(identity.certificate.serial_number),
        ),
        digest_algorithm,
        _context(0, b"".join(sorted(attributes))),
        signature_algorithm,
        _octet_string(signature),
    )
    certificates = _context(
        0,
        b"".join(
            certificate.public_bytes(serialization.Encoding.DER)
            for certificate in (
                identity.certificate,
                *identity.additional_certificates,
            )
        ),
    )
    signed_data = _sequence(
        _integer_der(1),
        _set(digest_algorithm),
        _sequence(_oid(_OID_DATA)),
        certificates,
        _set(signer_info),
    )
    return _sequence(_oid(_OID_SIGNED_DATA), _context(0, signed_data))


def _generate_cms(
    identity: _SignerIdentity,
    signed_bytes: bytes,
    hash_name: str,
    *,
    signature_profile: str = SIGNATURE_PROFILE_ADBE,
) -> bytes:
    if signature_profile == SIGNATURE_PROFILE_PADES_BB:
        return _generate_pades_cms(identity, signed_bytes, hash_name)
    builder = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(signed_bytes)
        .add_signer(
            identity.certificate,
            identity.private_key,
            _hash_algorithm(hash_name),
        )
    )
    for certificate in identity.additional_certificates:
        builder = builder.add_certificate(certificate)
    try:
        return builder.sign(
            serialization.Encoding.DER,
            [
                pkcs7.PKCS7Options.Binary,
                pkcs7.PKCS7Options.DetachedSignature,
            ],
        )
    except (TypeError, UnsupportedAlgorithm, ValueError) as exc:
        raise ToolError(
            f"CMS signature creation is unsupported for this identity: "
            f"{type(exc).__name__}"
        ) from exc


def _validate_signing_inputs(
    source: Path,
    output: Path,
    *,
    page_number: int,
    certify_permission: int | None,
) -> tuple[Any, Any, str]:
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise ToolError(f"Source PDF file not found or not a PDF: {source.name}")
    if source.stat().st_size > MAX_PDF_SIGNING_FILE_BYTES:
        raise ToolError("PDF signing is limited to 100 MB files.")
    if output.suffix.lower() != ".pdf":
        raise ToolError("The signed PDF output must be a .pdf file.")
    if output.exists():
        raise ToolError(
            "Output file already exists. PDF tools never overwrite; "
            "choose a new output path."
        )
    if source.expanduser().resolve() == output.expanduser().resolve():
        raise ToolError("The signed PDF output must not be the source PDF.")
    if page_number < 1:
        raise ToolError("The signature page must be at least 1.")
    if certify_permission is not None and certify_permission not in {1, 2, 3}:
        raise ToolError("DocMDP certification permission must be 1, 2 or 3.")

    before_signatures = inspect_pdf_signatures(source)
    if before_signatures.pages > MAX_PDF_SIGNING_PAGES:
        raise ToolError(f"PDF signing is limited to {MAX_PDF_SIGNING_PAGES} pages.")
    existing_permission = _existing_docmdp_permission(before_signatures)
    if before_signatures.signed_count and certify_permission is not None:
        raise ToolError(
            "A DocMDP certification signature must be the first signature; "
            "it cannot be added after an existing signature."
        )
    if existing_permission == 1:
        raise ToolError(
            "The existing DocMDP certification permission P=1 prohibits "
            "all changes, including an incremental signature."
        )
    from .pdf_forms import inspect_pdf_form

    before_form = inspect_pdf_form(source)
    return before_signatures, before_form, before_signatures.sha256


def sign_pdf(
    source: Path,
    output: Path,
    *,
    pkcs12_path: Path,
    pkcs12_password: str,
    field_name: str | None = None,
    create_field: bool = False,
    page: int = 1,
    rectangle: tuple[float, float, float, float] | None = (36.0, 36.0, 200.0, 40.0),
    reason: str | None = None,
    location: str | None = None,
    contact_info: str | None = None,
    certify_permission: int | None = None,
    hash_algorithm: str = "sha256",
    signature_profile: str = SIGNATURE_PROFILE_ADBE,
    timestamp_url: str | None = None,
    timestamp_hash_algorithm: str = "sha256",
    timestamp_timeout_seconds: int = 15,
    timestamp_allow_private_network: bool = False,
    timestamp_opener: Any | None = None,
) -> PdfSigningResult:
    """Create a verified detached CMS signature in a new PDF copy."""

    before_signatures, before_form, source_hash = _validate_signing_inputs(
        source,
        output,
        page_number=page,
        certify_permission=certify_permission,
    )
    normalized_hash = hash_algorithm.strip().lower().replace("-", "")
    _hash_algorithm(normalized_hash)
    normalized_profile = _normalise_signature_profile(signature_profile)
    reason = _clean_text(reason, "reason")
    location = _clean_text(location, "location")
    contact_info = _clean_text(contact_info, "contact information")
    target_field, should_create = _select_field(
        before_signatures,
        field_name=field_name,
        create_field=create_field,
        all_field_names=set(before_form.field_names),
    )
    incremental = before_signatures.signed_count > 0
    existing_permission = _existing_docmdp_permission(before_signatures)
    if incremental and existing_permission == 2 and should_create:
        raise ToolError(
            "The existing DocMDP certification permission P=2 permits "
            "signing an existing blank signature field, but not creating "
            "a new signature field."
        )
    source_bytes = source.read_bytes() if incremental else b""
    if incremental and hashlib.sha256(source_bytes).hexdigest() != source_hash:
        raise ToolError("PDF changed during signing preparation; retry.")
    identity = _load_identity(pkcs12_path, pkcs12_password)
    signing_time = datetime.now(UTC)
    clean_timestamp_url = timestamp_url.strip() if timestamp_url else None
    normalized_timestamp_hash = (
        timestamp_hash_algorithm.strip().lower().replace("-", "")
    )
    if clean_timestamp_url is not None and normalized_timestamp_hash not in (
        SUPPORTED_SIGNATURE_HASHES
    ):
        raise ToolError("Timestamp hash must be sha256, sha384 or sha512.")
    capacity = _initial_contents_capacity(identity)
    if clean_timestamp_url is not None:
        capacity += 32_768

    signed_data: bytes | None = None
    cms_der: bytes | None = None
    timestamp_attachment: TimestampAttachment | None = None
    while capacity <= MAX_SIGNATURE_CONTENTS_BYTES:
        placeholder_data = _build_placeholder_pdf(
            source=source,
            identity=identity,
            field_name=target_field,
            create_field=should_create,
            contents_capacity=capacity,
            page_number=page,
            rectangle=rectangle,
            reason=reason,
            location=location,
            contact_info=contact_info,
            certify_permission=certify_permission,
            signing_time=signing_time,
            incremental=incremental,
            signature_profile=normalized_profile,
        )
        if incremental and not placeholder_data.startswith(source_bytes):
            raise ToolError(
                "Incremental PDF signing failed to preserve the exact "
                "source revision prefix."
            )
        match, hex_start, hex_end = _placeholder_layout(
            placeholder_data, capacity
        )
        data = bytearray(placeholder_data)
        first_length = hex_start
        second_start = hex_end + 1
        second_length = len(data) - second_start
        for group, value in zip(
            (1, 2, 3),
            (first_length, second_start, second_length),
            strict=True,
        ):
            start, end = match.span(group)
            encoded = f"{value:010d}".encode("ascii")
            if len(encoded) != 10:
                raise ToolError("The PDF is too large for the reserved ByteRange.")
            data[start:end] = encoded

        signed_bytes = bytes(data[:first_length]) + bytes(data[second_start:])
        cms_der = _generate_cms(
            identity,
            signed_bytes,
            normalized_hash,
            signature_profile=normalized_profile,
        )
        timestamp_attachment = None
        if clean_timestamp_url is not None:
            timestamp_attachment = request_timestamp_for_cms(
                cms_der,
                timestamp_url=clean_timestamp_url,
                hash_algorithm=normalized_timestamp_hash,
                timeout_seconds=timestamp_timeout_seconds,
                allow_private_network=timestamp_allow_private_network,
                opener=timestamp_opener,
            )
            cms_der = timestamp_attachment.cms_der
        if len(cms_der) <= capacity:
            hexed = cms_der.hex().encode("ascii")
            hex_capacity = hex_end - hex_start - 1
            data[hex_start + 1 : hex_end] = (
                hexed + b"0" * (hex_capacity - len(hexed))
            )
            signed_data = bytes(data)
            break

        next_capacity = ((len(cms_der) + 4095) // 4096) * 4096
        if next_capacity <= capacity:
            next_capacity = capacity + 4096
        capacity = next_capacity

    if signed_data is None or cms_der is None:
        raise ToolError("The CMS signature exceeds the 1 MB reserved limit.")
    if incremental and not signed_data.startswith(source_bytes):
        raise ToolError(
            "Incremental PDF signing failed to preserve the exact source "
            "revision prefix."
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(
        f".{output.stem}.tmp-{secrets.token_hex(4)}.pdf"
    )
    preserved_valid_signature_count = 0
    try:
        temporary.write_bytes(signed_data)
        after_signatures = inspect_pdf_signatures(temporary)
        if after_signatures.pages != before_signatures.pages:
            raise ToolError("Signed PDF failed verification: page count changed.")
        expected_signature_count = before_signatures.signature_count + (
            1 if should_create else 0
        )
        if (
            after_signatures.signature_count != expected_signature_count
            or after_signatures.signed_count
            != before_signatures.signed_count + 1
        ):
            raise ToolError(
                "Signed PDF failed verification: signature count changed "
                "unexpectedly."
            )
        preserved_valid_signature_count = _verify_preserved_signatures(
            before_signatures, after_signatures
        )

        from .pdf_forms import inspect_pdf_form

        after_form = inspect_pdf_form(temporary)
        expected_fields = list(before_form.field_names)
        if should_create:
            expected_fields.append(target_field)
        if set(after_form.field_names) != set(expected_fields):
            raise ToolError("Signed PDF failed verification: form fields changed.")

        target = next(
            (
                item
                for item in after_signatures.signatures
                if item.field_name == target_field
            ),
            None,
        )
        if target is None:
            raise ToolError("Signed PDF failed verification: signature field is missing.")
        if target.status != "cryptographically_valid_untrusted":
            raise ToolError(
                "Signed PDF failed cryptographic verification: "
                f"{target.status_detail}"
            )
        expected_subfilter = _signature_subfilter(normalized_profile)
        if target.sub_filter != expected_subfilter:
            raise ToolError("Signed PDF failed signature-profile verification.")
        if normalized_profile == SIGNATURE_PROFILE_PADES_BB:
            target_signer = next(
                (
                    signer
                    for signer in target.signers
                    if signer.certificate is not None
                    and signer.certificate.sha256_fingerprint == identity.fingerprint
                ),
                None,
            )
            if (
                target.pades_profile_status != "bb_structure_verified"
                or target_signer is None
                or target_signer.signing_certificate_v2_valid is not True
                or target_signer.signing_time is not None
            ):
                raise ToolError(
                    "Signed PDF failed PAdES B-B structure verification: "
                    f"{target.pades_profile_detail}"
                )
        if target.covers_whole_file is not True:
            raise ToolError("Signed PDF failed ByteRange coverage verification.")
        if certify_permission is not None and target.docmdp_permission != certify_permission:
            raise ToolError("Signed PDF failed DocMDP verification.")
        signer_fingerprints = {
            signer.certificate.sha256_fingerprint
            for signer in target.signers
            if signer.certificate is not None
        }
        if identity.fingerprint not in signer_fingerprints:
            raise ToolError("Signed PDF failed signer-certificate verification.")
        if clean_timestamp_url is not None:
            timestamp = next(
                (
                    signer.timestamp
                    for signer in target.signers
                    if signer.certificate is not None
                    and signer.certificate.sha256_fingerprint == identity.fingerprint
                ),
                None,
            )
            if timestamp is None or timestamp.status != "verified_untrusted":
                raise ToolError("Signed PDF failed RFC 3161 timestamp verification.")
            if timestamp_attachment is None or timestamp.generation_time != (
                timestamp_attachment.generation_time
            ):
                raise ToolError("Signed PDF timestamp receipt does not match.")
        if sha256_file(source) != source_hash:
            raise ToolError("PDF signing changed the source; output rejected.")
        if output.exists():
            raise ToolError(
                "Output file appeared during PDF signing; it was not overwritten."
            )
        os.replace(temporary, output)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    final = inspect_pdf_signatures(output)
    final_target = next(
        item for item in final.signatures if item.field_name == target_field
    )
    final_timestamp = next(
        (
            signer.timestamp
            for signer in final_target.signers
            if signer.timestamp is not None
        ),
        None,
    )
    preserved_valid_signature_count = _verify_preserved_signatures(
        before_signatures, final
    )
    return PdfSigningResult(
        source_path=source,
        output_path=output,
        field_name=target_field,
        created_field=should_create,
        page=final_target.page,
        signer_subject=identity.subject,
        certificate_sha256=identity.fingerprint,
        hash_algorithm=normalized_hash,
        certify_permission=certify_permission,
        cms_size=len(cms_der),
        reserved_contents_size=capacity,
        pages=final.pages,
        size_bytes=final.size_bytes,
        sha256=final.sha256,
        status=final_target.status,
        timestamp_status=(
            final_timestamp.status if final_timestamp is not None else None
        ),
        timestamp_time=(
            final_timestamp.generation_time
            if final_timestamp is not None
            else None
        ),
        timestamp_policy_oid=(
            final_timestamp.policy_oid if final_timestamp is not None else None
        ),
        timestamp_hash_algorithm=(
            final_timestamp.hash_algorithm
            if final_timestamp is not None
            else None
        ),
        tsa_subject=(
            final_timestamp.signer_certificate.subject
            if final_timestamp is not None
            and final_timestamp.signer_certificate is not None
            else None
        ),
        tsa_certificate_sha256=(
            final_timestamp.signer_certificate.sha256_fingerprint
            if final_timestamp is not None
            and final_timestamp.signer_certificate is not None
            else None
        ),
        incremental_update=incremental,
        existing_signature_count=before_signatures.signed_count,
        preserved_valid_signature_count=preserved_valid_signature_count,
        total_signature_count=final.signed_count,
        source_revision_bytes=len(source_bytes) if incremental else 0,
        signature_profile=normalized_profile,
        signature_sub_filter=final_target.sub_filter or expected_subfilter,
        pades_profile_status=final_target.pades_profile_status,
    )


