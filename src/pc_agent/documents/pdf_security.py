"""Direct-only PDF password inspection, protection and unprotection.

Phase 26 deliberately keeps PDF passwords outside registered tools, task
plans, command arguments, configuration and persisted results.  The direct
CLI/GUI collect a password through a hidden prompt and pass it only to these
functions in memory.  Operations write a new PDF, never modify or overwrite
the source/output, and verify the temporary result before publication.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ToolError
from .pdf import _load_pypdf, sha256_file

MAX_PDF_SECURITY_PAGES = 500
MAX_PDF_PASSWORD_CHARS = 4_096
MIN_NEW_PDF_PASSWORD_CHARS = 8
MAX_NEW_PDF_PASSWORD_UTF8_BYTES = 127

PDF_PERMISSION_NAMES = (
    "print",
    "modify",
    "extract",
    "annotate",
    "fill_forms",
    "accessibility",
    "assemble",
    "print_high_quality",
)
DEFAULT_PDF_PERMISSIONS = ("print", "extract", "fill_forms")

_PERMISSION_BITS = {
    "print": 4,
    "modify": 8,
    "extract": 16,
    "annotate": 32,
    "fill_forms": 256,
    "accessibility": 512,
    "assemble": 1024,
    "print_high_quality": 2048,
}


@dataclass(frozen=True, slots=True)
class PdfSecurityInfo:
    path: Path
    encrypted: bool
    pages: int | None
    unlocked: bool
    password_supplied: bool
    password_accepted: bool | None
    password_type: str | None
    algorithm: str | None
    key_bits: int | None
    handler_version: int | None
    handler_revision: int | None
    permissions: tuple[str, ...]
    permissions_valid: bool | None
    size_bytes: int
    sha256: str

    def summary(self) -> dict[str, Any]:
        return {
            "encrypted": self.encrypted,
            "pages": self.pages,
            "unlocked": self.unlocked,
            "password_supplied": self.password_supplied,
            "password_accepted": self.password_accepted,
            "password_type": self.password_type,
            "algorithm": self.algorithm,
            "key_bits": self.key_bits,
            "handler_version": self.handler_version,
            "handler_revision": self.handler_revision,
            "permissions": list(self.permissions),
            "permissions_valid": self.permissions_valid,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class PdfSecurityResult:
    source_path: Path
    output_path: Path
    operation: str
    source_pages: int
    output_pages: int
    encrypted: bool
    algorithm: str | None
    permissions: tuple[str, ...]
    size_bytes: int
    sha256: str
    verified: bool = True

    def summary(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "source_pages": self.source_pages,
            "output_pages": self.output_pages,
            "encrypted": self.encrypted,
            "algorithm": self.algorithm,
            "permissions": list(self.permissions),
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "verified": self.verified,
        }


def _check_pdf_path(path: Path, *, label: str = "PDF") -> None:
    if not path.is_file():
        raise ToolError(f"{label} file not found: {path.name}")
    if path.suffix.lower() != ".pdf":
        raise ToolError(f"Not a PDF file: {path.name}")


def _check_existing_password(password: str | None) -> None:
    if password is None:
        return
    if not isinstance(password, str):
        raise ToolError("PDF password must be text.")
    if len(password) > MAX_PDF_PASSWORD_CHARS:
        raise ToolError("PDF password is too long.")
    if "\x00" in password:
        raise ToolError("PDF password contains a NUL character.")


def _check_new_password(password: str, *, label: str) -> None:
    _check_existing_password(password)
    if len(password) < MIN_NEW_PDF_PASSWORD_CHARS:
        raise ToolError(
            f"New PDF {label} password needs at least "
            f"{MIN_NEW_PDF_PASSWORD_CHARS} characters."
        )
    if len(password.encode("utf-8")) > MAX_NEW_PDF_PASSWORD_UTF8_BYTES:
        raise ToolError(
            f"New PDF {label} password exceeds 127 UTF-8 bytes for AES-256."
        )


def _object(value: Any) -> Any:
    try:
        return value.get_object() if hasattr(value, "get_object") else value
    except (AttributeError, KeyError, TypeError, ValueError):
        return value


def _get(container: Any, key: str, default: Any = None) -> Any:
    container = _object(container)
    try:
        return container.get(key, default)
    except (AttributeError, KeyError, TypeError, ValueError):
        return default


def _as_int(value: Any) -> int | None:
    value = _object(value)
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _permission_names(raw_permissions: Any) -> tuple[str, ...]:
    value = _as_int(raw_permissions)
    if value is None:
        return ()
    unsigned = value & 0xFFFFFFFF
    return tuple(
        name for name, bit in _PERMISSION_BITS.items() if unsigned & bit
    )


def _algorithm(encryption: Any) -> tuple[str | None, int | None, int | None, int | None]:
    if encryption is None:
        return None, None, None, None
    version = _as_int(_get(encryption, "/V"))
    revision = _as_int(_get(encryption, "/R"))
    length = _as_int(_get(encryption, "/Length"))
    stream_filter = _get(_get(encryption, "/CF"), "/StmF")
    method = str(_get(stream_filter, "/CFM", "")).lstrip("/")
    filter_length = _as_int(_get(stream_filter, "/Length"))

    if version == 5:
        return "AES-256", 256, version, revision
    if version == 4:
        key_bits = length
        if key_bits is None and filter_length is not None:
            key_bits = filter_length * 8
        if method == "AESV2":
            return "AES-128", key_bits or 128, version, revision
        if method in {"V2", "RC4"}:
            label = f"RC4-{key_bits}" if key_bits else "RC4"
            return label, key_bits, version, revision
        return (method or "crypt-filter"), key_bits, version, revision
    if version == 2:
        key_bits = length or 128
        return f"RC4-{key_bits}", key_bits, version, revision
    if version == 1:
        return "RC4-40", length or 40, version, revision
    label = str(_get(encryption, "/Filter", "encrypted")).lstrip("/") or "encrypted"
    return label, length, version, revision


def _page_count(reader: Any, path: Path) -> int:
    try:
        pages = len(reader.pages)
        for page in reader.pages:
            box = page.mediabox
            if float(box.width) <= 0 or float(box.height) <= 0:
                raise ToolError(f"PDF contains an invalid page size: {path.name}")
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(
            f"Could not read PDF pages in '{path.name}': {type(exc).__name__}"
        ) from exc
    if pages < 1:
        raise ToolError(f"PDF has no pages: {path.name}")
    if pages > MAX_PDF_SECURITY_PAGES:
        raise ToolError(
            f"PDF password operations are limited to {MAX_PDF_SECURITY_PAGES} pages."
        )
    return pages


def inspect_pdf_security(
    path: Path, *, password: str | None = None
) -> PdfSecurityInfo:
    """Inspect encryption metadata, optionally testing an in-memory password."""

    _check_pdf_path(path)
    _check_existing_password(password)
    PdfReader, _PdfWriter = _load_pypdf()

    try:
        reader = PdfReader(str(path))
        encrypted = bool(reader.is_encrypted)
        encryption = _get(getattr(reader, "trailer", {}), "/Encrypt")
        algorithm, key_bits, version, revision = _algorithm(encryption)
        raw_permissions = _get(encryption, "/P") if encrypted else None
        permissions = _permission_names(raw_permissions) if encrypted else ()

        password_accepted: bool | None = None
        password_type: str | None = None
        unlocked = not encrypted
        pages: int | None = None

        if encrypted and password is not None:
            try:
                result = int(reader.decrypt(password))
            except Exception as exc:
                raise ToolError(
                    f"Could not test the PDF password: {type(exc).__name__}"
                ) from exc
            password_accepted = result != 0
            unlocked = password_accepted
            if result == 1:
                password_type = "user"
            elif result == 2:
                password_type = "owner"
            elif result != 0:
                password_type = "accepted"

        if unlocked:
            pages = _page_count(reader, path)

        permissions_valid = getattr(reader, "are_permissions_valid", None)
        if not isinstance(permissions_valid, bool):
            permissions_valid = None
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(
            f"Could not inspect PDF security for '{path.name}': {type(exc).__name__}"
        ) from exc

    return PdfSecurityInfo(
        path=path,
        encrypted=encrypted,
        pages=pages,
        unlocked=unlocked,
        password_supplied=password is not None,
        password_accepted=password_accepted,
        password_type=password_type,
        algorithm=algorithm,
        key_bits=key_bits,
        handler_version=version,
        handler_revision=revision,
        permissions=permissions,
        permissions_valid=permissions_valid,
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
    )


def _is_signature_value(value: Any) -> bool:
    signature = _object(value)
    if signature is None:
        return False
    return (
        _get(signature, "/ByteRange") is not None
        or _get(signature, "/Contents") is not None
    )


def _field_tree_has_signature(
    node: Any,
    inherited_type: str,
    inherited_value: Any,
    seen: set[int],
) -> bool:
    obj = _object(node)
    if obj is None:
        return False
    identity = id(obj)
    if identity in seen:
        return False
    seen.add(identity)
    field_type = str(_get(obj, "/FT", inherited_type) or inherited_type).lstrip("/")
    field_value = _get(obj, "/V", inherited_value)
    if field_type == "Sig" and _is_signature_value(field_value):
        return True
    kids = _object(_get(obj, "/Kids"))
    if kids is None:
        return False
    try:
        values = list(kids)
    except TypeError:
        return False
    return any(
        _field_tree_has_signature(kid, field_type, field_value, seen)
        for kid in values
    )


def _has_digital_signature(reader: Any) -> bool:
    root = _object(getattr(reader, "root_object", None))
    if root is None:
        root = _get(getattr(reader, "trailer", {}), "/Root")
    if _get(_get(root, "/Perms"), "/DocMDP") is not None:
        return True
    acro_form = _get(root, "/AcroForm")
    signature_flags = _as_int(_get(acro_form, "/SigFlags")) or 0
    if signature_flags & 1:
        return True
    fields = _object(_get(acro_form, "/Fields"))
    if fields is None:
        return False
    try:
        values = list(fields)
    except TypeError:
        return False
    seen: set[int] = set()
    return any(
        _field_tree_has_signature(field, "", None, seen) for field in values
    )


def _open_unlocked_reader(path: Path, password: str | None) -> tuple[Any, int, str | None]:
    PdfReader, _PdfWriter = _load_pypdf()
    try:
        reader = PdfReader(str(path))
        password_type: str | None = None
        if reader.is_encrypted:
            if password is None:
                raise ToolError("This PDF is encrypted; a current password is required.")
            result = int(reader.decrypt(password))
            if result == 0:
                raise ToolError("Incorrect PDF password.")
            password_type = "user" if result == 1 else "owner" if result == 2 else "accepted"
        pages = _page_count(reader, path)
        if _has_digital_signature(reader):
            raise ToolError(
                "Digitally signed PDFs are refused because rewriting would "
                "invalidate the signature."
            )
        return reader, pages, password_type
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(
            f"Could not open PDF '{path.name}': {type(exc).__name__}"
        ) from exc


def _prepare_output(source: Path, output: Path) -> None:
    _check_pdf_path(source, label="Source PDF")
    if output.suffix.lower() != ".pdf":
        raise ToolError("The PDF output must be a .pdf file.")
    if output.exists():
        raise ToolError(
            "Output file already exists. PDF tools never overwrite; "
            "choose a new output path."
        )
    if source.expanduser().resolve() == output.expanduser().resolve():
        raise ToolError("The PDF output must not be the source PDF.")


def _validated_permissions(permissions: Iterable[str]) -> tuple[str, ...]:
    chosen: list[str] = []
    for raw in permissions:
        name = str(raw).strip().lower().replace("-", "_")
        if name not in _PERMISSION_BITS:
            allowed = ", ".join(PDF_PERMISSION_NAMES)
            raise ToolError(
                f"Unknown PDF permission {raw!r}. Choose from: {allowed}."
            )
        if name not in chosen:
            chosen.append(name)
    return tuple(chosen)


def _permission_flag(permissions: tuple[str, ...]) -> Any:
    try:
        from pypdf.constants import UserAccessPermissions
    except ImportError as exc:  # pragma: no cover - pypdf is declared.
        raise ToolError("PDF encryption support is unavailable.") from exc

    mapping = {
        "print": UserAccessPermissions.PRINT,
        "modify": UserAccessPermissions.MODIFY,
        "extract": UserAccessPermissions.EXTRACT,
        "annotate": UserAccessPermissions.ADD_OR_MODIFY,
        "fill_forms": UserAccessPermissions.FILL_FORM_FIELDS,
        "accessibility": UserAccessPermissions.EXTRACT_TEXT_AND_GRAPHICS,
        "assemble": UserAccessPermissions.ASSEMBLE_DOC,
        "print_high_quality": UserAccessPermissions.PRINT_TO_REPRESENTATION,
    }
    flag = UserAccessPermissions(0)
    for name in permissions:
        flag |= mapping[name]
    return flag


def _clone_to_temporary(
    *,
    reader: Any,
    output: Path,
    encrypt: tuple[str, str, tuple[str, ...]] | None,
) -> Path:
    _PdfReader, PdfWriter = _load_pypdf()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(
        f".{output.stem}.tmp-{secrets.token_hex(4)}.pdf"
    )
    writer = PdfWriter()
    try:
        try:
            writer.append(reader)
            if encrypt is not None:
                user_password, owner_password, permissions = encrypt
                writer.encrypt(
                    user_password,
                    owner_password,
                    algorithm="AES-256",
                    permissions_flag=_permission_flag(permissions),
                )
            with temporary.open("wb") as handle:
                writer.write(handle)
        except Exception as exc:
            raise ToolError(
                f"PDF password operation failed while writing: {type(exc).__name__}"
            ) from exc
        finally:
            writer.close()
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return temporary


def _publish_verified(source: Path, output: Path, temporary: Path, source_hash: str) -> None:
    try:
        if sha256_file(source) != source_hash:
            raise ToolError("PDF password operation changed the source; output rejected.")
        if output.exists():
            raise ToolError(
                "Output file appeared during the PDF password operation; "
                "it was not overwritten."
            )
        os.replace(temporary, output)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def protect_pdf(
    source: Path,
    output: Path,
    *,
    user_password: str,
    owner_password: str,
    current_password: str | None = None,
    permissions: Iterable[str] = DEFAULT_PDF_PERMISSIONS,
) -> PdfSecurityResult:
    """Write a new AES-256 encrypted copy after unlocking the source in memory."""

    _prepare_output(source, output)
    _check_existing_password(current_password)
    _check_new_password(user_password, label="user")
    _check_new_password(owner_password, label="owner")
    if user_password == owner_password:
        raise ToolError("New PDF user and owner passwords must be different.")
    chosen_permissions = _validated_permissions(permissions)

    before = inspect_pdf_security(source, password=current_password)
    if before.encrypted and current_password is None:
        raise ToolError("This PDF is already encrypted; its current password is required.")
    if before.encrypted and before.password_accepted is not True:
        raise ToolError("Incorrect PDF password.")
    if before.pages is None:
        raise ToolError("The source PDF could not be unlocked.")

    reader, source_pages, _password_type = _open_unlocked_reader(
        source, current_password
    )
    temporary = _clone_to_temporary(
        reader=reader,
        output=output,
        encrypt=(user_password, owner_password, chosen_permissions),
    )
    try:
        locked = inspect_pdf_security(temporary)
        user_info = inspect_pdf_security(temporary, password=user_password)
        owner_info = inspect_pdf_security(temporary, password=owner_password)
        wrong_password = f"wrong-{secrets.token_hex(8)}"
        wrong_info = inspect_pdf_security(temporary, password=wrong_password)
        if not locked.encrypted or locked.unlocked:
            raise ToolError("Protected PDF failed verification: it is not locked.")
        if user_info.password_type != "user" or user_info.pages != source_pages:
            raise ToolError("Protected PDF failed user-password verification.")
        if owner_info.password_type != "owner" or owner_info.pages != source_pages:
            raise ToolError("Protected PDF failed owner-password verification.")
        if wrong_info.password_accepted is not False or wrong_info.unlocked:
            raise ToolError("Protected PDF failed wrong-password verification.")
        if user_info.algorithm != "AES-256":
            raise ToolError("Protected PDF failed AES-256 verification.")
        if set(user_info.permissions) != set(chosen_permissions):
            raise ToolError("Protected PDF failed permission verification.")
        if user_info.permissions_valid is False:
            raise ToolError("Protected PDF failed permission-integrity verification.")
        _publish_verified(source, output, temporary, before.sha256)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    final = inspect_pdf_security(output, password=user_password)
    return PdfSecurityResult(
        source_path=source,
        output_path=output,
        operation="protect",
        source_pages=source_pages,
        output_pages=final.pages or source_pages,
        encrypted=True,
        algorithm=final.algorithm,
        permissions=final.permissions,
        size_bytes=final.size_bytes,
        sha256=final.sha256,
    )


def unprotect_pdf(source: Path, output: Path, *, password: str) -> PdfSecurityResult:
    """Write a new unencrypted copy after verifying the supplied password."""

    _prepare_output(source, output)
    _check_existing_password(password)

    before = inspect_pdf_security(source, password=password)
    if not before.encrypted:
        raise ToolError("This PDF is not encrypted; there is no password to remove.")
    if before.password_accepted is not True or before.pages is None:
        raise ToolError("Incorrect PDF password.")

    reader, source_pages, _password_type = _open_unlocked_reader(source, password)
    temporary = _clone_to_temporary(reader=reader, output=output, encrypt=None)
    try:
        after = inspect_pdf_security(temporary)
        if after.encrypted or not after.unlocked:
            raise ToolError("Unprotected PDF failed verification: it remains locked.")
        if after.pages != source_pages:
            raise ToolError("Unprotected PDF failed page-count verification.")
        _publish_verified(source, output, temporary, before.sha256)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    final = inspect_pdf_security(output)
    return PdfSecurityResult(
        source_path=source,
        output_path=output,
        operation="unprotect",
        source_pages=source_pages,
        output_pages=final.pages or source_pages,
        encrypted=False,
        algorithm=None,
        permissions=(),
        size_bytes=final.size_bytes,
        sha256=final.sha256,
    )
