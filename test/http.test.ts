/**
 * End-to-end HTTP tests through Fastify's in-process inject():
 * envelopes, status-code mapping, request-id correlation, diagnostics lookup,
 * malformed-body handling, and document reads reflecting committed patches.
 */

import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import type { FastifyInstance } from 'fastify';
import { createLogger } from '../src/logger';
import { buildApp } from '../src/server';
import { PatchService } from '../src/service';
import { DocumentStore, openStore } from '../src/store';

interface Harness {
  app: FastifyInstance;
  store: DocumentStore;
  logPath: string;
}

function makeHarness(): Harness {
  const dir = mkdtempSync(join(tmpdir(), 'rfc6902-http-'));
  const logPath = join(dir, 'service.log');
  const { store } = openStore(':memory:', false);
  const logger = createLogger({ filePath: logPath, toStdout: false });
  const service = new PatchService(store, logger);
  const app = buildApp({ service, store, logger });
  return { app, store, logPath };
}

describe('HTTP interface', () => {
  let h: Harness;
  beforeEach(() => {
    h = makeHarness();
  });
  afterEach(async () => {
    await h.app.close();
    h.store.close();
  });

  it('GET /health reports ok', async () => {
    const res = await h.app.inject({ method: 'GET', url: '/health' });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual({ status: 'ok' });
  });

  it('creates, reads and lists documents with the success envelope', async () => {
    const created = await h.app.inject({
      method: 'POST',
      url: '/documents',
      payload: { id: 'doc-1', doc: { a: 1 } },
    });
    expect(created.statusCode).toBe(200);
    const createdBody = created.json();
    expect(createdBody.success).toBe(true);
    expect(createdBody.data).toMatchObject({ id: 'doc-1', version: 0 });
    expect(createdBody.meta.requestId).toBeTypeOf('string');

    const got = await h.app.inject({ method: 'GET', url: '/documents/doc-1' });
    expect(got.json().data.doc).toEqual({ a: 1 });

    const list = await h.app.inject({ method: 'GET', url: '/documents' });
    expect(list.json().data.documents).toEqual([{ id: 'doc-1', version: 0 }]);
  });

  it('rejects a duplicate id and an invalid id', async () => {
    await h.app.inject({ method: 'POST', url: '/documents', payload: { id: 'd', doc: {} } });
    const dup = await h.app.inject({ method: 'POST', url: '/documents', payload: { id: 'd', doc: {} } });
    expect(dup.statusCode).toBe(400);
    expect(dup.json().error.category).toBe('BAD_REQUEST_BODY');

    const badId = await h.app.inject({
      method: 'POST',
      url: '/documents',
      payload: { id: 'bad id!', doc: {} },
    });
    expect(badId.statusCode).toBe(400);
  });

  it('applies a version-bound patch and returns from/to versions plus steps', async () => {
    await h.app.inject({ method: 'POST', url: '/documents', payload: { id: 'p', doc: { tags: ['a'] } } });

    const res = await h.app.inject({
      method: 'POST',
      url: '/documents/p/patch',
      headers: { 'x-request-id': 'req-success-1' },
      payload: {
        expectedVersion: 0,
        patch: [
          { op: 'test', path: '/tags/0', value: 'a' },
          { op: 'add', path: '/tags/-', value: 'b' },
        ],
      },
    });

    expect(res.statusCode).toBe(200);
    const body = res.json();
    expect(body.success).toBe(true);
    expect(body.meta.requestId).toBe('req-success-1');
    expect(body.meta.fromVersion).toBe(0);
    expect(body.meta.toVersion).toBe(1);
    expect(body.data.applied).toBe(2);
    expect(body.data.result.tags).toEqual(['a', 'b']);
    expect(body.data.steps.map((s: { status: string }) => s.status)).toEqual(['applied', 'applied']);
  });

  it('returns 409 TEST_FAILURE mid-patch, keeps document at the old version', async () => {
    await h.app.inject({
      method: 'POST',
      url: '/documents',
      payload: { id: 'f', doc: { title: 'orig', tags: ['a'], v: 0 } },
    });

    const res = await h.app.inject({
      method: 'POST',
      url: '/documents/f/patch',
      headers: { 'x-request-id': 'req-fail-1' },
      payload: {
        expectedVersion: 0,
        patch: [
          { op: 'replace', path: '/title', value: 'changed' },
          { op: 'add', path: '/tags/-', value: 'b' },
          { op: 'test', path: '/v', value: 99 },
        ],
      },
    });

    expect(res.statusCode).toBe(409);
    const body = res.json();
    expect(body.success).toBe(false);
    expect(body.error.category).toBe('TEST_FAILURE');
    expect(body.meta.failedAtIndex).toBe(2);
    expect(body.meta.steps.map((s: { status: string }) => s.status)).toEqual([
      'applied',
      'applied',
      'failed',
    ]);

    const after = await h.app.inject({ method: 'GET', url: '/documents/f' });
    expect(after.json().data).toMatchObject({ version: 0 });
    expect(after.json().data.doc).toEqual({ title: 'orig', tags: ['a'], v: 0 });
  });

  it('returns 409 VERSION_CONFLICT for a stale expectedVersion', async () => {
    await h.app.inject({ method: 'POST', url: '/documents', payload: { id: 'v', doc: { n: 1 } } });

    const res = await h.app.inject({
      method: 'POST',
      url: '/documents/v/patch',
      payload: { expectedVersion: 3, patch: [{ op: 'replace', path: '/n', value: 2 }] },
    });
    expect(res.statusCode).toBe(409);
    expect(res.json().error.category).toBe('VERSION_CONFLICT');
  });

  it('returns 404 for an unknown document and a missing audit record', async () => {
    const doc = await h.app.inject({
      method: 'POST',
      url: '/documents/nope/patch',
      payload: { expectedVersion: 0, patch: [] },
    });
    expect(doc.statusCode).toBe(404);
    expect(doc.json().error.category).toBe('DOCUMENT_NOT_FOUND');

    const audit = await h.app.inject({ method: 'GET', url: '/diagnostics/requests/does-not-exist' });
    expect(audit.statusCode).toBe(404);
    expect(audit.json().error.category).toBe('AUDIT_RECORD_NOT_FOUND');
  });

  it('rejects move-into-descendant with 409 and the from/path evidence', async () => {
    await h.app.inject({
      method: 'POST',
      url: '/documents',
      payload: { id: 'm', doc: { x: { child: {} } } },
    });
    const res = await h.app.inject({
      method: 'POST',
      url: '/documents/m/patch',
      payload: { patch: [{ op: 'move', from: '/x', path: '/x/child' }] },
    });
    expect(res.statusCode).toBe(409);
    expect(res.json().error.category).toBe('MOVE_INTO_DESCENDANT');
    expect(res.json().error.details).toMatchObject({ from: '/x', path: '/x/child' });
  });

  it('correlates a patch to its diagnostics record by the request id', async () => {
    await h.app.inject({ method: 'POST', url: '/documents', payload: { id: 'c', doc: { n: 1 } } });
    await h.app.inject({
      method: 'POST',
      url: '/documents/c/patch',
      headers: { 'x-request-id': 'corr-42' },
      payload: { expectedVersion: 0, patch: [{ op: 'replace', path: '/n', value: 2 }] },
    });

    const diag = await h.app.inject({ method: 'GET', url: '/diagnostics/requests/corr-42' });
    expect(diag.statusCode).toBe(200);
    const record = diag.json().data;
    expect(record.requestId).toBe('corr-42');
    expect(record.docId).toBe('c');
    expect(record.ok).toBe(true);
    expect(record.fromVersion).toBe(0);
    expect(record.toVersion).toBe(1);
    expect(Array.isArray(record.traces)).toBe(true);

    const history = await h.app.inject({ url: '/documents/c/history?limit=5', method: 'GET' });
    expect(history.json().data.records).toHaveLength(1);
    expect(history.json().data.records[0].requestId).toBe('corr-42');
  });

  it('maps malformed JSON body to a 400 envelope', async () => {
    const res = await h.app.inject({
      method: 'POST',
      url: '/documents',
      headers: { 'content-type': 'application/json' },
      payload: '{ not valid json ',
    });
    expect(res.statusCode).toBe(400);
    expect(res.json().error.category).toBe('BAD_REQUEST_BODY');
  });

  it('rejects a patch body that is missing the patch array', async () => {
    await h.app.inject({ method: 'POST', url: '/documents', payload: { id: 'z', doc: {} } });
    const res = await h.app.inject({
      method: 'POST',
      url: '/documents/z/patch',
      payload: { expectedVersion: 0 },
    });
    expect(res.statusCode).toBe(400);
    expect(res.json().error.category).toBe('BAD_REQUEST_BODY');
  });

  it('rejects a reused client request id before executing the second patch', async () => {
    await h.app.inject({ method: 'POST', url: '/documents', payload: { id: 'dup', doc: { n: 1 } } });

    const first = await h.app.inject({
      method: 'POST',
      url: '/documents/dup/patch',
      headers: { 'x-request-id': 'same-id' },
      payload: { expectedVersion: 0, patch: [{ op: 'replace', path: '/n', value: 2 }] },
    });
    expect(first.statusCode).toBe(200);

    // Second attempt with the same id, stale version: id collision is reported
    // (400) rather than VERSION_CONFLICT or a 500 from the audit primary key.
    const second = await h.app.inject({
      method: 'POST',
      url: '/documents/dup/patch',
      headers: { 'x-request-id': 'same-id' },
      payload: { expectedVersion: 1, patch: [{ op: 'replace', path: '/n', value: 3 }] },
    });
    expect(second.statusCode).toBe(400);
    expect(second.json().error.message).toMatch(/already been used/);

    // First patch still committed exactly once.
    const got = await h.app.inject({ method: 'GET', url: '/documents/dup' });
    expect(got.json().data).toMatchObject({ version: 1 });
    expect(got.json().data.doc).toEqual({ n: 2 });
  });
});
