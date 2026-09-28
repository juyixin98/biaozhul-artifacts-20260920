/**
 * Service entry point.
 *   PORT=3000 DB_PATH=./data/diffs.db npm start
 */
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { buildApp } from './diagnostics/app.js';
import { DiffStore } from './state/diff-store.js';
import { config } from './config.js';

const { port, host, dbPath } = config;

if (dbPath !== ':memory:') {
  mkdirSync(dirname(dbPath), { recursive: true });
}

const store = new DiffStore(dbPath);
const app = await buildApp({ store });

const shutdown = async (): Promise<void> => {
  await app.close();
  store.close();
  process.exit(0);
};
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);

await app.listen({ port, host });
app.log.info({ port, host, dbPath }, 'contract diff service listening');
