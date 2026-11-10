import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { GROUP_LABELS, ROUTES, type Route, type RouteId } from "@/nav";
import { IconLogo, IconChevron } from "@/components/icons";
import { ICONS } from "@/components/iconMap";
import { useResource } from "@/store/hooks";
import type { Health, Overview } from "@/lib/types";
import { useUi } from "@/store/ui";

export function Sidebar({ active, onNavigate }: { active: RouteId; onNavigate: (id: RouteId) => void }) {
  const health = useResource<Health>("health");
  const overview = useResource<Overview>("overview");
  const { ui } = useUi();
  const collapsed = ui.sidebarCollapsed;
  const [clock, setClock] = useState(() => new Date());

  // One timer for the whole shell; the sidebar clock ticks each second.
  useEffect(() => {
    const timer = window.setInterval(() => setClock(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const ok = health.data?.status === "ok";
  const stopped = health.data?.emergency_stop_engaged === true;
  const badgeFor = (route: Route): number | null => {
    if (route.id === "agents") return null;
    if (route.id === "tasks") return overview.data?.tasks.total ?? null;
    if (route.id === "missions") return overview.data?.missions ?? null;
    if (route.id === "security") return overview.data?.pending_approvals ?? null;
    if (route.id === "calendar") return overview.data?.schedules ?? null;
    return null;
  };

  const groups = ["core", "work", "intelligence", "control"] as const;

  return (
    <motion.aside
      layout
      transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
      className="relative z-20 flex h-full shrink-0 flex-col border-e border-line/45 bg-elevated/70 backdrop-blur-xl"
      style={{ width: collapsed ? 72 : 246 }}
      aria-label="Primary navigation"
    >
      <div className={`flex items-center gap-2.5 px-4 pt-4 pb-3 ${collapsed ? "justify-center px-0" : ""}`}>
        <span className="relative grid place-items-center">
          <span className="absolute inset-0 rounded-full bg-accent/20 blur-md" aria-hidden />
          <motion.span
            animate={ui.motion === "reduce" ? undefined : { rotate: 360 }}
            transition={{ duration: 46, repeat: Infinity, ease: "linear" }}
            className="relative grid place-items-center"
          >
            <IconLogo size={collapsed ? 30 : 34} />
          </motion.span>
        </span>
        {!collapsed ? (
          <div className="min-w-0">
            <p className="font-display text-[13.5px] font-semibold tracking-[0.16em] text-ink uppercase">Omni</p>
            <p className="hud-label truncate">Assistant core</p>
          </div>
        ) : null}
      </div>

      {!collapsed ? (
        <div className="mx-3 mb-3 rounded-xl border border-line/50 bg-inset/60 px-3 py-2">
          <div className="flex items-center justify-between">
            <span className="hud-label">Runtime</span>
            <span className={`flex items-center gap-1.5 text-[11px] font-medium ${stopped ? "text-danger" : ok ? "text-ok" : "text-warn"}`}>
              <span className={`h-1.5 w-1.5 rounded-full ${stopped ? "bg-danger" : ok ? "bg-ok" : "bg-warn"}`} />
              {stopped ? "STOPPED" : ok ? "ONLINE" : health.error ? "OFFLINE" : "…"}
            </span>
          </div>
          <div className="mt-1.5 flex items-center justify-between text-[11px] text-faint">
            <span className="tabular">v{health.data?.version ?? "—"}</span>
            <span className="tabular">{clock.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span>
          </div>
        </div>
      ) : null}

      <nav className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {groups.map((group) => (
          <div key={group} className="mb-1">
            {!collapsed ? <p className="hud-label px-2 pt-2 pb-1.5">{GROUP_LABELS[group]}</p> : <div className="h-2" />}
            {ROUTES.filter((route) => route.group === group).map((route) => (
              <NavItem
                key={route.id}
                route={route}
                active={active === route.id}
                collapsed={collapsed}
                badge={badgeFor(route)}
                onNavigate={onNavigate}
              />
            ))}
          </div>
        ))}
      </nav>

      <div className={`border-t border-line/45 p-2.5 ${collapsed ? "flex justify-center" : ""}`}>
        {!collapsed ? (
          <div className="rounded-lg bg-inset/50 px-2.5 py-2">
            <p className="hud-label">Device</p>
            <p className="mt-0.5 truncate text-[11.5px] text-muted">Local · {health.data?.active_profile ?? "no profile"}</p>
          </div>
        ) : null}
      </div>
    </motion.aside>
  );
}

function NavItem({
  route,
  active,
  collapsed,
  badge,
  onNavigate,
}: {
  route: Route;
  active: boolean;
  collapsed: boolean;
  badge: number | null;
  onNavigate: (id: RouteId) => void;
}) {
  const Icon = ICONS[route.icon] ?? ICONS.center;
  return (
    <button
      type="button"
      onClick={() => onNavigate(route.id)}
      title={collapsed ? `${route.label} — ${route.hint}` : route.hint}
      aria-current={active ? "page" : undefined}
      className={`group relative mb-0.5 flex w-full items-center gap-2.5 rounded-lg px-2.5 py-[7px] text-start transition-colors duration-200 ${
        active ? "text-ink" : "text-muted hover:text-ink"
      } ${collapsed ? "justify-center px-0" : ""}`}
    >
      {active ? (
        <motion.span
          layoutId="nav-active"
          transition={{ duration: 0.26, ease: [0.22, 1, 0.36, 1] }}
          className="absolute inset-0 rounded-lg border border-accent/35 bg-accent/10"
        />
      ) : (
        <span className="absolute inset-0 rounded-lg opacity-0 transition-opacity duration-200 group-hover:bg-inset/70 group-hover:opacity-100" />
      )}
      <span className={`relative ${active ? "text-accent" : "text-faint group-hover:text-accent"} transition-colors`}>
        <Icon size={17} />
      </span>
      {!collapsed ? (
        <>
          <span className="relative flex-1 truncate text-[12.5px] font-medium">{route.label}</span>
          {badge !== null && badge > 0 ? (
            <span className="relative rounded-full bg-accent/15 px-1.5 py-[1px] text-[10px] font-semibold tabular text-accent">{badge}</span>
          ) : null}
          <IconChevron size={11} className={`relative text-faint/50 transition-opacity ${active ? "opacity-100" : "opacity-0"}`} />
        </>
      ) : null}
    </button>
  );
}
