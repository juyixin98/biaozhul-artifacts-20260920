/**
 * 执行内核的类型定义。内核只做纯计算（bigint 偏移），不做任何 I/O，
 * 因此即使偏移超过 2^53 或对象实际不可放入内存，也能被确定性测试。
 */

/** 已经对照对象大小裁剪、保证落在 [0, size-1] 内的闭区间。 */
export interface ResolvedInterval {
  readonly start: bigint;
  readonly end: bigint;
}

/** 对象元数据（不可变对象：写入后永不变更）。 */
export interface ObjectMeta {
  readonly id: string;
  readonly size: bigint;
  readonly etag: string;
  readonly lastModifiedMs: number;
  readonly contentType: string;
}

export interface RangePolicy {
  /** 允许的 Range 规格数量上限（解析计数，先于合并检查）。 */
  readonly maxRanges: number;
  /** 允许的响应总字节上限（含 multipart 封装开销）。 */
  readonly maxResponseBytes: bigint;
}

export type FullReason =
  | 'no-range-header'
  | 'unsupported-unit-ignored'
  | 'if-range-mismatch'
  | 'if-range-undecidable'
  | 'range-covers-entire-representation';

export type RejectReason =
  | 'malformed-range'
  | 'too-many-ranges'
  | 'response-too-large'
  | 'none-satisfiable';

/** 供诊断接口消费的关键状态快照。 */
export interface ResolutionSummary {
  readonly parsedSpecCount: number;
  readonly satisfiableCount: number;
  readonly unsatisfiableCount: number;
  readonly mergedIntervalCount: number;
  /** 实际对象字节之和（不含封装）。 */
  readonly contentBytes: bigint | null;
  /** multipart 封装字节；单范围/非部分响应为 null。 */
  readonly overheadBytes: bigint | null;
  /** 客户端将收到的总字节。 */
  readonly totalBytes: bigint | null;
  /** 不可满足规格的分类原因（与 unsatisfiableCount 对齐）。 */
  readonly unsatisfiableReasons: readonly string[];
}

export type Resolution =
  | {
      readonly outcome: 'full';
      readonly statusCode: 200;
      readonly reason: FullReason;
      readonly summary: ResolutionSummary;
    }
  | {
      readonly outcome: 'rejected';
      readonly statusCode: 400 | 416;
      readonly reason: RejectReason;
      readonly detail: string;
      readonly summary: ResolutionSummary;
    }
  | {
      readonly outcome: 'partial';
      readonly statusCode: 206;
      readonly multipart: boolean;
      readonly intervals: readonly ResolvedInterval[];
      /** multipart=true 时存在；边界由调用方（传输层）生成并传入。 */
      readonly boundary: string | null;
      readonly summary: ResolutionSummary;
    };

export type FullResolution = Extract<Resolution, { readonly outcome: 'full' }>;
export type RejectedResolution = Extract<Resolution, { readonly outcome: 'rejected' }>;
export type PartialResolution = Extract<Resolution, { readonly outcome: 'partial' }>;
