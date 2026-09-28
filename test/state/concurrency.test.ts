/**
 * Real concurrency tests using worker_threads against one on-disk SQLite
 * database. Two clients hold the same pre-race validator and are released
 * simultaneously; the SQLite write lock plus in-transaction re-evaluation
 * must prevent lost updates.
 */

import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Worker } from "node:worker_threads";
import { afterEach, describe, expect, it } from "vitest";
import { Kernel } from "../../src/kernel/kernel.js";
import { SqliteResourceStore } from "../../src/state/sqliteStore.js";
import { BODIES } from "../fixtures.ts";
import { logAssertion, logOutcome, scenarioHeader } from "../helpers/testLog.ts";

const WORKER_URL = new URL("../helpers/concurrencyWorker.ts", import.meta.url);

interface WorkerResult {
  type: "result";
  clientId: string;
  statuses: number[];
  categories: (string | null)[];
  finalStatus: number;
  finalVersion: number;
  body: string;
  committed: boolean;
  error?: string;
}

type WorkerMessage = { type: "ready"; clientId: string } | WorkerResult;

function runWorker(dbPath: string, opts: {
  runId: string;
  clientId: string;
  resourceId: string;
  body: string;
  mode: "once" | "loop" | "wildcard";
  startingEtag: string | null;
  busyTimeoutMs?: number;
}): { worker: Worker; ready: Promise<void>; result: Promise<WorkerResult> } {
  const worker = new Worker(WORKER_URL, {
    workerData: { dbPath, busyTimeoutMs: 5000, ...opts },
    // Runtime-spawned workers need the tsx loader to execute TypeScript and
    // resolve the project's .js specifiers to their .ts sources.
    execArgv: ["--import", "tsx"],
  });
  let onReady: () => void;
  const ready = new Promise<void>((resolve) => {
    onReady = resolve;
  });
  const result = new Promise<WorkerResult>((resolve, reject) => {
    worker.on("message", (msg: WorkerMessage) => {
      if (msg.type === "ready") {
        onReady!();
        return;
      }
      resolve(msg);
    });
    worker.on("error", reject);
    worker.on("exit", (code) => {
      if (code !== 0) reject(new Error(`worker exited with code ${code}`));
    });
  });
  return { worker, ready, result };
}

function tempDbPath(): string {
  const dir = mkdtempSync(join(tmpdir(), "rvapi-concurrency-"));
  return join(dir, "app.db");
}

describe("concurrent conditional writers", () => {
  let stores: SqliteResourceStore[] = [];
  afterEach(() => {
    for (const s of stores) s.close();
    stores = [];
  });

  it("two clients racing the same validator: exactly one wins, loser gets 412, no lost update", async () => {
    scenarioHeader("concurrency-once", "two writers, one pinned If-Match each, single attempt");
    const dbPath = tempDbPath();

    // Seed v1 from a third connection.
    const seedStore = new SqliteResourceStore({ path: dbPath, busyTimeoutMs: 5000 });
    stores.push(seedStore);
    const seedKernel = new Kernel({ store: seedStore, etagStrength: "strong", clock: () => Date.now() });
    const seeded = seedKernel.put({
      resourceId: "concurrent/ledger",
      conditions: {},
      correlation: { runId: "concurrency-once", clientId: "seeder", requestId: "seed" },
      body: BODIES.concurrentA.body,
      contentType: "text/plain",
    });
    if (seeded.kind !== "present") throw new Error("seed failed");
    const v1Etag = seeded.snapshot.canonicalEtag;

    const a = runWorker(dbPath, {
      runId: "concurrency-once",
      clientId: "client-A",
      resourceId: "concurrent/ledger",
      body: BODIES.concurrentB.body,
      mode: "once",
      startingEtag: v1Etag,
    });
    const b = runWorker(dbPath, {
      runId: "concurrency-once",
      clientId: "client-B",
      resourceId: "concurrent/ledger",
      body: BODIES.concurrentC.body,
      mode: "once",
      startingEtag: v1Etag,
    });
    await Promise.all([a.ready, b.ready]);

    // Release both at the same instant.
    a.worker.postMessage({ type: "go" });
    b.worker.postMessage({ type: "go" });

    const [ra, rb] = await Promise.all([a.result, b.result]);
    expect(ra.error).toBeUndefined();
    expect(rb.error).toBeUndefined();
    for (const r of [ra, rb]) {
      logOutcome(
        { runId: "concurrency-once", clientId: r.clientId, requestId: "race" },
        {
          status: r.finalStatus,
          verdict: r.finalStatus === 200 ? "proceed" : "precondition-failed",
          category: r.categories.find((c) => c !== null) ?? null,
          resultingVersion: r.finalVersion,
          note: `attempts=${JSON.stringify(r.statuses)}`,
        },
      );
    }

    const winners = [ra, rb].filter((r) => r.finalStatus === 200);
    const losers = [ra, rb].filter((r) => r.finalStatus === 412);
    expect(winners).toHaveLength(1);
    expect(losers).toHaveLength(1);
    expect(losers[0]!.categories).toContain("if-match-mismatch");

    // The surviving state must be exactly one of the two bodies at version 2,
    // and the other body must be absent (not silently merged/overwritten).
    const verify = new SqliteResourceStore({ path: dbPath, busyTimeoutMs: 5000 });
    stores.push(verify);
    const after = verify.readCommitted((tx) => tx.getCurrent("concurrent/ledger"));
    expect(after).not.toBeNull();
    expect(after!.version).toBe(2);
    const winningBody = winners[0]!.body;
    expect(after!.body).toBe(winningBody);
    expect([BODIES.concurrentB.body, BODIES.concurrentC.body]).toContain(after!.body);
    logAssertion(
      "concurrency-once",
      after!.version === 2 && losers.length === 1,
      `winner=${winners[0]!.clientId} body="${winningBody}", loser 412 if-match-mismatch; final v2 only`,
    );

    // The decision trail records both attempts in commit order.
    const decisions = verify.listDecisions({ runId: "concurrency-once" }).filter((d) => d.method === "PUT");
    expect(decisions).toHaveLength(3); // seed + 2 racing attempts
    const statuses = decisions.map((d) => d.outcomeStatus).sort();
    expect(statuses).toEqual([200, 201, 412]);
    await Promise.all([a.worker.terminate(), b.worker.terminate()]);
  }, 30_000);

  it("loser that retries with a fresh validator commits its own update (both updates survive, v2 and v3)", async () => {
    scenarioHeader("concurrency-loop", "optimistic retries serialize and preserve both writes");
    const dbPath = tempDbPath();

    const seedStore = new SqliteResourceStore({ path: dbPath, busyTimeoutMs: 5000 });
    stores.push(seedStore);
    const seedKernel = new Kernel({ store: seedStore, etagStrength: "strong", clock: () => Date.now() });
    const seeded = seedKernel.put({
      resourceId: "concurrent/ledger2",
      conditions: {},
      correlation: { runId: "concurrency-loop", clientId: "seeder", requestId: "seed" },
      body: BODIES.concurrentA.body,
      contentType: "text/plain",
    });
    if (seeded.kind !== "present") throw new Error("seed failed");

    const a = runWorker(dbPath, {
      runId: "concurrency-loop",
      clientId: "client-A",
      resourceId: "concurrent/ledger2",
      body: BODIES.concurrentB.body,
      mode: "loop",
      startingEtag: seeded.snapshot.canonicalEtag,
    });
    const d = runWorker(dbPath, {
      runId: "concurrency-loop",
      clientId: "client-D",
      resourceId: "concurrent/ledger2",
      body: BODIES.concurrentD.body,
      mode: "loop",
      startingEtag: seeded.snapshot.canonicalEtag,
    });
    await Promise.all([a.ready, d.ready]);
    a.worker.postMessage({ type: "go" });
    d.worker.postMessage({ type: "go" });

    const [ra, rd] = await Promise.all([a.result, d.result]);
    expect(ra.committed).toBe(true);
    expect(rd.committed).toBe(true);
    expect(ra.finalStatus === 200 || rd.finalStatus === 200).toBe(true);
    for (const r of [ra, rd]) {
      logOutcome(
        { runId: "concurrency-loop", clientId: r.clientId, requestId: "race" },
        {
          status: r.finalStatus,
          verdict: "proceed",
          category: r.categories.find((c) => c !== null) ?? null,
          resultingVersion: r.finalVersion,
          note: `attempts=${JSON.stringify(r.statuses)}`,
        },
      );
    }

    const verify = new SqliteResourceStore({ path: dbPath, busyTimeoutMs: 5000 });
    stores.push(verify);
    const history = verify.listHistory("concurrent/ledger2");
    // v1 seed plus exactly one version per client.
    expect(history.map((x) => x.version)).toEqual([1, 2, 3]);
    const bodiesInOrder = history.map((x) => x.etag);
    expect(new Set(bodiesInOrder).size).toBe(3); // all etags distinct

    const finalRow = verify.readCommitted((tx) => tx.getCurrent("concurrent/ledger2"));
    expect(finalRow!.version).toBe(3);
    expect([BODIES.concurrentB.body, BODIES.concurrentD.body]).toContain(finalRow!.body);
    logAssertion(
      "concurrency-loop",
      history.length === 3,
      `both racing writes survived; final body="${finalRow!.body}" at v3`,
    );
    await Promise.all([a.worker.terminate(), d.worker.terminate()]);
  }, 30_000);

  it("concurrent If-None-Match: * creates: one 201, one 412 (no duplicate create)", async () => {
    scenarioHeader("concurrency-wildcard", "two simultaneous creates with INM *");
    const dbPath = tempDbPath();

    const a = runWorker(dbPath, {
      runId: "concurrency-wildcard",
      clientId: "creator-A",
      resourceId: "wildcard/resource",
      body: BODIES.beta.body,
      mode: "wildcard",
      startingEtag: null,
    });
    const b = runWorker(dbPath, {
      runId: "concurrency-wildcard",
      clientId: "creator-B",
      resourceId: "wildcard/resource",
      body: BODIES.gamma.body,
      mode: "wildcard",
      startingEtag: null,
    });
    await Promise.all([a.ready, b.ready]);
    a.worker.postMessage({ type: "go" });
    b.worker.postMessage({ type: "go" });

    const [ra, rb] = await Promise.all([a.result, b.result]);
    const created = [ra, rb].filter((r) => r.finalStatus === 201);
    const blocked = [ra, rb].filter((r) => r.finalStatus === 412);
    expect(created).toHaveLength(1);
    expect(blocked).toHaveLength(1);
    expect(blocked[0]!.categories).toContain("if-none-match-exists");
    for (const r of [ra, rb]) {
      logOutcome(
        { runId: "concurrency-wildcard", clientId: r.clientId, requestId: "race" },
        {
          status: r.finalStatus,
          verdict: r.finalStatus === 201 ? "proceed" : "precondition-failed",
          category: r.categories.find((c) => c !== null) ?? null,
          resultingVersion: r.finalVersion,
        },
      );
    }

    const verify = new SqliteResourceStore({ path: dbPath, busyTimeoutMs: 5000 });
    stores.push(verify);
    const row = verify.readCommitted((tx) => tx.getCurrent("wildcard/resource"));
    expect(row!.version).toBe(1);
    expect(row!.body).toBe(created[0]!.body);
    logAssertion(
      "concurrency-wildcard",
      row!.version === 1,
      `single create by ${created[0]!.clientId}; other creator received 412 if-none-match-exists`,
    );
    await Promise.all([a.worker.terminate(), b.worker.terminate()]);
  }, 30_000);
});
