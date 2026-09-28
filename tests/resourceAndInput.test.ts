import { describe, expect, it } from 'vitest';
import { BoundedSemaphore } from '../src/kernel/semaphore.js';
import { CompositeKernel } from '../src/kernel/executor.js';
import { parseContract } from '../src/contract/parser.js';
import { FixtureSource } from '../src/sources/fixtureSource.js';
import { buildScenario, HAPPY_INPUT, snapshotToken } from '../src/fixtures/scenario.js';
import { RunStore } from '../src/state/runStore.js';
import { nodeById, recordVerdict } from './helpers.js';

describe('resource exhaustion is distinguishable', () => {
  it('fails fast with RESOURCE_EXHAUSTED when the bounded queue is full', async () => {
    const runId = 'resource-exhausted';
    // Five INDEPENDENT slow nodes contend for one slot with a two-entry
    // queue: one acquires, two queue, two fail fast with exhaustion.
    const scenario = buildScenario();
    const slow = new FixtureSource('slow-fixture', {
      behavior: { latencyMs: 60, dataVersion: 'v1' },
      table: { lookupParam: 'k', rows: [{ k: 1, v: 'ok' }] },
    });
    scenario.sources.set('slow-fixture', slow);

    const contract = parseContract({
      name: 'fanout',
      version: 1,
      nodes: [1, 2, 3, 4, 5].map((i) => ({
        id: `n${i}`,
        source: 'slow-fixture',
        params: { k: { value: 1 } },
      })),
      fields: [1, 2, 3, 4, 5].map((i) => ({
        path: `f${i}`,
        required: false,
        ref: { from: `n${i}`, property: 'v', fallback: null },
      })),
    });

    const kernel = new CompositeKernel({
      sources: scenario.sources,
      semaphore: new BoundedSemaphore(1, 2),
    });
    const response = await kernel.run(contract, {
      runId,
      timeoutMs: 1000,
      snapshotToken: snapshotToken('v1'),
      params: {},
    });

    const exhausted = response.nodeResults.filter(
      (n) => n.failure?.category === 'RESOURCE_EXHAUSTED',
    );
    expect(exhausted.length).toBe(2);
    expect(exhausted[0]!.failure!.code).toBe('LOCAL_CONCURRENCY_LIMIT');
    expect(exhausted[0]!.failure!.retryable).toBe(true);

    // It is classified distinctly from source failure / timeout / cancel.
    const categories = new Set(response.failures.map((f) => f.category));
    expect(categories.has('RESOURCE_EXHAUSTED')).toBe(true);
    expect(categories.has('SOURCE_TIMEOUT')).toBe(false);

    expect(slow.invocationCount).toBe(3); // 1 running + 2 queued admitted
    recordVerdict(
      runId,
      'PASS',
      'queue cap overflow -> RESOURCE_EXHAUSTED/LOCAL_CONCURRENCY_LIMIT on exactly 2 nodes, retryable, 3 sources admitted',
    );
  });
});

describe('input errors are distinguishable', () => {
  it('rejects a missing required request parameter with MISSING_INPUT', async () => {
    const runId = 'missing-input-param';
    const scenario = buildScenario();
    const kernel = new CompositeKernel({
      sources: scenario.sources,
      semaphore: new BoundedSemaphore(4, 16),
    });
    await expect(
      kernel.run(scenario.contract, {
        runId,
        params: { customerId: 'C1' }, // sku absent
      }),
    ).rejects.toMatchObject({
      category: 'MISSING_INPUT',
      code: 'MISSING_REQUEST_PARAM',
      httpStatus: 400,
    });
    recordVerdict(runId, 'PASS', 'missing sku -> MISSING_INPUT 400 before any scheduling');
  });

  it('fails a node that reads a property missing on an upstream record (SOURCE_FAILURE)', async () => {
    const runId = 'upstream-property-missing';
    const scenario = buildScenario();
    // Tamper with the parsed contract: inventory expects a property the
    // customers row does not carry.
    const contract = structuredClone(scenario.contract) as typeof scenario.contract;
    const inventory = contract.nodes.find((n) => n.id === 'inventory')!;
    const regionParam = inventory.params['region'];
    if (!regionParam || regionParam.kind !== 'ref') throw new Error('test setup: region param missing');
    regionParam.property = 'nonexistent';

    const store = new RunStore(':memory:');
    const kernel = new CompositeKernel({
      sources: scenario.sources,
      semaphore: new BoundedSemaphore(4, 16),
      store,
    });
    const response = await kernel.run(contract, {
      runId,
      timeoutMs: 1000,
      snapshotToken: snapshotToken('v1'),
      params: HAPPY_INPUT,
    });

    const inventoryNode = nodeById(response, 'inventory');
    expect(inventoryNode.status).toBe('failed');
    expect(inventoryNode.failure).toMatchObject({
      category: 'SOURCE_FAILURE',
      code: 'UPSTREAM_PROPERTY_MISSING',
    });
    recordVerdict(
      runId,
      'PASS',
      'missing upstream property classified SOURCE_FAILURE/UPSTREAM_PROPERTY_MISSING, not a crash',
    );
  });
});

describe('BoundedSemaphore unit behaviour', () => {
  it('rejects a queued acquirer when the signal aborts', async () => {
    const sem = new BoundedSemaphore(1, 5);
    const first = await sem.acquire('a');
    const ac = new AbortController();
    const waiting = sem.acquire('b', ac.signal);
    ac.abort(new Error('deadline passed'));
    await expect(waiting).rejects.toThrow('deadline passed');
    // Queue drained; a fresh acquire can queue without capacity leak.
    const second = sem.acquire('c');
    first();
    await expect(second).resolves.toBeTypeOf('function');
    (await second)();
  });

  it('rejects over-capacity when queue is at its cap', async () => {
    const sem = new BoundedSemaphore(1, 0);
    await sem.acquire('a');
    await expect(sem.acquire('b')).rejects.toMatchObject({
      category: 'RESOURCE_EXHAUSTED',
    });
  });
});
