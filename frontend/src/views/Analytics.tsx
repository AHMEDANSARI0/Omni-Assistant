import { useEffect } from "react";
import { DataGate, Meter, Panel } from "@/components/Panel";
import { Button, Grid, KeyValue, PageBody, PageHeader } from "@/views/common";
import { useResource } from "@/store/hooks";
import { ensureFresh, refresh } from "@/store/store";
import { pct } from "@/lib/format";
import type { AutomationStats, FlowsStatsPayload, Overview, PerfBench, UsagePayload } from "@/lib/types";


export function Analytics() {
  const overview = useResource<Overview>("overview");
  const usage = useResource<UsagePayload>("usage");
  const bench = useResource<PerfBench>("perfBench");
  const automation = useResource<AutomationStats>("automation");
  const flows = useResource<FlowsStatsPayload>("flowsStats");

  useEffect(() => {
    ensureFresh("usage", 20000);
    ensureFresh("automation", 30000);
    ensureFresh("flowsStats", 20000);
  }, []);

  const measure = async () => {
    await refresh("perfBench");
    await refresh("overview");
  };

  return (
    <>
      <PageHeader
        title="Analytics"
        subtitle="Everything on this page is counted or timed by the local engine. There are no projections, no synthetic trends and no placeholder charts."
        actions={<Button tone="accent" onClick={() => void measure()}>Re-measure</Button>}
      />
      <PageBody>
        <Grid cols={4}>
          <Stat title="Tasks" value={overview.data?.tasks.total ?? "—"} note={overview.data ? Object.entries(overview.data.tasks.by_status).map(([key, count]) => `${count} ${key}`).join(" · ") || "none yet" : ""} />
          <Stat title="Events" value={overview.data?.events_total ?? "—"} note="append-only audit log" />
          <Stat title="Missions" value={overview.data?.missions ?? "—"} note={`${overview.data?.schedules ?? 0} schedules · ${overview.data?.schedules_enabled ?? 0} enabled`} />
          <Stat title="Tools" value={overview.data?.tools ?? "—"} note="registered and deny-by-default" />
        </Grid>

        <div className="mt-2.5">
          <Grid cols={2}>
            <Panel title="API timings" subtitle="Wall clock, this machine, single process" actions={<Button onClick={() => void refresh("perfBench")}>Run</Button>}>
              <DataGate state={bench}>
                {(data) => (
                  <>
                    {Object.entries(data.measurements_ms).map(([key, value]) => {
                      const max = Math.max(...Object.values(data.measurements_ms), 0.001);
                      return (
                        <div key={key} className="border-b border-line/30 py-1 last:border-0">
                          <div className="flex items-center justify-between">
                            <span className="text-[11px] text-muted">{key.replace(/_/g, " ")}</span>
                            <span className="font-mono text-[10.5px] text-ink">{value.toFixed(3)} ms</span>
                          </div>
                          <div className="mt-0.5">
                            <Meter value={(value / max) * 100} tone="accent" />
                          </div>
                        </div>
                      );
                    })}
                    <p className="mt-1 text-[9.5px] leading-relaxed text-faint">
                      {data.notes} · python {data.python} · {data.platform}
                    </p>
                  </>
                )}
              </DataGate>
            </Panel>

            <div className="space-y-2.5">
              <Panel title="Provider usage" subtitle="Local JSONL analytics, may be disabled">
                <DataGate state={usage}>
                  {(data) => (
                    <>
                      <KeyValue label="Calls" value={data.usage.calls} />
                      <KeyValue label="Success rate" value={data.usage.success_rate === null ? "no calls recorded" : pct(data.usage.success_rate, 1)} />
                      <KeyValue label="Input tokens" value={data.usage.input_tokens} />
                      <KeyValue label="Output tokens" value={data.usage.output_tokens} />
                      <KeyValue label="Estimated cost" value={data.usage.estimated_cost === null ? "not configured" : String(data.usage.estimated_cost)} />
                    </>
                  )}
                </DataGate>
              </Panel>
              <Panel title="Automation & flows" subtitle="Refusals are counted too">
                <DataGate state={automation}>
                  {(data) => (
                    <>
                      <KeyValue label="Automation runs" value={data.automation.total} />
                      <KeyValue label="Refused by policy" value={data.automation.refused} />
                      <KeyValue label="Last automation" value={data.automation.last_at ?? "never"} />
                      <KeyValue label="Flows" value={flows.data?.flows.flows ?? "—"} />
                      <KeyValue label="Flow runs" value={flows.data?.flows.runs ?? "—"} />
                    </>
                  )}
                </DataGate>
              </Panel>
            </div>
          </Grid>
        </div>
      </PageBody>
    </>
  );
}

function Stat({ title, value, note }: { title: string; value: number | string; note: string }) {
  return (
    <div className="glass hud-corners relative rounded-2xl px-3 py-2.5">
      <p className="hud-label">{title}</p>
      <p className="mt-1 font-mono text-[19px] text-ink">{value}</p>
      <p className="mt-0.5 truncate text-[9.5px] text-faint">{note}</p>
    </div>
  );
}
