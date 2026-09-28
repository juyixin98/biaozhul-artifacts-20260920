/**
 * Test harness: fresh in-memory SQLite store + kernel per test, with a
 * controllable clock and correlation id factory.
 */

import { Kernel } from "../../src/kernel/kernel.js";
import { SqliteResourceStore } from "../../src/state/sqliteStore.js";
import type { ConditionInput, CorrelationIds } from "../../src/contract/model.js";

let seq = 0;

export interface Harness {
  store: SqliteResourceStore;
  kernel: Kernel;
  runId: string;
  nextCorrelation(clientId: string): CorrelationIds;
  close(): void;
}

export function makeHarness(opts?: { runId?: string; etagStrength?: "strong" | "weak"; clock?: () => number }): Harness {
  const runId = opts?.runId ?? `test-run-${process.pid}-${++seq}`;
  const store = new SqliteResourceStore({ path: ":memory:", busyTimeoutMs: 2000 });
  const kernel = new Kernel({
    store,
    etagStrength: opts?.etagStrength ?? "strong",
    clock: opts?.clock ?? (() => Date.now()),
  });
  let counter = 0;
  return {
    store,
    kernel,
    runId,
    nextCorrelation(clientId: string): CorrelationIds {
      counter += 1;
      return { runId, clientId, requestId: `${clientId}-req-${counter}` };
    },
    close() {
      store.close();
    },
  };
}

export function conditions(over: Partial<ConditionInput> = {}): ConditionInput {
  return { ...over };
}
