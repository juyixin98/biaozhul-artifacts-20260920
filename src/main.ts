import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { buildServer } from './diagnostics/http.js';
import { ContractStore } from './state/store.js';

/**
 * Entry point. DB path overridable via CONTRACT_DIFF_DB so tests/local runs
 * can use an on-disk file or :memory:.
 */
async function main(): Promise<void> {
  const dbPath = process.env['CONTRACT_DIFF_DB'] ?? './data/contract-diff.sqlite';
  if (dbPath !== ':memory:') mkdirSync(dirname(dbPath), { recursive: true });
  const store = new ContractStore(dbPath);
  const app = await buildServer(store);
  const port = Number(process.env['PORT'] ?? 3000);
  await app.listen({ port, host: '0.0.0.0' });
  app.log.info(`contract diff service listening on :${port} (db=${dbPath})`);
  // eslint-disable-next-line no-console
  console.log(JSON.stringify({ event: 'listening', port, db: dbPath }));
}

main().catch((err) => {
  // eslint-disable-next-line no-console
  console.error(JSON.stringify({ event: 'fatal', error: (err as Error).message }));
  process.exit(1);
});
