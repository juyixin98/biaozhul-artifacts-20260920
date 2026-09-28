import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { MultipartParser } from '../../src/protocol/parser.js';
import {
  createDefaultConfig,
  DEFAULT_LIMITS,
  DEFAULT_RESTRICTIONS,
  type MultipartLimits,
} from '../../src/protocol/config.js';
import { isMultipartError, type MultipartError } from '../../src/protocol/errors.js';
import type { ParsedForm } from '../../src/protocol/types.js';
import {
  allSplits,
  chunkSchedule,
  deterministicBytes,
  encodeMultipart,
  type EncodedPart,
} from '../helpers/encode.js';
import { CaptureSink, SinkRegistry } from '../helpers/harness.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const GOLDEN = path.resolve(HERE, '..', '..', 'fixtures', 'golden');

function sha256(buf: Buffer): string {
  return createHash('sha256').update(buf).digest('hex');
}

interface BuildOpts {
  boundaryDelimiter?: string;
  limits?: Partial<MultipartLimits>;
  strictCrlf?: boolean;
  sinks?: SinkRegistry;
}

function makeParser(opts: BuildOpts = {}): {
  parser: MultipartParser;
  sinks: SinkRegistry;
} {
  const sinks = opts.sinks ?? new SinkRegistry();
  const config = createDefaultConfig();
  const parser = new MultipartParser({
    boundaryDelimiter: opts.boundaryDelimiter ?? '--XYZ',
    limits: { ...config.limits, ...opts.limits },
    restrictions: DEFAULT_RESTRICTIONS,
    strictCrlf: opts.strictCrlf ?? true,
    createSink: sinks.factory,
  });
  return { parser, sinks };
}

/**
 * Feed a whole wire buffer (in fixed small chunks) and capture the first
 * error raised by EITHER write() or finish() — protocol violations surface as
 * soon as the offending bytes are buffered, not necessarily at finish().
 */
async function runWire(
  parser: MultipartParser,
  wire: Buffer,
  chunkSize = 3,
): Promise<{ ok: true; form: ParsedForm } | { ok: false; err: unknown }> {
  try {
    for (let pos = 0; pos < wire.length; pos += chunkSize) {
      await parser.write(wire.subarray(pos, Math.min(wire.length, pos + chunkSize)));
    }
    const form = await parser.finish();
    return { ok: true, form };
  } catch (err) {
    return { ok: false, err };
  }
}

async function expectWireError(
  parser: MultipartParser,
  wire: Buffer,
  errorClass: MultipartError['errorClass'],
  code: MultipartError['code'],
  chunkSize = 3,
): Promise<MultipartError> {
  const result = await runWire(parser, wire, chunkSize);
  if (result.ok) throw new Error(`expected ${errorClass}/${code} but parsing succeeded`);
  assert.ok(isMultipartError(result.err), `expected MultipartError, got ${String(result.err)}`);
  assert.equal(result.err.errorClass, errorClass);
  assert.equal(result.err.code, code);
  return result.err;
}

async function expectErrorClass(
  fn: () => Promise<unknown>,
  errorClass: MultipartError['errorClass'],
  code: MultipartError['code'],
): Promise<MultipartError> {
  try {
    await fn();
  } catch (err) {
    assert.ok(isMultipartError(err), `expected MultipartError, got ${String(err)}`);
    assert.equal(err.errorClass, errorClass, `errorClass mismatch: ${err.message}`);
    assert.equal(err.code, code, `code mismatch: ${err.message}`);
    return err as MultipartError;
  }
  throw new Error(`expected ${errorClass}/${code} but no error was thrown`);
}

// ---------------------------------------------------------------- golden vectors

interface GoldenManifest {
  vectors: Array<{
    id: string;
    file: string;
    boundary: string;
    expectedParts: Array<{
      name: string;
      kind: 'field' | 'file';
      filename?: string;
      contentType: string;
      size: number;
      sha256: string;
    }>;
  }>;
}

async function loadGolden(): Promise<GoldenManifest['vectors']> {
  const manifest = JSON.parse(
    await readFile(path.join(GOLDEN, 'manifest.json'), 'utf8'),
  ) as GoldenManifest;
  return manifest.vectors;
}

async function runGoldenVector(
  vector: GoldenManifest['vectors'][number],
  chunks: Buffer[],
): Promise<{ form: ParsedForm; sinks: SinkRegistry }> {
  const sinks = new SinkRegistry();
  const parser = new MultipartParser({
    boundaryDelimiter: `--${vector.boundary}`,
    limits: DEFAULT_LIMITS,
    restrictions: DEFAULT_RESTRICTIONS,
    createSink: sinks.factory,
  });
  for (const c of chunks) await parser.write(c);
  const form = await parser.finish();
  return { form, sinks };
}

test('golden vectors exist and parse in a single write', async (t) => {
  const vectors = await loadGolden();
  for (const vector of vectors) {
    await t.test(vector.id, async () => {
      const wire = await readFile(path.join(GOLDEN, vector.file));
      const sinks = new SinkRegistry();
      const parser = new MultipartParser({
        boundaryDelimiter: `--${vector.boundary}`,
        limits: DEFAULT_LIMITS,
        restrictions: DEFAULT_RESTRICTIONS,
        createSink: sinks.factory,
      });
      await parser.write(wire);
      const form = await parser.finish();
      assert.equal(form.parts.length, vector.expectedParts.length);
      vector.expectedParts.forEach((expected, i) => {
        const part = form.parts[i]!;
        assert.equal(part.meta.name, expected.name);
        assert.equal(part.meta.kind, expected.kind);
        assert.equal(part.size, expected.size);
        if (expected.filename !== undefined) {
          assert.equal(part.meta.filename, expected.filename);
        }
        const body = sinks.sinks[i]!.body();
        assert.equal(body.length, expected.size);
        assert.equal(sha256(body), expected.sha256);
      });
    });
  }
});

test('every 2-piece split point of every golden vector parses identically', async (t) => {
  const vectors = await loadGolden();
  let cases = 0;
  for (const vector of vectors) {
    const wire = await readFile(path.join(GOLDEN, vector.file));
    await t.test(`${vector.id} (${wire.length - 1} splits)`, async () => {
      for (const [a, b] of allSplits(wire)) {
        cases++;
        const sinks = new SinkRegistry();
        const parser = new MultipartParser({
          boundaryDelimiter: `--${vector.boundary}`,
          limits: DEFAULT_LIMITS,
          restrictions: DEFAULT_RESTRICTIONS,
          createSink: sinks.factory,
        });
        await parser.write(a);
        await parser.write(b);
        const form = await parser.finish();
        assert.equal(form.parts.length, vector.expectedParts.length);
        assert.equal(form.totalSize, vector.expectedParts.reduce((s, p) => s + p.size, 0));
        sinks.sinks.forEach((sink, i) => {
          assert.equal(sha256(sink.body()), vector.expectedParts[i]!.sha256);
        });
      }
    });
  }
  assert.ok(cases > 1000, `expected exhaustive coverage, ran ${cases}`);
});

test('seeded irregular chunk schedules (1..7 bytes) parse identically', async () => {
  const vectors = await loadGolden();
  for (const vector of vectors) {
    const wire = await readFile(path.join(GOLDEN, vector.file));
    for (const seed of [1, 7, 42, 999, 20260927]) {
      const chunks = chunkSchedule(wire, seed);
      const sinks = new SinkRegistry();
      const parser = new MultipartParser({
        boundaryDelimiter: `--${vector.boundary}`,
        limits: DEFAULT_LIMITS,
        restrictions: DEFAULT_RESTRICTIONS,
        createSink: sinks.factory,
      });
      for (const c of chunks) await parser.write(c);
      const form = await parser.finish();
      assert.equal(
        form.totalSize,
        vector.expectedParts.reduce((s, p) => s + p.size, 0),
        `${vector.id} seed ${seed}`,
      );
    }
  }
});

test('near-boundary bytes inside a body are never treated as a boundary', async () => {
  const vectors = await loadGolden();
  const vector = vectors.find((v) => v.id === 'v03-near-boundary-in-body')!;
  const wire = await readFile(path.join(GOLDEN, vector.file));
  const { form } = await runGoldenVector(vector, [wire]);
  assert.equal(form.parts.length, 2);
  assert.equal(form.parts[1]!.size, vector.expectedParts[1]!.size);
  assert.equal(form.parts[1]!.meta.name, 'blob');
});

// ---------------------------------------------------------------- framing negatives

const simpleParts: EncodedPart[] = [
  { name: 'a', body: Buffer.from('1') },
  {
    name: 'f',
    filename: 'a.txt',
    contentType: 'text/plain',
    body: Buffer.from('data'),
  },
];

test('missing closing delimiter fails with MISSING_TERMINATOR (INPUT_ERROR)', async () => {
  const wire = encodeMultipart('XYZ', simpleParts, { terminator: false });
  const { parser, sinks } = makeParser();
  const err = await expectWireError(parser, wire, 'INPUT_ERROR', 'MISSING_TERMINATOR');
  assert.equal(sinks.allDestroyed, true);
  assert.ok(err.message.includes('closing boundary'));
});

test('truncation right after the close dashes waits, then EOF is accepted leniently', async () => {
  const full = encodeMultipart('XYZ', [{ name: 'a', body: Buffer.from('x') }]);
  const head = full.subarray(0, full.length - 2); // strip final CRLF
  const { parser } = makeParser();
  await parser.write(head);
  const form = await parser.finish();
  assert.equal(form.parts.length, 1);
});

test('wrong opening boundary fails with MALFORMED_BOUNDARY', async () => {
  const { parser } = makeParser();
  await expectErrorClass(
    () => parser.write(Buffer.from('--OTHER\r\nContent-Disposition: form-data; name="a"\r\n\r\nx\r\n--OTHER--\r\n')),
    'INPUT_ERROR',
    'MALFORMED_BOUNDARY',
  );
});

test('close delimiter followed by garbage fails with MALFORMED_BOUNDARY', async () => {
  // Keep the "--boundary--" prefix but replace the required CRLF with junk.
  const wire = encodeMultipart('XYZ', [{ name: 'a', body: Buffer.from('x') }], {
    closeSuffix: Buffer.from('--X\r\n'),
  });
  const { parser } = makeParser();
  await expectWireError(parser, wire, 'INPUT_ERROR', 'MALFORMED_BOUNDARY');
});

test('bare LF in header section is rejected when strictCrlf is on', async () => {
  const wire = encodeMultipart('XYZ', simpleParts, { bareLfHeaders: true });
  const { parser, sinks } = makeParser();
  await expectWireError(parser, wire, 'INPUT_ERROR', 'BARE_LF');
  assert.equal(sinks.allDestroyed, true);
});

test('bare LF is tolerated when strictCrlf is explicitly disabled', async () => {
  const wire = encodeMultipart('XYZ', [{ name: 'a', body: Buffer.from('x') }], {
    bareLfHeaders: true,
  });
  const { parser } = makeParser({ strictCrlf: false });
  const result = await runWire(parser, wire);
  assert.equal(result.ok, true, result.ok ? '' : String(result.err));
  if (result.ok) assert.equal(result.form.parts.length, 1);
});

test('immediate close-delimiter with zero parts fails with EMPTY_BODY', async () => {
  const { parser } = makeParser();
  await parser.write(Buffer.from('--XYZ--\r\n'));
  await expectErrorClass(() => parser.finish(), 'INPUT_ERROR', 'EMPTY_BODY');
});

// ---------------------------------------------------------------- header rules

test('duplicated part header is rejected with DUPLICATE_HEADER', async () => {
  const wire = encodeMultipart('XYZ', [
    {
      name: 'a',
      body: Buffer.from('x'),
      duplicateHeader: 'Content-Type: text/csv',
      contentType: 'text/plain',
    },
  ]);
  const { parser } = makeParser();
  await expectWireError(parser, wire, 'INPUT_ERROR', 'DUPLICATE_HEADER');
});

test('duplicated disposition parameter is rejected with DUPLICATE_HEADER', async () => {
  const wire = encodeMultipart('XYZ', [
    {
      name: 'a',
      rawDisposition: 'Content-Disposition: form-data; name="a"; name="b"',
      body: Buffer.from('x'),
    },
  ]);
  const { parser } = makeParser();
  await expectWireError(parser, wire, 'INPUT_ERROR', 'DUPLICATE_HEADER');
});

test('missing Content-Disposition fails with MISSING_CONTENT_DISPOSITION', async () => {
  const wire = encodeMultipart('XYZ', [
    { name: 'a', body: Buffer.from('x'), omitDisposition: true, contentType: 'text/plain' },
  ]);
  const { parser } = makeParser();
  await expectWireError(parser, wire, 'INPUT_ERROR', 'MISSING_CONTENT_DISPOSITION');
});

test('unknown part header is rejected as UNSUPPORTED_HEADER', async () => {
  const wire = encodeMultipart('XYZ', [
    {
      name: 'a',
      body: Buffer.from('x'),
      extraHeaderLine: 'Content-Transfer-Encoding: base64',
    },
  ]);
  const { parser } = makeParser();
  await expectWireError(parser, wire, 'INPUT_ERROR', 'UNSUPPORTED_HEADER');
});

test('disposition without a name fails with MISSING_FIELD_NAME', async () => {
  const wire = encodeMultipart('XYZ', [
    {
      name: 'a',
      rawDisposition: 'Content-Disposition: form-data',
      body: Buffer.from('x'),
    },
  ]);
  const { parser } = makeParser();
  await expectWireError(parser, wire, 'INPUT_ERROR', 'MISSING_FIELD_NAME');
});

// ---------------------------------------------------------------- filename rules

const maliciousCases: Array<{ label: string; part: EncodedPart; reason: string }> = [
  {
    label: 'path traversal via ../',
    part: { name: 'f', filename: '../../etc/passwd', contentType: 'text/plain', body: Buffer.from('x') },
    reason: 'separator',
  },
  {
    label: 'windows path separator',
    part: { name: 'f', filename: '..\\evil.txt', contentType: 'text/plain', body: Buffer.from('x') },
    reason: 'separator',
  },
  {
    label: 'disallowed extension',
    part: { name: 'f', filename: 'payload.exe', contentType: 'application/octet-stream', body: Buffer.from('x') },
    reason: 'extension',
  },
  {
    label: 'dotfile',
    part: { name: 'f', filename: '.env', contentType: 'text/plain', body: Buffer.from('x') },
    reason: 'dotfile',
  },
  {
    label: 'NUL byte in filename',
    part: {
      name: 'f',
      filename: `ok\x00.txt`,
      contentType: 'text/plain',
      body: Buffer.from('x'),
    },
    reason: 'control-char',
  },
  {
    label: 'disallowed mime type',
    part: { name: 'f', filename: 'a.txt', contentType: 'application/x-msdownload', body: Buffer.from('x') },
    reason: 'mime',
  },
];

for (const { label, part, reason } of maliciousCases) {
  test(`malicious filename rejected: ${label}`, async () => {
    const wire = encodeMultipart('XYZ', [part]);
    const { parser, sinks } = makeParser();
    const err = await expectWireError(parser, wire, 'INPUT_ERROR', 'FILENAME_REJECTED');
    assert.equal((err.details as { reason?: string }).reason, reason);
    assert.equal(sinks.allDestroyed, true);
  });
}

test('filename* extended form overrides ordinary filename and decodes UTF-8', async () => {
  const wire = encodeMultipart('XYZ', [
    {
      name: 'f',
      filename: 'ascii.txt',
      filenameStar: "UTF-8''%E2%82%AC.txt",
      contentType: 'text/plain',
      body: Buffer.from('x'),
    },
  ]);
  const { parser } = makeParser();
  await parser.write(wire);
  const form = await parser.finish();
  assert.equal(form.parts[0]!.meta.filename, '€.txt');
});

// ---------------------------------------------------------------- limits

test('file part over the per-part quota fails with PART_TOO_LARGE', async () => {
  const body = Buffer.alloc(20, 0x61);
  const wire = encodeMultipart('XYZ', [
    { name: 'f', filename: 'a.txt', contentType: 'text/plain', body },
  ]);
  const { parser, sinks } = makeParser({ limits: { maxPartBytes: 10 } });
  const err = await expectWireError(parser, wire, 'RESOURCE_LIMIT', 'PART_TOO_LARGE');
  assert.ok((err.details.observed as number) >= 10);
  assert.equal(sinks.allDestroyed, true);
});

test('field over the field quota fails with FIELD_TOO_LARGE', async () => {
  const wire = encodeMultipart('XYZ', [{ name: 'a', body: Buffer.alloc(50, 0x78) }]);
  const { parser } = makeParser({ limits: { maxFieldBytes: 10 } });
  await expectWireError(parser, wire, 'RESOURCE_LIMIT', 'FIELD_TOO_LARGE');
});

test('aggregate over the total quota fails with TOTAL_TOO_LARGE', async () => {
  const wire = encodeMultipart('XYZ', [
    { name: 'a', body: Buffer.alloc(8, 0x61) },
    { name: 'b', body: Buffer.alloc(8, 0x62) },
  ]);
  const { parser } = makeParser({ limits: { maxFieldBytes: 100, maxTotalBytes: 10 } });
  await expectWireError(parser, wire, 'RESOURCE_LIMIT', 'TOTAL_TOO_LARGE');
});

test('header section over the header quota fails with HEADER_TOO_LONG', async () => {
  const longName = 'x'.repeat(200);
  const wire = encodeMultipart('XYZ', [{ name: longName, body: Buffer.from('v') }]);
  const { parser } = makeParser({ limits: { maxHeaderBytes: 64 } });
  await expectWireError(parser, wire, 'RESOURCE_LIMIT', 'HEADER_TOO_LONG');
});

test('part count over the cap fails with TOO_MANY_PARTS', async () => {
  const parts: EncodedPart[] = [];
  for (let i = 0; i < 5; i++) parts.push({ name: `p${i}`, body: Buffer.from('v') });
  const wire = encodeMultipart('XYZ', parts);
  const { parser } = makeParser({ limits: { maxParts: 3 } });
  await expectWireError(parser, wire, 'RESOURCE_LIMIT', 'TOO_MANY_PARTS');
});

test('limits are enforced while bytes stream, across chunk boundaries', async () => {
  const body = deterministicBytes(3, 4096);
  const wire = encodeMultipart('XYZ', [
    { name: 'f', filename: 'a.png', contentType: 'image/png', body },
  ]);
  const { parser } = makeParser({ limits: { maxPartBytes: 100, maxTotalBytes: 100000 } });
  try {
    for (const c of chunkSchedule(wire, 5, 1, 3)) await parser.write(c);
    await parser.finish();
  } catch (err) {
    assert.ok(isMultipartError(err) && err.errorClass === 'RESOURCE_LIMIT' && err.code === 'PART_TOO_LARGE');
    return;
  }
  throw new Error('expected PART_TOO_LARGE');
});

// ---------------------------------------------------------------- cancellation / state

test('upload abort destroys all sinks and later writes are STATE_CONFLICT', async () => {
  const wire = encodeMultipart('XYZ', [
    { name: 'a', body: Buffer.alloc(30, 0x61) },
    { name: 'f', filename: 'a.txt', contentType: 'text/plain', body: Buffer.alloc(30) },
  ]);
  const { parser, sinks } = makeParser();
  await parser.write(wire.subarray(0, 60)); // well inside the first part body
  await parser.abort();
  assert.ok(sinks.sinks.length >= 1);
  assert.equal(sinks.allDestroyed, true);
  await expectErrorClass(() => parser.write(Buffer.from('x')), 'STATE_CONFLICT', 'ALREADY_ABORTED');
  await expectErrorClass(() => parser.finish(), 'STATE_CONFLICT', 'ALREADY_ABORTED');
  await parser.abort(); // idempotent
});

test('writing after finish is a PARSER_FINISHED state conflict', async () => {
  const wire = encodeMultipart('XYZ', [{ name: 'a', body: Buffer.from('x') }]);
  const { parser } = makeParser();
  await parser.write(wire);
  await parser.finish();
  await expectErrorClass(() => parser.write(Buffer.from('x')), 'STATE_CONFLICT', 'PARSER_FINISHED');
});

test('a sink I/O failure surfaces as COMPUTE_ERROR/TEMP_IO_ERROR and cleans up', async () => {
  let destroyed = false;
  const parser = new MultipartParser({
    boundaryDelimiter: '--XYZ',
    limits: DEFAULT_LIMITS,
    restrictions: DEFAULT_RESTRICTIONS,
    createSink: () => ({
      write: () => Promise.reject(new Error('disk on fire')),
      end: () => ({ size: 0, sha256: '' }),
      destroy: () => {
        destroyed = true;
      },
    }),
  });
  const wire = encodeMultipart('XYZ', [{ name: 'a', body: Buffer.from('x') }]);
  try {
    // Feed the whole wire one byte at a time so a body chunk (and therefore
    // the failing sink write) is definitely reached.
    for (let pos = 0; pos < wire.length; pos++) {
      await parser.write(wire.subarray(pos, pos + 1));
    }
    await parser.finish();
  } catch (err) {
    assert.ok(isMultipartError(err));
    assert.equal(err.errorClass, 'COMPUTE_ERROR');
    assert.equal(err.code, 'TEMP_IO_ERROR');
    assert.equal(destroyed, true);
    return;
  }
  throw new Error('expected COMPUTE_ERROR/TEMP_IO_ERROR');
});

test('failure mid-stream does not produce a committed ParsedForm', async () => {
  const wire = encodeMultipart('XYZ', [
    { name: 'a', body: Buffer.from('ok') },
    { name: 'f', filename: 'evil.exe', body: Buffer.from('x') },
  ]);
  const { parser } = makeParser();
  const result = await runWire(parser, wire);
  assert.equal(result.ok, false);
  if (!result.ok) {
    assert.ok(isMultipartError(result.err) && result.err.code === 'FILENAME_REJECTED');
  }
});
