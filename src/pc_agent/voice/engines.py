"""Speech engine discovery: what can actually run on this machine.

Every engine is probed, never assumed. A discovered engine that is missing
its runtime (or its model file) reports ``available: false`` with a reason,
so ``pc-agent voice status`` can tell the user exactly what to install
instead of failing at the first utterance.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from dataclasses import dataclass
from typing import Any

STT_ENGINES = ("windows-sapi", "faster-whisper", "whisper-cpp", "openai-compatible")
TTS_ENGINES = ("windows-sapi", "espeak-ng")


@dataclass(frozen=True, slots=True)
class EngineInfo:
    id: str
    kind: str
    label: str
    local: bool
    available: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "local": self.local,
            "available": self.available,
            "reason": self.reason,
        }


def _powershell() -> str | None:
    if os.name != "nt":
        return None
    return shutil.which("powershell") or shutil.which("pwsh")


def discover_stt_engines(
    *, whisper_model: str = "", cloud_available: bool = False
) -> list[EngineInfo]:
    """Probe every speech-to-text backend, cheapest/local first."""
    engines: list[EngineInfo] = []

    shell = _powershell()
    engines.append(
        EngineInfo(
            id="windows-sapi",
            kind="stt",
            label="Windows System.Speech (built in, offline)",
            local=True,
            available=shell is not None,
            reason="" if shell is not None else "PowerShell was not found on PATH.",
        )
    )

    has_faster_whisper = importlib.util.find_spec("faster_whisper") is not None
    engines.append(
        EngineInfo(
            id="faster-whisper",
            kind="stt",
            label="faster-whisper (local model)",
            local=True,
            available=has_faster_whisper,
            reason=(
                ""
                if has_faster_whisper
                else "Install with: python -m pip install faster-whisper"
            ),
        )
    )

    whisper_cpp = shutil.which("whisper-cli") or shutil.which("whisper-cpp")
    if whisper_cpp is None:
        whisper_reason = "whisper.cpp CLI (whisper-cli) not found on PATH."
    elif not whisper_model:
        whisper_reason = "Set PC_AGENT_VOICE_WHISPER_MODEL to a .bin model path."
    else:
        whisper_reason = ""
    engines.append(
        EngineInfo(
            id="whisper-cpp",
            kind="stt",
            label="whisper.cpp (local, explicit model)",
            local=True,
            available=whisper_cpp is not None and bool(whisper_model),
            reason=whisper_reason,
        )
    )

    engines.append(
        EngineInfo(
            id="openai-compatible",
            kind="stt",
            label="OpenAI-compatible /audio/transcriptions (cloud, per-call consent)",
            local=False,
            available=cloud_available,
            reason=(
                ""
                if cloud_available
                else "No OpenAI-compatible API key is configured; cloud STT is optional."
            ),
        )
    )
    return engines


def discover_tts_engines() -> list[EngineInfo]:
    """Probe every text-to-speech backend."""
    engines: list[EngineInfo] = []
    shell = _powershell()
    engines.append(
        EngineInfo(
            id="windows-sapi",
            kind="tts",
            label="Windows System.Speech synthesizer (built in, offline)",
            local=True,
            available=shell is not None,
            reason="" if shell is not None else "PowerShell was not found on PATH.",
        )
    )
    espeak = shutil.which("espeak-ng") or shutil.which("espeak")
    engines.append(
        EngineInfo(
            id="espeak-ng",
            kind="tts",
            label="espeak-ng (offline)",
            local=True,
            available=espeak is not None,
            reason="" if espeak is not None else "Install espeak-ng to speak on this host.",
        )
    )
    return engines


def select_engine(
    engines: list[EngineInfo], preference: str
) -> tuple[EngineInfo | None, str]:
    """Pick the engine to use.

    ``auto`` prefers any available local engine and only then a cloud one;
    an explicit preference is honoured exactly, and reports *why* it cannot
    run rather than silently falling back to something the user did not ask
    for.
    """
    wanted = (preference or "auto").strip().lower()
    if wanted != "auto":
        for engine in engines:
            if engine.id != wanted:
                continue
            if engine.available:
                return engine, ""
            return None, f"{engine.id} is not available: {engine.reason}"
        return None, f"Unknown engine '{wanted}'."
    for engine in engines:
        if engine.available and engine.local:
            return engine, ""
    for engine in engines:
        if engine.available:
            return engine, ""
    reasons = "; ".join(f"{engine.id}: {engine.reason}" for engine in engines)
    return None, f"No speech engine is available ({reasons})."
