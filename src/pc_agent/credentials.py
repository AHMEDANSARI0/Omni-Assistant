"""Provider API-key storage backed by native OS credential stores.

Secrets are deliberately handled only as return values from this module.
Status objects expose provider names and backend availability, never key
material. The Windows backend talks directly to ``advapi32`` via
``ctypes``; the Linux backend talks to the freedesktop Secret Service
through the system ``secret-tool`` executable; and the macOS backend uses
Apple's ``security`` executable in interactive stdin mode for writes. In
both process-backed stores, a secret is never passed as an argv value. No
third-party credential dependency is required. Unsupported hosts keep
using environment/``.env`` values.
"""

from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
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
_SECRET_SERVICE_APPLICATION = "self-healing-pc-agent"
_SECRET_SERVICE_PURPOSE = "provider-api-key"
_SECRET_SERVICE_PROBE_PROVIDER = "__pc_agent_probe__"
_SECRET_TOOL_TIMEOUT_SECONDS = 10.0
_SECRET_TOOL_MAX_STDOUT_BYTES = 8192
_MACOS_SECURITY_EXECUTABLE = "/usr/bin/security"
_MACOS_KEYCHAIN_SERVICE = "self-healing-pc-agent-provider-api-key"
_MACOS_KEYCHAIN_VALUE_PREFIX = b"pc-agent-v1:"
_MACOS_KEYCHAIN_MAX_INTERACTIVE_LINE_BYTES = 4000
_MACOS_NOT_FOUND_MARKERS = (
    b"specified item could not be found",
    b"errsecitemnotfound",
    b"-25300",
)
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


_SecretToolRunner = Callable[
    [Sequence[str], bytes | None, float], subprocess.CompletedProcess[bytes]
]


def _run_secret_tool_process(
    argv: Sequence[str],
    input_data: bytes | None,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(argv),
        input=input_data,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )


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


class LinuxSecretServiceBackend:
    """Provider keys in the current user's freedesktop Secret Service.

    The system ``secret-tool`` program is invoked as an argv array (never a
    shell). Stored secrets are supplied on stdin and lookup stderr is never
    copied into exceptions, status objects, or logs.
    """

    name = "linux-secret-service"

    def __init__(
        self,
        *,
        executable: str | Path | None = None,
        command_prefix: Sequence[str] | None = None,
        is_linux: bool | None = None,
        runner: _SecretToolRunner | None = None,
        timeout_seconds: float = _SECRET_TOOL_TIMEOUT_SECONDS,
    ) -> None:
        if executable is not None and command_prefix is not None:
            raise CredentialError(
                "Specify only one Linux Secret Service command form."
            )
        if not 0 < timeout_seconds <= 60:
            raise CredentialError(
                "The Linux Secret Service timeout must be between 0 and 60 seconds."
            )
        self._executable = str(executable) if executable is not None else None
        self._command_prefix = (
            tuple(str(part) for part in command_prefix)
            if command_prefix is not None
            else None
        )
        if self._command_prefix is not None and not self._command_prefix:
            raise CredentialError("The Linux Secret Service command is empty.")
        self._is_linux = (
            sys.platform.startswith("linux") if is_linux is None else is_linux
        )
        self._runner = runner or _run_secret_tool_process
        self._timeout_seconds = float(timeout_seconds)
        self._probe_cache: tuple[bool, str] | None = None

    def probe(self) -> tuple[bool, str]:
        if self._probe_cache is not None:
            return self._probe_cache
        if not self._is_linux:
            result = (False, "Linux Secret Service is available only on Linux")
            self._probe_cache = result
            return result
        try:
            # A search for an intentionally unsupported provider proves the
            # Secret Service is reachable without reading a real secret.
            completed = self._run(
                (
                    "search",
                    "application",
                    _SECRET_SERVICE_APPLICATION,
                    "provider",
                    _SECRET_SERVICE_PROBE_PROVIDER,
                    "purpose",
                    _SECRET_SERVICE_PURPOSE,
                )
            )
        except CredentialError as exc:
            result = (False, str(exc))
            self._probe_cache = result
            return result
        if completed.returncode == 0:
            result = (True, "Linux Secret Service via secret-tool (libsecret)")
        else:
            result = (False, "Linux Secret Service is unavailable or locked")
        self._probe_cache = result
        return result

    def get(self, provider: str) -> str | None:
        canonical = canonical_credential_provider(provider)
        self._require_available()
        completed = self._run(("lookup", *self._attributes(canonical)))
        if completed.returncode == 0:
            if len(completed.stdout) > _SECRET_TOOL_MAX_STDOUT_BYTES:
                raise CredentialError(
                    "The stored Linux Secret Service credential has an invalid size."
                )
            if not completed.stdout:
                return None
            try:
                decoded = completed.stdout.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CredentialError(
                    "The stored Linux Secret Service credential is not valid UTF-8 text."
                ) from exc
            return _validate_secret(decoded)
        if completed.returncode == 1 and not completed.stderr:
            # libsecret's secret-tool returns 1 with empty stderr when no
            # item matches; service errors write a diagnostic to stderr.
            return None
        raise CredentialError(
            f"Linux Secret Service could not read the {canonical} API key."
        )

    def set(self, provider: str, secret: str) -> None:
        canonical = canonical_credential_provider(provider)
        cleaned = _validate_secret(secret)
        self._require_available()
        completed = self._run(
            (
                "store",
                f"--label=Self-Healing PC Agent provider API key ({canonical})",
                *self._attributes(canonical),
            ),
            input_data=cleaned.encode("utf-8"),
        )
        if completed.returncode != 0:
            raise CredentialError(
                f"Linux Secret Service could not store the {canonical} API key."
            )
        if self.get(canonical) != cleaned:
            raise CredentialError(
                f"Linux Secret Service did not verify the stored {canonical} API key."
            )

    def delete(self, provider: str) -> bool:
        canonical = canonical_credential_provider(provider)
        self._require_available()
        if self.get(canonical) is None:
            return False
        completed = self._run(("clear", *self._attributes(canonical)))
        if completed.returncode != 0 and completed.stderr:
            raise CredentialError(
                f"Linux Secret Service could not delete the {canonical} API key."
            )
        if self.get(canonical) is not None:
            raise CredentialError(
                f"Linux Secret Service could not delete the {canonical} API key."
            )
        return True

    @staticmethod
    def _attributes(provider: str) -> tuple[str, ...]:
        canonical = canonical_credential_provider(provider)
        return (
            "application",
            _SECRET_SERVICE_APPLICATION,
            "provider",
            canonical,
            "purpose",
            _SECRET_SERVICE_PURPOSE,
        )

    def _require_available(self) -> None:
        available, detail = self.probe()
        if not available:
            raise CredentialError(f"Credential storage is unavailable: {detail}.")

    def _command_prefix_resolved(self) -> tuple[str, ...]:
        if self._command_prefix is not None:
            return self._command_prefix
        if self._executable is not None:
            return (self._executable,)
        found = shutil.which("secret-tool")
        if found is None:
            raise CredentialError("secret-tool (libsecret) was not found")
        return (found,)

    def _run(
        self,
        arguments: Sequence[str],
        *,
        input_data: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        argv = (*self._command_prefix_resolved(), *arguments)
        try:
            return self._runner(argv, input_data, self._timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise CredentialError("Linux Secret Service operation timed out.") from exc
        except OSError as exc:
            raise CredentialError(
                "Linux Secret Service executable could not be started."
            ) from exc


class MacOSKeychainBackend:
    """Provider keys in the current user's macOS Keychain.

    Apple's ``security`` executable creates and owns the generic-password
    items. Writes use ``security -q -i`` with one bounded stdin command and
    a hexadecimal ``-X`` value, so neither plaintext nor its encoding is
    present in the child process argv. Error output is never copied into an
    exception or status object.
    """

    name = "macos-keychain"

    def __init__(
        self,
        *,
        executable: str | Path | None = None,
        command_prefix: Sequence[str] | None = None,
        is_macos: bool | None = None,
        runner: _SecretToolRunner | None = None,
        timeout_seconds: float = _SECRET_TOOL_TIMEOUT_SECONDS,
    ) -> None:
        if executable is not None and command_prefix is not None:
            raise CredentialError("Specify only one macOS Keychain command form.")
        if not 0 < timeout_seconds <= 60:
            raise CredentialError(
                "The macOS Keychain timeout must be between 0 and 60 seconds."
            )
        self._executable = str(executable) if executable is not None else None
        self._command_prefix = (
            tuple(str(part) for part in command_prefix)
            if command_prefix is not None
            else None
        )
        if self._command_prefix is not None and not self._command_prefix:
            raise CredentialError("The macOS Keychain command is empty.")
        self._is_macos = sys.platform == "darwin" if is_macos is None else is_macos
        self._runner = runner or _run_secret_tool_process
        self._timeout_seconds = float(timeout_seconds)
        self._probe_cache: tuple[bool, str] | None = None

    def probe(self) -> tuple[bool, str]:
        if self._probe_cache is not None:
            return self._probe_cache
        if not self._is_macos:
            result = (False, "macOS Keychain is available only on macOS")
            self._probe_cache = result
            return result
        try:
            completed = self._run(("list-keychains",))
        except CredentialError as exc:
            result = (False, str(exc))
            self._probe_cache = result
            return result
        if completed.returncode == 0:
            result = (True, "macOS Keychain via /usr/bin/security")
        else:
            result = (False, "macOS Keychain is unavailable or locked")
        self._probe_cache = result
        return result

    def get(self, provider: str) -> str | None:
        canonical = canonical_credential_provider(provider)
        self._require_available()
        completed = self._run(
            (
                "find-generic-password",
                "-a",
                canonical,
                "-s",
                _MACOS_KEYCHAIN_SERVICE,
                "-w",
            )
        )
        if completed.returncode == 0:
            if len(completed.stdout) > _SECRET_TOOL_MAX_STDOUT_BYTES:
                raise CredentialError(
                    "The stored macOS Keychain credential has an invalid size."
                )
            payload = self._decode_stored_value(completed.stdout)
            try:
                decoded = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CredentialError(
                    "The stored macOS Keychain credential is not valid UTF-8 text."
                ) from exc
            return _validate_secret(decoded)
        if self._is_not_found(completed.stderr):
            return None
        raise CredentialError(
            f"macOS Keychain could not read the {canonical} API key."
        )

    def set(self, provider: str, secret: str) -> None:
        canonical = canonical_credential_provider(provider)
        cleaned = _validate_secret(secret)
        self._require_available()
        payload = _MACOS_KEYCHAIN_VALUE_PREFIX + cleaned.encode("utf-8")
        command = (
            "add-generic-password -U "
            f"-a {canonical} -s {_MACOS_KEYCHAIN_SERVICE} -X {payload.hex()}"
        )
        command_bytes = command.encode("ascii")
        if len(command_bytes) >= _MACOS_KEYCHAIN_MAX_INTERACTIVE_LINE_BYTES:
            raise CredentialError(
                "The API key is too large for the macOS Keychain command transport."
            )
        completed = self._run(("-q", "-i"), input_data=command_bytes + b"\n")
        if completed.returncode != 0 or completed.stderr:
            # Interactive security(1) can report an inner-command failure on
            # stderr while still exiting zero. Do not surface that output.
            raise CredentialError(
                f"macOS Keychain could not store the {canonical} API key."
            )
        if self.get(canonical) != cleaned:
            raise CredentialError(
                f"macOS Keychain did not verify the stored {canonical} API key."
            )

    def delete(self, provider: str) -> bool:
        canonical = canonical_credential_provider(provider)
        self._require_available()
        if self.get(canonical) is None:
            return False
        self._run(
            (
                "delete-generic-password",
                "-a",
                canonical,
                "-s",
                _MACOS_KEYCHAIN_SERVICE,
            )
        )
        if self.get(canonical) is not None:
            raise CredentialError(
                f"macOS Keychain could not delete the {canonical} API key."
            )
        return True

    @staticmethod
    def _decode_stored_value(output: bytes) -> bytes:
        raw = output
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        if raw.startswith(_MACOS_KEYCHAIN_VALUE_PREFIX):
            return raw[len(_MACOS_KEYCHAIN_VALUE_PREFIX) :]

        candidate = raw[2:] if raw[:2].lower() == b"0x" else raw
        if (
            candidate
            and len(candidate) % 2 == 0
            and all(
                byte in b"0123456789abcdefABCDEF" for byte in candidate
            )
        ):
            decoded = bytes.fromhex(candidate.decode("ascii"))
            if decoded.startswith(_MACOS_KEYCHAIN_VALUE_PREFIX):
                return decoded[len(_MACOS_KEYCHAIN_VALUE_PREFIX) :]
        raise CredentialError(
            "The stored macOS Keychain credential has an unsupported format."
        )

    @staticmethod
    def _is_not_found(stderr: bytes) -> bool:
        lowered = stderr.lower()
        return any(marker in lowered for marker in _MACOS_NOT_FOUND_MARKERS)

    def _require_available(self) -> None:
        available, detail = self.probe()
        if not available:
            raise CredentialError(f"Credential storage is unavailable: {detail}.")

    def _command_prefix_resolved(self) -> tuple[str, ...]:
        if self._command_prefix is not None:
            return self._command_prefix
        if self._executable is not None:
            return (self._executable,)
        return (_MACOS_SECURITY_EXECUTABLE,)

    def _run(
        self,
        arguments: Sequence[str],
        *,
        input_data: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        argv = (*self._command_prefix_resolved(), *arguments)
        try:
            return self._runner(argv, input_data, self._timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise CredentialError("macOS Keychain operation timed out.") from exc
        except OSError as exc:
            raise CredentialError(
                "macOS Keychain executable could not be started."
            ) from exc


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
    def source_name(self) -> str:
        """Stable non-secret label used for an OS-stored effective key."""

        if self._backend.name in {
            LinuxSecretServiceBackend.name,
            MacOSKeychainBackend.name,
        }:
            return self._backend.name
        return WindowsCredentialBackend.name

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
    """Build a store for ``auto``, ``windows``, ``linux``, ``macos`` or ``env``."""

    cleaned_mode = mode.strip().lower() or "auto"
    if cleaned_mode not in {"auto", "windows", "linux", "macos", "env"}:
        raise CredentialError(
            "Credential store mode must be one of: auto, windows, linux, macos, env."
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
    if cleaned_mode == "windows":
        return CredentialStore(WindowsCredentialBackend(), mode=cleaned_mode)
    if cleaned_mode == "linux":
        return CredentialStore(LinuxSecretServiceBackend(), mode=cleaned_mode)
    if cleaned_mode == "macos":
        return CredentialStore(MacOSKeychainBackend(), mode=cleaned_mode)
    if sys.platform.startswith("win"):
        return CredentialStore(WindowsCredentialBackend(), mode=cleaned_mode)
    if sys.platform.startswith("linux"):
        return CredentialStore(LinuxSecretServiceBackend(), mode=cleaned_mode)
    if sys.platform == "darwin":
        return CredentialStore(MacOSKeychainBackend(), mode=cleaned_mode)
    return CredentialStore(
        UnavailableCredentialBackend(
            name="unsupported-platform",
            detail="OS credential storage is not implemented on this platform",
        ),
        mode=cleaned_mode,
    )
