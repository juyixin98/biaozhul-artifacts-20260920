import { describe, expect, it } from 'vitest';
import { createHarness, fieldByPath, nodeById, recordVerdict } from './helpers.js';
import { snapshotToken } from '../src/fixtures/scenario.js';

describe('diamond dependency graph', () => {
  it('invokes the join node exactly once and assembles exact values', async () => {
    const runId = 'diamond-happy';
    const h = createHarness({}, runId);
    const response = await h.run({ runId, snapshotToken: snapshotToken('v1') });

    // --- Outcome & concrete assembled values (from fixture table literals) ---
    expect(response.outcome).toBe('complete');
    expect(response.data).toEqual({
      customer: { name: 'Alice', tier: 'gold' },
      product: { name: 'Widget', stock: 7 },
      price: 100,
      fee: 5,
      discount: 15,
      recommendation: 'bundle Widget + Gadget at 10% off',
      label: 'Alice / Widget',
      total: 105,
    });

    // --- Call counts: every source invoked exactly once, including the
    //     three-parent join node recommendations (diamond fan-in). ---
    expect(h.scenario.handles.customers.invocationCount).toBe(1);
    expect(h.scenario.handles.inventory.invocationCount).toBe(1);
    expect(h.scenario.handles.pricing.invocationCount).toBe(1);
    expect(h.scenario.handles.promotions.invocationCount).toBe(1);
    expect(h.scenario.handles.recommendations.invocationCount).toBe(1);

    for (const node of response.nodeResults) {
      expect(node.attempts).toBe(1);
      expect(node.didNotInvoke).toBe(false);
      expect(node.status).toBe('succeeded');
    }

    // --- The same snapshot token reached EVERY source. ---
    const tokens = [
      h.scenario.handles.customers.calls[0]!.snapshotToken,
      h.scenario.handles.inventory.calls[0]!.snapshotToken,
      h.scenario.handles.pricing.calls[0]!.snapshotToken,
      h.scenario.handles.promotions.calls[0]!.snapshotToken,
      h.scenario.handles.recommendations.calls[0]!.snapshotToken,
    ];
    expect(new Set(tokens).size).toBe(1);
    expect(tokens[0]).toBe(snapshotToken('v1'));

    // --- Data actually flowed along graph edges: recommendations got
    //     stock=7 from inventory and promoDiscount=15 from promotions. ---
    const recCall = h.scenario.handles.recommendations.calls[0]!;
    expect(recCall.params).toMatchObject({
      customerId: 'C1',
      sku: 'SKU-1',
      stock: 7,
      promoDiscount: 15,
    });
    const invCall = h.scenario.handles.inventory.calls[0]!;
    expect(invCall.params['region']).toBe('cn-north'); // A→B edge

    // --- Field provenance is explicit per field. ---
    expect(fieldByPath(response, 'customer.name').source).toBe('customers.name');
    expect(fieldByPath(response, 'price').source).toBe('pricing.price');
    expect(fieldByPath(response, 'fee').source).toBe('constant');
    expect(fieldByPath(response, 'fee').value).toBe(5);
    expect(fieldByPath(response, 'total').source).toBe('derive:add');
    expect(fieldByPath(response, 'total').value).toBe(105);
    expect(fieldByPath(response, 'label').source).toBe('derive:concat');

    // --- Every node recorded the propagated deadline it ran under. ---
    for (const node of response.nodeResults) {
      expect(node.deadline).toBeGreaterThan(node.startedAt!);
    }

    // --- Event trace: recommendations sees one ready/one invoked. ---
    const recEvents = h.sink.eventsFor('recommendations');
    expect(recEvents.filter((e) => e.type === 'node-invoked')).toHaveLength(1);
    expect(recEvents.filter((e) => e.type === 'node-ready')).toHaveLength(1);

    recordVerdict(
      runId,
      'PASS',
      'join node invoked once under three parents; exact values; one shared snapshot token',
    );
  });

  it('persists the run and its ordered event trace for replay', async () => {
    const runId = 'diamond-persisted';
    const h = createHarness({}, runId);
    const response = await h.run({ runId });

    const stored = h.store.getRun(runId);
    expect(stored?.outcome).toBe('complete');
    expect(JSON.parse(stored!.request_params)).toEqual({ customerId: 'C1', sku: 'SKU-1' });

    const events = h.store.getEvents(runId);
    expect(events[0]!.type).toBe('run-started');
    expect(events[events.length - 1]!.type).toBe('run-finished');
    // Monotonic seq: the trace is a replayable total order.
    events.forEach((e, i) => expect(e.seq).toBe(i));

    const counts = h.store.nodeStatusCounts(runId);
    expect(counts['succeeded']).toBe(response.nodeResults.length);

    expect(nodeById(response, 'customers').status).toBe('succeeded');
    recordVerdict(runId, 'PASS', 'run row, monotonic events and node counts persisted');
  });
});
