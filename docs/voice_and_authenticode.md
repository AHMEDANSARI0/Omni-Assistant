# Voice assistant completion and Authenticode signing (0.81.0)

This page records what 0.81.0 added on top of Transformation Phase 18, why it was
still missing, and what it still does not do.

## 1. What already existed

* `pc_agent.voice` (Phase 10): privacy state machine, push-to-talk capture
  (`winmm` on Windows, `arecord` on Linux), speech-to-text engine discovery with
  optional per-call cloud consent, text-to-speech, a session ledger, purge
  controls, and honest `available: false` reporting when a host has no device or
  engine.
* Signed updates (Phase 21 onward): Ed25519-signed bundles, verification, staging,
  apply and rollback, feed publishing, HTTPS hosting, auto-upgrade scheduling, and
  Inno Setup / Debian / RPM / POSIX packaging plus installer acceptance.

## 2. What was genuinely missing

1. **Nothing turned a transcript into an action.** Transcription produced text;
   the user still had to copy it. "Full voice assistant" was therefore a claim
   about components, not behaviour.
2. **No hands-free session.** Only one-shot push-to-talk existed; there was no
   bounded listen/act/speak loop, no visible state machine for it, and no
   single-call stop.
3. **No acceptance artefact for voice.** Contrast was measured, the release chain
   was measured, but voice could not be checked on the machine that would use it.
4. **The installer EXE had no Authenticode signature path.** The update bundle was
   signed and enforced, but SmartScreen-grade signing of the EXE was simply not
   implemented.

## 3. What 0.81.0 adds

| Area | Implementation |
| --- | --- |
| Intent routing | `voice/intents.py`: deterministic router, 9 intents, wake phrase stripped only at the start |
| Execution | `VoiceIntentExecutor`: notes -> memory, reminders -> proactive engine, free text -> `create_task(source="voice")` (approvals unchanged) |
| Hands-free loop | `voice/listener.py`: bounded cycles, states, immediate `stop()`, per-cycle records |
| Acceptance | `voice/acceptance.py`: 7 real gates, sealed report, `passed` / `incomplete` / `failed` |
| CLI | `voice intent`, `voice loop`, `voice acceptance`, `voice acceptance-verify` |
| API | `POST /v1/voice/intent`, `GET /v1/voice/acceptance` |
| Authenticode | `windows_signing.py`: signtool discovery, thumbprint-first signing, `WinVerifyTrust` verification, PFX fallback with an explicit exposure flag |
| CLI | `release signing`, `release sign`, `release signature` |
| Audit | check **33** `windows_authenticode`; `Hardening:` doctor line gains `authenticode=` |

## 4. Measured on the development host (Linux, no audio hardware)

```
pc-agent voice acceptance
INCOMPLETE; gates=7; passed=4; failures=0; not-run=push_to_talk,stt_engine,tts_engine
```

That is the intended output: the four gates that do not need hardware passed, and
the three that do were reported as skipped rather than passed.

On a Windows machine with a microphone and SAPI the same command prints `PASSED`.
Since 0.82.1 that pass is only granted when the gate found a capture backend **and**
at least one enumerated input device - a host whose backend module exists but whose
device scan returns nothing is reported as `not-run` with the scan reason. The
`push_to_talk` detail states what was actually proved (`presence only - no audio was
recorded by this gate`), so nothing in the report implies a recording that did not
happen.

## 5. Honest limitations

* Wake-phrase detection is **transcript-based** (one bounded capture per cycle),
  not a low-power always-on acoustic detector. A dedicated detector is future
  work; pretending otherwise would waste battery and lie about latency.
* The loop is single-session and single-user; there is no diarisation and no
  speaker identification.
* Intent routing is rule-based by design. Anything unrecognised becomes an
  ordinary task, which means the same approval rules apply - voice is not a
  privilege escalation path.
* Authenticode verification only answers on Windows. Off Windows the answer is
  `unverifiable`, which is why the audit reports "unsigned" as a fact instead of
  claiming a verification it cannot perform.
* A PFX password can only reach `signtool` through `argv`; that mode is refused
  unless `--allow-argv-password` is passed, and the refusal message explains why.
  Thumbprint mode has no such exposure and is the documented default.
* No code-signing key is generated, stored or bundled by this product.
