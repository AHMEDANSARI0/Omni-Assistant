"""Environment-based configuration.

Values come from real environment variables first, then an optional `.env`
file in the application root. Secrets are never printed: API key fields are
excluded from repr and from the public summary.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import math
import os
import platform
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

from .credentials import (
    SUPPORTED_CREDENTIAL_PROVIDERS,
    CredentialStore,
    build_credential_store,
    canonical_credential_provider,
)
from .errors import ConfigError, CredentialError

_ALLOWED_ENVIRONMENTS = frozenset({"development", "test", "production"})
_ALLOWED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_ALLOWED_LOG_FORMATS = frozenset({"json", "text"})
_ALLOWED_PROVIDER_STRATEGIES = frozenset(
    {"default", "local-first", "fastest", "cheapest"}
)
_ALLOWED_EMBEDDING_PROVIDERS = frozenset(
    {"auto", "local", "mock", "openai", "openrouter", "openai-compatible", "ollama"}
)
_ALLOWED_LICENSE_ENFORCEMENT = frozenset({"off", "warn", "enforce"})
_ALLOWED_CREDENTIAL_STORES = frozenset({"auto", "windows", "linux", "macos", "env"})
_ALLOWED_AUTO_UPGRADE_MODES = frozenset({"stage-only", "apply-when-idle"})
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
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"

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
    # Transformation Phase 3 local service / structured logging / profiles.
    log_format: str = "text"
    active_profile: str | None = None
    api_enabled: bool = True
    api_host: str = "127.0.0.1"
    api_port: int = 8765
    api_token: str | None = field(default=None, repr=False)
    # Transformation Phase 4 Mission Center bounds.
    mission_max_parallel: int = 3
    scheduler_interval_seconds: int = 30
    # Transformation Phase 7 Provider Platform v2. Routing strategy is a
    # preference, never a capability gate; prices/context sizes exist only
    # when the user configures them (nothing is guessed). Usage analytics
    # are local append-only JSONL under the data dir and can be disabled.
    provider_strategy: str = "default"
    provider_analytics_enabled: bool = True
    provider_prices: tuple[tuple[str, float, float], ...] = ()
    model_context_tokens: tuple[tuple[str, int], ...] = ()
    # Transformation Phase 8 Memory v2 / knowledge. Embeddings default to
    # an offline local hashing embedder; provider embeddings are used
    # only when explicitly configured (or discoverable under "auto").
    embedding_provider: str = "auto"
    embedding_model: str = ""
    embedding_dimensions: int = 384
    # Phase 12 approved subprocess. Off by default: every subprocess tool
    # fails closed until the user explicitly opts in.
    allow_subprocess: bool = False
    command_allowlist: tuple[str, ...] = (
        "git",
        "python",
        "python3",
        "py",
        "node",
        "npm",
    )
    command_timeout_seconds: float = 60.0
    # Phase 13 browser automation. Off by default: the user starts a debug
    # browser explicitly and every browser tool fails closed until opted in.
    allow_browser: bool = False
    browser_port: int = 9222
    browser_timeout_seconds: float = 20.0
    # Phase 14 Windows UI Automation. Off by default; native desktop
    # actions fail closed until explicitly enabled and separately approved.
    allow_desktop_automation: bool = False
    desktop_automation_timeout_seconds: float = 30.0
    # Phase 15 provider vision. Local image tools work without this gate;
    # sending one selected image to a model is opt-in and separately approved.
    allow_vision: bool = False
    vision_max_image_bytes: int = 10_000_000
    # Transformation Phase 10 Voice. The microphone is OFF until the user
    # turns it on explicitly (persisted as a preference), wake-word listening
    # needs a second, separate opt-in, audio is discarded after transcription
    # unless retention is enabled, and cloud speech-to-text additionally
    # requires a per-call consent flag. Nothing here ever records silently.
    voice_enabled: bool = False
    voice_wake_word_enabled: bool = False
    voice_wake_word: str = "jarvis"
    voice_retain_audio: bool = False
    voice_store_transcripts: bool = False
    voice_cloud_stt: bool = False
    voice_stt_engine: str = "auto"
    voice_tts_engine: str = "auto"
    voice_max_seconds: float = 60.0
    voice_sample_rate: int = 16_000
    voice_whisper_model: str = ""
    # Transformation Phase 13 connectors. Every connector is off until the user
    # opts in, secrets are read from the environment/.env only (never stored in
    # the database), and outbound calls are bounded and signed.
    allow_connectors: bool = False
    allow_email_connector: bool = False
    connector_calendar_path: str = ""
    connector_drive_root: str = ""
    connector_chat_url: str = ""
    connector_webhook_secret: str = field(default="", repr=False)
    connector_chat_secret: str = field(default="", repr=False)
    connector_email_host: str = ""
    connector_email_user: str = ""
    connector_email_password: str = field(default="", repr=False)
    connector_email_folder: str = "INBOX"
    connector_smtp_host: str = ""
    connector_smtp_port: int = 587
    connector_max_items: int = 25
    connector_timeout_seconds: float = 15.0
    connector_webhook_skew_seconds: int = 300
    connector_allow_private_endpoints: bool = False
    # Transformation Phase 14 proactive intelligence. Local in-app notifications
    # are on (they are just durable events); anything that leaves the machine is
    # a separate, default-off gate.
    allow_proactive: bool = True
    allow_proactive_outbound: bool = False
    quiet_hours: str = ""
    quiet_hours_allow_critical: bool = False
    notification_max_per_hour: int = 20
    notification_cooldown_seconds: int = 300
    briefing_enabled: bool = False
    briefing_time: str = "08:00"
    reminder_max_open: int = 200
    proactive_tick_limit: int = 25
    # Transformation Phase 15 plugin/skill ecosystem. Plugins are code, so the
    # master gate is off by default, signatures are required, and any plugin
    # tool call runs in an OS-limited child process with a hard timeout.
    allow_plugins: bool = False
    plugin_allow_unsigned: bool = False
    plugin_trusted_keys: str = ""
    plugin_timeout_seconds: float = 30.0
    plugin_memory_limit_mb: int = 512
    plugin_cpu_limit_seconds: int = 30
    plugin_max_bundle_bytes: int = 8_000_000
    plugin_max_files: int = 200
    plugin_max_file_bytes: int = 2_000_000
    # Transformation Phase 16 optional encrypted device sync. Off by default and
    # network-free: bundles go into a folder the user already syncs. Everything
    # in a bundle is signed with the sender's Ed25519 key and encrypted to each
    # paired device's X25519 key.
    allow_sync: bool = False
    sync_device_name: str = ""
    sync_dir: str = ""
    sync_max_bundle_bytes: int = 4_000_000
    sync_max_records: int = 500
    sync_retention_days: int = 30
    sync_conflict_policy: str = "newest"
    sync_clock_skew_seconds: int = 900

    # -- interface / accessibility (Transformation Phase 17) -----------------
    ui_locale: str = "en"
    ui_scale: float = 1.0
    ui_high_contrast: bool = False
    ui_reduce_motion: bool = False
    ui_keyboard_shortcuts: bool = True
    ui_cli_color: str = "auto"
    # Phase 16/44 secure credential storage. Environment/.env values remain
    # authoritative when present; otherwise the platform OS credential store
    # is consulted in production loads. Explicit test environ mappings never
    # silently read the developer's real OS vault.
    credential_store: str = "auto"
    credential_store_backend: str = "environment-only"
    credential_store_available: bool = False
    credential_store_detail: str = ""
    credential_sources: tuple[tuple[str, str], ...] = ()
    # Phase 17: self-repair is a separate default-off maintenance gate;
    # task budgets are deterministic local ceilings (0 means unlimited,
    # except the step budget which is 1..20).
    allow_self_repair: bool = False
    task_token_budget: int = 20_000
    task_provider_call_budget: int = 10
    task_tool_budget: int = 25
    task_step_budget: int = 5
    task_time_budget_seconds: float = 600.0
    # Transformation Phase 2 Safety Kernel. Hard tool timeouts and the
    # isolated worker are on by default; turning them off is an explicit,
    # audited operator choice. The per-tool ceiling clamps every tool's
    # declared timeout, and Approval Center decisions expire so a stale
    # approval can never be consumed by a much later run.
    safety_hard_timeouts: bool = True
    safety_worker_isolation: bool = True
    safety_max_tool_seconds: float = 600.0
    safety_approval_expiry_minutes: int = 1440
    # Phase 6 licensing. Enforcement defaults to "off" so development and
    # existing installs keep working; commercial builds opt in explicitly.
    license_enforcement: str = "off"
    license_file: Path = field(default_factory=lambda: Path("data") / "license.key")
    license_public_key: str | None = field(default=None, repr=False)
    license_key_id: str = "default"
    trial_days: int = 14
    # Phase 21 signed offline updates. This is a public trust anchor, never a
    # secret. Updates stay explicit and local; requiring signatures makes an
    # unsigned or unverifiable bundle fail closed during staging.
    release_public_key: str | None = field(default=None, repr=False)
    release_key_id: str | None = None
    require_signed_updates: bool = False
    online_updates_enabled: bool = False
    update_feed_url: str | None = None
    update_channel: str = "stable"
    online_update_timeout_seconds: float = 30.0
    online_update_max_bytes: int = 25_000_000
    # Phase 56 background auto-upgrade. This is a second, separate opt-in
    # from explicit online downloads. It accepts signed updates only; the
    # default mode stages a verified update but does not apply it.
    auto_upgrade_enabled: bool = False
    auto_upgrade_mode: str = "stage-only"
    auto_upgrade_interval_hours: int = 24
    auto_upgrade_idle_minutes: int = 15
    auto_upgrade_maintenance_window: str | None = None
    auto_upgrade_require_ac_power: bool = True
    auto_upgrade_reinstall: bool = False
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
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL
    gemini_base_url: str = DEFAULT_GEMINI_BASE_URL
    openrouter_base_url: str = DEFAULT_OPENROUTER_BASE_URL
    openai_base_url: str = DEFAULT_OPENAI_BASE_URL
    openai_compatible_base_url: str = ""
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL
    anthropic_base_url: str = DEFAULT_ANTHROPIC_BASE_URL

    def credential_source(self, provider: str) -> str:
        """Where a provider key came from: environment, OS store or missing."""

        canonical = canonical_credential_provider(provider)
        return dict(self.credential_sources).get(canonical, "missing")

    @property
    def _provider_keys(self) -> dict[str, str | None]:
        return {
            "gemini": self.gemini_api_key,
            "openrouter": self.openrouter_api_key,
            "openai": self.openai_api_key,
            "openai-compatible": self.openai_compatible_api_key,
            "ollama": self.ollama_api_key,
            "anthropic": self.anthropic_api_key,
        }

    @property
    def effective_provider_order(self) -> tuple[str, ...]:
        """The route to build: the configured order, else the keyed providers.

        0.82.17: ``PC_AGENT_PROVIDER_ORDER`` used to be the only way to enable a
        cloud provider, so putting ``GEMINI_API_KEY`` in ``.env`` - which is what
        every document tells the user to do - left the router with nothing but
        ``mock``. When no order is configured, the keyed providers are used, in a
        stable preference order. ``ollama`` is deliberately excluded: a local
        server that may not be running must be opted into explicitly.
        """

        if self.provider_order:
            return self.provider_order
        keys = self._provider_keys
        return tuple(
            name
            for name in ("gemini", "openrouter", "openai", "anthropic", "openai-compatible")
            if keys.get(name)
        )

    @property
    def providers_with_credentials(self) -> tuple[str, ...]:
        """Providers from the effective order for which a key is present."""
        keys = self._provider_keys
        return tuple(name for name in self.effective_provider_order if keys.get(name))

    def ensure_directories(self) -> None:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def plugin_trusted_key_map(self) -> dict[str, str]:
        """Parsed ``{key id: public key}`` map, or ``{}`` when unset/invalid."""

        import json as _json

        raw = str(self.plugin_trusted_keys or "").strip()
        if not raw:
            return {}
        try:
            payload = _json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(key): str(value) for key, value in payload.items()}

    def plugin_trusted_key_count(self) -> int:
        return len(self.plugin_trusted_key_map())

    def public_summary(self) -> dict[str, str]:
        """Non-secret values safe to show in CLI output and logs."""
        configured = ", ".join(self.providers_with_credentials) or "none"
        credential_sources = ", ".join(
            f"{name}={self.credential_source(name)}"
            for name in self.providers_with_credentials
        )
        return {
            "environment": self.environment,
            "root": str(self.root_dir),
            "workspace": str(self.workspace_dir),
            "database": str(self.database_path),
            "log_level": self.log_level,
            "log_format": self.log_format,
            "active_profile": self.active_profile or "none",
            "api_enabled": "yes" if self.api_enabled else "no",
            "api_host": self.api_host,
            "api_port": str(self.api_port),
            "api_token": "environment" if self.api_token else "file (auto)",
            "mission_max_parallel": str(self.mission_max_parallel),
            "scheduler_interval_seconds": str(self.scheduler_interval_seconds),
            "provider_order": ", ".join(self.effective_provider_order) or "mock (default)",
            "providers_with_credentials": configured,
            "max_recovery_attempts": str(self.max_recovery_attempts),
            "recovery_stale_after_minutes": str(self.recovery_stale_after_minutes),
            "web_timeout_seconds": str(self.web_timeout_seconds),
            "web_max_bytes": str(self.web_max_bytes),
            "allow_subprocess": "yes" if self.allow_subprocess else "no",
            "command_allowlist": ", ".join(self.command_allowlist) or "none",
            "command_timeout_seconds": str(self.command_timeout_seconds),
            "allow_browser": "yes" if self.allow_browser else "no",
            "browser_port": str(self.browser_port),
            "browser_timeout_seconds": str(self.browser_timeout_seconds),
            "allow_desktop_automation": (
                "yes" if self.allow_desktop_automation else "no"
            ),
            "desktop_automation_timeout_seconds": str(
                self.desktop_automation_timeout_seconds
            ),
            "allow_vision": "yes" if self.allow_vision else "no",
            "vision_max_image_bytes": str(self.vision_max_image_bytes),
            "voice_enabled": "yes" if self.voice_enabled else "no",
            "voice_wake_word_enabled": (
                "yes" if self.voice_wake_word_enabled else "no"
            ),
            "voice_retain_audio": "yes" if self.voice_retain_audio else "no",
            "voice_store_transcripts": (
                "yes" if self.voice_store_transcripts else "no"
            ),
            "voice_cloud_stt": "yes" if self.voice_cloud_stt else "no",
            "voice_stt_engine": self.voice_stt_engine,
            "voice_tts_engine": self.voice_tts_engine,
            "voice_max_seconds": str(self.voice_max_seconds),
            "allow_connectors": "yes" if self.allow_connectors else "no",
            "allow_email_connector": "yes" if self.allow_email_connector else "no",
            "connector_calendar": _mask_path(self.connector_calendar_path),
            "connector_drive": _mask_path(self.connector_drive_root),
            "connector_chat": "configured" if self.connector_chat_url else "none",
            "connector_email": "configured" if self.connector_email_host else "none",
            "connector_webhook_secret": "set" if self.connector_webhook_secret else "missing",
            "ui_locale": self.ui_locale,
            "ui_scale": f"{self.ui_scale:.2f}",
            "ui_theme": "high-contrast" if self.ui_high_contrast else "jarvis",
            "ui_reduce_motion": "yes" if self.ui_reduce_motion else "no",
            "ui_keyboard_shortcuts": "yes" if self.ui_keyboard_shortcuts else "no",
            "ui_cli_color": self.ui_cli_color,
            "allow_proactive": "yes" if self.allow_proactive else "no",
            "proactive_outbound": "yes" if self.allow_proactive_outbound else "no",
            "quiet_hours": self.quiet_hours or "none",
            "briefing": (
                f"daily at {self.briefing_time}" if self.briefing_enabled else "off"
            ),
            "notification_max_per_hour": str(self.notification_max_per_hour),
            "allow_plugins": "yes" if self.allow_plugins else "no",
            "plugin_signatures": "optional" if self.plugin_allow_unsigned else "required",
            "plugin_trusted_keys": str(self.plugin_trusted_key_count()),
            "plugin_sandbox": "isolated-process",
            "allow_sync": "yes" if self.allow_sync else "no",
            "sync_device": _mask_path(self.sync_device_name),
            "sync_dir": _mask_path(self.sync_dir),
            "sync_conflict_policy": self.sync_conflict_policy,
            "connector_private_endpoints": (
                "yes" if self.connector_allow_private_endpoints else "no"
            ),
            "credential_store": self.credential_store,
            "credential_store_backend": self.credential_store_backend,
            "credential_store_available": (
                "yes" if self.credential_store_available else "no"
            ),
            "provider_credential_sources": credential_sources or "none",
            "allow_self_repair": "yes" if self.allow_self_repair else "no",
            "task_token_budget": str(self.task_token_budget),
            "task_provider_call_budget": str(self.task_provider_call_budget),
            "task_tool_budget": str(self.task_tool_budget),
            "task_step_budget": str(self.task_step_budget),
            "task_time_budget_seconds": str(self.task_time_budget_seconds),
            "safety_hard_timeouts": "yes" if self.safety_hard_timeouts else "no",
            "safety_worker_isolation": (
                "yes" if self.safety_worker_isolation else "no"
            ),
            "safety_max_tool_seconds": str(self.safety_max_tool_seconds),
            "safety_approval_expiry_minutes": str(
                self.safety_approval_expiry_minutes
            ),
            "license_enforcement": self.license_enforcement,
            "license_public_key_configured": "yes" if self.license_public_key else "no",
            "license_file": str(self.license_file),
            "trial_days": str(self.trial_days),
            "release_public_key_configured": (
                "yes" if self.release_public_key else "no"
            ),
            "release_key_id": self.release_key_id or "any",
            "require_signed_updates": "yes" if self.require_signed_updates else "no",
            "online_updates": "yes" if self.online_updates_enabled else "no",
            "update_feed_configured": "yes" if self.update_feed_url else "no",
            "update_feed_host": (
                urlparse(self.update_feed_url).hostname or "none"
                if self.update_feed_url
                else "none"
            ),
            "update_channel": self.update_channel,
            "online_update_timeout_seconds": str(
                self.online_update_timeout_seconds
            ),
            "online_update_max_bytes": str(self.online_update_max_bytes),
            "auto_upgrade": "yes" if self.auto_upgrade_enabled else "no",
            "auto_upgrade_mode": self.auto_upgrade_mode,
            "auto_upgrade_interval_hours": str(self.auto_upgrade_interval_hours),
            "auto_upgrade_idle_minutes": str(self.auto_upgrade_idle_minutes),
            "auto_upgrade_maintenance_window": (
                self.auto_upgrade_maintenance_window or "any"
            ),
            "auto_upgrade_require_ac_power": (
                "yes" if self.auto_upgrade_require_ac_power else "no"
            ),
            "auto_upgrade_reinstall": (
                "yes" if self.auto_upgrade_reinstall else "no"
            ),
        }

    @classmethod
    def from_env(
        cls,
        root_dir: str | Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        load_dotenv: bool = True,
        credential_store: CredentialStore | None = None,
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

        log_format = get("PC_AGENT_LOG_FORMAT", "text").lower()
        if log_format not in _ALLOWED_LOG_FORMATS:
            raise ConfigError("PC_AGENT_LOG_FORMAT must be one of: json, text.")
        api_enabled = _parse_auto_upgrade_boolean(
            "PC_AGENT_API_ENABLED", get("PC_AGENT_API_ENABLED", "true")
        )
        api_host = _parse_api_host(get("PC_AGENT_API_HOST", "127.0.0.1"))
        api_port = _parse_api_port(get("PC_AGENT_API_PORT", "8765"))
        api_token = get("PC_AGENT_API_TOKEN") or None
        if api_token is not None and len(api_token) < 16:
            raise ConfigError("PC_AGENT_API_TOKEN must be at least 16 characters when set.")
        mission_max_parallel = _parse_mission_max_parallel(
            get("PC_AGENT_MISSION_MAX_PARALLEL", "3")
        )
        scheduler_interval_seconds = _parse_scheduler_interval(
            get("PC_AGENT_SCHEDULER_INTERVAL_SECONDS", "30")
        )
        provider_strategy = get("PC_AGENT_PROVIDER_STRATEGY", "default").strip().lower()
        if provider_strategy not in _ALLOWED_PROVIDER_STRATEGIES:
            raise ConfigError(
                "PC_AGENT_PROVIDER_STRATEGY must be one of: "
                + ", ".join(sorted(_ALLOWED_PROVIDER_STRATEGIES))
                + "."
            )
        provider_analytics_enabled = _parse_safety_bool(
            "PC_AGENT_PROVIDER_ANALYTICS",
            get("PC_AGENT_PROVIDER_ANALYTICS", "1") or "1",
        )
        provider_prices = _parse_provider_prices(get("PC_AGENT_PROVIDER_PRICES"))
        model_context_tokens = _parse_model_context_tokens(
            get("PC_AGENT_MODEL_CONTEXT_TOKENS")
        )
        embedding_provider = (
            get("PC_AGENT_EMBEDDING_PROVIDER", "auto").strip().lower() or "auto"
        )
        if embedding_provider not in _ALLOWED_EMBEDDING_PROVIDERS:
            raise ConfigError(
                "PC_AGENT_EMBEDDING_PROVIDER must be one of: "
                + ", ".join(sorted(_ALLOWED_EMBEDDING_PROVIDERS))
                + "."
            )
        embedding_model = get("PC_AGENT_EMBEDDING_MODEL", "").strip()
        embedding_dimensions = _parse_embedding_dimensions(
            get("PC_AGENT_EMBEDDING_DIMENSIONS", "384")
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
        allow_subprocess = _parse_allow_subprocess(
            get("PC_AGENT_ALLOW_SUBPROCESS", "0")
        )
        command_allowlist = _parse_command_allowlist(
            get("PC_AGENT_COMMAND_ALLOWLIST", "git,python,python3,py,node,npm")
        )
        command_timeout_seconds = _parse_command_timeout(
            get("PC_AGENT_COMMAND_TIMEOUT_SECONDS", "60")
        )
        allow_browser = _parse_allow_browser(get("PC_AGENT_ALLOW_BROWSER", "0"))
        browser_port = _parse_browser_port(get("PC_AGENT_BROWSER_PORT", "9222"))
        browser_timeout_seconds = _parse_browser_timeout(
            get("PC_AGENT_BROWSER_TIMEOUT_SECONDS", "20")
        )
        allow_desktop_automation = _parse_allow_desktop_automation(
            get("PC_AGENT_ALLOW_DESKTOP_AUTOMATION", "0")
        )
        desktop_automation_timeout_seconds = _parse_desktop_automation_timeout(
            get("PC_AGENT_DESKTOP_AUTOMATION_TIMEOUT_SECONDS", "30")
        )
        allow_vision = _parse_allow_vision(get("PC_AGENT_ALLOW_VISION", "0"))
        voice_enabled = _parse_safety_bool(
            "PC_AGENT_VOICE_ENABLED", get("PC_AGENT_VOICE_ENABLED", "0")
        )
        voice_wake_word_enabled = _parse_safety_bool(
            "PC_AGENT_VOICE_WAKE_WORD_ENABLED",
            get("PC_AGENT_VOICE_WAKE_WORD_ENABLED", "0"),
        )
        voice_retain_audio = _parse_safety_bool(
            "PC_AGENT_VOICE_RETAIN_AUDIO", get("PC_AGENT_VOICE_RETAIN_AUDIO", "0")
        )
        voice_store_transcripts = _parse_safety_bool(
            "PC_AGENT_VOICE_STORE_TRANSCRIPTS",
            get("PC_AGENT_VOICE_STORE_TRANSCRIPTS", "0"),
        )
        voice_cloud_stt = _parse_safety_bool(
            "PC_AGENT_VOICE_CLOUD_STT", get("PC_AGENT_VOICE_CLOUD_STT", "0")
        )
        voice_stt_engine = _parse_voice_engine(
            "PC_AGENT_VOICE_STT_ENGINE", get("PC_AGENT_VOICE_STT_ENGINE", "auto")
        )
        voice_tts_engine = _parse_voice_engine(
            "PC_AGENT_VOICE_TTS_ENGINE", get("PC_AGENT_VOICE_TTS_ENGINE", "auto")
        )
        voice_max_seconds = _parse_voice_max_seconds(
            get("PC_AGENT_VOICE_MAX_SECONDS", "60")
        )
        voice_sample_rate = _parse_voice_sample_rate(
            get("PC_AGENT_VOICE_SAMPLE_RATE", "16000")
        )
        voice_wake_word = get("PC_AGENT_VOICE_WAKE_WORD", "jarvis").lower() or "jarvis"
        voice_whisper_model = get("PC_AGENT_VOICE_WHISPER_MODEL", "")
        vision_max_image_bytes = _parse_vision_max_image_bytes(
            get("PC_AGENT_VISION_MAX_IMAGE_BYTES", "10000000")
        )
        credential_store_mode = _parse_credential_store(
            get("PC_AGENT_CREDENTIAL_STORE", "auto")
        )
        status_store = credential_store or build_credential_store(
            credential_store_mode
        )
        credential_status = status_store.status(include_stored=False)
        allow_self_repair = _parse_allow_self_repair(
            get("PC_AGENT_ALLOW_SELF_REPAIR", "0")
        )
        task_token_budget = _parse_nonnegative_budget(
            "PC_AGENT_TASK_TOKEN_BUDGET",
            get("PC_AGENT_TASK_TOKEN_BUDGET", "20000"),
            maximum=10_000_000,
        )
        task_provider_call_budget = _parse_nonnegative_budget(
            "PC_AGENT_TASK_PROVIDER_CALL_BUDGET",
            get("PC_AGENT_TASK_PROVIDER_CALL_BUDGET", "10"),
            maximum=1000,
        )
        task_tool_budget = _parse_nonnegative_budget(
            "PC_AGENT_TASK_TOOL_BUDGET",
            get("PC_AGENT_TASK_TOOL_BUDGET", "25"),
            maximum=1000,
        )
        task_step_budget = _parse_task_step_budget(
            get("PC_AGENT_TASK_STEP_BUDGET", "5")
        )
        task_time_budget_seconds = _parse_task_time_budget(
            get("PC_AGENT_TASK_TIME_BUDGET_SECONDS", "600")
        )
        safety_hard_timeouts = _parse_safety_bool(
            "PC_AGENT_SAFETY_HARD_TIMEOUTS",
            get("PC_AGENT_SAFETY_HARD_TIMEOUTS", "1") or "1",
        )
        safety_worker_isolation = _parse_safety_bool(
            "PC_AGENT_SAFETY_WORKER_ISOLATION",
            get("PC_AGENT_SAFETY_WORKER_ISOLATION", "1") or "1",
        )
        safety_max_tool_seconds = _parse_safety_max_tool_seconds(
            get("PC_AGENT_SAFETY_MAX_TOOL_SECONDS", "600")
        )
        safety_approval_expiry_minutes = _parse_safety_approval_expiry_minutes(
            get("PC_AGENT_SAFETY_APPROVAL_EXPIRY_MINUTES", "1440")
        )
        lookup_store = (
            status_store
            if credential_store_mode != "env"
            and (credential_store is not None or environ is None)
            else None
        )

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
        release_public_key = _parse_release_public_key(
            get("PC_AGENT_RELEASE_PUBLIC_KEY")
        )
        release_key_id = _parse_release_key_id(get("PC_AGENT_RELEASE_KEY_ID"))
        require_signed_updates = _parse_require_signed_updates(
            get("PC_AGENT_REQUIRE_SIGNED_UPDATES", "0")
        )
        online_updates_enabled = _parse_online_updates_enabled(
            get("PC_AGENT_ONLINE_UPDATES", "0")
        )
        update_feed_url = _parse_update_feed_url(get("PC_AGENT_UPDATE_FEED_URL"))
        update_channel = _parse_update_channel(
            get("PC_AGENT_UPDATE_CHANNEL", "stable")
        )
        online_update_timeout_seconds = _parse_online_update_timeout(
            get("PC_AGENT_ONLINE_UPDATE_TIMEOUT_SECONDS", "30")
        )
        online_update_max_bytes = _parse_online_update_max_bytes(
            get("PC_AGENT_ONLINE_UPDATE_MAX_BYTES", "25000000")
        )
        auto_upgrade_enabled = _parse_auto_upgrade_enabled(
            get("PC_AGENT_AUTO_UPGRADE", "0")
        )
        auto_upgrade_mode = _parse_auto_upgrade_mode(
            get("PC_AGENT_AUTO_UPGRADE_MODE", "stage-only")
        )
        auto_upgrade_interval_hours = _parse_auto_upgrade_interval_hours(
            get("PC_AGENT_AUTO_UPGRADE_INTERVAL_HOURS", "24")
        )
        auto_upgrade_idle_minutes = _parse_auto_upgrade_idle_minutes(
            get("PC_AGENT_AUTO_UPGRADE_IDLE_MINUTES", "15")
        )
        auto_upgrade_maintenance_window = _parse_auto_upgrade_window(
            get("PC_AGENT_AUTO_UPGRADE_MAINTENANCE_WINDOW")
        )
        auto_upgrade_require_ac_power = _parse_auto_upgrade_require_ac_power(
            get("PC_AGENT_AUTO_UPGRADE_REQUIRE_AC_POWER", "1")
        )
        auto_upgrade_reinstall = _parse_auto_upgrade_reinstall(
            get("PC_AGENT_AUTO_UPGRADE_REINSTALL", "0")
        )

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
            "PC_AGENT_ANTHROPIC_MODEL",
            get("PC_AGENT_ANTHROPIC_MODEL") or DEFAULT_ANTHROPIC_MODEL,
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

        api_key_env_names = {
            "gemini": "GEMINI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "openai": "OPENAI_API_KEY",
            "openai-compatible": "OPENAI_COMPATIBLE_API_KEY",
            "ollama": "OLLAMA_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }
        api_keys: dict[str, str | None] = {}
        credential_sources: list[tuple[str, str]] = []
        for provider in SUPPORTED_CREDENTIAL_PROVIDERS:
            value = get(api_key_env_names[provider]) or None
            if value:
                api_keys[provider] = value
                credential_sources.append((provider, "environment"))
                continue
            if lookup_store is not None and lookup_store.available:
                try:
                    value = lookup_store.get_api_key(provider)
                except CredentialError as exc:
                    raise ConfigError(str(exc)) from exc
            api_keys[provider] = value
            if value:
                assert lookup_store is not None  # value came from OS storage
                credential_sources.append((provider, lookup_store.source_name))

        constructed = cls(
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
            log_format=log_format,
            api_enabled=api_enabled,
            api_host=api_host,
            api_port=api_port,
            api_token=api_token,
            mission_max_parallel=mission_max_parallel,
            scheduler_interval_seconds=scheduler_interval_seconds,
            provider_strategy=provider_strategy,
            provider_analytics_enabled=provider_analytics_enabled,
            provider_prices=provider_prices,
            model_context_tokens=model_context_tokens,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            embedding_dimensions=embedding_dimensions,
            allow_subprocess=allow_subprocess,
            command_allowlist=command_allowlist,
            command_timeout_seconds=command_timeout_seconds,
            allow_browser=allow_browser,
            browser_port=browser_port,
            browser_timeout_seconds=browser_timeout_seconds,
            allow_desktop_automation=allow_desktop_automation,
            desktop_automation_timeout_seconds=desktop_automation_timeout_seconds,
            allow_vision=allow_vision,
            vision_max_image_bytes=vision_max_image_bytes,
            voice_enabled=voice_enabled,
            voice_wake_word_enabled=voice_wake_word_enabled,
            voice_wake_word=voice_wake_word,
            voice_retain_audio=voice_retain_audio,
            voice_store_transcripts=voice_store_transcripts,
            voice_cloud_stt=voice_cloud_stt,
            voice_stt_engine=voice_stt_engine,
            voice_tts_engine=voice_tts_engine,
            voice_max_seconds=voice_max_seconds,
            voice_sample_rate=voice_sample_rate,
            voice_whisper_model=voice_whisper_model,
            allow_connectors=_parse_safety_bool(
                "PC_AGENT_ALLOW_CONNECTORS", get("PC_AGENT_ALLOW_CONNECTORS", "0")
            ),
            allow_email_connector=_parse_safety_bool(
                "PC_AGENT_ALLOW_EMAIL_CONNECTOR",
                get("PC_AGENT_ALLOW_EMAIL_CONNECTOR", "0"),
            ),
            connector_calendar_path=str(get("PC_AGENT_CONNECTOR_CALENDAR", "") or "").strip(),
            connector_drive_root=str(get("PC_AGENT_CONNECTOR_DRIVE_ROOT", "") or "").strip(),
            connector_chat_url=str(get("PC_AGENT_CONNECTOR_CHAT_URL", "") or "").strip(),
            connector_webhook_secret=str(get("PC_AGENT_CONNECTOR_WEBHOOK_SECRET", "") or ""),
            connector_chat_secret=str(get("PC_AGENT_CONNECTOR_CHAT_SECRET", "") or ""),
            connector_email_host=str(get("PC_AGENT_CONNECTOR_EMAIL_HOST", "") or "").strip(),
            connector_email_user=str(get("PC_AGENT_CONNECTOR_EMAIL_USER", "") or "").strip(),
            connector_email_password=str(get("PC_AGENT_CONNECTOR_EMAIL_PASSWORD", "") or ""),
            connector_email_folder=str(get("PC_AGENT_CONNECTOR_EMAIL_FOLDER", "INBOX") or "INBOX"),
            connector_smtp_host=str(get("PC_AGENT_CONNECTOR_SMTP_HOST", "") or "").strip(),
            connector_smtp_port=_parse_connector_int(
                "PC_AGENT_CONNECTOR_SMTP_PORT", get("PC_AGENT_CONNECTOR_SMTP_PORT", "587"), 587, 1, 65_535
            ),
            connector_max_items=_parse_connector_int(
                "PC_AGENT_CONNECTOR_MAX_ITEMS", get("PC_AGENT_CONNECTOR_MAX_ITEMS", "25"), 25, 1, 100
            ),
            connector_timeout_seconds=_parse_connector_float(
                "PC_AGENT_CONNECTOR_TIMEOUT_SECONDS",
                get("PC_AGENT_CONNECTOR_TIMEOUT_SECONDS", "15"),
                15.0,
                1.0,
                120.0,
            ),
            connector_webhook_skew_seconds=_parse_connector_int(
                "PC_AGENT_CONNECTOR_WEBHOOK_SKEW_SECONDS",
                get("PC_AGENT_CONNECTOR_WEBHOOK_SKEW_SECONDS", "300"),
                300,
                30,
                86_400,
            ),
            allow_proactive=_parse_safety_bool(
                "PC_AGENT_ALLOW_PROACTIVE", get("PC_AGENT_ALLOW_PROACTIVE", "1")
            ),
            allow_proactive_outbound=_parse_safety_bool(
                "PC_AGENT_ALLOW_PROACTIVE_OUTBOUND",
                get("PC_AGENT_ALLOW_PROACTIVE_OUTBOUND", ""),
            ),
            quiet_hours=_parse_quiet_hours(get("PC_AGENT_QUIET_HOURS", "")) or "",
            quiet_hours_allow_critical=_parse_safety_bool(
                "PC_AGENT_QUIET_HOURS_ALLOW_CRITICAL",
                get("PC_AGENT_QUIET_HOURS_ALLOW_CRITICAL", ""),
            ),
            notification_max_per_hour=_parse_connector_int(
                "PC_AGENT_NOTIFICATION_MAX_PER_HOUR",
                get("PC_AGENT_NOTIFICATION_MAX_PER_HOUR", "20"),
                20,
                1,
                500,
            ),
            notification_cooldown_seconds=_parse_connector_int(
                "PC_AGENT_NOTIFICATION_COOLDOWN_SECONDS",
                get("PC_AGENT_NOTIFICATION_COOLDOWN_SECONDS", "300"),
                300,
                0,
                86_400,
            ),
            briefing_enabled=_parse_safety_bool(
                "PC_AGENT_BRIEFING_ENABLED", get("PC_AGENT_BRIEFING_ENABLED", "")
            ),
            briefing_time=_parse_clock("PC_AGENT_BRIEFING_TIME", get("PC_AGENT_BRIEFING_TIME", "08:00")),
            reminder_max_open=_parse_connector_int(
                "PC_AGENT_REMINDER_MAX_OPEN", get("PC_AGENT_REMINDER_MAX_OPEN", "200"), 200, 1, 1_000
            ),
            proactive_tick_limit=_parse_connector_int(
                "PC_AGENT_PROACTIVE_TICK_LIMIT",
                get("PC_AGENT_PROACTIVE_TICK_LIMIT", "25"),
                25,
                1,
                200,
            ),
            allow_plugins=_parse_safety_bool(
                "PC_AGENT_ALLOW_PLUGINS", get("PC_AGENT_ALLOW_PLUGINS", "")
            ),
            plugin_allow_unsigned=_parse_safety_bool(
                "PC_AGENT_PLUGIN_ALLOW_UNSIGNED",
                get("PC_AGENT_PLUGIN_ALLOW_UNSIGNED", ""),
            ),
            plugin_trusted_keys=_parse_plugin_keys(
                get("PC_AGENT_PLUGIN_TRUSTED_KEYS", ""), root
            ),
            plugin_timeout_seconds=_parse_connector_float(
                "PC_AGENT_PLUGIN_TIMEOUT_SECONDS",
                get("PC_AGENT_PLUGIN_TIMEOUT_SECONDS", "30"),
                30.0,
                1.0,
                600.0,
            ),
            plugin_memory_limit_mb=_parse_connector_int(
                "PC_AGENT_PLUGIN_MEMORY_LIMIT_MB",
                get("PC_AGENT_PLUGIN_MEMORY_LIMIT_MB", "512"),
                512,
                32,
                4_096,
            ),
            plugin_cpu_limit_seconds=_parse_connector_int(
                "PC_AGENT_PLUGIN_CPU_LIMIT_SECONDS",
                get("PC_AGENT_PLUGIN_CPU_LIMIT_SECONDS", "30"),
                30,
                1,
                600,
            ),
            plugin_max_bundle_bytes=_parse_connector_int(
                "PC_AGENT_PLUGIN_MAX_BUNDLE_BYTES",
                get("PC_AGENT_PLUGIN_MAX_BUNDLE_BYTES", "8000000"),
                8_000_000,
                1_000_000,
                64_000_000,
            ),
            plugin_max_files=_parse_connector_int(
                "PC_AGENT_PLUGIN_MAX_FILES",
                get("PC_AGENT_PLUGIN_MAX_FILES", "200"),
                200,
                1,
                1_000,
            ),
            plugin_max_file_bytes=_parse_connector_int(
                "PC_AGENT_PLUGIN_MAX_FILE_BYTES",
                get("PC_AGENT_PLUGIN_MAX_FILE_BYTES", "2000000"),
                2_000_000,
                1_024,
                16_000_000,
            ),
            allow_sync=_parse_safety_bool(
                "PC_AGENT_ALLOW_SYNC", get("PC_AGENT_ALLOW_SYNC", "")
            ),
            sync_device_name=_parse_device_name(
                get("PC_AGENT_SYNC_DEVICE_NAME", "") or platform.node()
            ),
            sync_dir=get("PC_AGENT_SYNC_DIR", "").strip(),
            sync_max_bundle_bytes=_parse_connector_int(
                "PC_AGENT_SYNC_MAX_BUNDLE_BYTES",
                get("PC_AGENT_SYNC_MAX_BUNDLE_BYTES", "4000000"),
                4_000_000,
                65_536,
                32_000_000,
            ),
            sync_max_records=_parse_connector_int(
                "PC_AGENT_SYNC_MAX_RECORDS",
                get("PC_AGENT_SYNC_MAX_RECORDS", "500"),
                500,
                1,
                2_000,
            ),
            sync_retention_days=_parse_connector_int(
                "PC_AGENT_SYNC_RETENTION_DAYS",
                get("PC_AGENT_SYNC_RETENTION_DAYS", "30"),
                30,
                1,
                365,
            ),
            sync_conflict_policy=_parse_choice(
                "PC_AGENT_SYNC_CONFLICT_POLICY",
                get("PC_AGENT_SYNC_CONFLICT_POLICY", "newest"),
                ("local", "newest", "remote"),
            ),
            sync_clock_skew_seconds=_parse_connector_int(
                "PC_AGENT_SYNC_CLOCK_SKEW_SECONDS",
                get("PC_AGENT_SYNC_CLOCK_SKEW_SECONDS", "900"),
                900,
                60,
                86_400,
            ),
            ui_locale=_parse_ui_locale(
                get("PC_AGENT_UI_LOCALE", ""),
                data_dir=data_dir,
            ),
            ui_scale=_parse_ui_scale(
                get("PC_AGENT_UI_SCALE", ""),
                data_dir=data_dir,
            ),
            ui_high_contrast=_parse_ui_flag(
                "PC_AGENT_UI_HIGH_CONTRAST",
                get("PC_AGENT_UI_HIGH_CONTRAST", ""),
                data_dir=data_dir,
                pref="high_contrast",
            ),
            ui_reduce_motion=_parse_ui_flag(
                "PC_AGENT_UI_REDUCE_MOTION",
                get("PC_AGENT_UI_REDUCE_MOTION", ""),
                data_dir=data_dir,
                pref="reduce_motion",
            ),
            ui_keyboard_shortcuts=_parse_ui_shortcuts(
                get("PC_AGENT_UI_KEYBOARD_SHORTCUTS", ""),
                data_dir=data_dir,
            ),
            ui_cli_color=_parse_ui_color(
                get("PC_AGENT_UI_CLI_COLOR", ""),
                data_dir=data_dir,
            ),
            connector_allow_private_endpoints=_parse_safety_bool(
                "PC_AGENT_CONNECTOR_ALLOW_PRIVATE_ENDPOINTS",
                get("PC_AGENT_CONNECTOR_ALLOW_PRIVATE_ENDPOINTS", ""),
            ),
            credential_store=credential_store_mode,
            credential_store_backend=credential_status.backend,
            credential_store_available=credential_status.available,
            credential_store_detail=credential_status.detail,
            credential_sources=tuple(credential_sources),
            allow_self_repair=allow_self_repair,
            task_token_budget=task_token_budget,
            task_provider_call_budget=task_provider_call_budget,
            task_tool_budget=task_tool_budget,
            task_step_budget=task_step_budget,
            task_time_budget_seconds=task_time_budget_seconds,
            safety_hard_timeouts=safety_hard_timeouts,
            safety_worker_isolation=safety_worker_isolation,
            safety_max_tool_seconds=safety_max_tool_seconds,
            safety_approval_expiry_minutes=safety_approval_expiry_minutes,
            license_enforcement=license_enforcement,
            license_file=license_file,
            license_public_key=license_public_key,
            license_key_id=license_key_id,
            trial_days=trial_days,
            release_public_key=release_public_key,
            release_key_id=release_key_id,
            require_signed_updates=require_signed_updates,
            online_updates_enabled=online_updates_enabled,
            update_feed_url=update_feed_url,
            update_channel=update_channel,
            online_update_timeout_seconds=online_update_timeout_seconds,
            online_update_max_bytes=online_update_max_bytes,
            auto_upgrade_enabled=auto_upgrade_enabled,
            auto_upgrade_mode=auto_upgrade_mode,
            auto_upgrade_interval_hours=auto_upgrade_interval_hours,
            auto_upgrade_idle_minutes=auto_upgrade_idle_minutes,
            auto_upgrade_maintenance_window=auto_upgrade_maintenance_window,
            auto_upgrade_require_ac_power=auto_upgrade_require_ac_power,
            auto_upgrade_reinstall=auto_upgrade_reinstall,
            gemini_api_key=api_keys["gemini"],
            openrouter_api_key=api_keys["openrouter"],
            openai_api_key=api_keys["openai"],
            openai_compatible_api_key=api_keys["openai-compatible"],
            ollama_api_key=api_keys["ollama"],
            anthropic_api_key=api_keys["anthropic"],
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
        return _apply_settings_profile(constructed, values)


def _apply_settings_profile(settings: Settings, values: Mapping[str, str]) -> Settings:
    """Profile values fill gaps left by explicit environment values only."""
    from .profiles import PROFILE_FIELDS, active_profile_name, load_profile

    data_dir = settings.data_dir
    profile_name, _source = active_profile_name(
        data_dir, environment_value=values.get("PC_AGENT_PROFILE")
    )
    if profile_name is None:
        return settings
    profile_values = load_profile(data_dir, profile_name)
    overrides: dict[str, object] = {"active_profile": profile_name}
    for field_name, raw_value in profile_values.items():
        env_name = PROFILE_FIELDS[field_name]
        if env_name in values and str(values[env_name]).strip():
            continue  # Explicit environment always wins over a profile.
        overrides[field_name] = _profile_value(field_name, raw_value, profile_name)
    return replace(settings, **overrides)  # type: ignore[arg-type]


def _profile_value(field_name: str, raw_value: object, profile_name: str) -> object:
    def invalid(message: str) -> ConfigError:
        return ConfigError(f"Settings profile '{profile_name}': {message}")

    try:
        if isinstance(raw_value, list):
            joined = ",".join(str(item) for item in raw_value)
        else:
            joined = str(raw_value)
        if field_name == "log_level":
            value = joined.strip().upper()
            if value not in _ALLOWED_LOG_LEVELS:
                raise invalid("log_level must be a standard level name.")
            return value
        if field_name == "log_format":
            value = joined.strip().lower()
            if value not in _ALLOWED_LOG_FORMATS:
                raise invalid("log_format must be 'text' or 'json'.")
            return value
        if field_name == "provider_order":
            return _parse_provider_order(joined)
        if field_name == "command_allowlist":
            return _parse_command_allowlist(joined)
        if field_name == "max_recovery_attempts":
            return _parse_max_recovery_attempts(joined)
        if field_name == "safety_max_tool_seconds":
            return _parse_safety_max_tool_seconds(joined)
        if field_name == "safety_approval_expiry_minutes":
            return _parse_safety_approval_expiry_minutes(joined)
        if field_name == "web_timeout_seconds":
            return _parse_web_timeout(joined)
        if field_name == "web_max_bytes":
            return _parse_web_max_bytes(joined)
        if field_name == "mission_max_parallel":
            return _parse_mission_max_parallel(joined)
        if field_name == "scheduler_interval_seconds":
            return _parse_scheduler_interval(joined)
        if field_name in _PROFILE_INT_BUDGETS:
            env_name, maximum = _PROFILE_INT_BUDGETS[field_name]
            if field_name == "task_step_budget":
                return _parse_task_step_budget(joined)
            return _parse_nonnegative_budget(env_name, joined, maximum=maximum)
        if field_name == "task_time_budget_seconds":
            return _parse_task_time_budget(joined)
    except ConfigError as exc:
        raise invalid(str(exc)) from exc
    raise invalid(f"unsupported field '{field_name}'.")


_PROFILE_INT_BUDGETS = {
    "task_token_budget": ("PC_AGENT_TASK_TOKEN_BUDGET", 10_000_000),
    "task_provider_call_budget": ("PC_AGENT_TASK_PROVIDER_CALL_BUDGET", 1000),
    "task_tool_budget": ("PC_AGENT_TASK_TOOL_BUDGET", 1000),
    "task_step_budget": ("PC_AGENT_TASK_STEP_BUDGET", 25),
}


def _parse_api_host(raw: str) -> str:
    host = raw.strip()
    if host.lower() == "localhost":
        return host.lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ConfigError(
            "PC_AGENT_API_HOST must be a loopback address (127.0.0.1, ::1, or localhost)."
        ) from exc
    if not address.is_loopback:
        raise ConfigError(
            "PC_AGENT_API_HOST must be loopback-only; the local API is never exposed "
            "on a network interface in this phase."
        )
    return str(address)


def _parse_api_port(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_API_PORT must be a port number.") from exc
    if value < 1 or value > 65535:
        raise ConfigError("PC_AGENT_API_PORT must be between 1 and 65535.")
    return value


def _parse_mission_max_parallel(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_MISSION_MAX_PARALLEL must be a whole number.") from exc
    if value < 1 or value > 8:
        raise ConfigError("PC_AGENT_MISSION_MAX_PARALLEL must be between 1 and 8.")
    return value


def _parse_scheduler_interval(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_SCHEDULER_INTERVAL_SECONDS must be a whole number.") from exc
    if value < 0 or value > 3600:
        raise ConfigError(
            "PC_AGENT_SCHEDULER_INTERVAL_SECONDS must be between 0 (off) and 3600."
        )
    return value


def _parse_provider_prices(
    raw: str | None,
) -> tuple[tuple[str, float, float], ...]:
    """Parse PC_AGENT_PROVIDER_PRICES: {"provider[/model]": {"input_per_1k": x, "output_per_1k": y}}."""
    if raw is None or not raw.strip():
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("PC_AGENT_PROVIDER_PRICES must be valid JSON.") from exc
    if not isinstance(parsed, Mapping):
        raise ConfigError("PC_AGENT_PROVIDER_PRICES must be a JSON object.")
    prices: list[tuple[str, float, float]] = []
    for key, value in parsed.items():
        name = str(key).strip().lower()
        if not name or not isinstance(value, Mapping):
            raise ConfigError(
                "PC_AGENT_PROVIDER_PRICES entries must map a provider or "
                "provider/model name to an object with input_per_1k/output_per_1k."
            )
        try:
            input_cost = float(value.get("input_per_1k", 0))
            output_cost = float(value.get("output_per_1k", 0))
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"PC_AGENT_PROVIDER_PRICES entry '{name}' has a non-numeric price."
            ) from exc
        if input_cost < 0 or output_cost < 0 or input_cost > 1000 or output_cost > 1000:
            raise ConfigError(
                f"PC_AGENT_PROVIDER_PRICES entry '{name}' prices must be between 0 and 1000."
            )
        prices.append((name, input_cost, output_cost))
    return tuple(sorted(prices))


def _parse_model_context_tokens(
    raw: str | None,
) -> tuple[tuple[str, int], ...]:
    """Parse PC_AGENT_MODEL_CONTEXT_TOKENS: {"provider[/model]": tokens}."""
    if raw is None or not raw.strip():
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("PC_AGENT_MODEL_CONTEXT_TOKENS must be valid JSON.") from exc
    if not isinstance(parsed, Mapping):
        raise ConfigError("PC_AGENT_MODEL_CONTEXT_TOKENS must be a JSON object.")
    entries: list[tuple[str, int]] = []
    for key, value in parsed.items():
        name = str(key).strip().lower()
        try:
            tokens = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"PC_AGENT_MODEL_CONTEXT_TOKENS entry '{name}' must be a whole number."
            ) from exc
        if not name or tokens < 1 or tokens > 100_000_000:
            raise ConfigError(
                f"PC_AGENT_MODEL_CONTEXT_TOKENS entry '{name}' must be between 1 and 100000000."
            )
        entries.append((name, tokens))
    return tuple(sorted(entries))


def _parse_embedding_dimensions(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(
            "PC_AGENT_EMBEDDING_DIMENSIONS must be a whole number."
        ) from exc
    if value < 64 or value > 2048:
        raise ConfigError("PC_AGENT_EMBEDDING_DIMENSIONS must be between 64 and 2048.")
    return value


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


def _parse_allow_subprocess(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"", "0", "false", "no", "off"}:
        return False
    raise ConfigError(
        "PC_AGENT_ALLOW_SUBPROCESS must be a boolean (1/0, true/false, yes/no, on/off)."
    )


def _parse_command_allowlist(raw: str) -> tuple[str, ...]:
    names: list[str] = []
    for item in raw.split(","):
        name = item.strip().lower()
        if not name:
            continue
        if "/" in name or "\\" in name or len(name) > 100:
            raise ConfigError(
                "PC_AGENT_COMMAND_ALLOWLIST entries must be bare program names, not paths."
            )
        if name not in names:
            names.append(name)
    return tuple(names)


def _parse_allow_browser(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"", "0", "false", "no", "off"}:
        return False
    raise ConfigError(
        "PC_AGENT_ALLOW_BROWSER must be a boolean (1/0, true/false, yes/no, on/off)."
    )


def _parse_browser_port(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_BROWSER_PORT must be a port number.") from exc
    if value < 1 or value > 65535:
        raise ConfigError("PC_AGENT_BROWSER_PORT must be between 1 and 65535.")
    return value


def _parse_browser_timeout(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_BROWSER_TIMEOUT_SECONDS must be a number.") from exc
    if value <= 0 or value > 120:
        raise ConfigError(
            "PC_AGENT_BROWSER_TIMEOUT_SECONDS must be greater than 0 and at most 120."
        )
    return value


def _parse_allow_desktop_automation(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"", "0", "false", "no", "off"}:
        return False
    raise ConfigError(
        "PC_AGENT_ALLOW_DESKTOP_AUTOMATION must be a boolean "
        "(1/0, true/false, yes/no, on/off)."
    )


def _parse_desktop_automation_timeout(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(
            "PC_AGENT_DESKTOP_AUTOMATION_TIMEOUT_SECONDS must be a number."
        ) from exc
    if value <= 0 or value > 300:
        raise ConfigError(
            "PC_AGENT_DESKTOP_AUTOMATION_TIMEOUT_SECONDS must be greater "
            "than 0 and at most 300."
        )
    return value


def _parse_allow_vision(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"", "0", "false", "no", "off"}:
        return False
    raise ConfigError(
        "PC_AGENT_ALLOW_VISION must be a boolean (1/0, true/false, yes/no, on/off)."
    )


def _parse_vision_max_image_bytes(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_VISION_MAX_IMAGE_BYTES must be an integer.") from exc
    if value < 1024 or value > 20_000_000:
        raise ConfigError(
            "PC_AGENT_VISION_MAX_IMAGE_BYTES must be between 1024 and 20000000."
        )
    return value


_VOICE_ENGINES = frozenset(
    {
        "auto",
        "windows-sapi",
        "faster-whisper",
        "whisper-cpp",
        "openai-compatible",
        "espeak-ng",
    }
)


def _parse_voice_engine(name: str, raw: str) -> str:
    value = raw.strip().lower() or "auto"
    if value not in _VOICE_ENGINES:
        allowed = ", ".join(sorted(_VOICE_ENGINES))
        raise ConfigError(f"{name} must be one of: {allowed}.")
    return value


def _parse_voice_max_seconds(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_VOICE_MAX_SECONDS must be a number.") from exc
    if value < 1 or value > 300:
        raise ConfigError(
            "PC_AGENT_VOICE_MAX_SECONDS must be at least 1 and at most 300."
        )
    return value


def _parse_voice_sample_rate(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_VOICE_SAMPLE_RATE must be an integer.") from exc
    if value not in {8_000, 16_000, 22_050, 44_100, 48_000}:
        raise ConfigError(
            "PC_AGENT_VOICE_SAMPLE_RATE must be one of 8000, 16000, 22050,"
            " 44100, 48000."
        )
    return value


def _parse_credential_store(raw: str) -> str:
    value = raw.strip().lower() or "auto"
    if value not in _ALLOWED_CREDENTIAL_STORES:
        raise ConfigError(
            "PC_AGENT_CREDENTIAL_STORE must be one of: auto, windows, linux, macos, env."
        )
    return value


def _parse_allow_self_repair(raw: str) -> bool:
    return _parse_safety_bool("PC_AGENT_ALLOW_SELF_REPAIR", raw)


def _parse_safety_bool(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"", "0", "false", "no", "off"}:
        return False
    raise ConfigError(
        f"{name} must be a boolean (1/0, true/false, yes/no, on/off)."
    )


def _parse_safety_max_tool_seconds(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_SAFETY_MAX_TOOL_SECONDS must be a number.") from exc
    if value <= 0 or value > 600:
        raise ConfigError(
            "PC_AGENT_SAFETY_MAX_TOOL_SECONDS must be greater than 0 and at most 600."
        )
    return value


def _parse_safety_approval_expiry_minutes(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(
            "PC_AGENT_SAFETY_APPROVAL_EXPIRY_MINUTES must be an integer."
        ) from exc
    if value < 1 or value > 10_080:
        raise ConfigError(
            "PC_AGENT_SAFETY_APPROVAL_EXPIRY_MINUTES must be between 1 and 10080."
        )
    return value


def _parse_require_signed_updates(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"", "0", "false", "no", "off"}:
        return False
    raise ConfigError(
        "PC_AGENT_REQUIRE_SIGNED_UPDATES must be a boolean "
        "(1/0, true/false, yes/no, on/off)."
    )


def _parse_online_updates_enabled(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"", "0", "false", "no", "off"}:
        return False
    raise ConfigError(
        "PC_AGENT_ONLINE_UPDATES must be a boolean "
        "(1/0, true/false, yes/no, on/off)."
    )


def _parse_auto_upgrade_boolean(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"", "0", "false", "no", "off"}:
        return False
    raise ConfigError(
        f"{name} must be a boolean (1/0, true/false, yes/no, on/off)."
    )


def _parse_auto_upgrade_enabled(raw: str) -> bool:
    return _parse_auto_upgrade_boolean("PC_AGENT_AUTO_UPGRADE", raw)


def _parse_auto_upgrade_mode(raw: str) -> str:
    value = raw.strip().lower().replace("_", "-") or "stage-only"
    if value not in _ALLOWED_AUTO_UPGRADE_MODES:
        raise ConfigError(
            "PC_AGENT_AUTO_UPGRADE_MODE must be one of: "
            "stage-only, apply-when-idle."
        )
    return value


def _parse_auto_upgrade_interval_hours(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(
            "PC_AGENT_AUTO_UPGRADE_INTERVAL_HOURS must be an integer."
        ) from exc
    if value < 1 or value > 168:
        raise ConfigError(
            "PC_AGENT_AUTO_UPGRADE_INTERVAL_HOURS must be between 1 and 168."
        )
    return value


def _parse_auto_upgrade_idle_minutes(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(
            "PC_AGENT_AUTO_UPGRADE_IDLE_MINUTES must be an integer."
        ) from exc
    if value < 0 or value > 240:
        raise ConfigError(
            "PC_AGENT_AUTO_UPGRADE_IDLE_MINUTES must be between 0 and 240."
        )
    return value


def _parse_auto_upgrade_window(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    match = re.fullmatch(
        r"([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)", value
    )
    if match is None or match.group(1, 2) == match.group(3, 4):
        raise ConfigError(
            "PC_AGENT_AUTO_UPGRADE_MAINTENANCE_WINDOW must be HH:MM-HH:MM "
            "with different start and end times."
        )
    return value


def _parse_auto_upgrade_require_ac_power(raw: str) -> bool:
    return _parse_auto_upgrade_boolean(
        "PC_AGENT_AUTO_UPGRADE_REQUIRE_AC_POWER", raw
    )


def _parse_auto_upgrade_reinstall(raw: str) -> bool:
    return _parse_auto_upgrade_boolean("PC_AGENT_AUTO_UPGRADE_REINSTALL", raw)


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parse_update_feed_url(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if (
        len(value) > 4096
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in value)
    ):
        raise ConfigError("PC_AGENT_UPDATE_FEED_URL must be a single safe URL.")
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or not host:
        raise ConfigError("PC_AGENT_UPDATE_FEED_URL must be a valid http(s) URL.")
    if parsed.scheme == "http" and not _is_loopback_host(host):
        raise ConfigError(
            "PC_AGENT_UPDATE_FEED_URL must use HTTPS unless it points to loopback."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError(
            "PC_AGENT_UPDATE_FEED_URL must not contain credentials, a query "
            "string, or a fragment."
        )
    if not parsed.path or parsed.path == "/":
        raise ConfigError("PC_AGENT_UPDATE_FEED_URL must include a feed path.")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ConfigError("PC_AGENT_UPDATE_FEED_URL has an invalid port.") from exc
    return value


def _parse_update_channel(raw: str) -> str:
    return _parse_key_id("PC_AGENT_UPDATE_CHANNEL", raw.strip().lower())


def _parse_online_update_timeout(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(
            "PC_AGENT_ONLINE_UPDATE_TIMEOUT_SECONDS must be a number."
        ) from exc
    if not math.isfinite(value) or value <= 0 or value > 120:
        raise ConfigError(
            "PC_AGENT_ONLINE_UPDATE_TIMEOUT_SECONDS must be greater than 0 "
            "and at most 120."
        )
    return value


def _parse_online_update_max_bytes(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_ONLINE_UPDATE_MAX_BYTES must be an integer.") from exc
    if value < 1_000_000 or value > 100_000_000:
        raise ConfigError(
            "PC_AGENT_ONLINE_UPDATE_MAX_BYTES must be between 1000000 and 100000000."
        )
    return value


def _parse_nonnegative_budget(name: str, raw: str, *, maximum: int) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer.") from exc
    if value < 0 or value > maximum:
        raise ConfigError(f"{name} must be between 0 and {maximum} (0 means unlimited).")
    return value


def _parse_task_step_budget(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_TASK_STEP_BUDGET must be an integer.") from exc
    if value < 1 or value > 20:
        raise ConfigError("PC_AGENT_TASK_STEP_BUDGET must be between 1 and 20.")
    return value


def _parse_task_time_budget(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_TASK_TIME_BUDGET_SECONDS must be a number.") from exc
    if not math.isfinite(value) or value < 0 or value > 86_400:
        raise ConfigError(
            "PC_AGENT_TASK_TIME_BUDGET_SECONDS must be between 0 and 86400 "
            "(0 means unlimited)."
        )
    return value


def _parse_command_timeout(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError("PC_AGENT_COMMAND_TIMEOUT_SECONDS must be a number.") from exc
    if value <= 0 or value > 300:
        raise ConfigError(
            "PC_AGENT_COMMAND_TIMEOUT_SECONDS must be greater than 0 and at most 300."
        )
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


def _parse_key_id(name: str, raw: str) -> str:
    value = raw.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
        raise ConfigError(
            f"{name} must be 1-64 letters, digits, '.', '_' or '-'."
        )
    return value


def _parse_license_key_id(raw: str) -> str:
    return _parse_key_id("PC_AGENT_LICENSE_KEY_ID", raw)


def _parse_release_key_id(raw: str) -> str | None:
    if not raw.strip():
        return None
    return _parse_key_id("PC_AGENT_RELEASE_KEY_ID", raw)


def _parse_public_key(name: str, raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ConfigError(f"{name} must be unpadded base64url data.")
    padded = value + "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True
        )
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise ConfigError(f"{name} must be base64url data.") from exc
    if len(decoded) != 32:
        raise ConfigError(f"{name} must decode to a 32-byte Ed25519 key.")
    return value


def _parse_license_public_key(raw: str) -> str | None:
    return _parse_public_key("PC_AGENT_LICENSE_PUBLIC_KEY", raw)


def _parse_release_public_key(raw: str) -> str | None:
    return _parse_public_key("PC_AGENT_RELEASE_PUBLIC_KEY", raw)


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


def _mask_path(value: str) -> str:
    """Non-secret label for a configured path (never the full local path)."""
    if not value:
        return "none"
    name = Path(value).name or value
    return f"configured ({name})"


def _parse_plugin_keys(raw: str, root: Path) -> str:
    """Validate a plugin trust store: JSON object of key id -> public key.

    Accepts either inline JSON or a path to a JSON file. Every value must decode
    to 32 raw bytes (base64 or hex); the canonical, sorted JSON is stored so the
    rest of the product never re-parses untrusted input differently.
    """

    import base64 as _base64
    import binascii as _binascii
    import json as _json

    text = str(raw or "").strip()
    if not text:
        return ""
    candidate = Path(text)
    if not text.startswith("{") and candidate.suffix and not candidate.is_absolute():
        candidate = root / text
    if not text.startswith("{") and candidate.is_file():
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(
                "PC_AGENT_PLUGIN_TRUSTED_KEYS file could not be read."
            ) from exc
    try:
        payload = _json.loads(text)
    except ValueError as exc:
        raise ConfigError(
            "PC_AGENT_PLUGIN_TRUSTED_KEYS must be a JSON object of key id to public key, "
            "or a path to such a file."
        ) from exc
    if not isinstance(payload, dict):
        raise ConfigError("PC_AGENT_PLUGIN_TRUSTED_KEYS must be a JSON object.")
    if len(payload) > 32:
        raise ConfigError("PC_AGENT_PLUGIN_TRUSTED_KEYS may hold at most 32 keys.")
    cleaned: dict[str, str] = {}
    for key_id, value in payload.items():
        name = str(key_id or "").strip()
        if not name or len(name) > 120:
            raise ConfigError("Trusted plugin key ids must be 1-120 characters.")
        blob = str(value or "").strip()
        if not blob:
            raise ConfigError(f"Trusted plugin key '{name}' must not be empty.")
        raw_bytes: bytes | None = None
        try:
            raw_bytes = _base64.b64decode(blob, validate=True)
        except (_binascii.Error, ValueError):
            raw_bytes = None
        if raw_bytes is None or len(raw_bytes) != 32:
            try:
                raw_bytes = bytes.fromhex(blob)
            except ValueError as exc:
                raise ConfigError(
                    f"Trusted plugin key '{name}' must be 32 raw bytes as base64 or hex."
                ) from exc
        if len(raw_bytes) != 32:
            raise ConfigError(f"Trusted plugin key '{name}' must be 32 raw bytes.")
        cleaned[name] = _base64.b64encode(raw_bytes).decode("ascii")
    return _json.dumps(cleaned, sort_keys=True, separators=(",", ":"))


def _parse_choice(name: str, raw: str, allowed: tuple[str, ...]) -> str:
    """Validate a small closed vocabulary; refuse anything else by name."""

    value = str(raw or "").strip().lower()
    if value not in allowed:
        raise ConfigError(f"{name} must be one of: {', '.join(allowed)}.")
    return value


def _parse_device_name(raw: str) -> str:
    """A short, printable device label (never a path or a secret)."""

    text = " ".join(str(raw or "").split())
    if len(text) > 60:
        raise ConfigError("PC_AGENT_SYNC_DEVICE_NAME must be 60 characters or fewer.")
    if any(ord(char) < 32 for char in text):
        raise ConfigError("PC_AGENT_SYNC_DEVICE_NAME must not contain control characters.")
    return text or "this-device"


def _parse_clock(name: str, raw: str) -> str:
    """Validate a 24-hour ``HH:MM`` local time and return it canonical."""

    text = str(raw or "").strip()
    if not text:
        return "08:00"
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ConfigError(f"{name} must be a local time such as 08:00.")
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        raise ConfigError(f"{name} must be a valid local time (00:00-23:59).")
    return f"{hour:02d}:{minute:02d}"


def _parse_quiet_hours(raw: str) -> str | None:
    """Validate ``HH:MM-HH:MM`` (wrap-around allowed) or return ``None``."""

    text = str(raw or "").strip()
    if not text:
        return None
    if text.count("-") != 1:
        raise ConfigError(
            "PC_AGENT_QUIET_HOURS must look like 22:00-07:00 (or be empty)."
        )
    start, end = (part.strip() for part in text.split("-", 1))
    canonical_start = _parse_clock("PC_AGENT_QUIET_HOURS start", start)
    canonical_end = _parse_clock("PC_AGENT_QUIET_HOURS end", end)
    if canonical_start == canonical_end:
        raise ConfigError("PC_AGENT_QUIET_HOURS start and end must differ.")
    return f"{canonical_start}-{canonical_end}"


def _parse_connector_int(name: str, raw: str, default: int, low: int, high: int) -> int:
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number.") from exc
    if value < low or value > high:
        raise ConfigError(f"{name} must be between {low} and {high}.")
    return value


def _parse_connector_float(name: str, raw: str, default: float, low: float, high: float) -> float:
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number.") from exc
    if value < low or value > high:
        raise ConfigError(f"{name} must be between {low} and {high}.")
    return value


# --------------------------------------------------------------------------- #
# Transformation Phase 17 interface settings
# --------------------------------------------------------------------------- #
_UI_LOCALES = frozenset({"en", "es", "ur", "ar"})
_UI_COLORS = frozenset({"auto", "always", "never"})


def _ui_prefs(data_dir: Path) -> dict[str, object]:
    from .ui_prefs import load_prefs

    return load_prefs(data_dir)


def _parse_ui_locale(raw: str, *, data_dir: Path) -> str:
    """Environment first, then the persisted preference, then English."""

    candidate = str(raw or "").strip().lower()
    if not candidate:
        candidate = str(_ui_prefs(data_dir).get("locale", "") or "").strip().lower()
    if not candidate:
        return "en"
    base = candidate.replace("_", "-").split("-", 1)[0]
    if base not in _UI_LOCALES:
        raise ConfigError(
            f"PC_AGENT_UI_LOCALE '{raw}' is not supported. Choose one of: "
            + ", ".join(sorted(_UI_LOCALES))
        )
    return base


def _parse_ui_scale(raw: str, *, data_dir: Path) -> float:
    candidate = str(raw or "").strip()
    if not candidate:
        candidate = str(_ui_prefs(data_dir).get("scale", "") or "").strip()
    if not candidate:
        return 1.0
    try:
        value = float(candidate)
    except ValueError as exc:
        raise ConfigError(f"PC_AGENT_UI_SCALE '{raw}' is not a number.") from exc
    if not 0.8 <= value <= 2.0:
        raise ConfigError("PC_AGENT_UI_SCALE must be between 0.8 and 2.0 (80%-200%).")
    return round(value, 2)


def _parse_ui_flag(
    name: str, raw: str, *, data_dir: Path, pref: str
) -> bool:
    candidate = str(raw or "").strip().lower()
    if not candidate:
        stored = _ui_prefs(data_dir).get(pref)
        if isinstance(stored, bool):
            return stored
        if isinstance(stored, str) and stored.strip().lower() in {"1", "true", "yes", "on"}:
            return True
        if isinstance(stored, str) and stored.strip().lower() in {"0", "false", "no", "off"}:
            return False
        return False
    if candidate in {"1", "true", "yes", "on"}:
        return True
    if candidate in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be 1/0, true/false, yes/no or on/off.")


def _parse_ui_shortcuts(raw: str, *, data_dir: Path) -> bool:
    candidate = str(raw or "").strip().lower()
    if not candidate:
        stored = _ui_prefs(data_dir).get("keyboard_shortcuts")
        if isinstance(stored, bool):
            return stored
        return True
    if candidate in {"1", "true", "yes", "on"}:
        return True
    if candidate in {"0", "false", "no", "off"}:
        return False
    raise ConfigError("PC_AGENT_UI_KEYBOARD_SHORTCUTS must be 1/0, true/false, yes/no or on/off.")


def _parse_ui_color(raw: str, *, data_dir: Path) -> str:
    candidate = str(raw or "").strip().lower()
    if not candidate:
        candidate = str(_ui_prefs(data_dir).get("cli_color", "") or "").strip().lower()
    if not candidate:
        return "auto"
    if candidate not in _UI_COLORS:
        raise ConfigError(
            "PC_AGENT_UI_CLI_COLOR must be auto, always or never."
        )
    return candidate
