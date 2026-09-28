/**
 * Range 请求头语法解析（RFC 9110 14.4），纯函数、不接触对象大小。
 * 语义解析（裁剪、后缀换算、合并、可满足性）在 kernel/resolve.ts 中完成。
 */
import type { ByteRangeSpec, RangeParseResult } from './types.js';

const UNSIGNED_INT = /^\d+$/;

function parseBigUint(token: string): bigint | null {
  if (!UNSIGNED_INT.test(token)) return null;
  try {
    return BigInt(token);
  } catch {
    return null;
  }
}

function parseSpec(rawSpec: string, index: number): ByteRangeSpec | string {
  // 语法允许逗号两侧的 OWS，但规格内部不允许空白。
  const spec = rawSpec.trim();
  if (spec === '') return `第 ${index + 1} 个范围规格为空`;
  const dash = spec.indexOf('-');
  if (dash < 0) return `第 ${index + 1} 个范围规格缺少 "-": ${JSON.stringify(spec)}`;

  const left = spec.slice(0, dash);
  const right = spec.slice(dash + 1);

  if (left === '') {
    // 后缀形式：-suffix-length
    const length = parseBigUint(right);
    if (length === null) {
      return `第 ${index + 1} 个后缀范围长度非法: ${JSON.stringify(right)}`;
    }
    return { kind: 'suffix', length };
  }

  const start = parseBigUint(left);
  if (start === null) {
    return `第 ${index + 1} 个范围起始偏移非法: ${JSON.stringify(left)}`;
  }
  if (right === '') {
    return { kind: 'explicit', start, end: null };
  }
  const end = parseBigUint(right);
  if (end === null) {
    return `第 ${index + 1} 个范围结束偏移非法: ${JSON.stringify(right)}`;
  }
  return { kind: 'explicit', start, end };
}

/** 解析单个 Range 头值；无 Range 头的场景由调用方直接传 null。 */
export function parseRangeHeader(header: string): RangeParseResult {
  const value = header.trim();
  const eq = value.indexOf('=');
  if (eq < 0) {
    return { ok: false, failure: { kind: 'malformed', detail: '缺少 "=" 分隔符' } };
  }
  const unit = value.slice(0, eq).trim().toLowerCase();
  if (unit !== 'bytes') {
    return { ok: false, failure: { kind: 'unsupported-unit', unit } };
  }

  const setPart = value.slice(eq + 1);
  if (setPart.trim() === '') {
    return { ok: false, failure: { kind: 'malformed', detail: '范围集合为空' } };
  }

  const ranges: ByteRangeSpec[] = [];
  const rawSpecs = setPart.split(',');
  for (let i = 0; i < rawSpecs.length; i++) {
    const parsed = parseSpec(rawSpecs[i] ?? '', i);
    if (typeof parsed === 'string') {
      return { ok: false, failure: { kind: 'malformed', detail: parsed } };
    }
    ranges.push(parsed);
  }

  return { ok: true, ranges };
}
