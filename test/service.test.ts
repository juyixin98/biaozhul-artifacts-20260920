/**
 * Integration tests for the service orchestration + SQLite state adapter:
 * version binding, atomic rollback at the persistence boundary, audit trail,
 * and concurrent version conflict behaviour.
 *
 * Uses a fresh in-memory SQLite database and a silent logger per test.
 */

import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { createLogger, type Logger } from '../src/logger';
import { PatchService } from '../src/service';
import { DocumentStore, openStore } from '../src/store';

function silentLogger(): Logger {
  return { log: () => undefined, child() { return this; } } as unknown as Logger;
}

function makeService(): { service: PatchService; store: DocumentStore } {
  const dir = mkdtempSync(join(tmpdir(), 'rfc6902-it-'));
  const { store } = openStore(join(dir, 'test.sqlite'), false);
  const service = new PatchService(store, silentLogger());
  return { service, store };
}

describe('service — happy path and version progression', () => {
  let service: PatchService;
  let store: DocumentStore;
  beforeEach(() => {
    ({ service, store } = makeService());
  });
  afterEach(() => store.close());

  it('creates a document at version 0 and increments per successful patch', () => {
    service.createDocument('d1', { a: 1, tags: ['x'] });

    const r1 = service.apply({
      docId: 'd1',
      ops: [{ op: 'add', path: '/tags/-', value: 'y' }],
      expectedVersion: 0,
    });
    expect(r1.ok).toBe(true);
    expect(r1.fromVersion).toBe(0);
    expect(r1.toVersion).toBe(1);
    expect(r1.result).toEqual({ a: 1, tags: ['x', 'y'] });

    const stored = service.getDocument('d1')!;
    expect(stored.version).toBe(1);
    expect(stored.doc).toEqual({ a: 1, tags: ['x', 'y'] });

    const r2 = service.apply({
      docId: 'd1',
      ops: [{ op: 'remove', path: '/a' }],
      expectedVersion: 1,
    });
    expect(r2.ok).toBe(true);
    expect(r2.toVersion).toBe(2);
  });

  it('does not require expectedVersion (unbound patch still applies)', () => {
    service.createDocument('d2', { n: 0 });
    const r = service.apply({ docId: 'd2', ops: [{ op: 'replace', path: '/n', value: 1 }], expectedVersion: null });
    expect(r.ok).toBe(true);
    expect(r.toVersion).toBe(1);
  });

  it('treats an empty patch as a successful no-op that does not change the version', () => {
    service.createDocument('d3', { n: 1 });
    const r = service.apply({ docId: 'd3', ops: [], expectedVersion: 0 });
    expect(r.ok).toBe(true);
    expect(r.applied).toBe(0);
    expect(r.fromVersion).toBe(0);
    expect(r.toVersion).toBe(0);
    expect(service.getDocument('d3')!.version).toBe(0);
  });
});

describe('service — version binding and conflicts', () => {
  let service: PatchService;
  let store: DocumentStore;
  beforeEach(() => {
    ({ service, store } = makeService());
  });
  afterEach(() => store.close());

  it('rejects a stale expectedVersion with VERSION_CONFLICT and does NOT execute', () => {
    service.createDocument('d', { n: 1 });
    const conflict = service.apply({
      docId: 'd',
      ops: [{ op: 'replace', path: '/n', value: 999 }],
      expectedVersion: 5,
    });
    expect(conflict.ok).toBe(false);
    expect(conflict.failure?.category).toBe('VERSION_CONFLICT');
    expect(conflict.failure?.details).toMatchObject({ expectedVersion: 5, actualVersion: 0 });
    expect(conflict.steps).toEqual([]);
    // Document untouched, version unchanged.
    expect(service.getDocument('d')!.version).toBe(0);
    expect(service.getDocument('d')!.doc).toEqual({ n: 1 });
  });

  it('returns DOCUMENT_NOT_FOUND for an unknown id', () => {
    const r = service.apply({
      docId: 'missing',
      ops: [{ op: 'test', path: '', value: null }],
      expectedVersion: 0,
    });
    expect(r.ok).toBe(false);
    expect(r.failure?.category).toBe('DOCUMENT_NOT_FOUND');
  });

  it('rejects negative / non-integer expectedVersion', () => {
    service.createDocument('d', {});
    const r = service.apply({
      docId: 'd',
      ops: [],
      expectedVersion: -1,
    });
    expect(r.ok).toBe(false);
    expect(r.failure?.category).toBe('BAD_REQUEST_BODY');
  });
});

describe('service — atomic rollback at the persistence boundary', () => {
  let service: PatchService;
  let store: DocumentStore;
  beforeEach(() => {
    ({ service, store } = makeService());
  });
  afterEach(() => store.close());

  it('a mid-patch test failure leaves the stored document byte-identical', () => {
    const start = { title: 'orig', tags: ['a'], meta: { v: 0 } };
    service.createDocument('roll', start);

    const r = service.apply({
      docId: 'roll',
      ops: [
        { op: 'add', path: '/tags/-', value: 'b' },
        { op: 'replace', path: '/title', value: 'changed' },
        { op: 'test', path: '/meta/v', value: 42 },
        { op: 'add', path: '/never', value: true },
      ],
      expectedVersion: 0,
    });

    expect(r.ok).toBe(false);
    expect(r.failure?.category).toBe('TEST_FAILURE');
    expect(r.failure?.failedAtIndex).toBe(2);

    const stored = service.getDocument('roll')!;
    expect(stored.version).toBe(0); // version never advanced
    expect(stored.doc).toEqual(start);

    // Per-step trace shows earlier ops applied in-kernel, then failure + skipped.
    expect(r.steps.map((s) => s.status)).toEqual(['applied', 'applied', 'failed', 'skipped']);
    const docAtFailure = r.steps[2]!.documentAt as { title: string; tags: string[] };
    expect(docAtFailure.title).toBe('changed');
    expect(docAtFailure.tags).toEqual(['a', 'b']);
  });

  it('records BOTH failed and successful attempts in the audit table', () => {
    service.createDocument('aud', { n: 1 });

    const failed = service.apply({
      docId: 'aud',
      ops: [{ op: 'test', path: '/n', value: 2 }],
      expectedVersion: 0,
    });
    const succeeded = service.apply({
      docId: 'aud',
      ops: [{ op: 'replace', path: '/n', value: 3 }],
      expectedVersion: 0,
    });

    expect(failed.ok).toBe(false);
    expect(succeeded.ok).toBe(true);

    const failedAudit = store.getAudit(failed.requestId)!;
    expect(failedAudit.ok).toBe(false);
    expect(failedAudit.category).toBe('TEST_FAILURE');
    expect(failedAudit.failedAtIndex).toBe(0);
    expect(failedAudit.toVersion).toBeNull();
    expect((failedAudit.traces as unknown[]).length).toBeGreaterThan(0);

    const okAudit = store.getAudit(succeeded.requestId)!;
    expect(okAudit.ok).toBe(true);
    expect(okAudit.fromVersion).toBe(0);
    expect(okAudit.toVersion).toBe(1);

    const history = store.historyForDocument('aud', 10);
    expect(history.map((h) => h.requestId)).toEqual([succeeded.requestId, failed.requestId]);
  });
});

describe('service — concurrency: only one version-bound writer wins', () => {
  let service: PatchService;
  let store: DocumentStore;
  beforeEach(() => {
    ({ service, store } = makeService());
  });
  afterEach(() => store.close());

  it('two patches racing on expectedVersion=0: one commits, one VERSION_CONFLICT', () => {
    service.createDocument('race', { counter: 0 });

    const results = [1, 2].map((n) =>
      service.apply({
        docId: 'race',
        ops: [{ op: 'replace', path: '/counter', value: n }],
        expectedVersion: 0,
      }),
    );

    const oks = results.filter((r) => r.ok);
    const conflicts = results.filter((r) => r.failure?.category === 'VERSION_CONFLICT');
    expect(oks).toHaveLength(1);
    expect(conflicts).toHaveLength(1);

    const final = service.getDocument('race')!;
    expect(final.version).toBe(1);
    expect([1, 2]).toContain((final.doc as { counter: number }).counter);
  });
});
