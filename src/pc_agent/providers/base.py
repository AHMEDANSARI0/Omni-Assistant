"""Provider contracts and a small standard-library JSON HTTP transport.

Adapters return ordinary text completions. They never receive tool objects,
filesystem handles or approval authority: a model may propose text/JSON, and
the planner/orchestrator validates that untrusted output before any tool can
run.
"""

from __future__ import annotations

import enum
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ..errors import ProviderError
from ..logging_config import redact_text

_MAX_RESPONSE_BYTES = 2_000_000
_RETRYABLE_HTTP_STATUSES = frozenset({408, 409, 425, 429})


class MessageRole(str, enum.Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: MessageRole
    content: str

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("Chat message content must not be empty.")

    @classmethod
    def system(cls, content: str) -> ChatMessage:
        return cls(MessageRole.SYSTEM, content)

    @classmethod
    def user(cls, content: str) -> ChatMessage:
        return cls(MessageRole.USER, content)

    @classmethod
    def assistant(cls, content: str) -> ChatMessage:
        return cls(MessageRole.ASSISTANT, content)


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    messages: tuple[ChatMessage, ...]
    temperature: float = 0.0
    max_output_tokens: int = 1024
    json_mode: bool = False

    def __post_init__(self) -> None:
        if not self.messages or not any(
            message.role is MessageRole.USER for message in self.messages
        ):
            raise ValueError("A completion request needs at least one user message.")
        if self.temperature < 0 or self.temperature > 2:
            raise ValueError("temperature must be between 0 and 2.")
        if self.max_output_tokens < 1 or self.max_output_tokens > 8192:
            raise ValueError("max_output_tokens must be between 1 and 8192.")

    @classmethod
    def from_prompt(
        cls,
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 1024,
        json_mode: bool = False,
    ) -> CompletionRequest:
        messages: list[ChatMessage] = []
        if system_instruction:
            messages.append(ChatMessage.system(system_instruction))
        messages.append(ChatMessage.user(prompt))
        return cls(
            messages=tuple(messages),
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            json_mode=json_mode,
        )

    @property
    def system_instruction(self) -> str:
        return "\n\n".join(
            message.content for message in self.messages if message.role is MessageRole.SYSTEM
        )

    @property
    def last_user_message(self) -> str:
        for message in reversed(self.messages):
            if message.role is MessageRole.USER:
                return message.content
        return ""


@dataclass(frozen=True, slots=True)
class CompletionResponse:
    content: str
    provider: str
    model: str
    usage: Mapping[str, Any] = field(default_factory=dict)


class AIProvider(Protocol):
    name: str
    model: str
    implemented: bool

    @property
    def configured(self) -> bool: ...

    def complete(self, request: CompletionRequest) -> CompletionResponse: ...


@dataclass(frozen=True, slots=True)
class HttpJsonRequest:
    url: str
    payload: Mapping[str, Any]
    headers: Mapping[str, str]
    timeout_seconds: float


JsonTransport = Callable[[HttpJsonRequest], Mapping[str, Any]]


class UrllibJsonTransport:
    """POST JSON with a hard response-size cap and safe, redacted errors."""

    def __call__(self, request: HttpJsonRequest) -> Mapping[str, Any]:
        _validate_http_url(request.url)
        body = json.dumps(dict(request.payload)).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "pc-agent/0.3"}
        headers.update({str(key): str(value) for key, value in request.headers.items()})
        http_request = Request(request.url, data=body, headers=headers, method="POST")

        try:
            # URL scheme/host were validated by _validate_http_url above.
            with urlopen(http_request, timeout=request.timeout_seconds) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            detail = _http_error_detail(exc)
            retryable = exc.code in _RETRYABLE_HTTP_STATUSES or 500 <= exc.code <= 599
            raise ProviderError(
                f"Provider HTTP {exc.code}: {detail}",
                retryable=retryable,
                status_code=exc.code,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ProviderError(
                f"Provider network error: {redact_text(str(exc))}", retryable=True
            ) from exc

        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ProviderError("Provider response exceeded the local size limit.")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError("Provider returned invalid JSON.") from exc
        if not isinstance(parsed, Mapping):
            raise ProviderError("Provider returned a non-object JSON response.")
        return parsed


def _validate_http_url(url: str) -> None:
    parsed = urlparse(url)
    is_loopback = (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise ProviderError("Provider endpoint must be a valid http(s) URL.")
    if parsed.scheme == "http" and not is_loopback:
        raise ProviderError("Provider endpoint must use HTTPS outside loopback addresses.")


def _http_error_detail(error: HTTPError) -> str:
    try:
        raw = error.read(65_537)
        text = raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - error detail is best-effort diagnostics only.
        return "request failed"
    detail = text
    try:
        parsed = json.loads(text)
        if isinstance(parsed, Mapping):
            nested = parsed.get("error")
            if isinstance(nested, Mapping) and nested.get("message"):
                detail = str(nested["message"])
            elif parsed.get("message"):
                detail = str(parsed["message"])
            elif parsed.get("detail"):
                detail = str(parsed["detail"])
    except json.JSONDecodeError:
        pass
    return redact_text(detail.strip())[:300] or "request failed"


def extract_text_content(value: Any) -> str:
    """Normalize common provider content shapes to text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if isinstance(value, Mapping):
        text = value.get("text") or value.get("content")
        return extract_text_content(text) if text is not None else ""
    return ""


def ensure_nonempty_content(content: str, provider: str) -> str:
    cleaned = content.strip()
    if not cleaned:
        raise ProviderError(f"Provider {provider} returned an empty response.")
    return cleaned
