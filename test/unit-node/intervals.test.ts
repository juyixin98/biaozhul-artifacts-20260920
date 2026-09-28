import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseRangeHeader } from '../../src/range/parser.js';
import { mergeIntervals, resolveSpecs } from '../../src/range/intervals.js';
import type { ParsedSpec } from '../../src/types.js';

function specsOf(header: string): ParsedSpec[] {
  const r = parseRangeHeader(header);
  if (!r.ok) throw new Error(r.message);
  return r.specs;
}

test('closed range clamps the last position to size-1 but keeps the start', () => {
  const { intervals, dropped, requestedBytes } = resolveSpecs(specsOf('bytes=5-999'), 10);
  assert.deepEqual(intervals, [{ start: 5, end: 9 }]);
  assert.deepEqual(dropped, []);
  assert.equal(requestedBytes, 5);
});

test('open-ended range reaches the final byte', () => {
  const { intervals } = resolveSpecs(specsOf('bytes=7-'), 20);
  assert.deepEqual(intervals, [{ start: 7, end: 19 }]);
});

test('suffix shorter than the object returns its last N bytes', () => {
  const { intervals } = resolveSpecs(specsOf('bytes=-5'), 20);
  assert.deepEqual(intervals, [{ start: 15, end: 19 }]);
});

test('suffix longer than the object clamps to the WHOLE object, not an error', () => {
  const { intervals, dropped } = resolveSpecs(specsOf('bytes=-500'), 20);
  assert.deepEqual(intervals, [{ start: 0, end: 19 }]);
  assert.deepEqual(dropped, []);
});

test('suffix of astronomic length still clamps to the whole object', () => {
  const { intervals } = resolveSpecs(specsOf(`bytes=-${'9'.repeat(40)}`), 20);
  assert.deepEqual(intervals, [{ start: 0, end: 19 }]);
});

test('first byte at exactly size is unsatisfiable, one beyond too', () => {
  const atSize = resolveSpecs(specsOf('bytes=20-'), 20);
  assert.deepEqual(atSize.intervals, []);
  assert.equal(atSize.dropped[0]!.reason, 'START_BEYOND_OBJECT');

  const beyond = resolveSpecs(specsOf(`bytes=${'9'.repeat(40)}-`), 20);
  assert.deepEqual(beyond.intervals, []);
  assert.equal(beyond.dropped[0]!.reason, 'START_BEYOND_OBJECT');
});

test('partly satisfiable request keeps good specs and records each drop', () => {
  const r = resolveSpecs(specsOf('bytes=0-3,100-200,5-'), 10);
  assert.deepEqual(r.intervals, [
    { start: 0, end: 3 },
    { start: 5, end: 9 },
  ]);
  assert.deepEqual(r.dropped, [
    { index: 1, raw: '100-200', reason: 'START_BEYOND_OBJECT' },
  ]);
});

test('bytes=-0 is dropped as EMPTY_SUFFIX even on a non-empty object', () => {
  const r = resolveSpecs(specsOf('bytes=-0'), 10);
  assert.deepEqual(r.intervals, []);
  assert.equal(r.dropped[0]!.reason, 'EMPTY_SUFFIX');
});

test('every spec on a zero-length object is dropped; -0 keeps its category', () => {
  const closed = resolveSpecs(specsOf('bytes=0-0'), 0);
  assert.deepEqual(closed.intervals, []);
  assert.equal(closed.dropped[0]!.reason, 'OBJECT_EMPTY');

  const suffix = resolveSpecs(specsOf('bytes=-5'), 0);
  assert.equal(suffix.dropped[0]!.reason, 'OBJECT_EMPTY');

  const zeroSuffix = resolveSpecs(specsOf('bytes=-0'), 0);
  assert.equal(zeroSuffix.dropped[0]!.reason, 'EMPTY_SUFFIX');
});

test('requestedBytes sums PRE-merge lengths, counting overlaps twice', () => {
  const r = resolveSpecs(specsOf('bytes=0-9,5-15'), 20);
  assert.equal(r.requestedBytes, 10 + 11);
});

// --- mergeIntervals --------------------------------------------------------

test('mergeIntervals with gap=1 joins ADJACENT intervals', () => {
  const { intervals, stats } = mergeIntervals(
    [{ start: 0, end: 9 }, { start: 10, end: 19 }],
    1,
  );
  assert.deepEqual(intervals, [{ start: 0, end: 19 }]);
  assert.equal(stats.mergeCount, 1);
});

test('mergeIntervals with gap=0 keeps merely-adjacent intervals apart', () => {
  const { intervals } = mergeIntervals(
    [{ start: 0, end: 9 }, { start: 10, end: 19 }],
    0,
  );
  assert.deepEqual(intervals, [
    { start: 0, end: 9 },
    { start: 10, end: 19 },
  ]);
});

test('overlap, containment and exact duplicates collapse to one interval', () => {
  const { intervals, stats } = mergeIntervals(
    [
      { start: 0, end: 100 },
      { start: 10, end: 20 },   // contained
      { start: 50, end: 60 },   // contained
      { start: 0, end: 100 },   // duplicate
      { start: 95, end: 130 },  // overlaps at the right edge
    ],
    0,
  );
  assert.deepEqual(intervals, [{ start: 0, end: 130 }]);
  assert.equal(stats.mergeCount, 4);
});

test('sorts out-of-order requests before merging', () => {
  const { intervals } = mergeIntervals(
    [{ start: 20, end: 29 }, { start: 0, end: 9 }, { start: 10, end: 19 }],
    1,
  );
  assert.deepEqual(intervals, [{ start: 0, end: 29 }]);
});

test('boundary-overlapping intervals at a shared byte merge (gap=0)', () => {
  const { intervals } = mergeIntervals(
    [{ start: 0, end: 10 }, { start: 10, end: 20 }],
    0,
  );
  assert.deepEqual(intervals, [{ start: 0, end: 20 }]);
});

test('sources attribute each survivor to the pre-sort interval indexes', () => {
  const { stats } = mergeIntervals(
    [{ start: 30, end: 39 }, { start: 0, end: 9 }, { start: 1, end: 2 }],
    1,
  );
  // [0..9] <- source 1, source 2 ; [30..39] <- source 0, in sorted order
  assert.deepEqual(stats.sources, [[1, 2], [0]]);
});
