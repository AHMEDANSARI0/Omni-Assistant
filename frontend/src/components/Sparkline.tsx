/**
 * Tiny SVG sparkline over real history samples from `/v1/system`.
 * No charts library: the dashboard needs one line shape, and a dependency would
 * cost more than the 30 lines it takes to draw it.
 */
export function Sparkline({
  values,
  height = 34,
  tone = "accent",
  label,
}: {
  values: (number | null)[];
  height?: number;
  tone?: "accent" | "violet" | "ok" | "warn";
  label?: string;
}) {
  const points = values.filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  const stroke =
    tone === "violet" ? "rgb(var(--violet))" : tone === "ok" ? "rgb(var(--ok))" : tone === "warn" ? "rgb(var(--warn))" : "rgb(var(--accent))";
  if (points.length < 2) {
    return (
      <div className="flex items-center justify-center text-[10px] text-faint" style={{ height }} aria-label={label}>
        collecting samples…
      </div>
    );
  }
  const max = Math.max(...points, 1);
  const min = Math.min(...points, 0);
  const span = max - min || 1;
  const step = 100 / (points.length - 1);
  const path = points
    .map((value, index) => {
      const x = index * step;
      const y = 100 - ((value - min) / span) * 100;
      return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
  const area = `${path} L100,100 L0,100 Z`;
  return (
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" style={{ height, width: "100%" }} role="img" aria-label={label ?? "history"}>
      <path d={area} fill={stroke} opacity={0.1} />
      <path d={path} fill="none" stroke={stroke} strokeWidth={1.6} vectorEffect="non-scaling-stroke" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}
