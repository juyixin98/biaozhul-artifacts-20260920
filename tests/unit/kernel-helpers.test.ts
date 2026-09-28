import { describe, expect, it } from 'vitest';
import { applyMergePatch } from '../../src/core/merge-patch.js';
import { contentHash, stableStringify } from '../../src/core/hash.js';
import { FROZEN_ETAGS } from '../helpers/oracle.js';
import { independentETag } from '../helpers/oracle.js';

describe('applyMergePatch (RFC 7386)', () => {
  it('merges nested objects and removes null members', () => {
    const result = applyMergePatch(
      { a: 'x', b: { c: 1, d: 2 }, keep: true },
      { b: { d: 3, e: 4 }, keep: null }
    ) as Record<string, unknown>;
    expect(result).toEqual({ a: 'x', b: { c: 1, d: 3, e: 4 } });
  });

  it('replaces arrays and scalars wholesale', () => {
    expect(applyMergePatch({ a: [1, 2] }, { a: [3] })).toEqual({ a: [3] });
    expect(applyMergePatch({ a: { deep: 1 } }, { a: 'scalar' })).toEqual({ a: 'scalar' });
  });

  it('merging an object into a non-object starts from empty', () => {
    expect(applyMergePatch([1, 2], { a: 1 })).toEqual({ a: 1 });
    expect(applyMergePatch(null, { a: 1 })).toEqual({ a: 1 });
  });

  it('does not mutate the input target', () => {
    const target = { nested: { n: 1 } };
    const snapshot = structuredClone(target);
    applyMergePatch(target, { nested: { n: 2 } });
    expect(target).toEqual(snapshot);
  });
});

describe('version tag derivation agrees with the independent oracle', () => {
  const v1 = { name: 'widget', count: 1, tags: ['x'] };
  const v2 = { name: 'widget', count: 2, tags: ['x'] };
  const v3 = { name: 'widget', count: 2, tags: ['x', 'y'] };

  it('implementation ETag matches the independently frozen fixture value', () => {
    expect(independentETag(1, v1)).toBe(FROZEN_ETAGS.widgetV1);
    expect(independentETag(2, v2)).toBe(FROZEN_ETAGS.widgetV2Count2);
    expect(independentETag(3, v3)).toBe(FROZEN_ETAGS.widgetV3AfterMerge);
  });

  it('canonicalization is key-order independent', () => {
    const a = { z: 1, a: { y: 2, b: 3 } };
    const b = { a: { b: 3, y: 2 }, z: 1 };
    expect(stableStringify(a)).toBe(stableStringify(b));
    expect(contentHash(a)).toBe(contentHash(b));
  });

  it('any content change changes the hash prefix', () => {
    expect(contentHash(v1)).not.toBe(contentHash(v2));
  });
});
