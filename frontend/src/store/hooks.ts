import { useSyncExternalStore } from "react";
import { getResource, getSnapshot, subscribeKey, subscribeStore } from "@/store/store";

/** Subscribe to one resource only: other updates never re-render this component. */
export function useResource<T>(key: string) {
  const state = useSyncExternalStore(
    (fn) => subscribeKey(key, fn),
    () => getResource<T>(key),
  );
  return state;
}

export function useConnection() {
  return useSyncExternalStore(subscribeStore, getSnapshot).connection;
}

export function useEventCounters() {
  const snapshot = useSyncExternalStore(subscribeStore, getSnapshot);
  return { lastEventAt: snapshot.lastEventAt, eventCount: snapshot.eventCount };
}

/** `true` while the resource has never produced data and has no error yet. */
export function isLoading(state: { data: unknown; error: unknown }) {
  return state.data === null && state.error === null;
}
