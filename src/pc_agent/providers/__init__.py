"""Replaceable AI provider adapters and the fallback model router."""

from .base import (
    AIProvider,
    ChatMessage,
    CompletionRequest,
    CompletionResponse,
    HttpJsonRequest,
    ImageInput,
    MessageRole,
)
from .router import (
    ModelRouter,
    ProviderAttempt,
    ProviderStatus,
    build_model_router,
    provider_statuses,
)

__all__ = [
    "AIProvider",
    "ChatMessage",
    "CompletionRequest",
    "CompletionResponse",
    "HttpJsonRequest",
    "ImageInput",
    "MessageRole",
    "ModelRouter",
    "ProviderAttempt",
    "ProviderStatus",
    "build_model_router",
    "provider_statuses",
]
