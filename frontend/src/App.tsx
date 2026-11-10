import { Suspense, lazy, useCallback, useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Sidebar } from "@/components/Sidebar";
import { TopBar } from "@/components/TopBar";
import { CommandBar } from "@/components/CommandBar";
import { CommandPalette } from "@/components/CommandPalette";
import { Toasts } from "@/components/Toasts";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { CommandCenter } from "@/views/CommandCenter";
import { installRouter, navigate, useRoute } from "@/store/router";
import { ensure, startStore, stopStore } from "@/store/store";
import { applyAutoDirection, useSystemMotionListener, useUi } from "@/store/ui";
import { PanelState } from "@/components/Panel";
import { useResource } from "@/store/hooks";
import type { A11yStatus } from "@/lib/types";
import type { RouteId } from "@/nav";

/* Views other than the dashboard are code-split: the first paint only pays for
   the shell plus the Command Center. */
const AICoreView = lazy(() => import("@/views/AICoreView").then((module) => ({ default: module.AICoreView })));
const Agents = lazy(() => import("@/views/Agents").then((module) => ({ default: module.Agents })));
const Missions = lazy(() => import("@/views/Missions").then((module) => ({ default: module.Missions })));
const Tasks = lazy(() => import("@/views/Tasks").then((module) => ({ default: module.Tasks })));
const CalendarView = lazy(() => import("@/views/Calendar").then((module) => ({ default: module.CalendarView })));
const Memory = lazy(() => import("@/views/Memory").then((module) => ({ default: module.Memory })));
const Conversations = lazy(() => import("@/views/Conversations").then((module) => ({ default: module.Conversations })));
const Knowledge = lazy(() => import("@/views/Knowledge").then((module) => ({ default: module.Knowledge })));
const Tools = lazy(() => import("@/views/Tools").then((module) => ({ default: module.Tools })));
const Workflows = lazy(() => import("@/views/Workflows").then((module) => ({ default: module.Workflows })));
const ModelCenter = lazy(() => import("@/views/ModelCenter").then((module) => ({ default: module.ModelCenter })));
const Voice = lazy(() => import("@/views/Voice").then((module) => ({ default: module.Voice })));
const Security = lazy(() => import("@/views/Security").then((module) => ({ default: module.Security })));
const SystemMonitor = lazy(() => import("@/views/SystemMonitor").then((module) => ({ default: module.SystemMonitor })));
const Analytics = lazy(() => import("@/views/Analytics").then((module) => ({ default: module.Analytics })));
const Settings = lazy(() => import("@/views/Settings").then((module) => ({ default: module.Settings })));

function View({ id, onNavigate }: { id: RouteId; onNavigate: (id: RouteId) => void }) {
  switch (id) {
    case "command-center":
      return <CommandCenter onNavigate={onNavigate} />;
    case "ai-core":
      return <AICoreView />;
    case "agents":
      return <Agents />;
    case "missions":
      return <Missions />;
    case "tasks":
      return <Tasks />;
    case "calendar":
      return <CalendarView />;
    case "memory":
      return <Memory />;
    case "conversations":
      return <Conversations />;
    case "knowledge":
      return <Knowledge />;
    case "tools":
      return <Tools />;
    case "workflows":
      return <Workflows />;
    case "models":
      return <ModelCenter />;
    case "voice":
      return <Voice />;
    case "security":
      return <Security />;
    case "system":
      return <SystemMonitor />;
    case "analytics":
      return <Analytics />;
    case "settings":
      return <Settings />;
    default:
      return <CommandCenter onNavigate={onNavigate} />;
  }
}

export function App() {
  const { route } = useRoute();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const { ui, toggleSidebar } = useUi();
  useSystemMotionListener();
  // `direction: auto` follows the locale the engine reports, not the browser.
  const a11y = useResource<A11yStatus>("a11y");
  useEffect(() => {
    // `a11y` has no polling interval: it is fetched once here so that
    // `direction: auto` resolves on every screen, not only in Settings.
    ensure("a11y");
  }, []);
  useEffect(() => {
    if (a11y.data) applyAutoDirection(a11y.data.direction);
  }, [a11y.data]);

  const onNavigate = useCallback((id: RouteId) => navigate(id), []);

  useEffect(() => {
    installRouter();
    startStore();
    return () => stopStore();
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const modifier = event.ctrlKey || event.metaKey;
      if (modifier && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((value) => !value);
      } else if (modifier && event.key.toLowerCase() === "b") {
        event.preventDefault();
        toggleSidebar();
      } else if (event.key === "Escape") {
        setPaletteOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggleSidebar]);

  return (
    <div className="app-backdrop flex h-full w-full overflow-hidden">
      <Sidebar active={route.id} onNavigate={onNavigate} />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar onOpenPalette={() => setPaletteOpen(true)} />
        <main className="min-h-0 flex-1 overflow-y-auto px-3 py-2.5">
          <AnimatePresence mode="wait">
            <motion.div
              key={route.id}
              initial={{ opacity: 0, y: ui.motion === "reduce" ? 0 : 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: ui.motion === "reduce" ? 0 : -6 }}
              transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
              className="flex min-h-full flex-col"
            >
              <ErrorBoundary label={route.label}>
                <Suspense fallback={<div className="glass rounded-2xl p-6"><PanelState kind="loading" title="Loading module…" /></div>}>
                  <View id={route.id} onNavigate={onNavigate} />
                </Suspense>
              </ErrorBoundary>
            </motion.div>
          </AnimatePresence>
        </main>
        <CommandBar />
      </div>
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} onNavigate={onNavigate} />
      <Toasts />
    </div>
  );
}
