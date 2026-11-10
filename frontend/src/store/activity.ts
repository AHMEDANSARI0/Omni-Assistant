/**
 * Runtime activity, recorded from real in-flight API work.
 *
 * The AI core needs states like "thinking" and "speaking". Rather than animate
 * a fake story, the core renders the state of work that is actually in progress:
 * a request that has been sent and not yet answered (`thinking`), a speech
 * synthesis call in flight (`speaking`), a blocking task reported by the engine
 * (`executing`), an active capture session (`listening`).
 */
import { useSyncExternalStore } from "react";

export type ActivityKind = "thinking" | "speaking";
type ActivityState = { thinking: number; speaking: number };

let state: ActivityState = { thinking: 0, speaking: 0 };
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((fn) => fn());
}

export function useActivity(kind: ActivityKind): number {
  return useSyncExternalStore(
    (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    () => state[kind],
  );
}

/** Wraps a promise so the core can show the state while it is genuinely running. */
export async function withActivity<T>(kind: ActivityKind, work: () => Promise<T>): Promise<T> {
  state = { ...state, [kind]: state[kind] + 1 };
  emit();
  try {
    return await work();
  } finally {
    state = { ...state, [kind]: Math.max(0, state[kind] - 1) };
    emit();
  }
}
