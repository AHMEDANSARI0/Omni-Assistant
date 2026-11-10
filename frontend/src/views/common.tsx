import type { ReactNode } from "react";
import { motion } from "framer-motion";
import { Badge, DataGate, Panel } from "@/components/Panel";
import type { ApiError } from "@/lib/api";

export function PageHeader({
  title,
  subtitle,
  actions,
  tone = "accent",
}: {
  title: string;
  subtitle: string;
  actions?: ReactNode;
  tone?: "accent" | "violet" | "ok" | "warn" | "danger";
}) {
  return (
    <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
      <div>
        <div className="flex items-center gap-2">
          <h1 className="font-display text-[19px] font-semibold tracking-[0.06em] text-ink uppercase">{title}</h1>
          <Badge tone={tone}>{tone === "danger" ? "attention" : tone === "warn" ? "limited" : "live"}</Badge>
        </div>
        <p className="mt-1 max-w-[92ch] text-[11.5px] leading-relaxed text-faint">{subtitle}</p>
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-1.5">{actions}</div> : null}
    </div>
  );
}

export function PageBody({ children }: { children: ReactNode }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
      className="min-h-0 flex-1"
    >
      {children}
    </motion.div>
  );
}

export function TextField({
  label,
  value,
  onChange,
  placeholder,
  mono,
  onEnter,
  disabled,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  mono?: boolean;
  onEnter?: () => void;
  disabled?: boolean;
}) {
  return (
    <label className="block min-w-0">
      <span className="hud-label">{label}</span>
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && onEnter) onEnter();
        }}
        placeholder={placeholder}
        disabled={disabled}
        className={`mt-1 w-full rounded-lg border border-line/60 bg-inset/50 px-2.5 py-1.5 text-[12px] text-ink placeholder:text-faint transition focus:border-accent/60 focus:outline-none disabled:opacity-60 ${
          mono ? "font-mono text-[11.5px]" : ""
        }`}
      />
    </label>
  );
}

export function Button({
  children,
  onClick,
  tone = "line",
  disabled,
  title,
  busy,
}: {
  children: ReactNode;
  onClick: () => void;
  tone?: "line" | "accent" | "danger" | "ok" | "violet";
  disabled?: boolean;
  title?: string;
  busy?: boolean;
}) {
  const map: Record<string, string> = {
    line: "border-line/65 text-muted hover:border-accent/50 hover:text-accent",
    accent: "border-accent/50 bg-accent/10 text-accent hover:bg-accent/20",
    danger: "border-danger/50 bg-danger/10 text-danger hover:bg-danger/20",
    ok: "border-ok/50 bg-ok/10 text-ok hover:bg-ok/20",
    violet: "border-violet/50 bg-violet/10 text-violet hover:bg-violet/20",
  };
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || busy}
      title={title}
      className={`rounded-lg border px-2.5 py-1 text-[11.5px] font-medium transition disabled:opacity-45 ${map[tone]}`}
    >
      {busy ? "Working…" : children}
    </button>
  );
}

export function Rows({ children }: { children: ReactNode }) {
  return <div className="divide-y divide-line/35">{children}</div>;
}

export function Row({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`flex items-center gap-2 py-1.5 ${className}`}>{children}</div>;
}

export function KeyValue({ label, value, mono = true }: { label: string; value: ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-line/30 py-1 last:border-0">
      <span className="text-[11px] text-faint">{label}</span>
      <span className={`text-end text-[11.5px] text-ink ${mono ? "font-mono" : ""}`}>{value}</span>
    </div>
  );
}

export function Empty({ title, detail }: { title: string; detail?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-1 py-8 text-center">
      <p className="text-[12px] text-muted">{title}</p>
      {detail ? <p className="max-w-[60ch] text-[11px] leading-relaxed text-faint">{detail}</p> : null}
    </div>
  );
}

export function Grid({ children, cols = 2 }: { children: ReactNode; cols?: 1 | 2 | 3 | 4 }) {
  const map = { 1: "", 2: "lg:grid-cols-2", 3: "lg:grid-cols-3", 4: "lg:grid-cols-2 xl:grid-cols-4" } as const;
  return <div className={`grid grid-cols-1 gap-2.5 ${map[cols]}`}>{children}</div>;
}

export function InfoPanel({ title, children, subtitle }: { title: string; subtitle?: string; children: ReactNode }) {
  return (
    <Panel title={title} subtitle={subtitle} className="h-full">
      {children}
    </Panel>
  );
}

/** A Panel whose body is gated on one resource, with a generic data type. */
export function KeyValuePanel<T>({
  title,
  subtitle,
  state,
  children,
  actions,
  onRetry,
}: {
  title: string;
  subtitle?: string;
  state: { data: T | null; error: ApiError | null };
  children: (data: T) => ReactNode;
  actions?: ReactNode;
  onRetry?: () => void;
}) {
  return (
    <Panel title={title} subtitle={subtitle} actions={actions} className="h-full">
      <DataGate state={state} onRetry={onRetry}>
        {children}
      </DataGate>
    </Panel>
  );
}

export function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="max-h-[220px] overflow-auto rounded-lg border border-line/45 bg-inset/40 p-2 font-mono text-[10.5px] leading-relaxed text-muted">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}
