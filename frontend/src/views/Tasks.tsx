import { useMemo, useState } from "react";
import { Badge, DataGate, Panel } from "@/components/Panel";
import { Button, Grid, KeyValue, PageBody, PageHeader, Rows, Row, TextField } from "@/views/common";
import { useResource } from "@/store/hooks";
import { refresh } from "@/store/store";
import { createTask, runInstruction } from "@/lib/actions";
import { attempt } from "@/store/toast";
import { withActivity } from "@/store/activity";
import { relative, shorten } from "@/lib/format";
import type { TasksPayload } from "@/lib/types";

const STATUS_TONE: Record<string, "ok" | "warn" | "danger" | "accent" | "line" | "violet"> = {
  queued: "accent",
  running: "violet",
  completed: "ok",
  succeeded: "ok",
  failed: "danger",
  cancelled: "line",
};

export function Tasks() {
  const tasks = useResource<TasksPayload>("tasks");
  const [filter, setFilter] = useState("all");
  const [instruction, setInstruction] = useState("");
  const [useAi, setUseAi] = useState(false);
  const [busy, setBusy] = useState<null | "create" | "run">(null);
  const [open, setOpen] = useState<string | null>(null);

  const filtered = useMemo(() => {
    const list = tasks.data?.tasks ?? [];
    return filter === "all" ? list : list.filter((task) => task.status === filter);
  }, [tasks.data, filter]);

  const statuses = useMemo(() => {
    const counts = (tasks.data?.tasks ?? []).reduce<Record<string, number>>((acc, task) => {
      acc[task.status] = (acc[task.status] ?? 0) + 1;
      return acc;
    }, {});
    return ["all", ...Object.keys(counts)];
  }, [tasks.data]);

  const run = async (mode: "create" | "run") => {
    if (!instruction.trim()) return;
    setBusy(mode);
    const result = await attempt(mode === "run" ? "Task executed" : "Task queued", () =>
      withActivity<unknown>("thinking", () =>
        mode === "run" ? runInstruction(instruction.trim(), useAi) : createTask(instruction.trim()),
      ),
    );
    setBusy(null);
    if (result) {
      setInstruction("");
      await refresh("tasks");
      await refresh("events");
    }
  };

  return (
    <>
      <PageHeader
        title="Tasks"
        subtitle="The queue is the engine's own task table: create records here, or execute an instruction immediately. Nothing is marked done by the UI."
        actions={<Button onClick={() => void refresh("tasks")}>Refresh</Button>}
      />
      <PageBody>
        <Grid cols={2}>
          <Panel
            title="Queue"
            subtitle={tasks.data ? `${tasks.data.total} total` : "Loading"}
            actions={
              <div className="flex flex-wrap gap-1">
                {statuses.map((status) => (
                  <button
                    key={status}
                    type="button"
                    onClick={() => setFilter(status)}
                    className={`rounded px-1.5 py-[2px] text-[10px] capitalize transition ${
                      filter === status ? "bg-accent/15 text-accent" : "text-faint hover:text-ink"
                    }`}
                  >
                    {status}
                  </button>
                ))}
              </div>
            }
            className="min-h-[340px]"
          >
            <DataGate
              state={tasks}
              isEmpty={() => filtered.length === 0}
              emptyDetail={filter === "all" ? "The queue is empty. Queue or execute an instruction to create the first real record." : "No task with this status."}
            >
              {() => (
                <div className="max-h-[440px] overflow-y-auto pe-1">
                  <Rows>
                    {filtered.map((task) => (
                      <div key={task.id}>
                        <Row>
                          <button type="button" onClick={() => setOpen(open === task.id ? null : task.id)} className="min-w-0 flex-1 text-start">
                            <p className="truncate text-[12px] text-ink">{shorten(task.instruction, 96)}</p>
                            <p className="truncate font-mono text-[10px] text-faint">
                              {task.id.slice(0, 8)} · {relative(task.created_at)}
                              {task.mission_id ? ` · mission ${String(task.mission_id).slice(0, 6)}` : ""}
                            </p>
                          </button>
                          <Badge tone={STATUS_TONE[task.status] ?? "line"}>{task.status}</Badge>
                        </Row>
                        {open === task.id ? (
                          <div className="mb-2 rounded-lg border border-line/45 bg-inset/35 p-2">
                            <KeyValue label="Instruction" value={task.instruction} mono={false} />
                            <KeyValue label="Created" value={task.created_at} />
                            <KeyValue label="Updated" value={task.updated_at} />
                            <KeyValue label="Completed" value={task.completed_at ?? "—"} />
                            <KeyValue label="Error" value={task.error ?? "none"} mono={false} />
                            {task.result ? <KeyValue label="Result" value={shorten(task.result, 400)} mono={false} /> : null}
                          </div>
                        ) : null}
                      </div>
                    ))}
                  </Rows>
                </div>
              )}
            </DataGate>
          </Panel>

          <Panel title="New work" subtitle="Both buttons create a real task record">
            <div className="space-y-2">
              <TextField
                label="Instruction"
                value={instruction}
                onChange={setInstruction}
                placeholder="Summarise every markdown file in the workspace"
                onEnter={() => void run("create")}
              />
              <label className="flex items-center gap-2 text-[11.5px] text-muted">
                <input type="checkbox" checked={useAi} onChange={(event) => setUseAi(event.target.checked)} className="accent-[rgb(var(--accent))]" />
                Ask the model to plan the task (uses the configured provider; falls back to the offline mock)
              </label>
              <div className="flex flex-wrap items-center gap-1.5">
                <Button tone="accent" onClick={() => void run("run")} busy={busy === "run"} disabled={!instruction.trim()}>
                  Execute now
                </Button>
                <Button onClick={() => void run("create")} busy={busy === "create"} disabled={!instruction.trim()}>
                  Queue only
                </Button>
              </div>
              <p className="text-[10.5px] leading-relaxed text-faint">
                “Execute now” posts to <span className="font-mono">/v1/run</span> and waits for the engine's answer; “Queue only” posts to
                <span className="font-mono"> /v1/tasks</span> and leaves the task queued for the scheduler.
              </p>
              <div className="mt-1 rounded-lg border border-line/45 bg-inset/30 p-2 text-[10.5px] text-faint">
                Task statuses come straight from the database. A failed task keeps its error string, which is what the Intelligence panel lists.
              </div>
            </div>
          </Panel>
        </Grid>
      </PageBody>
    </>
  );
}
