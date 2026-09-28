import { describe, expect, it } from "vitest";

import { parseRpcPayload } from "../../src/protocol/parser.js";
import { buildMethodRegistry } from "../../src/kernel/methods.js";
import { makeHarness } from "../support/harness.js";

async function invoke(
  kernel: ReturnType<typeof makeHarness>["kernel"],
  raw: string,
  signal?: AbortSignal,
) {
  return kernel.handle(parseRpcPayload(raw), {
    signal,
    sizeBytes: Buffer.byteLength(raw),
  });
}

describe("kernel — single messages", () => {
  it("returns a concrete success result for a single request", async () => {
    const { kernel } = makeHarness();
    const result = await invoke(
      kernel,
      JSON.stringify({ jsonrpc: "2.0", method: "math.add", params: { a: 2, b: 40 }, id: 1 }),
    );
    expect(result.kind).toBe("single");
    if (result.kind !== "single") throw new Error("narrow");
    expect(result.httpStatus).toBe(200);
    expect(result.response).toEqual({
      jsonrpc: "2.0",
      result: { sum: 42, addends: [2, 40] },
      id: 1,
    });
  });

  it("returns 204 with no body for a single notification", async () => {
    const { kernel } = makeHarness();
    const result = await invoke(
      kernel,
      JSON.stringify({ jsonrpc: "2.0", method: "math.add", params: { a: 1, b: 1 } }),
    );
    expect(result).toEqual({ kind: "notificationOnly", httpStatus: 204 });
  });

  it("records a notification failure in the ledger but never emits a response", async () => {
    const { kernel, store } = makeHarness();
    const result = await invoke(
      kernel,
      JSON.stringify({
        jsonrpc: "2.0",
        method: "accounts.transfer",
        params: { from: "acc-1", to: "acc-2", amount: 999_999_999 },
      }),
    );
    expect(result.kind).toBe("notificationOnly");

    const requestRow = store.listRecentRequests(1)[0]!;
    const calls = store.listCallsByRequest(requestRow.requestCorr);
    expect(calls).toHaveLength(1);
    expect(calls[0]!.isNotification).toBe(true);
    expect(calls[0]!.status).toBe("error");
    expect(calls[0]!.errorCategory).toBe("business_rule");
    expect(calls[0]!.errorCode).toBe(-32001);
    // No response body means there is nowhere an id could be attached.
    expect(calls[0]!.rpcIdJson).toBeNull();
  });
});

describe("kernel — batches, notifications and attribution", () => {
  it("answers a mixed batch while omitting notifications and keeping request order", async () => {
    const { kernel } = makeHarness();
    const batch = [
      { jsonrpc: "2.0", method: "math.add", params: { a: 1, b: 1 }, id: "a" },
      { jsonrpc: "2.0", method: "accounts.transfer", params: { from: "acc-1", to: "acc-2", amount: 100 } }, // notification
      { jsonrpc: "2.0", method: "echo", params: { echoed: "third" }, id: "c" },
      "not-an-object", // invalid element
    ];
    const result = await invoke(kernel, JSON.stringify(batch));
    expect(result.kind).toBe("batch");
    if (result.kind !== "batch") throw new Error("narrow");
    expect(result.responses).toHaveLength(3);
    expect(result.responses.map((r) => ("id" in r ? r.id : null))).toEqual([
      "a",
      "c",
      null, // invalid element always reports id null
    ]);
    const invalid = result.responses[2]!;
    expect("error" in invalid && invalid.error.code).toBe(-32600);
  });

  it("returns responses in REQUEST order even though execution completes out of order", async () => {
    const { kernel, store } = makeHarness();
    const registry = buildMethodRegistry();
    // Test-side observer, independent of the code under test: record the
    // actual wall-clock completion order of method execution.
    const completionOrder: string[] = [];
    for (const name of ["debug.sleep", "math.add"]) {
      const def = registry.get(name)!;
      registry.set(name, {
        ...def,
        execute: async (params, ctx) => {
          const value = await def.execute(params, ctx);
          completionOrder.push(name);
          return value;
        },
      });
    }
    // Rebuild a kernel with the instrumented registry on the SAME store.
    const { Kernel } = await import("../../src/kernel/engine.js");
    const instrumentedKernel = new Kernel({ store, registry });

    const batch = [
      { jsonrpc: "2.0", method: "debug.sleep", params: { delayMs: 60 }, id: "slow" },
      { jsonrpc: "2.0", method: "math.add", params: { a: 10, b: 5 }, id: "fast" },
    ];
    const result = await instrumentedKernel.handle(parseRpcPayload(JSON.stringify(batch)), {
      sizeBytes: 0,
    });

    // The fast call provably finished before the slow one...
    expect(completionOrder).toEqual(["math.add", "debug.sleep"]);

    expect(result.kind).toBe("batch");
    if (result.kind !== "batch") throw new Error("narrow");
    // ...yet the slow response is still first in the returned array.
    expect(result.responses).toHaveLength(2);
    expect(result.responses[0]!).toMatchObject({ id: "slow" });
    expect("result" in result.responses[0]! && result.responses[0]!.result).toEqual({
      slept: 60,
    });
    expect(result.responses[1]!).toMatchObject({ id: "fast" });
    expect("result" in result.responses[1]! && result.responses[1]!.result).toEqual({
      sum: 15,
      addends: [10, 5],
    });
  });

  it("handles duplicate RPC ids without cross-talk: each position keeps its own result", async () => {
    const { kernel, store } = makeHarness();
    const batch = [
      { jsonrpc: "2.0", method: "math.add", params: { a: 1, b: 2 }, id: "dup" },
      { jsonrpc: "2.0", method: "math.add", params: { a: 100, b: 200 }, id: "dup" },
      { jsonrpc: "2.0", method: "echo", params: { echoed: 3 }, id: "dup" },
    ];
    const result = await invoke(kernel, JSON.stringify(batch));
    expect(result.kind).toBe("batch");
    if (result.kind !== "batch") throw new Error("narrow");
    expect(result.responses).toHaveLength(3);
    expect(result.responses.map((r) => r.id)).toEqual(["dup", "dup", "dup"]);
    expect(result.responses[0]!).toMatchObject({ result: { sum: 3 } });
    expect(result.responses[1]!).toMatchObject({ result: { sum: 300 } });
    expect(result.responses[2]!).toMatchObject({
      result: { echoed: { echoed: 3 } },
    });

    // Ledger proof: three distinct call rows share the encoded id.
    const requestRow = store.listRecentRequests(1)[0]!;
    const calls = store.listCallsByRequest(requestRow.requestCorr);
    expect(calls).toHaveLength(3);
    expect(new Set(calls.map((c) => c.callCorr)).size).toBe(3);
    expect(calls.every((c) => c.rpcIdJson === JSON.stringify("dup"))).toBe(true);
    expect(calls.map((c) => c.position)).toEqual([0, 1, 2]);
  });

  it("numeric id 1, string id \"1\" and null id never collapse", async () => {
    const { kernel } = makeHarness();
    const batch = [
      { jsonrpc: "2.0", method: "echo", params: { echoed: "num" }, id: 1 },
      { jsonrpc: "2.0", method: "echo", params: { echoed: "str" }, id: "1" },
      { jsonrpc: "2.0", method: "echo", params: { echoed: "nul" }, id: null },
    ];
    const result = await invoke(kernel, JSON.stringify(batch));
    if (result.kind !== "batch") throw new Error("narrow");
    expect(result.responses.map((r) => r.id)).toEqual([1, "1", null]);
  });
});

describe("kernel — error layering", () => {
  it("empty batch is rejected at contract layer with 400", async () => {
    const { kernel, store } = makeHarness();
    const result = await invoke(kernel, "[]");
    expect(result.kind).toBe("topLevelError");
    if (result.kind !== "topLevelError") throw new Error("narrow");
    expect(result.httpStatus).toBe(400);
    expect(result.response.error.code).toBe(-32600);
    expect(result.response.error.data?.category).toBe("invalid_request");

    const row = store.listRecentRequests(1)[0]!;
    expect(row.verdict).toBe("rejected");
    expect(row.layer).toBe("contract");
    expect(row.failureCategory).toBe("invalid_request");
  });

  it("malformed JSON is rejected at parse layer with 500", async () => {
    const { kernel, store } = makeHarness();
    const result = await invoke(kernel, "{");
    expect(result.kind).toBe("topLevelError");
    if (result.kind !== "topLevelError") throw new Error("narrow");
    expect(result.httpStatus).toBe(500);
    expect(result.response.error.code).toBe(-32700);
    expect(result.response.error.data?.category).toBe("parse_error");

    const row = store.listRecentRequests(1)[0]!;
    expect(row.layer).toBe("parse");
    expect(row.failureCategory).toBe("parse_error");
  });

  it("separates invalid_params (contract) from business_rule failures", async () => {
    const { kernel } = makeHarness();

    const fractional = await invoke(
      kernel,
      JSON.stringify({
        jsonrpc: "2.0",
        method: "accounts.transfer",
        params: { from: "acc-1", to: "acc-2", amount: 12.5 },
        id: 1,
      }),
    );
    if (fractional.kind !== "single") throw new Error("narrow");
    expect(fractional.response).toMatchObject({
      error: { code: -32602, data: { category: "invalid_params" } },
    });

    const unknown = await invoke(
      kernel,
      JSON.stringify({
        jsonrpc: "2.0",
        method: "accounts.balance",
        params: { accountId: "acc-nope" },
        id: 2,
      }),
    );
    if (unknown.kind !== "single") throw new Error("narrow");
    expect(unknown.response).toMatchObject({
      error: { code: -32002, data: { category: "business_rule" } },
    });

    const funds = await invoke(
      kernel,
      JSON.stringify({
        jsonrpc: "2.0",
        method: "accounts.transfer",
        params: { from: "acc-3", to: "acc-2", amount: 1 },
        id: 3,
      }),
    );
    if (funds.kind !== "single") throw new Error("narrow");
    expect(funds.response).toMatchObject({
      error: { code: -32001, data: { category: "business_rule" } },
    });
    expect(
      "error" in funds.response &&
        funds.response.error.data &&
        (funds.response.error.data as { detail?: { balance?: number } }).detail
          ?.balance,
    ).toBe(0);
  });

  it("reports unknown methods with -32601", async () => {
    const { kernel } = makeHarness();
    const result = await invoke(
      kernel,
      JSON.stringify({ jsonrpc: "2.0", method: "does.not.exist", id: 9 }),
    );
    if (result.kind !== "single") throw new Error("narrow");
    expect(result.response).toMatchObject({
      error: {
        code: -32601,
        data: { category: "method_not_found" },
      },
      id: 9,
    });
  });
});
