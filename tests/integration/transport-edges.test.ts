/**
 * Transport-edge cases: content negotiation, error envelopes, diagnostics
 * endpoints, paging and body limits. These raise coverage of the HTTP layer
 * and pin down concrete failure categories.
 */
import { describe, expect, it } from 'vitest';
import { buildTempApp, injectStep } from './replay.js';

describe('HTTP transport edges', () => {
  it('rejects malformed JSON bodies with INVALID_JSON_BODY (400)', async () => {
    const env = buildTempApp();
    try {
      const res = await env.app.inject({
        method: 'PUT', url: '/resources/j',
        headers: { 'content-type': 'application/json' },
        payload: '{not json'
      });
      expect(res.statusCode).toBe(400);
      expect(JSON.parse(res.body).error.code).toBe('INVALID_JSON_BODY');
    } finally {
      env.cleanup();
    }
  });

  it('accepts application/merge-patch+json for PATCH and applies RFC 7386', async () => {
    const env = buildTempApp();
    try {
      await injectStep(env.app, {
        method: 'PUT', path: '/resources/p', body: { a: 1, b: { c: 2, drop: 1 } }, headers: {}
      });
      const res = await env.app.inject({
        method: 'PATCH', url: '/resources/p',
        headers: { 'content-type': 'application/merge-patch+json' },
        payload: JSON.stringify({ b: { drop: null, c: 3 } })
      });
      expect(res.statusCode).toBe(200);
      expect(JSON.parse(res.body).body).toEqual({ a: 1, b: { c: 3 } });
    } finally {
      env.cleanup();
    }
  });

  it('HEAD returns validator headers and no body', async () => {
    const env = buildTempApp();
    try {
      await injectStep(env.app, { method: 'PUT', path: '/resources/h', body: { v: 1 }, headers: {} });
      const res = await env.app.inject({ method: 'HEAD', url: '/resources/h' });
      expect(res.statusCode).toBe(200);
      expect(res.headers.etag).toBeDefined();
      expect(res.headers['x-resource-version']).toBe('1');
      expect(res.body).toBe('');
    } finally {
      env.cleanup();
    }
  });

  it('GET ?weak=1 emits a W/ validator that still 304s a weak conditional GET', async () => {
    const env = buildTempApp();
    try {
      await injectStep(env.app, { method: 'PUT', path: '/resources/w', body: { v: 1 }, headers: {} });
      const weak = await env.app.inject({ method: 'GET', url: '/resources/w?weak=1' });
      expect(weak.headers.etag).toMatch(/^W\//);

      const cond = await env.app.inject({
        method: 'GET', url: '/resources/w',
        headers: { 'If-None-Match': weak.headers.etag as string }
      });
      expect(cond.statusCode).toBe(304);
    } finally {
      env.cleanup();
    }
  });

  it('exposes full version history and one specific version, with explicit 404s', async () => {
    const env = buildTempApp();
    try {
      await injectStep(env.app, { method: 'PUT', path: '/resources/hist', body: { v: 1 }, headers: {} });
      await injectStep(env.app, { method: 'PUT', path: '/resources/hist', body: { v: 2 }, headers: {} });
      await injectStep(env.app, { method: 'DELETE', path: '/resources/hist', headers: {} });

      const list = await env.app.inject({ method: 'GET', url: '/resources/hist/versions' });
      expect(list.statusCode).toBe(200);
      const parsed = JSON.parse(list.body) as {
        versions: Array<{ version: number; deleted: boolean }>;
      };
      expect(parsed.versions.map((x) => [x.version, x.deleted])).toEqual([
        [1, false], [2, false], [3, true]
      ]);

      const one = await env.app.inject({ method: 'GET', url: '/resources/hist/versions/2' });
      expect(one.statusCode).toBe(200);
      expect(JSON.parse(one.body).body).toEqual({ v: 2 });

      const missingVersion = await env.app.inject({ method: 'GET', url: '/resources/hist/versions/99' });
      expect(missingVersion.statusCode).toBe(404);

      const badVersion = await env.app.inject({ method: 'GET', url: '/resources/hist/versions/abc' });
      expect(badVersion.statusCode).toBe(400);
      expect(JSON.parse(badVersion.body).error.code).toBe('MALFORMED_CONDITION_HEADER');

      const noHistory = await env.app.inject({ method: 'GET', url: '/resources/nope/versions' });
      expect(noHistory.statusCode).toBe(404);
    } finally {
      env.cleanup();
    }
  });

  it('collection paging clamps invalid limit/offset to sane bounds', async () => {
    const env = buildTempApp();
    try {
      for (const id of ['a', 'b', 'c']) {
        await injectStep(env.app, { method: 'PUT', path: `/resources/${id}`, body: { id }, headers: {} });
      }
      const res = await env.app.inject({ method: 'GET', url: '/resources?limit=notanint&offset=-5' });
      expect(res.statusCode).toBe(200);
      const parsed = JSON.parse(res.body) as { items: unknown[] };
      expect(parsed.items).toHaveLength(3);
    } finally {
      env.cleanup();
    }
  });

  it('rejects an over-sized body with a 4xx, not a success', async () => {
    const env = buildTempApp();
    try {
      const res = await env.app.inject({
        method: 'PUT', url: '/resources/big',
        headers: { 'content-type': 'application/json' },
        payload: JSON.stringify({ pad: 'x'.repeat(1_100_000) })
      });
      expect(res.statusCode).toBeGreaterThanOrEqual(400);
      expect(res.statusCode).toBeLessThan(500);
    } finally {
      env.cleanup();
    }
  });

  it('DELETE is 204 with no body and the resource then reads 404', async () => {
    const env = buildTempApp();
    try {
      await injectStep(env.app, { method: 'PUT', path: '/resources/d', body: { v: 1 }, headers: {} });
      const del = await env.app.inject({ method: 'DELETE', url: '/resources/d' });
      expect(del.statusCode).toBe(204);
      expect(del.body).toBe('');
      const gone = await env.app.inject({ method: 'GET', url: '/resources/d' });
      expect(gone.statusCode).toBe(404);
    } finally {
      env.cleanup();
    }
  });

  it('rejects resource ids with unsupported characters', async () => {
    const env = buildTempApp();
    try {
      const res = await env.app.inject({
        method: 'PUT', url: '/resources/bad%20id',
        headers: { 'content-type': 'application/json' },
        payload: '{}'
      });
      expect([400, 404]).toContain(res.statusCode);
    } finally {
      env.cleanup();
    }
  });

  it('date validators over HTTP: If-Modified-Since 304/200 and If-Unmodified-Since 412', async () => {
    const env = buildTempApp();
    try {
      await injectStep(env.app, { method: 'PUT', path: '/resources/dt', body: { v: 1 }, headers: {} });
      const created = await env.app.inject({ method: 'GET', url: '/resources/dt' });
      const lastModified = created.headers['last-modified'] as string;
      const future = new Date(Date.now() + 86_400_000).toUTCString();
      const ancient = 'Wed, 01 Jan 2020 00:00:00 GMT';

      const unchanged = await env.app.inject({
        method: 'GET', url: '/resources/dt', headers: { 'If-Modified-Since': future }
      });
      expect(unchanged.statusCode).toBe(304);

      const modified = await env.app.inject({
        method: 'GET', url: '/resources/dt', headers: { 'If-Modified-Since': ancient }
      });
      expect(modified.statusCode).toBe(200);

      const blockedWrite = await env.app.inject({
        method: 'PUT', url: '/resources/dt',
        headers: { 'content-type': 'application/json', 'If-Unmodified-Since': ancient },
        payload: JSON.stringify({ v: 2 })
      });
      expect(blockedWrite.statusCode).toBe(412);
      expect(JSON.parse(blockedWrite.body).error.code).toBe(
        'PRECONDITION_IF_UNMODIFIED_SINCE_FAILED'
      );

      const allowedWrite = await env.app.inject({
        method: 'PUT', url: '/resources/dt',
        headers: { 'content-type': 'application/json', 'If-Unmodified-Since': future },
        payload: JSON.stringify({ v: 2 })
      });
      expect(allowedWrite.statusCode).toBe(200);

      // A malformed If-Modified-Since is ignored, so the GET succeeds.
      const badDateGet = await env.app.inject({
        method: 'GET', url: '/resources/dt', headers: { 'If-Modified-Since': 'not-a-date' }
      });
      expect(badDateGet.statusCode).toBe(200);

      // Last-Modified sanity for the test clock assumption.
      expect(lastModified).toMatch(/GMT$/);
    } finally {
      env.cleanup();
    }
  });

  it('an empty body is a 400 for both JSON and merge-patch content types', async () => {
    const env = buildTempApp();
    try {
      await injectStep(env.app, { method: 'PUT', path: '/resources/e', body: { v: 1 }, headers: {} });
      const emptyPatch = await env.app.inject({
        method: 'PATCH', url: '/resources/e',
        headers: { 'content-type': 'application/merge-patch+json' },
        payload: ''
      });
      expect(emptyPatch.statusCode).toBe(400);

      const emptyPut = await env.app.inject({
        method: 'PUT', url: '/resources/e2',
        headers: { 'content-type': 'application/json' },
        payload: ''
      });
      expect(emptyPut.statusCode).toBe(400);
      expect(JSON.parse(emptyPut.body).error.code).toBe('INVALID_JSON_BODY');

      // The literal JSON null is a valid representation.
      const nullPut = await env.app.inject({
        method: 'PUT', url: '/resources/e3',
        headers: { 'content-type': 'application/json' },
        payload: 'null'
      });
      expect(nullPut.statusCode).toBe(201);
      expect(JSON.parse(nullPut.body).body).toBeNull();
    } finally {
      env.cleanup();
    }
  });

  it('PATCH against an absent resource is 404 RESOURCE_NOT_FOUND', async () => {
    const env = buildTempApp();
    try {
      const res = await env.app.inject({
        method: 'PATCH', url: '/resources/ghost',
        headers: { 'content-type': 'application/json' },
        payload: '{"v":1}'
      });
      expect(res.statusCode).toBe(404);
      expect(JSON.parse(res.body).error.code).toBe('RESOURCE_NOT_FOUND');
    } finally {
      env.cleanup();
    }
  });
});
