import { describe, expect, it } from "vitest";

import { redactValue, redactedJson, isSensitiveKey } from "../../src/util/redact.js";
import { SqliteLedgerStore } from "../../src/state/store.js";

describe("redaction", () => {
  it("masks allow-listed sensitive keys at any depth and keeps the rest", () => {
    const input = {
      name: "transfer",
      secret: "plaintext",
      nested: { apiKey: "k", ok: 1 },
      list: [{ password: "p" }, { safe: 2 }],
    };
    expect(redactValue(input)).toEqual({
      name: "transfer",
      secret: "***redacted***",
      nested: { apiKey: "***redacted***", ok: 1 },
      list: [{ password: "***redacted***" }, { safe: 2 }],
    });
  });

  it("does not mutate its input", () => {
    const input = { token: "abc" };
    redactValue(input);
    expect(input.token).toBe("abc");
  });

  it("matches key variants case-insensitively", () => {
    expect(isSensitiveKey("API_KEY")).toBe(true);
    expect(isSensitiveKey("apiKey")).toBe(true);
    expect(isSensitiveKey("privateKey")).toBe(true);
    expect(isSensitiveKey("username")).toBe(false);
  });

  it("redactedJson never contains the secret plaintext", () => {
    const out = redactedJson({ note: "x", secret: "topsecret" });
    expect(out).not.toContain("topsecret");
    expect(out).toContain("***redacted***");
  });
});

describe("SqliteLedgerStore", () => {
  function fresh(): SqliteLedgerStore {
    return new SqliteLedgerStore(":memory:");
  }

  it("records request, calls and operations with independent sequences", () => {
    const store = fresh();
    store.beginRequest({
      requestCorr: "req_1",
      receivedAt: "2026-01-01T00:00:00.000Z",
      payloadKind: "batch",
      sizeBytes: 10,
    });
    store.beginCall({
      callCorr: "call_1",
      requestCorr: "req_1",
      position: 0,
      rpcIdJson: "1",
      isNotification: false,
      method: "m",
      startedAt: "2026-01-01T00:00:00.000Z",
    });
    const seq1 = store.beginOperation({
      callCorr: "call_1",
      requestCorr: "req_1",
      rpcIdJson: "1",
      idempotencyKey: null,
      kind: "transfer",
      startedAt: "2026-01-01T00:00:00.000Z",
      redactedInput: "{}",
    });
    const seq2 = store.beginOperation({
      callCorr: "call_1",
      requestCorr: "req_1",
      rpcIdJson: "1",
      idempotencyKey: null,
      kind: "transfer",
      startedAt: "2026-01-01T00:00:01.000Z",
      redactedInput: "{}",
    });
    expect(seq2).toBeGreaterThan(seq1);
    expect(store.getOperation(seq1)!.opSeq).toBe(seq1);
  });

  it("conditional finishCall/finishOperation only changes pending rows", () => {
    const store = fresh();
    store.beginRequest({
      requestCorr: "req_1",
      receivedAt: "t",
      payloadKind: "single",
      sizeBytes: 1,
    });
    store.beginCall({
      callCorr: "call_1",
      requestCorr: "req_1",
      position: 0,
      rpcIdJson: null,
      isNotification: false,
      method: "m",
      startedAt: "t",
    });
    const opSeq = store.beginOperation({
      callCorr: "call_1",
      requestCorr: "req_1",
      rpcIdJson: null,
      idempotencyKey: null,
      kind: "k",
      startedAt: "t",
      redactedInput: null,
    });
    expect(
      store.finishOperation({
        opSeq,
        finishedAt: "t2",
        status: "succeeded",
      }),
    ).toBe(true);
    // A second finish (e.g. late continuation after interruption) is a no-op.
    expect(
      store.finishOperation({ opSeq, finishedAt: "t3", status: "failed" }),
    ).toBe(false);
    expect(store.getOperation(opSeq)!.status).toBe("succeeded");
  });

  it("interruptPending terminal-marks only pending rows", () => {
    const store = fresh();
    store.beginRequest({
      requestCorr: "req_1",
      receivedAt: "t",
      payloadKind: "batch",
      sizeBytes: 1,
    });
    for (let i = 0; i < 2; i++) {
      store.beginCall({
        callCorr: `call_${i}`,
        requestCorr: "req_1",
        position: i,
        rpcIdJson: `${i}`,
        isNotification: false,
        method: "m",
        startedAt: "t",
      });
    }
    store.finishCall({
      callCorr: "call_0",
      finishedAt: "t2",
      status: "success",
    });
    const slowOp = store.beginOperation({
      callCorr: "call_1",
      requestCorr: "req_1",
      rpcIdJson: "1",
      idempotencyKey: null,
      kind: "k",
      startedAt: "t",
      redactedInput: null,
    });
    const marks = store.interruptPending("req_1", "t3");
    expect(marks).toEqual({ calls: 1, operations: 1 });
    expect(store.getCall("call_0")!.status).toBe("success");
    expect(store.getCall("call_1")!.status).toBe("interrupted");
    expect(store.getOperation(slowOp)!.status).toBe("interrupted");
  });

  it("idempotency lookup ignores failed attempts but keeps them visible", () => {
    const store = fresh();
    store.beginRequest({
      requestCorr: "req_1",
      receivedAt: "t",
      payloadKind: "single",
      sizeBytes: 1,
    });
    store.beginCall({
      callCorr: "call_1",
      requestCorr: "req_1",
      position: 0,
      rpcIdJson: null,
      isNotification: false,
      method: "m",
      startedAt: "t",
    });
    const opSeq = store.beginOperation({
      callCorr: "call_1",
      requestCorr: "req_1",
      rpcIdJson: null,
      idempotencyKey: "key-x",
      kind: "transfer",
      startedAt: "t",
      redactedInput: null,
    });
    store.finishOperation({
      opSeq,
      finishedAt: "t2",
      status: "failed",
      errorCode: -32001,
      errorCategory: "business_rule",
      errorMessage: "x",
    });
    expect(store.findOperationByIdempotencyKey("key-x", "transfer")).toBeNull();
    expect(
      store.findLatestOperationByIdempotencyKey("key-x", "transfer")!.status,
    ).toBe("failed");
  });
});
