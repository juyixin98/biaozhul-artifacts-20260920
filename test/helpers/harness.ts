/**
 * Parser test harness: an independent in-memory sink factory.
 * Records every write and every destroy() call so tests can assert exact
 * byte routing and resource reclamation without touching the file system.
 */

import type { PartSink, PartMeta, FinalizedPart } from '../../src/protocol/types.js';

export class CaptureSink implements PartSink {
  readonly chunks: Buffer[] = [];
  size = 0;
  destroyed = false;
  ended = false;

  constructor(readonly meta: PartMeta) {}

  write(chunk: Buffer): void {
    this.chunks.push(Buffer.from(chunk));
    this.size += chunk.length;
  }

  end(): FinalizedPart {
    this.ended = true;
    return { size: this.size, sha256: 'not-hashed' };
  }

  destroy(): void {
    this.destroyed = true;
  }

  body(): Buffer {
    return Buffer.concat(this.chunks);
  }
}

export class SinkRegistry {
  readonly sinks: CaptureSink[] = [];

  factory = (meta: PartMeta): PartSink => {
    const sink = new CaptureSink(meta);
    this.sinks.push(sink);
    return sink;
  };

  get destroyedCount(): number {
    return this.sinks.filter((s) => s.destroyed).length;
  }

  get allDestroyed(): boolean {
    return this.sinks.every((s) => s.destroyed);
  }
}

/** Feed a complete buffer to a parser using a fixed chunk size. */
export async function feed(
  parser: { write(c: Buffer): Promise<void>; finish(): Promise<unknown> },
  wire: Buffer,
  chunkSize: number,
): Promise<void> {
  for (let pos = 0; pos < wire.length; pos += chunkSize) {
    await parser.write(wire.subarray(pos, Math.min(wire.length, pos + chunkSize)));
  }
  await parser.finish();
}

/** Feed using an explicit list of chunks (permits boundary-aligned splits). */
export async function feedChunks(
  parser: { write(c: Buffer): Promise<void>; finish(): Promise<unknown> },
  chunks: Buffer[],
): Promise<void> {
  for (const c of chunks) await parser.write(c);
  await parser.finish();
}
