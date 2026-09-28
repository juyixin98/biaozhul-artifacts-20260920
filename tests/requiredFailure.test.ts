import { describe, expect, it } from 'vitest';
import { createHarness, fieldByPath, nodeById, recordVerdict } from './helpers.js';

describe('required dependency failure aggregation', () => {
  it('aggregates a required source failure into partial data with field-level reasons', async () => {
    const runId = 'required-pricing-failure';
    const h = createHarness({ pricingFailForSku: 'SKU-BROKEN' }, runId);
    const response = await h.run({
      runId,
      params: { customerId: 'C1', sku: 'SKU-BROKEN' },
    });

    // Outcome is partial (not a 500-style hard failure): independent
    // branches still deliver their concrete values.
    expect(response.outcome).toBe('partial');
    expect(response.data).toEqual({
      customer: { name: 'Alice', tier: 'gold' },
      product: { name: 'Mystery', stock: 3 },
      fee: 5,
      discount: 15,
      recommendation: 'bundle Widget + Gadget at 10% off',
      label: 'Alice / Mystery',
    });
    expect(response.data['price']).toBeUndefined();

    // Pricing attempted exactly once and failed with the injected category.
    const pricing = nodeById(response, 'pricing');
    expect(pricing.status).toBe('failed');
    expect(pricing.attempts).toBe(1);
    expect(pricing.didNotInvoke).toBe(false);
    expect(pricing.failure).toMatchObject({
      category: 'SOURCE_FAILURE',
      code: 'PRICING_BACKEND_ERROR',
      retryable: false,
      at: 'node:pricing',
    });
    expect(h.scenario.handles.pricing.invocationCount).toBe(1);

    // Required field derived from a failed node carries the reason;
    // the optional/constant/independent fields are untouched.
    const priceField = fieldByPath(response, 'price');
    expect(priceField.status).toBe('missing-required-failed');
    expect(priceField.value).toBeUndefined();
    expect(priceField.reasons[0]).toMatchObject({
      category: 'SOURCE_FAILURE',
      code: 'PRICING_BACKEND_ERROR',
    });

    // Derived add over the missing required operand fails closed.
    const totalField = fieldByPath(response, 'total');
    expect(totalField.status).toBe('derivation-failed');
    expect(totalField.reasons.some((r) => r.code === 'DERIVED_OPERAND_MISSING')).toBe(true);

    // Top-level failures list contains the root cause exactly once.
    const rootCauses = response.failures.filter((f) => f.code === 'PRICING_BACKEND_ERROR');
    expect(rootCauses).toHaveLength(1);
    expect(rootCauses[0]!.category).toBe('SOURCE_FAILURE');

    recordVerdict(
      runId,
      'PASS',
      'required pricing failure -> partial; price/total missing with SOURCE_FAILURE reasons, other branches intact',
    );
  });

  it('skips dependents of a failed REQUIRED node and never invokes them', async () => {
    const runId = 'required-inventory-failure-skips-join';
    const h = createHarness({ inventoryFailForSku: 'SKU-BROKEN' }, runId);
    const response = await h.run({
      runId,
      params: { customerId: 'C1', sku: 'SKU-BROKEN' },
    });

    const inventory = nodeById(response, 'inventory');
    expect(inventory.status).toBe('failed');
    expect(inventory.failure?.category).toBe('SOURCE_FAILURE');

    // recommendations REQUIRES inventory (stock param) -> skipped, 0 invocations.
    const recommendations = nodeById(response, 'recommendations');
    expect(recommendations.status).toBe('skipped');
    expect(recommendations.attempts).toBe(0);
    expect(recommendations.didNotInvoke).toBe(true);
    expect(recommendations.failure?.code).toBe('UPSTREAM_DEPENDENCY_FAILED');
    expect(recommendations.failure?.causedBy).toBe('INVENTORY_BACKEND_ERROR');
    expect(h.scenario.handles.recommendations.invocationCount).toBe(0);

    // The optional recommendation field defaults instead of hard-failing.
    const recommendationField = fieldByPath(response, 'recommendation');
    expect(recommendationField.status).toBe('missing-optional-default');
    expect(recommendationField.value).toBe('generic-bundle');
    expect(recommendationField.source).toBe('recommendations.reason::fallback');

    // product fields are required and sourced from the failed (attempted)
    // inventory node itself -> required-failed, with the root cause attached.
    expect(fieldByPath(response, 'product.stock').status).toBe('missing-required-failed');
    expect(fieldByPath(response, 'product.stock').reasons[0]?.code).toBe(
      'INVENTORY_BACKEND_ERROR',
    );

    // Pricing is independent: still called once and succeeded.
    expect(nodeById(response, 'pricing').status).toBe('succeeded');
    expect(h.scenario.handles.pricing.invocationCount).toBe(1);

    expect(response.outcome).toBe('partial');
    recordVerdict(
      runId,
      'PASS',
      'failed required upstream skips dependent with attempts=0; optional field defaults; independent branch runs',
    );
  });
});
