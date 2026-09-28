/**
 * 契约层类型：Range / If-Range 头解析后的结构化结果。
 * 数字一律使用 bigint，避免超大字节偏移在 Number 精度内丢失。
 */

/** 客户端请求的一个字节区间规格（尚未对照对象大小解析）。 */
export type ByteRangeSpec =
  | { readonly kind: 'explicit'; readonly start: bigint; readonly end: bigint | null }
  | { readonly kind: 'suffix'; readonly length: bigint };

export type RangeParseFailure =
  | { readonly kind: 'unsupported-unit'; readonly unit: string }
  | { readonly kind: 'malformed'; readonly detail: string };

export type RangeParseResult =
  | { readonly ok: true; readonly ranges: readonly ByteRangeSpec[] }
  | { readonly ok: false; readonly failure: RangeParseFailure };

/** If-Range 携带的校验子：强 ETag 或 HTTP 日期（弱 ETag 按不匹配处理）。 */
export type IfRangeValidator =
  | { readonly kind: 'etag'; readonly value: string }
  | { readonly kind: 'date'; readonly epochMs: number };

export type IfRangeParseResult =
  | { readonly ok: true; readonly validator: IfRangeValidator }
  | { readonly ok: false; readonly detail: string };

/** 契约层输出：一次对象读取请求的完整意图。 */
export interface ReadContract {
  readonly objectId: string;
  /** 无 Range 头时为 null（整体读取）。 */
  readonly rangeHeader: string | null;
  readonly ranges: RangeParseResult | null;
  readonly ifRange: IfRangeParseResult | null;
}
