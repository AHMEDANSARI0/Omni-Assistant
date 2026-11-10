import { Badge, DataGate, Meter, Panel } from "@/components/Panel";
import { Sparkline } from "@/components/Sparkline";
import { Grid, KeyValue, PageBody, PageHeader, Button } from "@/views/common";
import { useResource } from "@/store/hooks";
import { refresh } from "@/store/store";
import { bps, mb, pct } from "@/lib/format";
import type { PerfBench, SystemPayload, SystemSnapshot } from "@/lib/types";
import { useEffect } from "react";
import { ensureFresh } from "@/store/store";

export function SystemMonitor() {
  const system = useResource<SystemPayload>("system");
  const bench = useResource<PerfBench>("perfBench");

  useEffect(() => {
    ensureFresh("perfBench", 60000);
  }, []);

  return (
    <>
      <PageHeader
        title="System Monitor"
        subtitle="Sampled from /proc or Windows performance counters by the Python core. A metric the platform cannot provide is shown as unavailable, never as zero."
        actions={<Button onClick={() => void refresh("system")}>Sample now</Button>}
      />
      <PageBody>
        <DataGate state={system} onRetry={() => void refresh("system")}>
          {(data) => {
            const snapshot = data.snapshot;
            const history = data.history ?? [];
            return (
              <div className="space-y-2.5">
                <Grid cols={4}>
                  <Tile title="CPU" value={snapshot.cpu.percent === null ? "unavailable" : pct(snapshot.cpu.percent)} note={`${snapshot.cpu.logical_cores} logical · ${snapshot.cpu.method}`}>
                    <Sparkline values={history.map((sample) => sample.cpu_percent)} height={30} />
                  </Tile>
                  <Tile title="Memory" value={pct(snapshot.memory.percent)} note={`${mb(snapshot.memory.used_bytes / 1048576)} / ${mb(snapshot.memory.total_bytes / 1048576)}`}>
                    <Sparkline values={history.map((sample) => sample.memory_percent)} height={30} tone="violet" />
                  </Tile>
                  <Tile
                    title="Network"
                    value={bps(snapshot.network.receive_bytes_per_second)}
                    note={`sent ${bps(snapshot.network.send_bytes_per_second)} · ${snapshot.network.interfaces.length} interfaces`}
                  >
                    <Sparkline values={history.map((sample) => sample.receive_bytes_per_second)} height={30} tone="ok" />
                  </Tile>
                  <Tile
                    title="GPU"
                    value={snapshot.gpu.available ? pct(snapshot.gpu.utilization_percent ?? 0) : "unavailable"}
                    note={snapshot.gpu.available ? snapshot.gpu.name ?? "present" : snapshot.gpu.method}
                  >
                    {snapshot.gpu.available ? <Meter value={snapshot.gpu.utilization_percent ?? 0} tone="violet" /> : null}
                  </Tile>
                </Grid>

                <Grid cols={2}>
                  <Panel title="Disks" subtitle="Install root and workspace">
                    {Object.entries(snapshot.disks).map(([key, disk]) => (
                      <div key={key} className="border-b border-line/30 py-1.5 last:border-0">
                        <div className="flex items-center justify-between">
                          <span className="text-[11.5px] text-ink capitalize">{key}</span>
                          <span className="font-mono text-[10.5px] text-faint">{mb(disk.free_bytes / 1048576)} free</span>
                        </div>
                        <div className="mt-1">
                          <Meter value={disk.percent} tone={disk.percent > 90 ? "danger" : disk.percent > 75 ? "warn" : "accent"} />
                        </div>
                        <p className="mt-0.5 truncate font-mono text-[9.5px] text-faint">{disk.path}</p>
                      </div>
                    ))}
                  </Panel>

                  <Panel title="Top processes" subtitle="Resident memory, descending">
                    <DataGate state={{ data: snapshot, error: null }} isEmpty={(value: SystemSnapshot) => (value.top_processes?.length ?? 0) === 0} emptyDetail="This platform cannot enumerate processes.">
                      {(value: SystemSnapshot) => (
                        <div className="space-y-0.5">
                          {value.top_processes!.map((process) => (
                            <div key={`${process.pid}-${process.name}`} className="flex items-center justify-between text-[11px]">
                              <span className="truncate text-muted">
                                {process.name} <span className="font-mono text-[10px] text-faint/70">{process.pid}</span>
                              </span>
                              <span className="font-mono text-[10.5px] text-faint">{mb(process.rss_bytes / 1048576)}</span>
                            </div>
                          ))}
                        </div>
                      )}
                    </DataGate>
                  </Panel>
                </Grid>

                <Grid cols={3}>
                  <Panel title="Platform" subtitle="Reported by the interpreter">
                    <KeyValue label="OS" value={`${snapshot.platform.name} ${snapshot.platform.release}`} />
                    <KeyValue label="Machine" value={snapshot.platform.machine} />
                    <KeyValue label="Python" value={snapshot.platform.python} />
                    <KeyValue label="Captured" value={snapshot.captured_at} />
                  </Panel>
                  <Panel title="Core process" subtitle="The Python service itself">
                    <KeyValue label="PID" value={snapshot.process.pid} />
                    <KeyValue label="RSS" value={mb(snapshot.process.rss_bytes / 1048576)} />
                    <KeyValue label="Threads" value={snapshot.process.python_threads} />
                    <KeyValue label="Uptime" value={`${snapshot.process.uptime_seconds.toFixed(1)}s`} />
                  </Panel>
                  <Panel title="API timings" subtitle="Real measurements from /v1/perf/bench" actions={<Button onClick={() => void refresh("perfBench")}>Measure</Button>}>
                    <DataGate state={bench}>
                      {(value) => (
                        <>
                          {Object.entries(value.measurements_ms).map(([key, milliseconds]) => (
                            <KeyValue key={key} label={key.replace(/_/g, " ")} value={`${milliseconds.toFixed(3)} ms`} />
                          ))}
                          <p className="mt-1 text-[9.5px] leading-relaxed text-faint">{value.notes}</p>
                        </>
                      )}
                    </DataGate>
                  </Panel>
                </Grid>
              </div>
            );
          }}
        </DataGate>
      </PageBody>
    </>
  );
}

function Tile({ title, value, note, children }: { title: string; value: string; note?: string; children?: React.ReactNode }) {
  return (
    <div className="glass hud-corners relative rounded-2xl px-3 py-2.5">
      <div className="flex items-baseline justify-between">
        <span className="hud-label">{title}</span>
        <Badge tone="line">live</Badge>
      </div>
      <p className="mt-1 font-mono text-[17px] text-ink">{value}</p>
      {children ? <div className="mt-1">{children}</div> : null}
      {note ? <p className="mt-1 truncate text-[9.5px] text-faint">{note}</p> : null}
    </div>
  );
}
