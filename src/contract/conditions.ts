/**
 * Conditional request evaluation (RFC 9110 §13.1 – §13.2).
 *
 * Comparison mode is selected by the REQUEST METHOD:
 *   - safe methods (GET/HEAD): If-None-Match uses the WEAK comparison and a
 *     match yields 304 (Not Modified); If-Modified-Since is the date fallback.
 *   - state-changing methods (PUT/PATCH/DELETE): If-Match uses the STRONG
 *     comparison, If-None-Match uses the WEAK comparison, and a failed
 *     precondition yields 412; If-Modified-Since is ignored.
 *
 * Precedence when both entity-tag and date validators are present is fixed
 * (RFC 9110 §13.2.2), and recorded as an ordered trace:
 *
 *   safe:          If-None-Match  →  If-Modified-Since
 *   state-changing: If-Match / If-Unmodified-Since (step 1)  →  If-None-Match (step 2)
 *
 * The evaluator is pure: it decides whether the operation may proceed given a
 * selected representation. The kernel performs the read and the write inside
 * one database transaction, so the ETag/body of the response always come from
 * the same committed snapshot.
 */
import {
  EntityTag,
  IfList,
  formatETag,
  listMatches,
  parseIfList,
  strongCompare,
  weakCompare
} from './etag.js';
import { parseHttpDate, formatHttpDate, truncateToSeconds } from './http-date.js';
import {
  MalformedConditionError,
  NotModifiedError,
  PreconditionFailedError,
  TraceStep
} from './errors.js';

/** Current validators, attached to 412 responses so clients can reconcile. */
export interface CurrentValidator {
  readonly etag: string;
  readonly lastModified: string;
}

export type HttpMethod = 'GET' | 'HEAD' | 'PUT' | 'PATCH' | 'DELETE';

const SAFE_METHODS: ReadonlySet<string> = new Set(['GET', 'HEAD']);

export interface RawConditionHeaders {
  readonly ifMatch?: string | undefined;
  readonly ifNoneMatch?: string | undefined;
  readonly ifModifiedSince?: string | undefined;
  readonly ifUnmodifiedSince?: string | undefined;
}

export interface CurrentRepresentation {
  readonly etag: EntityTag;
  /** Last-Modified in epoch milliseconds. */
  readonly lastModifiedMs: number;
}

export type Selection =
  | { readonly kind: 'present'; readonly representation: CurrentRepresentation }
  | { readonly kind: 'absent' };

export interface ConditionDecision {
  readonly allowed: boolean;
  readonly trace: readonly TraceStep[];
}

function requireIfList(headerName: string, raw: string): IfList {
  const list = parseIfList(raw);
  if (!list) throw new MalformedConditionError(headerName, raw, 'not a valid entity-tag list');
  return list;
}

/**
 * Evaluate all applicable preconditions.
 * Throws MalformedConditionError (400), NotModifiedError (304) or
 * PreconditionFailedError (412); returns nothing on success (the trace is
 * attached to the thrown errors for diagnostics).
 */
export function evaluatePreconditions(
  method: HttpMethod,
  headers: RawConditionHeaders,
  selection: Selection
): ConditionDecision {
  const trace: TraceStep[] = [];
  const currentValidator = deriveValidator(selection);
  const safe = SAFE_METHODS.has(method);
  return safe
    ? evaluateSafe(headers, selection, trace)
    : evaluateStateChanging(method, headers, selection, trace, currentValidator);
}

/** Current validators for a 412 response, derived from the selected representation. */
function deriveValidator(selection: Selection): CurrentValidator | null {
  if (selection.kind === 'absent') return null;
  const { etag, lastModifiedMs } = selection.representation;
  return {
    etag: formatETag(etag.opaque, etag.weak),
    lastModified: formatHttpDate(lastModifiedMs)
  };
}

function evaluateSafe(
  headers: RawConditionHeaders,
  selection: Selection,
  trace: TraceStep[]
): ConditionDecision {
  // Step 1: If-None-Match always takes precedence over If-Modified-Since.
  if (headers.ifNoneMatch !== undefined) {
    const list = requireIfList('If-None-Match', headers.ifNoneMatch);
    let matched = false;
    if (selection.kind === 'present') {
      const actual = selection.representation.etag;
      matched = listMatches(actual, list, weakCompare);
      trace.push({
        header: 'If-None-Match',
        comparison: 'weak',
        expected: headers.ifNoneMatch.trim(),
        actual: `${actual.weak ? 'W/' : ''}"${actual.opaque}"`,
        result: matched ? 'match' : 'no-match',
        basis: 'safe method: weak comparison per RFC 9110 §13.1.3'
      });
    } else {
      trace.push({
        header: 'If-None-Match',
        comparison: 'weak',
        expected: headers.ifNoneMatch.trim(),
        actual: '<no selected representation>',
        result: 'no-match',
        basis: 'wildcard/tag list cannot match an absent representation'
      });
    }
    // Per RFC 9110 §13.1.4, If-Modified-Since is ignored when If-None-Match
    // is present. Record this BEFORE returning/304 so the precedence is always
    // visible in the decision trail.
    if (headers.ifModifiedSince !== undefined) {
      trace.push(ignored('If-Modified-Since', headers.ifModifiedSince,
        'ignored because If-None-Match is present (RFC 9110 §13.1.4)'));
    }
    if (matched) throw new NotModifiedError(trace);
    return { allowed: true, trace };
  }

  // Step 2: date-only conditional GET/HEAD.
  if (headers.ifModifiedSince !== undefined) {
    const since = parseHttpDate(headers.ifModifiedSince);
    if (!since) {
      // A malformed If-Modified-Since is ignored, not an error (RFC 9110 §13.1.4).
      trace.push(ignored('If-Modified-Since', headers.ifModifiedSince,
        'malformed HTTP-date is ignored per RFC 9110 §13.1.4'));
    } else if (selection.kind === 'present') {
      const lastModified = truncateToSeconds(selection.representation.lastModifiedMs);
      const unmodified = lastModified <= since.getTime();
      trace.push({
        header: 'If-Modified-Since',
        comparison: 'date',
        expected: since.toUTCString(),
        actual: new Date(lastModified).toUTCString(),
        result: unmodified ? 'match' : 'no-match',
        basis: '304 when Last-Modified is not later than the validator'
      });
      if (unmodified) throw new NotModifiedError(trace);
    }
  }

  return { allowed: true, trace };
}

function evaluateStateChanging(
  method: HttpMethod,
  headers: RawConditionHeaders,
  selection: Selection,
  trace: TraceStep[],
  currentValidator: CurrentValidator | null
): ConditionDecision {
  // ---- Step 1: If-Match, else If-Unmodified-Since.
  if (headers.ifMatch !== undefined) {
    const list = requireIfList('If-Match', headers.ifMatch);
    if (selection.kind === 'absent') {
      trace.push({
        header: 'If-Match',
        comparison: 'strong',
        expected: headers.ifMatch.trim(),
        actual: '<no selected representation>',
        result: 'no-match',
        basis: 'If-Match is false when the representation does not exist (RFC 9110 §13.1.1)'
      });
      throw new PreconditionFailedError(
        'PRECONDITION_IF_MATCH_FAILED',
        'If-Match failed: resource does not exist',
        trace,
        null
      );
    }
    const actual = selection.representation.etag;
    const matched = listMatches(actual, list, strongCompare);
    trace.push({
      header: 'If-Match',
      comparison: 'strong',
      expected: headers.ifMatch.trim(),
      actual: `${actual.weak ? 'W/' : ''}"${actual.opaque}"`,
      result: matched ? 'match' : 'no-match',
      basis: 'state-changing method: strong comparison per RFC 9110 §8.8.3.2'
    });
    if (!matched) {
      throw new PreconditionFailedError(
        'PRECONDITION_IF_MATCH_FAILED',
        'If-Match failed: current version does not strongly match',
        trace,
        currentValidator
      );
    }
  } else if (headers.ifUnmodifiedSince !== undefined) {
    const date = parseHttpDate(headers.ifUnmodifiedSince);
    if (!date) {
      trace.push(ignored('If-Unmodified-Since', headers.ifUnmodifiedSince,
        'malformed HTTP-date is ignored per RFC 9110 §13.1.5'));
    } else if (selection.kind === 'present') {
      const lastModified = truncateToSeconds(selection.representation.lastModifiedMs);
      const modifiedAfter = lastModified > date.getTime();
      trace.push({
        header: 'If-Unmodified-Since',
        comparison: 'date',
        expected: date.toUTCString(),
        actual: new Date(lastModified).toUTCString(),
        result: modifiedAfter ? 'no-match' : 'match',
        basis: '412 when Last-Modified is later than the validator'
      });
      if (modifiedAfter) {
        throw new PreconditionFailedError(
          'PRECONDITION_IF_UNMODIFIED_SINCE_FAILED',
          'If-Unmodified-Since failed: resource was modified after the given date',
          trace,
          currentValidator
        );
      }
    }
  }

  // ---- Step 2: If-None-Match (weak comparison for all state-changing methods).
  if (headers.ifNoneMatch !== undefined) {
    const list = requireIfList('If-None-Match', headers.ifNoneMatch);
    if (selection.kind === 'present') {
      const actual = selection.representation.etag;
      const matched = listMatches(actual, list, weakCompare);
      trace.push({
        header: 'If-None-Match',
        comparison: 'weak',
        expected: headers.ifNoneMatch.trim(),
        actual: `${actual.weak ? 'W/' : ''}"${actual.opaque}"`,
        result: matched ? 'match' : 'no-match',
        basis:
          method === 'PUT' && list.wildcard
            ? 'PUT with "*": reject overwriting an existing representation (RFC 9110 §13.1.3)'
            : 'state-changing method: weak comparison per RFC 9110 §13.1.3'
      });
      if (matched) {
        throw new PreconditionFailedError(
          'PRECONDITION_IF_NONE_MATCH_FAILED',
          'If-None-Match failed: a matching representation already exists',
          trace,
          currentValidator
        );
      }
    } else {
      trace.push({
        header: 'If-None-Match',
        comparison: 'weak',
        expected: headers.ifNoneMatch.trim(),
        actual: '<no selected representation>',
        result: 'no-match',
        basis: list.wildcard
          ? 'wildcard requires an existing representation; absent state passes'
          : 'no current representation to compare against'
      });
    }
  }

  // If-Modified-Since is never evaluated for state-changing requests.
  if (headers.ifModifiedSince !== undefined) {
    trace.push(ignored('If-Modified-Since', headers.ifModifiedSince,
      `ignored for ${method}: only valid on GET/HEAD (RFC 9110 §13.1.4)`));
  }

  return { allowed: true, trace };
}

function ignored(header: TraceStep['header'], raw: string, basis: string): TraceStep {
  return {
    header,
    comparison: 'date',
    expected: raw.trim(),
    actual: '—',
    result: 'ignored',
    basis
  };
}
