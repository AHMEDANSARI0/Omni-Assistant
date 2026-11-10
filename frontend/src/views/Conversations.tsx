import { useEffect, useState } from "react";
import { Badge, DataGate, Panel } from "@/components/Panel";
import { Button, Empty, PageBody, PageHeader, Rows, Row } from "@/views/common";
import { useResource } from "@/store/hooks";
import { apiGet } from "@/lib/api";
import { relative, shorten } from "@/lib/format";
import type { EventsPayload, TasksPayload } from "@/lib/types";

/**
 * Conversations are built from the task journal the engine already keeps: every
 * instruction plus the events recorded against its task id. Nothing is
 * synthesised into a "chat" that did not happen.
 */
export function Conversations() {
  const tasks = useResource<TasksPayload>("tasks");
  const [selected, setSelected] = useState<string | null>(null);
  const [events, setEvents] = useState<EventsPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    apiGet<EventsPayload>(`/v1/events?task_id=${encodeURIComponent(selected)}&limit=80`)
      .then((payload) => {
        if (!cancelled) setEvents(payload);
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected]);

  const list = tasks.data?.tasks ?? [];
  const active = list.find((task) => task.id === selected) ?? null;

  return (
    <>
      <PageHeader
        title="Conversations"
        subtitle="Instruction history with the engine's own event trail per task. Selecting a task reads its events through /v1/events?task_id=."
      />
      <PageBody>
        <div className="grid grid-cols-1 gap-2.5 lg:grid-cols-[320px_minmax(0,1fr)]">
          <Panel title="Instructions" subtitle={tasks.data ? `${tasks.data.total} recorded` : "Loading"} className="min-h-[320px]">
            <DataGate state={tasks} isEmpty={(data) => data.tasks.length === 0} emptyDetail="No instruction yet. Send one from the command bar at the bottom.">
              {(data) => (
                <div className="max-h-[480px] overflow-y-auto pe-1">
                  <Rows>
                    {data.tasks.map((task) => (
                      <Row key={task.id} className={selected === task.id ? "bg-accent/5" : ""}>
                        <button type="button" onClick={() => setSelected(task.id)} className="min-w-0 flex-1 text-start">
                          <p className="truncate text-[11.5px] text-ink">{shorten(task.instruction, 60)}</p>
                          <p className="truncate font-mono text-[10px] text-faint">{task.id.slice(0, 8)} · {relative(task.created_at)}</p>
                        </button>
                        <Badge tone={task.status === "failed" ? "danger" : task.status === "completed" ? "ok" : "accent"}>{task.status}</Badge>
                      </Row>
                    ))}
                  </Rows>
                </div>
              )}
            </DataGate>
          </Panel>

          <Panel
            title={active ? `Thread · ${active.id.slice(0, 8)}` : "Thread"}
            subtitle={active ? shorten(active.instruction, 120) : "Select an instruction on the left"}
            actions={active ? <Button onClick={() => setSelected(null)}>Clear</Button> : undefined}
            className="min-h-[320px]"
          >
            {!active ? (
              <Empty title="No thread selected" detail="A thread shows the instruction, then every event the engine recorded for that task, in order." />
            ) : (
              <div className="max-h-[480px] overflow-y-auto pe-1">
                <div className="mb-2 rounded-xl border border-accent/30 bg-accent/5 p-2.5">
                  <p className="hud-label">You</p>
                  <p className="mt-0.5 text-[12px] leading-relaxed text-ink">{active.instruction}</p>
                  <p className="mt-1 font-mono text-[10px] text-faint">
                    created {active.created_at} · status {active.status}
                  </p>
                </div>
                {loading ? <p className="py-3 text-center text-[11px] text-faint">Reading task events…</p> : null}
                {error ? <p className="rounded border border-danger/40 bg-danger/5 px-2 py-1.5 text-[11.5px] text-danger">{error}</p> : null}
                {events && !loading ? (
                  events.events.length === 0 ? (
                    <Empty title="No events for this task" detail="The engine writes an event when a task is created, started, retried or finished. An empty trail means nothing else happened yet." />
                  ) : (
                    <ol className="space-y-1">
                      {events.events.map((event) => (
                        <li key={event.id} className="rounded-lg border border-line/40 bg-inset/25 px-2 py-1.5">
                          <div className="flex items-center justify-between gap-2">
                            <span className="font-mono text-[10px] text-accent">{event.event_type}</span>
                            <span className="text-[10px] text-faint">{relative(event.created_at)}</span>
                          </div>
                          <p className="mt-0.5 text-[11px] leading-relaxed text-muted">{event.summary}</p>
                        </li>
                      ))}
                    </ol>
                  )
                ) : null}
                {active.result ? (
                  <div className="mt-2 rounded-xl border border-ok/30 bg-ok/5 p-2.5">
                    <p className="hud-label">Engine result</p>
                    <p className="mt-0.5 whitespace-pre-wrap text-[11.5px] leading-relaxed text-ink">{active.result}</p>
                  </div>
                ) : null}
                {active.error ? (
                  <div className="mt-2 rounded-xl border border-danger/35 bg-danger/5 p-2.5">
                    <p className="hud-label">Error</p>
                    <p className="mt-0.5 whitespace-pre-wrap text-[11.5px] leading-relaxed text-danger">{active.error}</p>
                  </div>
                ) : null}
              </div>
            )}
          </Panel>
        </div>
      </PageBody>
    </>
  );
}
