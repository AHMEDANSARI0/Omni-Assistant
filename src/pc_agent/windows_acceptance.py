"""Windows acceptance harness (release 0.80.1).

The programme's last open item was evidence: the Windows fixes in 0.79.1 and the
hardening in 0.80.1 have only ever been verified on a POSIX host. A statement in
a document cannot close that; a recorded, hash-sealed run on the user's machine
can.

This module produces and verifies that record.

* ``run_windows_acceptance()`` runs the gates for real - the test suite, the
  release audit, the end-to-end release chain, a real sandboxed plugin worker,
  real owner-only file hardening and the measured accessibility report - and
  writes a JSON report whose body is sealed with a SHA-256 hash.
* ``verify_acceptance_report()`` re-computes the hash and checks the shape, so an
  edited file is rejected rather than believed. It runs on any platform, which is
  what makes the report reviewable before it is trusted.
* ``record_acceptance_report()`` copies a *verified, passed Windows* report into
  ``data/windows-acceptance.json``. Only then does the baseline stop saying
  ``pending_user_windows_evidence`` - see ``baseline.windows_acceptance_evidence``.

Honesty rules, because this is exactly the kind of feature that goes wrong:

* On a non-Windows host the default run returns ``status="skipped"`` with the
  platform gate ``not-applicable``. It never claims a pass.
* ``allow_non_windows=True`` (the ``--self-test`` flag) runs the platform-neutral
  gates so the machinery itself is exercised - and the report says
  ``status="self-test"``, which can never be recorded as Windows evidence.
* Every gate carries its own evidence text; a gate that did not run says so.
* No secret is written: the report contains a host fingerprint, gate verdicts and
  timings, nothing else, and the verifier refuses a report that looks like it
  carries key material.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .baseline import (
    WINDOWS_ACCEPTANCE_EVIDENCE,
    acceptance_body_sha256,
    windows_acceptance_evidence,
)
from .config import Settings
from .errors import PcAgentError

__all__ = [
    "REQUIRED_GATES",
    "AcceptanceGate",
    "AcceptanceReport",
    "AcceptanceVerification",
    "record_acceptance_report",
    "run_windows_acceptance",
    "verify_acceptance_report",
]

#: Gates every recorded Windows run must contain, in order.
REQUIRED_GATES = (
    "platform",
    "tests",
    "release_audit",
    "release_chain",
    "plugin_sandbox",
    "file_permissions",
    "accessibility",
)

_KNOWN_STATUSES = frozenset({"passed", "failed", "skipped", "self-test"})
_GATE_STATUSES = frozenset({"pass", "fail", "not-run", "not-applicable"})
#: Anything that looks like a credential makes a report unusable as evidence.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
)


@dataclass(frozen=True, slots=True)
class AcceptanceGate:
    name: str
    status: str
    detail: str
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class AcceptanceReport:
    status: str
    platform: str
    generated_at: str
    version: str
    gates: tuple[AcceptanceGate, ...]
    machine: dict[str, Any]
    body_sha256: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    @property
    def failures(self) -> int:
        return sum(1 for gate in self.gates if gate.status == "fail")

    def gate(self, name: str) -> AcceptanceGate | None:
        for gate in self.gates:
            if gate.name == name:
                return gate
        return None

    def summary(self) -> str:
        passed = sum(1 for gate in self.gates if gate.status == "pass")
        return (
            f"{self.status.upper()}; platform={self.platform}; "
            f"gates={len(self.gates)}; passed={passed}; failures={self.failures}"
        )

    def body(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "platform": self.platform,
            "generated_at": self.generated_at,
            "version": self.version,
            "machine": dict(self.machine),
            "gates": [gate.to_dict() for gate in self.gates],
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.body()
        payload["body_sha256"] = self.body_sha256 or acceptance_body_sha256(payload)
        return payload


@dataclass(frozen=True, slots=True)
class AcceptanceVerification:
    ok: bool
    status: str
    problems: tuple[str, ...]
    report: dict[str, Any]

    def summary(self) -> str:
        if self.ok:
            return f"VERIFIED; status={self.status}"
        return "REJECTED; " + "; ".join(self.problems[:3])


# ------------------------------------------------------------------ helpers ---


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _machine_fingerprint(settings: Settings) -> dict[str, Any]:
    """Host facts an evidence file needs to be reviewable - and no secrets."""

    facts: dict[str, Any] = {
        "platform": sys.platform,
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "cpu_count": os.cpu_count() or 0,
        "timezone": time.tzname[0] if time.tzname else "unknown",
        "locale": os.environ.get("LANG") or os.environ.get("LC_ALL") or "default",
        "install_root": str(Path(settings.root_dir).expanduser().resolve()),
        "filesystem_encoding": sys.getfilesystemencoding(),
    }
    if sys.platform.startswith("win"):
        try:  # pragma: no cover - Windows only
            facts["windows_build"] = f"{sys.getwindowsversion().major}." f"{sys.getwindowsversion().build}"
        except AttributeError:  # pragma: no cover - very old builds
            facts["windows_build"] = "unknown"
        facts["comspec"] = os.environ.get("COMSPEC", "")
    return facts


def _gate(
    name: str, status: str, detail: str, since: float
) -> AcceptanceGate:
    return AcceptanceGate(
        name=name, status=status, detail=detail, duration_ms=int((time.perf_counter() - since) * 1000)
    )


def _run_tests_gate(settings: Settings, timeout_seconds: float) -> AcceptanceGate:
    """Run the real suite in this install; report the summary line it printed."""

    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=str(settings.root_dir),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _gate("tests", "fail", f"the suite could not be run: {exc}", started)
    output = (completed.stdout or "") + (completed.stderr or "")
    tail = [line.strip() for line in output.strip().splitlines() if line.strip()]
    summary = tail[-1][:200] if tail else "no output"
    counts = re.search(r"(\d+) passed", summary)
    failed = re.search(r"(\d+) failed", summary)
    if completed.returncode == 0:
        passed = counts.group(1) if counts else "unknown"
        return _gate(
            "tests",
            "pass",
            f"{passed} tests passed on this machine ({summary})",
            started,
        )
    return _gate(
        "tests",
        "fail",
        f"the suite reported {failed.group(1) if failed else 'an error'} failure(s): {summary}",
        started,
    )


def _run_audit_gate(settings: Settings) -> AcceptanceGate:
    from .release import audit_install

    started = time.perf_counter()
    try:
        audit = audit_install(settings)
    except PcAgentError as exc:
        return _gate("release_audit", "fail", f"the audit could not run: {exc}", started)
    status = "pass" if audit.failures == 0 else "fail"
    return _gate("release_audit", status, audit.summary(), started)


def _run_chain_gate(settings: Settings) -> AcceptanceGate:
    from .production_check import run_production_check

    started = time.perf_counter()
    try:
        report = run_production_check(settings)
    except PcAgentError as exc:
        return _gate(
            "release_chain", "fail", f"the release chain could not run: {exc}", started
        )
    status = "pass" if report.ok else "fail"
    failing = [step.name for step in report.steps if step.status == "fail"]
    detail = f"{report.summary()} in {report.duration_ms} ms"
    if failing:
        detail += f"; failing steps: {', '.join(failing)}"
    return _gate("release_chain", status, detail, started)


def _run_sandbox_gate() -> AcceptanceGate:
    """Prove a plugin worker can really start on this machine (0.79.1 fix)."""

    from .plugins.sandbox import SandboxLimits, describe_sandbox, run_sandboxed

    started = time.perf_counter()
    workdir = Path(tempfile.mkdtemp(prefix="pc-agent-acceptance-"))
    try:
        result = run_sandboxed(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys; sys.stdout.write('worker-alive')",
            ],
            cwd=workdir,
            payload=b"",
            limits=SandboxLimits(timeout_seconds=60),
        )
    except (OSError, PcAgentError) as exc:  # pragma: no cover - defensive
        return _gate("plugin_sandbox", "fail", f"the sandbox raised: {exc}", started)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    mechanism = describe_sandbox()["mechanism"]
    if result.spawn_error:
        return _gate(
            "plugin_sandbox",
            "fail",
            f"the sandbox could not start a worker: {result.spawn_error}",
            started,
        )
    if not result.ok or result.stdout.strip() != "worker-alive":
        return _gate(
            "plugin_sandbox",
            "fail",
            f"worker exited with {result.returncode}: {result.stderr.strip()[:200]}",
            started,
        )
    return _gate(
        "plugin_sandbox",
        "pass",
        f"an isolated worker started and answered in {result.duration_ms} ms "
        f"via {mechanism} (interpreter={result.interpreter})",
        started,
    )


def _run_permissions_gate() -> AcceptanceGate:
    """Prove owner-only hardening works with this platform's real mechanism."""

    from .sync.perms import harden_private_file, private_file_report

    started = time.perf_counter()
    workdir = Path(tempfile.mkdtemp(prefix="pc-agent-acceptance-"))
    try:
        target = workdir / "secret.json"
        target.write_text("{}", encoding="utf-8")
        applied = harden_private_file(target)
        report = private_file_report(target)
    except OSError as exc:  # pragma: no cover - defensive
        return _gate("file_permissions", "fail", f"hardening raised: {exc}", started)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if not report.get("owner_only"):
        return _gate(
            "file_permissions",
            "fail",
            f"{report.get('mechanism')} did not produce an owner-only file: "
            f"{report.get('detail')}",
            started,
        )
    return _gate(
        "file_permissions",
        "pass",
        f"owner-only via {report.get('mechanism')} ({report.get('detail')}); "
        f"applied={applied.get('applied')}",
        started,
    )


def _run_accessibility_gate(settings: Settings) -> AcceptanceGate:
    from .a11y import a11y_status

    started = time.perf_counter()
    report = a11y_status(settings)
    contrast = report["contrast"]
    coverage = report["coverage"]["percent"]
    ok = contrast["passing"] == contrast["total"] and coverage >= 100.0
    return _gate(
        "accessibility",
        "pass" if ok else "fail",
        f"locale={report['locale']} coverage={coverage}% "
        f"contrast={contrast['passing']}/{contrast['total']} {contrast['wcag_level']} "
        f"limits={len(report['limitations'])}",
        started,
    )


# --------------------------------------------------------------- public API ---


def run_windows_acceptance(
    settings: Settings,
    *,
    run_suite: bool = True,
    suite_timeout_seconds: float = 3_600.0,
    allow_non_windows: bool = False,
    write_report: bool = True,
    out_path: Path | None = None,
) -> AcceptanceReport:
    """Run the acceptance gates and return (and optionally store) a sealed report."""

    is_windows = sys.platform.startswith("win")
    machine = _machine_fingerprint(settings)
    gates: list[AcceptanceGate] = []

    started = time.perf_counter()
    if is_windows:
        gates.append(
            _gate(
                "platform",
                "pass",
                f"running on {platform.system()} {platform.release()} "
                f"(build {machine.get('windows_build', 'unknown')}), "
                f"Python {platform.python_version()}",
                started,
            )
        )
    else:
        gates.append(
            _gate(
                "platform",
                "not-applicable" if allow_non_windows else "fail",
                f"this host is {platform.system()} ({sys.platform}); Windows acceptance "
                "evidence can only be produced on Windows",
                started,
            )
        )

    if not is_windows and not allow_non_windows:
        # Refuse to produce something that could be mistaken for Windows
        # evidence: the remaining gates are recorded as not run.
        for name in REQUIRED_GATES[1:]:
            gates.append(
                AcceptanceGate(
                    name=name,
                    status="not-run",
                    detail="requires a Windows host; run this command there",
                    duration_ms=0,
                )
            )
        report = AcceptanceReport(
            status="skipped",
            platform="posix" if os.name == "posix" else os.name,
            generated_at=_now(),
            version=__version__,
            gates=tuple(gates),
            machine=machine,
        )
        report = _sealed(report)
        if write_report:
            _write_report(report, settings, out_path)
        return report

    gates.append(
        _run_tests_gate(settings, suite_timeout_seconds)
        if run_suite
        else AcceptanceGate(
            name="tests",
            status="not-run",
            detail="the suite was skipped on request (--no-suite)",
            duration_ms=0,
        )
    )
    gates.append(_run_audit_gate(settings))
    gates.append(_run_chain_gate(settings))
    gates.append(_run_sandbox_gate())
    gates.append(_run_permissions_gate())
    gates.append(_run_accessibility_gate(settings))

    ordered = tuple(sorted(gates, key=lambda gate: REQUIRED_GATES.index(gate.name)))
    failed = any(gate.status == "fail" for gate in ordered)
    if failed:
        status = "failed"
    elif is_windows:
        status = "passed"
    else:
        # A non-Windows self-test can never be recorded as Windows evidence.
        status = "self-test"
    report = AcceptanceReport(
        status=status,
        platform="windows" if is_windows else ("posix" if os.name == "posix" else os.name),
        generated_at=_now(),
        version=__version__,
        gates=ordered,
        machine=machine,
    )
    report = _sealed(report)
    if write_report:
        _write_report(report, settings, out_path)
    return report


def _sealed(report: AcceptanceReport) -> AcceptanceReport:
    digest = acceptance_body_sha256(report.body())
    return AcceptanceReport(
        status=report.status,
        platform=report.platform,
        generated_at=report.generated_at,
        version=report.version,
        gates=report.gates,
        machine=report.machine,
        body_sha256=digest,
    )


def _write_report(
    report: AcceptanceReport, settings: Settings, out_path: Path | None
) -> Path:
    target = (
        Path(out_path).expanduser()
        if out_path is not None
        else Path(settings.root_dir) / "workspace" / WINDOWS_ACCEPTANCE_EVIDENCE
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report.to_dict(), indent=1, sort_keys=True), encoding="utf-8"
    )
    return target


def verify_acceptance_report(path: Path | str) -> AcceptanceVerification:
    """Check a report offline: hash, shape, gate set, and no key material."""

    problems: list[str] = []
    target = Path(path).expanduser()
    if not target.is_file():
        return AcceptanceVerification(
            ok=False, status="missing", problems=(f"report not found: {target}",), report={}
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return AcceptanceVerification(
            ok=False,
            status="unreadable",
            problems=(f"report is not readable JSON: {exc}",),
            report={},
        )
    if not isinstance(payload, dict):
        return AcceptanceVerification(
            ok=False,
            status="invalid",
            problems=("report must be a single JSON object",),
            report={},
        )

    recorded_hash = payload.get("body_sha256")
    if not isinstance(recorded_hash, str) or not recorded_hash:
        problems.append("report has no body_sha256 seal")
    elif recorded_hash != acceptance_body_sha256(payload):
        problems.append("report body was modified after it was sealed (hash mismatch)")

    status = payload.get("status")
    if status not in _KNOWN_STATUSES:
        problems.append(f"unknown report status {status!r}")
    if payload.get("platform") not in {"windows", "posix", "nt"}:
        problems.append(f"unknown platform {payload.get('platform')!r}")
    if not str(payload.get("version") or "").strip():
        problems.append("report does not record a version")
    elif payload.get("version") != __version__:
        problems.append(
            f"report was produced by {payload.get('version')}, this build is {__version__}"
        )

    gates = payload.get("gates")
    if not isinstance(gates, list) or not gates:
        problems.append("report has no gates")
        gates = []
    names = [gate.get("name") for gate in gates if isinstance(gate, dict)]
    missing = [name for name in REQUIRED_GATES if name not in names]
    if missing:
        problems.append("report is missing gates: " + ", ".join(missing))
    for gate in gates:
        if not isinstance(gate, dict) or gate.get("status") not in _GATE_STATUSES:
            problems.append("report contains a gate with an unknown status")
            break

    if status == "passed":
        if payload.get("platform") != "windows":
            problems.append("a passed report must come from a Windows host")
        failing = [
            gate.get("name")
            for gate in gates
            if isinstance(gate, dict) and gate.get("status") in {"fail", "not-run", "not-applicable"}
        ]
        if failing:
            problems.append(
                "a passed report cannot contain unpassed gates: " + ", ".join(map(str, failing))
            )

    serialised = json.dumps(payload, default=str)
    for pattern in _SECRET_PATTERNS:
        if pattern.search(serialised):
            problems.append("report looks like it contains key material and is refused")
            break

    return AcceptanceVerification(
        ok=not problems,
        status=str(status or "unknown"),
        problems=tuple(problems),
        report=payload,
    )


def record_acceptance_report(
    settings: Settings, path: Path | str
) -> dict[str, Any]:
    """Record a verified, passed Windows report so the baseline stops pending."""

    verification = verify_acceptance_report(path)
    if not verification.ok:
        raise PcAgentError(f"Evidence was rejected: {verification.summary()}")
    if verification.status != "passed":
        raise PcAgentError(
            f"Only a passed Windows run can be recorded; this report is "
            f"{verification.status!r}."
        )
    if verification.report.get("platform") != "windows":
        raise PcAgentError("Only a run produced on a Windows host can be recorded.")
    target = Path(settings.root_dir).expanduser() / "data" / WINDOWS_ACCEPTANCE_EVIDENCE
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(path).expanduser(), target)
    state = windows_acceptance_evidence(Path(settings.root_dir))
    if not state.get("recorded"):
        raise PcAgentError(
            "The report was copied but does not verify from its recorded location: "
            f"{state.get('detail')}"
        )
    return {
        "recorded": True,
        "path": str(target),
        "status": state["status"],
        "gates": state["gates"],
        "generated_at": state["generated_at"],
        "detail": state["detail"],
    }
