import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

/**
 * The Command Center is served by the Python local API from the same loopback
 * origin, so the build must not assume a dev server exists in production:
 * `base: "/"`, relative fetches to `/v1/*`, and no external asset hosts.
 *
 * In development the same origin is faked with a proxy so `npm run dev` talks to
 * a real `pc-agent api serve` - the dev UI is never fed by mocks.
 */
const api = process.env.PC_AGENT_API_URL ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  base: "/",
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    // The built files live inside the Python package so a release carries them.
    outDir: "../src/pc_agent/webui",
    emptyOutDir: false,
    sourcemap: false,
    target: "es2022",
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom"],
          motion: ["framer-motion"],
        },
      },
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/v1": { target: api, changeOrigin: true },
      "/assets": { target: api, changeOrigin: true },
    },
  },
});
