"""Minimal bootstrap CLI for deterministic core self-repair.

The normal ``pc-agent`` CLI imports the planner, orchestrator and provider
layers.  If one of those files is the file that needs repair, that CLI may
not start.  This entry point therefore imports only the self-repair service
and the small shared error/redaction modules it already depends on.  It can
diagnose and repair most core files even when, for example, the budget or
orchestrator module has a syntax error.

If this bootstrap module, ``self_repair.py``, ``errors.py`` or
``logging_config.py`` is itself damaged, re-run the verified cumulative
patcher first; the patcher is the external fallback.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from .errors import PcAgentError
from .self_repair import SelfRepairDiagnosis, SelfRepairRun, SelfRepairService

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"", "0", "false", "no", "off"}


def _local_values(root: Path, environ: Mapping[str, str]) -> dict[str, str]:
    """Read the two settings the bootstrap needs without importing config."""

    values: dict[str, str] = {}
    dotenv = root / ".env"
    if dotenv.is_file():
        for line_number, raw_line in enumerate(
            dotenv.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].lstrip()
            if "=" not in line:
                raise PcAgentError(
                    f"Invalid .env entry at .env:{line_number} (expected KEY=VALUE)."
                )
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[key.strip()] = value
    for name in ("PC_AGENT_ALLOW_SELF_REPAIR", "PC_AGENT_DATA_DIR", "PC_AGENT_HOME"):
        if name in environ:
            values[name] = str(environ[name])
    return values


def _enabled(values: Mapping[str, str]) -> bool:
    raw = str(values.get("PC_AGENT_ALLOW_SELF_REPAIR", "0")).strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise PcAgentError(
        "PC_AGENT_ALLOW_SELF_REPAIR must be a boolean (1/0, true/false, yes/no, on/off)."
    )


def _resolve_from(root: Path, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


def _confirm(summary: str, *, assume_yes: bool) -> bool:
    print("Approval required for this exact self-repair operation:")
    print(f"  {summary}")
    if assume_yes:
        print("Decision: APPROVED by --yes.")
        return True
    try:
        answer = input("Type APPROVE and press Enter to continue (Ctrl+C cancels): ")
    except EOFError:
        print("No interactive input available; re-run with --yes to approve.")
        return False
    except KeyboardInterrupt:
        print()
        print("Cancelled with Ctrl+C.")
        return False
    approved = answer.strip().upper() == "APPROVE"
    print(f"Decision: {'APPROVED' if approved else 'DENIED'}")
    return approved


def _print_diagnosis(diagnosis: SelfRepairDiagnosis, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(diagnosis.to_dict(), indent=2))
        return
    print(f"Self-repair diagnosis: {diagnosis.summary()}")
    if diagnosis.issues:
        print("Issues:")
        for issue in diagnosis.issues:
            print(f"  - [{issue.severity}] {issue.code} {issue.path}: {issue.detail}")
    else:
        print("Issues: none")
    if diagnosis.operations:
        print("Planned deterministic repair operations:")
        for operation in diagnosis.operations:
            print(f"  - {operation.kind} {operation.path}")
    if diagnosis.extra_files:
        print("Extra eligible files (reported only; never deleted automatically):")
        for path in diagnosis.extra_files:
            print(f"  - {path}")
    if diagnosis.blocked:
        print("Stage/apply are blocked by the baseline issue above.")


def _print_run(run: SelfRepairRun, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(run.to_dict(), indent=2))
        return
    print(f"Self-repair run: {run.run_id}")
    print(f"Status: {run.status}")
    print(f"Check profile: {run.check_profile}")
    if run.operations:
        print("Operations:")
        for operation in run.operations:
            print(f"  - {operation.kind} {operation.path}")
    for label, checks in (("Staged", run.checks), ("Post-apply", run.post_checks)):
        if checks:
            print(f"{label} checks:")
            for check in checks:
                state = "PASS" if check.success else "FAIL"
                detail = (
                    check.output.strip().splitlines()[-1]
                    if check.output.strip()
                    else ""
                )
                print(f"  - [{state}] {check.name}: {detail[:500]}")
    if run.backups:
        print(f"Backed-up files: {len(run.backups)}")
    if run.error:
        print(f"Error: {run.error}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pc-agent-self-repair",
        description=(
            "Bootstrap core self-repair. Use this when the normal pc-agent "
            "CLI cannot start because a core file needs repair."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Application root folder (default: PC_AGENT_HOME or current folder)",
    )
    subparsers = parser.add_subparsers(dest="command")

    baseline = subparsers.add_parser("baseline", help="Capture a known-good baseline")
    baseline.add_argument(
        "--check", choices=("static", "import", "full"), default="import"
    )
    baseline.add_argument("--force", action="store_true")
    baseline.add_argument("--yes", action="store_true")
    baseline.add_argument("--json", action="store_true")

    diagnose = subparsers.add_parser("diagnose", help="Read-only diagnosis")
    diagnose.add_argument("--json", action="store_true")

    stage = subparsers.add_parser("stage", help="Stage and verify a repair")
    stage.add_argument("--check", choices=("static", "import", "full"), default="import")
    stage.add_argument("--json", action="store_true")

    apply_parser = subparsers.add_parser("apply", help="Apply one staged repair")
    apply_parser.add_argument("run_id", help="Exact run ID or latest")
    apply_parser.add_argument(
        "--check", choices=("static", "import", "full"), default=None
    )
    apply_parser.add_argument("--yes", action="store_true")
    apply_parser.add_argument("--json", action="store_true")

    rollback = subparsers.add_parser("rollback", help="Roll back one applied repair")
    rollback.add_argument("run_id", help="Exact run ID or latest")
    rollback.add_argument("--yes", action="store_true")
    rollback.add_argument("--json", action="store_true")

    list_parser = subparsers.add_parser("list", help="List self-repair runs")
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.add_argument("--json", action="store_true")

    show = subparsers.add_parser("show", help="Show one self-repair run")
    show.add_argument("run_id", help="Exact run ID or latest")
    show.add_argument("--json", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    env: Mapping[str, str] = os.environ if environ is None else environ
    try:
        root = Path(
            args.root or env.get("PC_AGENT_HOME") or Path.cwd()
        ).expanduser().resolve()
        values = _local_values(root, env)
        if not _enabled(values):
            raise PcAgentError(
                "Self-repair is disabled. Set PC_AGENT_ALLOW_SELF_REPAIR=1 in "
                ".env to enable core baseline diagnosis and repair."
            )
        data_dir = _resolve_from(root, values.get("PC_AGENT_DATA_DIR", "data"))
        service = SelfRepairService(root, data_dir)
        command = args.command or "diagnose"

        if command == "baseline":
            if not _confirm(
                "capture self-repair baseline "
                f"(checks={args.check}, force={'yes' if args.force else 'no'})",
                assume_yes=bool(args.yes),
            ):
                print("Self-repair baseline capture cancelled.")
                return 3
            baseline = service.create_baseline(
                force=bool(args.force), check_profile=args.check
            )
            if args.json:
                print(json.dumps(baseline.to_dict(), indent=2))
            else:
                print(f"Self-repair baseline captured: {baseline.summary()}")
            return 0
        if command == "diagnose":
            diagnosis = service.diagnose()
            _print_diagnosis(diagnosis, as_json=bool(args.json))
            return 1 if diagnosis.blocked else 0
        if command == "stage":
            run = service.stage(check_profile=args.check)
            _print_run(run, as_json=bool(args.json))
            return 0 if run.status == "staged" else 1
        if command == "apply":
            run_id = service.resolve_run_id(args.run_id)
            if not _confirm(
                f"apply self-repair run {run_id}", assume_yes=bool(args.yes)
            ):
                print("Self-repair apply cancelled.")
                return 3
            run = service.apply(run_id, post_check_profile=args.check)
            _print_run(run, as_json=bool(args.json))
            return 0 if run.status == "applied" else 1
        if command == "rollback":
            run_id = service.resolve_run_id(args.run_id)
            if not _confirm(
                f"rollback self-repair run {run_id}", assume_yes=bool(args.yes)
            ):
                print("Self-repair rollback cancelled.")
                return 3
            run = service.rollback(run_id)
            _print_run(run, as_json=bool(args.json))
            return 0
        if command == "list":
            runs = service.list_runs(limit=args.limit)
            if args.json:
                print(json.dumps([run.to_dict() for run in runs], indent=2))
            elif not runs:
                print("No self-repair runs recorded.")
            else:
                for run in runs:
                    print(
                        f"- {run.run_id} [{run.status}] "
                        f"operations={len(run.operations)}"
                    )
            return 0
        if command == "show":
            run_id = service.resolve_run_id(args.run_id)
            _print_run(service.load_run(run_id), as_json=bool(args.json))
            return 0
        raise PcAgentError(f"Unknown self-repair command: {command}")
    except PcAgentError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised via console script
    raise SystemExit(main())
