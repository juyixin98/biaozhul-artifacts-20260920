import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, test } from 'node:test';
import { rmSync } from 'node:fs';
import { SqliteObjectStore, strongEtag } from '../../src/state/store.js';
import { BIN256, EMPTY, PATTERN_1K } from '../fixtures/reference.js';

let store: SqliteObjectStore | null = null;
const dbPath = join(tmpdir(), `range-store-${process.pid}-${Date.now()}.db`);

afterEach(() => {
  store?.close();
  rmSync(dbPath, { force: true });
  rmSync(`${dbPath}-wal`, { force: true });
  rmSync(`${dbPath}-shm`, { force: true });
});

test('strongEtag is the quoted SHA-256 hex digest', () => {
  const expected = `"${createHash('sha256').update(BIN256).digest('hex')}"`;
  assert.equal(strongEtag(BIN256), expected);
});

test('put + getMeta round trip records size, etag, content type, date', () => {
  store = new SqliteObjectStore(dbPath);
  const saved = store.put({
    id: 'bin',
    data: BIN256,
    contentType: 'application/octet-stream',
    createdAt: new Date('2026-06-07T08:09:10.000Z'),
  });
  assert.equal(saved.size, 256);
  assert.equal(saved.etag, strongEtag(BIN256));

  const meta = store.getMeta('bin')!;
  assert.deepEqual(meta, saved);
});

test('zero-length object stores and returns an empty buffer', () => {
  store = new SqliteObjectStore(dbPath);
  store.put({ id: 'empty', data: EMPTY, contentType: 'application/octet-stream' });
  const obj = store.getObject('empty')!;
  assert.equal(obj.size, 0);
  assert.ok(Buffer.isBuffer(obj.data));
  assert.equal(obj.data.length, 0);
  const slice = store.getSlice('empty', 0, 0)!;
  assert.equal(slice.length, 0);
});

test('getSlice returns exact inclusive byte ranges at original offsets', () => {
  store = new SqliteObjectStore(dbPath);
  store.put({ id: 'p1k', data: PATTERN_1K, contentType: 'application/octet-stream' });
  assert.ok(store.getSlice('p1k', 0, 0)!.equals(PATTERN_1K.subarray(0, 1)));
  assert.ok(store.getSlice('p1k', 100, 199)!.equals(PATTERN_1K.subarray(100, 200)));
  assert.ok(store.getSlice('p1k', 1023, 1023)!.equals(PATTERN_1K.subarray(1023, 1024)));
  // Clamped-looking read beyond the end still reads only present bytes.
  assert.ok(store.getSlice('p1k', 1020, 2000)!.equals(PATTERN_1K.subarray(1020, 1024)));
});

test('binary octets survive the BLOB round trip unchanged', () => {
  store = new SqliteObjectStore(dbPath);
  store.put({ id: 'bin', data: BIN256, contentType: 'application/octet-stream' });
  assert.ok(store.getObject('bin')!.data.equals(BIN256));
});

test('objects are immutable: a second put with the same id is rejected', () => {
  store = new SqliteObjectStore(dbPath);
  store.put({ id: 'once', data: Buffer.from('v1'), contentType: 'text/plain' });
  assert.throws(
    () => store!.put({ id: 'once', data: Buffer.from('v2'), contentType: 'text/plain' }),
    /already exists/,
  );
  // First version is untouched.
  assert.equal(store.getObject('once')!.data.toString(), 'v1');
});

test('missing object lookups return null', () => {
  store = new SqliteObjectStore(dbPath);
  assert.equal(store.getMeta('nope'), null);
  assert.equal(store.getObject('nope'), null);
  assert.equal(store.getSlice('nope', 0, 1), null);
});

test('persistence survives reopening the database file', () => {
  const first = new SqliteObjectStore(dbPath);
  first.put({ id: 'keep', data: PATTERN_1K, contentType: 'application/octet-stream' });
  first.close();

  const second = new SqliteObjectStore(dbPath);
  store = second;
  assert.ok(second.getObject('keep')!.data.equals(PATTERN_1K));
});
