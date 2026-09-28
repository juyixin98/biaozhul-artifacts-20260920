/**
 * Golden-byte conformance tests.
 *
 * Source of truth: fixtures/golden/golden-manifest.json + .bin files, produced
 * by scripts/build_golden.py using Python stdlib only (independent hashlib
 * digests, hand-concatenated bytes). This suite never generates expectations
 * with the code under test.
 *
 * Every positive vector is replayed many times with different chunk
 * boundaries, exhaustively traversing every two-write split point plus random
 * chunkings, because a delimiter may straddle input chunks.
 */

import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { MultipartError } from '../src/protocol/errors.js';
import { MultipartParser, type ParserSink } from '../src/protocol/multipart-parser.js';
import type { PartMeta } from '../src/protocol/part-headers.js';
import { DEFAULT_FILE_POLICY, DEFAULT_LIMITS, type Limits } from '../src/protocol/types.js';
import { enforceFilePolicy } from '../src/storage/policy.js';
import { MemorySink, feedInChunks, feedRandomChunks } from './helpers.js';

/**
 * Negative vectors include storage-policy rejections (e.g. traversal
 * filenames), which the pure parser deliberately leaves to its sink. This sink
 * applies the same policy as the real UploadSession.
 */
class PolicyMemorySink extends MemorySink {
  override openPart(meta: PartMeta): ReturnType<MemorySink['openPart']> {
    if (meta.isFile) enforceFilePolicy(meta, DEFAULT_FILE_POLICY);
    return super.openPart(meta);
  }
}

const FIXTURE_DIR = new URL('../fixtures/golden/', import.meta.url);

interface ExpectedPart {
  index: number;
  name: string;
  isFile: boolean;
  filename: string | null;
  filenameStar?: string | null;
  filenameStarCharset?: string | null;
  contentType: string | null;
  size: number;
  sha256: string;
  value?: string;
}

interface ExpectedCounters {
  bodyBytes: number;
  headerBytes: number;
  parts: number;
  fields: number;
  files: number;
}

interface PositiveVector {
  id: string;
  boundary: string;
  file: string;
  parts: ExpectedPart[];
  counters: ExpectedCounters;
  wireLength: number;
}

interface NegativeVector {
  id: string;
  boundary: string;
  file: string;
  errorCode: string;
  errorClass: string;
  wireLength: number;
}

const manifest = JSON.parse(
  readFileSync(new URL('golden-manifest.json', FIXTURE_DIR), 'utf8')
) as { positive: PositiveVector[]; negative: NegativeVector[] };

function fixtureUrl(file: string): URL {
  return new URL(file, FIXTURE_DIR);
}

function runParse(
  data: Buffer,
  boundary: string,
  sink: ParserSink,
  limits: Limits = DEFAULT_LIMITS
): ReturnType<MultipartParser['end']> {
  const parser = new MultipartParser(boundary, limits, sink);
  parser.write(data);
  return parser.end();
}

function assertPositiveVector(v: PositiveVector, data: Buffer, label: string): void {
  const sink = new MemorySink();
  const result = runParse(data, v.boundary, sink);

  expect(result.parts.length, `${label}: part count`).toBe(v.parts.length);
  expect(result.counters.bodyBytes, `${label}: bodyBytes`).toBe(v.counters.bodyBytes);
  expect(result.counters.headerBytes, `${label}: headerBytes`).toBe(v.counters.headerBytes);
  expect(result.counters.parts, `${label}: parts counter`).toBe(v.counters.parts);
  expect(result.counters.fields, `${label}: fields counter`).toBe(v.counters.fields);
  expect(result.counters.files, `${label}: files counter`).toBe(v.counters.files);
  expect(result.counters.wireBytes, `${label}: wireBytes`).toBe(v.wireLength);

  v.parts.forEach((expected, i) => {
    const captured = sink.parts[i]!;
    const info = captured.info;
    const ctx = `${label} part#${expected.index}`;
    expect(info.index, ctx).toBe(expected.index);
    expect(info.name, ctx).toBe(expected.name);
    expect(info.filename, ctx).toStrictEqual(expected.filename);
    expect(info.contentType, ctx).toStrictEqual(expected.contentType);
    expect(info.size, `${ctx} size`).toBe(expected.size);
    expect(captured.data.length, `${ctx} captured length`).toBe(expected.size);
    expect(info.sha256, `${ctx} sha256`).toBe(expected.sha256);
    if (expected.filenameStar !== undefined) {
      expect(info.filenameStar, `${ctx} filenameStar`).toStrictEqual(expected.filenameStar);
      expect(info.filenameStarCharset, `${ctx} filenameStarCharset`).toBe(
        expected.filenameStarCharset ?? 'UTF-8'
      );
    }
    if (expected.value !== undefined) {
      expect(captured.data.toString('utf8'), `${ctx} field value`).toBe(expected.value);
    }
  });
}

function assertNegativeVector(v: NegativeVector, data: Buffer, label: string): void {
  const sink = new PolicyMemorySink();
  const parser = new MultipartParser(v.boundary, DEFAULT_LIMITS, sink);
  try {
    parser.write(data);
    parser.end();
    throw new Error(`${label}: expected error ${v.errorCode} but parse succeeded`);
  } catch (err) {
    expect(err, `${label}: expected MultipartError`).toBeInstanceOf(MultipartError);
    const me = err as MultipartError;
    expect(me.code, `${label}: error code`).toBe(v.errorCode);
    expect(me.errorClass, `${label}: error class`).toBe(v.errorClass);
    expect(me.httpStatus, `${label}: http status`).toBeGreaterThanOrEqual(400);
  }
}

describe('golden positive vectors — single write', () => {
  for (const v of manifest.positive) {
    it(v.id, () => {
      const data = readFileSync(fixtureUrl(v.file));
      expect(data.length).toBe(v.wireLength);
      assertPositiveVector(v, data, v.id);
    });
  }
});

describe('golden positive vectors — every two-write split point', () => {
  for (const v of manifest.positive) {
    const data = readFileSync(fixtureUrl(v.file));
    // One test per split offset so failures name the exact cut point.
    for (let cut = 0; cut <= data.length; cut++) {
      it(`${v.id} cut@${cut}`, () => {
        const sink = new MemorySink();
        const parser = new MultipartParser(v.boundary, DEFAULT_LIMITS, sink);
        parser.write(data.subarray(0, cut));
        parser.write(data.subarray(cut));
        parser.end();
        assertPositiveVector(v, data, `${v.id} cut@${cut}`);
      });
    }
  }
});

describe('golden positive vectors — fixed chunk sizes 1..8 and 33', () => {
  const sizes = [1, 2, 3, 4, 5, 6, 7, 8, 33];
  for (const v of manifest.positive) {
    for (const size of sizes) {
      it(`${v.id} chunks=${size}`, () => {
        const sink = new MemorySink();
        const parser = new MultipartParser(v.boundary, DEFAULT_LIMITS, sink);
        feedInChunks((c) => parser.write(c), readFileSync(fixtureUrl(v.file)), size);
        parser.end();
        assertPositiveVector(v, readFileSync(fixtureUrl(v.file)), `${v.id} chunks=${size}`);
      });
    }
  }
});

describe('golden positive vectors — randomized chunkings (5 seeds)', () => {
  for (const v of manifest.positive) {
    for (const seed of [1, 7, 42, 12345, 0x9e3779b9]) {
      it(`${v.id} seed=${seed}`, () => {
        const sink = new MemorySink();
        const parser = new MultipartParser(v.boundary, DEFAULT_LIMITS, sink);
        const usedSizes = feedRandomChunks(
          (c) => parser.write(c),
          readFileSync(fixtureUrl(v.file)),
          seed
        );
        parser.end();
        assertPositiveVector(v, readFileSync(fixtureUrl(v.file)), `${v.id} seed=${seed}`);
        expect(usedSizes.length).toBeGreaterThan(0);
      });
    }
  }
});

describe('golden negative vectors', () => {
  for (const v of manifest.negative) {
    it(v.id, () => {
      assertNegativeVector(v, readFileSync(fixtureUrl(v.file)), v.id);
    });

    it(`${v.id} — same failure under 1-byte chunking`, () => {
      const data = readFileSync(fixtureUrl(v.file));
      const sink = new PolicyMemorySink();
      const parser = new MultipartParser(v.boundary, DEFAULT_LIMITS, sink);
      try {
        feedInChunks((c) => parser.write(c), data, 1);
        parser.end();
        throw new Error('expected error');
      } catch (err) {
        expect(err).toBeInstanceOf(MultipartError);
        expect((err as MultipartError).code).toBe(v.errorCode);
        expect((err as MultipartError).errorClass).toBe(v.errorClass);
      }
    });
  }
});

describe('golden positive vectors — boundary-shape bytes never false-split', () => {
  const v = manifest.positive.find((x) => x.id === 'v3-boundary-decoys-in-body')!;
  it('reassembles the exact 199-byte body despite 7 invalid delimiter candidates', () => {
    const sink = new MemorySink();
    const parser = new MultipartParser(v.boundary, DEFAULT_LIMITS, sink);
    feedInChunks((c) => parser.write(c), readFileSync(fixtureUrl(v.file)), 1);
    const result = parser.end();
    const falseCandidates = parser.getEvents().filter((e) => e.t === 'candidate' && e.kind === 'false');
    // The 7 decoys prefixed with CRLF are fully-formed false candidates.
    expect(falseCandidates.length).toBe(7);
    expect(result.parts[0]!.size).toBe(199);
    expect(sink.parts[0]!.data.subarray(0, 5).toString()).toBe('start');
    expect(sink.parts[0]!.data.subarray(-8).toString()).toBe('real-end');
  });
});
