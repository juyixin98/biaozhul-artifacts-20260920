/**
 * Fastify 诊断接口。
 *
 * HTTP 状态码与失败类别的映射：
 *   INPUT_INVALID       → 400
 *   STATE_CONFLICT      → 409
 *   RESOURCE_EXHAUSTED  → 507（静态预算门拒绝；运行时 PARTIAL 仍返回 200 + status=PARTIAL）
 *   COMPUTATION_FAILED  → 500
 */
import type { FastifyInstance } from 'fastify';
import type { ServiceHandle } from '../diag/service.js';
import type { VariableValues } from '../contract/types.js';

interface QueryBody {
  query?: unknown;
  variables?: unknown;
  budget?: unknown;
  runId?: unknown;
}

export function registerRoutes(app: FastifyInstance, service: ServiceHandle): void {
  app.get('/health', async () => ({ status: 'ok' }));

  app.post('/query', async (req, reply) => {
    const body = req.body as QueryBody | null;
    if (!body || typeof body.query !== 'string' || typeof body.budget !== 'number') {
      return reply.code(400).send({
        error: {
          category: 'INPUT_INVALID',
          phase: 'HTTP',
          message: 'body must contain string "query" and numeric "budget"',
          context: { received: body },
        },
      });
    }
    if (body.variables !== undefined && (typeof body.variables !== 'object' || body.variables === null || Array.isArray(body.variables))) {
      return reply.code(400).send({
        error: {
          category: 'INPUT_INVALID',
          phase: 'HTTP',
          message: '"variables" must be an object',
          context: {},
        },
      });
    }

    const { runId, outcome } = service.run({
      query: body.query,
      budget: body.budget,
      variables: body.variables as VariableValues | undefined,
      runId: typeof body.runId === 'string' ? body.runId : undefined,
    });

    if (outcome.ok) {
      return reply.code(200).send({ runId, result: outcome.result });
    }
    const e = outcome.error!;
    const status =
      e.category === 'INPUT_INVALID' ? 400
        : e.category === 'STATE_CONFLICT' ? 409
          : e.category === 'RESOURCE_EXHAUSTED' ? 507
            : 500;
    return reply.code(status).send({ runId, error: e });
  });

  app.get('/runs', async () => ({
    runs: service.store.list().map((r) => ({
      runId: r.runId,
      startedAt: r.startedAt,
      verdict: r.verdict,
      query: r.request.query,
      budget: r.request.budget,
    })),
  }));

  app.get('/runs/:id', async (req, reply) => {
    const { id } = req.params as { id: string };
    const rec = service.store.get(id);
    if (!rec) return reply.code(404).send({ error: { category: 'INPUT_INVALID', message: `unknown runId ${id}` } });
    return rec;
  });
}
