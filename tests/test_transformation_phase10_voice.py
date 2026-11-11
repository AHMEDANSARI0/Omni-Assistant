"""Transformation Phase 10: voice pipeline, microphone privacy, honest engines."""

from __future__ import annotations

import json
import wave
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pc_agent import __version__
from pc_agent.config import Settings
from pc_agent.errors import VoiceError
from pc_agent.storage.db import LATEST_SCHEMA_VERSION, Database
from pc_agent.voice import VoiceService
from pc_agent.voice.capture import CaptureResult, PushToTalkRecorder, wav_statistics
from pc_agent.voice.engines import EngineInfo, discover_stt_engines, select_engine
from pc_agent.voice.privacy import (
    MODE_WAKE_WORD,
    STATE_CAPTURING,
    STATE_DISABLED,
)
from pc_agent.voice.stt import SpeechToText, Transcription, clean_transcript
from pc_agent.voice.tts import SpeechOutput, TextToSpeech

# -- helpers ---------------------------------------------------------------


def _write_wav(path: Path, *, seconds: float = 1.0, tone: bool = True) -> Path:
    rate = 16_000
    frames = max(1, int(seconds * rate))
    samples = bytearray()
    for index in range(frames):
        value = (8000 if index % 2 else -8000) if tone else 0
        samples.extend(int(value).to_bytes(2, "little", signed=True))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(samples))
    return path


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    settings = replace(
        Settings.from_env(tmp_path, environ={}, load_dotenv=False),
        **overrides,
    )
    settings.ensure_directories()
    return settings


class FakeSTT:
    """Stand-in that records exactly what it was asked to do."""

    def __init__(self, text: str = "merge the folder a pdfs", *, available: bool = True) -> None:
        self.text = text
        self.available = available
        self.calls: list[dict[str, Any]] = []

    def engines(self) -> list[EngineInfo]:
        return [
            EngineInfo(
                id="fake-stt",
                kind="stt",
                label="fake local engine",
                local=True,
                available=self.available,
                reason="" if self.available else "no engine installed",
            )
        ]

    def selected(self) -> tuple[EngineInfo | None, str]:
        engine = self.engines()[0]
        return (engine, "") if engine.available else (None, engine.reason)

    def transcribe(self, audio_path: Path, *, cloud_consent: bool = False, language: str | None = None) -> Transcription:
        self.calls.append({"path": Path(audio_path), "cloud_consent": cloud_consent})
        return Transcription(text=self.text, engine="fake-stt", local=True, characters=len(self.text))


class FakeTTS:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    def engines(self) -> list[EngineInfo]:
        return [
            EngineInfo(
                id="fake-tts",
                kind="tts",
                label="fake local synthesizer",
                local=True,
                available=True,
            )
        ]

    def selected(self) -> tuple[EngineInfo | None, str]:
        return self.engines()[0], ""

    def speak(self, text: str, *, output_path: Path | None = None) -> SpeechOutput:
        self.spoken.append(text)
        return SpeechOutput(
            engine="fake-tts", characters=len(text), played=True, output_path=output_path
        )


def _service(
    tmp_path: Path,
    *,
    settings: Settings | None = None,
    stt: Any = None,
    tts: Any = None,
    tone: bool = True,
) -> tuple[VoiceService, Database, FakeSTT, FakeTTS]:
    active = settings or _settings(tmp_path)
    database = Database(active.database_path)
    database.initialize()
    fake_stt = stt or FakeSTT()
    fake_tts = tts or FakeTTS()

    def capture(path: Path, **kwargs: Any) -> CaptureResult:
        seconds = 0.5
        _write_wav(path, seconds=seconds, tone=tone)
        frames, rate, rms, peak = wav_statistics(path)
        return CaptureResult(
            path=path,
            duration_ms=int(seconds * 1000),
            sample_rate=rate,
            frames=frames,
            backend="fake",
            rms=rms,
            peak=peak,
            silent=rms < 120.0,
        )

    recorder = PushToTalkRecorder(
        work_dir=active.data_dir / "voice" / "captures",
        backend="fake",
        max_seconds=5.0,
        capture_fn=capture,
    )
    service = VoiceService(
        active, database, recorder=recorder, stt=fake_stt, tts=fake_tts
    )
    return service, database, fake_stt, fake_tts


# -- tests -----------------------------------------------------------------


def test_phase10_identity() -> None:
    assert __version__ == "0.82.17"
    assert LATEST_SCHEMA_VERSION == 18


def test_microphone_is_off_until_enabled_and_capture_is_refused(tmp_path: Path) -> None:
    service, _db, fake_stt, _tts = _service(tmp_path)
    status = service.status()
    assert status.microphone["enabled"] is False
    assert status.microphone["state"] == STATE_DISABLED
    assert status.microphone["indicator"].startswith("✕")
    with pytest.raises(VoiceError, match="microphone is off"):
        service.start_capture()
    assert fake_stt.calls == []
    assert service.sessions() == []


def test_enable_persists_across_service_instances(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    service, database, fake_stt, fake_tts = _service(tmp_path, settings=settings)
    service.enable()
    assert service.status().microphone["enabled"] is True

    def capture(path: Path, **kwargs: Any) -> CaptureResult:
        _write_wav(path, seconds=0.2)
        frames, rate, rms, peak = wav_statistics(path)
        return CaptureResult(
            path=path, duration_ms=200, sample_rate=rate, frames=frames,
            backend="fake", rms=rms, peak=peak, silent=False,
        )

    second = VoiceService(
        settings,
        database,
        recorder=PushToTalkRecorder(
            work_dir=settings.data_dir / "voice" / "captures",
            backend="fake",
            capture_fn=capture,
        ),
        stt=fake_stt,
        tts=fake_tts,
    )
    assert second.status().microphone["enabled"] is True
    assert second.status().microphone["state"] != STATE_DISABLED


def test_wake_word_needs_microphone_and_second_opt_in(tmp_path: Path) -> None:
    service, _db, _stt, _tts = _service(tmp_path)
    with pytest.raises(VoiceError, match="Enable the microphone"):
        service.set_wake_word(True)
    service.enable()
    payload = service.set_wake_word(True)
    assert payload["wake_word_enabled"] is True
    assert payload["wake_word"] == "jarvis"

    with pytest.raises(VoiceError, match="No capture is running"):
        service.stop_capture()

    # A wake-word capture is allowed only once opted in.
    service.start_capture(MODE_WAKE_WORD)
    assert service.status().microphone["state"] == STATE_CAPTURING
    service.cancel_capture()


def test_push_to_talk_transcribes_and_discards_audio_by_default(tmp_path: Path) -> None:
    service, _db, fake_stt, _tts = _service(tmp_path)
    service.enable()
    service.start_capture()
    result = service.stop_capture()
    assert result["text"] == "merge the folder a pdfs"
    assert result["engine"] == "fake-stt"
    assert result["audio_retained"] is False
    assert result["audio_path"] is None
    # The WAV was deleted, and it was a real file before deletion.
    assert fake_stt.calls and fake_stt.calls[0]["path"].exists() is False
    capture_dir = tmp_path / "data" / "voice" / "captures"
    assert not list(capture_dir.glob("*.wav")) if capture_dir.exists() else True


def test_retention_keeps_audio_and_purge_removes_it(tmp_path: Path) -> None:
    settings = _settings(tmp_path, voice_retain_audio=True)
    service, _db, _stt, _tts = _service(tmp_path, settings=settings)
    service.enable()
    service.start_capture()
    result = service.stop_capture()
    assert result["audio_retained"] is True
    retained = Path(result["audio_path"])
    assert retained.is_file()
    assert service.status().sessions["retained"] == 1

    purged = service.purge_audio()
    assert purged["removed"] >= 1
    assert not retained.exists()
    assert service.status().sessions["retained"] == 0


def test_silent_capture_reports_no_speech_and_still_records_session(tmp_path: Path) -> None:
    service, _db, fake_stt, _tts = _service(tmp_path, tone=False)
    service.enable()
    service.start_capture()
    result = service.stop_capture()
    assert result["text"] == ""
    assert "silent" in result["error"].lower()
    assert fake_stt.calls == []
    sessions = service.sessions()
    assert len(sessions) == 1
    assert sessions[0]["status"] == "empty"


def test_transcripts_stored_only_when_enabled(tmp_path: Path) -> None:
    service, _db, _stt, _tts = _service(tmp_path)
    service.enable()
    service.start_capture()
    first = service.stop_capture()
    assert first["text"]
    default_row = service.sessions()[0]
    assert default_row["transcript"] is None
    assert default_row["transcript_chars"] > 0

    stored_settings = _settings(tmp_path / "stored", voice_store_transcripts=True)
    stored, _db2, _s, _t = _service(tmp_path / "stored", settings=stored_settings)
    stored.enable()
    stored.start_capture()
    stored.stop_capture()
    assert stored.sessions()[0]["transcript"] == "merge the folder a pdfs"


def test_cloud_stt_requires_per_call_consent(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        voice_stt_engine="openai-compatible",
        voice_cloud_stt=False,
        openai_api_key="test-key-not-real",
    )
    engine = SpeechToText(settings)
    audio = _write_wav(tmp_path / "clip.wav")
    _engine, reason = engine.selected()
    assert "not available" in reason  # cloud engines stay unavailable without the flag
    with pytest.raises(VoiceError, match="No speech-to-text engine is available"):
        engine.transcribe(audio, cloud_consent=True)

    enabled = replace(settings, voice_cloud_stt=True)
    cloud = SpeechToText(enabled)
    selected, _ = cloud.selected()
    assert selected is not None and selected.local is False
    with pytest.raises(VoiceError, match="explicit consent"):
        cloud.transcribe(audio, cloud_consent=False)


def test_speech_never_places_text_in_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, voice_tts_engine="windows-sapi")
    # Pretend a PowerShell host exists so the SAPI path runs on any OS; the
    # runner below is what actually executes, never a real shell.
    monkeypatch.setattr(
        "pc_agent.voice.tts.shutil.which", lambda name: "/usr/bin/powershell"
    )
    monkeypatch.setattr(
        "pc_agent.voice.engines._powershell", lambda: "/usr/bin/powershell"
    )

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    calls: list[dict[str, Any]] = []

    def runner(argv: list[str], *, environment_extra: dict[str, str], timeout: float, stdin_text: str | None = None) -> Result:
        calls.append({"argv": argv, "env": environment_extra, "stdin": stdin_text})
        return Result()

    engine = TextToSpeech(settings, engine="windows-sapi", runner=runner)
    transcript = "open the report; rm -rf / ; $(calc) `whoami`"
    output = engine.speak(transcript)
    assert output.ok
    assert calls, "the synthesizer was never invoked"
    argv = calls[0]["argv"]
    assert all(transcript not in part for part in argv)
    text_file = Path(calls[0]["env"]["PC_AGENT_TTS_TEXT_FILE"])
    # The temporary text file is deleted as soon as synthesis finishes.
    assert not text_file.exists()


def test_tts_unavailable_reports_the_reason(tmp_path: Path) -> None:
    settings = _settings(tmp_path, voice_tts_engine="espeak-ng")
    engine = TextToSpeech(settings, engine="espeak-ng")
    selected, reason = engine.selected()
    if selected is None:
        with pytest.raises(VoiceError, match="No text-to-speech engine"):
            engine.speak("hello")
        assert "espeak" in reason.lower()


def test_emergency_stop_blocks_voice(tmp_path: Path) -> None:
    service, _db, _stt, _tts = _service(tmp_path)
    service.enable()
    from pc_agent.safety import EmergencyStop

    EmergencyStop(service._settings.data_dir).engage(
        reason="test", engaged_by="pytest"
    )
    with pytest.raises(VoiceError, match="Emergency stop"):
        service.start_capture()
    service._privacy.emergency_stop()
    assert service.status().microphone["state"] == STATE_DISABLED


def test_only_one_capture_at_a_time(tmp_path: Path) -> None:
    service, _db, _stt, _tts = _service(tmp_path)
    service.enable()
    service.start_capture()
    with pytest.raises(VoiceError, match="already"):
        service.start_capture()
    service.cancel_capture()
    service.start_capture()
    service.cancel_capture()


def test_clean_transcript_normalises_without_inventing_words() -> None:
    assert clean_transcript("  hello\r\n\r\n  world \t") == "hello world"
    assert clean_transcript("a\x00b") == "a b"
    assert len(clean_transcript("x" * 9000)) == 4_000


def test_select_engine_prefers_local_and_explains_unavailable_preferences() -> None:
    engines = [
        EngineInfo("cloud", "stt", "cloud", local=False, available=True),
        EngineInfo("local", "stt", "local", local=True, available=True),
    ]
    chosen, reason = select_engine(engines, "auto")
    assert chosen is not None and chosen.id == "local" and reason == ""

    offline = [
        EngineInfo("local", "stt", "local", local=True, available=False, reason="not installed"),
        EngineInfo("cloud", "stt", "cloud", local=False, available=True),
    ]
    chosen, _ = select_engine(offline, "auto")
    assert chosen is not None and chosen.id == "cloud"
    chosen, reason = select_engine(offline, "local")
    assert chosen is None and "not installed" in reason
    assert select_engine(offline, "nonsense")[0] is None


def test_stt_engine_discovery_is_honest_about_this_host() -> None:
    engines = discover_stt_engines(whisper_model="")
    ids = {engine.id for engine in engines}
    assert {"windows-sapi", "faster-whisper", "whisper-cpp", "openai-compatible"} == ids
    for engine in engines:
        if not engine.available:
            assert engine.reason, f"{engine.id} must say why it is unavailable"


def test_voice_cli_status_and_privacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from pc_agent.cli import main

    monkeypatch.setenv("PC_AGENT_HOME", str(tmp_path))
    assert main(["voice", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["microphone"]["enabled"] is False
    assert payload["microphone"]["retention"] == "discard"

    assert main(["voice", "privacy"]) == 0
    printed = capsys.readouterr().out
    assert "retention policy" in printed
    assert "wake word" in printed


def test_voice_api_endpoints(tmp_path: Path) -> None:
    from pc_agent.api_server import LocalApiServer
    from pc_agent.config import Settings as RealSettings

    settings = replace(
        RealSettings.from_env(tmp_path, environ={}, load_dotenv=False),
        api_port=0,
        scheduler_interval_seconds=0,
    )
    settings.ensure_directories()
    server = LocalApiServer(settings)
    server.start()
    try:
        import urllib.request

        host, port = server.address
        token = server.token

        def call(path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
            data = json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(
                f"http://{host}:{port}{path}", data=data, method=method
            )
            request.add_header("Authorization", f"Bearer {token}")
            if data is not None:
                request.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode())
            assert payload.get("ok") is True
            return payload["data"]

        status = call("/v1/voice/status")
        assert status["microphone"]["enabled"] is False
        enabled = call("/v1/voice/enable", method="POST", body={"enabled": True})
        assert enabled["enabled"] is True
        wake = call("/v1/voice/wake-word", method="POST", body={"enabled": True})
        assert wake["wake_word_enabled"] is True
        sessions = call("/v1/voice/sessions")
        assert sessions["sessions"] == []
        with pytest.raises(Exception):  # noqa: B017 - stop without a capture is a 4xx
            call("/v1/voice/capture/stop", method="POST", body={})
    finally:
        server.stop()


def test_diagnostics_and_release_include_voice(tmp_path: Path) -> None:
    from pc_agent.diagnostics import build_diagnostics
    from pc_agent.release import audit_install

    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    diagnostics = build_diagnostics(settings, database)
    assert "voice" in diagnostics
    assert diagnostics["voice"]["microphone"]["enabled"] is False
    assert "mic=" in diagnostics["voice"]["summary"]

    audit = audit_install(settings)
    names = [check.name for check in audit.checks]
    assert "voice_privacy" in names
    voice_check = next(check for check in audit.checks if check.name == "voice_privacy")
    assert voice_check.status == "pass"
    assert "microphone default=off" in voice_check.detail


def test_recorder_reports_missing_backend_instead_of_pretending(tmp_path: Path) -> None:
    recorder = PushToTalkRecorder(work_dir=tmp_path / "captures", backend="none")
    backend, reason = recorder.resolve_backend()
    assert backend == "none"
    assert "disabled" in reason.lower() or "available" in reason.lower()
    with pytest.raises(VoiceError, match="Microphone unavailable"):
        recorder.start()
