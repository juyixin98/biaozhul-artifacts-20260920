/**
 * Per-part header section parsing and upload restriction enforcement.
 *
 * Explicit rules (each is asserted by contract tests):
 *  - Headers are folded-free RFC 5322-style lines split on CRLF. A header
 *    section is "Name: value"; whitespace after the colon is skipped.
 *  - Header names are case-insensitive. A header name appearing twice in the
 *    SAME part is DUPLICATE_HEADER.
 *  - Content-Disposition is REQUIRED and must be `form-data` with a `name`.
 *  - Only Content-Disposition and Content-Type are accepted in parts; the
 *    obsolete Content-Transfer-Encoding (forbidden by RFC 7578 §4.7) and any
 *    other header are UNSUPPORTED_HEADER.
 *  - A `filename` parameter marks a file part. `filename*` (RFC 5987) takes
 *    precedence when both are present.
 *  - Filenames are validated: no control chars, no path separators /
 *    traversal sequences, extension and declared Content-Type must be on the
 *    configured allow-lists.
 */

import {
  decodeExtValue,
  decodeRfc7578Bytes,
  assertNoEncodedWord,
} from './charset.js';
import { inputError } from './errors.js';
import { parseHeaderParameters } from './params.js';
import type { UploadRestrictions } from './config.js';
import type { PartMeta } from './types.js';

const KNOWN_PART_HEADERS = new Set([
  'content-disposition',
  'content-type',
]);

function splitHeaderLines(section: string): string[] {
  const lines: string[] = [];
  const rawLines = section.split('\r\n');
  for (const raw of rawLines) {
    // Obsolete line folding (continuation starting with SP/HTAB) is rejected.
    lines.push(raw);
  }
  return lines;
}

/**
 * Parse a complete header section (without the trailing blank line) into a
 * case-insensitive map. Duplicates are rejected.
 */
function collectHeaders(section: string): Map<string, string> {
  const map = new Map<string, string>();
  for (const line of splitHeaderLines(section)) {
    if (line.length === 0) {
      throw inputError('MALFORMED_HEADER', 'empty line inside header section');
    }
    const colon = line.indexOf(':');
    if (colon <= 0) {
      throw inputError('MALFORMED_HEADER', 'header line is missing ":"', { line });
    }
    const name = line.slice(0, colon).trim().toLowerCase();
    if (!/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(name)) {
      throw inputError('MALFORMED_HEADER', `illegal header name "${name}"`);
    }
    const value = line.slice(colon + 1).trim();
    if (map.has(name)) {
      throw inputError(
        'DUPLICATE_HEADER',
        `part contains a repeated "${name}" header`,
        { header: name },
      );
    }
    if (!KNOWN_PART_HEADERS.has(name)) {
      throw inputError(
        'UNSUPPORTED_HEADER',
        `part header "${name}" is not permitted; allowed: Content-Disposition, Content-Type`,
        { header: name },
      );
    }
    map.set(name, value);
  }
  return map;
}

function extname(filename: string): string {
  const slash = Math.max(filename.lastIndexOf('/'), filename.lastIndexOf('\\'));
  const base = slash >= 0 ? filename.slice(slash + 1) : filename;
  const dot = base.lastIndexOf('.');
  if (dot <= 0) return ''; // dot at 0 => ".env" style, treated as no extension
  return base.slice(dot).toLowerCase();
}

function assertSafeFilename(filename: string, restrictions: UploadRestrictions): void {
  if (filename.length === 0) {
    throw inputError('FILENAME_REJECTED', 'empty filename');
  }
  // NUL and other control characters can never appear in a stored name.
  // eslint-disable-next-line no-control-regex
  if (/[\x00-\x1f\x7f]/.test(filename)) {
    throw inputError('FILENAME_REJECTED', 'filename contains control characters', {
      reason: 'control-char',
    });
  }
  if (restrictions.rejectPathTraversal) {
    if (filename.includes('/') || filename.includes('\\')) {
      throw inputError(
        'FILENAME_REJECTED',
        'filename must not contain path separators',
        { reason: 'separator' },
      );
    }
    if (filename === '..' || filename.includes('..')) {
      throw inputError(
        'FILENAME_REJECTED',
        'filename must not contain a ".." segment',
        { reason: 'traversal' },
      );
    }
    if (filename === '.' || filename.startsWith('.')) {
      // ".env" style dotfiles: reject outright — the allow-list has no match
      // anyway, but a dedicated reason makes diagnostics clearer.
      throw inputError(
        'FILENAME_REJECTED',
        'dotfile filenames are not permitted',
        { reason: 'dotfile' },
      );
    }
  }
  const ext = extname(filename);
  if (!restrictions.allowedExtensions.includes(ext)) {
    throw inputError(
      'FILENAME_REJECTED',
      `extension "${ext || '(none)'}" is not on the upload allow-list`,
      { reason: 'extension', extension: ext },
    );
  }
}

/**
 * Parse one part's header section into {@link PartMeta} and enforce upload
 * restrictions. Throws a {@link MultipartError} with class INPUT_ERROR on any
 * violation.
 */
export function parsePartHeaders(
  section: string,
  restrictions: UploadRestrictions,
): PartMeta {
  const headers = collectHeaders(section);
  const headerLines = splitHeaderLines(section);

  const dispositionRaw = headers.get('content-disposition');
  if (dispositionRaw === undefined) {
    throw inputError(
      'MISSING_CONTENT_DISPOSITION',
      'every part must carry a Content-Disposition header',
    );
  }
  const disposition = parseHeaderParameters(dispositionRaw, 'Content-Disposition');
  if (disposition.value !== 'form-data') {
    throw inputError(
      'MISSING_CONTENT_DISPOSITION',
      `expected Content-Disposition: form-data, got "${disposition.value}"`,
      { received: disposition.value },
    );
  }

  const rawName = disposition.params.get('name');
  if (rawName === undefined || rawName.length === 0) {
    throw inputError(
      'MISSING_FIELD_NAME',
      'Content-Disposition is missing a non-empty name parameter',
    );
  }
  assertNoEncodedWord(rawName, 'part name');
  const name = decodeRfc7578Bytes(rawName, 'part name');

  const rawFilename = disposition.params.get('filename');
  const rawFilenameExt = disposition.params.get('filename*');

  let filename: string | undefined;
  if (rawFilenameExt !== undefined) {
    // RFC 5987 extended form wins when both are present.
    filename = decodeExtValue(rawFilenameExt, 'filename*');
  } else if (rawFilename !== undefined) {
    assertNoEncodedWord(rawFilename, 'filename');
    filename = decodeRfc7578Bytes(rawFilename, 'filename');
  }

  let contentType = (headers.get('content-type') ?? '').toLowerCase();
  if (contentType) {
    // Normalise: accept parameters (e.g. charset) but validate the media type.
    const ctParsed = parseHeaderParameters(headers.get('content-type')!, 'Content-Type');
    contentType = ctParsed.value;
  }

  const kind = filename !== undefined ? 'file' : 'field';

  if (kind === 'file') {
    assertSafeFilename(filename!, restrictions);
    // RFC 7578: a file without Content-Type defaults to application/octet-stream.
    const declaredType = contentType || 'application/octet-stream';
    if (!restrictions.allowedMimeTypes.includes(declaredType)) {
      throw inputError(
        'FILENAME_REJECTED',
        `Content-Type "${declaredType}" is not on the upload allow-list`,
        { reason: 'mime', mimeType: declaredType, filename: filename! },
      );
    }
    return {
      name,
      kind,
      filename,
      contentType: declaredType,
      headerLines,
    };
  }

  if (contentType && contentType !== 'text/plain') {
    // Fields are plain form values; a field claiming another media type is
    // almost certainly a client bug or an evasion attempt.
    throw inputError(
      'UNSUPPORTED_HEADER',
      `field parts must be text/plain, got "${contentType}"`,
      { header: 'content-type' },
    );
  }

  return {
    name,
    kind: 'field',
    contentType: 'text/plain',
    headerLines,
  };
}
