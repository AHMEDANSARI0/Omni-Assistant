from __future__ import annotations

from pathlib import Path

import pytest

from pc_agent.errors import PolicyViolationError
from pc_agent.policies.paths import resolve_within
from pc_agent.policies.policy import Action, ActionRequest, PolicyEngine, PolicyOutcome


@pytest.fixture()
def engine(tmp_path: Path) -> PolicyEngine:
    return PolicyEngine(tmp_path / "workspace")


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    return (tmp_path / "workspace").resolve()


def test_read_and_write_inside_workspace_are_allowed(engine: PolicyEngine, workspace: Path) -> None:
    read = engine.evaluate(ActionRequest(Action.READ_FILE, workspace / "a.pdf"))
    write = engine.evaluate(ActionRequest(Action.WRITE_FILE, workspace / "out.pdf"))

    assert read.allowed
    assert write.allowed


def test_delete_inside_workspace_needs_approval(engine: PolicyEngine, workspace: Path) -> None:
    decision = engine.evaluate(ActionRequest(Action.DELETE_FILE, workspace / "a.pdf"))

    assert decision.outcome is PolicyOutcome.APPROVAL_REQUIRED


def test_delete_outside_workspace_is_denied(engine: PolicyEngine, tmp_path: Path) -> None:
    decision = engine.evaluate(ActionRequest(Action.DELETE_FILE, tmp_path / "outside" / "a.pdf"))

    assert decision.outcome is PolicyOutcome.DENY


def test_read_outside_workspace_needs_approval(engine: PolicyEngine, tmp_path: Path) -> None:
    decision = engine.evaluate(ActionRequest(Action.READ_FILE, tmp_path / "Desktop" / "a.pdf"))

    assert decision.outcome is PolicyOutcome.APPROVAL_REQUIRED


def test_credential_access_is_always_denied(engine: PolicyEngine, workspace: Path) -> None:
    by_action = engine.evaluate(ActionRequest(Action.ACCESS_CREDENTIALS))
    by_path = engine.evaluate(ActionRequest(Action.READ_FILE, workspace / ".env"))

    assert by_action.outcome is PolicyOutcome.DENY
    assert by_path.outcome is PolicyOutcome.DENY


def test_shell_and_network_need_approval(engine: PolicyEngine) -> None:
    assert engine.evaluate(ActionRequest(Action.RUN_SHELL)).requires_approval
    assert engine.evaluate(ActionRequest(Action.NETWORK_ACCESS)).requires_approval


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    base = tmp_path / "workspace"
    base.mkdir()

    with pytest.raises(PolicyViolationError):
        resolve_within(base, "../secret.txt")
    with pytest.raises(PolicyViolationError):
        resolve_within(base, tmp_path / "other" / "secret.txt")
    # A sibling folder sharing a name prefix must not slip through either.
    with pytest.raises(PolicyViolationError):
        resolve_within(base, tmp_path / "workspace-other" / "file.txt")


def test_path_inside_workspace_resolves(tmp_path: Path) -> None:
    base = tmp_path / "workspace"
    base.mkdir()

    resolved = resolve_within(base, "sub/file.txt")

    assert resolved == (base / "sub" / "file.txt").resolve()
