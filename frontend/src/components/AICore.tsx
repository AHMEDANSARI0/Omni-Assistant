import { useEffect, useMemo, useRef } from "react";
import { motion } from "framer-motion";
import { useActivity } from "@/store/activity";
import { useResource } from "@/store/hooks";
import { useUi } from "@/store/ui";
import type { Health, Overview, VoicePayload } from "@/lib/types";
import { ApiError } from "@/lib/api";

export type CoreState = "offline" | "error" | "executing" | "listening" | "speaking" | "thinking" | "idle";

const STATE_COPY: Record<CoreState, { label: string; detail: string; tone: string }> = {
  offline: { label: "Offline", detail: "No answer from the local API", tone: "danger" },
  error: { label: "Error", detail: "Emergency stop engaged", tone: "danger" },
  executing: { label: "Executing", detail: "Engine reports running work", tone: "accent" },
  listening: { label: "Listening", detail: "Microphone capture session open", tone: "ok" },
  speaking: { label: "Speaking", detail: "Synthesis request in flight", tone: "violet" },
  thinking: { label: "Thinking", detail: "Request sent, waiting for the engine", tone: "violet" },
  idle: { label: "Idle", detail: "No work in flight, engines ready", tone: "accent" },
};

/** The state is derived only from signals the backend actually exposes. */
export function useCoreState(): CoreState {
  const health = useResource<Health>("health");
  const voice = useResource<VoicePayload>("voice");
  const thinking = useActivity("thinking");
  const speaking = useActivity("speaking");

  return useMemo(() => {
    if (health.error) return "offline";
    if (health.data?.emergency_stop_engaged) return "error";
    if (speaking > 0) return "speaking";
    if (voice.data?.microphone.listening) return "listening";
    if (thinking > 0) return "thinking";
    if ((health.data?.running_tasks.length ?? 0) > 0 || (health.data?.running_missions.length ?? 0) > 0) return "executing";
    return "idle";
  }, [health.error, health.data, voice.data, thinking, speaking]);
}

function rgbFor(state: CoreState, light: boolean) {
  const dark: Record<CoreState, string> = {
    offline: "248,113,113",
    error: "248,113,113",
    executing: "34,211,238",
    listening: "52,211,153",
    speaking: "167,139,250",
    thinking: "167,139,250",
    idle: "34,211,238",
  };
  const bright: Record<CoreState, string> = {
    offline: "176,42,42",
    error: "176,42,42",
    executing: "6,130,158",
    listening: "4,120,87",
    speaking: "92,63,191",
    thinking: "92,63,191",
    idle: "6,130,158",
  };
  return (light ? bright : dark)[state];
}

const PARTICLES = 168;

/**
 * Holographic core.
 *
 * Deliberately canvas-2D, not WebGL: the visual is a particle shell plus
 * concentric rings, which 2D can draw at 60 fps on integrated graphics, keeps
 * the bundle ~600 kB smaller than three.js, and cannot fail on a machine without
 * a working WebGL context. The "3D" is real - each particle is placed on a
 * sphere and rotated with a rotation matrix, then depth-sorted by z for size and
 * alpha. Motion stops when the tab is hidden, when the core scrolls out of view,
 * and entirely when reduced motion is on.
 */
export function AICore({ compact = false }: { compact?: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const state = useCoreState();
  const { ui } = useUi();
  const reduced = ui.motion === "reduce";
  const light = ui.theme === "light";
  const stateRef = useRef(state);
  stateRef.current = state;

  useEffect(() => {
    const canvas = canvasRef.current;
    const wrapper = wrapperRef.current;
    if (!canvas || !wrapper) return;
    const ctx = canvas.getContext("2d", { alpha: true });
    if (!ctx) return;

    const light = document.documentElement.dataset.theme === "light";
    let width = 0;
    let height = 0;
    let frame = 0;
    let running = true;
    let visible = true;
    let t = 0;

    // Deterministic particle shell: Fibonacci sphere, so the distribution is
    // even and the same on every machine (no random jitter at every mount).
    const points = Array.from({ length: reduced ? 40 : PARTICLES }, (_, index) => {
      const y = 1 - (index / ((reduced ? 40 : PARTICLES) - 1)) * 2;
      const radius = Math.sqrt(Math.max(0, 1 - y * y));
      const theta = index * 2.399963229728653;
      return { x: Math.cos(theta) * radius, y, z: Math.sin(theta) * radius, size: 0.7 + ((index * 37) % 10) / 14 };
    });

    const resize = () => {
      const rect = wrapper.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = Math.max(120, rect.width);
      height = Math.max(120, rect.height);
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();

    const observer = new ResizeObserver(resize);
    observer.observe(wrapper);
    const intersection = new IntersectionObserver((entries) => {
      visible = entries[0]?.isIntersecting ?? true;
    });
    intersection.observe(wrapper);

    const draw = () => {
      if (!running) return;
      if (!visible || document.visibilityState !== "visible") {
        frame = window.requestAnimationFrame(draw);
        return;
      }
      const color = rgbFor(stateRef.current, light);
      const cx = width / 2;
      const cy = height / 2;
      // Scale from the shorter side but let a wide hero use more of its height,
      // so the core does not shrink into a coin on a 1600 px window.
      const scale = Math.min(width * 0.72, height * 1.12) / 2;
      const coreRadius = scale * (compact ? 0.4 : 0.44);
      const speed = reduced ? 0.00016 : stateRef.current === "thinking" || stateRef.current === "executing" ? 0.0011 : 0.00042;
      if (!reduced) t += 1;

      ctx.clearRect(0, 0, width, height);

      // Backlight bloom.
      // Light mode needs a stronger wash: a translucent cyan on white reads as
      // grey haze, so the alphas are raised rather than reusing the dark values.
      const bloom = ctx.createRadialGradient(cx, cy, coreRadius * 0.1, cx, cy, coreRadius * 1.9);
      bloom.addColorStop(0, `rgba(${color},${light ? 0.26 : 0.24})`);
      bloom.addColorStop(0.45, `rgba(${color},${light ? 0.12 : 0.09})`);
      bloom.addColorStop(1, `rgba(${color},0)`);
      ctx.fillStyle = bloom;
      ctx.beginPath();
      ctx.arc(cx, cy, coreRadius * 1.9, 0, Math.PI * 2);
      ctx.fill();

      // Concentric technical rings, each on its own rotation.
      const rings = [
        { radius: coreRadius * 1.32, tilt: 0.34, dash: [2, 9], width: 1, alpha: 0.5, rate: 0.22 },
        { radius: coreRadius * 1.12, tilt: -0.6, dash: [1, 0], width: 1.2, alpha: 0.62, rate: -0.3 },
        { radius: coreRadius * 0.92, tilt: 1.18, dash: [16, 12], width: 1, alpha: 0.45, rate: 0.5 },
      ];
      for (const ring of rings) {
        const angle = t * speed * ring.rate * 620;
        ctx.save();
        ctx.translate(cx, cy);
        ctx.rotate(angle);
        ctx.scale(1, Math.cos(ring.tilt));
        ctx.beginPath();
        ctx.arc(0, 0, ring.radius, 0, Math.PI * 2);
        ctx.setLineDash(ring.dash);
        ctx.lineWidth = ring.width;
        ctx.strokeStyle = `rgba(${color},${ring.alpha * (light ? 1.15 : 1)})`;
        ctx.stroke();
        ctx.restore();
      }

      // Particle shell with depth sorting (real 3D rotation).
      const rotY = t * speed * 620;
      const rotX = Math.sin(t * speed * 210) * 0.35;
      const cosY = Math.cos(rotY);
      const sinY = Math.sin(rotY);
      const cosX = Math.cos(rotX);
      const sinX = Math.sin(rotX);
      const projected = points.map((point) => {
        const x1 = point.x * cosY - point.z * sinY;
        const z1 = point.x * sinY + point.z * cosY;
        const y1 = point.y * cosX - z1 * sinX;
        const z2 = point.y * sinX + z1 * cosX;
        return { x: x1, y: y1, z: z2, size: point.size };
      });
      projected.sort((a, b) => a.z - b.z);
      for (const point of projected) {
        const depth = (point.z + 1) / 2; // 0 = far, 1 = near
        const perspective = 0.72 + depth * 0.45;
        const px = cx + point.x * coreRadius * perspective;
        const py = cy + point.y * coreRadius * perspective;
        const radius = point.size * (0.7 + depth * 1.5) * (compact ? 0.8 : 1);
        ctx.beginPath();
        ctx.arc(px, py, radius, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(${color},${(0.16 + depth * 0.72) * (light ? 1.18 : 1)})`;
        ctx.fill();
      }

      // Bright inner core with a soft pulse.
      const pulse = reduced ? 0 : Math.sin(t * 0.026) * 0.05;
      const inner = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreRadius * (0.52 + pulse));
      inner.addColorStop(0, `rgba(${color},${light ? 0.62 : 0.62})`);
      inner.addColorStop(0.55, `rgba(${color},0.14)`);
      inner.addColorStop(1, `rgba(${color},0)`);
      ctx.fillStyle = inner;
      ctx.beginPath();
      ctx.arc(cx, cy, coreRadius * (0.58 + pulse), 0, Math.PI * 2);
      ctx.fill();

      frame = window.requestAnimationFrame(draw);
    };
    frame = window.requestAnimationFrame(draw);

    return () => {
      running = false;
      window.cancelAnimationFrame(frame);
      observer.disconnect();
      intersection.disconnect();
    };
  }, [compact, reduced]);

  const copy = STATE_COPY[state];

  return (
    <div className="relative flex h-full min-h-0 flex-col">
      <div ref={wrapperRef} className="relative min-h-0 flex-1">
        <canvas ref={canvasRef} className="absolute inset-0" aria-hidden />
        <div className="pointer-events-none absolute inset-0 grid place-items-center">
          <div className="text-center">
            <motion.p
              key={state}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.3 }}
              className="font-display text-[15px] font-semibold tracking-[0.42em] text-ink uppercase"
              style={{ textShadow: light ? "0 0 16px rgba(255,255,255,0.95), 0 0 4px rgba(255,255,255,0.9)" : "0 0 18px rgba(5,11,22,0.95), 0 0 6px rgba(5,11,22,0.9)" }}
            >
              AI Core
            </motion.p>
            <p className="mt-1 font-mono text-[10.5px] tracking-[0.22em] uppercase" style={{ color: `rgb(var(--${copy.tone}))` }}>
              {copy.label}
            </p>
          </div>
        </div>
      </div>
      <div className="shrink-0 px-4 pb-3">
        <StateLegend current={state} />
      </div>
    </div>
  );
}

const STATES: CoreState[] = ["idle", "thinking", "executing", "listening", "speaking", "error"];

/* Static class maps: Tailwind only keeps classes it can see in the source, so
   tone-dependent styles are written out rather than interpolated. */
const LEGEND_ON: Record<string, string> = {
  accent: "border-accent/50 text-accent",
  ok: "border-ok/50 text-ok",
  violet: "border-violet/50 text-violet",
  danger: "border-danger/50 text-danger",
};
const LEGEND_DOT: Record<string, string> = {
  accent: "bg-accent",
  ok: "bg-ok",
  violet: "bg-violet",
  danger: "bg-danger",
};

/** Legend doubles as documentation of what each state is derived from. */
function StateLegend({ current }: { current: CoreState }) {
  return (
    <div className="flex flex-wrap items-center justify-center gap-x-2 gap-y-1">
      {STATES.map((item) => {
        const tone = STATE_COPY[item].tone;
        const on = current === item;
        return (
          <span
            key={item}
            title={STATE_COPY[item].detail}
            className={`flex items-center gap-1 rounded-full border px-1.5 py-[1px] text-[9.5px] tracking-wide uppercase transition ${
              on ? LEGEND_ON[tone] : "border-line/45 text-faint/70"
            }`}
          >
            <span className={`h-1 w-1 rounded-full ${on ? LEGEND_DOT[tone] : "bg-line"}`} />
            {item}
          </span>
        );
      })}
    </div>
  );
}

/** Small headline block used above the core on the dashboard. */
export function CoreHeadline() {
  const health = useResource<Health>("health");
  const overview = useResource<Overview>("overview");
  const state = useCoreState();
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-faint">
      <span>
        Engine <span className="text-mono text-ink">v{health.data?.version ?? "—"}</span>
      </span>
      <span>
        Schema <span className="text-mono text-ink">{health.data?.schema_version ?? "—"}</span>
      </span>
      <span>
        Tasks <span className="text-mono text-ink">{overview.data?.tasks.total ?? "—"}</span>
      </span>
      <span>
        Tools <span className="text-mono text-ink">{overview.data?.tools ?? "—"}</span>
      </span>
      <span className="text-muted">{STATE_COPY[state].detail}</span>
    </div>
  );
}

export function coreErrorText(error: ApiError | null): string {
  return error ? error.message : "";
}
