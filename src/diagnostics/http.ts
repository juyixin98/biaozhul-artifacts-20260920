import Fastify, { type FastifyInstance } from 'fastify';
import { randomUUID } from 'node:crypto';
import { DiffService, type DiffRequest } from './service.js';
import type { ContractStore } from '../state/store.js';

/**
 * Diagnostic HTTP surface:
 *   POST /v1/diff                 run an analysis
 *   GET  /v1/analyses/:id         fetch a stored analysis (findings + witnesses)
 *   GET  /v1/analyses/:id/logs    fetch processing logs for the owning request
 *   GET  /healthz
 *
 * Every response carries the request id (client may supply X-Request-Id)
 * so a result and its step log can always be correlated.
 */
export async function buildServer(store: ContractStore): Promise<FastifyInstance> {
  const app = Fastify({ logger: false });
  const service = new DiffService(store);

  app.addHook('onRequest', async (req, reply) => {
    const incoming = req.headers['x-request-id'];
    req.requestContext = { requestId: typeof incoming === 'string' && incoming ? incoming : randomUUID() };
    reply.header('x-request-id', req.requestContext.requestId);
  });

  app.get('/healthz', async () => ({ status: 'ok' }));

  app.post<{ Body: DiffRequest }>('/v1/diff', async (req, reply) => {
    const body = req.body ?? ({} as DiffRequest);
    if (typeof body.oldContract !== 'string' || typeof body.newContract !== 'string') {
      return reply.code(400).send({
        error: 'VALIDATION',
        message: 'oldContract and newContract must be YAML/JSON strings',
        requestId: req.requestContext.requestId,
      });
    }
    const out = service.run(body, req.requestContext.requestId);
    const code = out.status === 'error' ? 422 : 200;
    return reply.code(code).send(out);
  });

  app.get<{ Params: { id: string } }>('/v1/analyses/:id', async (req, reply) => {
    const row = store.getAnalysis(req.params.id);
    if (!row) {
      return reply.code(404).send({ error: 'NOT_FOUND', message: 'unknown analysis id', requestId: req.requestContext.requestId });
    }
    return { ...row, requestId: row.request_id };
  });

  app.get<{ Params: { id: string } }>('/v1/analyses/:id/logs', async (req, reply) => {
    const row = store.getAnalysis(req.params.id);
    if (!row) {
      return reply.code(404).send({ error: 'NOT_FOUND', message: 'unknown analysis id', requestId: req.requestContext.requestId });
    }
    return { requestId: row.request_id, logs: store.logsFor(row.request_id) };
  });

  return app;
}

// Minimal per-request context attached by the hook.
declare module 'fastify' {
  interface FastifyRequest {
    requestContext: { requestId: string };
  }
}
