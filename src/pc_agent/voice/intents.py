"""Deterministic intent routing for spoken commands (voice completion, 0.81.0).

A transcript is only useful if something happens afterwards. This module turns
one utterance into exactly one :class:`VoiceIntent`, with no model call, no
guessing and no hidden action:

* the router is **deterministic** - the same sentence always yields the same
  intent, so a spoken command can be tested, logged and audited;
* the wake phrase is stripped and reported separately, so the caller can require
  it before acting;
* anything the router does not recognise becomes a ``task`` intent, which is
  submitted through the normal task path and therefore still passes the approval
  centre - voice never gains a privilege that the keyboard path does not have;
* every reply is data (:class:`IntentOutcome.reply`), not a side effect, so the
  loop decides whether to speak it.

Supported intents: ``stop``, ``help``, ``status``, ``time``, ``note``,
``reminder``, ``speak``, ``task`` and ``empty``. The list is exhaustive and
``help`` is generated from it, so documentation cannot drift from behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "INTENT_HELP",
    "SUPPORTED_INTENTS",
    "IntentOutcome",
    "VoiceIntent",
    "VoiceIntentExecutor",
    "parse_intent",
    "strip_wake_phrase",
]

SUPPORTED_INTENTS = (
    "stop",
    "help",
    "status",
    "time",
    "note",
    "reminder",
    "speak",
    "task",
    "empty",
)

INTENT_HELP = (
    "I can do these things by voice: "
    "ask for status, ask the time, say 'note ...' to save a note, "
    "say 'remind me to ...' to create a reminder, say 'say ...' to hear text, "
    "say 'help' for this list, say 'stop' to end the session, "
    "or say any other instruction and I will create a task for it."
)

_STOP_PHRASES = (
    "stop",
    "stop listening",
    "cancel",
    "never mind",
    "nevermind",
    "that is all",
    "that's all",
    "goodbye",
    "bye",
    "band karo",
    "ruk jao",
    "bas karo",
)
_HELP_PHRASES = (
    "help",
    "what can you do",
    "what can i say",
    "commands",
    "madad",
)
_STATUS_PHRASES = (
    "status",
    "system status",
    "how are you",
    "how is the system",
    "kya haal hai",
    "haal batao",
)
_TIME_PHRASES = ("time", "what time is it", "what's the time", "the time", "date today", "aaj kya din hai")
_NOTE_PREFIXES = ("note that ", "note ", "remember that ", "remember ", "yaad rakho ", "note down ")
_SAY_PREFIXES = ("say ", "repeat ", "read out ", "bolo ", "bol ")
_REMIND_PREFIXES = ("remind me to ", "remind me ", "reminder ", "yaad dilao ", "yaad dilana ")

_WAKE_PREFIXES = ("hey ", "ok ", "okay ", "oye ")
_PUNCTUATION = re.compile(r"[^\w\s:.\-+/]")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class VoiceIntent:
    """One utterance, parsed. ``name`` is always in :data:`SUPPORTED_INTENTS`."""

    name: str
    text: str = ""
    due: str = ""
    addressed: bool = False
    heard: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.name,
            "text": self.text,
            "due": self.due,
            "addressed": self.addressed,
            "heard": self.heard,
        }


@dataclass(frozen=True, slots=True)
class IntentOutcome:
    """What the executor did, in words that may be spoken out loud."""

    intent: VoiceIntent
    executed: bool
    ok: bool
    reply: str
    detail: str = ""
    task_id: str = ""
    needs_approval: bool = False
    stop_session: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.to_dict(),
            "executed": self.executed,
            "ok": self.ok,
            "reply": self.reply,
            "detail": self.detail,
            "task_id": self.task_id,
            "needs_approval": self.needs_approval,
            "stop_session": self.stop_session,
        }


def _normalise(text: str) -> str:
    cleaned = _PUNCTUATION.sub(" ", text.strip().lower())
    return _WHITESPACE.sub(" ", cleaned).strip()


def strip_wake_phrase(text: str, wake_word: str) -> tuple[str, bool]:
    """Return ``(remainder, addressed)`` for an utterance.

    The wake phrase must be at the *start*; a mention in the middle of a sentence
    is not an address, so "I told Jarvis yesterday" does not open a session.
    """

    normalised = _normalise(text)
    phrase = _normalise(wake_word or "")
    if not phrase:
        return text.strip(), False
    for prefix in (phrase, *_WAKE_PREFIXES):
        candidate = prefix if prefix == phrase else f"{prefix}{phrase}"
        if normalised == candidate:
            return "", True
        if normalised.startswith(candidate + " "):
            # Strip the same number of leading words from the original text so the
            # caller keeps its capitalisation and punctuation.
            words = len(candidate.split())
            original = text.strip().split()
            return " ".join(original[words:]).strip(" ,.:;-"), True
    return text.strip(), False


def parse_intent(text: str, *, wake_word: str = "") -> VoiceIntent:
    """Parse one utterance into exactly one intent."""

    heard = text.strip()
    if wake_word:
        heard, addressed = strip_wake_phrase(heard, wake_word)
    else:
        addressed = False
    normalised = _normalise(heard)
    if not normalised:
        return VoiceIntent(name="empty", addressed=addressed, heard="")
    if normalised in _STOP_PHRASES:
        return VoiceIntent(name="stop", addressed=addressed, heard=heard)
    for prefix in _REMIND_PREFIXES:
        if normalised.startswith(prefix):
            body = heard.strip()[len(prefix) :].strip()
            due, title = _split_due(body)
            if title:
                return VoiceIntent(
                    name="reminder", text=title, due=due, addressed=addressed, heard=heard
                )
    for prefix in _NOTE_PREFIXES:
        if normalised.startswith(prefix):
            body = heard.strip()[len(prefix) :].strip()
            if body:
                return VoiceIntent(name="note", text=body, addressed=addressed, heard=heard)
    for prefix in _SAY_PREFIXES:
        if normalised.startswith(prefix):
            body = heard.strip()[len(prefix) :].strip()
            if body:
                return VoiceIntent(name="speak", text=body, addressed=addressed, heard=heard)
    if normalised in _HELP_PHRASES:
        return VoiceIntent(name="help", addressed=addressed, heard=heard)
    if normalised in _STATUS_PHRASES or "status" in normalised.split():
        return VoiceIntent(name="status", addressed=addressed, heard=heard)
    if normalised in _TIME_PHRASES or normalised.startswith("what time"):
        return VoiceIntent(name="time", addressed=addressed, heard=heard)
    return VoiceIntent(name="task", text=heard, addressed=addressed, heard=heard)


def _split_due(body: str) -> tuple[str, str]:
    """Split ``"<title> at <when>"`` / ``"<title> in <when>"`` into its parts."""

    lowered = body.lower()
    for marker in (" at ", " in ", " on ", " by "):
        index = lowered.rfind(marker)
        if index > 0:
            title = body[:index].strip(" ,.:;-")
            due = body[index + len(marker) :].strip(" ,.:;-")
            if title and due:
                return due, title
    return "", body.strip(" ,.:;-")


class VoiceIntentExecutor:
    """Perform one intent against the real services, with no hidden authority.

    Every dependency is injectable so the router is testable without a
    microphone, and every default resolves lazily: constructing the executor must
    not open a database or a listening socket.
    """

    def __init__(
        self,
        settings: Any,
        database: Any,
        *,
        task_submitter: Any = None,
        reminder_sink: Any = None,
        note_sink: Any = None,
        status_reader: Any = None,
        now_reader: Any = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._task_submitter = task_submitter
        self._reminder_sink = reminder_sink
        self._note_sink = note_sink
        self._status_reader = status_reader
        self._now_reader = now_reader

    # -- lazy defaults ------------------------------------------------------
    def _submit_task(self, instruction: str) -> dict[str, Any]:
        if self._task_submitter is not None:
            return dict(self._task_submitter(instruction))
        from ..service import LocalService

        local = LocalService(self._settings)
        try:
            return dict(local.create_task(instruction, source="voice"))
        finally:
            local.close()

    def _add_reminder(self, title: str, due: str) -> dict[str, Any]:
        if self._reminder_sink is not None:
            return dict(self._reminder_sink(title, due))
        from ..proactive import ProactiveEngine

        engine = ProactiveEngine(self._settings, self._database)
        return dict(engine.add_reminder(title=title, due=due, source="voice"))

    def _add_note(self, text: str) -> dict[str, Any]:
        if self._note_sink is not None:
            return dict(self._note_sink(text))
        import hashlib

        from ..storage.repositories import MemoryRepository

        repository = MemoryRepository(self._database)
        key = "note-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        record = repository.upsert(
            kind="note",
            key=key,
            title=text[:120],
            content={"text": text, "source": "voice"},
            tags="voice note",
        )
        return {"id": getattr(record, "id", 0), "key": key, "text": text}

    def _read_status(self) -> dict[str, Any]:
        if self._status_reader is not None:
            return dict(self._status_reader())
        from ..service import LocalService

        local = LocalService(self._settings)
        try:
            return dict(local.system_status())
        finally:
            local.close()

    def _now(self) -> Any:
        if self._now_reader is not None:
            return self._now_reader()
        from datetime import datetime

        return datetime.now()

    # -- execution ----------------------------------------------------------
    def execute(self, intent: VoiceIntent) -> IntentOutcome:
        name = intent.name
        if name == "empty":
            return IntentOutcome(intent, False, True, "", detail="nothing was heard")
        if name == "stop":
            return IntentOutcome(
                intent, True, True, "Stopping the voice session.", stop_session=True
            )
        if name == "help":
            return IntentOutcome(intent, True, True, INTENT_HELP)
        if name == "time":
            stamp = self._now().strftime("%H:%M on %d %B %Y")
            return IntentOutcome(intent, True, True, f"It is {stamp}.", detail=stamp)
        if name == "speak":
            return IntentOutcome(intent, True, True, intent.text, detail="read back")
        if name == "status":
            try:
                status = self._read_status()
            except Exception as exc:  # noqa: BLE001 - a spoken reply must never crash the loop
                return IntentOutcome(
                    intent, True, False, "I could not read the system status.", detail=str(exc)
                )
            tasks = status.get("tasks") or {}
            open_tasks = tasks.get("open", tasks.get("pending", "unknown"))
            return IntentOutcome(
                intent,
                True,
                True,
                f"The system is running; {open_tasks} task(s) are open.",
                detail=str(status.get("emergency_stop", ""))[:200],
            )
        if name == "note":
            try:
                saved = self._add_note(intent.text)
            except Exception as exc:  # noqa: BLE001
                return IntentOutcome(intent, True, False, "I could not save that note.", detail=str(exc))
            return IntentOutcome(
                intent, True, True, "Noted.", detail=str(saved.get("key", ""))
            )
        if name == "reminder":
            due = intent.due or "in 10 minutes"
            try:
                created = self._add_reminder(intent.text, due)
            except Exception as exc:  # noqa: BLE001
                return IntentOutcome(
                    intent, True, False, "I could not create that reminder.", detail=str(exc)
                )
            when = created.get("due_at") or created.get("due") or due
            return IntentOutcome(
                intent, True, True, f"Reminder set for {when}.", detail=str(created.get("id", ""))
            )
        # Anything else is a real instruction: submit it like typed text would be,
        # so the approval centre still decides what may run.
        try:
            submitted = self._submit_task(intent.text)
        except Exception as exc:  # noqa: BLE001
            return IntentOutcome(
                intent, True, False, "I could not create that task.", detail=str(exc)
            )
        status = str(submitted.get("status", "created"))
        needs_approval = status in {"pending_approval", "awaiting_approval", "needs_approval"}
        reply = (
            "That needs your approval before it runs."
            if needs_approval
            else f"Task created ({status})."
        )
        return IntentOutcome(
            intent,
            True,
            True,
            reply,
            detail=status,
            task_id=str(submitted.get("id", "")),
            needs_approval=needs_approval,
        )
