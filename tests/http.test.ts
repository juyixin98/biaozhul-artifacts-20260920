/**
 * HTTP 诊断接口集成测试（Fastify inject，不起真实端口）。
 */
import { afterAll, describe, expect, it } from 'vitest';
import { buildApp, type BuiltApp } from '../src/bootstrap.js';

let built: BuiltApp;

function app(): BuiltApp {
  if (!built) built = buildApp({ logFile: null });
  return built;
}

afterAll(async () => {
  if (built) {
    await built.app.close();
    built.db.close();
  }
});

describe('HTTP 接口与状态码语义', () => {
  it('GET /health', async () => {
    const res = await app().app.inject({ method: 'GET', url: '/health' });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual({ success: true, data: { status: 'up' } });
  });

  it('POST /query/explain 不执行即可给出估计与网关判定', async () => {
    const res = await app().app.inject({
      method: 'POST',
      url: '/query/explain',
      payload: {
        budget: 100,
        query: {
          root: 'Org',
          rootId: 1,
          fields: [{ kind: 'field', alias: 'name', field: 'name' }],
        },
      },
    });
    expect(res.statusCode).toBe(200);
    const body = res.json();
    expect(body.success).toBe(true);
    expect(body.data.estimatedTotal).toBe(1);
    expect(body.data.accepted).toBe(true);
    expect(body.data.runId).toMatch(/^run_/);
    expect(body.data.costTree.label).toBe('root:Org');
  });

  it('静态超预算返回 413 且不执行', async () => {
    const res = await app().app.inject({
      method: 'POST',
      url: '/query',
      payload: {
        budget: 0,
        query: {
          root: 'Org',
          rootId: 1,
          fields: [{ kind: 'field', alias: 'name', field: 'name' }],
        },
      },
    });
    expect(res.statusCode).toBe(413);
    expect(res.json().error).toMatchObject({
      category: 'RESOURCE_EXHAUSTED',
      code: 'BUDGET_EXCEEDED_STATIC',
    });
  });

  it('输入错误返回 400 / INPUT_ERROR', async () => {
    const res = await app().app.inject({
      method: 'POST',
      url: '/query',
      payload: { budget: 10, query: { root: 'Org', rootId: 1, fields: 42 } },
    });
    expect(res.statusCode).toBe(400);
    expect(res.json().error.category).toBe('INPUT_ERROR');
  });

  it('状态冲突（根不存在）返回 409 / STATE_CONFLICT', async () => {
    const res = await app().app.inject({
      method: 'POST',
      url: '/query',
      payload: {
        budget: 10,
        query: {
          root: 'Org',
          rootId: 4040,
          fields: [{ kind: 'field', alias: 'name', field: 'name' }],
        },
      },
    });
    expect(res.statusCode).toBe(409);
    expect(res.json().error).toMatchObject({
      category: 'STATE_CONFLICT',
      code: 'ROOT_NOT_FOUND',
    });
  });

  it('运行中超限是 200 + partial，携带部分结果、取消信息与台账', async () => {
    const res = await app().app.inject({
      method: 'POST',
      url: '/query',
      payload: {
        budget: 5,
        query: {
          root: 'Org',
          rootId: 1,
          fields: [
            { kind: 'field', alias: 'name', field: 'name' },
            {
              kind: 'list',
              alias: 'orgTasks',
              relation: 'orgTasks',
              declaredUpperBound: 2,
              children: [{ kind: 'field', alias: 'title', field: 'title' }],
            },
            { kind: 'field', alias: 'plan', field: 'plan' },
          ],
        },
      },
    });
    expect(res.statusCode).toBe(200);
    const body = res.json();
    expect(body.success).toBe(false);
    expect(body.meta.partial).toBe(true);
    expect(body.data.status).toBe('partial');
    expect(body.data.abort).toMatchObject({
      category: 'RESOURCE_EXHAUSTED',
      code: 'BUDGET_EXCEEDED_RUNTIME',
      consumed: 5,
    });
    expect(body.data.data.orgTasks).toHaveLength(5);
    expect(body.data.ledger.at(-1).rejected).toBe(true);
  });
});

describe('诊断运行记录', () => {
  it('每次执行可用 runId 取回含中间阶段与判定理由的重放记录', async () => {
    const a = app();
    const exec = await a.app.inject({
      method: 'POST',
      url: '/query',
      payload: {
        budget: 100,
        query: {
          root: 'Org',
          rootId: 1,
          fields: [{ kind: 'field', alias: 'name', field: 'name' }],
        },
      },
    });
    const runId = exec.json().data.runId as string;

    const detail = await a.app.inject({ method: 'GET', url: `/runs/${runId}` });
    expect(detail.statusCode).toBe(200);
    const record = detail.json().data;
    expect(record.runId).toBe(runId);
    expect(record.finalStatus).toBe('ok');
    expect(record.estimatedTotal).toBe(1);
    expect(record.decisionReason).toContain('accepted');
    const phaseNames = record.phases.map((p: { phase: string }) => p.phase);
    expect(phaseNames).toEqual(
      expect.arrayContaining([
        'parse',
        'validate-vars',
        'expand-fragments',
        'validate-selection',
        'estimate-static',
        'gateway-accepted',
        'execute',
      ]),
    );
    expect(record.ledger.length).toBe(1);
  });

  it('GET /runs 返回最近运行列表；未知编号 404', async () => {
    const a = app();
    const list = await a.app.inject({ method: 'GET', url: '/runs' });
    expect(list.statusCode).toBe(200);
    expect(Array.isArray(list.json().data)).toBe(true);
    expect(list.json().data.length).toBeGreaterThan(0);

    const missing = await a.app.inject({ method: 'GET', url: '/runs/run_does_not_exist' });
    expect(missing.statusCode).toBe(404);
  });
});
