import { mkdirSync, appendFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { BoundedSemaphore } from '../src/kernel/semaphore.js';
import { CompositeKernel } from '../src/kernel/executor.js';
import type { EmittableEvent, EventSink, KernelEvent } from '../src/kernel/events.js';
import { RunStore } from '../src/state/runStore.js';
import { buildScenario, HAPPY_INPUT, snapshotToken, type Scenario } from '../src/fixtures/scenario.js';
import type { CompositeContract, CompositeResponse } from '../src/types.js';

/**
 * Test harness. Every run gets:
 *  - a fixed, human-readable runId (so a failing run can be replayed)
 *  - a JSONL event trace at test-results/traces/<runId>.jsonl
 *  - a verdict line in test-results/verdict.log with the assertion reason
 *
 * Expected values are declared as literals next to each test — they come from
 * the fixture table definitions, never from the kernel under test.
 */
export const RESULT_DIR = join(process.cwd(), 'test-results');
export const TRACE_DIR = join(RESULT_DIR, 'traces');

mkdirSync(TRACE_DIR, { recursive: true });
writeFileSync(join(RESULT_DIR, 'verdict.log'), '');

export class FileEventSink implements EventSink {
  private buffer: KernelEvent[] = [];

  constructor(private readonly runId: string) {}

  record(event: KernelEvent): void {
    this.buffer.push(event);
    appendFileSync(
      join(TRACE_DIR, `${this.runId}.jsonl`),
      JSON.stringify(event) + '\n',
    );
  }

  get events(): KernelEvent[] {
    return this.buffer;
  }

  types(): string[] {
    return this.buffer.map((e) => e.type);
  }

  eventsFor(nodeId: string): KernelEvent[] {
    return this.buffer.filter(
      (e) => 'nodeId' in e && (e as { nodeId?: string }).nodeId === nodeId,
    );
  }
}

export interface Harness {
  scenario: Scenario;
  store: RunStore;
  sink: FileEventSink;
  run: (opts?: {
    runId?: string;
    timeoutMs?: number;
    snapshotToken?: string | null;
    params?: Record<string, unknown>;
    maxConcurrent?: number;
    maxQueue?: number;
  }) => Promise<CompositeResponse>;
  contract: CompositeContract;
}

export function createHarness(
  scenarioOverrides: Parameters<typeof buildScenario>[0] = {},
  runId = 'test-run',
): Harness {
  const scenario = buildScenario(scenarioOverrides);
  const store = new RunStore(':memory:');
  const sink = new FileEventSink(runId);
  const kernel = new CompositeKernel({
    sources: scenario.sources,
    semaphore: new BoundedSemaphore(4, 16),
    store,
    sink,
  });

  return {
    scenario,
    store,
    sink,
    contract: scenario.contract,
    run: (opts = {}) =>
      kernel.run(scenario.contract, {
        runId: opts.runId ?? runId,
        timeoutMs: opts.timeoutMs ?? 1000,
        snapshotToken:
          opts.snapshotToken === undefined ? snapshotToken('v1') : opts.snapshotToken,
        params: opts.params ?? HAPPY_INPUT,
      }),
  };
}

/**
 * Kernel with caller-controlled concurrency limits (for resource-exhaustion).
 */
export async function runWithLimits(
  limits: { maxConcurrent: number; maxQueue: number },
  scenarioOverrides: Parameters<typeof buildScenario>[0] = {},
  runId = 'test-limits',
): Promise<{ response: CompositeResponse; store: RunStore; sink: FileEventSink }> {
  const scenario = buildScenario(scenarioOverrides);
  const store = new RunStore(':memory:');
  const sink = new FileEventSink(runId);
  const kernel = new CompositeKernel({
    sources: scenario.sources,
    semaphore: new BoundedSemaphore(limits.maxConcurrent, limits.maxQueue),
    store,
    sink,
  });
  const response = await kernel.run(scenario.contract, {
    runId,
    timeoutMs: 1000,
    snapshotToken: snapshotToken('v1'),
    params: HAPPY_INPUT,
  });
  return { response, store, sink };
}

/** Append a human-readable verdict so a CI log explains each check. */
export function recordVerdict(runId: string, verdict: string, reason: string): void {
  appendFileSync(
    join(RESULT_DIR, 'verdict.log'),
    `${new Date().toISOString()} run=${runId} verdict=${verdict} reason="${reason}"\n`,
  );
}

export function nodeById(
  response: CompositeResponse,
  nodeId: string,
): CompositeResponse['nodeResults'][number] {
  const node = response.nodeResults.find((n) => n.nodeId === nodeId);
  if (!node) throw new Error(`test setup: no node result for ${nodeId}`);
  return node;
}

export function fieldByPath(
  response: CompositeResponse,
  path: string,
): CompositeResponse['fields'][number] {
  const field = response.fields.find((f) => f.path === path);
  if (!field) throw new Error(`test setup: no field result for ${path}`);
  return field;
}

export type { EmittableEvent };
