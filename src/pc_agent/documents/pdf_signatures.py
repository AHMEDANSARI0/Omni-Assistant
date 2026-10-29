"""Bounded PDF digital-signature inspection and cryptographic checks.

Phase 27 is read-only and direct-only. It reports signature fields,
ByteRange coverage, CMS digest/signature verification and embedded X.509
certificate details. It does not create a signature, contact a network,
check revocation, or evaluate whether a certificate chains to a trusted
root; trust is always reported as ``not_evaluated``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed448, ed25519, padding, rsa

from ..errors import ToolError
from .pdf import _load_pypdf, inspect_pdf, sha256_file
from .pdf_forms import (
    MAX_PDF_FORM_PAGES,
    _as_int,
    _as_name,
    _as_text,
    _get,
    _items,
    _object,
    _raw_fields,
    _widget_locations,
    inspect_pdf_form,
)

MAX_PDF_SIGNATURE_FILE_BYTES = 100_000_000
MAX_PDF_SIGNATURES = 50
MAX_PDF_SIGNATURE_CONTENTS_BYTES = 2_000_000
MAX_CMS_CERTIFICATES = 20
MAX_CMS_SIGNERS = 10
MAX_DER_NODES = 10_000
MAX_DER_DEPTH = 30

_OID_SIGNED_DATA = "1.2.840.113549.1.7.2"
_OID_CONTENT_TYPE = "1.2.840.113549.1.9.3"
_OID_MESSAGE_DIGEST = "1.2.840.113549.1.9.4"
_OID_SIGNING_TIME = "1.2.840.113549.1.9.5"
_OID_RSA_PSS = "1.2.840.113549.1.1.10"
_OID_MGF1 = "1.2.840.113549.1.1.8"

_DIGEST_OIDS = {
    "1.2.840.113549.2.5": "md5",
    "1.3.14.3.2.26": "sha1",
    "2.16.840.1.101.3.4.2.4": "sha224",
    "2.16.840.1.101.3.4.2.1": "sha256",
    "2.16.840.1.101.3.4.2.2": "sha384",
    "2.16.840.1.101.3.4.2.3": "sha512",
}
_SUPPORTED_SUBFILTERS = {"adbe.pkcs7.detached", "ETSI.CAdES.detached"}


class _DerError(ValueError):
    """A bounded DER parse failure with a safe, local message."""


@dataclass(frozen=True, slots=True)
class _DerNode:
    tag_class: int
    constructed: bool
    tag: int
    header_length: int
    length: int
    start: int
    content_start: int
    end: int
    value: bytes
    raw: bytes
    children: tuple[_DerNode, ...] = ()


@dataclass(frozen=True, slots=True)
class PdfCertificateSummary:
    subject: str
    issuer: str
    serial_number: str
    sha256_fingerprint: str
    not_valid_before: str
    not_valid_after: str
    expired_now: bool
    self_signed: bool
    key_type: str
    key_size: int | str | None

    def summary(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "issuer": self.issuer,
            "serial_number": self.serial_number,
            "sha256_fingerprint": self.sha256_fingerprint,
            "not_valid_before": self.not_valid_before,
            "not_valid_after": self.not_valid_after,
            "expired_now": self.expired_now,
            "self_signed": self.self_signed,
            "key_type": self.key_type,
            "key_size": self.key_size,
        }


@dataclass(frozen=True, slots=True)
class PdfSignerSummary:
    digest_algorithm: str | None
    digest_matches: bool | None
    signature_algorithm: str | None
    signature_valid: bool | None
    signing_time: str | None
    certificate_valid_at_signing: bool | None
    certificate: PdfCertificateSummary | None
    status_detail: str

    def summary(self) -> dict[str, Any]:
        return {
            "digest_algorithm": self.digest_algorithm,
            "digest_matches": self.digest_matches,
            "signature_algorithm": self.signature_algorithm,
            "signature_valid": self.signature_valid,
            "signing_time": self.signing_time,
            "certificate_valid_at_signing": self.certificate_valid_at_signing,
            "certificate": (
                self.certificate.summary() if self.certificate is not None else None
            ),
            "status_detail": self.status_detail,
        }


@dataclass(frozen=True, slots=True)
class PdfSignatureSummary:
    field_name: str
    page: int | None
    widget_count: int
    status: str
    status_detail: str
    signed: bool
    signer_name: str | None
    reason: str | None
    location: str | None
    contact_info: str | None
    signature_date: str | None
    filter_name: str | None
    sub_filter: str | None
    byte_range: tuple[int, int, int, int] | None
    covers_whole_file: bool | None
    unsigned_trailing_bytes: int | None
    contents_size: int | None
    cms_size: int | None
    docmdp_permission: int | None
    signers: tuple[PdfSignerSummary, ...]
    certificates: tuple[PdfCertificateSummary, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "field_name": self.field_name,
            "page": self.page,
            "widget_count": self.widget_count,
            "status": self.status,
            "status_detail": self.status_detail,
            "signed": self.signed,
            "signer_name": self.signer_name,
            "reason": self.reason,
            "location": self.location,
            "contact_info": self.contact_info,
            "signature_date": self.signature_date,
            "filter": self.filter_name,
            "sub_filter": self.sub_filter,
            "byte_range": list(self.byte_range) if self.byte_range else None,
            "covers_whole_file": self.covers_whole_file,
            "unsigned_trailing_bytes": self.unsigned_trailing_bytes,
            "contents_size": self.contents_size,
            "cms_size": self.cms_size,
            "docmdp_permission": self.docmdp_permission,
            "signers": [item.summary() for item in self.signers],
            "certificates": [item.summary() for item in self.certificates],
            "trust_status": "not_evaluated",
        }


@dataclass(frozen=True, slots=True)
class PdfSignatureInspection:
    path: Path
    pages: int
    signatures: tuple[PdfSignatureSummary, ...]
    signature_flags: int | None
    size_bytes: int
    sha256: str

    @property
    def signature_count(self) -> int:
        return len(self.signatures)

    @property
    def signed_count(self) -> int:
        return sum(1 for item in self.signatures if item.signed)

    @property
    def valid_count(self) -> int:
        return sum(
            1
            for item in self.signatures
            if item.status == "cryptographically_valid_untrusted"
        )

    @property
    def invalid_count(self) -> int:
        return sum(
            1
            for item in self.signatures
            if item.status in {"invalid", "malformed"}
        )

    @property
    def unsupported_count(self) -> int:
        return sum(1 for item in self.signatures if item.status == "unsupported")

    @property
    def unsigned_count(self) -> int:
        return sum(1 for item in self.signatures if item.status == "unsigned")

    def summary(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "signature_count": self.signature_count,
            "signed_count": self.signed_count,
            "valid_count": self.valid_count,
            "invalid_count": self.invalid_count,
            "unsupported_count": self.unsupported_count,
            "unsigned_count": self.unsigned_count,
            "signature_flags": self.signature_flags,
            "trust_status": "not_evaluated",
            "signatures": [item.summary() for item in self.signatures],
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


def _der_length_bytes(length: int) -> bytes:
    if length < 128:
        return bytes([length])
    encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(encoded)]) + encoded


def _parse_der(
    data: bytes,
    offset: int,
    limit: int,
    *,
    depth: int,
    counter: list[int],
) -> _DerNode:
    if depth > MAX_DER_DEPTH:
        raise _DerError("DER structure is too deeply nested")
    counter[0] += 1
    if counter[0] > MAX_DER_NODES:
        raise _DerError("DER structure has too many elements")
    if offset >= limit:
        raise _DerError("Unexpected end of DER data")

    start = offset
    first = data[offset]
    offset += 1
    tag_class = first >> 6
    constructed = bool(first & 0x20)
    tag = first & 0x1F
    if tag == 0x1F:
        tag = 0
        for _index in range(4):
            if offset >= limit:
                raise _DerError("Truncated DER tag")
            byte = data[offset]
            offset += 1
            tag = (tag << 7) | (byte & 0x7F)
            if not byte & 0x80:
                break
        else:
            raise _DerError("DER tag is too long")

    if offset >= limit:
        raise _DerError("Truncated DER length")
    length_byte = data[offset]
    offset += 1
    if length_byte == 0x80:
        raise _DerError("Indefinite DER length is not allowed")
    if length_byte & 0x80:
        count = length_byte & 0x7F
        if count == 0 or count > 4 or offset + count > limit:
            raise _DerError("Invalid DER length")
        length = int.from_bytes(data[offset : offset + count], "big")
        offset += count
    else:
        length = length_byte

    content_start = offset
    end = content_start + length
    if end > limit:
        raise _DerError("DER element exceeds its containing data")

    children: list[_DerNode] = []
    if constructed:
        child_offset = content_start
        while child_offset < end:
            child = _parse_der(
                data,
                child_offset,
                end,
                depth=depth + 1,
                counter=counter,
            )
            children.append(child)
            child_offset = child.end
        if child_offset != end:
            raise _DerError("DER constructed element has trailing bytes")

    return _DerNode(
        tag_class=tag_class,
        constructed=constructed,
        tag=tag,
        header_length=content_start - start,
        length=length,
        start=start,
        content_start=content_start,
        end=end,
        value=data[content_start:end],
        raw=data[start:end],
        children=tuple(children),
    )


def _parse_der_root(data: bytes) -> _DerNode:
    if not data:
        raise _DerError("CMS contents are empty")
    if len(data) > MAX_PDF_SIGNATURE_CONTENTS_BYTES:
        raise _DerError("CMS contents exceed the inspection limit")
    counter = [0]
    root = _parse_der(data, 0, len(data), depth=0, counter=counter)
    if any(data[root.end :]):
        raise _DerError("CMS contents have non-zero trailing bytes")
    return root


def _is(node: _DerNode, tag_class: int, tag: int, *, constructed: bool | None = None) -> bool:
    return (
        node.tag_class == tag_class
        and node.tag == tag
        and (constructed is None or node.constructed is constructed)
    )


def _require(node: _DerNode, tag_class: int, tag: int, label: str) -> _DerNode:
    if not _is(node, tag_class, tag):
        raise _DerError(f"Malformed CMS {label}")
    return node


def _oid_string(node: _DerNode) -> str:
    _require(node, 0, 6, "object identifier")
    if not node.value:
        raise _DerError("Empty CMS object identifier")
    first = node.value[0]
    numbers = [first // 40, first % 40]
    current = 0
    pending = False
    for byte in node.value[1:]:
        pending = True
        current = (current << 7) | (byte & 0x7F)
        if current.bit_length() > 63:
            raise _DerError("CMS object identifier component is too large")
        if not byte & 0x80:
            numbers.append(current)
            current = 0
            pending = False
    if pending:
        raise _DerError("Truncated CMS object identifier")
    return ".".join(str(item) for item in numbers)


def _integer(node: _DerNode) -> int:
    _require(node, 0, 2, "integer")
    if not node.value or len(node.value) > 64:
        raise _DerError("Invalid CMS integer")
    return int.from_bytes(node.value, "big", signed=True)



def _hash_algorithm(name: str | None) -> hashes.HashAlgorithm | None:
    return {
        "md5": hashes.MD5(),
        "sha1": hashes.SHA1(),
        "sha224": hashes.SHA224(),
        "sha256": hashes.SHA256(),
        "sha384": hashes.SHA384(),
        "sha512": hashes.SHA512(),
    }.get(name or "")


def _digest_name_from_algorithm(node: _DerNode) -> str | None:
    _require(node, 0, 16, "digest algorithm")
    if not node.children:
        raise _DerError("Malformed CMS digest algorithm")
    return _DIGEST_OIDS.get(_oid_string(node.children[0]))


def _parse_asn1_time(node: _DerNode) -> datetime | None:
    try:
        text = node.value.decode("ascii")
    except UnicodeDecodeError:
        return None
    try:
        if node.tag == 23:  # UTCTime YYMMDDHHMMSSZ / offsets
            main = text[:12]
            parsed = datetime.strptime(main, "%y%m%d%H%M%S")
            rest = text[12:]
        elif node.tag == 24:  # GeneralizedTime YYYYMMDDHHMMSS
            main = text[:14]
            parsed = datetime.strptime(main, "%Y%m%d%H%M%S")
            rest = text[14:]
        else:
            return None
    except ValueError:
        return None
    offset = UTC
    if rest and rest not in {"Z"}:
        match = re.fullmatch(r"([+-])(\d{2})(\d{2})", rest)
        if match is None:
            return None
        delta = timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))
        offset = timezone(delta if match.group(1) == "+" else -delta)
    return parsed.replace(tzinfo=offset).astimezone(UTC)


def _certificate_summary(certificate: x509.Certificate) -> PdfCertificateSummary:
    public_key = certificate.public_key()
    if isinstance(public_key, rsa.RSAPublicKey):
        key_type = "RSA"
        key_size: int | str | None = public_key.key_size
    elif isinstance(public_key, ec.EllipticCurvePublicKey):
        key_type = "EC"
        key_size = public_key.curve.name
    elif isinstance(public_key, dsa.DSAPublicKey):
        key_type = "DSA"
        key_size = public_key.key_size
    elif isinstance(public_key, ed25519.Ed25519PublicKey):
        key_type, key_size = "Ed25519", 255
    elif isinstance(public_key, ed448.Ed448PublicKey):
        key_type, key_size = "Ed448", 448
    else:
        key_type, key_size = type(public_key).__name__, None

    self_signed = False
    if certificate.subject == certificate.issuer:
        try:
            parameters = certificate.signature_algorithm_parameters
            signature_hash = certificate.signature_hash_algorithm
            if isinstance(public_key, rsa.RSAPublicKey):
                public_key.verify(
                    certificate.signature,
                    certificate.tbs_certificate_bytes,
                    parameters,
                    signature_hash,
                )
            elif isinstance(public_key, dsa.DSAPublicKey):
                public_key.verify(
                    certificate.signature,
                    certificate.tbs_certificate_bytes,
                    signature_hash,
                )
            elif isinstance(
                public_key,
                (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey),
            ):
                public_key.verify(
                    certificate.signature, certificate.tbs_certificate_bytes
                )
            else:
                public_key.verify(
                    certificate.signature,
                    certificate.tbs_certificate_bytes,
                    parameters,
                )
            self_signed = True
        except (
            AttributeError,
            InvalidSignature,
            TypeError,
            UnsupportedAlgorithm,
            ValueError,
        ):
            self_signed = False
    now = datetime.now(UTC)
    return PdfCertificateSummary(
        subject=certificate.subject.rfc4514_string()[:1000],
        issuer=certificate.issuer.rfc4514_string()[:1000],
        serial_number=f"{certificate.serial_number:x}",
        sha256_fingerprint=certificate.fingerprint(hashes.SHA256()).hex(),
        not_valid_before=certificate.not_valid_before_utc.isoformat(),
        not_valid_after=certificate.not_valid_after_utc.isoformat(),
        expired_now=not (
            certificate.not_valid_before_utc <= now <= certificate.not_valid_after_utc
        ),
        self_signed=self_signed,
        key_type=key_type,
        key_size=key_size,
    )


def _load_certificates(node: _DerNode | None) -> tuple[x509.Certificate, ...]:
    if node is None:
        return ()
    certificates: list[x509.Certificate] = []
    entries = node.children
    if len(entries) > MAX_CMS_CERTIFICATES:
        raise _DerError("CMS contains too many certificates")
    for entry in entries:
        try:
            certificates.append(x509.load_der_x509_certificate(entry.raw))
        except ValueError:
            continue
    return tuple(certificates)


def _matching_certificate(
    signer_id: _DerNode, certificates: tuple[x509.Certificate, ...]
) -> x509.Certificate | None:
    if _is(signer_id, 0, 16):  # IssuerAndSerialNumber
        if len(signer_id.children) != 2:
            raise _DerError("Malformed CMS signer identifier")
        issuer_raw = signer_id.children[0].raw
        serial = _integer(signer_id.children[1])
        for certificate in certificates:
            if (
                certificate.issuer.public_bytes() == issuer_raw
                and certificate.serial_number == serial
            ):
                return certificate
        return None
    if _is(signer_id, 2, 0, constructed=False):  # SubjectKeyIdentifier
        wanted = signer_id.value
        for certificate in certificates:
            try:
                extension = certificate.extensions.get_extension_for_class(
                    x509.SubjectKeyIdentifier
                )
            except x509.ExtensionNotFound:
                continue
            if extension.value.digest == wanted:
                return certificate
        return None
    raise _DerError("Unsupported CMS signer identifier")


def _pss_padding(signature_algorithm: _DerNode, fallback_digest: str | None):
    hash_name = fallback_digest or "sha1"
    mgf_hash_name = hash_name
    salt_length = 20
    if len(signature_algorithm.children) > 1:
        params = signature_algorithm.children[1]
        if _is(params, 0, 16):
            for item in params.children:
                if not (_is(item, 2, item.tag, constructed=True) and item.children):
                    continue
                inner = item.children[0]
                if item.tag == 0:
                    hash_name = _digest_name_from_algorithm(inner) or hash_name
                elif item.tag == 1:
                    _require(inner, 0, 16, "RSA-PSS mask generation algorithm")
                    if not inner.children or _oid_string(inner.children[0]) != _OID_MGF1:
                        raise _DerError("Unsupported RSA-PSS mask generation function")
                    if len(inner.children) > 1:
                        mgf_hash_name = (
                            _digest_name_from_algorithm(inner.children[1])
                            or mgf_hash_name
                        )
                elif item.tag == 2:
                    salt_length = _integer(inner)
    hash_obj = _hash_algorithm(hash_name)
    mgf_hash = _hash_algorithm(mgf_hash_name)
    if hash_obj is None or mgf_hash is None or salt_length < 0:
        raise _DerError("Unsupported RSA-PSS parameters")
    return padding.PSS(mgf=padding.MGF1(mgf_hash), salt_length=salt_length), hash_obj


def _verify_cms_signature(
    *,
    certificate: x509.Certificate | None,
    signature_algorithm_node: _DerNode,
    signature: bytes,
    verification_data: bytes,
    digest_name: str | None,
) -> bool | None:
    if certificate is None:
        return None
    _require(signature_algorithm_node, 0, 16, "signature algorithm")
    if not signature_algorithm_node.children:
        raise _DerError("Malformed CMS signature algorithm")
    algorithm_oid = _oid_string(signature_algorithm_node.children[0])
    hash_obj = _hash_algorithm(digest_name)
    public_key = certificate.public_key()

    try:
        if isinstance(public_key, rsa.RSAPublicKey):
            if hash_obj is None:
                return None
            if algorithm_oid == _OID_RSA_PSS:
                pad, pss_hash = _pss_padding(
                    signature_algorithm_node, digest_name
                )
                public_key.verify(signature, verification_data, pad, pss_hash)
            else:
                public_key.verify(
                    signature,
                    verification_data,
                    padding.PKCS1v15(),
                    hash_obj,
                )
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            if hash_obj is None:
                return None
            public_key.verify(signature, verification_data, ec.ECDSA(hash_obj))
        elif isinstance(public_key, dsa.DSAPublicKey):
            if hash_obj is None:
                return None
            public_key.verify(signature, verification_data, hash_obj)
        elif isinstance(public_key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
            public_key.verify(signature, verification_data)
        else:
            return None
    except InvalidSignature:
        return False
    except Exception as exc:  # Unsupported key/algorithm details from provider.
        raise _DerError(
            f"Unsupported CMS signature verification: {type(exc).__name__}"
        ) from exc
    return True


def _verify_signer(
    signer: _DerNode,
    *,
    signed_bytes: bytes,
    encapsulated_content_type: str,
    certificates: tuple[x509.Certificate, ...],
) -> PdfSignerSummary:
    _require(signer, 0, 16, "signer info")
    if len(signer.children) < 5:
        raise _DerError("Malformed CMS signer info")

    certificate = _matching_certificate(signer.children[1], certificates)
    certificate_summary = (
        _certificate_summary(certificate) if certificate is not None else None
    )
    digest_name = _digest_name_from_algorithm(signer.children[2])
    index = 3
    signed_attrs: _DerNode | None = None
    if index < len(signer.children) and _is(
        signer.children[index], 2, 0, constructed=True
    ):
        signed_attrs = signer.children[index]
        index += 1
    if index + 1 >= len(signer.children):
        raise _DerError("Malformed CMS signer signature")
    signature_algorithm_node = signer.children[index]
    signature_node = signer.children[index + 1]
    _require(signature_node, 0, 4, "signature value")
    signature = signature_node.value
    digest_name_for_signature = digest_name

    digest_matches: bool | None = None
    signing_time: datetime | None = None
    details: list[str] = []

    if signed_attrs is not None:
        attributes: dict[str, _DerNode] = {}
        for attribute in signed_attrs.children:
            _require(attribute, 0, 16, "signed attribute")
            if len(attribute.children) != 2:
                raise _DerError("Malformed CMS signed attribute")
            oid = _oid_string(attribute.children[0])
            values = attribute.children[1]
            _require(values, 0, 17, "signed attribute values")
            if values.children:
                attributes[oid] = values.children[0]

        content_type_node = attributes.get(_OID_CONTENT_TYPE)
        if content_type_node is not None:
            content_type = _oid_string(content_type_node)
            if content_type != encapsulated_content_type:
                digest_matches = False
                details.append("CMS content-type attribute does not match")

        digest_node = attributes.get(_OID_MESSAGE_DIGEST)
        if digest_node is None or digest_name is None:
            details.append("CMS messageDigest attribute is missing or unsupported")
        else:
            _require(digest_node, 0, 4, "message digest")
            expected = hashlib.new(digest_name, signed_bytes).digest()
            computed_matches = expected == digest_node.value
            digest_matches = (
                computed_matches if digest_matches is not False else False
            )
            if not computed_matches:
                details.append("ByteRange digest does not match CMS messageDigest")

        time_node = attributes.get(_OID_SIGNING_TIME)
        if time_node is not None:
            signing_time = _parse_asn1_time(time_node)
        verification_data = (
            b"\x31" + _der_length_bytes(len(signed_attrs.value)) + signed_attrs.value
        )
    else:
        details.append("CMS has no signed attributes/messageDigest")
        verification_data = signed_bytes

    signature_algorithm = _signature_algorithm_label(
        signature_algorithm_node, digest_name_for_signature
    )
    signature_valid = _verify_cms_signature(
        certificate=certificate,
        signature_algorithm_node=signature_algorithm_node,
        signature=signature,
        verification_data=verification_data,
        digest_name=digest_name_for_signature,
    )
    if certificate is None:
        details.append("Signer certificate is not embedded in the CMS")
    if signature_valid is False:
        details.append("CMS signature does not verify with the signer certificate")

    valid_at_signing: bool | None = None
    if certificate is not None and signing_time is not None:
        valid_at_signing = (
            certificate.not_valid_before_utc
            <= signing_time
            <= certificate.not_valid_after_utc
        )
        if not valid_at_signing:
            details.append("Signer certificate was not valid at the CMS signing time")

    if not details:
        details.append("Digest and CMS signature verify; trust was not evaluated")

    return PdfSignerSummary(
        digest_algorithm=digest_name,
        digest_matches=digest_matches,
        signature_algorithm=signature_algorithm,
        signature_valid=signature_valid,
        signing_time=signing_time.isoformat() if signing_time else None,
        certificate_valid_at_signing=valid_at_signing,
        certificate=certificate_summary,
        status_detail="; ".join(details),
    )


def _signature_algorithm_label(node: _DerNode, digest_name: str | None) -> str:
    _require(node, 0, 16, "signature algorithm")
    if not node.children:
        return "unknown"
    oid = _oid_string(node.children[0])
    if oid == _OID_RSA_PSS:
        return f"RSA-PSS/{digest_name or 'unknown'}"
    known = {
        "1.2.840.113549.1.1.1": "RSA",
        "1.2.840.113549.1.1.4": "RSA/MD5",
        "1.2.840.113549.1.1.5": "RSA/SHA1",
        "1.2.840.113549.1.1.11": "RSA/SHA256",
        "1.2.840.113549.1.1.12": "RSA/SHA384",
        "1.2.840.113549.1.1.13": "RSA/SHA512",
        "1.2.840.113549.1.1.14": "RSA/SHA224",
        "1.2.840.10045.4.3.1": "ECDSA/SHA224",
        "1.2.840.10045.4.3.2": "ECDSA/SHA256",
        "1.2.840.10045.4.3.3": "ECDSA/SHA384",
        "1.2.840.10045.4.3.4": "ECDSA/SHA512",
        "1.3.101.112": "Ed25519",
        "1.3.101.113": "Ed448",
    }
    return known.get(oid, oid)



def _parse_cms(
    cms_bytes: bytes, signed_bytes: bytes
) -> tuple[tuple[PdfSignerSummary, ...], tuple[x509.Certificate, ...]]:
    root = _parse_der_root(cms_bytes)
    _require(root, 0, 16, "content info")
    if len(root.children) != 2:
        raise _DerError("Malformed CMS content info")
    if _oid_string(root.children[0]) != _OID_SIGNED_DATA:
        raise _DerError("CMS content is not SignedData")
    wrapper = _require(root.children[1], 2, 0, "signed data")
    if len(wrapper.children) != 1:
        raise _DerError("Malformed CMS signed-data wrapper")
    signed_data = _require(wrapper.children[0], 0, 16, "signed data")
    if len(signed_data.children) < 4:
        raise _DerError("Malformed CMS signed data")

    encapsulated = _require(signed_data.children[2], 0, 16, "encapsulated content")
    if not encapsulated.children:
        raise _DerError("CMS encapsulated content type is missing")
    content_type = _oid_string(encapsulated.children[0])
    if len(encapsulated.children) > 1:
        raise _DerError("PDF signature CMS is not detached")

    certificates_node: _DerNode | None = None
    signer_infos: _DerNode | None = None
    for child in signed_data.children[3:]:
        if _is(child, 2, 0, constructed=True):
            certificates_node = child
        elif _is(child, 0, 17, constructed=True):
            signer_infos = child
    if signer_infos is None:
        raise _DerError("CMS signer infos are missing")
    if not signer_infos.children or len(signer_infos.children) > MAX_CMS_SIGNERS:
        raise _DerError("CMS signer count is outside the inspection limit")

    certificates = _load_certificates(certificates_node)
    signers = tuple(
        _verify_signer(
            signer,
            signed_bytes=signed_bytes,
            encapsulated_content_type=content_type,
            certificates=certificates,
        )
        for signer in signer_infos.children
    )
    return signers, certificates


def _optional_text(value: Any) -> str | None:
    text = _as_text(value).strip()
    return text[:1000] if text else None


def _parse_pdf_date(value: Any) -> str | None:
    text = _as_text(value).strip()
    if not text:
        return None
    text = text.removeprefix("D:")
    match = re.fullmatch(
        r"(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
        r"(Z|[+-]\d{2}'?\d{0,2}'?)?",
        text,
    )
    if match is None:
        return text[:100]
    year = int(match.group(1))
    month = int(match.group(2) or 1)
    day = int(match.group(3) or 1)
    hour = int(match.group(4) or 0)
    minute = int(match.group(5) or 0)
    second = int(match.group(6) or 0)
    zone_text = match.group(7)
    zone = UTC
    if zone_text and zone_text != "Z":
        digits = re.sub(r"\D", "", zone_text[1:])
        zone_hours = int(digits[:2] or 0)
        zone_minutes = int(digits[2:4] or 0)
        delta = timedelta(hours=zone_hours, minutes=zone_minutes)
        zone = timezone(delta if zone_text[0] == "+" else -delta)
    try:
        return datetime(
            year, month, day, hour, minute, second, tzinfo=zone
        ).astimezone(UTC).isoformat()
    except ValueError:
        return text[:100]


def _parse_byte_range(
    raw: Any, file_size: int
) -> tuple[tuple[int, int, int, int], bytes, bool, int] | None:
    values = [_as_int(item) for item in _items(raw)]
    if len(values) != 4 or any(value is None for value in values):
        return None
    first_start, first_length, second_start, second_length = (
        int(value) for value in values if value is not None
    )
    first_end = first_start + first_length
    second_end = second_start + second_length
    if (
        first_start < 0
        or first_length < 0
        or second_length < 0
        or first_end > second_start
        or second_end > file_size
    ):
        return None
    return (
        (first_start, first_length, second_start, second_length),
        b"",
        first_start == 0 and second_end == file_size,
        file_size - second_end,
    )


def _contents_bytes(raw: Any) -> bytes | None:
    value = _object(raw)
    if value is None:
        return None
    if isinstance(value, bytes):
        return bytes(value)
    if isinstance(value, str):
        try:
            return value.encode("latin-1")
        except UnicodeEncodeError:
            return value.encode("utf-8", errors="replace")
    return None


def _docmdp_permission(signature: Any) -> int | None:
    for reference in _items(_get(signature, "/Reference")):
        if _as_name(_get(reference, "/TransformMethod")) == "DocMDP":
            permission = _as_int(
                _get(_get(reference, "/TransformParams"), "/P")
            )
            if permission in {1, 2, 3}:
                return permission
    return None


def _signature_status(
    signers: tuple[PdfSignerSummary, ...],
) -> tuple[str, str]:
    if any(
        signer.digest_matches is False
        or signer.signature_valid is False
        or signer.certificate_valid_at_signing is False
        for signer in signers
    ):
        return "invalid", "At least one CMS signer failed verification"
    if any(
        signer.digest_algorithm in {"md5", "sha1"} for signer in signers
    ):
        return (
            "unsupported",
            "The CMS uses a weak MD5/SHA-1 digest and is not reported as valid",
        )
    if any(
        signer.digest_matches is not True
        or signer.signature_valid is not True
        or signer.certificate is None
        for signer in signers
    ):
        return "unsupported", "CMS verification is incomplete or unsupported"
    return (
        "cryptographically_valid_untrusted",
        (
            "ByteRange digest and CMS signature verify; certificate trust and "
            "revocation were not evaluated"
        ),
    )


def _inspect_signature(
    *,
    field_name: str,
    page: int | None,
    widget_count: int,
    signature_value: Any,
    file_bytes: bytes,
) -> PdfSignatureSummary:
    signature = _object(signature_value)
    if signature is None:
        return PdfSignatureSummary(
            field_name=field_name,
            page=page,
            widget_count=widget_count,
            status="unsigned",
            status_detail="Signature field has no signature value",
            signed=False,
            signer_name=None,
            reason=None,
            location=None,
            contact_info=None,
            signature_date=None,
            filter_name=None,
            sub_filter=None,
            byte_range=None,
            covers_whole_file=None,
            unsigned_trailing_bytes=None,
            contents_size=None,
            cms_size=None,
            docmdp_permission=None,
            signers=(),
            certificates=(),
        )

    metadata = {
        "signer_name": _optional_text(_get(signature, "/Name")),
        "reason": _optional_text(_get(signature, "/Reason")),
        "location": _optional_text(_get(signature, "/Location")),
        "contact_info": _optional_text(_get(signature, "/ContactInfo")),
        "signature_date": _parse_pdf_date(_get(signature, "/M")),
        "filter_name": _as_name(_get(signature, "/Filter")) or None,
        "sub_filter": _as_name(_get(signature, "/SubFilter")) or None,
    }
    parsed_range = _parse_byte_range(
        _get(signature, "/ByteRange"), len(file_bytes)
    )
    contents = _contents_bytes(_get(signature, "/Contents"))
    contents_size = len(contents) if contents is not None else None
    permission = _docmdp_permission(signature)

    def result(
        status: str,
        detail: str,
        *,
        signers: tuple[PdfSignerSummary, ...] = (),
        certificates: tuple[PdfCertificateSummary, ...] = (),
        cms_size: int | None = None,
    ) -> PdfSignatureSummary:
        return PdfSignatureSummary(
            field_name=field_name,
            page=page,
            widget_count=widget_count,
            status=status,
            status_detail=detail,
            signed=True,
            byte_range=parsed_range[0] if parsed_range else None,
            covers_whole_file=parsed_range[2] if parsed_range else None,
            unsigned_trailing_bytes=parsed_range[3] if parsed_range else None,
            contents_size=contents_size,
            cms_size=cms_size,
            docmdp_permission=permission,
            signers=signers,
            certificates=certificates,
            **metadata,
        )

    if metadata["sub_filter"] not in _SUPPORTED_SUBFILTERS:
        return result(
            "unsupported",
            f"Unsupported signature SubFilter: {metadata['sub_filter'] or 'missing'}",
        )
    if parsed_range is None:
        return result("malformed", "Signature ByteRange is missing or invalid")
    if contents is None:
        return result("malformed", "Signature Contents are missing")
    if len(contents) > MAX_PDF_SIGNATURE_CONTENTS_BYTES:
        return result("unsupported", "Signature Contents exceed the inspection limit")

    first_start, first_length, second_start, second_length = parsed_range[0]
    signed_bytes = (
        file_bytes[first_start : first_start + first_length]
        + file_bytes[second_start : second_start + second_length]
    )
    try:
        root = _parse_der_root(contents)
        cms_size = root.end
        signers, certificates = _parse_cms(contents[:cms_size], signed_bytes)
    except _DerError as exc:
        return result("malformed", str(exc))
    except (
        ArithmeticError,
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        UnicodeError,
        UnsupportedAlgorithm,
        ValueError,
    ) as exc:  # Never leak untrusted bytes in exception text.
        return result(
            "unsupported",
            f"CMS inspection is unsupported for this structure: {type(exc).__name__}",
        )

    status, detail = _signature_status(signers)
    if parsed_range[3] and status == "cryptographically_valid_untrusted":
        detail += (
            f"; {parsed_range[3]} unsigned byte(s) follow the signed revision"
        )
    return result(
        status,
        detail,
        signers=signers,
        certificates=tuple(_certificate_summary(cert) for cert in certificates),
        cms_size=cms_size,
    )


def inspect_pdf_signatures(path: Path) -> PdfSignatureInspection:
    """Inspect PDF signature fields and verify detached CMS signatures."""

    if not path.is_file():
        raise ToolError(f"PDF file not found: {path.name}")
    if path.suffix.lower() != ".pdf":
        raise ToolError(f"Not a PDF file: {path.name}")
    if path.stat().st_size > MAX_PDF_SIGNATURE_FILE_BYTES:
        raise ToolError("PDF signature inspection is limited to 100 MB files")

    info = inspect_pdf(path)
    if info.encrypted or info.pages is None:
        raise ToolError(
            "Encrypted PDFs must be unprotected through the direct Phase 26 "
            "workflow before signature inspection."
        )
    if info.pages > MAX_PDF_FORM_PAGES:
        raise ToolError(
            f"PDF signature inspection is limited to {MAX_PDF_FORM_PAGES} pages"
        )

    form = inspect_pdf_form(path)
    form_fields = form.by_name()
    file_bytes = path.read_bytes()
    digest = hashlib.sha256(file_bytes).hexdigest()
    if digest != sha256_file(path) or digest != form.sha256:
        raise ToolError("PDF changed during signature inspection; retry")

    PdfReader, _PdfWriter = _load_pypdf()
    try:
        reader = PdfReader(str(path))
        raw_fields = _raw_fields(reader)
        locations = _widget_locations(reader)
        root = _object(getattr(reader, "root_object", None))
        if root is None:
            root = _get(getattr(reader, "trailer", {}), "/Root")
        acro_form = _get(root, "/AcroForm")
        signature_flags = _as_int(_get(acro_form, "/SigFlags"))
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(
            f"Could not inspect PDF signatures in '{path.name}': {type(exc).__name__}"
        ) from exc

    signature_fields = [
        raw for raw in raw_fields if _as_name(raw.values.get("/FT")) == "Sig"
    ]
    if len(signature_fields) > MAX_PDF_SIGNATURES:
        raise ToolError(
            f"PDF signature inspection is limited to {MAX_PDF_SIGNATURES} fields"
        )

    signatures: list[PdfSignatureSummary] = []
    for raw in signature_fields:
        form_field = form_fields.get(raw.name)
        page, widget_count = locations.get(raw.name, (None, 0))
        if form_field is not None:
            page = form_field.page
            widget_count = form_field.widget_count
        signatures.append(
            _inspect_signature(
                field_name=raw.name,
                page=page,
                widget_count=widget_count,
                signature_value=raw.values.get("/V"),
                file_bytes=file_bytes,
            )
        )

    return PdfSignatureInspection(
        path=path,
        pages=info.pages,
        signatures=tuple(signatures),
        signature_flags=signature_flags,
        size_bytes=len(file_bytes),
        sha256=digest,
    )


