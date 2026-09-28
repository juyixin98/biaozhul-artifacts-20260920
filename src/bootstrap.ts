/**
 * 应用组装：内存 SQLite + 合成夹具 + schema + 引擎 + HTTP。
 * 独立为工厂函数，供服务入口与测试复用。
 */
import { DatabaseSync } from 'node:sqlite';
import { FIXTURE_SCHEMA } from './fixtures/schema.js';
import { seedDatabase } from './fixtures/seed.js';
import { SqliteEntityStore } from './state/sqlite-store.js';
import { QueryEngine } from './kernel/engine.js';
import { RunLogger } from './diagnostics/run-log.js';
import { createApp, type AppDeps } from './http/app.js';
import type { FastifyInstance } from 'fastify';

export interface BuiltApp extends AppDeps {
  app: FastifyInstance;
  db: DatabaseSync;
  store: SqliteEntityStore;
}

export function buildApp(options: { logFile?: string | null } = {}): BuiltApp {
  const db = seedDatabase(new DatabaseSync(':memory:'));
  const store = new SqliteEntityStore(db);
  const logger = new RunLogger(options.logFile ?? 'logs/runs.jsonl');
  const engine = new QueryEngine({ schema: FIXTURE_SCHEMA, store, logger });
  const app = createApp({ engine, logger });
  return { app, engine, logger, db, store };
}
