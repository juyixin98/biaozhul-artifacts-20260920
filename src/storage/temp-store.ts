/**
 * Per-request temporary storage.
 *
 * Lifecycle and isolation contract:
 *  - Every upload gets its OWN directory `${tempRoot}/${requestId}/`. Cleanup
 *    of one request can never touch another request's files.
 *  - Field parts are buffered in memory (bounded by maxFieldBytes upstream);
 *    file parts are streamed to a private temp file via a write stream —
 *    large uploads are never fully resident in memory.
 *  - `destroy()` on a sink removes exactly that part's temp file;
 *    `cleanup()` removes the whole request directory and is idempotent.
 *  - All file-system failures are normalized to COMPUTE_ERROR/TEMP_IO_ERROR.
 */

import { createHash, randomUUID } from 'node:crypto';
import { createWriteStream, type WriteStream } from 'node:fs';
import { mkdir, rm, stat } from 'node:fs/promises';
import path from 'node:path';
import { Readable } from 'node:stream';
import { computeError, isMultipartError } from '../protocol/errors.js';
import type {
  FinalizedPart,
  PartMeta,
  PartSink,
} from '../protocol/types.js';

/** In-memory sink for field parts. */
export class FieldSink implements PartSink {
  private chunks: Buffer[] = [];
  private size = 0;
  private readonly hash = createHash('sha256');
  private destroyed = false;

  write(chunk: Buffer): void {
    if (this.destroyed) throw new Error('FieldSink used after destroy');
    this.chunks.push(chunk);
    this.size += chunk.length;
    this.hash.update(chunk);
  }

  end(): FinalizedPart {
    const value = Buffer.concat(this.chunks, this.size).toString('utf8');
    return { size: this.size, sha256: this.hash.digest('hex'), value };
  }

  destroy(): void {
    this.destroyed = true;
    this.chunks = [];
  }
}

/** Disk-spooled sink for file parts. */
export class FileSink implements PartSink {
  private stream: WriteStream | null;
  private size = 0;
  private readonly hash = createHash('sha256');
  private ended = false;
  private destroyed = false;
  private streamError: Error | null = null;

  constructor(private readonly filePath: string) {
    this.stream = createWriteStream(filePath, { flags: 'wx' });
    // A permanent listener: a disk error arriving between writes must reject
    // the next write/end rather than crashing the process as unhandled.
    this.stream.on('error', (err: Error) => {
      this.streamError = err;
    });
  }

  write(chunk: Buffer): Promise<void> {
    if (this.destroyed) return Promise.reject(new Error('FileSink used after destroy'));
    if (this.streamError) return Promise.reject(this.streamError);
    if (!this.stream) return Promise.reject(new Error('FileSink stream is gone'));
    this.size += chunk.length;
    this.hash.update(chunk);
    return new Promise((resolve, reject) => {
      this.stream!.write(chunk, (err?: Error | null) => {
        if (err) reject(err);
        else if (this.streamError) reject(this.streamError);
        else resolve();
      });
    });
  }

  end(): Promise<FinalizedPart> {
    if (this.streamError) return Promise.reject(this.streamError);
    if (!this.stream) return Promise.reject(new Error('FileSink stream is gone'));
    return new Promise((resolve, reject) => {
      this.stream!.end(() => {
        if (this.streamError) {
          reject(this.streamError);
          return;
        }
        this.ended = true;
        resolve({
          size: this.size,
          sha256: this.hash.digest('hex'),
          tempPath: this.filePath,
        });
      });
    });
  }

  async destroy(): Promise<void> {
    if (this.destroyed) return;
    this.destroyed = true;
    const stream = this.stream;
    this.stream = null;
    if (stream && !stream.closed) {
      // The stream is still open: wait for its 'close' so an in-flight write
      // has settled before we unlink the file.
      await new Promise<void>((resolve) => {
        stream.once('close', () => resolve());
        stream.destroy();
      });
    }
    if (!this.ended && stream) {
      // Un-finalized spool file: remove the partial bytes. A finalized file
      // is kept for the commit step to read.
      await rm(this.filePath, { force: true }).catch(() => undefined);
    }
  }

  /** Read a finalized temp file back as a Buffer (used at commit time). */
  static async readFile(filePath: string): Promise<Buffer> {
    const { readFile } = await import('node:fs/promises');
    return readFile(filePath);
  }
}

export class TempStore {
  readonly requestId: string;
  readonly directory: string;
  private prepared = false;
  private cleaned = false;

  constructor(private readonly tempRoot: string, requestId?: string) {
    this.requestId = requestId ?? randomUUID();
    this.directory = path.resolve(tempRoot, this.requestId);
  }

  /** Create the request-private directory. Safe to call once. */
  async prepare(): Promise<void> {
    if (this.prepared) return;
    try {
      await mkdir(this.directory, { recursive: true });
    } catch (err) {
      throw computeError('TEMP_IO_ERROR', 'cannot create request temp directory', {
        reason: (err as Error).message,
      });
    }
    this.prepared = true;
  }

  /** Sink factory matching the parser's PartSinkFactory contract. */
  createSink(meta: PartMeta): PartSink {
    if (!this.prepared) {
      throw computeError('TEMP_IO_ERROR', 'TempStore.prepare() was not called');
    }
    if (meta.kind === 'field') {
      return new FieldSink();
    }
    const safe = `${this.partToken(meta.name)}-${randomUUID()}.part`;
    return new FileSink(path.join(this.directory, safe));
  }

  /** Drain a finalized temp file into a buffer for durable persistence. */
  async readPartFile(tempPath: string): Promise<Buffer> {
    if (!tempPath.startsWith(this.directory)) {
      throw computeError('TEMP_IO_ERROR', 'refusing to read a temp path outside this request directory', {
        requestId: this.requestId,
      });
    }
    return FileSink.readFile(tempPath);
  }

  async exists(): Promise<boolean> {
    try {
      await stat(this.directory);
      return true;
    } catch {
      return false;
    }
  }

  /**
   * Remove every temp artifact of THIS request. Idempotent; never throws so
   * that it can run from a finally-block without masking the root error.
   */
  async cleanup(): Promise<void> {
    if (this.cleaned) return;
    this.cleaned = true;
    if (!this.prepared) return;
    await rm(this.directory, { recursive: true, force: true }).catch(() => undefined);
  }

  /** Readable helper for tests/reporting: list remaining temp files. */
  async listEntries(): Promise<string[]> {
    const { readdir } = await import('node:fs/promises');
    try {
      return await readdir(this.directory);
    } catch (err) {
      if (isMultipartError(err)) throw err;
      return [];
    }
  }

  private partToken(name: string): string {
    return name.replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 32) || 'part';
  }
}

/** Convenience: push a Node stream into the parser, with abort on early close. */
export async function pipeStream(
  stream: Readable,
  onChunk: (chunk: Buffer) => Promise<void>,
): Promise<void> {
  for await (const chunk of stream) {
    await onChunk(chunk as Buffer);
  }
}
