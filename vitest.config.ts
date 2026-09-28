import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    testTimeout: 60_000,
    hookTimeout: 30_000,
    pool: "forks",
    fileParallelism: true,
    reporters: ["default"],
    chaiConfig: {
      truncateThreshold: 2000,
    },
    coverage: {
      provider: "v8",
      include: ["src/**/*.ts"],
      // server.ts is the process bootstrap (side-effecting composition root);
      // model.ts contains type declarations only. Neither holds branch logic.
      exclude: ["src/server.ts", "src/contract/model.ts"],
      reporter: ["text", "json-summary"],
      thresholds: {
        statements: 80,
        branches: 80,
        functions: 80,
        lines: 80,
      },
    },
  },
});
