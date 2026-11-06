"""Plugin/Skill Ecosystem v2 (Transformation Phase 15).

A plugin is a signed, versioned bundle of **declared** capability plus, at
most, one Python module of tool functions and any number of Markdown skills.
Nothing here trusts a plugin: bundles are strict-validated, Ed25519-signed,
checksum-verified, installed under a private directory tree, and any tool call
runs in a separate, resource-limited child process with a scrubbed environment
and a hard timeout. Skills never execute - they are reviewed text the agent may
read, nothing more.
"""

from __future__ import annotations

__all__ = [
    "CAPABILITIES",
    "DENIED_CAPABILITIES",
    "PluginError",
    "PluginService",
    "PluginStore",
]

from ..errors import PluginError
from .manifest import CAPABILITIES, DENIED_CAPABILITIES
from .service import PluginService
from .store import PluginStore
