/**
 * One tiny store, no state library.
 *
 * Why hand-rolled: the dashboard re-renders on a 2 s system poll and a live event
 * stream, so the cost that matters is *how many components re-render per update*.
 * A resource store with per-key subscriptions lets a widget subscribe to exactly
 * one slice (`useResource("system")`) and stay untouched by every other update.
 *
 * Performance rules enforced here:
 *  - polling stops when the tab is hidden (`document.visibilityState`);
 *  - one in-flight request per resource, so a slow endpoint cannot pile up;
 *  - a resource can be marked `streamed` and skip polling entirely;
 *  - every resource has an explicit interval, tuned per endpoint cost.
 */

import { apiGet, ApiError } from "@/lib/api";
import type { AppEvent } from "@/lib/types";

export type ResourceKey =
  | "health"
  | "overview"
  | "system"
  | "agents"
  | "agentStats"
  | "missions"
  | "tasks"
  | "approvals"
  | "events"
  | "providers"
  | "voice"
  | "safety"
  | "knowledge"
  | "tools"
  | "flows"
  | "schedules"
  | "memory"
  | "notifications"
  | "reminders"
  | "agentRuns"
  | "knowledgeDocs"
  | "usage"
  | "automation"
  | "i18n"
  | "a11y"
  | "plugins"
  | "connectors"
  | "sync"
  | "profiles"
  | "settings"
  | "proactive"
  | "flowsStats"
  | "perfBench";

type ResourceState<T = unknown> = {
  data: T | null;
  error: ApiError | null;
  updatedAt: number;
  loading: boolean;
};

export type Snapshot = {
  resources: Record<string, ResourceState>;
  connection: "connecting" | "live" | "polling" | "offline";
  lastEventAt: number;
  eventCount: number;
};

const INTERVALS: Record<ResourceKey, number> = {
  health: 15000,
  overview: 5000,
  system: 2000,
  agents: 8000,
  agentStats: 15000,
  missions: 6000,
  tasks: 5000,
  approvals: 4000,
  events: 0, // streamed
  providers: 20000,
  voice: 4000,
  safety: 10000,
  knowledge: 20000,
  tools: 30000,
  flows: 20000,
  schedules: 15000,
  memory: 0, // needs an explicit query; loaded on demand only
  notifications: 8000,
  reminders: 10000,
  agentRuns: 12000,
  knowledgeDocs: 25000,
  usage: 30000,
  automation: 30000,
  i18n: 0,
  a11y: 0,
  plugins: 0,
  connectors: 0,
  sync: 0,
  profiles: 0,
  settings: 0,
  proactive: 15000,
  flowsStats: 20000,
  perfBench: 0,
};

const PATHS: Record<ResourceKey, string> = {
  health: "/v1/health",
  overview: "/v1/overview",
  system: "/v1/system",
  agents: "/v1/agents",
  agentStats: "/v1/agents/stats",
  missions: "/v1/missions?limit=20",
  tasks: "/v1/tasks?limit=25",
  approvals: "/v1/approvals",
  events: "/v1/events?limit=40",
  providers: "/v1/providers",
  voice: "/v1/voice/status",
  safety: "/v1/safety",
  knowledge: "/v1/knowledge/stats",
  tools: "/v1/tools",
  flows: "/v1/flows",
  schedules: "/v1/schedules",
  // Memory search needs an explicit query, so the dashboard never guesses one.
  memory: "/v1/memory/search?q=&limit=0",
  notifications: "/v1/proactive/notifications?limit=10",
  reminders: "/v1/proactive/reminders?limit=25",
  agentRuns: "/v1/agents/runs?limit=10",
  knowledgeDocs: "/v1/knowledge/documents",
  usage: "/v1/providers/usage",
  automation: "/v1/automation/stats",
  i18n: "/v1/i18n/status",
  a11y: "/v1/a11y/status",
  plugins: "/v1/plugins/status",
  connectors: "/v1/connectors/status",
  sync: "/v1/sync/status",
  profiles: "/v1/profiles",
  settings: "/v1/settings",
  proactive: "/v1/proactive/status",
  flowsStats: "/v1/flows/stats",
  perfBench: "/v1/perf/bench?iterations=5",
};

let snapshot: Snapshot = {
  resources: {},
  connection: "connecting",
  lastEventAt: 0,
  eventCount: 0,
};

const listeners = new Set<() => void>();
const keyListeners = new Map<string, Set<() => void>>();
const timers = new Map<ResourceKey, number>();
const inFlight = new Set<ResourceKey>();
let streamAbort: AbortController | null = null;
let streamTimer: number | null = null;
let started = false;

function emit(key?: string) {
  if (key) keyListeners.get(key)?.forEach((fn) => fn());
  listeners.forEach((fn) => fn());
}

export function getSnapshot(): Snapshot {
  return snapshot;
}

export function subscribeStore(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function subscribeKey(key: string, fn: () => void): () => void {
  const set = keyListeners.get(key) ?? new Set();
  set.add(fn);
  keyListeners.set(key, set);
  return () => set.delete(fn);
}

/**
 * Identity matters here: `useSyncExternalStore` compares snapshots by reference,
 * so an empty resource must be the *same* object on every read or React re-renders
 * forever. One frozen placeholder per key.
 */
const EMPTY = new Map<string, ResourceState>();
function emptyFor(key: string): ResourceState {
  let placeholder = EMPTY.get(key);
  if (!placeholder) {
    placeholder = Object.freeze({ data: null, error: null, updatedAt: 0, loading: true });
    EMPTY.set(key, placeholder);
  }
  return placeholder;
}

export function getResource<K = unknown>(key: string): ResourceState<K> {
  return (snapshot.resources[key] as ResourceState<K>) ?? (emptyFor(key) as ResourceState<K>);
}

function setResource(key: string, next: Partial<ResourceState>) {
  const current = getResource(key);
  snapshot = {
    ...snapshot,
    resources: { ...snapshot.resources, [key]: { ...current, ...next } },
  };
  emit(key);
}

function setConnection(state: Snapshot["connection"]) {
  if (snapshot.connection === state) return;
  snapshot = { ...snapshot, connection: state };
  emit();
}

async function load(key: ResourceKey) {
  if (inFlight.has(key)) return;
  inFlight.add(key);
  try {
    /* eslint-disable @typescript-eslint/no-explicit-any */
    const data = await apiGet<any>(PATHS[key]);
    setResource(key, { data, error: null, updatedAt: Date.now(), loading: false });
    if (snapshot.connection === "offline") setConnection("polling");
  } catch (error) {
    const apiError = error instanceof ApiError ? error : new ApiError(0, "network_error", String(error));
    setResource(key, { error: apiError, loading: false });
    if (apiError.status === 0) setConnection("offline");
  } finally {
    inFlight.delete(key);
  }
}

export async function refresh(key: ResourceKey) {
  await load(key);
}

/** Load a zero-interval resource the first time a view needs it. */
export function ensure(key: ResourceKey) {
  const state = getResource(key);
  if (state.data === null && state.error === null) void load(key);
}

/** Re-read a resource only if it is older than `maxAgeMs` (view switches). */
export function ensureFresh(key: ResourceKey, maxAgeMs = 15000) {
  const state = getResource(key);
  if (state.data === null || Date.now() - state.updatedAt > maxAgeMs) void load(key);
}

/** Manually push a new event list (used by the stream and by optimistic edits). */
export function pushEvent(event: AppEvent) {
  const current = getResource<{ events?: AppEvent[]; total?: number }>("events");
  const list = current.data?.events ?? [];
  if (list.some((item) => item.id === event.id)) return;
  const next = [event, ...list].slice(0, 60);
  setResource("events", {
    data: { events: next, total: (current.data?.total ?? list.length) + 1 },
    updatedAt: Date.now(),
    loading: false,
  });
  snapshot = { ...snapshot, lastEventAt: Date.now(), eventCount: snapshot.eventCount + 1 };
  emit();
}

/** Server-sent event stream (NDJSON). Falls back to polling if it dies twice. */
function startStream() {
  if (streamTimer !== null) return;
  const connect = async () => {
    streamAbort?.abort();
    const controller = new AbortController();
    streamAbort = controller;
    setConnection("connecting");
    let since = getResource<{ events?: AppEvent[] }>("events").data?.events?.[0]?.id ?? 0;
    let opened = false;
    try {
      const response = await fetch(`/v1/events/stream?since_id=${since}&duration=120`, {
        headers: {
          Authorization: `Bearer ${document.querySelector<HTMLMetaElement>('meta[name="pc-agent-token"]')?.content?.trim() ?? ""}`,
          Accept: "application/x-ndjson",
        },
        signal: controller.signal,
        credentials: "omit",
      });
      if (!response.ok || !response.body) throw new Error(`stream ${response.status}`);
      opened = true;
      setConnection("live");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let index = buffer.indexOf("\n");
        while (index >= 0) {
          const line = buffer.slice(0, index).trim();
          buffer = buffer.slice(index + 1);
          if (line) {
            try {
              const parsed = JSON.parse(line) as (AppEvent & { type?: string }) & { type: string; since_id?: number };
              if (parsed.type === "stream.ready") {
                since = Number(parsed.since_id ?? since) || since;
              } else if (parsed.type !== "stream.heartbeat" && typeof parsed.id === "number") {
                since = Math.max(since, parsed.id);
                pushEvent(parsed);
              }
            } catch {
              /* a malformed line must never break the feed */
            }
          }
          index = buffer.indexOf("\n");
        }
      }
    } catch {
      if (!opened) setConnection("polling");
    } finally {
      // Reconnect with a small backoff; the server bounds each stream on purpose.
      streamTimer = window.setTimeout(() => {
        streamTimer = null;
        if (document.visibilityState === "visible") void connect();
      }, opened ? 400 : 4000);
    }
  };
  void connect();
}

function schedule(key: ResourceKey) {
  const interval = INTERVALS[key];
  if (interval <= 0) return;
  const existing = timers.get(key);
  if (existing) window.clearInterval(existing);
  timers.set(
    key,
    window.setInterval(() => {
      if (document.visibilityState === "visible") void load(key);
    }, interval),
  );
}

export function startStore() {
  if (started) return;
  started = true;
  (Object.keys(INTERVALS) as ResourceKey[]).forEach((key) => {
    schedule(key);
    // Zero-interval resources are loaded on demand by the view that needs them,
    // so the dashboard never pays for a screen the user has not opened.
    if (INTERVALS[key] <= 0) return;
    // First paint gets the cheap, high-signal resources; the rest fill in.
    const delay = key === "system" || key === "overview" ? 0 : 120 + Math.random() * 400;
    window.setTimeout(() => void load(key), delay);
  });
  startStream();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      (Object.keys(INTERVALS) as ResourceKey[]).forEach((key) => {
        if (INTERVALS[key] > 0) void load(key);
      });
      startStream();
    } else {
      streamAbort?.abort();
      if (streamTimer !== null) {
        window.clearTimeout(streamTimer);
        streamTimer = null;
      }
    }
  });
}

export function stopStore() {
  timers.forEach((timer) => window.clearInterval(timer));
  timers.clear();
  streamAbort?.abort();
  started = false;
}
