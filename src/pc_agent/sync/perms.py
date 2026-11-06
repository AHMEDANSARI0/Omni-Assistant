"""Owner-only file hardening that is honest about what each OS can actually do.

Sync artefacts (the device identity and outgoing bundles) contain key material,
so they must not be readable by other accounts. The mechanisms differ by
platform and, crucially, so do the guarantees:

* **POSIX** - ``chmod 0o600`` is authoritative: the kernel enforces it and the
  mode can be read back.
* **Windows** - ``os.chmod`` only toggles the read-only flag, so the mode always
  reads ``0o666`` no matter what was asked for. A real owner-only file therefore
  needs a discretionary ACL: inheritance is removed and the current user gets
  full control, applied through ``icacls``. The result is verified by reading the
  ACL back. When ``icacls`` is missing (or the volume has no ACL support, e.g.
  FAT32/exFAT), the report says ``applied: False`` instead of pretending.

Nothing here is silent: every call returns a report that is stored next to the
artefact and surfaced in docs and in the phase report.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

__all__ = ["harden_private_file", "private_file_report"]

_IS_WINDOWS = sys.platform.startswith("win")
_ICACLS_TIMEOUT_SECONDS = 20


def _posix_report(path: Path, *, applied: bool, detail: str) -> dict[str, Any]:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:  # pragma: no cover - the caller just created the file
        mode = -1
    return {
        "mechanism": "posix-mode",
        "applied": applied,
        "mode": mode,
        "owner_only": mode == 0o600,
        "detail": detail,
    }


def _windows_report(path: Path, *, applied: bool, detail: str) -> dict[str, Any]:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:  # pragma: no cover - the caller just created the file
        mode = -1
    return {
        "mechanism": "windows-acl",
        "applied": applied,
        # Reported for transparency only: NTFS has no POSIX mode, so this value
        # never proves anything on Windows and must not be asserted on.
        "mode": mode,
        "owner_only": applied,
        "detail": detail,
    }


def _current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "").strip()
    name = os.environ.get("USERNAME", "").strip()
    if not name:
        return ""
    if domain and name and "\\" not in name:
        return f"{domain}\\{name}"
    return name


def _windows_acl_entries(path: Path) -> list[str] | None:
    """Return the ACL lines ``icacls`` prints, or ``None`` when unavailable."""

    try:
        completed = subprocess.run(
            ["icacls", str(path)],
            capture_output=True,
            text=True,
            timeout=_ICACLS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - Windows only
        return None
    if completed.returncode != 0:
        return None
    lines = [line.strip() for line in (completed.stdout or "").splitlines()]
    return [line for line in lines if line]


def harden_private_file(path: Path) -> dict[str, Any]:
    """Make ``path`` owner-only where the platform allows it; report the truth."""

    path = Path(path)
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError as exc:  # pragma: no cover - unusual filesystem
            return _posix_report(path, applied=False, detail=f"chmod failed: {exc.strerror or exc}")
        report = _posix_report(path, applied=True, detail="chmod 0600 applied")
        return report

    if not _IS_WINDOWS:
        return {
            "mechanism": "none",
            "applied": False,
            "mode": None,
            "owner_only": False,
            "detail": f"no owner-only hardening implemented for {os.name}",
        }

    user = _current_user()  # pragma: no cover - Windows only
    if not user:  # pragma: no cover - Windows only
        return _windows_report(path, applied=False, detail="current user name is unavailable")
    try:  # pragma: no cover - Windows only
        completed = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{user}:(F)",
            ],
            capture_output=True,
            text=True,
            timeout=_ICACLS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - Windows only
        return _windows_report(path, applied=False, detail=f"icacls unavailable: {exc}")
    if completed.returncode != 0:  # pragma: no cover - Windows only
        tail = (completed.stderr or completed.stdout or "").strip()[-200:]
        return _windows_report(
            path,
            applied=False,
            detail=f"icacls failed ({completed.returncode}): {tail}",
        )
    entries = _windows_acl_entries(path)  # pragma: no cover - Windows only
    inherited = [line for line in (entries or []) if "(I)" in line]
    if entries is None:  # pragma: no cover - Windows only
        return _windows_report(
            path,
            applied=False,
            detail="ACL written but could not be read back for verification",
        )
    if inherited:  # pragma: no cover - Windows only
        return _windows_report(
            path,
            applied=False,
            detail=f"{len(inherited)} inherited ACE(s) remain after icacls",
        )
    return _windows_report(path, applied=True, detail="ACL inheritance removed; owner has full control")


def private_file_report(path: Path) -> dict[str, Any]:
    """Describe the current owner-only state of ``path`` without changing it."""

    path = Path(path)
    if os.name == "posix":
        return _posix_report(path, applied=True, detail="posix mode read back")
    if not _IS_WINDOWS:  # pragma: no cover - exotic platforms
        return {
            "mechanism": "none",
            "applied": False,
            "mode": None,
            "owner_only": False,
            "detail": f"not inspected on {os.name}",
        }
    entries = _windows_acl_entries(path)  # pragma: no cover - Windows only
    if entries is None:  # pragma: no cover - Windows only
        return _windows_report(path, applied=False, detail="icacls unavailable")
    inherited = [line for line in entries if "(I)" in line]
    return _windows_report(
        path,  # pragma: no cover - Windows only
        applied=not inherited,
        detail=(
            "no inherited ACEs"
            if not inherited
            else f"{len(inherited)} inherited ACE(s) present"
        ),
    )
