import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // Proxy API calls to FastAPI during dev to avoid CORS friction.
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});