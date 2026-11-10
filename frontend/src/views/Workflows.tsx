import { useState } from "react";
import { Badge, DataGate, Panel } from "@/components/Panel";
import { Button, Grid, KeyValue, PageBody, PageHeader, Rows, Row } from "@/views/common";
import { useResource } from "@/store/hooks";
import { refresh } from "@/store/store";
import { apiPost } from "@/lib/api";
import { attempt } from "@/store/toast";
import { relative, shorten } from "@/lib/format";
import { flowTick } from "@/lib/actions";
import type { FlowsPayload } from "@/lib/types";

export function Workflows() {
  const flows = useResource<FlowsPayload>("flows");
  const [document, setDocument] = useState("");
  const [busy, setBusy] = useState<null | "import" | "tick">(null);

  const importFlow = async () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(document);
    } catch (error) {
      await attempt("Flow import", async () => {
        throw new Error(`The pasted document is not valid JSON: ${error instanceof Error ? error.message : error}`);
      });
      return;
    }
    setBusy("import");
    const result = await attempt("Flow imported", () => apiPost<{ flow?: unknown }>("/v1/flows/import", { document: parsed }));
    setBusy(null);
    if (result) {
      setDocument("");
      await refresh("flows");
      await refresh("events");
    }
  };

  return (
    <>
      <PageHeader
        title="Workflows"
        subtitle="Saved flows, their triggers and run counts. Flows execute through the same tool registry and approval rules as everything else."
        actions={
          <>
            <Button onClick={() => void refresh("flows")}>Refresh</Button>
            <Button
              tone="accent"
              busy={busy === "tick"}
              onClick={async () => {
                setBusy("tick");
                await attempt("Flow triggers evaluated", () => flowTick());
                setBusy(null);
                await refresh("flows");
                await refresh("tasks");
              }}
            >
              Fire due triggers
            </Button>
          </>
        }
      />
      <PageBody>
        <Grid cols={2}>
          <Panel title="Flows" subtitle={flows.data ? `${flows.data.summary.flows} saved` : "Loading"} className="min-h-[300px]">
            <DataGate state={flows} isEmpty={(data) => data.flows.length === 0} emptyDetail="No flow yet. Import a definition on the right — the engine validates it before saving.">
              {(data) => (
                <div className="max-h-[440px] overflow-y-auto pe-1">
                  <Rows>
                    {data.flows.map((flow) => (
                      <Row key={flow.id}>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-[12px] text-ink">{flow.name}</p>
                          <p className="truncate font-mono text-[10px] text-faint">{flow.id}</p>
                        </div>
                        <Badge tone={flow.enabled === false ? "line" : "accent"}>{String(flow.status ?? (flow.enabled === false ? "paused" : "active"))}</Badge>
                      </Row>
                    ))}
                  </Rows>
                </div>
              )}
            </DataGate>
          </Panel>

          <div className="space-y-2.5">
            <Panel title="Summary" subtitle="From /v1/flows">
              <DataGate state={flows}>
                {(data) => (
                  <>
                    <KeyValue label="Flows" value={data.summary.flows} />
                    <KeyValue label="Triggers enabled" value={data.summary.triggers_enabled} />
                    <KeyValue label="Runs recorded" value={data.summary.runs} />
                    <KeyValue label="Last run" value={data.summary.last_run_at ? relative(data.summary.last_run_at) : "never"} />
                    {Object.entries(data.summary.by_status).map(([status, count]) => (
                      <KeyValue key={status} label={`Status ${status}`} value={count} />
                    ))}
                  </>
                )}
              </DataGate>
            </Panel>

            <Panel title="Import a flow" subtitle="POST /v1/flows/import — validated by the engine">
              <label className="block">
                <span className="hud-label">Flow document (JSON)</span>
                <textarea
                  value={document}
                  onChange={(event) => setDocument(event.target.value)}
                  rows={6}
                  placeholder='{"name":"Daily sweep","steps":[{"tool":"workspace.list_files","values":{}}]}'
                  className="mt-1 w-full resize-y rounded-lg border border-line/60 bg-inset/50 px-2.5 py-1.5 font-mono text-[11px] text-ink placeholder:text-faint focus:border-accent/60 focus:outline-none"
                />
              </label>
              <div className="mt-2 flex items-center gap-1.5">
                <Button tone="accent" onClick={() => void importFlow()} busy={busy === "import"} disabled={!document.trim()}>
                  Validate & import
                </Button>
                <span className="text-[10.5px] text-faint">A rejected definition surfaces the engine's error verbatim.</span>
              </div>
              <p className="mt-2 text-[10.5px] leading-relaxed text-faint">
                The visual canvas for building flows without JSON is a separate surface; this screen only imports and inspects definitions that
                already exist. {shorten("No node editor is embedded here on purpose: a decorative canvas would be a fake feature.", 240)}
              </p>
            </Panel>
          </div>
        </Grid>
      </PageBody>
    </>
  );
}
