import { useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Badge, Dot, Panel, PanelState } from "@/components/Panel";
import { useResource } from "@/store/hooks";
import { refresh } from "@/store/store";
import type {
  ApprovalsPayload,
  EventsPayload,
  Health,
  KnowledgeStatsPayload,
  ProvidersPayload,
  TasksPayload,
  VoicePayload,
} from "@/lib/types";
import { eventSeverity, relative, shorten } from "@/lib/format";
import { decideApproval } from "@/lib/actions";
import { attempt } from "@/store/toast";
import type { RouteId } from "@/nav";

type Tab = "activity" | "approvals" | "alerts" | "insights";

const SEVERITY_DOT: Record<string, "ok" | "warn" | "danger" | "accent" | "violet"> = {
  ok: "ok",
  warn: "warn",
  danger: "danger",
  info: "accent",
  violet: "violet",
};

/**
 * Right-hand intelligence panel: activity feed, approvals that need a human,
 * failures, and recommendations derived from measured state.
 */
export function IntelligenceFeed({ onNavigate }: { onNavigate: (id: RouteId) => void }) {
  const [tab, setTab] = useState<Tab>("activity");
  const events = useResource<EventsPayload>("events");
  const approvals = useResource<ApprovalsPayload>("approvals");
  const tasks = useResource<TasksPayload>("tasks");
  const pending = approvals.data?.pending_count ?? 0;

  const failedTasks = useMemo(
    () => (tasks.data?.tasks ?? []).filter((task) => task.status === "failed" || task.error),
    [tasks.data],
  );
  const alertEvents = useMemo(
    () => (events.data?.events ?? []).filter((event) => ["danger", "warn"].includes(eventSeverity(event.event_type))),
    [events.data],
  );

  return (
    <Panel
      title="Intelligence"
      subtitle="Live signal from the local engine"
      className="h-full"
      badge={pending > 0 ? <Badge tone="warn">{pending} to decide</Badge> : undefined}
    >
      <div className="flex h-full min-h-0 flex-col">
        {/* Tabs live on their own row: sharing the title row truncated the title
            at panel widths under ~360 px. */}
        <div className="mb-1.5 flex shrink-0 gap-0.5 rounded-md border border-line/55 p-0.5">
          {(["activity", "approvals", "alerts", "insights"] as Tab[]).map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => setTab(item)}
              aria-pressed={tab === item}
              className={`flex-1 rounded px-1 py-[2px] text-[10px] capitalize transition ${
                tab === item ? "bg-accent/15 text-accent" : "text-faint hover:text-ink"
              }`}
            >
              {item}
              {item === "approvals" && pending > 0 ? <span className="ms-0.5 text-warn">·</span> : null}
            </button>
          ))}
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto pe-0.5">
          <AnimatePresence mode="wait">
            <motion.div
            key={tab}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
          >
            {tab === "activity" ? <ActivityList events={events} /> : null}
            {tab === "approvals" ? <ApprovalList approvals={approvals} onNavigate={onNavigate} /> : null}
            {tab === "alerts" ? <AlertList failures={failedTasks} events={alertEvents} onNavigate={onNavigate} /> : null}
            {tab === "insights" ? <Insights onNavigate={onNavigate} /> : null}
            </motion.div>
          </AnimatePresence>
        </div>
      </div>
    </Panel>
  );
}

function ActivityList({ events }: { events: { data: EventsPayload | null; error: import("@/lib/api").ApiError | null } }) {
  if (events.error) return <PanelState kind="error" detail={events.error.message} onRetry={() => void refresh("events")} />;
  if (events.data === null) return <PanelState kind="loading" />;
  if (events.data.events.length === 0)
    return <PanelState kind="empty" detail="The engine writes an event for every action it takes. Start by queueing an instruction." />;
  return (
    <ol className="space-y-0.5">
      {events.data.events.slice(0, 26).map((event) => {
        const severity = eventSeverity(event.event_type);
        return (
          <li key={event.id} className="group relative flex gap-2.5 rounded-lg px-1.5 py-1.5 transition hover:bg-inset/60">
            <span className="mt-[5px] shrink-0">
              <Dot tone={SEVERITY_DOT[severity] ?? "accent"} />
            </span>
            <div className="min-w-0 flex-1">
              <p className="line-clamp-2 text-[11.5px] leading-snug text-ink">{shorten(event.summary, 120)}</p>
              {/* The meta line is a flexible row of variable-length pieces: an
                  event type can be 30+ characters, and before 0.82.13 it simply
                  ran past the panel and was clipped by the overflow container
                  (measured: 353 px inside 306 px). It wraps now, and the type
                  ellipsises instead of pushing the timestamp out of view. */}
              <p className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[10px] text-faint">
                <span className="min-w-0 max-w-full truncate font-mono text-[9.5px] tracking-wide text-faint/85">
                  {event.event_type}
                </span>
                <span aria-hidden="true">·</span>
                <span className="shrink-0" title={event.created_at}>
                  {relative(event.created_at)}
                </span>
                {event.task_id ? (
                  <>
                    <span aria-hidden="true">·</span>
                    <span className="shrink-0 font-mono text-[9.5px]">{event.task_id.slice(0, 8)}</span>
                  </>
                ) : null}
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function ApprovalList({
  approvals,
  onNavigate,
}: {
  approvals: { data: ApprovalsPayload | null; error: import("@/lib/api").ApiError | null };
  onNavigate: (id: RouteId) => void;
}) {
  if (approvals.error) return <PanelState kind="error" detail={approvals.error.message} onRetry={() => void refresh("approvals")} />;
  if (approvals.data === null) return <PanelState kind="loading" />;
  if (approvals.data.requests.length === 0)
    return (
      <PanelState
        kind="empty"
        title="No approvals waiting"
        detail="The safety kernel asks for an exact-scope approval before any tool with side effects runs. Nothing is pending right now."
      />
    );
  return (
    <div className="space-y-1.5">
      {approvals.data.requests.map((request) => (
        <div key={request.key} className="rounded-lg border border-warn/30 bg-warn/5 p-2">
          <p className="text-[11.5px] font-medium text-ink">{shorten(String(request.summary ?? request.tool ?? "Tool request"), 76)}</p>
          <p className="mt-0.5 font-mono text-[10px] text-faint">
            {String(request.tool_id ?? request.tool ?? "unknown tool")} · {request.requested_at ? relative(String(request.requested_at)) : "—"}
          </p>
          <div className="mt-1.5 flex gap-1.5">
            <button
              type="button"
              onClick={async () => {
                await attempt("Approved", () => decideApproval(request.key, true));
                await refresh("approvals");
                await refresh("events");
              }}
              className="rounded border border-ok/50 bg-ok/10 px-2 py-[3px] text-[10.5px] font-medium text-ok transition hover:bg-ok/20"
            >
              Approve
            </button>
            <button
              type="button"
              onClick={async () => {
                await attempt("Denied", () => decideApproval(request.key, false));
                await refresh("approvals");
                await refresh("events");
              }}
              className="rounded border border-danger/50 bg-danger/10 px-2 py-[3px] text-[10.5px] font-medium text-danger transition hover:bg-danger/20"
            >
              Deny
            </button>
            <button
              type="button"
              onClick={() => onNavigate("security")}
              className="rounded border border-line/60 px-2 py-[3px] text-[10.5px] text-muted transition hover:border-accent/50 hover:text-accent"
            >
              Details
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

function AlertList({
  failures,
  events,
  onNavigate,
}: {
  failures: { id: string; instruction: string; error: string | null; updated_at: string }[];
  events: { id: number; event_type: string; summary: string; created_at: string }[];
  onNavigate: (id: RouteId) => void;
}) {
  if (failures.length === 0 && events.length === 0)
    return <PanelState kind="empty" title="No failures or warnings" detail="Task failures and warning-level events appear here the moment they happen." />;
  return (
    <div className="space-y-1.5">
      {failures.map((task) => (
        <button
          key={task.id}
          type="button"
          onClick={() => onNavigate("tasks")}
          className="block w-full rounded-lg border border-danger/35 bg-danger/5 p-2 text-start transition hover:border-danger/60"
        >
          <p className="flex items-center gap-1.5 text-[10px] font-semibold tracking-wide text-danger uppercase">
            Task failed <span className="font-mono text-[9.5px] font-normal text-faint">{task.id.slice(0, 8)}</span>
          </p>
          <p className="mt-0.5 truncate text-[11.5px] text-ink">{shorten(task.instruction, 70)}</p>
          {task.error ? <p className="mt-0.5 line-clamp-2 text-[10.5px] text-faint">{task.error}</p> : null}
        </button>
      ))}
      {events.slice(0, 10).map((event) => (
        <div key={event.id} className="rounded-lg border border-warn/25 bg-warn/5 p-2">
          <p className="truncate text-[11.5px] text-ink">{shorten(event.summary, 76)}</p>
          <p className="mt-0.5 flex gap-1.5 font-mono text-[9.5px] text-faint">
            <span>{event.event_type}</span>
            <span>·</span>
            <span>{relative(event.created_at)}</span>
          </p>
        </div>
      ))}
    </div>
  );
}

/**
 * Recommendations are rules over real state - each one names the measurement it
 * comes from and the action that would change it. Nothing is invented.
 */
function Insights({ onNavigate }: { onNavigate: (id: RouteId) => void }) {
  const health = useResource<Health>("health");
  const providers = useResource<ProvidersPayload>("providers");
  const voice = useResource<VoicePayload>("voice");
  const knowledge = useResource<KnowledgeStatsPayload>("knowledge");
  const approvals = useResource<ApprovalsPayload>("approvals");

  const items: { title: string; why: string; action: string; route: RouteId; tone: "warn" | "accent" | "violet" }[] = [];

  if ((approvals.data?.pending_count ?? 0) > 0) {
    items.push({
      title: `${approvals.data?.pending_count} approval(s) are blocking tool work`,
      why: "Measured from /v1/approvals pending_count.",
      action: "Review in Security Center",
      route: "security",
      tone: "warn",
    });
  }
  if (providers.data && providers.data.providers.length === 0) {
    items.push({
      title: "No cloud provider is configured",
      why: "Every task currently routes to the offline mock engine; cloud models stay optional.",
      action: "Open Model Center",
      route: "models",
      tone: "accent",
    });
  }
  if (voice.data && !voice.data.stt.available) {
    items.push({
      title: "Speech-to-text engine not available",
      why: voice.data.stt.reason.split("(")[0].trim(),
      action: "Voice setup details",
      route: "voice",
      tone: "violet",
    });
  }
  if (knowledge.data && knowledge.data.knowledge.documents === 0) {
    items.push({
      title: "Knowledge base is empty",
      why: "Embeddings are local; ingesting a file gives recall something real to search.",
      action: "Ingest a document",
      route: "knowledge",
      tone: "accent",
    });
  }
  if (health.data && health.data.schema_version !== health.data.schema_latest) {
    items.push({
      title: "Schema is behind the latest migration",
      why: `Running schema ${health.data.schema_version}, latest ${health.data.schema_latest}.`,
      action: "Open System Monitor",
      route: "system",
      tone: "warn",
    });
  }

  if (items.length === 0) {
    return <PanelState kind="empty" title="Nothing needs attention" detail="Every recommendation rule currently measures a healthy value." />;
  }

  return (
    <div className="space-y-1.5">
      {items.map((item) => (
        <div key={item.title} className="rounded-lg border border-line/55 bg-inset/40 p-2">
          <p className="text-[11.5px] font-medium text-ink">{item.title}</p>
          <p className="mt-0.5 text-[10.5px] leading-relaxed text-faint">{item.why}</p>
          <button
            type="button"
            onClick={() => onNavigate(item.route)}
            className="mt-1.5 rounded border border-accent/40 px-2 py-[2px] text-[10.5px] text-accent transition hover:bg-accent/10"
          >
            {item.action}
          </button>
        </div>
      ))}
    </div>
  );
}
