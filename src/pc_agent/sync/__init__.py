"""Optional encrypted device sync (Transformation Phase 16).

Local-first by design: this package never opens a network connection. It writes
an end-to-end encrypted bundle into a folder the *user* chose (OneDrive, Dropbox,
a USB stick, a LAN share) and reads bundles the user's other devices put there.

Every bundle is:

* **signed** with the sending device's Ed25519 key and refused unless that key is
  a paired (trusted) peer;
* **encrypted** to each recipient's X25519 key with an ephemeral-static ECDH,
  HKDF-SHA256 and AES-256-GCM, so the sync folder (and any cloud behind it)
  never sees plaintext;
* **replay-protected** with a bundle id, a per-sender sequence number and a
  clock-skew window;
* **bounded** in size, record count and retention.

Nothing here can execute code from a peer. Remote "coordination" is limited to
asking this device to create a reminder or a notification, and every such request
lands in a ledger that a human must accept.
"""

from __future__ import annotations

__all__ = [
    "BUNDLE_SUFFIX",
    "COLLECTIONS",
    "REQUEST_KINDS",
    "SyncError",
    "SyncService",
    "SyncStore",
]

from ..errors import SyncError
from .service import BUNDLE_SUFFIX, COLLECTIONS, REQUEST_KINDS, SyncService
from .store import SyncStore
