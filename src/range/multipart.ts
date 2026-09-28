import type { ByteInterval } from '../types.js';

/**
 * multipart/byteranges framing (RFC 9110 §14.1.3, format per Appendix A).
 *
 * The encoder is deliberately low-level and deterministic. Callers hand it
 * one already-fetched byte block per post-merge interval; the state layer
 * reads each block at the ORIGINAL byte offset. The encoder never recomputes
 * offsets from a compressed stream — objects are identity-encoded only.
 */

const CRLF = '\r\n';

export interface BodyPart {
  interval: ByteInterval;
  bytes: Buffer;
}

export interface MultipartEncoding {
  contentType: string;
  body: Buffer;
}

export function encodeMultipart(
  parts: BodyPart[],
  completeLength: number,
  contentType: string,
  boundary: string,
): MultipartEncoding {
  const chunks: Buffer[] = [];
  for (const part of parts) {
    const { interval, bytes } = part;
    if (bytes.length !== interval.end - interval.start + 1) {
      throw new Error(
        `Part bytes ${bytes.length} does not match interval ${interval.start}-${interval.end}`,
      );
    }
    const header =
      `--${boundary}${CRLF}` +
      `Content-Type: ${contentType}${CRLF}` +
      `Content-Range: bytes ${interval.start}-${interval.end}/${completeLength}${CRLF}` +
      CRLF;
    chunks.push(Buffer.from(header, 'ascii'));
    chunks.push(bytes);
    chunks.push(Buffer.from(CRLF, 'ascii'));
  }
  chunks.push(Buffer.from(`--${boundary}--${CRLF}`, 'ascii'));
  return {
    contentType: `multipart/byteranges; boundary=${boundary}`,
    body: Buffer.concat(chunks),
  };
}

/** Single-part Content-Range header value, e.g. "bytes 0-9/100". */
export function contentRangeValue(interval: ByteInterval, completeLength: number): string {
  return `bytes ${interval.start}-${interval.end}/${completeLength}`;
}
