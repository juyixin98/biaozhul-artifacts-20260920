/**
 * Shared domain types for the Range read backend.
 *
 * Byte offsets everywhere are zero-based, inclusive on both ends, and refer
 * to the ORIGINAL stored representation (identity encoding). We never store
 * a compressed alternative, so there is no ambiguity about which byte space
 * a range applies to.
 */

/** Inclusive byte interval [start, end] over the original object bytes. */
export interface ByteInterval {
  start: number;
  end: number;
}

/** One syntactically parsed range-spec, before resolution against a size. */
export interface ParsedSpec {
  /** Zero-based position in the comma separated Range header. */
  index: number;
  /** Exact trimmed text of the spec, kept for diagnostics. */
  raw: string;
  kind: 'closed' | 'open-ended' | 'suffix';
  /** Present for closed and open-ended. Arbitrary precision. */
  firstBytePos?: bigint;
  /** Present for closed only. */
  lastBytePos?: bigint;
  /** Present for suffix only (bytes=-N). */
  suffixLength?: bigint;
}

export type HeaderRejectCode =
  | 'MALFORMED_RANGE_HEADER'
  | 'UNSUPPORTED_RANGE_UNIT';

export type ResolveRejectCode =
  | 'MALFORMED_RANGE_HEADER'
  | 'UNSUPPORTED_RANGE_UNIT'
  | 'OBJECT_EMPTY'
  | 'UNSATISFIABLE_RANGE'
  | 'TOO_MANY_RANGES'
  | 'RESPONSE_SIZE_LIMIT_EXCEEDED';

/** Why an individual spec could not be served, recorded for diagnostics. */
export type SpecDropReason =
  | 'START_BEYOND_OBJECT'
  | 'EMPTY_SUFFIX'
  | 'OBJECT_EMPTY';

export interface DroppedSpec {
  index: number;
  raw: string;
  reason: SpecDropReason;
}

export interface SpecError {
  index: number;
  raw: string;
  reason: string;
}

export type RangeParseResult =
  | { ok: true; unit: 'bytes'; specs: ParsedSpec[] }
  | {
      ok: false;
      code: HeaderRejectCode;
      message: string;
      /** Offending spec when the problem is spec-local. */
      specError?: SpecError;
    };

export interface RangeLimits {
  /** Maximum number of comma separated specs accepted in one request. */
  maxSpecs: number;
  /** Maximum total body size (bytes, including multipart overhead). */
  maxResponseBytes: number;
  /**
   * Merge gap in bytes: two intervals sorted by start merge when
   * next.start <= prev.end + 1 + mergeGap. 0 merges strict overlaps only,
   * 1 (default) also merges adjacent intervals.
   */
  mergeGap: number;
}

export type IfRangeVerdict =
  | { outcome: 'absent' }
  | { outcome: 'match' }
  | { outcome: 'mismatch'; kind: 'etag-strong' | 'etag-weak' | 'date'; detail: string }
  | { outcome: 'indeterminate'; detail: string };

export interface ImmutableObjectMeta {
  id: string;
  size: number;
  /** Strong validator, including double quotes, e.g. `"ab12..."`. */
  etag: string;
  lastModified: Date;
  contentType: string;
}

export interface ImmutableObject extends ImmutableObjectMeta {
  data: Buffer;
}

/** Outcome of the execution core: either serve bytes or reject explicitly. */
export type RangeResolution =
  | {
      decision: 'FULL_REPRESENTATION';
      status: 200;
      reason:
        | 'NO_RANGE_HEADER'
        | 'UNSUPPORTED_UNIT_IGNORED'
        | 'IF_RANGE_MISMATCH'
        | 'IF_RANGE_INDETERMINATE';
      intervals: [];
      dropped: DroppedSpec[];
    }
  | {
      decision: 'SINGLE_PART';
      status: 206;
      intervals: [ByteInterval];
      dropped: DroppedSpec[];
      mergeCount: number;
      requestedBytes: number;
      servedBytes: number;
    }
  | {
      decision: 'MULTIPART';
      status: 206;
      intervals: ByteInterval[];
      dropped: DroppedSpec[];
      mergeCount: number;
      requestedBytes: number;
      servedBytes: number;
    }
  | {
      decision: 'REJECT';
      /**
       * HTTP status to send: 400 for malformed syntax / exceeded request
       * limits, 416 for a syntactically valid but unsatisfiable range.
       */
      status: 400 | 416;
      code: ResolveRejectCode;
      message: string;
      specError?: SpecError;
      dropped: DroppedSpec[];
      intervals: ByteInterval[];
    };

export interface MergeStats {
  mergeCount: number;
  /** Requested spec indexes that fed each surviving interval. */
  sources: number[][];
}
