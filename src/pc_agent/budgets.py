"""Deterministic per-task provider/tool/time budgets.

Budgets are local safety limits, not provider billing quotes.  When a
provider reports usage, that usage is counted.  When it does not, the
tracker uses a transparent character-based estimate and reserves the
request's maximum output before the call starts.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import ProviderError
from .providers.base import CompletionRequest


class BudgetExceededError(ProviderError):
    """A task reached a configured provider/tool/time budget."""

    def __init__(self, message: str) -> None:
        super().__init__(f"Task budget exceeded: {message}", retryable=False)


@dataclass(frozen=True, slots=True)
class TaskBudgetLimits:
    token_budget: int = 20_000
    provider_call_budget: int = 10
    tool_budget: int = 25
    step_budget: int = 5
    time_budget_seconds: float = 600.0

    def __post_init__(self) -> None:
        if self.token_budget < 0 or self.token_budget > 10_000_000:
            raise ValueError("token_budget must be between 0 and 10000000.")
        if self.provider_call_budget < 0 or self.provider_call_budget > 1000:
            raise ValueError("provider_call_budget must be between 0 and 1000.")
        if self.tool_budget < 0 or self.tool_budget > 1000:
            raise ValueError("tool_budget must be between 0 and 1000.")
        if self.step_budget < 1 or self.step_budget > 20:
            raise ValueError("step_budget must be between 1 and 20.")
        if (
            not math.isfinite(self.time_budget_seconds)
            or self.time_budget_seconds < 0
            or self.time_budget_seconds > 86_400
        ):
            raise ValueError("time_budget_seconds must be between 0 and 86400.")


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    token_budget: int
    tokens_used: int
    tokens_reported: int
    tokens_estimated: int
    provider_call_budget: int
    provider_calls: int
    provider_attempts: int
    tool_budget: int
    tool_invocations: int
    step_budget: int
    steps_planned: int
    time_budget_seconds: float
    elapsed_seconds: float
    exceeded: str = ""

    def summary(self) -> str:
        def limit(value: float) -> str:
            return "unlimited" if value == 0 else str(value)

        text = (
            f"tokens={self.tokens_used}/{limit(self.token_budget)}, "
            f"provider_calls={self.provider_calls}/{limit(self.provider_call_budget)}, "
            f"provider_attempts={self.provider_attempts}, "
            f"tools={self.tool_invocations}/{limit(self.tool_budget)}, "
            f"steps={self.steps_planned}/{self.step_budget}, "
            f"elapsed={self.elapsed_seconds:.1f}s/{limit(self.time_budget_seconds)}"
        )
        if self.tokens_estimated:
            text += f", estimated_tokens={self.tokens_estimated}"
        if self.exceeded:
            text += f", exceeded={self.exceeded}"
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_budget": self.token_budget,
            "tokens_used": self.tokens_used,
            "tokens_reported": self.tokens_reported,
            "tokens_estimated": self.tokens_estimated,
            "provider_call_budget": self.provider_call_budget,
            "provider_calls": self.provider_calls,
            "provider_attempts": self.provider_attempts,
            "tool_budget": self.tool_budget,
            "tool_invocations": self.tool_invocations,
            "step_budget": self.step_budget,
            "steps_planned": self.steps_planned,
            "time_budget_seconds": self.time_budget_seconds,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "exceeded": self.exceeded,
        }


def _usage_int(usage: Mapping[str, Any], *keys: str) -> int | None:
    # Providers use a mixture of snake_case/camelCase and nested metadata.
    normalized = {
        str(key).lower().replace("_", "").replace("-", ""): value
        for key, value in usage.items()
    }
    for key in keys:
        value = normalized.get(key.lower().replace("_", ""))
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, float) and value >= 0:
            return int(value)
    return None


def reported_usage_tokens(usage: Mapping[str, Any]) -> int | None:
    """Extract a total token count from common provider usage shapes."""

    total = _usage_int(
        usage, "total_tokens", "totalTokenCount", "total_token_count", "total"
    )
    if total is not None:
        return total
    prompt = _usage_int(
        usage,
        "prompt_tokens",
        "input_tokens",
        "promptTokenCount",
        "prompt_eval_count",
    )
    completion = _usage_int(
        usage,
        "completion_tokens",
        "output_tokens",
        "candidatesTokenCount",
        "eval_count",
    )
    if prompt is None and completion is None:
        return None
    return (prompt or 0) + (completion or 0)


def estimate_request_tokens(request: CompletionRequest) -> int:
    chars = sum(len(message.content) for message in request.messages)
    # Image token accounting varies sharply by provider/model.  A fixed
    # conservative reserve keeps this a safety cap without pretending to be
    # an invoice.
    image_tokens = len(request.images) * 1024
    return max(1, math.ceil(chars / 4) + image_tokens)


def estimate_response_tokens(content: str) -> int:
    return max(1, math.ceil(len(content) / 4)) if content else 0


class TaskBudgetTracker:
    """Mutable usage counters for exactly one task execution/resume."""

    def __init__(
        self,
        limits: TaskBudgetLimits,
        *,
        clock: Any = time.monotonic,
    ) -> None:
        self.limits = limits
        self._clock = clock
        self._started = float(clock())
        self.tokens_used = 0
        self.tokens_reported = 0
        self.tokens_estimated = 0
        self.provider_calls = 0
        self.provider_attempts = 0
        self.tool_invocations = 0
        self.steps_planned = 0
        self.exceeded = ""

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------
    def check_time(self) -> None:
        limit = self.limits.time_budget_seconds
        if limit and self.elapsed_seconds > limit:
            self._exceed(
                "time",
                f"elapsed {self.elapsed_seconds:.1f}s exceeds the {limit:.1f}s "
                "task time budget; no further step was started.",
            )

    def check_steps(self, steps: int) -> None:
        self.steps_planned = max(self.steps_planned, steps)
        if steps > self.limits.step_budget:
            self._exceed(
                "steps",
                f"plan has {steps} steps but the task step budget is "
                f"{self.limits.step_budget}; no tool was executed.",
            )

    def check_tool(self) -> None:
        self.check_time()
        limit = self.limits.tool_budget
        if limit and self.tool_invocations >= limit:
            self._exceed(
                "tools",
                f"tool invocation {self.tool_invocations + 1} would exceed "
                f"the task tool budget of {limit}; the tool was not executed.",
            )

    def record_tool(self) -> None:
        self.check_tool()
        self.tool_invocations += 1

    def before_provider_call(self, request: CompletionRequest) -> int:
        self.check_time()
        call_limit = self.limits.provider_call_budget
        if call_limit and self.provider_calls >= call_limit:
            self._exceed(
                "provider_calls",
                f"provider call {self.provider_calls + 1} would exceed the "
                f"task provider-call budget of {call_limit}; no provider "
                "request was sent.",
            )
        estimated_input = estimate_request_tokens(request)
        token_limit = self.limits.token_budget
        if token_limit:
            reserve = estimated_input + request.max_output_tokens
            if self.tokens_used + reserve > token_limit:
                self._exceed(
                    "tokens",
                    f"provider request needs an estimated reserve of {reserve} "
                    f"tokens but only {max(0, token_limit - self.tokens_used)} "
                    f"of {token_limit} remain; no provider request was sent.",
                )
        self.provider_calls += 1
        return estimated_input

    def after_provider_call(
        self,
        request: CompletionRequest,
        *,
        estimated_input: int,
        response: Any | None,
        attempts: int,
    ) -> None:
        self.provider_attempts += max(1, attempts)
        usage = getattr(response, "usage", {}) if response is not None else {}
        if not isinstance(usage, Mapping):
            usage = {}
        reported = reported_usage_tokens(usage)
        failed_attempt_estimates = estimated_input * max(0, attempts - 1)
        if reported is not None:
            self.tokens_reported += reported
            self.tokens_used += reported
            if failed_attempt_estimates:
                self.tokens_estimated += failed_attempt_estimates
                self.tokens_used += failed_attempt_estimates
        else:
            content = str(getattr(response, "content", "") or "")
            estimated = (
                estimated_input * max(1, attempts)
                + estimate_response_tokens(content)
            )
            self.tokens_estimated += estimated
            self.tokens_used += estimated
        if self.limits.token_budget and self.tokens_used > self.limits.token_budget:
            self._exceed(
                "tokens",
                f"provider usage is {self.tokens_used} tokens, above the "
                f"task token budget of {self.limits.token_budget}.",
            )

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, float(self._clock()) - self._started)

    def snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            token_budget=self.limits.token_budget,
            tokens_used=self.tokens_used,
            tokens_reported=self.tokens_reported,
            tokens_estimated=self.tokens_estimated,
            provider_call_budget=self.limits.provider_call_budget,
            provider_calls=self.provider_calls,
            provider_attempts=self.provider_attempts,
            tool_budget=self.limits.tool_budget,
            tool_invocations=self.tool_invocations,
            step_budget=self.limits.step_budget,
            steps_planned=self.steps_planned,
            time_budget_seconds=self.limits.time_budget_seconds,
            elapsed_seconds=self.elapsed_seconds,
            exceeded=self.exceeded,
        )

    def _exceed(self, kind: str, message: str) -> None:
        self.exceeded = kind
        raise BudgetExceededError(message)


class BudgetedModelRouter:
    """Delegate router that records every completion against one tracker."""

    def __init__(self, delegate: Any, tracker: TaskBudgetTracker) -> None:
        self._delegate = delegate
        self._tracker = tracker

    @property
    def providers(self) -> Any:
        return getattr(self._delegate, "providers", ())

    @property
    def provider_names(self) -> Any:
        return getattr(self._delegate, "provider_names", ())

    def complete(self, request: CompletionRequest, **kwargs: Any) -> Any:
        estimated_input = self._tracker.before_provider_call(request)
        try:
            routed = self._delegate.complete(request, **kwargs)
        except Exception:
            # The router does not expose attempt counts on total failure.
            # Count one routed attempt's estimated input conservatively;
            # retries remain separately bounded by provider settings.
            self._tracker.after_provider_call(
                request,
                estimated_input=estimated_input,
                response=None,
                attempts=1,
            )
            raise
        attempts = len(getattr(routed, "attempts", ()) or ()) or 1
        self._tracker.after_provider_call(
            request,
            estimated_input=estimated_input,
            response=getattr(routed, "response", routed),
            attempts=attempts,
        )
        return routed
