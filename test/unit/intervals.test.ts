import { describe, expect, it } from 'vitest';
import {
  coversEntireObject,
  intervalSize,
  mergeIntervals,
  resolveSpec,
  totalSize,
} from '../../src/kernel/intervals.js';

describe('resolveSpec —— 显式区间', () => {
  it('普通区间原样返回', () => {
    const r = resolveSpec({ kind: 'explicit', start: 0n, end: 9n }, 100n);
    expect(r.interval).toEqual({ start: 0n, end: 9n });
    expect(r.reason).toBeNull();
  });

  it('结束偏移越界时裁剪到对象末尾', () => {
    const r = resolveSpec({ kind: 'explicit', start: 95n, end: 999n }, 100n);
    expect(r.interval).toEqual({ start: 95n, end: 99n });
  });

  it('开放结尾解析到对象末尾', () => {
    const r = resolveSpec({ kind: 'explicit', start: 90n, end: null }, 100n);
    expect(r.interval).toEqual({ start: 90n, end: 99n });
  });

  it('恰好落在最后一个字节可满足', () => {
    const r = resolveSpec({ kind: 'explicit', start: 99n, end: 99n }, 100n);
    expect(r.interval).toEqual({ start: 99n, end: 99n });
  });

  it('起始偏移越过末尾 → start-beyond-size', () => {
    const r = resolveSpec({ kind: 'explicit', start: 100n, end: 200n }, 100n);
    expect(r.interval).toBeNull();
    expect(r.reason).toBe('start-beyond-size');
  });

  it('反转区间 start>end → reversed-explicit-range', () => {
    const r = resolveSpec({ kind: 'explicit', start: 50n, end: 10n }, 100n);
    expect(r.reason).toBe('reversed-explicit-range');
  });
});

describe('resolveSpec —— 后缀区间', () => {
  it('取对象末尾 N 字节', () => {
    const r = resolveSpec({ kind: 'suffix', length: 10n }, 100n);
    expect(r.interval).toEqual({ start: 90n, end: 99n });
  });

  it('后缀长度超过对象大小时等价于整个对象', () => {
    const r = resolveSpec({ kind: 'suffix', length: 1000n }, 100n);
    expect(r.interval).toEqual({ start: 0n, end: 99n });
  });

  it('后缀长度等于对象大小也覆盖整个对象', () => {
    const r = resolveSpec({ kind: 'suffix', length: 100n }, 100n);
    expect(r.interval).toEqual({ start: 0n, end: 99n });
  });

  it('后缀长度 0 → zero-suffix 不可满足', () => {
    const r = resolveSpec({ kind: 'suffix', length: 0n }, 100n);
    expect(r.interval).toBeNull();
    expect(r.reason).toBe('zero-suffix');
  });
});

describe('resolveSpec —— 零长度对象', () => {
  it('任何规格都不可满足且原因是 zero-length-object', () => {
    expect(resolveSpec({ kind: 'explicit', start: 0n, end: 0n }, 0n).reason).toBe('zero-length-object');
    expect(resolveSpec({ kind: 'explicit', start: 0n, end: null }, 0n).reason).toBe('zero-length-object');
    expect(resolveSpec({ kind: 'suffix', length: 1n }, 0n).reason).toBe('zero-length-object');
  });
});

describe('mergeIntervals —— 合并策略', () => {
  it('重叠区间合并为并集', () => {
    const r = mergeIntervals([
      { start: 0n, end: 10n },
      { start: 5n, end: 20n },
    ]);
    expect(r.intervals).toEqual([{ start: 0n, end: 20n }]);
    expect(r.mergedPairs).toBe(1);
  });

  it('相邻区间（首尾相差 1）也合并', () => {
    const r = mergeIntervals([
      { start: 0n, end: 9n },
      { start: 10n, end: 19n },
    ]);
    expect(r.intervals).toEqual([{ start: 0n, end: 19n }]);
  });

  it('间隔 1 字节以上不合并', () => {
    const r = mergeIntervals([
      { start: 0n, end: 8n },
      { start: 10n, end: 19n },
    ]);
    expect(r.intervals).toEqual([
      { start: 0n, end: 8n },
      { start: 10n, end: 19n },
    ]);
    expect(r.mergedPairs).toBe(0);
  });

  it('乱序输入先排序再合并，传递重叠可链式合并', () => {
    const r = mergeIntervals([
      { start: 20n, end: 29n },
      { start: 0n, end: 9n },
      { start: 8n, end: 21n },
    ]);
    expect(r.intervals).toEqual([{ start: 0n, end: 29n }]);
  });

  it('同起点区间按 end 排序后正确合并', () => {
    const r = mergeIntervals([
      { start: 0n, end: 5n },
      { start: 0n, end: 3n },
    ]);
    expect(r.intervals).toEqual([{ start: 0n, end: 5n }]);
  });
});

describe('尺寸助手', () => {
  it('intervalSize 为闭区间字节数', () => {
    expect(intervalSize({ start: 0n, end: 0n })).toBe(1n);
    expect(intervalSize({ start: 10n, end: 19n })).toBe(10n);
  });

  it('totalSize 为各区间之和', () => {
    expect(totalSize([{ start: 0n, end: 9n }, { start: 20n, end: 29n }])).toBe(20n);
  });

  it('coversEntireObject 精确判定', () => {
    expect(coversEntireObject({ start: 0n, end: 99n }, 100n)).toBe(true);
    expect(coversEntireObject({ start: 0n, end: 98n }, 100n)).toBe(false);
    expect(coversEntireObject({ start: 1n, end: 99n }, 100n)).toBe(false);
  });
});
