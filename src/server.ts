/**
 * Process bootstrap. All external resources are local: SQLite file under
 * ./data and an in-memory fixture world. No external accounts or services.
 */

import { loadConfig } from "./config.js";
import { JsonDiagnosticsLogger } from "./diagnostics/logger.js";
import { Kernel } from "./kernel/engine.js";
import { openStore } from "./state/store.js";
import { buildApp } from "./transport/http.js";

async function main(): Promise<void> {
  const config = loadConfig();
  const logger = new JsonDiagnosticsLogger();
  const store = openStore(config.dbPath);
  const kernel = new Kernel({ store, logger });
  const app = buildApp({ config, store, kernel, logger });

  const shutdown = async (signal: string): Promise<void> => {
    logger.warn({ decision: `shutdown:${signal}` });
    // Let detached async operations settle so tests/operators see terminal
    // states rather than pending rows, then close HTTP and the database.
    await kernel.awaitBackgroundSettled().catch(() => {});
    await app.close().catch(() => {});
    store.close();
    process.exit(0);
  };
  process.on("SIGINT", () => void shutdown("SIGINT"));
  process.on("SIGTERM", () => void shutdown("SIGTERM"));

  await app.listen({ host: config.host, port: config.port });
  logger.accepted({
    decision: "server_listening",
    detail: { host: config.host, port: config.port, dbPath: config.dbPath },
  });
}

main().catch((error: unknown) => {
  process.stderr.write(
    `fatal startup error: ${error instanceof Error ? error.stack : String(error)}\n`,
  );
  process.exit(1);
});
