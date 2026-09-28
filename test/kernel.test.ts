import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import type { PatchOperation } from '../src/contract.ts';
import {
  applyPatch,
  jsonDeepEqual,
  type KernelFailure,
  type KernelSuccess,
} from '../src/kernel.ts';

const here = dirname(fileURLToPath(import.meta.url));

interface HandCase {
  id: string;
  document: unknown;
  operations: PatchOperation[];
  expected: {
    ok: boolean;
    result?: unknown;
    category?: string;
    failedAtIndex?: number;
    appliedBeforeFailure?: number;
    documentUnchanged?: unknown;
  };
}

const fixture = JSON.parse(
  readFileSync(join(here, 'fixtures', 'hand-authored-cases.json'), 'utf8'),
) as { cases: HandCase[] };

describe('kernel: hand-authored RFC 6902 boundary cases', () => {
  for (const testCase of fixture.cases) {
    it(testCase.id, () => {
      const snapshot = structuredClone(testCase.document);
      const outcome = applyPatch(testCase.document, testCase.operations);

      if (testCase.expected.ok) {
        assert.equal(outcome.ok, true, failureMessage(outcome));
        const success = outcome as KernelSuccess;
        assert.deepEqual(success.result, testCase.expected.result);
        assert.equal(success.steps.length, testCase.operations.length);
        // Per-step documented state: each resultAfter is concrete.
        assert.deepEqual(success.steps[success.steps.length - 1]!.resultAfter,
          testCase.expected.result);
      } else {
        assert.equal(outcome.ok, false, 'expected failure but patch succeeded');
        const failure = outcome as KernelFailure;
        assert.equal(failure.category, testCase.expected.category);
        assert.equal(failure.failedAtIndex, testCase.expected.failedAtIndex);
        assert.equal(failure.rolledBack, true);
        if (testCase.expected.appliedBeforeFailure !== undefined) {
          assert.equal(
            failure.appliedBeforeFailure.length,
            testCase.expected.appliedBeforeFailure,
          );
        }
        if (testCase.expected.documentUnchanged !== undefined) {
          assert.deepEqual(testCase.document, testCase.expected.documentUnchanged);
        }
      }

      // The caller's input must never be mutated, success or failure.
      assert.deepEqual(testCase.document, snapshot);
    });
  }
});

describe('kernel: atomicity and per-step state', () => {
  it('records a concrete document snapshot after every successful step', () => {
    const outcome = applyPatch(
      { a: [1], b: 0 },
      [
        { op: 'add', path: '/a/-', value: 2 },
        { op: 'add', path: '/b', value: 1 },
        { op: 'copy', from: '/a', path: '/c' },
      ],
    );
    assert.equal(outcome.ok, true);
    const success = outcome as KernelSuccess;
    assert.deepEqual(success.steps[0]!.resultAfter, { a: [1, 2], b: 0 });
    assert.deepEqual(success.steps[1]!.resultAfter, { a: [1, 2], b: 1 });
    assert.deepEqual(success.steps[2]!.resultAfter, { a: [1, 2], b: 1, c: [1, 2] });
  });

  it('keeps earlier step snapshots even when a later test fails', () => {
    const outcome = applyPatch(
      { list: [1] },
      [
        { op: 'add', path: '/list/-', value: 2 },
        { op: 'test', path: '/list/0', value: 99 },
      ],
    );
    assert.equal(outcome.ok, false);
    const failure = outcome as KernelFailure;
    assert.equal(failure.category, 'TEST_FAILED');
    assert.equal(failure.failedAtIndex, 1);
    assert.equal(failure.appliedBeforeFailure.length, 1);
    assert.deepEqual(failure.appliedBeforeFailure[0]!.resultAfter, { list: [1, 2] });
  });

  it('added object values are independent copies (no aliasing into input)', () => {
    const value = { nested: [1] };
    const outcome = applyPatch({ x: 0 }, [{ op: 'add', path: '/y', value }]);
    assert.equal(outcome.ok, true);
    value.nested.push(2);
    const success = outcome as KernelSuccess;
    assert.deepEqual((success.result as { y: { nested: number[] } }).y.nested, [1]);
  });
});

describe('kernel: jsonDeepEqual', () => {
  it('compares structurally, not by reference', () => {
    assert.equal(jsonDeepEqual({ a: [1, { b: null }] }, { a: [1, { b: null }] }), true);
  });
  it('is type sensitive (number vs boolean)', () => {
    assert.equal(jsonDeepEqual(1, true), false);
    assert.equal(jsonDeepEqual({ a: 1 }, { a: true }), false);
  });
  it('distinguishes arrays from objects and missing keys', () => {
    assert.equal(jsonDeepEqual([1], { 0: 1 }), false);
    assert.equal(jsonDeepEqual({ a: 1, b: 2 }, { a: 1 }), false);
  });
});

function failureMessage(outcome: KernelSuccess | KernelFailure): string {
  return outcome.ok ? 'unexpected success' : `${outcome.category}: ${outcome.message}`;
}
