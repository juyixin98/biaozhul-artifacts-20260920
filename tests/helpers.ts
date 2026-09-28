/**
 * In-memory ParserSink used by parser-level tests. It stores part bytes in
 * Buffers and computes SHA-256 via node:crypto — independent of the Python
 * hashlib digests recorded in the golden manifest.
 */

import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { mkdirSync, rmSync } from 'node:fs';
import type { PartHandler, ParserSink } from '../src/protocol/multipart-parser.js';
import type { PartMeta } from '../src/protocol/part-headers.js';
import type { PartInfo } from '../src/protocol/types.js';

export interface CapturedPart {
  info: PartInfo;
  data: Buffer;
}

export class MemorySink implements ParserSink {
  readonly parts: CapturedPart[] = [];
  private chunks: Buffer[] = [];
  private hash = createHash('sha256');
  private size = 0;
  private meta: PartMeta | null = null;

  openPart(meta: PartMeta): PartHandler {
    this.meta = meta;
    this.chunks = [];
    this.hash = createHash('sha256');
    this.size = 0;
    return {
      write: (chunk) => {
        this.chunks.push(Buffer.from(chunk));
        this.hash.update(chunk);
        this.size += chunk.length;
      },
      end: () => ({ size: this.size, sha256: this.hash.digest('hex') }),
      discard: () => {}
    };
  }

  closePart(info: PartInfo): void {
    this.parts.push({ info, data: Buffer.concat(this.chunks) });
  }
}

/** Feed `data` to a writable-like target in fixed-size chunks. */
export function feedInChunks(write: (chunk: Buffer) => void, data: Buffer, chunkSize: number): void {
  for (let off = 0; off < data.length; off += chunkSize) {
    write(data.subarray(off, Math.min(off + chunkSize, data.length)));
  }
}

/** Feed data in pseudo-random chunk sizes driven by a seeded PRNG. */
export function feedRandomChunks(
  write: (chunk: Buffer) => void,
  data: Buffer,
  seed: number
): number[] {
  let state = seed >>> 0;
  const sizes: number[] = [];
  let off = 0;
  while (off < data.length) {
    // xorshift32
    state ^= state << 13;
    state ^= state >>> 17;
    state ^= state << 5;
    state = state >>> 0;
    const size = 1 + (state % 17); // 1..17 bytes
    const end = Math.min(off + size, data.length);
    write(data.subarray(off, end));
    sizes.push(end - off);
    off = end;
  }
  return sizes;
}

export function uniqueTmpDir(label: string): string {
  const dir = join(tmpdir(), `mp-test-${label}-${process.pid}-${Math.random().toString(16).slice(2, 10)}`);
  mkdirSync(dir, { recursive: true });
  return dir;
}

export function removeDir(dir: string): void {
  rmSync(dir, { recursive: true, force: true });
}
