import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  CompositeSink,
  MemoryRingSink,
  redactPreview,
  type DiagnosticRecord,
} from '../../src/diagnostics/logger.js';

function record(partial: Partial<DiagnosticRecord> = {}): DiagnosticRecord {
  return {
    ts: '2026-01-01T00:00:00.000Z',
    requestId: 'r1',
    method: 'GET',
    url: '/objects/x',
    objectId: 'x',
    objectSize: 10,
    decision: 'accepted:single',
    status: 206,
    code: 'OK',
    reason: 'test',
    rangeHeader: null,
    specCount: 1,
    intervals: [],
    requestedBytes: 1,
    servedBytes: 1,
    mergeCount: 0,
    dropped: [],
    ...partial,
  };
}

test('redactPreview masks every alphanumeric byte but keeps punctuation shape', () => {
  const masked = redactPreview(Buffer.from('SECRET-token-xyz123!', 'utf8'));
  assert.equal(masked, '######-#####-######!');
});

test('redactPreview maps binary octets to a marker, never the raw value', () => {
  const masked = redactPreview(Buffer.from([0x00, 0xff, 0x41, 0x2e]));
  assert.equal(masked, '××#.');
});

test('redactPreview truncates with an explicit total-size suffix', () => {
  const masked = redactPreview(Buffer.alloc(500, 0x41), 10);
  assert.equal(masked, `${'#'.repeat(10)}…(500 bytes total)`);
});

test('MemoryRingSink returns newest first and applies the limit', () => {
  const ring = new MemoryRingSink(100);
  for (let i = 0; i < 5; i++) {
    ring.record(record({ requestId: `r${i}` }));
  }
  const rows = ring.query({ limit: 3 });
  assert.deepEqual(rows.map((r) => r.requestId), ['r4', 'r3', 'r2']);
});

test('MemoryRingSink filters by objectId and decision', () => {
  const ring = new MemoryRingSink(100);
  ring.record(record({ requestId: 'a', objectId: 'o1', decision: 'accepted:single' }));
  ring.record(record({ requestId: 'b', objectId: 'o2', decision: 'rejected', status: 416, code: 'X' }));
  ring.record(record({ requestId: 'c', objectId: 'o1', decision: 'rejected', status: 416, code: 'Y' }));

  const o1 = ring.query({ objectId: 'o1' });
  assert.deepEqual(o1.map((r) => r.requestId), ['c', 'a']);

  const rejectedO1 = ring.query({ objectId: 'o1', decision: 'rejected' });
  assert.deepEqual(rejectedO1.map((r) => r.requestId), ['c']);
});

test('MemoryRingSink evicts oldest entries past capacity', () => {
  const ring = new MemoryRingSink(2);
  ring.record(record({ requestId: 'a' }));
  ring.record(record({ requestId: 'b' }));
  ring.record(record({ requestId: 'c' }));
  assert.deepEqual(ring.query().map((r) => r.requestId), ['c', 'b']);
});

test('CompositeSink fans out and isolates a throwing sink', () => {
  const ok = new MemoryRingSink(10);
  const throwing = { record: () => { throw new Error('disk on fire'); } };
  const composite = new CompositeSink([throwing, ok]);
  assert.doesNotThrow(() => composite.record(record()));
  assert.equal(ok.query().length, 1);
});

test('CompositeSink caps an overlong Range header before fanning out', () => {
  const ring = new MemoryRingSink(10);
  const composite = new CompositeSink([ring]);
  const longHeader = `bytes=${'0-0,'.repeat(200)}0-0`;
  composite.record(record({ rangeHeader: longHeader }));
  const stored = ring.query()[0]!.rangeHeader!;
  assert.ok(stored.length < longHeader.length);
  assert.match(stored, /chars\)$/);
});
