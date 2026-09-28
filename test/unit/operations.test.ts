import { describe, expect, it } from "vitest";

import { parseRpcPayload } from "../../src/protocol/parser.js";
import type { KernelTransportResult } from "../../src/kernel/types.js";
import { makeHarness } from "../support/harness.js";

type SingleResult = Extract<
  KernelTransportResult,
  { kind: "single" }
>["response"];

/** Narrow a single response to its success value or fail the test loudly. */
function resultOf(response: SingleResult): unknown {
  if (!("result" in response)) {
    throw new Error(
      `expected success response, got error: ${JSON.stringify(response.error)}`,
    );
  }
  return response.result;
}

async function invoke(
  kernel: ReturnType<typeof makeHarness>["kernel"],
  payload: unknown,
) {
  const raw = typeof payload === "string" ? payload : JSON.stringify(payload);
  return kernel.handle(parseRpcPayload(raw), { sizeBytes: Buffer.byteLength(raw) });
}

describe("side effects — independent operation numbers", () => {
  it("allocates a fresh, strictly increasing opSeq per invocation even with the same RPC id", async () => {
    const { kernel, store } = makeHarness();

    const first = await invoke(kernel, {
      jsonrpc: "2.0",
      method: "accounts.transfer",
      params: {
        from: "acc-1",
        to: "acc-2",
        amount: 100,
        idempotencyKey: "k-1",
      },
      id: 7,
    });
    const second = await invoke(kernel, {
      jsonrpc: "2.0",
      method: "accounts.transfer",
      params: { from: "acc-2", to: "acc-1", amount: 50 },
      id: 7, // same RPC id, no idempotency key -> a NEW operation
    });

    if (first.kind !== "single" || second.kind !== "single") {
      throw new Error("expected two single success responses");
    }
    const op1 = resultOf(first.response) as { operation: { opSeq: number } };
    const op2 = resultOf(second.response) as { operation: { opSeq: number } };
    expect(op1.operation.opSeq).toBe(1);
    expect(op2.operation.opSeq).toBe(2);
    expect(op2.operation.opSeq).not.toBe(op1.operation.opSeq);

    // First move: acc-1 -> acc-2 100, so acc-2 holds 50,100. Second move:
    // acc-2 -> acc-1 50. fromBalance describes the SOURCE (acc-2), 50,050;
    // the shared RPC id did not make the two operations one.
    expect(
      (resultOf(second.response) as { fromBalance: number }).fromBalance,
    ).toBe(50_050);
    expect(
      (resultOf(second.response) as { toBalance: number }).toBalance,
    ).toBe(99_950);
    expect(store.listOperations()).toHaveLength(2);
  });

  it("does not allocate an operation number when params are invalid", async () => {
    const { kernel, store } = makeHarness();
    const result = await invoke(kernel, {
      jsonrpc: "2.0",
      method: "accounts.transfer",
      params: { from: "acc-1", to: "acc-1", amount: 10 },
      id: 1,
    });
    if (result.kind !== "single") throw new Error("narrow");
    expect("error" in result.response).toBe(true);
    expect(store.listOperations()).toHaveLength(0);
  });
});

describe("idempotency — explicit key, never the RPC id", () => {
  it("replays the original result without a second side effect when the key repeats", async () => {
    const { kernel, store } = makeHarness();

    const payload = (id: number) => ({
      jsonrpc: "2.0",
      method: "accounts.transfer",
      params: {
        from: "acc-1",
        to: "acc-2",
        amount: 300,
        idempotencyKey: "transfer-AAA",
      },
      id,
    });

    const first = await invoke(kernel, payload(1));
    // Second call even comes from a DIFFERENT RPC id: replay must still win.
    const second = await invoke(kernel, payload(2));

    if (first.kind !== "single" || second.kind !== "single") {
      throw new Error("narrow");
    }
    const r1 = resultOf(first.response) as {
      fromBalance: number;
      operation: { opSeq: number; replayed: boolean };
    };
    const r2 = resultOf(second.response) as {
      fromBalance: number;
      operation: { opSeq: number; replayed: boolean };
    };
    expect(r1.operation.replayed).toBe(false);
    expect(r2.operation.replayed).toBe(true);
    expect(r2.operation.opSeq).toBe(r1.operation.opSeq);
    expect(r1.fromBalance).toBe(99_700);
    // Replay reports the ORIGINAL resulting balance, not the current one.
    expect(r2.fromBalance).toBe(99_700);

    // Money moved exactly once.
    const balances = await invoke(kernel, {
      jsonrpc: "2.0",
      method: "accounts.balance",
      params: { accountId: "acc-2" },
      id: 3,
    });
    if (balances.kind !== "single") throw new Error("narrow");
    expect(resultOf(balances.response)).toEqual({
      accountId: "acc-2",
      balance: 50_300,
    });

    // Only one side-effect row exists for the key.
    const ops = store.listOperations({ idempotencyKey: "transfer-AAA" });
    expect(ops).toHaveLength(1);
    expect(ops[0]!.status).toBe("succeeded");
  });

  it("allows retries after a FAILED attempt instead of replaying the failure", async () => {
    const { kernel, store } = makeHarness();

    const failed = await invoke(kernel, {
      jsonrpc: "2.0",
      method: "accounts.transfer",
      params: {
        from: "acc-3",
        to: "acc-1",
        amount: 10,
        idempotencyKey: "retry-key",
      },
      id: 1,
    });
    if (failed.kind !== "single") throw new Error("narrow");
    expect(failed.response).toMatchObject({
      error: { code: -32001 },
    });

    const retried = await invoke(kernel, {
      jsonrpc: "2.0",
      method: "accounts.transfer",
      params: {
        from: "acc-1",
        to: "acc-2",
        amount: 10,
        idempotencyKey: "retry-key",
      },
      id: 2,
    });
    if (retried.kind !== "single") throw new Error("narrow");
    expect("result" in retried.response).toBe(true);
    const result = resultOf(retried.response) as {
      operation: { replayed: boolean; opSeq: number };
    };
    expect(result.operation.replayed).toBe(false);
    expect(result.operation.opSeq).toBe(2);
    void store;
  });

  it("serializes two concurrent same-key requests: exactly one side effect", async () => {
    const { kernel, store } = makeHarness();
    const raw = JSON.stringify({
      jsonrpc: "2.0",
      method: "accounts.transfer",
      params: {
        from: "acc-1",
        to: "acc-2",
        amount: 70,
        idempotencyKey: "concurrent-key",
      },
      id: 1,
    });
    const [a, b] = await Promise.all([
      kernel.handle(parseRpcPayload(raw), { sizeBytes: 1 }),
      kernel.handle(parseRpcPayload(raw), { sizeBytes: 1 }),
    ]);
    for (const r of [a, b]) {
      expect(r.kind).toBe("single");
    }
    const flags = [a, b].map((r) => {
      if (r.kind !== "single") throw new Error("narrow");
      return (resultOf(r.response) as {
        operation: { replayed: boolean };
      }).operation.replayed;
    });
    expect([...flags].sort()).toEqual([false, true]);
    expect(store.listOperations({ idempotencyKey: "concurrent-key" })).toHaveLength(1);
  });
});

describe("async operations", () => {
  it("accepts immediately with pending opSeq and later reports terminal success", async () => {
    const { kernel, store } = makeHarness();
    const accepted = await invoke(kernel, {
      jsonrpc: "2.0",
      method: "tasks.schedule",
      params: { name: "job-A", delayMs: 30 },
      id: 1,
    });
    if (accepted.kind !== "single") throw new Error("narrow");
    expect(resultOf(accepted.response)).toEqual({
      accepted: true,
      opSeq: 1,
      status: "pending",
      poll: "operations.get",
    });

    // Immediately after acceptance the operation is queryable as pending.
    const pendingRow = store.getOperation(1)!;
    expect(pendingRow.status).toBe("pending");

    await kernel.awaitBackgroundSettled();

    const query = await invoke(kernel, {
      jsonrpc: "2.0",
      method: "operations.get",
      params: { opSeq: 1 },
      id: 2,
    });
    if (query.kind !== "single") throw new Error("narrow");
    const op = resultOf(query.response) as {
      status: string;
      kind: string;
      result: { task: string };
    };
    expect(op.status).toBe("succeeded");
    expect(op.kind).toBe("scheduled_task");
    expect(op.result.task).toBe("job-A");
  });

  it("records an async NOTIFICATION failure in the ledger even though no response exists", async () => {
    const { kernel, store } = makeHarness();
    const result = await invoke(kernel, {
      jsonrpc: "2.0",
      method: "tasks.schedule",
      params: { name: "job-fail", delayMs: 10, fail: true },
    });
    expect(result.kind).toBe("notificationOnly");

    await kernel.awaitBackgroundSettled();

    const ops = store.listOperations({ kind: "scheduled_task" });
    expect(ops).toHaveLength(1);
    expect(ops[0]!.status).toBe("failed");
    expect(ops[0]!.errorCategory).toBe("business_rule");
    expect(ops[0]!.errorCode).toBe(-32020);

    const requestRow = store.listRecentRequests(1)[0]!;
    const calls = store.listCallsByRequest(requestRow.requestCorr);
    expect(calls[0]!.isNotification).toBe(true);
    expect(calls[0]!.status).toBe("error");
  });

  it("returns not_found for an unknown operation sequence", async () => {
    const { kernel } = makeHarness();
    const result = await invoke(kernel, {
      jsonrpc: "2.0",
      method: "operations.get",
      params: { opSeq: 99999 },
      id: 1,
    });
    if (result.kind !== "single") throw new Error("narrow");
    expect(result.response).toMatchObject({
      error: { code: -32010, data: { category: "not_found" } },
    });
  });
});

describe("redaction", () => {
  it("never stores the raw secret value, only a redacted summary", async () => {
    const { kernel, store } = makeHarness();
    const secret = "super-secret-value-42";
    const result = await invoke(kernel, {
      jsonrpc: "2.0",
      method: "secrets.put",
      params: { name: "api-key-1", secret },
      id: 1,
    });
    if (result.kind !== "single") throw new Error("narrow");
    expect("result" in result.response).toBe(true);

    const rows = store.listOperations({ kind: "secret_store" });
    expect(rows).toHaveLength(1);
    expect(rows[0]!.redactedInput).toContain("***redacted***");
    expect(rows[0]!.redactedInput).not.toContain(secret);
    // The token returned to the caller is derived, not the plaintext.
    expect(rows[0]!.resultJson).not.toContain(secret);
  });
});
