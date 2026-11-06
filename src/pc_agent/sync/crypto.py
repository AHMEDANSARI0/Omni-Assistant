"""Device identity and end-to-end encryption for sync bundles.

Scheme (deliberately boring, standard primitives only):

* Device identity = one Ed25519 key (sign) + one X25519 key (encrypt). Both are
  generated locally, stored under the data directory with owner-only
  permissions and never leave the machine in plaintext.
* Bundle = plaintext *header* (who, when, which sequence, which recipients) +
  one sealed entry per recipient:
  ``ECDH(sender_ephemeral_X25519, recipient_static_X25519) -> HKDF-SHA256 -> AES-256-GCM``.
* The header and every recipient entry are covered by the sender's Ed25519
  signature, so a bundle cannot be re-targeted, re-ordered or spliced.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ..errors import SyncError
from .perms import harden_private_file

__all__ = [
    "DEVICE_ID_LENGTH",
    "DeviceIdentity",
    "device_id_for",
    "fingerprint",
    "load_or_create_identity",
    "open_bundle",
    "seal_bundle",
]

DEVICE_ID_LENGTH = 32
_KDF_INFO = b"pc-agent-sync-v1"
_NONCE_BYTES = 12
_SIGN_CONTEXT = b"pc-agent-sync-bundle-v1"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: Any, *, field: str) -> bytes:
    try:
        return base64.b64decode(str(value or ""), validate=True)
    except Exception as exc:  # any decode failure is a refusal
        raise SyncError(f"Sync bundle field '{field}' is not valid base64.") from exc


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def device_id_for(sign_public: bytes, encrypt_public: bytes) -> str:
    """Stable short id derived from both public keys (never a secret)."""

    digest = hashlib.sha256(b"device" + sign_public + encrypt_public).hexdigest()
    return digest[:DEVICE_ID_LENGTH]


def fingerprint(sign_public: bytes, encrypt_public: bytes) -> str:
    """Human-comparable fingerprint, grouped for reading aloud."""

    digest = hashlib.sha256(b"fp" + sign_public + encrypt_public).hexdigest()[:32].upper()
    return "-".join(digest[index : index + 4] for index in range(0, 32, 4))


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """This device's two keypairs (sign + encrypt)."""

    sign_seed: bytes
    encrypt_seed: bytes

    @classmethod
    def generate(cls) -> DeviceIdentity:
        return cls(secrets.token_bytes(32), secrets.token_bytes(32))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DeviceIdentity:
        try:
            sign_seed = _unb64(payload.get("sign_seed"), field="sign_seed")
            encrypt_seed = _unb64(payload.get("encrypt_seed"), field="encrypt_seed")
        except SyncError as exc:
            raise SyncError("Stored sync identity is corrupt; refusing to use it.") from exc
        if len(sign_seed) != 32 or len(encrypt_seed) != 32:
            raise SyncError("Stored sync identity must hold two 32-byte seeds.")
        return cls(sign_seed, encrypt_seed)

    def to_dict(self) -> dict[str, str]:
        return {"sign_seed": _b64(self.sign_seed), "encrypt_seed": _b64(self.encrypt_seed)}

    @property
    def sign_private(self) -> Ed25519PrivateKey:
        return Ed25519PrivateKey.from_private_bytes(self.sign_seed)

    @property
    def encrypt_private(self) -> X25519PrivateKey:
        return X25519PrivateKey.from_private_bytes(self.encrypt_seed)

    @property
    def sign_public(self) -> bytes:
        return self.sign_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    @property
    def encrypt_public(self) -> bytes:
        return self.encrypt_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    @property
    def device_id(self) -> str:
        return device_id_for(self.sign_public, self.encrypt_public)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.sign_public, self.encrypt_public)

    def public_document(self) -> dict[str, str]:
        """What a peer may know about this device - no secrets."""

        return {
            "device_id": self.device_id,
            "fingerprint": self.fingerprint,
            "sign_public": _b64(self.sign_public),
            "encrypt_public": _b64(self.encrypt_public),
        }


def identity_path(data_dir: Path) -> Path:
    return Path(data_dir) / "sync" / "identity.json"


def load_or_create_identity(data_dir: Path) -> tuple[DeviceIdentity, bool]:
    """Load the device identity, creating it once with owner-only permissions."""

    path = identity_path(data_dir)
    if path.is_file():
        if path.stat().st_size > 4_096:
            raise SyncError("Stored sync identity file is implausibly large; refusing it.")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SyncError("Stored sync identity is unreadable; refusing to use it.") from exc
        if not isinstance(payload, dict):
            raise SyncError("Stored sync identity must be a JSON object.")
        return DeviceIdentity.from_dict(payload), False

    identity = DeviceIdentity.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(identity.to_dict()), encoding="utf-8")
    # POSIX applies 0600; Windows applies an owner-only ACL (os.chmod is a no-op
    # there, so the mode is never the guarantee on that platform).
    harden_private_file(path)
    return identity, True


def _signature_payload(header: dict[str, Any], recipients: list[dict[str, Any]]) -> bytes:
    digests = sorted(
        (
            {
                "device_id": entry["device_id"],
                "ephemeral_public": entry["ephemeral_public"],
                "nonce": entry["nonce"],
                "sha256": hashlib.sha256(_unb64(entry["ciphertext"], field="ciphertext")).hexdigest(),
            }
            for entry in recipients
        ),
        key=lambda item: item["device_id"],
    )
    return _SIGN_CONTEXT + _canonical({"header": header, "recipients": digests})


def seal_bundle(
    payload: dict[str, Any],
    *,
    identity: DeviceIdentity,
    recipients: list[dict[str, Any]],
    bundle_id: str,
    seq: int,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Encrypt ``payload`` for every recipient and sign the whole envelope."""

    if not recipients:
        raise SyncError("A sync bundle needs at least one paired recipient device.")
    body = _canonical(payload)
    header = {
        "schema": 1,
        "bundle_id": bundle_id,
        "created_at": created_at
        or datetime.now(UTC).replace(microsecond=0).isoformat(),
        "sender_device": identity.device_id,
        "sender_sign_public": _b64(identity.sign_public),
        "sender_fingerprint": identity.fingerprint,
        "seq": int(seq),
        "size_bytes": len(body),
    }
    entries: list[dict[str, Any]] = []
    for recipient in recipients:
        encrypt_public = _unb64(recipient.get("encrypt_public"), field="encrypt_public")
        if len(encrypt_public) != 32:
            raise SyncError("A recipient device public key must be 32 raw bytes.")
        ephemeral = X25519PrivateKey.generate()
        shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(encrypt_public))
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=_KDF_INFO
        ).derive(shared)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = AESGCM(key).encrypt(nonce, body, _canonical(header))
        entries.append(
            {
                "device_id": str(recipient.get("device_id") or ""),
                "ephemeral_public": _b64(
                    ephemeral.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
                ),
                "nonce": _b64(nonce),
                "ciphertext": _b64(ciphertext),
            }
        )
    signature = identity.sign_private.sign(_signature_payload(header, entries))
    return {
        "header": header,
        "recipients": entries,
        "signature": _b64(signature),
    }


def open_bundle(
    envelope: dict[str, Any],
    *,
    identity: DeviceIdentity,
    trusted_peers: dict[str, dict[str, Any]],
    max_bytes: int,
    clock_skew_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify, decrypt and return ``(payload, header)``. Every failure refuses."""

    if not isinstance(envelope, dict):
        raise SyncError("Sync bundle must be a JSON object.")
    header = envelope.get("header")
    entries = envelope.get("recipients")
    signature = _unb64(envelope.get("signature"), field="signature")
    if not isinstance(header, dict) or not isinstance(entries, list) or not entries:
        raise SyncError("Sync bundle is missing its header or recipients.")
    if len(entries) > 32:
        raise SyncError("Sync bundle addresses too many recipients.")
    if header.get("schema") != 1:
        raise SyncError(f"Sync bundle schema {header.get('schema')!r} is not supported.")

    sender = str(header.get("sender_device") or "")
    peer = trusted_peers.get(sender)
    if peer is None:
        raise SyncError(
            f"Sync bundle came from unpaired device '{sender or '(missing)'}'; pair it first."
        )
    if peer.get("status") != "paired":
        raise SyncError(f"Device '{sender}' is {peer.get('status')}; its bundles are refused.")
    sign_public = _unb64(peer.get("sign_public"), field="sign_public")
    if _b64(sign_public) != str(header.get("sender_sign_public") or ""):
        raise SyncError("Sync bundle signing key does not match the paired device key.")
    try:
        Ed25519PublicKey.from_public_bytes(sign_public).verify(
            signature, _signature_payload(header, entries)
        )
    except InvalidSignature as exc:
        raise SyncError("Sync bundle signature verification failed.") from exc

    created_raw = str(header.get("created_at") or "")
    try:
        created = datetime.fromisoformat(created_raw)
    except ValueError as exc:
        raise SyncError("Sync bundle timestamp is not ISO-8601.") from exc
    if created.tzinfo is None:
        raise SyncError("Sync bundle timestamp must carry a timezone.")
    delta = abs((datetime.now(UTC) - created.astimezone(UTC)).total_seconds())
    if delta > clock_skew_seconds:
        raise SyncError(
            f"Sync bundle is {int(delta)}s away from this device's clock "
            f"(allowed skew {clock_skew_seconds}s)."
        )

    entry = next(
        (item for item in entries if str(item.get("device_id") or "") == identity.device_id),
        None,
    )
    if entry is None:
        raise SyncError("Sync bundle is not addressed to this device.")
    ephemeral = _unb64(entry.get("ephemeral_public"), field="ephemeral_public")
    nonce = _unb64(entry.get("nonce"), field="nonce")
    ciphertext = _unb64(entry.get("ciphertext"), field="ciphertext")
    if len(ephemeral) != 32 or len(nonce) != _NONCE_BYTES:
        raise SyncError("Sync bundle recipient entry is malformed.")
    if len(ciphertext) > max_bytes:
        raise SyncError(
            f"Sync bundle payload is {len(ciphertext)} bytes, over the {max_bytes}-byte limit."
        )
    try:
        shared = identity.encrypt_private.exchange(X25519PublicKey.from_public_bytes(ephemeral))
    except Exception as exc:  # invalid public key material
        raise SyncError("Sync bundle ephemeral key was rejected.") from exc
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_KDF_INFO).derive(shared)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _canonical(header))
    except InvalidTag as exc:
        raise SyncError(
            "Sync bundle could not be decrypted: it is not addressed to this device's key."
        ) from exc
    try:
        payload = json.loads(plaintext)
    except ValueError as exc:
        raise SyncError("Sync bundle payload is not JSON.") from exc
    if not isinstance(payload, dict):
        raise SyncError("Sync bundle payload must be a JSON object.")
    return payload, header
