"""Environment-based configuration.

Values come from real environment variables first, then an optional `.env`
file in the application root. Secrets are never printed: API key fields are
excluded from repr and from the public summary.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .errors import ConfigError

_ALLOWED_ENVIRONMENTS = frozenset({"development", "test", "production"})
_ALLOWED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_ALLOWED_LICENSE_ENFORCEMENT = frozenset({"off", "warn", "enforce"})
# Provider slots understood by the replaceable provider-abstraction layer.
# `providers list` says which slots have a direct adapter in this build.
_ALLOWED_PROVIDERS = frozenset(
    {
        "mock",
        "gemini",
        "openrouter",
        "openai",
        "openai-compatible",
        "ollama",
        "anthropic",
    }
)

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_OPENROUTER_MODEL = "openrouter/auto"
DEFAULT_OPENAI_MODEL = "gpt-5-mini"
DEFAULT_OLLAMA_MODEL = "llama3.1"

DEFAULT_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"


def load_dotenv_values(path: Path) -> dict[str, str]:
    """Parse a minimal KEY=VALUE .env file. No interpolation is performed."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            raise ConfigError(
                f"Invalid .env entry at {path.name}:{line_number} (expected KEY=VALUE)."
            )
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _resolve_from(root: Path, raw: str | Path) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


@dataclass(slots=True, kw_only=True)
class Settings:
    root_dir: Path
    environment: str = "development"
    workspace_dir: Path
    data_dir: Path
    database_path: Path
    log_level: str = "INFO"
    provider_order: tuple[str, ...] = ()
    provider_timeout_seconds: float = 30.0
    max_retries: int = 2
    max_recovery_attempts: int = 2
    recovery_backoff_seconds: float = 0.25
    recovery_stale_after_minutes: int = 10
    web_timeout_seconds: float = 15.0
    web_max_bytes: int = 2_000_000
    # Phase 6 licensing. Enforcement defaults to "off" so development and
    # existing installs keep working; commercial builds opt in explicitly.
    license_enforcement: str = "off"
    license_file: Path = field(default_factory=lambda: Path("data") / "license.key")
    license_public_key: str | None = field(default=None, repr=False)
    license_key_id: str = "default"
    trial_days: int = 14
    gemini_api_key: str | None = field(default=None, repr=False)
    openrouter_api_key: str | None = field(default=None, repr=False)
    openai_api_key: str | None = field(default=None, repr=False)
    openai_compatible_api_key: str | None = field(default=None, repr=False)
    ollama_api_key: str | None = field(default=None, repr=False)
    anthropic_api_key: str | None = field(default=None, repr=False)
    gemini_model: str = DEFAULT_GEMINI_MODEL
    openrouter_model: str = DEFAULT_OPENROUTER_MODEL
    openai_model: str = DEFAULT_OPENAI_MODEL
    openai_compatible_model: str = ""
    ollama_model: str = DEFAULT_OLLAMA_MODEL
    anthropic_model: str = ""
    gemini_base_url: str = DEFAULT_GEMINI_BASE_URL
    openrouter_base_url: str = DEFAULT_OPENROUTER_BASE_URL
    openai_base_url: str = DEFAULT_OPENAI_BASE_URL
    openai_compatible_base_url: str = ""
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL
    anthropic_base_url: str = DEFAULT_ANTHROPIC_BASE_URL

    @property
    def providers_with_credentials(self) -> tuple[str, ...]:
        """Providers from provider_order for which a key is present (names only)."""
        keys = {
            "gemini": self.gemini_api_key,
            "openrouter": self.openrouter_api_key,
            "openai": self.openai_api_key,
            "openai-compatible": self.openai_compatible_api_key,
            "ollama": self.ollama_api_key,
            "anthropic": self.anthropic_api_key,
        }
        return tuple(name for name in self.provider_order if keys.get(name))

    def ensure_directories(self) -> None:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def public_summary(self) -> dict[str, str]:
        """Non-secret values safe to show in CLI output and logs."""
        configured = ", ".join(self.providers_with_credentials) or "none"
        return {
            "environment": self.environment,
            "root": str(self.root_dir),
            "workspace": str(self.workspace_dir),
            "database": str(self.database_path),
            "log_level": self.log_level,
            "provider_order": ", ".join(self.provider_order) or "mock (default)",
            "providers_with_credentials": configured,
            "max_recovery_attempts": str(self.max_recovery_attempts),
            "recovery_stale_after_minutes": str(self.recovery_stale_after_minutes),
            "web_timeout_seconds": str(self.web_timeout_seconds),
            "web_max_bytes": str(self.web_max_bytes),
            "license_enforcement": self.license_enforcement,
            "license_public_key_configured": "yes" if self.license_public_key else "no",
            "license_file": str(self.license_file),
            "trial_days": str(self.trial_days),
        }

    @classmethod
    def from_env(
        cls,
        root_dir: str | Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        load_dotenv: bool = True,
    ) -> Settings:
        real_environ: Mapping[str, str] = os.environ if environ is None else environ
        root_candidate: str | Path
        if root_dir is not None:
            root_candidate = root_dir
        elif real_environ.get("PC_AGENT_HOME"):
            root_candidate = real_environ["PC_AGENT_HOME"]
        else:
            root_candidate = Path.cwd()
        root = Path(root_candidate).expanduser().resolve()

        values: dict[str, str] = {}
        if load_dotenv:
            values.update(load_dotenv_values(root / ".env"))
        values.update({key: str(value) for key, value in real_environ.items()})

        def get(name: str, default: str = "") -> str:
            return values.get(name, default).strip()

        environment = get("PC_AGENT_ENVIRONMENT", "development").lower()
        if environment not in _ALLOWED_ENVIRONMENTS:
            raise ConfigError(
                "PC_AGENT_ENVIRONMENT must be one of: development, test, production."
            )

        log_level = get("PC_AGENT_LOG_LEVEL", "INFO").upper()
        if log_level not in _ALLOWED_LOG_LEVELS:
            raise ConfigError(
                "PC_AGENT_LOG_LEVEL must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL."
            )

        workspace_dir = _resolve_from(root, get("PC_AGENT_WORKSPACE_DIR", "workspace"))
        data_dir = _resolve_from(root, get("PC_AGENT_DATA_DIR", "data"))
        raw_database = get("PC_AGENT_DATABASE_PATH")
        database_path = (
            _resolve_from(root, raw_database) if raw_database else data_dir / "pc_agent.db"
        )

        provider_order = _parse_provider_order(get("PC_AGENT_PROVIDER_ORDER"))
        timeout = _parse_timeout(get("PC_AGENT_PROVIDER_TIMEOUT_SECONDS", "30"))
        max_retries = _parse_max_retries(get("PC_AGENT_MAX_RETRIES", "2"))
        max_recovery_attempts = _parse_max_recovery_attempts(
            get("PC_AGENT_MAX_RECOVERY_ATTEMPTS", "2")
        )
        recovery_backoff_seconds = _parse_recovery_backoff(
            get("PC_AGENT_RECOVERY_BACKOFF_SECONDS", "0.25")
        )
        recovery_stale_after_minutes = _parse_stale_after_minutes(
            get("PC_AGENT_RECOVERY_STALE_AFTER_MINUTES", "10")
        )
        web_timeout_seconds = _parse_web_timeout(
            get("PC_AGENT_WEB_TIMEOUT_SECONDS", "15")
        )
        web_max_bytes = _parse_web_max_bytes(get("PC_AGENT_WEB_MAX_BYTES", "2000000"))

        license_enforcement = get("PC_AGENT_LICENSE_ENFORCEMENT", "off").lower()
        if license_enforcement not in _ALLOWED_LICENSE_ENFORCEMENT:
            raise ConfigError(
                "PC_AGENT_LICENSE_ENFORCEMENT must be one of: off, warn, enforce."
            )
        raw_license_file = get("PC_AGENT_LICENSE_FILE")
        license_file = (
            _resolve_from(root, raw_license_file)
            if raw_license_file
            else data_dir / "license.key"
        )
        license_public_key = _parse_license_public_key(get("PC_AGENT_LICENSE_PUBLIC_KEY"))
        license_key_id = _parse_license_key_id(
            get("PC_AGENT_LICENSE_KEY_ID", "default")
        )
        trial_days = _parse_trial_days(get("PC_AGENT_TRIAL_DAYS", "14"))

        gemini_model = _parse_model(
            "PC_AGENT_GEMINI_MODEL", get("PC_AGENT_GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        )
        openrouter_model = _parse_model(
            "PC_AGENT_OPENROUTER_MODEL",
            get("PC_AGENT_OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL),
        )
        openai_model = _parse_model(
            "PC_AGENT_OPENAI_MODEL", get("PC_AGENT_OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
        )
        openai_compatible_model = _parse_model(
            "PC_AGENT_OPENAI_COMPATIBLE_MODEL",
            get("PC_AGENT_OPENAI_COMPATIBLE_MODEL"),
            allow_empty=True,
        )
        ollama_model = _parse_model(
            "PC_AGENT_OLLAMA_MODEL", get("PC_AGENT_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
        )
        anthropic_model = _parse_model(
            "PC_AGENT_ANTHROPIC_MODEL", get("PC_AGENT_ANTHROPIC_MODEL"), allow_empty=True
        )

        gemini_base_url = _parse_base_url(
            "PC_AGENT_GEMINI_BASE_URL",
            get("PC_AGENT_GEMINI_BASE_URL", DEFAULT_GEMINI_BASE_URL),
        )
        openrouter_base_url = _parse_base_url(
            "PC_AGENT_OPENROUTER_BASE_URL",
            get("PC_AGENT_OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL),
        )
        openai_base_url = _parse_base_url(
            "PC_AGENT_OPENAI_BASE_URL",
            get("PC_AGENT_OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL),
        )
        openai_compatible_base_url = _parse_base_url(
            "PC_AGENT_OPENAI_COMPATIBLE_BASE_URL",
            get("PC_AGENT_OPENAI_COMPATIBLE_BASE_URL"),
            allow_empty=True,
        )
        ollama_base_url = _parse_base_url(
            "PC_AGENT_OLLAMA_BASE_URL",
            get("PC_AGENT_OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL),
        )
        anthropic_base_url = _parse_base_url(
            "PC_AGENT_ANTHROPIC_BASE_URL",
            get("PC_AGENT_ANTHROPIC_BASE_URL", DEFAULT_ANTHROPIC_BASE_URL),
        )

        return cls(
            root_dir=root,
            environment=environment,
            workspace_dir=workspace_dir,
            data_dir=data_dir,
            database_path=database_path,
            log_level=log_level,
            provider_order=provider_order,
            provider_timeout_seconds=timeout,
            max_retries=max_retries,
            max_recovery_attempts=max_recovery_attempts,
            recovery_backoff_seconds=recovery_backoff_seconds,
            recovery_stale_after_minutes=recovery_stale_after_minutes,
            web_timeout_seconds=web_timeout_seconds,
            web_max_bytes=web_max_bytes,
            license_enforcement=license_enforcement,
            license_file=license_file,
            license_public_key=license_public_key,
            license_key_id=license_key_id,
            trial_days=trial_days,
            gemini_api_key=get("GEMINI_API_KEY") or None,
            openrouter_api_key=get("OPENROUTER_API_KEY") or None,
            openai_api_key=get("OPENAI_API_KEY") or None,
            openai_compatible_api_key=get("OPENAI_COMPATIBLE_API_KEY") or None,
            ollama_api_key=get("OLLAMA_API_KEY") or None,
            anthropic_api_key=get("ANTHROPIC_API_KEY") or None,
            gemini_model=gemini_model,
            openrouter_model=openrouter_model,
            openai_model=openai_model,
            openai_compatible_model=openai_compatible_model,
            ollama_model=ollama_model,
            anthropic_model=anthropic_model,
            gemini_base_url=gemini_base_url,
            openrouter_base_url=openrouter_base_url,
            openai_base_url=openai_base_url,
            openai_compatible_base_url=openai_compatible_base_url,
            ollama_base_url=ollama_base_url,
            anthropic_base_url=anthropic_base_url,
        )


def _parse_provider_order(raw: str) -> tuple[str, ...]:
    result: list[str] = []
    for chunk in raw.split(","):
        name = chunk.strip().lower()
        if not name:
            continue
        if name not in _ALLOWED_PROVIDERS:
            allowed = ", ".join(sorted(_ALLOWED_PROVIDERS))
            raise ConfigError(
                f"Unknown provider '{name}' in PC_AGENT_PROVIDER_ORDER. Allowed: {allowed}."
            )
        if name not in result:
            result.append(name)
    return tuple(result)


def _parse_timeout(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_PROVIDER_TIMEOUT_SECONDS must be a number.") from exc
    if value <= 0 or value > 300:
        raise ConfigError(
            "PC_AGENT_PROVIDER_TIMEOUT_SECONDS must be greater than 0 and at most 300."
        )
    return value


def _parse_max_retries(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_MAX_RETRIES must be an integer.") from exc
    if value < 0 or value > 5:
        # Bounded on purpose: an agent must never retry indefinitely.
        raise ConfigError("PC_AGENT_MAX_RETRIES must be between 0 and 5.")
    return value


def _parse_max_recovery_attempts(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_MAX_RECOVERY_ATTEMPTS must be an integer.") from exc
    if value < 0 or value > 3:
        # Healing is deliberately capped so a broken workflow cannot loop forever.
        raise ConfigError("PC_AGENT_MAX_RECOVERY_ATTEMPTS must be between 0 and 3.")
    return value


def _parse_recovery_backoff(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_RECOVERY_BACKOFF_SECONDS must be a number.") from exc
    if value < 0 or value > 5:
        raise ConfigError("PC_AGENT_RECOVERY_BACKOFF_SECONDS must be between 0 and 5.")
    return value


def _parse_stale_after_minutes(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_RECOVERY_STALE_AFTER_MINUTES must be an integer.") from exc
    if value < 1 or value > 10_080:
        raise ConfigError(
            "PC_AGENT_RECOVERY_STALE_AFTER_MINUTES must be between 1 and 10080."
        )
    return value


def _parse_web_timeout(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_WEB_TIMEOUT_SECONDS must be a number.") from exc
    if value <= 0 or value > 60:
        raise ConfigError("PC_AGENT_WEB_TIMEOUT_SECONDS must be greater than 0 and at most 60.")
    return value


def _parse_web_max_bytes(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_WEB_MAX_BYTES must be an integer.") from exc
    if value < 1024 or value > 10_000_000:
        raise ConfigError("PC_AGENT_WEB_MAX_BYTES must be between 1024 and 10000000.")
    return value


def _parse_trial_days(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_TRIAL_DAYS must be an integer.") from exc
    if value < 0 or value > 90:
        raise ConfigError("PC_AGENT_TRIAL_DAYS must be between 0 and 90 (0 disables the trial).")
    return value


def _parse_license_key_id(raw: str) -> str:
    value = raw.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
        raise ConfigError(
            "PC_AGENT_LICENSE_KEY_ID must be 1-64 letters, digits, '.', '_' or '-'."
        )
    return value


def _parse_license_public_key(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    padded = value + "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise ConfigError("PC_AGENT_LICENSE_PUBLIC_KEY must be base64url data.") from exc
    if len(decoded) != 32:
        raise ConfigError("PC_AGENT_LICENSE_PUBLIC_KEY must decode to a 32-byte Ed25519 key.")
    return value


def _parse_model(name: str, raw: str, *, allow_empty: bool = False) -> str:
    value = raw.strip()
    if not value and allow_empty:
        return ""
    if not value or len(value) > 200 or any(char.isspace() for char in value):
        raise ConfigError(f"{name} must be a non-empty model name without whitespace.")
    return value


def _parse_base_url(name: str, raw: str, *, allow_empty: bool = False) -> str:
    value = raw.strip().rstrip("/")
    if not value and allow_empty:
        return ""
    parsed = urlparse(value)
    loopback_hosts = {"localhost", "127.0.0.1", "::1"}
    is_loopback = (parsed.hostname or "").lower() in loopback_hosts
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise ConfigError(f"{name} must be a valid http(s) base URL.")
    if parsed.scheme == "http" and not is_loopback:
        raise ConfigError(f"{name} must use HTTPS unless it points to a loopback address.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError(
            f"{name} must not contain credentials, a query string, or a fragment."
        )
    return value
