/**
 * Fastify HTTP 集成测试：
 *  - GraphQL-over-HTTP：部分数据返回 200 而非统一 500
 *  - x-request-id 与 /diagnostics 联动
 *  - 请求体校验、healthz
 */

import { afterEach, describe, expect, it } from 'vitest';
import { buildApp } from '../../src/server/app.js';
import { loadConfig } from '../../src/server/config.js';
import { DiagnosticCollector } from '../../src/diagnostics/collector.js';
import { createHarness } from '../helpers/harness.js';
import type { FastifyInstance } from 'fastify';

async function makeServer(): Promise<{ app: FastifyInstance; db: import('better-sqlite3').Database }> {
  const harness = await createHarness();
  const collector = new DiagnosticCollector(50);
  const app = await buildApp({
    schema: harness.schema,
    db: harness.db,
    config: { ...loadConfig({}), logRequests: false },
    collector,
  });
  return { app, db: harness.db };
}

describe('HTTP 层', () => {
  let app: FastifyInstance | null = null;

  afterEach(async () => {
    if (app) await app.close();
    app = null;
  });

  it('健康检查返回 ok', async () => {
    const server = await makeServer();
    app = server.app;
    const res = await app.inject({ method: 'GET', url: '/healthz' });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toMatchObject({ status: 'ok' });
  });

  it('成功查询返回 200 与 data，响应头带 x-request-id', async () => {
    const server = await makeServer();
    app = server.app;
    const res = await app.inject({
      method: 'POST',
      url: '/graphql',
      payload: { query: '{ users(limit: 1) { id name } }' },
    });
    expect(res.statusCode).toBe(200);
    expect(res.headers['x-request-id']).toBeTruthy();
    expect(res.json()).toEqual({
      data: { users: [{ id: 'u1', name: 'Alice' }] },
    });
  });

  it('解析器失败的部分数据仍为 200，错误携带 path/category', async () => {
    const server = await makeServer();
    app = server.app;
    const res = await app.inject({
      method: 'POST',
      url: '/graphql',
      payload: { query: '{ strictTags }' },
    });
    expect(res.statusCode).toBe(200);
    const body = res.json();
    expect(body.data).toBeNull();
    expect(body.errors[0]).toMatchObject({
      path: ['strictTags', 2],
      extensions: { category: 'RESOLVER' },
    });
  });

  it('校验拒绝为 200 且无 data（GraphQL 语义），不触碰 HTTP 500', async () => {
    const server = await makeServer();
    app = server.app;
    const res = await app.inject({
      method: 'POST',
      url: '/graphql',
      payload: {
        query: 'fragment A on User { ...B } fragment B on User { ...A } query { users(limit: 1) { ...A } }',
      },
    });
    expect(res.statusCode).toBe(200);
    const body = res.json();
    expect(body.data).toBeUndefined();
    expect(body.errors[0].extensions.category).toBe('VALIDATION');
  });

  it('缺少 query 字符串 => 400（HTTP 层请求格式错误）', async () => {
    const server = await makeServer();
    app = server.app;
    const res = await app.inject({
      method: 'POST',
      url: '/graphql',
      payload: { variables: {} },
    });
    expect(res.statusCode).toBe(400);
    expect(res.json().errors[0].extensions.category).toBe('VALIDATION');
  });

  it('diagnostics 按 requestId 取回决策记录，变量被脱敏', async () => {
    const server = await makeServer();
    app = server.app;
    const gqlRes = await app.inject({
      method: 'POST',
      url: '/graphql',
      payload: {
        query: 'query($id: ID!) { user(id: $id) { id email } }',
        variables: { id: 'u1' },
      },
    });
    const requestId = gqlRes.headers['x-request-id'] as string;

    const diagRes = await app.inject({
      method: 'GET',
      url: `/diagnostics?requestId=${requestId}`,
    });
    expect(diagRes.statusCode).toBe(200);
    const entry = diagRes.json().entry;
    expect(entry.requestId).toBe(requestId);
    expect(entry.decision).toBe('accepted');
    expect(entry.reasons).toContain('EXECUTED');
    expect(entry.variableShape).toEqual({ id: 'string' });
    expect(entry.explanation).toContain('完整执行');

    // email 字段实际值不应出现在诊断记录中（数据中 email 是解析结果，此处只校验变量脱敏）
    expect(JSON.stringify(entry)).not.toContain('alice@example.com');
  });

  it('敏感变量值在诊断中打码', async () => {
    const server = await makeServer();
    app = server.app;
    // 用一个会失败的变量制造记录；password 值不应泄露
    await app.inject({
      method: 'POST',
      url: '/graphql',
      payload: {
        query: 'query($n: Int!) { users(limit: $n) { id } }',
        variables: { n: 'x', password: 'hunter2', token: 'tok-secret' },
      },
    });
    const list = await app.inject({ method: 'GET', url: '/diagnostics?limit=1' });
    const serialized = JSON.stringify(list.json());
    expect(serialized).not.toContain('hunter2');
    expect(serialized).not.toContain('tok-secret');
    expect(serialized).toContain('<redacted>');
  });

  it('未知 requestId => 404', async () => {
    const server = await makeServer();
    app = server.app;
    const res = await app.inject({
      method: 'GET',
      url: '/diagnostics?requestId=does-not-exist',
    });
    expect(res.statusCode).toBe(404);
  });
});
