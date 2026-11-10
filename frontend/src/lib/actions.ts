/**
 * Every write the UI can perform, in one place.
 * Each function maps 1:1 onto a documented `/v1` route; none of them fabricates
 * a result, and each returns the API's own payload.
 */
import { apiGet, apiPost, qs } from "@/lib/api";
import type {
  ApprovalsPayload,
  KnowledgeDocumentsPayload,
  MemorySearchPayload,
  MissionsPayload,
  NotificationsPayload,
  RemindersPayload,
  SafetyPayload,
  SchedulesPayload,
  Task,
  TasksPayload,
  VoicePayload,
} from "@/lib/types";

export const runInstruction = (instruction: string, useAi = false) =>
  apiPost<{ task?: Task }>("/v1/run", { instruction, use_ai: useAi });

export const createTask = (instruction: string) => apiPost<Task>("/v1/tasks", { instruction });

export const createMission = (name: string, description: string, instructions: string[]) =>
  apiPost<{ mission?: unknown }>("/v1/missions", {
    name,
    description,
    tasks: instructions.filter((text) => text.trim()).map((instruction) => ({ instruction })),
  });

export const decideApproval = (key: string, approve: boolean, note = "") =>
  apiPost<{ approval?: unknown }>(`/v1/approvals/${encodeURIComponent(key)}/decide`, { approve, note });

export const listApprovals = (status = "pending") =>
  apiGet<ApprovalsPayload>(`/v1/approvals${qs({ status })}`);

export const safetyStop = (reason: string) => apiPost<SafetyPayload>("/v1/safety/stop", { reason });

export const safetyResume = () => apiPost<SafetyPayload>("/v1/safety/resume", {});

export const setScheduleEnabled = (id: string, enabled: boolean) =>
  apiPost<SchedulesPayload>(`/v1/schedules/${encodeURIComponent(id)}/enabled`, { enabled });

export const runDueSchedules = () => apiPost<{ ran?: number }>("/v1/schedules/run-due", {});

export const deleteSchedule = (id: string) =>
  fetchJson<void>(`/v1/schedules/${encodeURIComponent(id)}`, "DELETE");

export const addReminder = (title: string, due: string, note = "") =>
  apiPost<RemindersPayload>("/v1/proactive/reminders", { title, due, note });

export const reminderAction = (id: string, action: "done" | "dismiss" | "snooze") =>
  apiPost<RemindersPayload>(`/v1/proactive/reminders/${encodeURIComponent(id)}/${action}`, action === "snooze" ? { for: "+15m" } : {});

export const deleteReminder = (id: string) =>
  fetchJson<void>(`/v1/proactive/reminders/${encodeURIComponent(id)}`, "DELETE");

export const clearNotifications = () => apiPost<NotificationsPayload>("/v1/proactive/notifications/clear", {});

export const searchMemory = (query: string, limit = 8) =>
  apiGet<MemorySearchPayload>(`/v1/memory/search${qs({ q: query, limit })}`);

export const searchKnowledge = (query: string, limit = 5) =>
  apiGet<{ results?: unknown[] } & Record<string, unknown>>(`/v1/knowledge/search${qs({ q: query, limit })}`);

export const askKnowledge = (question: string) =>
  apiPost<{ answer?: string; citations?: unknown[] } & Record<string, unknown>>("/v1/knowledge/ask", { question });

export const ingestKnowledge = (path: string) =>
  apiPost<{ document?: unknown }>("/v1/knowledge/ingest", { path });

export const listKnowledgeDocuments = () => apiGet<KnowledgeDocumentsPayload>("/v1/knowledge/documents");

export const removeKnowledgeDocument = (id: number) =>
  fetchJson<void>(`/v1/knowledge/documents/${id}`, "DELETE");

export const runAgent = (name: string, instruction: string) =>
  apiPost<{ run?: unknown }>(`/v1/agents/${encodeURIComponent(name)}/run`, { instruction });

export const toggleAgent = (agent: Record<string, unknown>, enabled: boolean) =>
  apiPost<{ agent?: unknown }>("/v1/agents", { ...agent, enabled });

export const voiceEnable = (enabled: boolean) => apiPost<VoicePayload>("/v1/voice/enable", { enabled });

export const voiceWakeWord = (enabled: boolean) => apiPost<VoicePayload>("/v1/voice/wake-word", { enabled });

export const voiceCaptureStart = (mode = "push_to_talk") =>
  apiPost<{ session?: unknown }>("/v1/voice/capture/start", { mode });

export const voiceCaptureStop = (payload: Record<string, unknown> = {}) =>
  apiPost<{ transcript?: string } & Record<string, unknown>>("/v1/voice/capture/stop", payload);

export const voiceCaptureCancel = () => apiPost<{ cancelled?: boolean }>("/v1/voice/capture/cancel", {});

export const voiceSpeak = (text: string) => apiPost<{ spoken?: boolean; engine?: string }>("/v1/voice/speak", { text });

export const voicePurge = () => apiPost<{ purged?: number }>("/v1/voice/purge", {});

export const tickSchedules = () => apiPost<{ ran?: number }>("/v1/schedules/run-due", {});

export const flowTick = () => apiPost<{ fired?: number }>("/v1/flows/tick", {});

export const runEndpointsBench = () => apiGet<Record<string, unknown>>("/v1/perf/bench?iterations=5");

export const setLocale = (locale: string) => apiPost<{ locale?: string }>("/v1/i18n/locale", { locale });

export const proactiveTick = () => apiPost<{ fired?: number }>("/v1/proactive/tick", {});

export const listTasks = (status?: string, limit = 50) =>
  apiGet<TasksPayload>(`/v1/tasks${qs({ limit, status })}`);

export const listMissions = () => apiGet<MissionsPayload>("/v1/missions?limit=50");

export const listReminders = () => apiGet<RemindersPayload>("/v1/proactive/reminders");

/** Small DELETE helper: the API answers `{ok, data}` like every other route. */
async function fetchJson<T>(path: string, method: "DELETE"): Promise<T> {
  const token = document.querySelector<HTMLMetaElement>('meta[name="pc-agent-token"]')?.content?.trim() ?? "";
  const response = await fetch(path, {
    method,
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    credentials: "omit",
  });
  const payload = (await response.json().catch(() => null)) as { ok?: boolean; data?: T; error?: { message?: string } } | null;
  if (!response.ok || !payload?.ok) {
    throw new Error(payload?.error?.message ?? `Request failed with status ${response.status}`);
  }
  return payload.data as T;
}
