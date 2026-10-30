"""Direct-only DocTimeStamp-only incremental PDF revisions.

Phase 35 appends one RFC 3161 ``/DocTimeStamp`` dictionary to an unencrypted
PDF without adding a signer signature or rewriting any earlier revision.
The source bytes remain an exact prefix of the output. Publication requires
every existing approval signature and document timestamp to verify again,
and requires the new timestamp token to verify over the whole new revision.
Only a digest of the ByteRange bytes is sent to the user-selected HTTPS TSA;
the PDF itself is never uploaded. TSA trust is not evaluated here.
"""

from __future__ import annotations

import hashlib
import io
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ToolError
from .pdf import _load_pypdf, sha256_file
from .pdf_forms import _as_int, _get, _object, inspect_pdf_form
from .pdf_signatures import inspect_pdf_signatures
from .pdf_signing import (
    MAX_PDF_SIGNING_FILE_BYTES,
    MAX_PDF_SIGNING_PAGES,
    MAX_SIGNATURE_FIELD_NAME_CHARS,
    _find_writer_signature_field,
    _placeholder_layout,
    _signer_fingerprints,
)
from .pdf_timestamps import (
    MAX_TIMESTAMP_TOKEN_BYTES,
    SUPPORTED_TIMESTAMP_HASHES,
    request_timestamp_token,
)

INITIAL_DOCUMENT_TIMESTAMP_CONTENTS_BYTES = 32_768
MAX_DOCUMENT_TIMESTAMP_CONTENTS_BYTES = MAX_TIMESTAMP_TOKEN_BYTES


@dataclass(frozen=True, slots=True)
class PdfDocumentTimestampResult:
    source_path: Path
    output_path: Path
    field_name: str
    created_field: bool
    page: int | None
    status: str
    timestamp_status: str
    timestamp_time: str | None
    timestamp_policy_oid: str | None
    timestamp_serial_number: str | None
    timestamp_hash_algorithm: str
    tsa_subject: str | None
    tsa_certificate_sha256: str | None
    token_size: int
    reserved_contents_size: int
    pages: int
    size_bytes: int
    sha256: str
    existing_signature_count: int = 0
    existing_document_timestamp_count: int = 0
    preserved_valid_count: int = 0
    total_signed_field_count: int = 1
    source_revision_bytes: int = 0
    incremental_update: bool = True
    verified: bool = True

    def summary(self) -> dict[str, Any]:
        return {
            "operation": "document_timestamp",
            "field_name": self.field_name,
            "created_field": self.created_field,
            "page": self.page,
            "status": self.status,
            "verified": self.verified,
            "signature_created": False,
            "timestamp_status": self.timestamp_status,
            "timestamp_time": self.timestamp_time,
            "timestamp_policy_oid": self.timestamp_policy_oid,
            "timestamp_serial_number": self.timestamp_serial_number,
            "timestamp_hash_algorithm": self.timestamp_hash_algorithm,
            "tsa_subject": self.tsa_subject,
            "tsa_certificate_sha256": self.tsa_certificate_sha256,
            "tsa_trust_status": "not_evaluated",
            "token_size": self.token_size,
            "reserved_contents_size": self.reserved_contents_size,
            "pages": self.pages,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "existing_signature_count": self.existing_signature_count,
            "existing_document_timestamp_count": (
                self.existing_document_timestamp_count
            ),
            "preserved_valid_count": self.preserved_valid_count,
            "total_signed_field_count": self.total_signed_field_count,
            "source_revision_bytes": self.source_revision_bytes,
            "incremental_update": self.incremental_update,
        }


def _classify_existing_fields(
    inspection: Any,
) -> tuple[int | None, int, int]:
    """Return the strictest DocMDP permission and preserved-field counts."""

    signed = [item for item in inspection.signatures if item.signed]
    unsupported: list[str] = []
    approval_count = 0
    timestamp_count = 0
    permissions: list[int] = []
    for item in signed:
        if item.docmdp_permission is not None:
            permissions.append(item.docmdp_permission)
        is_document_timestamp = (
            item.sub_filter == "ETSI.RFC3161"
            and item.document_timestamp is not None
        )
        if is_document_timestamp:
            timestamp = item.document_timestamp
            if (
                item.status == "cryptographically_valid_untrusted"
                and item.byte_range is not None
                and timestamp is not None
                and timestamp.status == "verified_untrusted"
                and timestamp.message_imprint_matches is True
            ):
                timestamp_count += 1
            else:
                unsupported.append(item.field_name)
        elif (
            item.status == "cryptographically_valid_untrusted"
            and item.byte_range is not None
            and item.document_timestamp is None
            and item.sub_filter != "ETSI.RFC3161"
        ):
            approval_count += 1
        else:
            unsupported.append(item.field_name)

    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise ToolError(
            "A DocTimeStamp revision requires every existing signature and "
            "document timestamp to be cryptographically valid and "
            f"supported; unsupported or invalid field(s): {names}."
        )
    return (
        min(permissions) if permissions else None,
        approval_count,
        timestamp_count,
    )


def _select_timestamp_field(
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
    blank_names = {
        item.field_name
        for item in inspection.signatures
        if item.status == "unsigned"
    }
    if field_name is not None:
        cleaned = field_name.strip()
        if not cleaned or len(cleaned) > MAX_SIGNATURE_FIELD_NAME_CHARS:
            raise ToolError("The DocTimeStamp field name is empty or too long.")
        if "\x00" in cleaned:
            raise ToolError("The DocTimeStamp field name contains a NUL character.")
        if cleaned in signed_names:
            raise ToolError(
                f"PDF signature field {cleaned!r} already has a value; choose "
                "a blank signature field or create a new DocTimeStamp field."
            )
        if cleaned in blank_names:
            return cleaned, False
        if cleaned in signature_names or cleaned in all_field_names:
            raise ToolError(
                f"PDF field {cleaned!r} is not a blank signature field."
            )
        if not create_field:
            raise ToolError(f"PDF signature field was not found: {cleaned!r}.")
        if "." in cleaned or "/" in cleaned:
            raise ToolError("A new DocTimeStamp field name must not contain '.' or '/'.")
        return cleaned, True

    if "DocTimeStamp" in blank_names:
        return "DocTimeStamp", False
    index = 1
    while True:
        candidate = "DocTimeStamp" if index == 1 else f"DocTimeStamp{index}"
        if candidate not in all_field_names:
            return candidate, True
        index += 1


def _timestamp_identity(signature: Any) -> tuple[Any, ...] | None:
    timestamp = signature.document_timestamp
    if timestamp is None:
        return None
    certificate = timestamp.signer_certificate
    return (
        timestamp.status,
        timestamp.generation_time,
        timestamp.policy_oid,
        timestamp.serial_number,
        timestamp.hash_algorithm,
        timestamp.message_imprint_matches,
        timestamp.nonce,
        timestamp.token_size,
        certificate.sha256_fingerprint if certificate is not None else None,
    )


def _verify_preserved_fields(before: Any, after: Any) -> int:
    before_signed = {
        item.field_name: item for item in before.signatures if item.signed
    }
    after_by_name = {item.field_name: item for item in after.signatures}
    for field_name, previous in before_signed.items():
        current = after_by_name.get(field_name)
        if (
            current is None
            or current.status != "cryptographically_valid_untrusted"
            or current.sub_filter != previous.sub_filter
            or current.byte_range != previous.byte_range
            or current.contents_size != previous.contents_size
            or current.cms_size != previous.cms_size
            or current.docmdp_permission != previous.docmdp_permission
            or _signer_fingerprints(current) != _signer_fingerprints(previous)
            or _timestamp_identity(current) != _timestamp_identity(previous)
        ):
            raise ToolError(
                "Timestamped PDF failed verification: existing field "
                f"{field_name!r} was not preserved."
            )
    return len(before_signed)


def _build_placeholder_pdf(
    *,
    source: Path,
    field_name: str,
    create_field: bool,
    contents_capacity: int,
    page_number: int,
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
    writer = PdfWriter(str(source), incremental=True)
    # Reserve the previous XRef-stream object number for the same reason as
    # Phase 34 incremental signing: pypdf does not clone it as a regular
    # object, and reusing it can collide with readers' cached XRef object.
    try:
        reserved_objects = int(writer._reader.trailer.get("/Size")) - 1
    except (AttributeError, TypeError, ValueError):
        reserved_objects = len(writer._objects)
    while len(writer._objects) < reserved_objects:
        writer._objects.append(None)

    try:
        if not (1 <= page_number <= len(writer.pages)):
            raise ToolError(
                f"DocTimeStamp page {page_number} is outside this PDF "
                f"(1 to {len(writer.pages)})."
            )

        placeholder = NumberObject(9999999999)
        timestamp = DictionaryObject()
        timestamp.update(
            {
                NameObject("/Type"): NameObject("/DocTimeStamp"),
                NameObject("/Filter"): NameObject("/Adobe.PPKLite"),
                NameObject("/SubFilter"): NameObject("/ETSI.RFC3161"),
                NameObject("/ByteRange"): ArrayObject(
                    [NumberObject(0), placeholder, placeholder, placeholder]
                ),
                NameObject("/Contents"): ByteStringObject(
                    b"\x00" * contents_capacity
                ),
            }
        )
        app = DictionaryObject()
        app[NameObject("/Name")] = NameObject("/SelfHealingPCAgent")
        sig_build = DictionaryObject()
        sig_build[NameObject("/App")] = app
        prop_build = DictionaryObject()
        prop_build[NameObject("/Sig")] = sig_build
        timestamp[NameObject("/Prop_Build")] = prop_build
        timestamp_ref = writer._add_object(timestamp)

        root = writer._root_object
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
            field = DictionaryObject()
            field.update(
                {
                    NameObject("/Type"): NameObject("/Annot"),
                    NameObject("/Subtype"): NameObject("/Widget"),
                    NameObject("/Rect"): RectangleObject([0, 0, 0, 0]),
                    NameObject("/F"): NumberObject(4),
                    NameObject("/FT"): NameObject("/Sig"),
                    NameObject("/T"): TextStringObject(field_name),
                    NameObject("/V"): timestamp_ref,
                }
            )
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
                raise ToolError(
                    f"PDF signature field was not found: {field_name!r}."
                )
            target[NameObject("/V")] = timestamp_ref

        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()
    finally:
        writer.close()


def _validate_inputs(
    source: Path,
    output: Path,
    *,
    page_number: int,
) -> tuple[Any, Any, str, int | None, int, int]:
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise ToolError(f"Source PDF file not found or not a PDF: {source.name}")
    if source.stat().st_size > MAX_PDF_SIGNING_FILE_BYTES:
        raise ToolError("PDF document timestamping is limited to 100 MB files.")
    if output.suffix.lower() != ".pdf":
        raise ToolError("The timestamped PDF output must be a .pdf file.")
    if output.exists():
        raise ToolError(
            "Output file already exists. PDF tools never overwrite; "
            "choose a new output path."
        )
    if source.expanduser().resolve() == output.expanduser().resolve():
        raise ToolError("The timestamped PDF output must not be the source PDF.")
    if page_number < 1:
        raise ToolError("The DocTimeStamp page must be at least 1.")

    before = inspect_pdf_signatures(source)
    if before.pages > MAX_PDF_SIGNING_PAGES:
        raise ToolError(
            f"PDF document timestamping is limited to {MAX_PDF_SIGNING_PAGES} pages."
        )
    permission, approval_count, timestamp_count = _classify_existing_fields(
        before
    )
    if permission == 1:
        raise ToolError(
            "The existing DocMDP certification permission P=1 prohibits "
            "all changes, including a DocTimeStamp revision."
        )
    before_form = inspect_pdf_form(source)
    return (
        before,
        before_form,
        before.sha256,
        permission,
        approval_count,
        timestamp_count,
    )


def add_pdf_document_timestamp(
    source: Path,
    output: Path,
    *,
    timestamp_url: str,
    field_name: str | None = None,
    create_field: bool = False,
    page: int = 1,
    hash_algorithm: str = "sha256",
    timeout_seconds: int = 15,
    allow_private_network: bool = False,
    timestamp_opener: Any | None = None,
) -> PdfDocumentTimestampResult:
    """Append one verified RFC 3161 DocTimeStamp incremental revision."""

    clean_url = timestamp_url.strip()
    if not clean_url:
        raise ToolError("A timestamp authority HTTPS URL is required.")
    normalized_hash = hash_algorithm.strip().lower().replace("-", "")
    if normalized_hash not in SUPPORTED_TIMESTAMP_HASHES:
        raise ToolError("Timestamp hash must be sha256, sha384 or sha512.")
    if not 1 <= timeout_seconds <= 60:
        raise ToolError("Timestamp timeout must be between 1 and 60 seconds.")

    (
        before,
        before_form,
        source_hash,
        existing_permission,
        approval_count,
        existing_timestamp_count,
    ) = _validate_inputs(source, output, page_number=page)
    target_field, should_create = _select_timestamp_field(
        before,
        field_name=field_name,
        create_field=create_field,
        all_field_names=set(before_form.field_names),
    )
    if existing_permission == 2 and should_create:
        raise ToolError(
            "The existing DocMDP certification permission P=2 permits "
            "timestamping an existing blank signature field, but not "
            "creating a new DocTimeStamp field."
        )

    source_bytes = source.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != source_hash:
        raise ToolError("PDF changed during timestamp preparation; retry.")

    capacity = INITIAL_DOCUMENT_TIMESTAMP_CONTENTS_BYTES
    timestamped_data: bytes | None = None
    token = None
    while capacity <= MAX_DOCUMENT_TIMESTAMP_CONTENTS_BYTES:
        placeholder_data = _build_placeholder_pdf(
            source=source,
            field_name=target_field,
            create_field=should_create,
            contents_capacity=capacity,
            page_number=page,
        )
        if not placeholder_data.startswith(source_bytes):
            raise ToolError(
                "DocTimeStamp signing failed to preserve the exact source "
                "revision prefix."
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

        message_bytes = bytes(data[:first_length]) + bytes(data[second_start:])
        token = request_timestamp_token(
            message_bytes,
            timestamp_url=clean_url,
            hash_algorithm=normalized_hash,
            timeout_seconds=timeout_seconds,
            allow_private_network=allow_private_network,
            opener=timestamp_opener,
        )
        if len(token.token_der) <= capacity:
            hexed = token.token_der.hex().encode("ascii")
            hex_capacity = hex_end - hex_start - 1
            data[hex_start + 1 : hex_end] = (
                hexed + b"0" * (hex_capacity - len(hexed))
            )
            timestamped_data = bytes(data)
            break

        next_capacity = ((len(token.token_der) + 4095) // 4096) * 4096
        if next_capacity <= capacity:
            next_capacity = capacity + 4096
        capacity = next_capacity

    if timestamped_data is None or token is None:
        raise ToolError("The timestamp token exceeds the reserved size limit.")
    if not timestamped_data.startswith(source_bytes):
        raise ToolError(
            "DocTimeStamp signing failed to preserve the exact source "
            "revision prefix."
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(
        f".{output.stem}.tmp-{secrets.token_hex(4)}.pdf"
    )
    preserved_count = 0
    try:
        temporary.write_bytes(timestamped_data)
        after = inspect_pdf_signatures(temporary)
        if after.pages != before.pages:
            raise ToolError("Timestamped PDF failed verification: page count changed.")
        expected_field_count = before.signature_count + (
            1 if should_create else 0
        )
        if (
            after.signature_count != expected_field_count
            or after.signed_count != before.signed_count + 1
        ):
            raise ToolError(
                "Timestamped PDF failed verification: signature-field count "
                "changed unexpectedly."
            )
        preserved_count = _verify_preserved_fields(before, after)

        after_form = inspect_pdf_form(temporary)
        expected_fields = list(before_form.field_names)
        if should_create:
            expected_fields.append(target_field)
        if set(after_form.field_names) != set(expected_fields):
            raise ToolError(
                "Timestamped PDF failed verification: form fields changed."
            )

        target = next(
            (
                item
                for item in after.signatures
                if item.field_name == target_field
            ),
            None,
        )
        if target is None:
            raise ToolError(
                "Timestamped PDF failed verification: DocTimeStamp field "
                "is missing."
            )
        timestamp = target.document_timestamp
        if (
            target.status != "cryptographically_valid_untrusted"
            or target.sub_filter != "ETSI.RFC3161"
            or target.covers_whole_file is not True
            or target.signers
            or timestamp is None
            or timestamp.status != "verified_untrusted"
            or timestamp.message_imprint_matches is not True
            or timestamp.generation_time != token.generation_time
            or timestamp.hash_algorithm != normalized_hash
            or timestamp.token_size != len(token.token_der)
        ):
            raise ToolError(
                "Timestamped PDF failed DocTimeStamp verification: "
                f"{target.status_detail}"
            )
        certificate = timestamp.signer_certificate
        if (
            certificate is None
            or certificate.sha256_fingerprint != token.tsa_certificate_sha256
        ):
            raise ToolError(
                "Timestamped PDF failed TSA certificate verification."
            )
        if sha256_file(source) != source_hash:
            raise ToolError("PDF timestamping changed the source; output rejected.")
        if output.exists():
            raise ToolError(
                "Output file appeared during PDF timestamping; it was not "
                "overwritten."
            )
        os.replace(temporary, output)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    final = inspect_pdf_signatures(output)
    preserved_count = _verify_preserved_fields(before, final)
    final_target = next(
        item for item in final.signatures if item.field_name == target_field
    )
    final_timestamp = final_target.document_timestamp
    if final_timestamp is None:
        raise ToolError("Timestamped PDF lost its DocTimeStamp after publication.")
    certificate = final_timestamp.signer_certificate
    return PdfDocumentTimestampResult(
        source_path=source,
        output_path=output,
        field_name=target_field,
        created_field=should_create,
        page=final_target.page,
        status=final_target.status,
        timestamp_status=final_timestamp.status,
        timestamp_time=final_timestamp.generation_time,
        timestamp_policy_oid=final_timestamp.policy_oid,
        timestamp_serial_number=final_timestamp.serial_number,
        timestamp_hash_algorithm=normalized_hash,
        tsa_subject=certificate.subject if certificate is not None else None,
        tsa_certificate_sha256=(
            certificate.sha256_fingerprint if certificate is not None else None
        ),
        token_size=len(token.token_der),
        reserved_contents_size=capacity,
        pages=final.pages,
        size_bytes=final.size_bytes,
        sha256=final.sha256,
        existing_signature_count=approval_count,
        existing_document_timestamp_count=existing_timestamp_count,
        preserved_valid_count=preserved_count,
        total_signed_field_count=final.signed_count,
        source_revision_bytes=len(source_bytes),
    )
