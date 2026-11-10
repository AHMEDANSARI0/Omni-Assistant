/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["class", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        // Semantic tokens only: components never hard-code a hex value, which is
        // what keeps dark and light mode contrast-checked in one place.
        surface: "rgb(var(--surface) / <alpha-value>)",
        elevated: "rgb(var(--elevated) / <alpha-value>)",
        inset: "rgb(var(--inset) / <alpha-value>)",
        line: "rgb(var(--line) / <alpha-value>)",
        ink: "rgb(var(--ink) / <alpha-value>)",
        muted: "rgb(var(--muted) / <alpha-value>)",
        faint: "rgb(var(--faint) / <alpha-value>)",
        accent: "rgb(var(--accent) / <alpha-value>)",
        accentSoft: "rgb(var(--accent-soft) / <alpha-value>)",
        violet: "rgb(var(--violet) / <alpha-value>)",
        ok: "rgb(var(--ok) / <alpha-value>)",
        warn: "rgb(var(--warn) / <alpha-value>)",
        danger: "rgb(var(--danger) / <alpha-value>)",
      },
      fontFamily: {
        display: ["'Space Grotesk'", "Sora", "Segoe UI", "system-ui", "sans-serif"],
        sans: ["Inter", "'Segoe UI'", "system-ui", "sans-serif"],
        mono: ["'JetBrains Mono'", "Cascadia Code", "ui-monospace", "monospace"],
      },
      letterSpacing: { hud: "0.18em", wide: "0.32em" },
      boxShadow: {
        panel: "0 1px 0 0 rgb(var(--line) / 0.65) inset, 0 18px 40px -28px rgb(2 8 20 / 0.85)",
        glow: "0 0 0 1px rgb(var(--accent) / 0.35), 0 0 28px -8px rgb(var(--accent) / 0.55)",
      },
      borderRadius: { xl: "14px", "2xl": "18px" },
      transitionTimingFunction: { hud: "cubic-bezier(0.22, 1, 0.36, 1)" },
    },
  },
  plugins: [],
};
