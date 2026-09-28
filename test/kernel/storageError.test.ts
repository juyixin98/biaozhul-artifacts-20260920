import { describe, expect, it } from "vitest";
import { Kernel } from "../../src/kernel/kernel.js";
import {
  StorageError,
  type IdempotentOutcome,
  type NewVersionInput,
  type ResourceStore,
  type StoreTransaction,
  type StoredResource,
} from "../../src/state/store.js";
import type { DecisionRecord } from "../../src/contract/model.js";

/**
 * Minimal in-memory ResourceStore used to deterministically exercise kernel
 * branches that are impractical to trigger against real SQLite (notably the
 * SQLITE_BUSY -> 503 mapping). It implements the same port the kernel uses.
 */
class StubStore implements ResourceStore {
  readonly kind = "stub";
  current: StoredResource | null = null;
  decisions: DecisionRecord[] = [];
  throwOnImmediate: StorageError | null = null;

  run<T>(mode: "deferred" | "immediate", fn: (tx: StoreTransaction) => T): T {
    if (mode === "immediate" && this.throwOnImmediate) {
      // Throw only on the first attempt; the kernel's follow-up recording
      // transaction must then succeed.
      const err = this.throwOnImmediate;
      this.throwOnImmediate = null;
      throw err;
    }
    return fn(this.tx());
  }

  readCommitted<T>(fn: (tx: StoreTransaction) => T): T {
    return fn(this.tx());
  }

  listDecisions(filter: { runId?: string; clientId?: string; resourceId?: string }): DecisionRecord[] {
    return this.decisions.filter(
      (d) =>
        (!filter.runId || d.runId === filter.runId) &&
        (!filter.clientId || d.clientId === filter.clientId) &&
        (!filter.resourceId || d.resourceId === filter.resourceId),
    );
  }

  listHistory(): Array<{ version: number; etag: string; updatedAtMs: number }> {
    return [];
  }

  close(): void {}

  private tx(): StoreTransaction {
    const self = this;
    return {
      getCurrent: () => self.current,
      nextVersionFromHistory: () => (self.current ? self.current.version + 1 : 1),
      upsertCurrent(input: NewVersionInput) {
        self.current = {
          resourceId: input.resourceId,
          version: input.version,
          body: input.body,
          etag: input.etag,
          contentType: input.contentType,
          updatedAtMs: input.updatedAtMs,
        };
      },
      insertHistory() {},
      deleteCurrent() {
        self.current = null;
      },
      getIdempotentOutcome: () => null,
      putIdempotentOutcome(_key: string, _o: IdempotentOutcome) {},
      insertDecision(record: DecisionRecord) {
        self.decisions.push(record);
      },
    };
  }
}

describe("kernel storage-error mapping", () => {
  it("maps a write-lock-busy StorageError on PUT to a 503 with an explicit category (never a false success)", () => {
    const store = new StubStore();
    store.throwOnImmediate = new StorageError("Database write lock busy (SQLITE_BUSY)", "write-lock-busy");
    const kernel = new Kernel({ store, etagStrength: "strong", clock: () => 1_700_000_000_000 });
    const outcome = kernel.put({
      resourceId: "busy/doc",
      conditions: {},
      correlation: { runId: "busy-run", clientId: "c1", requestId: "r1" },
      body: "alpha",
    });
    expect(outcome.kind).toBe("busy");
    expect(outcome.status).toBe(503);
    if (outcome.kind === "busy") expect(outcome.category).toBe("write-lock-busy");
    // The diagnostic trail records the busy verdict rather than hiding it.
    expect(store.decisions).toHaveLength(1);
    expect(store.decisions[0]!.failureCategory).toBe("write-lock-busy");
    expect(store.decisions[0]!.outcomeStatus).toBe(503);
  });

  it("lists decisions by each filter dimension and unfiltered", () => {
    const store = new StubStore();
    store.throwOnImmediate = null;
    const kernel = new Kernel({ store, etagStrength: "strong", clock: () => 1_700_000_000_000 });
    kernel.put({
      resourceId: "f/a",
      conditions: {},
      correlation: { runId: "run-1", clientId: "cA", requestId: "1" },
      body: "alpha",
    });
    kernel.put({
      resourceId: "f/b",
      conditions: {},
      correlation: { runId: "run-2", clientId: "cB", requestId: "2" },
      body: "beta",
    });
    expect(store.listDecisions({}).length).toBe(2);
    expect(store.listDecisions({ runId: "run-1" }).length).toBe(1);
    expect(store.listDecisions({ clientId: "cB" })[0]!.resourceId).toBe("f/b");
    expect(store.listDecisions({ resourceId: "f/a" })[0]!.runId).toBe("run-1");
    expect(store.listDecisions({ runId: "run-1", clientId: "cB" }).length).toBe(0);
  });
});
