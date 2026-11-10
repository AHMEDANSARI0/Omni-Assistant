"""0.82.8: EV / post-2023 OV certificates keep their key on hardware. Say so.

Since June 2023 a publicly-trusted code-signing certificate must have its private
key in a hardware module (a token, an HSM, or a TPM-backed container). That turns
"sign it" into a policy question with a real trap: a PFX is an *exportable copy*
of the key, which is precisely what the CA rules forbid, and a software-stored
key will not sign what the user thinks it signs.

So signing gained a mode that says it out loud:

* ``--require-hardware`` refuses a PFX outright, with the reason;
* a certificate-store thumbprint is accepted only when its own metadata reports a
  hardware device, and refused when the metadata says "software container";
* when the metadata cannot be read (a custom CNG/KSP provider, an HSM that does
  not expose the legacy CSP fields, or a non-Windows host), the answer is a
  **note**, never a silent pass and never a fake "hardware confirmed".

Everything here reads public certificate metadata - provider name and the
``HardwareDevice`` flag. The private key is never exported, read or unlocked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pc_agent import cli
from pc_agent.windows_signing import (
    certificate_storage,
    list_signing_certificates,
    plan_signing,
    sign_bundle,
)

THUMBPRINT = "B" * 40


class Completed:
    def __init__(self, code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


def _bundle(tmp_path: Path) -> Path:
    shell = tmp_path / "src-tauri"
    for folder, name in (
        ("nsis", "Omni_0.82.9_x64-setup.exe"),
        ("msi", "Omni_0.82.9_x64_en-US.msi"),
    ):
        target = shell / "target" / "release" / "bundle" / folder
        target.mkdir(parents=True)
        (target / name).write_bytes(b"MZ")
    return shell


def _windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pc_agent.windows_signing._IS_WINDOWS", True)
    monkeypatch.setattr(
        "pc_agent.windows_signing.find_signtool", lambda explicit=None: Path("/opt/signtool.exe")
    )


# ------------------------------------------------------- certificate listing

def test_listing_reports_key_storage_per_certificate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pc_agent.windows_signing._IS_WINDOWS", True)
    payload = json.dumps(
        [
            {
                "Subject": "CN=Token EV",
                "Thumbprint": THUMBPRINT,
                "NotBefore": "2026-01-01T00:00:00.0000000+00:00",
                "NotAfter": "2027-01-01T00:00:00.0000000+00:00",
                "HasPrivateKey": True,
                "Issuer": "CN=CA",
                "KeyType": "RSA",
                "ProviderName": "eToken Base Cryptographic Provider",
                "HardwareDevice": True,
            },
            {
                "Subject": "CN=Software",
                "Thumbprint": "C" * 40,
                "NotBefore": "2026-01-01T00:00:00.0000000+00:00",
                "NotAfter": "2027-01-01T00:00:00.0000000+00:00",
                "HasPrivateKey": True,
                "Issuer": "CN=CA",
                "KeyType": "RSA",
                "ProviderName": "Microsoft Software Key Storage Provider",
                "HardwareDevice": False,
            },
        ]
    )
    listing = list_signing_certificates(runner=lambda argv, timeout: Completed(0, payload))
    assert listing["status"] == "ok"
    assert listing["usable"] == 2
    assert listing["hardware_backed"] == 1
    first, second = listing["certificates"]
    assert first["storage"] == "hardware-token"
    assert "eToken" in first["provider"]
    assert second["storage"] == "software-store"
    assert "hardware-backed" in listing["detail"]


def test_listing_never_claims_hardware_it_cannot_see(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pc_agent.windows_signing._IS_WINDOWS", True)
    payload = json.dumps(
        {
            "Subject": "CN=Custom KSP",
            "Thumbprint": THUMBPRINT,
            "HasPrivateKey": True,
            "ProviderName": "",
            "HardwareDevice": False,
        }
    )
    listing = list_signing_certificates(runner=lambda argv, timeout: Completed(0, payload))
    assert listing["certificates"][0]["storage"] == "unknown"
    assert listing["hardware_backed"] == 0


# ------------------------------------------------------- certificate_storage

def test_storage_reports_a_hardware_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pc_agent.windows_signing._IS_WINDOWS", True)
    payload = json.dumps({"Provider": "SafeNet Smart Card Key Storage Provider", "Hardware": True,
                          "KeyType": "RSA"})
    result = certificate_storage(THUMBPRINT, runner=lambda argv, timeout: Completed(0, payload))
    assert result["storage"] == "hardware-token"
    assert "SafeNet" in result["detail"]


def test_storage_reports_a_software_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pc_agent.windows_signing._IS_WINDOWS", True)
    payload = json.dumps({"Provider": "Microsoft Software Key Storage Provider", "Hardware": False,
                          "KeyType": "RSA"})
    result = certificate_storage(THUMBPRINT, runner=lambda argv, timeout: Completed(0, payload))
    assert result["storage"] == "software-store"
    assert "no hardware device" in result["detail"]


def test_storage_says_unknown_for_an_absent_certificate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pc_agent.windows_signing._IS_WINDOWS", True)
    result = certificate_storage(THUMBPRINT, runner=lambda argv, timeout: Completed(0, "not-found"))
    assert result["storage"] == "unknown"
    assert "not in this user's store" in result["detail"]


def test_storage_is_unknown_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pc_agent.windows_signing._IS_WINDOWS", False)
    assert certificate_storage(THUMBPRINT)["storage"] == "unknown"


def test_storage_survives_a_broken_powershell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pc_agent.windows_signing._IS_WINDOWS", True)
    result = certificate_storage(
        THUMBPRINT, runner=lambda argv, timeout: Completed(1, "", "Access denied")
    )
    assert result["storage"] == "unknown"
    assert "exited 1" in result["detail"]


def test_storage_never_exports_a_key() -> None:
    source = (
        Path(__file__).resolve().parent.parent / "src" / "pc_agent" / "windows_signing.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("Export-PfxCertificate", "certutil -exportPFX", "Export("):
        assert forbidden not in source, forbidden


# ------------------------------------------------------------ require-hardware

def test_require_hardware_refuses_a_pfx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = _bundle(tmp_path)
    _windows(monkeypatch)
    pfx = tmp_path / "codesign.pfx"
    pfx.write_bytes(b"\x00")
    plan = plan_signing(shell, pfx=pfx, require_hardware=True)
    assert plan.ready is False
    assert any("refuses a PFX" in blocker for blocker in plan.blockers)
    assert any("exportable copy" in blocker for blocker in plan.blockers)


def test_require_hardware_accepts_a_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = _bundle(tmp_path)
    _windows(monkeypatch)
    monkeypatch.setattr(
        "pc_agent.windows_signing.certificate_storage",
        lambda thumbprint, **kwargs: {"storage": "hardware-token", "detail": "RSA in 'eToken'"},
    )
    plan = plan_signing(shell, thumbprint=THUMBPRINT, require_hardware=True)
    assert plan.ready is True, plan.blockers
    assert any("hardware-backed key confirmed" in note for note in plan.notes)


def test_require_hardware_refuses_a_software_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = _bundle(tmp_path)
    _windows(monkeypatch)
    monkeypatch.setattr(
        "pc_agent.windows_signing.certificate_storage",
        lambda thumbprint, **kwargs: {
            "storage": "software-store",
            "detail": "RSA in 'Microsoft Software Key Storage Provider' (no hardware device reported)",
        },
    )
    plan = plan_signing(shell, thumbprint=THUMBPRINT, require_hardware=True)
    assert plan.ready is False
    assert any("not a hardware token" in blocker for blocker in plan.blockers)
    assert any("middleware" in blocker for blocker in plan.blockers)


def test_require_hardware_is_a_note_when_storage_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = _bundle(tmp_path)
    _windows(monkeypatch)
    monkeypatch.setattr(
        "pc_agent.windows_signing.certificate_storage",
        lambda thumbprint, **kwargs: {"storage": "unknown", "detail": "KSP did not report storage"},
    )
    plan = plan_signing(shell, thumbprint=THUMBPRINT, require_hardware=True)
    assert plan.ready is True, "an unreadable provider must not block signing by guesswork"
    assert any("could not read the key storage" in note for note in plan.notes)


def test_without_the_flag_nothing_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = _bundle(tmp_path)
    _windows(monkeypatch)
    plan = plan_signing(shell, thumbprint=THUMBPRINT)
    assert plan.ready is True
    assert not any("hardware key required" in note for note in plan.notes)


# --------------------------------------------------------------- sign_bundle

def test_sign_bundle_forwards_the_policy_and_refuses_to_sign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = _bundle(tmp_path)
    _windows(monkeypatch)
    pfx = tmp_path / "x.pfx"
    pfx.write_bytes(b"\x00")
    result = sign_bundle(
        shell,
        pfx=pfx,
        password="secret",
        require_hardware=True,
        runner=lambda argv, timeout: pytest.fail("signtool must not run when the policy fails"),
    )
    assert result["ok"] is False
    assert result["results"] == []
    assert any("refuses a PFX" in blocker for blocker in result["blockers"])


# -------------------------------------------------------------------- CLI

def test_cli_has_the_flag() -> None:
    args = cli.build_parser().parse_args(["release", "sign-bundle", "--require-hardware", "--check"])
    assert args.require_hardware is True


def test_cli_check_reports_the_hardware_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pc_agent.config import Settings

    shell = _bundle(tmp_path)
    monkeypatch.setenv("PC_AGENT_SIGN_THUMBPRINT", THUMBPRINT)
    monkeypatch.setattr(
        "pc_agent.windows_signing.certificate_storage",
        lambda thumbprint, **kwargs: {"storage": "software-store", "detail": "software provider"},
    )
    settings = Settings.from_env(tmp_path / "install")
    settings.ensure_directories()
    args = cli.build_parser().parse_args(
        ["release", "sign-bundle", "--check", "--require-hardware", "--shell", str(shell)]
    )
    code = cli._run_release_sign_bundle(settings, args)
    out = capsys.readouterr().out
    assert code == 1, "a software key under an EV policy must not be READY"
    assert "hardware key required" in out
    assert "not a hardware token" in out


def test_the_docs_explain_the_policy() -> None:
    doc = (Path(__file__).resolve().parent.parent / "docs" / "authenticode_signing.md").read_text(
        encoding="utf-8"
    )
    assert "--require-hardware" in doc
    assert "hardware" in doc.lower()
    assert "June 2023" in doc or "2023" in doc
