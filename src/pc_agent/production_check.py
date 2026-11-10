"""End-to-end release-chain verification (Transformation Phase 18).

Everything this module does is a real, observable operation on real files: it
builds a portable release bundle from the running tree, verifies it, proving that
a tampered archive is rejected, signs it with an **ephemeral self-test key**, and
then drives the actual update pipeline - stage, apply, rollback - against a
throw-away install root, comparing tree hashes before and after.

Deliberate properties:

* **No production key is touched.** The signing step generates a fresh Ed25519
  key pair inside the temporary work directory and deletes it with the rest of
  the work directory. The report says so instead of implying a real signing
  ceremony happened.
* **No network.** The transport under test is the local bundle/signature pair,
  not an update feed.
* **Nothing is left behind** unless ``keep=True`` is asked for, and the path is
  reported when it is.
* **Read-only for the user's install.** The update is applied to a copy inside
  the work directory, never to ``settings.root_dir``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings
from .errors import PcAgentError
from .release import (
    RELEASE_MANIFEST,
    build_release_bundle,
    release_candidate_files,
    verify_release_bundle,
)
from .storage.db import LATEST_SCHEMA_VERSION
from .updates import (
    apply_staged_update,
    build_signature_payload,
    default_signature_path,
    rollback_update,
    signature_payload_bytes,
    stage_update,
)

#: Kept short on purpose: the work directory is ``<temp>/<this>/install/<file>``
#: and a long prefix pushes deep release paths towards the Windows MAX_PATH limit.
_TEMP_PREFIX = "pcprod-"

__all__ = [
    "PRODUCTION_REPORT_NAME",
    "ProductionReport",
    "ProductionStep",
    "production_report_path",
    "run_production_check",
    "stored_production_report",
]

#: Where the last report is kept, relative to the data directory.
PRODUCTION_REPORT_NAME = "production-check.json"
_STEP_ORDER = (
    "release_catalog",
    "bundle_build",
    "bundle_verify",
    "tamper_detection",
    "signature_verify",
    "unsigned_refused",
    "stage",
    "apply",
    "rollback",
    "cleanup",
)


@dataclass(frozen=True, slots=True)
class ProductionStep:
    name: str
    status: str  # pass | fail | skip
    detail: str
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class ProductionReport:
    version: str
    generated_at: str
    environment: dict[str, Any] = field(default_factory=dict)
    steps: tuple[ProductionStep, ...] = ()
    workdir: str = ""
    kept: bool = False
    duration_ms: int = 0

    @property
    def failures(self) -> int:
        return sum(1 for step in self.steps if step.status == "fail")

    @property
    def ok(self) -> bool:
        return bool(self.steps) and self.failures == 0

    def step(self, name: str) -> ProductionStep | None:
        for step in self.steps:
            if step.name == name:
                return step
        return None

    @property
    def first_failure(self) -> ProductionStep | None:
        for step in self.steps:
            if step.status == "fail":
                return step
        return None

    def failure_lines(self) -> list[str]:
        """One line per failing step - what the CLI prints to stderr."""

        return [
            f"{step.name}: {step.detail}"
            for step in self.steps
            if step.status == "fail"
        ]

    def summary(self) -> str:
        state = "READY" if self.ok else "ATTENTION"
        passed = sum(1 for step in self.steps if step.status == "pass")
        return (
            f"{state}; steps={len(self.steps)}; passed={passed}; "
            f"failures={self.failures}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "generated_at": self.generated_at,
            "ok": self.ok,
            "failures": self.failures,
            "environment": dict(self.environment),
            "steps": [step.to_dict() for step in self.steps],
            "workdir": self.workdir,
            "kept": self.kept,
            "duration_ms": self.duration_ms,
        }


def production_report_path(settings: Settings) -> Path:
    """Where ``release production-check`` stores its last report."""

    return Path(settings.data_dir) / PRODUCTION_REPORT_NAME


def stored_production_report(settings: Settings) -> dict[str, Any] | None:
    """Read the last stored report, or ``None`` when the check never ran.

    A malformed report is reported as malformed rather than silently ignored, so
    a corrupted file can never look like a passing check.
    """

    path = production_report_path(settings)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "failures": -1, "error": "report is unreadable"}
    return payload if isinstance(payload, dict) else {"ok": False, "failures": -1}


#: Windows keeps a handle on a file for a moment after another process wrote it
#: (indexer, antivirus). These are the errors that means, and they are transient.
_TRANSIENT_WINDOWS_ERRORS = {5, 32, 33}


def _retry_file_op(op: Any, *, attempts: int = 6, delay: float = 0.05) -> Any:
    """Run a file operation again if Windows says the handle is momentarily busy.

    Only ``PermissionError`` / ``OSError`` with a known sharing-violation code is
    retried - a genuine permission problem is re-raised after the last attempt
    instead of being hidden.
    """

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return op()
        except (PermissionError, OSError) as exc:
            winerror = getattr(exc, "winerror", None)
            if not isinstance(exc, PermissionError) and winerror not in _TRANSIENT_WINDOWS_ERRORS:
                raise
            last = exc
            if attempt + 1 < attempts:
                time.sleep(delay * (attempt + 1))
    assert last is not None
    raise last


def _tree_hash(root: Path, relatives: tuple[Path, ...]) -> str:
    """A content hash of a project tree limited to the release allow-list."""

    digest = hashlib.sha256()
    for relative in relatives:
        path = root / relative
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            digest.update(_retry_file_op(path.read_bytes))
        digest.update(b"\0\0")
    return digest.hexdigest()


def _copy_tree(source: Path, destination: Path, relatives: tuple[Path, ...]) -> None:
    for relative in relatives:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        source_file = source / relative
        _retry_file_op(
            lambda src=source_file, dst=target: shutil.copy2(src, dst)
        )


def _tampered_copy(bundle: Path, destination: Path) -> Path:
    """Copy a bundle and silently corrupt one entry's bytes."""

    with zipfile.ZipFile(bundle, "r") as source, zipfile.ZipFile(
        destination, "w", zipfile.ZIP_DEFLATED
    ) as target:
        corrupted = False
        for info in source.infolist():
            data = source.read(info.filename)
            if not corrupted and info.filename != RELEASE_MANIFEST:
                data = data + b"\n# tampered by the production check\n"
                corrupted = True
            target.writestr(info, data)
    if not corrupted:
        raise PcAgentError("The bundle had no payload entry to tamper with.")
    return destination


def _sign_bundle(bundle: Any, bundle_path: Path, workdir: Path) -> tuple[Path, str]:
    """Sign a bundle with a throw-away key; return the sidecar path and key id."""

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    private_key_path = workdir / "self-test-private-key.b64"
    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    private_key_path.write_text(
        base64.b64encode(seed).decode("ascii") + "\n", encoding="utf-8"
    )
    key_id = "production-self-test"
    payload = build_signature_payload(bundle, key_id=key_id)
    signature = private.sign(signature_payload_bytes(payload))
    from .updates import b64url_encode

    document = dict(payload)
    document["signature"] = b64url_encode(signature)
    sidecar = default_signature_path(bundle_path)
    sidecar.write_text(json.dumps(document, indent=1, sort_keys=True), encoding="utf-8")
    return sidecar, b64url_encode(public)


def run_production_check(
    settings: Settings,
    *,
    workdir: Path | None = None,
    keep: bool = False,
    write_report: bool = True,
) -> ProductionReport:
    """Verify the whole release chain and return a structured report."""

    steps: list[ProductionStep] = []
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    created_workdir = workdir is None
    base = Path(workdir) if workdir is not None else Path(
        tempfile.mkdtemp(prefix=_TEMP_PREFIX)
    )
    base.mkdir(parents=True, exist_ok=True)
    install_root = base / "install"
    work = base / "work"
    work.mkdir(parents=True, exist_ok=True)
    source_root = Path(settings.root_dir).expanduser().resolve()

    def record(name: str, status: str, detail: str, since: float) -> None:
        steps.append(
            ProductionStep(
                name=name,
                status=status,
                detail=detail,
                duration_ms=int((time.perf_counter() - since) * 1000),
            )
        )

    def fail(name: str, detail: str, since: float) -> None:
        record(name, "fail", detail, since)
        raise PcAgentError(detail)

    environment = {
        "platform": platform.platform(),
        "os_name": os.name,
        "python": platform.python_version(),
        "version": __version__,
        "schema": LATEST_SCHEMA_VERSION,
        "source_root": str(source_root),
        "signing_key": "ephemeral self-test key (no production key is used)",
    }

    try:
        # 1) the allow-list itself ------------------------------------------
        since = time.perf_counter()
        relatives = release_candidate_files(source_root)
        if not relatives:
            fail(
                "release_catalog",
                "No release files were found in the project root; run the check from the install.",
                since,
            )
        def _forbidden(relative: Path) -> bool:
            if relative.name == ".env":
                return True
            if not relative.parts:
                return False
            top = relative.parts[0]
            if top in {"data", ".git", ".venv"}:
                return True
            # ``workspace`` ships only its empty placeholder; no user content.
            return top == "workspace" and relative.as_posix() != "workspace/.gitkeep"

        forbidden = [
            relative.as_posix() for relative in relatives if _forbidden(relative)
        ]
        if forbidden:
            fail(
                "release_catalog",
                f"{len(forbidden)} forbidden path(s) reached the release allow-list: "
                + ", ".join(forbidden[:3]),
                since,
            )
        record(
            "release_catalog",
            "pass",
            f"{len(relatives)} allow-listed files; no .env, data, workspace, .git or "
            ".venv content can enter a bundle",
            since,
        )

        # 2) build a real bundle -------------------------------------------
        since = time.perf_counter()
        bundle_path = work / f"pc-agent-{__version__}.zip"
        try:
            bundle = build_release_bundle(source_root, bundle_path)
        except PcAgentError as exc:
            fail("bundle_build", f"Release bundle could not be built: {exc}", since)
        if bundle.file_count != len(relatives):
            fail(
                "bundle_build",
                f"Bundle holds {bundle.file_count} files but the allow-list has "
                f"{len(relatives)}.",
                since,
            )
        record("bundle_build", "pass", f"built {bundle.summary()}", since)

        # 3) verify it without extracting --------------------------------
        since = time.perf_counter()
        try:
            verified = verify_release_bundle(bundle_path)
        except PcAgentError as exc:
            fail("bundle_verify", f"Built bundle failed verification: {exc}", since)
        if verified.bundle_sha256 != bundle.bundle_sha256:
            fail(
                "bundle_verify",
                "Rebuilt bundle hash differs from the built bundle hash.",
                since,
            )
        record(
            "bundle_verify",
            "pass",
            f"verified {verified.file_count} files / {verified.total_bytes} bytes "
            f"without extracting; sha256={verified.bundle_sha256[:16]}...",
            since,
        )

        # 4) a tampered archive must be refused ---------------------------
        since = time.perf_counter()
        tampered = _tampered_copy(bundle_path, work / "tampered.zip")
        try:
            verify_release_bundle(tampered)
        except PcAgentError as exc:
            record("tamper_detection", "pass", f"tampered bundle refused: {exc}", since)
        else:
            fail(
                "tamper_detection",
                "A bundle with modified payload bytes passed verification.",
                since,
            )

        # 5) sign it and verify the signature -----------------------------
        since = time.perf_counter()
        sidecar, public_key = _sign_bundle(bundle, bundle_path, work)
        install_settings = Settings.from_env(
            install_root,
            environ={
                "PC_AGENT_RELEASE_PUBLIC_KEY": public_key,
                "PC_AGENT_RELEASE_KEY_ID": "production-self-test",
                "PC_AGENT_REQUIRE_SIGNED_UPDATES": "1",
            },
            load_dotenv=False,
        )
        install_settings.ensure_directories()
        try:
            from .updates import check_update

            check = check_update(bundle_path, install_settings, signature_path=sidecar)
        except PcAgentError as exc:
            fail("signature_verify", f"Signature verification failed: {exc}", since)
        if not check.signature.verified:
            fail("signature_verify", "Signature was inspected but not verified.", since)
        record(
            "signature_verify",
            "pass",
            f"signed with an ephemeral Ed25519 key and verified "
            f"(key_id={check.signature.key_id}); no production key was used",
            since,
        )

        # 6) an unsigned bundle must be refused when signing is required ---
        since = time.perf_counter()
        unsigned_dir = work / "unsigned"
        unsigned_dir.mkdir(parents=True, exist_ok=True)
        unsigned_bundle = unsigned_dir / bundle_path.name
        shutil.copy2(bundle_path, unsigned_bundle)
        try:
            stage_update(
                unsigned_bundle,
                install_settings,
                signature_path=None,
                allow_unsigned=False,
                # The self-test reinstalls the same version, so the only policy
                # that may refuse this call is the signature requirement.
                allow_downgrade=True,
            )
        except PcAgentError as exc:
            reason = str(exc)
            if "sign" not in reason.lower():
                record(
                    "unsigned_refused",
                    "fail",
                    f"unsigned update was refused, but not for the signature rule: {reason}",
                    since,
                )
            else:
                record(
                    "unsigned_refused",
                    "pass",
                    "unsigned update refused while PC_AGENT_REQUIRE_SIGNED_UPDATES=1: "
                    f"{reason}",
                    since,
                )
        else:
            fail(
                "unsigned_refused",
                "An unsigned bundle was staged even though signed updates are required.",
                since,
            )

        # 7) stage, apply and roll back against a copy of the tree --------
        original_hash = _tree_hash(source_root, relatives)
        _copy_tree(source_root, install_root, relatives)
        applied_hash_before = _tree_hash(install_root, relatives)

        since = time.perf_counter()
        try:
            staged = stage_update(
                bundle_path,
                install_settings,
                signature_path=sidecar,
                # Reinstalling the running version is the point of a self-test.
                allow_downgrade=True,
            )
        except PcAgentError as exc:
            fail("stage", f"Signed bundle could not be staged: {exc}", since)
        record(
            "stage",
            "pass",
            f"staged {len(staged.files)} files in {staged.update_id}",
            since,
        )

        since = time.perf_counter()
        try:
            applied = apply_staged_update(install_settings, staged.update_id)
        except PcAgentError as exc:
            fail("apply", f"Staged update could not be applied: {exc}", since)
        applied_hash = _tree_hash(install_root, release_candidate_files(install_root))
        if applied_hash != applied_hash_before:
            fail(
                "apply",
                "The applied tree does not match the built tree byte for byte.",
                since,
            )
        record(
            "apply",
            "pass",
            f"applied {len(applied.files)} files; the install tree matches the "
            f"release tree byte for byte (sha256={applied_hash[:16]}...)",
            since,
        )

        since = time.perf_counter()
        try:
            rolled_back = rollback_update(install_settings, applied.update_id)
        except PcAgentError as exc:
            fail("rollback", f"Rollback failed: {exc}", since)
        restored_hash = _tree_hash(install_root, relatives)
        if restored_hash != original_hash:
            fail(
                "rollback",
                "Rollback did not restore the previous tree byte for byte.",
                since,
            )
        record(
            "rollback",
            "pass",
            f"rolled back {len(rolled_back.files)} files to the pre-apply tree "
            f"(sha256={restored_hash[:16]}...)",
            since,
        )

        since = time.perf_counter()
        if created_workdir and not keep:
            shutil.rmtree(base, ignore_errors=True)
            record(
                "cleanup",
                "pass",
                "temporary work directory removed; no test keys or bundles remain",
                since,
            )
        else:
            record(
                "cleanup",
                "skip",
                f"work directory kept at {base} (requested or caller-provided)",
                since,
            )
    except PcAgentError as exc:
        # Every deliberate failure records its own step first; anything else that
        # raises here is still reported instead of ending the chain silently.
        if not steps or steps[-1].status != "fail":
            steps.append(
                ProductionStep(
                    name="aborted",
                    status="fail",
                    detail=str(exc),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            )
    except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
        steps.append(
            ProductionStep(
                name="unexpected_error",
                status="fail",
                detail=f"{exc.__class__.__name__}: {exc}",
                duration_ms=0,
            )
        )

    ordered = sorted(
        steps,
        key=lambda item: (
            _STEP_ORDER.index(item.name) if item.name in _STEP_ORDER else len(_STEP_ORDER),
            item.name,
        ),
    )
    total_ms = int((time.perf_counter() - started) * 1000)
    report = ProductionReport(
        version=__version__,
        generated_at=started_at.replace(microsecond=0).isoformat(),
        environment=environment,
        steps=tuple(ordered),
        workdir=str(base),
        kept=bool(keep or not created_workdir),
        duration_ms=total_ms,
    )
    if write_report:
        try:
            target = production_report_path(settings)
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = dict(report.to_dict())
            target.write_text(
                json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8"
            )
        except OSError:
            pass
    return report
