"""Risk-based autonomy policy.

Default rule: safe, reversible, workspace-scoped actions may run on their own.
Anything destructive, privileged, external, credential-related, or outside the
workspace needs explicit approval or is denied. This decision is made here, in
ordinary code - an AI model can request an action but can never approve it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path

from .paths import is_sensitive_path, is_within


class Action(str, enum.Enum):
    LIST_FILES = "list_files"
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    OVERWRITE_FILE = "overwrite_file"
    DELETE_FILE = "delete_file"
    RUN_SHELL = "run_shell"
    INSTALL_SOFTWARE = "install_software"
    NETWORK_ACCESS = "network_access"
    EXTERNAL_SEND = "external_send"
    PRIVILEGED_CHANGE = "privileged_change"
    ACCESS_CREDENTIALS = "access_credentials"


class RiskLevel(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PolicyOutcome(str, enum.Enum):
    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ActionRequest:
    action: Action
    target_path: Path | None = None
    description: str = ""


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    outcome: PolicyOutcome
    risk: RiskLevel
    reason: str

    @property
    def allowed(self) -> bool:
        return self.outcome is PolicyOutcome.ALLOW

    @property
    def requires_approval(self) -> bool:
        return self.outcome is PolicyOutcome.APPROVAL_REQUIRED


_FILE_ACTIONS = frozenset(
    {
        Action.LIST_FILES,
        Action.READ_FILE,
        Action.WRITE_FILE,
        Action.OVERWRITE_FILE,
        Action.DELETE_FILE,
    }
)
_DESTRUCTIVE_ACTIONS = frozenset({Action.OVERWRITE_FILE, Action.DELETE_FILE})


class PolicyEngine:
    def __init__(
        self,
        workspace_dir: Path,
        approved_read_roots: tuple[Path, ...] = (),
    ) -> None:
        self._workspace_dir = workspace_dir.expanduser().resolve()
        resolved_roots = {
            root.expanduser().resolve() for root in approved_read_roots
        }
        resolved_roots.discard(self._workspace_dir)
        self._approved_read_roots = tuple(sorted(resolved_roots, key=str))

    @property
    def workspace_dir(self) -> Path:
        return self._workspace_dir

    @property
    def approved_read_roots(self) -> tuple[Path, ...]:
        return self._approved_read_roots

    def evaluate(self, request: ActionRequest) -> PolicyDecision:
        action = request.action

        if action is Action.ACCESS_CREDENTIALS:
            return _deny(RiskLevel.CRITICAL, "Access to credentials is not permitted.")

        if request.target_path is not None and is_sensitive_path(request.target_path):
            return _deny(RiskLevel.CRITICAL, "The target looks like a credential or secret file.")

        if action in _FILE_ACTIONS:
            return self._evaluate_file_action(request)

        if action in {Action.RUN_SHELL, Action.INSTALL_SOFTWARE}:
            return _approval(
                RiskLevel.HIGH, "Running commands or installing software needs approval."
            )
        if action in {Action.NETWORK_ACCESS, Action.EXTERNAL_SEND, Action.PRIVILEGED_CHANGE}:
            return _approval(
                RiskLevel.HIGH, "Network, external, or system-level actions need approval."
            )

        return _deny(RiskLevel.HIGH, "Unknown action; denied by default.")

    def _evaluate_file_action(self, request: ActionRequest) -> PolicyDecision:
        if request.target_path is None:
            return _deny(RiskLevel.MEDIUM, "A file action requires a target path.")

        inside = is_within(self._workspace_dir, request.target_path)
        action = request.action

        if action in _DESTRUCTIVE_ACTIONS:
            if not inside:
                return _deny(
                    RiskLevel.CRITICAL,
                    "Deleting or overwriting outside the workspace is not allowed.",
                )
            return _approval(RiskLevel.HIGH, "Deleting or overwriting a file needs approval.")

        if inside:
            risk = RiskLevel.LOW if action is not Action.WRITE_FILE else RiskLevel.MEDIUM
            return PolicyDecision(PolicyOutcome.ALLOW, risk, "Workspace-scoped file action.")

        if action in {Action.LIST_FILES, Action.READ_FILE} and any(
            is_within(root, request.target_path) for root in self._approved_read_roots
        ):
            return PolicyDecision(
                PolicyOutcome.ALLOW,
                RiskLevel.MEDIUM,
                "Read/list is inside a folder approved for this specific task.",
            )

        return _approval(RiskLevel.HIGH, "The target is outside the workspace.")


def _approval(risk: RiskLevel, reason: str) -> PolicyDecision:
    return PolicyDecision(PolicyOutcome.APPROVAL_REQUIRED, risk, reason)


def _deny(risk: RiskLevel, reason: str) -> PolicyDecision:
    return PolicyDecision(PolicyOutcome.DENY, risk, reason)
