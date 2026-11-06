"""Full voice assistant completion (0.81.2).

Covers the three pieces that were still missing from "full voice assistant":

* a deterministic intent router, so a transcript actually does something;
* a bounded hands-free loop with a visible state and an immediate stop;
* a sealed acceptance report that runs the real gates on the host.

The loop tests use a fake voice object with the same surface as ``VoiceService``
so a session can be exercised deterministically without a microphone. Nothing
here asserts that audio hardware exists - that is exactly what the acceptance
report refuses to fake.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pc_agent import __version__
from pc_agent.config import Settings
from pc_agent.storage.db import Database
from pc_agent.voice.acceptance import (
    REQUIRED_VOICE_GATES,
    run_voice_acceptance,
    verify_voice_acceptance_report,
)
from pc_agent.voice.intents import (
    INTENT_HELP,
    SUPPORTED_INTENTS,
    VoiceIntentExecutor,
    parse_intent,
    strip_wake_phrase,
)
from pc_agent.voice.listener import (
    STATE_BLOCKED,
    STATE_IDLE,
    STATE_STOPPED,
    VoiceLoop,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path: Path) -> Settings:
    root = tmp_path / "install"
    data = tmp_path / "data"
    root.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    settings = Settings.from_env(
        root,
        environ={
            "PC_AGENT_DATA_DIR": str(data),
            "PC_AGENT_WORKSPACE_DIR": str(tmp_path / "workspace"),
            "PC_AGENT_DB_PATH": str(data / "pc_agent.db"),
        },
        load_dotenv=False,
    )
    settings.ensure_directories()
    return settings


# ------------------------------------------------------------------ router ---


@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("status", "status"),
        ("what is the system status", "status"),
        ("what time is it", "time"),
        ("note buy milk", "note"),
        ("remember that the key is in the drawer", "note"),
        ("remind me to call Ali at 18:00", "reminder"),
        ("reminder water the plants in 2 hours", "reminder"),
        ("say good morning", "speak"),
        ("help", "help"),
        ("stop", "stop"),
        ("never mind", "stop"),
        ("", "empty"),
        ("open the downloads folder and sort it", "task"),
    ],
)
def test_utterances_route_to_one_intent(utterance: str, expected: str) -> None:
    intent = parse_intent(utterance)
    assert intent.name == expected
    assert intent.name in SUPPORTED_INTENTS


def test_reminder_splits_the_title_from_the_due_time() -> None:
    intent = parse_intent("remind me to call Ali at 18:00")
    assert intent.name == "reminder"
    assert intent.text == "call Ali"
    assert intent.due == "18:00"

    no_due = parse_intent("remind me to stretch")
    assert no_due.name == "reminder"
    assert no_due.text == "stretch"
    assert no_due.due == ""


def test_wake_phrase_is_stripped_only_at_the_start() -> None:
    remainder, addressed = strip_wake_phrase("jarvis status", "jarvis")
    assert (remainder, addressed) == ("status", True)

    remainder, addressed = strip_wake_phrase("hey jarvis, what time is it", "jarvis")
    assert addressed is True
    assert remainder == "what time is it"

    remainder, addressed = strip_wake_phrase("i asked jarvis yesterday", "jarvis")
    assert addressed is False
    assert remainder == "i asked jarvis yesterday"


def test_help_text_lists_real_capabilities_only() -> None:
    intent = parse_intent("help")
    assert intent.name == "help"
    for word in ("status", "note", "remind", "say", "stop", "task"):
        assert word in INTENT_HELP


# ---------------------------------------------------------------- executor ---


def test_executor_submits_free_text_as_an_ordinary_task(tmp_path: Path) -> None:
    submitted: list[str] = []

    def submitter(instruction: str) -> dict[str, Any]:
        submitted.append(instruction)
        return {"id": "task-1", "status": "pending_approval"}

    executor = VoiceIntentExecutor(
        _settings(tmp_path), DATABASE_PLACEHOLDER, task_submitter=submitter
    )
    outcome = executor.execute(parse_intent("open the downloads folder"))
    assert submitted == ["open the downloads folder"]
    assert outcome.task_id == "task-1"
    assert outcome.needs_approval is True  # voice never bypasses approvals
    assert "approval" in outcome.reply.lower()


def test_executor_writes_a_note_through_the_injected_sink(tmp_path: Path) -> None:
    saved: list[str] = []
    executor = VoiceIntentExecutor(
        _settings(tmp_path), DATABASE_PLACEHOLDER, note_sink=lambda text: saved.append(text) or {"key": "k"}
    )
    outcome = executor.execute(parse_intent("note buy milk"))
    assert saved == ["buy milk"]
    assert outcome.ok is True
    assert outcome.reply == "Noted."


def test_executor_reports_failures_without_raising(tmp_path: Path) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("provider offline")

    executor = VoiceIntentExecutor(
        _settings(tmp_path), DATABASE_PLACEHOLDER, task_submitter=boom
    )
    outcome = executor.execute(parse_intent("summarise my downloads"))
    assert outcome.ok is False
    assert "could not create that task" in outcome.reply
    assert "provider offline" in outcome.detail


def test_stop_intent_ends_the_session_and_speak_reads_back(tmp_path: Path) -> None:
    executor = VoiceIntentExecutor(_settings(tmp_path), DATABASE_PLACEHOLDER)
    stop = executor.execute(parse_intent("stop"))
    assert stop.stop_session is True
    spoken = executor.execute(parse_intent("say the meeting is at noon"))
    assert spoken.reply == "the meeting is at noon"


DATABASE_PLACEHOLDER = None  # injected sinks are used; no database is opened


# -------------------------------------------------------------------- loop ---


class FakeVoice:
    """The smallest object with the same surface as ``VoiceService``."""

    def __init__(
        self,
        transcripts: list[str],
        *,
        enabled: bool = True,
        wake_word_enabled: bool = True,
        backend: str = "fake",
        emergency_stop: bool = False,
    ) -> None:
        self.transcripts = list(transcripts)
        self.captures: list[float] = []
        self.spoken: list[str] = []
        self._enabled = enabled
        self._wake = wake_word_enabled
        self._backend = backend
        self._stop = emergency_stop

    @property
    def emergency_stop(self) -> bool:
        return self._stop

    def status(self) -> Any:
        return _Status(
            microphone={
                "enabled": self._enabled,
                "wake_word_enabled": self._wake,
                "backend": self._backend,
                "backend_reason": "" if self._backend != "none" else "no backend",
                "indicator": "○ ready" if self._enabled else "✕ off",
            },
            emergency_stop=self._stop,
        )

    def start_capture(self) -> dict[str, Any]:
        self.captures.append(0.0)
        return {"state": "capturing"}

    def stop_capture(self) -> dict[str, Any]:
        text = self.transcripts.pop(0) if self.transcripts else ""
        return {
            "text": text,
            "audio_seconds": 1.0,
            "error": "" if text else "No speech detected",
        }

    def speak(self, text: str) -> dict[str, Any]:
        self.spoken.append(text)
        return {"ok": True, "engine": "fake-tts"}


class _Status:
    def __init__(self, *, microphone: dict[str, Any], emergency_stop: bool) -> None:
        self.microphone = microphone
        self.emergency_stop = emergency_stop


def _loop(voice: FakeVoice, *, speak: bool = True) -> VoiceLoop:
    executor = VoiceIntentExecutor(
        None,
        None,
        task_submitter=lambda instruction: {"id": "t-1", "status": "queued"},
        note_sink=lambda text: {"key": "k"},
        reminder_sink=lambda title, due: {"id": "r-1", "due_at": due},
        status_reader=lambda: {"tasks": {"open": 2}},
        now_reader=lambda: __import__("datetime").datetime(2026, 11, 6, 9, 30),
    )
    return VoiceLoop(voice, executor, wake_word="jarvis", speak_replies=speak, sleep=lambda _s: None)


def test_loop_requires_the_persisted_privacy_gate() -> None:
    disabled = FakeVoice([], enabled=False)
    report = _loop(disabled).run(max_cycles=2)
    assert report.state == STATE_BLOCKED
    assert "microphone is off" in report.blocked_reason
    assert disabled.captures == []  # the microphone was never opened

    no_wake = FakeVoice([], wake_word_enabled=False)
    report = _loop(no_wake).run(max_cycles=2)
    assert "wake-word mode needs its own opt-in" in report.blocked_reason

    emergency = FakeVoice([], emergency_stop=True)
    report = _loop(emergency).run(max_cycles=2)
    assert report.blocked_reason == "the emergency stop is engaged"


def test_loop_handles_a_full_session_and_speaks_the_replies() -> None:
    voice = FakeVoice(
        [
            "jarvis what time is it",
            "jarvis note buy milk",
            "jarvis remind me to call Ali at 18:00",
            "jarvis open the downloads folder",
            "jarvis stop",
        ]
    )
    loop = _loop(voice)
    report = loop.run(max_cycles=5, cycle_seconds=0.0)
    assert report.state == STATE_IDLE
    assert report.stop_reason == "user said stop"
    names = [(cycle.get("intent") or {}).get("intent") for cycle in report.cycles]
    assert names == ["time", "note", "reminder", "task", "stop"]
    assert report.handled == 5
    assert voice.spoken[0].startswith("It is 09:30")
    assert loop.status()["last_heard"].startswith("jarvis stop")


def test_loop_ignores_speech_that_does_not_start_with_the_wake_word() -> None:
    voice = FakeVoice(["i asked jarvis yesterday", "jarvis status"])
    report = _loop(voice).run(max_cycles=2, cycle_seconds=0.0)
    first, second = report.cycles
    assert first["executed"] is False
    assert "did not start with the wake word" in first["reason"]
    assert second["executed"] is True
    assert (second["intent"] or {})["intent"] == "status"


def test_loop_is_bounded_and_stoppable() -> None:
    voice = FakeVoice(["jarvis status"] * 10)
    loop = _loop(voice, speak=False)
    report = loop.run(max_cycles=3, cycle_seconds=0.0)
    assert len(report.cycles) == 3
    assert report.stop_reason == "max_cycles"

    stopping = FakeVoice(["jarvis status"] * 10)
    stoppable = _loop(stopping, speak=False)
    stoppable.stop()
    report = stoppable.run(max_cycles=5, cycle_seconds=0.0)
    assert report.state == STATE_STOPPED
    assert report.stop_reason == "stopped"
    assert report.cycles == []


def test_loop_can_run_without_the_wake_phrase() -> None:
    voice = FakeVoice(["status", "note hello"])
    report = _loop(voice, speak=False).run(
        max_cycles=2, cycle_seconds=0.0, require_wake_phrase=False
    )
    assert [cycle["executed"] for cycle in report.cycles] == [True, True]


def test_loop_survives_a_capture_error() -> None:
    class Broken(FakeVoice):
        def start_capture(self) -> dict[str, Any]:
            raise RuntimeError("device disappeared")

    voice = Broken(["jarvis status"])
    report = _loop(voice).run(max_cycles=1, cycle_seconds=0.0)
    assert report.cycles[0]["executed"] is False
    assert "device disappeared" in report.cycles[0]["error"]


# -------------------------------------------------------------- acceptance ---


def test_voice_acceptance_is_sealed_and_honest_on_a_headless_host(tmp_path: Path) -> None:
    from pc_agent.voice import VoiceService

    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    report = run_voice_acceptance(VoiceService(settings, database), version=__version__)

    payload = report.to_dict()
    assert payload["body_sha256"]
    assert [gate["name"] for gate in payload["gates"]] == list(REQUIRED_VOICE_GATES)
    assert report.failures == 0
    # No microphone and no speech engine in CI: the gates say so instead of passing.
    assert report.not_run, "a headless host must not report every gate as passed"
    assert report.status == "incomplete"
    # The harness must not leave the microphone enabled.
    assert VoiceService(settings, database).status().microphone["enabled"] is False


def test_voice_acceptance_report_verifies_and_detects_edits(tmp_path: Path) -> None:
    from pc_agent.voice import VoiceService

    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    report = run_voice_acceptance(VoiceService(settings, database), version=__version__)
    target = tmp_path / "voice-acceptance.json"
    target.write_text(json.dumps(report.to_dict(), sort_keys=True), encoding="utf-8")
    assert verify_voice_acceptance_report(target)["ok"] is True

    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["gates"][0]["detail"] = "edited"
    target.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    result = verify_voice_acceptance_report(target)
    assert result["ok"] is False
    assert any("hash" in problem for problem in result["problems"])


def test_voice_cli_surfaces_exist_and_are_wired() -> None:
    source = (PROJECT_ROOT / "src" / "pc_agent" / "cli.py").read_text(encoding="utf-8")
    for command in ('"intent"', '"loop"', '"acceptance"', '"acceptance-verify"'):
        assert command in source
    api = (PROJECT_ROOT / "src" / "pc_agent" / "api_server.py").read_text(encoding="utf-8")
    # The HTTP surface keeps the same service object, so the new routes reuse it.
    assert "/v1/voice/" in api
