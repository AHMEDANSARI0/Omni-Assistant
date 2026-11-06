"""Authenticode signing and verification for Windows artefacts (0.81.0).

The update *bundle* has been signed with an Ed25519 key since Phase 21: that is
what the product verifies before it applies an update. What was still open was
the other half of "signed installation tooling" - the **Authenticode** signature
on the Windows installer, which is what SmartScreen and Windows itself look at.

This module does that honestly:

* **Verification is local and needs no external tool.** On Windows the real
  ``WinVerifyTrust`` API is called through :mod:`ctypes`, so "is this EXE
  trusted?" is answered by the operating system, not by a string match on some
  log output. On a non-Windows host the answer is ``unverifiable`` - never a
  guess.
* **Signing uses the user's own certificate.** Nothing is generated and no key
  is stored by this product. The preferred mode is ``/sha1 <thumbprint>`` of a
  certificate already in the current user's store, so **no password is ever on a
  command line**. A PFX file is supported as a fallback, but its password can
  only come from the environment and only when the caller explicitly accepts
  the exposure, because ``signtool`` requires ``/p <password>`` in ``argv`` and
  ``argv`` is visible to other processes on the same machine.
* **Nothing secret is kept.** Passwords are never logged, never written to a
  report, and are scrubbed out of captured tool output before it is stored.

Everything returns data (:class:`SigningReport` / dicts); nothing raises for a
missing tool, so an audit can report "unsigned, no certificate configured" as a
fact instead of crashing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_TIMESTAMP_URL",
    "SigningReport",
    "authenticode_status",
    "build_sign_argv",
    "find_signtool",
    "sanitize_tool_output",
    "sign_installer",
    "signtool_status",
    "verify_authenticode",
    "winverifytrust_check",
]

_IS_WINDOWS = sys.platform.startswith("win")
#: RFC 3161 timestamp authority used by default. A timestamp is what keeps a
#: signature valid after the signing certificate itself expires.
DEFAULT_TIMESTAMP_URL = "http://timestamp.digicert.com"
_MAX_OUTPUT = 20_000

# WinVerifyTrust results we care about (winerror.h).
TRUST_E_NOSIGNATURE = 0x800B0100
TRUST_E_SUBJECT_FORM_UNKNOWN = 0x800B0003
TRUST_E_PROVIDER_UNKNOWN = 0x800B0001
CERT_E_UNTRUSTEDROOT = 0x800B0109
CERT_E_EXPIRED = 0x800B0101
CERT_E_CHAINING = 0x800B010A
TRUST_E_EXPLICIT_DISTRUST = 0x800B0111

_WTD_UI_NONE = 2
_WTD_REVOKE_NONE = 0
_WTD_CHOICE_FILE = 1
_WTD_STATEACTION_VERIFY = 1
_WTD_STATEACTION_CLOSE = 2
_WTD_SAFER_FLAG = 0x100


@dataclass(frozen=True, slots=True)
class SigningReport:
    """Outcome of one signing or verification attempt."""

    ok: bool
    status: str
    detail: str
    path: str = ""
    signer: str = ""
    timestamped: bool = False
    duration_ms: int = 0
    command: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
            "path": self.path,
            "signer": self.signer,
            "timestamped": self.timestamped,
            "duration_ms": self.duration_ms,
            "command": list(self.command),
        }


# ------------------------------------------------------------------ tools ---


def find_signtool(explicit: Path | str | None = None) -> Path | None:
    """Locate ``signtool.exe``: explicit path, ``PATH``, then the Windows SDK.

    The SDK layout is version-sorted so the newest installed kit wins, and the
    host architecture is preferred without excluding the others.
    """

    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None
    from_path = shutil.which("signtool") or shutil.which("signtool.exe")
    if from_path:
        return Path(from_path)
    if not _IS_WINDOWS:
        return None
    programs = Path(os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)")
    kits = programs / "Windows Kits" / "10" / "bin"
    if not kits.is_dir():
        return None
    architectures = ["x64", "x86", "arm64"]
    versions = sorted((p for p in kits.iterdir() if p.is_dir()), reverse=True)
    for version in versions:
        for architecture in architectures:
            candidate = version / architecture / "signtool.exe"
            if candidate.is_file():
                return candidate
    return None


def signtool_status(explicit: Path | str | None = None) -> dict[str, Any]:
    """Where the tool is, and which signing sources this host actually has."""

    tool = find_signtool(explicit)
    thumbprint = (os.environ.get("PC_AGENT_SIGN_THUMBPRINT") or "").strip()
    pfx = (os.environ.get("PC_AGENT_SIGN_PFX") or "").strip()
    pfx_exists = bool(pfx) and Path(pfx).expanduser().is_file()
    return {
        "platform": "windows" if _IS_WINDOWS else sys.platform,
        "signtool": str(tool) if tool else "",
        "signtool_available": tool is not None,
        "thumbprint_configured": bool(thumbprint),
        "pfx_configured": pfx_exists,
        "timestamp_url": os.environ.get("PC_AGENT_SIGN_TIMESTAMP_URL")
        or DEFAULT_TIMESTAMP_URL,
        "certificate_configured": bool(thumbprint or pfx_exists),
        "verification": (
            "WinVerifyTrust (operating system API)"
            if _IS_WINDOWS
            else "unavailable on this host (Windows only)"
        ),
    }


# ----------------------------------------------------------- verification ---


def winverifytrust_check(path: Path | str) -> dict[str, Any]:
    """Ask Windows whether a file carries a trusted Authenticode signature.

    Returns ``{"status": ..., "code": int, "detail": str}`` where status is one of
    ``trusted-signed``, ``unsigned``, ``untrusted``, ``expired`` or
    ``unverifiable`` (non-Windows host, missing file, or API failure).
    """

    target = Path(path).expanduser()
    if not _IS_WINDOWS:
        return {
            "status": "unverifiable",
            "code": 0,
            "detail": "WinVerifyTrust is a Windows API; this host cannot answer",
        }
    if not target.is_file():
        return {"status": "unverifiable", "code": 0, "detail": f"file not found: {target}"}
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        class WINTRUST_FILE_INFO(ctypes.Structure):
            _fields_ = [
                ("cbStruct", wintypes.DWORD),
                ("pcwszFilePath", wintypes.LPCWSTR),
                ("hFile", wintypes.HANDLE),
                ("pgKnownSubject", ctypes.POINTER(GUID)),
            ]

        class WINTRUST_DATA(ctypes.Structure):
            _fields_ = [
                ("cbStruct", wintypes.DWORD),
                ("pPolicyCallbackData", wintypes.LPVOID),
                ("pSIPClientData", wintypes.LPVOID),
                ("dwUIChoice", wintypes.DWORD),
                ("fdwRevocationChecks", wintypes.DWORD),
                ("dwUnionChoice", wintypes.DWORD),
                ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)),
                ("dwStateAction", wintypes.DWORD),
                ("hWVTStateData", wintypes.HANDLE),
                ("pwszURLReference", wintypes.LPWSTR),
                ("dwProvFlags", wintypes.DWORD),
                ("dwUIContext", wintypes.DWORD),
                ("pSignatureSettings", wintypes.LPVOID),
            ]

        action = GUID(
            0x00AAC56B,
            0xCD44,
            0x11D0,
            (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE),
        )
        file_info = WINTRUST_FILE_INFO()
        file_info.cbStruct = ctypes.sizeof(WINTRUST_FILE_INFO)
        file_info.pcwszFilePath = str(target.resolve())
        data = WINTRUST_DATA()
        data.cbStruct = ctypes.sizeof(WINTRUST_DATA)
        data.dwUIChoice = _WTD_UI_NONE
        data.fdwRevocationChecks = _WTD_REVOKE_NONE
        data.dwUnionChoice = _WTD_CHOICE_FILE
        data.pFile = ctypes.pointer(file_info)
        data.dwStateAction = _WTD_STATEACTION_VERIFY
        data.dwProvFlags = _WTD_SAFER_FLAG
        wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
        code = int(
            wintrust.WinVerifyTrust(
                wintypes.HWND(0),
                ctypes.byref(action),
                ctypes.byref(data),
            )
        )
        # Close the state handle on every path; leaking it leaks a file handle.
        data.dwStateAction = _WTD_STATEACTION_CLOSE
        wintrust.WinVerifyTrust(wintypes.HWND(0), ctypes.byref(action), ctypes.byref(data))
    except Exception as exc:  # noqa: BLE001 - a missing API must not crash the app
        return {
            "status": "unverifiable",
            "code": 0,
            "detail": f"WinVerifyTrust could not be called: {exc.__class__.__name__}: {exc}",
        }
    unsigned_codes = {TRUST_E_NOSIGNATURE, TRUST_E_SUBJECT_FORM_UNKNOWN, TRUST_E_PROVIDER_UNKNOWN}
    if code == 0:
        return {"status": "trusted-signed", "code": code, "detail": "WinVerifyTrust: trusted"}
    if code in unsigned_codes:
        return {"status": "unsigned", "code": code, "detail": "the file carries no signature"}
    if code == CERT_E_EXPIRED:
        return {"status": "expired", "code": code, "detail": "the signing certificate expired"}
    if code in {CERT_E_UNTRUSTEDROOT, CERT_E_CHAINING, TRUST_E_EXPLICIT_DISTRUST}:
        return {
            "status": "untrusted",
            "code": code,
            "detail": "the signature chain is not trusted on this machine",
        }
    return {
        "status": "untrusted",
        "code": code,
        "detail": f"WinVerifyTrust returned 0x{code & 0xFFFFFFFF:08X}",
    }


def verify_authenticode(
    path: Path | str,
    *,
    signtool_path: Path | str | None = None,
    timeout: float = 120.0,
) -> SigningReport:
    """Verify a signed artefact with the OS, then add tool detail if available."""

    target = Path(path).expanduser()
    started = time.perf_counter()
    if not target.is_file():
        return SigningReport(
            ok=False, status="missing", detail=f"file not found: {target}", path=str(target)
        )
    trust = winverifytrust_check(target)
    detail = str(trust["detail"])
    tool = find_signtool(signtool_path)
    if tool is not None and trust["status"] != "unverifiable":
        completed = _run(
            [str(tool), "verify", "/pa", "/all", str(target.resolve())], timeout=timeout
        )
        tail = sanitize_tool_output(
            (completed.stdout or "") + (completed.stderr or "")
        ).strip().splitlines()
        if tail:
            detail = f"{detail}; signtool: {tail[-1][:200]}"
    return SigningReport(
        ok=trust["status"] == "trusted-signed",
        status=str(trust["status"]),
        detail=detail,
        path=str(target),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


# --------------------------------------------------------------- signing ---


def build_sign_argv(
    signtool: Path | str,
    target: Path | str,
    *,
    thumbprint: str = "",
    pfx: Path | str | None = None,
    password: str | None = None,
    timestamp_url: str = DEFAULT_TIMESTAMP_URL,
    allow_argv_password: bool = False,
) -> list[str]:
    """Build a ``signtool sign`` command line without leaking a secret.

    ``/p <password>`` is the only way ``signtool`` accepts a PFX password, and
    ``argv`` is readable by other processes, so that path is opt-in and refused
    otherwise. The recommended path - a certificate thumbprint from the user's
    own store - needs no password at all.
    """

    argv = [str(signtool), "sign", "/fd", "sha256"]
    if timestamp_url:
        argv += ["/td", "sha256", "/tr", timestamp_url]
    if thumbprint:
        clean = re.sub(r"[^0-9A-Fa-f]", "", thumbprint)
        if len(clean) < 20:
            raise ValueError("A certificate thumbprint must be at least 20 hex characters.")
        argv += ["/sha1", clean.upper()]
    elif pfx:
        argv += ["/f", str(Path(pfx).expanduser())]
        if password:
            if not allow_argv_password:
                raise ValueError(
                    "Refusing to put a PFX password on a command line: argv is visible "
                    "to other processes on this machine. Use a certificate thumbprint "
                    "(/sha1) instead, or pass allow_argv_password=True explicitly."
                )
            argv += ["/p", password]
    else:
        raise ValueError(
            "Signing needs either a certificate thumbprint or a PFX file; "
            "this product never generates a code-signing key."
        )
    argv.append(str(Path(target).expanduser()))
    return argv


def sanitize_tool_output(text: str, secrets: Any = ()) -> str:
    """Remove any secret from captured tool output before it is stored."""

    cleaned = text
    for secret in secrets:
        if secret and len(str(secret)) >= 4:
            cleaned = cleaned.replace(str(secret), "[redacted]")
    return cleaned[:_MAX_OUTPUT]


def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def sign_installer(
    path: Path | str,
    *,
    thumbprint: str = "",
    pfx: Path | str | None = None,
    password: str | None = None,
    timestamp_url: str = DEFAULT_TIMESTAMP_URL,
    allow_argv_password: bool = False,
    signtool_path: Path | str | None = None,
    timeout: float = 600.0,
    runner: Any = None,
) -> SigningReport:
    """Sign an installer EXE with the user's certificate, then verify it.

    ``runner`` exists so the command construction can be tested without a
    Windows SDK; the default is a real ``subprocess.run``.
    """

    target = Path(path).expanduser()
    started = time.perf_counter()
    if not target.is_file():
        return SigningReport(
            ok=False, status="missing", detail=f"file not found: {target}", path=str(target)
        )
    tool = find_signtool(signtool_path)
    if tool is None:
        return SigningReport(
            ok=False,
            status="tool-missing",
            detail=(
                "signtool.exe was not found (install the Windows SDK or add it to PATH); "
                "no signature was produced"
            ),
            path=str(target),
        )
    try:
        argv = build_sign_argv(
            tool,
            target,
            thumbprint=thumbprint,
            pfx=pfx,
            password=password,
            timestamp_url=timestamp_url,
            allow_argv_password=allow_argv_password,
        )
    except ValueError as exc:
        return SigningReport(ok=False, status="refused", detail=str(exc), path=str(target))
    redacted = ["[redacted]" if password and item == password else item for item in argv]
    try:
        completed = (runner or _run)(argv, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return SigningReport(
            ok=False,
            status="error",
            detail=f"{exc.__class__.__name__}: {exc}",
            path=str(target),
            command=tuple(redacted),
        )
    output = sanitize_tool_output(
        (completed.stdout or "") + (completed.stderr or ""), secrets=[password or ""]
    )
    if completed.returncode != 0:
        tail = output.strip().splitlines()[-1][:300] if output.strip() else "no output"
        return SigningReport(
            ok=False,
            status="failed",
            detail=f"signtool exited {completed.returncode}: {tail}",
            path=str(target),
            duration_ms=int((time.perf_counter() - started) * 1000),
            command=tuple(redacted),
        )
    verified = verify_authenticode(target, signtool_path=signtool_path, timeout=timeout)
    return SigningReport(
        ok=verified.ok,
        status="signed-verified" if verified.ok else f"signed-{verified.status}",
        detail=(
            f"signtool signed the file; verification: {verified.detail}"
            if verified.ok
            else f"signtool reported success but verification says {verified.status}: "
            f"{verified.detail}"
        ),
        path=str(target),
        timestamped=bool(timestamp_url),
        duration_ms=int((time.perf_counter() - started) * 1000),
        command=tuple(redacted),
    )


def authenticode_status(path: Path | str | None) -> dict[str, Any]:
    """One-call summary used by the release audit and diagnostics."""

    tool = signtool_status()
    if path is None or not Path(path).expanduser().is_file():
        return {
            **tool,
            "status": "no-artifact",
            "detail": "no built installer was found to inspect",
        }
    report = verify_authenticode(path)
    return {**tool, "status": report.status, "detail": report.detail, "path": report.path}
