import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseRangeHeader } from '../../src/range/parser.js';

test('parses closed, open-ended and suffix forms', () => {
  const r = parseRangeHeader('bytes=0-499');
  assert.equal(r.ok, true);
  if (!r.ok) throw new Error('expected ok');
  assert.deepEqual(r.specs[0], {
    index: 0, raw: '0-499', kind: 'closed',
    firstBytePos: 0n, lastBytePos: 499n,
  });

  const open = parseRangeHeader('bytes=500-');
  if (!open.ok) throw new Error('expected ok');
  assert.equal(open.specs[0]!.kind, 'open-ended');
  assert.equal(open.specs[0]!.firstBytePos, 500n);
  assert.equal(open.specs[0]!.lastBytePos, undefined);

  const suffix = parseRangeHeader('bytes=-500');
  if (!suffix.ok) throw new Error('expected ok');
  assert.deepEqual(suffix.specs[0], {
    index: 0, raw: '-500', kind: 'suffix', suffixLength: 500n,
  });
});

test('parses multiple specs preserving order and raw text', () => {
  const r = parseRangeHeader(' bytes = 0-9 , 10- , -5 ');
  if (!r.ok) throw new Error(r.message);
  assert.equal(r.unit, 'bytes');
  assert.deepEqual(r.specs.map((s) => s.raw), ['0-9', '10-', '-5']);
  assert.deepEqual(r.specs.map((s) => s.index), [0, 1, 2]);
});

test('accepts leading zeros only as a single zero digit', () => {
  for (const good of ['bytes=0-0', 'bytes=0-', 'bytes=-0']) {
    assert.equal(parseRangeHeader(good).ok, true, good);
  }
});

const malformed: Array<[string, string]> = [
  ['bytes=5-2', 'last-byte-pos'],
  ['bytes=abc', 'expected'],
  ['bytes=1-2-3', 'expected'],
  ['bytes=', 'Malformed'],
  ['bytes=,0-1', 'empty range spec'],
  ['bytes=0-1,', 'empty range spec'],
  ['bytes=-', 'suffix range needs'],
  ['bytes=1--2', 'expected'],
  ['bytes=0x1-2', 'first byte position'],
  ['bytes=1 -2', 'first byte position'],
  ['bytes==0-1', 'Malformed'],
  ['bytes=0-1=2', 'Malformed'],
  ['bytes=-01', 'suffix length'],
  ['nonsense', 'Malformed'],
];

for (const [header, fragment] of malformed) {
  test(`malformed: ${header}`, () => {
    const r = parseRangeHeader(header);
    assert.equal(r.ok, false);
    if (r.ok) throw new Error('should fail');
    assert.equal(r.code, 'MALFORMED_RANGE_HEADER');
    assert.match(r.message, new RegExp(fragment.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  });
}

test('non-bytes unit is a distinct UNSUPPORTED_RANGE_UNIT result', () => {
  const r = parseRangeHeader('items=0-10');
  assert.equal(r.ok, false);
  if (r.ok) throw new Error('should fail');
  assert.equal(r.code, 'UNSUPPORTED_RANGE_UNIT');
});

test('arbitrarily large integers parse (syntax is not bounded by Number)', () => {
  const huge = '9'.repeat(40);
  const suffix = parseRangeHeader(`bytes=-${huge}`);
  if (!suffix.ok) throw new Error(suffix.message);
  assert.equal(suffix.specs[0]!.suffixLength, BigInt(huge));

  const open = parseRangeHeader(`bytes=${huge}-`);
  if (!open.ok) throw new Error(open.message);
  assert.equal(open.specs[0]!.firstBytePos, BigInt(huge));
});

test('101-digit integer is rejected at the defensive digit bound', () => {
  const r = parseRangeHeader(`bytes=${'1'.repeat(101)}-`);
  assert.equal(r.ok, false);
  if (r.ok) throw new Error('should fail');
  assert.equal(r.code, 'MALFORMED_RANGE_HEADER');
  assert.equal(r.specError?.reason.includes('max 100 digits'), true);
});
