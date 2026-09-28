/**
 * Diamond dependency behavior (healthy case).
 *
 * Graph: profile, product (roots) -> quote, stock, upsell converge both branches
 * (the diamond); contact <- profile; promo <- product.
 *
 * Assertions are concrete: exact call counts, exact per-node attempts, exact
 * values independently recomputed from the fixture file, field provenance, and
 * the snapshot-incapable legacy source producing a consistency limitation.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { runComposite, expectedFixtureValues } from './helpers/harness.js';

describe('diamond dependency: orderSummary healthy', () => {
  it('invokes every node exactly once and resolves the diamond', async () => {
    const { result, registry } = await runComposite({ baseLatencyMs: 0 });

    assert.equal(result.status, 'complete');
    assert.equal(result.nodes.length, 7);
    for (const node of result.nodes) {
      assert.equal(node.attempts, 1, `node ${node.callId} must be invoked exactly once`);
      assert.equal(node.state, 'resolved', `node ${node.callId} must resolve`);
    }
    // One invocation per source method, independently observed at the registry.
    assert.equal(registry.count('users', 'getProfile'), 1);
    assert.equal(registry.count('catalog', 'getProduct'), 1);
    assert.equal(registry.count('pricing', 'getQuote'), 1);
    assert.equal(registry.count('inventory', 'getAvailability'), 1);
    assert.equal(registry.count('recommendations', 'getRecommendation'), 1);
    assert.equal(registry.count('contacts', 'getContact'), 1);
    assert.equal(registry.count('promotions', 'getPromotion'), 1);
    assert.equal(registry.all().length, 7);
  });

  it('converges both diamond branches: quote/stock/upsell receive profile AND product values', async () => {
    const { registry } = await runComposite({ baseLatencyMs: 0 });
    const quoteReq = registry.all().find((i) => i.source === 'pricing')!.request as {
      sku: string;
      loyaltyTier: string;
    };
    assert.deepEqual(quoteReq, { sku: 'sku-1', loyaltyTier: 'gold' });

    const stockReq = registry.all().find((i) => i.source === 'inventory')!.request as {
      region: string;
      sku: string;
    };
    assert.deepEqual(stockReq, { region: 'cn-east', sku: 'sku-1' });

    const upsellReq = registry.all().find((i) => i.source === 'recommendations')!.request as {
      userId: string;
      sku: string;
    };
    assert.deepEqual(upsellReq, { userId: 'u-1001', sku: 'sku-1' });
  });

  it('returns concrete typed values independently expected from the fixtures', async () => {
    const expected = expectedFixtureValues();
    const { result, field, node } = await runComposite({ baseLatencyMs: 0 });

    assert.equal(result.data.customerName, expected.userName);
    assert.equal(result.data.region, expected.region);
    assert.equal(result.data.finalPrice, expected.goldFinalPriceForSku1); // 1104.15
    assert.equal(result.data.available, true);
    assert.equal(result.data.warehouseCount, 3);
    assert.equal(result.data.recommendedSku, 'sku-2');
    assert.equal(result.data.email, expected.email);
    // sku-1 carries a concrete promotion code in the fixtures.
    assert.equal(result.data.promotionCode, 'WELCOME10');

    // Field provenance is explicit on every field.
    assert.equal(field('finalPrice').source, 'pricing');
    assert.equal(field('finalPrice').call, 'quote');
    assert.equal(field('finalPrice').requirement, 'required');
    assert.equal(field('customerName').source, 'users');
    assert.equal(field('available').source, 'inventory');
    assert.equal(field('promotionCode').state, 'present');
    assert.equal(node('promo').state, 'resolved');
  });

  it('reports an optional field as missing when the fixture explicitly has null (sku-2 promo)', async () => {
    const { result, field, node } = await runComposite({
      baseLatencyMs: 0,
      input: { userId: 'u-1002', sku: 'sku-2' },
    });
    assert.equal(node('promo').state, 'resolved');
    assert.equal(field('promotionCode').state, 'missing');
    assert.equal(field('promotionCode').requirement, 'optional');
    assert.equal(result.data.promotionCode, undefined);
    // A legal optional absence does not degrade an otherwise complete run.
    assert.equal(result.status, 'complete');
  });

  it('propagates the SAME snapshot token and deadline to every source call', async () => {
    const { registry, result } = await runComposite({
      baseLatencyMs: 0,
      timeoutMs: 432,
      snapshotToken: 'snap-shared-xyz',
    });
    for (const invocation of registry.all()) {
      assert.equal(invocation.contextSnapshot.snapshotToken, 'snap-shared-xyz');
      assert.equal(invocation.contextSnapshot.timeoutMs, 432);
      assert.equal(invocation.contextSnapshot.runId, result.runId);
    }
  });

  it('flags SNAPSHOT_UNSUPPORTED for the legacy contacts source (consistency limitation, not failure)', async () => {
    const { result } = await runComposite({ baseLatencyMs: 0 });
    const snapshotLimitation = result.limitations.find((l) => l.type === 'SNAPSHOT_UNSUPPORTED');
    assert.ok(snapshotLimitation, 'a snapshot-incapable source must be reported as a limitation');
    assert.deepEqual(snapshotLimitation!.sources, ['contacts']);
    // Other sources did honor the token.
    const contactNode = result.nodes.find((n) => n.callId === 'contact')!;
    assert.equal(contactNode.snapshotHonored, false);
    assert.equal(result.status, 'complete');
  });
});
