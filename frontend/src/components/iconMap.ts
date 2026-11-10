import {
  IconAgents,
  IconAnalytics,
  IconCalendar,
  IconCenter,
  IconChat,
  IconCore,
  IconFlow,
  IconKnowledge,
  IconMemory,
  IconMission,
  IconModel,
  IconMonitor,
  IconSettings,
  IconShield,
  IconTasks,
  IconTools,
  IconVoice,
} from "@/components/icons";
import type { ComponentType } from "react";

/** String keys keep the route table free of component imports. */
type Glyph = ComponentType<{ size?: number; className?: string }>;

export const ICONS: Record<string, Glyph> = {
  center: IconCenter,
  core: IconCore,
  agents: IconAgents,
  mission: IconMission,
  tasks: IconTasks,
  calendar: IconCalendar,
  memory: IconMemory,
  chat: IconChat,
  knowledge: IconKnowledge,
  tools: IconTools,
  flow: IconFlow,
  model: IconModel,
  voice: IconVoice,
  shield: IconShield,
  monitor: IconMonitor,
  analytics: IconAnalytics,
  settings: IconSettings,
};
