/**
 * Hash routing.
 *
 * Hash routing is deliberate: the Python server serves exactly one shell at `/`
 * and must not need a catch-all rewrite for client routes, and a hash never hits
 * the network - so deep links keep working offline on a single loopback origin.
 */
import { useSyncExternalStore } from "react";
import { routeByPath, ROUTES, type Route, type RouteId } from "@/nav";

const listeners = new Set<() => void>();

function current(): string {
  const raw = window.location.hash.replace(/^#/, "");
  return raw || "/";
}

function emit() {
  listeners.forEach((fn) => fn());
}

export function navigate(id: RouteId | string) {
  const route = ROUTES.find((item) => item.id === id || item.path === id);
  const path = route ? route.path : "/";
  if (current() === path) return;
  window.location.hash = path === "/" ? "#/" : `#${path}`;
}

export function useRoute(): { path: string; route: Route } {
  const path = useSyncExternalStore(
    (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    current,
  );
  return { path, route: routeByPath(path) };
}

export function installRouter() {
  window.addEventListener("hashchange", emit);
  if (!window.location.hash) window.location.hash = "#/";
}
