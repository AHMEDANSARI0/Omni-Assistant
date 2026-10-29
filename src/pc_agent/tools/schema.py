"""A deliberately small, strict schema validator for registered tools.

Tool schemas use a JSON-Schema-like shape, but this validator only implements
the checks Phase 1B needs: object shape, required/unknown fields, primitive
types, enums, arrays, string length, numeric bounds and array size. Unknown fields
are rejected so a caller can never smuggle an undeclared option into a tool.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import ToolValidationError


def validate_object(schema: Mapping[str, Any], value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ToolValidationError(f"{label} must be an object.")
    result = dict(value)
    properties: Mapping[str, Any] = schema.get("properties", {})
    required: list[str] = list(schema.get("required", []))

    missing = [name for name in required if name not in result]
    if missing:
        raise ToolValidationError(f"{label} is missing required field(s): {', '.join(missing)}.")

    unknown = [name for name in result if name not in properties]
    if unknown:
        raise ToolValidationError(f"{label} contains unknown field(s): {', '.join(unknown)}.")

    for name, item in result.items():
        _validate_value(properties[name], item, f"{label}.{name}")
    return result


def _validate_value(schema: Any, value: Any, path: str) -> None:
    if not isinstance(schema, Mapping):
        return

    expected = schema.get("type")
    if expected is not None:
        expected_types = expected if isinstance(expected, list) else [expected]
        if not any(_matches_type(kind, value) for kind in expected_types):
            readable = " or ".join(str(kind) for kind in expected_types)
            raise ToolValidationError(f"{path} must be {readable}.")

    allowed_values = schema.get("enum")
    if allowed_values is not None and (
        not isinstance(allowed_values, list)
        or not any(
            value == allowed and type(value) is type(allowed)
            for allowed in allowed_values
        )
    ):
        raise ToolValidationError(f"{path} is not an allowed value.")

    if isinstance(value, str):
        min_length = schema.get("minLength")
        if min_length is not None and len(value) < int(min_length):
            raise ToolValidationError(f"{path} is too short.")
        max_length = schema.get("maxLength")
        if max_length is not None and len(value) > int(max_length):
            raise ToolValidationError(f"{path} is too long.")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if minimum is not None and value < minimum:
            raise ToolValidationError(f"{path} is below its allowed minimum.")
        maximum = schema.get("maximum")
        if maximum is not None and value > maximum:
            raise ToolValidationError(f"{path} exceeds its allowed maximum.")

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if min_items is not None and len(value) < int(min_items):
            raise ToolValidationError(f"{path} contains too few items.")
        max_items = schema.get("maxItems")
        if max_items is not None and len(value) > int(max_items):
            raise ToolValidationError(f"{path} contains too many items.")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                _validate_value(item_schema, item, f"{path}[{index}]")

    if isinstance(value, Mapping) and schema.get("type") == "object":
        validate_object(schema, value, label=path)


def _matches_type(kind: Any, value: Any) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, Mapping)
    if kind == "null":
        return value is None
    return False
