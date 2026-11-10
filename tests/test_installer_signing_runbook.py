"""0.82.5: signing the *installers* is a guided, verifiable runbook.

Signing an update bundle was already covered (Phase 21). What a user actually
downloads from the desktop build is an NSIS ``.exe`` and an ``.msi``, and those
need Authenticode - which needs the user's own certificate, which this product
never generates, stores or asks for beyond an environment variable.

These tests pin the three things that make that practical without pretending to
have a Windows machine here:

* **discovery** - only installer files inside the known bundle folders are ever
  considered, so a typo cannot sign an arbitrary file on disk;
* **preflight** (`pc-agent release sign-bundle --check`) - names exactly what is
  missing (no build, no SDK, no certificate) and signs nothing;
* **bundle signing** - *both* the ``.exe`` and the ``.msi`` are signed and each
  result is verified, so a report can never say "signed" for a file Windows would
  still call untrusted;
* **certificate listing** - answers "which thumbprint do I pass?" from the user's
  own store, and says ``not-run`` off Windows instead of inventing an empty list.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pc_agent import cli
from pc_agent.windows_signing import (
    discover_installer_artifacts,
    list_signing_certificates,
    plan_signing,
    sign_bundle,
)

ROOT = Path(__file__).resolve().parent.parent
THUMBPRINT = "A" * 40


# ------------------------------------------------------------------ helpers

def _bundle_tree(tmp_path: Path) -> Path:
    """A fake Tauri bundle folder with one NSIS exe and one MSI."""

    shell = tmp_path / "src-tauri"
    (shell / "target" / "release" / "bundle" / "nsis").mkdir(parents=True)
    (shell / "target" / "release" / "bundle" / "msi").mkdir(parents=True)
    (shell / "target" / "release" / "bundle" / "nsis" / "Omni_0.82.9_x64-setup.exe").write_bytes(b"MZ" + b"\0" * 64)
    (shell / "target" / "release" / "bundle" / "msi" / "Omni_0.82.9_x64_en-US.msi").write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 64)
    # Noise that must never be picked up.
    (shell / "target" / "release" / "bundle" / "nsis" / "notes.txt").write_bytes(b"not an installer")
    (shell / "target" / "release" / "omni-command-center.exe").write_bytes(b"MZ")  # the app itself
    return shell


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# --------------------------------------------------------------- discovery

def test_discovery_only_returns_bundle_installers(tmp_path: Path) -> None:
    shell = _bundle_tree(tmp_path)
    found = discover_installer_artifacts(shell)
    names = sorted(path.name for path in found)
    assert names == ["Omni_0.82.9_x64-setup.exe", "Omni_0.82.9_x64_en-US.msi"]
    assert all(path.suffix in {".exe", ".msi"} for path in found)
    assert not any(path.name == "omni-command-center.exe" for path in found)


def test_discovery_of_a_missing_shell_is_empty_not_an_error(tmp_path: Path) -> None:
    assert discover_installer_artifacts(tmp_path / "nope") == []
    assert discover_installer_artifacts(None) == []


# ---------------------------------------------------------------- preflight

def test_preflight_names_every_blocker(tmp_path: Path) -> None:
    plan = plan_signing(tmp_path / "empty")
    assert plan.ready is False
    joined = " | ".join(plan.blockers)
    assert "no installer found" in joined
    assert "signtool.exe not found" in joined or "not on Windows" in " ".join(plan.notes)
    assert "no certificate selected" in joined


def test_preflight_ready_needs_artifacts_tool_and_certificate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = _bundle_tree(tmp_path)
    monkeypatch.setattr("pc_agent.windows_signing.find_signtool", lambda explicit=None: Path("/opt/signtool.exe"))
    monkeypatch.setattr("pc_agent.windows_signing._IS_WINDOWS", True)
    plan = plan_signing(shell, thumbprint=THUMBPRINT)
    assert plan.ready is True, plan.blockers
    assert len(plan.artifacts) == 2
    assert any(THUMBPRINT in note for note in plan.notes)


def test_preflight_rejects_a_short_thumbprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = _bundle_tree(tmp_path)
    monkeypatch.setattr("pc_agent.windows_signing.find_signtool", lambda explicit=None: Path("/opt/signtool.exe"))
    plan = plan_signing(shell, thumbprint="ABC")
    assert plan.ready is False
    assert any("too short" in blocker for blocker in plan.blockers)


def test_preflight_reports_a_missing_pfx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = _bundle_tree(tmp_path)
    monkeypatch.setattr("pc_agent.windows_signing.find_signtool", lambda explicit=None: Path("/opt/signtool.exe"))
    plan = plan_signing(shell, pfx=tmp_path / "missing.pfx")
    assert any("PFX file not found" in blocker for blocker in plan.blockers)


def test_cli_check_endpoint_signs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pc_agent.config import Settings

    settings = Settings.from_env(tmp_path)
    settings.ensure_directories()
    args = cli.build_parser().parse_args(["release", "sign-bundle", "--check", "--json"])
    called: list[str] = []
    monkeypatch.setattr(
        "pc_agent.windows_signing.sign_installer",
        lambda *a, **k: called.append("signed"),
    )
    code = cli._run_release_sign_bundle(settings, args)
    assert code == 1, "an empty tree cannot be ready"
    assert called == [], "--check must not sign anything"


# ------------------------------------------------------------ bundle signing

def test_bundle_signs_and_verifies_every_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = _bundle_tree(tmp_path)
    monkeypatch.setattr("pc_agent.windows_signing.find_signtool", lambda explicit=None: Path("/opt/signtool.exe"))
    monkeypatch.setattr("pc_agent.windows_signing._IS_WINDOWS", True)

    seen: list[list[str]] = []

    def fake_run(argv, timeout):
        seen.append(list(argv))
        return _FakeCompleted(0, "Done Adding Additional Store\nSuccessfully signed")

    # `verify_authenticode` would need the real OS API; this test is about the
    # contract that every artifact is signed and then checked.
    monkeypatch.setattr(
        "pc_agent.windows_signing.verify_authenticode",
        lambda path, **kwargs: type(
            "R", (), {"ok": True, "status": "trusted", "detail": "signature verified"}
        )(),
    )

    result = sign_bundle(shell, thumbprint=THUMBPRINT, runner=fake_run)
    assert result["ok"] is True
    assert result["artifacts"] == 2
    assert result["signed"] == 2
    assert len(seen) == 2, "both the EXE and the MSI must be signed"
    for argv in seen:
        assert "/sha1" in argv and THUMBPRINT in argv
        assert "/tr" in argv, "a timestamp keeps the signature alive after expiry"
        assert any(str(argv[-1]).endswith((".exe", ".msi")) for _ in [0])


def test_bundle_is_not_ok_when_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = _bundle_tree(tmp_path)
    monkeypatch.setattr("pc_agent.windows_signing.find_signtool", lambda explicit=None: Path("/opt/signtool.exe"))
    monkeypatch.setattr("pc_agent.windows_signing._IS_WINDOWS", True)
    monkeypatch.setattr(
        "pc_agent.windows_signing.verify_authenticode",
        lambda path, **kwargs: type(
            "R", (), {"ok": False, "status": "untrusted", "detail": "no chain to a trusted root"}
        )(),
    )
    result = sign_bundle(
        shell,
        thumbprint=THUMBPRINT,
        runner=lambda argv, timeout: _FakeCompleted(0, "Successfully signed"),
    )
    assert result["ok"] is False, "signtool success alone must not count as signed"
    assert result["signed"] == 0
    assert all("untrusted" in item["status"] for item in result["results"])


def test_bundle_without_certificate_does_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = _bundle_tree(tmp_path)
    monkeypatch.setattr("pc_agent.windows_signing.find_signtool", lambda explicit=None: Path("/opt/signtool.exe"))
    result = sign_bundle(shell, runner=lambda argv, timeout: pytest.fail("must not run signtool"))
    assert result["ok"] is False
    assert result["results"] == []
    assert any("no certificate selected" in blocker for blocker in result["blockers"])


# ----------------------------------------------------- certificate listing

def test_certificate_listing_is_not_run_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pc_agent.windows_signing._IS_WINDOWS", False)
    listing = list_signing_certificates()
    assert listing["status"] == "not-run"
    assert listing["certificates"] == []
    assert "Windows" in listing["detail"]


def test_certificate_listing_parses_a_windows_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pc_agent.windows_signing._IS_WINDOWS", True)

    class Runner:
        def __init__(self, payload: str, code: int = 0) -> None:
            self.payload = payload
            self.code = code
            self.argv: list[str] = []

        def __call__(self, argv, timeout):
            self.argv = list(argv)
            return _FakeCompleted(self.code, self.payload)

    payload = json.dumps(
        {
            "Subject": "CN=Omni Code Signing, O=Home",
            "Thumbprint": THUMBPRINT,
            "NotBefore": "2026-01-01T00:00:00.0000000+00:00",
            "NotAfter": "2027-01-01T00:00:00.0000000+00:00",
            "HasPrivateKey": True,
            "Issuer": "CN=Omni Code Signing, O=Home",
        }
    )
    runner = Runner(payload)
    listing = list_signing_certificates(runner=runner)
    assert listing["status"] == "ok"
    assert listing["usable"] == 1
    certificate = listing["certificates"][0]
    assert certificate["thumbprint"] == THUMBPRINT
    assert certificate["has_private_key"] is True
    assert "CurrentUser" in " ".join(runner.argv)
    assert "Cert" in " ".join(runner.argv)


def test_certificate_listing_reports_an_empty_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pc_agent.windows_signing._IS_WINDOWS", True)
    listing = list_signing_certificates(runner=lambda argv, timeout: _FakeCompleted(0, ""))
    assert listing["status"] == "empty"
    assert "no code-signing certificate" in listing["detail"]


def test_certificate_listing_survives_powershell_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pc_agent.windows_signing._IS_WINDOWS", True)
    listing = list_signing_certificates(
        runner=lambda argv, timeout: _FakeCompleted(1, "", "Access is denied")
    )
    assert listing["status"] == "error"
    assert "Access is denied" in listing["detail"]


# --------------------------------------------------------------- doc + CLI

def test_cli_surfaces_exist() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["release", "sign-bundle"]).command == "release"
    assert parser.parse_args(["release", "sign-bundle", "--check"]).check is True
    assert parser.parse_args(["release", "signing", "--certificates"]).certificates is True
    source = (ROOT / "src" / "pc_agent" / "cli.py").read_text(encoding="utf-8")
    assert "def _run_release_sign_bundle(" in source
    # The report name lives in one constant, used by both the writer and the
    # reputation reader, so the two can never drift apart.
    assert "INSTALLER_SIGNING_REPORT" in source
    assert 'INSTALLER_SIGNING_REPORT = "installer-signing.json"' in (
        ROOT / "src" / "pc_agent" / "windows_signing.py"
    ).read_text(encoding="utf-8")


def test_the_runbook_is_documented() -> None:
    doc = ROOT / "docs" / "authenticode_signing.md"
    assert doc.is_file(), "the signing runbook must ship"
    text = doc.read_text(encoding="utf-8")
    for step in (
        "release signing --certificates",
        "release sign-bundle --check",
        "release sign-bundle",
        "PC_AGENT_SIGN_THUMBPRINT",
        "release signature",
    ):
        assert step in text, step
    guide = (ROOT / "INSTALLATION_GUIDE.md").read_text(encoding="utf-8")
    assert "authenticode_signing.md" in guide


def test_no_certificate_material_is_ever_written() -> None:
    module = (ROOT / "src" / "pc_agent" / "windows_signing.py").read_text(encoding="utf-8")
    for forbidden in ("BEGIN CERTIFICATE", "BEGIN PRIVATE KEY", "Export-PfxCertificate", "certutil -exportPFX"):
        assert forbidden not in module
    # The public API never accepts a password positionally or as argv by default.
    assert "allow_argv_password: bool = False" in module


def test_pfx_password_only_comes_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pc_agent.config import Settings

    settings = Settings.from_env(tmp_path)
    settings.ensure_directories()
    args = cli.build_parser().parse_args(
        ["release", "sign-bundle", "--pfx", str(tmp_path / "x.pfx")]
    )
    monkeypatch.delenv("PC_AGENT_SIGN_PFX_PASSWORD", raising=False)
    code = cli._run_release_sign_bundle(settings, args)
    assert code == 2
    text = (ROOT / "src" / "pc_agent" / "cli.py").read_text(encoding="utf-8")
    assert "PC_AGENT_SIGN_PFX_PASSWORD" in text
    # `--pfx-password-env` names an environment variable (fine); a literal
    # `--pfx-password` flag that takes the secret itself must not exist.
    assert re.search(r"--pfx-password(?!-env)", text) is None, (
        "a password must never be a CLI argument"
    )
    assert "--allow-argv-password" in text, "the argv exposure flag is explicit, never implicit"
