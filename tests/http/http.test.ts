/**
 * HTTP 层与诊断接口测试：请求信封、状态码分类、请求标识关联、脱敏。
 */
import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { buildTestApp, graphql } from '../fixtures/helpers.js';

const built = buildTestApp();
after(async () => {
  await built.app.close();
  built.db.close();
});

test('health: GET /health 返回 ok', async () => {
  const res = await built.app.inject({ method: 'GET', url: '/health' });
  assert.equal(res.statusCode, 200);
  assert.deepEqual(res.json(), { status: 'ok' });
});

test('信封: 非 JSON 对象 body -> 400 BAD_REQUEST 且不执行', async () => {
  const res = await built.app.inject({
    method: 'POST',
    url: '/graphql',
    headers: { 'content-type': 'application/json' },
    payload: JSON.stringify([1, 2, 3]),
  });
  assert.equal(res.statusCode, 400);
  const body = res.json() as { data: null; errors: Array<{ extensions: { category: string } }> };
  assert.equal(body.data, null);
  assert.equal(body.errors[0]!.extensions.category, 'BAD_REQUEST');
});

test('信封: 缺少 query 字符串 -> 400 BAD_REQUEST', async () => {
  const res = await built.app.inject({
    method: 'POST',
    url: '/graphql',
    headers: { 'content-type': 'application/json' },
    payload: JSON.stringify({ variables: {} }),
  });
  assert.equal(res.statusCode, 400);
  assert.equal(
    (res.json() as { errors: Array<{ extensions: { category: string } }> }).errors[0]!.extensions
      .category,
    'BAD_REQUEST',
  );
});

test('信封: variables 非对象 -> 400 BAD_REQUEST', async () => {
  const res = await built.app.inject({
    method: 'POST',
    url: '/graphql',
    headers: { 'content-type': 'application/json' },
    payload: JSON.stringify({ query: '{ __typename }', variables: 5 }),
  });
  // __typename 不支持，会先在变量信封阶段失败
  assert.equal(res.statusCode, 400);
  assert.match(
    (res.json() as { errors: Array<{ message: string }> }).errors[0]!.message,
    /variables/i,
  );
});

test('GET: 查询参数形式可用，variables 作为 JSON 字符串传入', async () => {
  const res = await built.app.inject({
    method: 'GET',
    url:
      '/graphql?query=' +
      encodeURIComponent('query Q($id: ID!){ post(id: $id){ id } }') +
      '&variables=' +
      encodeURIComponent('{"id":"p-01"}'),
  });
  assert.equal(res.statusCode, 200, res.body);
  assert.deepEqual((res.json() as { data: unknown }).data, { post: { id: 'p-01' } });
});

test('GET: variables 非法 JSON -> 400 BAD_REQUEST', async () => {
  const res = await built.app.inject({
    method: 'GET',
    url: '/graphql?query=' + encodeURIComponent('{ post(id:"x"){id} }') + '&variables=not-json',
  });
  assert.equal(res.statusCode, 400);
});

test('状态码: 未知 operationName -> 404 UNKNOWN_OPERATION', async () => {
  const res = await built.app.inject({
    method: 'POST',
    url: '/graphql',
    headers: { 'content-type': 'application/json' },
    payload: JSON.stringify({ query: 'query A { post(id:"p-01"){id} }', operationName: 'Nope' }),
  });
  assert.equal(res.statusCode, 404);
  assert.equal(
    (res.json() as { errors: Array<{ extensions: { category: string } }> }).errors[0]!.extensions
      .category,
    'UNKNOWN_OPERATION',
  );
});

test('状态码: 多操作未给 operationName -> 400 AMBIGUOUS_OPERATION', async () => {
  const res = await graphql(built, {
    query: `query A { post(id: "p-01") { id } } query B { post(id: "p-02") { id } }`,
  });
  assert.equal(res.statusCode, 400);
  assert.equal(res.json.errors[0].extensions.category, 'AMBIGUOUS_OPERATION');
});

test('诊断: 成功请求被记录为 ACCEPTED，可用 x-request-id 关联', async () => {
  const gql = await graphql(built, {
    query: `{ post(id: "p-01") { id } }`,
  });
  assert.equal(gql.statusCode, 200);
  assert.ok(gql.requestId);

  const diag = await built.app.inject({
    method: 'GET',
    url: `/diagnostics/${encodeURIComponent(gql.requestId!)}`,
  });
  assert.equal(diag.statusCode, 200);
  const entry = (diag.json() as { entry: Record<string, unknown> }).entry;
  assert.equal(entry.requestId, gql.requestId);
  assert.equal(entry.decision, 'ACCEPTED');
  assert.equal(entry.phase, 'execute');
  assert.deepEqual(entry.reasons, ['OK']);
  assert.equal(entry.httpStatus, 200);
  assert.equal(entry.operationType, 'query');
});

test('诊断: 拒绝请求记录为 REJECTED 且原因是 FIELD_CONFLICT', async () => {
  const gql = await graphql(built, {
    query: `{ post(id: "p-01") { x: id x: title } }`,
  });
  assert.equal(gql.statusCode, 400);
  const diag = await built.app.inject({
    method: 'GET',
    url: `/diagnostics/${encodeURIComponent(gql.requestId!)}`,
  });
  const entry = (diag.json() as { entry: Record<string, unknown> }).entry;
  assert.equal(entry.decision, 'REJECTED');
  assert.deepEqual(entry.reasons, ['FIELD_CONFLICT']);
});

test('诊断: 信封错误记录为 UNDECIDABLE', async () => {
  const res = await built.app.inject({
    method: 'POST',
    url: '/graphql',
    headers: { 'content-type': 'application/json' },
    payload: JSON.stringify({}),
  });
  const requestId = res.headers['x-request-id'] as string;
  const diag = await built.app.inject({
    method: 'GET',
    url: `/diagnostics/${encodeURIComponent(requestId)}`,
  });
  const entry = (diag.json() as { entry: Record<string, unknown> }).entry;
  assert.equal(entry.decision, 'UNDECIDABLE');
  assert.equal(entry.phase, 'envelope');
});

test('诊断: 列表接口支持 limit 且非法 limit 返回 400', async () => {
  await graphql(built, { query: `{ post(id: "p-01") { id } }` });
  const ok = await built.app.inject({ method: 'GET', url: '/diagnostics?limit=2' });
  assert.equal(ok.statusCode, 200);
  const body = ok.json() as { count: number };
  assert.ok(body.count <= 2);

  const bad = await built.app.inject({ method: 'GET', url: '/diagnostics?limit=abc' });
  assert.equal(bad.statusCode, 400);
});

test('诊断: 查询未知请求标识 -> 404', async () => {
  const res = await built.app.inject({ method: 'GET', url: '/diagnostics/no-such-id' });
  assert.equal(res.statusCode, 404);
});

test('脱敏: 诊断记录不含变量真实值，只含形态', async () => {
  await graphql(built, {
    query: `query Q($ids: [ID!]) { users(ids: $ids) { id email } }`,
    variables: { ids: ['u-01'] },
  });
  const list = await built.app.inject({ method: 'GET', url: '/diagnostics?limit=50' });
  const entries = (list.json() as { entries: Array<{ variableShapes: Record<string, string> }> })
    .entries;
  const target = entries.find((e) => e.variableShapes.ids !== undefined);
  assert.ok(target);
  assert.equal(target!.variableShapes.ids, 'list[1]');
  // 记录序列化后不得包含真实变量值或 email 内容
  assert.doesNotMatch(list.body, /u-01@example\.test/);
  assert.doesNotMatch(list.body, /"ids":\[/);
});
