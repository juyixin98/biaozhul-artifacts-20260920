/**
 * Composite contracts exposed by the service.
 *
 * `orderSummary` is a diamond DAG:
 *
 *            profile (users)                product (catalog)
 *           /   |    \   \                  /   |   \   \
 *     contacts  inventory  pricing  recommendations   promotions
 *
 * pricing/inventory/recommendations each converge BOTH branches (they need the
 * user and the product), which is the diamond the test suite asserts on.
 *
 * Field requirement is explicit per binding: a REQUIRED field lost through a
 * failed node fails the run; an OPTIONAL field degrades (failed → reason, or
 * resolved-but-absent → missing) without failing the run.
 */
import type { CompositeContract } from '../contract/types.js';

export const orderSummaryContract: CompositeContract = {
  name: 'orderSummary',
  input: [
    { name: 'userId', required: true, type: 'string' },
    { name: 'sku', required: true, type: 'string' },
  ],
  calls: [
    {
      id: 'profile',
      source: 'users',
      method: 'getProfile',
      dependencies: [],
      buildRequest: (deps, input) => ({ userId: input.userId }),
      fields: [
        { output: 'customerName', requirement: 'required', path: 'displayName' },
        { output: 'loyaltyTier', requirement: 'required' },
        { output: 'region', requirement: 'required' },
      ],
    },
    {
      id: 'product',
      source: 'catalog',
      method: 'getProduct',
      dependencies: [],
      buildRequest: (_deps, input) => ({ sku: input.sku }),
      fields: [
        { output: 'sku', requirement: 'required' },
        { output: 'listPrice', requirement: 'required' },
        { output: 'currency', requirement: 'required' },
      ],
    },
    {
      id: 'contact',
      source: 'contacts',
      method: 'getContact',
      dependencies: ['profile'],
      buildRequest: (deps) => ({ userId: (deps.profile as { userId: string }).userId }),
      fields: [
        // Legacy source cannot pin snapshots; optional contact details degrade.
        { output: 'email', requirement: 'optional' },
        { output: 'phone', requirement: 'optional' },
      ],
    },
    {
      id: 'quote',
      source: 'pricing',
      method: 'getQuote',
      dependencies: ['profile', 'product'],
      buildRequest: (deps) => ({
        sku: (deps.product as { sku: string }).sku,
        loyaltyTier: (deps.profile as { loyaltyTier: string }).loyaltyTier,
      }),
      fields: [
        { output: 'finalPrice', requirement: 'required' },
        { output: 'discountPct', requirement: 'optional', defaultOnMissing: 0 },
      ],
    },
    {
      id: 'stock',
      source: 'inventory',
      method: 'getAvailability',
      dependencies: ['profile', 'product'],
      buildRequest: (deps) => ({
        region: (deps.profile as { region: string }).region,
        sku: (deps.product as { sku: string }).sku,
      }),
      fields: [
        { output: 'available', requirement: 'required' },
        { output: 'warehouseCount', requirement: 'optional' },
      ],
    },
    {
      id: 'upsell',
      source: 'recommendations',
      method: 'getRecommendation',
      dependencies: ['profile', 'product'],
      buildRequest: (deps) => ({
        userId: (deps.profile as { userId: string }).userId,
        sku: (deps.product as { sku: string }).sku,
      }),
      fields: [
        { output: 'recommendedSku', requirement: 'optional', path: 'recommendedSku' },
        { output: 'recommendationConfidence', requirement: 'optional', path: 'confidence' },
      ],
    },
    {
      id: 'promo',
      source: 'promotions',
      method: 'getPromotion',
      dependencies: ['product'],
      buildRequest: (deps) => ({ sku: (deps.product as { sku: string }).sku }),
      fields: [
        // Resolves to `missing` (null in fixture, no default) — optional absence.
        { output: 'promotionCode', requirement: 'optional' },
      ],
    },
  ],
};

export const contracts: Record<string, CompositeContract> = {
  orderSummary: orderSummaryContract,
};
