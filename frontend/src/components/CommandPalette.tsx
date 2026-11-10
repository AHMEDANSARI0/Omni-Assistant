import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { ROUTES, type RouteId } from "@/nav";
import { ICONS } from "@/components/iconMap";
import { IconSearch, IconSend } from "@/components/icons";
import { runInstruction } from "@/lib/actions";
import { notify } from "@/store/toast";
import { refresh } from "@/store/store";
import { useResource } from "@/store/hooks";
import type { SafetyPayload } from "@/lib/types";
import { safetyResume, safetyStop } from "@/lib/actions";

type Command = {
  id: string;
  label: string;
  hint: string;
  group: "Navigate" | "Act";
  run: () => void | Promise<void>;
  disabled?: string;
};

/**
 * Command palette (Ctrl/Cmd+K). Two kinds of entry: navigation, and real
 * actions. An action that cannot run says why; none of them pretend.
 */
export function CommandPalette({
  open,
  onClose,
  onNavigate,
}: {
  open: boolean;
  onClose: () => void;
  onNavigate: (id: RouteId) => void;
}) {
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const safety = useResource<SafetyPayload>("safety");
  const stopped = safety.data?.emergency_stop.engaged === true;

  useEffect(() => {
    if (open) {
      setQuery("");
      setIndex(0);
      window.setTimeout(() => inputRef.current?.focus(), 30);
    }
  }, [open]);

  const commands = useMemo<Command[]>(() => {
    const nav: Command[] = ROUTES.map((route) => ({
      id: `nav:${route.id}`,
      label: route.label,
      hint: route.hint,
      group: "Navigate",
      run: () => onNavigate(route.id),
    }));
    const act: Command[] = [
      {
        id: "act:stop",
        label: "Engage emergency stop",
        hint: "Halts every worker through the safety kernel",
        group: "Act",
        disabled: stopped ? "Already engaged" : undefined,
        run: async () => {
          await safetyStop("Engaged from the command palette.");
          notify("info", "Emergency stop engaged", "All further work is blocked until you resume.");
          await refresh("safety");
        },
      },
      {
        id: "act:resume",
        label: "Release emergency stop",
        hint: "Re-enables execution after a review",
        group: "Act",
        disabled: stopped ? undefined : "Not engaged",
        run: async () => {
          await safetyResume();
          notify("ok", "Emergency stop released");
          await refresh("safety");
        },
      },
      {
        id: "act:sweep",
        label: "Run due schedules now",
        hint: "Asks the scheduler to fire everything that is due",
        group: "Act",
        run: async () => {
          const result = await fetch("/v1/schedules/run-due", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: `Bearer ${document.querySelector<HTMLMetaElement>('meta[name="pc-agent-token"]')?.content ?? ""}`,
            },
            body: "{}",
          }).then((response) => response.json());
          notify(result?.ok ? "ok" : "error", "Scheduler sweep", JSON.stringify(result?.data ?? result?.error ?? {}));
          await refresh("schedules");
          await refresh("tasks");
        },
      },
      {
        id: "act:bench",
        label: "Measure API timings (perf bench)",
        hint: "Runs the real /v1/perf/bench and opens Analytics",
        group: "Act",
        run: async () => {
          onNavigate("analytics");
          await refresh("perfBench");
        },
      },
    ];
    return [...nav, ...act];
  }, [onNavigate, stopped]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return commands;
    return commands.filter(
      (command) =>
        command.label.toLowerCase().includes(needle) || command.hint.toLowerCase().includes(needle),
    );
  }, [commands, query]);

  const execute = async (command: Command | undefined) => {
    if (!command || command.disabled) return;
    onClose();
    try {
      await command.run();
    } catch (error) {
      notify("error", `${command.label} failed`, error instanceof Error ? error.message : String(error));
    }
  };

  const runTyped = async () => {
    const instruction = query.trim();
    if (!instruction) return;
    onClose();
    try {
      const result = await runInstruction(instruction);
      notify("ok", "Instruction queued", result?.task?.id ? `Task ${result.task.id.slice(0, 8)}` : undefined);
      await refresh("tasks");
      await refresh("events");
    } catch (error) {
      notify("error", "Instruction rejected", error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <AnimatePresence>
      {open ? (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.14 }}
          className="fixed inset-0 z-[80] flex items-start justify-center bg-[rgb(2_8_20/0.55)] px-4 pt-[12vh] backdrop-blur-[3px]"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) onClose();
          }}
          role="dialog"
          aria-modal="true"
          aria-label="Command palette"
        >
          <motion.div
            initial={{ opacity: 0, y: -10, scale: 0.985 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -8, scale: 0.99 }}
            transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
            className="glass w-full max-w-[620px] overflow-hidden rounded-2xl"
          >
            <div className="flex items-center gap-2.5 border-b border-line/55 px-3.5 py-3">
              <IconSearch size={16} className="text-accent" />
              <input
                ref={inputRef}
                value={query}
                onChange={(event) => {
                  setQuery(event.target.value);
                  setIndex(0);
                }}
                onKeyDown={(event) => {
                  if (event.key === "ArrowDown") {
                    event.preventDefault();
                    setIndex((value) => Math.min(value + 1, filtered.length - 1));
                  } else if (event.key === "ArrowUp") {
                    event.preventDefault();
                    setIndex((value) => Math.max(value - 1, 0));
                  } else if (event.key === "Enter") {
                    event.preventDefault();
                    const command = filtered[index];
                    if (query.trim() && filtered.length === 0) void runTyped();
                    else void execute(command);
                  } else if (event.key === "Escape") {
                    onClose();
                  }
                }}
                placeholder="Jump to a screen, run an action, or type an instruction…"
                className="h-7 flex-1 bg-transparent text-[13px] text-ink placeholder:text-faint focus:outline-none"
                aria-label="Palette input"
              />
              <kbd className="rounded border border-line/70 px-1 py-[1px] font-mono text-[10px] text-faint">Esc</kbd>
            </div>
            <div className="max-h-[46vh] overflow-y-auto p-1.5">
              {filtered.length === 0 ? (
                <div className="mx-1 mb-1 rounded-lg border border-accent/30 bg-accent/5 p-3">
                  <p className="text-[12px] text-ink">
                    Send “<span className="text-accent">{query.trim().slice(0, 70)}</span>” as an instruction
                  </p>
                  <p className="mt-1 text-[11px] text-faint">
                    This posts to <span className="font-mono">POST /v1/run</span> and creates a real task in the queue.
                  </p>
                  <button
                    type="button"
                    onClick={() => void runTyped()}
                    className="mt-2 flex items-center gap-1.5 rounded-md border border-accent/50 bg-accent/10 px-2.5 py-1 text-[11.5px] font-medium text-accent"
                  >
                    <IconSend size={12} /> Queue instruction
                  </button>
                </div>
              ) : (
                filtered.map((command, position) => {
                  const Icon = command.id.startsWith("nav:")
                    ? ICONS[ROUTES[position]?.icon ?? "center"] ?? ICONS.center
                    : ICONS.shield;
                  return (
                    <button
                      key={command.id}
                      type="button"
                      onMouseEnter={() => setIndex(position)}
                      onClick={() => void execute(command)}
                      disabled={Boolean(command.disabled)}
                      className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-start transition ${
                        position === index ? "bg-accent/10" : ""
                      } ${command.disabled ? "cursor-not-allowed opacity-55" : "hover:bg-accent/10"}`}
                    >
                      <Icon size={15} className={position === index ? "text-accent" : "text-faint"} />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[12.5px] font-medium text-ink">{command.label}</span>
                        <span className="block truncate text-[11px] text-faint">{command.disabled ?? command.hint}</span>
                      </span>
                      <span className="hud-label shrink-0">{command.group}</span>
                    </button>
                  );
                })
              )}
            </div>
          </motion.div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}
