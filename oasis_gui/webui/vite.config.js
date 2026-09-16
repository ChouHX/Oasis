import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built bundle is copied into the service image and served by
// core/webui.py from the same origin as the API, so requests stay relative.
//
// For local UI work run `npm run dev`: Vite proxies /api, /login and /logout to
// the Python service (start it with `python3 service.py`), which means cookies
// and the session work exactly as they do in production.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // one chunk keeps the image simple; the app is small enough that code
    // splitting would only add requests
    chunkSizeWarningLimit: 2000,
  },
  server: {
    port: 5174,
    proxy: {
      "/api": { target: "http://127.0.0.1:8080", changeOrigin: false },
      "/login": { target: "http://127.0.0.1:8080", changeOrigin: false },
      "/logout": { target: "http://127.0.0.1:8080", changeOrigin: false },
      "/health": { target: "http://127.0.0.1:8080", changeOrigin: false },
    },
  },
});
