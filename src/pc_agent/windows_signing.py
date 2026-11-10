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
    "BUNDLE_GLOBS",
    "DEFAULT_TIMESTAMP_URL",
    "INSTALLER_SIGNING_REPORT",
    "TIMESTAMP_FALLBACK_URLS",
    "SigningPlan",
    "SigningReport",
    "authenticode_status",
    "build_sign_argv",
    "discover_installer_artifacts",
    "find_signtool",
    "list_signing_certificates",
    "plan_signing",
    "reputation_status",
    "sanitize_tool_output",
    "sign_bundle",
    "sign_installer",
    "signtool_status",
    "verify_authenticode",
    "winverifytrust_check",
]

_IS_WINDOWS = sys.platform.startswith("win")
#: RFC 3161 timestamp authority used by default. A timestamp is what keeps a
#: signature valid after the signing certificate itself expires.
DEFAULT_TIMESTAMP_URL = "http://timestamp.digicert.com"

#: Where a bundle signing run records its outcome (inside the data directory).
INSTALLER_SIGNING_REPORT = "installer-signing.json"

#: RFC 3161 authorities tried in order when a timestamp request fails. A timestamp
#: failure is the most common way a *successful* signature still ends up invalid
#: later: without one, the signature dies the moment the certificate expires. So a
#: failed ``/tr`` is retried against the next authority instead of being reported as
#: a signed installer.
TIMESTAMP_FALLBACK_URLS: tuple[str, ...] = (
    "http://timestamp.digicert.com",
    "http://timestamp.sectigo.com",
    "http://timestamp.globalsign.com/tsa/r6advanced1",
)
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
    timestamp_url: str = ""
    timestamp_attempts: tuple[dict[str, Any], ...] = ()
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
            "timestamp_url": self.timestamp_url,
            "timestamp_attempts": [dict(item) for item in self.timestamp_attempts],
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
    # A timestamp authority can be down, rate-limited or unreachable. Retrying the
    # next one is what turns "signed but expires with the certificate" into a
    # properly timestamped signature; the URL that worked is reported.
    attempts: list[dict[str, Any]] = []
    urls = [timestamp_url] if timestamp_url else []
    if timestamp_url:
        urls += [url for url in TIMESTAMP_FALLBACK_URLS if url != timestamp_url]
    completed = None
    used_url = timestamp_url
    for candidate_url in urls:
        attempt_argv = [candidate_url if item == timestamp_url else item for item in argv]
        redacted_attempt = [
            "[redacted]" if password and item == password else item for item in attempt_argv
        ]
        try:
            completed = (runner or _run)(attempt_argv, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return SigningReport(
                ok=False,
                status="error",
                detail=f"{exc.__class__.__name__}: {exc}",
                path=str(target),
                command=tuple(redacted_attempt),
            )
        attempt_output = sanitize_tool_output(
            (completed.stdout or "") + (completed.stderr or ""), secrets=[password or ""]
        )
        attempts.append(
            {
                "timestamp_url": candidate_url,
                "exit_code": completed.returncode,
                "tail": (attempt_output.strip().splitlines() or [""])[-1][:200],
            }
        )
        redacted = redacted_attempt
        if completed.returncode == 0:
            used_url = candidate_url
            break
    if completed is None:  # pragma: no cover - only if no URL was given
        return SigningReport(
            ok=False,
            status="refused",
            detail="no timestamp authority was supplied",
            path=str(target),
        )
    output = sanitize_tool_output(
        (completed.stdout or "") + (completed.stderr or ""), secrets=[password or ""]
    )
    if completed.returncode != 0:
        tail = output.strip().splitlines()[-1][:300] if output.strip() else "no output"
        # Keep every timestamp attempt: "the authority was down" and "the
        # certificate was rejected" are different problems, and the person
        # reading this needs to tell them apart.
        tried = [item["timestamp_url"] for item in attempts]
        return SigningReport(
            ok=False,
            status="failed",
            detail=f"signtool exited {completed.returncode}: {tail}"
            + (f" (timestamp authorities tried: {', '.join(tried)})" if len(tried) > 1 else ""),
            path=str(target),
            timestamp_url="",
            timestamp_attempts=tuple(attempts),
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
        timestamped=bool(used_url),
        timestamp_url=used_url,
        timestamp_attempts=tuple(attempts),
        duration_ms=int((time.perf_counter() - started) * 1000),
        command=tuple(redacted),
    )


def reputation_status(
    *,
    report_path: Path | str | None = None,
    shell_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Where this machine actually stands on signing - facts, no promises.

    Combines three separate readings, each of which can independently be
    ``not-run``:

    * the certificate store (which certificate, is its key on hardware);
    * the last recorded signing run (``data/installer-signing.json``);
    * the installers currently on disk, verified if a Windows tool is available.

    SmartScreen reputation itself is Microsoft's and is not measurable from here,
    so nothing in the result pretends otherwise.
    """

    import json

    listing = list_signing_certificates()
    certificates = listing.get("certificates", [])
    hardware = [cert for cert in certificates if cert.get("hardware")]
    recorded: dict[str, Any] = {}
    if report_path is not None:
        path = Path(report_path).expanduser()
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                recorded = {"status": "unreadable", "detail": str(path)}
            else:
                recorded = loaded if isinstance(loaded, dict) else {"status": "invalid"}
        else:
            recorded = {"status": "not-recorded", "detail": f"no report at {path}"}
    artifacts = discover_installer_artifacts(shell_dir)
    verified: list[dict[str, Any]] = []
    for artifact in artifacts:
        report = verify_authenticode(artifact)
        verified.append(
            {
                "path": str(artifact),
                "status": report.status,
                "ok": report.ok,
                "detail": report.detail,
            }
        )
    blockers: list[str] = []
    if listing.get("status") == "not-run":
        blockers.append(
            "no certificate store on this host: signing must be done on Windows "
            "(run `pc-agent release signing --certificates` there)"
        )
    elif listing.get("status") == "empty":
        blockers.append(
            "no code-signing certificate installed: an OV/EV certificate is bought "
            "from a certificate authority, never generated by this product"
        )
    if not any(item.get("ok") for item in verified):
        blockers.append(
            "no verified installer found: run `pc-agent release sign-bundle` after a build"
        )
    return {
        "smart_screen_reputation": "not-measurable-here",
        "smart_screen_note": (
            "Microsoft decides SmartScreen reputation from downloads, publisher "
            "identity and malware reports; this product cannot measure, submit or "
            "purchase it. See docs/smartscreen_reputation.md"
        ),
        "certificate_store": listing,
        "certificate_count": len(certificates),
        "hardware_backed": len(hardware),
        # Corrected in 0.82.12: an EV certificate no longer bypasses SmartScreen.
        # Microsoft removed that behaviour in 2024 - EV- and OV-signed files now
        # build reputation the same way (see docs/smartscreen_reputation.md).
        # The honest expectation therefore depends on *whether* there is a
        # certificate, not on how expensive it was.
        "expected_first_run": (
            "signed: your publisher name is shown; SmartScreen warnings fade as "
            "reputation accrues (EV no longer bypasses this since 2024)"
            if certificates
            else "unsigned: 'Unknown publisher' warning"
        ),
        "last_signing_run": recorded,
        "installers": verified,
        "blockers": blockers,
        "timestamp_authorities": list(TIMESTAMP_FALLBACK_URLS),
        "next_command": "pc-agent release sign-bundle --check",
    }


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


# ------------------------------------------------- installer bundle signing ---

#: Where a Tauri build writes its Windows installers, relative to the shell dir.
BUNDLE_GLOBS = (
    "target/release/bundle/nsis/*.exe",
    "target/release/bundle/msi/*.msi",
)

#: Certificate EKUs that are allowed to sign code. Subject strings are not enough:
#: an email certificate with the same subject signs nothing Windows trusts.
_CODE_SIGNING_EKU = "1.3.6.1.5.5.7.3.3"


def discover_installer_artifacts(shell_dir: Path | str | None = None) -> list[Path]:
    """Return the installer files a Tauri build produced, newest first.

    Only ``.exe``/``.msi`` files inside the known bundle folders are considered -
    never an arbitrary file from the user's disk.
    """

    if shell_dir is None:
        return []
    base = Path(shell_dir).expanduser()
    found: list[Path] = []
    for pattern in BUNDLE_GLOBS:
        found.extend(path for path in base.glob(pattern) if path.is_file())
    unique = {path.resolve(): path for path in found}
    return sorted(unique.values(), key=lambda path: path.stat().st_mtime, reverse=True)


def list_signing_certificates(*, timeout: float = 60.0, runner: Any = None) -> dict[str, Any]:
    """List code-signing certificates in the current user's store.

    This answers the one question that blocks signing for most people: *which
    thumbprint do I pass?* Only non-secret fields are read (subject, thumbprint,
    validity, whether the certificate is valid for code signing at all). Private
    keys are never exported and never touched - signing happens inside
    ``signtool``, which talks to the certificate store itself.

    Off Windows the answer is ``not-run``, not an empty list pretending there are
    no certificates.
    """

    if not _IS_WINDOWS:
        return {
            "supported": False,
            "status": "not-run",
            "detail": "the certificate store only exists on Windows",
            "certificates": [],
        }
    # The extra fields matter for EV certificates: a publicly-trusted code-signing
    # key must live in hardware since June 2023, so "where is the key" decides
    # whether a certificate can be used at all on this machine. These are all
    # public, non-secret properties; the key itself is never exported or read.
    script = (
        "$ErrorObject = $null;"
        "$certs = Get-ChildItem Cert:\\CurrentUser\\My -CodeSigningCert -ErrorAction SilentlyContinue |"
        " ForEach-Object {"
        " $provider = ''; $hardware = $false; $keyType = '';"
        " try {"
        "  $rsa = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPrivateKey($_);"
        "  if ($rsa) { $keyType = 'RSA'; try { $hardware = [bool]$rsa.Key.CspKeyContainerInfo.HardwareDevice } catch {}"
        "   try { $provider = [string]$rsa.Key.CspKeyContainerInfo.ProviderName } catch {} }"
        " } catch {}"
        " if (-not $keyType) { try {"
        "  $ecdsa = [System.Security.Cryptography.X509Certificates.ECDsaCertificateExtensions]::GetECDsaPrivateKey($_);"
        "  if ($ecdsa) { $keyType = 'ECDSA' } } catch {} }"
        " [pscustomobject]@{"
        " Subject=$_.Subject; Thumbprint=$_.Thumbprint; NotBefore=$_.NotBefore.ToString('o');"
        " NotAfter=$_.NotAfter.ToString('o'); HasPrivateKey=$_.HasPrivateKey;"
        " Issuer=$_.Issuer; HasEkuCodeSigning=$true; KeyType=$keyType;"
        " ProviderName=$provider; HardwareDevice=$hardware;"
        " PublicKeyLength=$(try { $_.PublicKey.Key.KeySize } catch { 0 }) } };"
        "$certs | ConvertTo-Json -Compress -Depth 3"
    )
    argv = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]
    try:
        completed = (runner or _run)(argv, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "supported": True,
            "status": "error",
            "detail": f"{exc.__class__.__name__}: {exc}",
            "certificates": [],
        }
    if completed.returncode != 0:
        tail = ((completed.stdout or "") + (completed.stderr or "")).strip()[-300:]
        return {
            "supported": True,
            "status": "error",
            "detail": f"powershell exited {completed.returncode}: {tail or 'no output'}",
            "certificates": [],
        }
    raw = (completed.stdout or "").strip()
    if not raw:
        return {
            "supported": True,
            "status": "empty",
            "detail": "no code-signing certificate is installed for this user",
            "certificates": [],
        }
    import json

    try:
        loaded = json.loads(raw)
    except ValueError:
        return {
            "supported": True,
            "status": "error",
            "detail": "could not parse the certificate list",
            "certificates": [],
        }
    entries = loaded if isinstance(loaded, list) else [loaded]
    certificates = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("ProviderName", "") or "")[:120]
        hardware = bool(entry.get("HardwareDevice"))
        certificates.append(
            {
                "subject": str(entry.get("Subject", ""))[:200],
                "issuer": str(entry.get("Issuer", ""))[:200],
                "thumbprint": str(entry.get("Thumbprint", "")).upper(),
                "not_before": str(entry.get("NotBefore", "")),
                "not_after": str(entry.get("NotAfter", "")),
                "has_private_key": bool(entry.get("HasPrivateKey")),
                "key_type": str(entry.get("KeyType", "") or ""),
                "provider": provider,
                "hardware": hardware,
                # A hardware-backed key is what an EV / 2023-onward OV certificate
                # looks like. It is reported as a fact, not a promise: this says
                # "the key is not in a software container", nothing more.
                "storage": "hardware-token" if hardware else ("software-store" if provider else "unknown"),
            }
        )
    usable = [item for item in certificates if item["has_private_key"]]
    hardware = [item for item in usable if item["hardware"]]
    return {
        "supported": True,
        "status": "ok" if certificates else "empty",
        "detail": (
            f"{len(certificates)} code-signing certificate(s), {len(usable)} with a private key, "
            f"{len(hardware)} hardware-backed"
            if certificates
            else "no code-signing certificate is installed for this user"
        ),
        "certificates": certificates,
        "usable": len(usable),
        "hardware_backed": len(hardware),
    }


@dataclass(frozen=True, slots=True)
class SigningPlan:
    """What *would* happen, and what is missing - without touching a file."""

    ready: bool
    artifacts: tuple[str, ...]
    blockers: tuple[str, ...]
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "artifacts": list(self.artifacts),
            "blockers": list(self.blockers),
            "notes": list(self.notes),
        }


def certificate_storage(thumbprint: str, *, runner: Any = None, timeout: float = 60.0) -> dict[str, Any]:
    """Where does this certificate's private key live?

    Answers ``hardware-token`` / ``software-store`` / ``unknown`` plus a short
    human detail. The private key is never exported, read or unlocked: this reads
    the *provider name* and the ``HardwareDevice`` flag of the key container,
    which are public metadata.

    ``unknown`` is an honest answer: a custom CNG/KSP provider may not expose
    these fields (HSM-backed certificates often do not), and inventing
    "hardware" from a provider name would be a guess.
    """

    clean = re.sub(r"[^0-9A-Fa-f]", "", thumbprint or "")
    if not clean:
        return {"storage": "unknown", "detail": "no thumbprint given"}
    if not _IS_WINDOWS:
        return {"storage": "unknown", "detail": "the certificate store only exists on Windows"}
    script = (
        "$cert = Get-ChildItem Cert:\\CurrentUser\\My -CodeSigningCert |"
        f" Where-Object {{ $_.Thumbprint -eq '{clean}' }} | Select-Object -First 1;"
        "if (-not $cert) { 'not-found'; exit 0 };"
        "$out = [pscustomobject]@{ Provider=''; Hardware=$false; KeyType='' };"
        "try {"
        " $rsa = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPrivateKey($cert);"
        " if ($rsa) { $out.KeyType='RSA';"
        "  try { $out.Hardware = [bool]$rsa.Key.CspKeyContainerInfo.HardwareDevice } catch {}"
        "  try { $out.Provider = [string]$rsa.Key.CspKeyContainerInfo.ProviderName } catch {} }"
        "} catch {};"
        "$out | ConvertTo-Json -Compress"
    )
    argv = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]
    try:
        completed = (runner or _run)(argv, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"storage": "unknown", "detail": f"{exc.__class__.__name__}: {exc}"}
    raw = (completed.stdout or "").strip()
    if completed.returncode != 0:
        return {"storage": "unknown", "detail": f"powershell exited {completed.returncode}"}
    if raw.strip().lower() == "not-found":
        return {"storage": "unknown", "detail": f"{clean.upper()} is not in this user's store"}
    import json

    try:
        payload = json.loads(raw)
    except ValueError:
        return {"storage": "unknown", "detail": "could not parse the key metadata"}
    provider = str(payload.get("Provider", "") or "")
    hardware = bool(payload.get("Hardware"))
    key_type = str(payload.get("KeyType", "") or "")
    if hardware:
        return {
            "storage": "hardware-token",
            "detail": f"{key_type or 'key'} in '{provider or 'token provider'}' reports a hardware device",
        }
    if provider:
        return {
            "storage": "software-store",
            "detail": f"{key_type or 'key'} in '{provider}' (no hardware device reported)",
        }
    return {"storage": "unknown", "detail": "the key provider did not report its storage"}


def plan_signing(
    shell_dir: Path | str | None,
    *,
    thumbprint: str = "",
    pfx: Path | str | None = None,
    signtool_path: Path | str | None = None,
    require_hardware: bool = False,
) -> SigningPlan:
    """Check every prerequisite *before* a signing run.

    Returns blockers (things that make signing impossible right now) and notes
    (things that matter but do not stop it). Nothing here writes, signs or opens
    a network connection.

    ``require_hardware`` is for EV / post-2023 OV certificates, whose private key
    must live on a token (or in a TPM-backed container) rather than in an
    exportable PFX. With it on, a PFX is refused outright and a certificate-store
    thumbprint is only accepted if its key is hardware-backed - which is checked
    by *reading the certificate's own metadata*, and reported as
    ``not-verified`` when that metadata cannot be read, never assumed.
    """

    blockers: list[str] = []
    notes: list[str] = []

    artifacts = [str(path) for path in discover_installer_artifacts(shell_dir)]
    if not artifacts:
        blockers.append(
            "no installer found - build one first: "
            "cd frontend && npm run tauri:build (writes target/release/bundle/)"
        )

    tool = find_signtool(signtool_path)
    if tool is None:
        blockers.append(
            "signtool.exe not found - install the Windows SDK or pass --signtool <path>"
        )
    else:
        notes.append(f"signtool: {tool}")

    clean = re.sub(r"[^0-9A-Fa-f]", "", thumbprint or "")
    if clean:
        if len(clean) < 20:
            blockers.append("--thumbprint is too short to be a certificate thumbprint")
        else:
            notes.append(f"certificate thumbprint: {clean.upper()}")
    elif pfx:
        path = Path(pfx).expanduser()
        if not path.is_file():
            blockers.append(f"PFX file not found: {path}")
        else:
            notes.append(f"PFX: {path}")
    else:
        blockers.append(
            "no certificate selected - pass --thumbprint (recommended) or --pfx; "
            "run `pc-agent release signing --certificates` to list what this user has"
        )

    if not _IS_WINDOWS:
        notes.append(
            "not on Windows: signtool and WinVerifyTrust cannot run here, so signing "
            "must be done on the build machine"
        )

    if require_hardware:
        if pfx and not clean:
            blockers.append(
                "--require-hardware refuses a PFX: an EV / post-2023 OV key must stay "
                "on the token, and a PFX is an exportable copy of that key. Use the "
                "certificate-store thumbprint instead."
            )
        elif clean:
            storage = certificate_storage(clean)
            if storage["storage"] == "hardware-token":
                notes.append(
                    f"hardware-backed key confirmed for {clean.upper()} "
                    f"({storage['detail']})"
                )
            elif storage["storage"] == "software-store":
                blockers.append(
                    f"--require-hardware: the private key for {clean.upper()} is a "
                    f"software key ({storage['detail']}), not a hardware token. An EV "
                    "certificate cannot be exported this way; check that the token is "
                    "plugged in and its middleware is running."
                )
            else:
                notes.append(
                    f"--require-hardware: could not read the key storage for "
                    f"{clean.upper()} ({storage['detail']}); signtool will still be the "
                    "judge, and the signature result is verified afterwards"
                )
        notes.append("hardware key required (EV / post-2023 OV policy)")

    notes.append(f"timestamp authority: {DEFAULT_TIMESTAMP_URL}")
    return SigningPlan(
        ready=not blockers,
        artifacts=tuple(artifacts),
        blockers=tuple(blockers),
        notes=tuple(notes),
    )


def sign_bundle(
    shell_dir: Path | str | None,
    *,
    thumbprint: str = "",
    pfx: Path | str | None = None,
    password: str | None = None,
    timestamp_url: str = DEFAULT_TIMESTAMP_URL,
    allow_argv_password: bool = False,
    signtool_path: Path | str | None = None,
    timeout: float = 900.0,
    runner: Any = None,
    require_hardware: bool = False,
) -> dict[str, Any]:
    """Sign and verify every installer in a Tauri bundle, one by one.

    Signing only the ``.exe`` is a common mistake: the MSI installs the same
    payload and SmartScreen looks at the file the user actually downloads. Both
    are signed, and each result is verified independently, so a report cannot say
    "signed" for a file that Windows would still call untrusted.
    """

    plan = plan_signing(
        shell_dir,
        thumbprint=thumbprint,
        pfx=pfx,
        signtool_path=signtool_path,
        require_hardware=require_hardware,
    )
    results: list[dict[str, Any]] = []
    if not plan.ready:
        # Preflight already said this cannot work (no build, no signtool, no
        # certificate). Attempting anyway would only produce "refused" noise and
        # could half-sign a bundle.
        return {
            "ok": False,
            "ready": False,
            "blockers": list(plan.blockers),
            "notes": list(plan.notes),
            "artifacts": len(plan.artifacts),
            "signed": 0,
            "results": [],
        }
    for artifact in plan.artifacts:
        report = sign_installer(
            artifact,
            thumbprint=thumbprint,
            pfx=pfx,
            password=password,
            timestamp_url=timestamp_url,
            allow_argv_password=allow_argv_password,
            signtool_path=signtool_path,
            timeout=timeout,
            runner=runner,
        )
        results.append(report.to_dict())
    signed = [item for item in results if item["ok"]]
    return {
        "ok": plan.ready and bool(results) and len(signed) == len(results),
        "ready": plan.ready,
        "blockers": list(plan.blockers),
        "notes": list(plan.notes),
        "artifacts": len(plan.artifacts),
        "signed": len(signed),
        "results": results,
    }
