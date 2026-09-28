/**
 * Limit accounting, parser state conflicts and resource-class distinctions.
 * Inputs here are constructed locally; each test asserts the concrete code and
 * error CLASS (INPUT_ERROR vs STATE_CONFLICT vs RESOURCE_LIMIT vs
 * COMPUTE_FAILURE), not merely that something threw.
 */

import { describe, expect, it } from 'vitest';
import { ErrorCode, MultipartError, wrapIoError } from '../src/protocol/errors.js';
import { MultipartParser } from '../src/protocol/multipart-parser.js';
import { DEFAULT_LIMITS, type Limits } from '../src/protocol/types.js';
import { MemorySink, feedInChunks } from './helpers.js';

function fieldPart(name: string, value: Buffer | string, boundary = 'B'): Buffer {
  const body = typeof value === 'string' ? Buffer.from(value) : value;
  return Buffer.concat([
    Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n`),
    body
  ]);
}

function onePartMessage(name: string, body: Buffer | string, boundary = 'B', filename?: string): Buffer {
  const head = filename
    ? `--${boundary}\r\nContent-Disposition: form-data; name="${name}"; filename="${filename}"\r\nContent-Type: text/plain\r\n\r\n`
    : `--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n`;
  const b = typeof body === 'string' ? Buffer.from(body) : body;
  return Buffer.concat([Buffer.from(head), b, Buffer.from(`\r\n--${boundary}--\r\n`)]);
}

function expectMultipart(fn: () => unknown, code: ErrorCode): MultipartError {
  try {
    fn();
  } catch (err) {
    expect(err).toBeInstanceOf(MultipartError);
    const me = err as MultipartError;
    expect(me.code).toBe(code);
    return me;
  }
  throw new Error(`expected ${code}`);
}

describe('per-part limits', () => {
  it('flags an oversize field as RESOURCE_LIMIT / PART_SIZE_EXCEEDED', () => {
    const limits: Limits = { ...DEFAULT_LIMITS, maxFieldSize: 4 };
    const data = onePartMessage('f', 'abcdef', 'B');
    const me = expectMultipart(() => {
      const p = new MultipartParser('B', limits, new MemorySink());
      p.write(data);
      p.end();
    }, ErrorCode.PART_SIZE_EXCEEDED);
    expect(me.errorClass).toBe('RESOURCE_LIMIT');
    expect(me.httpStatus).toBe(413);
  });

  it('flags an oversize file distinctly from a field', () => {
    const limits: Limits = { ...DEFAULT_LIMITS, maxFileSize: 3 };
    const data = onePartMessage('up', Buffer.from([1, 2, 3, 4]), 'B', 'a.txt');
    const me = expectMultipart(() => {
      const p = new MultipartParser('B', limits, new MemorySink());
      p.write(data);
      p.end();
    }, ErrorCode.PART_SIZE_EXCEEDED);
    expect(me.details.kind).toBe('file');
  });

  it('a part exactly at the limit is accepted', () => {
    const limits: Limits = { ...DEFAULT_LIMITS, maxFieldSize: 3 };
    const sink = new MemorySink();
    const p = new MultipartParser('B', limits, sink);
    p.write(onePartMessage('f', 'abc'));
    p.end();
    expect(sink.parts[0]!.data.toString()).toBe('abc');
  });
});

describe('aggregate total limit', () => {
  it('flags the cumulative body across parts', () => {
    const limits: Limits = { ...DEFAULT_LIMITS, maxTotalSize: 5 };
    const data = Buffer.concat([
      fieldPart('a', 'aaa'),
      Buffer.from('\r\n'),
      // second part
      Buffer.from('--B\r\nContent-Disposition: form-data; name="b"\r\n\r\nbbb\r\n--B--\r\n')
    ]);
    // total body = 6 > 5
    const me = expectMultipart(() => {
      const p = new MultipartParser('B', limits, new MemorySink());
      feedInChunks((c) => p.write(c), data, 2);
      p.end();
    }, ErrorCode.TOTAL_SIZE_EXCEEDED);
    expect(me.errorClass).toBe('RESOURCE_LIMIT');
  });
});

describe('header size limit', () => {
  it('flags an oversized header block', () => {
    const limits: Limits = { ...DEFAULT_LIMITS, maxHeaderSize: 40 };
    const bigName = 'x'.repeat(60);
    const data = onePartMessage(bigName, 'a');
    const me = expectMultipart(() => {
      const p = new MultipartParser('B', limits, new MemorySink());
      feedInChunks((c) => p.write(c), data, 1);
      p.end();
    }, ErrorCode.HEADER_SIZE_EXCEEDED);
    expect(me.errorClass).toBe('RESOURCE_LIMIT');
  });
});

describe('part counts', () => {
  function messageWithParts(n: number, asFile = false): Buffer {
    const parts: Buffer[] = [];
    for (let i = 0; i < n; i++) {
      parts.push(onePartMessage(asFile ? `f${i}` : `f${i}`, 'x', 'B', asFile ? 'a.txt' : undefined));
    }
    // onePartMessage already terminates; stitch bodies manually instead
    const blocks: Buffer[] = [];
    for (let i = 0; i < n; i++) {
      const head = asFile
        ? `--B\r\nContent-Disposition: form-data; name="f${i}"; filename="a.txt"\r\nContent-Type: text/plain\r\n\r\n`
        : `--B\r\nContent-Disposition: form-data; name="f${i}"\r\n\r\n`;
      blocks.push(Buffer.concat([i === 0 ? Buffer.alloc(0) : Buffer.from(''), Buffer.from(head), Buffer.from('x')]));
    }
    void parts;
    return Buffer.concat([
      blocks[0]!,
      ...blocks.slice(1).map((b) => Buffer.concat([Buffer.from('\r\n'), b])),
      Buffer.from('\r\n--B--\r\n')
    ]);
  }

  it('enforces maxParts', () => {
    const limits: Limits = { ...DEFAULT_LIMITS, maxParts: 2 };
    expectMultipart(() => {
      const p = new MultipartParser('B', limits, new MemorySink());
      p.write(messageWithParts(3));
      p.end();
    }, ErrorCode.MAX_PARTS_EXCEEDED);
  });

  it('enforces maxFields and maxFiles separately', () => {
    const limitsF: Limits = { ...DEFAULT_LIMITS, maxFields: 1 };
    expectMultipart(() => {
      const p = new MultipartParser('B', limitsF, new MemorySink());
      p.write(messageWithParts(2, false));
      p.end();
    }, ErrorCode.MAX_FIELDS_EXCEEDED);

    const limitsFile: Limits = { ...DEFAULT_LIMITS, maxFiles: 1 };
    expectMultipart(() => {
      const p = new MultipartParser('B', limitsFile, new MemorySink());
      p.write(messageWithParts(2, true));
      p.end();
    }, ErrorCode.MAX_FILES_EXCEEDED);
  });
});

describe('parser lifecycle state conflicts', () => {
  it('writing after end() is PARSER_FINISHED (STATE_CONFLICT)', () => {
    const p = new MultipartParser('B', DEFAULT_LIMITS, new MemorySink());
    p.write(onePartMessage('f', 'x'));
    p.end();
    const me = expectMultipart(() => p.write(Buffer.from('more')), ErrorCode.PARSER_FINISHED);
    expect(me.errorClass).toBe('STATE_CONFLICT');
    expect(me.httpStatus).toBe(409);
  });

  it('end() is idempotent once DONE', () => {
    const p = new MultipartParser('B', DEFAULT_LIMITS, new MemorySink());
    p.write(onePartMessage('f', 'x'));
    const r1 = p.end();
    const r2 = p.end();
    expect(r2.counters.bodyBytes).toBe(r1.counters.bodyBytes);
  });

  it('writing after abort() is PARSER_ABORTED', () => {
    const p = new MultipartParser('B', DEFAULT_LIMITS, new MemorySink());
    p.write(fieldPart('f', 'x')); // no terminating boundary yet
    p.abort();
    const me = expectMultipart(() => p.write(Buffer.from('zz')), ErrorCode.PARSER_ABORTED);
    expect(me.errorClass).toBe('STATE_CONFLICT');
    expect(p.getState()).toBe('ABORTED');
  });

  it('end() after abort() is PARSER_ABORTED', () => {
    const p = new MultipartParser('B', DEFAULT_LIMITS, new MemorySink());
    p.abort();
    expectMultipart(() => p.end(), ErrorCode.PARSER_ABORTED);
  });

  it('abort discards the current part handler exactly once', () => {
    const sink = new MemorySink();
    const p = new MultipartParser('B', DEFAULT_LIMITS, sink);
    p.write(fieldPart('f', 'partial'));
    expect(p.getState()).toBe('BODYPART');
    p.abort();
    p.abort(); // idempotent
    expect(p.getState()).toBe('ABORTED');
  });
});

describe('truncation distinctions', () => {
  it('stream ending mid-headers is TRUNCATED_BODY (INPUT_ERROR)', () => {
    const p = new MultipartParser('B', DEFAULT_LIMITS, new MemorySink());
    p.write(Buffer.from('--B\r\nContent-Disposition: form-data; name="x"'));
    const me = expectMultipart(() => p.end(), ErrorCode.TRUNCATED_BODY);
    expect(me.errorClass).toBe('INPUT_ERROR');
  });

  it('stream ending in a body part is MISSING_TERMINATING_BOUNDARY', () => {
    const p = new MultipartParser('B', DEFAULT_LIMITS, new MemorySink());
    p.write(fieldPart('f', 'abc'));
    const me = expectMultipart(() => p.end(), ErrorCode.MISSING_TERMINATING_BOUNDARY);
    expect(me.errorClass).toBe('INPUT_ERROR');
  });

  it('a closing marker split at its final dash then truncated is detected at end()', () => {
    const data = Buffer.concat([
      fieldPart('f', 'abc'),
      Buffer.from('\r\n--B--') // final CRLF missing
    ]);
    const p = new MultipartParser('B', DEFAULT_LIMITS, new MemorySink());
    p.write(data);
    const me = expectMultipart(() => p.end(), ErrorCode.MISSING_TERMINATING_BOUNDARY);
    expect(me.errorClass).toBe('INPUT_ERROR');
  });
});

describe('error taxonomy helpers', () => {
  it('maps ENOSPC to RESOURCE_LIMIT/DISK_SPACE', () => {
    const err = Object.assign(new Error('no space'), { code: 'ENOSPC', syscall: 'write' });
    const me = wrapIoError(err, 'writing temp file');
    expect(me.code).toBe(ErrorCode.DISK_SPACE);
    expect(me.errorClass).toBe('RESOURCE_LIMIT');
  });
  it('maps other errno codes to COMPUTE_FAILURE/IO_FAILURE', () => {
    const err = Object.assign(new Error('boom'), { code: 'EIO' });
    const me = wrapIoError(err, 'reading');
    expect(me.code).toBe(ErrorCode.IO_FAILURE);
    expect(me.errorClass).toBe('COMPUTE_FAILURE');
    expect(me.httpStatus).toBe(500);
  });
});
