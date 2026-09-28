import assert from 'node:assert/strict';
import { test } from 'node:test';
import { resolveRangeRequest, type CoreInput } from '../../src/range/core.js';
import type { RangeLimits } from '../../src/types.js';
import { meta } from '../fixtures/reference.js';

const LIMITS: RangeLimits = { maxSpecs: 5, maxResponseBytes: 10_000, mergeGap: 1 };

function decide(
  rangeHeader: string | undefined,
  size = 100,
  extra: Partial<Pick<CoreInput, 'ifRangeHeader'>> & { limits?: RangeLimits } = {},
) {
  return resolveRangeRequest({
    rangeHeader,
    ifRangeHeader: extra.ifRangeHeader,
    meta: meta(size),
    limits: extra.limits ?? LIMITS,
  });
}

test('no Range header returns 200 FULL with reason NO_RANGE_HEADER', () => {
  const r = decide(undefined);
  assert.equal(r.decision, 'FULL_REPRESENTATION');
  if (r.decision !== 'FULL_REPRESENTATION') throw new Error('guard');
  assert.equal(r.status, 200);
  assert.equal(r.reason, 'NO_RANGE_HEADER');
});

test('unknown unit is ignored and the full representation is served', () => {
  const r = decide('items=0-9');
  assert.equal(r.decision, 'FULL_REPRESENTATION');
  if (r.decision !== 'FULL_REPRESENTATION') throw new Error('guard');
  assert.equal(r.reason, 'UNSUPPORTED_UNIT_IGNORED');
});

test('single satisfiable spec -> SINGLE_PART 206 with exact interval', () => {
  const r = decide('bytes=10-19');
  assert.equal(r.decision, 'SINGLE_PART');
  if (r.decision !== 'SINGLE_PART') throw new Error('guard');
  assert.equal(r.status, 206);
  assert.deepEqual(r.intervals, [{ start: 10, end: 19 }]);
  assert.equal(r.servedBytes, 10);
  assert.equal(r.mergeCount, 0);
});

test('multiple NON-adjacent specs -> MULTIPART with each interval', () => {
  const r = decide('bytes=0-9,50-59');
  assert.equal(r.decision, 'MULTIPART');
  if (r.decision !== 'MULTIPART') throw new Error('guard');
  assert.deepEqual(r.intervals, [
    { start: 0, end: 9 },
    { start: 50, end: 59 },
  ]);
});

test('adjacent specs merge so MULTIPART collapses to SINGLE_PART', () => {
  const r = decide('bytes=0-9,10-19');
  assert.equal(r.decision, 'SINGLE_PART');
  if (r.decision !== 'SINGLE_PART') throw new Error('guard');
  assert.deepEqual(r.intervals, [{ start: 0, end: 19 }]);
  assert.equal(r.mergeCount, 1);
  // Requested counted before merge; served after.
  assert.equal(r.requestedBytes, 20);
  assert.equal(r.servedBytes, 20);
});

test('overlapping duplicate specs are served once: requested > served', () => {
  const r = decide('bytes=0-50,10-20');
  assert.equal(r.decision, 'SINGLE_PART');
  if (r.decision !== 'SINGLE_PART') throw new Error('guard');
  assert.deepEqual(r.intervals, [{ start: 0, end: 50 }]);
  assert.equal(r.requestedBytes, 62); // 51 + 11 pre-merge
  assert.equal(r.servedBytes, 51);
  assert.equal(r.mergeCount, 1);
});

test('partly valid request is served (206) with the bad spec dropped', () => {
  const r = decide('bytes=0-9,500-600');
  assert.equal(r.status, 206);
  assert.equal(r.decision, 'SINGLE_PART');
  if (r.decision !== 'SINGLE_PART') throw new Error('guard');
  assert.deepEqual(r.dropped, [
    { index: 1, raw: '500-600', reason: 'START_BEYOND_OBJECT' },
  ]);
});

test('all-invalid request is REJECT 416 UNSATISFIABLE_RANGE', () => {
  const r = decide('bytes=500-600,700-');
  assert.equal(r.decision, 'REJECT');
  if (r.decision !== 'REJECT') throw new Error('guard');
  assert.equal(r.status, 416);
  assert.equal(r.code, 'UNSATISFIABLE_RANGE');
  assert.equal(r.dropped.length, 2);
});

test('range on zero-length object is REJECT 416 OBJECT_EMPTY', () => {
  const r = decide('bytes=0-0', 0);
  assert.equal(r.decision, 'REJECT');
  if (r.decision !== 'REJECT') throw new Error('guard');
  assert.equal(r.status, 416);
  assert.equal(r.code, 'OBJECT_EMPTY');
});

test('malformed header is REJECT 400 (distinct from 416)', () => {
  const r = decide('bytes=9-1');
  assert.equal(r.decision, 'REJECT');
  if (r.decision !== 'REJECT') throw new Error('guard');
  assert.equal(r.status, 400);
  assert.equal(r.code, 'MALFORMED_RANGE_HEADER');
  assert.equal(r.specError?.index, 0);
});

test('spec count over the limit is REJECT 400 TOO_MANY_RANGES', () => {
  const header = `bytes=${[0, 1, 2, 3, 4, 5].map((n) => `${n}-${n}`).join(',')}`;
  const r = decide(header);
  assert.equal(r.decision, 'REJECT');
  if (r.decision !== 'REJECT') throw new Error('guard');
  assert.equal(r.status, 400);
  assert.equal(r.code, 'TOO_MANY_RANGES');
});

test('total response cap rejects a single oversized range with 400', () => {
  const r = decide('bytes=0-9999', 100, {
    limits: { maxSpecs: 5, maxResponseBytes: 50, mergeGap: 1 },
  });
  assert.equal(r.decision, 'REJECT');
  if (r.decision !== 'REJECT') throw new Error('guard');
  assert.equal(r.status, 400);
  assert.equal(r.code, 'RESPONSE_SIZE_LIMIT_EXCEEDED');
});

test('multipart framing counts against the response cap', () => {
  // 40 one-byte parts = 40 bytes of payload but hundreds of framing bytes;
  // a 120-byte budget must reject even though payload alone fits.
  const specs = Array.from({ length: 40 }, (_, i) => `${i}-${i}`).join(',');
  const r = decide(`bytes=${specs}`, 1000, {
    limits: { maxSpecs: 50, maxResponseBytes: 120, mergeGap: 0 },
  });
  assert.equal(r.decision, 'REJECT');
  if (r.decision !== 'REJECT') throw new Error('guard');
  assert.equal(r.code, 'RESPONSE_SIZE_LIMIT_EXCEEDED');
});

test('huge suffix serves the whole object (206), never an integer error', () => {
  const r = decide(`bytes=-${'9'.repeat(40)}`, 100);
  assert.equal(r.decision, 'SINGLE_PART');
  if (r.decision !== 'SINGLE_PART') throw new Error('guard');
  assert.deepEqual(r.intervals, [{ start: 0, end: 99 }]);
});

test('huge first-byte-pos is unsatisfiable 416, not a syntax error', () => {
  const r = decide(`bytes=${'9'.repeat(40)}-`, 100);
  assert.equal(r.decision, 'REJECT');
  if (r.decision !== 'REJECT') throw new Error('guard');
  assert.equal(r.status, 416);
  assert.equal(r.code, 'UNSATISFIABLE_RANGE');
});

// --- If-Range ---------------------------------------------------------------

test('If-Range matching strong etag permits the range', () => {
  const r = decide('bytes=0-9', 100, { ifRangeHeader: meta(100).etag });
  assert.equal(r.decision, 'SINGLE_PART');
});

test('If-Range mismatched etag falls back to 200 full', () => {
  const r = decide('bytes=0-9', 100, { ifRangeHeader: '"ffffffff"' });
  assert.equal(r.decision, 'FULL_REPRESENTATION');
  if (r.decision !== 'FULL_REPRESENTATION') throw new Error('guard');
  assert.equal(r.reason, 'IF_RANGE_MISMATCH');
});

test('If-Range weak validator never matches: 200 full', () => {
  const r = decide('bytes=0-9', 100, { ifRangeHeader: `W/${meta(100).etag}` });
  assert.equal(r.decision, 'FULL_REPRESENTATION');
});

test('If-Range matching Last-Modified date permits the range', () => {
  const r = decide('bytes=0-9', 100, {
    ifRangeHeader: meta(100).lastModified.toUTCString(),
  });
  assert.equal(r.decision, 'SINGLE_PART');
});

test('If-Range stale date falls back to 200 full', () => {
  const r = decide('bytes=0-9', 100, {
    ifRangeHeader: 'Wed, 01 Jan 2020 00:00:00 GMT',
  });
  assert.equal(r.decision, 'FULL_REPRESENTATION');
});

test('If-Range unparseable value is indeterminate: conservative 200 full', () => {
  const r = decide('bytes=0-9', 100, { ifRangeHeader: 'not-a-validator' });
  assert.equal(r.decision, 'FULL_REPRESENTATION');
  if (r.decision !== 'FULL_REPRESENTATION') throw new Error('guard');
  assert.equal(r.reason, 'IF_RANGE_INDETERMINATE');
});
