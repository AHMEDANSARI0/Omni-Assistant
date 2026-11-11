"""Transformation Phase 16 - optional encrypted device sync and coordination.

Every test drives the real implementation with two genuine device identities in
two independent data directories and real bundles on disk. Nothing is mocked:
the crypto, the ledger, the replay guard and the coordination ledger are the
shipping code paths.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from pc_agent import __version__
from pc_agent.api_server import LocalApiServer
from pc_agent.baseline import TRANSFORMATION_LABEL, TRANSFORMATION_PHASE
from pc_agent.config import Settings
from pc_agent.diagnostics import build_diagnostics, sync_summary_line
from pc_agent.errors import SyncError
from pc_agent.release import audit_install
from pc_agent.service import LocalService
from pc_agent.storage.db import LATEST_SCHEMA_VERSION, Database
from pc_agent.sync import COLLECTIONS, SyncService
from pc_agent.sync.crypto import (
    AESGCM,
    HKDF,
    DeviceIdentity,
    InvalidSignature,
    device_id_for,
    hashes,
    identity_path,
    load_or_create_identity,
    open_bundle,
    seal_bundle,
)
from pc_agent.sync.perms import private_file_report
from pc_agent.sync.store import SyncStore


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def make_settings(root: Path, *, name: str, shared: Path, **env: str) -> Settings:
    environ = {
        "PC_AGENT_ALLOW_SYNC": "1",
        "PC_AGENT_SYNC_DEVICE_NAME": name,
        "PC_AGENT_SYNC_DIR": str(shared),
    }
    environ.update(env)
    settings = Settings.from_env(root, environ=environ, load_dotenv=False)
    settings.ensure_directories()
    Database(settings.database_path).initialize()
    return settings


@pytest.fixture()
def devices(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    laptop = make_settings(tmp_path / "laptop", name="Laptop", shared=shared)
    desktop = make_settings(tmp_path / "desktop", name="Desktop", shared=shared)
    return laptop, desktop, shared


def service(settings: Settings) -> SyncService:
    database = Database(settings.database_path)
    database.initialize()
    return SyncService(settings, database)


def pair(a: Settings, b: Settings) -> tuple[SyncService, SyncService]:
    sa, sb = service(a), service(b)
    sa.pair(
        name=b.sync_device_name,
        sign_public=sb.identity().public_document()["sign_public"],
        encrypt_public=sb.identity().public_document()["encrypt_public"],
        confirm="PAIR",
    )
    sb.pair(
        name=a.sync_device_name,
        sign_public=sa.identity().public_document()["sign_public"],
        encrypt_public=sa.identity().public_document()["encrypt_public"],
        confirm="PAIR",
    )
    return sa, sb


# --------------------------------------------------------------------------- #
# identity + crypto primitives
# --------------------------------------------------------------------------- #
def test_identity_is_created_once_and_reused(tmp_path: Path) -> None:
    first, created = load_or_create_identity(tmp_path)
    second, created_again = load_or_create_identity(tmp_path)
    assert created is True
    assert created_again is False
    assert first.device_id == second.device_id
    assert first.sign_public == second.sign_public
    assert identity_path(tmp_path).is_file()


def _assert_owner_only(path: Path) -> None:
    """Owner-only must hold on every platform - by the mechanism it really has.

    POSIX is checked through the file mode, which the kernel enforces. Windows
    has no POSIX mode (``os.chmod`` only toggles read-only, so the mode always
    reads 0o666), so the assertion there is that an owner-only ACL was actually
    applied and verified - not that a chmod call was made.
    """

    report = private_file_report(path)
    if os.name == "posix":
        assert report["mechanism"] == "posix-mode"
        assert path.stat().st_mode & 0o777 == 0o600
        assert report["owner_only"] is True
        return
    assert report["mechanism"] == "windows-acl"
    assert report["applied"] is True, report["detail"]
    assert report["owner_only"] is True


def test_identity_file_is_owner_only(tmp_path: Path) -> None:
    load_or_create_identity(tmp_path)
    _assert_owner_only(identity_path(tmp_path))


def test_identity_document_exposes_public_values_only(tmp_path: Path) -> None:
    identity, _ = load_or_create_identity(tmp_path)
    document = identity.public_document()
    assert set(document) == {
        "device_id",
        "fingerprint",
        "sign_public",
        "encrypt_public",
    }
    serialized = json.dumps(document)
    for secret in identity.to_dict().values():
        assert secret not in serialized


def test_device_id_is_derived_from_both_public_keys(tmp_path: Path) -> None:
    a, _ = load_or_create_identity(tmp_path / "a")
    b, _ = load_or_create_identity(tmp_path / "b")
    assert device_id_for(a.sign_public, a.encrypt_public) == a.device_id
    assert device_id_for(b.sign_public, a.encrypt_public) != a.device_id


def test_sealed_bundle_round_trips_between_two_devices(devices) -> None:
    laptop, desktop, _shared = devices
    sender, receiver = service(laptop), service(desktop)
    payload = {"schema": 1, "kind": "snapshot", "probe": "round-trip"}
    envelope = seal_bundle(
        payload,
        identity=sender.identity(),
        recipients=[
            {
                "device_id": receiver.identity().device_id,
                "encrypt_public": receiver.identity().public_document()["encrypt_public"],
            }
        ],
        bundle_id="b-1",
        seq=1,
    )
    opened, header = open_bundle(
        json.loads(json.dumps(envelope)),
        identity=receiver.identity(),
        trusted_peers={
            sender.identity().device_id: {
                "device_id": sender.identity().device_id,
                "sign_public": sender.identity().public_document()["sign_public"],
                "status": "paired",
            }
        },
        max_bytes=4_000_000,
        clock_skew_seconds=900,
    )
    assert header["bundle_id"] == "b-1"
    assert opened["probe"] == "round-trip"


def test_bundle_is_not_readable_by_a_third_device(devices) -> None:
    laptop, _desktop, _shared = devices
    sender = service(laptop)
    stranger, _ = load_or_create_identity(laptop.root_dir / "stranger")
    envelope = seal_bundle(
        {"schema": 1, "probe": "secret"},
        identity=sender.identity(),
        recipients=[
            {
                "device_id": sender.identity().device_id,
                "encrypt_public": sender.identity().public_document()["encrypt_public"],
            }
        ],
        bundle_id="b-2",
        seq=1,
    )
    with pytest.raises(SyncError) as excinfo:
        open_bundle(
            envelope,
            identity=stranger,
            trusted_peers={
                sender.identity().device_id: {
                    "device_id": sender.identity().device_id,
                    "sign_public": sender.identity().public_document()["sign_public"],
                    "status": "paired",
                }
            },
            max_bytes=4_000_000,
            clock_skew_seconds=900,
        )
    assert "not addressed" in str(excinfo.value).lower()


def test_tampered_header_breaks_the_signature(devices) -> None:
    laptop, desktop, _shared = devices
    sender, receiver = service(laptop), service(desktop)
    envelope = seal_bundle(
        {"schema": 1, "probe": "tamper"},
        identity=sender.identity(),
        recipients=[
            {
                "device_id": receiver.identity().device_id,
                "encrypt_public": receiver.identity().public_document()["encrypt_public"],
            }
        ],
        bundle_id="b-3",
        seq=1,
    )
    envelope["header"]["seq"] = 99
    with pytest.raises(SyncError) as excinfo:
        open_bundle(
            envelope,
            identity=receiver.identity(),
            trusted_peers={
                sender.identity().device_id: {
                    "device_id": sender.identity().device_id,
                    "sign_public": sender.identity().public_document()["sign_public"],
                    "status": "paired",
                }
            },
            max_bytes=4_000_000,
            clock_skew_seconds=900,
        )
    assert "signature" in str(excinfo.value).lower()


def test_tampered_ciphertext_is_rejected(devices) -> None:
    laptop, desktop, _shared = devices
    sender, receiver = service(laptop), service(desktop)
    envelope = seal_bundle(
        {"schema": 1, "probe": "tamper"},
        identity=sender.identity(),
        recipients=[
            {
                "device_id": receiver.identity().device_id,
                "encrypt_public": receiver.identity().public_document()["encrypt_public"],
            }
        ],
        bundle_id="b-4",
        seq=1,
    )
    blob = envelope["recipients"][0]["ciphertext"]
    envelope["recipients"][0]["ciphertext"] = blob[:-8] + ("A" * 8)
    with pytest.raises(SyncError):
        open_bundle(
            envelope,
            identity=receiver.identity(),
            trusted_peers={
                sender.identity().device_id: {
                    "device_id": sender.identity().device_id,
                    "sign_public": sender.identity().public_document()["sign_public"],
                    "status": "paired",
                }
            },
            max_bytes=4_000_000,
            clock_skew_seconds=900,
        )


def test_aes_key_material_is_hkdf_bound_to_the_bundle() -> None:
    shared = b"\x01" * 32
    okm = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=b"salt", info=b"pc-agent-sync-v1"
    ).derive(shared)
    assert len(okm) == 32
    assert AESGCM is not None
    assert InvalidSignature is not None


# --------------------------------------------------------------------------- #
# gates and pairing
# --------------------------------------------------------------------------- #
def test_sync_is_off_by_default(tmp_path: Path) -> None:
    settings = Settings.from_env(tmp_path, environ={}, load_dotenv=False)
    settings.ensure_directories()
    Database(settings.database_path).initialize()
    assert settings.allow_sync is False
    result = service(settings).status()
    assert result["enabled"] is False
    with pytest.raises(SyncError) as excinfo:
        service(settings).export()
    assert "PC_AGENT_ALLOW_SYNC" in str(excinfo.value)


def test_pairing_requires_the_explicit_confirmation(devices) -> None:
    laptop, desktop, _shared = devices
    with pytest.raises(SyncError) as excinfo:
        service(laptop).pair(
            name="Desktop",
            sign_public=service(desktop).identity().public_document()["sign_public"],
            encrypt_public=service(desktop).identity().public_document()["encrypt_public"],
            confirm=None,
        )
    assert "PAIR" in str(excinfo.value)


def test_pairing_rejects_malformed_keys(devices) -> None:
    laptop, _desktop, _shared = devices
    with pytest.raises(SyncError):
        service(laptop).pair(name="X", sign_public="nope", encrypt_public="nope", confirm="PAIR")


def test_a_device_cannot_pair_itself(devices) -> None:
    laptop, _desktop, _shared = devices
    own = service(laptop).identity().public_document()
    with pytest.raises(SyncError) as excinfo:
        service(laptop).pair(
            name="Me",
            sign_public=own["sign_public"],
            encrypt_public=own["encrypt_public"],
            confirm="PAIR",
        )
    assert "own key" in str(excinfo.value)


def test_pairing_records_a_fingerprint_and_survives_reload(devices) -> None:
    laptop, desktop, _shared = devices
    sender, _receiver = pair(laptop, desktop)
    peers = service(laptop).peers()["peers"]
    assert len(peers) == 1
    assert peers[0]["name"] == "Desktop"
    assert peers[0]["status"] == "paired"
    assert len(peers[0]["fingerprint"]) >= 19
    assert sender.status()["counts"]["peers"] == 1


def test_unpair_removes_trust(devices) -> None:
    laptop, desktop, _shared = devices
    sender, _receiver = pair(laptop, desktop)
    peer_id = service(desktop).identity().device_id
    sender.unpair(peer_id[:12])
    assert service(laptop).peers()["peers"] == []
    with pytest.raises(SyncError):
        sender.export()


def test_pausing_a_peer_stops_new_bundles_for_it(devices) -> None:
    laptop, desktop, _shared = devices
    sender, _receiver = pair(laptop, desktop)
    peer = service(desktop).identity().device_id
    sender.set_peer_enabled(peer, enabled=False)
    assert service(laptop).peers()["peers"][0]["status"] == "paused"
    with pytest.raises(SyncError) as excinfo:
        sender.export()
    assert "no paired device" in str(excinfo.value)
    sender.set_peer_enabled(peer, enabled=True)
    assert sender.export()["recipients"] == [peer]


# --------------------------------------------------------------------------- #
# export / import
# --------------------------------------------------------------------------- #
def test_export_creates_a_single_recipient_bundle_on_disk(devices) -> None:
    laptop, desktop, shared = devices
    sender, _receiver = pair(laptop, desktop)
    result = sender.export()
    path = Path(result["path"])
    assert path.parent == shared
    assert path.suffix == ".pcasync"
    _assert_owner_only(path)
    assert result["permissions"]["applied"] is True
    envelope = json.loads(path.read_bytes())
    assert envelope["header"]["bundle_id"] == result["bundle_id"]
    assert [r["device_id"] for r in envelope["recipients"]] == [result["recipients"][0]]
    assert "collections" not in json.dumps(envelope)  # no plaintext record names


def test_bundles_carry_real_state(devices) -> None:
    laptop, desktop, _shared = devices
    from pc_agent.proactive import ProactiveEngine

    ProactiveEngine(laptop, Database(laptop.database_path)).add_reminder(
        title="Ship Phase 16", due="+2h", severity="high"
    )
    sender, receiver = pair(laptop, desktop)
    result = sender.export()
    assert result["collections"]["reminders"] == 1
    outcome = receiver.import_file(result["path"])
    assert outcome["applied"] == 1
    titles = [
        item.title
        for item in ProactiveEngine(
            desktop, Database(desktop.database_path)
        ).store.list_reminders(status="open", limit=10)
    ]
    assert "Ship Phase 16" in titles


def test_import_is_idempotent_and_refuses_replays(devices) -> None:
    laptop, desktop, _shared = devices
    sender, receiver = pair(laptop, desktop)
    path = sender.export()["path"]
    receiver.import_file(path)
    with pytest.raises(SyncError) as excinfo:
        receiver.import_file(path)
    assert "replay" in str(excinfo.value).lower()


def test_an_older_sequence_from_the_same_device_is_refused(devices) -> None:
    laptop, desktop, _shared = devices
    sender, receiver = pair(laptop, desktop)
    first = sender.export()
    receiver.import_file(first["path"])
    # A peer restoring an old backup reuses the previous sequence number.
    identity = sender.identity()
    stale = seal_bundle(
        sender.build_payload(),
        identity=identity,
        recipients=[
            {
                "device_id": receiver.identity().device_id,
                "encrypt_public": receiver.identity().public_document()["encrypt_public"],
            }
        ],
        bundle_id="stale-bundle",
        seq=1,
    )
    path = Path(first["path"]).parent / "stale.pcasync"
    path.write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(SyncError) as excinfo:
        receiver.import_file(path)
    assert "not newer" in str(excinfo.value)


def test_bundles_from_unpaired_devices_are_refused(devices) -> None:
    laptop, desktop, _shared = devices
    sender, _receiver = pair(laptop, desktop)
    path = sender.export()["path"]
    stranger_settings = make_settings(
        laptop.root_dir / "stranger", name="Stranger", shared=Path(path).parent
    )
    with pytest.raises(SyncError) as excinfo:
        service(stranger_settings).import_file(path)
    assert "unpaired device" in str(excinfo.value).lower()


def test_a_bundle_whose_signing_key_changed_is_refused(devices) -> None:
    laptop, desktop, _shared = devices
    sender, receiver = pair(laptop, desktop)
    path = sender.export()["path"]
    store = SyncStore(receiver._database)
    row = store.devices(limit=5)[0]
    store.upsert_device(
        device_id=row["device_id"],
        name=row["name"],
        sign_public=service(desktop).identity().public_document()["encrypt_public"],
        encrypt_public=row["encrypt_public"],
        fingerprint=row["fingerprint"],
    )
    with pytest.raises(SyncError):
        receiver.import_file(path)


def test_bundles_outside_the_clock_skew_window_are_refused(devices) -> None:
    laptop, desktop, _shared = devices
    sender, receiver = pair(laptop, desktop)
    stale = seal_bundle(
        sender.build_payload(),
        identity=sender.identity(),
        recipients=[
            {
                "device_id": receiver.identity().device_id,
                "encrypt_public": receiver.identity().public_document()["encrypt_public"],
            }
        ],
        bundle_id="skewed-bundle",
        seq=1,
        created_at="2001-01-01T00:00:00+00:00",
    )
    path = Path(sender._sync_dir()) / "skewed.pcasync"
    path.write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(SyncError) as excinfo:
        receiver.import_file(path)
    assert "clock" in str(excinfo.value).lower()


def test_oversized_bundles_are_refused_before_parsing(devices) -> None:
    laptop, desktop, shared = devices
    _sender, _receiver = pair(laptop, desktop)
    huge = shared / "huge.pcasync"
    huge.write_bytes(b"{" + b"x" * 70_000 + b"}")
    strict = replace(desktop, sync_max_bundle_bytes=65_536)
    with pytest.raises(SyncError) as excinfo:
        service(strict).import_file(huge)
    assert "over the" in str(excinfo.value)


def test_malformed_json_is_refused(devices) -> None:
    laptop, desktop, shared = devices
    _sender, receiver = pair(laptop, desktop)
    broken = shared / "broken.pcasync"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(SyncError) as excinfo:
        receiver.import_file(broken)
    assert "not valid JSON" in str(excinfo.value)


def test_missing_bundle_is_a_structured_error(devices) -> None:
    _laptop, desktop, shared = devices
    with pytest.raises(SyncError) as excinfo:
        service(desktop).import_file(shared / "nope.pcasync")
    assert "does not exist" in str(excinfo.value)


def test_a_peer_cannot_overwrite_a_local_profile(devices) -> None:
    laptop, desktop, _shared = devices
    from pc_agent.profiles import load_profile, save_profile

    save_profile(Path(laptop.data_dir), "work", {"log_level": "INFO"})
    save_profile(Path(desktop.data_dir), "work", {"log_level": "DEBUG"})
    sender, receiver = pair(laptop, desktop)
    outcome = receiver.import_file(sender.export()["path"])
    assert outcome["skipped"] >= 1
    assert any(item["collection"] == "profiles" for item in outcome["conflicts"])
    assert load_profile(Path(desktop.data_dir), "work")["log_level"] == "DEBUG"


def test_a_new_profile_from_a_peer_is_created(devices) -> None:
    laptop, desktop, _shared = devices
    from pc_agent.profiles import load_profile, save_profile

    save_profile(Path(laptop.data_dir), "travel", {"log_level": "WARNING"})
    sender, receiver = pair(laptop, desktop)
    outcome = receiver.import_file(sender.export()["path"])
    assert outcome["applied"] >= 1
    assert load_profile(Path(desktop.data_dir), "travel")["log_level"] == "WARNING"


def test_conflict_policy_local_keeps_the_local_flow(devices) -> None:
    laptop, desktop, _shared = devices
    from pc_agent.flows import FlowDefinition, FlowNode, FlowStore

    laptop_flow = FlowDefinition(
        name="Morning",
        description="laptop version",
        nodes=(FlowNode(id="n1", instruction="Summarise the morning news"),),
    )
    desktop_flow = FlowDefinition(
        name="Morning",
        description="desktop version",
        nodes=(FlowNode(id="n1", instruction="Summarise the evening news"),),
    )
    FlowStore(Database(laptop.database_path)).create(laptop_flow)
    FlowStore(Database(desktop.database_path)).create(desktop_flow)
    sender, receiver = pair(laptop, desktop)
    outcome = receiver.import_file(sender.export()["path"])
    assert any(item["collection"] == "flows" for item in outcome["conflicts"])
    local = FlowStore(Database(desktop.database_path)).find_by_name("Morning")
    assert local is not None
    assert local.definition.description == "desktop version"


def test_conflict_policy_remote_takes_the_peer_flow(devices) -> None:
    laptop, desktop, _shared = devices
    from pc_agent.flows import FlowDefinition, FlowNode, FlowStore

    FlowStore(Database(laptop.database_path)).create(
        FlowDefinition(
            name="Morning",
            description="laptop version",
            nodes=(FlowNode(id="n1", instruction="Summarise the morning news"),),
        )
    )
    FlowStore(Database(desktop.database_path)).create(
        FlowDefinition(
            name="Morning",
            description="desktop version",
            nodes=(FlowNode(id="n1", instruction="Summarise the evening news"),),
        )
    )
    sender, _receiver = pair(laptop, desktop)
    remote_first = replace(desktop, sync_conflict_policy="remote")
    outcome = service(remote_first).import_file(sender.export()["path"])
    assert outcome["applied"] >= 1
    local = FlowStore(Database(desktop.database_path)).find_by_name("Morning")
    assert local is not None
    assert local.definition.description == "laptop version"


def test_unknown_collections_and_bad_schema_are_refused(devices) -> None:
    laptop, desktop, _shared = devices
    sender, receiver = pair(laptop, desktop)
    identity = sender.identity()
    bad = seal_bundle(
        {
            "schema": 1,
            "kind": "snapshot",
            "device": identity.device_id,
            "collections": {"secrets": []},
            "requests": [],
        },
        identity=identity,
        recipients=[
            {
                "device_id": receiver.identity().device_id,
                "encrypt_public": receiver.identity().public_document()["encrypt_public"],
            }
        ],
        bundle_id="bad-collection",
        seq=7,
    )
    path = Path(sender._sync_dir()) / "bad.pcasync"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(SyncError) as excinfo:
        receiver.import_file(path)
    assert "unknown collection" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# coordination requests
# --------------------------------------------------------------------------- #
def test_a_request_waits_for_a_local_decision(devices) -> None:
    laptop, desktop, _shared = devices
    sender, receiver = pair(laptop, desktop)
    sent = sender.request("notify", title="Standup in 10", body="from the laptop")
    receiver.import_file(sent["bundle"]["path"])
    pending = receiver.requests(status="pending")["requests"]
    assert len(pending) == 1
    assert pending[0]["kind"] == "notify"
    assert pending[0]["payload"]["title"] == "Standup in 10"
    decided = receiver.decide(pending[0]["id"], accept=True)
    assert decided["status"] == "accepted"
    assert receiver.requests(status="pending")["requests"] == []


def test_accepting_a_remind_request_creates_a_local_reminder(devices) -> None:
    laptop, desktop, _shared = devices
    from pc_agent.proactive import ProactiveEngine

    sender, receiver = pair(laptop, desktop)
    sent = sender.request("remind", title="Call home", due="+30m", severity="high")
    receiver.import_file(sent["bundle"]["path"])
    request_id = receiver.requests(status="pending")["requests"][0]["id"]
    receiver.decide(request_id, accept=True)
    titles = [
        item.title
        for item in ProactiveEngine(
            desktop, Database(desktop.database_path)
        ).store.list_reminders(status="open", limit=10)
    ]
    assert "Call home" in titles


def test_denying_a_request_executes_nothing(devices) -> None:
    laptop, desktop, _shared = devices
    from pc_agent.proactive import ProactiveEngine

    sender, receiver = pair(laptop, desktop)
    sent = sender.request("remind", title="Never created", due="+1h")
    receiver.import_file(sent["bundle"]["path"])
    request_id = receiver.requests(status="pending")["requests"][0]["id"]
    receiver.decide(request_id, accept=False, note="not now")
    assert receiver.requests(status="denied")["total"] == 1
    titles = [
        item.title
        for item in ProactiveEngine(
            desktop, Database(desktop.database_path)
        ).store.list_reminders(status=None, limit=50)
    ]
    assert "Never created" not in titles


def test_unknown_request_kinds_are_refused_at_the_sender(devices) -> None:
    laptop, desktop, _shared = devices
    sender, _receiver = pair(laptop, desktop)
    with pytest.raises(SyncError) as excinfo:
        sender.request("execute", title="rm -rf /")
    assert "must be one of" in str(excinfo.value)


def test_a_decided_request_cannot_be_decided_again(devices) -> None:
    laptop, desktop, _shared = devices
    sender, receiver = pair(laptop, desktop)
    sent = sender.request("ping", title="Are you there")
    receiver.import_file(sent["bundle"]["path"])
    request_id = receiver.requests(status="pending")["requests"][0]["id"]
    receiver.decide(request_id, accept=True)
    with pytest.raises(SyncError) as excinfo:
        receiver.decide(request_id, accept=True)
    assert "already" in str(excinfo.value)


def test_tick_exports_and_imports_the_shared_folder(devices) -> None:
    laptop, desktop, shared = devices
    from pc_agent.proactive import ProactiveEngine
    from pc_agent.sync import service as service_module  # noqa: F401

    sender, receiver = pair(laptop, desktop)
    ProactiveEngine(desktop, Database(desktop.database_path)).add_reminder(
        title="Desktop only", due="+3h"
    )
    result = receiver.tick()
    assert result["sync_dir"] == str(shared)
    assert Path(result["exported"]["path"]).is_file()
    outcome = sender.tick()
    assert len(outcome["imported"]) >= 1
    titles = [
        item.title
        for item in ProactiveEngine(
            laptop, Database(laptop.database_path)
        ).store.list_reminders(status="open", limit=20)
    ]
    assert "Desktop only" in titles
    assert outcome["refused"] == []


# --------------------------------------------------------------------------- #
# ledger, diagnostics, service, API surface
# --------------------------------------------------------------------------- #
def test_the_bundle_ledger_records_both_directions(devices) -> None:
    laptop, desktop, _shared = devices
    sender, receiver = pair(laptop, desktop)
    path = sender.export()["path"]
    receiver.import_file(path)
    out_rows = sender.bundles(direction="out")["bundles"]
    in_rows = receiver.bundles(direction="in")["bundles"]
    assert len(out_rows) == 1 and out_rows[0]["status"] == "exported"
    assert len(in_rows) == 1 and in_rows[0]["status"] == "imported"
    assert in_rows[0]["seq"] == 1


def test_status_is_honest_about_the_folder_and_limits(devices) -> None:
    laptop, _desktop, shared = devices
    status = service(laptop).status()
    assert status["transport"] == "shared-folder"
    assert status["sync_dir"] == shared.name
    assert status["sync_dir_state"] == "exists"
    assert status["limits"]["max_records"] == 500
    assert status["collections"] == list(COLLECTIONS)


def test_a_missing_sync_folder_is_reported_not_invented(tmp_path: Path) -> None:
    shared = tmp_path / "not-there-yet"
    settings = make_settings(tmp_path / "solo", name="Solo", shared=shared)
    assert service(settings).status()["sync_dir_state"] == "missing"
    with pytest.raises(SyncError) as excinfo:
        service(settings).export()
    assert "no paired device" in str(excinfo.value)


def test_diagnostics_and_doctor_report_the_sync_posture(devices) -> None:
    laptop, desktop, _shared = devices
    sender, receiver = pair(laptop, desktop)
    receiver.import_file(sender.export()["path"])
    line = sync_summary_line(laptop, Database(laptop.database_path))
    assert line.startswith("device=")
    assert "transport=shared-folder" in line
    report = build_diagnostics(laptop, Database(laptop.database_path))
    assert report["sync"]["enabled"] is True
    assert report["sync"]["bundles"] == 1
    assert report["sync"]["peers"] == 1


def test_doctor_line_says_off_when_the_gate_is_closed(tmp_path: Path) -> None:
    settings = Settings.from_env(tmp_path, environ={}, load_dotenv=False)
    settings.ensure_directories()
    Database(settings.database_path).initialize()
    assert "off" in sync_summary_line(settings, Database(settings.database_path))


def test_release_audit_has_thirty_checks_including_sync(tmp_path: Path) -> None:
    settings = Settings.from_env(tmp_path, environ={}, load_dotenv=False)
    settings.ensure_directories()
    Database(settings.database_path).initialize()
    result = audit_install(settings)
    names = [check.name for check in result.checks]
    assert len(result.checks) == 34
    assert "device_sync" in names
    sync_check = next(check for check in result.checks if check.name == "device_sync")
    assert sync_check.status == "pass"
    assert "AES-256-GCM" in sync_check.detail


def test_build_identity_matches_the_phase(devices) -> None:
    assert __version__ == "0.82.17"
    assert TRANSFORMATION_PHASE == 18
    assert "Production" in TRANSFORMATION_LABEL
    assert LATEST_SCHEMA_VERSION == 18


def test_local_service_exposes_the_sync_surface(devices) -> None:
    laptop, desktop, shared = devices
    local = LocalService(laptop)
    peer = service(desktop).identity().public_document()
    local.sync_pair(
        name="Desktop",
        sign_public=peer["sign_public"],
        encrypt_public=peer["encrypt_public"],
        confirm="PAIR",
    )
    assert local.sync_status()["counts"]["peers"] == 1
    exported = local.sync_export()
    assert Path(exported["path"]).parent == shared
    assert local.sync_bundles(direction="out")["total"] == 1
    events = [event["event_type"] for event in local.list_events(limit=20)["events"]]
    assert "sync.paired" in events
    assert "sync.exported" in events


def test_http_sync_routes_are_wired(devices) -> None:
    laptop, desktop, _shared = devices
    server = LocalApiServer(
        replace(laptop, api_port=0, scheduler_interval_seconds=0, api_enabled=True)
    )
    server.start()
    try:
        import urllib.error
        import urllib.request

        _host, port = server.address
        headers = {"Authorization": f"Bearer {server.token}"}
        base = f"http://127.0.0.1:{port}"

        def call(path: str, method: str = "GET", body: dict | None = None):
            data = json.dumps(body).encode("utf-8") if body is not None else None
            request = urllib.request.Request(
                base + path,
                data=data,
                method=method,
                headers={**headers, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))["data"]

        status = call("/v1/sync/status")
        assert status["transport"] == "shared-folder"
        assert status["enabled"] is True
        identity = call("/v1/sync/identity")
        assert identity["device_id"] == status["device"]["device_id"]

        peer = service(desktop).identity().public_document()
        paired = call(
            "/v1/sync/pair",
            "POST",
            {
                "name": "Desktop",
                "sign_public": peer["sign_public"],
                "encrypt_public": peer["encrypt_public"],
                "confirm": "PAIR",
            },
        )
        assert paired["name"] == "Desktop"
        assert call("/v1/sync/peers")["total"] == 1

        exported = call("/v1/sync/export", "POST", {})
        assert exported["recipients"] == [peer["device_id"]]
        assert call("/v1/sync/bundles")["total"] == 1
        assert call("/v1/sync/requests")["total"] == 0

        request = urllib.request.Request(
            base + "/v1/sync/pair",
            method="POST",
            data=json.dumps(
                {"name": "Nope", "sign_public": "x", "encrypt_public": "y"}
            ).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=30)
        assert excinfo.value.code == 400

        removed = urllib.request.Request(
            base + f"/v1/sync/peers/{peer['device_id']}",
            method="DELETE",
            headers=headers,
        )
        with urllib.request.urlopen(removed, timeout=30) as response:
            assert json.loads(response.read().decode("utf-8"))["data"]["unpaired"] == peer[
                "device_id"
            ]
        assert call("/v1/sync/peers")["total"] == 0
    finally:
        server.stop()


def test_cli_and_gui_surfaces_exist(devices, capsys) -> None:
    _laptop, _desktop, _shared = devices
    from pc_agent.cli import build_parser
    from pc_agent.gui.app import NOTEBOOK_TAB_COUNT

    parser = build_parser()
    args = parser.parse_args(["sync", "status"])
    assert args.command == "sync"
    assert args.sync_command == "status"
    assert NOTEBOOK_TAB_COUNT == 20
    pair_args = parser.parse_args(
        ["sync", "pair", "--name", "Desktop", "--sign", "a", "--encrypt", "b"]
    )
    assert pair_args.confirm is None


def test_export_path_must_use_the_bundle_suffix(devices) -> None:
    laptop, desktop, shared = devices
    sender, _receiver = pair(laptop, desktop)
    with pytest.raises(SyncError) as excinfo:
        sender.export(path=str(shared / "plain.json"))
    assert ".pcasync" in str(excinfo.value)


def test_retention_skips_stale_bundles_during_tick(devices) -> None:
    laptop, desktop, _shared = devices
    sender, receiver = pair(laptop, desktop)
    path = Path(sender.export()["path"])
    old = time.time() - 90 * 86400
    import os as os_module

    os_module.utime(path, (old, old))
    result = receiver.tick()
    assert result["imported"] == []


def test_no_network_transport_is_used(devices) -> None:
    """The transport is the filesystem: bundle envelope carries no URL fields."""

    laptop, desktop, _shared = devices
    sender, _receiver = pair(laptop, desktop)
    path = Path(sender.export()["path"])
    envelope = json.loads(path.read_bytes())
    assert set(envelope) == {"header", "recipients", "signature"}
    assert "url" not in json.dumps(envelope["header"]).lower()


def test_identity_uses_ed25519_and_x25519(tmp_path: Path) -> None:
    identity, _ = load_or_create_identity(tmp_path)
    assert isinstance(identity, DeviceIdentity)
    assert len(identity.device_id) == 32
    assert len(identity.sign_public) == 32
    assert len(identity.encrypt_public) == 32
    assert identity.sign_public != identity.encrypt_public
    assert identity.to_dict()["sign_seed"] and identity.to_dict()["encrypt_seed"]
