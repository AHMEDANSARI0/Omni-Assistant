"""Strict plugin manifest contract and Ed25519 signature verification.

The manifest is the *only* thing a plugin is allowed to tell us about itself,
so it is validated like an external document: known keys only, bounded sizes,
slug-shaped identifiers, semver versions, an explicit capability list and a
checksummed content map. Anything unexpected is refused instead of ignored.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..errors import PluginError

MAX_ID = 64
MAX_NAME = 120
MAX_VERSION = 32
MAX_DESCRIPTION = 500
MAX_PUBLISHER = 120
MAX_LICENSE = 60
MAX_TOOLS = 20
MAX_SKILLS = 20
MAX_TOOL_ARGS = 10
MAX_FILES = 200
MAX_FILE_BYTES = 2_000_000
MAX_BUNDLE_BYTES = 8_000_000

#: Capabilities a plugin may request. Everything else is refused by validation,
#: and every entry here is still gated again at run time by configuration.
CAPABILITIES = (
    "read_workspace",
    "write_workspace",
    "read_plugin_files",
    "network",
    "subprocess",
)

#: Capabilities that are never granted by the sandbox in this phase.
DENIED_CAPABILITIES = ("network", "subprocess")

ARGUMENT_TYPES = ("string", "integer", "number", "boolean")

MANIFEST_NAME = "manifest.json"
SIGNATURE_NAME = "manifest.sig.json"
ENTRY_NAME = "plugin.py"

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_VERSION_PATTERN = re.compile(r"^\d{1,6}\.\d{1,6}\.\d{1,6}$")
_TOOL_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_SAFE_PATH = re.compile(r"^(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/-]{1,160}$")


def canonical(payload: Any) -> str:
    """Deterministic JSON for hashing and signing."""

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def manifest_bytes(payload: dict[str, Any]) -> bytes:
    """The exact bytes a publisher signs: canonical JSON, no whitespace games."""

    return canonical(payload).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bundle_checksum(files: dict[str, str]) -> str:
    """Checksum over the ``{relative path: sha256}`` content map."""

    return sha256_hex(canonical(files).encode("utf-8"))


def safe_relative_path(value: Any, *, field_name: str = "path") -> str:
    """Reject absolute paths, traversal and unusual characters."""

    text = str(value or "").strip()
    if not text:
        raise PluginError(f"Plugin {field_name} must not be empty.")
    if not _SAFE_PATH.match(text):
        raise PluginError(
            f"Plugin {field_name} '{text}' must be a plain relative path "
            "without traversal segments."
        )
    if text.startswith("/") or "\\" in text:
        raise PluginError(f"Plugin {field_name} '{text}' must use forward slashes.")
    return text


def _text(value: Any, *, field_name: str, limit: int, allow_empty: bool = False) -> str:
    text = "" if value is None else str(value).strip()
    if not text and allow_empty:
        return ""
    if not text:
        raise PluginError(f"Plugin {field_name} must not be empty.")
    if len(text) > limit:
        raise PluginError(f"Plugin {field_name} is longer than {limit} characters.")
    if any(ord(char) < 32 for char in text):
        raise PluginError(f"Plugin {field_name} must not contain control characters.")
    return text


def _check_keys(payload: dict[str, Any], allowed: set[str], *, where: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise PluginError(f"Plugin {where} has unknown field(s): {', '.join(unknown)}.")


@dataclass(frozen=True, slots=True)
class PluginTool:
    name: str
    description: str
    arguments: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "arguments": dict(self.arguments),
        }


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """A validated manifest. Immutable and complete: no defaults are invented."""

    schema: int
    id: str
    name: str
    version: str
    description: str
    publisher: str
    license: str
    entry: str
    capabilities: tuple[str, ...]
    tools: tuple[PluginTool, ...]
    skills: tuple[str, ...]
    files: tuple[str, ...]
    checksum: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def denied_capabilities(self) -> tuple[str, ...]:
        return tuple(item for item in self.capabilities if item in DENIED_CAPABILITIES)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "publisher": self.publisher,
            "license": self.license,
            "entry": self.entry,
            "capabilities": list(self.capabilities),
            "denied_capabilities": list(self.denied_capabilities),
            "tools": [tool.to_dict() for tool in self.tools],
            "skills": list(self.skills),
            "checksum": self.checksum,
        }


_ALLOWED_MANIFEST_KEYS = {
    "schema",
    "id",
    "name",
    "version",
    "description",
    "publisher",
    "license",
    "entry",
    "capabilities",
    "tools",
    "skills",
    "files",
    "checksum",
}


def validate_manifest(payload: Any) -> PluginManifest:
    """Validate an untrusted manifest dict and return the frozen contract."""

    if not isinstance(payload, dict):
        raise PluginError("Plugin manifest must be a JSON object.")
    _check_keys(payload, _ALLOWED_MANIFEST_KEYS, where="manifest")

    schema = payload.get("schema", 1)
    if schema != 1:
        raise PluginError(
            f"Plugin manifest schema {schema!r} is not supported; this build understands 1."
        )

    plugin_id = _text(payload.get("id"), field_name="id", limit=MAX_ID)
    if not _ID_PATTERN.match(plugin_id):
        raise PluginError(
            "Plugin id must be 3-64 characters of lower-case letters, digits, '.', "
            "'_' or '-', starting with a letter or digit."
        )
    version = _text(payload.get("version"), field_name="version", limit=MAX_VERSION)
    if not _VERSION_PATTERN.match(version):
        raise PluginError("Plugin version must be a three-part semantic version, e.g. 1.0.0.")

    name = _text(payload.get("name"), field_name="name", limit=MAX_NAME)
    description = _text(
        payload.get("description"), field_name="description", limit=MAX_DESCRIPTION
    )
    publisher = _text(payload.get("publisher"), field_name="publisher", limit=MAX_PUBLISHER)
    license_name = _text(
        payload.get("license", "unspecified"),
        field_name="license",
        limit=MAX_LICENSE,
        allow_empty=True,
    ) or "unspecified"

    entry = safe_relative_path(payload.get("entry", ENTRY_NAME), field_name="entry")
    if not entry.endswith(".py"):
        raise PluginError("Plugin entry must be a .py file inside the bundle.")

    raw_capabilities = payload.get("capabilities", [])
    if not isinstance(raw_capabilities, list):
        raise PluginError("Plugin capabilities must be a list.")
    if len(raw_capabilities) > len(CAPABILITIES):
        raise PluginError(f"A plugin may declare at most {len(CAPABILITIES)} capabilities.")
    capabilities: list[str] = []
    for item in raw_capabilities:
        capability = str(item or "").strip()
        if capability not in CAPABILITIES:
            raise PluginError(
                f"Unknown capability '{capability}'. Allowed: {', '.join(CAPABILITIES)}."
            )
        if capability in capabilities:
            raise PluginError(f"Capability '{capability}' is declared twice.")
        capabilities.append(capability)

    raw_tools = payload.get("tools", [])
    if not isinstance(raw_tools, list):
        raise PluginError("Plugin tools must be a list.")
    if len(raw_tools) > MAX_TOOLS:
        raise PluginError(f"A plugin may declare at most {MAX_TOOLS} tools.")
    tools: list[PluginTool] = []
    seen_tools: set[str] = set()
    for item in raw_tools:
        if not isinstance(item, dict):
            raise PluginError("Each plugin tool must be a JSON object.")
        _check_keys(item, {"name", "description", "arguments"}, where="tool")
        tool_name = _text(item.get("name"), field_name="tool name", limit=64)
        if not _TOOL_PATTERN.match(tool_name):
            raise PluginError(
                f"Tool name '{tool_name}' must be lower snake_case, 3-64 characters."
            )
        if tool_name in seen_tools:
            raise PluginError(f"Tool '{tool_name}' is declared twice.")
        seen_tools.add(tool_name)
        tool_description = _text(
            item.get("description"), field_name="tool description", limit=200
        )
        raw_arguments = item.get("arguments", {})
        if not isinstance(raw_arguments, dict):
            raise PluginError(f"Tool '{tool_name}' arguments must be an object.")
        if len(raw_arguments) > MAX_TOOL_ARGS:
            raise PluginError(f"Tool '{tool_name}' declares too many arguments.")
        arguments: list[tuple[str, str]] = []
        for arg_name, arg_type in raw_arguments.items():
            key = str(arg_name or "").strip()
            if not _TOOL_PATTERN.match(key):
                raise PluginError(f"Tool '{tool_name}' argument '{key}' is not a valid name.")
            if arg_type not in ARGUMENT_TYPES:
                raise PluginError(
                    f"Tool '{tool_name}' argument '{key}' type must be one of "
                    f"{', '.join(ARGUMENT_TYPES)}."
                )
            arguments.append((key, str(arg_type)))
        tools.append(
            PluginTool(
                name=tool_name,
                description=tool_description,
                arguments=tuple(arguments),
            )
        )

    raw_skills = payload.get("skills", [])
    if not isinstance(raw_skills, list):
        raise PluginError("Plugin skills must be a list.")
    if len(raw_skills) > MAX_SKILLS:
        raise PluginError(f"A plugin may ship at most {MAX_SKILLS} skills.")
    skills: list[str] = []
    for item in raw_skills:
        path = safe_relative_path(item, field_name="skill path")
        if not path.endswith(".md"):
            raise PluginError("Plugin skills must be .md files inside the bundle.")
        if path in skills:
            raise PluginError(f"Skill '{path}' is declared twice.")
        skills.append(path)

    raw_files = payload.get("files", [])
    if not isinstance(raw_files, list):
        raise PluginError("Plugin files must be a list.")
    if len(raw_files) > MAX_FILES:
        raise PluginError(f"A plugin may ship at most {MAX_FILES} files.")
    files = [safe_relative_path(item, field_name="file path") for item in raw_files]

    checksum = str(payload.get("checksum") or "").strip().lower()
    if checksum and not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise PluginError("Plugin checksum must be a 64-character lower-case hex digest.")

    for required in (entry, *skills):
        if files and required not in files:
            raise PluginError(
                f"Declared file '{required}' is missing from the manifest file list."
            )

    return PluginManifest(
        schema=1,
        id=plugin_id,
        name=name,
        version=version,
        description=description,
        publisher=publisher,
        license=license_name,
        entry=entry,
        capabilities=tuple(capabilities),
        tools=tuple(tools),
        skills=tuple(skills),
        files=tuple(files),
        checksum=checksum,
        raw=dict(payload),
    )


def parse_public_key(value: Any) -> bytes:
    """Decode a base64 (or hex) 32-byte Ed25519 raw public key."""

    text = str(value or "").strip()
    if not text:
        raise PluginError("Publisher public key must not be empty.")
    raw: bytes | None = None
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raw = None
    if raw is None or len(raw) != 32:
        try:
            candidate = bytes.fromhex(text)
        except ValueError as exc:
            raise PluginError(
                "Publisher public key must be 32 raw bytes as base64 or hex."
            ) from exc
        raw = candidate
    if len(raw) != 32:
        raise PluginError("Publisher public key must be 32 raw bytes.")
    return raw


def verify_signature(
    payload: bytes, signature: Any, public_key: bytes, *, key_id: str = ""
) -> str:
    """Verify a detached Ed25519 signature over ``payload``; return the digest."""

    if not isinstance(signature, dict):
        raise PluginError("Plugin signature file must be a JSON object.")
    _check_keys(
        signature,
        {"algorithm", "key_id", "signature", "payload_sha256", "checksum"},
        where="signature",
    )
    if signature.get("algorithm") != "Ed25519":
        raise PluginError("Plugin signature algorithm must be Ed25519.")
    signature_key_id = str(signature.get("key_id") or "").strip()
    if not signature_key_id:
        raise PluginError("Plugin signature must name the publisher key id.")
    if key_id and signature_key_id != key_id:
        raise PluginError(
            f"Plugin was signed by key '{signature_key_id}', not the trusted key '{key_id}'."
        )
    digest = sha256_hex(payload)
    declared = str(signature.get("payload_sha256") or "").strip().lower()
    if declared and declared != digest:
        raise PluginError("Plugin manifest digest does not match the signed digest.")

    try:
        raw_signature = base64.b64decode(
            str(signature.get("signature") or ""), validate=True
        )
    except (binascii.Error, ValueError) as exc:
        raise PluginError("Plugin signature is not valid base64.") from exc
    if len(raw_signature) != 64:
        raise PluginError("Plugin signature must be a 64-byte Ed25519 signature.")

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(raw_signature, payload)
    except InvalidSignature as exc:
        raise PluginError(
            "Plugin signature verification failed against the configured publisher key."
        ) from exc
    return digest
