/**
 * 测试公共辅助：在内存 SQLite 上构建真实装配。
 * 参考答案独立于被测内核——单元测试另用自构 schema/解析器，
 * 不通过生产 schema 的解析器生成期望值。
 */
import { DatabaseSync } from 'node:sqlite';
import { buildApp, type BuiltApp } from '../../src/app.js';
import { migrateDatabase } from '../../src/data/db.js';
import { seedDatabase } from '../../src/data/fixtures.js';

export function buildTestApp(): BuiltApp & { db: DatabaseSync } {
  const db = new DatabaseSync(':memory:');
  db.exec('PRAGMA foreign_keys = ON;');
  migrateDatabase(db);
  seedDatabase(db);
  const built = buildApp({ db, ringBufferSize: 100 });
  return { ...built, db };
}

export interface RawHttpResponse {
  statusCode: number;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  json: any;
  requestId: string | undefined;
}

export async function graphql(
  built: BuiltApp,
  body: { query: string; variables?: Record<string, unknown>; operationName?: string | null },
): Promise<RawHttpResponse> {
  const response = await built.app.inject({
    method: 'POST',
    url: '/graphql',
    headers: { 'content-type': 'application/json' },
    payload: body,
  });
  return {
    statusCode: response.statusCode,
    json: response.json(),
    requestId: response.headers['x-request-id'] as string | undefined,
  };
}
