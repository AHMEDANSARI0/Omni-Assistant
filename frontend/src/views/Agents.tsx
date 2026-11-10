import { useEffect, useState } from "react";
import { Badge, DataGate, Panel } from "@/components/Panel";
import { Button, Grid, KeyValue, PageBody, PageHeader, Rows, Row, TextField } from "@/views/common";
import { useResource } from "@/store/hooks";
import { ensureFresh, refresh } from "@/store/store";
import { runAgent, toggleAgent } from "@/lib/actions";
import { attempt } from "@/store/toast";
import { relative, shorten } from "@/lib/format";
import { withActivity } from "@/store/activity";
import type { AgentRunsPayload, AgentsPayload, AgentStatsPayload } from "@/lib/types";

export function Agents() {
  const agents = useResource<AgentsPayload>("agents");
  const stats = useResource<AgentStatsPayload>("agentStats");
  const runs = useResource<AgentRunsPayload>("agentRuns");
  const [selected, setSelected] = useState<string>("");
  const [instruction, setInstruction] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    ensureFresh("agentRuns", 8000);
  }, []);

  const list = agents.data?.agents ?? [];
  const agent = list.find((item) => item.name === selected) ?? list[0];

  const launch = async () => {
    if (!agent || !instruction.trim()) return;
    setBusy(true);
    const result = await attempt(`Agent ${agent.name} started`, () =>
      withActivity("thinking", () => runAgent(agent.name, instruction.trim())),
    );
    setBusy(false);
    if (result) {
      setInstruction("");
      await refresh("agentRuns");
      await refresh("tasks");
      await refresh("events");
    }
  };

  return (
    <>
      <PageHeader
        title="Agents"
        subtitle="Specialists from the multi-agent runtime. Running an agent uses its own tool allow-list and step budget — the engine enforces both."
        actions={<Button onClick={() => { void refresh("agents"); void refresh("agentRuns"); }}>Refresh</Button>}
      />
      <PageBody>
        <Grid cols={2}>
          <Panel
            title="Roster"
            subtitle={stats.data ? `${stats.data.agents.agents_enabled}/${stats.data.agents.agents} enabled · ${stats.data.agents.agent_runs} runs` : "Registered specialists"}
            className="min-h-[320px]"
          >
            <DataGate state={agents} isEmpty={(data) => data.agents.length === 0} emptyDetail="No agent is registered.">
              {(data) => (
                <div className="max-h-[440px] overflow-y-auto pe-1">
                  <Rows>
                    {data.agents.map((item) => (
                      <Row key={item.name} className={agent?.name === item.name ? "bg-accent/5" : ""}>
                        <button type="button" onClick={() => setSelected(item.name)} className="min-w-0 flex-1 text-start">
                          <p className="truncate text-[12px] font-medium text-ink">
                            {item.name} {item.builtin ? <span className="ms-1 text-[9.5px] text-faint">built-in</span> : null}
                          </p>
                          <p className="truncate text-[10.5px] text-faint">{item.role} · {item.allowed_tools.length} tools · max {item.max_steps} steps</p>
                        </button>
                        <Badge tone={item.enabled ? "ok" : "line"}>{item.enabled ? "on" : "off"}</Badge>
                        <Button
                          tone={item.enabled ? "line" : "ok"}
                          onClick={async () => {
                            const draft = { ...item } as unknown as Record<string, unknown>;
                            await attempt(item.enabled ? "Agent disabled" : "Agent enabled", () => toggleAgent(draft, !item.enabled));
                            await refresh("agents");
                          }}
                        >
                          {item.enabled ? "Disable" : "Enable"}
                        </Button>
                      </Row>
                    ))}
                  </Rows>
                </div>
              )}
            </DataGate>
          </Panel>

          <div className="space-y-2.5">
            <Panel title="Run agent" subtitle={agent ? `${agent.name} — ${agent.role}` : "Select an agent"}>
              {agent ? (
                <>
                  <p className="mb-2 text-[11px] leading-relaxed text-faint">{shorten(agent.description, 220)}</p>
                  <TextField label="Instruction" value={instruction} onChange={setInstruction} placeholder="Describe the assignment for this specialist" onEnter={() => void launch()} />
                  <div className="mt-2 flex items-center gap-1.5">
                    <Button tone="accent" onClick={() => void launch()} busy={busy} disabled={!instruction.trim()}>
                      Run
                    </Button>
                    <span className="text-[10.5px] text-faint">POST /v1/agents/{agent.name}/run</span>
                  </div>
                  <div className="mt-2">
                    <KeyValue label="Allowed tools" value={agent.allowed_tools.length === 0 ? "none (coordination only)" : `${agent.allowed_tools.length}`} />
                    <KeyValue label="Provider" value={agent.provider || "engine default"} />
                    <KeyValue label="Token budget" value={agent.token_budget ?? "unset"} />
                    <KeyValue label="Updated" value={relative(agent.updated_at)} />
                  </div>
                </>
              ) : (
                <p className="text-[11.5px] text-faint">Pick an agent from the roster.</p>
              )}
            </Panel>

            <Panel title="Recent runs" subtitle="From /v1/agents/runs">
              <DataGate state={runs} isEmpty={(data) => data.runs.length === 0} emptyDetail="No agent has been run yet on this machine.">
                {(data) => (
                  <Rows>
                    {data.runs.slice(0, 8).map((run, index) => (
                      <Row key={String(run.run_id ?? index)}>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-[11.5px] text-ink">{String(run.agent ?? "agent")}</p>
                          <p className="truncate font-mono text-[10px] text-faint">
                            {run.run_id ? String(run.run_id).slice(0, 10) : "—"} · {run.started_at ? relative(String(run.started_at)) : "unknown time"}
                          </p>
                        </div>
                        <Badge tone={String(run.status) === "completed" ? "ok" : String(run.status) === "failed" ? "danger" : "accent"}>
                          {String(run.status ?? "unknown")}
                        </Badge>
                      </Row>
                    ))}
                  </Rows>
                )}
              </DataGate>
            </Panel>
          </div>
        </Grid>
      </PageBody>
    </>
  );
}
