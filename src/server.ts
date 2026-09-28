/**
 * Service entry point.
 *
 * Wires the four layers together:
 *   contract.ts  -> kernel.ts -> store.ts (SQLite) -> api.ts (Fastify)
 *
 * Run:  npm run build && npm start
 * Dev:  npm run dev
 */

import Fastify from 'fastify';

import { registerRoutes } from './api.js';
import { loadConfig } from './config.js';
import { JsonLogger, StdoutJsonSink } from './logger.js';
import { DocumentStore } from './store.js';

export async function buildServer(options?: {
  databasePath?: string;
  logger?: JsonLogger;
  maxBodyBytes?: number;
}) {
  const config = loadConfig();
  const logger =
    options?.logger ?? new JsonLogger(new StdoutJsonSink(), config.logLevel === 'debug' ? 'debug' : 'info');
  const store = new DocumentStore(options?.databasePath ?? config.databasePath);

  const app = Fastify({
    bodyLimit: options?.maxBodyBytes ?? config.maxBodyBytes,
    // Parse bodies leniently enough to surface our own typed MALFORMED_JSON
    // for patch payloads; Fastify's JSON parse error is mapped below.
    logger: false,
  });

  app.setErrorHandler((error, request, reply) => {
    const requestId =
      (typeof request.headers['x-request-id'] === 'string'
        ? request.headers['x-request-id']
        : request.id) as string;
    if (error.statusCode === 400 && error.message.includes('JSON')) {
      logger.warn('http.malformed.json', {
        requestId,
        phase: 'http',
        outcome: 'failure',
        certainty: 'certain',
        category: 'MALFORMED_JSON',
        reason: error.message,
      });
      return reply.code(400).send({
        requestId,
        success: false,
        error: { category: 'MALFORMED_JSON', message: 'request body is not valid JSON' },
      });
    }
    logger.error('http.unexpected', {
      requestId,
      phase: 'http',
      outcome: 'failure',
      certainty: 'uncertain',
      reason: error.message,
    });
    return reply.code(error.statusCode ?? 500).send({
      requestId,
      success: false,
      error: { category: 'INTERNAL_ERROR', message: error.message },
    });
  });

  await registerRoutes(app, { store, logger });

  // The store is closed exactly once, from Fastify's onClose hook.
  const shutdown = async () => {
    await app.close();
  };
  app.addHook('onClose', async () => {
    store.close();
  });

  return { app, store, shutdown };
}

async function main(): Promise<void> {
  const config = loadConfig();
  const { app } = await buildServer();
  await app.listen({ host: config.host, port: config.port });
  // Fastify does not log itself (logger:false); announce once.
  process.stdout.write(
    JSON.stringify({
      ts: new Date().toISOString(),
      level: 'info',
      msg: 'server.listening',
      host: config.host,
      port: config.port,
      databasePath: config.databasePath,
    }) + '\n',
  );
}

// Only start listening when run directly, not when imported by tests.
if (import.meta.url === new URL(process.argv[1] ?? '', 'file:').href) {
  main().catch((error) => {
    process.stderr.write(`fatal: ${(error as Error).stack ?? error}\n`);
    process.exit(1);
  });
}
