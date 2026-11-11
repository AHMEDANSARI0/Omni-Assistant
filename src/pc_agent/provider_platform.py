"""Provider Platform v2 (Transformation Phase 7).

Capabilities, local usage analytics, strategy routing, and offline
evaluations for the existing provider adapters. Standard library only.

Honesty rules enforced here:

- Prices and context-window sizes exist **only** when the user configures
  them (``PC_AGENT_PROVIDER_PRICES`` / ``PC_AGENT_MODEL_CONTEXT_TOKENS``);
  nothing is guessed from model names.
- Token counts come from provider-reported usage when an adapter
  returns it, otherwise from the same characters/4 estimate the task
  budgets use, clearly labelled ``tokens_estimated``.
- "fastest" routing ranks by **measured** local latency from recorded
  calls; "cheapest" ranks only user-priced entries. Providers without
  data keep their configured relative order after measured ones.
- Analytics are a local append-only JSONL file (never transmitted, no
  prompt text is stored) and can be disabled with
  ``PC_AGENT_PROVIDER_ANALYTICS=0``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .budgets import estimate_request_tokens, estimate_response_tokens
from .config import Settings
from .errors import ProviderError
from .logging_config import redact_text
from .providers.base import CompletionRequest
from .providers.router import ModelRouter, ProviderCallEvent, provider_statuses

_LOG = logging.getLogger("pc_agent.provider_platform")

USAGE_FILENAME = "provider-usage.jsonl"
EVAL_FILENAME = "provider-eval-latest.json"
_MAX_USAGE_READ_BYTES = 8_000_000
_USAGE_ROTATE_BYTES = 4_000_000
_LOCAL_PROVIDER_NAMES = frozenset({"mock", "ollama"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_INPUT_TOKEN_KEYS = (
    "input_tokens",
    "prompt_tokens",
    "prompt_eval_count",
    "promptTokenCount",
    "inputTokenCount",
)
_OUTPUT_TOKEN_KEYS = (
    "output_tokens",
    "completion_tokens",
    "eval_count",
    "candidatesTokenCount",
    "outputTokenCount",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _model_for_name(name: str, settings: Settings) -> str:
    return {
        "mock": "offline-mock",
        "gemini": settings.gemini_model,
        "openrouter": settings.openrouter_model,
        "openai": settings.openai_model,
        "openai-compatible": settings.openai_compatible_model,
        "ollama": settings.ollama_model,
        "anthropic": settings.anthropic_model,
    }.get(name, "")


def _base_url_for_name(name: str, settings: Settings) -> str:
    return {
        "gemini": settings.gemini_base_url,
        "openrouter": settings.openrouter_base_url,
        "openai": settings.openai_base_url,
        "openai-compatible": settings.openai_compatible_base_url,
        "ollama": settings.ollama_base_url,
        "anthropic": settings.anthropic_base_url,
    }.get(name, "")


def _is_local_name(name: str, settings: Settings) -> bool:
    if name in _LOCAL_PROVIDER_NAMES:
        return True
    host = (urlparse(_base_url_for_name(name, settings)).hostname or "").lower()
    return host in _LOOPBACK_HOSTS


def _lookup_keyed(
    entries: Sequence[tuple[str, Any]], provider: str, model: str
) -> Any:
    wanted = {f"{provider}/{model}".lower(): 0, provider.lower(): 1}
    best: tuple[int, Any] | None = None
    for key, value in entries:
        rank = wanted.get(key.lower())
        if rank is not None and (best is None or rank < best[0]):
            best = (rank, value)
    return best[1] if best else None


def price_for(
    settings: Settings, provider: str, model: str
) -> tuple[float, float] | None:
    entries = [(key, (inp, out)) for key, inp, out in settings.provider_prices]
    found = _lookup_keyed(entries, provider, model)
    return found if isinstance(found, tuple) else None


def context_tokens_for(settings: Settings, provider: str, model: str) -> int | None:
    found = _lookup_keyed(list(settings.model_context_tokens), provider, model)
    return found if isinstance(found, int) else None


def estimate_cost(
    settings: Settings,
    provider: str,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> float | None:
    price = price_for(settings, provider, model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    input_per_1k, output_per_1k = price
    return (input_tokens / 1000.0) * input_per_1k + (output_tokens / 1000.0) * output_per_1k


# ---------------------------------------------------------------------------
# Token usage normalisation
# ---------------------------------------------------------------------------
def _token_value(usage: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return max(0, int(value))
    return None


def normalize_token_usage(
    usage: Mapping[str, Any],
    *,
    request: CompletionRequest,
    content: str,
) -> tuple[int, int, bool]:
    """Return (input_tokens, output_tokens, estimated)."""
    input_tokens = _token_value(usage, _INPUT_TOKEN_KEYS)
    output_tokens = _token_value(usage, _OUTPUT_TOKEN_KEYS)
    total = _token_value(usage, ("total_tokens", "totalTokenCount"))
    if input_tokens is not None and output_tokens is None and total is not None:
        output_tokens = max(0, total - input_tokens)
    estimated = False
    if input_tokens is None:
        input_tokens = estimate_request_tokens(request)
        estimated = True
    if output_tokens is None:
        output_tokens = estimate_response_tokens(content)
        estimated = True
    return input_tokens, output_tokens, estimated


# ---------------------------------------------------------------------------
# Usage recording + analytics
# ---------------------------------------------------------------------------
def usage_path(settings: Settings) -> Path:
    return settings.data_dir / USAGE_FILENAME


def make_usage_observer(settings: Settings) -> Any:
    """Return a router observer that appends usage JSONL, or ``None``.

    Recording must never break a completion: every failure is swallowed
    after a debug log. No prompt/response text is persisted — only
    provider/model, token counts, latency, success, and shape flags.
    """
    if not settings.provider_analytics_enabled:
        return None
    path = usage_path(settings)
    lock = threading.Lock()

    def _observe(event: ProviderCallEvent) -> None:
        try:
            record = _record_from_event(settings, event)
            line = json.dumps(record, ensure_ascii=True)
            with lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if path.stat().st_size >= _USAGE_ROTATE_BYTES:
                        path.replace(path.with_suffix(".jsonl.1"))
                except OSError:
                    _LOG.debug("usage log rotation failed", exc_info=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except Exception:  # noqa: BLE001 - metering must never break a completion.
            _LOG.debug("provider usage recording failed", exc_info=True)

    return _observe


def _record_from_event(settings: Settings, event: ProviderCallEvent) -> dict[str, Any]:
    input_tokens: int | None = None
    output_tokens: int | None = None
    tokens_estimated = False
    cost: float | None = None
    if event.success:
        usage = event.response.usage if event.response is not None else {}
        content = ""
        if event.response is not None:
            content = event.response.content
        elif event.streamed_text is not None:
            content = event.streamed_text
        input_tokens, output_tokens, tokens_estimated = normalize_token_usage(
            usage, request=event.request, content=content
        )
        cost = estimate_cost(
            settings, event.provider, event.model, input_tokens, output_tokens
        )
    return {
        "ts": _now_iso(),
        "provider": event.provider,
        "model": event.model,
        "success": event.success,
        "elapsed_ms": round(event.elapsed_ms, 1),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tokens_estimated": tokens_estimated,
        "estimated_cost": round(cost, 6) if cost is not None else None,
        "json_mode": bool(event.request.json_mode),
        "images": len(event.request.images),
        "streamed": event.streamed,
        "error": redact_text(event.error)[:200] if event.error else None,
    }


def load_usage_records(settings: Settings, *, limit: int = 5000) -> list[dict[str, Any]]:
    path = usage_path(settings)
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > _MAX_USAGE_READ_BYTES:
                handle.seek(size - _MAX_USAGE_READ_BYTES)
                handle.readline()  # discard partial line
            raw = handle.read()
    except OSError:
        _LOG.debug("provider usage log unreadable", exc_info=True)
        return []
    records: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records[-limit:]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 1)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 1)


def usage_stats(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    total_input = 0
    total_output = 0
    total_cost = 0.0
    any_cost = False
    successes = 0
    for record in records:
        provider = str(record.get("provider") or "unknown")
        model = str(record.get("model") or "unknown")
        group = groups.setdefault(
            (provider, model),
            {
                "provider": provider,
                "model": model,
                "calls": 0,
                "successes": 0,
                "failures": 0,
                "latencies": [],
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_tokens_used": False,
                "estimated_cost": None,
                "streamed_calls": 0,
                "last_seen": None,
            },
        )
        group["calls"] += 1
        if record.get("success"):
            group["successes"] += 1
            successes += 1
        else:
            group["failures"] += 1
        elapsed = record.get("elapsed_ms")
        if isinstance(elapsed, (int, float)):
            group["latencies"].append(float(elapsed))
        in_tok = record.get("input_tokens")
        out_tok = record.get("output_tokens")
        if isinstance(in_tok, int):
            group["input_tokens"] += in_tok
            total_input += in_tok
        if isinstance(out_tok, int):
            group["output_tokens"] += out_tok
            total_output += out_tok
        if record.get("tokens_estimated"):
            group["estimated_tokens_used"] = True
        cost = record.get("estimated_cost")
        if isinstance(cost, (int, float)):
            group["estimated_cost"] = (group["estimated_cost"] or 0.0) + float(cost)
            total_cost += float(cost)
            any_cost = True
        if record.get("streamed"):
            group["streamed_calls"] += 1
        group["last_seen"] = record.get("ts") or group["last_seen"]

    rows: list[dict[str, Any]] = []
    for group in sorted(groups.values(), key=lambda item: (item["provider"], item["model"])):
        latencies: list[float] = group.pop("latencies")
        ordered = sorted(latencies)
        group["median_latency_ms"] = _median(latencies)
        group["p95_latency_ms"] = (
            round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 1)
            if ordered
            else None
        )
        group["success_rate"] = (
            round(group["successes"] / group["calls"], 3) if group["calls"] else None
        )
        if group["estimated_cost"] is not None:
            group["estimated_cost"] = round(group["estimated_cost"], 6)
        rows.append(group)

    calls = len(records)
    return {
        "calls": calls,
        "successes": successes,
        "success_rate": round(successes / calls, 3) if calls else None,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "estimated_cost": round(total_cost, 6) if any_cost else None,
        "by_model": rows,
    }


def usage_summary_line(stats: Mapping[str, Any]) -> str:
    cost = stats.get("estimated_cost")
    cost_text = f"; est_cost={cost}" if cost is not None else ""
    rate = stats.get("success_rate")
    rate_text = f"{round(rate * 100)}%" if isinstance(rate, (int, float)) else "n/a"
    return (
        f"calls={stats.get('calls', 0)}; success={rate_text}; "
        f"tokens_in={stats.get('input_tokens', 0)}; "
        f"tokens_out={stats.get('output_tokens', 0)}{cost_text}"
    )


# ---------------------------------------------------------------------------
# Capability catalog + strategy routing
# ---------------------------------------------------------------------------
def capability_catalog(settings: Settings, router: ModelRouter) -> list[dict[str, Any]]:
    built = {provider.name: provider for provider in router.providers}
    catalog: list[dict[str, Any]] = []
    for status in provider_statuses(settings, router=router):
        provider = built.get(status.name)
        price = price_for(settings, status.name, status.model)
        catalog.append(
            {
                "provider": status.name,
                "model": status.model,
                "configured": status.configured,
                "implemented": status.implemented,
                "in_route": status.in_route,
                "local": _is_local_name(status.name, settings),
                "streaming": bool(
                    provider is not None
                    and getattr(provider, "supports_streaming", False)
                    and callable(getattr(provider, "stream_complete", None))
                ),
                "vision": bool(
                    provider is not None and getattr(provider, "supports_vision", False)
                ),
                "context_tokens": context_tokens_for(
                    settings, status.name, status.model
                ),
                "input_per_1k": price[0] if price else None,
                "output_per_1k": price[1] if price else None,
                "priced": price is not None,
                "detail": status.detail,
            }
        )
    return catalog


def ordered_provider_names(settings: Settings) -> list[str]:
    """Configured order adjusted by the routing strategy (stable)."""
    names = list(settings.effective_provider_order)
    strategy = settings.provider_strategy
    if strategy == "default" or len(names) < 2:
        return names
    if strategy == "local-first":
        return sorted(names, key=lambda name: 0 if _is_local_name(name, settings) else 1)
    if strategy == "fastest":
        stats = usage_stats(load_usage_records(settings))
        median_by_provider: dict[str, float] = {}
        for row in stats["by_model"]:
            median = row.get("median_latency_ms")
            if isinstance(median, (int, float)):
                current = median_by_provider.get(row["provider"])
                if current is None or median < current:
                    median_by_provider[row["provider"]] = float(median)

        def fastest_key(name: str) -> tuple[int, float]:
            median = median_by_provider.get(name)
            return (0, median) if median is not None else (1, 0.0)

        return sorted(names, key=fastest_key)
    if strategy == "cheapest":
        def cheapest_key(name: str) -> tuple[int, float]:
            price = price_for(settings, name, _model_for_name(name, settings))
            if price is None:
                return (1, 0.0)
            return (0, price[0] + price[1])

        return sorted(names, key=cheapest_key)
    return names


def route_explanation(settings: Settings, router: ModelRouter) -> dict[str, Any]:
    effective = list(router.provider_names)
    strategy = settings.provider_strategy
    reasons = {
        "default": "configured PC_AGENT_PROVIDER_ORDER",
        "local-first": "loopback/offline providers first, configured order otherwise",
        "fastest": "measured median latency from local usage analytics; unmeasured keep configured order",
        "cheapest": "user-configured per-1k prices ascending; unpriced keep configured order last",
    }
    return {
        "strategy": strategy,
        "configured_order": list(settings.provider_order),
        "suggested_order": ordered_provider_names(settings),
        "effective_route": effective,
        "reason": reasons.get(strategy, reasons["default"]),
        "analytics_enabled": settings.provider_analytics_enabled,
    }


def provider_platform_line(settings: Settings, router: ModelRouter) -> str:
    stats = usage_stats(load_usage_records(settings))
    evaluation = load_latest_evaluation(settings)
    if evaluation:
        eval_text = f"eval={evaluation.get('passed')}/{evaluation.get('total')}"
    else:
        eval_text = "eval=not-run"
    priced = sum(
        1
        for row in capability_catalog(settings, router)
        if row["priced"]
    )
    return (
        f"strategy={settings.provider_strategy}; "
        f"route={' -> '.join(router.provider_names)}; "
        f"{usage_summary_line(stats)}; priced_models={priced}; {eval_text}"
    )


# ---------------------------------------------------------------------------
# Evaluations
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EvalCase:
    id: str
    prompt: str
    check: str  # contains | nonempty | json_object | json_key
    argument: str | None = None
    system: str | None = None
    json_mode: bool = False
    max_output_tokens: int = 256


BUILTIN_EVAL_CASES: tuple[EvalCase, ...] = (
    EvalCase(
        id="keyword_echo",
        prompt="Say the word BANANA.",
        check="contains",
        argument="BANANA",
    ),
    EvalCase(
        id="nonempty_reply",
        prompt="Greet the user in one short sentence.",
        check="nonempty",
    ),
    EvalCase(
        id="json_object",
        prompt="Describe a blue square.",
        check="json_object",
        system="Respond with a single JSON object only.",
        json_mode=True,
    ),
    EvalCase(
        id="json_planner_shape",
        prompt="Merge two PDF files.",
        check="json_key",
        argument="intent",
        system="Respond with a single JSON object that has an 'intent' key.",
        json_mode=True,
    ),
)


def _check_case(case: EvalCase, content: str) -> tuple[bool, str]:
    text = content.strip()
    if case.check == "nonempty":
        return (bool(text), "non-empty reply" if text else "empty reply")
    if case.check == "contains":
        found = (case.argument or "") in text
        return found, f"contains {case.argument!r}: {found}"
    if case.check in {"json_object", "json_key"}:
        candidate = text
        if candidate.startswith("```"):
            candidate = candidate.strip("`")
            if candidate.lower().startswith("json"):
                candidate = candidate[4:].strip()
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            return False, "no JSON object found"
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return False, "invalid JSON"
        if not isinstance(parsed, dict):
            return False, "JSON is not an object"
        if case.check == "json_key":
            present = (case.argument or "") in parsed
            return present, f"has key {case.argument!r}: {present}"
        return True, "valid JSON object"
    return False, f"unknown check {case.check}"


def run_evaluations(
    settings: Settings,
    router: ModelRouter,
    cases: Sequence[EvalCase] = BUILTIN_EVAL_CASES,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for case in cases:
        request = CompletionRequest.from_prompt(
            case.prompt,
            system_instruction=case.system,
            json_mode=case.json_mode,
            max_output_tokens=case.max_output_tokens,
        )
        started = time.perf_counter()
        try:
            routed = router.complete(request)
        except ProviderError as exc:
            results.append(
                {
                    "id": case.id,
                    "passed": False,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "provider": None,
                    "model": None,
                    "detail": redact_text(str(exc))[:200],
                }
            )
            continue
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        passed, detail = _check_case(case, routed.response.content)
        results.append(
            {
                "id": case.id,
                "passed": passed,
                "latency_ms": elapsed,
                "provider": routed.response.provider,
                "model": routed.response.model,
                "detail": detail,
            }
        )
    passed_count = sum(1 for row in results if row["passed"])
    report = {
        "generated_at": _now_iso(),
        "strategy": settings.provider_strategy,
        "route": list(router.provider_names),
        "cases": results,
        "passed": passed_count,
        "total": len(results),
        "score": round(passed_count / len(results), 3) if results else None,
        "note": (
            "Offline smoke checks against the configured route; with only the "
            "mock provider they verify wiring, not model quality."
        ),
    }
    save_latest_evaluation(settings, report)
    return report


def save_latest_evaluation(settings: Settings, report: Mapping[str, Any]) -> None:
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        (settings.data_dir / EVAL_FILENAME).write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
    except OSError:
        _LOG.debug("could not persist provider evaluation report", exc_info=True)


def load_latest_evaluation(settings: Settings) -> dict[str, Any] | None:
    path = settings.data_dir / EVAL_FILENAME
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None
