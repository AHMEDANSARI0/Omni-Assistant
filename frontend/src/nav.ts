/**
 * Route table. One source of truth for the sidebar, the command palette and the
 * router, so a destination can never exist in one place and not another.
 */
import type { ComponentType } from "react";

export type RouteId =
  | "command-center"
  | "ai-core"
  | "agents"
  | "missions"
  | "tasks"
  | "calendar"
  | "memory"
  | "conversations"
  | "knowledge"
  | "tools"
  | "workflows"
  | "models"
  | "voice"
  | "security"
  | "system"
  | "analytics"
  | "settings";

export type Route = {
  id: RouteId;
  label: string;
  path: string;
  group: "core" | "work" | "intelligence" | "control";
  icon: string;
  hint: string;
};

export const ROUTES: Route[] = [
  { id: "command-center", label: "Command Center", path: "/", group: "core", icon: "center", hint: "Live dashboard: AI core, feed, widgets" },
  { id: "ai-core", label: "AI Core", path: "/core", group: "core", icon: "core", hint: "Capability matrix and runtime state" },
  { id: "agents", label: "Agents", path: "/agents", group: "work", icon: "agents", hint: "Specialist roster and runs" },
  { id: "missions", label: "Missions", path: "/missions", group: "work", icon: "mission", hint: "Multi-step goals and progress" },
  { id: "tasks", label: "Tasks", path: "/tasks", group: "work", icon: "tasks", hint: "Task queue and results" },
  { id: "calendar", label: "Calendar", path: "/calendar", group: "work", icon: "calendar", hint: "Reminders, schedules, calendar" },
  { id: "memory", label: "Memory", path: "/memory", group: "intelligence", icon: "memory", hint: "Recall across notes and documents" },
  { id: "conversations", label: "Conversations", path: "/conversations", group: "intelligence", icon: "chat", hint: "Instruction history and outcomes" },
  { id: "knowledge", label: "Knowledge Base", path: "/knowledge", group: "intelligence", icon: "knowledge", hint: "Ingest, search and ask" },
  { id: "tools", label: "Tools & Skills", path: "/tools", group: "control", icon: "tools", hint: "Registered tools and permissions" },
  { id: "workflows", label: "Workflows", path: "/workflows", group: "control", icon: "flow", hint: "Saved flows and triggers" },
  { id: "models", label: "Model Center", path: "/models", group: "control", icon: "model", hint: "Providers, routing, usage" },
  { id: "voice", label: "Voice Assistant", path: "/voice", group: "control", icon: "voice", hint: "Microphone, engines, privacy" },
  { id: "security", label: "Security Center", path: "/security", group: "control", icon: "shield", hint: "Safety kernel, approvals, plugins" },
  { id: "system", label: "System Monitor", path: "/system", group: "control", icon: "monitor", hint: "CPU, memory, disk, network" },
  { id: "analytics", label: "Analytics", path: "/analytics", group: "control", icon: "analytics", hint: "Measured runtime and API timings" },
  { id: "settings", label: "Settings", path: "/settings", group: "control", icon: "settings", hint: "Theme, motion, locale, profiles" },
];

export const GROUP_LABELS: Record<Route["group"], string> = {
  core: "Overview",
  work: "Operations",
  intelligence: "Intelligence",
  control: "Control",
};

export function routeByPath(path: string): Route {
  const clean = path.replace(/\/+$/, "") || "/";
  return ROUTES.find((route) => route.path === clean) ?? ROUTES[0];
}

export type ViewProps = { onNavigate: (id: RouteId) => void };
export type ViewComponent = ComponentType<ViewProps>;
