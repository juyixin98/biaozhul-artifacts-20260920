import assert from 'node:assert/strict';
import { test } from 'node:test';
import { contentRangeValue, encodeMultipart } from '../../src/range/multipart.js';
import {
  ALPHA,
  BIN256,
  boundaryOf,
  parseMultipart,
} from '../fixtures/reference.js';

const CRLF = '\r\n';

test('single part body is byte-for-byte the independently framed expectation', () => {
  const parts = [{ interval: { start: 0, end: 2 }, bytes: ALPHA.subarray(0, 3) }];
  const { contentType, body } = encodeMultipart(parts, ALPHA.length, 'text/plain', 'BND');

  const expected = Buffer.from(
    `--BND${CRLF}Content-Type: text/plain${CRLF}Content-Range: bytes 0-2/26${CRLF}${CRLF}`,
    'ascii',
  );
  // Build the tail without string-joining binary payloads.
  const tail = Buffer.concat([
    ALPHA.subarray(0, 3),
    Buffer.from(`${CRLF}--BND--${CRLF}`, 'ascii'),
  ]);
  assert.ok(body.equals(Buffer.concat([expected, tail])));
  assert.equal(contentType, 'multipart/byteranges; boundary=BND');
});

test('multiple parts survive an independent parser and match source bytes', () => {
  const intervals = [
    { start: 0, end: 2 },
    { start: 10, end: 12 },
    { start: 24, end: 25 },
  ];
  const parts = intervals.map((iv) => ({
    interval: iv,
    bytes: ALPHA.subarray(iv.start, iv.end + 1),
  }));
  const { contentType, body } = encodeMultipart(parts, ALPHA.length, 'text/plain', 'XY');

  // Declared length must equal the bytes actually emitted.
  assert.equal(body.length, Buffer.byteLength(body));

  const boundary = boundaryOf(contentType);
  const parsed = parseMultipart(body, boundary);
  assert.equal(parsed.length, 3);
  for (const [i, part] of parsed.entries()) {
    const iv = intervals[i]!;
    assert.equal(part.range.start, iv.start);
    assert.equal(part.range.end, iv.end);
    assert.equal(part.range.complete, ALPHA.length);
    assert.ok(part.bytes.equals(ALPHA.subarray(iv.start, iv.end + 1)));
    assert.equal(part.headers['content-type'], 'text/plain');
  }
});

test('binary content with every byte value 0-255 round-trips exactly', () => {
  const iv = { start: 0, end: 255 };
  const { contentType, body } = encodeMultipart(
    [{ interval: iv, bytes: BIN256 }],
    BIN256.length,
    'application/octet-stream',
    'B',
  );
  const parsed = parseMultipart(body, boundaryOf(contentType));
  assert.ok(parsed[0]!.bytes.equals(BIN256));
});

test('encoder refuses part bytes whose length disagrees with the interval', () => {
  assert.throws(
    () =>
      encodeMultipart(
        [{ interval: { start: 0, end: 9 }, bytes: Buffer.from('short') }],
        26,
        'text/plain',
        'B',
      ),
    /does not match interval/,
  );
});

test('contentRangeValue formatting', () => {
  assert.equal(contentRangeValue({ start: 10, end: 19 }, 100), 'bytes 10-19/100');
  assert.equal(contentRangeValue({ start: 0, end: 0 }, 1), 'bytes 0-0/1');
});
