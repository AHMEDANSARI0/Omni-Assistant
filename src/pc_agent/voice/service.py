"""VoiceService: the single entry point for capture, speech and privacy.

It composes the pieces - privacy gate, recorder, STT, TTS, session ledger -
and is what the CLI, HTTP API, GUI and web UI all call, so every surface
shares exactly the same rules:

* a capture that the privacy gate refused never opens the microphone;
* a session row is written even when transcription failed, because "a
  capture happened" is itself an auditable fact;
* audio is deleted right after transcription unless retention is enabled;
* an engaged emergency stop refuses new captures and cancels a running one.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from ..errors import VoiceError
from ..logging_config import redact_text
from ..safety import EmergencyStop
from ..storage.db import Database
from ..storage.repositories import (
    EventRepository,
    PreferenceRepository,
    VoiceRepository,
    VoiceSessionRecord,
)
from .capture import CaptureResult, PushToTalkRecorder
from .devices import list_input_devices, list_output_devices
from .privacy import MODE_PUSH_TO_TALK, MicrophonePrivacy
from .stt import SpeechToText, Transcription
from .tts import SpeechOutput, TextToSpeech

_LOG = logging.getLogger("pc_agent.voice.service")


@dataclass(frozen=True, slots=True)
class VoiceStatus:
    """Live, honest snapshot of the voice subsystem."""

    microphone: dict[str, Any]
    stt: dict[str, Any]
    tts: dict[str, Any]
    input_devices: dict[str, Any]
    output_devices: dict[str, Any]
    sessions: dict[str, Any]
    emergency_stop: bool
    work_dir: str

    @property
    def available(self) -> bool:
        return bool(self.microphone.get("enabled")) and bool(self.stt.get("available"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "microphone": self.microphone,
            "stt": self.stt,
            "tts": self.tts,
            "input_devices": self.input_devices,
            "output_devices": self.output_devices,
            "sessions": self.sessions,
            "emergency_stop": self.emergency_stop,
            "work_dir": self.work_dir,
        }

    def summary_line(self) -> str:
        mic = self.microphone
        stt = self.stt
        tts = self.tts
        return (
            f"mic={mic.get('indicator')} ({mic.get('backend')}); "
            f"stt={stt.get('engine') or 'unavailable'}; "
            f"tts={tts.get('engine') or 'unavailable'}; "
            f"retention={mic.get('retention')}; "
            f"wake_word={'on' if mic.get('wake_word_enabled') else 'off'}; "
            f"sessions={self.sessions.get('total', 0)}"
        )


class VoiceService:
    """Capture -> transcribe -> (optional) act, plus speak and privacy."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        recorder: PushToTalkRecorder | None = None,
        stt: SpeechToText | None = None,
        tts: TextToSpeech | None = None,
        events: EventRepository | None = None,
        emergency_stop: EmergencyStop | None = None,
    ) -> None:
        self._settings = settings
        self._db = database
        self._work_dir = settings.data_dir / "voice"
        self._captures_dir = self._work_dir / "captures"
        self._retained_dir = self._work_dir / "retained"
        self._repository = VoiceRepository(database)
        self._preferences = PreferenceRepository(database)
        self._events = events or EventRepository(database)
        self._stop = emergency_stop or EmergencyStop(settings.data_dir)
        self._recorder = recorder or PushToTalkRecorder(
            work_dir=self._captures_dir,
            sample_rate=settings.voice_sample_rate,
            max_seconds=settings.voice_max_seconds,
        )
        backend, reason = self._recorder.resolve_backend()
        self._privacy = MicrophonePrivacy(
            settings, preferences=self._preferences, backend=backend, backend_reason=reason
        )
        self._stt = stt or SpeechToText(settings)
        self._tts = tts or TextToSpeech(settings, work_dir=self._work_dir)
        self._started_at: float | None = None

    @property
    def database(self) -> Database:
        """The database this service uses, so callers need no private attribute."""

        return self._db

    # -- introspection ------------------------------------------------------
    def status(self) -> VoiceStatus:
        selected_stt, stt_reason = self._stt.selected()
        selected_tts, tts_reason = self._tts.selected()
        return VoiceStatus(
            microphone=self._privacy.status().to_dict(),
            stt={
                "engine": selected_stt.id if selected_stt else "",
                "label": selected_stt.label if selected_stt else "",
                "local": bool(selected_stt.local) if selected_stt else True,
                "available": selected_stt is not None,
                "reason": stt_reason,
                "engines": [engine.to_dict() for engine in self._stt.engines()],
            },
            tts={
                "engine": selected_tts.id if selected_tts else "",
                "label": selected_tts.label if selected_tts else "",
                "available": selected_tts is not None,
                "reason": tts_reason,
                "engines": [engine.to_dict() for engine in self._tts.engines()],
            },
            input_devices=list_input_devices().to_dict(),
            output_devices=list_output_devices().to_dict(),
            sessions=self._repository.summary(),
            emergency_stop=self._stop.status().engaged,
            work_dir=str(self._work_dir),
        )

    def privacy(self) -> dict[str, Any]:
        status = self.status()
        payload = status.microphone
        payload["sessions"] = status.sessions
        payload["work_dir"] = status.work_dir
        return payload

    def devices(self) -> dict[str, Any]:
        return {
            "input": list_input_devices().to_dict(),
            "output": list_output_devices().to_dict(),
        }

    def sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        return [self._session_payload(record) for record in self._repository.list_sessions(limit)]

    # -- consent ------------------------------------------------------------
    def enable(self, *, actor: str = "user") -> dict[str, Any]:
        status = self._privacy.enable(actor=actor)
        self._event("voice.microphone_enabled", f"Microphone enabled by {actor}")
        return status.to_dict()

    def disable(self, *, actor: str = "user") -> dict[str, Any]:
        self._cancel_capture()
        status = self._privacy.disable(actor=actor)
        self._event("voice.microphone_disabled", f"Microphone disabled by {actor}")
        return status.to_dict()

    def set_wake_word(self, enabled: bool, *, actor: str = "user") -> dict[str, Any]:
        status = (
            self._privacy.enable_wake_word(actor=actor)
            if enabled
            else self._privacy.disable_wake_word(actor=actor)
        )
        self._event(
            "voice.wake_word",
            f"Wake word {'enabled' if enabled else 'disabled'} by {actor}",
        )
        return status.to_dict()

    # -- capture ------------------------------------------------------------
    def start_capture(self, mode: str = MODE_PUSH_TO_TALK) -> dict[str, Any]:
        blocked = self._emergency_block()
        if blocked:
            raise VoiceError(blocked)
        granted = self._privacy.begin_capture(mode)
        try:
            self._recorder.start()
        except VoiceError:
            self._privacy.end_capture("capture backend refused")
            raise
        self._started_at = time.perf_counter()
        self._event("voice.capture_started", f"Capture started ({granted})")
        return {
            "state": "capturing",
            "mode": granted,
            "backend": self._recorder.backend,
            "max_seconds": self._settings.voice_max_seconds,
        }

    def stop_capture(
        self,
        *,
        cloud_consent: bool = False,
        transcript_limit: int = 4_000,
    ) -> dict[str, Any]:
        if self._started_at is None:
            raise VoiceError("No capture is running.")
        started_at = self._started_at
        result = self._recorder.stop()
        self._started_at = None
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        self._privacy.end_capture("capture finished")
        transcript: Transcription | None = None
        error = ""
        if result.ok and not result.silent:
            try:
                transcript = self._stt.transcribe(result.path, cloud_consent=cloud_consent)  # type: ignore[arg-type]
            except VoiceError as exc:
                error = str(exc)
        elif result.ok and result.silent:
            error = "No speech detected: the recording was silent."
        else:
            error = result.reason or "Capture produced no audio."
        retained = self._finalize_audio(result)
        text = (transcript.text if transcript else "")[:transcript_limit]
        record = self._repository.add_session(
            mode=self._privacy.mode,
            engine_stt=transcript.engine if transcript else "",
            duration_ms=duration_ms,
            audio_seconds=result.seconds,
            audio_retained=retained is not None,
            audio_path=str(retained) if retained is not None else None,
            transcript=(
                redact_text(text) if text and self._settings.voice_store_transcripts else None
            ),
            transcript_chars=len(text),
            status="ok" if transcript and text else "empty",
            error=error,
        )
        self._event(
            "voice.capture_finished",
            f"Capture finished ({record.audio_seconds:.1f}s, "
            f"{record.transcript_chars} chars, engine={record.engine_stt or 'none'})",
        )
        return {
            "state": "armed" if self._privacy.enabled else "disabled",
            "session_id": record.id,
            "text": text,
            "engine": record.engine_stt,
            "local": bool(transcript.local) if transcript else True,
            "audio_seconds": record.audio_seconds,
            "duration_ms": record.duration_ms,
            "audio_retained": record.audio_retained,
            "audio_path": record.audio_path,
            "error": error,
            "capture": result.to_dict(),
        }

    def cancel_capture(self) -> dict[str, Any]:
        self._cancel_capture()
        return {"state": self._privacy.state}

    # -- speech -------------------------------------------------------------
    def speak(self, text: str, *, output_path: Path | None = None) -> dict[str, Any]:
        blocked = self._emergency_block()
        if blocked:
            raise VoiceError(blocked)
        output: SpeechOutput = self._tts.speak(text, output_path=output_path)
        self._repository.add_session(
            mode="speech",
            engine_tts=output.engine,
            duration_ms=0,
            transcript=None,
            transcript_chars=output.characters,
            status="ok" if output.ok else "failed",
            error=output.error,
        )
        self._event(
            "voice.spoken",
            f"Spoke {output.characters} characters via {output.engine}",
        )
        return output.to_dict()

    def transcribe_file(
        self, path: Path, *, cloud_consent: bool = False
    ) -> dict[str, Any]:
        source = Path(path).expanduser()
        if not source.is_absolute():
            source = self._settings.workspace_dir / source
        if not source.is_file():
            raise VoiceError(f"Audio file not found: {source.name}")
        blocked = self._emergency_block()
        if blocked:
            raise VoiceError(blocked)
        transcription = self._stt.transcribe(source, cloud_consent=cloud_consent)
        record = self._repository.add_session(
            mode="file",
            engine_stt=transcription.engine,
            transcript=(
                redact_text(transcription.text)
                if transcription.text and self._settings.voice_store_transcripts
                else None
            ),
            transcript_chars=transcription.characters,
            status="ok" if transcription.text else "empty",
        )
        return {
            "session_id": record.id,
            **transcription.to_dict(),
        }

    # -- retention ----------------------------------------------------------
    def purge_audio(self) -> dict[str, Any]:
        """Delete every retained recording and clear its reference."""
        removed = 0
        for record in self._repository.sessions_with_retained_audio():
            if record.audio_path:
                target = Path(record.audio_path)
                if target.is_file():
                    target.unlink(missing_ok=True)
                    removed += 1
            self._repository.clear_audio_reference(record.id)
        if self._retained_dir.is_dir():
            for leftover in self._retained_dir.glob("*.wav"):
                leftover.unlink(missing_ok=True)
                removed += 1
        self._event("voice.audio_purged", f"Purged {removed} retained recording(s)")
        return {"removed": removed, "retained_now": self._repository.summary()["retained"]}

    def clear_history(self) -> dict[str, Any]:
        """Delete the whole voice ledger (and any retained audio first)."""
        purged = self.purge_audio()
        removed = 0
        for record in self._repository.list_sessions(500):
            if self._repository.delete_session(record.id):
                removed += 1
        self._event("voice.history_cleared", f"Cleared {removed} voice session(s)")
        return {"removed": removed, "audio_removed": purged["removed"]}

    # -- internals ----------------------------------------------------------
    def _emergency_block(self) -> str:
        state = self._stop.status()
        if state.engaged:
            self._privacy.emergency_stop()
            return f"Emergency stop is engaged ({state.reason or state.source}); voice is blocked."
        return ""

    def _cancel_capture(self) -> None:
        self._recorder.cancel()
        self._started_at = None
        self._privacy.cancel_capture()

    def _finalize_audio(self, result: CaptureResult) -> Path | None:
        """Keep or delete the recording according to the retention policy."""
        if result.path is None:
            return None
        source = Path(result.path)
        if not self._settings.voice_retain_audio:
            source.unlink(missing_ok=True)
            return None
        self._retained_dir.mkdir(parents=True, exist_ok=True)
        target = self._retained_dir / f"capture-{int(time.time() * 1000)}.wav"
        try:
            shutil.move(str(source), str(target))
        except OSError:
            _LOG.warning("could not retain recording", exc_info=True)
            source.unlink(missing_ok=True)
            return None
        return target

    def _event(self, event_type: str, summary: str) -> None:
        try:
            self._events.record(event_type, source="voice", summary=summary)
        except Exception:  # noqa: BLE001 - an audit write must not break voice
            _LOG.warning("could not record voice event", exc_info=True)

    @staticmethod
    def _session_payload(record: VoiceSessionRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "mode": record.mode,
            "state": record.state,
            "engine_stt": record.engine_stt,
            "engine_tts": record.engine_tts,
            "started_at": record.started_at.isoformat(),
            "ended_at": record.ended_at.isoformat() if record.ended_at else None,
            "duration_ms": record.duration_ms,
            "audio_seconds": record.audio_seconds,
            "audio_retained": record.audio_retained,
            "audio_path": record.audio_path,
            "transcript": record.transcript,
            "transcript_chars": record.transcript_chars,
            "status": record.status,
            "error": record.error,
        }


__all__ = ["VoiceService", "VoiceStatus"]
