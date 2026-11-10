import { useEffect, useState } from "react";
import { Badge, DataGate, Panel } from "@/components/Panel";
import { Button, Grid, PageBody, PageHeader, Rows, Row, TextField } from "@/views/common";
import { useResource } from "@/store/hooks";
import { ensureFresh, refresh } from "@/store/store";
import {
  addReminder,
  deleteReminder,
  deleteSchedule,
  reminderAction,
  runDueSchedules,
  setScheduleEnabled,
} from "@/lib/actions";
import { attempt } from "@/store/toast";
import { relative, shorten } from "@/lib/format";
import type { ConnectorsStatus, RemindersPayload, SchedulesPayload } from "@/lib/types";

export function CalendarView() {
  const reminders = useResource<RemindersPayload>("reminders");
  const schedules = useResource<SchedulesPayload>("schedules");
  const connectors = useResource<ConnectorsStatus>("connectors");
  const [title, setTitle] = useState("");
  const [due, setDue] = useState("+1h");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    ensureFresh("connectors");
  }, []);

  const create = async () => {
    if (!title.trim()) return;
    setBusy(true);
    const result = await attempt("Reminder created", () => addReminder(title.trim(), due.trim(), note.trim()));
    setBusy(false);
    if (result) {
      setTitle("");
      setNote("");
      await refresh("reminders");
      await refresh("proactive");
      await refresh("events");
    }
  };

  const calendarConnector = connectors.data?.connectors.find((item) => item.connector === "calendar");

  return (
    <>
      <PageHeader
        title="Calendar"
        subtitle="Reminders and schedules are local records the scheduler really fires. The calendar connector is optional and gated."
        actions={
          <Button
            tone="accent"
            onClick={async () => {
              await attempt("Scheduler sweep requested", () => runDueSchedules());
              await refresh("schedules");
              await refresh("tasks");
            }}
          >
            Run due schedules
          </Button>
        }
      />
      <PageBody>
        <Grid cols={2}>
          <Panel title="Reminders" subtitle={reminders.data ? `${reminders.data.total} total` : "Loading"} className="min-h-[300px]">
            <DataGate state={reminders} isEmpty={(data) => data.reminders.length === 0} emptyDetail="No reminder yet. Add one below; due times accept ISO-8601, HH:MM or +15m.">
              {(data) => (
                <Rows>
                  {data.reminders.map((reminder) => (
                    <div key={reminder.id} className="py-1.5">
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <p className="truncate text-[12px] text-ink">{reminder.title}</p>
                          <p className="truncate text-[10.5px] text-faint">
                            due {reminder.due_at} · {reminder.severity} · fired {reminder.fired_count}×
                          </p>
                          {reminder.note ? <p className="truncate text-[10px] text-faint/85">{shorten(reminder.note, 70)}</p> : null}
                        </div>
                        <Badge tone={reminder.status === "open" ? "accent" : reminder.status === "done" ? "ok" : "line"}>{reminder.status}</Badge>
                      </div>
                      <div className="mt-1 flex flex-wrap gap-1.5">
                        <Button tone="ok" onClick={async () => { await attempt("Reminder done", () => reminderAction(reminder.id, "done")); await refresh("reminders"); }}>
                          Done
                        </Button>
                        <Button onClick={async () => { await attempt("Reminder snoozed 15m", () => reminderAction(reminder.id, "snooze")); await refresh("reminders"); }}>
                          Snooze 15m
                        </Button>
                        <Button onClick={async () => { await attempt("Reminder dismissed", () => reminderAction(reminder.id, "dismiss")); await refresh("reminders"); }}>
                          Dismiss
                        </Button>
                        <Button tone="danger" onClick={async () => { await attempt("Reminder deleted", () => deleteReminder(reminder.id)); await refresh("reminders"); }}>
                          Delete
                        </Button>
                      </div>
                    </div>
                  ))}
                </Rows>
              )}
            </DataGate>
          </Panel>

          <div className="space-y-2.5">
            <Panel title="Add reminder" subtitle="Stored locally, fires through the proactive engine">
              <div className="space-y-2">
                <TextField label="Title" value={title} onChange={setTitle} placeholder="Check the nightly backup" onEnter={() => void create()} />
                <div className="grid grid-cols-2 gap-2">
                  <TextField label="Due" value={due} onChange={setDue} placeholder="+1h" mono />
                  <TextField label="Note" value={note} onChange={setNote} placeholder="optional" />
                </div>
                <Button tone="accent" onClick={() => void create()} busy={busy} disabled={!title.trim()}>
                  Create reminder
                </Button>
              </div>
            </Panel>

            <Panel title="Schedules" subtitle={schedules.data ? `scheduler checks every ${schedules.data.scheduler_interval_seconds}s` : "Loading"}>
              <DataGate state={schedules} isEmpty={(data) => data.schedules.length === 0} emptyDetail="No schedule yet. Schedules are created from the CLI or the API with an explicit interval.">
                {(data) => (
                  <Rows>
                    {data.schedules.map((schedule) => (
                      <Row key={schedule.id}>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-[12px] text-ink">{schedule.name}</p>
                          <p className="truncate text-[10px] text-faint">
                            {schedule.kind}
                            {schedule.interval_seconds ? ` · every ${Math.round(schedule.interval_seconds / 60)}m` : ""} · next{" "}
                            {schedule.next_run_at ? relative(schedule.next_run_at) : "—"} · runs {schedule.run_count}
                          </p>
                        </div>
                        <Badge tone={schedule.enabled ? "ok" : "line"}>{schedule.enabled ? "on" : "off"}</Badge>
                        <Button
                          onClick={async () => {
                            await attempt(schedule.enabled ? "Schedule paused" : "Schedule enabled", () => setScheduleEnabled(schedule.id, !schedule.enabled));
                            await refresh("schedules");
                          }}
                        >
                          {schedule.enabled ? "Pause" : "Enable"}
                        </Button>
                        <Button
                          tone="danger"
                          onClick={async () => {
                            await attempt("Schedule deleted", () => deleteSchedule(schedule.id));
                            await refresh("schedules");
                          }}
                        >
                          ✕
                        </Button>
                      </Row>
                    ))}
                  </Rows>
                )}
              </DataGate>
            </Panel>

            <Panel title="Calendar connector" subtitle="Optional, gated by PC_AGENT_ALLOW_CONNECTORS">
              <DataGate state={connectors}>
                {() =>
                  calendarConnector ? (
                    <div className="space-y-1 text-[11.5px]">
                      <p className="text-ink">
                        State: <Badge tone={calendarConnector.enabled ? "ok" : "warn"}>{calendarConnector.enabled ? "enabled" : "disabled"}</Badge>
                      </p>
                      <p className="text-faint">{calendarConnector.reason}</p>
                      <p className="text-faint">Target: <span className="font-mono">{calendarConnector.target}</span></p>
                      {!calendarConnector.enabled ? (
                        <p className="rounded bg-warn/10 px-2 py-1 text-[10.5px] text-warn">
                          Calendar sync is unavailable until the connector gate is enabled in the environment. This UI does not fake calendar
                          events in the meantime.
                        </p>
                      ) : null}
                    </div>
                  ) : (
                    <p className="text-[11.5px] text-faint">The connector list did not include a calendar entry.</p>
                  )
                }
              </DataGate>
            </Panel>
          </div>
        </Grid>
      </PageBody>
    </>
  );
}
