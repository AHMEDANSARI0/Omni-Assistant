"""Bounded provider retries and user-configured fallback order."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..config import Settings
from ..errors import ProviderError
from ..logging_config import redact_text
from .base import AIProvider, CompletionRequest, CompletionResponse, JsonTransport
from .gemini_adapter import GeminiProvider
from .mock import MockProvider
from .ollama import OllamaProvider
from .openai_compatible import OpenAICompatibleProvider


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
class ProviderStatus:
    name: str
    model: str
    configured: bool
    implemented: bool
    in_route: bool
    detail: str


class UnsupportedProvider:
    """A reserved provider slot with no direct adapter in this build."""

    implemented = False

    def __init__(self, name: str, model: str = "") -> None:
        self.name = name
        self.model = model or "not configured"

    @property
    def configured(self) -> bool:
        return False

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise ProviderError(
            f"Direct {self.name} adapter is reserved but not implemented in this build. "
            "Use an implemented provider or an OpenRouter model for that vendor."
        )


class ModelRouter:
    def __init__(
        self,
        providers: Sequence[AIProvider],
        *,
        max_retries: int = 2,
        backoff_seconds: float = 0.25,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_retries < 0 or max_retries > 5:
            raise ValueError("max_retries must be between 0 and 5.")
        self._providers = tuple(providers)
        self._max_retries = max_retries
        self._backoff_seconds = max(0.0, backoff_seconds)
        self._sleeper = sleeper

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

        for provider in providers:
            for attempt_number in range(1, self._max_retries + 2):
                started = time.perf_counter()
                try:
                    response = provider.complete(request)
                except ProviderError as exc:
                    elapsed = (time.perf_counter() - started) * 1000
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
                return RoutedCompletion(response=response, attempts=tuple(attempts))

        detail = "; ".join(failures) if failures else "no provider produced a response"
        raise ProviderError(f"All configured AI providers failed. {detail}")

    def _sleep_before_retry(self, attempt_number: int) -> None:
        if self._backoff_seconds:
            self._sleeper(min(4.0, self._backoff_seconds * (2 ** (attempt_number - 1))))


def build_model_router(
    settings: Settings, *, transport: JsonTransport | None = None
) -> ModelRouter:
    providers: list[AIProvider] = []
    for name in settings.provider_order:
        provider = _build_provider(name, settings, transport)
        if provider is not None:
            providers.append(provider)
    if not providers:
        providers.append(MockProvider())
    return ModelRouter(providers, max_retries=settings.max_retries)


def provider_statuses(settings: Settings) -> list[ProviderStatus]:
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
    name: str, settings: Settings, transport: JsonTransport | None
) -> AIProvider | None:
    timeout = settings.provider_timeout_seconds
    if name == "mock":
        return MockProvider()
    if name == "Gemini" and settings.gemini_api_key:
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
        )
    if name == "openai" and settings.openai_api_key:
        return OpenAICompatibleProvider(
            name=name,
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url,
            timeout_seconds=timeout,
            transport=transport,
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
            api_key_required=False,
        )
    if name == "ollama":
        return OllamaProvider(
            api_key=settings.ollama_api_key,
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            timeout_seconds=timeout,
            transport=transport,
        )
    if name == "anthropic":
        return UnsupportedProvider(name, settings.anthropic_model)
    return None


def _model_for(name: str, settings: Settings) -> str:
    return {
        "mock": "offline-mock",
        "Gemini": settings.gemini_model,
        "openrouter": settings.openrouter_model,
        "openai": settings.openai_model,
        "openai-compatible": settings.openai_compatible_model,
        "ollama": settings.ollama_model,
        "anthropic": settings.anthropic_model,
    }.get(name, "")


def _status_detail(name: str, settings: Settings) -> tuple[bool, bool, str]:
    if name == "mock":
        return True, True, "Offline deterministic mock; no network or key."
    if name == "Gemini":
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
        return (
            bool(settings.anthropic_api_key),
            False,
            "Reserved slot; no direct adapter in this build (OpenRouter can route Anthropic models).",
        )
    return False, False, "Unknown provider slot."
