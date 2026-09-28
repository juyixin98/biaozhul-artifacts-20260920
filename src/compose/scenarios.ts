/**
 * Named local scenarios. Each scenario is a fully synthetic configuration of
 * sources + FaultPlan — no external accounts, no randomness. The demo script
 * and the HTTP API select scenarios by name; tests build equivalent plans
 * directly to keep their expectations independent of this table.
 */
import { FaultPlan } from '../sources/fixtures/faults.js';
import { buildSources, type BuildSourcesOptions } from '../sources/index.js';
import type { SourceRegistry } from '../sources/registry.js';

export interface Scenario {
  name: string;
  description: string;
  buildRegistry: () => SourceRegistry;
}

function registryFactory(options: BuildSourcesOptions = {}): () => SourceRegistry {
  return () => buildSources(options);
}

export const scenarios: Record<string, Scenario> = {
  healthy: {
    name: 'healthy',
    description: 'Diamond dependency, every source answers; contacts is snapshot-incapable',
    buildRegistry: registryFactory({ baseLatencyMs: 4 }),
  },
  'required-failure': {
    name: 'required-failure',
    description: 'catalog.getProduct fails -> required fields lost, dependents skipped, run failed',
    buildRegistry: registryFactory({
      baseLatencyMs: 4,
      faultPlan: new FaultPlan().add('catalog', 'getProduct', {
        type: 'fail',
        reason: 'CATALOG_UNAVAILABLE',
        message: 'catalog fixture forced failure',
      }),
    }),
  },
  'optional-timeout': {
    name: 'optional-timeout',
    description:
      'promotions source would take 5s; the propagated deadline aborts it; the optional promotionCode field degrades',
    buildRegistry: registryFactory({
      baseLatencyMs: 4,
      faultPlan: new FaultPlan().add('promotions', 'getPromotion', {
        type: 'timeout',
        delayMs: 5000,
      }),
    }),
  },
  'version-mismatch': {
    name: 'version-mismatch',
    description: 'pricing serves a newer data version than the rest under one snapshot token',
    buildRegistry: registryFactory({
      baseLatencyMs: 4,
      pricingDataVersion: 'pricing-hot-2026-09-27',
    }),
  },
};

export const DEFAULT_SCENARIO = 'healthy';

export function getScenario(name: string | undefined): Scenario {
  const key = name ?? DEFAULT_SCENARIO;
  const scenario = scenarios[key];
  if (!scenario) {
    throw new Error(`unknown scenario: ${key}`);
  }
  return scenario;
}
