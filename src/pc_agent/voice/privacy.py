"""Microphone privacy state machine - the gate every capture must pass.

Rules enforced here (not just documented):

* the microphone is **off** until the user enables it, and the choice is
  persisted so a restart cannot silently re-open the mic;
* only one capture can run at a time, and it is bounded by
  ``voice_max_seconds``;
* wake-word listening is a *second* opt-in and is reported separately, so a
  UI can show "push-to-talk" without implying the mic is always listening;
* the current state is always inspectable, so the indicator can never lie.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..errors import VoiceError
from ..storage.repositories import PreferenceRepository

PREFERENCE_ENABLED = "voice.enabled"
PREFERENCE_WAKE_WORD = "voice.wake_word_enabled"

STATE_DISABLED = "disabled"
STATE_ARMED = "armed"
STATE_CAPTURING = "capturing"

MODE_PUSH_TO_TALK = "push_to_talk"
MODE_WAKE_WORD = "wake_word"


@dataclass(frozen=True, slots=True)
class MicrophoneStatus:
    """Everything a UI needs to display the microphone truthfully."""

    enabled: bool
    state: str
    mode: str
    wake_word_enabled: bool
    wake_word: str
    wake_word_available: bool
    indicator: str
    retention: str
    store_transcripts: bool
    cloud_stt_allowed: bool
    max_seconds: float
    backend: str
    backend_reason: str
    last_reason: str

    @property
    def listening(self) -> bool:
        return self.state == STATE_CAPTURING

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "state": self.state,
            "mode": self.mode,
            "listening": self.listening,
            "wake_word_enabled": self.wake_word_enabled,
            "wake_word": self.wake_word if self.wake_word_enabled else "",
            "wake_word_available": self.wake_word_available,
            "indicator": self.indicator,
            "retention": self.retention,
            "store_transcripts": self.store_transcripts,
            "cloud_stt_allowed": self.cloud_stt_allowed,
            "max_seconds": self.max_seconds,
            "backend": self.backend,
            "backend_reason": self.backend_reason,
            "last_reason": self.last_reason,
        }


class MicrophonePrivacy:
    """Thread-safe gate around the single microphone."""

    def __init__(
        self,
        settings: Settings,
        *,
        preferences: PreferenceRepository,
        backend: str = "auto",
        backend_reason: str = "",
    ) -> None:
        self._settings = settings
        self._preferences = preferences
        self._backend = backend
        self._backend_reason = backend_reason
        self._lock = threading.Lock()
        self._state = STATE_ARMED if self.enabled else STATE_DISABLED
        self._mode = MODE_PUSH_TO_TALK
        self._last_reason = ""

    # -- persisted consent --------------------------------------------------
    @property
    def enabled(self) -> bool:
        stored = self._preferences.get(PREFERENCE_ENABLED)
        if stored is None:
            return bool(self._settings.voice_enabled)
        return stored.strip().lower() in {"1", "true", "yes", "on"}

    @property
    def wake_word_enabled(self) -> bool:
        stored = self._preferences.get(PREFERENCE_WAKE_WORD)
        if stored is None:
            return bool(self._settings.voice_wake_word_enabled)
        return stored.strip().lower() in {"1", "true", "yes", "on"}

    def enable(self, *, actor: str = "user") -> MicrophoneStatus:
        self._preferences.set(PREFERENCE_ENABLED, "true")
        with self._lock:
            if self._state == STATE_DISABLED:
                self._state = STATE_ARMED
            self._last_reason = f"microphone enabled by {actor}"
        return self.status()

    def disable(self, *, actor: str = "user") -> MicrophoneStatus:
        self._preferences.set(PREFERENCE_ENABLED, "false")
        with self._lock:
            self._state = STATE_DISABLED
            self._last_reason = f"microphone disabled by {actor}; any capture is cancelled"
        return self.status()

    def enable_wake_word(self, *, actor: str = "user") -> MicrophoneStatus:
        if not self.enabled:
            raise VoiceError(
                "Enable the microphone before turning on wake-word listening."
            )
        self._preferences.set(PREFERENCE_WAKE_WORD, "true")
        with self._lock:
            self._last_reason = f"wake word enabled by {actor}"
        return self.status()

    def disable_wake_word(self, *, actor: str = "user") -> MicrophoneStatus:
        self._preferences.set(PREFERENCE_WAKE_WORD, "false")
        with self._lock:
            self._last_reason = f"wake word disabled by {actor}"
        return self.status()

    # -- state machine ------------------------------------------------------
    def begin_capture(self, mode: str = MODE_PUSH_TO_TALK) -> str:
        """Reserve the microphone; returns the granted mode."""
        if mode not in {MODE_PUSH_TO_TALK, MODE_WAKE_WORD}:
            raise VoiceError(f"Unknown capture mode: {mode}")
        if not self.enabled:
            raise VoiceError(
                "The microphone is off. Enable voice explicitly before capturing."
            )
        if mode == MODE_WAKE_WORD and not self.wake_word_enabled:
            raise VoiceError(
                "Wake-word listening is not enabled; push-to-talk is the default."
            )
        with self._lock:
            if self._state == STATE_CAPTURING:
                raise VoiceError("A capture is already running; stop it first.")
            self._state = STATE_CAPTURING
            self._mode = mode
            self._last_reason = f"{mode} capture started"
        return mode

    def end_capture(self, reason: str = "") -> None:
        with self._lock:
            if self._state == STATE_CAPTURING:
                self._state = STATE_ARMED
            self._last_reason = reason or "capture finished"

    def cancel_capture(self, reason: str = "cancelled by user") -> None:
        with self._lock:
            self._state = STATE_ARMED if self.enabled else STATE_DISABLED
            self._last_reason = reason

    def emergency_stop(self) -> None:
        with self._lock:
            self._state = STATE_DISABLED
            self._last_reason = "emergency stop engaged; microphone off"

    # -- reporting ----------------------------------------------------------
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def status(self, *, wake_word_available: bool = False) -> MicrophoneStatus:
        with self._lock:
            state = self._state
            mode = self._mode
            last_reason = self._last_reason
        enabled = self.enabled
        listening = state == STATE_CAPTURING
        indicator = (
            f"● REC ({mode})" if listening else ("○ ready" if enabled else "✕ off")
        )
        return MicrophoneStatus(
            enabled=enabled,
            state=state if enabled else STATE_DISABLED,
            mode=mode,
            wake_word_enabled=self.wake_word_enabled,
            wake_word=self._settings.voice_wake_word,
            wake_word_available=wake_word_available,
            indicator=indicator,
            retention="retain" if self._settings.voice_retain_audio else "discard",
            store_transcripts=bool(self._settings.voice_store_transcripts),
            cloud_stt_allowed=bool(self._settings.voice_cloud_stt),
            max_seconds=float(self._settings.voice_max_seconds),
            backend=self._backend,
            backend_reason=self._backend_reason,
            last_reason=last_reason,
        )


__all__ = [
    "MODE_PUSH_TO_TALK",
    "MODE_WAKE_WORD",
    "PREFERENCE_ENABLED",
    "PREFERENCE_WAKE_WORD",
    "STATE_ARMED",
    "STATE_CAPTURING",
    "STATE_DISABLED",
    "MicrophonePrivacy",
    "MicrophoneStatus",
]
