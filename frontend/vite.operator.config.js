import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

export default defineConfig({
  base: "/operator-assets/",
  plugins: [vue()],
  build: {
    emptyOutDir: true,
    manifest: true,
    outDir: fileURLToPath(new URL("../src/ai_intel_agent/operator_static", import.meta.url)),
    assetsDir: "",
    rollupOptions: {
      input: fileURLToPath(new URL("./src/operator.js", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
  },
});
