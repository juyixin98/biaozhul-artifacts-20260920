import { describe, expect, it } from 'vitest';
import { parseRangeHeader } from '../../src/contract/rangeHeader.js';

describe('parseRangeHeader —— 合法语法', () => {
  it('解析单个显式区间', () => {
    const r = parseRangeHeader('bytes=0-499');
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.ranges).toEqual([{ kind: 'explicit', start: 0n, end: 499n }]);
  });

  it('解析开放结尾区间 bytes=500-', () => {
    const r = parseRangeHeader('bytes=500-');
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.ranges).toEqual([{ kind: 'explicit', start: 500n, end: null }]);
  });

  it('解析后缀区间 bytes=-500', () => {
    const r = parseRangeHeader('bytes=-500');
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.ranges).toEqual([{ kind: 'suffix', length: 500n }]);
  });

  it('解析多个区间（容忍逗号周围空白）', () => {
    const r = parseRangeHeader('bytes=0-9, 20-29 ,-5');
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.ranges).toHaveLength(3);
    expect(r.ranges[2]).toEqual({ kind: 'suffix', length: 5n });
  });

  it('接受超过 2^53 的整数而不丢精度', () => {
    const r = parseRangeHeader('bytes=9007199254740993-9007199254741000');
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.ranges[0]).toEqual({
      kind: 'explicit',
      start: 9007199254740993n,
      end: 9007199254741000n,
    });
  });
});

describe('parseRangeHeader —— 拒绝类别', () => {
  it('缺少等号分隔符 → malformed', () => {
    const r = parseRangeHeader('bytes 0-9');
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.failure.kind).toBe('malformed');
  });

  it('非 bytes 单位 → unsupported-unit 并保留单位名', () => {
    const r = parseRangeHeader('items=0-9');
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.failure).toEqual({ kind: 'unsupported-unit', unit: 'items' });
  });

  it('范围集合为空 → malformed', () => {
    const r = parseRangeHeader('bytes=');
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.failure.kind).toBe('malformed');
  });

  it('规格缺少横线 → malformed，且指出是第几个规格', () => {
    const r = parseRangeHeader('bytes=0-9,12');
    expect(r.ok).toBe(false);
    if (r.ok || r.failure.kind !== 'malformed') return;
    expect(r.failure.detail).toContain('第 2 个');
  });

  it('起始偏移非数字 → malformed', () => {
    const r = parseRangeHeader('bytes=a-9');
    expect(r.ok).toBe(false);
    if (r.ok || r.failure.kind !== 'malformed') return;
    expect(r.failure.detail).toContain('起始偏移');
  });

  it('结束偏移非数字 → malformed', () => {
    const r = parseRangeHeader('bytes=0-x');
    expect(r.ok).toBe(false);
    if (r.ok || r.failure.kind !== 'malformed') return;
    expect(r.failure.detail).toContain('结束偏移');
  });

  it('后缀长度为 0 在语法层合法（语义由内核处理）', () => {
    const r = parseRangeHeader('bytes=-0');
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.ranges[0]).toEqual({ kind: 'suffix', length: 0n });
  });

  it('带符号/小数的数字非法', () => {
    expect(parseRangeHeader('bytes=-1--5').ok).toBe(false);
    expect(parseRangeHeader('bytes=1.5-2').ok).toBe(false);
    expect(parseRangeHeader('bytes=+0-9').ok).toBe(false);
  });
});
