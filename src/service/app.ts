import Fastify, { type FastifyInstance } from 'fastify';
import { DomainError, failureHttpStatus } from '../errors.js';
import { BoundedSemaphore } from '../kernel/semaphore.js';
import { CompositeKernel } from '../kernel/executor.js';
import type { RunStore } from '../state/runStore.js';
import type { DataSource } from '../types.js';
import type { ContractRegistry } from './registry.js';

export interface AppDeps {
  registry: ContractRegistry;
  sources: Map<string, DataSource>;
  store: RunStore;
  maxConcurrent?: number;
  maxQueue?: number;
  defaultTimeoutMs?: number;
  logger?: boolean;
}

interface QueryBody {
  params?: unknown;
  snapshotToken?: unknown;
  timeoutMs?: unknown;
  version?: unknown;
  runId?: unknown;
}

/**
 * Builds the Fastify application. Boundaries:
 *  POST /query/:name            execute a composite query
 *  GET  /contracts              registered contracts
 *  GET  /runs                   run log index
 *  GET  /runs/:id               full persisted response
 *  GET  /runs/:id/events        ordered kernel event trace (replay)
 *  GET  /runs/:id/nodes         per-node execution rows
 *  GET  /health
 */
export async function buildApp(deps: AppDeps): Promise<FastifyInstance> {
  const app = Fastify({ logger: deps.logger ?? false });

  const maxConcurrent = deps.maxConcurrent ?? 4;
  const maxQueue = deps.maxQueue ?? 16;

  app.get('/health', async () => ({ status: 'ok' }));

  app.get('/contracts', async () => ({ contracts: deps.registry.list() }));

  app.post<{ Params: { name: string }; Body: QueryBody }>(
    '/query/:name',
    async (request, reply) => {
      const body = (request.body ?? {}) as QueryBody;

      const params = body.params ?? {};
      if (typeof params !== 'object' || params === null || Array.isArray(params)) {
        throw new DomainError({
          category: 'MISSING_INPUT',
          code: 'BAD_PARAMS',
          message: 'body.params must be an object',
          httpStatus: 400,
        });
      }
      let version: number | undefined;
      if (body.version !== undefined) {
        if (typeof body.version !== 'number' || !Number.isInteger(body.version)) {
          throw new DomainError({
            category: 'MISSING_INPUT',
            code: 'BAD_VERSION',
            message: 'body.version must be an integer',
            httpStatus: 400,
          });
        }
        version = body.version;
      }
      let timeoutMs = deps.defaultTimeoutMs ?? 2000;
      if (body.timeoutMs !== undefined) {
        if (
          typeof body.timeoutMs !== 'number' ||
          body.timeoutMs <= 0 ||
          !Number.isFinite(body.timeoutMs)
        ) {
          throw new DomainError({
            category: 'MISSING_INPUT',
            code: 'BAD_TIMEOUT',
            message: 'body.timeoutMs must be a positive number',
            httpStatus: 400,
          });
        }
        timeoutMs = body.timeoutMs;
      }
      let snapshotToken: string | null = null;
      if (body.snapshotToken !== undefined && body.snapshotToken !== null) {
        if (typeof body.snapshotToken !== 'string') {
          throw new DomainError({
            category: 'MISSING_INPUT',
            code: 'BAD_SNAPSHOT_TOKEN',
            message: 'body.snapshotToken must be a string',
            httpStatus: 400,
          });
        }
        snapshotToken = body.snapshotToken;
      }
      let runId: string | undefined;
      if (body.runId !== undefined) {
        if (typeof body.runId !== 'string' || body.runId.length === 0) {
          throw new DomainError({
            category: 'MISSING_INPUT',
            code: 'BAD_RUN_ID',
            message: 'body.runId must be a non-empty string',
            httpStatus: 400,
          });
        }
        runId = body.runId;
      }

      const contract = deps.registry.resolve(request.params.name, version);
      const kernel = new CompositeKernel({
        sources: deps.sources,
        semaphore: new BoundedSemaphore(maxConcurrent, maxQueue),
        store: deps.store,
      });

      const response = await kernel.run(contract, {
        runId,
        timeoutMs,
        snapshotToken,
        params: params as Record<string, unknown>,
      });

      // Partial aggregation is a successful delivery of a typed response;
      // field-level statuses/reasons carry the failures. HTTP 202 signals
      // that the composite accepted the request but not every field resolved.
      const httpStatus = response.outcome === 'partial' ? 202 : 200;
      reply.code(httpStatus);
      return response;
    },
  );

  app.get('/runs', async () => {
    const runs = deps.store.listRuns(100).map((row) => ({
      runId: row.run_id,
      contract: `${row.contract_name}@v${row.contract_version}`,
      outcome: row.outcome,
      snapshotToken: row.snapshot_token,
      startedAt: row.started_at,
      durationMs: row.duration_ms,
    }));
    return { count: runs.length, runs };
  });

  app.get<{ Params: { id: string } }>('/runs/:id', async (request, reply) => {
    const response = deps.store.getRunResponse(request.params.id);
    if (!response) {
      throw new DomainError({
        category: 'NOT_FOUND',
        code: 'RUN_NOT_FOUND',
        message: `no run with id ${request.params.id}`,
        httpStatus: 404,
      });
    }
    return response;
  });

  app.get<{ Params: { id: string } }>('/runs/:id/events', async (request) => {
    if (!deps.store.getRun(request.params.id)) {
      throw new DomainError({
        category: 'NOT_FOUND',
        code: 'RUN_NOT_FOUND',
        message: `no run with id ${request.params.id}`,
        httpStatus: 404,
      });
    }
    return { runId: request.params.id, events: deps.store.getEvents(request.params.id) };
  });

  app.get<{ Params: { id: string } }>('/runs/:id/nodes', async (request) => {
    if (!deps.store.getRun(request.params.id)) {
      throw new DomainError({
        category: 'NOT_FOUND',
        code: 'RUN_NOT_FOUND',
        message: `no run with id ${request.params.id}`,
        httpStatus: 404,
      });
    }
    return { runId: request.params.id, nodes: deps.store.getNodes(request.params.id) };
  });

  app.setErrorHandler((error: Error, request, reply) => {
    if (error instanceof DomainError) {
      reply.code(error.httpStatus).send({
        error: {
          category: error.category,
          code: error.code,
          message: error.message,
          ...(Object.keys(error.details).length > 0 ? { details: error.details } : {}),
          retryable: error.retryable,
        },
      });
      return;
    }
    // Fastify validation / body parsing errors.
    if ((error as { validation?: unknown }).validation) {
      reply.code(400).send({
        error: {
          category: 'MISSING_INPUT',
          code: 'REQUEST_VALIDATION',
          message: error.message,
          retryable: false,
        },
      });
      return;
    }
    if ((error as { statusCode?: number }).statusCode === 400) {
      reply.code(400).send({
        error: {
          category: 'MISSING_INPUT',
          code: 'BAD_REQUEST',
          message: error.message,
          retryable: false,
        },
      });
      return;
    }
    request.log?.error(error);
    reply.code(failureHttpStatus.COMPUTATION_FAILED ?? 500).send({
      error: {
        category: 'COMPUTATION_FAILED',
        code: 'INTERNAL',
        message: error.message,
        retryable: false,
      },
    });
  });

  return app;
}
