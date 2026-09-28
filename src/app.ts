/**
 * 应用装配：把执行内核、状态适配（仓储/解析器）、诊断与 HTTP 绑定在一起。
 * 数据库从外部注入，便于集成测试使用 :memory:。
 */
import Fastify, { type FastifyInstance } from 'fastify';
import type { DatabaseSync } from 'node:sqlite';
import { createRepository, type Repository } from './data/repository.js';
import { buildResolvers, type AppContext } from './resolvers.js';
import { buildSchema } from './graphql/schema.js';
import { RingBufferSink, DiagnosticLogger } from './diagnostics/logger.js';
import { createGraphqlHandler } from './http/gqlHandler.js';
import { registerDiagnosticsRoutes } from './http/diagnosticRoutes.js';

export interface AppOptions {
  db: DatabaseSync;
  ringBufferSize?: number;
  logFile?: string | null;
  /** 测试可注入自定义仓储（如内存实现） */
  repositoryFactory?: (db: DatabaseSync) => Repository;
}

export interface BuiltApp {
  app: FastifyInstance;
  schema: ReturnType<typeof buildSchema<AppContext>>;
  sink: RingBufferSink;
  diagnostics: DiagnosticLogger;
  repository: Repository;
}

export function buildApp(options: AppOptions): BuiltApp {
  const app = Fastify({
    logger: {
      transport: undefined,
      level: process.env.LOG_LEVEL ?? 'info',
    },
    bodyLimit: 1 * 1024 * 1024,
  });

  const repository = (options.repositoryFactory ?? createRepository)(options.db);
  const schema = buildSchema<AppContext>({ resolvers: buildResolvers() });
  const sink = new RingBufferSink(options.ringBufferSize ?? 200, options.logFile ?? null);
  const diagnostics = new DiagnosticLogger(sink);

  const gqlHandler = createGraphqlHandler({
    schema,
    createContext: (requestId: string): AppContext => ({ repo: repository, requestId }),
    diagnostics,
  });

  app.get('/health', async () => ({ status: 'ok' }));
  app.post('/graphql', gqlHandler);
  app.get('/graphql', gqlHandler);

  app.setNotFoundHandler((request, reply) => {
    void request;
    reply.code(404).send({ error: 'NOT_FOUND', message: 'Use POST /graphql or GET /diagnostics' });
  });

  registerDiagnosticsRoutes(app, sink);

  return { app, schema, sink, diagnostics, repository };
}
