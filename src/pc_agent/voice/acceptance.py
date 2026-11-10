"""Voice acceptance harness (voice completion, 0.81.0).

"Full voice assistant" is a claim. This module makes it checkable on the machine
that will use it: it runs the real gates, records what each one measured, and
seals the report so it cannot be edited afterwards (the same
``baseline.acceptance_body_sha256`` seal the Windows acceptance harness uses).

Gates, all real:

* ``privacy_gate`` - the persisted microphone gate: enabling and disabling really
  change the state and the indicator (the original state is restored).
* ``push_to_talk`` - the capture backend this host actually resolves to.
* ``wake_phrase`` - wake-phrase stripping and the "addressed" flag.
* ``intent_routing`` - utterances to intents, including the fallback.
* ``stt_engine`` - the speech-to-text engine that would be used.
* ``tts_engine`` - a real synthesis attempt to a temporary file, then deleted.
* ``retention`` - the audio-retention and transcript-storage policy in force.

Status vocabulary, deliberately not binary:

* ``passed`` - every gate ran and passed;
* ``incomplete`` - no gate failed, but at least one could not run here (no
  microphone, no speech engine); the report names them, and this is what a
  headless build machine will see;
* ``failed`` - at least one gate failed.

The report never contains audio, transcripts of the user's speech, or secrets -
only gate verdicts, engine identifiers and the host's platform facts.
"""

from __future__ import annotations

import platform
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..baseline import acceptance_body_sha256
from ..errors import VoiceError
from .intents import SUPPORTED_INTENTS, parse_intent, strip_wake_phrase

__all__ = [
    "REQUIRED_VOICE_GATES",
    "VoiceAcceptanceGate",
    "VoiceAcceptanceReport",
    "run_voice_acceptance",
    "verify_voice_acceptance_report",
]

REQUIRED_VOICE_GATES = (
    "privacy_gate",
    "push_to_talk",
    "wake_phrase",
    "intent_routing",
    "stt_engine",
    "tts_engine",
    "retention",
)

ROUTING_CASES = (
    ("jarvis status", "status"),
    ("jarvis what time is it", "time"),
    ("jarvis note buy milk", "note"),
    ("jarvis remind me to call Ali at 18:00", "reminder"),
    ("jarvis say good morning", "speak"),
    ("jarvis help", "help"),
    ("jarvis stop", "stop"),
    ("jarvis open the downloads folder", "task"),
)


@dataclass(frozen=True, slots=True)
class VoiceAcceptanceGate:
    name: str
    status: str  # pass | fail | not-run
    detail: str
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class VoiceAcceptanceReport:
    status: str  # passed | incomplete | failed
    platform: str
    generated_at: str
    version: str
    gates: tuple[VoiceAcceptanceGate, ...]
    host: dict[str, Any]
    body_sha256: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    @property
    def failures(self) -> int:
        return sum(1 for gate in self.gates if gate.status == "fail")

    @property
    def not_run(self) -> tuple[str, ...]:
        return tuple(gate.name for gate in self.gates if gate.status == "not-run")

    def body(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "platform": self.platform,
            "generated_at": self.generated_at,
            "version": self.version,
            "host": dict(self.host),
            "gates": [gate.to_dict() for gate in self.gates],
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.body()
        payload["body_sha256"] = self.body_sha256 or acceptance_body_sha256(payload)
        return payload

    def summary(self) -> str:
        passed = sum(1 for gate in self.gates if gate.status == "pass")
        line = (
            f"{self.status.upper()}; gates={len(self.gates)}; passed={passed}; "
            f"failures={self.failures}"
        )
        if self.not_run:
            line += f"; not-run={','.join(self.not_run)}"
        return line


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _seal(report: VoiceAcceptanceReport) -> VoiceAcceptanceReport:
    return VoiceAcceptanceReport(
        status=report.status,
        platform=report.platform,
        generated_at=report.generated_at,
        version=report.version,
        gates=report.gates,
        host=report.host,
        body_sha256=acceptance_body_sha256(report.body()),
    )


def run_voice_acceptance(service: Any, *, version: str) -> VoiceAcceptanceReport:
    """Run every voice gate against the real service and seal the result."""

    import time

    gates: list[VoiceAcceptanceGate] = []
    status = service.status()

    started = time.perf_counter()
    initial = status.microphone.get("enabled")
    try:
        enabled = service.enable() if not initial else dict(status.microphone)
        if not enabled.get("enabled"):
            gates.append(
                VoiceAcceptanceGate("privacy_gate", "fail", "the microphone did not enable")
            )
        else:
            disabled = service.disable()
            if disabled.get("enabled"):
                gates.append(
                    VoiceAcceptanceGate(
                        "privacy_gate", "fail", "the microphone did not disable"
                    )
                )
            elif disabled.get("state") != "disabled":
                gates.append(
                    VoiceAcceptanceGate(
                        "privacy_gate",
                        "fail",
                        f"state after disable is {disabled.get('state')!r}",
                    )
                )
            else:
                gates.append(
                    VoiceAcceptanceGate(
                        "privacy_gate",
                        "pass",
                        "enable/disable really change state and indicator "
                        f"({enabled.get('indicator')} -> {disabled.get('indicator')})",
                        int((time.perf_counter() - started) * 1000),
                    )
                )
    except VoiceError as exc:
        gates.append(VoiceAcceptanceGate("privacy_gate", "fail", f"privacy gate raised: {exc}"))
    finally:
        # Restore the user's own choice; the harness must not leave the mic on.
        try:
            if initial and not service.status().microphone.get("enabled"):
                service.enable()
            elif not initial and service.status().microphone.get("enabled"):
                service.disable()
        except VoiceError:  # pragma: no cover - restore is best effort
            pass

    # A capture backend being importable is not the same as a machine having a
    # microphone: winmm/ALSA exist on hosts with no input device at all, so the
    # gate needs an enumerated device before it may claim a pass. Even then it is
    # deliberately worded as "backend + device present" - this harness does not
    # record audio, and the report must not imply that it did.
    backend = status.microphone.get("backend", "none")
    input_devices = status.input_devices or {}
    input_count = input_devices.get("count")
    if backend and backend != "none" and input_count:
        gates.append(
            VoiceAcceptanceGate(
                "push_to_talk",
                "pass",
                f"capture backend {backend} with {input_count} input device(s) "
                "enumerated (presence only - no audio was recorded by this gate)",
            )
        )
    elif backend and backend != "none":
        gates.append(
            VoiceAcceptanceGate(
                "push_to_talk",
                "not-run",
                f"capture backend {backend} found no input device: "
                + (
                    input_devices.get("reason")
                    or "the device scan enumerated none"
                ),
            )
        )
    else:
        gates.append(
            VoiceAcceptanceGate(
                "push_to_talk",
                "not-run",
                status.microphone.get("backend_reason")
                or "no microphone backend on this host",
            )
        )

    wake_word = status.microphone.get("wake_word") or "jarvis"
    remainder, addressed = strip_wake_phrase(f"{wake_word} status", wake_word)
    _misaddressed, wrongly_addressed = strip_wake_phrase("i asked jarvis yesterday", wake_word)
    if addressed and remainder == "status" and not wrongly_addressed:
        gates.append(
            VoiceAcceptanceGate(
                "wake_phrase", "pass", f"'{wake_word}' is stripped at the start only"
            )
        )
    else:
        gates.append(
            VoiceAcceptanceGate(
                "wake_phrase",
                "fail",
                f"wake phrase parsing is wrong (addressed={addressed}, "
                f"remainder={remainder!r}, mid-sentence={wrongly_addressed})",
            )
        )

    mismatches = []
    for utterance, expected in ROUTING_CASES:
        actual = parse_intent(utterance, wake_word=wake_word).name
        if actual != expected:
            mismatches.append(f"{utterance!r}->{actual} (expected {expected})")
    if mismatches:
        gates.append(
            VoiceAcceptanceGate("intent_routing", "fail", "; ".join(mismatches[:4]))
        )
    else:
        gates.append(
            VoiceAcceptanceGate(
                "intent_routing",
                "pass",
                f"{len(ROUTING_CASES)} utterances routed deterministically "
                f"({len(SUPPORTED_INTENTS)} intents supported)",
            )
        )

    stt = status.stt
    if stt.get("available"):
        gates.append(
            VoiceAcceptanceGate(
                "stt_engine",
                "pass",
                f"engine={stt.get('engine')} ({stt.get('label')}); local={stt.get('local')}",
            )
        )
    else:
        gates.append(
            VoiceAcceptanceGate(
                "stt_engine",
                "not-run",
                stt.get("reason") or "no speech-to-text engine is installed",
            )
        )

    tts = status.tts
    if not tts.get("available"):
        gates.append(
            VoiceAcceptanceGate(
                "tts_engine",
                "not-run",
                tts.get("reason") or "no text-to-speech engine is installed",
            )
        )
    else:
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="pc-agent-voice-acceptance-") as temporary:
            target = Path(temporary) / "acceptance.wav"
            try:
                spoken = service.speak("voice acceptance", output_path=target)
            except VoiceError as exc:
                gates.append(
                    VoiceAcceptanceGate("tts_engine", "fail", f"synthesis raised: {exc}")
                )
            else:
                produced = target.is_file() and target.stat().st_size > 0
                if spoken.get("ok") and produced:
                    gates.append(
                        VoiceAcceptanceGate(
                            "tts_engine",
                            "pass",
                            f"engine={spoken.get('engine')} synthesised "
                            f"{produced and target.stat().st_size} bytes of audio",
                            int((time.perf_counter() - started) * 1000),
                        )
                    )
                else:
                    gates.append(
                        VoiceAcceptanceGate(
                            "tts_engine",
                            "fail",
                            f"no audio was produced: {spoken.get('error') or 'empty file'}",
                        )
                    )

    microphone = status.microphone
    gates.append(
        VoiceAcceptanceGate(
            "retention",
            "pass",
            f"retention={microphone.get('retention')}; "
            f"store_transcripts={'on' if microphone.get('store_transcripts') else 'off'}; "
            f"cloud_stt={'allowed' if microphone.get('cloud_stt_allowed') else 'not allowed'}",
        )
    )

    ordered = tuple(sorted(gates, key=lambda gate: REQUIRED_VOICE_GATES.index(gate.name)))
    if any(gate.status == "fail" for gate in ordered):
        status_text = "failed"
    elif any(gate.status == "not-run" for gate in ordered):
        status_text = "incomplete"
    else:
        status_text = "passed"
    host = {
        "platform": sys.platform,
        "system": platform.system(),
        "release": platform.release(),
        "python": platform.python_version(),
        "input_devices": status.input_devices.get("count"),
        "output_devices": status.output_devices.get("count"),
        "microphone_backend": backend,
    }
    return _seal(
        VoiceAcceptanceReport(
            status=status_text,
            platform=sys.platform,
            generated_at=_now(),
            version=version,
            gates=ordered,
            host=host,
        )
    )


def verify_voice_acceptance_report(path: Path) -> dict[str, Any]:
    """Recompute the seal and sanity-check a stored voice acceptance report."""

    import json

    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    problems: list[str] = []
    if not isinstance(payload, dict):
        return {"ok": False, "problems": ["report is not a JSON object"]}
    if payload.get("body_sha256") != acceptance_body_sha256(payload):
        problems.append("report body was modified after it was sealed (hash mismatch)")
    if payload.get("status") not in {"passed", "incomplete", "failed"}:
        problems.append(f"unknown status {payload.get('status')!r}")
    names = [gate.get("name") for gate in payload.get("gates") or [] if isinstance(gate, dict)]
    missing = [name for name in REQUIRED_VOICE_GATES if name not in names]
    if missing:
        problems.append("report is missing gates: " + ", ".join(missing))
    if payload.get("status") == "passed":
        bad = [
            gate.get("name")
            for gate in payload.get("gates") or []
            if isinstance(gate, dict) and gate.get("status") != "pass"
        ]
        if bad:
            problems.append("a passed report cannot contain unpassed gates: " + ", ".join(bad))
    return {
        "ok": not problems,
        "status": payload.get("status"),
        "problems": problems,
    }
