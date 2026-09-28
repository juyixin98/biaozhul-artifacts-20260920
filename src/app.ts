/**
 * Composition root: wires the SQLite adapter, kernel, logger and HTTP layer.
 * Kept separate from server.ts so tests can build the same app over a temp
 * database.
 */
import { loadConfig, AppConfig } from './config.js';
import { ResourceKernel } from './core/kernel.js';
import { SqliteResourceStore } from './state/sqlite-store.js';
import { buildApp } from './transport/http-app.js';
import { JsonRunLogger } from './transport/logger.js';
import { FastifyInstance } from 'fastify';

export interface AssembledApp {
  readonly app: FastifyInstance;
  readonly store: SqliteResourceStore;
  readonly kernel: ResourceKernel;
  readonly logs: JsonRunLogger;
  readonly config: AppConfig;
}

export function assemble(
  config: AppConfig = loadConfig(),
  stream: NodeJS.WritableStream = process.stdout,
  retainLogs = false
): AssembledApp {
  const store = new SqliteResourceStore(config.dbPath);
  const kernel = new ResourceKernel(store);
  const logs = new JsonRunLogger(stream, retainLogs);
  const app = buildApp({ store, kernel, logs });
  return { app, store, kernel, logs, config };
}
