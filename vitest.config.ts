import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    testTimeout: 120_000,
    hookTimeout: 180_000,
    pool: "forks",
    // Proving is CPU-heavy; run files serially to avoid oversubscription.
    fileParallelism: false,
  },
});
