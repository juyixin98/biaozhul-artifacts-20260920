/**
 * 本地启动入口：打开 SQLite、播种合成夹具（仅当为空）、启动 Fastify。
 */

import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { buildApp } from './server/app.js';
import { loadConfig } from './server/config.js';
import { isSeeded, openDatabase, seedDatabase, type SeedData } from './state/db.js';
import { buildSchema } from './state/schema.js';

async function main(): Promise<void> {
  const config = loadConfig();
  const db = openDatabase(config.databaseFile);
  if (!isSeeded(db)) {
    const seedPath = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      '../fixtures/seed-data.json',
    );
    const raw = await readFile(seedPath, 'utf8');
    seedDatabase(db, JSON.parse(raw) as SeedData);
    process.stderr.write(`[startup] seeded database at ${config.databaseFile}\n`);
  }

  const schema = buildSchema();
  const app = await buildApp({ schema, db, config });

  try {
    await app.listen({ host: config.host, port: config.port });
    process.stderr.write(
      `[startup] restricted-graphql-backend listening on http://${config.host}:${config.port}\n`,
    );
  } catch (error) {
    process.stderr.write(`[startup] failed: ${(error as Error).message}\n`);
    db.close();
    process.exitCode = 1;
  }
}

main().catch((error) => {
  process.stderr.write(`[fatal] ${(error as Error).stack ?? error}\n`);
  process.exit(1);
});
