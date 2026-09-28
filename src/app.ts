import Fastify, { type FastifyInstance } from 'fastify';
import { loadConfig, type AppConfig } from './config/index.js';
import {
  CompositeSink,
  FileSink,
  MemoryRingSink,
  StreamSink,
} from './diagnostics/logger.js';
import { registerRoutes } from './http/routes.js';
import { SqliteObjectStore, type ObjectStore } from './state/store.js';

export interface BuiltApp {
  app: FastifyInstance;
  store: ObjectStore;
  ring: MemoryRingSink;
  fileSink: FileSink | null;
  config: AppConfig;
}

/**
 * Assemble the real adapter stack. Tests build the same graph with
 * `:memory:` and a temp log path, so production and integration exercise
 * identical wiring.
 */
export async function buildApp(overrides: Partial<AppConfig> = {}): Promise<BuiltApp> {
  const config: AppConfig = { ...loadConfig(), ...overrides, limits: { ...loadConfig().limits, ...overrides.limits } };

  const app = Fastify({
    logger: false,
    bodyLimit: 32 * 1024 * 1024,
    genReqId: () =>
      `${Date.now().toString(36)}-${Math.random().toString(16).slice(2, 10)}`,
  });

  const store = new SqliteObjectStore(config.databasePath);
  const ring = new MemoryRingSink(1000);
  const fileSink = config.logPath === null ? null : new FileSink(config.logPath);
  const streamSink = new StreamSink((line) => console.log(line));
  const sinks = fileSink === null ? [ring, streamSink] : [ring, fileSink, streamSink];
  const diagnostics = new CompositeSink(sinks);

  registerRoutes({ app, store, diagnostics, ring, config });

  return { app, store, ring, fileSink, config };
}
