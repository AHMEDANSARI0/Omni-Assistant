"""Hands-free voice session loop (voice completion, 0.81.0).

This is what turns the pieces (privacy gate, recorder, STT, intent router, TTS)
into an assistant you can actually talk to. It is deliberately small, bounded and
stoppable:

* **Off by default.** A cycle only runs when the microphone has been enabled by
  the user *and*, in wake-word mode, when the second opt-in is on as well. Both
  checks use the persisted privacy gate, not a local flag.
* **Bounded.** ``max_cycles`` and ``max_seconds`` cap a session, one capture runs
  at a time, and each cycle is limited by ``voice_max_seconds``.
* **Stoppable.** ``stop()`` aborts between phases, so a hands-free session can be
  ended with a word, a UI button or Ctrl+C and it stops before the next capture.
* **Visible.** ``status()`` always reports the real state and the last heard
  utterance, so a UI indicator cannot lie about listening.
* **No hidden channel.** The loop never uploads audio: capture goes through the
  same privacy gate as push-to-talk, cloud STT still needs per-call consent, and
  the intent executor submits ordinary tasks that obey the approval centre.

Known limitation, stated rather than hidden: wake-phrase detection here is
*based on the transcript* - the first capture is transcribed and its opening
words are matched. It is not a low-power always-on acoustic detector, so a
session still costs one bounded capture per cycle.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .intents import IntentOutcome, VoiceIntent, VoiceIntentExecutor, parse_intent

__all__ = [
    "STATE_BLOCKED",
    "STATE_CAPTURING",
    "STATE_IDLE",
    "STATE_LISTENING",
    "STATE_SPEAKING",
    "STATE_STOPPED",
    "STATE_THINKING",
    "VoiceLoop",
    "VoiceLoopReport",
]

STATE_IDLE = "idle"
STATE_LISTENING = "listening"
STATE_CAPTURING = "capturing"
STATE_THINKING = "thinking"
STATE_SPEAKING = "speaking"
STATE_STOPPED = "stopped"
STATE_BLOCKED = "blocked"


@dataclass
class VoiceLoopReport:
    """Everything that happened in one session, as data."""

    state: str
    stop_reason: str
    wake_word: str
    require_wake_phrase: bool
    cycles: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0
    blocked_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.state != STATE_BLOCKED and not self.blocked_reason

    @property
    def handled(self) -> int:
        return sum(1 for cycle in self.cycles if cycle.get("executed"))

    def summary_line(self) -> str:
        if self.blocked_reason:
            return f"voice loop blocked: {self.blocked_reason}"
        return (
            f"voice loop {self.state}; cycles={len(self.cycles)}; "
            f"handled={self.handled}; stop={self.stop_reason}; "
            f"{self.duration_ms} ms"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "stop_reason": self.stop_reason,
            "wake_word": self.wake_word,
            "require_wake_phrase": self.require_wake_phrase,
            "cycles": list(self.cycles),
            "duration_ms": self.duration_ms,
            "blocked_reason": self.blocked_reason,
            "handled": self.handled,
            "ok": self.ok,
        }


class VoiceLoop:
    """Runs bounded listen -> transcribe -> act -> speak cycles."""

    def __init__(
        self,
        voice: Any,
        executor: VoiceIntentExecutor,
        *,
        wake_word: str = "jarvis",
        speak_replies: bool = True,
        sleep: Any = None,
    ) -> None:
        self._voice = voice
        self._executor = executor
        self._wake_word = wake_word
        self._speak_replies = speak_replies
        self._sleep = sleep or time.sleep
        self._stop = threading.Event()
        self._state = STATE_IDLE
        self._last_heard = ""
        self._last_intent: VoiceIntent | None = None
        self._lock = threading.Lock()

    # -- state --------------------------------------------------------------
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _set_state(self, state: str) -> None:
        with self._lock:
            self._state = state

    def status(self) -> dict[str, Any]:
        intent = self._last_intent
        return {
            "state": self.state,
            "wake_word": self._wake_word,
            "last_heard": self._last_heard,
            "last_intent": intent.to_dict() if intent is not None else None,
            "speak_replies": self._speak_replies,
            "stopping": self._stop.is_set(),
        }

    def stop(self) -> None:
        """Abort before the next phase; safe to call from another thread."""

        self._stop.set()
        self._set_state(STATE_STOPPED)

    # -- session ------------------------------------------------------------
    def run(
        self,
        *,
        max_cycles: int = 5,
        cycle_seconds: float = 4.0,
        max_seconds: float = 300.0,
        require_wake_phrase: bool = True,
    ) -> VoiceLoopReport:
        report = VoiceLoopReport(
            state=STATE_IDLE,
            stop_reason="",
            wake_word=self._wake_word,
            require_wake_phrase=require_wake_phrase,
        )
        started = time.perf_counter()
        blocked = self._blocked_reason(require_wake_phrase)
        if blocked:
            report.state = STATE_BLOCKED
            report.blocked_reason = blocked
            self._set_state(STATE_BLOCKED)
            report.duration_ms = int((time.perf_counter() - started) * 1000)
            return report

        stop_reason = "max_cycles"
        for index in range(max(0, int(max_cycles))):
            if self._stop.is_set():
                stop_reason = "stopped"
                break
            if time.perf_counter() - started > max_seconds:
                stop_reason = "max_seconds"
                break
            cycle = self._cycle(index, cycle_seconds, require_wake_phrase)
            report.cycles.append(cycle)
            if cycle.get("stop_session"):
                stop_reason = "user said stop"
                break
            if cycle.get("blocked"):
                stop_reason = "emergency stop engaged"
                report.blocked_reason = cycle.get("blocked", "")
                break
        else:
            stop_reason = "max_cycles"

        report.stop_reason = stop_reason
        report.state = STATE_STOPPED if self._stop.is_set() else STATE_IDLE
        self._set_state(report.state)
        report.duration_ms = int((time.perf_counter() - started) * 1000)
        return report

    def _blocked_reason(self, require_wake_phrase: bool) -> str:
        status = self._voice.status()
        microphone = status.microphone
        if status.emergency_stop:
            return "the emergency stop is engaged"
        if not microphone.get("enabled"):
            return "the microphone is off; run 'pc-agent voice enable' first"
        if require_wake_phrase and not microphone.get("wake_word_enabled"):
            return (
                "wake-word mode needs its own opt-in; run "
                "'pc-agent voice wake-word on' or start a push-to-talk session"
            )
        if microphone.get("backend") == "none":
            return microphone.get("backend_reason") or "no capture backend is available"
        return ""

    def _cycle(self, index: int, seconds: float, require_wake_phrase: bool) -> dict[str, Any]:
        record: dict[str, Any] = {"index": index, "state": STATE_LISTENING, "executed": False}
        try:
            self._set_state(STATE_CAPTURING)
            record["state"] = STATE_CAPTURING
            self._voice.start_capture()
        except Exception as exc:  # noqa: BLE001 - a session must survive one bad cycle
            record["error"] = f"{exc.__class__.__name__}: {exc}"
            record["state"] = STATE_IDLE
            self._set_state(STATE_IDLE)
            return record
        try:
            # Interruptible wait: stop() or an emergency stop ends the capture early.
            waited = 0.0
            step = 0.05
            while waited < seconds and not self._stop.is_set():
                self._sleep(step)
                waited += step
            transcript = self._voice.stop_capture()
        except Exception as exc:  # noqa: BLE001
            record["error"] = f"{exc.__class__.__name__}: {exc}"
            record["state"] = STATE_IDLE
            self._set_state(STATE_IDLE)
            return record
        self._set_state(STATE_THINKING)
        record["state"] = STATE_THINKING
        heard = str(transcript.get("text") or "").strip()
        record["heard"] = heard
        record["audio_seconds"] = transcript.get("audio_seconds", 0)
        record["transcribe_error"] = transcript.get("error", "")
        self._last_heard = heard
        if not heard:
            record["reason"] = transcript.get("error") or "no speech detected"
            record["state"] = STATE_IDLE
            self._set_state(STATE_IDLE)
            return record

        intent = parse_intent(heard, wake_word=self._wake_word)
        self._last_intent = intent
        record["intent"] = intent.to_dict()
        if require_wake_phrase and not intent.addressed:
            record["reason"] = "the utterance did not start with the wake word"
            record["state"] = STATE_LISTENING
            self._set_state(STATE_LISTENING)
            return record

        outcome: IntentOutcome = self._executor.execute(intent)
        record["executed"] = outcome.executed
        record["ok"] = outcome.ok
        record["reply"] = outcome.reply
        record["detail"] = outcome.detail
        record["task_id"] = outcome.task_id
        record["needs_approval"] = outcome.needs_approval
        record["stop_session"] = outcome.stop_session
        if outcome.reply and self._speak_replies:
            self._set_state(STATE_SPEAKING)
            record["state"] = STATE_SPEAKING
            try:
                spoken = self._voice.speak(outcome.reply)
            except Exception as exc:  # noqa: BLE001
                record["speak_error"] = f"{exc.__class__.__name__}: {exc}"
            else:
                record["spoken"] = bool(spoken.get("ok"))
                record["speak_engine"] = spoken.get("engine", "")
        record["state"] = STATE_IDLE
        self._set_state(STATE_IDLE)
        return record
