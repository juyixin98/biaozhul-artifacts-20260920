/**
 * Streaming multipart/form-data execution core.
 *
 * Design properties:
 *  - Pure framing/state machine. No file system or database knowledge; body
 *    bytes are pushed through a caller-supplied {@link PartSink} factory.
 *  - Boundary-safe across chunks: a delimiter straddling two writes is never
 *    split; a *near* boundary inside a body (`CRLF "--" boundary` followed by
 *    an extra byte) is classified as body content, not as a boundary.
 *  - Three independent quotas enforced while bytes flow: per-part, aggregate,
 *    header-section; plus a part-count cap.
 *  - A parsed form is produced ONLY after the close-delimiter
 *    ("--" boundary "--") has been verified. Stream truncation raises
 *    MISSING_TERMINATOR and produces no result.
 *  - Every protocol failure destroys every sink opened by THIS parser run.
 */

import { createHash } from 'node:crypto';
import {
  inputError,
  stateError,
  resourceError,
  computeError,
  isMultipartError,
  type MultipartError,
} from './errors.js';
import { parsePartHeaders } from './headers.js';
import type { MultipartLimits, UploadRestrictions } from './config.js';
import type {
  CompletedPart,
  ParsedForm,
  PartMeta,
  PartSink,
  ParserObserver,
} from './types.js';

const CR = 0x0d;
const LF = 0x0a;
const DASH = 0x2d;

type Phase =
  | 'start' // nothing consumed yet, expecting first delimiter
  | 'header' // accumulating a header section up to CRLF CRLF
  | 'body' // streaming a part body, watching for CRLF--boundary
  | 'epilogue' // close-delimiter seen, trailing bytes ignored
  | 'finished' // finish() resolved
  | 'aborted';

export interface ParserDeps {
  boundaryDelimiter: string;
  limits: MultipartLimits;
  restrictions: UploadRestrictions;
  /** Reject bare LF in framing/header regions when true (default behaviour). */
  strictCrlf?: boolean;
  createSink: (meta: PartMeta) => PartSink;
  observer?: ParserObserver;
}

export class MultipartParser {
  private readonly delimiter: Buffer; // "--" + boundary
  private readonly bodyNeedle: Buffer; // CRLF "--" + boundary
  private readonly limits: MultipartLimits;
  private readonly restrictions: UploadRestrictions;
  private readonly createSink: (meta: PartMeta) => PartSink;
  private readonly strictCrlf: boolean;
  private readonly observer?: ParserObserver;

  private carry = Buffer.alloc(0);
  private phase: Phase = 'start';
  /** Offset in `carry` where the current header section begins. */
  private headerStart = 0;
  /** Offset in `carry` where the current part body begins. */
  private bodyStart = 0;
  /** Offset from which boundary scanning should resume. */
  private searchFrom = 0;

  private currentMeta: PartMeta | null = null;
  private currentSink: PartSink | null = null;
  private partBytes = 0;
  private totalBytes = 0;
  private readonly parts: CompletedPart[] = [];
  private readonly sinks: PartSink[] = [];

  constructor(deps: ParserDeps) {
    this.delimiter = Buffer.from(deps.boundaryDelimiter, 'ascii');
    this.bodyNeedle = Buffer.concat([Buffer.from([CR, LF]), this.delimiter]);
    this.limits = deps.limits;
    this.restrictions = deps.restrictions;
    this.createSink = deps.createSink;
    this.strictCrlf = deps.strictCrlf ?? true;
    this.observer = deps.observer;
  }

  /** Feed one transport chunk. Calls are serialized: pumps never overlap. */
  async write(chunk: Buffer): Promise<void> {
    if (this.phase === 'finished') {
      throw stateError('PARSER_FINISHED', 'cannot write: parser already finished');
    }
    if (this.phase === 'aborted') {
      throw stateError('ALREADY_ABORTED', 'cannot write: parser was aborted');
    }
    if (chunk.length === 0) return;
    this.carry = this.carry.length === 0 ? chunk : Buffer.concat([this.carry, chunk]);
    await this.runPump(false);
  }

  /** Verify termination and return the parsed form. */
  async finish(): Promise<ParsedForm> {
    if (this.phase === 'aborted') {
      throw stateError('ALREADY_ABORTED', 'cannot finish: parser was aborted');
    }
    if (this.phase === 'finished') {
      throw stateError('PARSER_FINISHED', 'parser already finished');
    }
    await this.runPump(true);

    if (this.phase !== 'epilogue') {
      await this.fail(stateError('NOT_TERMINATED', 'stream ended before the parser reached a stable state'));
    }
    if (this.parts.length === 0) {
      await this.fail(inputError('EMPTY_BODY', 'multipart body contained no parts'));
    }
    this.phase = 'finished';
    return { parts: this.parts, totalSize: this.totalBytes };
  }

  /** Cancel parsing (upload aborted by client or server). Releases sinks. */
  async abort(): Promise<void> {
    if (this.phase === 'aborted') return;
    this.phase = 'aborted';
    await this.destroyAllSinks();
  }

  // ---------------------------------------------------------------- internals

  /**
   * Serialize pump executions. Even if a caller invokes write() and finish()
   * with overlapping promises (the whole body can arrive in a single data
   * event), the state machine itself never runs two pumps at once. The
   * rejection-swallowing keeps the chain alive; each caller still receives
   * the rejection through the returned promise.
   */
  private pumpChain: Promise<void> = Promise.resolve();

  private runPump(atEnd: boolean): Promise<void> {
    const current = this.pumpChain.then(() => this.pump(atEnd));
    this.pumpChain = current.then(
      () => undefined,
      () => undefined,
    );
    return current;
  }

  private async pump(atEnd = false): Promise<void> {
    // Drive the state machine as far as the buffered bytes allow.
    for (;;) {
      try {
        if (this.phase === 'start') {
          if (!this.consumeFirstDelimiter(atEnd)) break;
        } else if (this.phase === 'header') {
          const advanced = await this.consumeHeader(atEnd);
          if (!advanced) break;
        } else if (this.phase === 'body') {
          const advanced = await this.consumeBody(atEnd);
          if (!advanced) break;
        } else if (this.phase === 'epilogue') {
          // Everything past the close-delimiter is discarded.
          this.carry = Buffer.alloc(0);
          break;
        } else {
          break;
        }
      } catch (err) {
        await this.fail(err);
      }
    }
  }

  /** @returns true if a transition was made, false if more bytes are needed. */
  private consumeFirstDelimiter(atEnd: boolean): boolean {
    const need = this.delimiter.length + 2;
    if (this.carry.length < need) {
      if (atEnd) {
        throw inputError('MALFORMED_BOUNDARY', 'body is too short to contain the opening boundary');
      }
      return false;
    }
    if (!this.carry.subarray(0, this.delimiter.length).equals(this.delimiter)) {
      throw inputError('MALFORMED_BOUNDARY', 'body does not begin with the declared boundary', {
        expectedPrefix: this.delimiter.toString('latin1'),
      });
    }
    const a = this.carry[this.delimiter.length]!;
    const b = this.carry[this.delimiter.length + 1]!;
    if (a === DASH && b === DASH) {
      // Immediate close-delimiter: zero parts (rejected at finish()), but
      // framing still needs the trailing CRLF / EOF.
      const c = this.carry[this.delimiter.length + 2];
      if (c === undefined) {
        if (atEnd) {
          this.carry = Buffer.alloc(0);
          this.phase = 'epilogue';
          return true;
        }
        return false;
      }
      if (c !== CR) {
        throw inputError('MALFORMED_BOUNDARY', 'close-delimiter must be followed by CRLF or EOF');
      }
      if (this.carry[this.delimiter.length + 3] === undefined) {
        if (atEnd) throw inputError('MALFORMED_BOUNDARY', 'truncated CRLF after close-delimiter');
        return false;
      }
      if (this.carry[this.delimiter.length + 3] !== LF) {
        throw inputError('MALFORMED_BOUNDARY', 'close-delimiter must be followed by CRLF or EOF');
      }
      this.carry = this.carry.subarray(this.delimiter.length + 4);
      this.phase = 'epilogue';
      return true;
    }
    if (a !== CR || b !== LF) {
      throw inputError('MALFORMED_BOUNDARY', 'boundary must be followed by "--" or CRLF');
    }
    this.carry = this.carry.subarray(this.delimiter.length + 2);
    this.headerStart = 0;
    this.phase = 'header';
    return true;
  }

  /** Find the end of the current header section. Strict: CRLF CRLF only. */
  private findHeaderEnd(): { start: number; length: number } | null {
    const strict = this.carry.indexOf('\r\n\r\n', this.headerStart, 'latin1');
    if (strict >= 0) return { start: strict, length: 4 };
    if (this.strictCrlf) return null;
    // Lenient: accept blank lines formed by LF in any CR/LF combination.
    for (let i = this.headerStart; i < this.carry.length - 1; i++) {
      if (this.carry[i] !== LF) continue;
      // A blank line: current byte is the LF of an empty line. Determine the
      // terminator span: either "\r\n\r\n" (handled), "\n\n", "\r\n\n",
      // "\n\r\n".
      if (this.carry[i + 1] === LF) {
        const start = this.carry[i - 1] === CR ? i - 1 : i;
        return { start, length: i + 2 - start };
      }
      if (this.carry[i + 1] === CR && this.carry[i + 2] === LF) {
        const start = this.carry[i - 1] === CR ? i - 1 : i;
        return { start, length: i + 3 - start };
      }
    }
    return null;
  }

  private async consumeHeader(atEnd: boolean): Promise<boolean> {
    const terminator = this.findHeaderEnd();
    if (!terminator) {
      // In strict framing an LF in the header region is always preceded by CR.
      // A lone LF can never become part of a CRLF later (the CR precedes the
      // LF), so it can be rejected immediately, including mid-stream.
      if (this.strictCrlf) {
        for (let i = this.headerStart; i < this.carry.length; i++) {
          if (this.carry[i] === LF && (i === 0 || this.carry[i - 1] !== CR)) {
            throw inputError('BARE_LF', 'header framing uses a bare LF; CRLF is required', { at: i });
          }
        }
      }
      const sectionLength = this.carry.length - this.headerStart;
      if (sectionLength > this.limits.maxHeaderBytes) {
        throw resourceError('HEADER_TOO_LONG', `header section exceeds ${this.limits.maxHeaderBytes} bytes`, {
          limit: this.limits.maxHeaderBytes,
          observed: sectionLength,
        });
      }
      if (atEnd) {
        throw inputError('MALFORMED_HEADER', 'stream ended inside a part header section');
      }
      return false;
    }
    const blank = terminator.start;
    const sectionLength = blank - this.headerStart;
    if (sectionLength > this.limits.maxHeaderBytes) {
      throw resourceError('HEADER_TOO_LONG', `header section exceeds ${this.limits.maxHeaderBytes} bytes`, {
        limit: this.limits.maxHeaderBytes,
        observed: sectionLength,
      });
    }
    let section = this.carry.subarray(this.headerStart, blank).toString('latin1');
    if (!this.strictCrlf) {
      // Normalize CR/LF line endings so the header line splitter sees CRLF.
      section = section.replace(/\r\n|\r|\n/g, '\r\n');
    } else {
      const sectionBuf = Buffer.from(section, 'latin1');
      this.assertFramingCrlf(sectionBuf);
    }

    const meta = parsePartHeaders(section, this.restrictions);
    if (this.parts.length + this.openSlots() > this.limits.maxParts) {
      throw resourceError('TOO_MANY_PARTS', `part count would exceed ${this.limits.maxParts}`, {
        limit: this.limits.maxParts,
      });
    }

    this.currentMeta = meta;
    this.currentSink = this.openSink(meta);
    this.partBytes = 0;
    this.observer?.onPartBegin?.(meta);

    const bodyStart = blank + terminator.length;
    this.carry = this.carry.subarray(bodyStart);
    this.bodyStart = 0;
    this.searchFrom = 0;
    this.phase = 'body';
    return true;
  }

  private openSlots(): number {
    // One part is currently open whenever a sink exists.
    return this.currentSink ? 1 : 0;
  }

  private async consumeBody(atEnd: boolean): Promise<boolean> {
    const needle = this.bodyNeedle;
    for (;;) {
      // Need the full needle plus two classification bytes after it.
      const classifiableEnd = this.carry.length - needle.length - 2;
      let idx = -1;
      if (classifiableEnd >= this.searchFrom) {
        idx = this.carry.indexOf(needle, this.searchFrom, 'latin1');
        if (idx > classifiableEnd) idx = -1;
      }

      if (idx >= 0) {
        const after = idx + needle.length;
        const a = this.carry[after]!;
        const b = this.carry[after + 1]!;
        if (a === DASH && b === DASH) {
          const closed = await this.handleCloseDelimiter(idx, after + 2, atEnd);
          if (!closed) return false;
          return true;
        }
        if (a === CR && b === LF) {
          await this.emitBody(idx);
          await this.closePart('intermediate');
          // Skip CRLF following the delimiter; a new header starts there.
          this.carry = this.carry.subarray(after + 2);
          this.headerStart = 0;
          this.phase = 'header';
          return true;
        }
        // Needle matched but the suffix proves it is body content. Resume
        // scanning one byte past the false match (near-boundary case).
        this.searchFrom = idx + 1;
        continue;
      }

      // No classifiable match. Flush, but retain a tail wide enough that no
      // possible delimiter (plus the two bytes needed to classify it) can
      // straddle this and the next write. A match at idx is classifiable only
      // when idx + needle.length + 2 <= length, so the earliest as-yet
      // undecidable match could start at length - needle.length - 1; retain
      // needle.length + 1 bytes to keep every one of its bytes buffered.
      const reserve = needle.length + 1;
      const safeEnd = Math.max(this.bodyStart, this.carry.length - reserve);
      if (safeEnd > this.bodyStart) {
        await this.emitBody(safeEnd);
        this.carry = this.carry.subarray(safeEnd);
        this.bodyStart = 0;
        this.searchFrom = 0;
      }
      if (atEnd) {
        // Remaining bytes cannot start a delimiter and the stream is over.
        throw inputError('MISSING_TERMINATOR', 'stream ended without the multipart closing boundary');
      }
      return false;
    }
  }

  /**
   * Verify the bytes following the close-delimiter, then — and only then —
   * flush and finalize the pending part.
   * @returns false when more bytes are required to decide.
   */
  private async handleCloseDelimiter(
    idx: number,
    posAfterDashes: number,
    atEnd: boolean,
  ): Promise<boolean> {
    const c = this.carry[posAfterDashes];
    if (c === undefined) {
      if (atEnd) {
        // Lenient: accept EOF immediately after the close-delimiter.
        await this.completeTerminal(idx);
        return true;
      }
      return false; // wait for CRLF / EOF decision
    }
    if (c !== CR) {
      throw inputError('MALFORMED_BOUNDARY', 'close-delimiter must be followed by CRLF or EOF');
    }
    const d = this.carry[posAfterDashes + 1];
    if (d === undefined) {
      if (atEnd) throw inputError('MALFORMED_BOUNDARY', 'truncated CRLF after close-delimiter');
      return false;
    }
    if (d !== LF) {
      throw inputError('MALFORMED_BOUNDARY', 'close-delimiter must be followed by CRLF or EOF');
    }
    await this.completeTerminal(idx);
    return true;
  }

  private async completeTerminal(bodyEnd: number): Promise<void> {
    // Terminal boundary verified: only now may the pending part finalize.
    await this.emitBody(bodyEnd);
    await this.closePart('terminal');
    this.carry = Buffer.alloc(0);
    this.phase = 'epilogue';
  }

  private async emitBody(endExclusive: number): Promise<void> {
    if (endExclusive <= this.bodyStart) return;
    const slice = this.carry.subarray(this.bodyStart, endExclusive);
    this.partBytes += slice.length;
    this.totalBytes += slice.length;
    this.assertBodyQuotas();
    try {
      await this.currentSink?.write(slice);
    } catch (err) {
      throw this.wrapIo(err);
    }
    this.bodyStart = endExclusive;
    const meta = this.currentMeta;
    if (meta) this.observer?.onPartChunk?.(meta, slice.length, this.totalBytes);
  }

  private assertBodyQuotas(): void {
    if (this.totalBytes > this.limits.maxTotalBytes) {
      throw resourceError('TOTAL_TOO_LARGE', `aggregate body exceeds ${this.limits.maxTotalBytes} bytes`, {
        limit: this.limits.maxTotalBytes,
        observed: this.totalBytes,
      });
    }
    const meta = this.currentMeta;
    if (meta?.kind === 'file' && this.partBytes > this.limits.maxPartBytes) {
      throw resourceError('PART_TOO_LARGE', `file part exceeds ${this.limits.maxPartBytes} bytes`, {
        limit: this.limits.maxPartBytes,
        observed: this.partBytes,
        field: meta.name,
      });
    }
    if (meta?.kind === 'field' && this.partBytes > this.limits.maxFieldBytes) {
      throw resourceError('FIELD_TOO_LARGE', `field part exceeds ${this.limits.maxFieldBytes} bytes`, {
        limit: this.limits.maxFieldBytes,
        observed: this.partBytes,
        field: meta.name,
      });
    }
  }

  private openSink(meta: PartMeta): PartSink {
    let sink: PartSink;
    try {
      sink = this.createSink(meta);
    } catch (err) {
      throw isMultipartError(err) ? err : this.wrapIo(err);
    }
    this.sinks.push(sink);
    return sink;
  }

  private async closePart(kind: 'intermediate' | 'terminal'): Promise<void> {
    const sink = this.currentSink;
    const meta = this.currentMeta;
    if (!sink || !meta) {
      throw stateError('PART_NOT_OPEN', 'internal: attempted to close a part that is not open');
    }
    let finalized;
    try {
      finalized = await sink.end();
    } catch (err) {
      throw this.wrapIo(err);
    }
    const part: CompletedPart = {
      meta,
      size: finalized.size,
      sha256: finalized.sha256,
      ...(finalized.tempPath !== undefined ? { tempPath: finalized.tempPath } : {}),
      ...(finalized.value !== undefined ? { value: finalized.value } : {}),
    };
    this.parts.push(part);
    this.observer?.onPartEnd?.(part);
    this.observer?.onBoundary?.(kind, this.totalBytes);
    this.currentSink = null;
    this.currentMeta = null;
  }

  private assertFramingCrlf(sectionBuf: Buffer): void {
    // Bare LF inside the framed header region is a protocol violation. (Body
    // bytes are opaque and are never checked here.)
    for (let i = 0; i < sectionBuf.length; i++) {
      if (sectionBuf[i] === LF && (i === 0 || sectionBuf[i - 1] !== CR)) {
        throw inputError('BARE_LF', 'header framing uses a bare LF; CRLF is required', {
          at: this.headerStart + i,
        });
      }
    }
  }

  private wrapIo(err: unknown): MultipartError {
    if (isMultipartError(err)) return err;
    return computeError('TEMP_IO_ERROR', 'temporary storage I/O failed', {
      reason: (err as Error)?.message ?? String(err),
    });
  }

  private async fail(err: unknown): Promise<never> {
    await this.destroyAllSinks();
    if (isMultipartError(err)) throw err;
    throw computeError('TEMP_IO_ERROR', 'unexpected parser failure', {
      reason: (err as Error)?.message ?? String(err),
    });
  }

  private async destroyAllSinks(): Promise<void> {
    const pending = this.sinks.splice(0);
    this.currentSink = null;
    await Promise.all(
      pending.map(async (sink) => {
        try {
          await sink.destroy();
        } catch {
          // best-effort cleanup; original failure must not be masked
        }
      }),
    );
  }
}

/** Re-exported for callers that hash standalone bytes in tests/fixtures. */
export function sha256Hex(buf: Buffer): string {
  return createHash('sha256').update(buf).digest('hex');
}
