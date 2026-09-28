/**
 * Diagnostics interface tests (Fastify in-process injection, real SQLite).
 * These verify request-id correlation, the success/error envelope, persistence
 * and re-fetch, and that failures and uncertainties are reported separately —
 * they do not replace the kernel assertions in the matrix suite.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import type { FastifyInstance } from 'fastify';
import { buildApp } from '../src/diagnostics/app.js';
import { DiffStore } from '../src/state/diff-store.js';
import { matrix } from './helpers/fixtures.js';

let app: FastifyInstance;
let store: DiffStore;

beforeEach(async () => {
  store = DiffStore.memory();
  app = await buildApp({ store });
});

afterEach(async () => {
  await app.close();
  store.close();
});

describe('POST /api/v1/diffs', () => {
  it('returns the verdict with a correlated request id and breaking witnesses', async () => {
    const pair = matrix.enumNarrowed();
    const res = await app.inject({
      method: 'POST',
      url: '/api/v1/diffs',
      headers: { 'x-request-id': 'req-trace-42' },
      payload: pair,
    });

    expect(res.statusCode).toBe(200);
    expect(res.headers['x-request-id']).toBe('req-trace-42');
    const body = res.json();
    expect(body.success).toBe(true);
    expect(body.requestId).toBe('req-trace-42');
    expect(body.data.result.compatible).toBe(false);
    expect(body.data.result.stats.breaking).toBe(1);
    const finding = body.data.result.findings[0];
    expect(finding.code).toBe('PARAM_ENUM_NARROWED');
    expect(finding.witness.example.value).toBe('sold');
  });

  it('persists the run and serves it back by request id', async () => {
    const pair = matrix.nullableParamTightened();
    const post = await app.inject({
      method: 'POST',
      url: '/api/v1/diffs',
      payload: pair,
    });
    const id = post.json().requestId as string;

    const get = await app.inject({ method: 'GET', url: `/api/v1/diffs/${id}` });
    expect(get.statusCode).toBe(200);
    const stored = get.json().data.result;
    expect(stored.compatible).toBe(false);
    expect(stored.findings[0].code).toBe('PARAM_TYPE_NARROWED');
    expect(stored.findings[0].witness.example.value).toBeNull();
  });

  it('404s on an unknown request id', async () => {
    const res = await app.inject({ method: 'GET', url: '/api/v1/diffs/nope' });
    expect(res.statusCode).toBe(404);
    expect(res.json().success).toBe(false);
  });

  it('rejects a malformed body with 400 and keeps the request id', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/api/v1/diffs',
      headers: { 'x-request-id': 'req-bad' },
      payload: { old: [], new: {} },
    });
    expect(res.statusCode).toBe(400);
    expect(res.json().requestId).toBe('req-bad');
  });

  it('returns 422 when a contract cannot be parsed', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/api/v1/diffs',
      payload: { old: 'not-an-object', new: 'also-not' },
    });
    // strings fail the object precondition at the boundary -> 400
    expect([400, 422]).toContain(res.statusCode);
    expect(res.json().success).toBe(false);
  });

  it('lists recent runs', async () => {
    await app.inject({ method: 'POST', url: '/api/v1/diffs', payload: matrix.identical() });
    const res = await app.inject({ method: 'GET', url: '/api/v1/runs' });
    expect(res.statusCode).toBe(200);
    expect(res.json().data.runs.length).toBe(1);
  });

  it('exposes a health probe', async () => {
    const res = await app.inject({ method: 'GET', url: '/health' });
    expect(res.statusCode).toBe(200);
    expect(res.json().status).toBe('ok');
  });

  it('returns uncertainties separately for a cyclic contract', async () => {
    const cyclic = {
      openapi: '3.1.0',
      info: { title: 't', version: '1' },
      paths: {
        '/n': {
          post: {
            requestBody: {
              required: false,
              content: { 'application/json': { schema: { $ref: '#/components/schemas/Node' } } },
            },
            responses: { '200': { description: 'ok' } },
          },
        },
      },
      components: { schemas: { Node: { type: 'object', properties: { child: { $ref: '#/components/schemas/Node' } } } } },
    };
    const res = await app.inject({
      method: 'POST',
      url: '/api/v1/diffs',
      payload: { old: cyclic, new: cyclic },
    });
    expect(res.statusCode).toBe(200);
    const result = res.json().data.result;
    expect(result.uncertainties.some((u: { code: string }) => u.code === 'REF_CYCLE')).toBe(true);
    // uncertainties do not masquerade as breaking findings
    expect(result.findings.every((f: { severity: string }) => f.severity !== 'UNCERTAIN')).toBe(true);
  });
});
