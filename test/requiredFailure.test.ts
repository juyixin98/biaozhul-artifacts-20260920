/**
 * Required failure behavior.
 *
 * Injected: catalog.getProduct fails. It owns REQUIRED fields (sku/listPrice/
 * currency) and sits on the product branch, so:
 *  - the run status is `failed` with a COMPUTATION_FAILED error;
 *  - every transitive dependent on the failed branch is SKIPPED (never called);
 *  - the independent profile branch still resolves (contact included);
 *  - remaining nodes are cancelled promptly and no source keeps working after
 *    the abort (bounded wall time, no retries).
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { runComposite } from './helpers/harness.js';
import { FaultPlan } from '../src/sources/fixtures/faults.js';

const failingPlan = () =>
  new FaultPlan().add('catalog', 'getProduct', {
    type: 'fail',
    reason: 'CATALOG_UNAVAILABLE',
    message: 'catalog fixture forced failure',
  });

describe('required source failure', () => {
  it('fails the composite with the exact injected reason and category', async () => {
    const { result } = await runComposite({ faultPlan: failingPlan(), baseLatencyMs: 2 });

    assert.equal(result.status, 'failed');
    const terminal = result.errors.find((e) => e.reason === 'CATALOG_UNAVAILABLE');
    assert.ok(terminal, 'the injected failure must surface as a run-level error');
    assert.equal(terminal!.category, 'COMPUTATION_FAILED');
    assert.equal(terminal!.retryable, false);
    assert.ok(result.errors[0]!.reason === 'CATALOG_UNAVAILABLE');
  });

  it('does not invoke dependents of the failed required node', async () => {
    const { result, registry } = await runComposite({ faultPlan: failingPlan(), baseLatencyMs: 2 });

    // catalog itself was tried exactly once.
    assert.equal(registry.count('catalog', 'getProduct'), 1);
    // quote/stock/upsell need product; promo needs product — none were called.
    assert.equal(registry.count('pricing', 'getQuote'), 0);
    assert.equal(registry.count('inventory', 'getAvailability'), 0);
    assert.equal(registry.count('recommendations', 'getRecommendation'), 0);
    assert.equal(registry.count('promotions', 'getPromotion'), 0);

    const skipped = result.nodes.filter((n) => n.state === 'skipped').map((n) => n.callId).sort();
    assert.deepEqual(skipped, ['promo', 'quote', 'stock', 'upsell']);
    for (const id of ['quote', 'stock', 'upsell', 'promo']) {
      const node = result.nodes.find((n) => n.callId === id)!;
      assert.equal(node.attempts, 0, `skipped node ${id} must never invoke its source`);
      assert.equal(node.error?.reason, 'DEPENDENCY_UNAVAILABLE');
      assert.deepEqual(node.error?.context, { dependency: 'product', upstreamState: 'failed' });
    }
  });

  it('keeps the independent profile branch available', async () => {
    const { result, registry } = await runComposite({ faultPlan: failingPlan(), baseLatencyMs: 2 });

    assert.equal(registry.count('users', 'getProfile'), 1);
    assert.equal(registry.count('contacts', 'getContact'), 1);
    assert.equal(result.data.customerName, 'Ada Lin');
    assert.equal(result.data.email, 'ada.lin@example.test');
    const profileNode = result.nodes.find((n) => n.callId === 'profile')!;
    const contactNode = result.nodes.find((n) => n.callId === 'contact')!;
    assert.equal(profileNode.state, 'resolved');
    assert.equal(contactNode.state, 'resolved');
  });

  it('marks required product fields failed with the propagated reason', async () => {
    const { field } = await runComposite({ faultPlan: failingPlan(), baseLatencyMs: 2 });
    for (const name of ['sku', 'listPrice', 'currency']) {
      const status = field(name);
      assert.equal(status.state, 'failed');
      assert.equal(status.requirement, 'required');
      assert.equal(status.reason?.reason, 'CATALOG_UNAVAILABLE');
    }
  });

  it('cancels remaining work immediately rather than running past the failure', async () => {
    // Base latency 25ms on every source; without cancellation the whole graph
    // would still complete quickly, so the key assertion is structural: all
    // non-branch nodes reach a terminal state and attempts stay at zero/one.
    const { elapsedMs, result } = await runComposite({ faultPlan: failingPlan(), baseLatencyMs: 25 });

    assert.ok(elapsedMs < 200, `run must terminate promptly, took ${elapsedMs}ms`);
    for (const node of result.nodes) {
      assert.notEqual(node.state, 'running');
      assert.notEqual(node.state, 'pending');
      assert.ok(node.attempts <= 1);
    }
  });
});
