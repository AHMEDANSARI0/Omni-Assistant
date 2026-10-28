"""Global test isolation.

The automated suite must never read a developer's real Windows Credential
Manager entries. Tests that exercise secure storage inject a deterministic
in-memory or simulated Win32 backend explicitly.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_os_credential_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PC_AGENT_CREDENTIAL_STORE", "env")
