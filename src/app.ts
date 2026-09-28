/**
 * 应用装配工厂：schema + SQLite 适配器 + 诊断服务 + Fastify 路由。
 * 同时供 server 入口与 HTTP 集成测试使用。
 */
import Fastify, { type FastifyInstance } from 'fastify';
import { appSchema } from './fixtures/schemaDef.js';
import { createSqliteAdapter, type SqliteAdapter } from './state/sqliteAdapter.js';
import { createService, type ServiceHandle } from './diag/service.js';
import { registerRoutes } from './http/routes.js';

export interface BuiltApp {
  app: FastifyInstance;
  service: ServiceHandle;
  adapter: SqliteAdapter;
}

export function createApp(logPath?: string): BuiltApp {
  const app = Fastify({ logger: false });
  const adapter = createSqliteAdapter();
  const service = createService(appSchema(), adapter, logPath);
  registerRoutes(app, service);
  return { app, service, adapter };
}
