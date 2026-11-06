"""Speech-to-text with local-first engines and an explicit cloud gate.

Engine order under ``auto``: Windows System.Speech (built in, offline),
faster-whisper (local model), whisper.cpp (local model), and finally an
OpenAI-compatible transcription endpoint - which is *never* used implicitly:
it needs ``PC_AGENT_VOICE_CLOUD_STT=1`` **and** a per-call
``cloud_consent=True``. Audio for a refused cloud call is not uploaded at
all.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from ..errors import VoiceError
from .engines import EngineInfo, discover_stt_engines, select_engine

_LOG = logging.getLogger("pc_agent.voice.stt")

_MAX_TRANSCRIPT_CHARS = 4_000
_CLI_TIMEOUT_SECONDS = 120.0

_SAPI_SCRIPT = (
    "$ErrorActionPreference='Stop'\r\n"
    "Add-Type -AssemblyName System.Speech\r\n"
    "$engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine\r\n"
    "$engine.SetInputToWaveFile($env:PC_AGENT_STT_AUDIO)\r\n"
    "$engine.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))\r\n"
    "$result = $engine.Recognize()\r\n"
    "if ($null -ne $result) { [Console]::Out.Write($result.Text) }\r\n"
    "$engine.Dispose()\r\n"
)


@dataclass(frozen=True, slots=True)
class Transcription:
    """One transcript plus the engine that produced it."""

    text: str
    engine: str
    local: bool
    characters: int
    duration_ms: int = 0

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "engine": self.engine,
            "local": self.local,
            "characters": self.characters,
            "duration_ms": self.duration_ms,
            "empty": self.empty,
        }


def clean_transcript(raw: str) -> str:
    """Normalise engine output without inventing or editing words."""
    text = raw.replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.strip() for line in text.splitlines())
    text = " ".join(text.split())
    return text[:_MAX_TRANSCRIPT_CHARS]


class SpeechToText:
    """Transcribe a WAV file with the best available engine."""

    def __init__(
        self,
        settings: Settings,
        *,
        engine: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        http_post: Callable[[str, bytes, dict[str, str], float], str] | None = None,
    ) -> None:
        self._settings = settings
        self._preference = (engine or settings.voice_stt_engine or "auto").lower()
        self._runner = runner or _default_runner
        self._http_post = http_post
        self._whisper_model: Any = None

    # -- capability ---------------------------------------------------------
    def engines(self) -> list[EngineInfo]:
        cloud = bool(
            self._settings.voice_cloud_stt
            and (
                self._settings.openai_api_key
                or self._settings.openai_compatible_api_key
            )
        )
        return discover_stt_engines(
            whisper_model=self._settings.voice_whisper_model, cloud_available=cloud
        )

    def selected(self) -> tuple[EngineInfo | None, str]:
        return select_engine(self.engines(), self._preference)

    @property
    def available(self) -> bool:
        engine, _reason = self.selected()
        return engine is not None

    # -- transcription ------------------------------------------------------
    def transcribe(
        self,
        audio_path: Path,
        *,
        cloud_consent: bool = False,
        language: str | None = None,
    ) -> Transcription:
        path = Path(audio_path)
        if not path.is_file():
            raise VoiceError(f"Audio file not found: {path.name}")
        engine, reason = self.selected()
        if engine is None:
            raise VoiceError(f"No speech-to-text engine is available: {reason}")
        if not engine.local and not (cloud_consent and self._settings.voice_cloud_stt):
            raise VoiceError(
                "Cloud speech-to-text needs explicit consent for this utterance "
                "(set PC_AGENT_VOICE_CLOUD_STT=1 and confirm the request)."
            )
        if engine.id == "windows-sapi":
            text = self._transcribe_sapi(path)
        elif engine.id == "faster-whisper":
            text = self._transcribe_faster_whisper(path, language)
        elif engine.id == "whisper-cpp":
            text = self._transcribe_whisper_cpp(path, language)
        elif engine.id == "openai-compatible":
            text = self._transcribe_cloud(path, language)
        else:  # pragma: no cover - select_engine only returns known ids
            raise VoiceError(f"Unsupported speech engine: {engine.id}")
        cleaned = clean_transcript(text)
        return Transcription(
            text=cleaned,
            engine=engine.id,
            local=engine.local,
            characters=len(cleaned),
        )

    # -- engines ------------------------------------------------------------
    def _transcribe_sapi(self, path: Path) -> str:
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if shell is None:
            raise VoiceError("PowerShell is required for Windows System.Speech.")
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
            environment_extra={"PC_AGENT_STT_AUDIO": str(path.resolve())},
            timeout=_CLI_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()[:2]
            raise VoiceError(
                "Windows speech recognition failed: " + (" ".join(detail) or "unknown error")
            )
        return completed.stdout or ""

    def _transcribe_faster_whisper(self, path: Path, language: str | None) -> str:
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - discovery already checked
            raise VoiceError(
                "faster-whisper is not installed; run: python -m pip install faster-whisper"
            ) from exc
        if self._whisper_model is None:
            model_name = self._settings.voice_whisper_model or "base"
            self._whisper_model = WhisperModel(model_name, device="cpu", compute_type="int8")
        segments, _info = self._whisper_model.transcribe(str(path), language=language)
        return " ".join(segment.text for segment in segments)

    def _transcribe_whisper_cpp(self, path: Path, language: str | None) -> str:
        binary = shutil.which("whisper-cli") or shutil.which("whisper-cpp")
        model = self._settings.voice_whisper_model
        if binary is None or not model:
            raise VoiceError(
                "whisper.cpp needs both the whisper-cli binary and "
                "PC_AGENT_VOICE_WHISPER_MODEL pointing at a model file."
            )
        argv = [binary, "-m", model, "-f", str(path), "-nt", "--no-prints"]
        if language:
            argv.extend(["-l", language])
        completed = self._runner(argv, environment_extra={}, timeout=_CLI_TIMEOUT_SECONDS)
        if completed.returncode != 0:
            raise VoiceError("whisper.cpp failed: " f"{(completed.stderr or '').strip()[:200]}")
        return completed.stdout or ""

    def _transcribe_cloud(self, path: Path, language: str | None) -> str:
        key = self._settings.openai_api_key or self._settings.openai_compatible_api_key
        base = (
            self._settings.openai_base_url
            if self._settings.openai_api_key
            else self._settings.openai_compatible_base_url
        )
        if not key or not base:
            raise VoiceError("Cloud speech-to-text is not configured with a key and base URL.")
        body, headers = _multipart_transcription(
            path=path, model="whisper-1", language=language
        )
        url = base.rstrip("/") + "/audio/transcriptions"
        poster = self._http_post or _urllib_post
        raw = poster(url, body, headers, float(self._settings.provider_timeout_seconds))
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VoiceError("Cloud transcription returned a non-JSON response.") from exc
        text = payload.get("text")
        if not isinstance(text, str):
            raise VoiceError("Cloud transcription response did not contain text.")
        return text


def _default_runner(
    argv: list[str],
    *,
    environment_extra: dict[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    import os

    env = dict(os.environ)
    env.update(environment_extra)
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )


def _multipart_transcription(
    *, path: Path, model: str, language: str | None
) -> tuple[bytes, dict[str, str]]:
    boundary = f"----pcagent{uuid.uuid4().hex}"
    content_type = mimetypes.guess_type(path.name)[0] or "audio/wav"
    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n"
            ).encode()
        )

    add_field("model", model)
    if language:
        add_field("language", language)
    parts.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\";"
            f" filename=\"{path.name}\"\r\nContent-Type: {content_type}\r\n\r\n"
        ).encode()
    )
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }


def _urllib_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> str:
    import urllib.request

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    import os

    key = os.environ.get("OPENAI_API_KEY", "")
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")
