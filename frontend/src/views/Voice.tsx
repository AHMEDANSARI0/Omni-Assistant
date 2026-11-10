import { useState } from "react";
import { Badge, DataGate, Panel } from "@/components/Panel";
import { Button, Grid, KeyValue, KeyValuePanel, PageBody, PageHeader, TextField } from "@/views/common";
import { useResource } from "@/store/hooks";
import { refresh } from "@/store/store";
import { withActivity } from "@/store/activity";
import { attempt } from "@/store/toast";
import { voiceCaptureCancel, voiceCaptureStart, voiceCaptureStop, voiceEnable, voicePurge, voiceSpeak, voiceWakeWord } from "@/lib/actions";
import { relative } from "@/lib/format";
import type { VoicePayload } from "@/lib/types";

export function Voice() {
  const voice = useResource<VoicePayload>("voice");
  const [text, setText] = useState("");
  const [transcript, setTranscript] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [capturing, setCapturing] = useState(false);

  const data = voice.data;

  return (
    <>
      <PageHeader
        title="Voice Assistant"
        subtitle="Push-to-talk by default, wake word only as a second opt-in. Engines are detected, not assumed — unavailable ones list the reason and the install step."
        actions={<Button onClick={() => void refresh("voice")}>Refresh</Button>}
        tone={data?.available ? "accent" : "warn"}
      />
      <PageBody>
        <Grid cols={2}>
          <div className="space-y-2.5">
            <Panel title="Microphone" subtitle="State from /v1/voice/status">
              <DataGate state={voice} onRetry={() => void refresh("voice")}>
                {(payload) => (
                  <>
                    <div className="mb-2 flex items-center justify-between">
                      <Badge tone={payload.microphone.enabled ? (payload.microphone.listening ? "ok" : "accent") : "line"}>
                        {payload.microphone.enabled ? payload.microphone.state : "disabled"}
                      </Badge>
                      <Button
                        tone={payload.microphone.enabled ? "line" : "ok"}
                        busy={busy === "enable"}
                        onClick={async () => {
                          setBusy("enable");
                          await attempt(payload.microphone.enabled ? "Microphone disabled" : "Microphone enabled", () => voiceEnable(!payload.microphone.enabled));
                          setBusy(null);
                          await refresh("voice");
                        }}
                      >
                        {payload.microphone.enabled ? "Disable microphone" : "Enable microphone"}
                      </Button>
                    </div>
                    <KeyValue label="Mode" value={payload.microphone.mode} />
                    <KeyValue label="Backend" value={payload.microphone.backend} />
                    <KeyValue label="Retention" value={payload.microphone.retention} />
                    <KeyValue label="Store transcripts" value={payload.microphone.store_transcripts ? "yes" : "no"} />
                    <KeyValue label="Cloud STT allowed" value={payload.microphone.cloud_stt_allowed ? "yes" : "no"} />
                    <KeyValue label="Max capture" value={`${payload.microphone.max_seconds}s`} />
                    <KeyValue label="Wake word" value={payload.microphone.wake_word_enabled ? payload.microphone.wake_word : "off"} />
                    {!payload.available ? (
                      <p className="mt-2 rounded-lg border border-warn/35 bg-warn/5 px-2 py-1.5 text-[10.5px] leading-relaxed text-warn">
                        {payload.microphone.backend_reason}
                      </p>
                    ) : null}
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      <Button
                        tone="ok"
                        disabled={!payload.available || !payload.microphone.enabled}
                        busy={busy === "start"}
                        onClick={async () => {
                          setBusy("start");
                          const started = await attempt("Capture started", () => voiceCaptureStart("push_to_talk"));
                          setBusy(null);
                          setCapturing(Boolean(started));
                          await refresh("voice");
                        }}
                        title={!payload.available ? payload.microphone.backend_reason : undefined}
                      >
                        Start capture
                      </Button>
                      <Button
                        busy={busy === "stop"}
                        disabled={!capturing}
                        onClick={async () => {
                          setBusy("stop");
                          const result = await attempt("Capture stopped", () => voiceCaptureStop({}));
                          setBusy(null);
                          setCapturing(false);
                          setTranscript(result && typeof result.transcript === "string" ? result.transcript : null);
                          await refresh("voice");
                        }}
                      >
                        Stop & transcribe
                      </Button>
                      <Button
                        disabled={!capturing}
                        onClick={async () => {
                          await attempt("Capture cancelled", () => voiceCaptureCancel());
                          setCapturing(false);
                          await refresh("voice");
                        }}
                      >
                        Cancel
                      </Button>
                      <Button
                        busy={busy === "wake"}
                        onClick={async () => {
                          setBusy("wake");
                          await attempt(payload.microphone.wake_word_enabled ? "Wake word off" : "Wake word armed", () =>
                            voiceWakeWord(!payload.microphone.wake_word_enabled),
                          );
                          setBusy(null);
                          await refresh("voice");
                        }}
                        title={payload.microphone.wake_word_available ? undefined : "Wake-word detection is not available with the detected engine"}
                      >
                        {payload.microphone.wake_word_enabled ? "Disarm wake word" : "Arm wake word"}
                      </Button>
                    </div>
                    {transcript !== null ? (
                      <div className="mt-2 rounded-lg border border-line/45 bg-inset/30 p-2">
                        <p className="hud-label">Transcript</p>
                        <p className="mt-0.5 text-[11.5px] text-ink">{transcript || "(empty)"}</p>
                      </div>
                    ) : null}
                  </>
                )}
              </DataGate>
            </Panel>

            <Panel title="Speak" subtitle="Uses the detected TTS engine; disabled when none exists">
              <div className="flex items-end gap-2">
                <div className="min-w-0 flex-1">
                  <TextField label="Text" value={text} onChange={setText} placeholder="Good evening. All systems nominal." onEnter={() => undefined} />
                </div>
                <Button
                  tone="violet"
                  busy={busy === "speak"}
                  disabled={!data?.tts.available || !text.trim()}
                  title={data?.tts.available ? undefined : data?.tts.reason}
                  onClick={async () => {
                    setBusy("speak");
                    await attempt("Speech request sent", () => withActivity("speaking", () => voiceSpeak(text.trim())));
                    setBusy(null);
                    setText("");
                    await refresh("voice");
                  }}
                >
                  Speak
                </Button>
              </div>
              {data && !data.tts.available ? <p className="mt-1 text-[10.5px] text-warn">TTS unavailable: {data.tts.reason}</p> : null}
            </Panel>
          </div>

          <div className="space-y-2.5">
            <Panel title="Engine matrix" subtitle="Each engine and why it is or is not usable here">
              <DataGate state={voice}>
                {(payload) => (
                  <>
                    {[...payload.stt.engines, ...payload.tts.engines].map((engine) => (
                      <div key={`${engine.kind}-${engine.id}`} className="border-b border-line/30 py-1.5 last:border-0">
                        <div className="flex items-center justify-between gap-2">
                          <p className="truncate text-[11.5px] text-ink">
                            <span className="font-mono text-[10px] text-accent">{engine.kind}</span> {engine.label}
                          </p>
                          <Badge tone={engine.available ? "ok" : "line"}>{engine.available ? "ready" : "unavailable"}</Badge>
                        </div>
                        {!engine.available ? <p className="mt-0.5 text-[10px] leading-relaxed text-faint">{engine.reason}</p> : null}
                        <p className="mt-0.5 text-[9.5px] text-faint/80">{engine.local ? "runs locally" : "cloud (per-call consent)"}</p>
                      </div>
                    ))}
                  </>
                )}
              </DataGate>
            </Panel>

            <KeyValuePanel title="Devices" subtitle="Enumerated with the platform tool" state={voice}>
              {(payload) => (
                <>
                  <KeyValue label="Input method" value={payload.input_devices.method} />
                  <KeyValue label="Input devices" value={payload.input_devices.count} />
                  <KeyValue label="Output method" value={payload.output_devices.method} />
                  <KeyValue label="Output devices" value={payload.output_devices.count} />
                  {!payload.input_devices.available ? <p className="mt-1 text-[10px] text-faint">{payload.input_devices.reason}</p> : null}
                  {!payload.output_devices.available ? <p className="mt-1 text-[10px] text-faint">{payload.output_devices.reason}</p> : null}
                </>
              )}
            </KeyValuePanel>

            <Panel title="Privacy & history" subtitle="Retention is a setting, not a default">
              <DataGate state={voice}>
                {(payload) => (
                  <>
                    <KeyValue label="Sessions" value={payload.sessions.total} />
                    <KeyValue label="Audio seconds" value={payload.sessions.audio_seconds.toFixed(1)} />
                    <KeyValue label="Transcript chars" value={payload.sessions.transcript_chars} />
                    <KeyValue label="Retained files" value={payload.sessions.retained} />
                    <KeyValue label="Last session" value={payload.sessions.last_at ? relative(payload.sessions.last_at) : "never"} />
                    <div className="mt-2 flex gap-1.5">
                      <Button tone="danger" onClick={async () => { await attempt("Audio purged", () => voicePurge()); await refresh("voice"); }}>
                        Purge audio
                      </Button>
                      <Button
                        onClick={async () => {
                          await attempt("History cleared", () =>
                            fetch("/v1/voice/clear", {
                              method: "POST",
                              headers: {
                                "Content-Type": "application/json",
                                Authorization: `Bearer ${document.querySelector<HTMLMetaElement>('meta[name="pc-agent-token"]')?.content ?? ""}`,
                              },
                              body: "{}",
                            }).then((response) => response.json()),
                          );
                          await refresh("voice");
                        }}
                      >
                        Clear history
                      </Button>
                    </div>
                  </>
                )}
              </DataGate>
            </Panel>
          </div>
        </Grid>
      </PageBody>
    </>
  );
}
