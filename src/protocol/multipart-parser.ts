/**
 * Streaming multipart/form-data parser — the execution core.
 *
 * The parser is a deterministic byte state machine fed by arbitrary-sized
 * Buffers (`write`) and finalized with `end`. Every byte is accounted in
 * exactly one phase (prologue / header block / part body / delimiter /
 * epilogue), so a delimiter candidate split across two network chunks is
 * handled identically to the same bytes arriving in one chunk.
 *
 * Boundary safety:
 *  - A part body is split ONLY at `CRLF "--" boundary [LWSP] (CRLF | "--" CRLF)`.
 *  - A candidate whose trailing decision bytes do not validate is treated as
 *    ordinary body content; scanning resumes 2 bytes past the candidate's CRLF,
 *    so boundary-shaped sequences embedded in file content never false-split.
 *  - When a chunk ends inside a possible delimiter, the overlapping suffix is
 *    withheld until more bytes arrive (or `end` proves truncation).
 */

import { ErrorCode, MultipartError, type ErrorDetail } from './errors.js';
import { buildPartMeta, toPartInfo, type PartMeta } from './part-headers.js';
import type { Limits, PartInfo } from './types.js';

export type ParserState =
  | 'PROLOGUE'
  | 'HEADERS'
  | 'BODYPART'
  | 'EPILOGUE'
  | 'DONE'
  | 'ABORTED';

/** Per-part strategy supplied by the storage adapter. */
export interface PartHandler {
  /** Store one body chunk; parser has already validated size budgets. */
  write(chunk: Buffer): void;
  /** Finalize; returns measured size and content digest. */
  end(): { size: number; sha256: string };
  /** Release any temporary resource after cancel/abort (must not throw). */
  discard(): void;
}

export interface ParserSink {
  /** Build the storage handler and enforce per-part policy (may throw). */
  openPart(meta: PartMeta): PartHandler;
  /** Called once, in body order, after a part body is fully persisted. */
  closePart(info: PartInfo): void;
}

export interface ParseCounters {
  wireBytes: number;
  bodyBytes: number;
  headerBytes: number;
  parts: number;
  fields: number;
  files: number;
}

export interface ParseResult {
  parts: PartInfo[];
  counters: ParseCounters;
  boundary: string;
}

export type ParserEvent =
  | { t: 'state'; from: ParserState; to: ParserState; offset: number }
  | { t: 'candidate'; offset: number; kind: 'next' | 'close' | 'false' | 'wait' }
  | { t: 'part-start'; index: number; name: string; isFile: boolean; filename: string | null }
  | { t: 'part-end'; index: number; size: number }
  | { t: 'limit'; code: string; detail: string; offset: number };

/** RFC 2046 transport-padding is tolerated but bounded. */
const MAX_TRANSPORT_PADDING = 100;

export class MultipartParser {
  readonly boundary: string;
  private readonly delimBody: Buffer; // CRLF "--" boundary
  private readonly delimFirst: Buffer; // "--" boundary (start of entity)
  private readonly limits: Limits;
  private readonly sink: ParserSink;

  private state: ParserState = 'PROLOGUE';
  private pending = Buffer.alloc(0);
  private current: PartHandler | null = null;
  private meta: PartMeta | null = null;
  private partBodyBytes = 0;
  private parts: PartInfo[] = [];
  private counters: ParseCounters = {
    wireBytes: 0,
    bodyBytes: 0,
    headerBytes: 0,
    parts: 0,
    fields: 0,
    files: 0
  };
  private events: ParserEvent[] = [];

  constructor(boundary: string, limits: Limits, sink: ParserSink) {
    this.boundary = boundary;
    this.limits = limits;
    this.sink = sink;
    this.delimBody = Buffer.from(`\r\n--${boundary}`);
    this.delimFirst = Buffer.from(`--${boundary}`);
  }

  getState(): ParserState {
    return this.state;
  }

  getCounters(): ParseCounters {
    return { ...this.counters };
  }

  getParts(): readonly PartInfo[] {
    return this.parts;
  }

  getEvents(): readonly ParserEvent[] {
    return this.events;
  }

  private emitEvent(e: ParserEvent): void {
    if (this.events.length < 500) this.events.push(e);
  }

  private transition(to: ParserState, offset: number): void {
    this.emitEvent({ t: 'state', from: this.state, to, offset });
    this.state = to;
  }

  private fail(code: ErrorCode, message: string, details: ErrorDetail = {}, offset?: number): never {
    this.emitEvent({ t: 'limit', code, detail: message, offset: offset ?? this.counters.wireBytes });
    throw new MultipartError(code, message, details, offset);
  }

  /** Feed one network chunk. Any thrown MultipartError terminates the parse. */
  write(chunk: Buffer): void {
    if (this.state === 'DONE') this.fail(ErrorCode.PARSER_FINISHED, 'parser already finished');
    if (this.state === 'ABORTED') this.fail(ErrorCode.PARSER_ABORTED, 'parser was aborted');
    this.counters.wireBytes += chunk.length;
    if (this.state === 'EPILOGUE') return; // epilogue is discarded per RFC 2046
    this.pending = this.pending.length === 0 ? Buffer.from(chunk) : Buffer.concat([this.pending, chunk]);
    this.drain();
  }

  /** Declare the entity complete; returns the normalized parse summary. */
  end(): ParseResult {
    if (this.state === 'ABORTED') this.fail(ErrorCode.PARSER_ABORTED, 'parser was aborted');
    if (this.state === 'DONE' || this.state === 'EPILOGUE') {
      this.transition('DONE', this.counters.wireBytes);
      return this.result();
    }
    if (this.state === 'HEADERS') {
      this.fail(ErrorCode.TRUNCATED_BODY, 'stream ended in the middle of a part header block', {
        state: this.state,
        pendingBytes: this.pending.length
      });
    }
    this.fail(
      ErrorCode.MISSING_TERMINATING_BOUNDARY,
      `stream ended before the terminating boundary "--${this.boundary}--" was seen`,
      { state: this.state, pendingBytes: this.pending.length }
    );
  }

  /**
   * Cancel an in-flight upload (client disconnect / explicit abort).
   * Temp data of the current part is discarded; nothing is committed.
   */
  abort(): void {
    if (this.state === 'DONE' || this.state === 'ABORTED') return;
    this.transition('ABORTED', this.counters.wireBytes);
    if (this.current) {
      this.current.discard();
      this.current = null;
    }
    this.meta = null;
    this.pending = Buffer.alloc(0);
  }

  private result(): ParseResult {
    return { parts: this.parts, counters: { ...this.counters }, boundary: this.boundary };
  }

  // ---------------------------------------------------------------------------
  // State dispatch. Every drain step returns false when it is blocked waiting
  // for more bytes (same pending must not be reprocessed in that case).
  // ---------------------------------------------------------------------------

  private drain(): void {
    let guard = 0;
    while (this.pending.length > 0) {
      if (++guard > 1_000_000) this.fail(ErrorCode.INTERNAL, 'parser drain loop guard tripped');
      let progressed: boolean;
      switch (this.state) {
        case 'PROLOGUE':
          progressed = this.drainPrologue();
          break;
        case 'HEADERS':
          progressed = this.drainHeaders();
          break;
        case 'BODYPART':
          progressed = this.drainBody();
          break;
        case 'EPILOGUE':
        case 'DONE':
        case 'ABORTED':
          this.pending = Buffer.alloc(0);
          return;
        default:
          progressed = false;
      }
      if (!progressed) return;
    }
  }

  private drainPrologue(): boolean {
    const buf = this.pending;
    const marker = this.delimFirst;
    if (buf.length < marker.length) {
      if (this.isPrefixAt(marker, buf, 0)) return false;
      this.fail(ErrorCode.PROLOGUE_NOT_EMPTY, 'multipart body must begin with the opening boundary delimiter', {
        firstBytes: buf.subarray(0, Math.min(16, buf.length)).toString('latin1')
      });
    }
    if (!buf.subarray(0, marker.length).equals(marker)) {
      this.fail(ErrorCode.PROLOGUE_NOT_EMPTY, 'multipart body must begin with the opening boundary delimiter', {
        firstBytes: buf.subarray(0, Math.min(16, buf.length)).toString('latin1')
      });
    }
    const decision = this.decideDelimiter(buf, marker.length);
    if (decision === 'wait') return false;
    if (decision === 'false') {
      this.fail(
        ErrorCode.MALFORMED_BOUNDARY_DELIMITER,
        'opening boundary line has invalid transport padding or terminator',
        {}
      );
    }
    this.pending = buf.subarray(marker.length);
    return this.consumeDelimiterSuffix(decision);
  }

  private drainHeaders(): boolean {
    const buf = this.pending;
    const sep = buf.indexOf('\r\n\r\n');
    if (sep === -1) {
      if (buf.length > this.limits.maxHeaderSize) {
        this.fail(
          ErrorCode.HEADER_SIZE_EXCEEDED,
          `part header block exceeds ${this.limits.maxHeaderSize} bytes`,
          { maxHeaderSize: this.limits.maxHeaderSize, bytes: buf.length }
        );
      }
      return false;
    }
    if (sep + 4 > this.limits.maxHeaderSize) {
      this.fail(
        ErrorCode.HEADER_SIZE_EXCEEDED,
        `part header block exceeds ${this.limits.maxHeaderSize} bytes`,
        { maxHeaderSize: this.limits.maxHeaderSize, bytes: sep + 4 }
      );
    }
    const block = Buffer.from(buf.subarray(0, sep));
    this.counters.headerBytes += sep + 4;
    this.pending = buf.subarray(sep + 4);

    const index = this.counters.parts + 1;
    const meta = buildPartMeta(block, index);
    this.enforcePartQuotas(meta);
    this.counters.parts += 1;
    if (meta.isFile) this.counters.files += 1;
    else this.counters.fields += 1;

    let handler: PartHandler;
    try {
      handler = this.sink.openPart(meta);
    } catch (err) {
      if (err instanceof MultipartError) throw err;
      throw new MultipartError(ErrorCode.IO_FAILURE, `failed to open part "${meta.name}": ${(err as Error).message}`, {
        part: meta.name
      });
    }
    this.current = handler;
    this.meta = meta;
    this.partBodyBytes = 0;
    this.emitEvent({ t: 'part-start', index, name: meta.name, isFile: meta.isFile, filename: meta.effectiveFilename });
    this.transition('BODYPART', this.counters.wireBytes - this.pending.length);
    return true;
  }

  private enforcePartQuotas(meta: PartMeta): void {
    if (this.counters.parts >= this.limits.maxParts) {
      this.fail(ErrorCode.MAX_PARTS_EXCEEDED, `part count exceeds ${this.limits.maxParts}`, {
        maxParts: this.limits.maxParts
      });
    }
    if (!meta.isFile && this.counters.fields >= this.limits.maxFields) {
      this.fail(ErrorCode.MAX_FIELDS_EXCEEDED, `field count exceeds ${this.limits.maxFields}`, {
        maxFields: this.limits.maxFields
      });
    }
    if (meta.isFile && this.counters.files >= this.limits.maxFiles) {
      this.fail(ErrorCode.MAX_FILES_EXCEEDED, `file count exceeds ${this.limits.maxFiles}`, {
        maxFiles: this.limits.maxFiles
      });
    }
  }

  private drainBody(): boolean {
    const buf = this.pending;
    const marker = this.delimBody;
    const idx = buf.indexOf(marker);

    if (idx !== -1) {
      const decision = this.decideDelimiter(buf, idx + marker.length);
      if (decision === 'wait') return false;
      if (decision === 'false') {
        this.emitEvent({ t: 'candidate', offset: this.counters.bodyBytes + idx, kind: 'false' });
        // The candidate's own CRLF belongs to the body; rescan begins at '-'.
        this.dispatchBody(buf.subarray(0, idx + 2));
        this.pending = buf.subarray(idx + 2);
        return true;
      }
      this.dispatchBody(buf.subarray(0, idx));
      this.pending = buf.subarray(idx + marker.length);
      this.emitEvent({ t: 'candidate', offset: this.counters.bodyBytes, kind: decision });
      this.finishPart();
      return this.consumeDelimiterSuffix(decision);
    }

    const hold = this.longestSuffixPrefixLength(buf, marker);
    const safe = buf.length - hold;
    if (safe === 0) return false;
    this.dispatchBody(buf.subarray(0, safe));
    this.pending = buf.subarray(safe);
    return true;
  }

  /**
   * Inspect bytes right after a matched `--boundary`.
   *  - 'next'  : another part follows (CRLF seen)
   *  - 'close' : terminating boundary ("--" then CRLF seen)
   *  - 'false' : candidate is body content (invalid padding/terminator)
   *  - 'wait'  : not enough bytes yet; caller must retain the candidate
   */
  private decideDelimiter(buf: Buffer, start: number): 'next' | 'close' | 'false' | 'wait' {
    let i = start;
    let padding = 0;
    while (i < buf.length && (buf[i] === 0x20 || buf[i] === 0x09)) {
      i++;
      if (++padding > MAX_TRANSPORT_PADDING) return 'false';
    }
    if (i >= buf.length) return 'wait';
    const ch = buf[i]!;
    if (ch === 0x2d /* '-' */) {
      if (i + 1 >= buf.length) return 'wait';
      if (buf[i + 1] !== 0x2d) return 'false';
      let j = i + 2;
      let pad2 = 0;
      while (j < buf.length && (buf[j] === 0x20 || buf[j] === 0x09)) {
        j++;
        if (++pad2 > MAX_TRANSPORT_PADDING) return 'false';
      }
      if (j >= buf.length) return 'wait';
      if (j + 1 >= buf.length) return buf[j] === 0x0d ? 'wait' : 'false';
      return buf[j] === 0x0d && buf[j + 1] === 0x0a ? 'close' : 'false';
    }
    if (ch === 0x0d /* '\r' */) {
      if (i + 1 >= buf.length) return 'wait';
      return buf[i + 1] === 0x0a ? 'next' : 'false';
    }
    return 'false';
  }

  /**
   * Consume the validated suffix of a delimiter. `this.pending[0]` is the byte
   * right after `--boundary`. Transitions to HEADERS (next) or EPILOGUE (close).
   */
  private consumeDelimiterSuffix(decision: 'next' | 'close'): boolean {
    const buf = this.pending;
    let i = 0;
    const skipPadding = (): void => {
      let pad = 0;
      while (i < buf.length && (buf[i] === 0x20 || buf[i] === 0x09)) {
        i++;
        if (++pad > MAX_TRANSPORT_PADDING) {
          this.fail(ErrorCode.MALFORMED_BOUNDARY_DELIMITER, 'boundary transport padding exceeds 100 bytes', {});
        }
      }
    };
    skipPadding();
    if (decision === 'close') {
      // decideDelimiter already saw "--" [LWSP] CRLF; reproduce exactly.
      if (i + 1 >= buf.length || buf[i] !== 0x2d || buf[i + 1] !== 0x2d) {
        this.fail(ErrorCode.MALFORMED_BOUNDARY_DELIMITER, 'terminating boundary missing "--" suffix', {});
      }
      i += 2;
      skipPadding();
      if (i + 1 >= buf.length) return false; // need the final CRLF
      if (buf[i] !== 0x0d || buf[i + 1] !== 0x0a) {
        this.fail(ErrorCode.MALFORMED_BOUNDARY_DELIMITER, 'terminating boundary is not followed by CRLF', {});
      }
      i += 2;
      this.pending = buf.subarray(i); // remaining bytes are epilogue
      this.transition('EPILOGUE', this.counters.wireBytes - this.pending.length);
      if (this.pending.length > 0) this.pending = Buffer.alloc(0); // discard epilogue
      return true;
    }
    if (i + 1 >= buf.length) return false;
    if (buf[i] !== 0x0d || buf[i + 1] !== 0x0a) {
      this.fail(ErrorCode.MALFORMED_BOUNDARY_DELIMITER, 'boundary delimiter is not followed by CRLF', {});
    }
    i += 2;
    this.pending = buf.subarray(i);
    this.transition('HEADERS', this.counters.wireBytes - this.pending.length);
    return true;
  }

  private finishPart(): void {
    const handler = this.current;
    const meta = this.meta;
    if (!handler || !meta) this.fail(ErrorCode.INTERNAL, 'finishPart called without an open part');
    let ended: { size: number; sha256: string };
    try {
      ended = handler.end();
    } catch (err) {
      handler.discard();
      if (err instanceof MultipartError) throw err;
      throw new MultipartError(ErrorCode.IO_FAILURE, `failed to finalize part "${meta.name}": ${(err as Error).message}`, {
        part: meta.name
      });
    }
    if (ended.size !== this.partBodyBytes) {
      this.fail(
        ErrorCode.INTERNAL,
        `part handler size ${ended.size} disagrees with parser count ${this.partBodyBytes}`,
        { part: meta.name }
      );
    }
    const info = toPartInfo(meta, ended.size, ended.sha256);
    this.parts.push(info);
    this.emitEvent({ t: 'part-end', index: meta.index, size: ended.size });
    try {
      this.sink.closePart(info);
    } catch (err) {
      handler.discard();
      if (err instanceof MultipartError) throw err;
      throw new MultipartError(ErrorCode.IO_FAILURE, `closePart failed for "${meta.name}": ${(err as Error).message}`, {
        part: meta.name
      });
    }
    this.current = null;
    this.meta = null;
  }

  /** Dispatch body bytes, enforcing per-part and aggregate size budgets. */
  private dispatchBody(chunk: Buffer): void {
    if (chunk.length === 0) return;
    const perPartMax = this.meta?.isFile ? this.limits.maxFileSize : this.limits.maxFieldSize;
    if (this.partBodyBytes + chunk.length > perPartMax) {
      this.fail(
        ErrorCode.PART_SIZE_EXCEEDED,
        `${this.meta?.isFile ? 'file' : 'field'} part "${this.meta?.name}" exceeds ${perPartMax} bytes`,
        { part: this.meta?.name ?? '', kind: this.meta?.isFile ? 'file' : 'field', limit: perPartMax }
      );
    }
    if (this.counters.bodyBytes + chunk.length > this.limits.maxTotalSize) {
      this.fail(
        ErrorCode.TOTAL_SIZE_EXCEEDED,
        `aggregate part body size exceeds ${this.limits.maxTotalSize} bytes`,
        { limit: this.limits.maxTotalSize, bytes: this.counters.bodyBytes + chunk.length }
      );
    }
    try {
      this.current!.write(chunk);
    } catch (err) {
      if (err instanceof MultipartError) throw err;
      throw new MultipartError(
        ErrorCode.IO_FAILURE,
        `failed writing part "${this.meta?.name}": ${(err as Error).message}`,
        { part: this.meta?.name ?? '' }
      );
    }
    this.partBodyBytes += chunk.length;
    this.counters.bodyBytes += chunk.length;
  }

  private isPrefixAt(marker: Buffer, buf: Buffer, offset: number): boolean {
    const len = Math.min(marker.length, buf.length - offset);
    return buf.subarray(offset, offset + len).equals(marker.subarray(0, len));
  }

  /**
   * Length of the longest suffix of `buf` that is a (non-full) prefix of
   * `marker`. A full marker occurrence would already have been found by
   * indexOf, so the result is at most marker.length - 1.
   */
  private longestSuffixPrefixLength(buf: Buffer, marker: Buffer): number {
    const max = Math.min(buf.length, marker.length - 1);
    for (let len = max; len > 0; len--) {
      if (buf.subarray(buf.length - len).equals(marker.subarray(0, len))) return len;
    }
    return 0;
  }
}
