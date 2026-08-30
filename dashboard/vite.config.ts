import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    // Vercel serves this prebuilt; nothing runs at request time.
    outDir: "dist",
    // The data bundle is already the heavy part (21 MB raw, ~3 MB gzipped).
    // A warning at 500 KB of JS would fire on every build and stop being read.
    chunkSizeWarningLimit: 900,
  },
  test: {
    environment: "node",
    include: ["test/**/*.test.ts"],
  },
});
