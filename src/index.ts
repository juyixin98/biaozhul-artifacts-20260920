import { buildScenario } from './fixtures/scenario.js';
import { buildApp } from './service/app.js';
import { ContractRegistry } from './service/registry.js';
import { RunStore } from './state/runStore.js';

const PORT = Number(process.env['PORT'] ?? 8080);
const HOST = process.env['HOST'] ?? '127.0.0.1';
const DB_PATH = process.env['DB_PATH'] ?? './data/runs.sqlite';

async function main(): Promise<void> {
  const scenario = buildScenario();
  const registry = new ContractRegistry();
  registry.register(scenario.rawContract);

  const store = new RunStore(DB_PATH);
  const app = await buildApp({
    registry,
    sources: scenario.sources,
    store,
    maxConcurrent: 4,
    maxQueue: 16,
    defaultTimeoutMs: 2000,
    logger: true,
  });

  await app.listen({ port: PORT, host: HOST });
  app.log.info(`composite query service on http://${HOST}:${PORT} (run db: ${DB_PATH})`);

  const shutdown = async (): Promise<void> => {
    app.log.info('shutting down…');
    await app.close();
    store.close();
    scenario.db.close();
    process.exit(0);
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
