import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ALPHA, HELLO } from '../fixtures/reference.js';
import { header, startHarness, type Harness } from './harness.js';

async function seeded(logBodies = false): Promise<Harness> {
  const h = await startHarness({ logBodies });
  h.built.store.put({ id: 'alpha', data: ALPHA, contentType: 'text/plain; charset=utf-8' });
  h.built.store.put({ id: 'hello', data: HELLO, contentType: 'text/plain; charset=utf-8' });
  return h;
}

test('every accepted range is recorded with request id and key state', async () => {
  const h = await seeded();
  const res = await h.get('/objects/alpha', { Range: 'bytes=0-4,10-14' });
  assert.equal(res.statusCode, 206);
  const requestId = header(res, 'x-request-id')!;
  assert.match(requestId, /^[a-z0-9]+-[0-9a-f]{8}$/);

  const lookup = await h.get(`/diagnostics/${requestId}`);
  assert.equal(lookup.statusCode, 200);
  const rec = (lookup.json() as { record: Record<string, unknown> }).record;
  assert.equal(rec.requestId, requestId);
  assert.equal(rec.objectId, 'alpha');
  assert.equal(rec.objectSize, 26);
  assert.equal(rec.decision, 'accepted:multipart');
  assert.equal(rec.status, 206);
  assert.equal(rec.code, 'OK');
  assert.deepEqual(rec.intervals, [
    { start: 0, end: 4 },
    { start: 10, end: 14 },
  ]);
  assert.equal(rec.servedBytes, 10);
  assert.equal(rec.rangeHeader, 'bytes=0-4,10-14');
  // Why accepted.
  assert.match(String(rec.reason), /Serving 2 merged part/);
});

test('a rejection record explains WHY: all-unsatisfiable 416', async () => {
  const h = await seeded();
  const res = await h.get('/objects/alpha', { Range: 'bytes=900-' });
  assert.equal(res.statusCode, 416);
  const requestId = header(res, 'x-request-id')!;
  const rec = (
    (await h.get(`/diagnostics/${requestId}`)).json() as {
      record: Record<string, unknown>;
    }
  ).record;
  assert.equal(rec.decision, 'rejected');
  assert.equal(rec.code, 'UNSATISFIABLE_RANGE');
  assert.equal(rec.objectSize, 26);
  assert.match(String(rec.reason), /fall outside the 26-byte object/);
  assert.deepEqual(rec.dropped, [
    { index: 0, raw: '900-', reason: 'START_BEYOND_OBJECT' },
  ]);
});

test('If-Range mismatch record: accepted:full with code IF_RANGE_MISMATCH', async () => {
  const h = await seeded();
  const res = await h.get('/objects/alpha', {
    Range: 'bytes=0-4',
    'If-Range': '"stale-etag"',
  });
  assert.equal(res.statusCode, 200);
  const rec = (
    (await h.get(`/diagnostics/${header(res, 'x-request-id')!}`)).json() as {
      record: Record<string, unknown>;
    }
  ).record;
  assert.equal(rec.decision, 'accepted:full');
  assert.equal(rec.code, 'IF_RANGE_MISMATCH');
  assert.match(String(rec.reason), /stale/);
  assert.equal(rec.servedBytes, 26);
});

test('indeterminate If-Range is distinguishable from a hard mismatch', async () => {
  const h = await seeded();
  const res = await h.get('/objects/alpha', {
    Range: 'bytes=0-4',
    'If-Range': '???',
  });
  assert.equal(res.statusCode, 200);
  const rec = (
    (await h.get(`/diagnostics/${header(res, 'x-request-id')!}`)).json() as {
      record: Record<string, unknown>;
    }
  ).record;
  assert.equal(rec.code, 'IF_RANGE_INDETERMINATE');
  assert.match(String(rec.reason), /could not be evaluated/);
});

test('diagnostics list supports objectId and decision filters, newest first', async () => {
  const h = await seeded();
  await h.get('/objects/alpha', { Range: 'bytes=0-0' });
  await h.get('/objects/hello', { Range: 'bytes=0-0' });
  await h.get('/objects/hello', { Range: 'bytes=999-' }); // rejected 416

  const forAlpha = (
    await h.get('/diagnostics?objectId=alpha')
  ).json() as { records: Array<{ objectId: string }> };
  assert.ok(forAlpha.records.length >= 1);
  assert.ok(forAlpha.records.every((r) => r.objectId === 'alpha'));

  const rejected = (
    await h.get('/diagnostics?decision=rejected&objectId=hello')
  ).json() as { records: Array<{ objectId: string; decision: string }> };
  assert.ok(rejected.records.length >= 1);
  assert.ok(
    rejected.records.every(
      (r) => r.objectId === 'hello' && r.decision === 'rejected',
    ),
  );

  const limited = (await h.get('/diagnostics?limit=1')).json() as {
    records: unknown[];
  };
  assert.equal(limited.records.length, 1);
});

test('unknown request id yields 404', async () => {
  const h = await seeded();
  const res = await h.get('/diagnostics/does-not-exist');
  assert.equal(res.statusCode, 404);
});

test('records are persisted to the JSONL file and remain valid JSON', async () => {
  const h = await seeded();
  await h.get('/objects/alpha', { Range: 'bytes=0-2' });
  await h.built.fileSink!.flush();
  const lines = h.readJsonl();
  assert.ok(lines.length >= 1);
  const parsed = lines.map((line) => JSON.parse(line)) as Array<
    Record<string, unknown>
  >;
  const match = parsed.find((r) => r.objectId === 'alpha');
  assert.ok(match);
  assert.equal(match.decision, 'accepted:single');
});

test('default logging never includes body bytes', async () => {
  const h = await seeded(false);
  await h.get('/objects/alpha', { Range: 'bytes=0-9' });
  await h.built.fileSink!.flush();
  const lines = h.readJsonl();
  for (const line of lines) {
    const rec = JSON.parse(line) as Record<string, unknown>;
    assert.equal(rec.bodyPreview, undefined);
    assert.ok(!line.includes('abcdefghij'));
  }
});

test('LOG_BODIES=1 adds only a MASKED preview: real content never appears', async () => {
  const h = await seeded(true);
  await h.get('/objects/hello', { Range: 'bytes=0-18' });
  await h.built.fileSink!.flush();
  const lines = h.readJsonl();
  const rec = JSON.parse(lines.at(-1)!) as { bodyPreview?: string };
  assert.ok(rec.bodyPreview !== undefined);
  assert.equal(rec.bodyPreview!.length, 19);
  // Letters are masked as #; punctuation and spacing survive.
  assert.equal(rec.bodyPreview, '#####, ##### #####!');
  // And the raw payload must not occur anywhere in the log file.
  assert.ok(!h.readJsonl().join('\n').includes('Hello'));
});
