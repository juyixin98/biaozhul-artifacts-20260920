/**
 * Header charset rules (explicit, per RFC 7578 §4.2 / RFC 5987).
 *
 * Rules enforced here:
 *  - Ordinary `filename="..."` values carry raw bytes. RFC 7578 mandates
 *    UTF-8; the header scanner preserves bytes via latin1, and this module
 *    validates/decodes them as UTF-8 (fatal). Legacy RFC 2047 encoded-words
 *    ("=?utf-8?b?...?=") are explicitly NOT interpreted.
 *  - Extended `filename*=UTF-8''%E2%82%AC` uses the RFC 5987 ext-value
 *    production: charset "'" [ language ] "'" value-chars, where non-attr
 *    bytes are percent-encoded. Only UTF-8 and ISO-8859-1 charsets are
 *    accepted; anything else is a BAD_HEADER_ENCODING input error.
 */

import { inputError } from './errors.js';

const SUPPORTED_CHARSETS = new Set(['utf-8', 'utf8', 'iso-8859-1', 'latin1']);

/**
 * Decode a raw header fragment (preserved as latin1) per RFC 7578.
 * Pure ASCII passes through unchanged; otherwise bytes must be valid UTF-8.
 */
export function decodeRfc7578Bytes(latin1Fragment: string, where: string): string {
  if (/[\x80-\xff]/.test(latin1Fragment)) {
    const bytes = Buffer.from(latin1Fragment, 'latin1');
    try {
      return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    } catch {
      throw inputError(
        'BAD_HEADER_ENCODING',
        `${where} contains bytes that are not valid UTF-8`,
        { where },
      );
    }
  }
  return latin1Fragment;
}

/** RFC 5987 attr-char production (the un-percent-encoded allowed chars). */
const ATTR_CHAR = /[A-Za-z0-9!#$&+\-.^_`|~]/;

/**
 * Parse an RFC 5987 ext-value: charset "'" [ language ] "'" value-chars.
 * Example: `UTF-8''%E2%82%AC%20rates`.
 */
export function decodeExtValue(raw: string, where: string): string {
  const firstQuote = raw.indexOf("'");
  if (firstQuote < 0) {
    throw inputError(
      'BAD_HEADER_ENCODING',
      `${where} extended value is missing the charset/language delimiters`,
      { where },
    );
  }
  const charset = raw.slice(0, firstQuote).toLowerCase();
  const rest = raw.slice(firstQuote + 1);
  const secondQuote = rest.indexOf("'");
  if (secondQuote < 0) {
    throw inputError(
      'BAD_HEADER_ENCODING',
      `${where} extended value is missing the language/value delimiter`,
      { where },
    );
  }
  const valueChars = rest.slice(secondQuote + 1);
  if (valueChars.length === 0) {
    throw inputError(
      'BAD_HEADER_ENCODING',
      `${where} extended value is empty`,
      { where },
    );
  }
  if (!SUPPORTED_CHARSETS.has(charset)) {
    throw inputError(
      'BAD_HEADER_ENCODING',
      `${where} uses unsupported charset "${charset}"`,
      { where, charset, supported: ['utf-8', 'iso-8859-1'] },
    );
  }

  const out = Buffer.alloc(valueChars.length);
  let n = 0;
  for (let i = 0; i < valueChars.length; i++) {
    const ch = valueChars[i]!;
    if (ch === '%') {
      const hex = valueChars.slice(i + 1, i + 3);
      if (!/^[0-9A-Fa-f]{2}$/.test(hex)) {
        throw inputError(
          'BAD_HEADER_ENCODING',
          `${where} contains a malformed percent escape`,
          { where, at: i },
        );
      }
      out[n++] = parseInt(hex, 16);
      i += 2;
    } else if (ATTR_CHAR.test(ch)) {
      out[n++] = ch.charCodeAt(0);
    } else {
      throw inputError(
        'BAD_HEADER_ENCODING',
        `${where} contains a character not allowed in an extended value`,
        { where, char: ch, at: i },
      );
    }
  }

  const bytes = out.subarray(0, n);
  const useCharset = charset === 'utf-8' || charset === 'utf8' ? 'utf-8' : 'iso-8859-1';
  try {
    return new TextDecoder(useCharset, { fatal: true }).decode(bytes);
  } catch {
    throw inputError(
      'BAD_HEADER_ENCODING',
      `${where} payload is not valid ${useCharset}`,
      { where },
    );
  }
}

/**
 * Reject legacy RFC 2047 encoded-words explicitly, so callers never
 * accidentally interpret "=?utf-8?b?...?=" inside a filename.
 */
export function assertNoEncodedWord(value: string, where: string): void {
  if (/=\?[A-Za-z0-9-]+\?[bBqQ]\?/.test(value)) {
    throw inputError(
      'BAD_HEADER_ENCODING',
      `${where} uses obsolete RFC 2047 encoded-word encoding; send UTF-8 or filename* instead`,
      { where },
    );
  }
}
