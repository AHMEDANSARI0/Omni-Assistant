import { useEffect, useState } from "react";
import { Badge, DataGate, Panel } from "@/components/Panel";
import { Button, Grid, KeyValue, PageBody, PageHeader } from "@/views/common";
import { useResource } from "@/store/hooks";
import { ensureFresh, refresh } from "@/store/store";
import { apiGet } from "@/lib/api";
import { attempt } from "@/store/toast";
import { pct } from "@/lib/format";
import type { ProvidersPayload, UsagePayload } from "@/lib/types";

export function ModelCenter() {
  const providers = useResource<ProvidersPayload>("providers");
  const usage = useResource<UsagePayload>("usage");
  const [capabilities, setCapabilities] = useState<Record<string, unknown> | null>(null);
  const [evaluation, setEvaluation] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState<null | "caps" | "eval">(null);

  useEffect(() => {
    ensureFresh("usage", 20000);
  }, []);

  const loadCapabilities = async () => {
    setBusy("caps");
    try {
      setCapabilities(await apiGet<Record<string, unknown>>("/v1/providers/capabilities"));
    } catch (error) {
      setCapabilities({ error: error instanceof Error ? error.message : String(error) });
    }
    setBusy(null);
  };

  const runEvaluation = async () => {
    setBusy("eval");
    const result = await attempt("Provider evaluation recorded", () =>
      fetch("/v1/providers/evaluations/run", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${document.querySelector<HTMLMetaElement>('meta[name="pc-agent-token"]')?.content ?? ""}`,
        },
        body: "{}",
      }).then(async (response) => {
        const payload = (await response.json()) as { ok: boolean; data?: Record<string, unknown>; error?: { message?: string } };
        if (!payload.ok) throw new Error(payload.error?.message ?? `status ${response.status}`);
        return payload.data ?? {};
      }),
    );
    setBusy(null);
    if (result) {
      setEvaluation(result);
      await refresh("providers");
    }
  };

  return (
    <>
      <PageHeader
        title="Model Center"
        subtitle="Provider slots, routing order, usage counters and evaluations. Keys are never entered in this UI — they live in OS secure storage via the CLI."
        actions={<Button onClick={() => void refresh("providers")}>Refresh</Button>}
      />
      <PageBody>
        <Grid cols={2}>
          <Panel title="Routing" subtitle="The route the engine would take right now" className="min-h-[260px]">
            <DataGate state={providers} onRetry={() => void refresh("providers")}>
              {(data) => (
                <>
                  <div className="mb-2 flex flex-wrap items-center gap-1.5">
                    {data.route.length === 0 ? (
                      <span className="text-[11.5px] text-warn">No provider is in the route.</span>
                    ) : (
                      data.route.map((slot, index) => (
                        <span key={slot} className="flex items-center gap-1.5">
                          {index > 0 ? <span className="text-faint">→</span> : null}
                          <Badge tone={slot === "mock" ? "warn" : "accent"}>{slot}</Badge>
                        </span>
                      ))
                    )}
                  </div>
                  <KeyValue label="Strategy" value={data.strategy} />
                  <KeyValue label="Configured providers" value={data.providers.length} />
                  <KeyValue label="Preference order" value={data.order.length ? data.order.join(" → ") : "engine default"} />
                  {data.providers.length === 0 ? (
                    <p className="mt-2 rounded-lg border border-warn/35 bg-warn/5 px-2 py-1.5 text-[10.5px] leading-relaxed text-warn">
                      Only the offline mock engine is configured. Add a key with
                      <span className="font-mono"> pc-agent credentials set</span> and the route picks it up on the next read — this screen will
                      never pretend a cloud model answered.
                    </p>
                  ) : null}
                  <div className="mt-2 flex gap-1.5">
                    <Button busy={busy === "caps"} onClick={() => void loadCapabilities()}>
                      Read capabilities
                    </Button>
                    <Button busy={busy === "eval"} tone="violet" onClick={() => void runEvaluation()}>
                      Run evaluation
                    </Button>
                  </div>
                  {capabilities ? (
                    <pre className="mt-2 max-h-[160px] overflow-auto rounded-lg border border-line/45 bg-inset/40 p-2 font-mono text-[10px] text-muted">
                      {JSON.stringify(capabilities, null, 2)}
                    </pre>
                  ) : null}
                </>
              )}
            </DataGate>
          </Panel>

          <div className="space-y-2.5">
            <Panel title="Usage" subtitle="Local analytics file, can be disabled in settings">
              <DataGate state={usage}>
                {(data) => (
                  <>
                    <KeyValue label="Calls" value={data.usage.calls} />
                    <KeyValue label="Successes" value={data.usage.successes} />
                    <KeyValue label="Success rate" value={data.usage.success_rate === null ? "not enough data" : pct(data.usage.success_rate, 1)} />
                    <KeyValue label="Input tokens" value={data.usage.input_tokens} />
                    <KeyValue label="Output tokens" value={data.usage.output_tokens} />
                    <KeyValue label="Estimated cost" value={data.usage.estimated_cost === null ? "not configured" : data.usage.estimated_cost} />
                    <KeyValue label="Analytics" value={data.analytics_enabled ? "enabled" : "disabled"} />
                    {data.usage.by_model.length > 0 ? (
                      <div className="mt-2">
                        {data.usage.by_model.map((model, index) => (
                          <KeyValue key={index} label={String(model.model ?? `model ${index + 1}`)} value={`${model.calls ?? 0} calls · ${model.tokens ?? 0} tokens`} />
                        ))}
                      </div>
                    ) : (
                      <p className="mt-2 text-[10.5px] text-faint">No per-model usage recorded yet.</p>
                    )}
                  </>
                )}
              </DataGate>
            </Panel>
            {evaluation ? (
              <Panel title="Last evaluation" subtitle="Result of /v1/providers/evaluations/run">
                <pre className="max-h-[180px] overflow-auto rounded-lg border border-line/45 bg-inset/40 p-2 font-mono text-[10px] text-muted">
                  {JSON.stringify(evaluation, null, 2)}
                </pre>
              </Panel>
            ) : null}
            <Panel title="Where keys live" subtitle="Security posture, stated plainly">
              <ul className="space-y-1 text-[11px] leading-relaxed text-faint">
                <li>· Provider keys are stored in the OS credential store, never in this UI and never in a config file.</li>
                <li>· Requests are made from the local Python process; the browser never talks to a provider.</li>
                <li>· With no key configured the engine answers from the offline mock and says so.</li>
              </ul>
            </Panel>
          </div>
        </Grid>
      </PageBody>
    </>
  );
}
