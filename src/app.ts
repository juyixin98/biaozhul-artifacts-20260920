import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import type { AppConfig } from './config.js';
import { DiagnosticLogger } from './diagnostics/logger.js';
import { Kernel } from './kernel/kernel.js';
import { buildMethods } from './kernel/methods/index.js';
import { TaskRegistry } from './kernel/task-registry.js';
import { SqliteDomainStore } from './state/sqlite-domain-store.js';
import { SqliteStateStore } from './state/sqlite-store.js';
import { buildServer } from './transport/server.js';

/**
 * Composition root. Wires the four real modules together:
 *   contract (via kernel) -> execution kernel -> state adapters -> diagnostics,
 * with Fastify as the transport on top.
 */
export interface BuiltApp {
  server: ReturnType<typeof buildServer>;
  kernel: Kernel;
  state: SqliteStateStore;
  domain: SqliteDomainStore;
  tasks: TaskRegistry;
  logger: DiagnosticLogger;
}

export function buildApp(config: AppConfig): BuiltApp {
  if (config.dbPath !== ':memory:') {
    mkdirSync(dirname(config.dbPath), { recursive: true });
  }
  const state = new SqliteStateStore(config.dbPath);
  state.init();

  // Domain shares the same file via a second connection (WAL allows it).
  const domain = new SqliteDomainStore(config.dbPath);
  domain.init();

  const tasks = new TaskRegistry();
  const logger = new DiagnosticLogger(state, config.logLevel);
  const methods = buildMethods({
    domain,
    store: state,
    tasks,
    maxTaskDelayMs: config.maxTaskDelayMs,
  });
  const kernel = new Kernel({ store: state, methods, tasks, logger });
  const server = buildServer({ kernel, tasks, fastifyLoggerLevel: config.logLevel });

  return { server, kernel, state, domain, tasks, logger };
}
