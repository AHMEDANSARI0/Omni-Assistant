import { useEffect } from "react";
import { Badge, DataGate, Panel } from "@/components/Panel";
import { Button, Grid, KeyValue, KeyValuePanel, PageBody, PageHeader, Rows, Row, TextField } from "@/views/common";
import { useResource } from "@/store/hooks";
import { ensureFresh, refresh } from "@/store/store";
import { decideApproval, safetyResume, safetyStop } from "@/lib/actions";
import { attempt } from "@/store/toast";
import { relative, shorten } from "@/lib/format";
import { useState } from "react";
import type { ApprovalsPayload, PluginsStatus, SafetyPayload, SyncStatus } from "@/lib/types";

export function Security() {
  const safety = useResource<SafetyPayload>("safety");
  const approvals = useResource<ApprovalsPayload>("approvals");
  const plugins = useResource<PluginsStatus>("plugins");
  const sync = useResource<SyncStatus>("sync");
  const [reason, setReason] = useState("Engaged from Security Center.");

  useEffect(() => {
    ensureFresh("plugins");
    ensureFresh("sync");
  }, []);

  const engaged = safety.data?.emergency_stop.engaged === true;

  return (
    <>
      <PageHeader
        title="Security Center"
        subtitle="Safety kernel state, the approval queue, plugin sandbox and encrypted-sync posture. Every switch here maps to a real API call."
        tone={engaged ? "danger" : "ok"}
        actions={<Button onClick={() => { void refresh("safety"); void refresh("approvals"); }}>Refresh</Button>}
      />
      <PageBody>
        <Grid cols={2}>
          <Panel title="Emergency stop" subtitle="Blocks further execution until released" className="min-h-[200px]">
            <DataGate state={safety} onRetry={() => void refresh("safety")}>
              {(data) => (
                <>
                  <div className={`mb-2 rounded-xl border px-3 py-2 ${engaged ? "border-danger/45 bg-danger/8" : "border-ok/35 bg-ok/5"}`}>
                    <p className={`font-display text-[13px] tracking-[0.16em] uppercase ${engaged ? "text-danger" : "text-ok"}`}>
                      {engaged ? "Engaged" : "Released"}
                    </p>
                    <p className="mt-0.5 text-[10.5px] text-faint">
                      {engaged
                        ? `Since ${data.emergency_stop.engaged_at ?? "unknown"} · by ${data.emergency_stop.engaged_by ?? "unknown"} · ${data.emergency_stop.reason || "no reason recorded"}`
                        : "No work is being blocked by the kernel."}
                    </p>
                  </div>
                  <div className="flex items-end gap-2">
                    <div className="min-w-0 flex-1">
                      <TextField label="Reason (stored with the stop)" value={reason} onChange={setReason} />
                    </div>
                    <Button
                      tone="danger"
                      busy={false}
                      onClick={async () => {
                        await attempt("Emergency stop engaged", () => safetyStop(reason));
                        await refresh("safety");
                        await refresh("health");
                        await refresh("events");
                      }}
                    >
                      Engage
                    </Button>
                    <Button
                      tone="ok"
                      onClick={async () => {
                        await attempt("Emergency stop released", () => safetyResume());
                        await refresh("safety");
                        await refresh("health");
                      }}
                    >
                      Release
                    </Button>
                  </div>
                  <div className="mt-2">
                    <KeyValue label="Hard timeouts" value={data.hard_timeouts ? "on" : "off"} />
                    <KeyValue label="Worker isolation" value={data.worker_isolation ? "on" : "off"} />
                    <KeyValue label="Tool ceiling" value={`${data.max_tool_seconds}s`} />
                    <KeyValue label="Approval expiry" value={`${data.approval_expiry_minutes} min`} />
                    <KeyValue label="Stop file" value={data.emergency_stop_file} />
                  </div>
                </>
              )}
            </DataGate>
          </Panel>

          <Panel title="Approval queue" subtitle="Exact-scope approval before side effects" className="min-h-[200px]">
            <DataGate state={approvals} isEmpty={(data) => data.requests.length === 0} emptyDetail="Nothing is waiting for a human decision.">
              {(data) => (
                <div className="max-h-[300px] overflow-y-auto pe-1">
                  <Rows>
                    {data.requests.map((request) => (
                      <Row key={request.key} className="items-start">
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-[11.5px] text-ink">{shorten(String(request.summary ?? request.tool_id ?? request.key), 90)}</p>
                          <p className="truncate font-mono text-[10px] text-faint">
                            {String(request.tool_id ?? request.tool ?? "tool")} · {request.requested_at ? relative(String(request.requested_at)) : "—"}
                          </p>
                        </div>
                        <Button
                          tone="ok"
                          onClick={async () => {
                            await attempt("Approved", () => decideApproval(request.key, true));
                            await refresh("approvals");
                            await refresh("events");
                          }}
                        >
                          Approve
                        </Button>
                        <Button
                          tone="danger"
                          onClick={async () => {
                            await attempt("Denied", () => decideApproval(request.key, false));
                            await refresh("approvals");
                            await refresh("events");
                          }}
                        >
                          Deny
                        </Button>
                      </Row>
                    ))}
                  </Rows>
                </div>
              )}
            </DataGate>
          </Panel>

          <KeyValuePanel title="Plugin sandbox" subtitle="Gate, signature policy and limits" state={plugins}>
            {(data) => (
              <>
                <div className="mb-1 flex items-center gap-2">
                  <Badge tone={data.enabled ? "ok" : "warn"}>{data.enabled ? "enabled" : "disabled"}</Badge>
                  <span className="text-[10.5px] text-faint">gate {data.gate}</span>
                </div>
                <KeyValue label="Signed bundles required" value={data.signature_required ? "yes" : "no"} />
                <KeyValue label="Unsigned allowed" value={data.unsigned_allowed ? "yes" : "no"} />
                <KeyValue label="Trusted keys" value={data.trusted_keys.length} />
                <KeyValue label="Sandbox" value={data.sandbox.mechanism} />
                <KeyValue label="OS limits" value={data.sandbox.os_limits.join(", ") || "none"} />
                <KeyValue label="Plugins installed" value={data.counts.plugins} />
                <KeyValue label="Runs" value={data.counts.runs} />
                <p className="mt-1 text-[10px] leading-relaxed text-faint">{data.detail}</p>
              </>
            )}
          </KeyValuePanel>

          <KeyValuePanel title="Encrypted device sync" subtitle="Opt-in, signed and encrypted bundles" state={sync}>
            {(data) => (
              <>
                <div className="mb-1 flex items-center gap-2">
                  <Badge tone={data.enabled ? "ok" : "warn"}>{data.enabled ? "enabled" : "disabled"}</Badge>
                  <span className="text-[10.5px] text-faint">gate {data.gate}</span>
                </div>
                <KeyValue label="Device id" value={data.device.device_id.slice(0, 12)} />
                <KeyValue label="Fingerprint" value={data.device.fingerprint} />
                <KeyValue label="Transport" value={data.transport} />
                <KeyValue label="Sync dir" value={data.sync_dir_state} />
                <KeyValue label="Conflict policy" value={data.conflict_policy} />
                <KeyValue label="Peers" value={data.counts.peers} />
                <KeyValue label="Bundles" value={data.counts.bundles} />
                <KeyValue label="Collections" value={data.collections.join(", ")} />
                <p className="mt-1 text-[10px] leading-relaxed text-faint">{data.detail}</p>
              </>
            )}
          </KeyValuePanel>
        </Grid>
      </PageBody>
    </>
  );
}
