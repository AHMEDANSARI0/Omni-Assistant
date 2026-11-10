import { useEffect, useMemo, useRef, useState } from "react";
import { motion } from "framer-motion";
import { IconMic, IconSend, IconStop } from "@/components/icons";
import { useResource } from "@/store/hooks";
import { refresh } from "@/store/store";
import { withActivity } from "@/store/activity";
import { attempt, notify } from "@/store/toast";
import {
  runInstruction,
  safetyStop,
  voiceCaptureCancel,
  voiceCaptureStart,
  voiceCaptureStop,
  voiceEnable,
} from "@/lib/actions";
import type { Health, SafetyPayload, TasksPayload, VoicePayload } from "@/lib/types";
import { useCoreState } from "@/components/AICore";
import { useUi } from "@/store/ui";

/**
 * Bottom command bar.
 *
 * The text field is the real input to `POST /v1/run`; the mic controls the real
 * capture session and is disabled with its reason when the host has no
 * microphone backend; stop engages the real safety-kernel emergency stop.
 * The "waveform" only ever indicates that a capture session is open - the API
 * does not expose an audio level, so no fake level is drawn.
 */
export function CommandBar() {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [capturing, setCapturing] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const voice = useResource<VoicePayload>("voice");
  const safety = useResource<SafetyPayload>("safety");
  const tasks = useResource<TasksPayload>("tasks");
  const health = useResource<Health>("health");
  const state = useCoreState();
  const { ui } = useUi();

  // The voice widget can hand a real transcript over to this input.
  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<string>).detail;
      if (typeof detail === "string" && detail.trim()) {
        setText(detail.trim());
        inputRef.current?.focus();
      }
    };
    window.addEventListener("omni:compose", handler);
    return () => window.removeEventListener("omni:compose", handler);
  }, []);

  const micReason = useMemo(() => {
    if (!voice.data) return "Reading microphone state…";
    if (!voice.data.available) return voice.data.microphone.backend_reason || "No microphone backend on this host.";
    if (!voice.data.microphone.enabled) return "Microphone is off. Enable it in Voice Assistant.";
    return "";
  }, [voice.data]);

  const stopped = safety.data?.emergency_stop.engaged === true;

  const submit = async () => {
    const instruction = text.trim();
    if (!instruction || busy) return;
    if (stopped) {
      notify("error", "Engine stopped", "Release the emergency stop before sending new work.");
      return;
    }
    setBusy(true);
    const result = await attempt("Instruction queued", () => withActivity("thinking", () => runInstruction(instruction)));
    setBusy(false);
    if (result) {
      setText("");
      await refresh("tasks");
      await refresh("events");
    }
  };

  const toggleMic = async () => {
    if (!voice.data) return;
    if (!voice.data.available) return;
    if (!voice.data.microphone.enabled) {
      await attempt("Microphone enabled", async () => {
        const payload = await voiceEnable(true);
        await refresh("voice");
        return payload;
      });
      return;
    }
    if (!capturing) {
      const started = await attempt("Capture started", async () => {
        const payload = await voiceCaptureStart("push_to_talk");
        await refresh("voice");
        return payload;
      });
      if (started) setCapturing(true);
      return;
    }
    const finished = await attempt("Capture stopped", async () => {
      const payload = await voiceCaptureStop({});
      await refresh("voice");
      return payload;
    });
    setCapturing(false);
    if (finished && typeof finished.transcript === "string" && finished.transcript.trim()) {
      setText(finished.transcript.trim());
    } else if (finished) {
      notify("info", "Capture closed", "The engine returned no transcript for this session.");
    }
  };

  const cancelCapture = async () => {
    await attempt("Capture cancelled", async () => {
      const payload = await voiceCaptureCancel();
      await refresh("voice");
      return payload;
    });
    setCapturing(false);
  };

  const queue = tasks.data?.tasks ?? [];
  const done = queue.filter((task) => task.status === "completed" || task.status === "succeeded").length;
  const failed = queue.filter((task) => task.status === "failed").length;
  const ratio = queue.length === 0 ? 0 : (done / queue.length) * 100;

  return (
    <footer className="relative z-20 shrink-0 border-t border-line/45 bg-elevated/70 px-3 py-2 backdrop-blur-xl">
      <div className="flex items-center gap-2">
        <div className="relative flex min-w-0 flex-1 items-center gap-2 rounded-xl border border-line/60 bg-inset/55 px-2.5 py-1.5 transition focus-within:border-accent/60">
          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${stopped ? "bg-danger" : "bg-accent"}`} aria-hidden />
          <input
            ref={inputRef}
            value={text}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void submit();
              } else if (event.key === "Escape") {
                setText("");
              }
            }}
            placeholder={stopped ? "Emergency stop engaged — release it in Security Center" : "Tell Omni what to do…  (Enter to queue, Esc to clear)"}
            className="h-7 min-w-0 flex-1 bg-transparent text-[12.5px] text-ink placeholder:text-faint focus:outline-none"
            aria-label="Instruction input"
            disabled={stopped}
          />
          {capturing ? (
            <div className="flex shrink-0 items-end gap-[2px]" aria-label="Capture session open">
              {[0, 1, 2, 3, 4].map((index) => (
                <motion.span
                  key={index}
                  className="w-[2px] rounded-full bg-ok"
                  animate={ui.motion === "reduce" ? { height: 6 } : { height: [4, 12, 6, 14, 5] }}
                  transition={{ duration: 1.1, repeat: Infinity, delay: index * 0.12, ease: "easeInOut" }}
                />
              ))}
            </div>
          ) : null}
          <button
            type="button"
            onClick={() => void submit()}
            disabled={busy || !text.trim() || stopped}
            className="flex shrink-0 items-center gap-1.5 rounded-lg border border-accent/45 bg-accent/12 px-2 py-1 text-[11px] font-medium text-accent transition hover:bg-accent/20 disabled:opacity-45"
            title="Queue instruction (POST /v1/run)"
          >
            <IconSend size={12} /> {busy ? "Sending" : "Send"}
          </button>
        </div>

        <button
          type="button"
          onClick={() => void toggleMic()}
          disabled={Boolean(micReason) && !voice.data?.microphone.enabled}
          title={micReason || (capturing ? "Stop capture and get a transcript" : "Start push-to-talk capture")}
          className={`grid h-9 w-9 shrink-0 place-items-center rounded-xl border transition ${
            capturing
              ? "border-ok/60 bg-ok/15 text-ok"
              : micReason
                ? "border-line/50 text-faint/60"
                : "border-line/60 text-muted hover:border-accent/50 hover:text-accent"
          }`}
          aria-label="Microphone"
        >
          <IconMic size={15} />
        </button>

        {capturing ? (
          <button
            type="button"
            onClick={() => void cancelCapture()}
            className="h-9 shrink-0 rounded-xl border border-line/60 px-2 text-[10.5px] text-faint transition hover:border-warn/50 hover:text-warn"
            title="Cancel capture without transcribing"
          >
            Cancel
          </button>
        ) : null}

        <button
          type="button"
          onClick={async () => {
            if (stopped) {
              notify("info", "Already stopped", "The emergency stop is engaged.");
              return;
            }
            await attempt("Emergency stop engaged", async () => {
              const payload = await safetyStop("Engaged from the command bar.");
              await refresh("safety");
              await refresh("health");
              return payload;
            });
          }}
          className={`grid h-9 w-9 shrink-0 place-items-center rounded-xl border transition ${
            stopped ? "border-danger/60 bg-danger/15 text-danger" : "border-line/60 text-muted hover:border-danger/60 hover:text-danger"
          }`}
          title={stopped ? "Emergency stop is engaged" : "Engage emergency stop"}
          aria-label="Stop all work"
        >
          <IconStop size={14} />
        </button>
      </div>

      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-faint">
        <span className="flex items-center gap-1.5">
          <span className={`h-1.5 w-1.5 rounded-full ${state === "error" || state === "offline" ? "bg-danger" : state === "idle" ? "bg-ok" : "bg-accent"}`} />
          State <span className="text-muted">{state}</span>
        </span>
        <span>
          Queue <span className="font-mono text-muted">{done}/{queue.length} done</span>
          {failed > 0 ? <span className="text-danger"> · {failed} failed</span> : null}
        </span>
        {queue.length > 0 ? (
          <span className="relative hidden h-1 w-28 overflow-hidden rounded-full bg-inset sm:block" aria-label="Task completion">
            <span className="absolute inset-y-0 start-0 rounded-full bg-accent transition-[width] duration-500" style={{ width: `${ratio}%` }} />
          </span>
        ) : null}
        <span className="ms-auto flex items-center gap-2">
          {micReason ? <span className="max-w-[46ch] truncate text-warn" title={micReason}>Mic: {micReason}</span> : <span className="text-ok">Mic ready</span>}
          <span className="hidden md:inline">Engine {health.data?.version ?? "—"} · schema {health.data?.schema_version ?? "—"}</span>
        </span>
      </div>
    </footer>
  );
}
