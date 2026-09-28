import { describe, expect, it } from 'vitest';
import { parseHttpDate, formatHttpDate, truncateToSeconds } from '../../src/contract/http-date.js';

describe('parseHttpDate', () => {
  it('parses a valid IMF-fixdate', () => {
    expect(parseHttpDate('Sun, 06 Nov 1994 08:49:37 GMT')?.getTime())
      .toBe(Date.UTC(1994, 10, 6, 8, 49, 37));
  });

  it('rejects impossible calendar days', () => {
    expect(parseHttpDate('Fri, 31 Feb 2020 00:00:00 GMT')).toBeNull();
  });

  it('rejects legacy/non-GMT and garbage dates', () => {
    expect(parseHttpDate('Sunday, 06-Nov-94 08:49:37 GMT')).toBeNull();
    expect(parseHttpDate('Sun Nov  6 08:49:37 1994')).toBeNull();
    expect(parseHttpDate('06 Nov 1994')).toBeNull();
    expect(parseHttpDate('Sun, 06 Nov 1994 08:49:37 UTC')).toBeNull();
  });

  it('formats and truncates to second resolution', () => {
    const ms = Date.UTC(2026, 0, 15, 9, 0, 0) + 750;
    expect(formatHttpDate(ms)).toBe('Thu, 15 Jan 2026 09:00:00 GMT');
    expect(truncateToSeconds(ms)).toBe(Date.UTC(2026, 0, 15, 9, 0, 0));
  });
});
