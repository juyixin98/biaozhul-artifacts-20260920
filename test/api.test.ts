/**
 * End-to-end HTTP tests against the real Fastify app + real SQLite store
 * (in-memory), driven by app.inject (no listening socket).
 *
 * These assert concrete status codes, bodies, versions and failure
 * categories, plus log correlation: every emitted log record for a request
 * carries the same requestId and includes phase / category information.
 */

import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import type { FastifyInstance } from 'fastify';

import { buildServer } from '../src/server.ts';
import { MemorySink } from '../src/logger.ts';
import type { DocumentStore } from '../src/store.ts';

interface Harness {
  app: FastifyInstance;
  store: DocumentStore;
  sink: MemorySink;
  shutdown: () => Promise<void>;
}

async function setup(): Promise<Harness> {
  const sink = new MemorySink();
  const { JsonLogger } = await import('../src/logger.ts');
  const logger = new JsonLogger(sink, 'debug');
  const built = await buildServer({ databasePath: ':memory:', logger });
  return { app: built.app, store: built.store, sink, shutdown: built.shutdown };
}

describe('HTTP API: document lifecycle and patching', () => {
  let h: Harness;

  before(async () => {
    h = await setup();
  });
  after(async () => {
    await h.shutdown();
  });

  it('creates a document at version 0', async () => {
    const res = await h.app.inject({
      method: 'POST',
      url: '/documents',
      payload: { id: 'd1', document: { list: [1, 2], n: 0 } },
      headers: { 'x-request-id': 'req-create' },
    });
    assert.equal(res.statusCode, 201);
    const body = res.json();
    assert.equal(body.requestId, 'req-create');
    assert.equal(body.version, 0);
    assert.deepEqual(body.document, { list: [1, 2], n: 0 });
  });

  it('applies a multi-op patch bound to version 0 and returns per-step states', async () => {
    const res = await h.app.inject({
      method: 'POST',
      url: '/documents/d1/patch',
      headers: { 'x-request-id': 'req-apply' },
      payload: {
        expectedVersion: 0,
        patch: [
          { op: 'add', path: '/list/-', value: 3 },
          { op: 'test', path: '/list/2', value: 3 },
          { op: 'move', from: '/list/0', path: '/list/2' },
        ],
      },
    });
    assert.equal(res.statusCode, 200, res.body);
    const body = res.json();
    assert.equal(body.baseVersion, 0);
    assert.equal(body.newVersion, 1);
    // move /0 -> /2 (post-removal position), concrete expected result:
    assert.deepEqual(body.result, { list: [2, 3, 1], n: 0 });
    assert.equal(body.steps.length, 3);
    // Intermediate documented states are concrete and distinct.
    assert.deepEqual(body.steps[0].resultAfter, { list: [1, 2, 3], n: 0 });
    assert.deepEqual(body.steps[2].resultAfter, { list: [2, 3, 1], n: 0 });
  });

  it('rejects a mid-patch test failure with 422, typed category and full rollback', async () => {
    const before = await h.app.inject({ method: 'GET', url: '/documents/d1' });
    assert.equal(before.json().version, 1);

    const res = await h.app.inject({
      method: 'POST',
      url: '/documents/d1/patch',
      headers: { 'x-request-id': 'req-fail' },
      payload: {
        expectedVersion: 1,
        patch: [
          { op: 'add', path: '/list/-', value: 99 },
          { op: 'replace', path: '/n', value: 42 },
          { op: 'test', path: '/n', value: 0 }, // fails: n is 42
        ],
      },
    });
    assert.equal(res.statusCode, 422);
    const body = res.json();
    assert.equal(body.success, false);
    assert.equal(body.requestId, 'req-fail');
    assert.equal(body.error.category, 'TEST_FAILED');
    assert.equal(body.error.failedAtIndex, 2);
    assert.equal(body.error.rolledBack, true);
    assert.equal(body.error.appliedBeforeFailure.length, 2);
    assert.equal(body.error.appliedBeforeFailure[0].op, 'add');

    const after = await h.app.inject({ method: 'GET', url: '/documents/d1' });
    assert.equal(after.json().version, 1, 'version must not advance on failure');
    assert.deepEqual(after.json().document, { list: [2, 3, 1], n: 0 });
  });

  it('rejects a stale version binding with 409 VERSION_CONFLICT', async () => {
    const res = await h.app.inject({
      method: 'POST',
      url: '/documents/d1/patch',
      headers: { 'x-request-id': 'req-conflict' },
      payload: { expectedVersion: 0, patch: [{ op: 'replace', path: '/n', value: 1 }] },
    });
    assert.equal(res.statusCode, 409);
    const body = res.json();
    assert.equal(body.error.category, 'VERSION_CONFLICT');
    assert.equal(body.error.expectedVersion, 0);
    assert.equal(body.error.actualVersion, 1);
  });

  it('returns 400 for contract errors (bad op, malformed pointer, move-into-self)', async () => {
    const cases = [
      { patch: [{ op: 'frobnicate', path: '/a' }], category: 'UNKNOWN_OP' },
      { patch: [{ op: 'remove', path: 'noslash' }], category: 'MALFORMED_POINTER' },
      { patch: [{ op: 'move', from: '/list', path: '/list/x' }], category: 'MOVE_INTO_SELF' },
      { patch: [], category: 'EMPTY_PATCH' },
      { patch: 'not-an-array', category: 'PATCH_NOT_ARRAY' },
    ];
    for (const [i, entry] of cases.entries()) {
      const res = await h.app.inject({
        method: 'POST',
        url: '/documents/d1/patch',
        headers: { 'x-request-id': `req-contract-${i}` },
        payload: { expectedVersion: 1, patch: entry.patch },
      });
      assert.equal(res.statusCode, 400, `case ${i}: ${res.body}`);
      assert.equal(res.json().error.category, entry.category, `case ${i}`);
    }
  });

  it('requires a non-negative integer expectedVersion', async () => {
    const res = await h.app.inject({
      method: 'POST',
      url: '/documents/d1/patch',
      payload: { expectedVersion: '1', patch: [{ op: 'remove', path: '/n' }] },
    });
    assert.equal(res.statusCode, 400);
    assert.equal(res.json().error.category, 'MISSING_FIELD');
  });

  it('returns 404 for an unknown document', async () => {
    const res = await h.app.inject({
      method: 'POST',
      url: '/documents/ghost/patch',
      payload: { expectedVersion: 0, patch: [{ op: 'remove', path: '/a' }] },
    });
    assert.equal(res.statusCode, 404);
    assert.equal(res.json().error.category, 'DOCUMENT_NOT_FOUND');
  });

  it('handles empty key and escaped slash/tilde keys over HTTP', async () => {
    await h.app.inject({
      method: 'POST',
      url: '/documents',
      payload: { id: 'esc', document: { 'a/b': { 'a~b': [1] }, '': 0 } },
    });
    const res = await h.app.inject({
      method: 'POST',
      url: '/documents/esc/patch',
      headers: { 'x-request-id': 'req-esc' },
      payload: {
        expectedVersion: 0,
        patch: [
          { op: 'test', path: '/a~1b/a~0b/0', value: 1 },
          { op: 'replace', path: '/', value: 'seen' },
        ],
      },
    });
    assert.equal(res.statusCode, 200, res.body);
    assert.deepEqual(res.json().result, { 'a/b': { 'a~b': [1] }, '': 'seen' });
  });
});

describe('HTTP API: diagnostics and log correlation', () => {
  let h: Harness;

  before(async () => {
    h = await setup();
  });
  after(async () => {
    await h.shutdown();
  });

  it('exposes per-document events and lookup by request id', async () => {
    await h.app.inject({
      method: 'POST',
      url: '/documents',
      payload: { id: 'diag', document: { n: 1 } },
    });
    await h.app.inject({
      method: 'POST',
      url: '/documents/diag/patch',
      headers: { 'x-request-id': 'diag-ok' },
      payload: { expectedVersion: 0, patch: [{ op: 'replace', path: '/n', value: 2 }] },
    });
    await h.app.inject({
      method: 'POST',
      url: '/documents/diag/patch',
      headers: { 'x-request-id': 'diag-bad' },
      payload: { expectedVersion: 1, patch: [{ op: 'test', path: '/n', value: 2 }, { op: 'remove', path: '/nope' }] },
    });

    const byRequest = await h.app.inject({ method: 'GET', url: '/events/diag-bad' });
    assert.equal(byRequest.statusCode, 200);
    assert.equal(byRequest.json().event.status, 'rejected');
    assert.equal(byRequest.json().event.category, 'POINTER_ERROR');
    assert.equal(byRequest.json().event.failedAtIndex, 1);

    const events = await h.app.inject({ method: 'GET', url: '/documents/diag/events' });
    assert.equal(events.statusCode, 200);
    const ids = events.json().events.map((e: { requestId: string }) => e.requestId);
    assert.deepEqual(ids.sort(), ['diag-bad', 'diag-ok']);
  });

  it('correlates every structured log line to the request id with phase and category', async () => {
    await h.app.inject({
      method: 'POST',
      url: '/documents',
      payload: { id: 'logs', document: { n: 1 } },
    });
    await h.app.inject({
      method: 'POST',
      url: '/documents/logs/patch',
      headers: { 'x-request-id': 'corr-fail' },
      payload: { expectedVersion: 0, patch: [{ op: 'test', path: '/n', value: 999 }] },
    });

    const records = h.sink.forRequest('corr-fail');
    assert.ok(records.length >= 2, 'expected received + rejected log records');
    for (const record of records) {
      assert.equal(record.requestId, 'corr-fail');
      assert.ok(record.documentId === 'logs');
    }
    const rejected = records.find((r) => r.msg === 'patch.rejected');
    assert.ok(rejected, 'a patch.rejected record must exist');
    assert.equal(rejected.category, 'TEST_FAILED');
    assert.equal(rejected.outcome, 'failure');
    assert.equal(rejected.certainty, 'certain');
    assert.equal(rejected.failedAtIndex, 0);

    // A success request records the version transition.
    await h.app.inject({
      method: 'POST',
      url: '/documents/logs/patch',
      headers: { 'x-request-id': 'corr-ok' },
      payload: { expectedVersion: 0, patch: [{ op: 'replace', path: '/n', value: 2 }] },
    });
    const okRecords = h.sink.forRequest('corr-ok');
    const applied = okRecords.find((r) => r.msg === 'patch.applied');
    assert.equal(applied.baseVersion, 0);
    assert.equal(applied.newVersion, 1);
    assert.equal(applied.phase, 'store');
  });

  it('health endpoint responds', async () => {
    const res = await h.app.inject({ method: 'GET', url: '/health' });
    assert.equal(res.statusCode, 200);
    assert.deepEqual(res.json(), { status: 'ok' });
  });

  it('maps a malformed JSON body to 400 MALFORMED_JSON', async () => {
    const res = await h.app.inject({
      method: 'POST',
      url: '/documents',
      headers: { 'content-type': 'application/json', 'x-request-id': 'bad-json' },
      payload: '{ not valid json',
    });
    assert.equal(res.statusCode, 400);
    assert.equal(res.json().error.category, 'MALFORMED_JSON');
  });

  it('returns 404 EVENT_NOT_FOUND for an unknown request id', async () => {
    const res = await h.app.inject({ method: 'GET', url: '/events/no-such-request' });
    assert.equal(res.statusCode, 404);
    assert.equal(res.json().error.category, 'EVENT_NOT_FOUND');
  });
});
