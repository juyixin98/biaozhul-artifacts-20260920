/**
 * Diagnostics interface (诊断接口).
 *
 * Read-only HTTP surface for replay and inspection:
 *   GET  /health                 liveness
 *   GET  /contracts              declared contracts and field provenance
 *   GET  /scenarios              named synthetic scenarios
 *   GET  /runs?limit=            run index (newest first)
 *   GET  /runs/:id               full typed response envelope
 *   GET  /runs/:id/events        ordered intermediate states + decisions
 *
 * The event stream is the replay record: run id, monotonic seq, timestamps,
 * node, and the judgment each DECISION event encoded.
 */
import type { FastifyInstance } from 'fastify';
import type { RunStore } from '../state/runStore.js';
import { inputError } from '../kernel/errors.js';
import { contracts } from '../compose/contracts.js';
import { scenarios } from '../compose/scenarios.js';

export async function registerDiagnostics(app: FastifyInstance, store: RunStore): Promise<void> {
  app.get('/health', async () => ({ status: 'ok' }));

  app.get('/contracts', async () => ({
    contracts: Object.values(contracts).map((c) => ({
      name: c.name,
      input: c.input,
      nodes: c.calls.map((call) => ({
        id: call.id,
        source: call.source,
        method: call.method,
        dependencies: call.dependencies,
        fields: call.fields.map((f) => ({
          output: f.output,
          requirement: f.requirement,
          ...(f.path !== undefined ? { path: f.path } : {}),
          ...(f.defaultOnMissing !== undefined ? { defaultOnMissing: f.defaultOnMissing } : {}),
        })),
      })),
    })),
  }));

  app.get('/scenarios', async () => ({
    scenarios: Object.values(scenarios).map((s) => ({ name: s.name, description: s.description })),
  }));

  app.get('/runs', async (request) => {
    const query = (request.query ?? {}) as { limit?: string };
    const limit = query.limit === undefined ? 50 : Number(query.limit);
    if (!Number.isInteger(limit) || limit <= 0 || limit > 200) {
      throw inputError('INVALID_QUERY', 'limit must be an integer between 1 and 200');
    }
    return { runs: store.listRuns(limit) };
  });

  app.get<{ Params: { id: string } }>('/runs/:id', async (request, reply) => {
    const run = store.getRun(request.params.id);
    if (!run) {
      reply.statusCode = 404;
      return {
        error: {
          category: 'INPUT_ERROR',
          reason: 'RUN_NOT_FOUND',
          message: `no run with id ${request.params.id}`,
          retryable: false,
        },
      };
    }
    return run;
  });

  app.get<{ Params: { id: string } }>('/runs/:id/events', async (request, reply) => {
    const events = store.getEvents(request.params.id);
    if (events.length === 0 && store.getRun(request.params.id) === null) {
      reply.statusCode = 404;
      return {
        error: {
          category: 'INPUT_ERROR',
          reason: 'RUN_NOT_FOUND',
          message: `no run with id ${request.params.id}`,
          retryable: false,
        },
      };
    }
    return { runId: request.params.id, count: events.length, events };
  });
}
