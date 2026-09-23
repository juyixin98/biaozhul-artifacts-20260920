/**
 * HTTP 服务端到端测试：注入后直接走 fastify.inject，无需占用端口。
 */

import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { FastifyInstance } from 'fastify';

import { buildServer } from '../src/server.js';

const here = dirname(fileURLToPath(import.meta.url));

function loadPair(name: string): [unknown, unknown] {
  const dir = join(here, '..', 'samples', 'pairs', name);
  return [
    JSON.parse(readFileSync(join(dir, 'v1.storageLayout.json'), 'utf8')),
    JSON.parse(readFileSync(join(dir, 'v2.storageLayout.json'), 'utf8')),
  ];
}

describe('HTTP API', () => {
  let app: FastifyInstance;

  before(async () => {
    app = await buildServer();
  });
  after(async () => {
    await app.close();
  });

  it('GET /healthz', async () => {
    const res = await app.inject({ method: 'GET', url: '/healthz' });
    assert.equal(res.statusCode, 200);
    assert.deepEqual(res.json(), { ok: true, service: 'storage-layout-compat' });
  });

  it('GET / 返回用法', async () => {
    const res = await app.inject({ method: 'GET', url: '/' });
    assert.equal(res.statusCode, 200);
    assert.ok((res.json() as { endpoints: unknown }).endpoints);
  });

  it('POST /check 安全追加 -> compatible', async () => {
    const [o, n] = loadPair('01-safe-append');
    const res = await app.inject({
      method: 'POST',
      url: '/check',
      payload: { old: o, new: n },
    });
    assert.equal(res.statusCode, 200);
    const body = res.json() as { verdict: string };
    assert.equal(body.verdict, 'compatible');
  });

  it('POST /check 接受 newLayout 别名', async () => {
    const [o, n] = loadPair('04-gap-shrink');
    const res = await app.inject({
      method: 'POST',
      url: '/check',
      payload: { oldLayout: o, newLayout: n },
    });
    assert.equal(res.statusCode, 200);
    assert.equal((res.json() as { verdict: string }).verdict, 'compatible');
  });

  it('POST /check 插入 -> incompatible', async () => {
    const [o, n] = loadPair('03-packed-insert');
    const res = await app.inject({
      method: 'POST',
      url: '/check',
      payload: { old: o, new: n },
    });
    assert.equal(res.statusCode, 200);
    const body = res.json() as { verdict: string; findings: { kind: string }[] };
    assert.equal(body.verdict, 'incompatible');
    assert.ok(body.findings.some((f) => f.kind === 'field-inserted'));
  });

  it('POST /check 未知类型 -> unknown（不默认安全）', async () => {
    const [o, n] = loadPair('18-unknown-type');
    const res = await app.inject({
      method: 'POST',
      url: '/check',
      payload: { old: o, new: n },
    });
    assert.equal(res.statusCode, 200);
    assert.equal((res.json() as { verdict: string }).verdict, 'unknown');
  });

  it('POST /check 缺字段 -> 400', async () => {
    const res = await app.inject({ method: 'POST', url: '/check', payload: { old: {} } });
    assert.equal(res.statusCode, 400);
  });

  it('POST /check 非法 gapNamePattern -> 400', async () => {
    const [o, n] = loadPair('01-safe-append');
    const res = await app.inject({
      method: 'POST',
      url: '/check',
      payload: { old: o, new: n, gapNamePattern: '([a-z' },
    });
    assert.equal(res.statusCode, 400);
  });

  it('POST /check-artifacts', async () => {
    const [o, n] = loadPair('11-fixed-array-length');
    const res = await app.inject({
      method: 'POST',
      url: '/check-artifacts',
      payload: { oldArtifact: { storageLayout: o }, newArtifact: { storageLayout: n } },
    });
    assert.equal(res.statusCode, 200);
    assert.equal((res.json() as { verdict: string }).verdict, 'incompatible');
  });

  it('POST /check 非法 JSON 结构 -> 400（不崩溃）', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/check',
      payload: { old: { storage: [] }, new: { storage: [] } },
    });
    assert.equal(res.statusCode, 400);
  });
});
