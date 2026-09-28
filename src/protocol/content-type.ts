/**
 * HTTP Content-Type envelope parsing.
 *
 * Extracts and validates the `multipart/form-data; boundary=...` envelope.
 * The boundary delimiter in the body is CRLF "--" + boundary. Per RFC 2046
 * the boundary value is 1..70 bchars; a longer value is rejected before any
 * body is consumed (RESOURCE_LIMIT / BOUNDARY_TOO_LONG) so an attacker cannot
 * make us buffer an unbounded delimiter.
 */

import {
  inputError,
  resourceError,
} from './errors.js';
import { parseHeaderParameters } from './params.js';
import type { MultipartLimits } from './config.js';

/** bchars / bcharsnospace per RFC 2046. */
const BOUNDARY_CHAR = /^[A-Za-z0-9'()+_,\-./:=?]$/;

export interface MultipartEnvelope {
  contentType: string;
  boundary: string;
  /** Wire delimiter that starts every boundary line ("--" + boundary). */
  boundaryDelimiter: string;
}

/**
 * @param contentTypeHeader raw value of the request Content-Type header
 * @param limits            active limits (maxBoundaryLength is enforced)
 */
export function parseMultipartContentType(
  contentTypeHeader: string | undefined,
  limits: MultipartLimits,
): MultipartEnvelope {
  if (!contentTypeHeader) {
    throw inputError('NOT_MULTIPART', 'request is missing a Content-Type header');
  }
  let parsed;
  try {
    parsed = parseHeaderParameters(contentTypeHeader, 'Content-Type');
  } catch (err) {
    // Parameter-level malformedness becomes a Content-Type envelope error.
    throw inputError('NOT_MULTIPART', 'Content-Type header is malformed', {
      reason: (err as Error).message,
    });
  }
  if (parsed.value !== 'multipart/form-data') {
    throw inputError(
      'NOT_MULTIPART',
      `expected multipart/form-data, got "${parsed.value}"`,
      { received: parsed.value },
    );
  }
  const boundary = parsed.params.get('boundary');
  if (boundary === undefined) {
    throw inputError(
      'MISSING_BOUNDARY',
      'multipart/form-data Content-Type is missing the boundary parameter',
    );
  }
  if (boundary.length === 0) {
    throw inputError('MISSING_BOUNDARY', 'boundary parameter is empty');
  }
  if (boundary.length > limits.maxBoundaryLength) {
    throw resourceError(
      'BOUNDARY_TOO_LONG',
      `boundary is ${boundary.length} bytes; limit is ${limits.maxBoundaryLength}`,
      { length: boundary.length, limit: limits.maxBoundaryLength },
    );
  }
  for (const ch of boundary) {
    if (!BOUNDARY_CHAR.test(ch)) {
      throw inputError(
        'MISSING_BOUNDARY',
        'boundary contains a character outside the RFC 2046 bchars set',
        { char: ch },
      );
    }
  }
  return {
    contentType: 'multipart/form-data',
    boundary,
    boundaryDelimiter: `--${boundary}`,
  };
}
