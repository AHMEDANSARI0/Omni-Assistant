"""Provider API-key storage backed by Windows Credential Manager.

Secrets are deliberately handled only as return values from this module.
Status objects expose provider names and backend availability, never key
material.  The native backend talks directly to Windows ``advapi32`` via
``ctypes``; no third-party credential dependency is required.  On
non-Windows hosts the backend reports itself unavailable and configuration
keeps using environment/``.env`` values.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import CredentialError

SUPPORTED_CREDENTIAL_PROVIDERS = (
    "gemini",
    "openrouter",
    "openai",
    "openai-compatible",
    "ollama",
    "anthropic",
)

_PROVIDER_ALIASES = {
    "openai_compatible": "openai-compatible",
    "openai-compatible": "openai-compatible",
}

_CREDENTIAL_TARGET_PREFIX = "Self-Healing PC Agent/provider-api-key"
_CREDENTIAL_COMMENT = "Self-Healing PC Agent provider API key"
_MAX_SECRET_CHARS = 1024
_MAX_SECRET_BLOB_BYTES = 2560  # CRED_MAX_CREDENTIAL_BLOB_SIZE (5 * 512)

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168

# Use fixed-width Win32 aliases: ctypes.wintypes maps DWORD to C ``long``,
# which is 8 bytes on LP64 Unix test hosts but 4 bytes in the Windows ABI.
_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32


def canonical_credential_provider(provider: str) -> str:
    """Return the canonical provider name used as a credential target."""

    cleaned = provider.strip().lower()
    cleaned = _PROVIDER_ALIASES.get(cleaned, cleaned)
    if cleaned not in SUPPORTED_CREDENTIAL_PROVIDERS:
        allowed = ", ".join(SUPPORTED_CREDENTIAL_PROVIDERS)
        raise CredentialError(
            f"Unsupported credential provider {provider!r}. Allowed: {allowed}."
        )
    return cleaned


def credential_target_name(provider: str) -> str:
    return f"{_CREDENTIAL_TARGET_PREFIX}/{canonical_credential_provider(provider)}"


def _validate_secret(secret: str) -> str:
    cleaned = secret.strip()
    if not cleaned:
        raise CredentialError("An API key must not be empty.")
    if len(cleaned) > _MAX_SECRET_CHARS:
        raise CredentialError(
            f"An API key must be at most {_MAX_SECRET_CHARS} characters."
        )
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in cleaned):
        raise CredentialError(
            "An API key must be one line and must not contain whitespace."
        )
    if len(cleaned.encode("utf-16-le")) > _MAX_SECRET_BLOB_BYTES:
        raise CredentialError("An API key is too large for Windows Credential Manager.")
    return cleaned


@dataclass(frozen=True, slots=True)
class CredentialStoreStatus:
    """Non-secret credential-store status safe for doctor/CLI output."""

    mode: str
    backend: str
    available: bool
    detail: str
    stored_providers: tuple[str, ...] = ()

    def summary(self) -> str:
        return (
            f"mode={self.mode}; backend={self.backend}; "
            f"available={'yes' if self.available else 'no'}; "
            f"stored={', '.join(self.stored_providers) or 'none'}"
            + (f"; {self.detail}" if self.detail and not self.available else "")
        )


class CredentialBackend(Protocol):
    name: str

    def probe(self) -> tuple[bool, str]: ...

    def get(self, provider: str) -> str | None: ...

    def set(self, provider: str, secret: str) -> None: ...

    def delete(self, provider: str) -> bool: ...


class InMemoryCredentialBackend:
    """Deterministic backend for tests and dependency injection only."""

    name = "memory-test-double"

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}

    def probe(self) -> tuple[bool, str]:
        return True, "in-memory test backend"

    def get(self, provider: str) -> str | None:
        return self._secrets.get(canonical_credential_provider(provider))

    def set(self, provider: str, secret: str) -> None:
        self._secrets[canonical_credential_provider(provider)] = secret

    def delete(self, provider: str) -> bool:
        return self._secrets.pop(canonical_credential_provider(provider), None) is not None


class UnavailableCredentialBackend:
    """Backend used when configuration intentionally selects env-only keys."""

    def __init__(self, *, name: str = "environment-only", detail: str = "") -> None:
        self.name = name
        self._detail = detail or "OS credential storage is not selected"

    def probe(self) -> tuple[bool, str]:
        return False, self._detail

    def get(self, provider: str) -> str | None:
        canonical_credential_provider(provider)
        return None

    def set(self, provider: str, secret: str) -> None:
        canonical_credential_provider(provider)
        raise CredentialError(f"Credential storage is unavailable: {self._detail}.")

    def delete(self, provider: str) -> bool:
        canonical_credential_provider(provider)
        raise CredentialError(f"Credential storage is unavailable: {self._detail}.")


class _FileTime(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", _DWORD),
        ("dwHighDateTime", _DWORD),
    ]


class _CredentialW(ctypes.Structure):
    """Win32 ``CREDENTIALW`` from wincred.h (pointers kept opaque)."""

    _fields_ = [
        ("Flags", _DWORD),
        ("Type", _DWORD),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FileTime),
        ("CredentialBlobSize", _DWORD),
        ("CredentialBlob", ctypes.c_void_p),
        ("Persist", _DWORD),
        ("AttributeCount", _DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


class WindowsCredentialBackend:
    """Generic credentials in the current user's Windows credential store."""

    name = "windows-credential-manager"

    def __init__(
        self,
        *,
        library: Any | None = None,
        is_windows: bool | None = None,
    ) -> None:
        self._library = library
        self._library_injected = library is not None
        self._is_windows = (
            sys.platform.startswith("win") if is_windows is None else is_windows
        )

    def probe(self) -> tuple[bool, str]:
        try:
            self._load_library()
        except CredentialError as exc:
            return False, str(exc)
        return True, "Windows Credential Manager (generic credentials)"

    def _reset_last_error(self) -> None:
        setter = getattr(ctypes, "set_last_error", None)
        if callable(setter):
            setter(0)
        if self._library is not None and hasattr(self._library, "last_error"):
            self._library.last_error = 0

    def _last_error(self) -> int:
        # An injected library is a complete Win32 test double: its own
        # last-error value is authoritative.  On a real Windows host,
        # ctypes.get_last_error() also exists for the (uninjected)
        # advapi32 handle, so checking ctypes first would incorrectly
        # return 0 for a simulated ERROR_NOT_FOUND.
        if (
            self._library_injected
            and self._library is not None
            and hasattr(self._library, "last_error")
        ):
            return int(getattr(self._library, "last_error", 0) or 0)
        getter = getattr(ctypes, "get_last_error", None)
        if callable(getter):
            return int(getter())
        return int(getattr(self._library, "last_error", 0) or 0)

    def get(self, provider: str) -> str | None:
        canonical = canonical_credential_provider(provider)
        library = self._load_library()
        pointer = ctypes.POINTER(_CredentialW)()
        self._reset_last_error()
        ok = library.CredReadW(
            credential_target_name(canonical),
            _CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        )
        if not ok:
            code = self._last_error()
            if code == _ERROR_NOT_FOUND:
                return None
            raise CredentialError(
                "Windows Credential Manager could not read the "
                f"{canonical} API key (Win32 error {code})."
            )
        if not pointer:
            raise CredentialError(
                "Windows Credential Manager returned no credential record."
            )
        try:
            credential = pointer.contents
            size = int(credential.CredentialBlobSize)
            if size < 0 or size > _MAX_SECRET_BLOB_BYTES:
                raise CredentialError(
                    "The stored Windows credential has an invalid size."
                )
            if size == 0 or not credential.CredentialBlob:
                return None
            raw = ctypes.string_at(credential.CredentialBlob, size)
        finally:
            library.CredFree(ctypes.cast(pointer, ctypes.c_void_p))
        try:
            decoded = raw.decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise CredentialError(
                "The stored Windows credential is not valid UTF-16 text."
            ) from exc
        return _validate_secret(decoded.rstrip("\x00"))

    def set(self, provider: str, secret: str) -> None:
        canonical = canonical_credential_provider(provider)
        library = self._load_library()
        blob = secret.encode("utf-16-le")
        blob_buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        target_buffer = ctypes.create_unicode_buffer(credential_target_name(canonical))
        comment_buffer = ctypes.create_unicode_buffer(_CREDENTIAL_COMMENT)
        username_buffer = ctypes.create_unicode_buffer(canonical)

        credential = _CredentialW()
        credential.Flags = 0
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = ctypes.cast(target_buffer, ctypes.c_wchar_p).value
        credential.Comment = ctypes.cast(comment_buffer, ctypes.c_wchar_p).value
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(blob_buffer, ctypes.c_void_p)
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.AttributeCount = 0
        credential.Attributes = None
        credential.TargetAlias = None
        credential.UserName = ctypes.cast(username_buffer, ctypes.c_wchar_p).value

        self._reset_last_error()
        ok = library.CredWriteW(ctypes.byref(credential), 0)
        if not ok:
            code = self._last_error()
            raise CredentialError(
                "Windows Credential Manager could not store the "
                f"{canonical} API key (Win32 error {code})."
            )

    def delete(self, provider: str) -> bool:
        canonical = canonical_credential_provider(provider)
        library = self._load_library()
        self._reset_last_error()
        ok = library.CredDeleteW(
            credential_target_name(canonical), _CRED_TYPE_GENERIC, 0
        )
        if ok:
            return True
        code = self._last_error()
        if code == _ERROR_NOT_FOUND:
            return False
        raise CredentialError(
            "Windows Credential Manager could not delete the "
            f"{canonical} API key (Win32 error {code})."
        )

    def _load_library(self) -> Any:
        if self._library is not None:
            return self._library
        if not self._is_windows:
            raise CredentialError(
                "Windows Credential Manager is available only on Windows"
            )
        try:
            library = ctypes.WinDLL("advapi32.dll", use_last_error=True)
        except (AttributeError, OSError) as exc:
            raise CredentialError(
                "Windows Credential Manager could not be loaded "
                f"({exc.__class__.__name__})"
            ) from exc
        self._configure_library(library)
        self._library = library
        return library

    @staticmethod
    def _configure_library(library: Any) -> None:
        library.CredReadW.argtypes = [
            ctypes.c_wchar_p,
            _DWORD,
            _DWORD,
            ctypes.POINTER(ctypes.POINTER(_CredentialW)),
        ]
        library.CredReadW.restype = _BOOL
        library.CredWriteW.argtypes = [
            ctypes.POINTER(_CredentialW),
            _DWORD,
        ]
        library.CredWriteW.restype = _BOOL
        library.CredDeleteW.argtypes = [
            ctypes.c_wchar_p,
            _DWORD,
            _DWORD,
        ]
        library.CredDeleteW.restype = _BOOL
        library.CredFree.argtypes = [ctypes.c_void_p]
        library.CredFree.restype = None


class CredentialStore:
    """Validated provider-key operations over one credential backend."""

    def __init__(
        self,
        backend: CredentialBackend,
        *,
        mode: str = "auto",
    ) -> None:
        self._backend = backend
        self.mode = mode.strip().lower() or "auto"

    @property
    def backend_name(self) -> str:
        return self._backend.name

    @property
    def available(self) -> bool:
        return self._backend.probe()[0]

    def status(self, *, include_stored: bool = True) -> CredentialStoreStatus:
        available, detail = self._backend.probe()
        stored = self.configured_providers() if available and include_stored else ()
        return CredentialStoreStatus(
            mode=self.mode,
            backend=self.backend_name,
            available=available,
            detail=detail,
            stored_providers=stored,
        )

    def get_api_key(self, provider: str) -> str | None:
        canonical = canonical_credential_provider(provider)
        if not self.available:
            return None
        value = self._backend.get(canonical)
        return _validate_secret(value) if value is not None else None

    def contains(self, provider: str) -> bool:
        return self.get_api_key(provider) is not None

    def configured_providers(
        self, providers: tuple[str, ...] = SUPPORTED_CREDENTIAL_PROVIDERS
    ) -> tuple[str, ...]:
        if not self.available:
            return ()
        return tuple(
            canonical
            for canonical in (
                canonical_credential_provider(provider) for provider in providers
            )
            if self.get_api_key(canonical) is not None
        )

    def set_api_key(self, provider: str, secret: str) -> str:
        canonical = canonical_credential_provider(provider)
        cleaned = _validate_secret(secret)
        self._require_available()
        self._backend.set(canonical, cleaned)
        return canonical

    def delete_api_key(self, provider: str) -> bool:
        canonical = canonical_credential_provider(provider)
        self._require_available()
        return self._backend.delete(canonical)

    def _require_available(self) -> None:
        available, detail = self._backend.probe()
        if not available:
            raise CredentialError(f"Credential storage is unavailable: {detail}.")


def build_credential_store(
    mode: str = "auto",
    *,
    backend: CredentialBackend | None = None,
) -> CredentialStore:
    """Build a store for ``auto``, ``windows`` or ``env`` mode."""

    cleaned_mode = mode.strip().lower() or "auto"
    if cleaned_mode not in {"auto", "windows", "env"}:
        raise CredentialError(
            "Credential store mode must be one of: auto, windows, env."
        )
    if backend is not None:
        return CredentialStore(backend, mode=cleaned_mode)
    if cleaned_mode == "env":
        return CredentialStore(
            UnavailableCredentialBackend(
                detail="environment/.env only (PC_AGENT_CREDENTIAL_STORE=env)"
            ),
            mode=cleaned_mode,
        )
    return CredentialStore(WindowsCredentialBackend(), mode=cleaned_mode)
