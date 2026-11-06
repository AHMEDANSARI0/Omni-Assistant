"""Bounded provider retries and user-configured fallback order."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from ..config import Settings
from ..errors import ProviderError
from ..logging_config import redact_text
from .anthropic import AnthropicProvider
from .base import (
    AIProvider,
    CompletionRequest,
    CompletionResponse,
    JsonTransport,
    StreamTransport,
)
from .gemini_adapter import GeminiProvider
from .mock import MockProvider
from .ollama import OllamaProvider
from .openai_compatible import OpenAICompatibleProvider

_LOG = logging.getLogger("pc_agent.providers.router")


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    provider: str
    model: str
    success: bool
    attempt: int
    error: str | None = None
    elapsed_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class RoutedCompletion:
    response: CompletionResponse
    attempts: tuple[ProviderAttempt, ...]


@dataclass(frozen=True, slots=True)
class ProviderCallEvent:
    """One metered provider attempt, handed to the usage observer.

    Carries no derived analytics on purpose: token normalisation and
    pricing live in ``pc_agent.provider_platform``. The router never
    lets an observer failure affect a completion.
    """

    provider: str
    model: str
    success: bool
    elapsed_ms: float
    streamed: bool
    request: CompletionRequest
    response: CompletionResponse | None = None
    streamed_text: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    name: str
    model: str
    configured: bool
    implemented: bool
    in_route: bool
    detail: str


class ModelRouter:
    def __init__(
        self,
        providers: Sequence[AIProvider],
        *,
        max_retries: int = 2,
        backoff_seconds: float = 0.25,
        sleeper: Callable[[float], None] = time.sleep,
        observer: Callable[[ProviderCallEvent], None] | None = None,
    ) -> None:
        if max_retries < 0 or max_retries > 5:
            raise ValueError("max_retries must be between 0 and 5.")
        self._providers = tuple(providers)
        self._max_retries = max_retries
        self._backoff_seconds = max(0.0, backoff_seconds)
        self._sleeper = sleeper
        self._observer = observer

    def _emit(self, event: ProviderCallEvent) -> None:
        if self._observer is None:
            return
        try:
            self._observer(event)
        except Exception:  # noqa: BLE001 - metering must never break a completion.
            _LOG.debug("provider usage observer failed", exc_info=True)

    @property
    def providers(self) -> tuple[AIProvider, ...]:
        return self._providers

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(provider.name for provider in self._providers)

    def complete(
        self, request: CompletionRequest, *, allow_fallback: bool = True
    ) -> RoutedCompletion:
        if not self._providers:
            raise ProviderError("No AI providers are available in the configured route.")

        attempts: list[ProviderAttempt] = []
        failures: list[str] = []
        providers = self._providers if allow_fallback else self._providers[:1]
        if request.has_images:
            if allow_fallback:
                providers = tuple(
                    provider
                    for provider in providers
                    if getattr(provider, "supports_vision", False)
                )
                if not providers:
                    raise ProviderError(
                        "No vision-capable provider is available in the configured route. "
                        "Configure Gemini, OpenRouter/OpenAI with a vision model, or a "
                        "vision-capable Ollama model."
                    )
            elif not getattr(providers[0], "supports_vision", False):
                raise ProviderError(
                    f"Provider {providers[0].name} does not accept image inputs in this build."
                )

        for provider in providers:
            for attempt_number in range(1, self._max_retries + 2):
                started = time.perf_counter()
                try:
                    response = provider.complete(request)
                except ProviderError as exc:
                    elapsed = (time.perf_counter() - started) * 1000
                    self._emit(
                        ProviderCallEvent(
                            provider.name,
                            provider.model,
                            False,
                            elapsed,
                            False,
                            request,
                            error=redact_text(str(exc)),
                        )
                    )
                    attempts.append(
                        ProviderAttempt(
                            provider.name,
                            provider.model,
                            False,
                            attempt_number,
                            redact_text(str(exc)),
                            elapsed,
                        )
                    )
                    failures.append(f"{provider.name}: {redact_text(str(exc))}")
                    if exc.retryable and attempt_number <= self._max_retries:
                        self._sleep_before_retry(attempt_number)
                        continue
                    break
                except Exception as exc:  # noqa: BLE001 - one broken adapter must not block fallback.
                    elapsed = (time.perf_counter() - started) * 1000
                    message = f"Unexpected provider error ({type(exc).__name__}): {exc}"
                    self._emit(
                        ProviderCallEvent(
                            provider.name,
                            provider.model,
                            False,
                            elapsed,
                            False,
                            request,
                            error=redact_text(message),
                        )
                    )
                    attempts.append(
                        ProviderAttempt(
                            provider.name,
                            provider.model,
                            False,
                            attempt_number,
                            redact_text(message),
                            elapsed,
                        )
                    )
                    failures.append(f"{provider.name}: {redact_text(message)}")
                    break

                elapsed = (time.perf_counter() - started) * 1000
                attempts.append(
                    ProviderAttempt(
                        provider.name, response.model, True, attempt_number, None, elapsed
                    )
                )
                self._emit(
                    ProviderCallEvent(
                        provider.name,
                        response.model,
                        True,
                        elapsed,
                        False,
                        request,
                        response=response,
                    )
                )
                return RoutedCompletion(response=response, attempts=tuple(attempts))

        detail = "; ".join(failures) if failures else "no provider produced a response"
        raise ProviderError(f"All configured AI providers failed. {detail}")

    def stream_complete(self, request: CompletionRequest) -> Iterator[str]:
        """Yield text deltas from the first usable streaming provider.

        Fallback happens only *before* the first delta — once text has
        been emitted, silently restarting on another provider would
        corrupt the consumer's output, so a mid-stream failure raises
        instead. Every attempt is metered like a normal completion.
        """
        candidates = [
            provider
            for provider in self._providers
            if getattr(provider, "supports_streaming", False)
            and callable(getattr(provider, "stream_complete", None))
        ]
        if request.has_images:
            candidates = [
                provider
                for provider in candidates
                if getattr(provider, "supports_vision", False)
            ]
        if not candidates:
            raise ProviderError(
                "No streaming-capable provider is available in the configured "
                "route. Streaming is implemented for the offline mock, "
                "OpenAI-compatible endpoints, Ollama, and Anthropic."
            )

        failures: list[str] = []
        for provider in candidates:
            started = time.perf_counter()
            stream = provider.stream_complete
            try:
                iterator = iter(stream(request))
                first = next(iterator)
            except StopIteration:
                elapsed = (time.perf_counter() - started) * 1000
                message = f"Provider {provider.name} returned an empty stream."
                self._emit(
                    ProviderCallEvent(
                        provider.name, provider.model, False, elapsed, True, request,
                        error=message,
                    )
                )
                failures.append(f"{provider.name}: empty stream")
                continue
            except Exception as exc:  # noqa: BLE001 - try the next streaming provider.
                elapsed = (time.perf_counter() - started) * 1000
                self._emit(
                    ProviderCallEvent(
                        provider.name, provider.model, False, elapsed, True, request,
                        error=redact_text(str(exc)),
                    )
                )
                failures.append(f"{provider.name}: {redact_text(str(exc))}")
                continue

            chunks = [first]
            yield first
            try:
                for delta in iterator:
                    chunks.append(delta)
                    yield delta
            except Exception as exc:
                elapsed = (time.perf_counter() - started) * 1000
                self._emit(
                    ProviderCallEvent(
                        provider.name, provider.model, False, elapsed, True, request,
                        streamed_text="".join(chunks),
                        error=redact_text(str(exc)),
                    )
                )
                raise ProviderError(
                    f"Provider {provider.name} stream failed after partial output: "
                    f"{redact_text(str(exc))}"
                ) from exc
            elapsed = (time.perf_counter() - started) * 1000
            self._emit(
                ProviderCallEvent(
                    provider.name, provider.model, True, elapsed, True, request,
                    streamed_text="".join(chunks),
                )
            )
            return

        detail = "; ".join(failures) if failures else "no provider produced a stream"
        raise ProviderError(f"All streaming-capable providers failed. {detail}")

    def _sleep_before_retry(self, attempt_number: int) -> None:
        if self._backoff_seconds:
            self._sleeper(min(4.0, self._backoff_seconds * (2 ** (attempt_number - 1))))


def build_model_router(
    settings: Settings,
    *,
    transport: JsonTransport | None = None,
    stream_transport: StreamTransport | None = None,
    observer: Callable[[ProviderCallEvent], None] | None = None,
) -> ModelRouter:
    # Provider Platform v2 supplies strategy ordering and the default
    # local usage observer. The import is deferred because
    # provider_platform imports this module at load time.
    from ..provider_platform import make_usage_observer, ordered_provider_names

    providers: list[AIProvider] = []
    for name in ordered_provider_names(settings):
        provider = _build_provider(name, settings, transport, stream_transport)
        if provider is not None:
            providers.append(provider)
    if not providers:
        providers.append(MockProvider())
    if observer is None:
        observer = make_usage_observer(settings)
    return ModelRouter(
        providers, max_retries=settings.max_retries, observer=observer
    )


def provider_statuses(
    settings: Settings, *, router: ModelRouter | None = None
) -> list[ProviderStatus]:
    if router is None:
        router = build_model_router(settings)
    route_names = set(router.provider_names)
    ordered_names = list(settings.provider_order)
    if not ordered_names:
        ordered_names = ["mock"]

    statuses: list[ProviderStatus] = []
    for name in ordered_names:
        model = _model_for(name, settings)
        configured, implemented, detail = _status_detail(name, settings)
        statuses.append(
            ProviderStatus(
                name=name,
                model=model or "not configured",
                configured=configured,
                implemented=implemented,
                in_route=name in route_names,
                detail=detail,
            )
        )
    if "mock" in route_names and "mock" not in ordered_names:
        statuses.append(
            ProviderStatus(
                "mock",
                "offline-mock",
                True,
                True,
                True,
                "Offline fallback added because no external provider is usable.",
            )
        )
    return statuses


def _build_provider(
    name: str,
    settings: Settings,
    transport: JsonTransport | None,
    stream_transport: StreamTransport | None = None,
) -> AIProvider | None:
    timeout = settings.provider_timeout_seconds
    if name == "mock":
        return MockProvider()
    if name == "gemini" and settings.gemini_api_key:
        return GeminiProvider(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            base_url=settings.gemini_base_url,
            timeout_seconds=timeout,
            transport=transport,
        )
    if name == "openrouter" and settings.openrouter_api_key:
        return OpenAICompatibleProvider(
            name=name,
            api_key=settings.openrouter_api_key,
            model=settings.openrouter_model,
            base_url=settings.openrouter_base_url,
            timeout_seconds=timeout,
            transport=transport,
            stream_transport=stream_transport,
        )
    if name == "openai" and settings.openai_api_key:
        return OpenAICompatibleProvider(
            name=name,
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url,
            timeout_seconds=timeout,
            transport=transport,
            stream_transport=stream_transport,
        )
    if name == "openai-compatible" and settings.openai_compatible_base_url:
        if not settings.openai_compatible_model:
            return None
        return OpenAICompatibleProvider(
            name=name,
            api_key=settings.openai_compatible_api_key,
            model=settings.openai_compatible_model,
            base_url=settings.openai_compatible_base_url,
            timeout_seconds=timeout,
            transport=transport,
            stream_transport=stream_transport,
            api_key_required=False,
        )
    if name == "ollama":
        return OllamaProvider(
            api_key=settings.ollama_api_key,
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            timeout_seconds=timeout,
            transport=transport,
            stream_transport=stream_transport,
        )
    if name == "anthropic" and settings.anthropic_api_key:
        return AnthropicProvider(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            base_url=settings.anthropic_base_url,
            timeout_seconds=timeout,
            transport=transport,
            stream_transport=stream_transport,
        )
    return None


def _model_for(name: str, settings: Settings) -> str:
    return {
        "mock": "offline-mock",
        "gemini": settings.gemini_model,
        "openrouter": settings.openrouter_model,
        "openai": settings.openai_model,
        "openai-compatible": settings.openai_compatible_model,
        "ollama": settings.ollama_model,
        "anthropic": settings.anthropic_model,
    }.get(name, "")


def _status_detail(name: str, settings: Settings) -> tuple[bool, bool, str]:
    if name == "mock":
        return True, True, "Offline deterministic mock; no network or key."
    if name == "gemini":
        return bool(settings.gemini_api_key), True, "Gemini Interactions REST adapter."
    if name == "openrouter":
        return bool(settings.openrouter_api_key), True, "OpenAI-compatible Chat Completions adapter."
    if name == "openai":
        return bool(settings.openai_api_key), True, "OpenAI-compatible Chat Completions adapter."
    if name == "openai-compatible":
        configured = bool(
            settings.openai_compatible_base_url and settings.openai_compatible_model
        )
        return configured, True, "Generic OpenAI-compatible endpoint."
    if name == "ollama":
        return bool(settings.ollama_model), True, "Ollama endpoint; server availability is not probed."
    if name == "anthropic":
        configured = bool(
            settings.anthropic_api_key
            and settings.anthropic_model
            and settings.anthropic_base_url
        )
        return configured, True, "Anthropic Messages API adapter."
    return False, False, "Unknown provider slot."
