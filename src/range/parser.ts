import type {
  IfRangeVerdict,
  ParsedSpec,
  RangeParseResult,
} from '../types.js';

/**
 * Contract parsing for HTTP range requests (RFC 9110 §13, §14).
 *
 * This layer is purely syntactic: it validates the Range header and the
 * If-Range precondition against header text. It performs NO object lookups
 * and NO size clamping — that is the execution core's job. Keeping syntax
 * separate from resolution lets us prove, for example, that `bytes=0-0` on
 * a 10-byte object parses but is a perfectly normal interval rather than
 * an error.
 */

// RFC 9110: byte-range-spec = first-byte-pos "-" [ last-byte-pos ]
// Offsets are 1*DIGIT with no grammar-level bound. We parse them as
// arbitrary-precision BigInts: a 40-digit `bytes=<huge>-` is legal syntax
// that simply resolves to unsatisfiable for any real object, and
// `bytes=-<huge>` clamps to the whole representation — neither is a 400.
const INT_RE = /^(0|[1-9][0-9]*)$/;
// Header lines are already length-bounded by the HTTP server; still cap
// individual integer digit runs defensively.
const MAX_INT_DIGITS = 100;

function malformed(specError?: {
  index: number;
  raw: string;
  reason: string;
}): RangeParseResult {
  return {
    ok: false,
    code: 'MALFORMED_RANGE_HEADER',
    message: specError
      ? `Invalid byte range at position ${specError.index}: "${specError.raw}" (${specError.reason})`
      : 'Malformed or unsupported Range header',
    specError,
  };
}

function parseNonNegativeInt(
  text: string,
  index: number,
  raw: string,
  role: string,
): bigint | RangeParseResult {
  if (!INT_RE.test(text) || text.length > MAX_INT_DIGITS) {
    return malformed({
      index,
      raw,
      reason: `${role} must be a non-negative integer (max ${MAX_INT_DIGITS} digits)`,
    });
  }
  return BigInt(text);
}

/**
 * Parse a Range header value.
 *
 * Accepted (bytes unit only):
 *   bytes=0-499          closed
 *   bytes=500-           open-ended (suffix from an offset)
 *   bytes=-500           suffix (last 500 bytes)
 *
 * Rejected: other units, empty specs, non-integer integers,
 * last-byte-pos < first-byte-pos, negative numbers, embedded whitespace,
 * multiple equals signs.
 */
export function parseRangeHeader(header: string | undefined): RangeParseResult {
  if (header === undefined) {
    return { ok: false, code: 'MALFORMED_RANGE_HEADER', message: 'No Range header' };
  }
  const eq = header.indexOf('=');
  if (eq <= 0 || header.indexOf('=', eq + 1) !== -1) {
    return malformed();
  }
  const unit = header.slice(0, eq).trim().toLowerCase();
  if (unit !== 'bytes') {
    return {
      ok: false,
      code: 'UNSUPPORTED_RANGE_UNIT',
      message: `Unsupported range unit "${unit}": only bytes is implemented`,
    };
  }
  const body = header.slice(eq + 1);
  if (body.length === 0) return malformed();

  const pieces = body.split(',');
  const specs: ParsedSpec[] = [];
  for (let i = 0; i < pieces.length; i++) {
    const raw = pieces[i]!.trim();
    if (raw === '') {
      return malformed({ index: i, raw: pieces[i]!, reason: 'empty range spec' });
    }
    const dash = raw.indexOf('-');
    // Exactly one dash. "a-b" (closed), "a-" (open-ended) and "-n"
    // (suffix) are all legal; empty pieces are handled below.
    if (dash === -1 || raw.indexOf('-', dash + 1) !== -1) {
      return malformed({ index: i, raw, reason: 'expected "<a>-<b>", "<a>-" or "-<n>"' });
    }

    const left = raw.slice(0, dash);
    const right = raw.slice(dash + 1);

    if (left === '') {
      // Suffix range: bytes=-N
      if (right === '') {
        return malformed({ index: i, raw, reason: 'suffix range needs a length: "-N"' });
      }
      const n = parseNonNegativeInt(right, i, raw, 'suffix length');
      if (typeof n !== 'bigint') return n;
      specs.push({ index: i, raw, kind: 'suffix', suffixLength: n });
      continue;
    }

    const first = parseNonNegativeInt(left, i, raw, 'first byte position');
    if (typeof first !== 'bigint') return first;

    if (right === '') {
      specs.push({ index: i, raw, kind: 'open-ended', firstBytePos: first });
      continue;
    }
    const last = parseNonNegativeInt(right, i, raw, 'last byte position');
    if (typeof last !== 'bigint') return last;
    if (last < first) {
      return malformed({
        index: i,
        raw,
        reason: `last-byte-pos ${last} precedes first-byte-pos ${first}`,
      });
    }
    specs.push({
      index: i,
      raw,
      kind: 'closed',
      firstBytePos: first,
      lastBytePos: last,
    });
  }

  return { ok: true, unit: 'bytes', specs };
}

/**
 * Evaluate an If-Range precondition against the selected representation's
 * validators.
 *
 * RFC 9110 §13.1.11:
 *  - If-Range holding an entity-tag matches only a STRONG comparison; weak
 *    validators never match and the client must get the full representation.
 *  - If-Range holding an HTTP-date matches when it equals the
 *    representation's Last-Modified at whole-second precision; a value that
 *    is neither a quoted entity-tag nor a parseable date is
 *    "indeterminate" (we cannot prove the copy current, so conservatively
 *    return the full representation).
 */
export function evaluateIfRange(
  ifRange: string | undefined,
  validators: { etag: string; lastModified: Date },
): IfRangeVerdict {
  if (ifRange === undefined || ifRange.trim() === '') return { outcome: 'absent' };
  const value = ifRange.trim();

  const looksLikeEtag = value.startsWith('"') || value.startsWith('W/');
  if (looksLikeEtag) {
    if (value.startsWith('W/')) {
      return {
        outcome: 'mismatch',
        kind: 'etag-weak',
        detail: 'If-Range uses a weak entity-tag; only strong comparison permits a range',
      };
    }
    if (!value.endsWith('"') || value.length < 2) {
      return { outcome: 'indeterminate', detail: 'Malformed entity-tag in If-Range' };
    }
    if (value === validators.etag) {
      return { outcome: 'match' };
    }
    return {
      outcome: 'mismatch',
      kind: 'etag-strong',
      detail: `If-Range etag ${value} does not match current ${validators.etag}`,
    };
  }

  const stamped = Date.parse(value);
  if (Number.isNaN(stamped)) {
    return {
      outcome: 'indeterminate',
      detail: `If-Range value is neither a quoted entity-tag nor a parseable HTTP-date: "${value}"`,
    };
  }
  const headerSec = Math.floor(stamped / 1000);
  const metaSec = Math.floor(validators.lastModified.getTime() / 1000);
  if (headerSec === metaSec) return { outcome: 'match' };
  return {
    outcome: 'mismatch',
    kind: 'date',
    detail: `If-Range date ${new Date(stamped).toUTCString()} != Last-Modified ${validators.lastModified.toUTCString()}`,
  };
}
