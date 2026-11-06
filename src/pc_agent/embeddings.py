"""Embedding backends for Memory v2 / knowledge search (Phase 8).

The default backend is a deterministic, offline **signed feature-hashing**
embedder (the classic hashing-trick text vectoriser): word unigrams and
bigrams are hashed into a fixed number of buckets with a sign, weighted
by ``1 + log(count)``, and L2-normalised. It needs no network, no model
download, and no API key, and it gives genuinely useful cosine-similarity
retrieval for personal corpora. It is *not* a neural embedding model —
results are labelled with its model tag (``local-hashing-<dims>``) so
nobody mistakes it for one.

When the user configures it, real providers can embed instead:
OpenAI-compatible ``/embeddings`` endpoints (OpenAI, OpenRouter,
generic) and Ollama ``/api/embed``. Vectors are always stored with the
exact producing model tag, and search compares only same-tag vectors.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import math
import re
from array import array
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from .config import Settings
from .errors import ProviderError
from .providers.base import HttpJsonRequest, JsonTransport, UrllibJsonTransport

_LOG = logging.getLogger("pc_agent.embeddings")

_MAX_BATCH = 16
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def pack_vector(values: Sequence[float]) -> bytes:
    return array("f", values).tobytes()


def unpack_vector(raw: bytes) -> array:
    values = array("f")
    values.frombytes(raw[: len(raw) - (len(raw) % 4)])
    return values


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def _stem(token: str) -> str:
    """Very light English suffix stripping so visitors≈visitor, parking≈park.

    Deliberately conservative (this is a retrieval aid, not a linguist):
    only common inflectional endings on words longer than three chars.
    """
    if len(token) <= 3:
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    for suffix in ("ingly", "edly", "ing", "ed", "ly"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    if token.endswith("es") and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    return [_stem(token) for token in _TOKEN_RE.findall(text.lower())]


def keyword_overlap(query_text: str, candidate_text: str) -> float:
    """Fraction of distinct query terms present in the candidate (0..1)."""
    query_terms = set(tokenize(query_text))
    if not query_terms:
        return 0.0
    candidate_terms = set(tokenize(candidate_text))
    return len(query_terms & candidate_terms) / len(query_terms)


class HashingEmbedder:
    """Offline deterministic embeddings via signed feature hashing."""

    local = True

    def __init__(self, dimensions: int = 384) -> None:
        self.dimensions = max(64, min(dimensions, 2048))
        self.model = f"local-hashing-{self.dimensions}"

    def embed(self, texts: Sequence[str]) -> list[bytes]:
        return [pack_vector(self._vectorize(text)) for text in texts]

    def _vectorize(self, text: str) -> list[float]:
        tokens = tokenize(text)
        features: Counter[str] = Counter(tokens)
        features.update(
            f"{first} {second}" for first, second in itertools.pairwise(tokens)
        )
        vector = [0.0] * self.dimensions
        for feature, count in features.items():
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "little") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[bucket] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            vector = [value / norm for value in vector]
        return vector


class OpenAICompatibleEmbedder:
    """Embeddings via a Chat-Completions-style ``/embeddings`` endpoint."""

    local = False

    def __init__(
        self,
        *,
        name: str,
        api_key: str | None,
        model: str,
        base_url: str,
        timeout_seconds: float,
        transport: JsonTransport | None = None,
    ) -> None:
        self.name = name
        self._api_key = api_key
        self.model = f"{name}:{model}"
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport or UrllibJsonTransport()

    def embed(self, texts: Sequence[str]) -> list[bytes]:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        vectors: list[bytes] = []
        for start in range(0, len(texts), _MAX_BATCH):
            batch = list(texts[start : start + _MAX_BATCH])
            data = self._transport(
                HttpJsonRequest(
                    url=f"{self._base_url}/embeddings",
                    payload={"model": self._model, "input": batch},
                    headers=headers,
                    timeout_seconds=self._timeout_seconds,
                )
            )
            vectors.extend(_parse_openai_embeddings(data, expected=len(batch)))
        return vectors


class OllamaEmbedder:
    """Embeddings via Ollama's ``/api/embed`` endpoint."""

    local = True

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        base_url: str,
        timeout_seconds: float,
        transport: JsonTransport | None = None,
    ) -> None:
        self.name = "ollama"
        self._api_key = api_key
        self.model = f"ollama:{model}"
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport or UrllibJsonTransport()

    def embed(self, texts: Sequence[str]) -> list[bytes]:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        vectors: list[bytes] = []
        for start in range(0, len(texts), _MAX_BATCH):
            batch = list(texts[start : start + _MAX_BATCH])
            data = self._transport(
                HttpJsonRequest(
                    url=f"{self._base_url}/api/embed",
                    payload={"model": self._model, "input": batch},
                    headers=headers,
                    timeout_seconds=self._timeout_seconds,
                )
            )
            if data.get("error"):
                raise ProviderError(f"Ollama embeddings error: {data['error']}")
            raw = data.get("embeddings") or data.get("embedding")
            if isinstance(raw, list) and raw and isinstance(raw[0], (int, float)):
                raw = [raw]  # single-vector shape from older endpoints
            parsed = _coerce_vectors(raw, expected=len(batch))
            vectors.extend(parsed)
        return vectors


def _coerce_vectors(raw: Any, *, expected: int) -> list[bytes]:
    if not isinstance(raw, list) or len(raw) != expected:
        raise ProviderError("Provider returned an unexpected embeddings shape.")
    vectors: list[bytes] = []
    for item in raw:
        if not isinstance(item, list) or not item:
            raise ProviderError("Provider returned an empty embedding vector.")
        try:
            vectors.append(pack_vector([float(value) for value in item]))
        except (TypeError, ValueError) as exc:
            raise ProviderError("Provider returned a non-numeric embedding.") from exc
    return vectors


def _parse_openai_embeddings(data: Mapping[str, Any], *, expected: int) -> list[bytes]:
    error = data.get("error")
    if error:
        detail = error.get("message") if isinstance(error, Mapping) else str(error)
        raise ProviderError(f"Embeddings provider returned an error: {detail}")
    items = data.get("data")
    if not isinstance(items, list) or len(items) != expected:
        raise ProviderError("Embeddings provider returned an unexpected shape.")
    ordered = sorted(
        (item for item in items if isinstance(item, Mapping)),
        key=lambda item: int(item.get("index", 0)),
    )
    if len(ordered) != expected:
        raise ProviderError("Embeddings provider returned malformed entries.")
    return _coerce_vectors([item.get("embedding") for item in ordered], expected=expected)


def _openai_style_embedder(
    name: str, settings: Settings, transport: JsonTransport | None
) -> OpenAICompatibleEmbedder | None:
    timeout = settings.provider_timeout_seconds
    model = settings.embedding_model
    if name == "openai" and settings.openai_api_key:
        return OpenAICompatibleEmbedder(
            name=name,
            api_key=settings.openai_api_key,
            model=model or "text-embedding-3-small",
            base_url=settings.openai_base_url,
            timeout_seconds=timeout,
            transport=transport,
        )
    if name == "openrouter" and settings.openrouter_api_key and model:
        return OpenAICompatibleEmbedder(
            name=name,
            api_key=settings.openrouter_api_key,
            model=model,
            base_url=settings.openrouter_base_url,
            timeout_seconds=timeout,
            transport=transport,
        )
    if (
        name == "openai-compatible"
        and settings.openai_compatible_base_url
        and model
    ):
        return OpenAICompatibleEmbedder(
            name=name,
            api_key=settings.openai_compatible_api_key,
            model=model,
            base_url=settings.openai_compatible_base_url,
            timeout_seconds=timeout,
            transport=transport,
        )
    return None


def build_embedder(
    settings: Settings, *, transport: JsonTransport | None = None
) -> Any:
    """Resolve the configured embedding backend.

    ``local``/``mock`` always means the offline hashing embedder. An
    explicit provider that is not usable raises ``ProviderError`` —
    silently switching model spaces would corrupt search. ``auto``
    prefers an explicitly modelled provider in route order, then the
    OpenAI default, and otherwise stays offline.
    """
    choice = settings.embedding_provider
    if choice in {"local", "mock"}:
        return HashingEmbedder(settings.embedding_dimensions)
    if choice == "ollama":
        if not settings.embedding_model:
            raise ProviderError(
                "PC_AGENT_EMBEDDING_MODEL must name an Ollama embedding model "
                "(for example nomic-embed-text) when PC_AGENT_EMBEDDING_PROVIDER=ollama."
            )
        return OllamaEmbedder(
            api_key=settings.ollama_api_key,
            model=settings.embedding_model,
            base_url=settings.ollama_base_url,
            timeout_seconds=settings.provider_timeout_seconds,
            transport=transport,
        )
    if choice in {"openai", "openrouter", "openai-compatible"}:
        embedder = _openai_style_embedder(choice, settings, transport)
        if embedder is None:
            raise ProviderError(
                f"Embedding provider {choice} is not usable: it needs its API "
                "key/base URL and (outside OpenAI's default) PC_AGENT_EMBEDDING_MODEL."
            )
        return embedder
    # auto
    for name in settings.provider_order:
        candidate = None
        if name == "ollama" and settings.embedding_model:
            candidate = OllamaEmbedder(
                api_key=settings.ollama_api_key,
                model=settings.embedding_model,
                base_url=settings.ollama_base_url,
                timeout_seconds=settings.provider_timeout_seconds,
                transport=transport,
            )
        elif name in {"openai", "openrouter", "openai-compatible"}:
            candidate = _openai_style_embedder(name, settings, transport)
        if candidate is not None:
            return candidate
    _LOG.debug("no provider embeddings configured; using offline hashing embedder")
    return HashingEmbedder(settings.embedding_dimensions)


def embedder_description(settings: Settings) -> str:
    """Secret-safe one-liner naming the active backend (may raise)."""
    embedder = build_embedder(settings)
    origin = "offline" if embedder.local else "provider"
    return f"{embedder.model} ({origin})"
