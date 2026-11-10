/**
 * The one panel primitive.
 *
 * Every widget uses it, so loading / empty / error / unavailable states look the
 * same everywhere and none of them can be skipped by accident: a panel cannot
 * render content without passing through a state check first.
 */
import type { ReactNode } from "react";
import { motion } from "framer-motion";
import { ApiError } from "@/lib/api";

type PanelProps = {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  dense?: boolean;
  badge?: ReactNode;
};

export function Panel({ title, subtitle, actions, children, className = "", dense, badge }: PanelProps) {
  return (
    <section className={`glass hud-corners relative flex min-h-0 flex-col rounded-2xl ${className}`}>
      <header className={`flex items-start justify-between gap-3 ${dense ? "px-3.5 pt-3 pb-2" : "px-4 pt-3.5 pb-2.5"}`}>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="truncate font-display text-[12.5px] font-semibold tracking-hud text-ink uppercase">{title}</h2>
            {badge}
          </div>
          {subtitle ? <p className="mt-1 truncate text-[11.5px] text-faint">{subtitle}</p> : null}
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-1.5">{actions}</div> : null}
      </header>
      <div className={`min-h-0 flex-1 ${dense ? "px-3.5 pb-3" : "px-4 pb-3.5"}`}>{children}</div>
    </section>
  );
}

export function PanelState({
  kind,
  title,
  detail,
  onRetry,
}: {
  kind: "loading" | "empty" | "error" | "unavailable";
  title?: string;
  detail?: string;
  onRetry?: () => void;
}) {
  const tone =
    kind === "error" ? "text-danger" : kind === "unavailable" ? "text-warn" : "text-faint";
  const label =
    title ??
    (kind === "loading"
      ? "Reading live state…"
      : kind === "empty"
        ? "Nothing recorded yet"
        : kind === "error"
          ? "Backend unreachable"
          : "Not available in this build");
  return (
    <div className="flex h-full min-h-[92px] flex-col items-center justify-center gap-2 text-center">
      <div className="flex items-center gap-2">
        {kind === "loading" ? (
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" aria-hidden />
        ) : (
          <span className={`h-1.5 w-1.5 rounded-full ${kind === "error" ? "bg-danger" : kind === "unavailable" ? "bg-warn" : "bg-line"}`} aria-hidden />
        )}
        <p className={`text-[12px] font-medium ${tone}`}>{label}</p>
      </div>
      {detail ? <p className="max-w-[42ch] text-[11px] leading-relaxed text-faint">{detail}</p> : null}
      {kind === "error" && onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="mt-1 rounded-md border border-line/70 px-2.5 py-1 text-[11px] text-muted transition hover:border-accent/60 hover:text-accent"
        >
          Retry now
        </button>
      ) : null}
    </div>
  );
}

/** Turns a resource state into the right panel state, or renders children. */
export function DataGate<T>({
  state,
  children,
  isEmpty,
  emptyDetail,
  unavailableDetail,
  onRetry,
  minHeight,
}: {
  state: { data: T | null; error: ApiError | null };
  children: (data: T) => ReactNode;
  isEmpty?: (data: T) => boolean;
  emptyDetail?: string;
  unavailableDetail?: string;
  onRetry?: () => void;
  minHeight?: number;
}) {
  if (state.error) {
    if (state.error.status === 404 || state.error.status === 501) {
      return (
        <PanelState
          kind="unavailable"
          detail={unavailableDetail ?? `This endpoint is not implemented yet (${state.error.message}).`}
        />
      );
    }
    if (state.error.status === 401) {
      return (
        <PanelState
          kind="error"
          title="Not authorised"
          detail="The API token this page was served with is missing or wrong. Reload the page from pc-agent ui."
        />
      );
    }
    return <PanelState kind="error" detail={state.error.message} onRetry={onRetry} />;
  }
  if (state.data === null) return <PanelState kind="loading" />;
  if (isEmpty?.(state.data)) {
    return <PanelState kind="empty" detail={emptyDetail} />;
  }
  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}
      style={minHeight ? { minHeight } : undefined}
      className="h-full"
    >
      {children(state.data)}
    </motion.div>
  );
}

export function Meter({ value, tone = "accent", label }: { value: number; tone?: "accent" | "ok" | "warn" | "danger" | "violet"; label?: string }) {
  const clamped = Math.max(0, Math.min(100, value));
  const color =
    tone === "ok" ? "bg-ok" : tone === "warn" ? "bg-warn" : tone === "danger" ? "bg-danger" : tone === "violet" ? "bg-violet" : "bg-accent";
  return (
    <div className="flex items-center gap-2" aria-label={label}>
      <div className="relative h-1 flex-1 overflow-hidden rounded-full bg-inset">
        <div className={`absolute inset-y-0 start-0 rounded-full ${color} transition-[width] duration-500 ease-hud`} style={{ width: `${clamped}%` }} />
      </div>
      <span className="tabular w-9 text-end text-[11px] text-muted">{clamped.toFixed(0)}%</span>
    </div>
  );
}

export function Badge({ children, tone = "line" }: { children: ReactNode; tone?: "line" | "ok" | "warn" | "danger" | "accent" | "violet" }) {
  const map: Record<string, string> = {
    line: "border-line/70 text-muted",
    ok: "border-ok/45 text-ok",
    warn: "border-warn/45 text-warn",
    danger: "border-danger/45 text-danger",
    accent: "border-accent/50 text-accent",
    violet: "border-violet/45 text-violet",
  };
  return (
    <span className={`rounded-full border px-2 py-0.5 text-[10px] font-medium tracking-wide uppercase ${map[tone]}`}>{children}</span>
  );
}

export function Dot({ tone = "ok", pulse = false }: { tone?: "ok" | "warn" | "danger" | "accent" | "line" | "violet"; pulse?: boolean }) {
  const color = tone === "ok" ? "bg-ok" : tone === "warn" ? "bg-warn" : tone === "danger" ? "bg-danger" : tone === "accent" ? "bg-accent" : tone === "violet" ? "bg-violet" : "bg-line";
  return (
    <span className="relative inline-flex h-2 w-2" aria-hidden>
      {pulse ? <span className={`absolute inset-0 rounded-full ${color} pulse-ring`} /> : null}
      <span className={`relative h-2 w-2 rounded-full ${color}`} />
    </span>
  );
}
