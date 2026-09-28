/**
 * 执行内核编排：输入“契约解析结果 + 对象元数据 + 策略 + boundary”，
 * 输出确定的 Resolution（full / rejected / partial）与诊断摘要。
 * 纯计算，不读数据库、不碰网络。
 */
import type {
  IfRangeParseResult,
  RangeParseResult,
} from '../contract/types.js';
import {
  coversEntireObject,
  mergeIntervals,
  resolveSpec,
  totalSize,
} from './intervals.js';
import { planMultipart } from './multipart.js';
import type {
  FullReason,
  ObjectMeta,
  RangePolicy,
  RejectReason,
  Resolution,
  ResolutionSummary,
} from './types.js';

function emptySummary(parsedSpecCount = 0): ResolutionSummary {
  return {
    parsedSpecCount,
    satisfiableCount: 0,
    unsatisfiableCount: 0,
    mergedIntervalCount: 0,
    contentBytes: null,
    overheadBytes: null,
    totalBytes: null,
    unsatisfiableReasons: [],
  };
}

function full(reason: FullReason, summary: ResolutionSummary): Resolution {
  return { outcome: 'full', statusCode: 200, reason, summary };
}

function rejected(
  statusCode: 400 | 416,
  reason: RejectReason,
  detail: string,
  summary: ResolutionSummary,
): Resolution {
  return { outcome: 'rejected', statusCode, reason, detail, summary };
}

/**
 * 秒级比较：If-Range 日期 >= Last-Modified 视为未变化（HTTP 日期分辨率只有 1 秒）。
 */
function ifRangeDateMatches(ifRangeEpochMs: number, lastModifiedMs: number): boolean {
  return Math.floor(ifRangeEpochMs / 1000) >= Math.floor(lastModifiedMs / 1000);
}

export interface ResolveInput {
  readonly meta: ObjectMeta;
  /** null 表示请求未携带 Range 头。 */
  readonly range: RangeParseResult | null;
  /** null 表示请求未携带 If-Range 头。 */
  readonly ifRange: IfRangeParseResult | null;
  readonly policy: RangePolicy;
  /** multipart 时的 boundary（由传输层生成，便于测试固定）。 */
  readonly boundary: string;
}

export function resolveRangeRequest(input: ResolveInput): Resolution {
  const { meta, range, ifRange, policy, boundary } = input;

  // 1) 无 Range 头：完整表示。
  if (range === null) {
    return full('no-range-header', emptySummary());
  }

  // 2) Range 语法层失败。
  if (!range.ok) {
    if (range.failure.kind === 'unsupported-unit') {
      // 仅支持 bytes 的服务器忽略未知 range-unit（RFC 9110 14.4 允许），回完整表示。
      return full('unsupported-unit-ignored', emptySummary());
    }
    return rejected(400, 'malformed-range', range.failure.detail, emptySummary());
  }

  const specs = range.ranges;

  // 3) If-Range 预检：不匹配或无法判定时一律回完整表示（绝不返回错误）。
  if (ifRange !== null) {
    if (!ifRange.ok) {
      return full('if-range-undecidable', emptySummary(specs.length));
    }
    const v = ifRange.validator;
    const matched =
      v.kind === 'etag'
        ? v.value === meta.etag
        : ifRangeDateMatches(v.epochMs, meta.lastModifiedMs);
    if (!matched) {
      return full('if-range-mismatch', emptySummary(specs.length));
    }
  }

  // 4) 范围数量限制（按解析出的规格数计，先于合并，防止客户端用海量规格绕过）。
  if (specs.length > policy.maxRanges) {
    return rejected(
      400,
      'too-many-ranges',
      `范围规格数量 ${specs.length} 超过上限 ${policy.maxRanges}`,
      emptySummary(specs.length),
    );
  }

  // 5) 逐规格语义解析。
  const resolved = specs.map((spec) => resolveSpec(spec, meta.size));
  const intervals = resolved.flatMap((r) => (r.interval ? [r.interval] : []));
  const unsatisfiableReasons = resolved.flatMap((r) => (r.reason ? [r.reason] : []));
  const satisfiableCount = intervals.length;

  // 6) 全部不可满足：416（含零长度对象）。
  if (satisfiableCount === 0) {
    const summary: ResolutionSummary = {
      ...emptySummary(specs.length),
      unsatisfiableCount: specs.length,
      contentBytes: 0n,
      totalBytes: 0n,
      unsatisfiableReasons,
    };
    return rejected(
      416,
      'none-satisfiable',
      meta.size === 0n
        ? '对象长度为 0，任何字节范围都不可满足'
        : `全部 ${specs.length} 个范围均落在对象之外（对象大小 ${meta.size.toString()} 字节）`,
      summary,
    );
  }

  // 7) 合并重叠/相邻区间（部分合法：不可满足规格仅计数，不影响其余区间）。
  const mergeResult = mergeIntervals(intervals);

  // 8) 合并后恰好是整个对象：直接回完整表示（RFC 允许，且省去封装）。
  if (
    mergeResult.intervals.length === 1 &&
    coversEntireObject(mergeResult.intervals[0]!, meta.size)
  ) {
    const contentBytes = meta.size;
    const summary: ResolutionSummary = {
      parsedSpecCount: specs.length,
      satisfiableCount,
      unsatisfiableCount: unsatisfiableReasons.length,
      mergedIntervalCount: 1,
      contentBytes,
      overheadBytes: null,
      totalBytes: contentBytes,
      unsatisfiableReasons,
    };
    return full('range-covers-entire-representation', summary);
  }

  const contentBytes = totalSize(mergeResult.intervals);
  const multipart = mergeResult.intervals.length > 1;
  const overhead = multipart
    ? planMultipart(boundary, meta, mergeResult.intervals).overheadBytes
    : null;
  const totalBytes = contentBytes + (overhead ?? 0n);

  const summary: ResolutionSummary = {
    parsedSpecCount: specs.length,
    satisfiableCount,
    unsatisfiableCount: unsatisfiableReasons.length,
    mergedIntervalCount: mergeResult.intervals.length,
    contentBytes,
    overheadBytes: overhead,
    totalBytes,
    unsatisfiableReasons,
  };

  // 9) 总响应量限制（含 multipart 封装开销）。
  if (totalBytes > policy.maxResponseBytes) {
    return rejected(
      400,
      'response-too-large',
      `响应总字节 ${totalBytes.toString()} 超过上限 ${policy.maxResponseBytes.toString()}`,
      summary,
    );
  }

  return {
    outcome: 'partial',
    statusCode: 206,
    multipart,
    intervals: mergeResult.intervals,
    boundary: multipart ? boundary : null,
    summary,
  };
}
