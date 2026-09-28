/**
 * Structured header-value parsing for multipart contracts.
 *
 * Implements, deliberately strictly:
 *  - media type + parameters (RFC 9110 §8.3, §5.6.6) used for the HTTP
 *    Content-Type and per-part Content-Type headers
 *  - Content-Disposition `form-data` with `name`, `filename`, `filename*`
 *    (RFC 7578 §4.2, RFC 6266, RFC 5987)
 *  - `filename*` charset/language/value decoding (only UTF-8 accepted)
 *
 * Rules made explicit here (the spec leaves choices open):
 *  - duplicate parameters in one header field are MALFORMED_HEADER_SYNTAX
 *  - tokens must satisfy RFC 9110 tchar; quoted strings may contain
 *    quoted-pair escapes; obs-text / obs-fold are rejected
 *  - boundary values are additionally validated against RFC 2046 bcharsnospace
 *    and must be 1..70 characters
 */

import { ErrorCode, MultipartError } from './errors.js';

const TOKEN_CHARS = new Set(
  `!#$%&'*+-.^_\`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ`
);

export function isToken(s: string): boolean {
  if (s.length === 0) return false;
  for (const ch of s) if (!TOKEN_CHARS.has(ch)) return false;
  return true;
}

const VCHAR_AND_HTAB_SP = /^[\t\x20-\x7e]*$/;

/** RFC 2046 bcharsnospace (the set allowed in a boundary, excluding spaces). */
const BCHARS_NOSPACE = new Set(
  `'()+_,-./:=?0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ`
);

export interface RawParameter {
  name: string;
  value: string;
  /** true when the value originated from a quoted-string */
  quoted: boolean;
}

/**
 * Tokenize `;`-separated parameters. Quoted strings are honored (a `;` inside
 * quotes does not split). Returns parameters in document order.
 */
export function parseParameters(text: string, context: string): RawParameter[] {
  const params: RawParameter[] = [];
  let i = 0;
  const n = text.length;
  while (i < n) {
    while (i < n && (text[i] === ' ' || text[i] === '\t')) i++;
    if (i >= n) break;
    const nameStart = i;
    while (i < n && text[i] !== '=' && text[i] !== ';') i++;
    const name = text.slice(nameStart, i);
    if (name.length === 0 || !isToken(name)) {
      throw new MultipartError(
        ErrorCode.MALFORMED_HEADER_SYNTAX,
        `${context}: invalid parameter name near offset ${nameStart}`,
        { context, offset: nameStart }
      );
    }
    if (i >= n || text[i] === ';') {
      throw new MultipartError(
        ErrorCode.MALFORMED_HEADER_SYNTAX,
        `${context}: parameter "${name}" has no value`,
        { context, parameter: name }
      );
    }
    i++; // '='
    while (i < n && (text[i] === ' ' || text[i] === '\t')) i++;
    let value: string;
    let quoted = false;
    if (text[i] === '"') {
      quoted = true;
      const res = parseQuotedValue(text, i, context);
      value = res.value;
      i = res.next;
    } else {
      const valueStart = i;
      while (i < n && text[i] !== ';') i++;
      value = text.slice(valueStart, i).replace(/[ \t]+$/, '');
      if (value.length === 0 || !isToken(value)) {
        throw new MultipartError(
          ErrorCode.MALFORMED_HEADER_SYNTAX,
          `${context}: invalid token value for parameter "${name}"`,
          { context, parameter: name }
        );
      }
    }
    while (i < n && (text[i] === ' ' || text[i] === '\t')) i++;
    if (i < n && text[i] !== ';') {
      throw new MultipartError(
        ErrorCode.MALFORMED_HEADER_SYNTAX,
        `${context}: unexpected character after parameter "${name}"`,
        { context, parameter: name, offset: i }
      );
    }
    i++; // consume ';' (or i === n)
    params.push({ name: name.toLowerCase(), value, quoted });
  }
  return params;
}

function parseQuotedValue(
  text: string,
  start: number,
  context: string
): { value: string; next: number } {
  let i = start + 1; // skip opening quote
  let out = '';
  while (i < text.length) {
    const ch = text[i]!;
    if (ch === '"') return { value: out, next: i + 1 };
    if (ch === '\\') {
      // quoted-pair: backslash followed by HTAB / SP / VCHAR
      const next = text[i + 1];
      if (next === undefined || !/[\t\x20-\x7e]/.test(next)) {
        throw new MultipartError(
          ErrorCode.MALFORMED_HEADER_SYNTAX,
          `${context}: invalid quoted-pair escape near offset ${i}`,
          { context, offset: i }
        );
      }
      out += next;
      i += 2;
      continue;
    }
    if (ch === '\t' || ch === ' ' || /[\x21\x23-\x5b\x5d-\x7e]/.test(ch)) {
      out += ch;
      i++;
      continue;
    }
    throw new MultipartError(
      ErrorCode.MALFORMED_HEADER_SYNTAX,
      `${context}: illegal byte in quoted-string at offset ${i}`,
      { context, offset: i }
    );
  }
  throw new MultipartError(
    ErrorCode.MALFORMED_HEADER_SYNTAX,
    `${context}: unterminated quoted-string`,
    { context }
  );
}

/** Case-insensitive parameter lookup that rejects duplicates. */
export function parameterMap(
  params: RawParameter[],
  context: string
): Map<string, RawParameter> {
  const map = new Map<string, RawParameter>();
  for (const p of params) {
    if (map.has(p.name)) {
      throw new MultipartError(
        ErrorCode.MALFORMED_HEADER_SYNTAX,
        `${context}: duplicate parameter "${p.name}"`,
        { context, parameter: p.name }
      );
    }
    map.set(p.name, p);
  }
  return map;
}

export interface MediaType {
  type: string;
  subtype: string;
  /** canonical lower-case `type/subtype` */
  essence: string;
  params: Map<string, RawParameter>;
}

/** Parse `type/subtype ; params` (e.g. the HTTP Content-Type field). */
export function parseMediaType(raw: string, context: string): MediaType {
  const semi = raw.indexOf(';');
  const essenceRaw = (semi === -1 ? raw : raw.slice(0, semi)).trim();
  const slash = essenceRaw.indexOf('/');
  if (slash === -1) {
    throw new MultipartError(
      ErrorCode.MALFORMED_CONTENT_TYPE,
      `${context}: media type missing "/"`,
      { context }
    );
  }
  const type = essenceRaw.slice(0, slash).trim().toLowerCase();
  const subtype = essenceRaw.slice(slash + 1).trim().toLowerCase();
  if (!isToken(type) || !isToken(subtype)) {
    throw new MultipartError(
      ErrorCode.MALFORMED_CONTENT_TYPE,
      `${context}: invalid media type "${essenceRaw}"`,
      { context, raw: essenceRaw }
    );
  }
  const params =
    semi === -1
      ? new Map<string, RawParameter>()
      : parameterMap(parseParameters(raw.slice(semi + 1), context), context);
  return { type, subtype, essence: `${type}/${subtype}`, params };
}

/** Parse and validate the boundary parameter of a multipart media type. */
export function parseMultipartContentType(raw: string): {
  boundary: string;
  mediaType: MediaType;
} {
  const mediaType = parseMediaType(raw, 'Content-Type');
  if (mediaType.type !== 'multipart' || mediaType.subtype !== 'form-data') {
    throw new MultipartError(
      ErrorCode.MALFORMED_CONTENT_TYPE,
      `expected multipart/form-data, got ${mediaType.essence}`,
      { got: mediaType.essence }
    );
  }
  const boundaryParam = mediaType.params.get('boundary');
  if (!boundaryParam) {
    throw new MultipartError(
      ErrorCode.MISSING_BOUNDARY,
      'multipart/form-data Content-Type is missing the boundary parameter',
      {}
    );
  }
  const boundary = boundaryParam.value;
  if (boundary.length === 0 || boundary.length > 70) {
    throw new MultipartError(
      ErrorCode.MISSING_BOUNDARY,
      `boundary must be 1..70 characters, got ${boundary.length}`,
      { length: boundary.length }
    );
  }
  for (const ch of boundary) {
    if (!BCHARS_NOSPACE.has(ch)) {
      throw new MultipartError(
        ErrorCode.MISSING_BOUNDARY,
        `boundary contains character not permitted by RFC 2046 bcharsnospace: ${JSON.stringify(ch)}`,
        { character: ch }
      );
    }
  }
  return { boundary, mediaType };
}

export interface Disposition {
  type: string;
  name: string;
  filename: string | null;
  filenameStar: string | null;
  filenameStarCharset: string | null;
  filenameStarLanguage: string | null;
}

/** Parse a per-part Content-Disposition header into the normalized form. */
export function parseContentDisposition(raw: string): Disposition {
  const semi = raw.indexOf(';');
  const type = (semi === -1 ? raw : raw.slice(0, semi)).trim().toLowerCase();
  if (type !== 'form-data') {
    throw new MultipartError(
      ErrorCode.UNSUPPORTED_DISPOSITION,
      `unsupported Content-Disposition "${type}" (only form-data accepted)`,
      { disposition: type }
    );
  }
  const params =
    semi === -1
      ? new Map<string, RawParameter>()
      : parameterMap(parseParameters(raw.slice(semi + 1), 'Content-Disposition'), 'Content-Disposition');

  const nameParam = params.get('name');
  if (!nameParam) {
    throw new MultipartError(
      ErrorCode.MISSING_DISPOSITION_NAME,
      'Content-Disposition form-data part is missing the name parameter',
      {}
    );
  }
  if (nameParam.value.length === 0) {
    throw new MultipartError(
      ErrorCode.EMPTY_FIELD_NAME,
      'Content-Disposition name parameter must not be empty',
      {}
    );
  }

  const filenameParam = params.get('filename');
  let filename: string | null = null;
  if (filenameParam) {
    filename = filenameParam.value; // quoted-pair escapes already applied
    if (filename.length === 0) filename = null; // empty filename => not a file
  }

  let filenameStar: string | null = null;
  let filenameStarCharset: string | null = null;
  let filenameStarLanguage: string | null = null;
  const starParam = params.get('filename*');
  if (starParam) {
    const decoded = decodeExtValue(starParam.value, 'filename*');
    filenameStar = decoded.value;
    filenameStarCharset = decoded.charset;
    filenameStarLanguage = decoded.language;
    if (filenameStar.length === 0) filenameStar = null;
  }

  return {
    type,
    name: nameParam.value,
    filename,
    filenameStar,
    filenameStarCharset,
    filenameStarLanguage
  };
}

/**
 * Decode an RFC 5987 ext-value: charset "'" [language] "'" value-chars.
 * Only UTF-8 is accepted; percent-decoding is strict and UTF-8 decoding is fatal.
 */
export function decodeExtValue(
  raw: string,
  context: string
): { value: string; charset: string; language: string } {
  const firstQuote = raw.indexOf("'");
  if (firstQuote === -1) {
    throw new MultipartError(
      ErrorCode.MALFORMED_FILENAME_ENCODING,
      `${context}: ext-value missing charset/language delimiters`,
      { raw }
    );
  }
  const charset = raw.slice(0, firstQuote);
  const rest1 = raw.slice(firstQuote + 1);
  const secondQuote = rest1.indexOf("'");
  if (secondQuote === -1) {
    throw new MultipartError(
      ErrorCode.MALFORMED_FILENAME_ENCODING,
      `${context}: ext-value missing second "'" delimiter`,
      { raw }
    );
  }
  const language = rest1.slice(0, secondQuote);
  const valueChars = rest1.slice(secondQuote + 1);
  if (charset.toLowerCase() !== 'utf-8') {
    throw new MultipartError(
      ErrorCode.UNSUPPORTED_ENCODING,
      `${context}: only UTF-8 ext-value charset is supported, got "${charset}"`,
      { charset }
    );
  }
  if (language !== '' && !/^[A-Za-z0-9-]+$/.test(language)) {
    throw new MultipartError(
      ErrorCode.MALFORMED_FILENAME_ENCODING,
      `${context}: invalid language tag "${language}"`,
      { language }
    );
  }
  // value-chars = *( pct-encoded / attr-char )
  const bytes: number[] = [];
  let i = 0;
  while (i < valueChars.length) {
    const ch = valueChars[i]!;
    if (ch === '%') {
      const hex = valueChars.slice(i + 1, i + 3);
      if (!/^[0-9A-Fa-f]{2}$/.test(hex)) {
        throw new MultipartError(
          ErrorCode.MALFORMED_FILENAME_ENCODING,
          `${context}: malformed percent-escape near offset ${i}`,
          { offset: i, raw: valueChars.slice(i, i + 3) }
        );
      }
      bytes.push(parseInt(hex, 16));
      i += 3;
    } else if (/[A-Za-z0-9!#$&+\-.^_`|~]/.test(ch)) {
      bytes.push(ch.charCodeAt(0));
      i++;
    } else {
      throw new MultipartError(
        ErrorCode.MALFORMED_FILENAME_ENCODING,
        `${context}: illegal character in ext-value at offset ${i}`,
        { character: ch, offset: i }
      );
    }
  }
  const decoder = new TextDecoder('utf-8', { fatal: true });
  try {
    const value = decoder.decode(Uint8Array.from(bytes));
    return { value, charset: 'UTF-8', language };
  } catch {
    throw new MultipartError(
      ErrorCode.MALFORMED_FILENAME_ENCODING,
      `${context}: percent-decoded bytes are not valid UTF-8`,
      { raw }
    );
  }
}

/** Reject empty names, control chars, NUL, path separators and `..` segments. */
export function safeBasename(filename: string, context: string): string {
  if (filename.length === 0) {
    throw new MultipartError(
      ErrorCode.PATH_TRAVERSAL_FILENAME,
      `${context}: empty filename`,
      {}
    );
  }
  if (/[\x00-\x1f\x7f]/.test(filename)) {
    throw new MultipartError(
      ErrorCode.PATH_TRAVERSAL_FILENAME,
      `${context}: filename contains control characters`,
      { filename }
    );
  }
  if (filename.includes('/') || filename.includes('\\')) {
    throw new MultipartError(
      ErrorCode.PATH_TRAVERSAL_FILENAME,
      `${context}: filename must not contain path separators`,
      { filename }
    );
  }
  if (filename === '..' || filename === '.' || filename.includes('..')) {
    throw new MultipartError(
      ErrorCode.PATH_TRAVERSAL_FILENAME,
      `${context}: filename must not contain ".." segments`,
      { filename }
    );
  }
  if (filename !== filename.trim() || filename.endsWith('.')) {
    throw new MultipartError(
      ErrorCode.PATH_TRAVERSAL_FILENAME,
      `${context}: filename must not have surrounding whitespace or a trailing dot`,
      { filename }
    );
  }
  return filename;
}

export function isHeaderValueText(raw: string): boolean {
  return VCHAR_AND_HTAB_SP.test(raw);
}
