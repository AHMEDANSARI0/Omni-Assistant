"""Deterministic, rule-based replies for conversational input (0.82.15).

Why this exists: typing "hello" into the Command Center produced a task that
immediately moved to ``needs_input`` with the message "This instruction is not a
supported deterministic capability yet", and the interface showed only
"Instruction queued". From the user's side that is silence - the assistant looked
broken when it was being strictly correct.

This module answers the *conversational floor* - greetings, thanks, "who are
you", "what can you do", "are you there" - with plain, honest text produced by
rules, not by a language model. It never invents facts about the machine, never
claims to have done work, and says outright that open-ended conversation needs a
configured provider. Everything it returns is deterministic, so it can be tested
exactly.

Deliberately not covered: questions about the user's files, the PC, the web, or
anything requiring execution. Those must go through the planner or a provider -
guessing an answer would be worse than saying "I cannot do that yet".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "CAPABILITIES",
    "SYSTEM_PROMPT",
    "ConversationalReply",
    "model_is_real",
    "reply_for",
    "reply_without_model",
]

#: The instruction a real provider gets when the user is just talking. It keeps
#: the assistant honest: it may not claim to have run tools it did not run.
SYSTEM_PROMPT = (
    "You are Omni-Assistant, a local desktop assistant running on the user's own "
    "Windows machine. Answer briefly and directly (under 120 words). You are in "
    "conversation mode: you have NOT run any tool, opened any file, or changed "
    "anything - never claim you did, and never invent facts about the user's files, "
    "system or accounts. If the user asks you to do something to their machine, say "
    "what you would need and that they should give it as a task so it can be planned, "
    "approved and verified."
)


def model_is_real(route: list[str] | tuple[str, ...]) -> bool:
    """True when the route contains a provider that is an actual model.

    ``mock`` (``offline-mock``) is a placeholder that cannot hold a conversation;
    treating it as a model would produce the fake answers this project refuses to
    ship.
    """

    return any(name not in {"mock", "offline-mock"} for name in route)


def reply_without_model(instruction: str, *, route: list[str] | tuple[str, ...]) -> ConversationalReply:
    """What to say when there is no real model: the rule text, plus how to fix it."""

    canned = reply_for(instruction)
    header = (
        "No language model is configured, so this answer is a built-in rule - not a "
        "model. Add one key to .env and restart: GEMINI_API_KEY (free tier), "
        "OPENAI_API_KEY, ANTHROPIC_API_KEY or OPENROUTER_API_KEY; or run Ollama "
        "locally and set PC_AGENT_PROVIDER_ORDER=ollama. Then ask me again."
    )
    if canned is not None:
        return ConversationalReply(f"{canned.text}\n\n{header}", rule=f"{canned.rule}+no-model")
    return ConversationalReply(
        f"I cannot answer that without a language model.\n\n{header}",
        rule="no-model",
    )

#: What the local engine can actually do without a model. Kept short on purpose:
#: it is shown in the interface, so it must stay true after every release.
CAPABILITIES = (
    "tools          list and run the 69 built-in tools (pdf, files, web, desktop, image)",
    "tasks          queue an instruction, watch it plan, run and verify",
    "missions       group tasks into a goal with dependencies",
    "memory         store and recall notes locally (sqlite + local embeddings)",
    "knowledge      index documents and query them",
    "automation     browser/desktop actions, workflows",
    "system         health, logs, events, settings, profiles",
)


@dataclass(frozen=True, slots=True)
class ConversationalReply:
    """A canned answer plus the rule that produced it."""

    text: str
    rule: str

    def to_dict(self) -> dict[str, str]:
        return {"text": self.text, "rule": self.rule}


def _normalise(instruction: str) -> str:
    lowered = instruction.strip().lower()
    lowered = re.sub(r"[^\w\s'?]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _capability_text() -> str:
    lines = "\n".join(f"  {line}" for line in CAPABILITIES)
    return (
        "I run locally and deterministically; there is no language model in this "
        "path, so I will not pretend to chat freely. What works right now:\n"
        f"{lines}\n"
        "  conversation   greetings, thanks, this help text\n"
        "\nFor open conversation (and for instructions outside these capabilities) "
        "configure a provider: set GEMINI_API_KEY, OPENAI_API_KEY or ANTHROPIC_API_KEY "
        "in .env, then restart the app. `pc-agent release audit` warns while only the "
        "mock provider is available."
    )


_GREETING = re.compile(
    r"^(hi|hey|hello|salam|assalam|assalamualaikum|asalam|aoa|yo|hola|hiya|good (morning|afternoon|evening))"
    r"(\s+(there|omni|assistant|bot|friend))?$"
)
_THANKS = re.compile(r"^(thanks|thank you|thx|shukriya|shukria|jazakallah)(\s+\w+)?$")
_FAREWELL = re.compile(r"^(bye|goodbye|good night|khuda hafiz|allah hafiz|see you)$")
_IDENTITY = re.compile(
    r"^(who are you|what are you|what is your name|your name|tum kon ho|aap kon hain|kaun ho)$"
)
_HELP = re.compile(
    r"^(help|what can you do|what do you do|capabilities|commands|features|"
    r"kya kar sakte ho|kya kar sakta hai|tum kya kar sakte ho)$"
)
_ALIVE = re.compile(r"^(are you (there|alive|ok)|you there|test|ping|testing|hello\?+|hmm+)$")


def reply_for(instruction: str) -> ConversationalReply | None:
    """Return a canned reply, or ``None`` when this is real work to plan."""

    text = _normalise(instruction)
    if not text:
        return None

    if _GREETING.match(text):
        return ConversationalReply(
            "Hello. I am Omni, running on this machine - not a cloud service. "
            "I answered with a built-in rule, because no language model is configured "
            "yet. Type 'help' to see what I can actually do, or give me a task "
            "(for example: 'list the files in the workspace').",
            rule="greeting",
        )
    if _THANKS.match(text):
        return ConversationalReply(
            "You are welcome. I only report work I actually did, so the task journal is "
            "the honest record of what happened.",
            rule="thanks",
        )
    if _FAREWELL.match(text):
        return ConversationalReply(
            "Closing time. The engine keeps running locally; queue another instruction "
            "whenever you need it.",
            rule="farewell",
        )
    if _IDENTITY.match(text):
        return ConversationalReply(
            "I am Omni-Assistant: a local, Windows-first agent with a safety kernel, a "
            "tool registry, a mission scheduler and an audited task journal. The engine "
            "runs on loopback in this machine's own Python - nothing leaves it unless a "
            "provider or connector you configured is called.",
            rule="identity",
        )
    if _HELP.match(text):
        return ConversationalReply(_capability_text(), rule="help")
    if _ALIVE.match(text):
        return ConversationalReply(
            "Here. The local API is answering, which is why you are reading this.",
            rule="alive",
        )
    return None
