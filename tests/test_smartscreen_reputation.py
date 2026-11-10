"""0.82.9: SmartScreen honesty, and timestamps that do not give up on the first try.

Two facts drive this work:

1. **A signature is not reputation.** A signed installer with an OV certificate
   shows your *name*; SmartScreen may still warn until reputation accrues, and EV
   certificates are the ones trusted immediately. (Corrected in 0.82.12: since
  2024 EV no longer bypasses SmartScreen - both build reputation over time.)
  Nothing this product does can
   change that, so `pc-agent release signing --reputation` reports the machine's
   real state and says out loud that reputation is `not-measurable-here`.
2. **An untimestamped signature is a time bomb.** It dies with the certificate, so
   every installer ever shipped starts warning again. A failed timestamp request
   is now retried against the next authority, and the report says which one
   worked - and which ones were tried.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pc_agent import cli
from pc_agent.windows_signing import (
    TIMESTAMP_FALLBACK_URLS,
    reputation_status,
    sign_installer,
)


class Completed:
    def __init__(self, code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


def _installer(tmp_path: Path) -> Path:
    shell = tmp_path / "src-tauri"
    nsis = shell / "target" / "release" / "bundle" / "nsis"
    nsis.mkdir(parents=True)
    artifact = nsis / "Omni_0.82.9_x64-setup.exe"
    artifact.write_bytes(b"MZ")
    return shell


def _windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pc_agent.windows_signing._IS_WINDOWS", True)
    monkeypatch.setattr(
        "pc_agent.windows_signing.find_signtool", lambda explicit=None: Path("/opt/signtool.exe")
    )


# ------------------------------------------------------- timestamp fallbacks

def test_the_fallback_list_starts_with_the_default_and_has_spares() -> None:
    assert TIMESTAMP_FALLBACK_URLS[0] == "http://timestamp.digicert.com"
    assert len(TIMESTAMP_FALLBACK_URLS) >= 3
    assert len(set(TIMESTAMP_FALLBACK_URLS)) == len(TIMESTAMP_FALLBACK_URLS)


def test_a_failing_timestamp_authority_falls_through_to_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _installer(tmp_path) / "target" / "release" / "bundle" / "nsis" / (
        "Omni_0.82.9_x64-setup.exe"
    )
    _windows(monkeypatch)
    seen: list[str] = []

    def _run(argv, timeout):
        url = argv[argv.index("/tr") + 1]
        seen.append(url)
        if "digicert" in url:
            return Completed(1, "", "SignTool Error: The timestamp server returned an error")
        return Completed(0, "Successfully signed")

    monkeypatch.setattr(
        "pc_agent.windows_signing.verify_authenticode",
        lambda path, **kwargs: type("R", (), {"ok": True, "status": "trusted", "detail": "ok"})(),
    )
    report = sign_installer(artifact, thumbprint="D" * 40, runner=_run)
    assert report.ok is True
    assert len(seen) == 2, seen
    assert "digicert" in seen[0] and "sectigo" in seen[1]
    assert report.timestamp_url == seen[1]
    assert [item["exit_code"] for item in report.timestamp_attempts] == [1, 0]


def test_every_authority_failing_is_a_failure_not_a_silent_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _installer(tmp_path) / "target" / "release" / "bundle" / "nsis" / (
        "Omni_0.82.9_x64-setup.exe"
    )
    _windows(monkeypatch)
    report = sign_installer(
        artifact,
        thumbprint="D" * 40,
        runner=lambda argv, timeout: Completed(1, "", "timestamp server unreachable"),
    )
    assert report.ok is False
    assert report.status == "failed"
    assert len(report.timestamp_attempts) == len(TIMESTAMP_FALLBACK_URLS)
    assert "unreachable" in report.detail


def test_a_custom_url_is_tried_first_then_the_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _installer(tmp_path) / "target" / "release" / "bundle" / "nsis" / (
        "Omni_0.82.9_x64-setup.exe"
    )
    _windows(monkeypatch)
    seen: list[str] = []

    def _run(argv, timeout):
        seen.append(argv[argv.index("/tr") + 1])
        return Completed(0, "Successfully signed")

    monkeypatch.setattr(
        "pc_agent.windows_signing.verify_authenticode",
        lambda path, **kwargs: type("R", (), {"ok": True, "status": "trusted", "detail": "ok"})(),
    )
    report = sign_installer(
        artifact, thumbprint="D" * 40, timestamp_url="http://tsa.example/ts", runner=_run
    )
    assert seen == ["http://tsa.example/ts"]
    assert report.timestamp_url == "http://tsa.example/ts"


def test_the_report_lists_the_attempts_so_a_failure_is_diagnosable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _installer(tmp_path) / "target" / "release" / "bundle" / "nsis" / (
        "Omni_0.82.9_x64-setup.exe"
    )
    _windows(monkeypatch)
    monkeypatch.setattr(
        "pc_agent.windows_signing.verify_authenticode",
        lambda path, **kwargs: type("R", (), {"ok": True, "status": "trusted", "detail": "ok"})(),
    )
    report = sign_installer(
        artifact, thumbprint="D" * 40, runner=lambda argv, timeout: Completed(0, "Successfully signed")
    )
    payload = report.to_dict()
    assert payload["timestamp_url"] == TIMESTAMP_FALLBACK_URLS[0]
    assert payload["timestamp_attempts"][0]["timestamp_url"] == TIMESTAMP_FALLBACK_URLS[0]
    assert payload["timestamped"] is True


# ------------------------------------------------------------ reputation

def test_reputation_reports_what_it_cannot_measure(tmp_path: Path) -> None:
    shell = _installer(tmp_path)
    status = reputation_status(report_path=tmp_path / "missing.json", shell_dir=shell)
    assert status["smart_screen_reputation"] == "not-measurable-here"
    assert "cannot measure" in status["smart_screen_note"]
    assert status["expected_first_run"].startswith("unsigned")
    assert status["last_signing_run"]["status"] == "not-recorded"
    assert any("certificate" in blocker for blocker in status["blockers"])
    assert any("no verified installer" in blocker for blocker in status["blockers"])
    assert status["next_command"] == "pc-agent release sign-bundle --check"


def test_reputation_names_the_ev_expectation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = _installer(tmp_path)
    monkeypatch.setattr(
        "pc_agent.windows_signing.list_signing_certificates",
        lambda **kwargs: {
            "status": "ok",
            "detail": "1 certificate, 1 with a private key, 1 hardware-backed",
            "certificates": [
                {"subject": "CN=EV", "thumbprint": "E" * 40, "hardware": True,
                 "storage": "hardware-token", "has_private_key": True}
            ],
            "usable": 1,
            "hardware_backed": 1,
        },
    )
    monkeypatch.setattr(
        "pc_agent.windows_signing.verify_authenticode",
        lambda path, **kwargs: type("R", (), {"ok": True, "status": "trusted", "detail": "valid"})(),
    )
    report_path = tmp_path / "installer-signing.json"
    report_path.write_text(
        json.dumps({"ok": True, "artifacts": 2, "signed": 2}), encoding="utf-8"
    )
    status = reputation_status(report_path=report_path, shell_dir=shell)
    assert status["hardware_backed"] == 1
    # hardware-backed and EV are not the same claim: hardware is post-2023 policy
    # compliance, and no certificate type bypasses SmartScreen any more.
    assert "no longer bypasses" in status["expected_first_run"]
    assert status["last_signing_run"]["signed"] == 2
    assert status["installers"][0]["ok"] is True
    assert status["blockers"] == []


def test_reputation_gives_the_same_expectation_for_ov_and_ev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A certificate is a certificate: the expectation no longer depends on tier."""
    monkeypatch.setattr(
        "pc_agent.windows_signing.list_signing_certificates",
        lambda **kwargs: {
            "status": "ok",
            "detail": "1 certificate",
            "certificates": [
                {"subject": "CN=OV", "thumbprint": "F" * 40, "hardware": False,
                 "storage": "software-store", "has_private_key": True}
            ],
            "usable": 1,
            "hardware_backed": 0,
        },
    )
    monkeypatch.setattr(
        "pc_agent.windows_signing.verify_authenticode",
        lambda path, **kwargs: type("R", (), {"ok": False, "status": "not-signed", "detail": "no" })(),
    )
    status = reputation_status(report_path=None, shell_dir=None)
    assert status["expected_first_run"].startswith("signed")
    assert any("no verified installer" in blocker for blocker in status["blockers"])


def test_an_unreadable_report_is_reported_not_swallowed(tmp_path: Path) -> None:
    broken = tmp_path / "installer-signing.json"
    broken.write_bytes(b"not json")
    status = reputation_status(report_path=broken, shell_dir=None)
    assert status["last_signing_run"]["status"] == "unreadable"


# -------------------------------------------------------------------- CLI

def test_cli_has_the_reputation_flag() -> None:
    args = cli.build_parser().parse_args(["release", "signing", "--reputation"])
    assert args.reputation is True


def test_cli_prints_the_status_and_exits_non_zero_without_a_certificate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from pc_agent.config import Settings

    settings = Settings.from_env(tmp_path)
    settings.ensure_directories()
    args = cli.build_parser().parse_args(["release", "signing", "--reputation"])
    code = cli._run_release_signing(settings, args)
    out = capsys.readouterr().out
    if code == 0:  # pragma: no cover - a machine with a certificate installed
        assert "hardware-backed" in out
    else:
        assert "SmartScreen reputation: not-measurable-here" in out
        assert "REMAINING:" in out


def test_cli_json_is_machine_readable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from pc_agent.config import Settings

    settings = Settings.from_env(tmp_path)
    settings.ensure_directories()
    monkeypatch.setattr(
        "pc_agent.windows_signing.list_signing_certificates",
        lambda **kwargs: {"status": "empty", "detail": "none", "certificates": []},
    )
    args = cli.build_parser().parse_args(["release", "signing", "--reputation", "--json"])
    code = cli._run_release_signing(settings, args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["smart_screen_reputation"] == "not-measurable-here"
    assert code == 1


def test_the_runbook_ships_and_is_linked() -> None:
    root = Path(__file__).resolve().parent.parent
    doc = root / "docs" / "smartscreen_reputation.md"
    assert doc.is_file()
    text = doc.read_text(encoding="utf-8")
    for phrase in ("OV", "EV", "reputation", "timestamp.digicert.com", "Get-AuthenticodeSignature"):
        assert phrase in text, phrase
    guide = (root / "INSTALLATION_GUIDE.md").read_text(encoding="utf-8")
    assert "smartscreen_reputation.md" in guide
    signing_doc = (root / "docs" / "authenticode_signing.md").read_text(encoding="utf-8")
    assert "smartscreen_reputation.md" in signing_doc


# ------------------------------------------------ the purchase guide (0.82.12)
def test_the_certificate_purchase_guide_exists_and_is_honest() -> None:
    """The BARA item is a decision document, so its claims are pinned too."""

    from pathlib import Path as _Path

    guide = _Path(__file__).resolve().parents[1] / "docs" / "certificate_purchase_guide.md"
    text = guide.read_text(encoding="utf-8")

    # the correction that matters: EV no longer bypasses SmartScreen
    assert "no longer" in text and "2024" in text
    # the geographic reality for a developer outside US/CA/EU/UK
    assert "Artifact Signing" in text and "USA and Canada" in text
    assert "Pakistan" in text
    # what it does not buy
    assert "does **not** buy" in text
    # and the commands the product actually implements
    for command in (
        "release signing --certificates",
        "release sign-bundle --require-hardware --check",
        "release signing --reputation",
    ):
        assert command in text
    # no angle-bracket placeholder in a copyable command block
    for line in text.splitlines():
        if line.startswith("pc-agent "):
            assert "<" not in line and ">" not in line, line


def test_the_reputation_wording_never_promises_an_ev_bypass() -> None:
    from pathlib import Path as _Path

    source = (
        _Path(__file__).resolve().parents[1] / "src" / "pc_agent" / "windows_signing.py"
    ).read_text(encoding="utf-8")
    assert "trusted immediately" not in source
    doc = (_Path(__file__).resolve().parents[1] / "docs" / "smartscreen_reputation.md").read_text(
        encoding="utf-8"
    )
    assert "Typically trusted immediately" not in doc
    assert "Corrected in 0.82.12" in doc
