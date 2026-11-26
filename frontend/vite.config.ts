/// <reference types="vitest/config" />
import path from "node:path";
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type ProxyOptions } from "vite";

const root = path.dirname(fileURLToPath(import.meta.url));
const apiProxy = process.env.CEHO_DEV_PROXY ?? "http://127.0.0.1:8000";

// GET /login and GET /sessions/:id are client routes. Only POST is the API.
function proxyApiPosts(target: string): ProxyOptions {
  return {
    target,
    bypass(request) {
      if (request.method !== "POST") {
        return "/index.html";
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(root, "src"),
    },
  },
  server: {
    host: true,
    proxy: {
      "/login": proxyApiPosts(apiProxy),
      "/sessions": proxyApiPosts(apiProxy),
      "/health": apiProxy,
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.ts",
  },
});
