/**
 * Per-part header block parsing (RFC 7578 §4, RFC 9110 §5.1).
 *
 * Explicit rules:
 *  - the block is a sequence of CRLF-terminated `field-name ":" OWS value OWS`
 *    lines; obs-fold continuation lines are rejected (MALFORMED_HEADER_SYNTAX)
 *  - the block MUST be valid UTF-8. Raw non-ASCII octets are allowed only as
 *    content of the (quoted) `filename` value following deployed browser
 *    convention; the standards-track mechanism stays `filename*` (RFC 5987).
 *  - field names satisfy the RFC 9110 token grammar
 *  - field values may contain only VCHAR / SP / HTAB (obs-text rejected)
 *  - duplicate Content-Disposition or Content-TYPE lines inside one part are fatal
 */

import { ErrorCode, MultipartError } from './errors.js';
import {
  isToken,
  parseContentDisposition,
  parseMediaType,
  safeBasename,
  type Disposition
} from './header-values.js';
import type { PartInfo } from './types.js';

export interface RawHeader {
  name: string;
  value: string;
}

const VCHAR_SP_HTAB = /^[\t\x20-\x7e]*$/;

export function parseHeaderBlock(block: Buffer): RawHeader[] {
  let text: string;
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(block);
  } catch {
    throw new MultipartError(
      ErrorCode.MALFORMED_PART_HEADERS,
      'part header block is not valid UTF-8',
      {}
    );
  }
  if (text.length === 0) {
    throw new MultipartError(
      ErrorCode.MALFORMED_PART_HEADERS,
      'part header block is empty',
      {}
    );
  }
  const lines = text.split('\r\n');
  // split() never returns fewer than 1 element; every line must be non-empty
  // (the terminating empty element is NOT present here: callers strip CRLFCRLF).
  const headers: RawHeader[] = [];
  let sawDisposition = false;
  let sawContentType = false;
  for (const line of lines) {
    if (line === '') {
      throw new MultipartError(
        ErrorCode.MALFORMED_PART_HEADERS,
        'empty line inside part header block',
        {}
      );
    }
    if (line[0] === ' ' || line[0] === '\t') {
      throw new MultipartError(
        ErrorCode.MALFORMED_HEADER_SYNTAX,
        'obs-fold header continuation is not accepted',
        { line: line.slice(0, 32) }
      );
    }
    const colon = line.indexOf(':');
    if (colon <= 0) {
      throw new MultipartError(
        ErrorCode.MALFORMED_HEADER_SYNTAX,
        'header line missing ":" separator',
        { line: line.slice(0, 32) }
      );
    }
    const name = line.slice(0, colon);
    if (!isToken(name)) {
      throw new MultipartError(
        ErrorCode.HEADER_NAME_NOT_TOKEN,
        `header field name "${name}" is not a valid token`,
        { name }
      );
    }
    const value = line.slice(colon + 1).trim();
    if (!VCHAR_SP_HTAB.test(value)) {
      throw new MultipartError(
        ErrorCode.MALFORMED_HEADER_SYNTAX,
        `header "${name}" contains illegal octets (obs-text/control characters)`,
        { name }
      );
    }
    const lower = name.toLowerCase();
    if (lower === 'content-disposition') {
      if (sawDisposition) {
        throw new MultipartError(
          ErrorCode.DUPLICATE_CONTENT_DISPOSITION,
          'a part must not contain two Content-Disposition headers',
          {}
        );
      }
      sawDisposition = true;
    }
    if (lower === 'content-type') {
      if (sawContentType) {
        throw new MultipartError(
          ErrorCode.DUPLICATE_CONTENT_TYPE,
          'a part must not contain two Content-Type headers',
          {}
        );
      }
      sawContentType = true;
    }
    headers.push({ name: lower, value });
  }
  if (!sawDisposition) {
    throw new MultipartError(
      ErrorCode.MALFORMED_PART_HEADERS,
      'part is missing the mandatory Content-Disposition header',
      {}
    );
  }
  return headers;
}

export interface PartMeta {
  name: string;
  effectiveFilename: string | null;
  filename: string | null;
  filenameStar: string | null;
  filenameStarCharset: string | null;
  contentType: string | null;
  disposition: Disposition;
  rawHeaders: Buffer;
  index: number;
  isFile: boolean;
}

/** Interpret a raw header block into the normalized part metadata. */
export function buildPartMeta(block: Buffer, index: number): PartMeta {
  const headers = parseHeaderBlock(block);
  let disposition: Disposition | null = null;
  let contentType: string | null = null;
  for (const h of headers) {
    if (h.name === 'content-disposition') disposition = parseContentDisposition(h.value);
    else if (h.name === 'content-type') contentType = parseMediaType(h.value, 'part Content-Type').essence;
  }
  // disposition presence guaranteed by parseHeaderBlock
  const d = disposition!;
  // RFC 6266: filename* takes precedence over filename when both are present.
  const effectiveFilename = d.filenameStar ?? d.filename;
  const isFile = effectiveFilename !== null;
  return {
    name: d.name,
    effectiveFilename,
    filename: d.filename,
    filenameStar: d.filenameStar,
    filenameStarCharset: d.filenameStarCharset,
    contentType,
    disposition: d,
    rawHeaders: block,
    index,
    isFile
  };
}

/** Final validated descriptor, enriched by the storage layer at part end. */
export function toPartInfo(meta: PartMeta, size: number, sha256: string): PartInfo {
  return {
    name: meta.name,
    filename: meta.effectiveFilename,
    filenameStar: meta.filenameStar,
    filenameStarCharset: meta.filenameStarCharset,
    contentType: meta.contentType,
    rawHeaders: meta.rawHeaders,
    index: meta.index,
    size,
    sha256
  };
}

export { safeBasename };
