/**
 * HTTP server bootstrap: wires configuration -> SQLite store -> kernel ->
 * Fastify application.
 */

import { buildApp } from "./http/app.js";
import { loadConfig } from "./config.js";
import { Kernel } from "./kernel/kernel.js";
import { SqliteResourceStore } from "./state/sqliteStore.js";

async function main(): Promise<void> {
  const config = loadConfig();
  const store = new SqliteResourceStore({
    path: config.databasePath,
    busyTimeoutMs: config.busyTimeoutMs,
  });
  const kernel = new Kernel({
    store,
    etagStrength: config.etagStrength,
    clock: () => Date.now(),
  });
  const app = await buildApp({
    kernel,
    store,
    etagStrength: config.etagStrength,
    diagnostics: config.diagnostics,
  });

  const shutdown = async (signal: string): Promise<void> => {
    app.log.info({ signal }, "shutting down");
    try {
      await app.close();
      store.close();
      process.exit(0);
    } catch (err) {
      app.log.error({ err }, "error during shutdown");
      process.exit(1);
    }
  };
  process.on("SIGINT", () => void shutdown("SIGINT"));
  process.on("SIGTERM", () => void shutdown("SIGTERM"));

  await app.listen({ port: config.port, host: config.host });
  app.log.info(
    {
      port: config.port,
      host: config.host,
      databasePath: config.databasePath,
      etagStrength: config.etagStrength,
    },
    "resource version API listening",
  );
}

main().catch((err) => {
  console.error("fatal startup error", err);
  process.exit(1);
});
