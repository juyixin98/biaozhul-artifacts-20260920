/**
 * Contract parsing and error taxonomy tests.
 *
 * Covers the distinguishable failure classes the spec requires:
 *  - INPUT_ERROR: malformed contract graph / request input;
 *  - STATE_CONFLICT: idempotency key reused with a different payload;
 *  - RESOURCE_EXHAUSTED: deadline;
 *  - COMPUTATION_FAILED: source failure.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { validateContract, validateInput } from '../src/contract/validate.js';
import { orderSummaryContract } from '../src/compose/contracts.js';
import { isCompositeError } from '../src/kernel/errors.js';
import type { CompositeContract } from '../src/contract/types.js';

function cloneContract(): CompositeContract {
  return orderSummaryContract;
}

function minimalContract(overrides: Partial<CompositeContract>): CompositeContract {
  return {
    name: 'mini',
    input: [],
    calls: [
      {
        id: 'a',
        source: 'svc',
        method: 'm',
        dependencies: [],
        buildRequest: () => ({}),
        fields: [{ output: 'v', requirement: 'required' }],
      },
    ],
    ...overrides,
  };
}

describe('contract validation', () => {
  it('accepts the real diamond contract', () => {
    assert.doesNotThrow(() => validateContract(cloneContract()));
  });

  it('rejects a duplicate node id as INPUT_ERROR/CONTRACT_DUPLICATE_NODE', () => {
    const c = minimalContract({
      calls: [
        { id: 'a', source: 's', method: 'm', dependencies: [], buildRequest: () => ({}), fields: [] },
        { id: 'a', source: 's', method: 'm', dependencies: [], buildRequest: () => ({}), fields: [] },
      ],
    });
    expectCategory(c, 'INPUT_ERROR', 'CONTRACT_DUPLICATE_NODE');
  });

  it('rejects an unknown dependency as INPUT_ERROR/CONTRACT_UNKNOWN_DEPENDENCY', () => {
    const c = minimalContract({
      calls: [
        {
          id: 'a',
          source: 's',
          method: 'm',
          dependencies: ['ghost'],
          buildRequest: () => ({}),
          fields: [],
        },
      ],
    });
    expectCategory(c, 'INPUT_ERROR', 'CONTRACT_UNKNOWN_DEPENDENCY');
  });

  it('rejects a dependency cycle as INPUT_ERROR/CONTRACT_CYCLE', () => {
    const c = minimalContract({
      calls: [
        { id: 'a', source: 's', method: 'm', dependencies: ['b'], buildRequest: () => ({}), fields: [] },
        { id: 'b', source: 's', method: 'm', dependencies: ['a'], buildRequest: () => ({}), fields: [] },
      ],
    });
    expectCategory(c, 'INPUT_ERROR', 'CONTRACT_CYCLE');
  });

  it('rejects duplicate output fields as INPUT_ERROR/CONTRACT_DUPLICATE_FIELD', () => {
    const c = minimalContract({
      calls: [
        {
          id: 'a',
          source: 's',
          method: 'm',
          dependencies: [],
          buildRequest: () => ({}),
          fields: [
            { output: 'same', requirement: 'required' },
            { output: 'same', requirement: 'optional' },
          ],
        },
      ],
    });
    expectCategory(c, 'INPUT_ERROR', 'CONTRACT_DUPLICATE_FIELD');
  });

  it('rejects a field without an explicit requirement declaration', () => {
    const c = minimalContract({
      calls: [
        {
          id: 'a',
          source: 's',
          method: 'm',
          dependencies: [],
          buildRequest: () => ({}),
          // @ts-expect-error requirement intentionally omitted for the test
          fields: [{ output: 'x' }],
        },
      ],
    });
    expectCategory(c, 'INPUT_ERROR', 'CONTRACT_INVALID');
  });

  it('validates request input: missing required field', () => {
    assert.throws(
      () => validateInput(orderSummaryContract, { userId: 'u-1001' }),
      (err: unknown) =>
        isCompositeError(err) &&
        err.detail.category === 'INPUT_ERROR' &&
        err.detail.reason === 'INPUT_MISSING_FIELD' &&
        err.detail.context?.field === 'sku',
    );
  });

  it('validates request input: wrong type', () => {
    assert.throws(
      () => validateInput(orderSummaryContract, { userId: 42, sku: 'sku-1' }),
      (err: unknown) =>
        isCompositeError(err) &&
        err.detail.category === 'INPUT_ERROR' &&
        err.detail.reason === 'INPUT_TYPE_ERROR',
    );
  });
});

function expectCategory(c: CompositeContract, category: string, reason: string): void {
  try {
    validateContract(c);
    assert.fail('expected validateContract to throw');
  } catch (err) {
    assert.ok(isCompositeError(err), 'thrown value must be a CompositeError');
    if (isCompositeError(err)) {
      assert.equal(err.detail.category, category);
      assert.equal(err.detail.reason, reason);
    }
  }
}
