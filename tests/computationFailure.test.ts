import { describe, expect, it } from 'vitest';
import { parseContract } from '../src/contract/parser.js';
import { CompositeKernel } from '../src/kernel/executor.js';
import { BoundedSemaphore } from '../src/kernel/semaphore.js';
import { buildScenario, snapshotToken } from '../src/fixtures/scenario.js';
import { fieldByPath, recordVerdict } from './helpers.js';

describe('computation failures are distinguishable', () => {
  it('classifies an unregistered source as COMPUTATION_FAILED', async () => {
    const runId = 'computation-unregistered-source';
    const scenario = buildScenario();
    const contract = parseContract({
      name: 'broken',
      version: 1,
      nodes: [{ id: 'n1', source: 'does-not-exist' }],
      fields: [{ path: 'x', required: true, ref: { from: 'n1' } }],
    });
    const kernel = new CompositeKernel({
      sources: scenario.sources,
      semaphore: new BoundedSemaphore(4, 16),
    });
    const response = await kernel.run(contract, { runId, params: {} });

    expect(response.outcome).toBe('partial');
    expect(response.failures[0]).toMatchObject({
      category: 'COMPUTATION_FAILED',
      code: 'SOURCE_NOT_REGISTERED',
    });
    expect(fieldByPath(response, 'x').status).toBe('missing-required-failed');
    recordVerdict(runId, 'PASS', 'unknown source -> COMPUTATION_FAILED/SOURCE_NOT_REGISTERED');
  });

  it('classifies a non-numeric add operand as a computation failure on that field only', async () => {
    const runId = 'computation-derived-type-error';
    const scenario = buildScenario();
    // label (string) then an add over it -> DERIVED_TYPE_ERROR must not crash
    // unrelated fields.
    const contract = parseContract({
      name: 'orderDetails',
      version: 1,
      nodes: scenario.rawContract.nodes,
      fields: [
        ...(scenario.rawContract.fields as unknown[]),
        {
          path: 'badTotal',
          required: true,
          ref: { derive: { op: 'add', fields: ['label', 'fee'] } },
        },
      ],
    });
    const kernel = new CompositeKernel({
      sources: scenario.sources,
      semaphore: new BoundedSemaphore(4, 16),
    });
    const response = await kernel.run(contract, {
      runId,
      timeoutMs: 1000,
      snapshotToken: snapshotToken('v1'),
      params: { customerId: 'C1', sku: 'SKU-1' },
    });

    const bad = fieldByPath(response, 'badTotal');
    expect(bad.status).toBe('derivation-failed');
    expect(bad.reasons[0]).toMatchObject({
      category: 'COMPUTATION_FAILED',
      code: 'DERIVED_TYPE_ERROR',
    });
    // Healthy fields are unaffected.
    expect(fieldByPath(response, 'total').value).toBe(105);
    expect(response.outcome).toBe('partial');
    recordVerdict(
      runId,
      'PASS',
      'add over a string -> COMPUTATION_FAILED/DERIVED_TYPE_ERROR scoped to one field; total still 105',
    );
  });
});
