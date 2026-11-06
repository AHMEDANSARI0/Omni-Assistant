"""Console-script bootstrap that fails with instructions, not a traceback.

The Windows acceptance machine ran ``pc-agent gui`` and got a raw
``ModuleNotFoundError: No module named 'cryptography'`` from the depth of the
import chain. Nothing in that message told the user the useful facts: the command
being executed came from the **global** interpreter (``…\\Python311\\Scripts\\
pc-agent.exe``), not from ``D:\\omni-assistant\\.venv``, so none of the project's
declared dependencies were installed in it.

This module is now the console-script entry point. It has exactly one job:

* import the real CLI,
* and if that fails, say **which package is missing**, **which interpreter is
  actually running**, and **the two commands that fix it** - then exit with
  status 2 instead of dumping a traceback.

It deliberately imports nothing from :mod:`pc_agent.cli` (or anything that pulls
in third-party packages) at module import time, so it can run inside a broken
environment - the same reason ``self_repair_cli`` exists.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

__all__ = ["explain_missing_dependency", "main", "venv_hint"]

#: Packages this product imports directly; the message names the real one.
_REQUIRED = ("cryptography", "pypdf", "PIL", "certifi", "pypdfium2")


def venv_hint(root: Path | None = None) -> str:
    """A copy-paste hint when a virtual environment exists but is not in use."""

    try:
        project = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    except (OSError, IndexError):  # pragma: no cover - defensive
        return ""
    candidates = [project / ".venv", project / "venv"]
    for candidate in candidates:
        interpreter = (
            candidate / "Scripts" / "python.exe"
            if (candidate / "Scripts").is_dir()
            else candidate / "bin" / "python"
        )
        if interpreter.is_file():
            return str(interpreter)
    return ""


def explain_missing_dependency(
    error: ImportError, *, prefix: str = "", project_root: Path | None = None
) -> str:
    """Build the actionable message for an import failure."""

    missing = getattr(error, "name", "") or ""
    if not missing:
        text = str(error)
        for candidate in _REQUIRED:
            if candidate in text:
                missing = candidate
                break
    interpreter = prefix or sys.executable
    inside_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    lines = [
        "pc-agent cannot start: a required package is missing.",
        f"  missing package : {missing or 'unknown'}",
        f"  interpreter     : {interpreter}",
        f"  virtualenv      : {'yes' if inside_venv else 'no'}",
    ]
    hint = venv_hint(project_root)
    if not inside_venv and hint:
        lines += [
            "",
            "You are running the GLOBAL Python, so the project's dependencies are not",
            "installed there. Use the project interpreter instead:",
            f'  "{hint}" -m pc_agent {prefix and ""}'.rstrip(),
            "",
            "or activate the environment first, then retry:",
            "  .\\.venv\\Scripts\\Activate.ps1        (PowerShell)",
            "  .\\.venv\\Scripts\\activate.bat        (cmd)",
            "  pc-agent gui",
        ]
    lines += [
        "",
        "To install or repair the dependencies in the interpreter you are using:",
        f'  "{interpreter}" -m pip install -e ".[dev]"',
    ]
    return "\n".join(line for line in lines if line is not None)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI, or explain the failure in words a user can act on."""

    try:
        from .cli import main as cli_main
    except ImportError as error:  # a broken/incomplete environment
        print(explain_missing_dependency(error), file=sys.stderr)
        return 2
    return int(cli_main(argv))


if __name__ == "__main__":  # pragma: no cover - module execution
    raise SystemExit(main())
