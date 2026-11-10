import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { IconBell, IconMoon, IconRefresh, IconSearch, IconSun } from "@/components/icons";
import { useConnection, useResource } from "@/store/hooks";
import { refresh } from "@/store/store";
import { useUi } from "@/store/ui";
import { useRoute } from "@/store/router";
import type { Health, NotificationsPayload, Overview } from "@/lib/types";
import { relative } from "@/lib/format";

/**
 * Top bar: identity, health, clock, global search entry, notifications, theme,
 * quick settings. Every control here performs a real action.
 */
export function TopBar({ onOpenPalette }: { onOpenPalette: () => void }) {
  const health = useResource<Health>("health");
  const overview = useResource<Overview>("overview");
  const notifications = useResource<NotificationsPayload>("notifications");
  const connection = useConnection();
  const { ui, toggleTheme, toggleSidebar } = useUi();
  const { route } = useRoute();
  const [now, setNow] = useState(() => new Date());
  const [openPanel, setOpenPanel] = useState<null | "notify" | "quick">(null);
  const wrapper = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const onDown = (event: MouseEvent) => {
      if (!wrapper.current?.contains(event.target as Node)) setOpenPanel(null);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, []);

  const ok = health.data?.status === "ok";
  const stopped = health.data?.emergency_stop_engaged === true;
  const pending = overview.data?.pending_approvals ?? 0;
  const connectionLabel =
    connection === "live" ? "Live stream" : connection === "polling" ? "Polling" : connection === "offline" ? "Offline" : "Connecting";

  return (
    <header ref={wrapper} className="relative z-30 flex h-[58px] shrink-0 items-center gap-3 border-b border-line/45 bg-elevated/55 px-3.5 backdrop-blur-xl">
      <button
        type="button"
        onClick={toggleSidebar}
        className="grid h-8 w-8 place-items-center rounded-lg border border-line/60 text-faint transition hover:border-accent/50 hover:text-accent"
        aria-label={ui.sidebarCollapsed ? "Expand navigation" : "Collapse navigation"}
        title="Toggle navigation (Ctrl+B)"
      >
        <span className="flex flex-col gap-[3px]">
          <span className="h-[1.5px] w-3.5 rounded bg-current" />
          <span className="h-[1.5px] w-3.5 rounded bg-current" />
          <span className="h-[1.5px] w-2.5 rounded bg-current" />
        </span>
      </button>

      <div className="min-w-0">
        <p className="hud-label leading-none">Omni-Assistant</p>
        <h1 className="mt-1 truncate font-display text-[13px] font-semibold tracking-[0.1em] text-ink uppercase">{route.label}</h1>
      </div>

      <div className="mx-1 hidden items-center gap-2 rounded-full border border-line/55 bg-inset/55 px-2.5 py-1 lg:flex">
        <span className={`h-1.5 w-1.5 rounded-full ${stopped ? "bg-danger" : ok ? "bg-ok" : "bg-warn"}`} />
        <span className={`text-[11px] font-medium ${stopped ? "text-danger" : ok ? "text-ok" : "text-warn"}`}>
          {stopped ? "Emergency stop" : ok ? "All systems nominal" : health.error ? "Backend offline" : "Checking"}
        </span>
        <span className="text-[10.5px] text-faint">·</span>
        <span className="text-[10.5px] text-faint">{connectionLabel}</span>
        {pending > 0 ? (
          <>
            <span className="text-[10.5px] text-faint">·</span>
            <span className="text-[10.5px] text-warn">{pending} approval{pending === 1 ? "" : "s"}</span>
          </>
        ) : null}
      </div>

      <button
        type="button"
        onClick={onOpenPalette}
        className="group ms-auto flex h-8 min-w-0 items-center gap-2 rounded-lg border border-line/60 bg-inset/50 px-2.5 text-start transition hover:border-accent/50"
        aria-label="Open command palette"
      >
        <IconSearch size={14} className="text-faint group-hover:text-accent" />
        <span className="hidden truncate text-[11.5px] text-faint sm:block">Search or run a command</span>
        <kbd className="ms-2 hidden rounded border border-line/70 px-1 py-[1px] font-mono text-[10px] text-faint md:block">Ctrl K</kbd>
      </button>

      <div className="hidden items-center gap-1.5 text-end md:flex">
        <span className="tabular text-[12px] text-ink">{now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span>
        <span className="hidden text-[11px] text-faint lg:block">{now.toLocaleDateString([], { weekday: "short", day: "2-digit", month: "short" })}</span>
      </div>

      <div className="flex items-center gap-1.5">
        <IconButton label="Refresh live data" onClick={() => { void refresh("system"); void refresh("overview"); void refresh("events"); }}>
          <IconRefresh size={15} />
        </IconButton>

        <div className="relative">
          <IconButton label="Notifications" onClick={() => setOpenPanel(openPanel === "notify" ? null : "notify")} active={openPanel === "notify"}>
            <IconBell size={15} />
            {pending > 0 ? (
              <span className="absolute -top-0.5 -end-0.5 grid h-3.5 min-w-3.5 place-items-center rounded-full bg-danger px-0.5 text-[9px] font-bold text-white">
                {pending > 9 ? "9+" : pending}
              </span>
            ) : null}
          </IconButton>
          <AnimatePresence>
            {openPanel === "notify" ? (
              <Dropdown title="Notifications">
                {notifications.error ? (
                  <p className="px-1 py-2 text-[11.5px] text-danger">{notifications.error.message}</p>
                ) : (notifications.data?.notifications.length ?? 0) === 0 ? (
                  <p className="px-1 py-2 text-[11.5px] text-faint">No notifications recorded by the proactive engine.</p>
                ) : (
                  notifications.data!.notifications.slice(0, 6).map((item) => (
                    <div key={item.id} className="border-b border-line/40 px-1 py-1.5 last:border-0">
                      <p className="text-[11.5px] font-medium text-ink">{item.title}</p>
                      <p className="mt-0.5 line-clamp-2 text-[11px] text-faint">{item.body}</p>
                      <p className="mt-0.5 text-[10px] text-faint/80">{relative(item.created_at)} · {item.kind}</p>
                    </div>
                  ))
                )}
                {pending > 0 ? (
                  <p className="mt-1 rounded bg-warn/10 px-1.5 py-1 text-[11px] text-warn">
                    {pending} approval{pending === 1 ? "" : "s"} waiting in Security Center.
                  </p>
                ) : null}
              </Dropdown>
            ) : null}
          </AnimatePresence>
        </div>

        <IconButton label={ui.theme === "dark" ? "Switch to light mode" : "Switch to dark mode"} onClick={toggleTheme}>
          {ui.theme === "dark" ? <IconSun size={15} /> : <IconMoon size={15} />}
        </IconButton>

        <div className="relative">
          <IconButton label="Quick settings" onClick={() => setOpenPanel(openPanel === "quick" ? null : "quick")} active={openPanel === "quick"}>
            <span className="grid grid-cols-2 gap-[2px]">
              <span className="h-[3px] w-[3px] rounded-full bg-current" />
              <span className="h-[3px] w-[3px] rounded-full bg-current" />
              <span className="h-[3px] w-[3px] rounded-full bg-current" />
              <span className="h-[3px] w-[3px] rounded-full bg-current" />
            </span>
          </IconButton>
          <AnimatePresence>
            {openPanel === "quick" ? (
              <Dropdown title="Quick settings">
                <div className="flex items-center justify-between px-1 py-1.5">
                  <span className="text-[11.5px] text-muted">Theme</span>
                  <div className="flex rounded-md border border-line/60 p-0.5">
                    {(["dark", "light"] as const).map((mode) => (
                      <button
                        key={mode}
                        type="button"
                        onClick={() => { if (ui.theme !== mode) toggleTheme(); }}
                        className={`rounded px-2 py-0.5 text-[11px] capitalize transition ${ui.theme === mode ? "bg-accent/15 text-accent" : "text-faint hover:text-ink"}`}
                      >
                        {mode}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="flex items-center justify-between px-1 py-1.5">
                  <span className="text-[11.5px] text-muted">Motion</span>
                  <MotionChoice />
                </div>
                <div className="mt-1 border-t border-line/45 px-1 pt-1.5 text-[11px] text-faint">
                  Collapse navigation: <kbd className="rounded border border-line/60 px-1 font-mono text-[10px]">Ctrl B</kbd>
                </div>
              </Dropdown>
            ) : null}
          </AnimatePresence>
        </div>
      </div>
    </header>
  );
}

function MotionChoice() {
  const { ui, setMotion } = useUi();
  return (
    <div className="flex rounded-md border border-line/60 p-0.5">
      {(["system", "full", "reduce"] as const).map((mode) => (
        <button
          key={mode}
          type="button"
          onClick={() => setMotion(mode)}
          className={`rounded px-1.5 py-0.5 text-[10.5px] capitalize transition ${ui.motion === mode ? "bg-accent/15 text-accent" : "text-faint hover:text-ink"}`}
        >
          {mode}
        </button>
      ))}
    </div>
  );
}

function IconButton({
  label,
  onClick,
  children,
  active,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
  active?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      className={`relative grid h-8 w-8 place-items-center rounded-lg border transition ${
        active ? "border-accent/55 bg-accent/10 text-accent" : "border-line/60 text-muted hover:border-accent/45 hover:text-accent"
      }`}
    >
      {children}
    </button>
  );
}

function Dropdown({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: -6, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -6, scale: 0.98 }}
      transition={{ duration: 0.16, ease: [0.22, 1, 0.36, 1] }}
      className="glass absolute end-0 top-[38px] z-50 w-[286px] rounded-xl p-2"
      role="dialog"
      aria-label={title}
    >
      <p className="hud-label px-1 pb-1.5">{title}</p>
      <div className="max-h-[320px] overflow-y-auto">{children}</div>
    </motion.div>
  );
}
