"""Sandboxed plugin worker: one tool call, one short-lived process.

Protocol: one JSON object on stdin, one JSON object on stdout. The worker is
started by :class:`pc_agent.plugins.service.PluginService` through
:func:`pc_agent.plugins.sandbox.run_sandboxed`, so the operating system limits
are already in place before this module runs.

Inside the process the worker:

* installs an audit hook that refuses sockets, subprocesses, ``os.system``,
  ``os.exec*``, ``ctypes`` loading and any *write* outside the paths the
  plugin's capabilities allow;
* imports only the plugin's own entry file, from its installed directory;
* calls one declared tool with type-checked keyword arguments;
* returns a JSON-safe result, or a structured error - never a traceback that
  leaks local paths and never unstructured text.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any

MAX_RESULT_BYTES = 200_000

_DENY_PREFIXES = ("socket.", "subprocess.", "os.exec", "os.spawn", "os.posix_spawn", "pty.spawn")
_DENY_EVENTS = {"os.system", "ctypes.dlopen", "ctypes.dlsym", "urllib.Request", "ftplib.connect"}


def _write_roots(request: dict[str, Any]) -> tuple[Path, ...]:
    roots: list[Path] = []
    sandbox = str(request.get("sandbox_dir") or "").strip()
    if sandbox:
        roots.append(Path(sandbox))
    workspace = str(request.get("workspace") or "").strip()
    capabilities = {str(item) for item in request.get("capabilities") or ()}
    if workspace and "write_workspace" in capabilities:
        roots.append(Path(workspace))
    return tuple(root.resolve() for root in roots if root)


def _install_audit_hook(request: dict[str, Any]) -> None:
    allow_network = bool(request.get("allow_network"))
    allow_subprocess = bool(request.get("allow_subprocess"))
    roots = _write_roots(request)

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if event == "open":
            path, mode = (args + (None, None))[:2]
            write_intent = isinstance(mode, str) and any(
                flag in mode for flag in ("w", "a", "x", "+")
            )
            if not write_intent:
                return
            try:
                target = Path(str(path)).resolve()
            except (OSError, ValueError):
                return
            if any(target == root or root in target.parents for root in roots):
                return
            raise PermissionError(
                "plugin write refused: path is outside the declared capability roots"
            )
            return
        if event.startswith("socket.") and not allow_network:
            raise PermissionError(
                f"plugin network access refused ({event}); the 'network' capability is "
                "not grantable in this build"
            )
        if (event.startswith(_DENY_PREFIXES) or event in _DENY_EVENTS) and not allow_subprocess:
            raise PermissionError(
                f"plugin process/spawn attempt refused ({event}); the 'subprocess' "
                "capability is not grantable in this build"
            )

    sys.addaudithook(hook)


def _coerce(value: Any, kind: str, *, name: str) -> Any:
    if kind == "string":
        if not isinstance(value, str):
            raise TypeError(f"argument '{name}' must be a string")
        if len(value) > 20_000:
            raise TypeError(f"argument '{name}' is longer than 20000 characters")
        return value
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"argument '{name}' must be an integer")
        return value
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"argument '{name}' must be a number")
        return float(value)
    if kind == "boolean":
        if not isinstance(value, bool):
            raise TypeError(f"argument '{name}' must be a boolean")
        return value
    raise TypeError(f"argument '{name}' has an unsupported declared type")


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "<truncated: nesting too deep>"
    if value is None or isinstance(value, (bool, int, str)):
        return value if not isinstance(value, str) else value[:20_000]
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        items = list(value.items())[:200]
        return {str(key)[:120]: _json_safe(item, depth=depth + 1) for key, item in items}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth=depth + 1) for item in list(value)[:500]]
    return f"<{type(value).__name__}>"


def _load_module(entry: Path, plugin_id: str) -> Any:
    module_name = "pc_agent_plugin_" + "".join(
        char if char.isalnum() else "_" for char in plugin_id
    )
    spec = importlib.util.spec_from_file_location(module_name, entry)
    if spec is None or spec.loader is None:
        raise ImportError("plugin entry could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    raw = sys.stdin.read()
    try:
        request = json.loads(raw or "{}")
    except ValueError:
        sys.stdout.write(json.dumps({"ok": False, "error": "worker received invalid JSON"}))
        return 2
    if not isinstance(request, dict):
        sys.stdout.write(json.dumps({"ok": False, "error": "worker request must be an object"}))
        return 2

    # Anything the plugin prints must never corrupt the protocol channel.
    protocol_out = sys.stdout
    sys.stdout = sys.stderr

    result: dict[str, Any]
    try:
        _install_audit_hook(request)
        entry = Path(str(request["entry"])).resolve()
        plugin_dir = Path(str(request["plugin_dir"])).resolve()
        if entry != plugin_dir and plugin_dir not in entry.parents:
            raise PermissionError("plugin entry is outside the installed plugin directory")
        sys.path.insert(0, str(plugin_dir))
        module = _load_module(entry, str(request.get("plugin_id") or "plugin"))
        tool_name = str(request.get("tool") or "")
        function = getattr(module, tool_name, None)
        if function is None:
            raise AttributeError(f"plugin does not define a callable tool named '{tool_name}'")
        if not callable(function):
            raise TypeError(f"plugin attribute '{tool_name}' is not callable")
        declared = request.get("arguments") or {}
        if not isinstance(declared, dict):
            raise TypeError("declared arguments must be an object")
        supplied = request.get("values") or {}
        if not isinstance(supplied, dict):
            raise TypeError("tool values must be an object")
        unknown = sorted(set(supplied) - set(declared))
        if unknown:
            raise TypeError(f"unexpected argument(s): {', '.join(unknown)}")
        kwargs = {}
        for name, kind in declared.items():
            if name not in supplied:
                raise TypeError(f"argument '{name}' is required")
            kwargs[str(name)] = _coerce(supplied[name], str(kind), name=str(name))
        value = function(**kwargs)
        result = {"ok": True, "result": _json_safe(value)}
    except BaseException as exc:  # noqa: BLE001 - report, never crash the protocol
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }

    payload = json.dumps(result, ensure_ascii=True)
    if len(payload) > MAX_RESULT_BYTES:
        result = {"ok": False, "error": "plugin result exceeded the output limit"}
        payload = json.dumps(result)
    protocol_out.write(payload)
    protocol_out.flush()
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover - exercised through run_sandboxed
    sys.exit(main())
