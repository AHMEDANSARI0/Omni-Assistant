"""Audio device discovery without third-party packages.

Windows uses ``winmm`` (``waveInGetDevCapsW`` / ``waveOutGetDevCapsW``) via
ctypes; Linux reads ``/proc/asound/cards`` and falls back to ``arecord -l``
when ALSA tools are installed. Anything else reports unavailable with the
method that was tried, so a UI can show the truth instead of an empty
list that looks like "no devices".
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("pc_agent.voice.devices")

_MAX_NAME = 31  # MAXPNAMELEN - 1
_PROBE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class AudioDevice:
    """One input or output device as the OS reports it."""

    index: int
    name: str
    kind: str
    is_default: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "kind": self.kind,
            "is_default": self.is_default,
        }


@dataclass(frozen=True, slots=True)
class DeviceProbe:
    """Result of a device scan: devices plus how they were found."""

    devices: tuple[AudioDevice, ...]
    method: str
    reason: str = ""

    @property
    def available(self) -> bool:
        return bool(self.devices)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "method": self.method,
            "reason": self.reason,
            "count": len(self.devices),
            "devices": [device.to_dict() for device in self.devices],
        }


def list_input_devices() -> DeviceProbe:
    """Microphones/capture endpoints on this machine."""
    if os.name == "nt":
        return _probe_winmm(kind="input")
    if os.name == "posix":
        probe = _probe_alsa_cards()
        if probe.available:
            return probe
        return _probe_arecord()
    return DeviceProbe(devices=(), method="unsupported", reason="No device API for this OS.")


def list_output_devices() -> DeviceProbe:
    """Speakers/output endpoints on this machine."""
    if os.name == "nt":
        return _probe_winmm(kind="output")
    if os.name == "posix":
        probe = _probe_alsa_cards()
        if probe.available:
            return probe
        if shutil.which("aplay"):
            return DeviceProbe(
                devices=(),
                method="aplay",
                reason="aplay is installed but no ALSA cards were found.",
            )
        return DeviceProbe(
            devices=(),
            method="alsa",
            reason="No ALSA cards found; install ALSA utilities to list outputs.",
        )
    return DeviceProbe(devices=(), method="unsupported", reason="No device API for this OS.")


# -- Windows ---------------------------------------------------------------


def _probe_winmm(*, kind: str) -> DeviceProbe:
    try:
        import ctypes

        class WaveInCaps(ctypes.Structure):
            _fields_ = [
                ("w_mid", ctypes.c_uint16),
                ("w_pid", ctypes.c_uint16),
                ("driver_version", ctypes.c_uint32),
                ("name", ctypes.c_wchar * 32),
                ("formats", ctypes.c_uint32),
                ("channels", ctypes.c_uint16),
                ("reserved", ctypes.c_uint16),
            ]

        class WaveOutCaps(ctypes.Structure):
            _fields_ = [
                ("w_mid", ctypes.c_uint16),
                ("w_pid", ctypes.c_uint16),
                ("driver_version", ctypes.c_uint32),
                ("name", ctypes.c_wchar * 32),
                ("formats", ctypes.c_uint32),
                ("channels", ctypes.c_uint16),
                ("reserved", ctypes.c_uint16),
                ("support", ctypes.c_uint32),
            ]

        winmm = ctypes.WinDLL("winmm", use_last_error=True)
        devices: list[AudioDevice] = []
        if kind == "input":
            total = int(winmm.waveInGetNumDevs())
            for index in range(total):
                caps = WaveInCaps()
                if winmm.waveInGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps)):
                    continue
                devices.append(
                    AudioDevice(
                        index=index,
                        name=str(caps.name).strip() or f"input {index}",
                        kind="input",
                        is_default=index == 0,
                    )
                )
            method = "winmm.waveInGetDevCapsW"
        else:
            total = int(winmm.waveOutGetNumDevs())
            for index in range(total):
                caps = WaveOutCaps()
                if winmm.waveOutGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps)):
                    continue
                devices.append(
                    AudioDevice(
                        index=index,
                        name=str(caps.name).strip() or f"output {index}",
                        kind="output",
                        is_default=index == 0,
                    )
                )
            method = "winmm.waveOutGetDevCapsW"
        reason = (
            ""
            if devices
            else "Windows reported no audio endpoints of this kind; check that the "
            "device is enabled and that microphone access is allowed in Windows "
            "privacy settings."
        )
        return DeviceProbe(devices=tuple(devices), method=method, reason=reason)
    except Exception as exc:  # noqa: BLE001 - a probe must never crash a status call
        _LOG.debug("winmm device probe failed", exc_info=True)
        return DeviceProbe(
            devices=(),
            method="winmm",
            reason=f"winmm probe failed: {type(exc).__name__}",
        )


# -- POSIX -----------------------------------------------------------------

_CARD_LINE = re.compile(r"^\s*(\d+)\s+\[([^\]]+)\]\s*:\s*(.*)$")


def _probe_alsa_cards() -> DeviceProbe:
    cards = Path("/proc/asound/cards")
    if not cards.is_file():
        return DeviceProbe(
            devices=(),
            method="/proc/asound/cards",
            reason="This host has no ALSA sound cards exposed in /proc.",
        )
    try:
        text = cards.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return DeviceProbe(
            devices=(), method="/proc/asound/cards", reason=f"read failed: {exc.strerror}"
        )
    devices: list[AudioDevice] = []
    for line in text.splitlines():
        match = _CARD_LINE.match(line)
        if not match:
            continue
        index = int(match.group(1))
        name = match.group(3).split()[0] if match.group(3).strip() else match.group(2)
        devices.append(
            AudioDevice(
                index=index,
                name=name.strip()[:64] or f"card {index}",
                kind="duplex",
                is_default=not devices,
            )
        )
    reason = "" if devices else "ALSA exposes cards but none could be parsed."
    return DeviceProbe(devices=tuple(devices), method="/proc/asound/cards", reason=reason)


def _probe_arecord() -> DeviceProbe:
    binary = shutil.which("arecord")
    if binary is None:
        return DeviceProbe(
            devices=(),
            method="arecord",
            reason=(
                "No capture devices found and arecord is not installed; "
                "install ALSA utilities (alsa-utils) to record."
            ),
        )
    try:
        completed = subprocess.run(
            [binary, "-l"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DeviceProbe(
            devices=(), method="arecord -l", reason=f"arecord probe failed: {type(exc).__name__}"
        )
    devices: list[AudioDevice] = []
    for line in (completed.stdout or "").splitlines():
        if not line.startswith("card "):
            continue
        match = re.match(r"card (\d+):.*?\[([^\]]+)\]", line)
        if match is None:
            continue
        devices.append(
            AudioDevice(
                index=int(match.group(1)),
                name=match.group(2).strip()[:64],
                kind="input",
                is_default=not devices,
            )
        )
    reason = "" if devices else "arecord reported no capture devices."
    return DeviceProbe(devices=tuple(devices), method="arecord -l", reason=reason)


def truncate_device_name(name: str) -> str:
    return name[:_MAX_NAME]
