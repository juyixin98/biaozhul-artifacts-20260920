/**
 * Golden-vector integrity checks that deliberately DO NOT import the parser.
 *
 * They confirm the fixtures on disk are byte-for-byte what the independent
 * generator claims (whole-wire SHA-256, length) and that the manifest's
 * expected per-part hashes/sizes are self-consistent with the encoded wire
 * structure. Parsing these vectors happens elsewhere (parser.test.ts); this
 * file guards the oracle itself.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const GOLDEN = path.resolve(HERE, '..', '..', 'fixtures', 'golden');

interface ExpectedPart {
  name: string;
  kind: 'field' | 'file';
  filename?: string;
  contentType: string;
  size: number;
  sha256: string;
}
interface Vector {
  id: string;
  description: string;
  file: string;
  boundary: string;
  totalBodyBytes: number;
  wireLength: number;
  wireSha256: string;
  expectedParts: ExpectedPart[];
}

function sha256(buf: Buffer): string {
  return createHash('sha256').update(buf).digest('hex');
}

const manifest = JSON.parse(
  await readFile(path.join(GOLDEN, 'manifest.json'), 'utf8'),
) as { vectors: Vector[] };

test('manifest lists five deterministic vectors', () => {
  assert.equal(manifest.vectors.length, 5);
  const ids = manifest.vectors.map((v) => v.id);
  assert.deepEqual(ids, [
    'v01-field-and-file',
    'v02-unicode-filename-star',
    'v03-near-boundary-in-body',
    'v04-quoted-parameters',
    'v05-duplicate-names-and-binary',
  ]);
});

for (const vector of manifest.vectors) {
  test(`wire bytes of ${vector.id} match their recorded SHA-256 and length`, async () => {
    const wire = await readFile(path.join(GOLDEN, vector.file));
    assert.equal(wire.length, vector.wireLength);
    assert.equal(sha256(wire), vector.wireSha256);
    // The terminal delimiter must actually be present in every happy vector.
    const terminator = Buffer.from(`--${vector.boundary}--`, 'ascii');
    assert.notEqual(wire.indexOf(terminator), -1);
  });

  test(`${vector.id} manifest part sizes sum to totalBodyBytes`, () => {
    const sum = vector.expectedParts.reduce((s, p) => s + p.size, 0);
    assert.equal(sum, vector.totalBodyBytes);
  });

  test(`${vector.id} manifest digests are valid 64-char lowercase hex`, () => {
    for (const part of vector.expectedParts) {
      assert.match(part.sha256, /^[0-9a-f]{64}$/);
    }
  });
}

test('v03 vector embeds a near (non-terminating) delimiter inside a body', async () => {
  const vector = manifest.vectors.find((v) => v.id === 'v03-near-boundary-in-body')!;
  const wire = await readFile(path.join(GOLDEN, vector.file));
  // The body contains CRLF "--" boundary immediately followed by 'X', which
  // must NOT be treated as a boundary. It occurs before the real terminator.
  const near = Buffer.from(`\r\n--${vector.boundary}X`, 'ascii');
  const nearIdx = wire.indexOf(near);
  const terminalIdx = wire.indexOf(Buffer.from(`--${vector.boundary}--`, 'ascii'));
  assert.ok(nearIdx >= 0);
  assert.ok(terminalIdx > nearIdx);
});
