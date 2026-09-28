import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';

import { parsePatch } from '../src/contract.ts';
import { DocumentStore, StoreError } from '../src/store.ts';

function ops(patch: unknown[]) {
  return parsePatch(patch).operations;
}

describe('DocumentStore: lifecycle and versioning', () => {
  let store: DocumentStore;

  before(() => {
    store = new DocumentStore(':memory:');
  });
  after(() => store.close());

  it('creates a document at version 0 and reads it back', () => {
    const record = store.createDocument('doc-1', { a: 1, list: [1] });
    assert.equal(record.version, 0);
    assert.deepEqual(record.document, { a: 1, list: [1] });

    const fetched = store.getDocument('doc-1');
    assert.equal(fetched.version, 0);
    assert.deepEqual(fetched.document, { a: 1, list: [1] });
  });

  it('rejects duplicate create with DOCUMENT_ALREADY_EXISTS', () => {
    assert.throws(
      () => store.createDocument('doc-1', {}),
      (e: Error) => e instanceof StoreError && e.category === 'DOCUMENT_ALREADY_EXISTS',
    );
  });

  it('applies a patch bound to the current version and increments by exactly 1', () => {
    const result = store.applyPatch(
      'doc-1',
      0,
      ops([{ op: 'add', path: '/list/-', value: 2 }]),
      'req-apply-1',
    );
    assert.equal(result.baseVersion, 0);
    assert.equal(result.newVersion, 1);
    assert.deepEqual(result.document, { a: 1, list: [1, 2] });
    assert.equal(store.getDocument('doc-1').version, 1);
  });

  it('rejects a patch bound to a stale version (409 semantics)', () => {
    assert.throws(
      () =>
        store.applyPatch(
          'doc-1',
          0, // stale: document is already at 1
          ops([{ op: 'replace', path: '/a', value: 2 }]),
          'req-stale',
        ),
      (e: Error) => {
        assert.ok(e instanceof StoreError);
        assert.equal(e.category, 'VERSION_CONFLICT');
        assert.equal(e.details?.expectedVersion, 0);
        assert.equal(e.details?.actualVersion, 1);
        return true;
      },
    );
  });

  it('reports unknown document as DOCUMENT_NOT_FOUND', () => {
    assert.throws(
      () => store.applyPatch('nope', 0, ops([{ op: 'remove', path: '/a' }]), 'req-404'),
      (e: Error) => e instanceof StoreError && e.category === 'DOCUMENT_NOT_FOUND',
    );
  });
});

describe('DocumentStore: atomic rollback on kernel failure', () => {
  let store: DocumentStore;

  before(() => {
    store = new DocumentStore(':memory:');
  });
  after(() => store.close());

  it('keeps the stored document and version untouched after a mid-patch test failure', () => {
    store.createDocument('atomic', { list: [1, 2], n: 0 });

    assert.throws(
      () =>
        store.applyPatch(
          'atomic',
          0,
          ops([
            { op: 'add', path: '/list/-', value: 3 },
            { op: 'replace', path: '/n', value: 5 },
            { op: 'test', path: '/n', value: 999 }, // fails
          ]),
          'req-atomic-rollback',
        ),
      (e: Error) => {
        assert.ok(e instanceof StoreError);
        assert.equal(e.category, 'KERNEL_FAILURE');
        const kernel = e.details!.kernel!;
        assert.equal(kernel.category, 'TEST_FAILED');
        assert.equal(kernel.failedAtIndex, 2);
        assert.equal(kernel.appliedBeforeFailure.length, 2);
        assert.equal(kernel.rolledBack, true);
        return true;
      },
    );

    const after = store.getDocument('atomic');
    assert.equal(after.version, 0);
    assert.deepEqual(after.document, { list: [1, 2], n: 0 });
  });

  it('still allows a later patch after a rolled-back failure (version reusable)', () => {
    const result = store.applyPatch(
      'atomic',
      0,
      ops([{ op: 'replace', path: '/n', value: 1 }]),
      'req-retry',
    );
    assert.equal(result.newVersion, 1);
    assert.deepEqual(result.document, { list: [1, 2], n: 1 });
  });
});

describe('DocumentStore: audit events', () => {
  let store: DocumentStore;

  before(() => {
    store = new DocumentStore(':memory:');
  });
  after(() => store.close());

  it('records applied, rejected and conflict events with versions and step counts', () => {
    store.createDocument('audited', { a: 1 });
    store.applyPatch('audited', 0, ops([{ op: 'replace', path: '/a', value: 2 }]), 'evt-ok');

    assert.throws(() =>
      store.applyPatch('audited', 1, ops([{ op: 'test', path: '/a', value: 2 }, { op: 'remove', path: '/missing' }]), 'evt-reject'),
    );
    // rejected patch does not move the version: v1 -> v2 still possible.
    store.applyPatch('audited', 1, ops([{ op: 'replace', path: '/a', value: 3 }]), 'evt-ok-2');
    // Now a client still bound to v1 must conflict (actual is 2).
    store.recordConflict('evt-conflict', 'audited', 1, 2, 1);

    const applied = store.getEvent('evt-ok')!;
    assert.equal(applied.status, 'applied');
    assert.equal(applied.baseVersion, 0);
    assert.equal(applied.newVersion, 1);
    assert.equal(applied.stepsAppliedBeforeOutcome, 1);

    const rejected = store.getEvent('evt-reject')!;
    assert.equal(rejected.status, 'rejected');
    assert.equal(rejected.category, 'POINTER_ERROR');
    assert.equal(rejected.failedAtIndex, 1);
    assert.equal(rejected.stepsAppliedBeforeOutcome, 1);
    assert.equal(rejected.newVersion, null);

    const conflict = store.getEvent('evt-conflict')!;
    assert.equal(conflict.status, 'conflict');
    assert.equal(conflict.category, 'VERSION_CONFLICT');

    const list = store.listEvents('audited');
    const statuses = list.map((e) => e.requestId);
    for (const id of ['evt-ok', 'evt-reject', 'evt-conflict', 'evt-ok-2']) {
      assert.ok(statuses.includes(id));
    }
  });
});
