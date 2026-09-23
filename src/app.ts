import Fastify, { type FastifyInstance } from 'fastify';
import type { Database as DbType } from 'better-sqlite3';
import { loadConfig, type Config } from './config.js';
import { openDb, createStore } from './store.js';
import { Verifier } from './verify/verifier.js';
import { ChainService } from './chain-service.js';
import { apiRoutes, registerErrorHandler, type Services } from './routes.js';

export interface BuiltApp {
  app: FastifyInstance;
  services: Services;
  close(): Promise<void>;
}

export function buildApp(overrides: Partial<Config> = {}): BuiltApp {
  const config = { ...loadConfig(), ...overrides };
  const db: DbType = openDb(config.dbPath);
  const store = createStore(db);
  const verifier = new Verifier(store, config);
  const chain = new ChainService(store, config.confirmations);
  const services: Services = { store, verifier, chain, config };

  const app = Fastify({
    logger: { level: config.logLevel },
    bodyLimit: 8 * 1024 * 1024,
  });
  registerErrorHandler(app);
  app.register(apiRoutes, { prefix: '/api/v1', services });
  app.get('/healthz', async () => ({ ok: true, service: 'nft-metadata-consistency', tip: services.chain.getTip() ?? null }));
  app.get('/', async () => ({
    service: 'nft-metadata-consistency',
    endpoints: [
      'POST /api/v1/blocks',
      'POST /api/v1/verify/metadata',
      'PUT /api/v1/tokens/:id/metadata',
      'POST /api/v1/chain/tip',
      'POST /api/v1/chain/reorg',
      'GET /api/v1/events',
    ],
  }));

  return {
    app,
    services,
    async close() {
      await app.close();
      db.close();
    },
  };
}
