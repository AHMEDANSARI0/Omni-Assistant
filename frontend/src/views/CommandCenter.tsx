import { useEffect } from "react";
import { AICore, CoreHeadline } from "@/components/AICore";
import { IntelligenceFeed } from "@/components/IntelligenceFeed";
import { WidgetGrid } from "@/components/WidgetGrid";
import {
  AgentsWidget,
  CoreOverviewWidget,
  FlowsWidget,
  MemoryWidget,
  MonitorWidget,
  ProvidersWidget,
  QuickCommands,
  TimelineWidget,
  VoiceActivityWidget,
  primeWidgetData,
} from "@/components/widgets";
import type { RouteId } from "@/nav";
import type { WidgetId } from "@/store/ui";

const RENDER: Record<WidgetId, (onNavigate: (id: RouteId) => void) => JSX.Element> = {
  agents: (onNavigate) => <AgentsWidget onNavigate={onNavigate} />,
  coreoverview: (onNavigate) => <CoreOverviewWidget onNavigate={onNavigate} />,
  flows: (onNavigate) => <FlowsWidget onNavigate={onNavigate} />,
  timeline: (onNavigate) => <TimelineWidget onNavigate={onNavigate} />,
  quick: (onNavigate) => <QuickCommands onNavigate={onNavigate} />,
  monitor: (onNavigate) => <MonitorWidget onNavigate={onNavigate} />,
  memory: (onNavigate) => <MemoryWidget onNavigate={onNavigate} />,
  providers: (onNavigate) => <ProvidersWidget onNavigate={onNavigate} />,
  voiceactivity: (onNavigate) => <VoiceActivityWidget onNavigate={onNavigate} />,
};

/**
 * The reference layout: navigation on the left, holographic core in the middle,
 * intelligence feed on the right, configurable widgets underneath.
 */
export function CommandCenter({ onNavigate }: { onNavigate: (id: RouteId) => void }) {
  useEffect(() => {
    primeWidgetData();
  }, []);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2.5">
      <div className="grid min-h-0 grid-cols-1 gap-2.5 xl:grid-cols-[minmax(0,1fr)_340px]">
        <section className="glass hud-corners relative flex h-[clamp(240px,42vh,420px)] min-h-0 flex-col rounded-2xl">
          <div className="flex items-center justify-between gap-2 px-3.5 pt-3">
            <div className="min-w-0">
              <h1 className="font-display text-[12.5px] font-semibold tracking-hud text-ink uppercase">Command Center</h1>
              <CoreHeadline />
            </div>
            <button
              type="button"
              onClick={() => onNavigate("ai-core")}
              className="rounded border border-line/60 px-1.5 py-[2px] text-[10px] text-muted transition hover:border-accent/50 hover:text-accent"
            >
              Core detail
            </button>
          </div>
          <AICore />
        </section>
        <div className="h-[clamp(240px,42vh,420px)] min-h-0">
          <IntelligenceFeed onNavigate={onNavigate} />
        </div>
      </div>
      <div>
        <WidgetGrid render={(id, nav) => RENDER[id](nav)} onNavigate={onNavigate} />
      </div>
    </div>
  );
}


