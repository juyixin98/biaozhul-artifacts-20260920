import { describe, expect, it } from 'vitest';
import { createHarness, fieldByPath, nodeById, recordVerdict } from './helpers.js';

describe('deadline propagation and cancellation', () => {
  it('times out the OPTIONAL node, defaults its field, and never blocks the join', async () => {
    const runId = 'optional-promotions-timeout';
    const h = createHarness(
      { promotionsHang: true, promotionsLatencyMs: 5000 },
      runId,
    );

    const started = Date.now();
    const response = await h.run({ runId, timeoutMs: 1000 });
    const wallMs = Date.now() - started;

    // The run settles close to the node deadline (80ms), nowhere near 5000ms.
    expect(wallMs).toBeLessThan(400);
    expect(response.outcome).toBe('complete');

    const promotions = nodeById(response, 'promotions');
    expect(promotions.status).toBe('timed-out');
    expect(promotions.attempts).toBe(1);
    expect(promotions.failure).toMatchObject({
      category: 'SOURCE_TIMEOUT',
      code: 'NODE_DEADLINE_EXCEEDED',
      retryable: true,
    });
    // Effective deadline = started + 80ms (allow 1ms timer rounding).
    const effectiveBudget = promotions.deadline - promotions.startedAt!;
    expect(effectiveBudget).toBeGreaterThanOrEqual(79);
    expect(effectiveBudget).toBeLessThanOrEqual(81);

    // The source received the abort and did no simulated work afterwards.
    const promoCall = h.scenario.handles.promotions.calls[0]!;
    expect(promoCall.aborted).toBe(true);
    expect(promoCall.outcome).toBe('aborted');
    expect(promoCall.postAbortTicks).toBe(0);

    // Optional field defaults with a timeout reason attached.
    const discount = fieldByPath(response, 'discount');
    expect(discount.status).toBe('missing-optional-default');
    expect(discount.value).toBe(0);
    expect(discount.source).toBe('promotions.discount::fallback');
    expect(discount.reasons[0]).toMatchObject({
      category: 'SOURCE_TIMEOUT',
      code: 'NODE_DEADLINE_EXCEEDED',
    });

    // Join node was NOT blocked by the slow optional dependency: invoked once.
    const recommendations = nodeById(response, 'recommendations');
    expect(recommendations.status).toBe('succeeded');
    expect(recommendations.attempts).toBe(1);
    expect(h.scenario.handles.recommendations.invocationCount).toBe(1);
    // It ran with the optional param absent (default undefined).
    const recCall = h.scenario.handles.recommendations.calls[0]!;
    expect(recCall.params['promoDiscount']).toBeUndefined();

    // A late-settle-ignored marker records that the source's eventual
    // rejection was discarded after the timeout.
    expect(
      h.sink.eventsFor('promotions').some((e) => e.type === 'late-settle-ignored'),
    ).toBe(true);

    recordVerdict(
      runId,
      'PASS',
      'optional node timed out at 80ms with abort + zero post-abort work; field defaulted; join ran once; run settled << 5000ms',
    );
  });

  it('propagates the run deadline: cancels in-flight nodes and never starts later ones', async () => {
    const runId = 'run-wide-deadline-cancellation';
    // Customers takes 30ms; the 10ms run deadline cancels it mid-flight.
    // Its dependent inventory must never be invoked; the independent pricing
    // branch (0ms) still completes, proving the cancellation is scoped.
    const h = createHarness(
      { customersLatencyMs: 30, pricingLatencyMs: 0 },
      runId,
    );

    const startedAt = Date.now();
    const response = await h.run({ runId, timeoutMs: 10 });
    const wallMs = Date.now() - startedAt;

    // Kernel returns promptly after the deadline rather than hanging.
    expect(wallMs).toBeLessThan(300);
    expect(response.outcome).toBe('partial');

    // In-flight nodes are cancelled with exactly one attempt.
    const customers = nodeById(response, 'customers');
    expect(customers.status).toBe('cancelled');
    expect(customers.attempts).toBe(1);
    expect(customers.failure?.category).toBe('CANCELLED');
    expect(customers.failure?.code).toBe('RUN_DEADLINE_EXCEEDED');

    // promotions requires customers; its upstream was cancelled, so it is
    // skipped without invocation — but the skipped failure preserves the
    // CANCELLED root cause (causedBy RUN_DEADLINE_EXCEEDED).
    const promotions = nodeById(response, 'promotions');
    expect(promotions.status).toBe('skipped');
    expect(promotions.attempts).toBe(0);
    expect(promotions.didNotInvoke).toBe(true);
    expect(promotions.failure?.category).toBe('CANCELLED');
    expect(promotions.failure?.causedBy).toBe('RUN_DEADLINE_EXCEEDED');
    expect(h.scenario.handles.promotions.invocationCount).toBe(0);

    // Dependents of the cancelled required node never start.
    const inventory = nodeById(response, 'inventory');
    expect(inventory.status).toBe('skipped');
    expect(inventory.attempts).toBe(0);
    expect(inventory.didNotInvoke).toBe(true);
    expect(inventory.failure?.code).toBe('UPSTREAM_DEPENDENCY_FAILED');
    expect(inventory.failure?.category).toBe('CANCELLED');

    // Join node: its required upstreams were cancelled/skipped, so it too is
    // skipped deterministically without invocation.
    const recommendations = nodeById(response, 'recommendations');
    expect(recommendations.status).toBe('skipped');
    expect(recommendations.attempts).toBe(0);
    expect(recommendations.didNotInvoke).toBe(true);
    expect(h.scenario.handles.recommendations.invocationCount).toBe(0);
    expect(h.scenario.handles.inventory.invocationCount).toBe(0);

    // Fast independent branch still delivered its value.
    expect(nodeById(response, 'pricing').status).toBe('succeeded');
    expect(response.data['price']).toBe(100);

    // The slow sources received the abort and did no simulated work afterwards.
    for (const handle of [h.scenario.handles.customers, h.scenario.handles.promotions]) {
      for (const call of handle.calls) {
        expect(call.aborted).toBe(true);
        expect(call.postAbortTicks).toBe(0);
      }
    }

    // The event trace contains a run-level deadline event.
    expect(
      h.sink.events.some((e) => e.type === 'deadline-fired' && e.scope === 'run'),
    ).toBe(true);

    recordVerdict(
      runId,
      'PASS',
      '10ms run deadline cancelled in-flight customers/promotions, dependents never invoked (attempts=0), no background work, pricing intact',
    );
  });

  it('uses the node-local budget when it is tighter than the run deadline', async () => {
    const runId = 'node-deadline-tighter';
    // Customers resolves in ~1ms; the run allows 1000ms but promotions
    // declares an 80ms budget, so its effective deadline is now+80.
    const h = createHarness(
      { customersLatencyMs: 1, promotionsHang: true, promotionsLatencyMs: 5000 },
      runId,
    );
    const response = await h.run({ runId, timeoutMs: 1000 });

    const promotions = nodeById(response, 'promotions');
    expect(promotions.status).toBe('timed-out');
    const nodeBudget = promotions.deadline - promotions.startedAt!;
    expect(nodeBudget).toBeGreaterThanOrEqual(79);
    expect(nodeBudget).toBeLessThanOrEqual(81);
    recordVerdict(runId, 'PASS', 'effective node deadline = now + min(node budget 80ms, run 1000ms) ~= 80ms');
  });

  it('propagates the tighter run deadline even to nodes that never start', async () => {
    const runId = 'run-deadline-tighter-recorded';
    // Customers (30ms) misses the 5ms run deadline; promotions is therefore
    // skipped — but every node record still carries the propagated deadline.
    const h = createHarness({ customersLatencyMs: 30 }, runId);
    const response = await h.run({ runId, timeoutMs: 5 });

    for (const node of response.nodeResults) {
      expect(node.deadline).toBe(response.startedAt + 5);
    }
    const promotions = nodeById(response, 'promotions');
    expect(promotions.status).toBe('skipped');
    expect(promotions.attempts).toBe(0);
    expect(promotions.failure?.category).toBe('CANCELLED');
    recordVerdict(
      runId,
      'PASS',
      'all node records carry startedAt+5ms; skipped node preserves CANCELLED root cause',
    );
  });
});
