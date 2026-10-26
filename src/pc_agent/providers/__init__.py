"""Replaceable AI provider adapters and the fallback model router."""

from .base import (
    AIProvider,
    ChatMessage,
    CompletionRequest,
    CompletionResponse,
    HttpJsonRequest,
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
    "MessageRole",
    "ModelRouter",
    "ProviderAttempt",
    "ProviderStatus",
    "build_model_router",
    "provider_statuses",
]
