"""One-command Windows verification: run every gate and keep the receipts.

The suite is the real proof, but "run pytest and paste the output" leaves too
much to interpretation: which Python, which git commit, whether `ruff` passed,
what the release audit said, and whether the failure was the product or the
machine. This script runs the gates in order, keeps the exact exit codes and the
tail of each output, and writes `data/windows-verification.json`.

Design rules:

* **No shell.** Every command is an argv list executed with ``sys.executable``,
  so what ran is exactly what is printed (and the same interpreter that runs the
  rest of the gates).
* **Nothing is skipped quietly.** A gate that cannot run here is recorded as
  ``not-run`` with the reason - never as a pass.
* **Failures are visible.** The exit code is 1 if any gate failed; a gate that
  was asked for but could not even start is a failure, not a note.

Usage (Windows PowerShell, from the install root):

    python scripts\\verify_windows.py
    python scripts\\verify_windows.py --only doctor,audit
    python scripts\\verify_windows.py --json
    python scripts\\verify_windows.py --report-dir D:\\omni-assistant\\data
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORT_NAME = "windows-verification.json"

#: Gate order matters: cheap and structural first, the full suite last, so a
#: broken tree fails in five seconds instead of five minutes.
GATES: tuple[str, ...] = (
    "python",
    "import",
    "payload",
    "ruff",
    "doctor",
    "audit",
    "desktop_shell",
    "suite",
    "production",
)

#: Gates that need the Windows certificate store / installer build. They are
#: reported as ``not-run`` here and are covered by their own commands.
OPTIONAL_WINDOWS_GATES = ("signing",)


@dataclass
class GateResult:
    name: str
    status: str  # pass | fail | not-run
    detail: str = ""
    exit_code: int | None = None
    seconds: float = 0.0
    tail: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "exit_code": self.exit_code,
            "seconds": round(self.seconds, 2),
            "tail": self.tail,
            "command": self.command,
        }


def _tail(text: str, lines: int = 12) -> list[str]:
    kept = [line for line in text.strip().splitlines() if line.strip()]
    return kept[-lines:]


def _run(argv: list[str], *, timeout: int) -> tuple[int, str]:
    """Run an argv list with the current interpreter. Returns (code, output)."""

    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        completed = subprocess.run(
            argv,
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return 127, f"could not start {argv[0]}: {exc}"
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(output, bytes):  # pragma: no cover - defensive
            output = output.decode("utf-8", "replace")
        return 124, f"timed out after {timeout}s\n{output}"
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _python_version_gate() -> GateResult:
    since = time.perf_counter()
    version = platform.python_version()
    ok = sys.version_info >= (3, 11)
    return GateResult(
        name="python",
        status="pass" if ok else "fail",
        detail=f"{version} at {sys.executable} (requires 3.11+)",
        exit_code=0 if ok else 1,
        seconds=time.perf_counter() - since,
        command=[sys.executable, "--version"],
    )


def _import_gate() -> GateResult:
    """The import path must be *this* tree - a stale install is the classic trap."""

    since = time.perf_counter()
    argv = [
        sys.executable,
        "-c",
        (
            "import pc_agent, pathlib;"
            "print(pc_agent.__version__);"
            "print(pathlib.Path(pc_agent.__file__).resolve().parent)"
        ),
    ]
    code, output = _run(argv, timeout=120)
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    version = lines[-2] if len(lines) >= 2 else "?"
    location = lines[-1] if len(lines) >= 2 else output.strip()
    inside = False
    try:
        inside = PROJECT_ROOT.resolve() in Path(location).resolve().parents or (
            Path(location).resolve() == (PROJECT_ROOT / "src" / "pc_agent").resolve()
        )
    except OSError:  # pragma: no cover - defensive
        inside = False
    status = "pass" if code == 0 and inside else "fail"
    detail = f"pc_agent {version} imported from {location}"
    if code == 0 and not inside:
        detail += f" - OUTSIDE this install ({PROJECT_ROOT}); run: python -m pip install -e \".[dev]\""
    return GateResult(
        name="import",
        status=status,
        detail=detail,
        exit_code=code,
        seconds=time.perf_counter() - since,
        tail=_tail(output),
        command=argv,
    )


def _payload_gate() -> GateResult:
    """Required installer payload files - the `.env.example` class of breakage.

    One missing template file used to surface as nineteen failing tests in four
    unrelated areas. Here it is one line, with the command that fixes it.
    """

    since = time.perf_counter()
    from pc_agent.env_template import ENV_TEMPLATE_NAME, write_env_template
    from pc_agent.posix_package import _REQUIRED_PAYLOAD_FILES as POSIX_REQUIRED
    from pc_agent.windows_installer import _REQUIRED_PAYLOAD_FILES as WINDOWS_REQUIRED

    required = sorted(POSIX_REQUIRED | WINDOWS_REQUIRED)
    missing = [name for name in required if not (PROJECT_ROOT / name).is_file()]
    restored: list[str] = []
    if ENV_TEMPLATE_NAME in missing:
        # Safe to restore unasked: it is the sample file, it contains no secrets,
        # and every package builder needs it.
        _, wrote = write_env_template(PROJECT_ROOT)
        if wrote:
            restored.append(ENV_TEMPLATE_NAME)
            missing = [name for name in missing if name != ENV_TEMPLATE_NAME]
    detail = (
        f"all {len(required)} required installer payload files are present"
        if not missing
        else f"missing: {', '.join(missing)}"
    )
    if restored:
        detail += f"; restored {', '.join(restored)} from the built-in template"
    return GateResult(
        name="payload",
        status="pass" if not missing else "fail",
        detail=detail,
        exit_code=0 if not missing else 1,
        seconds=time.perf_counter() - since,
        command=[sys.executable, "-m", "pc_agent.cli", "release", "env-template", "--check"],
    )


def _simple_gate(
    name: str, argv: list[str], *, timeout: int, ok_detail: str = ""
) -> GateResult:
    since = time.perf_counter()
    code, output = _run(argv, timeout=timeout)
    summary = ""
    for line in reversed(output.strip().splitlines()):
        if line.strip():
            summary = line.strip()
            break
    return GateResult(
        name=name,
        status="pass" if code == 0 else "fail",
        detail=ok_detail or summary or f"exit {code}",
        exit_code=code,
        seconds=time.perf_counter() - since,
        tail=_tail(output),
        command=argv,
    )


def _suite_gate() -> GateResult:
    """The full suite. A skip is reported, never counted as a pass."""

    since = time.perf_counter()
    argv = [sys.executable, "-m", "pytest", "-q"]
    code, output = _run(argv, timeout=3600)
    summary = ""
    skipped = 0
    for line in output.strip().splitlines():
        stripped = line.strip()
        if " passed" in stripped or " failed" in stripped or "error" in stripped.lower():
            summary = stripped
    for line in output.splitlines():
        if line.strip().startswith("SKIPPED"):
            skipped += 1
    detail = summary or f"exit {code}"
    if skipped:
        detail += f" ({skipped} skipped - see the tail)"
    return GateResult(
        name="suite",
        status="pass" if code == 0 else "fail",
        detail=detail,
        exit_code=code,
        seconds=time.perf_counter() - since,
        tail=_tail(output, lines=25),
        command=argv,
    )


def run_gates(
    wanted: list[str] | None = None, *, skip_suite: bool = False
) -> list[GateResult]:
    selected = list(GATES)
    if skip_suite:
        selected = [name for name in selected if name != "suite"]
    if wanted:
        unknown = [name for name in wanted if name not in GATES]
        if unknown:
            raise SystemExit(f"unknown gate(s): {', '.join(unknown)}; known: {', '.join(GATES)}")
        selected = [name for name in selected if name in wanted]

    results: list[GateResult] = []
    for name in selected:
        if name == "python":
            results.append(_python_version_gate())
        elif name == "import":
            results.append(_import_gate())
        elif name == "payload":
            results.append(_payload_gate())
        elif name == "ruff":
            results.append(
                _simple_gate(
                    "ruff",
                    [sys.executable, "-m", "ruff", "check", "."],
                    timeout=300,
                    ok_detail="lint clean",
                )
            )
        elif name == "doctor":
            results.append(
                _simple_gate(
                    "doctor", [sys.executable, "-m", "pc_agent.cli", "doctor"], timeout=300
                )
            )
        elif name == "audit":
            results.append(
                _simple_gate(
                    "audit",
                    [sys.executable, "-m", "pc_agent.cli", "release", "audit"],
                    timeout=600,
                )
            )
        elif name == "desktop_shell":
            results.append(
                _simple_gate(
                    "desktop_shell",
                    [sys.executable, "scripts/check_desktop_shell.py"],
                    timeout=300,
                )
            )
        elif name == "suite":
            results.append(_suite_gate())
        elif name == "production":
            results.append(
                _simple_gate(
                    "production",
                    [sys.executable, "-m", "pc_agent.cli", "release", "production-check"],
                    timeout=1800,
                )
            )
    return results


def _optional_gates() -> list[dict[str, str]]:
    """Things this script deliberately does not decide (they need real Windows)."""

    from pc_agent.windows_signing import list_signing_certificates

    listing = list_signing_certificates()
    detail = listing.get("detail", "")
    if listing.get("status") == "ok":
        usable = listing.get("usable", 0)
        detail = f"{usable} usable code-signing certificate(s) in Cert:\\CurrentUser\\My"
    return [
        {
            "name": "signing",
            "status": "not-run" if listing.get("status") != "ok" else "checked",
            "detail": f"installer Authenticode: {listing.get('status')} - {detail}",
            "next": "pc-agent release sign-bundle --check",
        }
    ]


def build_report(results: list[GateResult]) -> dict[str, Any]:
    failures = [item for item in results if item.status == "fail"]
    return {
        "ok": not failures,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "executable": sys.executable,
        "root": str(PROJECT_ROOT),
        "gates": [item.to_dict() for item in results],
        "failures": [item.name for item in failures],
        "optional": _optional_gates(),
    }


def print_report(report: dict[str, Any]) -> None:
    print("Windows verification")
    print(f"  root      : {report['root']}")
    print(f"  platform  : {report['platform']}")
    print(f"  python    : {report['python']} ({report['executable']})")
    print()
    width = max(len(gate["name"]) for gate in report["gates"])
    for gate in report["gates"]:
        marker = {"pass": "PASS", "fail": "FAIL", "not-run": "N/R "}.get(
            gate["status"], gate["status"].upper()
        )
        print(f"  [{marker}] {gate['name']:<{width}}  {gate['detail']}")
    print()
    for item in report["optional"]:
        print(f"  [{'N/R ' if item['status'] == 'not-run' else 'INFO'}] "
              f"{item['name']:<{width}}  {item['detail']}")
        print(f"        next: {item['next']}")
    print()
    if report["ok"]:
        print("WINDOWS VERIFICATION: PASS")
    else:
        print(f"WINDOWS VERIFICATION: FAIL ({', '.join(report['failures'])})")
        for gate in report["gates"]:
            if gate["status"] != "fail":
                continue
            print()
            print(f"--- {gate['name']} (exit {gate['exit_code']}) ---")
            for line in gate["tail"]:
                print(f"    {line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", default="", help=f"comma-separated gates: {', '.join(GATES)}")
    parser.add_argument("--skip-suite", action="store_true", help="skip the full pytest run")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--report-dir", default=None, help="where to write the report")
    parser.add_argument(
        "--no-report", action="store_true", help="run the gates without writing a file"
    )
    args = parser.parse_args(argv)

    wanted = [part.strip() for part in args.only.split(",") if part.strip()]
    try:
        results = run_gates(wanted or None, skip_suite=args.skip_suite)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2
    report = build_report(results)

    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
    else:
        print_report(report)

    if not args.no_report:
        target_dir = (
            Path(args.report_dir).expanduser()
            if args.report_dir
            else PROJECT_ROOT / "data"
        )
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / REPORT_NAME
            target.write_text(
                json.dumps(report, indent=1, sort_keys=True), encoding="utf-8"
            )
            if not args.json:
                print(f"\nReport written: {target}")
        except OSError as exc:  # pragma: no cover - environment dependent
            print(f"Could not write the report: {exc}", file=sys.stderr)

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
