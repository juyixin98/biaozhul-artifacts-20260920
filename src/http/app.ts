/**
 * Fastify application: composite endpoint + diagnostics.
 *
 * Error taxonomy -> HTTP status mapping:
 *   INPUT_ERROR        -> 400
 *   STATE_CONFLICT     -> 409
 *   RESOURCE_EXHAUSTED -> 504 (deadline / cancellation propagated to caller)
 *   COMPUTATION_FAILED -> 502
 */
import Fastify, { type FastifyInstance } from 'fastify';
import { ComposeService } from '../compose/service.js';
import { RunStore } from '../state/runStore.js';
import { registerDiagnostics } from '../diagnostics/routes.js';
import { inputError, isCompositeError, type ErrorCategory } from '../kernel/errors.js';
import { config, type AppConfig } from '../config/index.js';

const STATUS_BY_CATEGORY: Record<ErrorCategory, number> = {
  INPUT_ERROR: 400,
  STATE_CONFLICT: 409,
  RESOURCE_EXHAUSTED: 504,
  COMPUTATION_FAILED: 502,
};

export interface BuiltApp {
  app: FastifyInstance;
  store: RunStore;
}

export async function buildApp(overrides: Partial<AppConfig> = {}): Promise<BuiltApp> {
  const resolved: AppConfig = { ...config, ...overrides };
  const store = new RunStore(resolved.dbPath);
  const service = new ComposeService(store, resolved);
  const app = Fastify({ logger: false });

  app.setErrorHandler((error, _request, reply) => {
    if (isCompositeError(error)) {
      const { category, ...detail } = error.detail;
      void reply.status(STATUS_BY_CATEGORY[category]).send({ error: { category, ...detail } });
      return;
    }
    void reply.status(500).send({
      error: {
        category: 'COMPUTATION_FAILED',
        reason: 'INTERNAL_ERROR',
        message: error instanceof Error ? error.message : String(error),
        retryable: false,
      },
    });
  });

  app.post('/v1/compose', async (_request, reply) => {
    const body = (_request.body ?? {}) as Record<string, unknown>;
    if (typeof body.contract !== 'string') {
      throw inputError('INPUT_MISSING_FIELD', 'body.contract must be a string', { field: 'contract' });
    }
    if (body.input !== undefined && (typeof body.input !== 'object' || body.input === null)) {
      throw inputError('INPUT_TYPE_ERROR', 'body.input must be an object', { field: 'input' });
    }
    const { result, replayed } = await service.run({
      contract: body.contract,
      input: (body.input ?? {}) as Record<string, unknown>,
      ...(typeof body.scenario === 'string' ? { scenario: body.scenario } : {}),
      ...(typeof body.timeoutMs === 'number' ? { timeoutMs: body.timeoutMs } : {}),
      ...(typeof body.snapshotToken === 'string' ? { snapshotToken: body.snapshotToken } : {}),
      ...(typeof body.idempotencyKey === 'string' ? { idempotencyKey: body.idempotencyKey } : {}),
    });
    void reply.header('X-Run-Id', result.runId);
    if (replayed) void reply.header('Idempotency-Replayed', 'true');
    return { replayed, run: result };
  });

  await registerDiagnostics(app, store);
  return { app, store };
}
