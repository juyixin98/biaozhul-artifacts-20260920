/**
 * Independent multipart wire encoder used ONLY by tests and fixtures.
 *
 * It is deliberately hand-written and shares no code with the production
 * parser, so the expected byte vectors are a genuine independent oracle
 * rather than something the code under test generated about itself.
 */

export interface EncodedPart {
  name: string;
  filename?: string;
  /** RFC 5987 extended filename, emitted as the `filename*` parameter. */
  filenameStar?: string;
  contentType?: string;
  /** Body bytes. Field vs file is decided by presence of `filename`. */
  body: Buffer;
  /** Inject an extra raw header line (for negative cases). */
  extraHeaderLine?: string;
  /** Override the whole disposition line (for malformed cases). */
  rawDisposition?: string;
  /** Omit the Content-Disposition header entirely (negative case). */
  omitDisposition?: boolean;
  /** Emit a duplicated header name using this raw value (negative case). */
  duplicateHeader?: string;
}

export interface EncodeOptions {
  /** Append the closing delimiter. Default true. */
  terminator?: boolean;
  /** Bytes to emit after "--" + boundary instead of CRLF (negative cases). */
  closeSuffix?: Buffer;
  /** Replace the opening delimiter line entirely (negative cases). */
  badOpening?: Buffer;
  /** Use bare LF instead of CRLF between header lines (negative case). */
  bareLfHeaders?: boolean;
}

const CRLF = '\r\n';

function escapeQuoted(value: string): string {
  return value.replace(/["\\]/g, (m) => `\\${m}`);
}

function dispositionLine(p: EncodedPart): string {
  if (p.omitDisposition) return '';
  if (p.rawDisposition !== undefined) return p.rawDisposition;
  let line = 'Content-Disposition: form-data; name="' + escapeQuoted(p.name) + '"';
  if (p.filename !== undefined) line += `; filename="${escapeQuoted(p.filename)}"`;
  if (p.filenameStar !== undefined) line += `; filename*=${p.filenameStar}`;
  return line;
}

/** Encode a single part INCLUDING the preceding boundary line. */
export function encodePart(boundary: string, p: EncodedPart, opts: EncodeOptions = {}): Buffer {
  const nl = opts.bareLfHeaders ? '\n' : CRLF;
  const boundaryLine = Buffer.from(`--${boundary}${CRLF}`, 'ascii');
  const headerLines: string[] = [];
  const disp = dispositionLine(p);
  if (disp) headerLines.push(disp);
  if (p.contentType) headerLines.push(`Content-Type: ${p.contentType}`);
  if (p.extraHeaderLine) headerLines.push(p.extraHeaderLine);
  if (p.duplicateHeader) headerLines.push(p.duplicateHeader);
  // Boundary line always uses CRLF; bare LF applies inside the header block.
  const head = Buffer.concat([
    boundaryLine,
    Buffer.from(headerLines.join(nl) + nl + nl, 'latin1'),
  ]);
  return Buffer.concat([head, p.body]);
}

export function encodeMultipart(
  boundary: string,
  parts: EncodedPart[],
  opts: EncodeOptions = {},
): Buffer {
  const chunks: Buffer[] = [];
  for (const p of parts) {
    chunks.push(encodePart(boundary, p, opts));
    chunks.push(Buffer.from(CRLF, 'ascii'));
  }
  if (opts.terminator === false) {
    // Deliberately stop without the closing delimiter.
    return Buffer.concat(chunks);
  }
  const tail = opts.closeSuffix
    ? Buffer.concat([Buffer.from(`--${boundary}`, 'ascii'), opts.closeSuffix])
    : Buffer.from(`--${boundary}--${CRLF}`, 'ascii');
  chunks.push(tail);
  return Buffer.concat(chunks);
}

/** Deterministic pseudo-random byte filler (LCG), so vectors are reproducible. */
export function deterministicBytes(seed: number, length: number): Buffer {
  let state = seed >>> 0;
  const buf = Buffer.alloc(length);
  for (let i = 0; i < length; i++) {
    state = (state * 1664525 + 1013904223) >>> 0;
    buf[i] = state >>> 24;
  }
  return buf;
}

/** Every possible cut of `buf` into two pieces at positions 1..len-1. */
export function allSplits(buf: Buffer): Array<[Buffer, Buffer]> {
  const out: Array<[Buffer, Buffer]> = [];
  for (let at = 1; at < buf.length; at++) {
    out.push([buf.subarray(0, at), buf.subarray(at)]);
  }
  return out;
}

/** Reproducible chunk-size schedule (seeded), including 1-byte edges. */
export function chunkSchedule(buf: Buffer, seed: number, minSize = 1, maxSize = 7): Buffer[] {
  const chunks: Buffer[] = [];
  let state = seed >>> 0;
  let pos = 0;
  while (pos < buf.length) {
    state = (state * 1103515245 + 12345) >>> 0;
    const size = minSize + (state % (maxSize - minSize + 1));
    chunks.push(buf.subarray(pos, Math.min(buf.length, pos + size)));
    pos += size;
  }
  return chunks;
}
