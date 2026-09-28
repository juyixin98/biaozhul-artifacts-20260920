import { describe, expect, it } from 'vitest';
import { parseIfRange } from '../../src/contract/ifRange.js';
import { parseRangeHeader } from '../../src/contract/rangeHeader.js';
import { resolveRangeRequest } from '../../src/kernel/resolve.js';
import type { ObjectMeta, RangePolicy } from '../../src/kernel/types.js';

const META_100: ObjectMeta = {
  id: 'o100',
  size: 100n,
  etag: '"v1"',
  lastModifiedMs: Date.UTC(2026, 0, 1, 0, 0, 0),
  contentType: 'text/plain',
};

const POLICY: RangePolicy = { maxRanges: 4, maxResponseBytes: 10_000n };

function resolve(
  meta: ObjectMeta,
  rangeHeader: string | null,
  ifRangeHeader?: string,
  policy: RangePolicy = POLICY,
) {
  return resolveRangeRequest({
    meta,
    range: rangeHeader === null ? null : parseRangeHeader(rangeHeader),
    ifRange: ifRangeHeader === undefined ? null : parseIfRange(ifRangeHeader),
    policy,
    boundary: 'B',
  });
}

describe('resolveRangeRequest —— 完整表示路径', () => {
  it('无 Range 头 → 200 no-range-header', () => {
    const r = resolve(META_100, null);
    expect(r.outcome).toBe('full');
    if (r.outcome === 'full') expect(r.reason).toBe('no-range-header');
  });

  it('未知 range-unit 被忽略 → 200', () => {
    const r = resolve(META_100, 'items=0-9');
    expect(r.outcome).toBe('full');
    if (r.outcome === 'full') expect(r.reason).toBe('unsupported-unit-ignored');
  });

  it('范围恰好覆盖整个对象（含裁剪/后缀覆盖）→ 200', () => {
    const cases = ['bytes=0-99', 'bytes=0-999', 'bytes=-100', 'bytes=-500', 'bytes=0-'];
    for (const h of cases) {
      const r = resolve(META_100, h);
      expect(r.outcome, h).toBe('full');
      if (r.outcome === 'full') expect(r.reason, h).toBe('range-covers-entire-representation');
    }
  });
});

describe('resolveRangeRequest —— If-Range', () => {
  it('ETag 匹配 → 206', () => {
    const r = resolve(META_100, 'bytes=0-9', '"v1"');
    expect(r.outcome).toBe('partial');
  });

  it('ETag 不匹配 → 200 if-range-mismatch', () => {
    const r = resolve(META_100, 'bytes=0-9', '"v2-stale"');
    expect(r.outcome).toBe('full');
    if (r.outcome === 'full') expect(r.reason).toBe('if-range-mismatch');
  });

  it('日期晚于 Last-Modified（未变化）→ 206', () => {
    const r = resolve(META_100, 'bytes=0-9', 'Fri, 02 Jan 2026 00:00:00 GMT');
    expect(r.outcome).toBe('partial');
  });

  it('日期等于 Last-Modified 同一秒 → 206', () => {
    const r = resolve(META_100, 'bytes=0-9', 'Thu, 01 Jan 2026 00:00:00 GMT');
    expect(r.outcome).toBe('partial');
  });

  it('日期早于 Last-Modified（已变化）→ 200', () => {
    const r = resolve(META_100, 'bytes=0-9', 'Wed, 31 Dec 2025 00:00:00 GMT');
    expect(r.outcome).toBe('full');
    if (r.outcome === 'full') expect(r.reason).toBe('if-range-mismatch');
  });

  it('弱 ETag / 无法解析 → 200 if-range-undecidable', () => {
    const r = resolve(META_100, 'bytes=0-9', 'W/"v1"');
    expect(r.outcome).toBe('full');
    if (r.outcome === 'full') expect(r.reason).toBe('if-range-undecidable');
  });
});

describe('resolveRangeRequest —— 拒绝类别与状态码', () => {
  it('语法损坏 → 400 malformed-range', () => {
    const r = resolve(META_100, 'bytes=abc');
    expect(r.outcome).toBe('rejected');
    if (r.outcome === 'rejected') {
      expect(r.statusCode).toBe(400);
      expect(r.reason).toBe('malformed-range');
    }
  });

  it('范围数量超限 → 400 too-many-ranges（按合并前规格数）', () => {
    const r = resolve(META_100, 'bytes=0-1,0-1,0-1,0-1,0-1');
    expect(r.outcome).toBe('rejected');
    if (r.outcome === 'rejected') {
      expect(r.statusCode).toBe(400);
      expect(r.reason).toBe('too-many-ranges');
    }
  });

  it('全部越界 → 416 none-satisfiable', () => {
    const r = resolve(META_100, 'bytes=100-200');
    expect(r.outcome).toBe('rejected');
    if (r.outcome === 'rejected') {
      expect(r.statusCode).toBe(416);
      expect(r.reason).toBe('none-satisfiable');
    }
  });

  it('零长度对象上任何范围 → 416 且原因分类为 zero-length-object', () => {
    const empty: ObjectMeta = { ...META_100, id: 'empty', size: 0n };
    const r = resolve(empty, 'bytes=0-0');
    expect(r.outcome).toBe('rejected');
    if (r.outcome === 'rejected') {
      expect(r.statusCode).toBe(416);
      expect(r.reason).toBe('none-satisfiable');
      expect(r.summary.unsatisfiableReasons).toEqual(['zero-length-object']);
    }
  });

  it('总响应量超限 → 400 response-too-large（含 multipart 开销）', () => {
    const tight: RangePolicy = { maxRanges: 4, maxResponseBytes: 30n };
    const r = resolve(META_100, 'bytes=0-19,80-99', undefined, tight);
    expect(r.outcome).toBe('rejected');
    if (r.outcome === 'rejected') {
      expect(r.reason).toBe('response-too-large');
      // 40 数据字节 + 封装开销，两者都超过 30
      expect(r.summary.contentBytes).toBe(40n);
      expect((r.summary.overheadBytes ?? 0n) > 0n).toBe(true);
    }
  });
});

describe('resolveRangeRequest —— 部分合法与合并', () => {
  it('一个合法、一个越界 → 206 单区间且摘要区分两类计数', () => {
    const r = resolve(META_100, 'bytes=0-9,500-600');
    expect(r.outcome).toBe('partial');
    if (r.outcome !== 'partial') return;
    expect(r.multipart).toBe(false);
    expect(r.intervals).toEqual([{ start: 0n, end: 9n }]);
    expect(r.summary.satisfiableCount).toBe(1);
    expect(r.summary.unsatisfiableCount).toBe(1);
    expect(r.summary.unsatisfiableReasons).toEqual(['start-beyond-size']);
  });

  it('相邻区间合并后变单区间 → 206 非 multipart', () => {
    const r = resolve(META_100, 'bytes=0-9,10-19');
    expect(r.outcome).toBe('partial');
    if (r.outcome !== 'partial') return;
    expect(r.multipart).toBe(false);
    expect(r.intervals).toEqual([{ start: 0n, end: 19n }]);
  });

  it('重叠区间合并，内容字节按并集计而非求和', () => {
    const r = resolve(META_100, 'bytes=0-19,10-29');
    if (r.outcome !== 'partial') throw new Error('expected partial');
    expect(r.intervals).toEqual([{ start: 0n, end: 29n }]);
    expect(r.summary.contentBytes).toBe(30n);
  });

  it('不相邻的多区间 → multipart 且总字节=数据+开销', () => {
    const r = resolve(META_100, 'bytes=0-9,90-99');
    expect(r.outcome).toBe('partial');
    if (r.outcome !== 'partial') return;
    expect(r.multipart).toBe(true);
    expect(r.boundary).toBe('B');
    expect(r.intervals).toEqual([
      { start: 0n, end: 9n },
      { start: 90n, end: 99n },
    ]);
    expect(r.summary.contentBytes).toBe(20n);
    expect((r.summary.overheadBytes ?? 0n) > 0n).toBe(true);
    expect(r.summary.totalBytes).toBe(r.summary.contentBytes! + r.summary.overheadBytes!);
  });

  it('开放结尾与后缀混用：0- 已覆盖全对象 → 合并后回 200', () => {
    const r = resolve(META_100, 'bytes=0-,-5');
    expect(r.outcome).toBe('full');
    if (r.outcome === 'full') expect(r.reason).toBe('range-covers-entire-representation');
  });
});

describe('resolveRangeRequest —— 极大整数（无 Number 精度损失）', () => {
  const HUGE: ObjectMeta = {
    ...META_100,
    id: 'huge',
    size: 9007199254740993n, // 2^53 + 1，超过 Number.MAX_SAFE_INTEGER
  };

  it('末尾单字节区间精确定位到 2^53', () => {
    const r = resolve(HUGE, 'bytes=9007199254740992-9007199254740992');
    if (r.outcome !== 'partial') throw new Error(`expected partial, got ${r.outcome}`);
    expect(r.intervals[0]).toEqual({ start: 9007199254740992n, end: 9007199254740992n });
    expect(r.summary.contentBytes).toBe(1n);
  });

  it('起始恰好等于 size → 416', () => {
    const r = resolve(HUGE, 'bytes=9007199254740993-');
    expect(r.outcome).toBe('rejected');
    if (r.outcome === 'rejected') expect(r.statusCode).toBe(416);
  });

  it('超大开放结尾裁剪到对象末字节且 totalBytes 为精确 bigint', () => {
    const r = resolve(HUGE, 'bytes=9007199254740990-99999999999999999999');
    if (r.outcome !== 'partial') throw new Error(`got ${r.outcome}`);
    expect(r.intervals[0]).toEqual({ start: 9007199254740990n, end: 9007199254740992n });
    expect(r.summary.contentBytes).toBe(3n);
  });
});
