import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { SubmissionRepository } from '../../src/storage/repository.js';
import { TempStore } from '../../src/storage/temp-store.js';
import { MultipartParser } from '../../src/protocol/parser.js';
import { DEFAULT_LIMITS, DEFAULT_RESTRICTIONS } from '../../src/protocol/config.js';
import { isMultipartError } from '../../src/protocol/errors.js';
import type { ParsedForm } from '../../src/protocol/types.js';
import { encodeMultipart } from '../helpers/encode.js';

const root = await mkdtemp(path.join(tmpdir(), 'mp-repo-'));
after(async () => rm(root, { recursive: true, force: true }));

/** Parse wire bytes through a real FileSink-backed TempStore. */
async function parseToTemp(
  wire: Buffer,
  requestId: string,
): Promise<{ form: ParsedForm; temp: TempStore }> {
  const temp = new TempStore(path.join(root, 'tmp'), requestId);
  await temp.prepare();
  const parser = new MultipartParser({
    boundaryDelimiter: '--B',
    limits: DEFAULT_LIMITS,
    restrictions: DEFAULT_RESTRICTIONS,
    createSink: (meta) => temp.createSink(meta),
  });
  await parser.write(wire);
  const form = await parser.finish();
  return { form, temp };
}

test('a parsed form commits atomically and file bytes round-trip', async () => {
  const repo = new SubmissionRepository(path.join(root, 'a.db'));

  const wire = encodeMultipart('B', [
    { name: 'who', body: Buffer.from('alice') },
    {
      name: 'doc',
      filename: 'note.txt',
      contentType: 'text/plain',
      body: Buffer.from('persist me'),
    },
  ]);
  const { form, temp } = await parseToTemp(wire, 'commit-a');

  const result = await repo.commitAsync(form, (part) =>
    temp.readPartFile(part.tempPath!),
  );
  assert.equal(result.fieldCount, 1);
  assert.equal(result.fileCount, 1);
  assert.equal(result.totalSize, 5 + 10);

  const stored = repo.get(result.id)!;
  assert.equal(stored.parts.length, 2);
  assert.equal(stored.parts[0]!.value, 'alice');
  assert.equal(stored.parts[1]!.filename, 'note.txt');

  const blob = repo.getPartBlob(result.id, stored.parts[1]!.id)!;
  assert.equal(blob.toString(), 'persist me');

  // After commit the request temp directory is reclaimed.
  await temp.cleanup();
  await assert.rejects(stat(temp.directory), /ENOENT/);
  repo.close();
});

test('commit failure rolls everything back (no half-visible submission)', async () => {
  const repo = new SubmissionRepository(path.join(root, 'rollback.db'));
  const before = repo.count();

  const wire = encodeMultipart('B', [
    {
      name: 'doc',
      filename: 'note.txt',
      contentType: 'text/plain',
      body: Buffer.from('x'),
    },
  ]);
  const { form, temp } = await parseToTemp(wire, 'rollback');

  await assert.rejects(
    () =>
      repo.commitAsync(form, async () => {
        throw new Error('simulated blob read failure');
      }),
    (err: unknown) => isMultipartError(err) && err.code === 'DB_ERROR',
  );

  assert.equal(repo.count(), before);
  await temp.cleanup();
  repo.close();
});

test('reopening the database sees previously committed rows', async () => {
  const dbPath = path.join(root, 'persist.db');
  const repo1 = new SubmissionRepository(dbPath);
  const { form, temp } = await parseToTemp(
    encodeMultipart('B', [{ name: 'k', body: Buffer.from('v') }]),
    'persist',
  );
  await repo1.commitAsync(form, async () => Buffer.alloc(0));
  repo1.close();

  const repo2 = new SubmissionRepository(dbPath);
  assert.equal(repo2.count(), 1);
  assert.equal(repo2.list()[0]!.parts[0]!.value, 'v');
  repo2.close();
  await temp.cleanup();
});
