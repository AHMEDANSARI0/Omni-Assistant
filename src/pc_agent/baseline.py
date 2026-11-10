"""Canonical transformation-baseline identity and document checks.

Transformation Phase 1 freezes one source of truth for the post-audit
program.  The delivered Phase 60 tree is canonical; the audited GitHub
Phase 58 commit remains recorded here only as the comparison point that
was reconciled into this baseline.  This module contains no runtime state
and never treats documentation presence alone as a platform-acceptance
claim.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TRANSFORMATION_PHASE = 18
TRANSFORMATION_LABEL = "TRANSFORMATION PHASE 18 Production Hardening"
CANONICAL_SOURCE = "Delivered Phase 60 staging tree (0.62.0)"
GITHUB_AUDIT_REPOSITORY = "https://github.com/AHMEDANSARI0/Omni-Assistant.git"
GITHUB_AUDIT_BRANCH = "main"
GITHUB_AUDIT_COMMIT = "89610f3d18f9348b6fe8ead679bab81ee34155df"
GITHUB_AUDIT_VERSION = "0.60.0"
PRE_TRANSFORMATION_VERSION = "0.62.0"
PREVIOUS_TRANSFORMATION_VERSION = "0.82.11"
WINDOWS_ACCEPTANCE_STATUS = "pending_user_windows_evidence"
#: Recorded Windows evidence lives here, relative to the data directory. Its
#: presence is the *only* thing that can change the acceptance status: the
#: harness on the user's machine writes it, this module re-verifies the hash.
WINDOWS_ACCEPTANCE_EVIDENCE = "windows-acceptance.json"
_WINDOWS_EVIDENCE_HASH_KEY = "body_sha256"
_WINDOWS_EVIDENCE_EXCLUDED = frozenset({"body_sha256"})


def acceptance_body_sha256(payload: dict[str, Any]) -> str:
    """Canonical hash of a report body, excluding the hash field itself.

    Both the harness (writer) and this module (reader) use exactly this
    function, so a recorded file cannot be edited and still look valid.
    """

    import hashlib as _hashlib
    import json as _json

    body = {
        key: value
        for key, value in payload.items()
        if key not in _WINDOWS_EVIDENCE_EXCLUDED
    }
    canonical = _json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return _hashlib.sha256(canonical).hexdigest()


def windows_acceptance_evidence(root_dir: Path) -> dict[str, Any]:
    """The recorded Windows evidence for this install, or the pending default."""

    import json as _json

    default = {
        "status": WINDOWS_ACCEPTANCE_STATUS,
        "recorded": False,
        "path": "",
        "generated_at": "",
        "gates": 0,
        "detail": "no verified Windows evidence has been recorded for this install",
    }
    path = Path(root_dir).expanduser() / "data" / WINDOWS_ACCEPTANCE_EVIDENCE
    if not path.is_file():
        return default
    try:
        payload = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            **default,
            "status": WINDOWS_ACCEPTANCE_STATUS,
            "path": str(path),
            "detail": "recorded evidence is unreadable, so it does not count",
        }
    if not isinstance(payload, dict):
        return {**default, "path": str(path), "detail": "recorded evidence is not an object"}
    recorded_hash = str(payload.get(_WINDOWS_EVIDENCE_HASH_KEY) or "")
    if not recorded_hash or recorded_hash != acceptance_body_sha256(payload):
        return {
            **default,
            "path": str(path),
            "detail": "recorded evidence failed its own hash check, so it does not count",
        }
    if payload.get("status") != "passed" or payload.get("platform") != "windows":
        return {
            **default,
            "path": str(path),
            "detail": (
                "recorded evidence is not a passed Windows run "
                f"(status={payload.get('status')!r}, platform={payload.get('platform')!r})"
            ),
        }
    generated_at = str(payload.get("generated_at") or "")
    return {
        "status": f"verified:{generated_at[:10] or 'unknown-date'}",
        "recorded": True,
        "path": str(path),
        "generated_at": generated_at,
        "gates": len(payload.get("gates") or []),
        "version": str(payload.get("version") or ""),
        "detail": f"verified Windows evidence recorded at {generated_at or 'unknown time'}",
    }

REQUIRED_BASELINE_DOCUMENTS = (
    "ARCHITECTURE.md",
    "AUDIT_REPORT.md",
    "DEVELOPER_GUIDE.md",
    "INSTALLATION_GUIDE.md",
    "PLUGIN_DEVELOPMENT_GUIDE.md",
    "IMPLEMENTATION_ROADMAP.md",
    "FEATURE_MATRIX.md",
    "SECURITY_REPORT.md",
    "PERFORMANCE_REPORT.md",
    "TEST_REPORT.md",
    "docs/transformation_phase1.md",
    "docs/transformation_phase2.md",
    "docs/transformation_phase3.md",
    "docs/transformation_phase4.md",
    "docs/transformation_phase5.md",
    "docs/transformation_phase6.md",
    "docs/transformation_phase7.md",
    "docs/transformation_phase8.md",
    "docs/transformation_phase9.md",
    "docs/transformation_phase10.md",
    "docs/transformation_phase11.md",
    "docs/transformation_phase12.md",
    "docs/transformation_phase13.md",
    "docs/transformation_phase14.md",
    "docs/transformation_phase15.md",
    "API_DOCUMENTATION.md",
    "docs/windows_acceptance.md",
    "docs/voice_and_authenticode.md",
)


@dataclass(frozen=True, slots=True)
class BaselineStatus:
    transformation_phase: int
    label: str
    canonical_source: str
    github_audit_commit: str
    github_audit_version: str
    pre_transformation_version: str
    windows_acceptance_status: str
    documents_present: tuple[str, ...]
    documents_missing: tuple[str, ...]
    env_example_ignored: bool
    direct_cryptography_dependency: bool

    @property
    def ok(self) -> bool:
        return (
            not self.documents_missing
            and not self.env_example_ignored
            and self.direct_cryptography_dependency
        )

    def summary(self) -> str:
        missing = ", ".join(self.documents_missing) or "none"
        return (
            f"phase={self.transformation_phase}; canonical={self.canonical_source}; "
            f"github_audit={self.github_audit_commit[:12]}; "
            f"documents_missing={missing}; "
            f"env_example_ignored={'yes' if self.env_example_ignored else 'no'}; "
            "direct_cryptography="
            f"{'yes' if self.direct_cryptography_dependency else 'no'}; "
            f"windows_acceptance={self.windows_acceptance_status}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "transformation_phase": self.transformation_phase,
            "label": self.label,
            "canonical_source": self.canonical_source,
            "github_audit_repository": GITHUB_AUDIT_REPOSITORY,
            "github_audit_branch": GITHUB_AUDIT_BRANCH,
            "github_audit_commit": self.github_audit_commit,
            "github_audit_version": self.github_audit_version,
            "pre_transformation_version": self.pre_transformation_version,
            "windows_acceptance_status": self.windows_acceptance_status,
            "documents_present": list(self.documents_present),
            "documents_missing": list(self.documents_missing),
            "env_example_ignored": self.env_example_ignored,
            "direct_cryptography_dependency": self.direct_cryptography_dependency,
            "ok": self.ok,
        }


def baseline_identity() -> dict[str, Any]:
    """Return the static, secret-free release identity for diagnostics."""

    return {
        "transformation_phase": TRANSFORMATION_PHASE,
        "label": TRANSFORMATION_LABEL,
        "canonical_source": CANONICAL_SOURCE,
        "github_audit_repository": GITHUB_AUDIT_REPOSITORY,
        "github_audit_branch": GITHUB_AUDIT_BRANCH,
        "github_audit_commit": GITHUB_AUDIT_COMMIT,
        "github_audit_version": GITHUB_AUDIT_VERSION,
        "pre_transformation_version": PRE_TRANSFORMATION_VERSION,
        "previous_transformation_version": PREVIOUS_TRANSFORMATION_VERSION,
        "windows_acceptance_status": WINDOWS_ACCEPTANCE_STATUS,
    }


def baseline_status(root_dir: Path) -> BaselineStatus:
    """Inspect the project root for the frozen Phase 1 baseline artifacts."""

    root = root_dir.expanduser().resolve()
    present: list[str] = []
    missing: list[str] = []
    for relative in REQUIRED_BASELINE_DOCUMENTS:
        path = root / relative
        if path.is_file() and not path.is_symlink() and path.stat().st_size > 0:
            present.append(relative)
        else:
            missing.append(relative)
    return BaselineStatus(
        transformation_phase=TRANSFORMATION_PHASE,
        label=TRANSFORMATION_LABEL,
        canonical_source=CANONICAL_SOURCE,
        github_audit_commit=GITHUB_AUDIT_COMMIT,
        github_audit_version=GITHUB_AUDIT_VERSION,
        pre_transformation_version=PRE_TRANSFORMATION_VERSION,
        windows_acceptance_status=windows_acceptance_evidence(root)["status"],
        documents_present=tuple(present),
        documents_missing=tuple(missing),
        env_example_ignored=_env_example_ignored(root / ".gitignore"),
        direct_cryptography_dependency=_direct_cryptography_dependency(
            root / "pyproject.toml"
        ),
    )


def _env_example_ignored(gitignore: Path) -> bool:
    if not gitignore.is_file() or gitignore.is_symlink():
        return False
    ignored = False
    try:
        lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line in {".env.example", "/.env.example", "**/.env.example"}:
            ignored = True
        elif line in {"!.env.example", "!/.env.example", "!**/.env.example"}:
            ignored = False
    return ignored


def _direct_cryptography_dependency(pyproject: Path) -> bool:
    if not pyproject.is_file() or pyproject.is_symlink():
        return False
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False
    project = data.get("project") if isinstance(data, dict) else None
    dependencies = project.get("dependencies") if isinstance(project, dict) else None
    if not isinstance(dependencies, list):
        return False
    for dependency in dependencies:
        if not isinstance(dependency, str):
            continue
        name = dependency.strip().lower()
        if name.startswith("cryptography") and (
            len(name) == len("cryptography")
            or name[len("cryptography") :][:1] in {" ", "<", ">", "=", "!", "~", "[", ";"}
        ):
            return True
    return False


__all__ = [
    "CANONICAL_SOURCE",
    "GITHUB_AUDIT_BRANCH",
    "GITHUB_AUDIT_COMMIT",
    "GITHUB_AUDIT_REPOSITORY",
    "GITHUB_AUDIT_VERSION",
    "PREVIOUS_TRANSFORMATION_VERSION",
    "PRE_TRANSFORMATION_VERSION",
    "REQUIRED_BASELINE_DOCUMENTS",
    "TRANSFORMATION_LABEL",
    "TRANSFORMATION_PHASE",
    "WINDOWS_ACCEPTANCE_EVIDENCE",
    "WINDOWS_ACCEPTANCE_STATUS",
    "BaselineStatus",
    "acceptance_body_sha256",
    "baseline_identity",
    "baseline_status",
    "windows_acceptance_evidence",
]
