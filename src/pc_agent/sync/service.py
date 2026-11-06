"""SyncService: pairing, encrypted export/import and coordination requests.

Red lines enforced here (each is a refusal, never a warning):

* ``PC_AGENT_ALLOW_SYNC`` must be on;
* a bundle is only accepted from a **paired** device whose signing key matches
  the one recorded at pairing time;
* a bundle is only decrypted if it is addressed to this device's key;
* a bundle id is processed **once** and its per-sender sequence must move
  forward, inside the clock-skew window;
* payload size, record count and retention are bounded;
* a peer can never execute anything. The only remote actions are "create a
  reminder" and "show a notification", and both wait in a ledger for a human to
  accept them.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..errors import PcAgentError, SyncError
from ..proactive import ProactiveEngine
from ..storage.db import Database
from .crypto import (
    DeviceIdentity,
    device_id_for,
    fingerprint,
    load_or_create_identity,
    open_bundle,
    seal_bundle,
)
from .perms import harden_private_file
from .store import SyncStore

__all__ = ["BUNDLE_SUFFIX", "COLLECTIONS", "SyncService"]

_LOG = logging.getLogger(__name__)

BUNDLE_SUFFIX = ".pcasync"
COLLECTIONS = ("reminders", "flows", "profiles")
REQUEST_KINDS = ("remind", "notify", "ping")
CONFLICT_POLICIES = ("local", "newest", "remote")


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(moment: datetime | None = None) -> str:
    return (moment or _now()).replace(microsecond=0).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _clean(value: Any, limit: int) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()[:limit]


class SyncService:
    """Encrypted, replay-protected synchronisation between the user's devices."""

    def __init__(
        self,
        settings: Any,
        database: Database,
        *,
        events: Any | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._events = events
        self._store = SyncStore(database)
        #: How the last exported bundle was protected (posix mode or Windows ACL).
        self._last_permissions: dict[str, Any] = {}

    # -- gates --------------------------------------------------------------
    def store(self) -> SyncStore:
        return self._store

    @property
    def allowed(self) -> bool:
        return bool(getattr(self._settings, "allow_sync", False))

    def _require(self) -> None:
        if not self.allowed:
            raise SyncError(
                "device sync is disabled; set PC_AGENT_ALLOW_SYNC=1 to use it "
                "(each bundle is end-to-end encrypted and signed)"
            )

    def _data_dir(self) -> Path:
        return Path(str(getattr(self._settings, "data_dir", ".")))

    def _sync_dir(self) -> Path:
        raw = str(getattr(self._settings, "sync_dir", "") or "").strip()
        if not raw:
            raise SyncError(
                "no sync folder is configured; set PC_AGENT_SYNC_DIR to a folder your "
                "devices share (OneDrive, Dropbox, a USB stick, a LAN share)"
            )
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(str(getattr(self._settings, "root_dir", "."))) / path
        return path

    def identity(self) -> DeviceIdentity:
        identity, _created = load_or_create_identity(self._data_dir())
        return identity

    @property
    def max_bundle_bytes(self) -> int:
        return int(getattr(self._settings, "sync_max_bundle_bytes", 4_000_000))

    @property
    def max_records(self) -> int:
        return int(getattr(self._settings, "sync_max_records", 500))

    @property
    def conflict_policy(self) -> str:
        policy = str(getattr(self._settings, "sync_conflict_policy", "newest") or "newest")
        return policy if policy in CONFLICT_POLICIES else "newest"

    @property
    def clock_skew_seconds(self) -> int:
        return int(getattr(self._settings, "sync_clock_skew_seconds", 900))

    def _trusted(self) -> dict[str, dict[str, Any]]:
        return {row["device_id"]: row for row in self._store.devices(limit=50)}

    # -- status -------------------------------------------------------------
    def status(self, *, probe: bool = False) -> dict[str, Any]:
        identity = self.identity()
        counts = self._store.counts()
        peers = [
            {
                "device_id": row["device_id"],
                "name": row["name"],
                "fingerprint": row["fingerprint"],
                "status": row["status"],
                "paired_at": row["paired_at"],
                "last_seen_at": row["last_seen_at"],
                "bundle_count": row["bundle_count"],
            }
            for row in self._store.devices(limit=50)
        ]
        sync_dir = ""
        directory_state = "not configured"
        if str(getattr(self._settings, "sync_dir", "") or "").strip():
            try:
                path = self._sync_dir()
                sync_dir = path.name or str(path)
                directory_state = "exists" if path.is_dir() else "missing"
            except SyncError:
                directory_state = "invalid"
        return {
            "enabled": self.allowed,
            "gate": "PC_AGENT_ALLOW_SYNC",
            "device": identity.public_document(),
            "device_name": str(getattr(self._settings, "sync_device_name", "") or ""),
            "transport": "shared-folder",
            "sync_dir": sync_dir,
            "sync_dir_state": directory_state,
            "conflict_policy": self.conflict_policy,
            "limits": {
                "max_bundle_bytes": self.max_bundle_bytes,
                "max_records": self.max_records,
                "clock_skew_seconds": self.clock_skew_seconds,
                "retention_days": int(getattr(self._settings, "sync_retention_days", 30)),
            },
            "collections": list(COLLECTIONS),
            "counts": counts,
            "peers": peers,
            "last_export_at": self._store.get_state("last_export_at", ""),
            "last_import_at": self._store.get_state("last_import_at", ""),
            "detail": (
                "device sync is disabled; set PC_AGENT_ALLOW_SYNC=1 and pair a device"
                if not self.allowed
                else "sync is enabled; bundles are end-to-end encrypted and signed"
            ),
        }

    def summary(self) -> dict[str, Any]:
        counts = self._store.counts()
        return {
            "enabled": self.allowed,
            "device": self.identity().device_id,
            "peers": counts["peers"],
            "bundles": counts["bundles"],
            "pending_requests": counts["pending_requests"],
            "transport": "shared-folder",
        }

    # -- pairing ------------------------------------------------------------
    def pair(
        self,
        *,
        name: str,
        sign_public: str,
        encrypt_public: str,
        confirm: str | None,
    ) -> dict[str, Any]:
        self._require()
        clean_name = _clean(name, 60)
        if not clean_name:
            raise SyncError("A device name is required when pairing.")
        from .crypto import _unb64  # local import keeps the helper private

        sign_bytes = _unb64(sign_public, field="sign_public")
        encrypt_bytes = _unb64(encrypt_public, field="encrypt_public")
        if len(sign_bytes) != 32 or len(encrypt_bytes) != 32:
            raise SyncError("A peer's sign and encrypt public keys must each be 32 raw bytes.")
        if str(confirm or "") != "PAIR":
            raise SyncError(
                'Pairing trusts another device with your data; pass confirm="PAIR" to accept.'
            )
        device_id = device_id_for(sign_bytes, encrypt_bytes)
        if device_id == self.identity().device_id:
            raise SyncError("That is this device's own key; pair a different device.")
        row = self._store.upsert_device(
            device_id=device_id,
            name=clean_name,
            sign_public=sign_public,
            encrypt_public=encrypt_public,
            fingerprint=fingerprint(sign_bytes, encrypt_bytes),
        )
        self._emit(
            "sync.paired",
            summary=f"Paired sync device '{clean_name}'.",
            payload={"device": device_id, "fingerprint": row["fingerprint"]},
        )
        return self._peer_entry(row)

    def unpair(self, device: str) -> dict[str, Any]:
        self._require()
        row = self._resolve_peer(device)
        self._store.delete_device(row["device_id"])
        self._emit(
            "sync.unpaired",
            summary=f"Unpaired sync device '{row['name']}'.",
            payload={"device": row["device_id"]},
        )
        return {"unpaired": row["device_id"], "name": row["name"]}

    def peers(self) -> dict[str, Any]:
        rows = self._store.devices(limit=50)
        return {"peers": [self._peer_entry(row) for row in rows], "total": len(rows)}

    def _peer_entry(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "device_id": row["device_id"],
            "name": row["name"],
            "fingerprint": row["fingerprint"],
            "sign_public": row["sign_public"],
            "encrypt_public": row["encrypt_public"],
            "status": row["status"],
            "paired_at": row["paired_at"],
            "last_seen_at": row["last_seen_at"],
            "bundle_count": row["bundle_count"],
        }

    def _resolve_peer(self, reference: str) -> dict[str, Any]:
        text = str(reference or "").strip()
        if not text:
            raise SyncError("A device id (or id prefix) is required.")
        for row in self._store.devices(limit=50):
            if row["device_id"] == text or row["device_id"].startswith(text):
                return row
            if row["name"].lower() == text.lower():
                return row
        raise SyncError(f"No paired device matches '{text}'.")

    def set_peer_enabled(self, device: str, *, enabled: bool) -> dict[str, Any]:
        self._require()
        row = self._resolve_peer(device)
        status = "paired" if enabled else "paused"
        self._store.set_device_status(row["device_id"], status)
        self._emit(
            "sync.peer_status",
            summary=f"Sync device '{row['name']}' is now {status}.",
            payload={"device": row["device_id"], "status": status},
        )
        return self._peer_entry(self._store.get_device(row["device_id"]) or row)

    # -- snapshot -----------------------------------------------------------
    def build_payload(self, *, requests: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Collect the syncable collections from live state (never a mock)."""

        identity = self.identity()
        engine = ProactiveEngine(self._settings, self._database)
        reminders = [
            {
                "title": item.title,
                "note": item.note,
                "due_at": item.due_at,
                "severity": item.severity,
            }
            for item in engine.store.list_reminders(status="open", limit=self.max_records)
        ]
        collections: dict[str, list[dict[str, Any]]] = {
            "reminders": reminders[: self.max_records],
            "flows": self._flow_records(limit=self.max_records),
            "profiles": self._profile_records(limit=50),
        }
        return {
            "schema": 1,
            "kind": "snapshot",
            "device": identity.device_id,
            "device_name": str(getattr(self._settings, "sync_device_name", "") or ""),
            "created_at": _iso(),
            "collections": collections,
            "requests": list(requests or []),
        }

    def _flow_records(self, *, limit: int) -> list[dict[str, Any]]:
        from ..flows import FlowStore

        records = []
        for flow in FlowStore(self._database).list_flows(limit=limit):
            records.append(
                {
                    "name": flow.name,
                    "description": flow.description,
                    "definition": flow.definition.to_dict(),
                    "sha256": flow.sha256,
                    "updated_at": flow.updated_at.isoformat(),
                }
            )
        return records

    def _profile_records(self, *, limit: int) -> list[dict[str, Any]]:
        from ..profiles import list_profiles, load_profile

        records = []
        data_dir = self._data_dir()
        for info in list_profiles(data_dir)[:limit]:
            try:
                payload = load_profile(data_dir, info.name)
            except PcAgentError:  # unreadable profile is skipped, not fatal
                continue
            records.append({"name": info.name, "data": payload})
        return records

    # -- export -------------------------------------------------------------
    def export(
        self,
        *,
        path: str | Path | None = None,
        requests: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self._require()
        identity = self.identity()
        peers = [row for row in self._store.devices(limit=50) if row["status"] == "paired"]
        if not peers:
            raise SyncError(
                "no paired device to encrypt for; run 'pc-agent sync pair' with a peer's "
                "public keys first"
            )
        if path is None:
            directory = self._sync_dir()
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"bundle-{identity.device_id[:8]}-{self._store.new_id()[:12]}{BUNDLE_SUFFIX}"
        else:
            target = Path(str(path)).expanduser()
            if target.suffix != BUNDLE_SUFFIX:
                raise SyncError(f"A sync bundle must use the {BUNDLE_SUFFIX} extension.")
            if target.parent and not target.parent.exists():
                raise SyncError(f"Directory '{target.parent}' does not exist.")

        seq = int(self._store.get_state(f"seq:{identity.device_id}", 0)) + 1
        payload = self.build_payload(requests=requests)
        envelope = seal_bundle(
            payload,
            identity=identity,
            recipients=[
                {
                    "device_id": row["device_id"],
                    "encrypt_public": row["encrypt_public"],
                }
                for row in peers
            ],
            bundle_id=self._store.new_id(),
            seq=seq,
        )
        blob = json.dumps(envelope, sort_keys=True).encode("utf-8")
        if len(blob) > self.max_bundle_bytes:
            raise SyncError(
                f"Sync bundle would be {len(blob)} bytes, over the "
                f"{self.max_bundle_bytes}-byte PC_AGENT_SYNC_MAX_BUNDLE_BYTES limit; "
                "reduce records or raise the limit deliberately."
            )
        try:
            target.write_bytes(blob)
        except OSError as exc:
            raise SyncError(f"Sync bundle could not be written: {exc.strerror or exc}") from exc
        # 0600 on POSIX, owner-only ACL on Windows: the same intent, enforced by
        # whichever mechanism the host actually has.
        self._last_permissions = harden_private_file(target)

        records = sum(len(items) for items in payload["collections"].values())
        header = envelope["header"]
        self._store.set_state(f"seq:{identity.device_id}", seq)
        self._store.set_state("last_export_at", header["created_at"])
        entry = self._store.record_bundle(
            bundle_id=header["bundle_id"],
            direction="out",
            peer=",".join(row["name"] for row in peers),
            status="exported",
            detail=f"{len(peers)} recipient(s)",
            record_count=records,
            size_bytes=len(blob),
            seq=seq,
            path=str(target),
        )
        self._emit(
            "sync.exported",
            summary=f"Exported sync bundle with {records} record(s) for {len(peers)} device(s).",
            payload={"bundle": header["bundle_id"], "records": records, "seq": seq},
        )
        return {
            "bundle_id": header["bundle_id"],
            "path": str(target),
            "size_bytes": len(blob),
            "records": records,
            "recipients": [row["device_id"] for row in peers],
            "seq": seq,
            "collections": {name: len(items) for name, items in payload["collections"].items()},
            "permissions": dict(self._last_permissions),
            "ledger": entry,
        }

    # -- import -------------------------------------------------------------
    def import_file(self, path: str | Path) -> dict[str, Any]:
        self._require()
        target = Path(str(path)).expanduser()
        if not target.is_file():
            raise SyncError(f"Sync bundle '{target.name}' does not exist.")
        size = target.stat().st_size
        if size > self.max_bundle_bytes:
            raise SyncError(
                f"Sync bundle is {size} bytes, over the {self.max_bundle_bytes}-byte limit."
            )
        try:
            envelope = json.loads(target.read_bytes())
        except ValueError as exc:
            raise SyncError("Sync bundle is not valid JSON.") from exc

        identity = self.identity()
        bundle_id = ""
        if isinstance(envelope, dict) and isinstance(envelope.get("header"), dict):
            bundle_id = str(envelope["header"].get("bundle_id") or "")
        if bundle_id and self._store.bundle_seen(bundle_id):
            raise SyncError(f"Sync bundle '{bundle_id[:8]}' was already processed (replay refused).")

        payload, header = open_bundle(
            envelope,
            identity=identity,
            trusted_peers=self._trusted(),
            max_bytes=self.max_bundle_bytes,
            clock_skew_seconds=self.clock_skew_seconds,
        )
        sender = str(header["sender_device"])
        last_seq = int(self._store.get_state(f"in:{sender}", 0))
        if int(header["seq"]) <= last_seq:
            raise SyncError(
                f"Sync bundle sequence {header['seq']} is not newer than the last accepted "
                f"{last_seq} from that device (replay refused)."
            )
        result = self._apply(payload, sender=sender, bundle_id=str(header["bundle_id"]))
        self._store.set_state(f"in:{sender}", int(header["seq"]))
        self._store.set_state("last_import_at", _iso())
        self._store.touch_device(sender)
        entry = self._store.record_bundle(
            bundle_id=str(header["bundle_id"]),
            direction="in",
            peer=sender,
            status="imported",
            detail=(
                f"applied={result['applied']} skipped={result['skipped']} "
                f"conflicts={len(result['conflicts'])}"
            ),
            record_count=result["applied"] + result["skipped"],
            size_bytes=size,
            seq=int(header["seq"]),
            path=str(target),
        )
        self._emit(
            "sync.imported",
            summary=(
                f"Imported sync bundle from {sender[:8]}: applied {result['applied']}, "
                f"skipped {result['skipped']}, conflicts {len(result['conflicts'])}."
            ),
            payload={"bundle": header["bundle_id"], "applied": result["applied"]},
        )
        return {
            "bundle_id": header["bundle_id"],
            "from": sender,
            "sender_fingerprint": header.get("sender_fingerprint", ""),
            "seq": int(header["seq"]),
            **result,
            "ledger": entry,
        }

    def _apply(
        self, payload: dict[str, Any], *, sender: str, bundle_id: str
    ) -> dict[str, Any]:
        if payload.get("schema") != 1:
            raise SyncError(f"Sync payload schema {payload.get('schema')!r} is not supported.")
        if str(payload.get("device") or "") != sender:
            raise SyncError("Sync payload device does not match the signing device.")
        collections = payload.get("collections")
        if not isinstance(collections, dict):
            raise SyncError("Sync payload has no collections object.")
        total = 0
        applied = skipped = 0
        conflicts: list[dict[str, Any]] = []
        for name, items in collections.items():
            if name not in COLLECTIONS:
                raise SyncError(f"Sync payload contains an unknown collection '{name}'.")
            if not isinstance(items, list):
                raise SyncError(f"Sync collection '{name}' must be a list.")
            total += len(items)
            handler = {
                "reminders": self._apply_reminders,
                "flows": self._apply_flows,
                "profiles": self._apply_profiles,
            }[name]
            outcome = handler(items, conflicts=conflicts, sender=sender)
            applied += outcome[0]
            skipped += outcome[1]
        if total > self.max_records:
            raise SyncError(
                f"Sync payload has {total} records, over the {self.max_records}-record limit."
            )
        requests = payload.get("requests") or []
        if not isinstance(requests, list) or len(requests) > 20:
            raise SyncError("Sync payload has too many coordination requests.")
        stored = 0
        for item in requests:
            if not isinstance(item, dict):
                raise SyncError("Each coordination request must be an object.")
            kind = str(item.get("kind") or "")
            if kind not in REQUEST_KINDS:
                raise SyncError(f"Coordination request kind '{kind}' is not supported.")
            request_id = str(item.get("id") or "") or f"{bundle_id}:{stored}"
            self._store.add_request(
                request_id=request_id[:80],
                device=sender,
                kind=kind,
                payload={
                    "title": _clean(item.get("title"), 120),
                    "body": _clean(item.get("body"), 300),
                    "due": _clean(item.get("due"), 40),
                    "severity": _clean(item.get("severity"), 16) or "normal",
                },
                bundle_id=bundle_id,
            )
            stored += 1
        return {
            "applied": applied,
            "skipped": skipped,
            "conflicts": conflicts,
            "requests": stored,
        }

    # -- per-collection apply ----------------------------------------------
    def _apply_reminders(
        self, items: list[Any], *, conflicts: list[dict[str, Any]], sender: str
    ) -> tuple[int, int]:
        engine = ProactiveEngine(self._settings, self._database)
        existing = {
            (item.title.lower(), item.due_at)
            for item in engine.store.list_reminders(status=None, limit=500)
        }
        applied = skipped = 0
        for item in items:
            if not isinstance(item, dict):
                raise SyncError("Each synced reminder must be an object.")
            title = _clean(item.get("title"), 120)
            due_at = _clean(item.get("due_at"), 40)
            key = (title.lower(), due_at)
            if not title or not due_at or key in existing:
                skipped += 1
                continue
            try:
                engine.add_reminder(
                    title=title,
                    due=due_at,
                    note=_clean(item.get("note"), 300),
                    severity=_clean(item.get("severity"), 16) or "normal",
                    source=f"sync:{sender[:8]}",
                )
            except PcAgentError as exc:
                conflicts.append(
                    {"collection": "reminders", "name": title, "reason": _clean(exc, 200)}
                )
                continue
            existing.add(key)
            applied += 1
        return applied, skipped

    def _apply_flows(
        self, items: list[Any], *, conflicts: list[dict[str, Any]], sender: str
    ) -> tuple[int, int]:
        from ..flows import FlowStore, validate_definition

        store = FlowStore(self._database)
        applied = skipped = 0
        for item in items:
            if not isinstance(item, dict):
                raise SyncError("Each synced flow must be an object.")
            name = _clean(item.get("name"), 120)
            definition_payload = item.get("definition")
            if not name or not isinstance(definition_payload, dict):
                raise SyncError("A synced flow needs a name and a definition object.")
            remote_sha = str(item.get("sha256") or "")
            remote_updated = _parse_iso(item.get("updated_at"))
            local = store.find_by_name(name)
            if local is None:
                try:
                    validate_definition(definition_payload)
                except PcAgentError as exc:
                    conflicts.append(
                        {"collection": "flows", "name": name, "reason": _clean(exc, 200)}
                    )
                    continue
                store.create(validate_definition(definition_payload))
                applied += 1
                continue
            if remote_sha and remote_sha == local.sha256:
                skipped += 1
                continue
            policy = self.conflict_policy
            take_remote = False
            if policy == "remote":
                take_remote = True
            elif policy == "newest":
                take_remote = bool(
                    remote_updated
                    and remote_updated.astimezone(UTC) > local.updated_at.astimezone(UTC)
                )
            if not take_remote:
                conflicts.append(
                    {
                        "collection": "flows",
                        "name": name,
                        "reason": f"local copy kept (policy={policy}, remote is not newer)",
                    }
                )
                continue
            try:
                store.update(local.id, validate_definition(definition_payload))
            except PcAgentError as exc:
                conflicts.append(
                    {"collection": "flows", "name": name, "reason": _clean(exc, 200)}
                )
                continue
            applied += 1
        return applied, skipped

    def _apply_profiles(
        self, items: list[Any], *, conflicts: list[dict[str, Any]], sender: str
    ) -> tuple[int, int]:
        from ..profiles import list_profiles, save_profile

        data_dir = self._data_dir()
        existing = {info.name for info in list_profiles(data_dir)}
        applied = skipped = 0
        for item in items:
            if not isinstance(item, dict):
                raise SyncError("Each synced profile must be an object.")
            name = _clean(item.get("name"), 60)
            data = item.get("data")
            if not name or not isinstance(data, dict):
                raise SyncError("A synced profile needs a name and a data object.")
            if name in existing:
                # Profiles are settings; a peer never overwrites a local profile.
                conflicts.append(
                    {
                        "collection": "profiles",
                        "name": name,
                        "reason": "profile already exists locally and was not overwritten",
                    }
                )
                skipped += 1
                continue
            try:
                save_profile(data_dir, name, data)
            except PcAgentError as exc:
                conflicts.append(
                    {"collection": "profiles", "name": name, "reason": _clean(exc, 200)}
                )
                continue
            existing.add(name)
            applied += 1
        return applied, skipped

    # -- coordination -------------------------------------------------------
    def request(
        self,
        kind: str,
        *,
        title: str,
        body: str = "",
        due: str = "",
        severity: str = "normal",
        path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Ask a paired device to remind/notify you (encrypted like any bundle)."""

        self._require()
        if kind not in REQUEST_KINDS:
            raise SyncError(f"Coordination kind must be one of {', '.join(REQUEST_KINDS)}.")
        clean_title = _clean(title, 120)
        if not clean_title:
            raise SyncError("A coordination request needs a title.")
        request = {
            "id": self._store.new_id(),
            "kind": kind,
            "title": clean_title,
            "body": _clean(body, 300),
            "due": _clean(due, 40),
            "severity": _clean(severity, 16) or "normal",
        }
        exported = self.export(path=path, requests=[request])
        self._emit(
            "sync.request_sent",
            summary=f"Sent a '{kind}' coordination request to paired devices.",
            payload={"request": request["id"], "kind": kind},
        )
        return {"request": request, "bundle": exported}

    def requests(self, *, status: str | None = None, limit: int = 25) -> dict[str, Any]:
        rows = self._store.requests(status=status, limit=limit)
        return {"requests": rows, "total": len(rows)}

    def decide(self, request_id: str, *, accept: bool, note: str = "") -> dict[str, Any]:
        """Accept or deny a peer's coordination request. Accepting runs it locally."""

        self._require()
        reference = str(request_id or "").strip()
        if not reference:
            raise SyncError("A coordination request id is required.")
        record = self._store.get_request(reference)
        if record is None:
            for row in self._store.requests(limit=100):
                if row["id"].startswith(reference):
                    record = row
                    break
        if record is None:
            raise SyncError(f"No coordination request matches '{reference}'.")
        if record["status"] != "pending":
            raise SyncError(
                f"Coordination request {record['id'][:8]} is already {record['status']}."
            )
        payload = record.get("payload") or {}
        detail = _clean(note, 200)
        if accept:
            detail = f"{self._execute(record['kind'], payload)}; {detail}".strip("; ")
        status = "accepted" if accept else "denied"
        self._store.decide_request(record["id"], status=status, detail=detail)
        self._emit(
            "sync.request_decided",
            summary=f"Coordination request {record['id'][:8]} {status}.",
            payload={"request": record["id"], "kind": record["kind"], "status": status},
        )
        return {
            "request": self._store.get_request(record["id"]),
            "status": status,
            "detail": detail,
        }

    def _execute(self, kind: str, payload: dict[str, Any]) -> str:
        engine = ProactiveEngine(self._settings, self._database)
        if kind == "ping":
            return "ping acknowledged"
        if kind == "remind":
            due = str(payload.get("due") or "").strip() or "+15m"
            created = engine.add_reminder(
                title=str(payload.get("title") or "Shared reminder"),
                note=str(payload.get("body") or ""),
                due=due,
                severity=str(payload.get("severity") or "normal"),
                source="sync",
            )
            return f"reminder {created['id'][:8]} due {str(created['due_at'])[:19]}"
        if kind == "notify":
            result = engine.notify(
                title=str(payload.get("title") or "Peer message"),
                body=str(payload.get("body") or ""),
                kind="sync",
                severity=str(payload.get("severity") or "normal"),
                dedupe_key=f"sync:{payload.get('title', '')[:40]}",
            )
            return f"notification {result.get('status', 'recorded')}"
        raise SyncError(f"Coordination kind '{kind}' cannot be executed.")

    # -- folder transport ---------------------------------------------------
    def tick(self) -> dict[str, Any]:
        """Export a fresh bundle into the sync folder and import anything new."""

        self._require()
        directory = self._sync_dir()
        directory.mkdir(parents=True, exist_ok=True)
        exported = self.export()
        imported: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        cutoff = _now() - timedelta(
            days=int(getattr(self._settings, "sync_retention_days", 30))
        )
        for path in sorted(directory.glob(f"*{BUNDLE_SUFFIX}")):
            if path.name == Path(exported["path"]).name:
                continue
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            except OSError:
                continue
            if modified < cutoff:
                continue
            try:
                imported.append(self.import_file(path))
            except SyncError as exc:
                message = str(exc)
                if "already processed" in message:
                    continue
                refused.append({"path": path.name, "reason": message})
        return {
            "exported": exported,
            "imported": imported,
            "refused": refused,
            "sync_dir": str(directory),
        }

    def bundles(self, *, direction: str | None = None, limit: int = 25) -> dict[str, Any]:
        rows = self._store.bundles(direction=direction, limit=limit)
        return {
            "bundles": [
                {
                    "bundle_id": row["id"],
                    "direction": row["direction"],
                    "peer": row["peer"],
                    "status": row["status"],
                    "detail": row["detail"],
                    "record_count": row["record_count"],
                    "size_bytes": row["size_bytes"],
                    "seq": row["seq"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
            "total": len(rows),
        }

    # -- events -------------------------------------------------------------
    def _emit(self, event_type: str, *, summary: str, payload: dict[str, Any]) -> None:
        if self._events is None:
            return
        try:
            self._events.record(event_type, source="sync", summary=summary, payload=payload)
        except PcAgentError:
            _LOG.exception("Failed to record sync event %s", event_type)
