/**
 * Test fixtures and INDEPENDENT reference helpers.
 *
 * Nothing in this file imports from src/range: expected byte answers are
 * literal strings/buffers or plain arithmetic over the fixture itself, and
 * the multipart reader below parses the wire format from scratch. That
 * keeps the test oracle independent of the code under test — a range bug
 * cannot make the reference answer agree with the implementation.
 */

export const ALPHA = Buffer.from('abcdefghijklmnopqrstuvwxyz', 'utf8'); // 26
export const HELLO = Buffer.from('Hello, Range world!', 'utf8'); // 19
export const EMPTY = Buffer.alloc(0);
export const BIN256 = Buffer.from(Array.from({ length: 256 }, (_, i) => i));
export const PATTERN_1K = Buffer.from(
  Array.from({ length: 1024 }, (_, i) => (i * 31 + 7) & 0xff),
);

export function meta(size: number) {
  return {
    id: 'fixture',
    size,
    etag: '"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"',
    lastModified: new Date('2026-03-04T05:06:07.000Z'),
    contentType: 'text/plain; charset=utf-8',
  };
}

export interface ParsedPart {
  headers: Record<string, string>;
  bytes: Buffer;
  /** Values parsed out of the part's own Content-Range header. */
  range: { start: number; end: number; complete: number };
}

/**
 * Hand-written multipart/byteranges reader (RFC 9110 Appendix A shape):
 *
 *   --boundary CRLF
 *   header: value CRLF
 *   ...
 *   CRLF
 *   <exact part bytes>
 *   CRLF
 *   ...
 *   --boundary-- CRLF
 *
 * It locates delimiters by scanning raw bytes and derives each part's
 * length from its Content-Range header rather than trusting any
 * implementation-side accounting.
 */
export function parseMultipart(body: Buffer, boundary: string): ParsedPart[] {
  const delim = Buffer.from(`--${boundary}`);
  const endDelim = Buffer.from(`--${boundary}--`);
  const parts: ParsedPart[] = [];

  let pos = body.indexOf(delim);
  if (pos !== 0) throw new Error('body does not start with boundary delimiter');

  while (true) {
    const headerStart = pos + delim.length;
    assertSeq(body, headerStart, Buffer.from('\r\n'));
    const headerEnd = body.indexOf(Buffer.from('\r\n\r\n'), headerStart);
    if (headerEnd === -1) throw new Error('part header never terminates');
    const headerBlock = body.subarray(headerStart + 2, headerEnd).toString('ascii');
    const headers: Record<string, string> = {};
    for (const line of headerBlock.split('\r\n')) {
      const colon = line.indexOf(':');
      if (colon === -1) throw new Error(`bad header line: ${line}`);
      headers[line.slice(0, colon).trim().toLowerCase()] = line.slice(colon + 1).trim();
    }
    const cr = headers['content-range'];
    const m = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(cr ?? '');
    if (m === null) throw new Error(`missing/invalid content-range: ${cr}`);
    const range = {
      start: Number(m[1]),
      end: Number(m[2]),
      complete: Number(m[3]),
    };
    const length = range.end - range.start + 1;
    const bytes = body.subarray(headerEnd + 4, headerEnd + 4 + length);
    if (bytes.length !== length) throw new Error('truncated part body');
    parts.push({ headers, bytes, range });

    const after = headerEnd + 4 + length;
    assertSeq(body, after, Buffer.from('\r\n'));
    pos = after + 2;
    if (body.subarray(pos, pos + endDelim.length).equals(endDelim)) {
      const tail = body.subarray(pos + endDelim.length);
      if (!tail.equals(Buffer.from('\r\n'))) {
        throw new Error('closing delimiter must be followed by CRLF only');
      }
      return parts;
    }
    if (!body.subarray(pos, pos + delim.length).equals(delim)) {
      throw new Error('expected next boundary delimiter');
    }
  }
}

function assertSeq(buf: Buffer, at: number, expected: Buffer): void {
  if (!buf.subarray(at, at + expected.length).equals(expected)) {
    throw new Error(`expected ${JSON.stringify(expected.toString())} at offset ${at}`);
  }
}

/** Extract boundary parameter from a multipart Content-Type value. */
export function boundaryOf(contentType: string): string {
  const m = /boundary=([!-~]+)$/.exec(contentType);
  if (m === null) throw new Error(`no boundary in content-type: ${contentType}`);
  return m[1]!;
}
