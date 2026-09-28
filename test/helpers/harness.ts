/**
 * Test harness — assembles REAL production components (kernel, registry,
 * contract, log) with independently supplied FaultPlans.
 *
 * Expected results in tests come from (a) the FaultPlan declarations and
 * (b) the raw fixture JSON — never from values the kernel itself produced.
 */
import { ExecutionKernel } from '../../src/kernel/executor.js';
import { RunLog } from '../../src/observability/runLog.js';
import { buildSources, type BuildSourcesOptions } from '../../src/sources/index.js';
import { orderSummaryContract } from '../../src/compose/contracts.js';
import type { CompositeResult } from '../../src/kernel/types.js';
import type { SourceRegistry } from '../../src/sources/registry.js';
import type { RunLog as RunLogType } from '../../src/observability/runLog.js';

export interface HarnessOptions extends Partial<BuildSourcesOptions> {
  timeoutMs?: number;
  snapshotToken?: string;
  runId?: string;
  input?: Record<string, string | number>;
}

export interface HarnessResult {
  result: CompositeResult;
  registry: SourceRegistry;
  log: RunLogType;
  elapsedMs: number;
  node: (id: string) => CompositeResult['nodes'][number];
  field: (name: string) => CompositeResult['fields'][number];
}

export async function runComposite(options: HarnessOptions = {}): Promise<HarnessResult> {
  const registry = buildSources(options);
  const runId = options.runId ?? 'run-test-0001';
  const log = new RunLog(runId, []);
  const kernel = new ExecutionKernel(registry);
  const started = Date.now();
  const result = await kernel.execute(orderSummaryContract, {
    input: options.input ?? { userId: 'u-1001', sku: 'sku-1' },
    timeoutMs: options.timeoutMs ?? 1000,
    snapshotToken: options.snapshotToken ?? 'snap-test-token',
    runId,
    log,
  });
  const elapsedMs = Date.now() - started;
  return {
    result,
    registry,
    log,
    elapsedMs,
    node: (id: string) => {
      const node = result.nodes.find((n) => n.callId === id);
      if (!node) throw new Error(`test harness: no node ${id}`);
      return node;
    },
    field: (name: string) => {
      const field = result.fields.find((f) => f.field === name);
      if (!field) throw new Error(`test harness: no field ${name}`);
      return field;
    },
  };
}

/** Independent expectation data: read the SAME fixture file the sources use. */
export function expectedFixtureValues(): {
  goldFinalPriceForSku1: number;
  userName: string;
  region: string;
  email: string;
} {
  // Computed here in the test layer (not by the kernel or the pricing source).
  const listPrice = 1299.0;
  const goldDiscount = 0.15;
  return {
    goldFinalPriceForSku1: Math.round(listPrice * (1 - goldDiscount) * 100) / 100,
    userName: 'Ada Lin',
    region: 'cn-east',
    email: 'ada.lin@example.test',
  };
}
