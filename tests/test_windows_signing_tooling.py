"""Authenticode signing and verification tooling (0.81.2).

The update bundle has been Ed25519-signed and enforced since Phase 21. What these
tests cover is the other half of "signed installation tooling": the Authenticode
signature on the installer EXE, which no product can create without a user's own
code-signing certificate.

So the tests check the honesty properties, not a signature:

* verification asks the operating system (``WinVerifyTrust``), and says
  ``unverifiable`` on a host that cannot answer instead of guessing;
* a PFX password is refused unless the caller explicitly accepts that ``argv`` is
  world-readable, and the recommended thumbprint path never carries a secret;
* captured tool output is scrubbed before it is stored;
* a missing ``signtool`` produces a structured result, not an exception;
* the release audit reports ``unsigned`` as a fact and only fails when a
  *recorded* signing attempt produced something that does not verify.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from pc_agent.config import Settings
from pc_agent.release import audit_install
from pc_agent.windows_signing import (
    DEFAULT_TIMESTAMP_URL,
    build_sign_argv,
    find_signtool,
    sanitize_tool_output,
    sign_installer,
    signtool_status,
    verify_authenticode,
    winverifytrust_check,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
THUMBPRINT = "A1B2C3D4E5F60718293A4B5C6D7E8F9012345678"


def _settings(tmp_path: Path) -> Settings:
    root = tmp_path / "install"
    data = tmp_path / "data"
    root.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    settings = Settings.from_env(
        root,
        environ={
            "PC_AGENT_DATA_DIR": str(data),
            "PC_AGENT_WORKSPACE_DIR": str(tmp_path / "workspace"),
            "PC_AGENT_DB_PATH": str(data / "pc_agent.db"),
        },
        load_dotenv=False,
    )
    settings.ensure_directories()
    return settings


def _artifact(tmp_path: Path) -> Path:
    target = tmp_path / "omni-assistant-setup.exe"
    target.write_bytes(b"MZ" + b"\x00" * 128)
    return target


class FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------- commands ---


def test_thumbprint_mode_never_carries_a_password() -> None:
    argv = build_sign_argv("signtool.exe", "setup.exe", thumbprint=THUMBPRINT)
    assert "/sha1" in argv
    assert argv[argv.index("/sha1") + 1] == THUMBPRINT
    assert "/p" not in argv
    assert "/f" not in argv
    assert argv[-1] == "setup.exe"
    assert "/fd" in argv and argv[argv.index("/fd") + 1] == "sha256"
    assert "/tr" in argv and argv[argv.index("/tr") + 1] == DEFAULT_TIMESTAMP_URL
    # A timestamp is what keeps the signature valid after the certificate expires.
    assert "/td" in argv


def test_pfx_password_is_refused_unless_the_exposure_is_accepted() -> None:
    with pytest.raises(ValueError) as refused:
        build_sign_argv("signtool.exe", "setup.exe", pfx="cert.pfx", password="hunter2")
    assert "argv is visible to other processes" in str(refused.value)

    argv = build_sign_argv(
        "signtool.exe",
        "setup.exe",
        pfx="cert.pfx",
        password="hunter2",
        allow_argv_password=True,
    )
    assert argv[argv.index("/p") + 1] == "hunter2"
    assert argv[argv.index("/f") + 1] == "cert.pfx"


def test_signing_needs_a_certificate_and_never_generates_one() -> None:
    with pytest.raises(ValueError) as refused:
        build_sign_argv("signtool.exe", "setup.exe")
    assert "never generates a code-signing key" in str(refused.value)

    with pytest.raises(ValueError):
        build_sign_argv("signtool.exe", "setup.exe", thumbprint="ABC")


def test_thumbprint_is_normalised_and_validated() -> None:
    argv = build_sign_argv("signtool.exe", "a.exe", thumbprint=THUMBPRINT.lower())
    assert argv[argv.index("/sha1") + 1] == THUMBPRINT
    spaced = THUMBPRINT[:8] + " " + THUMBPRINT[8:]
    argv = build_sign_argv("signtool.exe", "a.exe", thumbprint=spaced)
    assert argv[argv.index("/sha1") + 1] == THUMBPRINT


# ------------------------------------------------------------ verification ---


def test_verification_answers_unverifiable_off_windows(tmp_path: Path) -> None:
    import sys

    trust = winverifytrust_check(_artifact(tmp_path))
    if sys.platform.startswith("win"):
        assert trust["status"] in {"trusted-signed", "unsigned", "untrusted"}
    else:
        assert trust["status"] == "unverifiable"
        assert "Windows API" in trust["detail"]

    report = verify_authenticode(_artifact(tmp_path))
    assert report.ok is False


def test_verification_reports_a_missing_file_instead_of_raising(tmp_path: Path) -> None:
    report = verify_authenticode(tmp_path / "nope.exe")
    assert report.status == "missing"
    assert report.ok is False


def test_tool_output_is_scrubbed_before_it_is_stored() -> None:
    text = "Signing failed with password hunter2 and key sk-abcdef0123456789"
    cleaned = sanitize_tool_output(text, secrets=["hunter2"])
    assert "hunter2" not in cleaned
    assert "[redacted]" in cleaned


def test_a_missing_signtool_is_a_structured_result(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert find_signtool(tmp_path / "nothing.exe") is None
    report = sign_installer(_artifact(tmp_path), thumbprint=THUMBPRINT)
    assert report.status == "tool-missing"
    assert report.ok is False
    assert "no signature was produced" in report.detail


def test_signing_command_is_built_and_verified_with_an_injected_runner(
    tmp_path: Path, monkeypatch
) -> None:
    """Exercise the real command path without a Windows SDK."""

    from pc_agent import windows_signing

    tool = tmp_path / "signtool.exe"
    tool.write_bytes(b"stub")
    monkeypatch.setattr(windows_signing, "find_signtool", lambda *_a, **_k: tool)
    monkeypatch.setattr(
        windows_signing,
        "verify_authenticode",
        lambda *a, **k: windows_signing.SigningReport(
            ok=True, status="trusted-signed", detail="WinVerifyTrust: trusted"
        ),
    )
    seen: list[list[str]] = []

    def runner(argv: list[str], *, timeout: float) -> Any:
        seen.append(argv)
        return FakeCompleted(0, stdout="Successfully signed")

    artifact = _artifact(tmp_path)
    report = sign_installer(
        artifact, thumbprint=THUMBPRINT, signtool_path=tool, runner=runner
    )
    assert report.ok is True
    assert report.status == "signed-verified"
    assert report.timestamped is True
    assert seen and seen[0][1] == "sign"
    assert THUMBPRINT in seen[0]
    assert str(artifact) in seen[0]


def test_a_failing_signtool_is_reported_not_raised(tmp_path: Path, monkeypatch) -> None:
    from pc_agent import windows_signing

    tool = tmp_path / "signtool.exe"
    tool.write_bytes(b"stub")
    monkeypatch.setattr(windows_signing, "find_signtool", lambda *_a, **_k: tool)

    def runner(argv: list[str], *, timeout: float) -> Any:
        return FakeCompleted(1, stderr="SignTool Error: No certificates were found")

    report = sign_installer(
        _artifact(tmp_path), thumbprint=THUMBPRINT, signtool_path=tool, runner=runner
    )
    assert report.ok is False
    assert report.status == "failed"
    assert "signtool exited 1" in report.detail
    assert "No certificates" in report.detail


def test_pfx_password_is_redacted_from_the_reported_command(
    tmp_path: Path, monkeypatch
) -> None:
    from pc_agent import windows_signing

    tool = tmp_path / "signtool.exe"
    tool.write_bytes(b"stub")
    monkeypatch.setattr(windows_signing, "find_signtool", lambda *_a, **_k: tool)
    monkeypatch.setattr(
        windows_signing,
        "verify_authenticode",
        lambda *a, **k: windows_signing.SigningReport(ok=True, status="trusted-signed", detail="ok"),
    )
    secret = "s3cr3t-pfx-password"

    def runner(argv: list[str], *, timeout: float) -> Any:
        # A careless tool echoing the password must not end up in the report.
        return FakeCompleted(0, stdout=f"signed with {secret}")

    report = sign_installer(
        _artifact(tmp_path),
        pfx=tmp_path / "cert.pfx",
        password=secret,
        allow_argv_password=True,
        signtool_path=tool,
        runner=runner,
    )
    assert report.ok is True
    assert secret not in json.dumps(report.to_dict())


def test_signtool_status_is_secret_free(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PC_AGENT_SIGN_THUMBPRINT", THUMBPRINT)
    monkeypatch.setenv("PC_AGENT_SIGN_PFX", str(tmp_path / "missing.pfx"))
    status = signtool_status()
    assert status["thumbprint_configured"] is True
    assert status["pfx_configured"] is False  # configured but not a real file
    assert status["certificate_configured"] is True
    assert THUMBPRINT not in json.dumps(status)  # the value itself is never echoed
    monkeypatch.delenv("PC_AGENT_SIGN_THUMBPRINT")
    monkeypatch.delenv("PC_AGENT_SIGN_PFX")
    assert signtool_status()["certificate_configured"] is False


# ------------------------------------------------------------------- audit ---


def test_audit_reports_unsigned_as_a_fact_not_a_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PC_AGENT_SIGN_THUMBPRINT", raising=False)
    monkeypatch.delenv("PC_AGENT_SIGN_PFX", raising=False)
    settings = _settings(tmp_path)
    audit = audit_install(settings)
    check = next(c for c in audit.checks if c.name == "windows_authenticode")
    assert check.status == "pass"
    assert "unsigned" in check.detail
    assert "Ed25519" in check.detail  # the enforced signature is named, not confused
    assert len(audit.checks) == 34
    assert "windows_authenticode" in {c.name for c in audit.checks}


def test_audit_reports_a_recorded_verified_signature(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (settings.data_dir / "windows-signing.json").write_text(
        json.dumps({"status": "signed-verified", "detail": "WinVerifyTrust: trusted"}),
        encoding="utf-8",
    )
    check = next(
        c for c in audit_install(settings).checks if c.name == "windows_authenticode"
    )
    assert check.status == "pass"
    assert "verified by WinVerifyTrust" in check.detail


def test_audit_warns_when_a_signing_attempt_did_not_verify(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (settings.data_dir / "windows-signing.json").write_text(
        json.dumps({"status": "signed-untrusted", "detail": "chain is not trusted"}),
        encoding="utf-8",
    )
    check = next(
        c for c in audit_install(settings).checks if c.name == "windows_authenticode"
    )
    assert check.status == "warn"
    assert "did not produce a verified signature" in check.detail
    assert "signed-untrusted" in check.detail


def test_cli_surfaces_and_installer_docs_are_wired() -> None:
    cli = (PROJECT_ROOT / "src" / "pc_agent" / "cli.py").read_text(encoding="utf-8")
    for command in ('"signing"', '"sign"', '"signature"'):
        assert command in cli
    assert "--allow-argv-password" in cli
    guide = (PROJECT_ROOT / "INSTALLATION_GUIDE.md").read_text(encoding="utf-8")
    assert "release sign" in guide


def test_no_private_key_material_is_ever_written_by_this_module() -> None:
    source = (PROJECT_ROOT / "src" / "pc_agent" / "windows_signing.py").read_text(
        encoding="utf-8"
    )
    # The module must never create or hold a code-signing key: it signs with a
    # certificate the user already owns, and never writes key material anywhere.
    for marker in ("Ed25519PrivateKey", "private_key_bytes", "-----BEGIN", "rsa.generate"):
        assert marker not in source
    assert subprocess is not None  # imported for the real runner, not for shell-outs
