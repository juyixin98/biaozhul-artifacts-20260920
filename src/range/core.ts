import type {
  ImmutableObjectMeta,
  RangeLimits,
  RangeResolution,
  ResolveRejectCode,
  SpecError,
} from '../types.js';
import { evaluateIfRange, parseRangeHeader } from './parser.js';
import { mergeIntervals, resolveSpecs } from './intervals.js';

/**
 * Execution core: given ONLY request header text + representation metadata
 * + configured limits, decide what to serve and why. It performs no I/O —
 * byte slicing and HTTP framing live elsewhere — so the decision itself is
 * exhaustively unit-testable.
 *
 * Status policy (RFC 9110 §14.1.2 / §13.1.11):
 *  - no Range / ignored unit / If-Range mismatch -> 200 full
 *  - malformed Range / abusive request           -> 400
 *  - valid Range, all specs unsatisfiable        -> 416
 *  - one or more specs satisfiable               -> 206 (single/multi)
 */

export interface CoreInput {
  rangeHeader: string | undefined;
  ifRangeHeader: string | undefined;
  meta: ImmutableObjectMeta;
  limits: RangeLimits;
}

/**
 * Loose upper bound on multipart framing overhead so the total-response
 * cap cannot be bypassed by requesting thousands of tiny pieces. The
 * Content-Range line is sized for 21-digit positions, larger than any
 * safe-integer object we can actually hold.
 */
function multipartOverheadBound(
  partCount: number,
  boundary: string,
  contentType: string,
): number {
  const perPart =
    2 + boundary.length + 2 + // --boundary\r\n
    `content-type: ${contentType}\r\n`.length +
    'content-range: bytes 000000000000000000000-000000000000000000000/000000000000000000000\r\n'.length +
    2; // blank line before the part body
  const closing = 2 + boundary.length + 2 + 2; // --boundary--\r\n
  return partCount * perPart + closing;
}

export function resolveRangeRequest(
  input: CoreInput,
  boundary = 'RANGEBOUNDARY',
): RangeResolution {
  const { rangeHeader, ifRangeHeader, meta, limits } = input;

  // 1) No Range at all: plain GET semantics.
  if (rangeHeader === undefined || rangeHeader.trim() === '') {
    return full('NO_RANGE_HEADER');
  }

  // 2) Syntax first.
  const parsed = parseRangeHeader(rangeHeader);
  if (!parsed.ok) {
    if (parsed.code === 'UNSUPPORTED_RANGE_UNIT') {
      // Unknown units are ignored (RFC 9110: a server MAY ignore ranges it
      // cannot understand), falling back to the full representation.
      return full('UNSUPPORTED_UNIT_IGNORED');
    }
    return reject(parsed.code, 400, parsed.message, parsed.specError);
  }

  // 3) If-Range gates everything: a client whose cached copy is stale (or
  //    whose precondition cannot be evaluated) gets the full representation
  //    with 200, never a partial response.
  const verdict = evaluateIfRange(ifRangeHeader, {
    etag: meta.etag,
    lastModified: meta.lastModified,
  });
  if (verdict.outcome === 'mismatch') {
    return full('IF_RANGE_MISMATCH');
  }
  if (verdict.outcome === 'indeterminate') {
    return full('IF_RANGE_INDETERMINATE');
  }

  // 4) Spec-count cap, checked on the raw parsed count BEFORE clamping:
  //    "10000 ranges, all duplicates" is still abusive.
  if (parsed.specs.length > limits.maxSpecs) {
    return reject(
      'TOO_MANY_RANGES',
      400,
      `Range header carries ${parsed.specs.length} specs; limit is ${limits.maxSpecs}`,
    );
  }

  // 5) Resolve against actual size. Zero-length objects can never serve a
  //    206; a syntactically valid range on one is unsatisfiable.
  const resolved = resolveSpecs(parsed.specs, meta.size);
  if (resolved.intervals.length === 0) {
    const isEmpty = meta.size === 0;
    return {
      decision: 'REJECT',
      status: 416,
      code: isEmpty ? 'OBJECT_EMPTY' : 'UNSATISFIABLE_RANGE',
      message: isEmpty
        ? 'Range requested on a zero-length object: no bytes can be returned'
        : `All ${parsed.specs.length} range spec(s) fall outside the ${meta.size}-byte object`,
      dropped: resolved.dropped,
      intervals: [],
    };
  }

  // 6) Merge (adjacency-aware) so overlapping/duplicate specs are served
  //    once. Partial validity is preserved in `dropped`.
  const { intervals, stats } = mergeIntervals(resolved.intervals, limits.mergeGap);
  const servedBytes = intervals.reduce((sum, iv) => sum + (iv.end - iv.start + 1), 0);

  // 7) Total response budget. For multipart include a framing bound; for
  //    the single-part case Content-Range lives in headers, not the body.
  const partCount = intervals.length;
  const overhead =
    partCount > 1
      ? multipartOverheadBound(partCount, boundary, meta.contentType)
      : 0;
  if (servedBytes + overhead > limits.maxResponseBytes) {
    return reject(
      'RESPONSE_SIZE_LIMIT_EXCEEDED',
      400,
      `Response would be ${servedBytes + overhead} bytes (limit ${limits.maxResponseBytes}); narrow the range(s)`,
    );
  }

  const common = {
    dropped: resolved.dropped,
    mergeCount: stats.mergeCount,
    requestedBytes: resolved.requestedBytes,
    servedBytes,
  } as const;

  if (partCount === 1) {
    return {
      decision: 'SINGLE_PART',
      status: 206,
      intervals: [intervals[0]!],
      ...common,
    };
  }
  return {
    decision: 'MULTIPART',
    status: 206,
    intervals,
    ...common,
  };
}

function full(
  reason:
    | 'NO_RANGE_HEADER'
    | 'UNSUPPORTED_UNIT_IGNORED'
    | 'IF_RANGE_MISMATCH'
    | 'IF_RANGE_INDETERMINATE',
): RangeResolution {
  return {
    decision: 'FULL_REPRESENTATION',
    status: 200,
    reason,
    intervals: [],
    dropped: [],
  };
}

function reject(
  code: ResolveRejectCode,
  status: 400 | 416,
  message: string,
  specError?: SpecError,
): RangeResolution {
  return {
    decision: 'REJECT',
    status,
    code,
    message,
    ...(specError === undefined ? {} : { specError }),
    dropped: [],
    intervals: [],
  };
}
