/**
 * If-Range 头解析（RFC 9110 13.1.6）。
 * If-Range 只能携带强 ETag 或 HTTP 日期；弱 ETag（W/）不得用于范围预检。
 * 无法解析时返回失败，由执行内核按“无法判定 → 回完整表示”处理。
 */
import { parseHttpDate } from '../kernel/httpDate.js';
import type { IfRangeParseResult } from './types.js';

const STRONG_ETAG_RE = /^"[\x21\x23-\x7E\x80-\xFF]*"$/;

export function parseIfRange(rawHeader: string): IfRangeParseResult {
  const value = rawHeader.trim();
  if (value === '') return { ok: false, detail: 'If-Range 值为空' };

  if (value.startsWith('"')) {
    if (!STRONG_ETAG_RE.test(value)) {
      return { ok: false, detail: 'If-Range ETag 语法非法' };
    }
    return { ok: true, validator: { kind: 'etag', value } };
  }

  // W/"..." 弱验证子显式拒绝（其余以 W/ 开头的怪值同样拒绝）。
  if (/^w\//i.test(value)) {
    return { ok: false, detail: 'If-Range 不接受弱 ETag' };
  }

  const epochMs = parseHttpDate(value);
  if (epochMs === null) {
    return { ok: false, detail: `If-Range 既不是合法强 ETag 也不是合法 HTTP 日期: ${JSON.stringify(value)}` };
  }
  return { ok: true, validator: { kind: 'date', epochMs } };
}
