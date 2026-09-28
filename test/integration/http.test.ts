import { afterAll, beforeAll, describe, expect, it } from "vitest";
import net from "node:net";

import type { AppConfig } from "../../src/config.js";
import { CapturingLogger } from "../support/harness.js";
import { SqliteLedgerStore } from "../../src/state/store.js";
import { Kernel } from "../../src/kernel/engine.js";
import { buildApp } from "../../src/transport/http.js";
import type { FastifyInstance } from "fastify";

const testConfig: AppConfig = {
  host: "127.0.0.1",
  port: 0,
  dbPath: ":memory:",
  logLevel: "silent",
};

function makeApp(): {
  app: FastifyInstance;
  store: SqliteLedgerStore;
  kernel: Kernel;
  logger: CapturingLogger;
} {
  const store = new SqliteLedgerStore(":memory:");
  const logger = new CapturingLogger();
  const kernel = new Kernel({ store, logger });
  const app = buildApp({ config: testConfig, store, kernel, logger });
  return { app, store, kernel, logger };
}

describe("HTTP transport — JSON-RPC contract", () => {
  it("serves a single request with a concrete result", async () => {
    const { app } = makeApp();
    const res = await app.inject({
      method: "POST",
      url: "/",
      headers: { "content-type": "application/json" },
      payload: JSON.stringify({
        jsonrpc: "2.0",
        method: "math.add",
        params: { a: 21, b: 21 },
        id: 1,
      }),
    });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual({
      jsonrpc: "2.0",
      result: { sum: 42, addends: [21, 21] },
      id: 1,
    });
    await app.close();
  });

  it("returns 500 + parse_error for malformed JSON (not a framework 400)", async () => {
    const { app } = makeApp();
    const res = await app.inject({
      method: "POST",
      url: "/",
      headers: { "content-type": "application/json" },
      payload: "###",
    });
    expect(res.statusCode).toBe(500);
    expect(res.json()).toMatchObject({
      jsonrpc: "2.0",
      error: { code: -32700, data: { category: "parse_error" } },
      id: null,
    });
    await app.close();
  });

  it("returns 400 + invalid_request for an empty batch", async () => {
    const { app } = makeApp();
    const res = await app.inject({
      method: "POST",
      url: "/",
      headers: { "content-type": "application/json" },
      payload: "[]",
    });
    expect(res.statusCode).toBe(400);
    expect(res.json()).toMatchObject({
      error: { code: -32600, data: { category: "invalid_request" } },
      id: null,
    });
    await app.close();
  });

  it("returns 204 with an empty body for an all-notification batch", async () => {
    const { app } = makeApp();
    const res = await app.inject({
      method: "POST",
      url: "/",
      headers: { "content-type": "application/json" },
      payload: JSON.stringify([
        { jsonrpc: "2.0", method: "math.add", params: { a: 1, b: 1 } },
        { jsonrpc: "2.0", method: "echo", params: {} },
      ]),
    });
    expect(res.statusCode).toBe(204);
    expect(res.body).toBe("");
    await app.close();
  });

  it("attributes mixed batch responses correctly and omits notifications", async () => {
    const { app, store } = makeApp();
    const payload = [
      { jsonrpc: "2.0", method: "math.add", params: { a: 1, b: 1 }, id: "r1" },
      { jsonrpc: "2.0", method: "no.such.method", id: "r2" },
      { jsonrpc: "2.0", method: "secrets.put", params: { name: "n", secret: "s" } },
      42,
      { jsonrpc: "2.0", method: "echo", params: { echoed: [1, 2] }, id: null },
    ];
    const res = await app.inject({
      method: "POST",
      url: "/",
      headers: { "content-type": "application/json" },
      payload: JSON.stringify(payload),
    });
    expect(res.statusCode).toBe(200);
    const body = res.json() as Array<Record<string, unknown>>;
    expect(body).toHaveLength(4); // 3 request responses + 1 invalid element
    expect(body.map((r) => r.id)).toEqual(["r1", "r2", null, null]);

    const invalidElement = body[2]!;
    expect(invalidElement.error).toMatchObject({ code: -32600 });
    const methodMissing = body[1]!;
    expect(methodMissing.error).toMatchObject({ code: -32601 });

    // Notification executed (its side effect exists) but produced no response.
    const secretOps = store.listOperations({ kind: "secret_store" });
    expect(secretOps).toHaveLength(1);
    expect(secretOps[0]!.status).toBe("succeeded");
    await app.close();
  });
});

describe("HTTP transport — diagnostics", () => {
  it("explains why a malformed request was rejected, with correlation ids", async () => {
    const { app } = makeApp();
    const rejected = await app.inject({
      method: "POST",
      url: "/",
      headers: { "content-type": "application/json" },
      payload: "{",
    });
    const corr = (rejected.json().error.data as { correlationId: string })
      .correlationId;

    const detail = await app.inject(`/diag/requests/${corr}`);
    expect(detail.statusCode).toBe(200);
    const view = detail.json();
    expect(view.decision).toMatchObject({
      verdict: "rejected",
      layer: "parse",
      category: "parse_error",
    });
    expect(view.decision.rationale).toEqual(expect.any(String));

    const list = await app.inject("/diag/requests");
    expect(list.statusCode).toBe(200);
    expect(list.json().requests[0].requestCorr).toBe(corr);

    const missing = await app.inject("/diag/requests/req_does_not_exist");
    expect(missing.statusCode).toBe(404);
    await app.close();
  });

  it("filters operation records by status and kind", async () => {
    const { app } = makeApp();
    await app.inject({
      method: "POST",
      url: "/",
      headers: { "content-type": "application/json" },
      payload: JSON.stringify({
        jsonrpc: "2.0",
        method: "accounts.transfer",
        params: { from: "acc-1", to: "acc-2", amount: 25 },
        id: 1,
      }),
    });
    const res = await app.inject("/diag/operations?kind=transfer&status=succeeded");
    expect(res.statusCode).toBe(200);
    const ops = res.json().operations as Array<{ kind: string; status: string }>;
    expect(ops).toHaveLength(1);
    expect(ops[0]).toMatchObject({ kind: "transfer", status: "succeeded" });

    const none = await app.inject("/diag/operations?status=failed");
    expect(none.json().operations).toEqual([]);
    await app.close();
  });
});

describe("HTTP transport — real connection interruption", () => {
  let app: FastifyInstance;
  let store: SqliteLedgerStore;

  beforeAll(async () => {
    const made = makeApp();
    app = made.app;
    store = made.store;
    await app.listen({ host: "127.0.0.1", port: 0 });
  });

  afterAll(async () => {
    await app.close();
  });

  it("marks the request undecidable and in-flight work interrupted when the client vanishes", async () => {
    // Use a raw TCP socket rather than fetch/undici: we need to sever the
    // connection mid-processing without a client-side library emitting an
    // unrelated unhandled socket rejection.
    const body = JSON.stringify([
      // Fast side effect completes; slow one is in flight at disconnect time.
      {
        jsonrpc: "2.0",
        method: "accounts.transfer",
        params: { from: "acc-1", to: "acc-2", amount: 5 },
        id: "fast",
      },
      {
        jsonrpc: "2.0",
        method: "debug.sideEffect",
        params: { delayMs: 1000, note: "will be interrupted" },
        id: "slow",
      },
    ]);

    await new Promise<void>((resolve, reject) => {
      const port = (app.server.address() as { port: number }).port;
      const socket = net.connect(port, "127.0.0.1");
      socket.once("connect", () => {
        socket.write(
          `POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: ${Buffer.byteLength(body)}\r\nConnection: close\r\n\r\n${body}`,
        );
        // Let the server start the batch, then vanish without reading.
        setTimeout(() => {
          socket.destroy();
          resolve();
        }, 150);
      });
      socket.on("error", () => {
        // ECONNRESET/EPIPE are expected once we sever the socket.
      });
      socket.once("close", () => resolve());
      setTimeout(() => reject(new Error("socket test timed out")), 5000);
    });

    // Give the kernel a tick to finish its post-abort bookkeeping.
    await new Promise((resolve) => setTimeout(resolve, 50));

    // Undecidable request row exists.
    const requests = store.listRecentRequests(5);
    const undecidable = requests.find((r) => r.verdict === "undecidable");
    expect(undecidable).toBeDefined();
    expect(undecidable!.failureCategory).toBe("interrupted");
    expect(undecidable!.reason).toContain("interrupted");

    // The slow call/op are terminal-marked interrupted; the fast one is not.
    const calls = store.listCallsByRequest(undecidable!.requestCorr);
    expect(calls).toHaveLength(2);
    const byId = new Map(calls.map((c) => [c.rpcIdJson, c]));
    expect(byId.get('"fast"')!.status).toBe("success");
    expect(byId.get('"slow"')!.status).toBe("interrupted");

    const ops = store
      .listOperations({ limit: 500 })
      .filter((o) => o.requestCorr === undecidable!.requestCorr);
    const slowOp = ops.find((o) => o.kind === "debug_side_effect")!;
    expect(slowOp.status).toBe("interrupted");

    // After the abandoned handler would have finished, the interrupted
    // terminal state must not be overwritten by its late continuation.
    await new Promise((resolve) => setTimeout(resolve, 1100));
    expect(store.getOperation(slowOp.opSeq)!.status).toBe("interrupted");

    // The interrupted request is explained as such on the diagnostics API.
    const detail = await app.inject(
      `/diag/requests/${undecidable!.requestCorr}`,
    );
    const view = detail.json();
    expect(view.decision.verdict).toBe("undecidable");
    expect(view.calls.find((c: { rpcId: string }) => c.rpcId === "slow"))
      .toMatchObject({ status: "interrupted", isNotification: false });
  }, 20000);
});
