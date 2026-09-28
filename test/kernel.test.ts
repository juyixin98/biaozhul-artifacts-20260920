import { describe, expect, it } from 'vitest';
import type { JsonValue } from '../src/equality';
import { applyPatch, type PatchOutcome, type PatchFailure } from '../src/patch';
import library from '../fixtures/documents/library.json';
import arrayShift from '../fixtures/patches/array-shift.json';
import escapePatch from '../fixtures/patches/escape-and-empty-key.json';
import midFailure from '../fixtures/patches/mid-failure-test.json';
import moveDescendant from '../fixtures/patches/move-into-descendant.json';
import moveCopyShift from '../fixtures/patches/move-copy-shift.json';

const clone = <T,>(v: T): T => structuredClone(v);

/** Assert a failed outcome and narrow the union to PatchFailure. */
function failed(outcome: PatchOutcome): PatchFailure {
  expect(outcome.ok).toBe(false);
  if (outcome.ok) throw new Error('expected the patch to fail');
  return outcome;
}

describe('kernel — array shifting and append semantics (fixture: array-shift)', () => {
  it('insert shifts later elements right, remove shifts left, "-" appends', () => {
    const input = clone(library) as unknown as JsonValue;
    const outcome = applyPatch(input, arrayShift);

    expect(outcome.ok).toBe(true);
    if (!outcome.ok) throw new Error(outcome.message);
    const result = outcome.result as { tags: string[] };
    expect(result.tags).toEqual(['inserted', 'beta', 'gamma', 'tail']);
    expect(outcome.applied).toBe(7);
  });

  it('records the document state after every single step', () => {
    const outcome = applyPatch(clone(library) as unknown as JsonValue, arrayShift);
    if (!outcome.ok) throw new Error(outcome.message);

    const after = (i: number) =>
      (outcome.traces.find((t) => t.index === i)!.documentAfter as { tags: string[] }).tags;

    expect(after(0)).toEqual(['alpha', 'beta', 'gamma']); // test: unchanged
    expect(after(1)).toEqual(['alpha', 'inserted', 'beta', 'gamma']); // insert at 1
    expect(after(2)).toEqual(['alpha', 'inserted', 'beta', 'gamma']); // test beta at 2
    expect(after(3)).toEqual(['alpha', 'inserted', 'beta', 'gamma']); // test gamma at 3
    expect(after(4)).toEqual(['inserted', 'beta', 'gamma']); // remove 0 shifts left
    expect(after(5)).toEqual(['inserted', 'beta', 'gamma']); // test new head
    expect(after(6)).toEqual(['inserted', 'beta', 'gamma', 'tail']); // append
  });

  it('inserting at index === length behaves like append; beyond length fails', () => {
    const ok = applyPatch({ xs: [1] }, [{ op: 'add', path: '/xs/1', value: 2 }]);
    expect(ok.ok).toBe(true);
    expect((ok.result as { xs: number[] }).xs).toEqual([1, 2]);

    const bad = applyPatch({ xs: [1] }, [{ op: 'add', path: '/xs/3', value: 9 }]);
    const badFailure = failed(bad);
    expect(badFailure.category).toBe('ARRAY_INDEX_OUT_OF_BOUNDS');
    expect(badFailure.failedAtIndex).toBe(0);
  });

  it('remove requires an existing target (object key and array index)', () => {
    const missingKey = failed(applyPatch({ a: 1 }, [{ op: 'remove', path: '/b' }]));
    expect(missingKey.category).toBe('POINTER_TARGET_MISSING');

    const missingIndex = failed(applyPatch({ xs: [1] }, [{ op: 'remove', path: '/xs/2' }]));
    expect(missingIndex.category).toBe('POINTER_TARGET_MISSING');

    const root = failed(applyPatch({ a: 1 }, [{ op: 'remove', path: '' }]));
    expect(root.category).toBe('PATH_TYPE_MISMATCH');
  });

  it('rejects adding a child to a scalar or null parent', () => {
    const scalar = failed(applyPatch({ n: 1 }, [{ op: 'add', path: '/n/child', value: 2 }]));
    expect(scalar.category).toBe('PATH_TYPE_MISMATCH');

    const nullParent = failed(applyPatch({ x: null }, [{ op: 'add', path: '/x/child', value: 2 }]));
    expect(nullParent.category).toBe('PATH_TYPE_MISMATCH');
  });

  it('classifies a missing intermediate parent distinctly from a missing leaf', () => {
    const intermediate = failed(applyPatch({ a: {} }, [{ op: 'replace', path: '/a/b/c', value: 1 }]));
    expect(intermediate.category).toBe('POINTER_PARENT_MISSING');

    const leaf = failed(applyPatch({ a: { b: 1 } }, [{ op: 'replace', path: '/a/c', value: 1 }]));
    expect(leaf.category).toBe('POINTER_TARGET_MISSING');
  });
});

describe('kernel — empty key and slash/tilde escaping (fixture: escape-and-empty-key)', () => {
  it('navigates empty tokens, escaped slashes/tildes and edits the right leaf', () => {
    const outcome = applyPatch(clone(library) as unknown as JsonValue, escapePatch);
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) throw new Error(outcome.message);
    const weird = (outcome.result as { weird: Record<string, unknown> }).weird;
    const emptyKeyed = weird[''] as Record<string, number | string>;
    expect(emptyKeyed).toEqual({
      'a/b': 10,
      'c~d': 2,
      'e~1f': 30,
      'g~01h': 4,
      newKey: 'empty-key-add',
    });
  });

  it('distinguishes tilde-escape edge cases on read', () => {
    const doc = { 'e~1f': 'target', 'g~01h': 4 };
    expect(applyPatch(doc, [{ op: 'test', path: '/e~01f', value: 'target' }]).ok).toBe(true);
    expect(applyPatch(doc, [{ op: 'test', path: '/g~001h', value: 4 }]).ok).toBe(true);
    const bad = applyPatch(doc, [{ op: 'test', path: '/e~1f', value: 'target' }]);
    // /e~1f decodes to key "e/f" which does not exist.
    expect(failed(bad).category).toBe('POINTER_TARGET_MISSING');
  });
});

describe('kernel — atomic rollback on mid-patch test failure', () => {
  it('reverts ALL earlier applied operations when a later test fails', () => {
    const input = clone(library) as unknown as JsonValue;
    const snapshot = JSON.stringify(input);
    const outcome = failed(applyPatch(input, midFailure));

    expect(outcome.category).toBe('TEST_FAILURE');
    expect(outcome.failedAtIndex).toBe(3);
    expect(outcome.rolledBack).toBe(true);

    // Returned document is the ORIGINAL and the input object was never mutated.
    expect(JSON.stringify(outcome.result)).toBe(snapshot);
    expect(outcome.result).toBe(input);
    expect(JSON.stringify(input)).toBe(snapshot);

    // The failed step exposes the document state AT failure (earlier ops visible),
    // while the overall result is rolled back.
    const failedStep = outcome.traces.find((t) => t.status === 'failed')!;
    const docAt = failedStep.documentAt as { title: string; tags: string[] };
    expect(docAt.title).toBe('Mutated Title');
    expect(docAt.tags).toContain('extra');

    // Trailing op after failure is marked skipped, never applied.
    expect(outcome.traces.map((t) => t.status)).toEqual([
      'applied',
      'applied',
      'applied',
      'failed',
      'skipped',
    ]);
  });

  it('test failure details carry expected vs actual values', () => {
    const outcome = failed(applyPatch({ n: 1 }, [{ op: 'test', path: '/n', value: 2 }]));
    expect(outcome.details).toMatchObject({ path: '/n', expected: 2, actual: 1 });
  });
});

describe('kernel — move rules', () => {
  it('refuses to move a value into one of its own descendants (fixture)', () => {
    const outcome = failed(applyPatch(clone(library) as unknown as JsonValue, moveDescendant));
    expect(outcome.category).toBe('MOVE_INTO_DESCENDANT');
    expect(outcome.failedAtIndex).toBe(0);
    expect(outcome.details).toMatchObject({ from: '/shelves/0', path: '/shelves/0/books/-' });
    // No state changed.
    expect(JSON.stringify(outcome.result)).toBe(JSON.stringify(library));
  });

  it('moving within an array removes at source then inserts at target (fixture)', () => {
    const outcome = applyPatch(clone(library) as unknown as JsonValue, moveCopyShift);
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) throw new Error(outcome.message);
    const r = outcome.result as { tags: string[]; shelves: Array<{ name: string }> };
    expect(r.tags).toEqual(['gamma', 'alpha', 'beta']);
    expect(r.shelves.map((s) => s.name)).toEqual(['a', 'b', 'b']);
    expect(outcome.applied).toBe(6);
  });

  it('move to the same path is a no-op-equivalent (remove then add same value)', () => {
    const outcome = applyPatch({ a: 1, b: 2 }, [{ op: 'move', from: '/a', path: '/a' }]);
    expect(outcome.ok).toBe(true);
    expect(outcome.result).toEqual({ a: 1, b: 2 });
  });
});

describe('kernel — operation chaining reads each prior result', () => {
  it('add then test the added location then replace and remove', () => {
    const outcome = applyPatch(
      { obj: {} },
      [
        { op: 'add', path: '/obj/k', value: [1, 2] },
        { op: 'test', path: '/obj/k/1', value: 2 },
        { op: 'replace', path: '/obj/k/1', value: { nested: true } },
        { op: 'test', path: '/obj/k/1/nested', value: true },
        { op: 'remove', path: '/obj/k/0' },
      ],
    );
    expect(outcome.ok).toBe(true);
    expect(outcome.result).toEqual({ obj: { k: [{ nested: true }] } });
  });

  it('copy does not alias storage: mutating the copy later leaves source intact', () => {
    const outcome = applyPatch(
      { src: { x: 1 }, holder: {} },
      [
        { op: 'copy', from: '/src', path: '/holder/cp' },
        { op: 'replace', path: '/holder/cp/x', value: 2 },
      ],
    );
    expect(outcome.ok).toBe(true);
    const r = outcome.result as { src: { x: number }; holder: { cp: { x: number } } };
    expect(r.src.x).toBe(1);
    expect(r.holder.cp.x).toBe(2);
  });
});

describe('kernel — immutability of input', () => {
  it('never mutates the supplied document on success', () => {
    const input: JsonValue = { a: [1, 2], b: { c: 3 } };
    const before = JSON.stringify(input);
    const outcome = applyPatch(input, [
      { op: 'add', path: '/a/-', value: 3 },
      { op: 'remove', path: '/b/c' },
    ]);
    expect(JSON.stringify(input)).toBe(before);
    expect(outcome.result).not.toBe(input);
  });
});

describe('kernel — contract validation produces concrete failure categories', () => {
  const cases: Array<[string, unknown, string, number]> = [
    ['patch is not an array', { op: 'add' }, 'MALFORMED_PATCH', -1],
    ['entry is not an object', [42], 'MALFORMED_PATCH', 0],
    ['unknown op', [{ op: 'frobnicate', path: '/a', value: 1 }], 'MALFORMED_PATCH', 0],
    ['missing path', [{ op: 'remove' }], 'MALFORMED_PATCH', 0],
    ['move without from', [{ op: 'move', path: '/a' }], 'MALFORMED_PATCH', 0],
    ['add without value', [{ op: 'add', path: '/a' }], 'MALFORMED_PATCH', 0],
    ['remove carrying value', [{ op: 'remove', path: '/a', value: 1 }], 'MALFORMED_PATCH', 0],
    ['test carrying from', [{ op: 'test', path: '/a', value: 1, from: '/b' }], 'MALFORMED_PATCH', 0],
    ['unknown member', [{ op: 'add', path: '/a', value: 1, extra: 2 }], 'MALFORMED_PATCH', 0],
    ['bad escape in path', [{ op: 'remove', path: '/~2' }], 'INVALID_POINTER', 0],
  ];

  it.each(cases)('%s', (_label, ops, category, at) => {
    const outcome = failed(applyPatch({}, ops));
    expect(outcome.category).toBe(category);
    expect(outcome.failedAtIndex).toBe(at);
  });

  it('uses lazy evaluation order: runtime error at index 0 beats bad pointer at index 1', () => {
    const outcome = failed(
      applyPatch(
        { a: 1 },
        [{ op: 'test', path: '/a', value: 2 }, { op: 'remove', path: '/~x' }],
      ),
    );
    expect(outcome.failedAtIndex).toBe(0);
    expect(outcome.category).toBe('TEST_FAILURE');
  });
});
