"""Bounded PDF AcroForm inspection and filling.

Phase 25 works only with interactive AcroForm fields already present in a
PDF.  It does not guess fields from page text, OCR a scanned form, handle a
password, sign a document or create a new form design.  Filling writes a new
PDF, never modifies the source, and is reported only after the temporary
output has been reopened and every expected and untouched field value has
been compared with an independent inspection.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ToolError
from .pdf import _load_pypdf, inspect_pdf, sha256_file

MAX_PDF_FORM_PAGES = 500
MAX_PDF_FORM_FIELDS = 200
MAX_PDF_FORM_VALUES = 200
MAX_PDF_FORM_FIELD_NAME_CHARS = 500
MAX_PDF_FORM_VALUE_CHARS = 10_000
MAX_PDF_FORM_TOTAL_VALUE_CHARS = 100_000
MAX_PDF_FORM_OPTIONS = 1_000

_FLAG_READ_ONLY = 1
_FLAG_REQUIRED = 2
_FLAG_NO_EXPORT = 4
_FLAG_TEXT_MULTILINE = 4096
_FLAG_TEXT_PASSWORD = 8192
_FLAG_BUTTON_RADIO = 16384
_FLAG_BUTTON_PUSHBUTTON = 32768
_FLAG_CHOICE_COMBO = 131072
_FLAG_CHOICE_EDIT = 262144
_FLAG_CHOICE_MULTI_SELECT = 2097152

FormValue = str | bool | list[str] | None


@dataclass(frozen=True, slots=True)
class PdfFormField:
    name: str
    field_type: str
    value: FormValue
    options: tuple[str, ...] = ()
    states: tuple[str, ...] = ()
    required: bool = False
    read_only: bool = False
    exportable: bool = True
    multiline: bool = False
    password: bool = False
    radio: bool = False
    pushbutton: bool = False
    combo: bool = False
    editable: bool = False
    multi_select: bool = False
    max_length: int | None = None
    alternate_name: str | None = None
    page: int | None = None
    widget_count: int = 0

    @property
    def checkbox(self) -> bool:
        return (
            self.field_type == "button"
            and not self.radio
            and not self.pushbutton
            and len(self.states) == 1
        )

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.field_type,
            "value": list(self.value) if isinstance(self.value, list) else self.value,
            "options": list(self.options),
            "states": list(self.states),
            "required": self.required,
            "read_only": self.read_only,
            "exportable": self.exportable,
            "multiline": self.multiline,
            "password": self.password,
            "radio": self.radio,
            "pushbutton": self.pushbutton,
            "combo": self.combo,
            "editable": self.editable,
            "multi_select": self.multi_select,
            "max_length": self.max_length,
            "alternate_name": self.alternate_name,
            "page": self.page,
            "widget_count": self.widget_count,
        }


@dataclass(frozen=True, slots=True)
class PdfFormInspection:
    path: Path
    pages: int
    fields: tuple[PdfFormField, ...]
    size_bytes: int
    sha256: str

    @property
    def field_count(self) -> int:
        return len(self.fields)

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields)

    def by_name(self) -> dict[str, PdfFormField]:
        return {field.name: field for field in self.fields}

    def summary(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "field_count": self.field_count,
            "fields": [field.summary() for field in self.fields],
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class PdfFormFillResult:
    source_path: Path
    output_path: Path
    source_pages: int
    field_count: int
    changed_fields: tuple[str, ...]
    values: dict[str, FormValue]
    fields: tuple[PdfFormField, ...]
    size_bytes: int
    sha256: str


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


def _has(container: Any, key: str) -> bool:
    container = _object(container)
    try:
        return key in container
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _items(value: Any) -> list[Any]:
    value = _object(value)
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        return list(value)
    except TypeError:
        return [value]


def _as_text(value: Any) -> str:
    value = _object(value)
    if value is None:
        return ""
    if isinstance(value, bytes):
        for encoding in ("utf-16", "utf-8", "latin-1"):
            try:
                return value.decode(encoding).lstrip("\ufeff")
            except UnicodeDecodeError:
                continue
    return str(value)


def _as_name(value: Any) -> str:
    return _as_text(value).lstrip("/")


def _as_int(value: Any) -> int | None:
    value = _object(value)
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _identity(value: Any) -> tuple[str, int]:
    reference = getattr(value, "indirect_reference", None)
    if reference is not None:
        try:
            return ("indirect", int(reference.idnum))
        except (AttributeError, TypeError, ValueError):
            pass
    return ("object", id(_object(value)))


@dataclass(frozen=True, slots=True)
class _RawField:
    name: str
    values: dict[str, Any]
    states: tuple[str, ...]


def _appearance_states(widget: Any) -> tuple[str, ...]:
    normal = _get(_get(widget, "/AP"), "/N")
    if normal is None:
        return ()
    try:
        keys = list(_object(normal).keys())
    except (AttributeError, KeyError, TypeError, ValueError):
        return ()
    states: list[str] = []
    for key in keys:
        state = _as_name(key)
        if state and state != "Off" and state not in states:
            states.append(state)
    return tuple(states)


def _widget_states(field: Any) -> tuple[str, ...]:
    states: list[str] = []
    candidates = [field, *_items(_get(field, "/Kids"))]
    for candidate in candidates:
        for state in _appearance_states(candidate):
            if state not in states:
                states.append(state)
    return tuple(states)


def _raw_fields(reader: Any) -> tuple[_RawField, ...]:
    root = _object(getattr(reader, "root_object", None))
    if root is None:
        root = _get(getattr(reader, "trailer", {}), "/Root")
    acro_form = _get(root, "/AcroForm")
    if acro_form is None:
        return ()

    fields: list[_RawField] = []
    seen_names: set[str] = set()
    visited: set[tuple[str, int]] = set()
    inheritable = ("/FT", "/V", "/DV", "/Ff", "/Opt", "/MaxLen", "/TU", "/DA")

    def walk(node: Any, prefix: str, inherited: dict[str, Any], depth: int) -> None:
        obj = _object(node)
        if obj is None:
            return
        identity = _identity(node)
        if identity in visited:
            raise ToolError("PDF form contains a cyclic field tree.")
        if depth > 20:
            raise ToolError("PDF form field tree is too deeply nested.")
        visited.add(identity)

        effective = dict(inherited)
        for key in inheritable:
            if _has(obj, key):
                effective[key] = _get(obj, key)
        local_name = _as_text(_get(obj, "/T")).strip()
        qualified = f"{prefix}.{local_name}" if prefix and local_name else local_name or prefix

        kids = _items(_get(obj, "/Kids"))
        field_kids: list[Any] = []
        for kid in kids:
            kid_obj = _object(kid)
            if _has(kid_obj, "/T") or _has(kid_obj, "/Kids") or _has(kid_obj, "/FT"):
                field_kids.append(kid)
        if field_kids:
            for kid in field_kids:
                walk(kid, qualified, effective, depth + 1)
            return

        if not qualified:
            return
        if len(qualified) > MAX_PDF_FORM_FIELD_NAME_CHARS:
            raise ToolError("PDF form contains an excessively long field name.")
        if qualified in seen_names:
            raise ToolError(f"PDF form contains duplicate field name: {qualified}.")
        seen_names.add(qualified)
        states = (
            _widget_states(obj)
            if _as_name(effective.get("/FT")) == "Btn"
            else ()
        )
        fields.append(_RawField(qualified, effective, states))
        if len(fields) > MAX_PDF_FORM_FIELDS:
            raise ToolError(
                f"PDF form has more than {MAX_PDF_FORM_FIELDS} fields."
            )

    for field in _items(_get(acro_form, "/Fields")):
        walk(field, "", {}, 0)
    return tuple(sorted(fields, key=lambda item: item.name.casefold()))


def _qualified_widget_name(widget: Any) -> str:
    parts: list[str] = []
    current = _object(widget)
    seen: set[tuple[str, int]] = set()
    while current is not None:
        identity = _identity(current)
        if identity in seen:
            break
        seen.add(identity)
        name = _as_text(_get(current, "/T")).strip()
        if name:
            parts.append(name)
        current = _object(_get(current, "/Parent"))
    return ".".join(reversed(parts))


def _widget_locations(reader: Any) -> dict[str, tuple[int | None, int]]:
    first_page: dict[str, int] = {}
    counts: dict[str, int] = {}
    for page_index, page in enumerate(reader.pages, start=1):
        for annotation in _items(_get(page, "/Annots")):
            widget = _object(annotation)
            if _as_name(_get(widget, "/Subtype")) != "Widget":
                continue
            name = _qualified_widget_name(widget)
            if not name:
                continue
            first_page.setdefault(name, page_index)
            counts[name] = counts.get(name, 0) + 1
    return {name: (first_page.get(name), count) for name, count in counts.items()}


def _options(raw: _RawField) -> tuple[str, ...]:
    options: list[str] = []
    for item in _items(raw.values.get("/Opt")):
        value = _object(item)
        if isinstance(value, (list, tuple)) or hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
            try:
                pair = list(value)  # type: ignore[arg-type]
                option = _as_text(pair[0]) if pair else ""
            except (AttributeError, KeyError, TypeError, ValueError):
                option = _as_text(value)
        else:
            option = _as_text(value)
        if len(option) > MAX_PDF_FORM_VALUE_CHARS:
            raise ToolError(f"PDF form field {raw.name!r} has an excessively long option.")
        if option not in options:
            options.append(option)
        if len(options) > MAX_PDF_FORM_OPTIONS:
            raise ToolError(f"PDF form field {raw.name!r} has too many options.")
    return tuple(options)


def _field_type(raw_name: str) -> str:
    return {
        "Tx": "text",
        "Btn": "button",
        "Ch": "choice",
        "Sig": "signature",
    }.get(raw_name, "unknown")


def _normalize_value(
    *,
    raw: _RawField,
    field_type: str,
    flags: int,
    options: tuple[str, ...],
    states: tuple[str, ...],
) -> FormValue:
    value = _object(raw.values.get("/V"))
    if field_type == "button":
        state = _as_name(value)
        if not state or state == "Off":
            return False if len(states) == 1 and not (flags & _FLAG_BUTTON_RADIO) else None
        if len(states) == 1 and not (flags & _FLAG_BUTTON_RADIO):
            return state == states[0]
        return state
    if field_type == "choice" and (flags & _FLAG_CHOICE_MULTI_SELECT):
        if value is None:
            return []
        values = [_as_text(item) for item in _items(value)]
        return values
    if field_type == "choice" and isinstance(value, int) and not isinstance(value, bool):
        return options[value] if 0 <= value < len(options) else str(value)
    if value is None:
        return "" if field_type in {"text", "choice"} else None
    if isinstance(value, (list, tuple)):
        return [_as_text(item) for item in _items(value)]
    return _as_text(value)


def inspect_pdf_form(path: Path) -> PdfFormInspection:
    """Inspect existing AcroForm fields without modifying the PDF."""

    info = inspect_pdf(path)
    if info.encrypted or info.pages is None:
        raise ToolError("Encrypted PDFs are not supported without a password.")
    if info.pages > MAX_PDF_FORM_PAGES:
        raise ToolError(
            f"PDF form inspection is limited to {MAX_PDF_FORM_PAGES} pages."
        )
    PdfReader, _PdfWriter = _load_pypdf()
    try:
        reader = PdfReader(str(path))
        raw_fields = _raw_fields(reader)
        locations = _widget_locations(reader)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(
            f"Could not inspect PDF form '{path.name}': {type(exc).__name__}"
        ) from exc

    fields: list[PdfFormField] = []
    for raw in raw_fields:
        flags = _as_int(raw.values.get("/Ff")) or 0
        field_type = _field_type(_as_name(raw.values.get("/FT")))
        options = _options(raw) if field_type == "choice" else ()
        page, widget_count = locations.get(raw.name, (None, 0))
        max_length = _as_int(raw.values.get("/MaxLen"))
        if max_length is not None and max_length <= 0:
            max_length = None
        fields.append(
            PdfFormField(
                name=raw.name,
                field_type=field_type,
                value=_normalize_value(
                    raw=raw,
                    field_type=field_type,
                    flags=flags,
                    options=options,
                    states=raw.states,
                ),
                options=options,
                states=raw.states,
                required=bool(flags & _FLAG_REQUIRED),
                read_only=bool(flags & _FLAG_READ_ONLY),
                exportable=not bool(flags & _FLAG_NO_EXPORT),
                multiline=field_type == "text" and bool(flags & _FLAG_TEXT_MULTILINE),
                password=field_type == "text" and bool(flags & _FLAG_TEXT_PASSWORD),
                radio=field_type == "button" and bool(flags & _FLAG_BUTTON_RADIO),
                pushbutton=(
                    field_type == "button" and bool(flags & _FLAG_BUTTON_PUSHBUTTON)
                ),
                combo=field_type == "choice" and bool(flags & _FLAG_CHOICE_COMBO),
                editable=field_type == "choice" and bool(flags & _FLAG_CHOICE_EDIT),
                multi_select=(
                    field_type == "choice"
                    and bool(flags & _FLAG_CHOICE_MULTI_SELECT)
                ),
                max_length=max_length,
                alternate_name=(
                    _as_text(raw.values.get("/TU")).strip() or None
                ),
                page=page,
                widget_count=widget_count,
            )
        )
    return PdfFormInspection(
        path=path,
        pages=info.pages,
        fields=tuple(fields),
        size_bytes=info.size_bytes,
        sha256=sha256_file(path),
    )


def _check_text(name: str, value: str, *, max_length: int | None) -> str:
    if "\x00" in value:
        raise ToolError(f"PDF form value for {name!r} contains a NUL character.")
    if len(value) > MAX_PDF_FORM_VALUE_CHARS:
        raise ToolError(
            f"PDF form value for {name!r} exceeds "
            f"{MAX_PDF_FORM_VALUE_CHARS} characters."
        )
    if max_length is not None and len(value) > max_length:
        raise ToolError(
            f"PDF form value for {name!r} exceeds its {max_length}-character limit."
        )
    return value


def _validate_values(
    inspection: PdfFormInspection,
    values: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, FormValue]]:
    if not isinstance(values, Mapping) or not values:
        raise ToolError("At least one PDF form field value is required.")
    if len(values) > MAX_PDF_FORM_VALUES:
        raise ToolError(f"At most {MAX_PDF_FORM_VALUES} PDF form values are allowed.")
    fields = inspection.by_name()
    converted: dict[str, Any] = {}
    expected: dict[str, FormValue] = {}
    total_chars = 0

    for raw_name, raw_value in values.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ToolError("PDF form field names must be non-empty strings.")
        name = raw_name.strip()
        if len(name) > MAX_PDF_FORM_FIELD_NAME_CHARS:
            raise ToolError("PDF form field name is too long.")
        field = fields.get(name)
        if field is None:
            raise ToolError(f"PDF form field not found: {name!r}.")
        if field.read_only:
            raise ToolError(f"PDF form field {name!r} is read-only.")
        if field.password:
            raise ToolError(
                f"PDF form field {name!r} is a password field and cannot be "
                "filled through recorded tool arguments."
            )
        if field.field_type in {"signature", "unknown"} or field.pushbutton:
            raise ToolError(f"PDF form field {name!r} cannot be filled safely.")

        if field.field_type == "text":
            if not isinstance(raw_value, str):
                raise ToolError(f"PDF text field {name!r} requires a string value.")
            value = _check_text(name, raw_value, max_length=field.max_length)
            if field.required and not value:
                raise ToolError(f"Required PDF form field {name!r} cannot be empty.")
            converted[name] = value
            expected[name] = value
            total_chars += len(value)
        elif field.field_type == "choice" and field.multi_select:
            if isinstance(raw_value, str):
                raw_value = [item for item in raw_value.split("|") if item]
            if not isinstance(raw_value, (list, tuple)):
                raise ToolError(
                    f"Multi-select PDF field {name!r} requires a list of strings."
                )
            chosen: list[str] = []
            for item in raw_value:
                if not isinstance(item, str):
                    raise ToolError(
                        f"Multi-select PDF field {name!r} requires string values."
                    )
                value = _check_text(name, item, max_length=None)
                if value not in field.options:
                    raise ToolError(
                        f"Value {value!r} is not an option for PDF field {name!r}."
                    )
                if value not in chosen:
                    chosen.append(value)
                total_chars += len(value)
            if field.required and not chosen:
                raise ToolError(f"Required PDF form field {name!r} cannot be empty.")
            converted[name] = chosen
            expected[name] = list(chosen)
        elif field.field_type == "choice":
            if not isinstance(raw_value, str):
                raise ToolError(f"PDF choice field {name!r} requires a string value.")
            value = _check_text(name, raw_value, max_length=None)
            if field.required and not value:
                raise ToolError(f"Required PDF form field {name!r} cannot be empty.")
            if value and value not in field.options and not field.editable:
                raise ToolError(
                    f"Value {value!r} is not an option for PDF field {name!r}."
                )
            converted[name] = value
            expected[name] = value
            total_chars += len(value)
        elif field.field_type == "button" and field.checkbox:
            if isinstance(raw_value, str):
                normalized = raw_value.strip().lower()
                if normalized in {"true", "1", "yes", "on"}:
                    raw_value = True
                elif normalized in {"false", "0", "no", "off"}:
                    raw_value = False
            if not isinstance(raw_value, bool):
                raise ToolError(
                    f"PDF checkbox field {name!r} requires true or false."
                )
            if field.required and raw_value is False:
                raise ToolError(
                    f"Required PDF checkbox field {name!r} cannot be unchecked."
                )
            converted[name] = f"/{field.states[0]}" if raw_value else "/Off"
            expected[name] = raw_value
        elif field.field_type == "button":
            if raw_value is None or raw_value in ("", "Off", "/Off"):
                if field.required:
                    raise ToolError(
                        f"Required PDF form field {name!r} cannot be empty."
                    )
                converted[name] = "/Off"
                expected[name] = None
            elif isinstance(raw_value, str):
                state = raw_value.strip().lstrip("/")
                if state not in field.states:
                    raise ToolError(
                        f"State {state!r} is not available for PDF field {name!r}."
                    )
                converted[name] = f"/{state}"
                expected[name] = state
                total_chars += len(state)
            else:
                raise ToolError(
                    f"PDF button field {name!r} requires a state string or null."
                )
        else:  # pragma: no cover - guarded above
            raise ToolError(f"PDF form field {name!r} cannot be filled safely.")

        if total_chars > MAX_PDF_FORM_TOTAL_VALUE_CHARS:
            raise ToolError(
                "PDF form values exceed "
                f"{MAX_PDF_FORM_TOTAL_VALUE_CHARS} total characters."
            )
    return converted, expected


def expected_pdf_form_values(
    inspection: PdfFormInspection,
    values: Mapping[str, Any],
) -> dict[str, FormValue]:
    """Return the normalized values a verified fill should produce."""

    return _validate_values(inspection, values)[1]


def fill_pdf_form(
    source: Path,
    output: Path,
    values: Mapping[str, Any],
) -> PdfFormFillResult:
    """Fill selected AcroForm fields into a new verified PDF."""

    if output.suffix.lower() != ".pdf":
        raise ToolError("The filled PDF output must be a .pdf file.")
    if output.exists():
        raise ToolError(
            "Output file already exists. PDF tools never overwrite; choose a new output path."
        )
    if source.expanduser().resolve() == output.expanduser().resolve():
        raise ToolError("The filled PDF output must not be the source PDF.")

    before = inspect_pdf_form(source)
    if not before.fields:
        raise ToolError("This PDF has no interactive AcroForm fields to fill.")
    converted, expected = _validate_values(before, values)
    source_hash = before.sha256

    _PdfReader, PdfWriter = _load_pypdf()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(
        f".{output.stem}.tmp-{secrets.token_hex(4)}.pdf"
    )
    writer = PdfWriter()
    try:
        try:
            writer.append(str(source))
            writer.update_page_form_field_values(
                None,
                converted,
                auto_regenerate=True,
                flatten=False,
            )
            with temporary.open("wb") as handle:
                writer.write(handle)
        except Exception as exc:
            raise ToolError(
                f"PDF form fill failed while writing the output: {type(exc).__name__}"
            ) from exc
        finally:
            writer.close()

        after = inspect_pdf_form(temporary)
        if after.pages != before.pages:
            raise ToolError("Filled PDF failed verification: page count changed.")
        if after.field_names != before.field_names:
            raise ToolError("Filled PDF failed verification: field list changed.")
        before_by_name = before.by_name()
        after_by_name = after.by_name()
        for name, field in after_by_name.items():
            wanted = expected.get(name, before_by_name[name].value)
            if field.value != wanted:
                raise ToolError(
                    f"Filled PDF failed verification for field {name!r}."
                )
        if sha256_file(source) != source_hash:
            raise ToolError("PDF form fill changed the source PDF; output rejected.")
        if output.exists():
            raise ToolError(
                "Output file appeared during PDF form filling; it was not overwritten."
            )
        os.replace(temporary, output)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    final = inspect_pdf_form(output)
    return PdfFormFillResult(
        source_path=source,
        output_path=output,
        source_pages=final.pages,
        field_count=final.field_count,
        changed_fields=tuple(expected),
        values=dict(expected),
        fields=final.fields,
        size_bytes=final.size_bytes,
        sha256=final.sha256,
    )
