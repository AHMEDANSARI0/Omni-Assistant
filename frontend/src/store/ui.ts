/** Persisted user preferences: theme, motion, layout, sidebar state. */

import { useCallback, useEffect, useSyncExternalStore } from "react";

export type Theme = "dark" | "light";
export type MotionPreference = "system" | "reduce" | "full";
/** `auto` follows the interface language the engine reports (RTL for ar/ur). */
export type Direction = "auto" | "ltr" | "rtl";
export type WidgetId =
  | "agents"
  | "coreoverview"
  | "timeline"
  | "quick"
  | "monitor"
  | "memory"
  | "providers"
  | "voiceactivity"
  | "flows";

export type WidgetLayout = { id: WidgetId; order: number; span: 1 | 2; hidden?: boolean };

export type UiState = {
  theme: Theme;
  motion: MotionPreference;
  direction: Direction;
  sidebarCollapsed: boolean;
  widgets: WidgetLayout[];
};

const KEY = "omni.ui.v1";
const DEFAULT_WIDGETS: WidgetLayout[] = [
  { id: "agents", order: 0, span: 2 },
  { id: "timeline", order: 1, span: 1 },
  { id: "quick", order: 2, span: 1 },
  { id: "coreoverview", order: 3, span: 1 },
  { id: "monitor", order: 4, span: 2 },
  { id: "memory", order: 5, span: 1 },
  { id: "providers", order: 6, span: 1 },
  { id: "voiceactivity", order: 7, span: 2 },
  { id: "flows", order: 8, span: 1 },
];

/**
 * Keep a saved layout usable when a release adds widgets: unknown ids are
 * dropped and any widget the saved layout predates is appended in its default
 * position, so an upgrade never silently hides a new panel.
 */
function normalizeWidgets(saved: unknown): WidgetLayout[] {
  const known = new Set(DEFAULT_WIDGETS.map((widget) => widget.id));
  const kept: WidgetLayout[] = [];
  if (Array.isArray(saved)) {
    for (const entry of saved) {
      const candidate = entry as Partial<WidgetLayout> | null;
      if (!candidate || typeof candidate !== "object") continue;
      if (!candidate.id || !known.has(candidate.id as WidgetId)) continue;
      if (kept.some((widget) => widget.id === candidate.id)) continue;
      kept.push({
        id: candidate.id as WidgetId,
        order: typeof candidate.order === "number" ? candidate.order : kept.length,
        span: candidate.span === 2 ? 2 : 1,
        hidden: candidate.hidden === true,
      });
    }
  }
  if (kept.length === 0) return DEFAULT_WIDGETS;
  for (const widget of DEFAULT_WIDGETS) {
    if (!kept.some((existing) => existing.id === widget.id)) {
      kept.push({ ...widget, order: kept.length });
    }
  }
  return kept.map((widget, index) => ({ ...widget, order: index }));
}

function read(): UiState {
  const fallback: UiState = {
    theme: "dark",
    motion: "system",
    direction: "auto",
    sidebarCollapsed: false,
    widgets: DEFAULT_WIDGETS,
  };
  try {
    // The pre-paint boot script in index.html reads `omni.theme` / `omni.motion`
    // before this module exists. Those two keys stay the authority for the very
    // first frame, so the store must honour them too - otherwise it would undo
    // the theme the page was already painted in.
    const bootTheme = localStorage.getItem("omni.theme");
    // `omni.motion` holds the resolved value for the first paint; the user's
    // actual preference (system/full/reduce) lives in `omni.motion.pref`.
    const bootMotion = localStorage.getItem("omni.motion.pref") ?? localStorage.getItem("omni.motion");
    const boot: Partial<UiState> = {
      theme: bootTheme === "light" || bootTheme === "dark" ? bootTheme : undefined,
      motion:
      bootMotion === "reduce" || bootMotion === "full" || bootMotion === "system" ? (bootMotion as MotionPreference) : undefined,
    };
    const raw = localStorage.getItem(KEY);
    if (!raw) return { ...fallback, ...boot, theme: boot.theme ?? fallback.theme, motion: boot.motion ?? fallback.motion };
    const parsed = JSON.parse(raw) as Partial<UiState>;
    const widgets = normalizeWidgets(parsed.widgets);
    return {
      theme: boot.theme ?? (parsed.theme === "light" ? "light" : "dark"),
      motion: boot.motion ?? (parsed.motion === "reduce" || parsed.motion === "full" ? parsed.motion : "system"),
      direction: parsed.direction === "ltr" || parsed.direction === "rtl" ? parsed.direction : "auto",
      sidebarCollapsed: Boolean(parsed.sidebarCollapsed),
      widgets,
    };
  } catch {
    return fallback;
  }
}

let state: UiState = read();
const listeners = new Set<() => void>();

function commit(next: UiState) {
  state = next;
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    /* a full quota must not break the UI */
  }
  applyToDocument(next);
  listeners.forEach((fn) => fn());
}

function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function reducedMotionActive(candidate: UiState = state): boolean {
  if (candidate.motion === "reduce") return true;
  if (candidate.motion === "full") return false;
  return prefersReducedMotion();
}

function applyToDocument(next: UiState) {
  const root = document.documentElement;
  root.dataset.theme = next.theme;
  root.dataset.motion = reducedMotionActive(next) ? "reduce" : "full";
  // `auto` is resolved by the engine's locale report (see useDirectionSource);
  // until that answer arrives the document keeps the direction it was built in.
  if (next.direction !== "auto") {
    root.dir = next.direction;
    root.dataset.direction = next.direction;
  }
  try {
    localStorage.setItem("omni.theme", next.theme);
    localStorage.setItem("omni.motion", reducedMotionActive(next) ? "reduce" : "full");
    localStorage.setItem("omni.motion.pref", next.motion);
  } catch {
    /* ignore */
  }
}

applyToDocument(state);

export function useUi() {
  const ui = useSyncExternalStore(
    (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    () => state,
  );
  const setTheme = useCallback((theme: Theme) => commit({ ...state, theme }), []);
  const toggleTheme = useCallback(() => commit({ ...state, theme: state.theme === "dark" ? "light" : "dark" }), []);
  const setMotion = useCallback((motion: MotionPreference) => commit({ ...state, motion }), []);
  const setDirection = useCallback((direction: Direction) => {
    if (direction === "auto") {
      document.documentElement.removeAttribute("dir");
      delete document.documentElement.dataset.direction;
    }
    commit({ ...state, direction });
  }, []);
  const toggleSidebar = useCallback(() => commit({ ...state, sidebarCollapsed: !state.sidebarCollapsed }), []);
  const moveWidget = useCallback((id: WidgetId, direction: -1 | 1) => {
    const ordered = [...state.widgets].sort((a, b) => a.order - b.order);
    const index = ordered.findIndex((widget) => widget.id === id);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= ordered.length) return;
    const swapped = [...ordered];
    [swapped[index], swapped[target]] = [swapped[target], swapped[index]];
    commit({ ...state, widgets: swapped.map((widget, position) => ({ ...widget, order: position })) });
  }, []);
  const resetLayout = useCallback(() => commit({ ...state, widgets: DEFAULT_WIDGETS }), []);
  /** Grow or shrink one widget between half and full width. */
  const setSpan = useCallback((id: WidgetId, span: 1 | 2) => {
    commit({
      ...state,
      widgets: state.widgets.map((widget) => (widget.id === id ? { ...widget, span } : widget)),
    });
  }, []);
  /** Hide or restore one widget without losing its position. */
  const toggleWidget = useCallback((id: WidgetId) => {
    commit({
      ...state,
      widgets: state.widgets.map((widget) =>
        widget.id === id ? { ...widget, hidden: !widget.hidden } : widget,
      ),
    });
  }, []);
  /** Whole-order commit, used by drag-and-drop reordering. */
  const setOrder = useCallback((ids: WidgetId[]) => {
    const widgets = ids.map((id, index) => {
      const existing = state.widgets.find((widget) => widget.id === id);
      return {
        id,
        order: index,
        span: existing?.span ?? (1 as const),
        hidden: existing?.hidden ?? false,
      };
    });
    commit({ ...state, widgets });
  }, []);
  return {
    ui,
    setTheme,
    toggleTheme,
    setMotion,
    setDirection,
    toggleSidebar,
    moveWidget,
    setSpan,
    toggleWidget,
    resetLayout,
    setOrder,
  };
}

/**
 * Apply the interface direction the engine reports.
 *
 * The engine is the authority on which language is active and whether it is
 * right-to-left (`/v1/i18n` -> `direction`), so `auto` never guesses from the
 * browser locale. An explicit user choice in Settings always wins.
 */
export function applyAutoDirection(localeDirection: string) {
  if (state.direction !== "auto") return;
  const resolved = localeDirection === "rtl" ? "rtl" : "ltr";
  const root = document.documentElement;
  if (root.dir !== resolved) root.dir = resolved;
  root.dataset.direction = resolved;
}

/** Subscribe to the OS reduced-motion setting for users on "system". */
export function useSystemMotionListener() {
  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const handler = () => {
      if (state.motion === "system") applyToDocument(state);
    };
    query.addEventListener("change", handler);
    return () => query.removeEventListener("change", handler);
  }, []);
}

export const WIDGET_LABELS: Record<WidgetId, string> = {
  agents: "Active agents",
  coreoverview: "AI core overview",
  flows: "Workflows & queue",
  timeline: "Mission timeline",
  quick: "Quick commands",
  monitor: "System monitor",
  memory: "Memory insights",
  providers: "Model status",
  voiceactivity: "Voice activity",
};
