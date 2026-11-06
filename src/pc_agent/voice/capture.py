"""Microphone capture: bounded, stoppable, and backend-honest.

Backends, in the order they are probed:

1. ``winmm`` - native Windows ``waveIn*`` capture through ctypes (no
   third-party audio package, no bundled binary).
2. ``arecord`` - ALSA's CLI recorder when the host has alsa-utils.
3. ``none`` - nothing is installed; capture refuses with the reason.

Recording always runs on a worker thread so the UI thread never blocks, the
duration is hard-bounded by ``max_seconds``, and :meth:`stop` returns a WAV
path plus the measured duration. A short recording that is only silence is
reported as such (RMS energy), so the service can tell the user "I heard
nothing" instead of inventing a transcript.
"""

from __future__ import annotations

import array
import logging
import math
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import VoiceError

_LOG = logging.getLogger("pc_agent.voice.capture")

_CALLBACK_NULL = 0
_WHDR_DONE = 0x00000001
_WAVE_MAPPER = 0xFFFFFFFF
_MMSYSERR_NOERROR = 0
_POLL_SECONDS = 0.02
_SILENCE_RMS_THRESHOLD = 120.0


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """Outcome of one capture attempt."""

    path: Path | None
    duration_ms: int
    sample_rate: int
    frames: int
    backend: str
    reason: str = ""
    rms: float = 0.0
    peak: float = 0.0
    silent: bool = True

    @property
    def ok(self) -> bool:
        return self.path is not None and self.frames > 0

    @property
    def seconds(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return round(self.frames / float(self.sample_rate), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": str(self.path) if self.path is not None else None,
            "duration_ms": self.duration_ms,
            "audio_seconds": self.seconds,
            "sample_rate": self.sample_rate,
            "frames": self.frames,
            "backend": self.backend,
            "reason": self.reason,
            "rms": round(self.rms, 1),
            "peak": round(self.peak, 1),
            "silent": self.silent,
        }


def wav_statistics(path: Path) -> tuple[int, int, float, float]:
    """Return ``(frames, sample_rate, rms, peak)`` for a PCM WAV file.

    Silent or corrupt files report zeros rather than raising, because this
    runs on the user-facing path where a clear "no speech detected" beats a
    traceback.
    """
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            raw = handle.readframes(frames)
    except (wave.Error, OSError):
        return 0, 0, 0.0, 0.0
    if width != 2 or not raw:
        return frames, rate, 0.0, 0.0
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    if not samples:
        return frames, rate, 0.0, 0.0
    if channels > 1:
        step = channels
        samples = array.array("h", samples[::step])
    total = 0.0
    peak = 0.0
    for value in samples:
        magnitude = float(abs(value))
        peak = max(peak, magnitude)
        total += magnitude * magnitude
    rms = math.sqrt(total / len(samples)) if samples else 0.0
    return frames, rate, rms, peak


def record_wav_via_winmm(
    path: Path,
    *,
    seconds: float,
    sample_rate: int = 16_000,
    channels: int = 1,
    device_index: int | None = None,
    stop_event: threading.Event | None = None,
    poll_seconds: float = _POLL_SECONDS,
) -> CaptureResult:
    """Record mono/stereo PCM through ``winmm`` ``waveIn*`` into ``path``.

    The buffer is prepared for ``seconds`` of audio and the loop stops early
    when ``stop_event`` is set, so push-to-talk feels instant while the hard
    ceiling still bounds a forgotten open microphone.
    """
    import ctypes

    class WaveFormatEx(ctypes.Structure):
        _fields_ = [
            ("format_tag", ctypes.c_uint16),
            ("channels", ctypes.c_uint16),
            ("samples_per_sec", ctypes.c_uint32),
            ("avg_bytes_per_sec", ctypes.c_uint32),
            ("block_align", ctypes.c_uint16),
            ("bits_per_sample", ctypes.c_uint16),
            ("cb_size", ctypes.c_uint16),
        ]

    class WaveHdr(ctypes.Structure):
        _fields_ = [
            ("data", ctypes.c_void_p),
            ("buffer_length", ctypes.c_uint32),
            ("bytes_recorded", ctypes.c_uint32),
            ("user", ctypes.c_size_t),
            ("flags", ctypes.c_uint32),
            ("loops", ctypes.c_uint32),
            ("next", ctypes.c_void_p),
            ("reserved", ctypes.c_size_t),
        ]

    winmm = ctypes.WinDLL("winmm", use_last_error=True)
    winmm.waveInOpen.argtypes = (
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint,
        ctypes.POINTER(WaveFormatEx),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint,
    )
    winmm.waveInOpen.restype = ctypes.c_uint
    winmm.waveInPrepareHeader.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(WaveHdr),
        ctypes.c_uint,
    )
    winmm.waveInAddBuffer.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(WaveHdr),
        ctypes.c_uint,
    )
    winmm.waveInStart.argtypes = (ctypes.c_void_p,)
    winmm.waveInStop.argtypes = (ctypes.c_void_p,)
    winmm.waveInReset.argtypes = (ctypes.c_void_p,)
    winmm.waveInUnprepareHeader.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(WaveHdr),
        ctypes.c_uint,
    )
    winmm.waveInClose.argtypes = (ctypes.c_void_p,)

    block_align = max(1, channels * 2)
    buffer_seconds = max(1.0, float(seconds))
    buffer_bytes = int(buffer_seconds * sample_rate) * block_align
    payload = ctypes.create_string_buffer(buffer_bytes)
    fmt = WaveFormatEx(
        format_tag=1,  # WAVE_FORMAT_PCM
        channels=channels,
        samples_per_sec=sample_rate,
        avg_bytes_per_sec=sample_rate * block_align,
        block_align=block_align,
        bits_per_sample=16,
        cb_size=0,
    )
    header = WaveHdr()
    header.data = ctypes.cast(payload, ctypes.c_void_p)
    header.buffer_length = buffer_bytes

    handle = ctypes.c_void_p()
    device = _WAVE_MAPPER if device_index is None else int(device_index)
    code = winmm.waveInOpen(
        ctypes.byref(handle), device, ctypes.byref(fmt), None, None, _CALLBACK_NULL
    )
    if code != _MMSYSERR_NOERROR or not handle:
        return CaptureResult(
            path=None,
            duration_ms=0,
            sample_rate=sample_rate,
            frames=0,
            backend="winmm",
            reason=f"waveInOpen failed (code {code}); check microphone privacy settings.",
        )

    started = time.perf_counter()
    try:
        code = winmm.waveInPrepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header))
        if code != _MMSYSERR_NOERROR:
            return CaptureResult(
                path=None,
                duration_ms=0,
                sample_rate=sample_rate,
                frames=0,
                backend="winmm",
                reason=f"waveInPrepareHeader failed (code {code}).",
            )
        code = winmm.waveInAddBuffer(handle, ctypes.byref(header), ctypes.sizeof(header))
        if code != _MMSYSERR_NOERROR:
            winmm.waveInUnprepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header))
            return CaptureResult(
                path=None,
                duration_ms=0,
                sample_rate=sample_rate,
                frames=0,
                backend="winmm",
                reason=f"waveInAddBuffer failed (code {code}).",
            )
        code = winmm.waveInStart(handle)
        if code != _MMSYSERR_NOERROR:
            winmm.waveInUnprepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header))
            return CaptureResult(
                path=None,
                duration_ms=0,
                sample_rate=sample_rate,
                frames=0,
                backend="winmm",
                reason=f"waveInStart failed (code {code}).",
            )

        deadline = started + buffer_seconds
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if header.flags & _WHDR_DONE:
                break
            if time.perf_counter() >= deadline:
                break
            time.sleep(poll_seconds)

        winmm.waveInStop(handle)
        winmm.waveInReset(handle)
        winmm.waveInUnprepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header))
    finally:
        winmm.waveInClose(handle)

    captured = min(int(header.bytes_recorded), buffer_bytes)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if captured <= 0:
        return CaptureResult(
            path=None,
            duration_ms=elapsed_ms,
            sample_rate=sample_rate,
            frames=0,
            backend="winmm",
            reason="The microphone produced no samples (muted or disabled device).",
        )
    frames = captured // block_align
    with wave.open(str(path), "wb") as handle_out:
        handle_out.setnchannels(channels)
        handle_out.setsampwidth(2)
        handle_out.setframerate(sample_rate)
        handle_out.writeframes(bytes(payload.raw[: frames * block_align]))
    frames, rate, rms, peak = wav_statistics(path)
    return CaptureResult(
        path=path,
        duration_ms=elapsed_ms,
        sample_rate=rate or sample_rate,
        frames=frames,
        backend="winmm",
        rms=rms,
        peak=peak,
        silent=rms < _SILENCE_RMS_THRESHOLD,
    )


def record_wav_via_arecord(
    path: Path,
    *,
    seconds: float,
    sample_rate: int = 16_000,
    device_index: int | None = None,
    stop_event: threading.Event | None = None,
) -> CaptureResult:
    """Record through ALSA's ``arecord`` with a bounded duration."""
    binary = shutil.which("arecord")
    if binary is None:
        return CaptureResult(
            path=None,
            duration_ms=0,
            sample_rate=sample_rate,
            frames=0,
            backend="arecord",
            reason="arecord is not installed (install alsa-utils).",
        )
    argv = [
        binary,
        "-q",
        "-t",
        "wav",
        "-f",
        "S16_LE",
        "-r",
        str(sample_rate),
        "-c",
        "1",
        "-d",
        str(max(1, math.ceil(seconds))),
    ]
    if device_index is not None:
        argv.extend(["-D", f"plughw:{int(device_index)}"])
    argv.append(str(path))
    started = time.perf_counter()
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        return CaptureResult(
            path=None,
            duration_ms=0,
            sample_rate=sample_rate,
            frames=0,
            backend="arecord",
            reason=f"could not start arecord: {exc.strerror}",
        )
    try:
        while process.poll() is None:
            if stop_event is not None and stop_event.is_set():
                process.send_signal(signal.SIGINT)
                break
            time.sleep(_POLL_SECONDS)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    finally:
        if process.poll() is None:  # pragma: no cover - defensive
            process.kill()
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if not path.is_file():
        return CaptureResult(
            path=None,
            duration_ms=elapsed_ms,
            sample_rate=sample_rate,
            frames=0,
            backend="arecord",
            reason="arecord exited without writing audio.",
        )
    frames, rate, rms, peak = wav_statistics(path)
    return CaptureResult(
        path=path,
        duration_ms=elapsed_ms,
        sample_rate=rate or sample_rate,
        frames=frames,
        backend="arecord",
        rms=rms,
        peak=peak,
        silent=rms < _SILENCE_RMS_THRESHOLD,
    )


class PushToTalkRecorder:
    """One bounded, stoppable recording session at a time.

    ``start()`` spawns the capture thread; ``stop()`` signals it, joins and
    returns the :class:`CaptureResult`. Calling ``start()`` twice raises
    instead of opening a second microphone stream.
    """

    def __init__(
        self,
        *,
        work_dir: Path,
        backend: str = "auto",
        sample_rate: int = 16_000,
        max_seconds: float = 60.0,
        device_index: int | None = None,
        capture_fn: Callable[..., CaptureResult] | None = None,
    ) -> None:
        self._work_dir = Path(work_dir)
        self._backend = backend
        self._sample_rate = sample_rate
        self._max_seconds = max_seconds
        self._device_index = device_index
        self._capture_fn = capture_fn
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._result: CaptureResult | None = None
        self._error: str = ""
        self._path: Path | None = None

    # -- capability ---------------------------------------------------------
    @property
    def backend(self) -> str:
        return self._backend

    def resolve_backend(self) -> tuple[str, str]:
        """Return ``(backend, reason)`` for the configured preference."""
        preference = (self._backend or "auto").strip().lower()
        if self._capture_fn is not None:
            return preference, ""
        if preference == "none":
            return "none", "Capture is disabled by configuration."
        candidates = (
            ["winmm", "arecord"] if preference == "auto" else [preference]
        )
        for candidate in candidates:
            if candidate == "winmm" and os.name == "nt":
                return "winmm", ""
            if candidate == "arecord" and shutil.which("arecord"):
                return "arecord", ""
        return "none", (
            "No microphone backend is available on this host; Windows uses "
            "winmm and Linux needs alsa-utils (arecord)."
        )

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise VoiceError("A capture is already in progress; stop it first.")
            backend, reason = self.resolve_backend()
            if backend == "none":
                raise VoiceError(f"Microphone unavailable: {reason}")
            self._work_dir.mkdir(parents=True, exist_ok=True)
            handle, raw_path = tempfile.mkstemp(
                prefix="voice-", suffix=".wav", dir=str(self._work_dir)
            )
            os.close(handle)
            self._path = Path(raw_path)
            self._stop_event = threading.Event()
            self._result = None
            self._error = ""
            self._thread = threading.Thread(
                target=self._run,
                args=(backend, self._path, self._stop_event),
                name="pc-agent-voice-capture",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> CaptureResult:
        with self._lock:
            thread = self._thread
            stop_event = self._stop_event
            path = self._path
        if thread is None:
            raise VoiceError("No capture is in progress.")
        stop_event.set()
        thread.join(timeout=max(5.0, self._max_seconds + 5.0))
        with self._lock:
            result = self._result
            error = self._error
            if thread.is_alive():  # pragma: no cover - defensive
                result = None
                error = error or "capture thread did not stop in time"
            self._thread = None
            self._path = None
        if result is not None:
            return result
        if path is not None and path.exists():
            path.unlink(missing_ok=True)
        return CaptureResult(
            path=None,
            duration_ms=0,
            sample_rate=self._sample_rate,
            frames=0,
            backend="none",
            reason=error or "Capture produced no audio.",
        )

    def cancel(self) -> None:
        """Discard the current capture without returning audio."""
        with self._lock:
            thread = self._thread
            stop_event = self._stop_event
            path = self._path
        if thread is None:
            return
        stop_event.set()
        thread.join(timeout=5.0)
        with self._lock:
            self._thread = None
            self._path = None
        if path is not None:
            path.unlink(missing_ok=True)

    # -- internals ----------------------------------------------------------
    def _run(self, backend: str, path: Path, stop_event: threading.Event) -> None:
        try:
            if self._capture_fn is not None:
                result = self._capture_fn(
                    path,
                    seconds=self._max_seconds,
                    sample_rate=self._sample_rate,
                    device_index=self._device_index,
                    stop_event=stop_event,
                )
            elif backend == "winmm":
                result = record_wav_via_winmm(
                    path,
                    seconds=self._max_seconds,
                    sample_rate=self._sample_rate,
                    device_index=self._device_index,
                    stop_event=stop_event,
                )
            else:
                result = record_wav_via_arecord(
                    path,
                    seconds=self._max_seconds,
                    sample_rate=self._sample_rate,
                    device_index=self._device_index,
                    stop_event=stop_event,
                )
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as a reason
            _LOG.debug("capture backend failed", exc_info=True)
            result = CaptureResult(
                path=None,
                duration_ms=0,
                sample_rate=self._sample_rate,
                frames=0,
                backend=backend,
                reason=f"capture failed: {type(exc).__name__}: {exc}",
            )
        with self._lock:
            self._result = result
            self._error = "" if result.ok else result.reason


__all__ = [
    "CaptureResult",
    "PushToTalkRecorder",
    "record_wav_via_arecord",
    "record_wav_via_winmm",
    "wav_statistics",
]
