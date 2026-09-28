import { describe, expect, it } from "vitest";
import { Kernel } from "../../src/kernel/kernel.js";
import { mapSqliteError, SqliteResourceStore } from "../../src/state/sqliteStore.js";
import { StorageError } from "../../src/state/store.js";

describe("SqliteResourceStore diagnostics queries", () => {
  it("filters decisions by run/client/resource and records version history", () => {
    const store = new SqliteResourceStore({ path: ":memory:", busyTimeoutMs: 2000 });
    const kernel = new Kernel({ store, etagStrength: "strong", clock: () => 1_700_000_000_000 });

    kernel.put({
      resourceId: "q/a",
      conditions: {},
      correlation: { runId: "r1", clientId: "c1", requestId: "1" },
      body: "alpha",
    });
    kernel.put({
      resourceId: "q/b",
      conditions: {},
      correlation: { runId: "r2", clientId: "c2", requestId: "2" },
      body: "beta",
    });
    kernel.put({
      resourceId: "q/a",
      conditions: {},
      correlation: { runId: "r1", clientId: "c2", requestId: "3" },
      body: "gamma",
    });

    expect(store.listDecisions({}).length).toBe(3);
    expect(store.listDecisions({ runId: "r1" }).map((d) => d.requestId)).toEqual(["1", "3"]);
    expect(store.listDecisions({ clientId: "c2" }).map((d) => d.requestId)).toEqual(["2", "3"]);
    expect(store.listDecisions({ resourceId: "q/a" }).map((d) => d.requestId)).toEqual(["1", "3"]);
    expect(
      store.listDecisions({ runId: "r1", clientId: "c1", resourceId: "q/a" }).map((d) => d.requestId),
    ).toEqual(["1"]);

    const history = store.listHistory("q/a");
    expect(history.map((h) => h.version)).toEqual([1, 2]);
    store.close();
  });

  it("maps SQLITE_BUSY to StorageError(write-lock-busy) and leaves other errors untouched", () => {
    const busy = mapSqliteError({ code: "SQLITE_BUSY" }) as StorageError;
    expect(busy).toBeInstanceOf(StorageError);
    expect(busy.code).toBe("write-lock-busy");

    const generic = new Error("boom");
    expect(mapSqliteError(generic)).toBe(generic);
    expect(mapSqliteError(null)).toBeNull();
    expect(mapSqliteError({ code: "SQLITE_CONSTRAINT" })).toEqual({ code: "SQLITE_CONSTRAINT" });
  });
});
