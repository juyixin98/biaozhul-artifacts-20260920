/**
 * 区间语义：把语法规格对照对象大小裁剪为闭区间，并做相邻/重叠合并。
 * 全部使用 bigint 计算；合并策略对调用方可见且可单测。
 */
import type { ByteRangeSpec } from '../contract/types.js';
import type { ResolvedInterval } from './types.js';

export interface SpecResolution {
  /** 可满足时为裁剪后的区间；否则为 null。 */
  readonly interval: ResolvedInterval | null;
  /** 不可满足原因；可满足时为 null。 */
  readonly reason:
    | 'zero-length-object'
    | 'start-past-end'
    | 'start-beyond-size'
    | 'reversed-explicit-range'
    | 'zero-suffix'
    | null;
}

/**
 * RFC 9110 14.1.2 语义解析：
 * - 显式区间裁剪到 [0, size-1]；起始越界即不可满足。
 * - 后缀范围取对象末尾 length 字节，length>=size 时等价于整个对象。
 * - 零长度对象上的任何范围均不可满足（最终产生 416）。
 */
export function resolveSpec(spec: ByteRangeSpec, size: bigint): SpecResolution {
  if (size <= 0n) {
    return { interval: null, reason: 'zero-length-object' };
  }
  const last = size - 1n;

  if (spec.kind === 'suffix') {
    if (spec.length === 0n) return { interval: null, reason: 'zero-suffix' };
    const start = spec.length >= size ? 0n : size - spec.length;
    return { interval: { start, end: last }, reason: null };
  }

  if (spec.start > last) return { interval: null, reason: 'start-beyond-size' };
  if (spec.end !== null && spec.end < spec.start) {
    return { interval: null, reason: 'reversed-explicit-range' };
  }
  const end = spec.end === null ? last : spec.end > last ? last : spec.end;
  return { interval: { start: spec.start, end }, reason: null };
}

export interface MergeResult {
  readonly intervals: readonly ResolvedInterval[];
  /** 输入中互相重叠或相邻而被合并的对数（诊断用）。 */
  readonly mergedPairs: number;
}

/**
 * 合并策略（明确声明）：
 * 1. 按起点排序；
 * 2. 重叠（next.start <= prev.end）或相邻（next.start === prev.end + 1）合并为同一区间。
 * 相邻也合并，因为拆开发出两个 part 只会浪费封装字节而不改变对象覆盖范围。
 */
export function mergeIntervals(input: readonly ResolvedInterval[]): MergeResult {
  const sorted = [...input].sort((a, b) =>
    a.start < b.start ? -1 : a.start > b.start ? 1 : a.end < b.end ? -1 : a.end > b.end ? 1 : 0,
  );
  const merged: ResolvedInterval[] = [];
  let mergedPairs = 0;

  for (const interval of sorted) {
    const prev = merged[merged.length - 1];
    if (prev !== undefined && interval.start <= prev.end + 1n) {
      merged[merged.length - 1] = {
        start: prev.start,
        end: interval.end > prev.end ? interval.end : prev.end,
      };
      mergedPairs += 1;
    } else {
      merged.push(interval);
    }
  }
  return { intervals: merged, mergedPairs };
}

export function intervalSize(interval: ResolvedInterval): bigint {
  return interval.end - interval.start + 1n;
}

export function totalSize(intervals: readonly ResolvedInterval[]): bigint {
  return intervals.reduce((acc, iv) => acc + intervalSize(iv), 0n);
}

/** 区间是否恰好覆盖整个对象（用于 200 快捷路径判断）。 */
export function coversEntireObject(interval: ResolvedInterval, size: bigint): boolean {
  return size > 0n && interval.start === 0n && interval.end === size - 1n;
}
