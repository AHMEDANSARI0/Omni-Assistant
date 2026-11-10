import { AICore, useCoreState } from "@/components/AICore";
import { Grid, KeyValue, KeyValuePanel, PageBody, PageHeader } from "@/views/common";
import { DataGate, Panel } from "@/components/Panel";
import { useResource } from "@/store/hooks";
import type {
  AgentStatsPayload,
  Health,
  KnowledgeStatsPayload,
  Overview,
  ProvidersPayload,
  SafetyPayload,
  ToolsPayload,
  VoicePayload,
} from "@/lib/types";

const STATE_SOURCE: Record<string, string> = {
  idle: "No signal claims work: /v1/health reports no running task or mission.",
  thinking: "A request from this UI is in flight and has not been answered yet.",
  executing: "/v1/health reports at least one running task or mission.",
  listening: "/v1/voice/status reports an open microphone capture session.",
  speaking: "A text-to-speech request is in flight.",
  error: "/v1/health reports the emergency stop engaged.",
  offline: "The last call to /v1/health failed.",
};

export function AICoreView() {
  const health = useResource<Health>("health");
  const overview = useResource<Overview>("overview");
  const tools = useResource<ToolsPayload>("tools");
  const agents = useResource<AgentStatsPayload>("agentStats");
  const providers = useResource<ProvidersPayload>("providers");
  const voice = useResource<VoicePayload>("voice");
  const knowledge = useResource<KnowledgeStatsPayload>("knowledge");
  const safety = useResource<SafetyPayload>("safety");
  const state = useCoreState();

  const capabilities = [
    { label: "Tools registered", value: overview.data?.tools ?? null, detail: "Deny-by-default registry; each tool declares its permissions." },
    { label: "Agents", value: agents.data?.agents.agents ?? null, detail: `${agents.data?.agents.agents_enabled ?? 0} enabled · ${agents.data?.agents.builtin_agents ?? 0} built in.` },
    { label: "Knowledge chunks", value: knowledge.data?.knowledge.chunks ?? null, detail: `Embedded with ${knowledge.data?.knowledge.active_embedding_model ?? "—"}${knowledge.data?.knowledge.embedding_local ? " (local)" : ""}.` },
    { label: "Model route", value: providers.data?.route.length ?? null, detail: `Route: ${providers.data?.route.join(" → ") || "—"}.` },
    {
      label: "Speech engines",
      value: voice.data ? Number(voice.data.stt.available) + Number(voice.data.tts.available) : null,
      detail: voice.data?.stt.available || voice.data?.tts.available ? "At least one local engine responded." : "No speech engine detected on this host.",
    },
    {
      label: "Emergency stop",
      value: safety.data ? (safety.data.emergency_stop.engaged ? 1 : 0) : null,
      detail: safety.data ? `Tool ceiling ${safety.data.max_tool_seconds}s · approvals expire after ${safety.data.approval_expiry_minutes} min.` : "",
    },
  ];

  return (
    <>
      <PageHeader
        title="AI Core"
        subtitle="The core visualises runtime state. Its state is derived only from signals the engine exposes — this page lists exactly which."
      />
      <PageBody>
        <div className="grid grid-cols-1 gap-2.5 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
          <Panel title="Core" subtitle="Particle shell · depth-sorted · pauses when hidden" className="min-h-[320px]">
            <AICore />
          </Panel>
          <div className="space-y-2.5">
            <Panel title="State" subtitle={`Current: ${state}`}>
              <KeyValue label="Derived from" value={STATE_SOURCE[state]} mono={false} />
              <KeyValue label="Engine status" value={health.data?.status ?? (health.error ? "unreachable" : "…")} />
              <KeyValue label="Running tasks" value={health.data?.running_tasks.length ?? 0} />
              <KeyValue label="Running missions" value={health.data?.running_missions.length ?? 0} />
              <KeyValue label="Microphone listening" value={voice.data?.microphone.listening ? "yes" : "no"} />
            </Panel>
            <Panel title="Capability matrix" subtitle="Read from the engine, not estimated">
              {capabilities.map((item) => (
                <div key={item.label} className="flex items-start justify-between gap-2 border-b border-line/30 py-1 last:border-0">
                  <div className="min-w-0">
                    <p className="text-[11.5px] text-ink">{item.label}</p>
                    <p className="text-[10px] leading-relaxed text-faint">{item.detail}</p>
                  </div>
                  <span className="shrink-0 font-mono text-[13px] text-accent">{item.value ?? "—"}</span>
                </div>
              ))}
            </Panel>
          </div>
        </div>
        <div className="mt-2.5">
          <Grid cols={2}>
            <Panel title="Tools by provenance" subtitle="Where each registered tool comes from">
              <DataGate state={tools} isEmpty={(data) => data.tools.length === 0} emptyDetail="No tool is registered in this install.">
                {(data) => {
                  const byProvenance = data.tools.reduce<Record<string, number>>((acc, tool) => {
                    acc[tool.provenance] = (acc[tool.provenance] ?? 0) + 1;
                    return acc;
                  }, {});
                  const byPermission = data.tools.reduce<Record<string, number>>((acc, tool) => {
                    for (const permission of tool.permissions) acc[permission] = (acc[permission] ?? 0) + 1;
                    return acc;
                  }, {});
                  return (
                    <div className="grid grid-cols-2 gap-3">
                      <div>
                        {Object.entries(byProvenance).map(([key, count]) => (
                          <KeyValue key={key} label={key} value={count} />
                        ))}
                      </div>
                      <div>
                        {Object.entries(byPermission).slice(0, 8).map(([key, count]) => (
                          <KeyValue key={key} label={key} value={count} />
                        ))}
                      </div>
                    </div>
                  );
                }}
              </DataGate>
            </Panel>
            <KeyValuePanel title="Health detail" subtitle="Verbatim from /v1/health" state={health}>
              {(data) => (
                <>
                  <KeyValue label="Service" value={data.service} />
                  <KeyValue label="Version" value={data.version} />
                  <KeyValue label="Schema" value={`${data.schema_version} / ${data.schema_latest}`} />
                  <KeyValue label="Active profile" value={data.active_profile ?? "none"} />
                  <KeyValue label="Emergency stop" value={data.emergency_stop_engaged ? "engaged" : "released"} />
                </>
              )}
            </KeyValuePanel>
          </Grid>
        </div>
      </PageBody>
    </>
  );
}
