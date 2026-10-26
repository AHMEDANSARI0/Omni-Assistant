"""Shared exception types for the local core."""


class PcAgentError(Exception):
    """Base class for errors the user can act on."""


class ConfigError(PcAgentError):
    """Configuration is missing or invalid."""


class InvalidTransitionError(PcAgentError):
    """A task was asked to move to a state it cannot reach from its current state."""


class PolicyViolationError(PcAgentError):
    """An action violated the deterministic local policy/safety boundary."""


class StorageError(PcAgentError):
    """Local persistence failed or a stored record was not found/invalid."""


class ToolError(PcAgentError):
    """A registered tool could not complete its declared operation safely."""


class ToolValidationError(ToolError):
    """Tool arguments or output did not match the registered schema."""


class ProviderError(PcAgentError):
    """An AI provider call failed in a way the router can report safely."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class PlanningError(PcAgentError):
    """A planner produced output that cannot safely become an executable plan."""


class ApprovalUnavailableError(PcAgentError):
    """Approval was required but no approval decision could be collected."""
