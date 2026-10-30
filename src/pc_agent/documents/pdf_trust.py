"""Local trust-chain and revocation validation for PDF signatures.

Phase 29 stays direct-only and local by default. It starts from the Phase 27
CMS/ByteRange result, builds a bounded basic X.509 chain from each embedded
signer certificate to an explicitly supplied local trust anchor, and
evaluates explicitly supplied local CRL files. Phase 31 adds certificate-
embedded CRL/OCSP endpoints only when the direct user explicitly enables
online revocation.

Phase 32 adds a bounded certificate-policy graph (including policy
mappings and policy/any-policy inhibition counters) plus name-constraint
checks for the supported GeneralName forms. Phase 33 optionally merges CA
certificates from the Windows ROOT store or OpenSSL's default certificate
store, but only when the direct user explicitly enables system anchors. This
is still deliberately not a full WebPKI, Adobe, PAdES/LTV or
jurisdiction-specific legal validator.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes

from ..errors import ToolError
from .pdf_policy import (
    CertificatePolicyOptions,
    evaluate_chain_constraints,
)
from .pdf_revocation import OnlineRevocationClient
from .pdf_signatures import (
    PdfSignatureInspection,
    PdfSignatureSummary,
    PdfSignerSummary,
    inspect_pdf_signatures,
)
from .pdf_system_trust import SystemTrustStore, load_system_trust_store

MAX_TRUST_ANCHOR_FILES = 50
MAX_TRUST_ANCHOR_CERTIFICATES = 100
MAX_TRUST_ANCHOR_FILE_BYTES = 1_000_000
MAX_CRL_FILES = 50
MAX_CRL_FILE_BYTES = 5_000_000
MAX_CERTIFICATE_CHAIN_LENGTH = 8

_CERTIFICATE_SUFFIXES = {".cer", ".crt", ".der", ".pem"}
_CRL_SUFFIXES = {".crl", ".der", ".pem"}
_PEM_CERTIFICATE = re.compile(
    rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class PdfTrustAnchorSummary:
    source_name: str
    subject: str
    issuer: str
    serial_number: str
    sha256_fingerprint: str

    def summary(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "subject": self.subject,
            "issuer": self.issuer,
            "serial_number": self.serial_number,
            "sha256_fingerprint": self.sha256_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class PdfCrlSummary:
    source_name: str
    issuer: str
    sha256_fingerprint: str
    last_update: str | None
    next_update: str | None

    def summary(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "issuer": self.issuer,
            "sha256_fingerprint": self.sha256_fingerprint,
            "last_update": self.last_update,
            "next_update": self.next_update,
        }


@dataclass(frozen=True, slots=True)
class PdfChainCertificateSummary:
    subject: str
    issuer: str
    serial_number: str
    sha256_fingerprint: str
    role: str
    valid_at_verification: bool

    def summary(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "issuer": self.issuer,
            "serial_number": self.serial_number,
            "sha256_fingerprint": self.sha256_fingerprint,
            "role": self.role,
            "valid_at_verification": self.valid_at_verification,
        }


@dataclass(frozen=True, slots=True)
class PdfSignerTrustSummary:
    signer_subject: str | None
    signer_sha256_fingerprint: str | None
    verification_time: str
    verification_time_source: str
    chain_status: str
    revocation_status: str
    overall_status: str
    status_detail: str
    chain: tuple[PdfChainCertificateSummary, ...]
    timestamp_status: str | None = None
    timestamp_chain_status: str | None = None
    timestamp_revocation_status: str | None = None
    timestamp_time: str | None = None
    timestamp_trusted: bool = False
    timestamp_chain: tuple[PdfChainCertificateSummary, ...] = ()
    policy_status: str = "not_evaluated"
    policy_detail: str = ""
    policy_oids: tuple[str, ...] = ()
    name_constraints_status: str = "not_evaluated"
    name_constraints_detail: str = ""
    timestamp_policy_status: str | None = None
    timestamp_policy_oids: tuple[str, ...] = ()
    timestamp_name_constraints_status: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "signer_subject": self.signer_subject,
            "signer_sha256_fingerprint": self.signer_sha256_fingerprint,
            "verification_time": self.verification_time,
            "verification_time_source": self.verification_time_source,
            "chain_status": self.chain_status,
            "revocation_status": self.revocation_status,
            "overall_status": self.overall_status,
            "status_detail": self.status_detail,
            "chain": [item.summary() for item in self.chain],
            "policy_status": self.policy_status,
            "policy_detail": self.policy_detail,
            "policy_oids": list(self.policy_oids),
            "name_constraints_status": self.name_constraints_status,
            "name_constraints_detail": self.name_constraints_detail,
            "timestamp_status": self.timestamp_status,
            "timestamp_chain_status": self.timestamp_chain_status,
            "timestamp_revocation_status": self.timestamp_revocation_status,
            "timestamp_time": self.timestamp_time,
            "timestamp_trusted": self.timestamp_trusted,
            "timestamp_chain": [item.summary() for item in self.timestamp_chain],
            "timestamp_policy_status": self.timestamp_policy_status,
            "timestamp_policy_oids": list(self.timestamp_policy_oids),
            "timestamp_name_constraints_status": (
                self.timestamp_name_constraints_status
            ),
        }


@dataclass(frozen=True, slots=True)
class PdfSignatureTrustSummary:
    field_name: str
    page: int | None
    cryptographic_status: str
    cryptographic_detail: str
    chain_status: str
    revocation_status: str
    overall_status: str
    status_detail: str
    signers: tuple[PdfSignerTrustSummary, ...]
    policy_status: str = "not_evaluated"
    name_constraints_status: str = "not_evaluated"
    policy_oids: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {
            "field_name": self.field_name,
            "page": self.page,
            "cryptographic_status": self.cryptographic_status,
            "cryptographic_detail": self.cryptographic_detail,
            "chain_status": self.chain_status,
            "revocation_status": self.revocation_status,
            "policy_status": self.policy_status,
            "name_constraints_status": self.name_constraints_status,
            "policy_oids": list(self.policy_oids),
            "overall_status": self.overall_status,
            "status_detail": self.status_detail,
            "signers": [item.summary() for item in self.signers],
        }


@dataclass(frozen=True, slots=True)
class PdfTrustValidation:
    inspection: PdfSignatureInspection
    trust_anchors: tuple[PdfTrustAnchorSummary, ...]
    crls: tuple[PdfCrlSummary, ...]
    signatures: tuple[PdfSignatureTrustSummary, ...]
    timestamp_trust_anchors: tuple[PdfTrustAnchorSummary, ...] = ()
    online_revocation_enabled: bool = False
    revocation_network_used: bool = False
    online_crl_count: int = 0
    ocsp_response_count: int = 0
    revocation_network_errors: tuple[str, ...] = ()
    required_policy_oids: tuple[str, ...] = ()
    timestamp_required_policy_oids: tuple[str, ...] = ()
    explicit_policy_required: bool = False
    inhibit_any_policy: bool = False
    inhibit_policy_mapping: bool = False
    system_trust_enabled: bool = False
    system_trust_anchor_count: int = 0
    timestamp_system_trust_enabled: bool = False
    timestamp_system_trust_anchor_count: int = 0
    system_trust_source: str | None = None
    system_trust_skipped_count: int = 0
    effective_trust_anchor_count: int = 0
    effective_timestamp_trust_anchor_count: int = 0

    @property
    def trusted_valid_count(self) -> int:
        return sum(1 for item in self.signatures if item.overall_status == "trusted_valid")

    @property
    def trusted_chain_revocation_unknown_count(self) -> int:
        return sum(
            1
            for item in self.signatures
            if item.overall_status == "trusted_chain_revocation_unknown"
        )

    @property
    def revoked_count(self) -> int:
        return sum(1 for item in self.signatures if item.revocation_status == "revoked")

    @property
    def untrusted_valid_count(self) -> int:
        return sum(
            1
            for item in self.signatures
            if item.cryptographic_status == "cryptographically_valid_untrusted"
            and item.chain_status != "trusted"
        )

    @property
    def policy_invalid_count(self) -> int:
        return sum(1 for item in self.signatures if item.policy_status == "invalid")

    @property
    def name_constraints_invalid_count(self) -> int:
        return sum(
            1
            for item in self.signatures
            if item.name_constraints_status == "invalid"
        )

    def summary(self) -> dict[str, Any]:
        effective_signer_anchors = (
            self.effective_trust_anchor_count or len(self.trust_anchors)
        )
        effective_timestamp_anchors = (
            self.effective_timestamp_trust_anchor_count
            or len(self.timestamp_trust_anchors)
        )
        return {
            "pages": self.inspection.pages,
            "size_bytes": self.inspection.size_bytes,
            "sha256": self.inspection.sha256,
            "signature_count": self.inspection.signature_count,
            "signed_count": self.inspection.signed_count,
            "cryptographically_valid_count": self.inspection.valid_count,
            "cryptographically_invalid_count": self.inspection.invalid_count,
            "unsupported_count": self.inspection.unsupported_count,
            "unsigned_count": self.inspection.unsigned_count,
            "trusted_valid_count": self.trusted_valid_count,
            "trusted_chain_revocation_unknown_count": (
                self.trusted_chain_revocation_unknown_count
            ),
            "revoked_count": self.revoked_count,
            "untrusted_valid_count": self.untrusted_valid_count,
            "trust_anchor_count": effective_signer_anchors,
            "local_trust_anchor_count": len(self.trust_anchors),
            "effective_trust_anchor_count": effective_signer_anchors,
            "trust_anchors": [item.summary() for item in self.trust_anchors],
            "timestamp_trust_anchor_count": effective_timestamp_anchors,
            "local_timestamp_trust_anchor_count": len(
                self.timestamp_trust_anchors
            ),
            "effective_timestamp_trust_anchor_count": (
                effective_timestamp_anchors
            ),
            "timestamp_trust_anchors": [
                item.summary() for item in self.timestamp_trust_anchors
            ],
            "trusted_timestamp_count": sum(
                1
                for signature in self.signatures
                for signer in signature.signers
                if signer.timestamp_trusted
            ),
            "crl_count": len(self.crls),
            "crls": [item.summary() for item in self.crls],
            "signatures": [item.summary() for item in self.signatures],
            "online_revocation_enabled": self.online_revocation_enabled,
            "network_used": self.revocation_network_used,
            "revocation_network_used": self.revocation_network_used,
            "online_crl_count": self.online_crl_count,
            "ocsp_response_count": self.ocsp_response_count,
            "revocation_network_errors": list(self.revocation_network_errors),
            "ocsp_status": (
                "evaluated" if self.ocsp_response_count else "not_evaluated"
            ),
            "policy_invalid_count": self.policy_invalid_count,
            "name_constraints_invalid_count": self.name_constraints_invalid_count,
            "required_policy_oids": list(self.required_policy_oids),
            "timestamp_required_policy_oids": list(
                self.timestamp_required_policy_oids
            ),
            "explicit_policy_required": self.explicit_policy_required,
            "inhibit_any_policy": self.inhibit_any_policy,
            "inhibit_policy_mapping": self.inhibit_policy_mapping,
            "system_trust_enabled": self.system_trust_enabled,
            "system_trust_anchor_count": self.system_trust_anchor_count,
            "timestamp_system_trust_enabled": (
                self.timestamp_system_trust_enabled
            ),
            "timestamp_system_trust_anchor_count": (
                self.timestamp_system_trust_anchor_count
            ),
            "system_trust_source": self.system_trust_source,
            "system_trust_skipped_count": self.system_trust_skipped_count,
            "adobe_trust_status": "not_evaluated",
            "policy_status": (
                "bounded_rfc5280_subset_policies_and_name_constraints"
            ),
        }


def _fingerprint(certificate: x509.Certificate) -> str:
    return certificate.fingerprint(hashes.SHA256()).hex()


def _name_bytes(name: x509.Name) -> bytes:
    return name.public_bytes()


def _iter_input_files(
    paths: Sequence[Path],
    *,
    suffixes: set[str],
    label: str,
    maximum: int,
) -> tuple[Path, ...]:
    files: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        candidates: Iterable[Path]
        if path.is_dir():
            candidates = sorted(
                item
                for item in path.iterdir()
                if item.is_file() and item.suffix.lower() in suffixes
            )
        elif path.is_file():
            if path.suffix.lower() not in suffixes:
                allowed = ", ".join(sorted(suffixes))
                raise ToolError(f"{label} file must use one of: {allowed}: {path.name}")
            candidates = (path,)
        else:
            raise ToolError(f"{label} file or folder not found: {path.name}")
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen:
                files.append(candidate)
                seen.add(resolved)
    if len(files) > maximum:
        raise ToolError(f"Too many {label} files; the limit is {maximum}.")
    return tuple(files)


def _read_bounded(path: Path, *, maximum: int, label: str) -> bytes:
    if path.stat().st_size > maximum:
        raise ToolError(f"{label} file is too large: {path.name}")
    return path.read_bytes()


def _load_anchor_certificates(
    paths: Sequence[Path],
) -> tuple[tuple[x509.Certificate, ...], tuple[PdfTrustAnchorSummary, ...]]:
    files = _iter_input_files(
        paths,
        suffixes=_CERTIFICATE_SUFFIXES,
        label="Trust anchor",
        maximum=MAX_TRUST_ANCHOR_FILES,
    )
    certificates: list[x509.Certificate] = []
    summaries: list[PdfTrustAnchorSummary] = []
    seen: set[str] = set()
    for path in files:
        data = _read_bounded(
            path, maximum=MAX_TRUST_ANCHOR_FILE_BYTES, label="Trust anchor"
        )
        loaded: list[x509.Certificate] = []
        blocks = _PEM_CERTIFICATE.findall(data)
        try:
            if blocks:
                loaded = [x509.load_pem_x509_certificate(block) for block in blocks]
            else:
                loaded = [x509.load_der_x509_certificate(data)]
        except ValueError as exc:
            raise ToolError(f"Could not parse trust-anchor certificate: {path.name}") from exc
        for certificate in loaded:
            fingerprint = _fingerprint(certificate)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            certificates.append(certificate)
            summaries.append(
                PdfTrustAnchorSummary(
                    source_name=path.name,
                    subject=certificate.subject.rfc4514_string()[:1000],
                    issuer=certificate.issuer.rfc4514_string()[:1000],
                    serial_number=f"{certificate.serial_number:x}",
                    sha256_fingerprint=fingerprint,
                )
            )
            if len(certificates) > MAX_TRUST_ANCHOR_CERTIFICATES:
                raise ToolError(
                    f"Too many trust-anchor certificates; the limit is "
                    f"{MAX_TRUST_ANCHOR_CERTIFICATES}."
                )
    return tuple(certificates), tuple(summaries)


def _merge_anchor_certificates(
    local: Sequence[x509.Certificate],
    system_store: SystemTrustStore | None,
) -> tuple[tuple[x509.Certificate, ...], int]:
    merged = {_fingerprint(item): item for item in local}
    added = 0
    if system_store is not None:
        for certificate in system_store.certificates:
            fingerprint = _fingerprint(certificate)
            if fingerprint not in merged:
                merged[fingerprint] = certificate
                added += 1
    return tuple(merged.values()), added


def _load_crls(
    paths: Sequence[Path],
) -> tuple[tuple[x509.CertificateRevocationList, ...], tuple[PdfCrlSummary, ...]]:
    files = _iter_input_files(
        paths,
        suffixes=_CRL_SUFFIXES,
        label="CRL",
        maximum=MAX_CRL_FILES,
    )
    crls: list[x509.CertificateRevocationList] = []
    summaries: list[PdfCrlSummary] = []
    seen: set[str] = set()
    for path in files:
        data = _read_bounded(path, maximum=MAX_CRL_FILE_BYTES, label="CRL")
        try:
            crl = (
                x509.load_pem_x509_crl(data)
                if b"-----BEGIN X509 CRL-----" in data
                else x509.load_der_x509_crl(data)
            )
        except ValueError as exc:
            raise ToolError(f"Could not parse CRL file: {path.name}") from exc
        fingerprint = crl.fingerprint(hashes.SHA256()).hex()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        crls.append(crl)
        last_update = getattr(crl, "last_update_utc", None)
        next_update = getattr(crl, "next_update_utc", None)
        summaries.append(
            PdfCrlSummary(
                source_name=path.name,
                issuer=crl.issuer.rfc4514_string()[:1000],
                sha256_fingerprint=fingerprint,
                last_update=last_update.isoformat() if last_update else None,
                next_update=next_update.isoformat() if next_update else None,
            )
        )
    return tuple(crls), tuple(summaries)



class _ChainProblem(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _ChainOutcome:
    status: str
    detail: str
    chain: tuple[x509.Certificate, ...]


def _extension(certificate: x509.Certificate, extension_type: Any) -> Any | None:
    try:
        return certificate.extensions.get_extension_for_class(extension_type).value
    except x509.ExtensionNotFound:
        return None
    except x509.DuplicateExtension as exc:
        raise _ChainProblem("certificate contains a duplicated extension") from exc


def _valid_at(certificate: x509.Certificate, when: datetime) -> bool:
    return certificate.not_valid_before_utc <= when <= certificate.not_valid_after_utc


def _validity_error(certificate: x509.Certificate, when: datetime) -> str | None:
    if when < certificate.not_valid_before_utc:
        return f"certificate for {certificate.subject.rfc4514_string()} was not yet valid"
    if when > certificate.not_valid_after_utc:
        return f"certificate for {certificate.subject.rfc4514_string()} was expired"
    return None


def _verify_issued_by(
    certificate: x509.Certificate, issuer: x509.Certificate
) -> bool:
    try:
        certificate.verify_directly_issued_by(issuer)
    except (
        InvalidSignature,
        TypeError,
        UnsupportedAlgorithm,
        ValueError,
    ):
        return False
    return True


def _check_signer_usage(certificate: x509.Certificate) -> str | None:
    usage = _extension(certificate, x509.KeyUsage)
    if usage is None:
        return None
    if usage.digital_signature or usage.content_commitment:
        return None
    return "signer certificate KeyUsage does not allow digital signatures"


def _check_ca_usage(
    certificate: x509.Certificate,
    chain_below: tuple[x509.Certificate, ...],
) -> str | None:
    constraints = _extension(certificate, x509.BasicConstraints)
    if constraints is None or not constraints.ca:
        return "issuer certificate is not marked as a certification authority"
    if constraints.path_length is not None:
        intermediate_count = 0
        for below in chain_below[1:]:
            below_constraints = _extension(below, x509.BasicConstraints)
            if below_constraints is not None and below_constraints.ca:
                intermediate_count += 1
        if intermediate_count > constraints.path_length:
            return "issuer certificate BasicConstraints path length would be exceeded"
    usage = _extension(certificate, x509.KeyUsage)
    if usage is not None and not usage.key_cert_sign:
        return "issuer certificate KeyUsage does not allow certificate signing"
    return None


def _build_chain(
    leaf: x509.Certificate,
    pool: tuple[x509.Certificate, ...],
    anchors: dict[str, x509.Certificate],
    *,
    verification_time: datetime,
) -> _ChainOutcome:
    usage_error = _check_signer_usage(leaf)
    if usage_error:
        return _ChainOutcome("invalid", usage_error, (leaf,))

    by_issuer_subject: dict[bytes, list[x509.Certificate]] = {}
    unique: dict[str, x509.Certificate] = {}
    for certificate in (*anchors.values(), *pool):
        unique.setdefault(_fingerprint(certificate), certificate)
    for certificate in unique.values():
        by_issuer_subject.setdefault(_name_bytes(certificate.subject), []).append(
            certificate
        )
    for candidates in by_issuer_subject.values():
        candidates.sort(
            key=lambda item: (
                _fingerprint(item) not in anchors,
                item.subject.rfc4514_string(),
                _fingerprint(item),
            )
        )

    ranks = {"trusted": 0, "incomplete": 1, "untrusted": 2, "invalid": 3}

    def search(
        current: x509.Certificate,
        chain: tuple[x509.Certificate, ...],
        visited: frozenset[str],
    ) -> _ChainOutcome:
        if error := _validity_error(current, verification_time):
            return _ChainOutcome("invalid", error, chain)

        current_fingerprint = _fingerprint(current)
        if current_fingerprint in anchors:
            if _name_bytes(current.subject) == _name_bytes(current.issuer) and not (
                _verify_issued_by(current, current)
            ):
                return _ChainOutcome(
                    "invalid",
                    "the trusted self-signed certificate signature does not verify",
                    chain,
                )
            return _ChainOutcome(
                "trusted",
                "the chain ends at an explicitly supplied local trust anchor",
                chain,
            )

        if _name_bytes(current.subject) == _name_bytes(current.issuer):
            if _verify_issued_by(current, current):
                return _ChainOutcome(
                    "untrusted",
                    "the chain ends at a self-signed certificate that was not supplied as a trust anchor",
                    chain,
                )
            return _ChainOutcome(
                "invalid",
                "the self-signed certificate signature does not verify",
                chain,
            )

        if len(chain) >= MAX_CERTIFICATE_CHAIN_LENGTH:
            return _ChainOutcome(
                "invalid",
                f"certificate chain exceeds the {MAX_CERTIFICATE_CHAIN_LENGTH}-certificate limit",
                chain,
            )

        candidates = [
            candidate
            for candidate in by_issuer_subject.get(_name_bytes(current.issuer), [])
            if _fingerprint(candidate) not in visited
        ]
        if not candidates:
            return _ChainOutcome(
                "incomplete",
                "no embedded or supplied issuer certificate completes the chain",
                chain,
            )

        outcomes: list[_ChainOutcome] = []
        for candidate in candidates:
            candidate_chain = (*chain, candidate)
            ca_error = _check_ca_usage(candidate, chain)
            if ca_error:
                outcomes.append(_ChainOutcome("invalid", ca_error, candidate_chain))
                continue
            if not _verify_issued_by(current, candidate):
                outcomes.append(
                    _ChainOutcome(
                        "invalid",
                        "an issuer certificate signature does not verify",
                        candidate_chain,
                    )
                )
                continue
            outcomes.append(
                search(candidate, candidate_chain, visited | {_fingerprint(candidate)})
            )
        return min(outcomes, key=lambda item: (ranks[item.status], len(item.chain)))

    try:
        return search(leaf, (leaf,), frozenset({_fingerprint(leaf)}))
    except _ChainProblem as exc:
        return _ChainOutcome("invalid", str(exc), (leaf,))


def _crl_status_for_certificate(
    certificate: x509.Certificate,
    issuer_candidates: tuple[x509.Certificate, ...],
    crls: tuple[x509.CertificateRevocationList, ...],
    *,
    verification_time: datetime,
    now: datetime,
    online_client: OnlineRevocationClient | None = None,
) -> tuple[str, str]:
    matching_crl_seen = False
    stale_crl_seen = False
    for issuer in issuer_candidates:
        if _name_bytes(issuer.subject) != _name_bytes(certificate.issuer):
            continue
        if not _verify_issued_by(certificate, issuer):
            continue
        for crl in crls:
            if _name_bytes(crl.issuer) != _name_bytes(issuer.subject):
                continue
            usage = _extension(issuer, x509.KeyUsage)
            if usage is not None and not usage.crl_sign:
                continue
            try:
                if not crl.is_signature_valid(issuer.public_key()):
                    continue
            except (InvalidSignature, TypeError, UnsupportedAlgorithm, ValueError):
                continue
            matching_crl_seen = True
            next_update = getattr(crl, "next_update_utc", None)
            if next_update is not None and next_update < now:
                stale_crl_seen = True
                continue
            revoked = crl.get_revoked_certificate_by_serial_number(
                certificate.serial_number
            )
            if revoked is None:
                return "not_revoked", "a fresh signed CRL does not list the certificate"
            if revoked.revocation_date_utc <= verification_time:
                return (
                    "revoked",
                    "a fresh signed CRL lists the certificate as revoked on or before the signing time",
                )
            return (
                "revoked_after_signing",
                "a fresh signed CRL lists a revocation date after the signing time",
            )
    if online_client is not None and online_client.enabled:
        fetched_crls = online_client.crls_for_certificate(certificate)
        if fetched_crls:
            online_result = _crl_status_for_certificate(
                certificate,
                issuer_candidates,
                fetched_crls,
                verification_time=verification_time,
                now=now,
            )
            if online_result[0] != "unknown":
                return online_result
        ocsp_detail: str | None = None
        for issuer in issuer_candidates:
            if _name_bytes(issuer.subject) != _name_bytes(certificate.issuer):
                continue
            if not _verify_issued_by(certificate, issuer):
                continue
            ocsp_result = online_client.ocsp_status(
                certificate,
                issuer,
                verification_time=verification_time,
                now=now,
            )
            if ocsp_result[0] != "unknown":
                return ocsp_result
            ocsp_detail = ocsp_result[1]
        if ocsp_detail is not None and not stale_crl_seen:
            return "unknown", ocsp_detail

    if stale_crl_seen and not matching_crl_seen:
        return "unknown", "the matching signed CRL is stale"
    if stale_crl_seen:
        return "unknown", "all matching signed CRLs are stale"
    return "unknown", "no fresh signed CRL for the certificate issuer was supplied or fetched"


def _evaluate_revocation(
    chain: tuple[x509.Certificate, ...],
    *,
    anchors: dict[str, x509.Certificate],
    crls: tuple[x509.CertificateRevocationList, ...],
    pool: tuple[x509.Certificate, ...],
    verification_time: datetime,
    now: datetime,
    online_client: OnlineRevocationClient | None = None,
) -> tuple[str, str]:
    checkable: list[tuple[x509.Certificate, tuple[x509.Certificate, ...]]] = []
    for index, certificate in enumerate(chain):
        if index == len(chain) - 1 and _fingerprint(certificate) in anchors:
            continue
        if (
            len(chain) == 1
            and _fingerprint(certificate) in anchors
            and _name_bytes(certificate.subject) == _name_bytes(certificate.issuer)
        ):
            continue
        candidates: list[x509.Certificate] = []
        if index + 1 < len(chain):
            candidates.append(chain[index + 1])
        candidates.extend(
            item
            for item in (*pool, *anchors.values())
            if _name_bytes(item.subject) == _name_bytes(certificate.issuer)
        )
        unique = {_fingerprint(item): item for item in candidates}
        checkable.append((certificate, tuple(unique.values())))

    if not checkable:
        return "not_applicable", "the directly trusted certificate needs no issuer CRL check"

    saw_after_signing = False
    details: list[str] = []
    try:
        for certificate, issuer_candidates in checkable:
            status, detail = _crl_status_for_certificate(
                certificate,
                issuer_candidates,
                crls,
                verification_time=verification_time,
                now=now,
                online_client=online_client,
            )
            details.append(detail)
            if status == "revoked":
                return status, detail
            if status == "revoked_after_signing":
                saw_after_signing = True
                continue
            if status != "not_revoked":
                return "unknown", detail
    except _ChainProblem as exc:
        return "unknown", str(exc)

    if saw_after_signing:
        return (
            "revoked_after_signing",
            "the certificate was not revoked at the CMS signing time, but current revocation evidence lists a later revocation",
        )
    return "not_revoked", "verified revocation evidence covers every non-anchor certificate"


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


def _chain_summaries(
    chain: tuple[x509.Certificate, ...],
    *,
    chain_status: str,
    verification_time: datetime,
) -> tuple[PdfChainCertificateSummary, ...]:
    summaries: list[PdfChainCertificateSummary] = []
    for index, certificate in enumerate(chain):
        if index == 0:
            role = "signer"
        elif index == len(chain) - 1 and chain_status == "trusted":
            role = "trust_anchor"
        else:
            role = "intermediate_or_root"
        summaries.append(
            PdfChainCertificateSummary(
                subject=certificate.subject.rfc4514_string()[:1000],
                issuer=certificate.issuer.rfc4514_string()[:1000],
                serial_number=f"{certificate.serial_number:x}",
                sha256_fingerprint=_fingerprint(certificate),
                role=role,
                valid_at_verification=_valid_at(certificate, verification_time),
            )
        )
    return tuple(summaries)



def _load_summary_certificates(
    summaries: Sequence[Any],
) -> dict[str, x509.Certificate]:
    loaded: dict[str, x509.Certificate] = {}
    for summary in summaries:
        if not summary.der_bytes:
            continue
        try:
            certificate = x509.load_der_x509_certificate(summary.der_bytes)
        except ValueError:
            continue
        loaded[_fingerprint(certificate)] = certificate
    return loaded


def _load_embedded_certificates(
    signature: PdfSignatureSummary,
) -> dict[str, x509.Certificate]:
    return _load_summary_certificates(signature.certificates)


def _signer_overall(
    chain_status: str,
    revocation_status: str,
    *,
    policy_status: str = "valid",
    name_constraints_status: str = "valid",
) -> str:
    if revocation_status == "revoked":
        return (
            "trusted_chain_revoked"
            if chain_status == "trusted"
            else "cryptographically_valid_untrusted_revoked"
        )
    if chain_status == "trusted" and policy_status == "invalid":
        return "trusted_chain_policy_invalid"
    if chain_status == "trusted" and name_constraints_status == "invalid":
        return "trusted_chain_name_constraints_invalid"
    if chain_status == "trusted" and revocation_status in {
        "not_revoked",
        "not_applicable",
        "revoked_after_signing",
    }:
        return "trusted_valid"
    if chain_status == "trusted":
        return "trusted_chain_revocation_unknown"
    return "cryptographically_valid_untrusted"


def _evaluate_timestamp(
    signer: PdfSignerSummary,
    *,
    timestamp_anchors: dict[str, x509.Certificate],
    crls: tuple[x509.CertificateRevocationList, ...],
    now: datetime,
    online_client: OnlineRevocationClient | None = None,
    policy_options: CertificatePolicyOptions | None = None,
) -> tuple[str | None, str | None, str | None, datetime | None, bool, tuple[PdfChainCertificateSummary, ...], str, str | None, tuple[str, ...], str | None]:
    timestamp = signer.timestamp
    if timestamp is None:
        return None, None, None, None, False, (), "", None, (), None
    generation_time = _parse_time(timestamp.generation_time)
    if timestamp.status != "verified_untrusted" or generation_time is None:
        return (
            timestamp.status,
            "not_evaluated",
            "not_evaluated",
            generation_time,
            False,
            (),
            "RFC 3161 timestamp token did not pass cryptographic verification",
            "not_evaluated",
            (),
            "not_evaluated",
        )
    if timestamp.signer_certificate is None:
        return (
            timestamp.status,
            "not_evaluated",
            "not_evaluated",
            generation_time,
            False,
            (),
            "RFC 3161 TSA certificate was not embedded",
            "not_evaluated",
            (),
            "not_evaluated",
        )

    loaded = _load_summary_certificates(timestamp.certificates)
    tsa_certificate = loaded.get(timestamp.signer_certificate.sha256_fingerprint)
    if tsa_certificate is None:
        return (
            timestamp.status,
            "not_evaluated",
            "not_evaluated",
            generation_time,
            False,
            (),
            "RFC 3161 TSA certificate could not be loaded for chain validation",
            "not_evaluated",
            (),
            "not_evaluated",
        )

    pool = tuple(loaded.values())
    outcome = _build_chain(
        tsa_certificate,
        pool,
        timestamp_anchors,
        verification_time=generation_time,
    )
    revocation_status, revocation_detail = _evaluate_revocation(
        outcome.chain,
        anchors=timestamp_anchors,
        crls=crls,
        pool=pool,
        verification_time=generation_time,
        now=now,
        online_client=online_client,
    )
    constraints = evaluate_chain_constraints(
        outcome.chain,
        anchor_fingerprints=frozenset(timestamp_anchors),
        options=policy_options,
    )
    trusted = (
        outcome.status == "trusted"
        and constraints.policy_status == "valid"
        and constraints.name_constraints_status == "valid"
        and revocation_status
        in {
            "not_revoked",
            "not_applicable",
            "revoked_after_signing",
        }
    )
    constraint_detail = (
        f"{constraints.policy_detail}; {constraints.name_constraints_detail}"
    )
    detail = (
        "RFC 3161 timestamp chain is trusted"
        if trusted
        else (
            f"RFC 3161 timestamp is not trusted: {outcome.detail}; "
            f"{revocation_detail}; {constraint_detail}"
        )
    )
    return (
        timestamp.status,
        outcome.status,
        revocation_status,
        generation_time,
        trusted,
        _chain_summaries(
            outcome.chain,
            chain_status=outcome.status,
            verification_time=generation_time,
        ),
        detail,
        constraints.policy_status,
        constraints.policy_oids,
        constraints.name_constraints_status,
    )


def _evaluate_signer(
    signer: PdfSignerSummary,
    *,
    embedded: dict[str, x509.Certificate],
    anchors: dict[str, x509.Certificate],
    timestamp_anchors: dict[str, x509.Certificate],
    crls: tuple[x509.CertificateRevocationList, ...],
    now: datetime,
    requested_verification_time: datetime | None,
    online_client: OnlineRevocationClient | None = None,
    policy_options: CertificatePolicyOptions | None = None,
    timestamp_policy_options: CertificatePolicyOptions | None = None,
) -> PdfSignerTrustSummary:
    signing_time = _parse_time(signer.signing_time)
    claimed_signing_time = _parse_time(signer.claimed_signing_time)
    if requested_verification_time is not None:
        verification_time = requested_verification_time.astimezone(UTC)
        time_source = "requested_verification_time"
    elif signing_time is not None:
        verification_time = signing_time
        time_source = "cms_signing_time"
    elif claimed_signing_time is not None:
        verification_time = claimed_signing_time
        time_source = signer.claimed_signing_time_source or "claimed_signing_time"
    else:
        verification_time = now
        time_source = "current_time"

    (
        timestamp_status,
        timestamp_chain_status,
        timestamp_revocation_status,
        timestamp_generation_time,
        timestamp_trusted,
        timestamp_chain,
        timestamp_detail,
        timestamp_policy_status,
        timestamp_policy_oids,
        timestamp_name_constraints_status,
    ) = _evaluate_timestamp(
        signer,
        timestamp_anchors=timestamp_anchors,
        crls=crls,
        now=now,
        online_client=online_client,
        policy_options=timestamp_policy_options,
    )
    if (
        requested_verification_time is None
        and timestamp_trusted
        and timestamp_generation_time is not None
    ):
        verification_time = timestamp_generation_time
        time_source = "rfc3161_timestamp"

    certificate = None
    if signer.certificate is not None:
        certificate = embedded.get(signer.certificate.sha256_fingerprint)
    if certificate is None:
        return PdfSignerTrustSummary(
            signer_subject=(
                signer.certificate.subject if signer.certificate is not None else None
            ),
            signer_sha256_fingerprint=(
                signer.certificate.sha256_fingerprint
                if signer.certificate is not None
                else None
            ),
            verification_time=verification_time.isoformat(),
            verification_time_source=time_source,
            chain_status="not_evaluated",
            revocation_status="not_evaluated",
            overall_status="cryptographically_valid_untrusted",
            status_detail="the CMS signer certificate was not embedded in the PDF",
            chain=(),
            timestamp_status=timestamp_status,
            timestamp_chain_status=timestamp_chain_status,
            timestamp_revocation_status=timestamp_revocation_status,
            timestamp_time=(
                timestamp_generation_time.isoformat()
                if timestamp_generation_time is not None
                else None
            ),
            timestamp_trusted=timestamp_trusted,
            timestamp_chain=timestamp_chain,
            timestamp_policy_status=timestamp_policy_status,
            timestamp_policy_oids=timestamp_policy_oids,
            timestamp_name_constraints_status=timestamp_name_constraints_status,
        )

    pool = tuple(embedded.values())
    outcome = _build_chain(
        certificate,
        pool,
        anchors,
        verification_time=verification_time,
    )
    revocation_status, revocation_detail = _evaluate_revocation(
        outcome.chain,
        anchors=anchors,
        crls=crls,
        pool=pool,
        verification_time=verification_time,
        now=now,
        online_client=online_client,
    )
    constraints = evaluate_chain_constraints(
        outcome.chain,
        anchor_fingerprints=frozenset(anchors),
        options=policy_options,
    )
    return PdfSignerTrustSummary(
        signer_subject=certificate.subject.rfc4514_string()[:1000],
        signer_sha256_fingerprint=_fingerprint(certificate),
        verification_time=verification_time.isoformat(),
        verification_time_source=time_source,
        chain_status=outcome.status,
        revocation_status=revocation_status,
        overall_status=_signer_overall(
            outcome.status,
            revocation_status,
            policy_status=constraints.policy_status,
            name_constraints_status=constraints.name_constraints_status,
        ),
        status_detail=(
            f"{outcome.detail}; {revocation_detail}; "
            f"{constraints.policy_detail}; "
            f"{constraints.name_constraints_detail}"
            + (f"; {timestamp_detail}" if timestamp_detail else "")
        ),
        chain=_chain_summaries(
            outcome.chain,
            chain_status=outcome.status,
            verification_time=verification_time,
        ),
        timestamp_status=timestamp_status,
        timestamp_chain_status=timestamp_chain_status,
        timestamp_revocation_status=timestamp_revocation_status,
        timestamp_time=(
            timestamp_generation_time.isoformat()
            if timestamp_generation_time is not None
            else None
        ),
        timestamp_trusted=timestamp_trusted,
        timestamp_chain=timestamp_chain,
        policy_status=constraints.policy_status,
        policy_detail=constraints.policy_detail,
        policy_oids=constraints.policy_oids,
        name_constraints_status=constraints.name_constraints_status,
        name_constraints_detail=constraints.name_constraints_detail,
        timestamp_policy_status=timestamp_policy_status,
        timestamp_policy_oids=timestamp_policy_oids,
        timestamp_name_constraints_status=timestamp_name_constraints_status,
    )


def _aggregate_status(values: Sequence[str], *, trusted_value: str) -> str:
    if not values:
        return "not_evaluated"
    if all(value == trusted_value for value in values):
        return trusted_value
    if "invalid" in values:
        return "invalid"
    if "incomplete" in values:
        return "incomplete"
    if "untrusted" in values:
        return "untrusted"
    return values[0]


def _aggregate_constraint_status(values: Sequence[str]) -> str:
    if not values:
        return "not_evaluated"
    if "invalid" in values:
        return "invalid"
    if all(value == "valid" for value in values):
        return "valid"
    return "not_evaluated"


def _evaluate_signature(
    signature: PdfSignatureSummary,
    *,
    anchors: dict[str, x509.Certificate],
    timestamp_anchors: dict[str, x509.Certificate],
    crls: tuple[x509.CertificateRevocationList, ...],
    now: datetime,
    requested_verification_time: datetime | None,
    online_client: OnlineRevocationClient | None = None,
    policy_options: CertificatePolicyOptions | None = None,
    timestamp_policy_options: CertificatePolicyOptions | None = None,
) -> PdfSignatureTrustSummary:
    if signature.status == "unsigned":
        return PdfSignatureTrustSummary(
            field_name=signature.field_name,
            page=signature.page,
            cryptographic_status=signature.status,
            cryptographic_detail=signature.status_detail,
            chain_status="not_applicable",
            revocation_status="not_applicable",
            overall_status="unsigned",
            status_detail="the signature field is unsigned",
            signers=(),
            policy_status="not_applicable",
            name_constraints_status="not_applicable",
        )
    if signature.status != "cryptographically_valid_untrusted":
        return PdfSignatureTrustSummary(
            field_name=signature.field_name,
            page=signature.page,
            cryptographic_status=signature.status,
            cryptographic_detail=signature.status_detail,
            chain_status="not_evaluated",
            revocation_status="not_evaluated",
            overall_status=signature.status,
            status_detail="trust was not evaluated because local CMS validation did not pass",
            signers=(),
        )

    embedded = _load_embedded_certificates(signature)
    signer_results = tuple(
        _evaluate_signer(
            signer,
            embedded=embedded,
            anchors=anchors,
            timestamp_anchors=timestamp_anchors,
            crls=crls,
            now=now,
            requested_verification_time=requested_verification_time,
            online_client=online_client,
            policy_options=policy_options,
            timestamp_policy_options=timestamp_policy_options,
        )
        for signer in signature.signers
    )
    chain_status = _aggregate_status(
        [item.chain_status for item in signer_results], trusted_value="trusted"
    )
    revocation_values = [item.revocation_status for item in signer_results]
    if "revoked" in revocation_values:
        revocation_status = "revoked"
    elif revocation_values and all(
        value in {"not_revoked", "not_applicable", "revoked_after_signing"}
        for value in revocation_values
    ):
        revocation_status = (
            "revoked_after_signing"
            if "revoked_after_signing" in revocation_values
            else ("not_applicable" if set(revocation_values) == {"not_applicable"} else "not_revoked")
        )
    else:
        revocation_status = "unknown" if revocation_values else "not_evaluated"

    policy_status = _aggregate_constraint_status(
        [item.policy_status for item in signer_results]
    )
    name_constraints_status = _aggregate_constraint_status(
        [item.name_constraints_status for item in signer_results]
    )
    policy_oids = tuple(
        sorted({oid for item in signer_results for oid in item.policy_oids})
    )
    overall_status = _signer_overall(
        chain_status,
        revocation_status,
        policy_status=policy_status,
        name_constraints_status=name_constraints_status,
    )
    details = "; ".join(item.status_detail for item in signer_results)
    return PdfSignatureTrustSummary(
        field_name=signature.field_name,
        page=signature.page,
        cryptographic_status=signature.status,
        cryptographic_detail=signature.status_detail,
        chain_status=chain_status,
        revocation_status=revocation_status,
        overall_status=overall_status,
        status_detail=details or "no CMS signer was available for trust evaluation",
        signers=signer_results,
        policy_status=policy_status,
        name_constraints_status=name_constraints_status,
        policy_oids=policy_oids,
    )


def validate_pdf_signature_trust(
    source: Path,
    *,
    trust_anchor_paths: Sequence[Path] = (),
    crl_paths: Sequence[Path] = (),
    timestamp_trust_anchor_paths: Sequence[Path] = (),
    verification_time: datetime | None = None,
    online_revocation: bool = False,
    revocation_timeout_seconds: int = 10,
    revocation_allow_private_network: bool = False,
    revocation_opener: Any | None = None,
    revocation_resolver: Any | None = None,
    required_policy_oids: Sequence[str] | str = (),
    timestamp_required_policy_oids: Sequence[str] | str = (),
    explicit_policy_required: bool = False,
    inhibit_any_policy: bool = False,
    inhibit_policy_mapping: bool = False,
    system_trust_anchors: bool = False,
    timestamp_system_trust_anchors: bool = False,
    system_trust_loader: Any | None = None,
) -> PdfTrustValidation:
    """Validate chains, policies/NC and local (or online) revocation."""

    if verification_time is not None and verification_time.tzinfo is None:
        raise ToolError("PDF trust verification time must include a timezone.")
    signer_policy_options = CertificatePolicyOptions(
        required_policy_oids=required_policy_oids,  # type: ignore[arg-type]
        explicit_policy_required=explicit_policy_required,
        inhibit_any_policy=inhibit_any_policy,
        inhibit_policy_mapping=inhibit_policy_mapping,
    )
    timestamp_policy_options = CertificatePolicyOptions(
        required_policy_oids=timestamp_required_policy_oids,  # type: ignore[arg-type]
        explicit_policy_required=explicit_policy_required,
        inhibit_any_policy=inhibit_any_policy,
        inhibit_policy_mapping=inhibit_policy_mapping,
    )
    anchor_certificates, anchor_summaries = _load_anchor_certificates(trust_anchor_paths)
    if timestamp_trust_anchor_paths:
        timestamp_anchor_certificates, timestamp_anchor_summaries = (
            _load_anchor_certificates(timestamp_trust_anchor_paths)
        )
    else:
        # A commonly supplied local root can issue both document and TSA
        # certificates. System anchors are not copied across implicitly:
        # signer and TSA system-store use have separate explicit switches.
        timestamp_anchor_certificates = anchor_certificates
        timestamp_anchor_summaries = anchor_summaries

    system_store: SystemTrustStore | None = None
    if system_trust_anchors or timestamp_system_trust_anchors:
        system_store = load_system_trust_store(system_trust_loader)
    effective_anchor_certificates, signer_system_anchor_count = (
        _merge_anchor_certificates(
            anchor_certificates,
            system_store if system_trust_anchors else None,
        )
    )
    effective_timestamp_anchor_certificates, timestamp_system_anchor_count = (
        _merge_anchor_certificates(
            timestamp_anchor_certificates,
            system_store if timestamp_system_trust_anchors else None,
        )
    )
    crls, crl_summaries = _load_crls(crl_paths)
    inspection = inspect_pdf_signatures(source)
    anchors = {_fingerprint(item): item for item in effective_anchor_certificates}
    timestamp_anchors = {
        _fingerprint(item): item
        for item in effective_timestamp_anchor_certificates
    }
    now = datetime.now(UTC)
    online_client = OnlineRevocationClient(
        enabled=online_revocation,
        timeout_seconds=revocation_timeout_seconds,
        allow_private_network=revocation_allow_private_network,
        opener=revocation_opener,
        resolver=revocation_resolver,
    )
    signatures = tuple(
        _evaluate_signature(
            signature,
            anchors=anchors,
            timestamp_anchors=timestamp_anchors,
            crls=crls,
            now=now,
            requested_verification_time=verification_time,
            online_client=online_client,
            policy_options=signer_policy_options,
            timestamp_policy_options=timestamp_policy_options,
        )
        for signature in inspection.signatures
    )
    combined_crl_summaries = list(crl_summaries)
    known_crls = {item.sha256_fingerprint for item in combined_crl_summaries}
    for record in online_client.online_crls:
        fingerprint = record.crl.fingerprint(hashes.SHA256()).hex()
        if fingerprint in known_crls:
            continue
        known_crls.add(fingerprint)
        last_update = getattr(record.crl, "last_update_utc", None)
        next_update = getattr(record.crl, "next_update_utc", None)
        combined_crl_summaries.append(
            PdfCrlSummary(
                source_name=f"online:{record.source_host}",
                issuer=record.crl.issuer.rfc4514_string()[:1000],
                sha256_fingerprint=fingerprint,
                last_update=last_update.isoformat() if last_update else None,
                next_update=next_update.isoformat() if next_update else None,
            )
        )
    return PdfTrustValidation(
        inspection=inspection,
        trust_anchors=anchor_summaries,
        crls=tuple(combined_crl_summaries),
        signatures=signatures,
        timestamp_trust_anchors=timestamp_anchor_summaries,
        online_revocation_enabled=online_client.enabled,
        revocation_network_used=online_client.network_used,
        online_crl_count=len(online_client.online_crls),
        ocsp_response_count=online_client.ocsp_response_count,
        revocation_network_errors=tuple(online_client.errors),
        required_policy_oids=signer_policy_options.required_policy_oids,
        timestamp_required_policy_oids=(
            timestamp_policy_options.required_policy_oids
        ),
        explicit_policy_required=explicit_policy_required,
        inhibit_any_policy=inhibit_any_policy,
        inhibit_policy_mapping=inhibit_policy_mapping,
        system_trust_enabled=system_trust_anchors,
        system_trust_anchor_count=signer_system_anchor_count,
        timestamp_system_trust_enabled=timestamp_system_trust_anchors,
        timestamp_system_trust_anchor_count=timestamp_system_anchor_count,
        system_trust_source=(
            system_store.source if system_store is not None else None
        ),
        system_trust_skipped_count=(
            system_store.skipped_count if system_store is not None else 0
        ),
        effective_trust_anchor_count=len(effective_anchor_certificates),
        effective_timestamp_trust_anchor_count=len(
            effective_timestamp_anchor_certificates
        ),
    )


