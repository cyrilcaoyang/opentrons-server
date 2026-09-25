import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The SPA is served by the gateway at /ui (see gateway/api.py), and through
// the Caddy edge at /ot2/{hte,complexation}/ui/ (handle_path strips the
// /ot2/<instance> prefix). A RELATIVE base ("./") makes asset paths resolve
// against the page URL in both cases — no per-instance build needed. API
// calls are prefixed at runtime with an apiBase derived from the page URL
// (see src/lib/api.ts), so /status etc. resolve back through the edge prefix.
// `npm run dev` proxies those paths to a locally running gateway (start one
// with OT2_DRY_RUN=true for a no-hardware loop).
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../src/opentrons_server/ui_dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/cameras": "http://127.0.0.1:8020",
      "/status": "http://127.0.0.1:8020",
      "/health": "http://127.0.0.1:8020",
      "/control": "http://127.0.0.1:8020",
      "/labware": "http://127.0.0.1:8020",
    },
  },
});
