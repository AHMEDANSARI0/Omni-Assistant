/**
 * Dashboard widgets. Each one is a `Panel` + `DataGate`, so no widget can show
 * invented content: with no data it renders an explicit empty/error/unavailable
 * state and a retry that re-reads the endpoint.
 */
import { useState } from "react";
import { motion } from "framer-motion";
import { Badge, DataGate, Dot, Meter, Panel } from "@/components/Panel";
import { IconMic } from "@/components/icons";
import { Sparkline } from "@/components/Sparkline";
import { useResource } from "@/store/hooks";
import { ensure, refresh } from "@/store/store";
import type {
  AgentRunsPayload,
  AgentsPayload,
  AgentStatsPayload,
  FlowsPayload,
  Health,
  KnowledgeStatsPayload,
  MissionsPayload,
  Overview,
  ProvidersPayload,
  RemindersPayload,
  SystemPayload,
  TasksPayload,
  UsagePayload,
  VoicePayload,
} from "@/lib/types";
import { bps, mb, pct, relative, shorten } from "@/lib/format";
import { attempt, notify } from "@/store/toast";
import { flowTick, proactiveTick, runDueSchedules, voiceCaptureStart, voiceCaptureStop, voiceEnable } from "@/lib/actions";
import type { RouteId } from "@/nav";

const viewAll = (onNavigate: (id: RouteId) => void, id: RouteId, label: string) => (
  <button
    type="button"
    onClick={() => onNavigate(id)}
    className="rounded border border-line/60 px-1.5 py-[2px] text-[10px] text-muted transition hover:border-accent/50 hover:text-accent"
  >
    {label}
  </button>
);


/* ------------------------------------------------------------------ gauges */

/**
 * Radial gauge. `value` is a real measured percentage; when the host cannot
 * measure something (no GPU, no disk) pass `null` and the ring renders empty
 * with the reason underneath instead of pretending to be 0%.
 */
export function Gauge({
  label,
  value,
  note,
  tone = "accent",
  size = 62,
}: {
  label: string;
  value: number | null;
  note?: string;
  tone?: "accent" | "violet" | "warn" | "danger";
  size?: number;
}) {
  const radius = (size - 8) / 2;
  const circumference = 2 * Math.PI * radius;
  const clamped = value === null ? 0 : Math.max(0, Math.min(100, value));
  // Design tokens hold space-separated RGB channels, so a CSS `rgb()` wrapper is
  // required - `var(--accent)` on its own is not a valid SVG paint.
  const stroke = { accent: "accent", violet: "violet", warn: "warn", danger: "danger" }[tone];
  return (
    <div className="flex flex-col items-center gap-1">
      <div className="relative" style={{ width: size, height: size }}>
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={`${label}: ${value === null ? "not measured" : `${Math.round(clamped)} percent`}`}>
          <circle cx={size / 2} cy={size / 2} r={radius} fill="none" stroke="rgb(var(--line) / 0.55)" strokeWidth={4} />
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke={`rgb(var(--${stroke}))`}
            strokeWidth={4}
            strokeLinecap="round"
            strokeDasharray={`${(clamped / 100) * circumference} ${circumference}`}
            transform={`rotate(-90 ${size / 2} ${size / 2})`}
            style={{ transition: "stroke-dasharray 600ms ease" }}
          />
        </svg>
        <span className="absolute inset-0 grid place-items-center font-mono text-[11px] text-ink">
          {value === null ? "n/a" : `${Math.round(clamped)}%`}
        </span>
      </div>
      <span className="hud-label">{label}</span>
      {note ? <span className="max-w-[13ch] truncate text-center text-[9px] text-faint" title={note}>{note}</span> : null}
    </div>
  );
}

/* ------------------------------------------------------- core overview card */

/**
 * The reference layout's left column: one row per subsystem, each row's state
 * read from its own endpoint - never a hard-coded "online".
 */
export function CoreOverviewWidget({ onNavigate }: { onNavigate: (id: RouteId) => void }) {
  const health = useResource<Health>("health");
  const knowledge = useResource<KnowledgeStatsPayload>("knowledge");
  const voice = useResource<VoicePayload>("voice");
  const stats = useResource<AgentStatsPayload>("agentStats");
  const providers = useResource<ProvidersPayload>("providers");
  const overview = useResource<Overview>("overview");

  const rows: { label: string; value: string; tone: "ok" | "warn" | "danger" | "accent" | "line"; route: RouteId }[] = [
    {
      label: "AI core",
      value: health.data ? (health.data.status === "ok" ? "Nominal" : health.data.status) : "reading…",
      tone: health.data?.status === "ok" ? "ok" : health.error ? "danger" : "line",
      route: "ai-core",
    },
    {
      label: "Memory",
      value: knowledge.data ? `${knowledge.data.knowledge.memories.note} notes` : "reading…",
      tone: knowledge.data ? "accent" : "line",
      route: "memory",
    },
    {
      label: "Voice",
      value: voice.data ? (voice.data.available ? (voice.data.microphone.listening ? "Listening" : voice.data.microphone.enabled ? "Ready" : "Mic off") : "Unavailable") : "reading…",
      tone: voice.data ? (voice.data.available ? "ok" : "warn") : "line",
      route: "voice",
    },
    {
      label: "Agents",
      value: stats.data ? `${stats.data.agents.agents_enabled}/${stats.data.agents.agents} enabled` : "reading…",
      tone: stats.data ? "accent" : "line",
      route: "agents",
    },
    {
      label: "LLMs",
      value: providers.data ? (providers.data.route.length > 0 ? `${providers.data.route.length} in route` : "none configured") : "reading…",
      tone: providers.data ? (providers.data.providers.length > 0 ? "ok" : "warn") : "line",
      route: "models",
    },
    {
      label: "System",
      value: overview.data ? `${overview.data.tasks.total} tasks · ${overview.data.pending_approvals} awaiting approval` : "reading…",
      tone: overview.error ? "danger" : "line",
      route: "system",
    },
  ];

  return (
    <Panel title="AI core overview" subtitle="Every row reads its own endpoint" className="h-full">
      <ul className="space-y-1">
        {rows.map((row) => (
          <li key={row.label}>
            <button
              type="button"
              onClick={() => onNavigate(row.route)}
              className="flex w-full items-center gap-2 rounded-lg border border-line/40 bg-inset/30 px-2 py-1 text-start transition hover:border-accent/50"
            >
              <Dot tone={row.tone} />
              <span className="w-[54px] shrink-0 text-[10.5px] text-faint">{row.label}</span>
              <span className="min-w-0 flex-1 truncate text-[11px] text-ink">{row.value}</span>
            </button>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

/* ------------------------------------------------------------------ agents */

export function AgentsWidget({ onNavigate }: { onNavigate: (id: RouteId) => void }) {
  const agents = useResource<AgentsPayload>("agents");
  const stats = useResource<AgentStatsPayload>("agentStats");
  const runs = useResource<AgentRunsPayload>("agentRuns");
  return (
    <Panel
      title="Active agents"
      subtitle={stats.data ? `${stats.data.agents.agents_enabled}/${stats.data.agents.agents} enabled · ${stats.data.agents.agent_runs} runs recorded` : "Specialist roster"}
      actions={viewAll(onNavigate, "agents", "Studio")}
      className="h-full"
    >
      <DataGate state={agents} isEmpty={(data) => data.agents.length === 0} emptyDetail="No agent is registered in this install.">
        {() => (
          <div className="grid gap-1 sm:grid-cols-2">
            {(agents.data?.agents ?? []).slice(0, 6).map((agent) => (
              <div key={agent.name} className="flex items-center gap-2 rounded-lg border border-line/45 bg-inset/35 px-2 py-1.5">
                <Dot tone={agent.enabled ? "ok" : "line"} />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[11.5px] font-medium text-ink">{agent.name}</p>
                  <p className="truncate text-[10px] text-faint">{agent.role}</p>
                </div>
                <span className="shrink-0 font-mono text-[9.5px] text-faint">{agent.allowed_tools.length} tools</span>
              </div>
            ))}
            {(agents.data?.agents?.length ?? 0) > 6 ? (
              <p className="col-span-full text-[10.5px] text-faint">
                +{(agents.data?.agents.length ?? 0) - 6} more in Agent Studio
              </p>
            ) : null}
            {runs.data && runs.data.runs.length > 0 ? (
              <p className="col-span-full text-[10.5px] text-faint">Latest run: {shorten(String(runs.data.runs[0].agent ?? "agent"), 40)} · {relative(String(runs.data.runs[0].started_at ?? ""))}</p>
            ) : (
              <p className="col-span-full text-[10.5px] text-faint">No agent run recorded yet.</p>
            )}
          </div>
        )}
      </DataGate>
    </Panel>
  );
}

/* ----------------------------------------------------------------- missions */

export function TimelineWidget({ onNavigate }: { onNavigate: (id: RouteId) => void }) {
  const missions = useResource<MissionsPayload>("missions");
  return (
    <Panel title="Mission timeline" subtitle="Live progress by mission" actions={viewAll(onNavigate, "missions", "All")} className="h-full">
      <DataGate
        state={missions}
        isEmpty={(data) => data.missions.length === 0}
        emptyDetail="Create a mission to break a goal into ordered tasks. Progress here is computed from the task tree, not estimated."
        onRetry={() => void refresh("missions")}
      >
        {(data) => (
          <ol className="space-y-2">
            {data.missions.slice(0, 4).map((mission) => {
              const done = Object.entries(mission.progress)
                .filter(([key]) => key === "completed" || key === "succeeded")
                .reduce((sum, [, value]) => sum + Number(value || 0), 0);
              const total = mission.progress.total || 1;
              const ratio = Math.min(100, (done / total) * 100);
              return (
                <li key={mission.id}>
                  <div className="flex items-center justify-between gap-2">
                    <p className="truncate text-[11.5px] font-medium text-ink">{mission.name}</p>
                    <Badge tone={mission.status === "completed" ? "ok" : mission.status === "failed" ? "danger" : "accent"}>{mission.status}</Badge>
                  </div>
                  <div className="mt-1">
                    <Meter value={ratio} label={`${mission.name} progress`} />
                  </div>
                  <p className="mt-0.5 text-[10px] text-faint">
                    {done}/{mission.progress.total} tasks · updated {relative(mission.updated_at)}
                  </p>
                </li>
              );
            })}
          </ol>
        )}
      </DataGate>
    </Panel>
  );
}

/* ----------------------------------------------------------- quick commands */

export function QuickCommands({ onNavigate }: { onNavigate: (id: RouteId) => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const health = useResource<Health>("health");

  const run = async (key: string, label: string, work: () => Promise<unknown>) => {
    setBusy(key);
    const result = await attempt(label, work);
    if (result !== null) {
      await refresh("tasks");
      await refresh("events");
      await refresh("schedules");
      await refresh("flows");
    }
    setBusy(null);
  };

  const buttons = [
    {
      key: "schedules",
      label: "Run due schedules",
      hint: "POST /v1/schedules/run-due",
      work: () => runDueSchedules(),
    },
    { key: "flows", label: "Fire due flow triggers", hint: "POST /v1/flows/tick", work: () => flowTick() },
    { key: "proactive", label: "Proactive intelligence tick", hint: "POST /v1/proactive/tick", work: () => proactiveTick() },
  ];

  return (
    <Panel title="Quick commands" subtitle="Each button calls one documented endpoint" className="h-full">
      <div className="space-y-1.5">
        {buttons.map((button) => (
          <button
            key={button.key}
            type="button"
            disabled={busy !== null}
            onClick={() => void run(button.key, button.label, button.work)}
            className="flex w-full items-center justify-between gap-2 rounded-lg border border-line/55 bg-inset/35 px-2 py-1.5 text-start transition hover:border-accent/50 disabled:opacity-60"
          >
            <span className="min-w-0">
              <span className="block truncate text-[11.5px] font-medium text-ink">{button.label}</span>
              <span className="block truncate font-mono text-[9.5px] text-faint">{button.hint}</span>
            </span>
            {busy === button.key ? <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" /> : <Dot tone="accent" />}
          </button>
        ))}
        <button
          type="button"
          onClick={() => onNavigate("tasks")}
          className="w-full rounded-lg border border-accent/40 bg-accent/8 px-2 py-1.5 text-[11.5px] font-medium text-accent transition hover:bg-accent/15"
        >
          Open task queue
        </button>
        <p className="pt-0.5 text-[10px] text-faint">
          Engine {health.data?.status ?? "unknown"} · schema {health.data?.schema_version ?? "—"}
        </p>
      </div>
    </Panel>
  );
}

/* ---------------------------------------------------------- system monitor */

export function MonitorWidget({ compact = true, onNavigate }: { compact?: boolean; onNavigate?: (id: RouteId) => void }) {
  const system = useResource<SystemPayload>("system");
  return (
    <Panel
      title="System monitor"
      subtitle="Measured on this machine, no estimates"
      actions={onNavigate ? viewAll(onNavigate, "system", "Details") : undefined}
      className="h-full"
      dense={compact}
    >
      <DataGate state={system} onRetry={() => void refresh("system")}>
        {(data) => {
          const snapshot = data.snapshot;
          const installDisk = snapshot.disks.install ?? Object.values(snapshot.disks)[0];
          const history = data.history ?? [];
          const memorySeries = history.map((sample) => sample.memory_percent);
          const cpuSeries = history.map((sample) => sample.cpu_percent);
          return (
            <div className="space-y-2">
              <div className="grid grid-cols-4 gap-1">
                <Gauge label="CPU" value={snapshot.cpu.percent} note={snapshot.cpu.percent === null ? snapshot.cpu.method : `${snapshot.cpu.logical_cores} cores`} />
                <Gauge label="RAM" value={snapshot.memory.percent} tone="violet" note={mb(snapshot.memory.used_bytes / 1024 / 1024)} />
                <Gauge
                  label="Disk"
                  value={installDisk ? installDisk.percent : null}
                  tone={installDisk && installDisk.percent > 90 ? "danger" : installDisk && installDisk.percent > 75 ? "warn" : "accent"}
                  note={installDisk ? `${mb(installDisk.free_bytes / 1024 / 1024)} free` : "no data"}
                />
                <Gauge
                  label="GPU"
                  value={snapshot.gpu.available ? snapshot.gpu.utilization_percent : null}
                  tone="violet"
                  note={snapshot.gpu.available ? "measured" : "absent"}
                />
              </div>
              <div className="grid grid-cols-2 gap-1.5">
                <div className="rounded-lg border border-line/40 bg-inset/25 px-1.5 py-1">
                  <p className="hud-label">CPU history</p>
                  <Sparkline values={cpuSeries} height={20} label="CPU history" />
                </div>
                <div className="rounded-lg border border-line/40 bg-inset/25 px-1.5 py-1">
                  <p className="hud-label">Memory history</p>
                  <Sparkline values={memorySeries} height={20} tone="violet" label="Memory history" />
                </div>
              </div>
              <div className="flex items-center justify-between rounded-lg border border-line/45 bg-inset/30 px-2 py-1">
                <span className="text-[10.5px] text-faint">Network</span>
                <span className="font-mono text-[10.5px] text-muted">
                  ↓ {bps(snapshot.network.receive_bytes_per_second)} · ↑ {bps(snapshot.network.send_bytes_per_second)}
                </span>
              </div>
              {snapshot.top_processes && snapshot.top_processes.length > 0 ? (
                <div className="space-y-0.5">
                  <p className="hud-label">Top memory</p>
                  {snapshot.top_processes.slice(0, 3).map((process) => (
                    <div key={`${process.pid}-${process.name}`} className="flex items-center justify-between text-[10.5px]">
                      <span className="truncate text-muted">
                        {process.name} <span className="font-mono text-faint/70">{process.pid}</span>
                      </span>
                      <span className="font-mono text-faint">{mb(process.rss_bytes / 1024 / 1024)}</span>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          );
        }}
      </DataGate>
    </Panel>
  );
}

/* ---------------------------------------------------------------- memory */

export function MemoryWidget({ onNavigate }: { onNavigate: (id: RouteId) => void }) {
  const knowledge = useResource<KnowledgeStatsPayload>("knowledge");
  return (
    <Panel title="Memory insights" subtitle="Local embeddings, no cloud call" actions={viewAll(onNavigate, "memory", "Recall")} className="h-full">
      <DataGate state={knowledge} onRetry={() => void refresh("knowledge")}>
        {(data) => {
          const stats = data.knowledge;
          return (
            <div className="space-y-1.5">
              <div className="grid grid-cols-3 gap-1.5 text-center">
                <Stat label="Docs" value={stats.documents} />
                <Stat label="Chunks" value={stats.chunks} />
                <Stat label="Notes" value={stats.memories.note + stats.memories.workflow} />
              </div>
              <div className="rounded-lg border border-line/45 bg-inset/30 px-2 py-1">
                <p className="text-[10.5px] text-faint">Embedding model</p>
                <p className="truncate font-mono text-[10.5px] text-ink">{stats.active_embedding_model}</p>
                <p className="mt-0.5 text-[9.5px] text-faint">{stats.embedding_local ? "runs locally" : "uses a remote model"}</p>
              </div>
              <button
                type="button"
                onClick={() => onNavigate("memory")}
                className="w-full rounded-lg border border-line/55 px-2 py-1 text-[10.5px] text-muted transition hover:border-accent/50 hover:text-accent"
              >
                Search memory
              </button>
            </div>
          );
        }}
      </DataGate>
    </Panel>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border border-line/45 bg-inset/30 px-1 py-1">
      <p className="font-mono text-[13px] text-ink">{value}</p>
      <p className="hud-label mt-0.5">{label}</p>
    </div>
  );
}

/* -------------------------------------------------------------- providers */

export function ProvidersWidget({ onNavigate }: { onNavigate: (id: RouteId) => void }) {
  const providers = useResource<ProvidersPayload>("providers");
  const usage = useResource<UsagePayload>("usage");
  return (
    <Panel title="Model status" subtitle="Routing decided by the engine" actions={viewAll(onNavigate, "models", "Center")} className="h-full">
      <DataGate state={providers} onRetry={() => void refresh("providers")}>
        {(data) => (
          <div className="space-y-1.5">
            <div className="flex flex-wrap items-center gap-1">
              {data.route.length === 0 ? (
                <span className="text-[11px] text-faint">No provider in the route.</span>
              ) : (
                data.route.map((slot, index) => (
                  <span key={slot} className="flex items-center gap-1">
                    {index > 0 ? <span className="text-faint/60">→</span> : null}
                    <Badge tone={slot === "mock" ? "warn" : "accent"}>{slot}</Badge>
                  </span>
                ))
              )}
            </div>
            <div className="grid grid-cols-2 gap-1">
              {data.providers.length === 0 ? (
                <p className="col-span-full rounded-lg border border-line/40 bg-inset/25 px-2 py-1 text-[10px] text-warn">
                  No cloud provider is configured. The offline mock engine answered the last call.
                </p>
              ) : (
                data.providers.slice(0, 8).map((provider) => {
                  const name = String(provider.slot ?? provider.name ?? "provider");
                  const available = provider.available !== false;
                  return (
                    <div key={name} className="flex items-center gap-1.5 rounded-lg border border-line/40 bg-inset/25 px-1.5 py-1">
                      <Dot tone={available ? "ok" : "warn"} />
                      <div className="min-w-0">
                        <p className="truncate text-[10.5px] text-ink">{name}</p>
                        <p className="truncate text-[9px] text-faint">
                          {available ? `${provider.models?.length ?? 0} models` : "not reachable"}
                        </p>
                      </div>
                    </div>
                  );
                })
              )}
            </div>
            <p className="text-[10.5px] text-faint">
              Strategy <span className="font-mono text-muted">{data.strategy}</span> · {data.providers.length} configured
            </p>
            {usage.data ? (
              <div className="rounded-lg border border-line/45 bg-inset/30 px-2 py-1 text-[10.5px]">
                <p className="text-faint">
                  Calls <span className="text-ink">{usage.data.usage.calls}</span> · success{" "}
                  <span className="text-ink">{usage.data.usage.success_rate === null ? "—" : pct(usage.data.usage.success_rate, 1)}</span>
                </p>
                <p className="mt-0.5 text-faint">
                  Tokens in/out <span className="font-mono text-muted">{usage.data.usage.input_tokens}/{usage.data.usage.output_tokens}</span>
                </p>
                {usage.data.analytics_enabled ? null : <p className="mt-0.5 text-warn">Usage analytics disabled in settings.</p>}
              </div>
            ) : null}
            {data.providers.length === 0 ? (
              <p className="text-[10px] text-warn">Only the offline mock engine is available; add a provider key in the CLI to route to a cloud model.</p>
            ) : null}
          </div>
        )}
      </DataGate>
    </Panel>
  );
}

/* ------------------------------------------------------------------ voice */

export function VoiceActivityWidget({ onNavigate }: { onNavigate: (id: RouteId) => void }) {
  const voice = useResource<VoicePayload>("voice");
  const reminders = useResource<RemindersPayload>("reminders");
  const [capturing, setCapturing] = useState(false);
  const [transcript, setTranscript] = useState<string | null>(null);
  const mic = voice.data?.microphone;

  const toggle = async () => {
    if (!voice.data) return;
    if (!voice.data.available) {
      notify("error", "Voice unavailable", voice.data.microphone.backend_reason || "No microphone backend on this host.");
      return;
    }
    if (!mic?.enabled) {
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
      if (started) {
        setTranscript(null);
        setCapturing(true);
      }
      return;
    }
    const finished = await attempt("Capture stopped", async () => {
      const payload = await voiceCaptureStop({});
      await refresh("voice");
      return payload;
    });
    setCapturing(false);
    if (finished && typeof finished.transcript === "string" && finished.transcript.trim()) {
      setTranscript(finished.transcript.trim());
    } else if (finished) {
      notify("info", "Capture closed", "The engine returned no transcript for this session.");
    }
  };

  const disabledReason = !voice.data
    ? "Reading microphone state…"
    : !voice.data.available
      ? voice.data.microphone.backend_reason || "No microphone backend on this host."
      : "";

  return (
    <Panel
      title="Voice & attention"
      subtitle="Push-to-talk, engines, and open reminders"
      actions={viewAll(onNavigate, "voice", "Voice")}
      className="h-full"
    >
      <DataGate state={voice} onRetry={() => void refresh("voice")}>
        {(data) => (
          <div className="flex items-start gap-3">
            <div className="flex flex-col items-center gap-1">
              <button
                type="button"
                onClick={() => void toggle()}
                disabled={Boolean(disabledReason)}
                title={disabledReason || (capturing ? "Stop and transcribe" : mic?.enabled ? "Start push-to-talk" : "Enable the microphone")}
                aria-label={capturing ? "Stop capture and transcribe" : "Start push-to-talk capture"}
                className={`grid h-14 w-14 place-items-center rounded-full border transition ${
                  capturing
                    ? "border-ok/70 bg-ok/15 text-ok"
                    : disabledReason
                      ? "border-line/50 text-faint/60"
                      : "border-accent/50 bg-accent/10 text-accent hover:bg-accent/20"
                }`}
              >
                <IconMic size={20} />
              </button>
              <span className="hud-label text-center">{capturing ? "stop" : mic?.enabled ? "mic ready" : "mic off"}</span>
              {capturing ? (
                <div className="flex h-3 items-end gap-[2px]" aria-label="Capture session open">
                  {[0, 1, 2, 3, 4, 5, 6].map((index) => (
                    <motion.span
                      key={index}
                      className="w-[2px] rounded-full bg-ok"
                      animate={{ height: [4, 11, 6, 13, 5] }}
                      transition={{ duration: 1.2, repeat: Infinity, delay: index * 0.1, ease: "easeInOut" }}
                    />
                  ))}
                </div>
              ) : null}
            </div>
            <div className="grid min-w-0 flex-1 gap-1.5 sm:grid-cols-2">
              <div className="rounded-lg border border-line/45 bg-inset/30 px-2 py-1.5">
                <p className="hud-label">Microphone</p>
                <p className="mt-0.5 flex items-center gap-1.5 text-[11.5px] text-ink">
                  <Dot tone={data.microphone.listening ? "ok" : data.microphone.enabled ? "accent" : "line"} pulse={data.microphone.listening} />
                  {data.microphone.enabled ? data.microphone.state : "disabled"}
                </p>
                <p className="mt-0.5 truncate text-[9.5px] text-faint">
                  {data.microphone.mode} · retains audio: {data.microphone.retention}
                </p>
              </div>
              <div className="rounded-lg border border-line/45 bg-inset/30 px-2 py-1.5">
                <p className="hud-label">Speech engines</p>
                <p className={`mt-0.5 truncate text-[11px] ${data.stt.available ? "text-ok" : "text-warn"}`}>
                  STT {data.stt.available ? data.stt.label : "unavailable"}
                </p>
                <p className={`truncate text-[11px] ${data.tts.available ? "text-ok" : "text-warn"}`}>
                  TTS {data.tts.available ? data.tts.label : "unavailable"}
                </p>
              </div>
              <div className="rounded-lg border border-line/45 bg-inset/30 px-2 py-1.5 sm:col-span-2">
                <p className="hud-label">Sessions</p>
                <p className="mt-0.5 text-[11px] text-ink">
                  <span className="font-mono">{data.sessions.total}</span> total ·{" "}
                  <span className="font-mono">{data.sessions.audio_seconds.toFixed(1)}s</span> audio ·{" "}
                  {reminders.data?.total ?? 0} reminders open
                </p>
                {transcript ? (
                  <div className="mt-1 rounded border border-ok/40 bg-ok/10 px-1.5 py-1">
                    <p className="text-[10.5px] text-ink">{transcript}</p>
                    <button
                      type="button"
                      onClick={() => window.dispatchEvent(new CustomEvent("omni:compose", { detail: transcript }))}
                      className="mt-1 rounded border border-accent/50 px-1.5 py-[1px] text-[10px] text-accent transition hover:bg-accent/15"
                    >
                      Put in command bar
                    </button>
                  </div>
                ) : null}
                {disabledReason ? <p className="mt-0.5 text-[9.5px] text-warn">{disabledReason}</p> : null}
              </div>
            </div>
          </div>
        )}
      </DataGate>
    </Panel>
  );
}

/* ------------------------------------------------------------ flows widget */

export function FlowsWidget({ onNavigate }: { onNavigate: (id: RouteId) => void }) {
  const flows = useResource<FlowsPayload>("flows");
  const tasks = useResource<TasksPayload>("tasks");
  return (
    <Panel title="Workflows & queue" subtitle="Flows and queued work" actions={viewAll(onNavigate, "workflows", "Builder")} className="h-full">
      <DataGate state={flows} onRetry={() => void refresh("flows")}>
        {(data) => (
          <div className="space-y-1">
            <p className="text-[11px] text-muted">
              {data.summary.flows} flows · {data.summary.triggers_enabled} triggers · {data.summary.runs} runs
            </p>
            <p className="text-[10.5px] text-faint">
              Queue: {tasks.data?.total ?? 0} tasks ({Object.entries(tasks.data?.tasks.reduce<Record<string, number>>((acc, task) => {
                acc[task.status] = (acc[task.status] ?? 0) + 1;
                return acc;
              }, {}) ?? {}).map(([status, count]) => `${count} ${status}`).join(", ") || "empty"})
            </p>
            {data.summary.last_run_at ? <p className="text-[10px] text-faint">Last flow run {relative(data.summary.last_run_at)}</p> : null}
          </div>
        )}
      </DataGate>
    </Panel>
  );
}

/** Kicks lazy, zero-polling resources the dashboard footer displays. */
export function primeWidgetData() {
  ensure("agentRuns");
  ensure("usage");
  ensure("reminders");
}
