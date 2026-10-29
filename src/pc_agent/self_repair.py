"""Isolated, reversible repair of the agent's own source tree.

Phase 17 deliberately does not let a model rewrite core code.  A user first
captures a known-good baseline after a verified install.  Diagnosis can then
produce only two deterministic operation types:

* restore an eligible ``src``/``tests``/``pyproject.toml`` file from that
  baseline; and
* delete generated Python cache files.

Every repair is staged in a separate copy, checked there, applied only by a
separate explicit command, backed up before mutation, post-checked, and can
be rolled back.  Unknown/extra source files are reported but never deleted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess  # nosec B404 - fixed argv vectors only, never a shell
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .errors import PcAgentError
from .logging_config import redact_text

_BASELINE_FORMAT = 1
_RUN_FORMAT = 1
_ELIGIBLE_ROOTS = ("src", "tests")
_ELIGIBLE_FILES = ("pyproject.toml",)
_EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".patcher-backups",
}
_EXCLUDED_TOP_LEVEL = {"data", "workspace", ".venv", ".git", ".patcher-backups"}
_MAX_CHECK_OUTPUT = 12_000
_CHECK_PROFILES: dict[str, tuple[str, ...]] = {
    "static": ("static",),
    "import": ("static", "import"),
    "full": ("static", "import", "pytest", "Ruff"),
}
_RUN_STATUSES = {
    "staging",
    "staged",
    "stage_failed",
    "applying",
    "applied",
    "apply_failed",
    "apply_failed_rolled_back",
    "rolled_back",
}

CommandRunner = Callable[
    [tuple[str, ...], Path, Mapping[str, str], float], tuple[int, str, float]
]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise PcAgentError(f"Unsafe self-repair path in manifest: {raw!r}.")
    return path


def _inside(root: Path, relative: str | Path) -> Path:
    candidate = (root / Path(relative)).resolve()
    resolved_root = root.resolve()
    if not candidate.is_relative_to(resolved_root):
        raise PcAgentError(f"Self-repair path escapes its root: {relative}.")
    return candidate


def _is_eligible(relative: Path) -> bool:
    if relative.as_posix() in _ELIGIBLE_FILES:
        return True
    return (
        len(relative.parts) > 1
        and relative.parts[0] in _ELIGIBLE_ROOTS
        and "__pycache__" not in relative.parts
        and not any(part.endswith(".egg-info") for part in relative.parts)
        and relative.suffix not in {".pyc", ".pyo"}
    )


def _is_cache(relative: Path) -> bool:
    return (
        len(relative.parts) > 1
        and relative.parts[0] in _ELIGIBLE_ROOTS
        and ("__pycache__" in relative.parts or relative.suffix in {".pyc", ".pyo"})
    )


@dataclass(frozen=True, slots=True)
class BaselineStatus:
    exists: bool
    created_at: str = ""
    app_version: str = ""
    file_count: int = 0
    valid: bool = True
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "exists": self.exists,
            "created_at": self.created_at,
            "app_version": self.app_version,
            "file_count": self.file_count,
            "valid": self.valid,
            "detail": self.detail,
        }

    def summary(self) -> str:
        if not self.valid:
            return f"baseline=invalid ({self.detail or 'unreadable manifest'})"
        if not self.exists:
            return "baseline=none"
        return (
            f"baseline=present; files={self.file_count}; "
            f"version={self.app_version or 'unknown'}; created={self.created_at}"
        )


@dataclass(frozen=True, slots=True)
class RepairIssue:
    code: str
    path: str
    detail: str
    severity: str = "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "detail": self.detail,
            "severity": self.severity,
        }


@dataclass(frozen=True, slots=True)
class RepairOperation:
    kind: str  # "restore" | "delete_cache"
    path: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path}


@dataclass(frozen=True, slots=True)
class SelfRepairDiagnosis:
    baseline: BaselineStatus
    issues: tuple[RepairIssue, ...]
    operations: tuple[RepairOperation, ...]
    extra_files: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return not self.issues and not self.operations

    @property
    def blocked(self) -> bool:
        """True when staging from this diagnosis would be unsafe."""

        return any(
            issue.code
            in {
                "baseline_snapshot_corrupt",
                "baseline_invalid_path",
                "baseline_version_mismatch",
                "baseline_invalid",
                "eligible_symlink",
            }
            for issue in self.issues
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline.to_dict(),
            "issues": [issue.to_dict() for issue in self.issues],
            "operations": [operation.to_dict() for operation in self.operations],
            "extra_files": list(self.extra_files),
            "healthy": self.healthy,
            "blocked": self.blocked,
            "plan_hash": self.plan_hash,
        }

    @property
    def plan_hash(self) -> str:
        payload = json.dumps(
            [operation.to_dict() for operation in self.operations],
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def summary(self) -> str:
        return (
            f"{self.baseline.summary()}; issues={len(self.issues)}; "
            f"repair_operations={len(self.operations)}; "
            f"extra_files={len(self.extra_files)}; plan={self.plan_hash[:16]}"
        )


@dataclass(frozen=True, slots=True)
class RepairCheckResult:
    name: str
    success: bool
    exit_code: int
    output: str = ""
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "success": self.success,
            "exit_code": self.exit_code,
            "output": self.output[-_MAX_CHECK_OUTPUT:],
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


@dataclass(slots=True)
class SelfRepairRun:
    run_id: str
    status: str
    created_at: str
    plan_hash: str
    operations: tuple[RepairOperation, ...]
    issues: tuple[RepairIssue, ...] = ()
    check_profile: str = "import"
    checks: tuple[RepairCheckResult, ...] = ()
    post_checks: tuple[RepairCheckResult, ...] = ()
    backups: dict[str, dict[str, Any]] = field(default_factory=dict)
    applied_at: str = ""
    rolled_back_at: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": _RUN_FORMAT,
            "run_id": self.run_id,
            "status": self.status,
            "created_at": self.created_at,
            "plan_hash": self.plan_hash,
            "operations": [operation.to_dict() for operation in self.operations],
            "issues": [issue.to_dict() for issue in self.issues],
            "check_profile": self.check_profile,
            "checks": [check.to_dict() for check in self.checks],
            "post_checks": [check.to_dict() for check in self.post_checks],
            "backups": self.backups,
            "applied_at": self.applied_at,
            "rolled_back_at": self.rolled_back_at,
            "error": self.error,
        }


class SelfRepairService:
    """Baseline/stage/apply/rollback operations for one install root."""

    def __init__(
        self,
        root_dir: str | Path,
        data_dir: str | Path,
        *,
        python_executable: str | None = None,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.base_dir = self.data_dir / "self-repair"
        self.baseline_dir = self.base_dir / "baseline"
        self.baseline_files_dir = self.baseline_dir / "files"
        self.baseline_manifest_path = self.baseline_dir / "manifest.json"
        self.runs_dir = self.base_dir / "runs"
        self._python = python_executable or sys.executable
        self._runner = command_runner or self._default_runner

    # ------------------------------------------------------------------
    # Baseline and diagnosis
    # ------------------------------------------------------------------
    def baseline_status(self) -> BaselineStatus:
        try:
            manifest = self._load_baseline_manifest(required=False)
        except PcAgentError as exc:
            return BaselineStatus(False, valid=False, detail=str(exc))
        if manifest is None:
            return BaselineStatus(False)
        files = manifest.get("files")
        return BaselineStatus(
            True,
            created_at=str(manifest.get("created_at") or ""),
            app_version=str(manifest.get("app_version") or ""),
            file_count=len(files) if isinstance(files, Mapping) else 0,
        )

    def create_baseline(
        self,
        *,
        force: bool = False,
        check_profile: str = "import",
        timeout_seconds: float = 900.0,
    ) -> BaselineStatus:
        if self.baseline_manifest_path.is_file() and not force:
            raise PcAgentError(
                "A self-repair baseline already exists. Re-run with --force "
                "only after the current install has passed its tests."
            )
        issues = self._static_issues(self.root_dir)
        errors = [issue for issue in issues if issue.severity == "error"]
        if errors:
            detail = "; ".join(
                f"{issue.path}: {issue.detail}" for issue in errors[:5]
            )
            raise PcAgentError(
                "Refusing to capture a baseline from an unhealthy source "
                f"tree: {detail}"
            )
        if check_profile not in {"static", "import", "full"}:
            raise PcAgentError("Self-repair checks must be static, import or full.")
        if timeout_seconds <= 0 or timeout_seconds > 3600:
            raise PcAgentError("Self-repair check timeout must be 1..3600 seconds.")
        checks = self._run_checks(
            self.root_dir,
            profile=check_profile,
            timeout_seconds=timeout_seconds,
        )
        if not all(check.success for check in checks):
            detail = "; ".join(
                f"{check.name}: {check.output.strip().splitlines()[-1] if check.output.strip() else 'failed'}"
                for check in checks
                if not check.success
            )
            raise PcAgentError(
                "Refusing to capture a baseline because its health checks "
                f"failed: {detail}"
            )
        eligible = self._eligible_files(self.root_dir)
        if not eligible:
            raise PcAgentError("No eligible source files were found for a baseline.")

        self.base_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = self.base_dir / f".baseline-{secrets.token_hex(4)}"
        temp_files = temp_dir / "files"
        records: dict[str, dict[str, Any]] = {}
        try:
            for relative in eligible:
                source = self.root_dir / relative
                destination = temp_files / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                records[relative.as_posix()] = {
                    "sha256": _sha256(source),
                    "size": source.stat().st_size,
                }
            self._write_json(
                temp_dir / "manifest.json",
                {
                    "format": _BASELINE_FORMAT,
                    "created_at": _utc_now(),
                    "app_version": self._package_version(self.root_dir) or __version__,
                    "files": records,
                },
            )
            if self.baseline_dir.exists():
                shutil.rmtree(self.baseline_dir)
            temp_dir.replace(self.baseline_dir)
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
        return self.baseline_status()

    def diagnose(self) -> SelfRepairDiagnosis:
        issues = list(self._static_issues(self.root_dir))
        operations: list[RepairOperation] = []
        baseline = self.baseline_status()
        baseline_files: dict[str, Mapping[str, Any]] = {}
        try:
            manifest = self._load_baseline_manifest(required=False)
        except PcAgentError as exc:
            issues.append(
                RepairIssue(
                    "baseline_invalid",
                    "data/self-repair/baseline",
                    str(exc),
                )
            )
            manifest = None
        if manifest is not None:
            baseline_version = str(manifest.get("app_version") or "")
            live_version = self._package_version(self.root_dir)
            if baseline_version and live_version and baseline_version != live_version:
                issues.append(
                    RepairIssue(
                        "baseline_version_mismatch",
                        "data/self-repair/baseline",
                        (
                            f"Baseline is for v{baseline_version}, but the live "
                            f"package is v{live_version}; capture a new baseline "
                            "after this version passes its tests."
                        ),
                    )
                )
            raw_files = manifest.get("files")
            if isinstance(raw_files, Mapping):
                baseline_files = {
                    str(path): value
                    for path, value in raw_files.items()
                    if isinstance(value, Mapping)
                }
            for raw_path, record in sorted(baseline_files.items()):
                relative = _safe_relative(raw_path)
                if not _is_eligible(relative):
                    issues.append(
                        RepairIssue(
                            "baseline_invalid_path",
                            raw_path,
                            "Baseline contains a non-eligible path; it will not be restored.",
                        )
                    )
                    continue
                snapshot = self.baseline_files_dir / relative
                expected = str(record.get("sha256") or "")
                expected_size = record.get("size")
                if (
                    snapshot.is_symlink()
                    or not snapshot.is_file()
                    or _sha256(snapshot) != expected
                    or (
                        isinstance(expected_size, int)
                        and snapshot.stat().st_size != expected_size
                    )
                ):
                    issues.append(
                        RepairIssue(
                            "baseline_snapshot_corrupt",
                            raw_path,
                            "Baseline snapshot is missing or its hash does not match.",
                        )
                    )
                    continue
                live = self.root_dir / relative
                if live.is_symlink():
                    issues.append(
                        RepairIssue(
                            "eligible_symlink",
                            raw_path,
                            "Eligible live path is a symbolic link; self-repair will not follow or replace it automatically.",
                        )
                    )
                    continue
                if not live.is_file():
                    issues.append(
                        RepairIssue(
                            "baseline_missing",
                            raw_path,
                            "File is missing compared with the known-good baseline.",
                        )
                    )
                    operations.append(RepairOperation("restore", raw_path))
                elif _sha256(live) != expected:
                    issues.append(
                        RepairIssue(
                            "baseline_changed",
                            raw_path,
                            "File differs from the known-good baseline.",
                            "warning",
                        )
                    )
                    operations.append(RepairOperation("restore", raw_path))
        elif not baseline.exists and baseline.valid:
            issues.append(
                RepairIssue(
                    "baseline_missing",
                    "data/self-repair/baseline",
                    "No known-good baseline exists; capture one after a verified test run.",
                    "warning",
                )
            )

        for relative in self._cache_files(self.root_dir):
            operations.append(
                RepairOperation("delete_cache", relative.as_posix())
            )

        current = {
            relative.as_posix() for relative in self._eligible_files(self.root_dir)
        }
        extras = tuple(sorted(current - set(baseline_files))) if baseline.exists else ()
        # Operations are hash-stable in a canonical order.
        unique = {(operation.kind, operation.path): operation for operation in operations}
        ordered = tuple(unique[key] for key in sorted(unique))
        return SelfRepairDiagnosis(
            baseline=baseline,
            issues=tuple(issues),
            operations=ordered,
            extra_files=extras,
        )

    # ------------------------------------------------------------------
    # Stage / apply / rollback
    # ------------------------------------------------------------------
    def stage(
        self,
        *,
        check_profile: str = "import",
        timeout_seconds: float = 900.0,
    ) -> SelfRepairRun:
        if check_profile not in {"static", "import", "full"}:
            raise PcAgentError("Self-repair checks must be static, import or full.")
        if timeout_seconds <= 0 or timeout_seconds > 3600:
            raise PcAgentError("Self-repair check timeout must be 1..3600 seconds.")
        diagnosis = self.diagnose()
        if diagnosis.blocked:
            raise PcAgentError(
                "Diagnosis is blocked by a corrupt, invalid or version-"
                "mismatched baseline; refusing to stage a repair."
            )
        if not diagnosis.operations:
            raise PcAgentError(
                "Diagnosis found no deterministic repair operation to stage."
            )

        run_id = (
            datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + secrets.token_hex(3)
        )
        run_dir = self.runs_dir / run_id
        staging = run_dir / "staging"
        run_dir.mkdir(parents=True, exist_ok=False)
        run = SelfRepairRun(
            run_id=run_id,
            status="staging",
            created_at=_utc_now(),
            plan_hash=diagnosis.plan_hash,
            operations=diagnosis.operations,
            issues=diagnosis.issues,
            check_profile=check_profile,
        )
        self._save_run(run)
        try:
            self._copy_project_to_staging(staging)
            self._apply_operations(staging, run.operations, staged=True)
            run.checks = self._run_checks(
                staging, profile=check_profile, timeout_seconds=timeout_seconds
            )
            run.status = (
                "staged"
                if all(check.success for check in run.checks)
                else "stage_failed"
            )
            if run.status == "stage_failed":
                run.error = "One or more staged checks failed; apply is blocked."
        except Exception as exc:
            run.status = "stage_failed"
            run.error = str(exc)
            self._save_run(run)
            raise PcAgentError(f"Self-repair staging failed: {exc}") from exc
        self._save_run(run)
        return run

    def apply(
        self,
        run_id: str,
        *,
        timeout_seconds: float = 300.0,
        post_check_profile: str | None = None,
    ) -> SelfRepairRun:
        run = self.load_run(run_id)
        if run.status != "staged":
            raise PcAgentError(
                f"Self-repair run {run_id} is {run.status}; only a staged run can apply."
            )
        current = self.diagnose()
        if current.plan_hash != run.plan_hash:
            raise PcAgentError(
                "The source tree changed after staging; this repair plan is "
                "stale. Diagnose and stage again."
            )
        run_dir = self._run_dir(run_id)
        staging = run_dir / "staging"
        backup_dir = run_dir / "backup"
        if not staging.is_dir():
            raise PcAgentError("The staged repair tree is missing.")
        backup_dir.mkdir(parents=True, exist_ok=True)
        run.status = "applying"
        run.backups = {}
        self._save_run(run)
        try:
            for operation in run.operations:
                relative = _safe_relative(operation.path)
                live = _inside(self.root_dir, relative)
                backup = backup_dir / relative
                existed = live.is_file()
                run.backups[operation.path] = {
                    "existed": existed,
                    "sha256": _sha256(live) if existed else "",
                }
                if existed:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(live, backup)
            self._save_run(run)
            self._apply_operations(
                self.root_dir, run.operations, staged=False, staging_root=staging
            )
            profile = post_check_profile or "import"
            if profile not in {"static", "import", "full"}:
                raise PcAgentError("Self-repair checks must be static, import or full.")
            run.post_checks = self._run_checks(
                self.root_dir, profile=profile, timeout_seconds=timeout_seconds
            )
            if not all(check.success for check in run.post_checks):
                raise PcAgentError("post-apply checks failed")
        except Exception as exc:
            rollback_error = ""
            try:
                self._restore_backups(run)
            except Exception as rollback_exc:  # noqa: BLE001
                rollback_error = f"; rollback also failed: {rollback_exc}"
            run.status = "apply_failed_rolled_back" if not rollback_error else "apply_failed"
            run.error = f"{exc}{rollback_error}"
            self._save_run(run)
            raise PcAgentError(
                f"Self-repair apply failed and was rolled back: {exc}{rollback_error}"
            ) from exc
        run.status = "applied"
        run.applied_at = _utc_now()
        run.error = ""
        self._save_run(run)
        return run

    def rollback(self, run_id: str) -> SelfRepairRun:
        run = self.load_run(run_id)
        if run.status != "applied":
            raise PcAgentError(
                f"Self-repair run {run_id} is {run.status}; only an applied run can roll back."
            )
        self._restore_backups(run)
        run.status = "rolled_back"
        run.rolled_back_at = _utc_now()
        self._save_run(run)
        return run

    def resolve_run_id(self, reference: str) -> str:
        value = reference.strip()
        if value.lower() != "latest":
            return value
        runs = self.list_runs(limit=1)
        if not runs:
            raise PcAgentError("No self-repair runs have been recorded.")
        return runs[0].run_id

    def load_run(self, run_id: str) -> SelfRepairRun:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
            raise PcAgentError("Invalid self-repair run id.")
        path = self._run_dir(run_id) / "manifest.json"
        if not path.is_file():
            raise PcAgentError(f"Self-repair run not found: {run_id}.")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PcAgentError("Self-repair run manifest is unreadable.") from exc
        if not isinstance(data, dict) or data.get("format") != _RUN_FORMAT:
            raise PcAgentError("Self-repair run manifest has an unsupported format.")
        operations: list[RepairOperation] = []
        for item in data.get("operations") or []:
            if not isinstance(item, Mapping):
                raise PcAgentError("Self-repair run contains an invalid operation.")
            kind = str(item.get("kind") or "")
            raw_path = str(item.get("path") or "")
            relative = _safe_relative(raw_path)
            if kind == "restore" and not _is_eligible(relative):
                raise PcAgentError("Self-repair run contains a non-eligible restore path.")
            if kind == "delete_cache" and not _is_cache(relative):
                raise PcAgentError("Self-repair run contains a non-cache delete path.")
            if kind not in {"restore", "delete_cache"}:
                raise PcAgentError("Self-repair run contains an unknown operation.")
            operations.append(RepairOperation(kind, raw_path))
        issues = tuple(
            RepairIssue(
                code=str(item.get("code") or "unknown"),
                path=str(item.get("path") or ""),
                detail=str(item.get("detail") or ""),
                severity=str(item.get("severity") or "error"),
            )
            for item in data.get("issues") or []
            if isinstance(item, Mapping)
        )

        def checks(key: str) -> tuple[RepairCheckResult, ...]:
            results = []
            for item in data.get(key) or []:
                if isinstance(item, Mapping):
                    results.append(
                        RepairCheckResult(
                            name=str(item.get("name") or "check"),
                            success=bool(item.get("success")),
                            exit_code=int(item.get("exit_code") or 0),
                            output=str(item.get("output") or ""),
                            elapsed_seconds=float(item.get("elapsed_seconds") or 0),
                        )
                    )
            return tuple(results)

        backups = data.get("backups")
        status = str(data.get("status") or "unknown")
        check_profile = str(data.get("check_profile") or "import")
        if str(data.get("run_id") or "") != run_id:
            raise PcAgentError("Self-repair run manifest id does not match its folder.")
        if status not in _RUN_STATUSES:
            raise PcAgentError("Self-repair run manifest has an unknown status.")
        if check_profile not in _CHECK_PROFILES:
            raise PcAgentError("Self-repair run manifest has an unknown check profile.")
        staged_checks = checks("checks")
        post_checks = checks("post_checks")
        expected_plan_hash = hashlib.sha256(
            json.dumps(
                [operation.to_dict() for operation in operations], sort_keys=True
            ).encode("utf-8")
        ).hexdigest()
        if str(data.get("plan_hash") or "") != expected_plan_hash:
            raise PcAgentError("Self-repair run manifest plan hash does not match its operations.")
        if status in {
            "staged",
            "applying",
            "applied",
            "apply_failed",
            "apply_failed_rolled_back",
            "rolled_back",
        }:
            expected_names = _CHECK_PROFILES[check_profile]
            if tuple(check.name for check in staged_checks) != expected_names or not all(
                check.success and check.exit_code == 0 for check in staged_checks
            ):
                raise PcAgentError(
                    "Self-repair run staged checks are missing or did not pass."
                )
        if status in {"applied", "rolled_back"} and (
            not post_checks
            or not all(check.success and check.exit_code == 0 for check in post_checks)
        ):
            raise PcAgentError(
                "Self-repair applied run post-checks are missing or did not pass."
            )
        return SelfRepairRun(
            run_id=run_id,
            status=status,
            created_at=str(data.get("created_at") or ""),
            plan_hash=expected_plan_hash,
            operations=tuple(operations),
            issues=issues,
            check_profile=check_profile,
            checks=staged_checks,
            post_checks=post_checks,
            backups=dict(backups) if isinstance(backups, Mapping) else {},
            applied_at=str(data.get("applied_at") or ""),
            rolled_back_at=str(data.get("rolled_back_at") or ""),
            error=str(data.get("error") or ""),
        )

    def list_runs(self, *, limit: int = 20) -> tuple[SelfRepairRun, ...]:
        if not self.runs_dir.is_dir():
            return ()
        try:
            run_ids = sorted(
                (path.name for path in self.runs_dir.iterdir() if path.is_dir()),
                reverse=True,
            )
        except OSError:
            return ()
        runs: list[SelfRepairRun] = []
        for run_id in run_ids[: max(1, min(limit, 100))]:
            try:
                runs.append(self.load_run(run_id))
            except PcAgentError:
                continue
        return tuple(runs)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _run_dir(self, run_id: str) -> Path:
        return _inside(self.runs_dir, run_id)

    def _save_run(self, run: SelfRepairRun) -> None:
        run_dir = self._run_dir(run.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(run_dir / "manifest.json", run.to_dict())

    @staticmethod
    def _write_json(path: Path, data: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(path)

    def _load_baseline_manifest(
        self, *, required: bool
    ) -> dict[str, Any] | None:
        if not self.baseline_manifest_path.is_file():
            if required:
                raise PcAgentError("No self-repair baseline exists.")
            return None
        try:
            data = json.loads(
                self.baseline_manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise PcAgentError("Self-repair baseline manifest is unreadable.") from exc
        if not isinstance(data, dict) or data.get("format") != _BASELINE_FORMAT:
            raise PcAgentError("Self-repair baseline has an unsupported format.")
        files = data.get("files")
        if not isinstance(files, Mapping) or not files:
            raise PcAgentError("Self-repair baseline manifest has no file records.")
        if not isinstance(data.get("app_version"), str) or not isinstance(
            data.get("created_at"), str
        ):
            raise PcAgentError("Self-repair baseline manifest metadata is invalid.")
        for raw_path, record in files.items():
            if not isinstance(raw_path, str) or not isinstance(record, Mapping):
                raise PcAgentError("Self-repair baseline manifest has an invalid file record.")
            digest = record.get("sha256")
            size = record.get("size")
            if (
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
            ):
                raise PcAgentError("Self-repair baseline manifest has an invalid file record.")
        return data

    @staticmethod
    def _eligible_files(root: Path) -> tuple[Path, ...]:
        results: list[Path] = []
        for relative_root in _ELIGIBLE_ROOTS:
            start = root / relative_root
            if not start.is_dir():
                continue
            for path in start.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    relative = path.relative_to(root)
                    if _is_eligible(relative):
                        results.append(relative)
        for name in _ELIGIBLE_FILES:
            if (root / name).is_file():
                results.append(Path(name))
        return tuple(sorted(set(results), key=lambda item: item.as_posix()))

    @staticmethod
    def _cache_files(root: Path) -> tuple[Path, ...]:
        results: list[Path] = []
        for relative_root in _ELIGIBLE_ROOTS:
            start = root / relative_root
            if start.is_dir():
                for path in start.rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        relative = path.relative_to(root)
                        if _is_cache(relative):
                            results.append(relative)
        return tuple(sorted(set(results), key=lambda item: item.as_posix()))

    @classmethod
    def _static_issues(cls, root: Path) -> tuple[RepairIssue, ...]:
        issues: list[RepairIssue] = []
        for relative in cls._eligible_files(root):
            if relative.suffix != ".py":
                continue
            path = root / relative
            try:
                compile(path.read_bytes(), str(relative), "exec", dont_inherit=True)
            except (OSError, SyntaxError, ValueError) as exc:
                issues.append(
                    RepairIssue(
                        "python_compile",
                        relative.as_posix(),
                        f"{exc.__class__.__name__}: {exc}",
                    )
                )
        pyproject_version = cls._pyproject_version(root)
        package_version = cls._package_version(root)
        if pyproject_version and package_version and pyproject_version != package_version:
            issues.append(
                RepairIssue(
                    "version_mismatch",
                    "pyproject.toml",
                    f"pyproject version {pyproject_version} != package version {package_version}.",
                )
            )
        return tuple(issues)

    @staticmethod
    def _pyproject_version(root: Path) -> str:
        path = root / "pyproject.toml"
        if not path.is_file():
            return ""
        try:
            import tomllib

            data = tomllib.loads(path.read_text(encoding="utf-8"))
            project = data.get("project") if isinstance(data, Mapping) else None
            return str(project.get("version") or "") if isinstance(project, Mapping) else ""
        except Exception:  # noqa: BLE001 - diagnosis should not crash on bad TOML.
            match = re.search(
                r'(?m)^version\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8", errors="replace")
            )
            return match.group(1) if match else ""

    @staticmethod
    def _package_version(root: Path) -> str:
        path = root / "src" / "pc_agent" / "__init__.py"
        if not path.is_file():
            return ""
        match = re.search(
            r'(?m)^__version__\s*=\s*"([^"]+)"',
            path.read_text(encoding="utf-8", errors="replace"),
        )
        return match.group(1) if match else ""

    def _copy_project_to_staging(self, staging: Path) -> None:
        staging.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.root_dir.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.root_dir)
            if relative.parts and relative.parts[0] in _EXCLUDED_TOP_LEVEL:
                continue
            if set(relative.parts) & _EXCLUDED_PARTS:
                continue
            if relative.name == ".env" or relative.suffix in {".pyc", ".pyo"}:
                continue
            if any(part.endswith(".egg-info") for part in relative.parts):
                continue
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)

    def _apply_operations(
        self,
        target_root: Path,
        operations: tuple[RepairOperation, ...],
        *,
        staged: bool,
        staging_root: Path | None = None,
    ) -> None:
        for operation in operations:
            relative = _safe_relative(operation.path)
            target = _inside(target_root, relative)
            if operation.kind == "restore":
                source_root = staging_root if not staged else None
                source = (
                    _inside(source_root, relative)
                    if source_root is not None
                    else _inside(self.baseline_files_dir, relative)
                )
                if not source.is_file():
                    raise PcAgentError(f"Repair source is missing: {operation.path}.")
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_name(f".{target.name}.self-repair.tmp")
                shutil.copy2(source, temp)
                temp.replace(target)
            elif operation.kind == "delete_cache":
                if target.is_file():
                    target.unlink()
                parent = target.parent
                if parent.name == "__pycache__" and parent.is_dir():
                    try:
                        parent.rmdir()
                    except OSError:
                        pass
            else:  # Defensive; load/diagnose validate operation kinds.
                raise PcAgentError(f"Unknown self-repair operation: {operation.kind}.")

    def _restore_backups(self, run: SelfRepairRun) -> None:
        backup_dir = self._run_dir(run.run_id) / "backup"
        for operation in reversed(run.operations):
            relative = _safe_relative(operation.path)
            record = run.backups.get(operation.path)
            if not isinstance(record, Mapping):
                raise PcAgentError(f"Missing backup record for {operation.path}.")
            live = _inside(self.root_dir, relative)
            if record.get("existed"):
                backup = backup_dir / relative
                if not backup.is_file():
                    raise PcAgentError(f"Backup file is missing: {operation.path}.")
                live.parent.mkdir(parents=True, exist_ok=True)
                temp = live.with_name(f".{live.name}.self-repair.tmp")
                shutil.copy2(backup, temp)
                temp.replace(live)
            elif live.is_file():
                live.unlink()

    def _run_checks(
        self,
        root: Path,
        *,
        profile: str,
        timeout_seconds: float,
    ) -> tuple[RepairCheckResult, ...]:
        results: list[RepairCheckResult] = []
        started = time.perf_counter()
        issues = self._static_issues(root)
        static_errors = [issue for issue in issues if issue.severity == "error"]
        results.append(
            RepairCheckResult(
                name="static",
                success=not static_errors,
                exit_code=0 if not static_errors else 1,
                output="; ".join(
                    f"{issue.path}: {issue.detail}" for issue in static_errors[:10]
                )
                or "Python compile and version checks passed.",
                elapsed_seconds=time.perf_counter() - started,
            )
        )
        if profile == "static":
            return tuple(results)

        commands: list[tuple[str, tuple[str, ...]]] = [
            (
                "import",
                (
                    self._python,
                    "-c",
                    (
                        "import pc_agent; from pc_agent.cli import build_parser; "
                        "build_parser(); print(pc_agent.__version__)"
                    ),
                ),
            )
        ]
        if profile == "full":
            commands.extend(
                [
                    ("pytest", (self._python, "-m", "pytest", "-q")),
                    ("Ruff", (self._python, "-m", "Ruff", "check")),
                ]
            )
        environment = self._check_environment(root)
        for name, argv in commands:
            exit_code, output, elapsed = self._runner(
                argv, root, environment, timeout_seconds
            )
            results.append(
                RepairCheckResult(
                    name=name,
                    success=exit_code == 0,
                    exit_code=exit_code,
                    output=redact_text(output[-_MAX_CHECK_OUTPUT:]),
                    elapsed_seconds=elapsed,
                )
            )
            if exit_code != 0:
                break
        return tuple(results)

    @staticmethod
    def _check_environment(root: Path) -> dict[str, str]:
        environment: dict[str, str] = {}
        for key in (
            "PATH",
            "SystemRoot",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
            "LOCALAPPDATA",
            "APPDATA",
        ):
            value = os.environ.get(key)
            if value:
                environment[key] = value
        source = str((root / "src").resolve())
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = source + (os.pathsep + existing if existing else "")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PC_AGENT_CREDENTIAL_STORE"] = "env"
        return environment

    @staticmethod
    def _default_runner(
        argv: tuple[str, ...],
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> tuple[int, str, float]:
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                list(argv),
                cwd=cwd,
                env=dict(environment),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
            return (
                int(completed.returncode),
                completed.stdout or "",
                time.perf_counter() - started,
            )
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
            return 124, output + "\nCheck timed out.", time.perf_counter() - started
        except OSError as exc:
            return 127, f"Could not start check: {exc}", time.perf_counter() - started
