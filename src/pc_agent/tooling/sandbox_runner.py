"""Trusted stdlib-only harness copied into each dynamic-tool sandbox."""

from __future__ import annotations

import builtins
import copy
import importlib
import json
import sys

MAX_INPUT_BYTES = 1_000_000
MAX_OUTPUT_BYTES = 1_000_000

ALLOWED_IMPORTS = {
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

SAFE_BUILTIN_NAMES = (
    "ArithmeticError",
    "AssertionError",
    "AttributeError",
    "Exception",
    "IndexError",
    "KeyError",
    "LookupError",
    "OverflowError",
    "RuntimeError",
    "StopIteration",
    "TypeError",
    "ValueError",
    "ZeroDivisionError",
    "abs",
    "all",
    "any",
    "bool",
    "chr",
    "dict",
    "divmod",
    "enumerate",
    "filter",
    "float",
    "format",
    "frozenset",
    "hash",
    "int",
    "isinstance",
    "issubclass",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "ord",
    "pow",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "slice",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
)


def _guarded_import(name, globals_dict=None, locals_dict=None, fromlist=(), level=0):
    if level != 0:
        raise ImportError("Relative imports are not allowed in dynamic tools")
    root = name.split(".", 1)[0]
    if root not in ALLOWED_IMPORTS:
        raise ImportError("Import '" + name + "' is not allowed in dynamic tools")
    module = importlib.import_module(name)
    if fromlist:
        for item in fromlist:
            if item.startswith("_"):
                raise ImportError("Private imported names are not allowed")
        return module
    return sys.modules[root]


def _safe_builtins():
    values = {}
    for name in SAFE_BUILTIN_NAMES:
        if hasattr(builtins, name):
            values[name] = getattr(builtins, name)
    values["__build_class__"] = None
    values["__import__"] = _guarded_import
    return values


def _fail(message):
    print(json.dumps({"ok": False, "error": message}, allow_nan=False))
    return 2


def main():
    raw = sys.stdin.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        return _fail("Dynamic tool input exceeds the 1 MB limit")

    try:
        arguments = json.loads(raw)
    except (TypeError, ValueError) as exc:
        return _fail("Dynamic tool input is not valid JSON: " + str(exc))
    if not isinstance(arguments, dict):
        return _fail("Dynamic tool input must be a JSON object")

    try:
        with open("candidate.py", encoding="utf-8") as handle:
            source = handle.read()
    except OSError as exc:
        return _fail("Could not read candidate source: " + str(exc))

    namespace = {
        "__builtins__": _safe_builtins(),
        "__name__": "dynamic_tool_candidate",
    }
    try:
        code = compile(source, "candidate.py", "exec")
        exec(code, namespace)  # noqa: S102 - candidate was statically validated
        run = namespace.get("run")
        if run is None or not callable(run):
            return _fail("Candidate did not define run(arguments)")
        result = run(copy.deepcopy(arguments))
    except BaseException as exc:  # noqa: BLE001 - last process boundary
        return _fail(type(exc).__name__ + ": " + str(exc)[:500])

    if not isinstance(result, dict):
        return _fail("Dynamic tool run(arguments) must return a JSON object")

    try:
        payload = json.dumps({"ok": True, "result": result}, allow_nan=False)
    except (TypeError, ValueError) as exc:
        return _fail("Dynamic tool result is not JSON-serializable: " + str(exc))
    if len(payload) > MAX_OUTPUT_BYTES:
        return _fail("Dynamic tool output exceeds the 1 MB limit")

    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
