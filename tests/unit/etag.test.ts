import { describe, expect, it } from 'vitest';
import {
  formatETag,
  parseEntityTag,
  parseIfList,
  strongCompare,
  weakCompare,
  listMatches
} from '../../src/contract/etag.js';

describe('parseEntityTag', () => {
  it('parses a strong validator', () => {
    expect(parseEntityTag('"v1-ab12"')).toEqual({ opaque: 'v1-ab12', weak: false });
  });

  it('parses a weak validator with either W/ casing', () => {
    expect(parseEntityTag('W/"v1-ab12"')).toEqual({ opaque: 'v1-ab12', weak: true });
    expect(parseEntityTag('w/"v1-ab12"')).toEqual({ opaque: 'v1-ab12', weak: true });
  });

  it('accepts an empty opaque tag but rejects missing/unterminated quotes', () => {
    expect(parseEntityTag('""')).toEqual({ opaque: '', weak: false });
    expect(parseEntityTag('v1-ab12"')).toBeNull();
    expect(parseEntityTag('"v1-ab12')).toBeNull();
    expect(parseEntityTag('W/')).toBeNull();
  });

  it('rejects control characters and whitespace inside the tag', () => {
    expect(parseEntityTag('"v1 ab"')).toBeNull();
    expect(parseEntityTag('"v1\tab"')).toBeNull();
  });

  it('accepts commas inside opaque tags (only split outside quotes)', () => {
    expect(parseEntityTag('"a,b"')).toEqual({ opaque: 'a,b', weak: false });
  });
});

describe('parseIfList', () => {
  it('parses the wildcard alone', () => {
    expect(parseIfList('*')).toEqual({ wildcard: true, tags: [] });
  });

  it('parses a comma-separated list preserving weakness', () => {
    const list = parseIfList('"a", W/"b",  "c"');
    expect(list?.tags).toEqual([
      { opaque: 'a', weak: false },
      { opaque: 'b', weak: true },
      { opaque: 'c', weak: false }
    ]);
  });

  it('does not split on commas embedded in opaque tags', () => {
    const list = parseIfList('"a,b", W/"x,y"');
    expect(list?.tags.map((t) => t.opaque)).toEqual(['a,b', 'x,y']);
  });

  it.each(['*, "x"', '"x", *', '', 'notags', '"a", broken'])(
    'rejects invalid list %j',
    (raw) => {
      expect(parseIfList(raw)).toBeNull();
    }
  );
});

describe('comparison functions', () => {
  const strongCurrent = { opaque: 'v1', weak: false };
  const weakCurrent = { opaque: 'v1', weak: true };

  it('strong compare is false when EITHER side is weak', () => {
    expect(strongCompare(strongCurrent, { opaque: 'v1', weak: false })).toBe(true);
    expect(strongCompare(strongCurrent, { opaque: 'v1', weak: true })).toBe(false);
    expect(strongCompare(weakCurrent, { opaque: 'v1', weak: false })).toBe(false);
    expect(strongCompare(strongCurrent, { opaque: 'v2', weak: false })).toBe(false);
  });

  it('weak compare ignores weakness on both sides', () => {
    expect(weakCompare(strongCurrent, weakCurrent)).toBe(true);
    expect(weakCompare(weakCurrent, weakCurrent)).toBe(true);
    expect(weakCompare(strongCurrent, { opaque: 'v2', weak: true })).toBe(false);
  });

  it('wildcard lists match under either comparison', () => {
    const wildcard = parseIfList('*')!;
    expect(listMatches(strongCurrent, wildcard, strongCompare)).toBe(true);
    expect(listMatches(weakCurrent, wildcard, weakCompare)).toBe(true);
  });

  it('formatETag round-trips through the parser', () => {
    expect(formatETag('v1-ab', false)).toBe('"v1-ab"');
    expect(parseEntityTag(formatETag('v1-ab', true))).toEqual({ opaque: 'v1-ab', weak: true });
  });
});
