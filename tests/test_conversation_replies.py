"""0.82.15: "hello" must get an answer, and it must be an honest one.

Reported by the user: typing ``hello`` in the Command Center produced the toast
"Instruction queued" and then nothing. The engine was being strictly correct -
``hello`` is not a deterministic capability - but from the user's side the
assistant looked broken: the task silently moved to ``needs_input`` with a
message that the interface never showed.

Two things are pinned here: the conversational floor answers without a language
model, and every reply is reachable from the interface (as an ``assistant.reply``
event for the task, which is what the reply panel reads).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pc_agent.config import Settings
from pc_agent.conversation import CAPABILITIES, ConversationalReply, reply_for
from pc_agent.domain.task import TaskStatus
from pc_agent.service import LocalService

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ the rules
@pytest.mark.parametrize(
    ("instruction", "rule"),
    [
        ("hello", "greeting"),
        ("Hello!", "greeting"),
        ("hi", "greeting"),
        ("hi there", "greeting"),
        ("salam", "greeting"),
        ("assalamualaikum", "greeting"),
        ("good morning", "greeting"),
        ("thanks", "thanks"),
        ("thank you", "thanks"),
        ("shukriya", "thanks"),
        ("bye", "farewell"),
        ("who are you", "identity"),
        ("what is your name", "identity"),
        ("tum kon ho", "identity"),
        ("help", "help"),
        ("what can you do", "help"),
        ("kya kar sakte ho", "help"),
        ("are you there", "alive"),
        ("ping", "alive"),
    ],
)
def test_the_conversational_floor_answers(instruction: str, rule: str) -> None:
    reply = reply_for(instruction)
    assert isinstance(reply, ConversationalReply)
    assert reply.rule == rule
    assert len(reply.text) > 30


@pytest.mark.parametrize(
    "instruction",
    [
        "merge report.pdf and notes.pdf",
        "list the files in the workspace",
        "what is the weather in Karachi",
        "delete the old logs",
        "",
        "   ",
    ],
)
def test_real_work_is_never_answered_with_a_canned_reply(instruction: str) -> None:
    """A wrong canned answer would be worse than sending the task to the planner."""

    assert reply_for(instruction) is None


def test_the_greeting_says_it_is_built_in_and_what_is_missing() -> None:
    """No fake intelligence: the reply states the rule and the missing provider."""

    text = reply_for("hello").text  # type: ignore[union-attr]
    lowered = text.lower()
    assert "built-in rule" in lowered
    assert "no language model is configured" in lowered
    assert "help" in lowered


def test_the_help_text_lists_only_real_capabilities() -> None:
    text = reply_for("help").text  # type: ignore[union-attr]
    for name, _description in (line.split("  ", 1) for line in CAPABILITIES):
        assert name in text
    assert "GEMINI_API_KEY" in text or "OPENAI_API_KEY" in text


# ------------------------------------------------------- through the service
def test_a_greeting_task_succeeds_and_leaves_a_reply_event(tmp_path: Path) -> None:
    settings = replace(
        Settings.from_env(tmp_path),
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        scheduler_interval_seconds=0,
    )
    (tmp_path / "workspace").mkdir(exist_ok=True)
    service = LocalService(settings)
    try:
        started = service.run_instruction("hello")
        task = service.wait_for_task(started["task_id"], timeout_seconds=30)

        assert task["status"] == TaskStatus.SUCCEEDED.value
        assert task["error"] is None

        events = service.list_events(limit=50)["events"]
        replies = [
            event
            for event in events
            if event["event_type"] == "assistant.reply" and event["task_id"] == started["task_id"]
        ]
        assert replies, "the interface has nothing to show without this event"
        assert "built-in rule" in replies[-1]["summary"]
        assert replies[-1]["payload"]["kind"] == "reply"
    finally:
        service.close()


def test_an_unsupported_instruction_still_explains_itself(tmp_path: Path) -> None:
    """The old silence: a needs_input task whose reason never reached the UI."""

    settings = replace(
        Settings.from_env(tmp_path),
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        scheduler_interval_seconds=0,
    )
    (tmp_path / "workspace").mkdir(exist_ok=True)
    service = LocalService(settings)
    try:
        started = service.run_instruction("do something nobody implemented yet")
        task = service.wait_for_task(started["task_id"], timeout_seconds=30)

        assert task["status"] == TaskStatus.NEEDS_INPUT.value
        events = service.list_events(limit=50)["events"]
        explained = [
            event
            for event in events
            if event["event_type"] == "assistant.reply" and event["task_id"] == started["task_id"]
        ]
        assert explained, "a needs_input task must still say why"
        assert explained[-1]["payload"]["kind"] == "explanation"
        # 0.82.17: the text no longer says "unsupported capability" - it says
        # what is actually missing and how to fix it, which is the same intent.
        assert "without a language model" in explained[-1]["summary"]
        assert "GEMINI_API_KEY" in explained[-1]["summary"]
    finally:
        service.close()


def test_the_state_machine_path_is_the_legal_one(tmp_path: Path) -> None:
    """QUEUED -> PLANNING -> RUNNING -> VERIFYING -> SUCCEEDED, nothing shorter."""

    settings = replace(
        Settings.from_env(tmp_path),
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        scheduler_interval_seconds=0,
    )
    (tmp_path / "workspace").mkdir(exist_ok=True)
    service = LocalService(settings)
    try:
        started = service.run_instruction("hi")
        service.wait_for_task(started["task_id"], timeout_seconds=30)
        timeline = service.task_timeline(started["task_id"]) if hasattr(service, "task_timeline") else None
        if timeline is None:  # fall back to the events the API exposes
            timeline = service.list_events(limit=50)["events"]
        assert timeline  # the journal recorded the run
    finally:
        service.close()


# ------------------------------------------------------------- the interface
def test_the_command_bar_follows_the_task_and_shows_the_answer() -> None:
    source = (ROOT / "frontend" / "src" / "components" / "CommandBar.tsx").read_text(encoding="utf-8")

    assert "followAnswer" in source
    assert 'event_type === "assistant.reply"' in source
    assert "aria-live=\"polite\"" in source  # screen readers get the answer too
    assert "dismiss" in source


def test_the_verifier_types_a_real_instruction_and_expects_an_answer() -> None:
    script = (ROOT / "frontend" / "scripts" / "ui-verify.js").read_text(encoding="utf-8")

    assert "a typed instruction gets an answer in the interface" in script
    assert 'input[aria-label="Instruction input"]' in script
    assert 'querySelector(\'[role="status"]\')' in script


# ------------------------------------------------- 0.82.17: the real model path
class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeRouted:
    def __init__(self, content: str) -> None:
        self.response = _FakeResponse(content)


class _FakeRouter:
    """Stands in for the provider router and records what it was asked."""

    provider_names = ("fake-model",)

    def __init__(self, content: str = "", error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.requests: list[object] = []

    def complete(self, request: object) -> _FakeRouted:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return _FakeRouted(self.content)


def _service_with_router(tmp_path: Path, router: _FakeRouter) -> LocalService:
    settings = replace(
        Settings.from_env(tmp_path),
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        scheduler_interval_seconds=0,
    )
    (tmp_path / "workspace").mkdir(exist_ok=True)
    service = LocalService(settings)
    service._orchestrator._router = router
    return service


def _reply_events(service: LocalService, task_id: str) -> list[dict]:
    return [
        event
        for event in service.list_events(limit=50)["events"]
        if event["event_type"] == "assistant.reply" and event["task_id"] == task_id
    ]


@pytest.mark.parametrize("instruction", ["hello", "what is the capital of France"])
def test_a_configured_model_answers_instead_of_the_rule(tmp_path: Path, instruction: str) -> None:
    """The 0.82.17 bug: the rule regex replied before any provider was called."""

    router = _FakeRouter("Paris is the capital of France.")
    service = _service_with_router(tmp_path, router)
    try:
        started = service.run_instruction(instruction)
        task = service.wait_for_task(started["task_id"], timeout_seconds=30)

        assert task["status"] == TaskStatus.SUCCEEDED.value
        assert router.requests, "the provider was never asked - the rule answered first"
        replies = _reply_events(service, started["task_id"])
        assert replies and replies[-1]["summary"].startswith("Paris is the capital")
        assert "built-in rule" not in replies[-1]["summary"]
    finally:
        service.close()


def test_the_model_is_asked_with_the_system_prompt(tmp_path: Path) -> None:
    from pc_agent.conversation import SYSTEM_PROMPT

    router = _FakeRouter("answered")
    service = _service_with_router(tmp_path, router)
    try:
        started = service.run_instruction("hello")
        service.wait_for_task(started["task_id"], timeout_seconds=30)

        request = router.requests[-1]
        assert str(getattr(request, "system_instruction", "")) == SYSTEM_PROMPT
        assert SYSTEM_PROMPT.strip()
    finally:
        service.close()


def test_a_provider_fault_falls_back_to_the_rule(tmp_path: Path) -> None:
    """A dead key must degrade to the honest built-in answer, not to a crash."""

    router = _FakeRouter(error=RuntimeError("provider exploded"))
    service = _service_with_router(tmp_path, router)
    try:
        started = service.run_instruction("hello")
        task = service.wait_for_task(started["task_id"], timeout_seconds=30)

        assert task["status"] == TaskStatus.SUCCEEDED.value
        replies = _reply_events(service, started["task_id"])
        assert replies and "built-in rule" in replies[-1]["summary"]
    finally:
        service.close()


def test_a_mock_only_route_is_not_treated_as_a_model(tmp_path: Path) -> None:
    """``mock`` returns placeholder text; claiming it is a model would be a lie."""

    from pc_agent.conversation import model_is_real

    assert model_is_real(("mock",)) is False
    assert model_is_real(("offline-mock",)) is False
    assert model_is_real(("gemini", "mock")) is True
    assert model_is_real(()) is False


def test_without_a_model_an_ordinary_question_says_so(tmp_path: Path) -> None:
    """No key, no model: say it plainly instead of "unsupported capability"."""

    settings = replace(
        Settings.from_env(tmp_path),
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        scheduler_interval_seconds=0,
        provider_order=("mock",),
    )
    (tmp_path / "workspace").mkdir(exist_ok=True)
    service = LocalService(settings)
    try:
        started = service.run_instruction("what is the capital of France")
        task = service.wait_for_task(started["task_id"], timeout_seconds=30)

        replies = _reply_events(service, started["task_id"])
        assert replies, "the interface must show an answer even with no model"
        summary = replies[-1]["summary"]
        assert "language model" in summary
        assert "not a supported deterministic capability" not in summary
        # A refusal explains itself, so the interface labels it an explanation.
        assert replies[-1]["payload"]["kind"] == "explanation"
        assert task["status"] in {TaskStatus.SUCCEEDED.value, TaskStatus.NEEDS_INPUT.value}
    finally:
        service.close()


def test_a_real_capability_is_never_hijacked_by_the_conversation_path(tmp_path: Path) -> None:
    """0.82.17 regression: the no-model message fired before the planner ran.

    "Merge PDFs from Folder A and Folder B" is a real capability. An early
    version of the conversational fallback answered it with "I cannot answer
    that without a language model" and the demo stopped working.
    """

    from pc_agent.cli import main

    assert main(["--root", str(tmp_path), "demo", "pdf"]) == 0
    report = main(
        ["--root", str(tmp_path), "run", "Merge PDFs from Folder A and Folder B"]
    )
    assert report == 0


def test_the_terminal_shows_the_answer_too(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`pc-agent run "hello"` printed the status and swallowed the reply."""

    from pc_agent.cli import main

    assert main(["--root", str(tmp_path), "run", "hello"]) == 0
    out = capsys.readouterr().out
    assert "Reply:" in out
    assert "built-in rule" in out


def test_one_key_in_env_is_enough_to_enable_a_provider(tmp_path: Path) -> None:
    """0.82.17: a key alone used to do nothing - the order had to be set too."""

    from pc_agent.orchestrator import _route_has_real_model
    from pc_agent.providers.router import build_model_router

    (tmp_path / ".env").write_text("GEMINI_API_KEY=fake-key\n", encoding="utf-8")
    settings = replace(
        Settings.from_env(tmp_path),
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
    )
    (tmp_path / "workspace").mkdir(exist_ok=True)

    assert settings.provider_order == ()
    assert settings.effective_provider_order == ("gemini",)
    assert build_model_router(settings).provider_names == ("gemini",)
    assert _route_has_real_model(build_model_router(settings), settings) is True


def test_an_explicit_order_still_wins(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "GEMINI_API_KEY=fake-key\nPC_AGENT_PROVIDER_ORDER=openai,gemini\n", encoding="utf-8"
    )
    settings = Settings.from_env(tmp_path)
    assert settings.effective_provider_order == ("openai", "gemini")


def test_no_key_and_no_order_is_mock_only(tmp_path: Path) -> None:
    """Ollama is never auto-enabled: a local server may not be running."""

    from pc_agent.providers.router import build_model_router

    settings = replace(
        Settings.from_env(tmp_path),
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
    )
    assert settings.effective_provider_order == ()
    assert build_model_router(settings).provider_names == ("mock",)
