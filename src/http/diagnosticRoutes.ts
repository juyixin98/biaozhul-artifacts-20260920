/**
 * 诊断接口（本地运维用途，默认仅绑定 127.0.0.1）：
 * - GET /diagnostics?limit=N  最近 N 条决策记录
 * - GET /diagnostics/:id      按请求标识查询单条
 * 返回内容只含脱敏后的形态信息，不回显查询正文与变量值。
 */
import type { FastifyInstance } from 'fastify';
import type { RingBufferSink } from '../diagnostics/logger.js';

export function registerDiagnosticsRoutes(app: FastifyInstance, sink: RingBufferSink): void {
  app.get('/diagnostics', async (request, reply) => {
    const query = request.query as { limit?: string };
    const limit = parseLimit(query.limit);
    if (limit === null) {
      return reply.code(400).send({
        error: 'BAD_REQUEST',
        message: '"limit" must be an integer between 1 and 500',
      });
    }
    const entries = sink.recent(limit);
    return reply.code(200).send({ count: entries.length, entries });
  });

  app.get('/diagnostics/:requestId', async (request, reply) => {
    const params = request.params as { requestId: string };
    const entry = sink.byRequestId(params.requestId);
    if (!entry) {
      return reply.code(404).send({
        error: 'NOT_FOUND',
        message: `No diagnostic entry for request id "${params.requestId}"`,
        requestId: params.requestId,
      });
    }
    return reply.code(200).send({ entry });
  });
}

function parseLimit(raw: string | undefined): number | null {
  if (raw === undefined) return 50;
  if (!/^[0-9]+$/.test(raw)) return null;
  const value = Number.parseInt(raw, 10);
  if (value < 1 || value > 500) return null;
  return value;
}
