import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [vue()],
  build: {
    emptyOutDir: true,
    manifest: true,
    outDir: fileURLToPath(new URL("../src/ai_intel_agent/static", import.meta.url)),
    assetsDir: "",
    rollupOptions: {
      input: {
        browse: fileURLToPath(new URL("./src/browse.js", import.meta.url)),
        research: fileURLToPath(new URL("./src/research.js", import.meta.url)),
      },
    },
  },
  test: {
    environment: "jsdom",
  },
});
