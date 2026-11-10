/** Small formatting helpers. Pure functions, no state, no fake defaults. */

export function pct(value: unknown, digits = 0): string {
  const number = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${number.toFixed(digits)}%`;
}

export function mb(value: unknown): string {
  const number = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(number)) return "—";
  if (number >= 1024) return `${(number / 1024).toFixed(1)} GB`;
  return `${number.toFixed(0)} MB`;
}

export function bps(value: unknown): string {
  const number = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(number) || number <= 0) return "0 B/s";
  const units = ["B/s", "KB/s", "MB/s", "GB/s"];
  let scaled = number;
  let unit = 0;
  while (scaled >= 1024 && unit < units.length - 1) {
    scaled /= 1024;
    unit += 1;
  }
  return `${scaled.toFixed(scaled >= 100 ? 0 : 1)} ${units[unit]}`;
}

export function clock(value: string | number | undefined | null): string {
  if (value === undefined || value === null || value === "") return "—";
  const date = typeof value === "number" ? new Date(value) : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function relative(value: string | number | undefined | null): string {
  if (value === undefined || value === null || value === "") return "—";
  const date = typeof value === "number" ? new Date(value) : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return date.toLocaleDateString();
}

export function shorten(value: string | undefined | null, length = 90): string {
  if (!value) return "";
  const text = value.trim();
  return text.length > length ? `${text.slice(0, length - 1)}…` : text;
}

/** Map an event type to a severity used for the feed marker. */
export function eventSeverity(type: string): "ok" | "warn" | "danger" | "info" | "violet" {
  if (/fail|error|denied|refused|blocked|timeout/i.test(type)) return "danger";
  if (/approval|pending|waiting|budget|retry|degraded/i.test(type)) return "warn";
  if (/complete|success|done|pass|verified|applied/i.test(type)) return "ok";
  if (/voice|agent|mission|plan/i.test(type)) return "violet";
  return "info";
}
