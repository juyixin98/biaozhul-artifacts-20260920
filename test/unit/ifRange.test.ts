import { describe, expect, it } from 'vitest';
import { parseIfRange } from '../../src/contract/ifRange.js';

describe('parseIfRange', () => {
  it('接受强 ETag', () => {
    const r = parseIfRange('"abc123"');
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.validator).toEqual({ kind: 'etag', value: '"abc123"' });
  });

  it('拒绝弱 ETag（W/ 前缀）', () => {
    const r = parseIfRange('W/"abc123"');
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.detail).toContain('弱 ETag');
  });

  it('接受 IMF-fixdate', () => {
    const r = parseIfRange('Sun, 02 Nov 2025 07:00:00 GMT');
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.validator.kind).toBe('date');
    if (r.validator.kind === 'date') {
      expect(new Date(r.validator.epochMs).toISOString()).toBe('2025-11-02T07:00:00.000Z');
    }
  });

  it('拒绝既非 ETag 也非日期的值', () => {
    const r = parseIfRange('not-a-validator');
    expect(r.ok).toBe(false);
  });

  it('拒绝空值', () => {
    expect(parseIfRange('   ').ok).toBe(false);
  });

  it('拒绝语法损坏的引号 ETag', () => {
    expect(parseIfRange('"abc').ok).toBe(false);
    expect(parseIfRange('"ab"c"').ok).toBe(false);
  });
});
