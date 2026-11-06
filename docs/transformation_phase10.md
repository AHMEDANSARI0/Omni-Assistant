# Transformation Phase 10 — Voice Pipeline: Push-to-Talk, STT, TTS, Microphone Privacy

Version **0.72.0** · label **TRANSFORMATION PHASE 10 Voice Pipeline** ·
schema **v12** · release audit **24 checks** · registered tools **64 (unchanged)**

Phase 10 gives Omni-Assistant a real voice path: speak an instruction instead
of typing it, and hear answers read back — **without** the microphone ever
opening on its own. Everything is local-first and stdlib-first: no bundled
binary, no mandatory cloud service, and no invented capabilities. When a
backend is missing, the UI says which one and why.

---

## 1. What existed before this phase

| Area | State at 0.71.0 |
| --- | --- |
| Text command surfaces | CLI, HTTP API, Tk GUI, web Command Center — all typed |
| Speech | None: no capture, no STT, no TTS, no microphone consent model |
| Audio device info | None |
| Privacy model | Approval Center, Safety Kernel, emergency stop (no microphone concept) |
| Windows process RSS | `psapi!GetProcessMemoryInfo` only, 32-bit HANDLE truncation → `None` |

## 2. What was missing / broken

1. **No microphone gate.** Nothing in the codebase could record, and nothing
   defined *when* recording is allowed. That is a privacy hole to close before
   any capture code exists, not after.
2. **No honest capability report.** A "voice" button that silently does nothing
   is worse than a button that says "install espeak-ng".
3. **No audio retention policy.** Recordings and transcripts need explicit
   keep-or-discard rules and a purge path.
4. **Two Windows acceptance failures** reported by the user (see §7):
   the Safety-Kernel hard-timeout test used `python3` (absent on Windows) and
   the live monitoring test could not read process RSS on Windows.

## 3. What was built

### 3.1 Package layout

```
src/pc_agent/voice/
├── __init__.py      # public exports
├── devices.py       # winmm (Windows) / ALSA (Linux) device probes
├── engines.py       # STT/TTS engine discovery + selection policy
├── capture.py       # winmm + arecord recording, silence analysis, PTT recorder
├── stt.py           # speech-to-text engines + cloud consent gate
├── tts.py           # text-to-speech engines, injection-safe text delivery
├── privacy.py       # persisted consent state machine (the gate)
└── service.py       # VoiceService: the only entry point for surfaces
```

### 3.2 Microphone privacy state machine (`privacy.py`)

States: `disabled` → `armed` → `capturing` (and back). Rules:

* the microphone starts **off**; `voice.enabled` is a **persisted preference**,
  so a restart cannot silently re-open it;
* p**ush-to-talk** is the only mode enabled by default; `wake_word` is a second,
  separate persisted opt-in and is refused while the microphone is off;
* exactly one capture at a time (a second `begin_capture` raises);
* the status always exposes `indicator` (`● REC` / `○ ready` / `✕ off`),
  `retention`, `store_transcripts`, `cloud_stt_allowed`, `backend` and
  `backend_reason`, so a UI cannot show a misleading state;
* `emergency_stop()` forces the state to `disabled`, and an engaged project-wide
  emergency stop also refuses new captures (`VoiceService._emergency_block`).

### 3.3 Capture (`capture.py`)

* **Windows:** native `winmm` `waveIn*` via `ctypes` (PCM 16-bit, configurable
  rate). Buffer is prepared for `max_seconds`; the poll loop stops early when
  the stop event fires, so push-to-talk is instant while a forgotten open
  microphone is still hard-bounded.
* **Linux:** ALSA `arecord` with `SIGINT` so the WAV header is finalised.
* **`none`:** refuses with a reason (`Microphone unavailable: ...`).
* Recording always runs on a worker thread — the Tk/HTTP caller never blocks.
* Every recording is measured (`wav_statistics`: frames, rate, RMS, peak). RMS
  below the silence threshold is reported as **silent**, so the service can say
  "no speech detected" instead of inventing a transcript.

### 3.4 Speech-to-text (`stt.py`)

| Engine | Local | Availability rule |
| --- | --- | --- |
| `windows-sapi` | yes | Windows + PowerShell (System.Speech dictation) |
| `faster-whisper` | yes | `faster_whisper` importable |
| `whisper-cpp` | yes | `whisper-cli` on PATH **and** `PC_AGENT_VOICE_WHISPER_MODEL` |
| `openai-compatible` | no | `PC_AGENT_VOICE_CLOUD_STT=1`, a key, **and** per-call consent |

`auto` prefers an available **local** engine and only then a cloud one. An
explicit preference that cannot run is reported with its reason — never
silently replaced by a different engine. Cloud transcription requires both the
global flag and `cloud_consent=True` for that utterance; otherwise the audio is
not uploaded at all. Transcripts are normalised (whitespace, NUL, 4 000 char
cap) without rewriting words.

### 3.5 Text-to-speech (`tts.py`)

`windows-sapi` (System.Speech) and `espeak-ng`. Text reaches the engine through
a **temporary UTF-8 file** referenced by an environment variable (PowerShell)
or through **stdin** (espeak) — never concatenated into a command line, so
`$(...)`, backticks or quotes in the text cannot become script. The temp file is
deleted in a `finally` block. Optional `--out` writes a WAV instead of playing.

### 3.6 Sessions, retention and purge

Schema **v12** adds one table, `voice_sessions`
(`mode`, `state`, engines, `duration_ms`, `audio_seconds`, `audio_retained`,
`audio_path`, `transcript`, `transcript_chars`, `status`, `error`).

* A row is written for **every** capture, including silent/failed ones — "a
  capture happened" is an auditable fact, even with no transcript.
* Audio is **deleted immediately** after transcription unless
  `PC_AGENT_VOICE_RETAIN_AUDIO=1`; retained files live under
  `data/voice/retained/`.
* Transcript text is stored only with `PC_AGENT_VOICE_STORE_TRANSCRIPTS=1`, and
  is passed through the existing secret redactor before it is persisted.
* `pc-agent voice privacy --purge-audio` deletes every retained recording and
  clears its reference; `--clear-history` purges audio first, then the ledger.
* The ledger is capped (2 000 rows) so it cannot grow without bound.

## 4. Surfaces

| Surface | What it exposes |
| --- | --- |
| CLI | `voice status` (honest capabilities + reasons), `devices`, `enable`, `disable`, `wake-word on\|off`, `ptt --seconds N [--run] [--cloud-consent]`, `transcribe PATH`, `say TEXT [--out FILE]`, `sessions`, `privacy [--purge-audio] [--clear-history]` |
| HTTP API | `GET /v1/voice/status`, `/devices`, `/privacy`, `/sessions`; `POST /v1/voice/enable`, `/wake-word`, `/capture/start` (202), `/capture/stop`, `/capture/cancel`, `/speak`, `/transcribe`, `/purge`, `/clear` |
| GUI (Tk) | 12th tab **Voice**: persisted enable + wake-word checkboxes, push-to-talk start / stop+transcribe / cancel, purge, say box, live indicator and privacy line |
| Web Command Center | **Voice** panel: same state, same controls, no duplicate logic |
| Diagnostics | `voice` section: microphone status, engines with reasons, device counts, session summary |
| `pc-agent doctor` | new `Voice:` line: `mic=✕ off (none); stt=...; tts=...; retention=discard; wake_word=off; sessions=0` |
| Release audit | 24th check `voice_privacy` (passes with wake word off; warns when continuous listening is on) |

Every control performs a real action against the same `VoiceService` — no
placeholders, no simulated meters.

## 5. Security implications

* **Least privilege:** voice is a *user-initiated surface*, not a tool. No agent
  can open the microphone, and the registry stays at 64 tools.
* **Fail-closed:** microphone off by default, wake word off by default, cloud
  STT off by default, and every refusal names its reason.
* **No secrets, no raw audio in logs:** events record mode, duration and
  character counts only; transcripts are redacted and opt-in.
* **Injection-resistant TTS:** text never touches a command line.
* **Bounded capture:** `PC_AGENT_VOICE_MAX_SECONDS` (1–300, default 60) caps
  every recording; the buffer is sized from it.
* **Emergency stop integration:** an engaged stop disables the microphone and
  refuses speech output.

## 6. Performance implications

All measurements are from the Linux sandbox (deterministic, no real mic):

| Operation | Result |
| --- | --- |
| `voice status` (engine probes + device scan, no capture) | ~3 ms |
| VoiceService construction | < 1 ms |
| Recording pipeline overhead (fake capture, 0.5 s WAV, fake STT) | ~12 ms |
| Silence analysis of a 1 s 16 kHz WAV | ~1.5 ms |
| Session insert + list (SQLite, WAL) | < 2 ms |

Capture itself is I/O-bound and runs off the UI thread; STT/TTS latency is
engine-bound (SAPI ~ real time, faster-whisper CPU model-dependent) and is
therefore reported by the engine, never estimated by us.

## 7. Windows acceptance fixes included

1. **`test_worker_hard_timeout_kills_hung_tool_and_recovers`** (Phase 2 test):
   it hard-coded `python3`, which Windows does not ship, so the allowlist
   rejected the command and the timeout assertion never ran. The probe now
   resolves a bare, PATH-resolvable interpreter (`python.exe` on Windows) and
   skips with a clear message only if none exists.
2. **`test_live_snapshot_on_this_machine`** (Phase 6 product bug): process RSS
   was `None` on Windows. `psapi!GetProcessMemoryInfo` was the only entry point
   attempted, and HANDLE/buffer arguments were passed without explicit ctypes
   signatures, so 64-bit values were truncated to 32-bit `int`. The new
   `WindowsProcessApi` probes `K32GetProcessMemoryInfo` →
   `psapi!GetProcessMemoryInfo` → `kernel32!GetProcessMemoryInfo`, declares
   explicit `HANDLE`/`POINTER` signatures, caches the result, and is shared by
   the self-RSS read and the top-process scan (which additionally probes
   `K32EnumProcesses` before `psapi!EnumProcesses`).

## 8. Limitations (honest)

* **Wake-word detection is not implemented as a detector.** Phase 10 ships the
  *consent and plumbing* for a wake-word mode (second opt-in, mode-specific
  gating, state reporting) but no keyword-spotting engine: there is no honest
  offline KWS available without a model, so `wake_word_available` is reported as
  `false`. Push-to-talk is the working path.
* **Windows System.Speech dictation quality is modest**; it is real, offline and
  dependency-free, which is why it is the default. faster-whisper/whisper.cpp
  give better accuracy once installed.
* **No cloud TTS** yet, by design (no mandatory paid service).
* **Linux capture needs alsa-utils**; a headless container reports `none`.
* **Audio device enumeration on Linux** lists ALSA *cards*, not individual PCM
  endpoints.
* **macOS** is explicitly out of scope for this phase (Windows-first).

## 9. Tests

`tests/test_transformation_phase10_voice.py` — 20 tests, all passing in the
sandbox: consent gating, persisted enable across instances, wake-word second
opt-in, push-to-talk → transcript, audio discard by default, retention + purge,
silent capture, transcript-storage opt-in, cloud-consent gating, TTS
injection-safety (text never in argv, temp file deleted), unavailable-engine
reasons, emergency-stop blocking, single-capture enforcement, transcript
normalisation, engine-selection policy, honest discovery, CLI JSON, API
round-trip, diagnostics + release check, and the `none`-backend refusal.

## 10. Next tasks (Phase 11 preview)

Browser/desktop automation hardening and computer vision beyond the existing
gated tools: screenshot understanding with approval, element targeting
robustness, and bounded vision calls — all behind the same approval model.
