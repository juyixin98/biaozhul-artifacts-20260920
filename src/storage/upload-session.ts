/**
 * Per-request upload session — the storage adapter (ParserSink).
 *
 * Lifecycle of request bytes:
 *
 *   openPart -> (write*) -> end -> closePart   per part, into a REQUEST-LOCAL
 *                                               temp directory
 *   parser.end() (terminating boundary validated)
 *   commit(): rename temp files into the permanent files dir, then publish one
 *             atomic SQLite transaction
 *
 * On ANY earlier failure (parse error, limit, cancel), `discard()` removes only
 * this request's temp directory. Permanent files and committed rows never
 * exist before the terminating boundary has been validated.
 */

import { createHash, randomBytes } from 'node:crypto';
import { closeSync, mkdirSync, openSync, renameSync, rmSync, writeSync } from 'node:fs';
import { join } from 'node:path';
import { ErrorCode, MultipartError, wrapIoError } from '../protocol/errors.js';
import type { PartHandler, ParserSink, ParseCounters } from '../protocol/multipart-parser.js';
import type { PartMeta } from '../protocol/part-headers.js';
import type { FilePolicy, Limits, PartInfo } from '../protocol/types.js';
import { enforceFilePolicy } from './policy.js';
import type { StoredPartRow, SubmissionRecord, SubmissionStore } from './submission-store.js';

interface OpenFile {
  kind: 'file';
  meta: PartMeta;
  storedName: string;
  tmpPath: string;
  fd: number;
  closed: boolean;
  hash: ReturnType<typeof createHash>;
  size: number;
}

interface OpenField {
  kind: 'field';
  meta: PartMeta;
  chunks: Buffer[];
  hash: ReturnType<typeof createHash>;
  size: number;
}

type OpenPart = OpenFile | OpenField;

interface FinalizedFile {
  info: PartInfo;
  tmpPath: string;
  finalPath: string;
  storedName: string;
}

export interface SessionOptions {
  runId: string;
  tmpRoot: string;
  filesDir: string;
  limits: Limits;
  policy: FilePolicy;
  requireFilePart: boolean;
  rejectDuplicateNames: boolean;
}

export class UploadSession implements ParserSink {
  readonly runId: string;
  readonly tmpDir: string;
  private readonly opts: SessionOptions;
  private readonly filesDir: string;
  private current: OpenPart | null = null;
  private readonly usedNames = new Set<string>();
  private readonly files: FinalizedFile[] = [];
  private readonly fieldRows: StoredPartRow[] = [];
  private discarded = false;
  private committed = false;
  private lastInfo: PartInfo | null = null;

  constructor(opts: SessionOptions) {
    this.opts = opts;
    this.runId = opts.runId;
    this.filesDir = opts.filesDir;
    this.tmpDir = join(opts.tmpRoot, opts.runId);
    try {
      mkdirSync(this.tmpDir, { recursive: true });
      mkdirSync(this.filesDir, { recursive: true });
    } catch (err) {
      throw wrapIoError(err, `creating upload directories for ${this.runId}`);
    }
  }

  // ---- ParserSink ----------------------------------------------------------

  openPart(meta: PartMeta): PartHandler {
    if (this.discarded) {
      throw new MultipartError(ErrorCode.PARSER_ABORTED, 'session was discarded', { runId: this.runId });
    }
    if (this.opts.rejectDuplicateNames && this.usedNames.has(meta.name)) {
      throw new MultipartError(
        ErrorCode.DUPLICATE_PART_NAME,
        `duplicate part name "${meta.name}"; each form field name may appear only once`,
        { part: meta.name }
      );
    }
    this.usedNames.add(meta.name);

    if (meta.isFile) {
      const { extension } = enforceFilePolicy(meta, this.opts.policy);
      const storedName = `p${meta.index}-${randomBytes(12).toString('hex')}${extension}`;
      const tmpPath = join(this.tmpDir, storedName);
      let fd: number;
      try {
        fd = openSync(tmpPath, 'w');
      } catch (err) {
        throw wrapIoError(err, `opening temp file for part "${meta.name}"`);
      }
      const open: OpenFile = {
        kind: 'file',
        meta,
        storedName,
        tmpPath,
        fd,
        closed: false,
        hash: createHash('sha256'),
        size: 0
      };
      this.current = open;
      return this.handler(open);
    }

    const open: OpenField = {
      kind: 'field',
      meta,
      chunks: [],
      hash: createHash('sha256'),
      size: 0
    };
    this.current = open;
    return this.handler(open);
  }

  private handler(open: OpenPart): PartHandler {
    return {
      write: (chunk: Buffer): void => {
        open.size += chunk.length;
        open.hash.update(chunk);
        if (open.kind === 'file') {
          try {
            writeSync(open.fd, chunk);
          } catch (err) {
            throw wrapIoError(err, `writing temp file for part "${open.meta.name}"`);
          }
        } else {
          open.chunks.push(Buffer.from(chunk));
        }
      },
      end: () => ({ size: open.size, sha256: open.hash.digest('hex') }),
      discard: () => {
        if (open.kind === 'file') {
          this.closeFd(open);
        }
      }
    };
  }

  /** Close an open file descriptor at most once. */
  private closeFd(open: OpenFile, partName?: string): void {
    if (open.closed) return;
    open.closed = true;
    try {
      closeSync(open.fd);
    } catch (err) {
      if (partName !== undefined) {
        throw wrapIoError(err, `closing temp file for part "${partName}"`);
      }
      // discard path: best effort
    }
  }

  closePart(info: PartInfo): void {
    const open = this.current;
    if (!open) throw new MultipartError(ErrorCode.PART_NOT_OPEN, 'closePart without openPart', { part: info.name });
    this.lastInfo = info;
    if (open.kind === 'file') {
      this.closeFd(open, info.name);
      this.files.push({
        info,
        tmpPath: open.tmpPath,
        finalPath: join(this.filesDir, open.storedName),
        storedName: open.storedName
      });
    } else {
      // RFC 7578 §4.4: form fields are text; non-UTF-8 field bytes are rejected.
      const raw = Buffer.concat(open.chunks);
      let value: string;
      try {
        value = new TextDecoder('utf-8', { fatal: true }).decode(raw);
      } catch {
        this.current = null;
        throw new MultipartError(
          ErrorCode.MALFORMED_FILENAME_ENCODING,
          `field "${info.name}" is not valid UTF-8 text`,
          { part: info.name }
        );
      }
      this.fieldRows.push({
        partIndex: info.index,
        name: info.name,
        isFile: false,
        filename: null,
        storedName: null,
        contentType: info.contentType,
        size: info.size,
        sha256: info.sha256,
        value
      });
    }
    this.current = null;
  }

  // ---- Completion / commit gate -------------------------------------------

  /**
   * Publish the upload. Call ONLY after parser.end() accepted the terminating
   * boundary. Returns the committed record. On failure all request-local data
   * (and any files already promoted) is removed.
   */
  commit(store: SubmissionStore, counters: ParseCounters): SubmissionRecord {
    if (this.discarded) {
      throw new MultipartError(ErrorCode.PARSER_ABORTED, 'cannot commit a discarded session', { runId: this.runId });
    }
    if (this.committed) {
      throw new MultipartError(ErrorCode.ALREADY_COMMITTED, 'session already committed', { runId: this.runId });
    }
    if (this.current) {
      throw new MultipartError(ErrorCode.COMMIT_BEFORE_COMPLETION, 'a part is still open at commit time', {});
    }
    if (this.opts.requireFilePart && this.files.length === 0) {
      // Deterministic rejection: no promotion happened, so only the empty
      // request-local temp directory needs reclaiming.
      this.cleanupTmp();
      throw new MultipartError(ErrorCode.NO_FILE_PARTS, 'submission must contain at least one file part', {});
    }

    // 1) promote temp files to the permanent directory
    const promoted: string[] = [];
    try {
      for (const f of this.files) {
        renameSync(f.tmpPath, f.finalPath);
        promoted.push(f.finalPath);
      }
    } catch (err) {
      for (const p of promoted) {
        try {
          rmSync(p, { force: true });
        } catch {
          // best effort
        }
      }
      throw wrapIoError(err, 'promoting uploaded files to permanent storage');
    }

    // 2) publish atomically in SQLite; undo the promotion on failure
    const rows: StoredPartRow[] = [];
    for (const f of this.files) {
      rows.push({
        partIndex: f.info.index,
        name: f.info.name,
        isFile: true,
        filename: f.info.filename,
        storedName: f.storedName,
        contentType: f.info.contentType,
        size: f.info.size,
        sha256: f.info.sha256,
        value: null
      });
    }
    rows.push(...this.fieldRows);
    rows.sort((a, b) => a.partIndex - b.partIndex);

    let record: SubmissionRecord;
    try {
      record = store.commitSubmission(this.runId, counters, rows);
    } catch (err) {
      for (const p of promoted) {
        try {
          rmSync(p, { force: true });
        } catch {
          // best effort
        }
      }
      throw err;
    }

    this.committed = true;
    this.cleanupTmp();
    return record;
  }

  /** Remove ONLY this request's temp directory (idempotent). */
  discard(): void {
    if (this.committed) return;
    this.discarded = true;
    if (this.current?.kind === 'file') {
      this.closeFd(this.current); // idempotent; handler.discard() may have run
    }
    this.current = null;
    this.cleanupTmp();
  }

  private cleanupTmp(): void {
    try {
      rmSync(this.tmpDir, { recursive: true, force: true });
    } catch {
      // cleanup is best effort; never mask the primary error
    }
  }

  get isCommitted(): boolean {
    return this.committed;
  }

  get fileCount(): number {
    return this.files.length;
  }
}
