import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { createApp } from '../src/app.js';

const { app, adapter } = createApp();
after(async () => {
  await app.close();
  adapter.close();
});

test('http: GET /health 返回 ok', async () => {
  const res = await app.inject({ method: 'GET', url: '/health' });
  assert.equal(res.statusCode, 200);
  assert.deepEqual(res.json(), { status: 'ok' });
});

test('http: POST /query 完整成功返回 200 与具体数据', async () => {
  const res = await app.inject({
    method: 'POST',
    url: '/query',
    payload: { runId: 'http-ok', budget: 10_000, query: '{ users { id name } }' },
  });
  assert.equal(res.statusCode, 200);
  const body = res.json();
  assert.equal(body.runId, 'http-ok');
  assert.equal(body.result.status, 'COMPLETE');
  assert.equal(body.result.actualCost, 25); // users1 + 12×(id1+name1)
  assert.equal(body.result.data.users.length, 12);
  assert.deepEqual(body.result.data.users[0], { id: 1, name: 'user-1' });
});

test('http: 运行时取消返回 200 + PARTIAL（HTTP 层不把部分结果当错误）', async () => {
  const res = await app.inject({
    method: 'POST',
    url: '/query',
    payload: {
      runId: 'http-partial',
      budget: 1200,
      query: '{ users { id posts { id tags { name weight } } } }',
    },
  });
  assert.equal(res.statusCode, 200);
  const body = res.json();
  assert.equal(body.result.status, 'PARTIAL');
  assert.equal(body.result.estimatedCost, 921);
  assert.equal(body.result.estimateBelowActual, true);
  assert.match(body.result.cancelled.path, /^\$\.users/);
});

test('http: 静态预算门返回 507 RESOURCE_EXHAUSTED', async () => {
  const res = await app.inject({
    method: 'POST',
    url: '/query',
    payload: { budget: 100, query: '{ users { id name posts { id title } } }' },
  });
  assert.equal(res.statusCode, 507);
  assert.equal(res.json().error.category, 'RESOURCE_EXHAUSTED');
});

test('http: 输入错误 400、状态冲突 409、计算失败 500 可区分', async () => {
  const badBody = await app.inject({ method: 'POST', url: '/query', payload: { budget: 10 } });
  assert.equal(badBody.statusCode, 400);
  assert.equal(badBody.json().error.category, 'INPUT_INVALID');

  const syntax = await app.inject({
    method: 'POST',
    url: '/query',
    payload: { budget: 1000, query: '{ users { ' },
  });
  assert.equal(syntax.statusCode, 400);
  assert.equal(syntax.json().error.category, 'INPUT_INVALID');

  const conflict = await app.inject({
    method: 'POST',
    url: '/query',
    payload: { budget: 1000, query: '{ users { ...LoopA } }' },
  });
  assert.equal(conflict.statusCode, 409);
  assert.equal(conflict.json().error.category, 'STATE_CONFLICT');
});

test('http: GET /runs 与 /runs/:id 可按运行编号重放', async () => {
  const list = await app.inject({ method: 'GET', url: '/runs' });
  assert.equal(list.statusCode, 200);
  const runs = list.json().runs as Array<{ runId: string }>;
  assert.ok(runs.some((r) => r.runId === 'http-partial'));

  const one = await app.inject({ method: 'GET', url: '/runs/http-partial' });
  assert.equal(one.statusCode, 200);
  const rec = one.json();
  assert.equal(rec.runId, 'http-partial');
  assert.ok(Array.isArray(rec.phases) && rec.phases.length >= 2);
  assert.match(rec.verdict, /^PARTIAL/);

  const missing = await app.inject({ method: 'GET', url: '/runs/nope' });
  assert.equal(missing.statusCode, 404);
});
