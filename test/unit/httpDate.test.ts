import { describe, expect, it } from 'vitest';
import { formatImfFixdate, parseHttpDate, sameHttpDateInstant } from '../../src/kernel/httpDate.js';

describe('IMF-fixdate', () => {
  it('格式化固定时刻', () => {
    expect(formatImfFixdate(Date.UTC(2026, 0, 2, 3, 4, 5))).toBe(
      'Fri, 02 Jan 2026 03:04:05 GMT',
    );
  });

  it('解析合法日期并与格式化往返一致', () => {
    const epoch = parseHttpDate('Fri, 02 Jan 2026 03:04:05 GMT');
    expect(epoch).toBe(Date.UTC(2026, 0, 2, 3, 4, 5));
    expect(formatImfFixdate(epoch!)).toBe('Fri, 02 Jan 2026 03:04:05 GMT');
  });

  it('拒绝非 GMT 日期与垃圾串', () => {
    expect(parseHttpDate('Fri, 02 Jan 2026 03:04:05 CST')).toBeNull();
    expect(parseHttpDate('not-a-date')).toBeNull();
  });

  it('秒级等价判断忽略毫秒差异', () => {
    expect(sameHttpDateInstant(1000, 1999)).toBe(true);
    expect(sameHttpDateInstant(1000, 2000)).toBe(false);
  });

  it('格式化非法时间戳抛错', () => {
    expect(() => formatImfFixdate(Number.NaN)).toThrow(/非法时间戳/);
  });

  it('宽容接收废弃历史格式（RFC 850，GMT）', () => {
    // Date.parse 对该形式可解析即可；关键是不接受非 GMT 时区。
    expect(parseHttpDate('Sunday, 02-Nov-25 07:00:00 GMT')).not.toBeNull();
  });

  it('看似 GMT 但无法解析的串返回 null（不抛错）', () => {
    expect(parseHttpDate('not gmt really')).toBeNull();
  });
});
