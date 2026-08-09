import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

export default defineConfig({
  plugins: [react()],
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  build: {
    outDir: resolve(import.meta.dirname, "../../larkflow/workflow/console_assets"),
    emptyOutDir: false,
    sourcemap: false,
    cssCodeSplit: false,
    lib: {
      entry: resolve(import.meta.dirname, "src/canvas.jsx"),
      name: "LarkflowCanvasBundle",
      formats: ["iife"],
      fileName: () => "canvas.js",
    },
    rollupOptions: {
      output: {
        assetFileNames: (assetInfo) => (
          assetInfo.name?.endsWith(".css") ? "canvas.css" : "canvas-[name][extname]"
        ),
      },
    },
  },
});
