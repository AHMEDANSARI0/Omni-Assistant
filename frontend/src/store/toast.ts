/**
 * Result feedback for every write the UI performs.
 *
 * A write action (approve, stop, run, toggle) reports either the truth from the
 * API or the API's own error message. There is no optimistic "success" path.
 */
import { useSyncExternalStore } from "react";

export type Toast = { id: number; tone: "ok" | "error" | "info"; title: string; detail?: string };

let items: Toast[] = [];
let nextId = 1;
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((fn) => fn());
}

export function notify(tone: Toast["tone"], title: string, detail?: string) {
  const toast = { id: nextId++, tone, title, detail };
  items = [toast, ...items].slice(0, 4);
  emit();
  window.setTimeout(() => dismiss(toast.id), tone === "error" ? 9000 : 4500);
}

export function dismiss(id: number) {
  items = items.filter((item) => item.id !== id);
  emit();
}

export function useToasts(): Toast[] {
  return useSyncExternalStore(
    (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    () => items,
  );
}

/** Runs an action and turns any failure into a visible error toast. */
export async function attempt<T>(label: string, action: () => Promise<T>): Promise<T | null> {
  try {
    const result = await action();
    notify("ok", label);
    return result;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    notify("error", `${label} failed`, message);
    return null;
  }
}
