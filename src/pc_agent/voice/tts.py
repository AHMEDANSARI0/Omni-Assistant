"""Text-to-speech: offline-first, injection-safe, honest about success.

Windows uses the built-in System.Speech synthesizer, Linux uses espeak-ng.
The text is handed to the engine through a temporary UTF-8 file referenced by
an environment variable (PowerShell) or through stdin (espeak-ng) - never
concatenated into a command line, so a sentence containing quotes, ``$()`` or
backticks cannot become part of a script.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from ..errors import VoiceError
from .engines import EngineInfo, discover_tts_engines, select_engine

_LOG = logging.getLogger("pc_agent.voice.tts")

_MAX_SPEAK_CHARS = 2_000
_CLI_TIMEOUT_SECONDS = 120.0

_SAPI_SCRIPT = (
    "$ErrorActionPreference='Stop'\r\n"
    "Add-Type -AssemblyName System.Speech\r\n"
    "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer\r\n"
    "$text = [System.IO.File]::ReadAllText($env:PC_AGENT_TTS_TEXT_FILE,"
    " [System.Text.Encoding]::UTF8)\r\n"
    "$out = $env:PC_AGENT_TTS_OUT\r\n"
    "if ($out) { $synth.SetOutputToWaveFile($out) } else"
    " { $synth.SetOutputToDefaultAudioDevice() }\r\n"
    "$synth.Speak($text)\r\n"
    "$synth.Dispose()\r\n"
)


@dataclass(frozen=True, slots=True)
class SpeechOutput:
    """Outcome of one speak/synthesize call."""

    engine: str
    characters: int
    played: bool
    output_path: Path | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "engine": self.engine,
            "characters": self.characters,
            "played": self.played,
            "output_path": str(self.output_path) if self.output_path else None,
            "error": self.error,
        }


class TextToSpeech:
    """Speak text through the best available offline engine."""

    def __init__(
        self,
        settings: Settings,
        *,
        engine: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        work_dir: Path | None = None,
    ) -> None:
        self._settings = settings
        self._preference = (engine or settings.voice_tts_engine or "auto").lower()
        self._runner = runner or _default_runner
        self._work_dir = Path(work_dir) if work_dir is not None else settings.data_dir / "voice"

    # -- capability ---------------------------------------------------------
    def engines(self) -> list[EngineInfo]:
        return discover_tts_engines()

    def selected(self) -> tuple[EngineInfo | None, str]:
        return select_engine(self.engines(), self._preference)

    @property
    def available(self) -> bool:
        engine, _reason = self.selected()
        return engine is not None

    # -- speaking -----------------------------------------------------------
    def speak(self, text: str, *, output_path: Path | None = None) -> SpeechOutput:
        clean = _clean_for_speech(text)
        if not clean:
            raise VoiceError("Nothing to say: the text was empty.")
        engine, reason = self.selected()
        if engine is None:
            raise VoiceError(f"No text-to-speech engine is available: {reason}")
        if engine.id == "windows-sapi":
            return self._speak_sapi(clean, output_path)
        if engine.id == "espeak-ng":
            return self._speak_espeak(clean, output_path)
        raise VoiceError(f"Unsupported speech engine: {engine.id}")  # pragma: no cover

    def _speak_sapi(self, text: str, output_path: Path | None) -> SpeechOutput:
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if shell is None:
            raise VoiceError("PowerShell is required for Windows System.Speech.")
        text_file = _write_private_text(self._work_dir, text)
        environment = {"PC_AGENT_TTS_TEXT_FILE": str(text_file.resolve())}
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            environment["PC_AGENT_TTS_OUT"] = str(Path(output_path).resolve())
        try:
            completed = self._runner(
                [
                    shell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    _SAPI_SCRIPT,
                ],
                environment_extra=environment,
                timeout=_CLI_TIMEOUT_SECONDS,
            )
        finally:
            text_file.unlink(missing_ok=True)
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()[:2]
            return SpeechOutput(
                engine="windows-sapi",
                characters=len(text),
                played=False,
                output_path=None,
                error="Windows speech synthesis failed: "
                + (" ".join(detail) or "unknown error"),
            )
        made = Path(output_path) if output_path is not None else None
        return SpeechOutput(
            engine="windows-sapi",
            characters=len(text),
            played=made is None,
            output_path=made if made is not None and made.is_file() else None,
        )

    def _speak_espeak(self, text: str, output_path: Path | None) -> SpeechOutput:
        binary = shutil.which("espeak-ng") or shutil.which("espeak")
        if binary is None:
            raise VoiceError("espeak-ng is not installed on this host.")
        argv = [binary, "--stdin"]
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            argv.extend(["-w", str(output_path)])
        completed = self._runner(
            argv,
            environment_extra={},
            stdin_text=text,
            timeout=_CLI_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            return SpeechOutput(
                engine="espeak-ng",
                characters=len(text),
                played=False,
                error="espeak-ng failed: " + (completed.stderr or "").strip()[:200],
            )
        made = Path(output_path) if output_path is not None else None
        return SpeechOutput(
            engine="espeak-ng",
            characters=len(text),
            played=made is None,
            output_path=made if made is not None and made.is_file() else None,
        )


def _clean_for_speech(text: str) -> str:
    cleaned = " ".join((text or "").replace("\x00", " ").split())
    return cleaned[:_MAX_SPEAK_CHARS]


def _write_private_text(directory: Path, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    handle, raw = tempfile.mkstemp(prefix="speech-", suffix=".txt", dir=str(directory))
    with open(handle, "w", encoding="utf-8", closefd=True) as stream:
        stream.write(text)
    return Path(raw)


def _default_runner(
    argv: list[str],
    *,
    environment_extra: dict[str, str],
    timeout: float,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(environment_extra)
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        input=stdin_text,
        check=False,
    )


__all__ = ["SpeechOutput", "TextToSpeech"]
