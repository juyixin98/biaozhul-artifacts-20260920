/**
 * Fastify 应用工厂。
 *  - POST /graphql：标准 GraphQL-over-HTTP 单请求入口
 *  - GET  /healthz：存活检查
 *  - GET  /diagnostics：最近请求的诊断记录（支持 ?requestId=）
 *
 * 注意：执行错误不是 HTTP 500——只要请求被处理，统一返回 200，
 * 错误在 GraphQL 响应体的 errors 中表达（规范的部分数据语义）。
 */

import Fastify, { type FastifyInstance } from 'fastify';
import type { Database as DatabaseType } from 'better-sqlite3';
import { runGraphQL, type GraphQLSchema } from '../graphql/index.js';
import {
  DiagnosticCollector,
  emitStructuredLog,
} from '../diagnostics/collector.js';
import type { AppConfig } from './config.js';

export interface AppDeps {
  schema: GraphQLSchema;
  db: DatabaseType;
  config: AppConfig;
  collector?: DiagnosticCollector;
}

interface GraphQLBody {
  query?: string;
  variables?: Record<string, unknown> | null;
  operationName?: string | null;
}

export async function buildApp(deps: AppDeps): Promise<FastifyInstance> {
  const { schema, db, config } = deps;
  const collector = deps.collector ?? new DiagnosticCollector();
  const app = Fastify({ logger: false });

  app.get('/healthz', async () => ({
    status: 'ok',
    database: db.name,
  }));

  app.post('/graphql', async (request, reply) => {
    const requestId = collector.newRequestId();
    const startedAt = Date.now();
    void reply.header('x-request-id', requestId);

    const body = (request.body ?? {}) as GraphQLBody;
    if (typeof body.query !== 'string' || body.query.trim() === '') {
      const result = {
        errors: [
          {
            message: 'Request body must include a non-empty "query" string.',
            extensions: { category: 'VALIDATION' },
          },
        ],
      };
      collector.record(requestId, {
        decision: 'rejected',
        reasons: ['MALFORMED_REQUEST'],
        operationType: null,
        operationName: null,
        variables: body.variables ?? null,
        errors: result.errors,
        durationMs: Date.now() - startedAt,
      });
      return reply.code(400).send(result);
    }

    const outcome = await runGraphQL({
      schema,
      query: body.query,
      variables: body.variables ?? {},
      operationName: body.operationName ?? null,
      context: { requestId, startedAt, db },
    });

    const formattedErrors = (outcome.result.errors ?? []).map((e) =>
      e.toFormattedError(),
    );
    const payload: Record<string, unknown> = {};
    if ('data' in outcome.result) payload.data = outcome.result.data;
    if (formattedErrors.length > 0) payload.errors = formattedErrors;

    collector.record(requestId, {
      decision: outcome.decision,
      reasons: outcome.reasons,
      operationType: outcome.operation?.operation ?? null,
      operationName: outcome.operation?.name?.value ?? null,
      variables: body.variables ?? null,
      errors: formattedErrors.map((e) => ({
        message: e.message,
        path: e.path,
        category: (e.extensions.category as string) ?? 'INTERNAL',
      })),
      durationMs: Date.now() - startedAt,
    });
    if (config.logRequests) {
      emitStructuredLog(collector.get(requestId)!);
    }
    return reply.code(200).send(payload);
  });

  app.get('/diagnostics', async (request, reply) => {
    const query = request.query as { requestId?: string; limit?: string };
    if (query.requestId) {
      const entry = collector.get(query.requestId);
      if (!entry) {
        return reply.code(404).send({
          error: 'not_found',
          message: `No diagnostic entry for requestId "${query.requestId}".`,
        });
      }
      return { entry };
    }
    const limit = Math.min(Number.parseInt(query.limit ?? '50', 10) || 50, 200);
    return { entries: collector.list(limit) };
  });

  return app;
}
