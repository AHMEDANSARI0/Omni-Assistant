"""Explicit live verification for the direct Anthropic provider.

The Phase 19 adapter is normally exercised through the provider router,
which can retry or fall back to another configured provider.  This module
provides the missing evidence path: one direct, bounded Anthropic Messages
API request, with no fallback, no router retry and no image input.  It
records only non-secret metadata and hashes in a JSON report that can be
retained and revalidated offline.

The command is intentionally direct-only.  No planner, task or registered
tool can invoke it, and it never reads an API key from a command-line value.
The key comes from the already-loaded application settings (environment or
the platform credential store) and is never written to the report.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import __version__
from .config import Settings
from .errors import PcAgentError, ProviderError
from .logging_config import redact_text
from .providers.anthropic import AnthropicProvider
from .providers.base import CompletionRequest, JsonTransport

ANTHROPIC_VERIFICATION_REPORT = "anthropic-live-verification-report.json"
ANTHROPIC_VERIFICATION_EXPECTED_RESPONSE = "PC_AGENT_ANTHROPIC_OK"

_REPORT_FORMAT = 1
_APP_NAME = "Self-Healing PC Agent"
_MAX_REPORT_BYTES = 1_000_000
_MAX_RESPONSE_CHARS = 4_096
_MAX_OUTPUT_TOKENS = 32
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CHECK_NAMES = (
    "configuration_present",
    "credential_present",
    "endpoint_allowed",
    "single_direct_request",
    "live_response_received",
    "provider_identity",
    "model_reported",
    "expected_response",
    "usage_reported",
    "secret_not_recorded",
)
_ALLOWED_CREDENTIAL_SOURCES = frozenset(
    {
        "environment",
        "windows-credential-manager",
        "linux-secret-service",
        "macos-keychain",
        "direct-settings",
        "missing",
    }
)
_SYSTEM_INSTRUCTION = (
    "You are performing a software connectivity check. Follow the user's "
    "exact-output instruction."
)
_PROMPT = (
    "Reply with exactly this token and nothing else: "
    + ANTHROPIC_VERIFICATION_EXPECTED_RESPONSE
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _platform_name() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return re.sub(r"[^a-z0-9_.-]+", "-", sys.platform.lower())[:32] or "unknown"


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _report_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _safe_text(value: str, *, limit: int = 300) -> str:
    cleaned = "".join(
        character
        for character in value.strip()
        if character == "\n" or (ord(character) >= 32 and ord(character) != 127)
    )
    return cleaned[:limit]


def _sanitize_error(error: BaseException | str, secret: str | None) -> str:
    text = redact_text(str(error))
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return _safe_text(text, limit=500) or "Anthropic verification failed."


def _valid_short_value(value: str, *, maximum: int) -> bool:
    return bool(value.strip()) and len(value) <= maximum and not any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    )


def _endpoint_origin(base_url: str) -> tuple[str | None, str]:
    value = base_url.strip()
    if not value or len(value) > 2_048:
        return None, "The Anthropic base URL is missing or too long."
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return None, "The Anthropic base URL is invalid."
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"https", "http"} or not hostname:
        return None, "The Anthropic base URL must be a valid HTTP(S) URL."
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None, "The Anthropic base URL must not contain credentials, a query or a fragment."
    loopback = hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http" and not loopback:
        return None, "The Anthropic base URL must use HTTPS outside loopback."
    host = f"[{hostname}]" if ":" in hostname else hostname
    origin = f"{parsed.scheme}://{host}"
    if port is not None:
        origin += f":{port}"
    return origin, "The Anthropic endpoint satisfies the TLS/loopback policy."


def _normalize_response(content: str) -> str:
    normalized = content.strip()
    if normalized.startswith("```"):
        lines = normalized.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        normalized = "\n".join(lines).strip()
    return normalized.strip("`\"'").rstrip(".").strip()


def _usage_value(usage: Any, name: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > 10_000_000:
        return None
    return value


@dataclass(slots=True)
class AnthropicVerificationCheck:
    name: str
    status: str = "pending"
    detail: str = ""

    def set(self, status: str, detail: str) -> None:
        self.status = status
        self.detail = _safe_text(detail)

    def to_dict(self) -> dict[str, str]:
        return {"detail": self.detail, "name": self.name, "status": self.status}


@dataclass(slots=True)
class AnthropicVerificationReport:
    report_path: Path
    started_at: str
    requested_model: str
    endpoint_origin: str
    credential_source: str
    timeout_seconds: float
    status: str = "running"
    finished_at: str | None = None
    response_model: str | None = None
    network_call_made: bool = False
    response_sha256: str | None = None
    response_chars: int | None = None
    expected_response_matched: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    elapsed_ms: float = 0.0
    error: str | None = None
    checks: list[AnthropicVerificationCheck] = field(
        default_factory=lambda: [
            AnthropicVerificationCheck(name) for name in _CHECK_NAMES
        ]
    )

    def check(self, name: str) -> AnthropicVerificationCheck:
        for check in self.checks:
            if check.name == name:
                return check
        raise KeyError(name)

    def summary(self) -> str:
        return (
            f"status={self.status}; provider=anthropic; "
            f"model={self.response_model or self.requested_model}; "
            f"report={self.report_path}"
        )

    def _base_dict(self) -> dict[str, Any]:
        return {
            "app": _APP_NAME,
            "checks": [check.to_dict() for check in self.checks],
            "credential_source": self.credential_source,
            "elapsed_ms": round(float(self.elapsed_ms), 3),
            "endpoint_origin": self.endpoint_origin,
            "error": self.error,
            "expected_response": ANTHROPIC_VERIFICATION_EXPECTED_RESPONSE,
            "fallback_used": False,
            "finished_at": self.finished_at,
            "format": _REPORT_FORMAT,
            "max_output_tokens": _MAX_OUTPUT_TOKENS,
            "network_call_made": self.network_call_made,
            "platform": _platform_name(),
            "provider": "anthropic",
            "requested_model": self.requested_model,
            "response": {
                "chars": self.response_chars,
                "exact_expected": self.expected_response_matched,
                "model": self.response_model,
                "sha256": self.response_sha256,
            },
            "retries_used": 0,
            "started_at": self.started_at,
            "status": self.status,
            "timeout_seconds": float(self.timeout_seconds),
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            },
            "version": __version__,
            "vision_status": "not_tested",
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._base_dict()
        payload["report_sha256"] = _report_hash(payload)
        return payload

    def write(self) -> None:
        data = (
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        if len(data) > _MAX_REPORT_BYTES:
            raise PcAgentError("Anthropic verification report is too large.")
        if self.report_path.exists():
            raise PcAgentError(
                f"Anthropic verification report already exists: {self.report_path.name}."
            )
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.report_path.with_name(
            f".{self.report_path.name}.tmp-{secrets.token_hex(4)}"
        )
        try:
            temporary.write_bytes(data)
            if self.report_path.exists():
                raise PcAgentError(
                    f"Anthropic verification report already exists: {self.report_path.name}."
                )
            temporary.replace(self.report_path)
        finally:
            if temporary.exists():
                temporary.unlink()


@dataclass(frozen=True, slots=True)
class AnthropicVerification:
    report_path: Path
    version: str
    status: str
    requested_model: str
    response_model: str
    endpoint_origin: str
    credential_source: str
    response_sha256: str
    report_sha256: str

    def summary(self) -> str:
        return (
            f"status={self.status}; version={self.version}; provider=anthropic; "
            f"model={self.response_model}; report_sha256={self.report_sha256[:16]}..."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "credential_source": self.credential_source,
            "endpoint_origin": self.endpoint_origin,
            "provider": "anthropic",
            "report": str(self.report_path),
            "report_sha256": self.report_sha256,
            "requested_model": self.requested_model,
            "response_model": self.response_model,
            "response_sha256": self.response_sha256,
            "status": self.status,
            "version": self.version,
            "vision_status": "not_tested",
        }


def _resolve_report_path(settings: Settings, report_path: Path | None) -> Path:
    if report_path is None:
        stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
        candidate = (
            settings.data_dir
            / "provider-verification"
            / f"anthropic-live-verification-{stamp}.json"
        )
    else:
        candidate = report_path.expanduser()
    if candidate.suffix.lower() != ".json":
        raise PcAgentError("Anthropic verification report output must end with .json.")
    if candidate.exists():
        raise PcAgentError(
            f"Anthropic verification report already exists: {candidate.name}."
        )
    return candidate.resolve()


def _finish_report(
    report: AnthropicVerificationReport,
    *,
    started_monotonic: float,
    secret: str | None,
) -> AnthropicVerificationReport:
    for check in report.checks:
        if check.status == "pending":
            check.set("skipped", "This check was not reached.")
    report.finished_at = _format_time(_utc_now())
    report.elapsed_ms = max(0.0, (time.monotonic() - started_monotonic) * 1_000)
    preliminary = json.dumps(report._base_dict(), ensure_ascii=False, sort_keys=True)
    if secret and secret in preliminary:
        report.check("secret_not_recorded").set(
            "failed", "Credential material would have been present in the report."
        )
        report.error = "Anthropic verification report failed its secret-safety check."
    else:
        report.check("secret_not_recorded").set(
            "passed", "The report contains no API key or response text."
        )
    all_passed = all(check.status == "passed" for check in report.checks)
    report.status = "passed" if all_passed and report.error is None else "failed"
    if report.status != "passed" and report.error is None:
        report.error = "One or more Anthropic live-verification checks failed."
    report.write()
    return report


def run_anthropic_live_verification(
    settings: Settings,
    report_path: Path | None = None,
    *,
    transport: JsonTransport | None = None,
) -> AnthropicVerificationReport:
    """Run one direct Anthropic request and retain a secret-safe report.

    A failed configuration or provider request still produces a failed
    report and returns it; only an unsafe/unwritable report destination is
    raised as an exception.  The injected transport is for deterministic
    tests.  Production uses the adapter's bounded HTTPS transport.
    """

    destination = _resolve_report_path(settings, report_path)
    started = _utc_now()
    started_monotonic = time.monotonic()
    secret = settings.anthropic_api_key
    source = settings.credential_source("anthropic")
    if secret and source == "missing":
        source = "direct-settings"
    if source not in _ALLOWED_CREDENTIAL_SOURCES:
        source = "direct-settings" if secret else "missing"
    endpoint_origin, endpoint_detail = _endpoint_origin(settings.anthropic_base_url)
    report = AnthropicVerificationReport(
        report_path=destination,
        started_at=_format_time(started),
        requested_model=_safe_text(settings.anthropic_model or "", limit=200),
        endpoint_origin=endpoint_origin or "",
        credential_source=source,
        timeout_seconds=float(settings.provider_timeout_seconds or 0),
    )

    configuration_problems: list[str] = []
    if not _valid_short_value(report.requested_model, maximum=200):
        configuration_problems.append("model")
    if not (0 < report.timeout_seconds <= 300):
        configuration_problems.append("timeout")
    report.check("configuration_present").set(
        "failed" if configuration_problems else "passed",
        (
            "Invalid Anthropic configuration: "
            + ", ".join(configuration_problems)
            + "."
        )
        if configuration_problems
        else "Anthropic model and timeout settings are present and bounded.",
    )

    secret_valid = bool(
        secret
        and secret == secret.strip()
        and len(secret) <= 1_024
        and not any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in secret
        )
    )
    report.check("credential_present").set(
        "passed" if secret_valid else "failed",
        (
            f"An Anthropic credential is available from {source}; its value was not recorded."
            if secret_valid
            else "No usable Anthropic credential is configured."
        ),
    )
    report.check("endpoint_allowed").set(
        "passed" if endpoint_origin else "failed", endpoint_detail
    )

    if configuration_problems or not secret_valid or endpoint_origin is None:
        report.error = (
            "Anthropic live verification was not attempted because the "
            "configuration is incomplete or unsafe."
        )
        return _finish_report(
            report, started_monotonic=started_monotonic, secret=secret
        )

    provider = AnthropicProvider(
        api_key=secret,
        model=report.requested_model,
        base_url=settings.anthropic_base_url,
        timeout_seconds=report.timeout_seconds,
        transport=transport,
    )
    request = CompletionRequest.from_prompt(
        _PROMPT,
        system_instruction=_SYSTEM_INSTRUCTION,
        temperature=0.0,
        max_output_tokens=_MAX_OUTPUT_TOKENS,
    )

    report.network_call_made = True
    report.check("single_direct_request").set(
        "passed", "One direct adapter request was made; router fallback and retries were bypassed."
    )
    try:
        response = provider.complete(request)
    except (PcAgentError, ProviderError, ValueError) as exc:
        report.check("live_response_received").set(
            "failed", "The direct Anthropic request did not return a usable response."
        )
        report.error = _sanitize_error(exc, secret)
        return _finish_report(
            report, started_monotonic=started_monotonic, secret=secret
        )

    report.check("live_response_received").set(
        "passed", "The direct Anthropic request returned a response."
    )
    report.check("provider_identity").set(
        "passed" if response.provider == "anthropic" else "failed",
        "The response identifies the Anthropic provider."
        if response.provider == "anthropic"
        else "The response provider identity was not Anthropic.",
    )

    response_model = response.model if isinstance(response.model, str) else ""
    model_valid = _valid_short_value(response_model, maximum=200)
    report.response_model = response_model if model_valid else None
    report.check("model_reported").set(
        "passed" if model_valid else "failed",
        "The response reported a bounded model identifier."
        if model_valid
        else "The response did not report a usable model identifier.",
    )

    content = response.content if isinstance(response.content, str) else ""
    report.response_chars = len(content)
    report.response_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    report.expected_response_matched = (
        len(content) <= _MAX_RESPONSE_CHARS
        and _normalize_response(content)
        == ANTHROPIC_VERIFICATION_EXPECTED_RESPONSE
    )
    report.check("expected_response").set(
        "passed" if report.expected_response_matched else "failed",
        "The response matched the fixed verification token; only its hash was retained."
        if report.expected_response_matched
        else "The response did not match the fixed verification token.",
    )

    report.input_tokens = _usage_value(response.usage, "input_tokens")
    report.output_tokens = _usage_value(response.usage, "output_tokens")
    usage_valid = (
        report.input_tokens is not None
        and report.output_tokens is not None
        and report.output_tokens > 0
    )
    report.check("usage_reported").set(
        "passed" if usage_valid else "failed",
        "The response reported bounded input and output token usage."
        if usage_valid
        else "The response did not report usable token usage.",
    )
    return _finish_report(report, started_monotonic=started_monotonic, secret=secret)


def _parse_report_time(report: dict[str, Any], name: str) -> datetime:
    value = report.get(name)
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PcAgentError(f"Anthropic verification report {name} is invalid.")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise PcAgentError(f"Anthropic verification report {name} is invalid.") from exc


def verify_anthropic_live_verification_report(
    report_path: Path,
) -> AnthropicVerification:
    """Strictly validate a passed Anthropic verification report offline.

    This is an integrity/shape check, not a cryptographic attestation from
    Anthropic: the report's self-hash detects alteration after it was
    written, but a malicious actor who can replace the whole file could
    also construct a new self-hash.  The report therefore remains local
    operator evidence, not a provider-issued certificate.
    """

    path = report_path.expanduser().resolve()
    if path.suffix.lower() != ".json" or not path.is_file() or path.is_symlink():
        raise PcAgentError(f"Anthropic verification report was not found: {path}.")
    if path.stat().st_size > _MAX_REPORT_BYTES:
        raise PcAgentError("Anthropic verification report exceeds the size limit.")
    try:
        report = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PcAgentError("Anthropic verification report is not valid JSON.") from exc

    expected_keys = {
        "app",
        "checks",
        "credential_source",
        "elapsed_ms",
        "endpoint_origin",
        "error",
        "expected_response",
        "fallback_used",
        "finished_at",
        "format",
        "max_output_tokens",
        "network_call_made",
        "platform",
        "provider",
        "report_sha256",
        "requested_model",
        "response",
        "retries_used",
        "started_at",
        "status",
        "timeout_seconds",
        "usage",
        "version",
        "vision_status",
    }
    if not isinstance(report, dict) or set(report) != expected_keys:
        raise PcAgentError("Anthropic verification report has an unexpected shape.")

    actual_hash = report.get("report_sha256")
    unsigned = {key: value for key, value in report.items() if key != "report_sha256"}
    if (
        not isinstance(actual_hash, str)
        or _SHA256_RE.fullmatch(actual_hash) is None
        or not secrets.compare_digest(actual_hash, _report_hash(unsigned))
    ):
        raise PcAgentError("Anthropic verification report hash does not match.")

    if report["format"] != _REPORT_FORMAT or report["app"] != _APP_NAME:
        raise PcAgentError("Anthropic verification report format is unsupported.")
    if report["version"] != __version__:
        raise PcAgentError("Anthropic verification report version does not match this build.")
    if report["provider"] != "anthropic" or report["status"] != "passed":
        raise PcAgentError("Anthropic verification report does not record a passed run.")
    if report["error"] is not None:
        raise PcAgentError("Anthropic verification report contains an error.")
    if (
        report["expected_response"] != ANTHROPIC_VERIFICATION_EXPECTED_RESPONSE
        or report["fallback_used"] is not False
        or report["network_call_made"] is not True
        or report["retries_used"] != 0
        or report["max_output_tokens"] != _MAX_OUTPUT_TOKENS
        or report["vision_status"] != "not_tested"
    ):
        raise PcAgentError("Anthropic verification report boundaries are unexpected.")

    platform = report["platform"]
    if not isinstance(platform, str) or not _valid_short_value(platform, maximum=32):
        raise PcAgentError("Anthropic verification report platform is invalid.")
    requested_model = report["requested_model"]
    if not isinstance(requested_model, str) or not _valid_short_value(
        requested_model, maximum=200
    ):
        raise PcAgentError("Anthropic verification report requested model is invalid.")
    credential_source = report["credential_source"]
    if credential_source not in _ALLOWED_CREDENTIAL_SOURCES - {"missing"}:
        raise PcAgentError("Anthropic verification report credential source is invalid.")

    endpoint_origin, _detail = _endpoint_origin(str(report["endpoint_origin"]))
    if endpoint_origin is None or endpoint_origin != report["endpoint_origin"]:
        raise PcAgentError("Anthropic verification report endpoint is invalid.")

    started = _parse_report_time(report, "started_at")
    finished = _parse_report_time(report, "finished_at")
    if finished < started:
        raise PcAgentError("Anthropic verification report timestamps are inconsistent.")
    timeout = report["timeout_seconds"]
    elapsed = report["elapsed_ms"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not (0 < float(timeout) <= 300)
        or isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not (0 <= float(elapsed) <= float(timeout) * 1_000 + 60_000)
    ):
        raise PcAgentError("Anthropic verification report timing is invalid.")

    checks = report["checks"]
    if not isinstance(checks, list) or len(checks) != len(_CHECK_NAMES):
        raise PcAgentError("Anthropic verification report checks are incomplete.")
    for expected_name, check in zip(_CHECK_NAMES, checks, strict=True):
        if not isinstance(check, dict) or set(check) != {"detail", "name", "status"}:
            raise PcAgentError("Anthropic verification report checks are malformed.")
        detail = check["detail"]
        if (
            check["name"] != expected_name
            or check["status"] != "passed"
            or not isinstance(detail, str)
            or not detail
            or len(detail) > 300
            or any(ord(character) < 32 or ord(character) == 127 for character in detail)
        ):
            raise PcAgentError("Anthropic verification report contains a failed check.")

    response = report["response"]
    if not isinstance(response, dict) or set(response) != {
        "chars",
        "exact_expected",
        "model",
        "sha256",
    }:
        raise PcAgentError("Anthropic verification report response record is malformed.")
    response_model = response["model"]
    response_hash = response["sha256"]
    response_chars = response["chars"]
    if (
        not isinstance(response_model, str)
        or not _valid_short_value(response_model, maximum=200)
        or not isinstance(response_hash, str)
        or _SHA256_RE.fullmatch(response_hash) is None
        or response["exact_expected"] is not True
        or isinstance(response_chars, bool)
        or not isinstance(response_chars, int)
        or not (
            len(ANTHROPIC_VERIFICATION_EXPECTED_RESPONSE)
            <= response_chars
            <= _MAX_RESPONSE_CHARS
        )
    ):
        raise PcAgentError("Anthropic verification report response record is invalid.")

    usage = report["usage"]
    if not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens"}:
        raise PcAgentError("Anthropic verification report usage record is malformed.")
    input_tokens = _usage_value(usage, "input_tokens")
    output_tokens = _usage_value(usage, "output_tokens")
    if input_tokens is None or output_tokens is None or output_tokens <= 0:
        raise PcAgentError("Anthropic verification report usage record is invalid.")

    return AnthropicVerification(
        report_path=path,
        version=str(report["version"]),
        status=str(report["status"]),
        requested_model=requested_model,
        response_model=response_model,
        endpoint_origin=endpoint_origin,
        credential_source=str(credential_source),
        response_sha256=response_hash,
        report_sha256=actual_hash,
    )
