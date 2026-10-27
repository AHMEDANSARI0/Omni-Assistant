"""Phase 6 offline licensing: signatures, tokens, trial, gates, CLI/GUI."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.errors import ConfigError, LicenseError
from pc_agent.gui.controller import GuiController, format_license_status
from pc_agent.licensing import (
    ALL_FEATURES,
    FEATURE_DESKTOP,
    FEATURE_DOCUMENTS,
    FEATURE_TOOL_BUILDER,
    FEATURE_WEB,
    LicenseService,
    ed25519,
    issue_token,
    machine_fingerprint,
    verify_token,
)
from pc_agent.licensing.admin import main as admin_main
from pc_agent.licensing.models import b64u_decode, b64u_encode
from pc_agent.storage.db import Database
from pc_agent.storage.repositories import LicenseRepository

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
# Test-only deterministic vendor key. Never used outside this file.
SEED = bytes(range(32))
PUBLIC_KEY = ed25519.public_key_from_seed(SEED)
KEY_ID = "test-vendor"
MACHINE = "a" * 64


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make_settings(tmp_path: Path, **environ: str) -> Settings:
    values = {
        "PC_AGENT_LICENSE_PUBLIC_KEY": b64u_encode(PUBLIC_KEY),
        "PC_AGENT_LICENSE_KEY_ID": KEY_ID,
    }
    values.update(environ)
    return Settings.from_env(tmp_path, environ=values)


def make_service(
    settings: Settings, clock: Clock | None = None, machine: str = MACHINE
) -> LicenseService:
    settings.ensure_directories()
    return LicenseService(
        settings,
        Database(settings.database_path),
        now_fn=clock or Clock(NOW),
        machine_id_fn=lambda: machine,
    )


def make_token(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "key_id": KEY_ID,
        "licensee": "Acme Ltd",
        "plan": "pro",
        "expires_at": NOW + timedelta(days=365),
        "issued_at": NOW,
    }
    kwargs.update(overrides)
    return issue_token(SEED, **kwargs)  # type: ignore[arg-type]


def tamper(token: str, **changes: object) -> str:
    head, payload_part, signature_part = token.split(".")
    data = json.loads(b64u_decode(payload_part))
    data.update(changes)
    canonical = json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"{head}.{b64u_encode(canonical)}.{signature_part}"


# ----------------------------------------------------------------------
# Ed25519 primitives
# ----------------------------------------------------------------------
def test_ed25519_rfc8032_test_vectors() -> None:
    seed1 = bytes.fromhex(
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
    )
    public1 = bytes.fromhex(
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    )
    signature1 = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    assert ed25519.public_key_from_seed(seed1) == public1
    assert ed25519.sign(seed1, b"") == signature1
    assert ed25519.verify(public1, b"", signature1)
    assert not ed25519.verify(public1, b"x", signature1)

    seed2 = bytes.fromhex(
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"
    )
    public2 = bytes.fromhex(
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"
    )
    signature2 = bytes.fromhex(
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"
    )
    assert ed25519.public_key_from_seed(seed2) == public2
    assert ed25519.sign(seed2, bytes([0x72])) == signature2
    assert ed25519.verify(public2, bytes([0x72]), signature2)


def test_ed25519_rejects_malleable_or_wrong_signatures() -> None:
    message = b"license"
    signature = ed25519.sign(SEED, message)
    flipped = bytes([signature[0] ^ 1]) + signature[1:]
    assert not ed25519.verify(PUBLIC_KEY, message, flipped)
    other_key = ed25519.public_key_from_seed(bytes([9]) * 32)
    assert not ed25519.verify(other_key, message, signature)
    # S must be below the group order L (signature malleability guard).
    group_order = 2**252 + 27742317777372353535851937790883648493
    forged = signature[:32] + group_order.to_bytes(32, "little")
    assert not ed25519.verify(PUBLIC_KEY, message, forged)
    assert not ed25519.verify(PUBLIC_KEY, message, signature[:63])
    assert not ed25519.verify(b"\x00" * 31, message, signature)


# ----------------------------------------------------------------------
# Token issuing / verification
# ----------------------------------------------------------------------
def test_issue_verify_round_trip_and_plan_features() -> None:
    verification = verify_token(make_token(), {KEY_ID: PUBLIC_KEY}, now=NOW)
    assert verification.valid and verification.code == "ok"
    assert verification.payload is not None
    assert verification.payload.features == ALL_FEATURES

    standard = verify_token(
        make_token(plan="standard"), {KEY_ID: PUBLIC_KEY}, now=NOW
    )
    assert standard.payload is not None
    assert FEATURE_TOOL_BUILDER not in standard.payload.features
    assert FEATURE_DOCUMENTS in standard.payload.features

    upgraded = verify_token(
        make_token(plan="standard", extra_features=(FEATURE_TOOL_BUILDER,)),
        {KEY_ID: PUBLIC_KEY},
        now=NOW,
    )
    assert upgraded.payload is not None
    assert FEATURE_TOOL_BUILDER in upgraded.payload.features


def test_verify_rejects_tampering_unknown_key_and_garbage() -> None:
    token = make_token(plan="standard")
    forged = tamper(token, plan="pro")
    result = verify_token(forged, {KEY_ID: PUBLIC_KEY}, now=NOW)
    assert not result.valid and result.code == "bad_signature"

    wrong_product = tamper(token, product="some-other-product")
    result = verify_token(wrong_product, {KEY_ID: PUBLIC_KEY}, now=NOW)
    assert not result.valid and result.code == "wrong_product"

    result = verify_token(token, {}, now=NOW)
    assert not result.valid and result.code == "unknown_key"

    assert verify_token("not-a-license", {KEY_ID: PUBLIC_KEY}, now=NOW).code == "malformed"
    assert verify_token("PCA1.@@@.@@@", {KEY_ID: PUBLIC_KEY}, now=NOW).code == "malformed"


def test_verify_expiry_future_issue_and_machine_binding() -> None:
    expired = make_token(expires_at=NOW - timedelta(seconds=1), issued_at=NOW - timedelta(days=2))
    assert verify_token(expired, {KEY_ID: PUBLIC_KEY}, now=NOW).code == "expired"

    future = make_token(issued_at=NOW + timedelta(hours=1))
    assert verify_token(future, {KEY_ID: PUBLIC_KEY}, now=NOW).code == "not_yet_valid"

    bound = make_token(machine_id=MACHINE)
    assert verify_token(bound, {KEY_ID: PUBLIC_KEY}, now=NOW, machine_id=MACHINE).valid
    assert (
        verify_token(bound, {KEY_ID: PUBLIC_KEY}, now=NOW, machine_id="b" * 64).code
        == "machine_mismatch"
    )
    assert verify_token(bound, {KEY_ID: PUBLIC_KEY}, now=NOW).code == "machine_mismatch"


def test_issue_rejects_invalid_plan_feature_term_and_seats() -> None:
    with pytest.raises(LicenseError):
        make_token(plan="gold")
    with pytest.raises(LicenseError):
        make_token(extra_features=("cloud.sync",))
    with pytest.raises(LicenseError):
        make_token(expires_at=NOW)  # not after issued_at
    with pytest.raises(LicenseError):
        make_token(seats=0)


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
def test_config_license_defaults_and_validation(tmp_path: Path) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    assert settings.license_enforcement == "off"
    assert settings.trial_days == 14
    assert settings.license_file == settings.data_dir / "license.key"
    assert settings.public_summary()["license_public_key_configured"] == "no"

    configured = make_settings(tmp_path)
    assert configured.public_summary()["license_public_key_configured"] == "yes"
    assert b64u_encode(PUBLIC_KEY) not in json.dumps(configured.public_summary())

    for environ in (
        {"PC_AGENT_LICENSE_ENFORCEMENT": "strict"},
        {"PC_AGENT_LICENSE_PUBLIC_KEY": "not!base64!"},
        {"PC_AGENT_LICENSE_PUBLIC_KEY": b64u_encode(b"short")},
        {"PC_AGENT_LICENSE_KEY_ID": "bad key id"},
        {"PC_AGENT_TRIAL_DAYS": "91"},
        {"PC_AGENT_TRIAL_DAYS": "soon"},
    ):
        with pytest.raises(ConfigError):
            Settings.from_env(tmp_path, environ=environ)


# ----------------------------------------------------------------------
# LicenseService behaviour
# ----------------------------------------------------------------------
def test_service_default_is_unconfigured_and_never_blocks(tmp_path: Path) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    service = make_service(settings)

    status = service.status()
    assert status.state == "unconfigured"
    assert status.enforcement == "off"
    assert service.require(FEATURE_WEB).state == "unconfigured"
    repository = LicenseRepository(Database(settings.database_path))
    assert repository.get_state("trial_started_at") is None


def test_service_trial_lifecycle_and_enforced_gate(tmp_path: Path) -> None:
    clock = Clock(NOW)
    settings = make_settings(tmp_path, PC_AGENT_LICENSE_ENFORCEMENT="enforce")
    service = make_service(settings, clock)

    status = service.status()
    assert status.state == "trial"
    assert status.days_remaining == 14
    assert FEATURE_TOOL_BUILDER in status.features
    assert service.require(FEATURE_DOCUMENTS).state == "trial"

    clock.now = NOW + timedelta(days=15)
    expired = service.status()
    assert expired.state == "trial_expired"
    with pytest.raises(LicenseError, match="trial_expired"):
        service.require(FEATURE_DOCUMENTS)


def test_service_trial_disabled_and_warn_mode(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        PC_AGENT_LICENSE_ENFORCEMENT="warn",
        PC_AGENT_TRIAL_DAYS="0",
    )
    service = make_service(settings)
    warnings: list[str] = []
    status = service.require(FEATURE_WEB, warn=warnings.append)
    assert status.state == "unlicensed"
    assert warnings and "enforcement=warn" in warnings[0]

    enforced = make_service(
        make_settings(
            tmp_path,
            PC_AGENT_LICENSE_ENFORCEMENT="enforce",
            PC_AGENT_TRIAL_DAYS="0",
            PC_AGENT_DATABASE_PATH="data/other.db",
        )
    )
    with pytest.raises(LicenseError, match="unlicensed"):
        enforced.require(FEATURE_WEB)


def test_service_activate_deactivate_and_audit_events(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        PC_AGENT_LICENSE_ENFORCEMENT="enforce",
        PC_AGENT_TRIAL_DAYS="0",
    )
    service = make_service(settings)
    token = make_token()

    status = service.activate(token)
    assert status.state == "licensed"
    assert status.plan == "pro"
    assert settings.license_file.read_text(encoding="utf-8").strip() == token

    repository = LicenseRepository(Database(settings.database_path))
    events = repository.list_events()
    assert events[0].event_type == "license_activated"
    assert token not in json.dumps(events[0].detail)

    deactivated = service.deactivate()
    assert deactivated.state == "unlicensed"
    assert not settings.license_file.exists()
    assert repository.list_events()[0].event_type == "license_deactivated"


def test_service_invalid_activation_keeps_existing_license(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, PC_AGENT_LICENSE_ENFORCEMENT="enforce")
    service = make_service(settings)
    token = make_token()
    service.activate(token)

    with pytest.raises(LicenseError, match="was not activated"):
        service.activate(tamper(token, licensee="Mallory"))
    assert settings.license_file.read_text(encoding="utf-8").strip() == token

    expired = make_token(
        expires_at=NOW - timedelta(days=1), issued_at=NOW - timedelta(days=30)
    )
    with pytest.raises(LicenseError, match="term has ended"):
        service.activate(expired)
    assert service.status().state == "licensed"


def test_service_expired_license_file_and_missing_public_key(tmp_path: Path) -> None:
    expired = make_token(
        expires_at=NOW - timedelta(days=1), issued_at=NOW - timedelta(days=30)
    )
    settings = make_settings(tmp_path, PC_AGENT_LICENSE_ENFORCEMENT="enforce")
    settings.ensure_directories()
    settings.license_file.write_text(expired + "\n", encoding="utf-8")
    service = make_service(settings)
    status = service.status()
    assert status.state == "expired"
    assert status.features == ()

    unconfigured = Settings.from_env(tmp_path, environ={})
    assert unconfigured.license_file == settings.license_file
    blind = make_service(unconfigured)
    blind_status = blind.status()
    assert blind_status.state == "invalid"
    assert blind_status.configured is False
    # Enforcement is off by default, so even this state never blocks.
    assert blind.require(FEATURE_DESKTOP).state == "invalid"


def test_service_clock_rollback_fails_closed(tmp_path: Path) -> None:
    clock = Clock(NOW)
    settings = make_settings(tmp_path, PC_AGENT_LICENSE_ENFORCEMENT="enforce")
    service = make_service(settings, clock)
    assert service.status().state == "trial"

    clock.now = NOW - timedelta(days=3)
    rolled_back = service.status()
    assert rolled_back.state == "invalid"
    assert rolled_back.clock_rollback is True
    with pytest.raises(LicenseError):
        service.require(FEATURE_DOCUMENTS)


def test_license_repository_round_trip(tmp_path: Path) -> None:
    database = Database(tmp_path / "license.db")
    assert database.initialize() == 6
    repository = LicenseRepository(database)
    repository.set_state("example", "one")
    repository.set_state("example", "two")
    assert repository.get_state("example") == "two"
    repository.record_event("first", license_id="abc", detail={"n": 1})
    repository.record_event("second")
    events = repository.list_events()
    assert [event.event_type for event in events] == ["second", "first"]
    assert events[1].detail == {"n": 1}


def test_machine_fingerprint_is_stable_hash() -> None:
    first = machine_fingerprint()
    assert first == machine_fingerprint()
    assert len(first) == 64
    assert all(char in "0123456789abcdef" for char in first)


# ----------------------------------------------------------------------
# Vendor admin tool + client round trip
# ----------------------------------------------------------------------
def test_admin_keygen_issue_and_client_activation(tmp_path: Path) -> None:
    vendor_dir = tmp_path / "vendor"
    assert (
        admin_main(["keygen", "--key-id", "acme-1", "--out-dir", str(vendor_dir)]) == 0
    )
    assert (
        admin_main(["keygen", "--key-id", "acme-1", "--out-dir", str(vendor_dir)]) == 1
    )

    license_path = tmp_path / "customer.license"
    assert (
        admin_main(
            [
                "issue",
                "--private-key-file",
                str(vendor_dir / "license_private_key.txt"),
                "--key-id",
                "acme-1",
                "--licensee",
                "Customer One",
                "--plan",
                "standard",
                "--days",
                "365",
                "--out",
                str(license_path),
            ]
        )
        == 0
    )

    public_values = dict(
        line.split("=", 1)
        for line in (vendor_dir / "license_public_key.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if "=" in line
    )
    settings = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_LICENSE_PUBLIC_KEY": public_values[
                "PC_AGENT_LICENSE_PUBLIC_KEY"
            ],
            "PC_AGENT_LICENSE_KEY_ID": public_values["PC_AGENT_LICENSE_KEY_ID"],
            "PC_AGENT_LICENSE_ENFORCEMENT": "enforce",
            "PC_AGENT_TRIAL_DAYS": "0",
        },
    )
    service = LicenseService(settings, Database(settings.database_path))
    status = service.activate_file(license_path)
    assert status.state == "licensed"
    assert status.licensee == "Customer One"
    with pytest.raises(LicenseError):
        service.require(FEATURE_TOOL_BUILDER)


# ----------------------------------------------------------------------
# CLI and GUI integration
# ----------------------------------------------------------------------
def _write_env(tmp_path: Path, **values: str) -> None:
    lines = [f"{key}={value}" for key, value in values.items()]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_cli_license_status_machine_id_and_doctor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--root", str(tmp_path), "license", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "unconfigured"
    assert payload["enforcement"] == "off"
    assert len(payload["machine_id"]) == 64

    assert main(["--root", str(tmp_path), "license", "machine-id"]) == 0
    assert "Machine ID:" in capsys.readouterr().out

    assert main(["--root", str(tmp_path), "doctor"]) == 0
    doctor = capsys.readouterr().out
    assert "License: state=unconfigured, enforcement=off" in doctor


def test_cli_activate_and_enforced_feature_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_env(
        tmp_path,
        PC_AGENT_LICENSE_PUBLIC_KEY=b64u_encode(PUBLIC_KEY),
        PC_AGENT_LICENSE_KEY_ID=KEY_ID,
        PC_AGENT_LICENSE_ENFORCEMENT="enforce",
        PC_AGENT_TRIAL_DAYS="0",
    )
    assert main(["--root", str(tmp_path), "desktop", "info"]) == 1
    blocked = capsys.readouterr()
    assert "not available" in blocked.err

    license_path = tmp_path / "issued.license"
    license_path.write_text(make_token() + "\n", encoding="utf-8")
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "license",
                "activate",
                "--file",
                str(license_path),
            ]
        )
        == 0
    )
    activated = capsys.readouterr().out
    assert "License activated." in activated
    assert "License state: licensed" in activated

    assert main(["--root", str(tmp_path), "desktop", "info"]) == 0
    capsys.readouterr()

    assert main(["--root", str(tmp_path), "license", "deactivate", "--yes"]) == 0
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "desktop", "info"]) == 1
    capsys.readouterr()


def test_gui_controller_license_flow_and_gate(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        PC_AGENT_LICENSE_ENFORCEMENT="enforce",
        PC_AGENT_TRIAL_DAYS="0",
    )
    controller = GuiController(settings, desktop_opener=lambda _target: None)

    assert controller.license_status().state == "unlicensed"
    with pytest.raises(LicenseError):
        controller.desktop_info()

    license_path = tmp_path / "gui.license"
    license_path.write_text(make_token() + "\n", encoding="utf-8")
    status = controller.activate_license_file(str(license_path))
    assert status.state == "licensed"
    formatted = format_license_status(status, machine_id=controller.machine_id())
    assert "Plan: pro" in formatted
    assert controller.desktop_info().success

    assert controller.doctor_summary().license_summary.startswith("state=licensed")
    assert controller.deactivate_license().state == "unlicensed"
    with pytest.raises(LicenseError):
        controller.desktop_info()
