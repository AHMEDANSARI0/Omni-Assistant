"""Path boundary helpers shared by policy and (later) tool execution."""

from __future__ import annotations

from pathlib import Path

from ..errors import PolicyViolationError

_SENSITIVE_FILENAMES = frozenset(
    {".env", "credentials", "credentials.json", "id_rsa", "id_ed25519", "known_hosts"}
)
_SENSITIVE_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx"})
_SENSITIVE_PARTS = frozenset({".ssh", ".aws", ".gnupg"})


def _resolved_candidate(base: Path, target: str | Path) -> Path:
    resolved_base = base.expanduser().resolve()
    candidate = Path(target).expanduser()
    if not candidate.is_absolute():
        candidate = resolved_base / candidate
    return candidate.resolve()


def is_within(base: Path, target: str | Path) -> bool:
    resolved_base = base.expanduser().resolve()
    return _resolved_candidate(base, target).is_relative_to(resolved_base)


def resolve_within(base: Path, target: str | Path) -> Path:
    """Resolve target under base, rejecting traversal outside the base directory."""
    resolved = _resolved_candidate(base, target)
    resolved_base = base.expanduser().resolve()
    if not resolved.is_relative_to(resolved_base):
        raise PolicyViolationError(
            f"Path '{target}' is outside the allowed directory '{resolved_base}'."
        )
    return resolved


def is_sensitive_path(target: str | Path) -> bool:
    path = Path(target)
    name = path.name.lower()
    if name in _SENSITIVE_FILENAMES or name.startswith(".env."):
        return True
    if path.suffix.lower() in _SENSITIVE_SUFFIXES:
        return True
    return any(part.lower() in _SENSITIVE_PARTS for part in path.parts)
