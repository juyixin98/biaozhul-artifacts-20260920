import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  ALPHA,
  BIN256,
  EMPTY,
  HELLO,
  PATTERN_1K,
} from '../fixtures/reference.js';
import { header, startHarness, type Harness } from './harness.js';

async function seeded(): Promise<Harness> {
  const h = await startHarness();
  const { store } = h.built;
  store.put({ id: 'alpha', data: ALPHA, contentType: 'text/plain; charset=utf-8', createdAt: new Date('2026-02-03T04:05:06.000Z') });
  store.put({ id: 'hello', data: HELLO, contentType: 'text/plain; charset=utf-8' });
  store.put({ id: 'bin', data: BIN256, contentType: 'application/octet-stream' });
  store.put({ id: 'empty', data: EMPTY, contentType: 'application/octet-stream' });
  store.put({ id: 'p1k', data: PATTERN_1K, contentType: 'application/octet-stream' });
  return h;
}

test('GET without Range returns 200 and the complete bytes', async () => {
  const h = await seeded();
  const res = await h.get('/objects/alpha');
  assert.equal(res.statusCode, 200);
  assert.equal(header(res, 'accept-ranges'), 'bytes');
  assert.equal(header(res, 'content-length'), String(ALPHA.length));
  assert.ok(res.rawPayload.equals(ALPHA));
});

test('closed range: exact status, Content-Range, Content-Length and bytes', async () => {
  const h = await seeded();
  const res = await h.get('/objects/alpha', { Range: 'bytes=2-5' });
  assert.equal(res.statusCode, 206);
  assert.equal(header(res, 'content-range'), 'bytes 2-5/26');
  assert.equal(header(res, 'content-length'), '4');
  assert.ok(res.rawPayload.equals(ALPHA.subarray(2, 6)));
  assert.equal(res.payload, 'cdef');
});

test('open-ended range runs to the last byte', async () => {
  const h = await seeded();
  const res = await h.get('/objects/alpha', { Range: 'bytes=23-' });
  assert.equal(res.statusCode, 206);
  assert.equal(header(res, 'content-range'), 'bytes 23-25/26');
  assert.equal(res.payload, 'xyz');
});

test('suffix range returns the final N bytes', async () => {
  const h = await seeded();
  const res = await h.get('/objects/alpha', { Range: 'bytes=-4' });
  assert.equal(res.statusCode, 206);
  assert.equal(header(res, 'content-range'), 'bytes 22-25/26');
  assert.equal(res.payload, 'wxyz');
});

test('last-byte-pos beyond the object is clamped, not rejected', async () => {
  const h = await seeded();
  const res = await h.get('/objects/alpha', { Range: 'bytes=24-9999' });
  assert.equal(res.statusCode, 206);
  assert.equal(header(res, 'content-range'), 'bytes 24-25/26');
  assert.equal(header(res, 'content-length'), '2');
  assert.equal(res.payload, 'yz');
});

test('suffix longer than the object returns all of it as one part', async () => {
  const h = await seeded();
  const res = await h.get('/objects/alpha', { Range: 'bytes=-500' });
  assert.equal(res.statusCode, 206);
  assert.equal(header(res, 'content-range'), 'bytes 0-25/26');
  assert.ok(res.rawPayload.equals(ALPHA));
});

test('Content-Length header always equals the actual body byte count', async () => {
  const h = await seeded();
  for (const range of ['bytes=0-0', 'bytes=0-9999', 'bytes=-1', 'bytes=13-', 'bytes=5-5']) {
    const res = await h.get('/objects/hello', { Range: range });
    assert.equal(
      Number(header(res, 'content-length')),
      res.rawPayload.length,
      `${range}: header/body length mismatch`,
    );
  }
});

test('first-byte-pos at/after size -> 416 with Content-Range bytes */size', async () => {
  const h = await seeded();
  const atSize = await h.get('/objects/alpha', { Range: 'bytes=26-' });
  assert.equal(atSize.statusCode, 416);
  assert.equal(header(atSize, 'content-range'), 'bytes */26');
  const body = atSize.json() as { error: string; dropped: Array<{ reason: string }> };
  assert.equal(body.error, 'UNSATISFIABLE_RANGE');
  assert.equal(body.dropped[0]!.reason, 'START_BEYOND_OBJECT');
});

test('zero-length object: plain GET is 200 empty; any Range is 416 bytes */0', async () => {
  const h = await seeded();
  const full = await h.get('/objects/empty');
  assert.equal(full.statusCode, 200);
  assert.equal(header(full, 'content-length'), '0');
  assert.equal(full.rawPayload.length, 0);

  for (const range of ['bytes=0-0', 'bytes=-1', 'bytes=0-']) {
    const res = await h.get('/objects/empty', { Range: range });
    assert.equal(res.statusCode, 416, range);
    assert.equal(header(res, 'content-range'), 'bytes */0', range);
  }
});

test('huge integers: oversized suffix serves all; oversized start is 416', async () => {
  const h = await seeded();
  const suffix = await h.get('/objects/alpha', { Range: `bytes=-${'9'.repeat(40)}` });
  assert.equal(suffix.statusCode, 206);
  assert.equal(header(suffix, 'content-range'), 'bytes 0-25/26');
  assert.ok(suffix.rawPayload.equals(ALPHA));

  const start = await h.get('/objects/alpha', { Range: `bytes=${'9'.repeat(40)}-` });
  assert.equal(start.statusCode, 416);
  assert.equal((start.json() as { error: string }).error, 'UNSATISFIABLE_RANGE');
});

test('malformed ranges are 400 with a precise spec error', async () => {
  const h = await seeded();
  const cases: Array<[string, number]> = [
    ['bytes=9-1', 0],
    ['bytes=abc', 0],
    ['bytes=0-1,,3-4', 1],
  ];
  for (const [range, index] of cases) {
    const res = await h.get('/objects/alpha', { Range: range });
    assert.equal(res.statusCode, 400, range);
    const body = res.json() as { error: string; specError: { index: number } };
    assert.equal(body.error, 'MALFORMED_RANGE_HEADER');
    assert.equal(body.specError.index, index);
  }
});

test('unknown range unit is ignored: 200 full representation', async () => {
  const h = await seeded();
  const res = await h.get('/objects/alpha', { Range: 'items=0-9' });
  assert.equal(res.statusCode, 200);
  assert.ok(res.rawPayload.equals(ALPHA));
});

test('unknown object id is 404', async () => {
  const h = await seeded();
  const res = await h.get('/objects/missing');
  assert.equal(res.statusCode, 404);
});

test('binary object slices are byte-exact, including every octet 0-255', async () => {
  const h = await seeded();
  const res = await h.get('/objects/bin', { Range: 'bytes=100-199' });
  assert.equal(res.statusCode, 206);
  assert.ok(res.rawPayload.equals(BIN256.subarray(100, 200)));
  // Full binary fetch as well, to rule out transport corruption.
  const all = await h.get('/objects/bin');
  assert.ok(all.rawPayload.equals(BIN256));
});

test('POST stores an object; a second PUT to the same id is 409 and data is unchanged', async () => {
  const h = await seeded();
  const first = await h.post('/objects/custom', Buffer.from('first-version'), {
    'content-type': 'text/plain',
  });
  assert.equal(first.statusCode, 201);
  const second = await h.post('/objects/custom', Buffer.from('second-version'), {
    'content-type': 'text/plain',
  });
  assert.equal(second.statusCode, 409);
  assert.equal((second.json() as { error: string }).error, 'OBJECT_EXISTS');
  const fetched = await h.get('/objects/custom');
  assert.equal(fetched.payload, 'first-version');
});

test('POST binary round-trips byte-for-byte over HTTP', async () => {
  const h = await seeded();
  await h.post('/objects/binup', BIN256);
  const fetched = await h.get('/objects/binup');
  assert.equal(fetched.statusCode, 200);
  assert.ok(fetched.rawPayload.equals(BIN256));
});

test('GET /objects lists stored metadata including zero-length object', async () => {
  const h = await seeded();
  const res = await h.get('/objects');
  const ids = (res.json() as { objects: Array<{ id: string; size: number }> }).objects.map(
    (o) => `${o.id}:${o.size}`,
  );
  assert.ok(ids.includes('empty:0'));
  assert.ok(ids.includes('alpha:26'));
});
