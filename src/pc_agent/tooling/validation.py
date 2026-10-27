"""Static validation for untrusted dynamic-tool proposals.

This is one layer, not a claim of an operating-system sandbox. Candidates are
additionally pure-compute only, run in a separate Python process with a
minimal environment, and receive no filesystem/network helper.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..errors import ToolBuilderError, ToolValidationError
from ..tools.schema import validate_object
from .models import ToolProposal

ALLOWED_IMPORTS = frozenset(
    {
        "base64",
        "binascii",
        "bisect",
        "collections",
        "datetime",
        "decimal",
        "fractions",
        "functools",
        "hashlib",
        "heapq",
        "itertools",
        "json",
        "math",
        "re",
        "statistics",
        "textwrap",
        "unicodedata",
    }
)

_MAX_SOURCE_BYTES = 30_000
_MAX_SOURCE_LINES = 1_000
_MAX_AST_NODES = 2_500
_TOOL_ID_RE = re.compile(r"^custom\.[a-z][a-z0-9_.-]*$")
_PROPERTY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODULE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_SCHEMA_TYPES = frozenset({"string", "integer", "number", "boolean", "array", "object", "null"})
_SCHEMA_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "items",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
    }
)
_BANNED_NAMES = frozenset(
    {
        "__import__",
        "breakpoint",
        "bytearray",
        "callable",
        "compile",
        "delattr",
        "dir",
        "eval",
        "exec",
        "getattr",
        "globals",
        "help",
        "id",
        "input",
        "locals",
        "memoryview",
        "object",
        "open",
        "print",
        "setattr",
        "super",
        "type",
        "vars",
    }
)
_BANNED_NODE_TYPES = {
    "AsyncFunctionDef": "async functions are not allowed",
    "AsyncFor": "async loops are not allowed",
    "AsyncWith": "async context managers are not allowed",
    "Await": "await is not allowed",
    "ClassDef": "classes are not allowed in dynamic tools",
    "Delete": "del statements are not allowed",
    "Global": "global statements are not allowed",
    "Match": "match statements are not allowed in dynamic tools",
    "Nonlocal": "nonlocal statements are not allowed",
    "TryStar": "exception groups are not allowed",
    "TypeAlias": "type alias statements are not allowed",
    "With": "context managers are not allowed in dynamic tools",
    "Yield": "generators are not allowed in dynamic tools",
    "YieldFrom": "generators are not allowed in dynamic tools",
}


@dataclass(frozen=True, slots=True)
class StaticIssue:
    severity: str
    code: str
    message: str
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "line": self.line,
        }


@dataclass(frozen=True, slots=True)
class StaticReport:
    passed: bool
    issues: tuple[StaticIssue, ...]
    imports: tuple[str, ...]
    node_count: int
    line_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "issues": [issue.to_dict() for issue in self.issues],
            "imports": list(self.imports),
            "node_count": self.node_count,
            "line_count": self.line_count,
        }


def prepare_proposal(
    proposal: ToolProposal, *, builtin_tool_ids: set[str] | frozenset[str]
) -> tuple[ToolProposal, StaticReport]:
    """Validate metadata/schemas/tests and normalize declared dependencies."""
    if not _TOOL_ID_RE.fullmatch(proposal.tool_id):
        raise ToolBuilderError(
            "Dynamic tool id must start with 'custom.' and use lowercase letters, digits, '_', '-' or '.'."
        )
    if proposal.tool_id in builtin_tool_ids:
        raise ToolBuilderError("Dynamic tool id conflicts with a reviewed built-in tool.")
    if len(proposal.name) > 120:
        raise ToolBuilderError("Tool name must be at most 120 characters.")
    if len(proposal.description) > 500:
        raise ToolBuilderError("Tool description must be at most 500 characters.")
    _parse_version(proposal.version)
    if proposal.timeout_seconds < 1 or proposal.timeout_seconds > 30:
        raise ToolBuilderError("Dynamic tool timeout must be between 1 and 30 seconds.")

    validate_schema_definition(proposal.input_schema, label="input_schema")
    validate_schema_definition(proposal.output_schema, label="output_schema")

    report = validate_source(proposal.normalized_source)
    declared = _normalized_declared_dependencies(proposal.dependencies)
    if declared and set(declared) != set(report.imports):
        raise ToolBuilderError(
            "Declared dependencies must exactly match the source imports: "
            f"declared={list(declared)}, imports={list(report.imports)}."
        )
    normalized = proposal.with_dependencies(report.imports)

    seen_names: set[str] = set()
    for test in normalized.test_cases:
        if test.name in seen_names:
            raise ToolBuilderError("Tool test case names must be unique.")
        seen_names.add(test.name)
        try:
            validate_object(normalized.input_schema, test.arguments, label="test arguments")
            validate_object(
                normalized.output_schema, test.expected_output, label="test expected_output"
            )
        except ToolValidationError as exc:
            raise ToolBuilderError(f"Invalid test case '{test.name}': {exc}") from exc
        if len(json.dumps(test.to_dict())) > 100_000:
            raise ToolBuilderError("A tool test case exceeds the 100 KB limit.")

    return normalized, report


def validate_schema_definition(schema: Mapping[str, Any], *, label: str) -> None:
    counter = {"properties": 0}
    _validate_schema_node(schema, label, depth=0, root=True, counter=counter)


def _validate_schema_node(
    schema: Any,
    path: str,
    *,
    depth: int,
    root: bool,
    counter: dict[str, int],
) -> None:
    if depth > 4:
        raise ToolBuilderError(f"{path} exceeds the maximum schema depth.")
    if not isinstance(schema, Mapping):
        raise ToolBuilderError(f"{path} must be a schema object.")
    unknown = set(schema) - _SCHEMA_KEYS
    if unknown:
        raise ToolBuilderError(
            f"{path} contains unsupported schema key(s): {', '.join(sorted(unknown))}."
        )

    raw_type = schema.get("type")
    if raw_type is None:
        raise ToolBuilderError(f"{path}.type is required.")
    types = raw_type if isinstance(raw_type, list) else [raw_type]
    if not types or any(not isinstance(item, str) or item not in _SCHEMA_TYPES for item in types):
        raise ToolBuilderError(f"{path}.type contains an unsupported JSON type.")
    if root and types != ["object"]:
        raise ToolBuilderError(f"{path}.type must be 'object' at the top level.")
    if "object" in types and types != ["object"]:
        raise ToolBuilderError(f"{path}.type must be the single value 'object' for object fields.")
    if ("properties" in schema or "required" in schema) and "object" not in types:
        raise ToolBuilderError(f"{path} may use properties/required only for object fields.")
    if "items" in schema and "array" not in types:
        raise ToolBuilderError(f"{path} may use items only for array fields.")

    if "object" in types:
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            raise ToolBuilderError(f"{path}.properties must be an object.")
        if len(properties) > 50:
            raise ToolBuilderError(f"{path}.properties contains too many fields.")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise ToolBuilderError(f"{path}.required must be an array of field names.")
        if len(set(required)) != len(required) or not set(required).issubset(properties):
            raise ToolBuilderError(f"{path}.required must contain unique declared properties.")
        for name, subschema in properties.items():
            if not isinstance(name, str) or not _PROPERTY_RE.fullmatch(name):
                raise ToolBuilderError(f"{path}.properties contains an invalid field name.")
            counter["properties"] += 1
            if counter["properties"] > 100:
                raise ToolBuilderError("A dynamic tool schema may contain at most 100 fields.")
            _validate_schema_node(
                subschema, f"{path}.{name}", depth=depth + 1, root=False, counter=counter
            )

    if "array" in types and "items" not in schema:
        raise ToolBuilderError(f"{path}.items is required for array fields.")
    if "items" in schema:
        _validate_schema_node(
            schema["items"], f"{path}.items", depth=depth + 1, root=False, counter=counter
        )

    for numeric_key in ("minimum", "maximum"):
        value = schema.get(numeric_key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ToolBuilderError(f"{path}.{numeric_key} must be numeric.")
    for count_key in ("minLength", "maxLength", "minItems", "maxItems"):
        value = schema.get(count_key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 1_000_000
        ):
            raise ToolBuilderError(f"{path}.{count_key} must be between 0 and 1000000.")


def validate_source(source: str) -> StaticReport:
    issues: list[StaticIssue] = []
    imports: set[str] = set()
    line_count = len(source.splitlines())

    if len(source.encode("utf-8")) > _MAX_SOURCE_BYTES:
        issues.append(_issue("source_too_large", "Source exceeds the 30 KB limit."))
    if line_count > _MAX_SOURCE_LINES:
        issues.append(_issue("source_too_many_lines", "Source exceeds the 1000-line limit."))

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        issues.append(
            _issue("syntax_error", f"Python syntax error: {exc.msg}", exc.lineno)
        )
        return StaticReport(False, tuple(issues), (), 0, line_count)

    nodes = list(ast.walk(tree))
    if len(nodes) > _MAX_AST_NODES:
        issues.append(_issue("ast_too_large", "Source exceeds the static node limit."))

    run_functions: list[ast.FunctionDef] = []
    top_level_import_ids: set[int] = set()

    for index, node in enumerate(tree.body):
        if index == 0 and _is_docstring(node):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            top_level_import_ids.add(id(node))
            _validate_import(node, issues, imports)
        elif isinstance(node, ast.FunctionDef):
            if node.name == "run":
                run_functions.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if not _is_constant_assignment(node):
                issues.append(
                    _issue(
                        "top_level_non_constant",
                        "Only constant assignments are allowed at module level.",
                        node.lineno,
                    )
                )
        else:
            issues.append(
                _issue(
                    "top_level_statement",
                    "Only imports, constants and function definitions are allowed at module level.",
                    getattr(node, "lineno", None),
                )
            )

    if len(run_functions) != 1:
        issues.append(
            _issue("run_function", "Source must define exactly one top-level run(arguments) function.")
        )
    elif not _has_single_argument(run_functions[0]):
        issues.append(
            _issue(
                "run_signature",
                "run must accept exactly one positional arguments object.",
                run_functions[0].lineno,
            )
        )

    for node in nodes:
        _validate_node(node, issues, top_level_import_ids)

    passed = not any(issue.severity == "error" for issue in issues)
    return StaticReport(
        passed=passed,
        issues=tuple(issues),
        imports=tuple(sorted(imports)),
        node_count=len(nodes),
        line_count=line_count,
    )


def _validate_node(
    node: ast.AST, issues: list[StaticIssue], top_level_import_ids: set[int]
) -> None:
    node_type = type(node).__name__
    line = getattr(node, "lineno", None)
    if node_type in _BANNED_NODE_TYPES:
        issues.append(_issue("banned_construct", _BANNED_NODE_TYPES[node_type], line))
        return

    if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in top_level_import_ids:
        issues.append(_issue("nested_import", "Imports are only allowed at module level.", line))

    if isinstance(node, ast.FunctionDef):
        if node.decorator_list:
            issues.append(_issue("decorator", "Decorators are not allowed.", line))
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None and not _is_constant_expr(default):
                issues.append(
                    _issue("default_value", "Function defaults must be constants.", line)
                )
        for argument in _all_arguments(node.args):
            if "__" in argument.arg:
                issues.append(_issue("dunder_name", "Dunder names are not allowed.", line))
            if argument.annotation is not None and not _is_simple_annotation(argument.annotation):
                issues.append(
                    _issue("annotation", "Only simple type annotations are allowed.", line)
                )
        if node.returns is not None and not _is_simple_annotation(node.returns):
            issues.append(_issue("annotation", "Only simple return annotations are allowed.", line))

    if isinstance(node, ast.Lambda):
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None and not _is_constant_expr(default):
                issues.append(
                    _issue("default_value", "Lambda defaults must be constants.", line)
                )

    if isinstance(node, ast.Name):
        if node.id in _BANNED_NAMES:
            issues.append(_issue("banned_name", f"Builtin/name '{node.id}' is not allowed.", line))
        if "__" in node.id:
            issues.append(_issue("dunder_name", "Dunder names are not allowed.", line))

    if isinstance(node, ast.Attribute):
        if node.attr.startswith("_") or "__" in node.attr:
            issues.append(
                _issue("private_attribute", "Private/dunder attributes are not allowed.", line)
            )
        if node.attr == "format":
            issues.append(
                _issue(
                    "dynamic_format",
                    "Dynamic format-string attribute access is not allowed; use an f-string.",
                    line,
                )
            )

    if isinstance(node, ast.Constant) and isinstance(node.value, str) and "__" in node.value:
        issues.append(
            _issue("dunder_string", "Strings containing '__' are not allowed.", line)
        )

    if isinstance(node, ast.While):
        issues.append(
            StaticIssue(
                "warning",
                "while_loop",
                "While loop relies on the subprocess timeout as its hard stop.",
                line,
            )
        )


def _validate_import(
    node: ast.Import | ast.ImportFrom, issues: list[StaticIssue], imports: set[str]
) -> None:
    if isinstance(node, ast.Import):
        modules = [alias.name for alias in node.names]
        aliases = node.names
    else:
        if node.level:
            issues.append(_issue("relative_import", "Relative imports are not allowed.", node.lineno))
            return
        if not node.module:
            issues.append(_issue("invalid_import", "Import module is missing.", node.lineno))
            return
        modules = [node.module]
        aliases = node.names
        if any(alias.name == "*" for alias in aliases):
            issues.append(_issue("star_import", "Star imports are not allowed.", node.lineno))

    for module in modules:
        if not _MODULE_RE.fullmatch(module):
            issues.append(_issue("invalid_import", f"Invalid import '{module}'.", node.lineno))
            continue
        top_level = module.split(".", 1)[0]
        if top_level not in ALLOWED_IMPORTS:
            issues.append(
                _issue(
                    "import_not_allowed",
                    f"Import '{module}' is not in the dynamic-tool allow-list.",
                    node.lineno,
                )
            )
        else:
            imports.add(top_level)

    for alias in aliases:
        if alias.asname and (alias.asname.startswith("_") or "__" in alias.asname):
            issues.append(_issue("import_alias", "Private import aliases are not allowed.", node.lineno))
        if isinstance(node, ast.ImportFrom) and alias.name.startswith("_"):
            issues.append(
                _issue("private_import", "Private imported names are not allowed.", node.lineno)
            )


def _issue(code: str, message: str, line: int | None = None) -> StaticIssue:
    return StaticIssue("error", code, message, line)


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _is_constant_assignment(node: ast.Assign | ast.AnnAssign) -> bool:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    if not all(isinstance(target, ast.Name) for target in targets):
        return False
    return node.value is not None and _is_constant_expr(node.value)


def _is_constant_expr(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, int, float, bool)) or node.value is None
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_constant_expr(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            key is not None and _is_constant_expr(key) and _is_constant_expr(value)
            for key, value in zip(node.keys, node.values, strict=False)
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _is_constant_expr(node.operand)
    return False


def _is_simple_annotation(node: ast.expr) -> bool:
    if isinstance(node, (ast.Name, ast.Attribute, ast.Constant)):
        return True
    if isinstance(node, ast.Subscript):
        return _is_simple_annotation(node.value) and _is_simple_annotation(node.slice)
    if isinstance(node, ast.Tuple):
        return all(_is_simple_annotation(item) for item in node.elts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_simple_annotation(node.left) and _is_simple_annotation(node.right)
    return False


def _all_arguments(args: ast.arguments) -> list[ast.arg]:
    result = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg:
        result.append(args.vararg)
    if args.kwarg:
        result.append(args.kwarg)
    return result


def _has_single_argument(node: ast.FunctionDef) -> bool:
    args = node.args
    return (
        len(args.posonlyargs) + len(args.args) == 1
        and not args.kwonlyargs
        and args.vararg is None
        and args.kwarg is None
    )


def _normalized_declared_dependencies(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: set[str] = set()
    for value in values:
        module = value.strip()
        if not module or not _MODULE_RE.fullmatch(module):
            raise ToolBuilderError(f"Invalid declared dependency '{value}'.")
        top_level = module.split(".", 1)[0]
        if top_level not in ALLOWED_IMPORTS:
            raise ToolBuilderError(
                "Dynamic tools may use only the allow-listed Python standard-library modules; "
                "pip dependencies are not permitted in Phase 2."
            )
        normalized.add(top_level)
    return tuple(sorted(normalized))


def _parse_version(version: str) -> tuple[int, ...]:
    try:
        parts = tuple(int(part) for part in version.split("."))
    except ValueError as exc:
        raise ToolBuilderError("Tool version must be numeric and dot-separated.") from exc
    if len(parts) != 3 or any(part < 0 for part in parts):
        raise ToolBuilderError("Dynamic tool version must use MAJOR.MINOR.PATCH format.")
    return parts


def next_patch_version(version: str) -> str:
    major, minor, patch = _parse_version(version)
    return f"{major}.{minor}.{patch + 1}"
