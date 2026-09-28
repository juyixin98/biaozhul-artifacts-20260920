/**
 * Fastify 诊断/执行 HTTP 接口。
 *
 * 路由：
 * - GET  /health           存活检查
 * - POST /query/explain    只做静态成本估计与网关判定，不接触状态层
 * - POST /query            执行；超限在静态阶段返回 413，运行中超限
 *                          返回 200 + status=partial（部分结果是正常响应）
 * - GET  /runs             最近运行编号列表
 * - GET  /runs/:id         单次运行的完整重放记录（中间状态/判定理由/台账）
 *
 * 统一响应包络：{ success, data, error, meta }。
 */
import Fastify, { type FastifyInstance } from 'fastify';
import type { Diagnostic } from '../contracts/errors.js';
import type { QueryEngine } from '../kernel/engine.js';
import type { RunLogger } from '../diagnostics/run-log.js';

export interface AppDeps {
  engine: QueryEngine;
  logger: RunLogger;
}

function statusFor(diag: Diagnostic): number {
  switch (diag.category) {
    case 'INPUT_ERROR':
      return 400;
    case 'STATE_CONFLICT':
      // 状态前置条件冲突（如根对象不存在）。
      return 409;
    case 'RESOURCE_EXHAUSTED':
      // 静态预算拒绝语义上最接近 413 Payload Too Large。
      return 413;
    case 'COMPUTATION_FAILED':
    default:
      return 500;
  }
}

export function createApp(deps: AppDeps): FastifyInstance {
  const app = Fastify({ logger: false });

  app.get('/health', async () => ({ success: true, data: { status: 'up' } }));

  app.post('/query/explain', async (request, reply) => {
    const body = request.body as { budget?: unknown; query?: unknown };
    const result = deps.engine.explain(body?.query, Number(body?.budget));
    if (result.error) {
      return reply.status(statusFor(result.error)).send({
        success: false,
        error: result.error,
        data: { runId: result.runId },
      });
    }
    return reply.send({
      success: true,
      data: {
        runId: result.runId,
        accepted: result.accepted,
        estimatedTotal: result.estimatedTotal,
        budget: result.budget,
        reason: result.reason,
        costTree: result.costTree,
      },
    });
  });

  app.post('/query', async (request, reply) => {
    const body = request.body as { budget?: unknown; query?: unknown };
    const result = await deps.engine.execute(body?.query, Number(body?.budget));

    if (result.status === 'rejected_static' && result.error) {
      return reply.status(413).send({
        success: false,
        error: result.error,
        data: {
          runId: result.runId,
          estimatedTotal: result.estimatedTotal,
          costTree: result.costTree,
        },
      });
    }
    if (result.status === 'failed' && result.error) {
      return reply.status(statusFor(result.error)).send({
        success: false,
        error: result.error,
        data: { runId: result.runId },
      });
    }
    // ok 与 partial 都是 200：partial 携带部分结果与取消信息。
    return reply.send({
      success: result.status === 'ok',
      data: result,
      meta: {
        partial: result.status === 'partial',
        warnings: result.warnings.length,
      },
    });
  });

  app.get('/runs', async () => ({
    success: true,
    data: deps.logger.list().map((e) => ({
      runId: e.runId,
      startedAt: e.startedAt,
      budget: e.budget,
      estimatedTotal: e.estimatedTotal,
      finalStatus: e.finalStatus,
      consumed: e.consumed,
      decisionReason: e.decisionReason,
    })),
  }));

  app.get('/runs/:id', async (request, reply) => {
    const { id } = request.params as { id: string };
    const entry = deps.logger.get(id);
    if (!entry) {
      return reply.status(404).send({
        success: false,
        error: { category: 'STATE_CONFLICT', code: 'ROOT_NOT_FOUND', message: `no such run: ${id}` },
      });
    }
    return reply.send({ success: true, data: entry });
  });

  return app;
}
