"""Voice pipeline: privacy gate, capture, speech-to-text, text-to-speech.

Transformation Phase 10. Everything is stdlib-first and honest about
availability: if no microphone, no speech engine or no TTS backend can be
found on this machine, the subsystem reports ``available: false`` with the
reason instead of faking a transcript or a spoken sentence.

Privacy contract (enforced in :mod:`pc_agent.voice.privacy`):

* the microphone is OFF until the user turns it on;
* push-to-talk is the only capture mode enabled by default;
* wake-word listening needs a second, separate opt-in;
* audio is deleted after transcription unless retention was enabled;
* transcripts are only stored when the user asked for it;
* cloud speech-to-text needs a per-call consent flag as well.
"""

from .capture import CaptureResult, PushToTalkRecorder, record_wav_via_winmm
from .devices import AudioDevice, list_input_devices, list_output_devices
from .engines import EngineInfo, discover_stt_engines, discover_tts_engines, select_engine
from .privacy import MicrophonePrivacy, MicrophoneStatus
from .service import VoiceService, VoiceStatus
from .stt import SpeechToText, Transcription
from .tts import SpeechOutput, TextToSpeech

__all__ = [
    "AudioDevice",
    "CaptureResult",
    "EngineInfo",
    "MicrophonePrivacy",
    "MicrophoneStatus",
    "PushToTalkRecorder",
    "SpeechOutput",
    "SpeechToText",
    "TextToSpeech",
    "Transcription",
    "VoiceService",
    "VoiceStatus",
    "discover_stt_engines",
    "discover_tts_engines",
    "list_input_devices",
    "list_output_devices",
    "record_wav_via_winmm",
    "select_engine",
]
