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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed448, ed25519, padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID

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

_OID_DATA = "1.2.840.113549.1.7.1"
_OID_SIGNED_DATA = "1.2.840.113549.1.7.2"
_OID_CONTENT_TYPE = "1.2.840.113549.1.9.3"
_OID_MESSAGE_DIGEST = "1.2.840.113549.1.9.4"
_OID_SIGNING_TIME = "1.2.840.113549.1.9.5"
_OID_SIGNING_CERTIFICATE_V2 = "1.2.840.113549.1.9.16.2.47"
_OID_TIMESTAMP_TOKEN = "1.2.840.113549.1.9.16.2.14"
_OID_TST_INFO = "1.2.840.113549.1.9.16.1.4"
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
_SUPPORTED_SUBFILTERS = {
    "adbe.pkcs7.detached",
    "ETSI.CAdES.detached",
    "ETSI.RFC3161",
}


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
    # Retained only for in-process Phase 29 trust analysis. It is deliberately
    # excluded from summary(), repr() and equality so raw certificate bytes do
    # not reach CLI/GUI text, reports, tasks or logs.
    der_bytes: bytes = field(default=b"", repr=False, compare=False)

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
class PdfTimestampSummary:
    status: str
    status_detail: str
    generation_time: str | None
    policy_oid: str | None
    serial_number: str | None
    hash_algorithm: str | None
    message_imprint_matches: bool | None
    nonce: str | None
    token_size: int
    signer_certificate: PdfCertificateSummary | None
    certificates: tuple[PdfCertificateSummary, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "status_detail": self.status_detail,
            "generation_time": self.generation_time,
            "policy_oid": self.policy_oid,
            "serial_number": self.serial_number,
            "hash_algorithm": self.hash_algorithm,
            "message_imprint_matches": self.message_imprint_matches,
            "nonce": self.nonce,
            "token_size": self.token_size,
            "signer_certificate": (
                self.signer_certificate.summary()
                if self.signer_certificate is not None
                else None
            ),
            "certificates": [item.summary() for item in self.certificates],
            "trust_status": "not_evaluated",
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
    timestamp: PdfTimestampSummary | None = None
    claimed_signing_time: str | None = None
    claimed_signing_time_source: str | None = None
    cms_content_type: str | None = None
    signed_attribute_oids: tuple[str, ...] = ()
    signing_certificate_v2_present: bool = False
    signing_certificate_v2_valid: bool | None = None
    signing_certificate_v2_hash_algorithm: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "digest_algorithm": self.digest_algorithm,
            "digest_matches": self.digest_matches,
            "signature_algorithm": self.signature_algorithm,
            "signature_valid": self.signature_valid,
            "signing_time": self.signing_time,
            "claimed_signing_time": self.claimed_signing_time,
            "claimed_signing_time_source": self.claimed_signing_time_source,
            "cms_content_type": self.cms_content_type,
            "signed_attribute_oids": list(self.signed_attribute_oids),
            "signing_certificate_v2_present": (
                self.signing_certificate_v2_present
            ),
            "signing_certificate_v2_valid": self.signing_certificate_v2_valid,
            "signing_certificate_v2_hash_algorithm": (
                self.signing_certificate_v2_hash_algorithm
            ),
            "certificate_valid_at_signing": self.certificate_valid_at_signing,
            "certificate": (
                self.certificate.summary() if self.certificate is not None else None
            ),
            "status_detail": self.status_detail,
            "timestamp": self.timestamp.summary() if self.timestamp else None,
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
    document_timestamp: PdfTimestampSummary | None = None
    pades_profile_status: str = "not_evaluated"
    pades_profile_detail: str = "PAdES profile structure was not evaluated"

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
            "document_timestamp": (
                self.document_timestamp.summary()
                if self.document_timestamp is not None
                else None
            ),
            "pades_profile_status": self.pades_profile_status,
            "pades_profile_detail": self.pades_profile_detail,
            "pades_conformance_status": "not_externally_evaluated",
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
    if rest.startswith((".", ",")):
        fraction_match = re.match(r"[.,](\d+)", rest)
        if fraction_match is None:
            return None
        digits = fraction_match.group(1)[:6].ljust(6, "0")
        parsed = parsed.replace(microsecond=int(digits))
        rest = rest[fraction_match.end() :]
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
        der_bytes=certificate.public_bytes(serialization.Encoding.DER),
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


def _validate_signing_certificate_v2(
    node: _DerNode,
    certificate: x509.Certificate | None,
) -> tuple[bool | None, str | None, str]:
    """Validate one RFC 5035 SigningCertificateV2 signed-attribute value."""

    if certificate is None:
        return None, None, "signer certificate is unavailable"
    _require(node, 0, 16, "signing-certificate-v2")
    if len(node.children) not in {1, 2}:
        raise _DerError("Malformed ESS signing-certificate-v2")
    certificates = _require(
        node.children[0], 0, 16, "signing-certificate-v2 certificates"
    )
    if len(node.children) == 2:
        _require(node.children[1], 0, 16, "signing-certificate-v2 policies")
    if not certificates.children or len(certificates.children) > 20:
        raise _DerError("ESS signing-certificate-v2 certificate count is invalid")

    certificate_der = certificate.public_bytes(serialization.Encoding.DER)
    seen_algorithm: str | None = None
    for entry in certificates.children:
        _require(entry, 0, 16, "ESS certificate identifier v2")
        if not entry.children or len(entry.children) > 3:
            raise _DerError("Malformed ESS certificate identifier v2")
        index = 0
        algorithm_name = "sha256"
        if _is(entry.children[0], 0, 16, constructed=True):
            algorithm_name = _digest_name_from_algorithm(entry.children[0])
            index = 1
        if algorithm_name is not None:
            seen_algorithm = algorithm_name
        if index >= len(entry.children):
            raise _DerError("ESS certificate identifier v2 has no certificate hash")
        cert_hash = _require(
            entry.children[index], 0, 4, "ESS certificate hash"
        ).value
        issuer_serial = (
            entry.children[index + 1] if index + 1 < len(entry.children) else None
        )
        if index + 1 < len(entry.children) and index + 2 != len(entry.children):
            raise _DerError("Malformed ESS certificate identifier v2")
        if algorithm_name not in {"sha256", "sha384", "sha512"}:
            continue
        if hashlib.new(algorithm_name, certificate_der).digest() != cert_hash:
            continue
        if issuer_serial is not None:
            _require(issuer_serial, 0, 16, "ESS issuer serial")
            if len(issuer_serial.children) != 2:
                raise _DerError("Malformed ESS issuer serial")
            general_names = _require(
                issuer_serial.children[0], 0, 16, "ESS issuer general names"
            )
            if _integer(issuer_serial.children[1]) != certificate.serial_number:
                continue
            issuer_matches = any(
                _is(name, 2, 4, constructed=True)
                and len(name.children) == 1
                and name.children[0].raw == certificate.issuer.public_bytes()
                for name in general_names.children
            )
            if not issuer_matches:
                continue
        return True, algorithm_name, "ESS signing-certificate-v2 matches the signer"
    return (
        False,
        seen_algorithm,
        "ESS signing-certificate-v2 does not match the signer certificate",
    )


def _parse_iso_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _pades_bb_assessment(
    *,
    sub_filter: str | None,
    filter_name: str | None,
    signature_date: str | None,
    cert_entry_present: bool,
    status: str,
    signers: tuple[PdfSignerSummary, ...],
) -> tuple[str, str]:
    """Assess locally checkable ETSI B-B structure without claiming conformance."""

    if sub_filter != "ETSI.CAdES.detached":
        return "not_pades_subfilter", "Signature does not use ETSI.CAdES.detached"
    issues: list[str] = []
    if status != "cryptographically_valid_untrusted":
        issues.append("CMS signature is not cryptographically valid")
    if not filter_name:
        issues.append("Filter is missing")
    if _parse_iso_time(signature_date) is None:
        issues.append("claimed signing time /M is missing or unparseable")
    if cert_entry_present:
        issues.append("forbidden Cert entry is present")
    if len(signers) != 1:
        issues.append("CMS does not contain exactly one signer")
    else:
        signer = signers[0]
        expected_attributes = {
            _OID_CONTENT_TYPE: 1,
            _OID_MESSAGE_DIGEST: 1,
            _OID_SIGNING_CERTIFICATE_V2: 1,
        }
        actual_attributes: dict[str, int] = {}
        for oid in signer.signed_attribute_oids:
            actual_attributes[oid] = actual_attributes.get(oid, 0) + 1
        if actual_attributes != expected_attributes:
            issues.append(
                "signed attributes are not exactly content-type, "
                "message-digest and signing-certificate-v2"
            )
        if signer.signing_time is not None:
            issues.append("CMS signing-time attribute is present")
        if signer.cms_content_type != _OID_DATA:
            issues.append("CMS content type is not id-data")
        if signer.signing_certificate_v2_valid is not True:
            issues.append("ESS signing-certificate-v2 does not verify")
        if signer.claimed_signing_time_source != "pdf_signature_dictionary_m":
            issues.append("claimed signing time is not provided by /M")
        if signer.digest_algorithm not in {"sha256", "sha384", "sha512"}:
            issues.append("CMS digest is outside the supported B-B profile")
    if issues:
        return "bb_structure_incomplete", "; ".join(sorted(set(issues)))
    return (
        "bb_structure_verified",
        (
            "ETSI.CAdES.detached, /M claimed time, content-type, "
            "message-digest and ESS signing-certificate-v2 structure verify; "
            "external PAdES conformance was not evaluated"
        ),
    )


def _verify_signer(
    signer: _DerNode,
    *,
    signed_bytes: bytes,
    encapsulated_content_type: str,
    certificates: tuple[x509.Certificate, ...],
    parse_timestamp: bool = True,
    claimed_pdf_time: datetime | None = None,
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
    signed_attribute_oids: tuple[str, ...] = ()
    signing_certificate_v2_present = False
    signing_certificate_v2_valid: bool | None = None
    signing_certificate_v2_hash_algorithm: str | None = None

    if signed_attrs is not None:
        attributes: dict[str, _DerNode] = {}
        attribute_oids: list[str] = []
        for attribute in signed_attrs.children:
            _require(attribute, 0, 16, "signed attribute")
            if len(attribute.children) != 2:
                raise _DerError("Malformed CMS signed attribute")
            oid = _oid_string(attribute.children[0])
            attribute_oids.append(oid)
            values = attribute.children[1]
            _require(values, 0, 17, "signed attribute values")
            if values.children and oid not in attributes:
                attributes[oid] = values.children[0]
        signed_attribute_oids = tuple(attribute_oids)

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

        ess_node = attributes.get(_OID_SIGNING_CERTIFICATE_V2)
        signing_certificate_v2_present = ess_node is not None
        if ess_node is not None:
            (
                signing_certificate_v2_valid,
                signing_certificate_v2_hash_algorithm,
                ess_detail,
            ) = _validate_signing_certificate_v2(ess_node, certificate)
            if signing_certificate_v2_valid is not True:
                details.append(ess_detail)
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

    claimed_signing_time = signing_time or claimed_pdf_time
    claimed_signing_time_source = (
        "cms_signing_time"
        if signing_time is not None
        else "pdf_signature_dictionary_m"
        if claimed_pdf_time is not None
        else None
    )
    valid_at_signing: bool | None = None
    if certificate is not None and claimed_signing_time is not None:
        valid_at_signing = (
            certificate.not_valid_before_utc
            <= claimed_signing_time
            <= certificate.not_valid_after_utc
        )
        if not valid_at_signing:
            details.append(
                "Signer certificate was not valid at the claimed signing time"
            )

    timestamp = None
    if parse_timestamp and index + 2 < len(signer.children):
        unsigned_attrs = signer.children[index + 2]
        if _is(unsigned_attrs, 2, 1, constructed=True):
            timestamp = _timestamp_from_unsigned_attrs(unsigned_attrs, signature)

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
        timestamp=timestamp,
        claimed_signing_time=(
            claimed_signing_time.isoformat() if claimed_signing_time else None
        ),
        claimed_signing_time_source=claimed_signing_time_source,
        cms_content_type=encapsulated_content_type,
        signed_attribute_oids=signed_attribute_oids,
        signing_certificate_v2_present=signing_certificate_v2_present,
        signing_certificate_v2_valid=signing_certificate_v2_valid,
        signing_certificate_v2_hash_algorithm=(
            signing_certificate_v2_hash_algorithm
        ),
    )


def _malformed_timestamp(token_size: int, detail: str) -> PdfTimestampSummary:
    return PdfTimestampSummary(
        status="malformed",
        status_detail=detail,
        generation_time=None,
        policy_oid=None,
        serial_number=None,
        hash_algorithm=None,
        message_imprint_matches=None,
        nonce=None,
        token_size=token_size,
        signer_certificate=None,
        certificates=(),
    )


def _octet_bytes(node: _DerNode) -> bytes:
    _require(node, 0, 4, "octet string")
    if not node.constructed:
        return node.value
    return b"".join(_octet_bytes(child) for child in node.children)


def _timestamp_eku_error(certificate: x509.Certificate | None) -> str | None:
    if certificate is None:
        return "timestamp signer certificate is not embedded"
    try:
        extension = certificate.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage
        )
    except x509.ExtensionNotFound:
        return "timestamp signer certificate has no ExtendedKeyUsage"
    except x509.DuplicateExtension:
        return "timestamp signer certificate has duplicated ExtendedKeyUsage"
    if not extension.critical:
        return "timestamp signer ExtendedKeyUsage is not critical"
    if list(extension.value) != [ExtendedKeyUsageOID.TIME_STAMPING]:
        return "timestamp signer ExtendedKeyUsage is not exactly timeStamping"
    return None


def _parse_timestamp_token(
    token_bytes: bytes, *, message_bytes: bytes
) -> tuple[PdfTimestampSummary, tuple[x509.Certificate, ...]]:
    certificates: tuple[x509.Certificate, ...] = ()
    try:
        root = _parse_der_root(token_bytes)
        _require(root, 0, 16, "timestamp token")
        if len(root.children) != 2 or _oid_string(root.children[0]) != _OID_SIGNED_DATA:
            raise _DerError("Timestamp token is not CMS SignedData")
        wrapper = _require(root.children[1], 2, 0, "timestamp signed data")
        if len(wrapper.children) != 1:
            raise _DerError("Malformed timestamp signed-data wrapper")
        signed_data = _require(wrapper.children[0], 0, 16, "timestamp signed data")
        if len(signed_data.children) < 4:
            raise _DerError("Malformed timestamp signed data")
        encapsulated = _require(
            signed_data.children[2], 0, 16, "timestamp encapsulated content"
        )
        if len(encapsulated.children) != 2:
            raise _DerError("Timestamp token has no encapsulated TSTInfo")
        if _oid_string(encapsulated.children[0]) != _OID_TST_INFO:
            raise _DerError("Timestamp token content type is not TSTInfo")
        content_wrapper = _require(
            encapsulated.children[1], 2, 0, "timestamp encapsulated content"
        )
        if len(content_wrapper.children) != 1:
            raise _DerError("Malformed timestamp encapsulated content")
        tst_bytes = _octet_bytes(content_wrapper.children[0])

        certificates_node: _DerNode | None = None
        signer_infos: _DerNode | None = None
        for child in signed_data.children[3:]:
            if _is(child, 2, 0, constructed=True):
                certificates_node = child
            elif _is(child, 0, 17, constructed=True):
                signer_infos = child
        if signer_infos is None or len(signer_infos.children) != 1:
            raise _DerError("Timestamp token must contain exactly one signer")
        certificates = _load_certificates(certificates_node)
        token_signer = _verify_signer(
            signer_infos.children[0],
            signed_bytes=tst_bytes,
            encapsulated_content_type=_OID_TST_INFO,
            certificates=certificates,
            parse_timestamp=False,
        )

        tst = _parse_der_root(tst_bytes)
        _require(tst, 0, 16, "TSTInfo")
        if len(tst.children) < 5:
            raise _DerError("Malformed TSTInfo")
        version = _integer(tst.children[0])
        policy_oid = _oid_string(tst.children[1])
        imprint = _require(tst.children[2], 0, 16, "timestamp message imprint")
        if len(imprint.children) != 2:
            raise _DerError("Malformed timestamp message imprint")
        hash_algorithm = _digest_name_from_algorithm(imprint.children[0])
        imprint_digest = _octet_bytes(imprint.children[1])
        serial = _integer(tst.children[3])
        generation_time = _parse_asn1_time(tst.children[4])
        nonce: int | None = None
        for child in tst.children[5:]:
            if _is(child, 0, 2):
                nonce = _integer(child)
                break

        details: list[str] = []
        invalid = False
        unsupported = False
        if version != 1:
            invalid = True
            details.append("TSTInfo version is not 1")
        if generation_time is None:
            invalid = True
            details.append("TSTInfo generation time is malformed")
        if hash_algorithm is None:
            unsupported = True
            imprint_matches: bool | None = None
            details.append("timestamp message-imprint hash is unsupported")
        else:
            imprint_matches = (
                hashlib.new(hash_algorithm, message_bytes).digest() == imprint_digest
            )
            if not imprint_matches:
                invalid = True
                details.append("timestamp message imprint does not match")
            if hash_algorithm in {"md5", "sha1"}:
                unsupported = True
                details.append("timestamp uses a weak MD5/SHA-1 imprint hash")
        if token_signer.digest_matches is False or token_signer.signature_valid is False:
            invalid = True
            details.append("timestamp CMS digest or signature does not verify")
        if token_signer.digest_matches is not True or token_signer.signature_valid is not True:
            unsupported = True
            details.append("timestamp CMS verification is incomplete")
        if token_signer.digest_algorithm in {"md5", "sha1"}:
            unsupported = True
            details.append("timestamp CMS uses a weak MD5/SHA-1 digest")

        tsa_certificate: x509.Certificate | None = None
        if token_signer.certificate is not None:
            tsa_certificate = next(
                (
                    cert
                    for cert in certificates
                    if cert.fingerprint(hashes.SHA256()).hex()
                    == token_signer.certificate.sha256_fingerprint
                ),
                None,
            )
        if eku_error := _timestamp_eku_error(tsa_certificate):
            invalid = True
            details.append(eku_error)
        if (
            tsa_certificate is not None
            and generation_time is not None
            and not (
                tsa_certificate.not_valid_before_utc
                <= generation_time
                <= tsa_certificate.not_valid_after_utc
            )
        ):
            invalid = True
            details.append("timestamp signer certificate was not valid at generation time")

        if invalid:
            status = "invalid"
        elif unsupported:
            status = "unsupported"
        else:
            status = "verified_untrusted"
            details.append(
                "TSTInfo imprint, CMS signature and TSA EKU verify; TSA trust was not evaluated"
            )
        summary = PdfTimestampSummary(
            status=status,
            status_detail="; ".join(details),
            generation_time=generation_time.isoformat() if generation_time else None,
            policy_oid=policy_oid,
            serial_number=f"{serial:x}",
            hash_algorithm=hash_algorithm,
            message_imprint_matches=imprint_matches,
            nonce=f"{nonce:x}" if nonce is not None else None,
            token_size=len(token_bytes),
            signer_certificate=token_signer.certificate,
            certificates=tuple(_certificate_summary(cert) for cert in certificates),
        )
        return summary, certificates
    except _DerError as exc:
        return _malformed_timestamp(len(token_bytes), str(exc)), certificates
    except (
        ArithmeticError,
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        UnicodeError,
        UnsupportedAlgorithm,
        ValueError,
    ) as exc:
        return (
            _malformed_timestamp(
                len(token_bytes),
                f"Timestamp token inspection is unsupported: {type(exc).__name__}",
            ),
            certificates,
        )


def _timestamp_from_unsigned_attrs(
    unsigned_attrs: _DerNode, signature_value: bytes
) -> PdfTimestampSummary | None:
    candidates: list[PdfTimestampSummary] = []
    try:
        for attribute in unsigned_attrs.children:
            _require(attribute, 0, 16, "unsigned attribute")
            if len(attribute.children) != 2:
                raise _DerError("Malformed CMS unsigned attribute")
            if _oid_string(attribute.children[0]) != _OID_TIMESTAMP_TOKEN:
                continue
            values = _require(
                attribute.children[1], 0, 17, "unsigned attribute values"
            )
            for token in values.children[:3]:
                summary, _certificates = _parse_timestamp_token(
                    token.raw, message_bytes=signature_value
                )
                candidates.append(summary)
    except _DerError as exc:
        return _malformed_timestamp(0, str(exc))
    for candidate in candidates:
        if candidate.status == "verified_untrusted":
            return candidate
    return candidates[0] if candidates else None


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
    cms_bytes: bytes,
    signed_bytes: bytes,
    *,
    claimed_pdf_time: datetime | None = None,
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
            claimed_pdf_time=claimed_pdf_time,
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
        or signer.signing_certificate_v2_valid is False
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
            pades_profile_status="not_evaluated",
            pades_profile_detail="Unsigned signature field has no PAdES signature",
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
    cert_entry_present = _get(signature, "/Cert") is not None
    claimed_pdf_time = _parse_iso_time(metadata["signature_date"])
    default_pades_status = (
        "bb_structure_incomplete"
        if metadata["sub_filter"] == "ETSI.CAdES.detached"
        else "not_pades_subfilter"
    )
    default_pades_detail = (
        "PAdES B-B structure could not be verified"
        if metadata["sub_filter"] == "ETSI.CAdES.detached"
        else "Signature does not use ETSI.CAdES.detached"
    )

    def result(
        status: str,
        detail: str,
        *,
        signers: tuple[PdfSignerSummary, ...] = (),
        certificates: tuple[PdfCertificateSummary, ...] = (),
        cms_size: int | None = None,
        document_timestamp: PdfTimestampSummary | None = None,
        pades_profile_status: str | None = None,
        pades_profile_detail: str | None = None,
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
            document_timestamp=document_timestamp,
            pades_profile_status=pades_profile_status or default_pades_status,
            pades_profile_detail=pades_profile_detail or default_pades_detail,
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
        if metadata["sub_filter"] == "ETSI.RFC3161":
            document_timestamp, token_certificates = _parse_timestamp_token(
                contents[:cms_size], message_bytes=signed_bytes
            )
            token_status = {
                "verified_untrusted": "cryptographically_valid_untrusted",
                "invalid": "invalid",
                "malformed": "malformed",
                "unsupported": "unsupported",
            }.get(document_timestamp.status, "unsupported")
            return result(
                token_status,
                f"Document timestamp: {document_timestamp.status_detail}",
                certificates=tuple(
                    _certificate_summary(cert) for cert in token_certificates
                ),
                cms_size=cms_size,
                document_timestamp=document_timestamp,
            )
        signers, certificates = _parse_cms(
            contents[:cms_size],
            signed_bytes,
            claimed_pdf_time=claimed_pdf_time,
        )
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
    pades_status, pades_detail = _pades_bb_assessment(
        sub_filter=metadata["sub_filter"],
        filter_name=metadata["filter_name"],
        signature_date=metadata["signature_date"],
        cert_entry_present=cert_entry_present,
        status=status,
        signers=signers,
    )
    return result(
        status,
        detail,
        signers=signers,
        certificates=tuple(_certificate_summary(cert) for cert in certificates),
        cms_size=cms_size,
        pades_profile_status=pades_status,
        pades_profile_detail=pades_detail,
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


