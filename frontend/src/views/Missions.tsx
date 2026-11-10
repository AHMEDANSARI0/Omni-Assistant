import { useState } from "react";
import { Badge, DataGate, Meter, Panel } from "@/components/Panel";
import { Button, Grid, PageBody, PageHeader, TextField } from "@/views/common";
import { useResource } from "@/store/hooks";
import { refresh } from "@/store/store";
import { createMission } from "@/lib/actions";
import { attempt } from "@/store/toast";
import { relative, shorten } from "@/lib/format";
import type { MissionsPayload } from "@/lib/types";

export function Missions() {
  const missions = useResource<MissionsPayload>("missions");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [steps, setSteps] = useState("");
  const [busy, setBusy] = useState(false);

  const create = async () => {
    if (!name.trim()) return;
    setBusy(true);
    const instructions = steps.split("\n").map((line) => line.trim()).filter(Boolean);
    const result = await attempt("Mission created", () =>
      createMission(name.trim(), description.trim(), instructions),
    );
    setBusy(false);
    if (result) {
      setName("");
      setDescription("");
      setSteps("");
      await refresh("missions");
      await refresh("tasks");
      await refresh("events");
    }
  };

  return (
    <>
      <PageHeader
        title="Missions"
        subtitle="A mission is an ordered set of tasks the engine schedules in parallel up to your configured limit. Progress is computed from task states."
        actions={<Button onClick={() => void refresh("missions")}>Refresh</Button>}
      />
      <PageBody>
        <Grid cols={2}>
          <Panel title="Mission board" subtitle={missions.data ? `${missions.data.total} total` : "Loading"} className="min-h-[300px]">
            <DataGate state={missions} isEmpty={(data) => data.missions.length === 0} emptyDetail="No mission yet. Create one on the right; tasks are real task records, not a simulation.">
              {(data) => (
                <div className="space-y-2">
                  {data.missions.map((mission) => {
                    const completed =
                      Number(mission.progress.completed ?? 0) + Number(mission.progress.succeeded ?? 0) + Number(mission.progress.done ?? 0);
                    const total = mission.progress.total || 1;
                    return (
                      <div key={mission.id} className="rounded-xl border border-line/50 bg-inset/30 p-2.5">
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0">
                            <p className="truncate text-[12.5px] font-medium text-ink">{mission.name}</p>
                            <p className="truncate text-[10.5px] text-faint">{shorten(mission.description, 96) || "no description"}</p>
                          </div>
                          <Badge tone={mission.status === "completed" ? "ok" : mission.status === "failed" ? "danger" : mission.status === "draft" ? "line" : "accent"}>
                            {mission.status}
                          </Badge>
                        </div>
                        <div className="mt-2">
                          <Meter value={(completed / total) * 100} label={`${mission.name} progress`} />
                        </div>
                        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[10px] text-faint">
                          <span>
                            {completed}/{mission.progress.total} tasks
                          </span>
                          {Object.entries(mission.progress)
                            .filter(([key]) => key !== "total")
                            .map(([key, value]) => (
                              <span key={key}>
                                {value} {key}
                              </span>
                            ))}
                          <span>updated {relative(mission.updated_at)}</span>
                          <span className="font-mono">{mission.id.slice(0, 8)}</span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </DataGate>
          </Panel>

          <Panel title="New mission" subtitle="Each line becomes one task in order">
            <div className="space-y-2">
              <TextField label="Name" value={name} onChange={setName} placeholder="Q4 workspace audit" />
              <TextField label="Description" value={description} onChange={setDescription} placeholder="What the mission should achieve" />
              <label className="block">
                <span className="hud-label">Tasks (one per line)</span>
                <textarea
                  value={steps}
                  onChange={(event) => setSteps(event.target.value)}
                  rows={5}
                  placeholder={"List every file in the workspace\nSummarise the README\nWrite a short report"}
                  className="mt-1 w-full resize-y rounded-lg border border-line/60 bg-inset/50 px-2.5 py-1.5 text-[12px] text-ink placeholder:text-faint focus:border-accent/60 focus:outline-none"
                />
              </label>
              <div className="flex items-center gap-1.5">
                <Button tone="accent" onClick={() => void create()} busy={busy} disabled={!name.trim()}>
                  Create mission
                </Button>
                <span className="text-[10.5px] text-faint">POST /v1/missions</span>
              </div>
              <p className="text-[10.5px] leading-relaxed text-faint">
                A mission starts as a draft. The engine moves it forward when its tasks are executed; the board never shows progress it cannot
                derive from task records.
              </p>
            </div>
          </Panel>
        </Grid>
      </PageBody>
    </>
  );
}
