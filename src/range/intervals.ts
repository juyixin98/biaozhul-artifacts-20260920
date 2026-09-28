import type {
  ByteInterval,
  DroppedSpec,
  MergeStats,
  ParsedSpec,
} from '../types.js';

/**
 * Resolve parsed range specs against an actual representation size and
 * merge the resulting intervals. Pure functions, no I/O — this is the
 * execution core's geometry layer.
 *
 * Byte offsets are always positions in the ORIGINAL stored representation.
 * Objects are stored uncompressed (identity encoding only), so clamping
 * and merging operate on the same byte space the client used in the
 * request.
 */

export interface ResolveResult {
  intervals: ByteInterval[];
  dropped: DroppedSpec[];
  /** Sum of resolved (pre-merge) interval lengths. */
  requestedBytes: number;
}

/**
 * Resolve each spec to a concrete inclusive interval within [0, size-1].
 *
 * Clamping rules (RFC 9110 §14.1.2):
 *  - closed  a-b  with b >= size clamps b to size-1 (satisfiable)
 *  - open    a-   extends to size-1
 *  - suffix  -n   covers the last min(n, size) bytes
 *
 * Unsatisfiable specs are DROPPED with a specific reason instead of
 * throwing, so the caller can distinguish "partly satisfiable" from
 * "nothing satisfiable":
 *  - first-byte-pos >= size          START_BEYOND_OBJECT
 *  - suffix length 0                 EMPTY_SUFFIX
 *  - any spec on a zero-length object OBJECT_EMPTY
 */
export function resolveSpecs(specs: ParsedSpec[], size: number): ResolveResult {
  const intervals: ByteInterval[] = [];
  const dropped: DroppedSpec[] = [];
  let requestedBytes = 0;

  const drop = (spec: ParsedSpec, reason: DroppedSpec['reason']): void => {
    dropped.push({ index: spec.index, raw: spec.raw, reason });
  };

  const sizeBig = BigInt(size);

  for (const spec of specs) {
    if (size === 0) {
      // bytes=-0 asks for zero bytes regardless; keep its category.
      if (spec.kind === 'suffix' && spec.suffixLength === 0n) {
        drop(spec, 'EMPTY_SUFFIX');
      } else {
        drop(spec, 'OBJECT_EMPTY');
      }
      continue;
    }

    if (spec.kind === 'suffix') {
      const n = spec.suffixLength!;
      if (n === 0n) {
        drop(spec, 'EMPTY_SUFFIX');
        continue;
      }
      // n may be astronomically large; start simply clamps to 0 and the
      // whole representation is served (never an error).
      const start = n >= sizeBig ? 0 : Number(sizeBig - n);
      const interval = { start, end: size - 1 };
      intervals.push(interval);
      requestedBytes += interval.end - interval.start + 1;
      continue;
    }

    const first = spec.firstBytePos!;
    if (first >= sizeBig) {
      // Includes values beyond Number safety: they can never index bytes.
      drop(spec, 'START_BEYOND_OBJECT');
      continue;
    }
    const firstNum = Number(first);
    const end =
      spec.kind === 'closed'
        ? Number(spec.lastBytePos! >= sizeBig ? sizeBig - 1n : spec.lastBytePos!)
        : size - 1;
    const interval = { start: firstNum, end };
    intervals.push(interval);
    requestedBytes += end - firstNum + 1;
  }

  return { intervals, dropped, requestedBytes };
}

/**
 * Sort intervals by start position and merge those that overlap or sit
 * within `gap` missing bytes of each other. With mergeGap=1 (configured
 * default) ADJACENT intervals [0,9],[10,19] become [0,19], which matters
 * for multipart output: two touching ranges are one contiguous part, not
 * two. mergeGap=0 merges strict overlaps only ([0,10],[10,19] still
 * merges; [0,9],[10,19] does not).
 *
 * `sources` tracks which original (pre-sort) interval indexes fed each
 * survivor, purely for diagnostics.
 */
export function mergeIntervals(
  intervals: ByteInterval[],
  gap: number,
): { intervals: ByteInterval[]; stats: MergeStats } {
  const ordered = intervals
    .map((interval, sourceIndex) => ({ ...interval, sourceIndex }))
    .sort((a, b) => a.start - b.start || a.end - b.end);

  let merged: ByteInterval[] = [];
  const sources: number[][] = [];
  let mergeCount = 0;

  for (const cur of ordered) {
    const lastIndex = merged.length - 1;
    const last = merged[lastIndex];
    if (last !== undefined && cur.start <= last.end + gap) {
      merged = [
        ...merged.slice(0, lastIndex),
        { start: last.start, end: Math.max(last.end, cur.end) },
      ];
      sources[lastIndex] = [...sources[lastIndex]!, cur.sourceIndex];
      mergeCount += 1;
      continue;
    }
    merged = [...merged, { start: cur.start, end: cur.end }];
    sources.push([cur.sourceIndex]);
  }

  return { intervals: merged, stats: { mergeCount, sources } };
}
