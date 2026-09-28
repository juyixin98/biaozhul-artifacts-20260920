import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { FieldSink, FileSink, TempStore } from '../../src/storage/temp-store.js';
import type { PartMeta } from '../../src/protocol/types.js';

const root = await mkdtemp(path.join(tmpdir(), 'mp-tempstore-'));
after(async () => rm(root, { recursive: true, force: true }));

function sha256(s: string): string {
  return createHash('sha256').update(s, 'utf8').digest('hex');
}

const fileMeta = (name: string): PartMeta => ({
  name,
  kind: 'file',
  filename: 'a.txt',
  contentType: 'text/plain',
  headerLines: [],
});
const fieldMeta = (name: string): PartMeta => ({
  name,
  kind: 'field',
  contentType: 'text/plain',
  headerLines: [],
});

test('FieldSink concatenates bytes and hashes SHA-256', async () => {
  const sink = new FieldSink();
  sink.write(Buffer.from('hello '));
  sink.write(Buffer.from('world'));
  const out = await sink.end();
  assert.equal(out.size, 11);
  assert.equal(out.value, 'hello world');
  assert.equal(out.sha256, sha256('hello world'));
});

test('FileSink spools bytes, and the file is readable then removable', async () => {
  const store = new TempStore(root);
  await store.prepare();
  const sink = store.createSink(fileMeta('up')) as FileSink;
  await sink.write(Buffer.from('spooled '));
  await sink.write(Buffer.from('payload'));
  const fin = await sink.end();
  assert.equal(fin.size, 15);
  assert.equal(fin.tempPath!.startsWith(store.directory), true);
  const onDisk = await readFile(fin.tempPath!);
  assert.equal(onDisk.toString(), 'spooled payload');
  await store.cleanup();
});

test('cleanup removes ONLY this request directory', async () => {
  const a = new TempStore(root, 'req-A');
  const b = new TempStore(root, 'req-B');
  await a.prepare();
  await b.prepare();
  const sinkA = a.createSink(fileMeta('f1')) as FileSink;
  await sinkA.write(Buffer.from('A-data'));
  await sinkA.end();
  const sinkB = b.createSink(fileMeta('f2')) as FileSink;
  await sinkB.write(Buffer.from('B-data'));
  await sinkB.end();

  await a.cleanup();

  await assert.rejects(stat(a.directory), /ENOENT/);
  // B survives untouched.
  const entriesB = await b.listEntries();
  assert.equal(entriesB.length, 1);
  await b.cleanup();
  await assert.rejects(stat(b.directory), /ENOENT/);
});

test('cleanup is idempotent and safe when prepare was never called', async () => {
  const s = new TempStore(root, 'req-cold');
  await s.cleanup();
  await s.cleanup();
});

test('destroying an un-finalized FileSink deletes its partial file', async () => {
  const s = new TempStore(root, 'req-cancel');
  await s.prepare();
  const sink = s.createSink(fileMeta('up')) as FileSink;
  await sink.write(Buffer.from('partial'));
  await sink.destroy();
  await sink.destroy(); // idempotent
  assert.deepEqual(await s.listEntries(), []);
  await s.cleanup();
});

test('TempStore refuses to read a path outside its own directory', async () => {
  const s = new TempStore(root, 'req-jail');
  await s.prepare();
  await assert.rejects(
    () => s.readPartFile(path.resolve(root, 'req-other', 'x.part')),
    /outside this request directory/,
  );
  await s.cleanup();
});

test('field parts are buffered, file parts get distinct filenames', async () => {
  const s = new TempStore(root, 'req-kinds');
  await s.prepare();
  const field = s.createSink(fieldMeta('note'));
  assert.ok(field instanceof FieldSink);
  const f1 = s.createSink(fileMeta('a')) as FileSink;
  const f2 = s.createSink(fileMeta('a')) as FileSink;
  assert.notEqual((f1 as unknown as { filePath: string }).filePath, undefined);
  await f1.end();
  await f2.end();
  assert.equal((await s.listEntries()).length, 2);
  await s.cleanup();
});
