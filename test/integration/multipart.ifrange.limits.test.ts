import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  ALPHA,
  BIN256,
  HELLO,
  PATTERN_1K,
  boundaryOf,
  parseMultipart,
} from '../fixtures/reference.js';
import { header, startHarness, type Harness } from './harness.js';

async function seeded(): Promise<Harness> {
  const h = await startHarness();
  const { store } = h.built;
  store.put({ id: 'alpha', data: ALPHA, contentType: 'text/plain; charset=utf-8' });
  store.put({ id: 'hello', data: HELLO, contentType: 'text/plain; charset=utf-8' });
  store.put({ id: 'bin', data: BIN256, contentType: 'application/octet-stream' });
  store.put({ id: 'p1k', data: PATTERN_1K, contentType: 'application/octet-stream' });
  return h;
}

test('multipart: reassembled body equals each requested interval byte-for-byte', async () => {
  const h = await seeded();
  const requested = [
    { start: 0, end: 2 },
    { start: 10, end: 12 },
    { start: 24, end: 25 },
  ];
  const spec = requested.map((iv) => `${iv.start}-${iv.end}`).join(',');
  const res = await h.get('/objects/alpha', { Range: `bytes=${spec}` });
  assert.equal(res.statusCode, 206);
  const ct = header(res, 'content-type')!;
  assert.match(ct, /^multipart\/byteranges; boundary=/);
  // Header length matches the real framed body.
  assert.equal(Number(header(res, 'content-length')), res.rawPayload.length);

  const parts = parseMultipart(res.rawPayload, boundaryOf(ct));
  assert.equal(parts.length, requested.length);
  for (const [i, part] of parts.entries()) {
    const iv = requested[i]!;
    assert.deepEqual(part.range, { start: iv.start, end: iv.end, complete: ALPHA.length });
    assert.ok(part.bytes.equals(ALPHA.subarray(iv.start, iv.end + 1)));
  }
});

test('multipart over binary 0..255 content round-trips with no octet corruption', async () => {
  const h = await seeded();
  const res = await h.get('/objects/bin', { Range: 'bytes=0-49,200-255' });
  const ct = header(res, 'content-type')!;
  const parts = parseMultipart(res.rawPayload, boundaryOf(ct));
  assert.ok(parts[0]!.bytes.equals(BIN256.subarray(0, 50)));
  assert.ok(parts[1]!.bytes.equals(BIN256.subarray(200, 256)));
});

test('adjacent ranges merge into ONE part -> single-part 206, not multipart', async () => {
  const h = await seeded();
  const res = await h.get('/objects/alpha', { Range: 'bytes=0-9,10-19' });
  assert.equal(res.statusCode, 206);
  assert.equal(header(res, 'content-type'), 'text/plain; charset=utf-8');
  assert.equal(header(res, 'content-range'), 'bytes 0-19/26');
  assert.equal(res.payload, 'abcdefghijklmnopqrst');
});

test('overlapping and duplicate ranges are merged, so bytes are served once', async () => {
  const h = await seeded();
  const res = await h.get('/objects/alpha', { Range: 'bytes=0-15,5-10,16-20' });
  assert.equal(res.statusCode, 206);
  assert.equal(header(res, 'content-range'), 'bytes 0-20/26');
  assert.equal(res.rawPayload.length, 21);
  assert.equal(res.payload, 'abcdefghijklmnopqrstu');
});

test('partly satisfiable multipart drops the bad interval and serves the rest', async () => {
  const h = await seeded();
  const res = await h.get('/objects/alpha', { Range: 'bytes=0-4,100-200,20-22' });
  assert.equal(res.statusCode, 206);
  const ct = header(res, 'content-type')!;
  const parts = parseMultipart(res.rawPayload, boundaryOf(ct));
  assert.deepEqual(parts.map((p) => p.range), [
    { start: 0, end: 4, complete: 26 },
    { start: 20, end: 22, complete: 26 },
  ]);
  assert.ok(parts[0]!.bytes.equals(ALPHA.subarray(0, 5)));
  assert.ok(parts[1]!.bytes.equals(ALPHA.subarray(20, 23)));
});

test('declared multipart Content-Length matches actual body for many parts', async () => {
  const h = await seeded();
  // Stride 4: 0-2, 4-6, 8-10 ... one gap byte keeps the parts distinct
  // (adjacent parts would correctly merge under the default mergeGap=1).
  const specs = Array.from({ length: 20 }, (_, i) => `${i * 4}-${i * 4 + 2}`).join(',');
  const res = await h.get('/objects/p1k', { Range: `bytes=${specs}` });
  assert.equal(res.statusCode, 206);
  assert.equal(Number(header(res, 'content-length')), res.rawPayload.length);
  const parts = parseMultipart(res.rawPayload, boundaryOf(header(res, 'content-type')!));
  assert.equal(parts.length, 20);
});

// --- If-Range ---------------------------------------------------------------

test('If-Range with the current strong ETag permits the partial response', async () => {
  const h = await seeded();
  const meta = h.built.store.getMeta('alpha')!;
  const res = await h.get('/objects/alpha', {
    Range: 'bytes=0-4',
    'If-Range': meta.etag,
  });
  assert.equal(res.statusCode, 206);
  assert.equal(res.payload, 'abcde');
});

test('If-Range with a stale ETag returns the FULL representation (200)', async () => {
  const h = await seeded();
  const res = await h.get('/objects/alpha', {
    Range: 'bytes=0-4',
    'If-Range': '"0000000000000000000000000000000000000000000000000000000000000000"',
  });
  assert.equal(res.statusCode, 200);
  assert.ok(res.rawPayload.equals(ALPHA));
  assert.equal(header(res, 'content-length'), String(ALPHA.length));
});

test('If-Range weak ETag never authorises a range: 200 full', async () => {
  const h = await seeded();
  const meta = h.built.store.getMeta('alpha')!;
  const res = await h.get('/objects/alpha', {
    Range: 'bytes=0-4',
    'If-Range': `W/${meta.etag}`,
  });
  assert.equal(res.statusCode, 200);
  assert.ok(res.rawPayload.equals(ALPHA));
});

test('If-Range with matching Last-Modified date permits the range', async () => {
  const h = await seeded();
  const meta = h.built.store.getMeta('alpha')!;
  const res = await h.get('/objects/alpha', {
    Range: 'bytes=0-4',
    'If-Range': meta.lastModified.toUTCString(),
  });
  assert.equal(res.statusCode, 206);
  assert.equal(res.payload, 'abcde');
});

test('If-Range with a garbage value conservatively returns the full body', async () => {
  const h = await seeded();
  const res = await h.get('/objects/alpha', {
    Range: 'bytes=0-4',
    'If-Range': 'midnight-ish',
  });
  assert.equal(res.statusCode, 200);
  assert.ok(res.rawPayload.equals(ALPHA));
});

// --- limits -----------------------------------------------------------------

test('RANGE_MAX_SPECS caps the number of specs with a 400', async () => {
  const h = await startHarness({
    limits: { maxSpecs: 3, maxResponseBytes: 1_000_000, mergeGap: 1 },
  });
  h.built.store.put({ id: 'a', data: ALPHA, contentType: 'text/plain' });
  const res = await h.get('/objects/a', { Range: 'bytes=0-0,1-1,2-2,3-3' });
  assert.equal(res.statusCode, 400);
  assert.equal((res.json() as { error: string }).error, 'TOO_MANY_RANGES');
});

test('RANGE_MAX_RESPONSE_BYTES rejects a response over budget', async () => {
  const h = await startHarness({
    limits: { maxSpecs: 50, maxResponseBytes: 10, mergeGap: 1 },
  });
  h.built.store.put({ id: 'a', data: ALPHA, contentType: 'text/plain' });
  const res = await h.get('/objects/a', { Range: 'bytes=0-99' });
  assert.equal(res.statusCode, 400);
  assert.equal(
    (res.json() as { error: string }).error,
    'RESPONSE_SIZE_LIMIT_EXCEEDED',
  );
});
