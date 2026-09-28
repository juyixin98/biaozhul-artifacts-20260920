/**
 * Golden byte-vector generator.
 *
 * Run with: npm run golden:generate
 *
 * Produces fixtures/golden/*.bin plus manifest.json. Every expected value
 * (sizes, SHA-256 digests) is computed here with node:crypto directly — the
 * production parser never participates, so these are an independent oracle.
 */

import { createHash } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  deterministicBytes,
  encodeMultipart,
  type EncodedPart,
} from '../test/helpers/encode.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.resolve(HERE, '..', 'fixtures', 'golden');
const BOUNDARY = '----goldenBoundary42';

function sha256(buf: Buffer): string {
  return createHash('sha256').update(buf).digest('hex');
}

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
  /** SHA-256 of the complete on-disk wire file — verified without the parser. */
  wireSha256: string;
  expectedParts: ExpectedPart[];
}

function partExpectation(p: EncodedPart): ExpectedPart {
  const isFile = p.filename !== undefined || p.filenameStar !== undefined;
  return {
    name: p.name,
    kind: isFile ? 'file' : 'field',
    ...(p.filenameStar !== undefined
      ? { filename: decodeURIComponent(p.filenameStar.split("'")[2]!) }
      : p.filename !== undefined
        ? { filename: p.filename }
        : {}),
    contentType: p.contentType ?? (isFile ? 'application/octet-stream' : 'text/plain'),
    size: p.body.length,
    sha256: sha256(p.body),
  };
}

async function emit(vector: {
  id: string;
  description: string;
  parts: EncodedPart[];
  opts?: Parameters<typeof encodeMultipart>[2];
}): Promise<Vector> {
  const wire = encodeMultipart(BOUNDARY, vector.parts, vector.opts);
  const file = `${vector.id}.bin`;
  await writeFile(path.join(OUT_DIR, file), wire);
  const totalBodyBytes = vector.parts.reduce((sum, p) => sum + p.body.length, 0);
  return {
    id: vector.id,
    description: vector.description,
    file,
    boundary: BOUNDARY,
    totalBodyBytes,
    wireLength: wire.length,
    wireSha256: sha256(wire),
    expectedParts: vector.parts.map(partExpectation),
  };
}

async function main(): Promise<void> {
  await mkdir(OUT_DIR, { recursive: true });

  // 1. Minimal happy path: one field, one ASCII file.
  const fieldBody = Buffer.from('alice@example.com', 'utf8');
  const fileBody = Buffer.from('hello golden file\n', 'utf8');

  // 2. Unicode via filename* (€ rates), UTF-8 field value.
  const unicodeField = Buffer.from('金额=12.50', 'utf8');
  const unicodeFile = Buffer.from('EURO rate export\n', 'utf8');
  const extFilename = "UTF-8''" + encodeURIComponent('€-rates.txt');

  // 3. Binary part whose body contains a near-boundary: the real delimiter
  //    prefixed by an extra byte so it must NOT split there.
  const near = Buffer.concat([
    deterministicBytes(7, 40),
    Buffer.from(`\r\n--${BOUNDARY}X`, 'ascii'),
    deterministicBytes(31, 5),
  ]);

  // 4. Quoted parameters exercise: filename with semicolon, space, escaped
  //    quote and backslash. The encoder emits quoted-pair escapes; the parser
  //    must unescape them back to the literal name.
  const quotedName = 'a;b "c".txt';
  const quotedBody = Buffer.from('quoted;param;body', 'utf8');

  // 5. Two same-named fields (legal HTML form shape) plus a larger binary file
  //    whose bytes also contain CRLF and double dashes.
  const multi1 = Buffer.from('v1', 'utf8');
  const multi2 = Buffer.from('v2', 'utf8');
  const bigBinary = deterministicBytes(20260927, 99);

  const vectors = await Promise.all([
    emit({
      id: 'v01-field-and-file',
      description: 'one text field and one text file',
      parts: [
        { name: 'email', body: fieldBody },
        {
          name: 'upload',
          filename: 'hello.txt',
          contentType: 'text/plain',
          body: fileBody,
        },
      ],
    }),
    emit({
      id: 'v02-unicode-filename-star',
      description: 'UTF-8 field plus RFC 5987 filename* with a euro sign',
      parts: [
        { name: 'label', body: unicodeField },
        {
          name: 'report',
          filename: 'rates.txt',
          filenameStar: extFilename,
          contentType: 'text/plain',
          body: unicodeFile,
        },
      ],
    }),
    emit({
      id: 'v03-near-boundary-in-body',
      description: 'binary body embeds a delimiter-like sequence with an extra byte',
      parts: [
        { name: 'note', body: Buffer.from('before', 'utf8') },
        {
          name: 'blob',
          filename: 'blob.pdf',
          contentType: 'application/pdf',
          body: near,
        },
      ],
    }),
    emit({
      id: 'v04-quoted-parameters',
      description: 'filename uses quoting, semicolons and backslash escapes',
      parts: [
        {
          name: 'doc',
          filename: quotedName,
          contentType: 'text/plain',
          body: quotedBody,
        },
      ],
    }),
    emit({
      id: 'v05-duplicate-names-and-binary',
      description: 'two same-named fields and a 99-byte seeded binary file',
      parts: [
        { name: 'tag', body: multi1 },
        { name: 'tag', body: multi2 },
        {
          name: 'pack',
          filename: 'pack.png',
          contentType: 'image/png',
          body: bigBinary,
        },
      ],
    }),
  ]);

  // Fixed, non-wall-clock marker so regeneration is byte-for-byte identical.
  const manifest = {
    generatedAt: '2026-09-27T00:00:00Z',
    generator: 'scripts/gen-golden.ts (independent encoder + node:crypto)',
    vectors,
  };
  await writeFile(
    path.join(OUT_DIR, 'manifest.json'),
    `${JSON.stringify(manifest, null, 2)}\n`,
  );
  process.stdout.write(`wrote ${vectors.length} golden vectors to ${OUT_DIR}\n`);
}

main().catch((err: unknown) => {
  process.stderr.write(`golden generation failed: ${(err as Error).message}\n`);
  process.exit(1);
});
