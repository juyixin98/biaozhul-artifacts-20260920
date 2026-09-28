/**
 * Worker-side actor for concurrency scenarios.
 *
 * Each worker opens ITS OWN connection to the same on-disk SQLite database,
 * builds a real kernel on top of it, waits at a message gate, and then fires
 * its conditional write. Two workers released together genuinely contend on
 * the SQLite write lock (WAL + busy_timeout) — this is not simulated
 * interleaving.
 */

import { parentPort, workerData } from "node:worker_threads";
import { Kernel } from "../../src/kernel/kernel.js";
import { SqliteResourceStore } from "../../src/state/sqliteStore.js";
import type { ConditionInput, CorrelationIds } from "../../src/contract/model.js";

interface WorkerArgs {
  dbPath: string;
  mode: "once" | "loop" | "wildcard";
  runId: string;
  clientId: string;
  resourceId: string;
  body: string;
  startingEtag: string | null;
  busyTimeoutMs: number;
}

const args = workerData as WorkerArgs;
const port = parentPort!;

const store = new SqliteResourceStore({ path: args.dbPath, busyTimeoutMs: args.busyTimeoutMs });
const kernel = new Kernel({ store, etagStrength: "strong", clock: () => Date.now() });

function corr(n: number): CorrelationIds {
  return { runId: args.runId, clientId: args.clientId, requestId: `${args.clientId}-attempt-${n}` };
}

function attemptOnce(conditions: ConditionInput, attempt: number) {
  return kernel.put({
    resourceId: args.resourceId,
    conditions,
    correlation: corr(attempt),
    body: args.body,
    contentType: "text/plain",
  });
}

port.on("message", (msg: { type: string }) => {
  if (msg.type !== "go") return;
  try {
    if (args.mode === "wildcard") {
      const out = attemptOnce({ ifNoneMatch: "*" }, 1);
      finish({
        statuses: [out.status],
        categories: [out.record.failureCategory],
        finalStatus: out.status,
        finalVersion: out.record.resultingVersion ?? 0,
        body: args.body,
        committed: out.status === 201,
      });
      return;
    }

    // Pinned to the validator each client observed before the race.
    let etag = args.startingEtag;
    const statuses: number[] = [];
    const categories: (string | null)[] = [];
    let attempt = 0;
    let finalStatus = 0;
    let finalVersion = 0;
    for (;;) {
      attempt += 1;
      const conds: ConditionInput =
        args.mode === "once" || attempt === 1 ? { ifMatch: etag ?? "*" } : { ifMatch: etag ?? "*" };
      const out = attemptOnce(conds, attempt);
      statuses.push(out.status);
      categories.push(out.record.failureCategory);
      finalStatus = out.status;
      finalVersion = out.record.resultingVersion ?? finalVersion;
      if (out.status < 400) {
        finish({ statuses, categories, finalStatus, finalVersion, body: args.body, committed: true });
        return;
      }
      if (args.mode === "once") {
        finish({ statuses, categories, finalStatus, finalVersion, body: args.body, committed: false });
        return;
      }
      // Optimistic retry: re-read the current validator and try again.
      const fresh = kernel.get({
        resourceId: args.resourceId,
        conditions: {},
        correlation: corr(attempt),
      });
      if (fresh.kind !== "present") {
        finish({ statuses, categories, finalStatus: fresh.status, finalVersion, body: args.body, committed: false });
        return;
      }
      etag = fresh.snapshot.canonicalEtag;
      if (attempt > 10) {
        finish({ statuses, categories, finalStatus: 500, finalVersion, body: args.body, committed: false });
        return;
      }
    }
  } catch (err) {
    port.postMessage({ type: "result", error: err instanceof Error ? err.message : String(err) });
    store.close();
  }
});

function finish(payload: Record<string, unknown>): void {
  port.postMessage({ type: "result", clientId: args.clientId, ...payload });
  store.close();
}

port.postMessage({ type: "ready", clientId: args.clientId });
